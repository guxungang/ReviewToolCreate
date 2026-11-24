"""Kernel-side literature operations: permission, disclosure, budget, caching, linkage."""
from __future__ import annotations

import json
from typing import Optional

from ..models.common import now_utc
from ..modules import ingest, literature
from . import budget as bud
from .errors import SRError
from .kernel import Kernel


def _active_run(k: Kernel, run_id: str):
    run = k._run(run_id)
    if run["state"] not in ("running", "pausing"):
        raise SRError("invalid_state", f"run is {run['state']}; literature access needs a running run")
    return run, json.loads(run["config_json"])


def lit_search(k: Kernel, run_id: str, provider: str, query: str, limit: int = 10,
               fetch: literature.Fetcher = literature.http_get) -> dict:
    run, cfg = _active_run(k, run_id)
    perms = cfg["permissions"]
    if provider not in literature.PROVIDERS:
        raise SRError("policy_denied", f"unknown provider '{provider}' (no fallback to other sources)",
                      details={"known": sorted(literature.PROVIDERS)})
    if provider not in perms["search_providers"]:
        raise SRError("policy_denied", f"provider '{provider}' is not permitted for this run",
                      details={"permitted": perms["search_providers"]})
    if perms["classification"] == "local_only":
        raise SRError("policy_denied", "local_only runs may not use external search")
    summary = json.loads(run["summary_json"] or "{}")
    title = (summary.get("paper_summary") or {}).get("title")
    literature.check_disclosure(query, k.docmap(run["paper_version_id"]), title, perms["search_disclosure"])
    limits = None
    with k.ws.tx() as c:
        limits = bud.account_limits(c, run["budget_account_id"])
        entry, _ = bud.reserve(c, account_id=run["budget_account_id"], run_id=run_id, task_id=None, attempt_id=None,
                               pool="analysis", amounts={"search_requests": 1}, note=f"search:{provider}")
    max_bytes = 5_000_000
    if limits.get("max_download_bytes") is not None:
        cons = bud.consumption(k.ws.conn, run["budget_account_id"])
        used = cons["settled"].get("download_bytes", 0) + cons["in_flight"].get("download_bytes", 0)
        max_bytes = max(0, min(max_bytes, limits["max_download_bytes"] - used))
    result = literature.search(provider, query, limit, cfg["rubric"]["submission_cutoff"], fetch, max_bytes)
    search_id = literature.new_search_id()
    with k.ws.tx() as c:
        bud.settle(c, entry, {"search_requests": 1}, "observed")
        c.execute("INSERT INTO searches VALUES(?,?,?,?,?,?)",
                  (search_id, run_id, provider, query, json.dumps(result, ensure_ascii=False), now_utc()))
        for r in result["records"]:
            c.execute("INSERT INTO sources(source_id,json,created_at) VALUES(?,?,?) "
                      "ON CONFLICT(source_id) DO UPDATE SET json=excluded.json",
                      (r["source_id"], json.dumps(r, ensure_ascii=False), now_utc()))
            c.execute("INSERT OR IGNORE INTO run_sources VALUES(?,?,?,?)", (run_id, r["source_id"], query, provider))
        k.ws.event(c, run_id, "literature_search", {"search_id": search_id, "provider": provider,
                                                    "status": result["provider_status"], "n": len(result["records"])})
    return {"search_id": search_id, **result}


def lit_fetch(k: Kernel, run_id: str, source_id: str, fetch: literature.Fetcher = literature.http_get) -> dict:
    run, cfg = _active_run(k, run_id)
    row = k.ws.q1("SELECT s.* FROM sources s JOIN run_sources rs USING(source_id) WHERE rs.run_id=? AND s.source_id=?",
                  (run_id, source_id))
    if row is None:
        raise SRError("not_found", "source not linked to this run; search for it first")
    src = json.loads(row["json"])
    if not source_id.startswith("arxiv:"):
        raise SRError("missing_capability", "v0.1 fetches full text from arXiv only")
    if "arxiv" not in cfg["permissions"]["search_providers"]:
        raise SRError("policy_denied", "arxiv access not permitted for this run")
    if row["fulltext_artifact_id"]:
        return {"source_id": source_id, "fulltext_artifact_id": row["fulltext_artifact_id"], "cached": True,
                "document": str(k.ws.artifact_path(row["fulltext_artifact_id"]))}
    with k.ws.tx() as c:
        limits = bud.account_limits(c, run["budget_account_id"])
        cons = bud.consumption(c, run["budget_account_id"])
        used = cons["settled"].get("download_bytes", 0) + cons["in_flight"].get("download_bytes", 0)
        room = 50_000_000 if limits.get("max_download_bytes") is None else limits["max_download_bytes"] - used
        entry, _ = bud.reserve(c, account_id=run["budget_account_id"], run_id=run_id, task_id=None, attempt_id=None,
                               pool="analysis", amounts={"external_fulltexts": 1, "search_requests": 1},
                               note=f"fulltext:{source_id}")
    try:
        data = literature.fetch_arxiv_pdf(source_id.split(":", 1)[1], src["identifiers"].get("version"), fetch,
                                          max(0, min(room, 50_000_000)))
    except SRError:
        with k.ws.tx() as c:
            bud.settle(c, entry, {"search_requests": 1}, "observed")
        raise
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory(dir=k.ws.tmp_dir) as td:
        p = Path(td) / "paper.pdf"
        p.write_bytes(data)
        parsed = ingest.parse_pdf(p)
    text = ingest.annotated_markdown({"paper_version_id": source_id, "blocks": parsed["blocks"]})
    with k.ws.tx() as c:
        aid = k.ws.store_artifact(text.encode(), "literature_fulltext", "text/markdown", None, source_id, c=c)
        c.execute("UPDATE sources SET fulltext_artifact_id=? WHERE source_id=?", (aid, source_id))
        bud.settle(c, entry, {"external_fulltexts": 1, "search_requests": 1, "download_bytes": len(data)}, "observed")
    return {"source_id": source_id, "fulltext_artifact_id": aid, "access_level": "fulltext",
            "document": str(k.ws.artifact_path(aid)), "bytes": len(data)}
