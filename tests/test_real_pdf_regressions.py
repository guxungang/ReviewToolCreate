"""Regressions found on a real arXiv PDF (2026-11-22 trial)."""
from scholarly_review.modules import ingest, numeric


def _dm(text: str) -> dict:
    return {"blocks": [{"block_id": "b1", "text": text, "page_index": 1, "page_label": None}]}


def test_line_break_hyphen_in_compound_and_word():
    dm = _dm("Our model achieves 28.4 BLEU on the WMT 2014 English-\nto-German translation task, an exam-\nple.")
    assert ingest.resolve_quote(dm, "b1", "WMT 2014 English-to-German translation task")["match_status"] == "exact"
    assert ingest.resolve_quote(dm, "b1", "an example")["match_status"] == "exact"
    assert ingest.normalize("English-\nto-German") == "English-to-German"


def test_year_led_line_is_not_a_heading():
    assert not ingest._is_heading("2014 English-French dataset consisting of 36M sentences and split tokens into a 32000 word-piece")
    assert ingest._is_heading("3.2.1 Scaled Dot-Product Attention")
    assert ingest._is_heading("References")


def test_reference_mode_ends_at_non_citation_block():
    chunks = [("heading", "References"), ("paragraph", "[1] A. Author. A paper. 2016."),
              ("paragraph", "Attention Visualizations\n" + "\n".join(["tok"] * 20))]
    blocks = ingest._mk_blocks(chunks, 13, None, 0, {}, "good")
    assert [b["kind"] for b in blocks] == ["heading", "reference", "figure_text"]
    assert blocks[2]["extraction_quality"] == "low"


def test_bleu_rounding_inequality_product_ratio():
    r = numeric.run_check("rounding_compatible", {"displays": ["41.8", "41.0"], "unit": "raw"}, {"compatible": "true"})
    assert r["outputs"]["compatible"] is False and r["comparisons"][0]["status"] == "contradicted"
    r = numeric.run_check("change", {"old": 26.36, "new": 28.4, "unit": "raw"}, {"absolute_change": ">2.0"})
    assert r["comparisons"][0]["status"] == "supported"
    r = numeric.run_check("change", {"old": 26.36, "new": 28.3, "unit": "raw"}, {"absolute_change": ">2.0"})
    assert r["comparisons"][0]["status"] == "contradicted"
    r = numeric.run_check("product", {"values": [12, 8, 9.5e12], "constants": [3600]}, {"product": "3.3e18"})
    assert r["comparisons"][0]["status"] == "supported"
    r = numeric.run_check("ratio", {"numerator": 2.3e19, "denominator": 1.2e20}, {"ratio": "<0.25"})
    assert r["comparisons"][0]["status"] == "supported"
    found, _ = numeric.inputs_found_in_text({"values": [12, 8, 9.5e12], "constants": [3600]},
                                            "for a total of 100,000 steps or 12 hours on 8 NVIDIA P100 GPUs; 9.5 TFLOPS for P100")
    assert found


def test_doctor_missing_cli_is_unknown_not_unsupported(monkeypatch):
    from scholarly_review.adapters import hosts
    monkeypatch.setattr(hosts.shutil, "which", lambda _b: None)
    monkeypatch.delenv("CLAUDE_CODE_EXECPATH", raising=False)
    monkeypatch.delenv("CLAUDECODE", raising=False)
    r = hosts.probe("claude")
    assert r["binary"] is None and set(r["capabilities"].values()) == {"unknown"}
    monkeypatch.setenv("CLAUDECODE", "1")
    assert hosts.probe("claude")["capabilities"]["interactive_skill"] == "supported"


def test_zh_report_header_has_no_english_control_or_limits():
    from scholarly_review.reports import render
    assert render.tr("interactive mode: model usage and cost ...", "zh-CN").startswith("交互模式")
    assert render.tr("3 of 15 pages have no extractable text; x", "zh-CN").startswith("15 页中有 3 页")
    assert render.tr("unmapped message", "zh-CN") == "unmapped message"
    assert render.tr("interactive mode: model usage", "en") == "interactive mode: model usage"
    assert render._enf("tool_calls_only", "zh-CN") == "仅限内核签发的任务"


def test_review_line_numbers_and_running_header_removed():
    lines = [f"{n:03d}" for n in range(54, 108)] + ["Under review as a conference paper at ICLR 2027",
                                                   "Accuracy 12", "17", "body text."]
    kept, k = ingest._strip_line_numbers(lines)
    assert k == 54 and kept[0].startswith("Under review") and "17" in kept
    pages = [["Under review as a conference paper at ICLR 2027", f"text {i}"] for i in range(5)]
    assert ingest._running_lines(pages) == {"Under review as a conference paper at ICLR 2027"}


def test_input_rounding_is_propagated():
    # 66.79 - 91.47 = -24.68 exactly, but the paper's rounded inputs are compatible with a printed -24.69
    r = numeric.run_check("change", {"old": "91.47", "new": "66.79", "unit": "percent"}, {"pp_change": "-24.69"})
    assert r["comparisons"][0]["status"] == "supported"
    r = numeric.run_check("change", {"old": "91.47", "new": "66.79", "unit": "percent"}, {"pp_change": "-24.80"})
    assert r["comparisons"][0]["status"] == "contradicted"
    r = numeric.run_check("change", {"old": 26.36, "new": 28.3, "unit": "raw"}, {"absolute_change": ">2.0"})
    assert r["comparisons"][0]["status"] == "contradicted"


def test_string_inputs_are_source_checked():
    assert not numeric.inputs_found_in_text({"old": "91.47", "new": "99.99", "unit": "percent"}, "Base 91.47 R 8.21")[0]
    assert numeric.inputs_found_in_text({"old": "91.47", "new": "8.21", "unit": "percent"}, "Base 91.47 R 8.21")[0]


def test_reason_codes_deduplicated_and_localised():
    from scholarly_review.reports import render
    assert render._code("TEMPORAL_UNCERTAIN", "zh-CN").startswith("文献与投稿")
    assert render._code("DUPLICATE_OF_fnd_x", "zh-CN") == "与另一条发现重复"
    assert render._code("TEMPORAL_UNCERTAIN", "en") == "TEMPORAL_UNCERTAIN"


def test_plan_from_paper_version_without_manuscript(kernel, paper):
    pv = kernel.import_paper(paper)["paper_version_id"]
    out = kernel.plan({"mode": "author", "effort": "quick"}, None, pv)
    assert out["run_id"].startswith("run_")
