"""Admission rules (spec §9.5), readiness, and literature gating (E08, E16, A08)."""
import json

import pytest

from scholarly_review.core import litops
from scholarly_review.core.admission import admit_finding, readiness
from scholarly_review.core.errors import SRError
from scholarly_review.modules import literature
from tests.conftest import plan_and_start, understand_output

PV = "pv_1"


def ev(eid, kind="manuscript", status="exact", audiences=("author_private", "authors"), payload=None, ext=None):
    refs = []
    if kind in ("manuscript", "supplement"):
        refs = [{"artifact_id": "a", "quote": "q", "locator": {"type": "document", "block_id": "b1", "page_index": 1,
                                                               "page_label": None, "bbox": None, "text_span": None,
                                                               "match_status": status}}]
    if kind == "literature":
        refs = [{"artifact_id": "external", "quote": None, "locator": ext}]
    return {"evidence_id": eid, "kind": kind, "paper_version_id": PV, "validity": "current", "source_refs": refs,
            "payload_artifact_id": payload, "disclosure": {"allowed_audiences": list(audiences)}}


def fnd(kind="concern", disposition="confirmed", impact="minor", evs=("e1",), vids=(), ext=None):
    return {"finding_id": "f1", "kind": kind, "paper_version_id": PV, "validity": "current", "disposition": disposition,
            "impact": impact, "potential_impact": impact, "evidence_ids": list(evs), "verification_ids": list(vids),
            "claim_ids": ["c1"], "duplicate_of": None, "extensions": ext or {}}


def ver(ctx="same_context", result="validated", method="re-read"):
    return {"v1": {"result": result, "context_independence": ctx, "method": method}}


def admit(f, evidence, verifications=None, audience="author_private", **kw):
    return admit_finding(f, evidence_by_id={e["evidence_id"]: e for e in evidence},
                         verifications_by_id=verifications or {}, paper_version_id=PV, audience=audience, **kw)


def test_minor_confirmed_concern_admitted():
    assert admit(fnd(), [ev("e1")])["category"] == "confirmed_concern"


def test_major_same_context_needs_more():
    e = admit(fnd(impact="major", vids=["v1"]), [ev("e1")], ver("same_context"))
    assert e["category"] == "question" and "INSUFFICIENT_VERIFICATION_FOR_IMPACT" in e["reason_codes"]
    assert admit(fnd(impact="major", vids=["v1"]), [ev("e1")], ver("human"))["category"] == "confirmed_concern"
    calc = ev("e2", kind="calculation", payload="art_1")
    assert admit(fnd(impact="major", evs=["e2"], vids=["v1"]), [calc],
                 ver("same_context", method="deterministic recomputation"))["category"] == "confirmed_concern"


@pytest.mark.parametrize("disp", ["refuted", "dismissed"])
def test_refuted_excluded(disp):
    assert admit(fnd(disposition=disp), [ev("e1")])["category"] == "excluded"


def test_stale_and_missing_evidence_excluded():
    f = fnd()
    f["validity"] = "stale"
    assert admit(f, [ev("e1")])["category"] == "excluded"
    assert "EVIDENCE_MISSING" in admit(fnd(evs=["nope"]), [ev("e1")])["reason_codes"]


def test_unresolved_concern_becomes_question_and_ambiguous_anchor_downgrades():
    assert admit(fnd(disposition="unresolved"), [ev("e1")])["category"] == "question"
    assert admit(fnd(), [ev("e1", status="ambiguous")])["category"] == "question"


def test_audience_filter():
    e = admit(fnd(), [ev("e1", audiences=("author_private",))], audience="public")
    assert e["category"] == "excluded" and "AUDIENCE_NOT_ALLOWED" in e["reason_codes"]


def test_a08_temporal_and_content_rules():
    loc_after = {"type": "external", "url": None, "source_id": "s_june", "version_id": None, "section_hint": None,
                 "access_level": "fulltext"}
    loc_abs = dict(loc_after, source_id="s_april", access_level="abstract")
    sources = {"s_june": {"first_public_date": "2026-06-10"}, "s_april": {"first_public_date": "2026-04-02"}}
    f = fnd(evs=["l1"], ext={"issue_type": "novelty"})
    e = admit(f, [ev("l1", kind="literature", ext=loc_after)], sources_by_id=sources, submission_cutoff="2026-05-01")
    assert e["category"] == "question" and "SOURCE_AFTER_CUTOFF" in e["reason_codes"]
    e = admit(f, [ev("l1", kind="literature", ext=loc_abs)], sources_by_id=sources, submission_cutoff="2026-05-01")
    assert "LITERATURE_CONTENT_INSUFFICIENT" in e["reason_codes"] and e["category"] == "question"
    e = admit(f, [ev("l1", kind="literature", ext=loc_after)], sources_by_id=sources, submission_cutoff=None)
    assert "TEMPORAL_UNCERTAIN" in e["reason_codes"]


