"""`sreview auto`: the whole run in one command, so the host session spends a handful of calls instead of
several per task. Every task (understand, numeric, checks, literature, verification) runs in a
kernel-launched worker; the host only starts the command, continues it if a time slice ends, and reads
the compact summary it returns.

Each invocation works for at most `slice_seconds` (default 420 s, so a host shell call with a 10-minute
timeout never cuts it off) and returns `done: false` with `next` when work remains; nothing is lost
between slices because all state lives in the kernel.
"""
from __future__ import annotations

import json
import time
from typing import Any, Optional

from ..core.errors import SRError
from ..reports import render
from ..reports.snapshot import build_snapshot
from . import worker

IMPACT_ORDER = {"critical": 0, "major": 1, "minor": 2, "informational": 3, "not_assessed": 4}
TERMINAL = ("completed", "partial", "cancelled", "failed", "blocked")


def _usage(kernel, run_id: str) -> dict:
    rows = kernel.ws.q("SELECT json FROM events WHERE run_id=? AND etype='isolated_worker'", (run_id,))
    ev = [json.loads(r["json"]) for r in rows]
    return {"worker_calls": len(ev),
            "worker_input_tokens": sum(e.get("input_tokens") or 0 for e in ev),
            "worker_output_tokens": sum(e.get("output_tokens") or 0 for e in ev),
            "worker_cost_usd": round(sum(e.get("cost_usd") or 0 for e in ev), 3),
            "note": "worker usage as reported by the workers; this host session's own usage is not measured"}


def _summary(kernel, run_id: str, exp: dict) -> dict:
    snap = build_snapshot(kernel, run_id)
    items, pending = [], []
    for f in snap["findings"]:
        elig = (f.get("report_eligibility") or [{}])[0]
        if f["kind"] == "concern" and f["disposition"] == "unresolved" and not f.get("duplicate_of") \
                and f["potential_impact"] in ("major", "critical"):
            pending.append({"finding_id": f["finding_id"], "proposition": f["proposition"][:220]})
        if not elig.get("admitted") or elig.get("category") == "strength":
            continue
        items.append({"impact": f["impact"] if f["impact"] != "not_assessed" else f["potential_impact"],
                      "category": elig.get("category"), "proposition": f["proposition"][:220]})
    items.sort(key=lambda x: IMPACT_ORDER.get(x["impact"], 9))
    strengths = sum(1 for f in snap["findings"] if (f.get("report_eligibility") or [{}])[0].get("category") == "strength")
    cov = snap.get("coverage", {}).get("summary", {}) if isinstance(snap.get("coverage"), dict) else {}
    ra = snap.get("reviewer_assessment")
    form = {"recommendation": (ra.get("recommendation") or {}).get("value"),
            "scores": {s["criterion_id"]: s["value"] for s in ra.get("scores", [])},
            "note": "draft for you to adopt or edit; export --audience editor for the editor view"} if ra else None
    return {
        "readiness": exp["readiness"], "partial": exp["partial"], **({"review_form_draft": form} if form else {}),
        "counts": {"reported_issues": len(items), "strengths": strengths, "awaiting_user_confirmation": len(pending)},
        "top_items": items[:8],
        "awaiting_user_confirmation": pending[:6],
        "coverage": {k: cov.get(k) for k in ("analyzed_blocks", "readable_blocks", "core_claims_assessed",
                                             "core_claims", "concerns_verified", "concerns") if k in cov},
        "files": {k: v for k, v in exp["files"].items() if k in ("report.md", "action_plan.md", "report.html")},
    }


def auto(kernel, *, params: Optional[dict] = None, project_config: Optional[dict] = None,
         paper_version_id: Optional[str] = None, run_id: Optional[str] = None, slice_seconds: int = 420,
         binary: Optional[str] = None, runner=None, search_fetch=None) -> dict:
    t_start = time.monotonic()
    deadline = t_start + max(0, slice_seconds)
    planned: dict[str, Any] = {}
    if run_id is None:
        params = dict(params or {})
        host = dict(params.get("host") or {})
        default_engine = "codex" if host.get("name") == "codex" else "claude"
        host.setdefault("analysis_worker", default_engine)
        host.setdefault("verify_worker", host["analysis_worker"])
        params["host"] = host
        res = kernel.plan(params, project_config, paper_version_id)
        run_id = res["run_id"]
        kernel.start(run_id)
        ps = res["plan_summary"]
        planned = {"effort": ps["effort"], "domain_profile": ps["domain_profile"],
                   "tasks": len(ps["planned_tasks"]), "time_fit": ps.get("time_fit"), "warnings": res["warnings"]}
    else:
        run = kernel._run(run_id)
        if run["state"] == "paused":
            raise SRError("invalid_state", "run is paused: `sreview resume` first")
    batches = []
    while time.monotonic() < deadline:
        if kernel.status(run_id)["state"] not in ("running", "pausing"):
            break
        out = worker.run_workers(kernel, run_id, max_tasks=200, deadline=deadline, binary=binary,
                                 runner=runner, search_fetch=search_fetch)
        batches.append(out)
        if not out["workers"]:
            probe = kernel.claim(run_id)  # nothing for workers: lets the kernel finalize, or shows a host task
            if probe.get("reason") == "final_assessment_added":
                continue
            if probe.get("task"):
                kernel.abandon(probe["task"]["task_token"], "auto mode leaves host tasks unclaimed")
                raise SRError("invalid_state", f"task type {probe['task']['task_type']} is not handled by workers")
            break
    state = kernel.status(run_id)
    done = [w for b in batches for w in b["workers"]]
    progress = {"tasks_this_slice": len(done),
                "rejected_this_slice": sum(1 for w in done if w["status"] not in ("submitted",)),
                "task_states": state["tasks"]["by_state"], "active_seconds": state["active_seconds"],
                "slice_seconds": round(time.monotonic() - t_start)}
    result: dict[str, Any] = {"run_id": run_id, "state": state["state"], "progress": progress,
                              "usage": _usage(kernel, run_id)}
    if planned:
        result["plan"] = planned
    if state["state"] in TERMINAL:
        exp = render.export(kernel, run_id)
        result |= {"done": True, **_summary(kernel, run_id, exp),
                   "budget_stops": state.get("budget_stops", [])}
    else:
        result |= {"done": False, "next": f"sreview auto --run {run_id}"}
    return result
