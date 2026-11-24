"""Frozen report snapshot: integrity checks, admission, coverage and readiness (spec §9.5, §9.6, B2)."""
from __future__ import annotations

import json
import re
from typing import Optional

from ..core import budget as bud
from ..core.admission import admit_finding, readiness
from ..core.errors import SRError
from ..core.kernel import Kernel
from ..models.common import IMPACT_RANK, now_utc, sha256_json
from ..modules.registry import NUMERIC_CHECK

EXTERNAL_AUDIENCES = {"authors", "editor", "public"}
NOT_LIMITATIONS = ("identical input content already imported", "removed ")
APPENDIX_HEADING_RE = re.compile(r"^(appendix|supplementary|[A-H](\.\d+)*\s+[A-Z])", re.I)


def compute_coverage(k: Kernel, run_id: str) -> dict:
    run = k._run(run_id)
    dm = k.docmap(run["paper_version_id"])
    tasks = k.ws.q("SELECT * FROM tasks WHERE run_id=?", (run_id,))
    analyzed: set[str] = set()
    for t in tasks:
        if t["state"] in ("completed", "partial"):
            analyzed |= set(json.loads(t["payload_json"]).get("analyzed_block_ids", []))
    material = []
    for src in ("manuscript", "supplement"):
        blocks = [b for b in dm["blocks"] if b.get("source", "manuscript") == src]
        if not blocks and src == "supplement":
            material.append({"artifact_id": "supplement", "total_blocks": None, "readable_blocks": 0,
                             "analyzed_blocks": 0, "missing_reason": "no supplement supplied or readable"})
            continue
        ids = {b["block_id"] for b in blocks}
        material.append({"artifact_id": src, "total_blocks": len(blocks),
                         "readable_blocks": sum(1 for b in blocks if b["extraction_quality"] != "empty"),
                         "analyzed_blocks": len(ids & analyzed),
                         "missing_reason": None})
    claims = [c for c in k.ws.records(run_id, "claim") if c["validity"] == "current"]
    assessments = k.ws.records(run_id, "assessment")
    check_tasks = [t for t in tasks if t["check_type"]]
    applicable = sorted({t["check_type"] for t in check_tasks if not (t["state"] == "skipped" and t["reason"] == "not_applicable")})
    claim_items = []
    for c in claims:
        mine = [a for a in assessments if a["claim_id"] == c["claim_id"]]
        claim_items.append({
            "claim_id": c["claim_id"], "importance": c["importance"], "applicable_checks": applicable,
            "completed_checks": sorted({a["check_type"] for a in mine if a["outcome"] not in ("not_checked",)}),
            "unresolved_checks": sorted({a["check_type"] for a in mine if a["outcome"] in ("unresolved", "insufficient_evidence")}),
        })
    check_items = []
    for t in check_tasks:
        n = sum(1 for a in assessments if a["check_type"] == t["check_type"] and a["outcome"] != "not_checked")
        reasons = json.loads(t["payload_json"]).get("not_checked", [])
        if t["state"] not in ("completed",):
            reasons = reasons + [f"task {t['state']}" + (f" ({t['reason']})" if t["reason"] else "")]
        check_items.append({"check_type": t["check_type"], "expected_items": None, "processed_items": n,
                            "scope_label": t["state"], "remaining_reasons": reasons})
    coverage = {"material_items": material, "claim_items": claim_items, "check_items": check_items,
                "unknown_denominators": ["expected assessments per check (claims applicable to each check are not "
                                         "enumerated in v0.1)", "pages without extractable text"]}
    core = [c for c in claims if c["importance"] == "core"]
    core_checked = [ci for ci in claim_items if ci["importance"] == "core" and ci["completed_checks"]]
    understand_ok = any(t["type"] == "understand" and t["state"] == "completed" for t in tasks)
    required_ok = all(t["state"] in ("completed", "partial") or (t["state"] == "skipped" and t["reason"] == "not_applicable")
                      for t in tasks if t["required"])
    # appendix sections inside the manuscript (headings like "A.1 ...", "Appendix") after the references
    appendix_ids: set[str] = set()
    seen_refs = in_app = False
    for b in dm["blocks"]:
        if b.get("source", "manuscript") != "manuscript":
            continue
        if b["kind"] == "heading" and b["text"].strip().lower().startswith(("references", "bibliography")):
            seen_refs = True
        elif seen_refs and b["kind"] == "heading" and APPENDIX_HEADING_RE.match(b["text"].strip()):
            in_app = True
        if in_app:
            appendix_ids.add(b["block_id"])
    findings = mark_cross_module_duplicates(k.ws.records(run_id, "finding"))
    to_verify = [f for f in findings if f["kind"] == "concern" and not f.get("duplicate_of")]
    summary = {
        "readable_blocks": material[0]["readable_blocks"], "analyzed_blocks": material[0]["analyzed_blocks"],
        "page_count": dm.get("page_count"),
        "appendix": "supplied" if material[1]["total_blocks"] else (
            "in manuscript" if appendix_ids else "not supplied"),
        "appendix_in_manuscript_blocks": len(appendix_ids),
        "appendix_in_manuscript_analyzed": len(appendix_ids & analyzed),
        "appendix_analyzed_blocks": material[1]["analyzed_blocks"],
        "core_claims_extracted": len(core), "core_claims_checked": len(core_checked),
        "concerns_total": len(to_verify),
        "concerns_verified": sum(1 for f in to_verify if f["verification_ids"]),
        "external_fulltexts": k.ws.q1("SELECT COUNT(*) AS n FROM run_sources rs JOIN sources s USING(source_id) "
                                      "WHERE rs.run_id=? AND s.fulltext_artifact_id IS NOT NULL", (run_id,))["n"],
        "incomplete": [f"{t['type']}{'/' + t['check_type'] if t['check_type'] else ''}: {t['state']}"
                       + (f" ({t['reason']})" if t["reason"] else "")
                       for t in tasks if t["state"] not in ("completed",)
                       and not (t["state"] == "skipped" and t["reason"] == "not_applicable")],
        "incomplete_items": [{"task": t["type"] + ("/" + t["check_type"] if t["check_type"] else ""),
                              "state": t["state"], "reason": t["reason"]}
                             for t in tasks if t["state"] not in ("completed",)
                             and not (t["state"] == "skipped" and t["reason"] == "not_applicable")],
        "core_coverage_complete": bool(understand_ok and required_ok and core and len(core_checked) == len(core)),
        "claim_coverage_note": "percentages refer only to claims identified so far, never to all claims of the paper",
    }
    return {"coverage": coverage, "summary": summary}


