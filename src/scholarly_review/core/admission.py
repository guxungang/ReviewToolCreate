"""Report admission (spec §9.5) and submission readiness (spec §2.1).

Evidence existing and being well-formed is not the same as supporting the conclusion: the
function inspects verification substance, never a vote count.
"""
from __future__ import annotations

from typing import Callable, Iterable, Optional

from ..models.common import IMPACT_RANK, now_utc

VALID_MATCH = {"exact", "fuzzy"}
INDEPENDENT = {"fresh", "human"}
THIN_ACCESS = {"metadata", "abstract"}


def _anchored(ev: dict) -> bool:
    if ev["kind"] in ("calculation", "execution"):
        return bool(ev.get("payload_artifact_id"))
    if ev["kind"] in ("manuscript", "supplement"):
        doc_refs = [r for r in ev["source_refs"] if r["locator"]["type"] == "document"]
        return bool(doc_refs) and all(r["locator"]["match_status"] in VALID_MATCH for r in doc_refs)
    if ev["kind"] == "literature":
        return any(r["locator"]["type"] == "external" for r in ev["source_refs"])
    if ev["kind"] == "user_assertion":
        return False  # never independent fact
    return bool(ev["source_refs"])


def admit_finding(
    finding: dict,
    *,
    evidence_by_id: dict[str, dict],
    verifications_by_id: dict[str, dict],
    paper_version_id: str,
    audience: str,
    sources_by_id: Optional[dict[str, dict]] = None,
    submission_cutoff: Optional[str] = None,
    artifact_ok: Callable[[str], bool] = lambda _aid: True,
) -> dict:
    sources_by_id = sources_by_id or {}
    reasons: list[str] = []

    def out(admitted: bool, category: str) -> dict:
        return {"audience": audience, "admitted": admitted, "category": category,
                "reason_codes": reasons, "checked_at": now_utc()}

    if finding["paper_version_id"] != paper_version_id:
        reasons.append("VERSION_MISMATCH")
        return out(False, "excluded")
    if finding.get("validity", "current") != "current":
        reasons.append(f"VALIDITY_{finding['validity'].upper()}")
        return out(False, "excluded")
    if finding["disposition"] in ("refuted", "dismissed"):
        reasons.append(f"DISPOSITION_{finding['disposition'].upper()}")
        return out(False, "excluded")
    if finding.get("duplicate_of"):
        reasons.append("DUPLICATE_OF_" + finding["duplicate_of"])
        return out(False, "excluded")

    evs = []
    for eid in finding["evidence_ids"]:
        ev = evidence_by_id.get(eid)
        if ev is None:
            reasons.append("EVIDENCE_MISSING")
            return out(False, "excluded")
        if ev["paper_version_id"] != paper_version_id:
            reasons.append("EVIDENCE_OTHER_VERSION")
            return out(False, "excluded")
        if ev.get("validity", "current") != "current" or (ev.get("payload_artifact_id") and not artifact_ok(ev["payload_artifact_id"])):
            reasons.append("EVIDENCE_NOT_CURRENT")
            return out(False, "excluded")
        evs.append(ev)
    if not evs:
        reasons.append("NO_EVIDENCE")
        return out(False, "excluded")

    exportable = [ev for ev in evs if audience in ev["disclosure"]["allowed_audiences"]]
    if not exportable:
        reasons.append("AUDIENCE_NOT_ALLOWED")
        return out(False, "excluded")

    anchored = any(_anchored(ev) for ev in exportable)
    has_calc = any(ev["kind"] in ("calculation", "execution") for ev in evs)
    if not anchored:
        reasons.append("LOCATOR_INVALID")

    # external comparison: temporal and content applicability (spec §8.3)
    lit_problem = False
    issue_type = (finding.get("extensions") or {}).get("issue_type")
    for ev in evs:
        if ev["kind"] != "literature":
            continue
        for r in ev["source_refs"]:
            loc = r["locator"]
            if loc["type"] != "external":
                continue
            src = sources_by_id.get(loc["source_id"], {})
            date = src.get("first_public_date")
            if submission_cutoff is None:
                reasons.append("TEMPORAL_UNCERTAIN")
                lit_problem = lit_problem or issue_type in ("novelty", "citation", "baseline")
            elif date is None:
                reasons.append("SOURCE_DATE_UNKNOWN")
                lit_problem = True
            elif date > submission_cutoff:
                reasons.append("SOURCE_AFTER_CUTOFF")
                lit_problem = True
            if loc["access_level"] in THIN_ACCESS and issue_type in ("novelty", "baseline"):
                reasons.append("LITERATURE_CONTENT_INSUFFICIENT")
                lit_problem = True

    kind = finding["kind"]
    if kind == "strength":
        if finding["disposition"] == "confirmed" and anchored:
            return out(True, "strength")
        reasons.append("STRENGTH_NOT_CONFIRMED")
        return out(False, "excluded")

    if kind == "clarification":
        return out(True, "question")

    # concern
    if finding["disposition"] != "confirmed":
        reasons.append(f"NOT_CONFIRMED_{finding['disposition'].upper()}")
        return out(True, "question")
    if not anchored or lit_problem:
        reasons.append("DOWNGRADED_TO_QUESTION")
        return out(True, "question")
    if IMPACT_RANK.get(finding["impact"], 0) >= IMPACT_RANK["major"]:
        qualified = False
        for vid in finding["verification_ids"]:
            v = verifications_by_id.get(vid)
            if not v or v["result"] != "validated":
                continue
            if v["context_independence"] in INDEPENDENT:
                qualified = True
            elif has_calc and "deterministic" in v["method"]:
                qualified = True  # verified-input deterministic calculation
        if not qualified:
            reasons.append("INSUFFICIENT_VERIFICATION_FOR_IMPACT")
            return out(True, "question")
    return out(True, "confirmed_concern")


def readiness(admitted: Iterable[tuple[dict, dict]], core_claim_ids: set[str], core_coverage_complete: bool) -> tuple[str, list[str]]:
    """Fixed-order author readiness: blocked -> not_assessed -> conditional -> no_blocker_found."""
    admitted = list(admitted)
    blockers = []
    for f, e in admitted:
        if e["category"] != "confirmed_concern":
            continue
        rank = IMPACT_RANK.get(f["impact"], 0)
        touches_core = bool(set(f["claim_ids"]) & core_claim_ids) or not f["claim_ids"]
        if f["impact"] == "critical" or (rank >= IMPACT_RANK["major"] and touches_core):
            blockers.append(f["finding_id"])
    if blockers:
        return "blocked", blockers
    if not core_coverage_complete:
        return "not_assessed", ["core checks incomplete"]
    conditions = [f["finding_id"] for f, e in admitted
                  if e["category"] == "question" or (e["category"] == "confirmed_concern"
                                                    and IMPACT_RANK.get(f["impact"], 0) >= IMPACT_RANK["major"])]
    if conditions:
        return "conditional", conditions
    return "no_blocker_found", []