def test_a12_no_issue_quota_and_readiness_order():
    strength = fnd(kind="strength", impact="informational")
    e = admit(strength, [ev("e1")])
    assert e["category"] == "strength"
    assert readiness([(strength, e)], {"c1"}, True)[0] == "no_blocker_found"
    assert readiness([(strength, e)], {"c1"}, False)[0] == "not_assessed"
    major = fnd(impact="major", vids=["v1"])
    em = admit(major, [ev("e1")], ver("human"))
    assert readiness([(major, em)], {"c1"}, False)[0] == "blocked"


# ------------------------------------------------------------ literature (network mocked)
OPENALEX = {"meta": {"count": 1}, "results": [{
    "id": "https://openalex.org/W1", "title": "Sparse gates", "publication_date": "2026-06-10", "doi": None,
    "authorships": [{"author": {"display_name": "A. Author"}}], "ids": {"openalex": "W1"},
    "abstract_inverted_index": {"Sparse": [0], "gates": [1]}, "primary_location": {"source": {"display_name": "X"}}}]}


def fake_fetch(payload):
    def f(url, hosts, max_bytes):
        assert "openalex" in url
        return json.dumps(payload).encode()
    return f


def test_e08_provider_misspelled_or_not_permitted(kernel, paper):
    run_id, _ = plan_and_start(kernel, paper, permissions={"search_providers": ["arxiv"]})
    with pytest.raises(SRError) as ei:
        litops.lit_search(kernel, run_id, "openalexx", "sparse gating", fetch=fake_fetch(OPENALEX))
    assert ei.value.category == "policy_denied"
    with pytest.raises(SRError) as ei:
        litops.lit_search(kernel, run_id, "openalex", "sparse gating", fetch=fake_fetch(OPENALEX))
    assert ei.value.category == "policy_denied"


def test_disclosure_blocks_title_and_copied_spans(kernel, paper):
    k = kernel
    run_id, pv = plan_and_start(k, paper)
    k.submit(k.claim(run_id)["task"]["task_token"], understand_output(k, pv))
    with pytest.raises(SRError):
        litops.lit_search(k, run_id, "openalex", "GateNet: Sparse Gating for Robust Text Classification",
                          fetch=fake_fetch(OPENALEX))
    with pytest.raises(SRError):
        litops.lit_search(k, run_id, "openalex", "inserts a gate g = sigmoid(Wh) after every attention block and",
                          fetch=fake_fetch(OPENALEX))


def test_search_records_and_e16_failures(kernel, paper):
    k = kernel
    run_id, _ = plan_and_start(k, paper)
    res = litops.lit_search(k, run_id, "openalex", "sparse token gating transformers", fetch=fake_fetch(OPENALEX))
    assert res["provider_status"] == "success" and res["records"][0]["after_cutoff"] is True
    assert res["records"][0]["access_level"] == "abstract"
    assert k.run_sources(run_id)[0]["source_id"] == "openalex:W1"

    def boom(url, hosts, max_bytes):
        raise SRError("provider_timeout", "timed out", retryable=True)
    res = litops.lit_search(k, run_id, "openalex", "token pruning", fetch=boom)
    assert res["provider_status"] == "failed" and res["records"] == [] and res["error_reason"]
    res = litops.lit_search(k, run_id, "openalex", "token pruning", fetch=lambda u, h, m: b"<<not json")
    assert res["provider_status"] == "failed"
    partial = dict(OPENALEX, meta={"count": 50})
    res = litops.lit_search(k, run_id, "openalex", "gating", fetch=fake_fetch(partial))
    assert res["provider_status"] == "partial" and res["complete"] is False and res["next_cursor"]


def test_redirect_outside_allowlist_blocked():
    with pytest.raises(SRError) as ei:
        literature.http_get("https://evil.example.com/x", {"api.openalex.org"}, 100)
    assert ei.value.category == "policy_denied"
