from pathlib import Path

import pytest

from scholarly_review.core.kernel import Kernel
from scholarly_review.core.store import Workspace

ROOT = Path(__file__).resolve().parents[1]
PAPER = ROOT / "examples" / "papers" / "synthetic_ml_paper.md"


@pytest.fixture
def ws(tmp_path):
    w = Workspace.open(tmp_path / ".scholarly-review")
    yield w
    w.close()


@pytest.fixture
def kernel(ws):
    return Kernel(ws)


@pytest.fixture
def paper():
    return str(PAPER)


def block_with(k: Kernel, pv_id: str, text: str) -> str:
    hits = [b["block_id"] for b in k.docmap(pv_id)["blocks"] if text in b["text"]]
    assert len(hits) == 1, (text, hits)
    return hits[0]


def core_not_checked(check_type: str, claims) -> list[dict]:
    """claim-evidence must assess every core claim; fixtures that check nothing say so explicitly."""
    if check_type != "core.claim_evidence":
        return []
    return [{"claim_id": c["claim_id"], "outcome": "not_checked", "evidence_keys": [], "scope_checked": "-"}
            for c in claims if c["importance"] == "core"]


def run_claims(k: Kernel, run_id: str) -> list[dict]:
    return [c for c in k.ws.records(run_id, "claim") if c["validity"] == "current"]


def empty_check(check_type: str, claims=()) -> dict:
    return {"output": {"check_type": check_type, "evidence": [], "assessments": core_not_checked(check_type, claims),
                       "findings": [], "analyzed_block_ids": [], "not_checked": []}}


def plan_and_start(k: Kernel, paper: str, **over) -> tuple[str, str]:
    params = {"input": {"manuscript": paper}, "mode": "author", "effort": "standard",
              "scope": {"domain_profile": "ml-v1"}, "rubric": {"submission_cutoff": "2026-05-01"},
              "permissions": {"search_providers": ["arxiv", "openalex"]}, "outputs": {"language": "en"}}
    for key, v in over.items():
        params[key] = v
    res = k.plan(params)
    k.start(res["run_id"])
    run = k._run(res["run_id"])
    return res["run_id"], run["paper_version_id"]


def understand_output(k: Kernel, pv: str, subfields=("other_ml",)) -> dict:
    b_abs = block_with(k, pv, "We propose GateNet")
    return {"output": {
        "summary": {"title": "GateNet: Sparse Gating for Robust Text Classification",
                    "research_problem": "Text classifiers overfit to spurious tokens.", "task": "text classification",
                    "method": "A sparse top-k gating layer after attention blocks.",
                    "contributions": ["sparse gating layer", "SOTA accuracy on SST-B", "gate sparsity analysis"],
                    "assumptions": [], "data": ["SST-B", "SST-B-large"], "main_results": ["84% accuracy"],
                    "paper_type": "empirical", "detected_domain": "ml", "ai_subfields": list(subfields)},
        "claims": [
            {"key": "c1", "original_text": "GateNet reaches an accuracy and macro-F1 of 80%",
             "normalized_text": "GateNet accuracy = 80% and macro-F1 = 80% on SST-B", "scope": "SST-B test",
             "origin": "explicit", "importance": "core", "claim_types": ["empirical"],
             "source_refs": [{"block_id": b_abs, "quote": "GateNet reaches an accuracy and macro-F1 of 80%"}]},
            {"key": "c2", "original_text": "it improves accuracy from 80% to 84% over the baseline encoder",
             "normalized_text": "GateNet improves accuracy by 4 pp over baseline", "scope": "SST-B",
             "origin": "explicit", "importance": "core", "claim_types": ["comparative"],
             "source_refs": [{"block_id": b_abs, "quote": "improves accuracy from 80% to 84% over the baseline encoder"}]},
            {"key": "c3", "original_text": "GateNet is robust across all text classification domains",
             "normalized_text": "GateNet is robust in all text classification domains", "scope": "all domains (as stated)",
             "origin": "explicit", "importance": "core", "claim_types": ["generality"],
             "source_refs": [{"block_id": b_abs, "quote": "robust across all text classification domains"}]},
        ],
        "hypotheses": [{"key": "h1", "question": "Is the gain caused by the gate or by the doubled data?",
                        "motivation": "Method section says training data was doubled", "related_claims": ["c2"]}],
        "analyzed_block_ids": [b["block_id"] for b in k.docmap(pv)["blocks"]],
        "uncovered_notes": [],
    }}


def core_handlers(k: Kernel, pv_id: str, run_id: str) -> dict:
    """Handlers that do the core jobs properly: claim-evidence assesses every core claim with anchored
    evidence, and the literature check runs one (offline) search. With these a run can end `completed`."""
    import json as _json
    from scholarly_review.core import litops

    def claim_evidence(req):
        b = block_with(k, pv_id, "GateNet reaches an accuracy")
        core = [c for c in run_claims(k, run_id) if c["importance"] == "core"]
        return {"output": {"check_type": "core.claim_evidence",
                           "evidence": [{"key": "e1", "kind": "manuscript", "content_summary": "abstract claims",
                                         "source_refs": [{"block_id": b, "quote": "GateNet reaches an accuracy"}],
                                         "method": "read abstract"}],
                           "assessments": [{"claim_id": c["claim_id"], "outcome": "supported", "evidence_keys": ["e1"],
                                            "scope_checked": "stated in the abstract"} for c in core],
                           "findings": [], "analyzed_block_ids": [b], "not_checked": []}}

    def literature(req):
        def fetch(url, hosts, max_bytes):
            return _json.dumps({"results": [], "meta": {"count": 0}}).encode()
        litops.lit_search(k, run_id, "openalex", "sparse gating text classification", fetch=fetch)
        return empty_check("core.novelty_positioning")

    return {"core.claim_evidence": claim_evidence, "core.novelty_positioning": literature}
