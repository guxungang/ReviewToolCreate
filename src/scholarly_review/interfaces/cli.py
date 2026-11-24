"""`sreview` command line. stdout carries only the JSON envelope; logs go to stderr (spec §13.4)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional

from .service import Service, envelope


def _set_path(d: dict, dotted: str, value: Any) -> None:
    keys = dotted.split(".")
    for k in keys[:-1]:
        d = d.setdefault(k, {})
    d[keys[-1]] = value


def _params_from_args(a: argparse.Namespace) -> dict:
    p: dict = {}
    if a.manuscript:
        _set_path(p, "input.manuscript", a.manuscript)
    if a.supplement:
        _set_path(p, "input.supplements", a.supplement)
    for flag, path in (("mode", "mode"), ("effort", "effort"), ("domain", "scope.domain_profile"),
                       ("language", "outputs.language"), ("cutoff", "rubric.submission_cutoff"),
                       ("venue", "rubric.venue"), ("rubric", "rubric.source_file"), ("host", "host.name"),
                       ("expertise", "scope.reviewer_expertise"), ("verify_worker", "host.verify_worker"),
                       ("analysis_worker", "host.analysis_worker"),
                       ("worker_model", "host.worker_model")):
        v = getattr(a, flag)
        if v is not None:
            _set_path(p, path, v)
    if a.search_providers is not None:
        _set_path(p, "permissions.search_providers", [x for x in a.search_providers.split(",") if x])
    if a.scores:
        _set_path(p, "rubric.score_requested", True)
    if a.recommendation:
        _set_path(p, "rubric.decision_requested", True)
    if a.no_new_experiments:
        _set_path(p, "scope.allow_new_experiments", False)
    if a.allow_new_experiments:
        _set_path(p, "scope.allow_new_experiments", True)
    if a.minutes is not None:
        _set_path(p, "budget.max_active_seconds", int(a.minutes * 60))
    for item in a.set or []:
        key, _, raw = item.partition("=")
        try:
            val = json.loads(raw)
        except json.JSONDecodeError:
            val = raw
        _set_path(p, key, val)
    return p


def _add_plan_flags(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--config", help="project/run config JSON (App. A1 keys)")
    sp.add_argument("--manuscript")
    sp.add_argument("--supplement", action="append")
    sp.add_argument("--paper-version", dest="paper_version")
    sp.add_argument("--verify-worker", dest="verify_worker", choices=["none", "claude"],
                    help="run verification tasks in an isolated `claude -p` worker launched by the kernel")
    sp.add_argument("--analysis-worker", dest="analysis_worker", choices=["none", "claude"],
                    help="run analysis tasks (understand, numeric, checks) in short-lived kernel-launched workers")
    sp.add_argument("--worker-model", dest="worker_model")
    sp.add_argument("--mode", choices=["author", "reviewer"])
    sp.add_argument("--effort", choices=["quick", "standard", "deep", "reproduction", "custom"])
    sp.add_argument("--domain", choices=["ml-v1", "systems-v1", "security-v1", "generic-v1"])
    sp.add_argument("--language", choices=["zh-CN", "en"])
    sp.add_argument("--cutoff", help="submission cutoff YYYY-MM-DD")
    sp.add_argument("--venue")
    sp.add_argument("--rubric", help="venue rubric JSON (RubricProfile keys)")
    sp.add_argument("--scores", action="store_true",
                    help="reviewer mode: draft the form's scores (needs --rubric with score_scales)")
    sp.add_argument("--recommendation", action="store_true",
                    help="reviewer mode: draft a recommendation (needs --rubric with decision_options)")
    sp.add_argument("--host", choices=["codex", "claude"])
    sp.add_argument("--expertise", choices=["general", "generalist", "specialist"])
    sp.add_argument("--search-providers", dest="search_providers", help="comma list, e.g. arxiv,openalex; empty disables")
    sp.add_argument("--minutes", type=float, help="active-time budget in minutes")
    sp.add_argument("--no-new-experiments", action="store_true")
    sp.add_argument("--allow-new-experiments", action="store_true")
    sp.add_argument("--set", action="append", help="dotted.key=JSON value, e.g. budget.max_tasks=20")
    sp.add_argument("--idempotency-key", dest="idempotency_key")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sreview", description="ScholarlyReview kernel CLI")
    p.add_argument("--workspace", default=".scholarly-review")
    p.add_argument("--json", action="store_true", help="accepted for compatibility; output is always JSON")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("doctor"); s.add_argument("--host", required=True, choices=["codex", "claude"])
    s = sub.add_parser("import")
    s.add_argument("--manifest"); s.add_argument("--manuscript"); s.add_argument("--supplement", action="append")
    s.add_argument("--paper-id"); s.add_argument("--title")
    s = sub.add_parser("plan"); _add_plan_flags(s)
    s = sub.add_parser("run"); s.add_argument("--run", dest="run_id"); _add_plan_flags(s)
    s = sub.add_parser("status"); s.add_argument("--run", dest="run_id", required=True)
    t = sub.add_parser("task").add_subparsers(dest="task_cmd", required=True)
    s = t.add_parser("claim"); s.add_argument("--run", dest="run_id", required=True)
    s = t.add_parser("submit"); s.add_argument("--run", dest="run_id", required=True)
    s.add_argument("--task-token", required=True); s.add_argument("--result", required=True)
    s.add_argument("--actor", choices=["model", "human"], default="model")
    lit = sub.add_parser("lit").add_subparsers(dest="lit_cmd", required=True)
    s = lit.add_parser("search"); s.add_argument("--run", dest="run_id", required=True)
    s.add_argument("--provider", required=True); s.add_argument("--query", required=True)
    s.add_argument("--limit", type=int, default=10)
    s = lit.add_parser("fetch"); s.add_argument("--run", dest="run_id", required=True)
    s.add_argument("--source-id", required=True)
    s = sub.add_parser("verify-human"); s.add_argument("--run", dest="run_id", required=True)
    s.add_argument("--finding", required=True); s.add_argument("--result", required=True,
                                                               choices=["validated", "invalidated", "inconclusive"])
    s.add_argument("--rationale", required=True); s.add_argument("--material", required=True)
    s.add_argument("--impact", choices=["critical", "major", "minor", "informational"])
    for name in ("pause", "resume", "cancel"):
        s = sub.add_parser(name); s.add_argument("--run", dest="run_id", required=True)
    b = sub.add_parser("budget").add_subparsers(dest="budget_cmd", required=True)
    s = b.add_parser("set"); s.add_argument("--run", dest="run_id", required=True)
    s.add_argument("--revision", required=True); s.add_argument("--authorization", required=True)
    s = sub.add_parser("export"); s.add_argument("--run", dest="run_id", required=True)
    s.add_argument("--audience", choices=["author_private", "reviewer_private", "authors", "editor", "public"])
    s.add_argument("--format", help="comma list: txt,md,json,html")
    s = sub.add_parser("compare"); s.add_argument("--old", required=True); s.add_argument("--new", required=True)
    s = sub.add_parser("install"); s.add_argument("--host", required=True, choices=["codex", "claude"])
    s.add_argument("--scope", default="project", choices=["project", "user"])
    s.add_argument("--project-root"); s.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("schemas"); s.add_argument("--out", default="schemas")
    s = sub.add_parser("auto", help="whole run in one command: every task in kernel-launched workers")
    _add_plan_flags(s)
    s.add_argument("--run", dest="run_id", help="continue an existing auto run")
    s.add_argument("--slice", type=int, default=420, help="seconds of work per invocation (default 420)")
    w = sub.add_parser("worker").add_subparsers(dest="worker_cmd", required=True)
    for name, hlp in (("run", "run ready worker-assigned tasks (analysis and/or verification) in claude -p workers"),
                      ("verify", "alias of `worker run`")):
        s = w.add_parser(name, help=hlp)
        s.add_argument("--run", dest="run_id", required=True)
        s.add_argument("--max-tasks", type=int, default=20)
        s.add_argument("--timeout", type=int, default=900, help="seconds per worker (must stay below the lease)")
        s.add_argument("--parallel", type=int, help="concurrent workers (default: the run's max_concurrency)")
    return p


def _load_json(path: Optional[str]) -> Optional[dict]:
    if not path:
        return None
    return json.loads(Path(path).read_text("utf-8"))


def dispatch(a: argparse.Namespace):
    svc = Service(a.workspace)
    if a.cmd == "doctor":
        return lambda: svc.doctor(a.host)
    if a.cmd == "import":
        def _imp():
            if a.manifest:
                m = _load_json(a.manifest)
                return svc.import_(m["manuscript"], m.get("supplements", []), a.paper_id, a.title)
            if not a.manuscript:
                raise SystemExit("import needs --manifest or --manuscript")
            return svc.import_(a.manuscript, a.supplement or [], a.paper_id, a.title)
        return _imp
    if a.cmd == "plan":
        return lambda: svc.plan(_params_from_args(a), _load_json(a.config), a.paper_version, a.idempotency_key)
    if a.cmd == "run":
        def _run():
            if a.run_id and (a.config or a.manuscript):
                from ..core.errors import SRError
                raise SRError("invalid_input", "--run and --config/--manuscript are mutually exclusive")
            if a.run_id:
                return svc.run(a.run_id)
            planned = svc.plan(_params_from_args(a), _load_json(a.config), a.paper_version, a.idempotency_key)
            started = svc.run(planned["run_id"])
            return {**planned, **started}
        return _run
    if a.cmd == "status":
        return lambda: svc.status(a.run_id)
    if a.cmd == "task":
        if a.task_cmd == "claim":
            return lambda: svc.claim(a.run_id)
        return lambda: svc.submit(a.run_id, a.task_token, a.result, a.actor)
    if a.cmd == "lit":
        if a.lit_cmd == "search":
            return lambda: svc.lit_search(a.run_id, a.provider, a.query, a.limit)
        return lambda: svc.lit_fetch(a.run_id, a.source_id)
    if a.cmd == "verify-human":
        return lambda: svc.verify_human(a.run_id, a.finding, a.result, a.rationale, a.material, a.impact)
    if a.cmd in ("pause", "resume", "cancel"):
        return lambda: getattr(svc, a.cmd)(a.run_id)
    if a.cmd == "budget":
        return lambda: svc.budget_set(a.run_id, a.revision, a.authorization)
    if a.cmd == "export":
        return lambda: svc.export(a.run_id, a.audience, a.format.split(",") if a.format else None)
    if a.cmd == "compare":
        return lambda: svc.compare(a.old, a.new)
    if a.cmd == "install":
        return lambda: svc.install(a.host, a.scope, a.project_root, a.dry_run)
    if a.cmd == "schemas":
        return lambda: svc.schemas(a.out)
    if a.cmd == "auto":
        def _auto():
            if a.slice < 60:
                raise SystemExit("--slice must be at least 60 seconds")
            if a.run_id:
                return svc.auto(run_id=a.run_id, slice_seconds=a.slice)
            return svc.auto(params=_params_from_args(a), project_config=_load_json(a.config),
                            paper_version_id=a.paper_version, slice_seconds=a.slice)
        return _auto
    if a.cmd == "worker":
        return lambda: svc.worker_run(a.run_id, a.max_tasks, a.timeout, a.parallel)
    raise SystemExit(f"unknown command {a.cmd}")


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    env, code = envelope(lambda: dispatch(args)())
    sys.stdout.write(json.dumps(env, ensure_ascii=False, indent=2) + "\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
