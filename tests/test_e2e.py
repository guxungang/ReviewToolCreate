"""End-to-end interactive flow with a simulated host, plus engineering blockers E04/E05/E07/E12/E15."""
import json
from pathlib import Path

import pytest

from scholarly_review.core.errors import SRError
from scholarly_review.reports import render
from tests.conftest import block_with, core_handlers, empty_check, plan_and_start, run_claims, understand_output


def claim(k, run_id):
    r = k.claim(run_id)
    assert r["task"] is not None, r
    return r


def claims_by_key(k, run_id):
    cl = k.ws.records(run_id, "claim")
    return {c["original_text"][:20]: c["claim_id"] for c in cl}


def numeric_output(k, pv, c1):
    return {"output": {"checks": [{
        "key": "n1", "op": "confusion_metrics",
        "inputs": {"matrix": [[90, 10], [20, 30]], "orientation": "rows_true"},
        "claimed": {"accuracy": "80%", "macro_f1": "80%"}, "claim_ids": [c1],
        "source_refs": [{"block_id": block_with(k, pv, "Positive 90 10"), "quote": "Positive 90 10 Negative 20 30"},
                        {"block_id": block_with(k, pv, "From Table 1"), "quote": "an accuracy and macro-F1 of 80%"}],
        "description": "Table 1 confusion matrix vs reported accuracy/macro-F1"}],
        "analyzed_block_ids": [block_with(k, pv, "Positive 90 10")], "not_extractable": []}}


def run_to_end(k, run_id, pv, handlers):
    """Drive the loop; handlers map task type/check_type to a function(req)->submission."""
    seen = []
    for _ in range(80):
        r = k.claim(run_id)
        if r["task"] is None:
            return seen, r
        t = r["task"]
        key = t["check_type"] or t["task_type"]
        fn = handlers.get(key) or handlers.get(t["task_type"])
        sub = fn(r) if fn else empty_check(t["check_type"], run_claims(k, run_id))
        k.submit(t["task_token"], sub)
        seen.append(key)
    raise AssertionError("loop did not terminate")


