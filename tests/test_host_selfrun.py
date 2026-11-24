"""Regressions from the first unattended /paper-preflight run in a fresh host session (2026-11-23)."""
import json
import urllib.request

from tests.conftest import plan_and_start, understand_output


def _set_active(k, run_id, seconds):
    with k.ws.tx() as c:
        c.execute("UPDATE runs SET active_seconds=?, running_since=NULL WHERE run_id=?", (seconds, run_id))



def test_quick_is_focused_and_fits_host_time(kernel, paper):
    out = kernel.plan({"input": {"manuscript": paper}, "mode": "author", "effort": "quick"})
    types = sorted((t["type"], t["check_type"] or "") for t in out["plan_summary"]["planned_tasks"])
    assert types == [("check", "core.claim_evidence"), ("literature", "core.novelty_positioning"),
                     ("numeric_extract", "numeric.consistency"), ("understand", "")]
    assert out["plan_summary"]["time_fit"]["fits"] is True
    assert any("focused checks only" in w for w in out["warnings"])
    out = kernel.plan({"input": {"manuscript": paper}, "mode": "author", "effort": "quick",
                       "budget": {"max_active_seconds": 300}})
    assert out["plan_summary"]["time_fit"]["fits"] is False
    assert any("expect a partial report" in w for w in out["warnings"])
    out = kernel.plan({"input": {"manuscript": paper}, "mode": "author", "effort": "standard"})
    assert out["plan_summary"]["time_fit"]["fits"] is True
    assert len(out["plan_summary"]["planned_tasks"]) > 4


def test_time_phase_stop_is_explained_and_resumable(kernel, paper):
    run_id, pv = plan_and_start(kernel, paper, effort="quick")
    first = kernel.claim(run_id)
    kernel.submit(first["task"]["task_token"], understand_output(kernel, pv), "host")
    _set_active(kernel, run_id, 430)  # past the 70 % analysis phase of 600 s
    res = kernel.claim(run_id)
    assert res["task"] is None and res["run_state"] == "partial"
    assert any("analysis phase cap 420s" in r for r in res["budget_stops"])
    assert kernel.status(run_id)["budget_stops"]
    kernel.revise_budget(run_id, {"max_active_seconds": 1800}, "user agreed to 30 min")
    kernel.resume(run_id)
    _set_active(kernel, run_id, 430)
    assert kernel.claim(run_id)["task"] is not None


def test_analysis_tasks_are_claimed_before_verification(kernel, paper):
    run_id, pv = plan_and_start(kernel, paper, effort="quick")
    first = kernel.claim(run_id)
    kernel.submit(first["task"]["task_token"], understand_output(kernel, pv), "host")
    with kernel.ws.tx() as c:  # a verification task created early must not jump ahead of analysis
        kernel._add_task(c, run_id, "verify", None, priority=0, required=False)
    nxt = kernel.claim(run_id)
    assert nxt["task"]["task_type"] != "verify"


def test_retry_of_started_analysis_task_may_use_verification_window(kernel, paper):
    run_id, pv = plan_and_start(kernel, paper, effort="quick")
    first = kernel.claim(run_id)
    kernel.submit(first["task"]["task_token"], understand_output(kernel, pv), "host")
    nxt = kernel.claim(run_id)
    tid = nxt["task"]["task_id"]
    with kernel.ws.tx() as c:  # simulate a rejected first submission
        c.execute("UPDATE attempts SET state='failed' WHERE task_id=?", (tid,))
        c.execute("UPDATE tasks SET state='ready' WHERE task_id=?", (tid,))
    _set_active(kernel, run_id, 450)
    again = kernel.claim(run_id)
    assert again["task"] is not None and again["task"]["task_id"] == tid


def test_rate_limit_reports_retry_after(monkeypatch):
    import email.message
    import urllib.error
    import pytest
    from scholarly_review.core.errors import SRError
    from scholarly_review.modules import literature

    def boom(self, req, timeout=None):
        h = email.message.Message(); h["Retry-After"] = "36"
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many", h, None)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", boom)
    with pytest.raises(SRError) as ei:
        literature.http_get("https://api.openalex.org/works?search=x", {"api.openalex.org"}, 1000)
    assert ei.value.code == "provider_rate_limited" or "rate limited" in ei.value.message
    assert "retry after 36 s" in ei.value.message


def _f(fid, prop, claims, issue="consistency", kind="concern", disp="confirmed"):
    return {"finding_id": fid, "proposition": prop, "claim_ids": claims, "kind": kind, "disposition": disp,
            "extensions": {"issue_type": issue,
                           "check_type": "numeric.consistency" if issue == "numeric" else "core.claim_evidence"},
            "duplicate_of": None}


