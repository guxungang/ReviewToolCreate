"""Reviewer mode: a draft of the venue form (scores, recommendation, editor note), validated by the kernel."""
import json
from pathlib import Path

import pytest

from scholarly_review.core.errors import SRError
from scholarly_review.reports import render
from tests.conftest import block_with, core_handlers, plan_and_start, understand_output

FORM = Path(__file__).resolve().parents[1] / "examples" / "rubrics" / "generic-ml-conference.json"


def _drive_to_assess(k, run_id, pv):
    h = core_handlers(k, pv, run_id)
    while True:
        r = k.claim(run_id)
        t = r["task"]
        if t is None:
            assert r.get("reason") == "final_assessment_added"
            continue
        if t["task_type"] == "understand":
            k.submit(t["task_token"], understand_output(k, pv))
        elif t["task_type"] == "numeric_extract":
            k.submit(t["task_token"], {"output": {"checks": [], "analyzed_block_ids": []}})
        elif t["check_type"] == "core.claim_evidence":
            sub = h["core.claim_evidence"](r)
            b = block_with(k, pv, "robust across all text classification domains")
            sub["output"]["evidence"].append({"key": "e2", "kind": "manuscript", "content_summary": "robustness claim",
                                              "source_refs": [{"block_id": b, "quote": "robust across all text classification domains"}],
                                              "method": "read abstract"})
            sub["output"]["findings"] = [{"key": "f1", "kind": "concern", "proposition": "The robustness claim covers all "
                                          "domains but only SST-B is evaluated.", "claim_ids": [], "evidence_keys": ["e2"],
                                          "potential_impact": "minor", "impact_rationale": "wording",
                                          "confidence_basis": "abstract", "issue_type": "scope",
                                          "neutral_question": "Which domains were evaluated?"}]
            k.submit(t["task_token"], sub)
        elif t["check_type"] == "core.novelty_positioning":
            k.submit(t["task_token"], h["core.novelty_positioning"](r))
        elif t["task_type"] == "assess":
            return r
        else:
            raise AssertionError(t)


def test_reviewer_form_draft_is_validated_and_kept_from_authors(kernel, paper):
    k = kernel
    rubric = {"source_file": str(FORM), "score_requested": True, "decision_requested": True,
              "submission_cutoff": "2026-05-01"}
    run_id, pv = plan_and_start(k, paper, mode="reviewer", effort="quick", rubric=rubric)
    r = _drive_to_assess(k, run_id, pv)
    ctx = json.loads(Path(r["files"]["context"]).read_text())
    assert [s["criterion_id"] for s in ctx["rubric"]["score_scales"]][:2] == ["soundness", "significance"]
    assert ctx["rubric"]["decision_options"][0] == "reject"
    assert not any(x.startswith("assess") for x in ctx["coverage"]["incomplete"])  # never counts itself
    fid = next(f["finding_id"] for f in ctx["admitted_findings"] if f["category"] == "confirmed_concern")

    bad = {"output": {"scores": [{"criterion_id": "soundness", "value": "5", "rationale": "x"},
                                 {"criterion_id": "novelty", "value": "3", "rationale": "x"}],
                      "recommendation": {"value": "reject", "rationale": "weak", "finding_ids": [fid]}}}
    with pytest.raises(SRError) as ei:
        k.submit(r["task"]["task_token"], bad)
    errs = " ".join(ei.value.details["errors"])
    assert "'5' is not one of" in errs and "unknown criterion_id 'novelty'" in errs
    assert "required criteria without a score" in errs
    assert "most negative option but no confirmed major" in errs  # only a minor concern stands

    r = k.claim(run_id)
    assert r["task"]["task_type"] == "assess"
    good = {"output": {"scores": [{"criterion_id": c, "value": "3", "rationale": "Claims hold on SST-B; scope wording.",
                                   "finding_ids": [fid]} for c in ("soundness", "significance", "originality")],
                       "recommendation": {"value": "weak_accept", "rationale": "Only a wording issue was confirmed.",
                                          "finding_ids": [fid]},
                       "editor_note": "Sound within SST-B; the robustness wording overreaches. Literature check was offline.",
                       "not_assessed": ["clarity: not part of this run's checks"]}}
    k.submit(r["task"]["task_token"], good)
    assert k.status(run_id)["state"] == "completed"

    editor = Path(render.export(k, run_id, "editor")["files"]["report.md"]).read_text()
    assert "weak_accept" in editor and "Confidential note to the editor" in editor
    private = Path(render.export(k, run_id, "reviewer_private")["files"]["report.md"]).read_text()
    assert "| soundness | 3 |" in private and "clarity: not part of" in private
    authors = Path(render.export(k, run_id, "authors")["files"]["report.md"]).read_text()
    assert "weak_accept" not in authors and "editor" not in authors.lower().split("disclaimer")[0]
    assert "Sound within SST-B" not in authors


def test_without_a_form_only_generic_decisions_never_scores(kernel, paper):
    k = kernel
    run_id, pv = plan_and_start(k, paper, mode="reviewer", effort="quick",
                                rubric={"score_requested": True, "submission_cutoff": "2026-05-01"})
    with k.ws.tx() as c:
        assert k._needs_assessment(c, k._run(run_id)) is False  # scales are never invented
    run2, _ = plan_and_start(k, paper, mode="reviewer", effort="quick",
                             rubric={"decision_requested": True, "submission_cutoff": "2026-05-01"})
    with k.ws.tx() as c:
        assert k._needs_assessment(c, k._run(run2)) is True  # same flow for every paper: generic options


def test_decision_semantics_follow_the_label_not_its_position():
    from scholarly_review.core.kernel import decision_options, decision_semantics
    assert decision_semantics("Reject/Resubmit (major revision and new external review required)") == "major"
    assert decision_semantics("Accept pending minor revision") == "minor"
    assert decision_semantics("Accept without changes") == "accept"
    assert decision_semantics("weak_reject") == "reject" and decision_semantics("borderline") == "major"
    opts, sem, src = decision_options({})
    assert opts == ["Reject", "Major revision", "Minor revision", "Accept"] and src.startswith("generic")
    assert [sem[o] for o in opts] == ["reject", "major", "minor", "accept"]