DECIMAL_RE = re.compile(r"(?<![\w.])-?\d+\.\d+(?![\w.])")


def _decimals(text: str) -> set[str]:
    return {m.lstrip("-") for m in DECIMAL_RE.findall(text or "")}


def mark_cross_module_duplicates(findings: list[dict]) -> list[dict]:
    """A model-written finding that restates a confirmed numeric finding (same claims, at least two of the
    same printed decimal values) is reported once, under the deterministic numeric finding. Returns copies;
    stored records are not modified."""
    def from_numeric_module(f: dict) -> bool:  # produced by the deterministic recomputation
        return f.get("extensions", {}).get("check_type") == "numeric.consistency"

    numeric = [f for f in findings if from_numeric_module(f)
               and f["kind"] == "concern" and f["disposition"] == "confirmed" and not f.get("duplicate_of")]
    advice: dict[str, str] = {}  # the restating finding usually carries the more specific revision advice
    out = []
    for f in findings:
        if f["kind"] != "strength" and not from_numeric_module(f) and not f.get("duplicate_of"):
            nums = _decimals(f["proposition"])
            for n in numeric:
                if set(f["claim_ids"]) & set(n["claim_ids"]) and len(nums & _decimals(n["proposition"])) >= 2:
                    f = {**f, "duplicate_of": n["finding_id"]}
                    if f.get("recommended_action"):
                        advice.setdefault(n["finding_id"], f["recommended_action"])
                    break
        out.append(f)
    return [{**f, "recommended_action": advice[f["finding_id"]],
             "extensions": {**f.get("extensions", {}), "advice_from_duplicate": True}}
            if f["finding_id"] in advice else f for f in out]