def test_wording_finding_restating_numeric_finding_is_deduplicated():
    from scholarly_review.reports.snapshot import mark_cross_module_duplicates
    num = _f("fn", "同一数值不一致：英法 BLEU 摘要和表 2 为 41.8，第 6.1 节正文为 41.0", ["c1", "c2"], "numeric")
    num["recommended_action"] = "统一各处数值。"
    dup = _f("fw", "英法 BLEU 在摘要中为 41.8，在第 6.1 节中为 41.0；41.0 低于 ConvS2S 集成的 41.29。",
             ["c1", "c3"], disp="unresolved")
    dup["recommended_action"] = "核对英法结果；如只对单模型成立，结论加“单模型”限定。"
    other = _f("fo", "英法相对最强集成的差距约 0.5 BLEU，未报告方差。", ["c1"])
    unrelated_claim = _f("fu", "Table 5 lists 41.8 and 41.0 for a different model.", ["c9"])
    strength = _f("fs", "41.8 and 41.0 both reported", ["c1"], kind="strength")
    out = {f["finding_id"]: f for f in mark_cross_module_duplicates([num, dup, other, unrelated_claim, strength])}
    assert out["fw"]["duplicate_of"] == "fn"
    assert out["fn"]["duplicate_of"] is None
    assert out["fn"]["recommended_action"].startswith("核对英法结果")
    assert out["fo"]["duplicate_of"] is None and out["fu"]["duplicate_of"] is None
    assert out["fs"]["duplicate_of"] is None
    assert dup["duplicate_of"] is None  # stored record untouched


def test_restatement_labelled_numeric_by_a_check_is_still_deduplicated():
    from scholarly_review.reports.snapshot import mark_cross_module_duplicates
    num = _f("fn", "BLEU inconsistent: 41.8 in the abstract, 41.0 in Sec. 6.1", ["c1"], "numeric")
    restated = _f("fw", "EN-FR BLEU is 41.8 in Table 2 but 41.0 in the text.", ["c1"], "numeric")
    restated["extensions"]["check_type"] = "core.claim_evidence"  # a check labelled its own finding numeric
    out = {f["finding_id"]: f for f in mark_cross_module_duplicates([num, restated])}
    assert out["fw"]["duplicate_of"] == "fn" and out["fn"]["duplicate_of"] is None


# ---- numeric false positives seen on real manuscripts (2026-11-23)
from scholarly_review.modules import numeric as _num


def test_percent_display_of_a_ratio_is_not_a_unit_mismatch():
    r = _num.run_check("ratio", {"numerator": "3242484", "denominator": "3479884"}, {"ratio": "93.18%"})
    assert r["comparisons"][0]["status"] == "supported"
    r = _num.run_check("ratio", {"numerator": "116317", "denominator": "3479884"}, {"ratio": "4.34%"})
    assert r["comparisons"][0]["status"] == "contradicted"  # a real error is still caught
    r = _num.run_check("ratio", {"numerator": "0.0157", "denominator": "0.0633"}, {"ratio": "24.8%"})
    assert r["comparisons"][0]["status"] == "supported"
    r = _num.run_check("ratio", {"numerator": "9.62", "denominator": "3.67"}, {"ratio": "2.62"})
    assert r["comparisons"][0]["status"] == "supported"  # ratios > 1 without % stay raw


def test_trailing_zero_inputs_are_found_in_the_text():
    ok, missing = _num.inputs_found_in_text({"a": 0.958, "b": 0.9486}, "F1 0.9580 vs ResUNet-LC 0.9486")
    assert ok, missing
    ok, _ = _num.inputs_found_in_text({"a": 12}, "trained for 12.0 hours")
    assert ok
    ok, missing = _num.inputs_found_in_text({"a": 12}, "trained for 120 hours")
    assert not ok and missing == [12.0]


def test_macro_f1_as_f1_of_macro_precision_and_recall_is_accepted():
    m = [[646894, 291, 132, 1308], [984, 22295, 6, 8], [228, 7, 16266, 26], [1462, 3, 31, 6613]]
    r = _num.run_check("confusion_metrics", {"matrix": m, "orientation": "rows_true"},
                       {"macro_f1": "0.9447", "macro_precision": "0.9509", "macro_recall": "0.9386"})
    st = {c["metric"]: c["status"] for c in r["comparisons"]}
    assert st == {"macro_f1": "supported", "macro_precision": "supported", "macro_recall": "supported"}, r
    mf1 = next(c for c in r["comparisons"] if c["metric"] == "macro_f1")
    assert "alternative definition" in mf1["reason"]
    r = _num.run_check("confusion_metrics", {"matrix": m, "orientation": "rows_true"}, {"macro_f1": "0.9547"})
    assert r["comparisons"][0]["status"] == "contradicted"


def test_mean_with_negative_inputs_propagates_rounding_and_small_inputs_match():
    vals = ["-0.0023", "0.0044", "0.0006", "0.0038", "0.0070", "0.0280", "0.0027", "-0.0029"]
    r = _num.run_check("mean", {"values": vals}, {"mean": "0.005153"})
    assert r["comparisons"][0]["status"] == "supported", r["comparisons"]
    ok, missing = _num.inputs_found_in_text({"values": [3.6e-05, -0.000866]}, "+0.000036 and -0.000866")
    assert ok, missing


# ---- public-paper evaluation (2026-11-24): BatchNorm 1502.03167, ResNet 1512.03385 with planted defects

