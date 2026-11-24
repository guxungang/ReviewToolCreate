"""Shared service functions behind CLI (and a future MCP server): one business logic (spec §13.2)."""
from __future__ import annotations

import difflib
import json
from pathlib import Path
from typing import Any, Callable, Optional

from ..adapters import hosts
from ..core import litops
from ..core.errors import SRError
from ..core.kernel import Kernel
from ..core.store import Workspace
from ..models.common import new_id
from ..modules import ingest
from ..reports import render


def envelope(fn: Callable[[], Any]) -> tuple[dict, int]:
    rid = new_id("req")
    try:
        data = fn()
        warnings = data.pop("warnings", []) if isinstance(data, dict) else []
        return {"ok": True, "data": data, "error": None, "warnings": warnings, "request_id": rid}, 0
    except SRError as e:
        return {"ok": False, "data": e.details.get("data"), "error": e.to_dict(), "warnings": [], "request_id": rid}, e.exit_code
    except Exception as e:  # never leak secrets or full manuscripts: type and short message only
        return {"ok": False, "data": None, "warnings": [], "request_id": rid,
                "error": {"code": "internal_error", "category": "internal_error",
                          "message": f"{type(e).__name__}: {str(e)[:300]}", "retryable": False, "details": {}}}, 9


class Service:
    def __init__(self, workspace: str) -> None:
        self.ws = Workspace.open(workspace)
        self.k = Kernel(self.ws)

    def doctor(self, host: str) -> dict:
        return hosts.probe(host)

    def import_(self, manuscript: str, supplements: list[str], paper_id: Optional[str], title: Optional[str]) -> dict:
        return self.k.import_paper(manuscript, supplements, paper_id, title)

    def plan(self, params: dict, project_config: Optional[dict], paper_version_id: Optional[str],
             idempotency_key: Optional[str]) -> dict:
        return self.k.plan(params, project_config, paper_version_id, idempotency_key)

    def run(self, run_id: str) -> dict:
        return self.k.start(run_id)

    def status(self, run_id: str) -> dict:
        return self.k.status(run_id)

    def claim(self, run_id: str) -> dict:
        return self.k.claim(run_id)

    def submit(self, run_id: str, token: str, result_file: str, actor: str) -> dict:
        try:
            submission = json.loads(Path(result_file).read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise SRError("invalid_input", f"result file unreadable or not JSON: {e}") from e
        if not isinstance(submission, dict):
            raise SRError("schema_invalid", "result file must contain a JSON object")
        if "output" not in submission:  # convenience: accept a bare output object
            submission = {"output": submission}
        import hashlib
        att = self.ws.q1("SELECT run_id FROM attempts WHERE token_sha256=?", (hashlib.sha256(token.encode()).hexdigest(),))
        if att is not None and att["run_id"] != run_id:
            raise SRError("policy_denied", "task token does not belong to this run")
        return self.k.submit(token, submission, actor)

    def auto(self, **kw) -> dict:
        from ..adapters import auto
        return auto.auto(self.k, **kw)

    def worker_run(self, run_id: str, max_tasks: int, timeout: int, parallel: Optional[int] = None) -> dict:
        from ..adapters import worker
        return worker.run_workers(self.k, run_id, max_tasks=max_tasks, timeout=timeout, parallel=parallel)

    def lit_search(self, run_id: str, provider: str, query: str, limit: int) -> dict:
        return litops.lit_search(self.k, run_id, provider, query, limit)

    def lit_fetch(self, run_id: str, source_id: str) -> dict:
        return litops.lit_fetch(self.k, run_id, source_id)

    def verify_human(self, run_id: str, finding_id: str, result: str, rationale: str, material: str,
                     impact: Optional[str]) -> dict:
        return self.k.verify_human(run_id, finding_id, result, rationale, impact, material)

    def pause(self, run_id: str) -> dict:
        return self.k.pause(run_id)

    def resume(self, run_id: str) -> dict:
        return self.k.resume(run_id)

    def cancel(self, run_id: str) -> dict:
        return self.k.cancel(run_id)

    def budget_set(self, run_id: str, revision_file: str, authorization: str) -> dict:
        revision = json.loads(Path(revision_file).read_text("utf-8"))
        return self.k.revise_budget(run_id, revision, authorization)

    def export(self, run_id: str, audience: Optional[str], formats: Optional[list[str]]) -> dict:
        return render.export(self.k, run_id, audience, formats)

    def compare(self, old_version_id: str, new_version_id: str) -> dict:
        old, new = self.k.docmap(old_version_id), self.k.docmap(new_version_id)
        a = [ingest.normalize(b["text"]) for b in old["blocks"]]
        b = [ingest.normalize(x["text"]) for x in new["blocks"]]
        sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
        changes = []
        for op, i1, i2, j1, j2 in sm.get_opcodes():
            if op == "equal":
                continue
            changes.append({"op": op,
                            "old_blocks": [old["blocks"][i]["block_id"] for i in range(i1, i2)],
                            "new_blocks": [new["blocks"][j]["block_id"] for j in range(j1, j2)]})
        return {"old_version_id": old_version_id, "new_version_id": new_version_id,
                "similarity": round(sm.ratio(), 4), "changed_regions": changes,
                "next": "plan a new run on the new version; matching of claims/tables across versions is a candidate "
                        "mapping only and high-impact claims must be re-verified",
                "warnings": ["v0.1 compares text blocks only; claim/table/code matching is not implemented"]}

    def install(self, host: str, scope: str, project_root: Optional[str], dry_run: bool) -> dict:
        return hosts.install_skills(host, scope, project_root, dry_run)

    def schemas(self, out_dir: str) -> dict:
        from ..models import config, entities, tasks
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        models = {
            "RunConfig": config.RunConfig, "Claim": entities.Claim, "Hypothesis": entities.Hypothesis,
            "Evidence": entities.Evidence, "Assessment": entities.Assessment, "Finding": entities.Finding,
            "Verification": entities.Verification, "Coverage": entities.Coverage, "SearchResult": entities.SearchResult,
            "TaskRequest": tasks.TaskRequest, "TaskResult": tasks.TaskResult, "Submission": tasks.Submission,
            **{f"{n}Output": m for n, m in tasks.OUTPUT_MODELS.items()},
        }
        written = []
        for name, m in models.items():
            schema = m.model_json_schema()
            schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
            p = out / f"{name}.schema.json"
            p.write_text(json.dumps(schema, indent=2, ensure_ascii=False))
            written.append(str(p))
        return {"written": written}