def build_snapshot(k: Kernel, run_id: str, audience: Optional[str] = None) -> dict:
    run = k._run(run_id)
    cfg = json.loads(run["config_json"])
    audience = audience or cfg["outputs"]["audience"]
    pv_id = run["paper_version_id"]
    integrity_problems: list[str] = []
    ok_cache: dict[str, bool] = {}

    def artifact_ok(aid: str) -> bool:
        if aid not in ok_cache:
            try:
                k.ws.artifact_path(aid, verify=True)
                ok_cache[aid] = True
            except SRError as e:
                ok_cache[aid] = False
                integrity_problems.append(e.message)
        return ok_cache[aid]

    pv = k._pv(pv_id)
    if not artifact_ok(pv["docmap_artifact_id"]):
        raise SRError("artifact_corrupt", "the frozen DocumentMap failed its integrity check; no report can be built")

    evidence = {e["evidence_id"]: e for e in k.ws.records(run_id, "evidence")}
    # mark corrupt evidence (E04): validity projection, academic disposition untouched
    corrupt = [e for e in evidence.values() if e.get("payload_artifact_id") and not artifact_ok(e["payload_artifact_id"])]
    if corrupt:
        with k.ws.tx() as c:
            for e in corrupt:
                e["validity"] = "corrupt"
                k.ws.update_record(c, e)
                k.ws.event(c, run_id, "evidence_corrupt", {"evidence_id": e["evidence_id"]})
    verifications = {v["verification_id"]: v for v in k.ws.records(run_id, "verification")}
    sources = {s["source_id"]: s for s in k.run_sources(run_id)}
    findings = mark_cross_module_duplicates(k.ws.records(run_id, "finding"))
    cutoff = cfg["rubric"]["submission_cutoff"]
    admitted = []
    for f in findings:
        elig = admit_finding(f, evidence_by_id=evidence, verifications_by_id=verifications, paper_version_id=pv_id,
                             audience=audience, sources_by_id=sources, submission_cutoff=cutoff,
                             artifact_ok=artifact_ok)
        f["report_eligibility"] = [elig]
        admitted.append((f, elig))
    cov = compute_coverage(k, run_id)
    claims = k.ws.records(run_id, "claim")
    core_ids = {c["claim_id"] for c in claims if c["importance"] == "core"}
    ready, ready_basis = readiness([(f, e) for f, e in admitted if e["admitted"]], core_ids,
                                   cov["summary"]["core_coverage_complete"])
    summary = json.loads(run["summary_json"] or "{}")
    budget = bud.budget_report(k.ws.conn, run["budget_account_id"], cfg["execution_mode"] == "interactive")
    # plan-time notices and parser clean-up notes are not limitations of the review
    limitations = [w for w in summary.get("warnings", []) if not w.startswith(NOT_LIMITATIONS)]
    if integrity_problems:
        limitations.append("integrity check failed for some artifacts; dependent findings were excluded")
    if cfg["scope"]["domain_profile"] == "generic-v1":
        limitations.append("no domain-specific standard was applied (generic profile)")
    if cfg["execution_mode"] == "interactive":
        if cfg["host"].get("verify_worker", "none") != "none":
            limitations.append("interactive mode with isolated verification worker: host session usage is not "
                               "measured; worker usage is reported; a fresh context of the same model family is "
                               "not a statistically independent reviewer")
        else:
            limitations.append("interactive mode: model usage and cost of the host session are not measured; "
                               "verification by the same host cannot confirm major issues on its own")
    if summary.get("reviewer_assessment"):
        limitations.append("scores and recommendation are a model draft for the reviewer to adopt or edit")
    elif not (summary.get("rubric") or {}).get("score_scales"):
        limitations.append("venue fit and scores: not assessed (no valid venue rubric supplied)")
    dm = k.docmap(pv_id)
    limitations += [w for w in dm.get("warnings", []) if not w.startswith(NOT_LIMITATIONS)][:3]
    limitations = list(dict.fromkeys(limitations))  # de-duplicate, keep order
    snap = {
        "run_id": run_id, "paper_version_id": pv_id, "run_state": run["state"], "audience": audience,
        "generated_at": now_utc(), "config": cfg, "paper_summary": summary.get("paper_summary"),
        "claims": claims, "hypotheses": k.ws.records(run_id, "hypothesis"),
        "findings": findings, "evidence": evidence, "verifications": verifications,
        "assessments": k.ws.records(run_id, "assessment"),
        "experiment_plans": k.ws.records(run_id, "experiment_plan"),
        "sources": sources, "coverage": cov["coverage"], "coverage_summary": cov["summary"],
        "readiness": {"value": ready, "basis": ready_basis} if cfg["mode"] == "author" else None,
        "reviewer_assessment": summary.get("reviewer_assessment") if cfg["mode"] == "reviewer" else None,
        "budget": budget, "enforcement": k._enforcement(cfg), "limitations": limitations,
        "is_partial": run["state"] != "completed" or not cov["summary"]["core_coverage_complete"],
    }
    snap["snapshot_id"] = "snap_" + sha256_json({k_: v for k_, v in snap.items() if k_ != "generated_at"})[:32]
    return snap


def sort_findings(items: list[dict]) -> list[dict]:
    return sorted(items, key=lambda f: (-IMPACT_RANK.get(f["impact"], 0), -IMPACT_RANK.get(f["potential_impact"], 0),
                                        f["created_at"]))


def category(f: dict) -> str:
    return f["report_eligibility"][0]["category"] if f["report_eligibility"] else "excluded"


def is_numeric(f: dict) -> bool:
    return (f.get("extensions") or {}).get("check_type") == NUMERIC_CHECK