def test_pdf_flattened_powers_of_ten_are_parsed():
    from scholarly_review.modules import numeric as n
    # pypdf turns "31.0·10^6" into "31.0 · 106"; the planted 3.1M (true 2.1M) must contradict "14 times"
    r = n.run_check("ratio", {"numerator": "31.0 · 106", "denominator": "3.1 · 106"}, {"ratio": "14"})
    assert r["error"] is None and r["comparisons"][0]["status"] == "contradicted"
    r = n.run_check("ratio", {"numerator": "31.0 · 106", "denominator": "2.1 · 106"}, {"ratio": "14"})
    assert r["comparisons"][0]["status"] == "supported"
    assert n.inputs_found_in_text({"numerator": "31.0 · 106", "denominator": "3.1 · 106"},
                                  "Inception 31.0 · 106 72.2% BN-x5 3.1 · 106 73.0%") == (True, [])
    assert n.sci_to_decimal_str("3.3×10¹⁸") == "3.3E18" and n.sci_to_decimal_str("5 · 10^-3") == "5E-3"
    assert n.sci_to_decimal_str("2 x 106") is None  # a literal "x" is not read as a power of ten
    lo, hi, _ = n.parse_display("3.1 · 106", "raw")
    assert abs(lo - 3.05e6) < 1 and abs(hi - 3.15e6) < 1
    # the missed learning-rate defect, checked the way the prompt now asks
    r = n.run_check("ratio", {"numerator": "0.060", "denominator": "0.0015"}, {"ratio": "30"})
    assert r["comparisons"][0]["status"] == "contradicted"


def _claim_until(k, run_id, check_type):
    while True:
        r = k.claim(run_id)
        t = r["task"]
        assert t is not None
        if t["check_type"] == check_type:
            return t
        if t["task_type"] == "numeric_extract":
            k.submit(t["task_token"], {"output": {"checks": [], "analyzed_block_ids": []}})
        else:
            k.submit(t["task_token"], {"output": {"check_type": t["check_type"], "evidence": [], "assessments": [],
                                                  "findings": [], "analyzed_block_ids": []}})


def test_claim_evidence_must_cover_every_core_claim_once(kernel, paper):
    import pytest
    from scholarly_review.core.errors import SRError
    from tests.conftest import run_claims
    k = kernel
    run_id, pv = plan_and_start(k, paper, effort="quick")
    r = k.claim(run_id)
    k.submit(r["task"]["task_token"], understand_output(k, pv))
    core = sorted(c["claim_id"] for c in run_claims(k, run_id) if c["importance"] == "core")
    t = _claim_until(k, run_id, "core.claim_evidence")
    partial = {"output": {"check_type": "core.claim_evidence", "evidence": [], "findings": [],
                          "assessments": [{"claim_id": core[0], "outcome": "not_checked", "evidence_keys": [],
                                           "scope_checked": "-"}], "analyzed_block_ids": []}}
    with pytest.raises(SRError) as ei:
        k.submit(t["task_token"], partial)
    assert "every core claim needs an assessment" in json.dumps(ei.value.details)
    # the retry leaves them out again: the kernel records them as not checked instead of looping
    t = _claim_until(k, run_id, "core.claim_evidence")
    k.submit(t["task_token"], partial)
    asm = [a for a in k.ws.records(run_id, "assessment") if a["check_type"] == "core.claim_evidence"]
    assert sorted(a["claim_id"] for a in asm) == core
    assert any("left this core claim out" in " ".join(a["limitations"]) for a in asm)


def test_scope_notes_do_not_make_a_task_partial_but_real_gaps_do(kernel, paper):
    """Public-paper runs: every task listed some `not_checked` note and ended `partial`, so the report
    looked incomplete even where the task had done its job."""
    from tests.conftest import core_handlers, run_claims
    k = kernel
    run_id, pv = plan_and_start(k, paper, effort="quick")
    r = k.claim(run_id)
    k.submit(r["task"]["task_token"], understand_output(k, pv))
    h = core_handlers(k, pv, run_id)
    states = {}
    while True:
        r = k.claim(run_id)
        t = r["task"]
        if t is None:
            break
        if t["task_type"] == "numeric_extract":
            sub = {"output": {"checks": [], "analyzed_block_ids": [], "not_extractable": ["Fig. 2 only as an image"]}}
        elif t["check_type"] == "core.claim_evidence":
            sub = h["core.claim_evidence"](r)
            sub["output"]["not_checked"] = ["robustness claim beyond SST-B not examined in depth"]
        elif t["check_type"] == "core.novelty_positioning":
            # no search was run at all: that is the literature task's own job, so it stays partial
            sub = {"output": {"check_type": t["check_type"], "evidence": [], "assessments": [], "findings": [],
                              "analyzed_block_ids": [], "not_checked": ["searches unavailable"]}}
        else:
            raise AssertionError(t)
        res = k.submit(t["task_token"], sub)
        states[t["check_type"] or t["task_type"]] = (res["execution_status"], res["incomplete_reasons"], res["scope_notes"])
    assert states["numeric.consistency"][0] == "completed" and states["numeric.consistency"][2]
    assert states["core.claim_evidence"][0] == "completed"
    assert states["core.novelty_positioning"] == ("partial", ["no literature search succeeded"], ["searches unavailable"])