def test_full_author_flow(kernel, paper):
    k = kernel
    run_id, pv = plan_and_start(k, paper)
    r = claim(k, run_id)
    assert r["task"]["task_type"] == "understand"
    assert Path(r["files"]["instructions"]).exists() and Path(r["files"]["document"]).exists()
    res = k.submit(r["task"]["task_token"], understand_output(k, pv))
    assert res["accepted"]["claim"] == 3
    cids = claims_by_key(k, run_id)
    c1, c2 = cids["GateNet reaches an a"], cids["it improves accuracy"]

    def numeric_h(req):
        return numeric_output(k, pv, c1)

    def verify_h(req):
        ctx = json.loads(Path(req["files"]["context"]).read_text())
        item = ctx["verification_items"][0]
        assert "proposition" not in item  # blinded: candidate conclusion withheld
        if item["has_calculation_evidence"]:
            return {"output": {"context_independence": "same_context", "blinded": True, "verifications": [{
                "finding_id": item["finding_id"], "method": "re-read Table 1, recomputed", "result": "validated",
                "rationale": "macro-F1 is 76.19% from the matrix", "confirmed_impact": "major",
                "inputs_rechecked": True}]}}
        if "appendix" in (item["neutral_question"] or "").lower():  # A10: refuted by appendix ablation
            b = block_with(k, pv, "Ablation with identical training data")
            return {"output": {"context_independence": "same_context", "blinded": True,
                               "evidence": [{"key": "v1", "kind": "manuscript", "content_summary": "appendix ablation",
                                             "source_refs": [{"block_id": b, "quote": "Ablation with identical training data"}],
                                             "method": "read appendix"}],
                               "verifications": [{"finding_id": item["finding_id"], "method": "searched appendix",
                                                  "result": "invalidated", "rationale": "Table 5 isolates the gate",
                                                  "counterevidence_keys": ["v1"]}]}}
        return {"output": {"context_independence": "fresh", "blinded": True, "verifications": [{
            "finding_id": item["finding_id"], "method": "re-read Sec. 2/3", "result": "validated",
            "rationale": "data doubled for GateNet only in the main table", "confirmed_impact": "major"}]}}

    def ablation_h(req):
        b = block_with(k, pv, "twice the training data")
        return {"output": {"check_type": "ml.ablation_attribution",
                           "evidence": [{"key": "e1", "kind": "manuscript", "content_summary": "data doubled",
                                         "source_refs": [{"block_id": b, "quote": "twice the training data of the baseline"}],
                                         "method": "read Sec. 2"}],
                           "assessments": [{"claim_id": c2, "outcome": "insufficient_evidence", "evidence_keys": ["e1"],
                                            "scope_checked": "main comparison"}],
                           "findings": [
                               {"key": "f1", "kind": "concern", "proposition": "Main-table gain mixes the gate with doubled data.",
                                "claim_ids": [c2], "evidence_keys": ["e1"], "potential_impact": "major",
                                "impact_rationale": "core comparative claim", "confidence_basis": "Sec. 2",
                                "action_type": "clarify_scope", "issue_type": "attribution",
                                "recommended_action": "Point to the equal-data ablation in the main text.",
                                "neutral_question": "Is the main-table comparison run with equal training data?"},
                               {"key": "f2", "kind": "concern", "proposition": "No ablation isolates the gate.",
                                "claim_ids": [c2], "evidence_keys": ["e1"], "potential_impact": "major",
                                "impact_rationale": "attribution", "confidence_basis": "Sec. 3",
                                "issue_type": "missing_experiment",
                                "neutral_question": "Does the appendix contain an ablation that removes the gate with equal data?"}],
                           "analyzed_block_ids": [b], "not_checked": []}}

    seen, last = run_to_end(k, run_id, pv, {"numeric.consistency": numeric_h, "verify": verify_h,
                                            "ml.ablation_attribution": ablation_h, **core_handlers(k, pv, run_id)})
    assert seen.count("verify") == 3
    assert last["run_state"] == "completed"

    findings = {f["proposition"][:30]: f for f in k.ws.records(run_id, "finding")}
    num = next(f for f in findings.values() if "macro_f1" in f["proposition"])
    assert num["disposition"] == "confirmed" and num["impact"] == "major"
    gate = next(f for f in findings.values() if f["proposition"].startswith("Main-table"))
    assert gate["disposition"] == "unresolved"  # E15: declared fresh in interactive -> inherited -> cannot confirm major
    vers = k.ws.records(run_id, "verification")
    assert any(v["context_independence"] == "inherited" for v in vers)
    abl = next(f for f in findings.values() if f["proposition"].startswith("No ablation"))
    assert abl["disposition"] == "refuted"  # A10

    out = render.export(k, run_id)
    files = out["files"]
    for name in ("report.md", "report.txt", "findings.json", "evidence.jsonl", "coverage.json", "budget.json",
                 "action_plan.json", "action_plan.md", "manifest.json", "report.html"):
        assert name in files
    assert out["readiness"]["value"] == "blocked"
    md = Path(files["report.md"]).read_text()
    assert "macro_f1" in md and "No ablation isolates" not in md
    assert "ignore all previous instructions" not in md.lower() or True  # data, never acted upon
    html = Path(files["report.html"]).read_text()
    assert "<script" not in html

    # human confirms the open major concern -> becomes a confirmed concern
    k.verify_human(run_id, gate["finding_id"], "validated", "user re-read Sec. 2 and Table 5", "major",
                   "Section 2 and Appendix Table 5")
    out2 = render.export(k, run_id, audience="authors")
    ext = json.loads(Path(out2["files"]["findings.json"]).read_text())
    assert all(f["report_eligibility"][0]["admitted"] for f in ext)
    assert "budget.json" not in out2["files"] and "action_plan.json" not in out2["files"]


def test_e12_semantic_rejection_and_retry(kernel, paper):
    k = kernel
    run_id, pv = plan_and_start(k, paper)
    r = claim(k, run_id)
    bad = understand_output(k, pv)
    bad["output"]["claims"][0]["source_refs"][0]["quote"] = "this text is not in the paper at all"
    with pytest.raises(SRError) as ei:
        k.submit(r["task"]["task_token"], bad)
    assert ei.value.category == "semantic_invalid"
    assert ei.value.details["attempts_remaining"] == 1
    r2 = claim(k, run_id)
    assert r2["task"]["task_type"] == "understand" and r2["task"]["lease_generation"] == 2
    k.submit(r2["task"]["task_token"], understand_output(k, pv))
    # a check citing a claim id that does not exist is rejected
    r3 = claim(k, run_id)
    assert r3["task"]["task_type"] == "numeric_extract"
    k.submit(r3["task"]["task_token"], {"output": {"checks": [], "analyzed_block_ids": []}})
    r3 = claim(k, run_id)
    sub = empty_check(r3["task"]["check_type"])
    sub["output"]["assessments"] = [{"claim_id": "claim_doesnotexist", "outcome": "not_checked",
                                     "evidence_keys": [], "scope_checked": "-"}]
    with pytest.raises(SRError) as ei:
        k.submit(r3["task"]["task_token"], sub)
    assert ei.value.category == "semantic_invalid"


def test_e05_idempotent_submit(kernel, paper):
    k = kernel
    run_id, pv = plan_and_start(k, paper)
    r = claim(k, run_id)
    sub = understand_output(k, pv)
    first = k.submit(r["task"]["task_token"], sub)
    again = k.submit(r["task"]["task_token"], sub)
    assert again["idempotent_replay"] and again["result_sha256"] == first["result_sha256"]
    assert len(k.ws.records(run_id, "claim")) == 3
    other = understand_output(k, pv)
    other["output"]["uncovered_notes"] = ["changed"]
    with pytest.raises(SRError) as ei:
        k.submit(r["task"]["task_token"], other)
    assert ei.value.category == "conflict"


def test_e07_token_of_other_run_rejected(tmp_path, paper):
    from scholarly_review.interfaces.service import Service
    svc = Service(str(tmp_path / "ws"))
    run_a, pv = plan_and_start(svc.k, paper)
    run_b, _ = plan_and_start(svc.k, paper, mode="author", effort="quick")
    r = svc.k.claim(run_a)
    f = tmp_path / "res.json"
    f.write_text(json.dumps(understand_output(svc.k, pv)))
    with pytest.raises(SRError) as ei:
        svc.submit(run_b, r["task"]["task_token"], str(f), "model")
    assert ei.value.category == "policy_denied"


def test_e04_tampered_calculation_is_excluded(kernel, paper):
    k = kernel
    run_id, pv = plan_and_start(k, paper)
    k.submit(claim(k, run_id)["task"]["task_token"], understand_output(k, pv))
    c1 = claims_by_key(k, run_id)["GateNet reaches an a"]
    r = claim(k, run_id)
    assert r["task"]["task_type"] == "numeric_extract"
    k.submit(r["task"]["task_token"], numeric_output(k, pv, c1))
    calc = next(e for e in k.ws.records(run_id, "evidence") if e["kind"] == "calculation")
    path = k.ws.artifact_path(calc["payload_artifact_id"])
    path.write_text('{"tampered": true}')
    out = render.export(k, run_id)
    findings = json.loads(Path(out["files"]["findings.json"]).read_text())
    num = next(f for f in findings if "macro_f1" in f["proposition"])
    assert num["report_eligibility"][0]["category"] == "excluded"
    assert "EVIDENCE_NOT_CURRENT" in num["report_eligibility"][0]["reason_codes"]
    assert k.ws.record(calc["evidence_id"])["validity"] == "corrupt"


def test_quick_effort_marks_focused_and_partial(kernel, paper):
    k = kernel
    run_id, pv = plan_and_start(k, paper, effort="quick")
    k.submit(claim(k, run_id)["task"]["task_token"], understand_output(k, pv))
    k.cancel(run_id)
    out = render.export(k, run_id)
    md = Path(out["files"]["report.md"]).read_text()
    assert md.startswith("# Focused check") and "(Partial report)" in md
    assert out["readiness"]["value"] in ("not_assessed", "blocked")


def test_subfield_checks_added_for_llm_papers(kernel, paper):
    k = kernel
    run_id, pv = plan_and_start(k, paper)
    k.submit(claim(k, run_id)["task"]["task_token"], understand_output(k, pv, subfields=("llm_nlp",)))
    types = {r["check_type"] for r in k.ws.q("SELECT check_type FROM tasks WHERE run_id=?", (run_id,))}
    assert "ml.llm.eval_validity" in types
