"""Workspace, SQLite state and content-addressed artifact store (spec §5.2, §11.2, §15.2).

Commit order for files: write to tmp -> fsync -> verify digest -> atomic rename into CAS ->
register in a short DB transaction. A file with no DB row is an orphan, never a result.
"""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Iterator, Optional

from ..models.common import new_id, now_utc, sha256_bytes
from .errors import SRError

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS artifacts(
  artifact_id TEXT PRIMARY KEY, sha256 TEXT NOT NULL, kind TEXT NOT NULL, media_type TEXT,
  size INTEGER NOT NULL, rel_path TEXT NOT NULL, scope_paper_id TEXT, original_name TEXT,
  created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_art_sha ON artifacts(sha256);
CREATE TABLE IF NOT EXISTS papers(
  paper_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, title TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS paper_versions(
  paper_version_id TEXT PRIMARY KEY, paper_id TEXT NOT NULL REFERENCES papers(paper_id),
  manifest_hash TEXT NOT NULL, manifest_json TEXT NOT NULL, docmap_artifact_id TEXT,
  annotated_artifact_id TEXT, state TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS budget_accounts(
  budget_account_id TEXT PRIMARY KEY, limits_json TEXT NOT NULL, created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs(
  run_id TEXT PRIMARY KEY, paper_version_id TEXT NOT NULL REFERENCES paper_versions(paper_version_id),
  budget_account_id TEXT NOT NULL REFERENCES budget_accounts(budget_account_id),
  state TEXT NOT NULL, reason TEXT, config_json TEXT NOT NULL, config_hash TEXT NOT NULL,
  provenance_json TEXT NOT NULL, idempotency_key TEXT UNIQUE, active_seconds REAL NOT NULL DEFAULT 0,
  running_since TEXT, exploration_stopped INTEGER NOT NULL DEFAULT 0, summary_json TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tasks(
  task_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id), type TEXT NOT NULL,
  check_type TEXT, pool TEXT NOT NULL, priority INTEGER NOT NULL, deps_json TEXT NOT NULL,
  payload_json TEXT NOT NULL, state TEXT NOT NULL, reason TEXT, attempts INTEGER NOT NULL DEFAULT 0,
  lease_generation INTEGER NOT NULL DEFAULT 0, required INTEGER NOT NULL, created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_tasks_run ON tasks(run_id, state);
CREATE TABLE IF NOT EXISTS attempts(
  attempt_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id), run_id TEXT NOT NULL,
  lease_generation INTEGER NOT NULL, token_sha256 TEXT NOT NULL UNIQUE, state TEXT NOT NULL,
  lease_expires TEXT NOT NULL, input_fingerprint TEXT NOT NULL, started_at TEXT NOT NULL,
  ended_at TEXT, idempotency_key TEXT, submission_sha256 TEXT, result_json TEXT);
CREATE TABLE IF NOT EXISTS ledger(
  entry_id TEXT PRIMARY KEY, budget_account_id TEXT NOT NULL, run_id TEXT NOT NULL, task_id TEXT,
  attempt_id TEXT, pool TEXT NOT NULL, reserved_json TEXT NOT NULL, usage_json TEXT,
  billing_state TEXT NOT NULL, amount_basis TEXT NOT NULL, note TEXT, created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_ledger_acct ON ledger(budget_account_id);
CREATE TABLE IF NOT EXISTS records(
  record_id TEXT PRIMARY KEY, run_id TEXT, paper_version_id TEXT NOT NULL, rtype TEXT NOT NULL,
  json TEXT NOT NULL, validity TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_records_run ON records(run_id, rtype);
CREATE TABLE IF NOT EXISTS sources(source_id TEXT PRIMARY KEY, json TEXT NOT NULL, fulltext_artifact_id TEXT,
  created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS run_sources(run_id TEXT NOT NULL, source_id TEXT NOT NULL, query TEXT,
  provider TEXT, PRIMARY KEY(run_id, source_id));
CREATE TABLE IF NOT EXISTS searches(search_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, provider TEXT NOT NULL,
  query TEXT NOT NULL, result_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, etype TEXT NOT NULL,
  json TEXT NOT NULL, ts TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS exports(export_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, audience TEXT NOT NULL,
  manifest_json TEXT NOT NULL, created_at TEXT NOT NULL);
"""


class Workspace:
    def __init__(self, root: str | os.PathLike) -> None:
        self.root = Path(root).resolve()
        self.db_path = self.root / "state.sqlite"
        self.artifacts_dir = self.root / "artifacts"
        self.runs_dir = self.root / "runs"
        self.exports_dir = self.root / "exports"
        self.cache_dir = self.root / "cache"
        self.tmp_dir = self.root / "tmp"
        self._conn: Optional[sqlite3.Connection] = None

    # ---------- lifecycle ----------
    @classmethod
    def open(cls, root: str | os.PathLike, create: bool = True) -> "Workspace":
        ws = cls(root)
        if not ws.root.exists():
            if not create:
                raise SRError("not_found", f"workspace {ws.root} does not exist")
            ws.root.mkdir(parents=True)
        for d in (ws.artifacts_dir, ws.runs_dir, ws.exports_dir, ws.cache_dir, ws.tmp_dir):
            d.mkdir(exist_ok=True)
        meta_file = ws.root / "workspace.json"
        if not meta_file.exists():
            meta_file.write_text(json.dumps({"workspace_id": new_id("ws"), "schema_version": "1.0.0",
                                             "created_at": now_utc()}, indent=2))
        ws.workspace_id = json.loads(meta_file.read_text())["workspace_id"]
        ws.conn.executescript(SCHEMA_SQL)
        with ws.tx() as c:
            c.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('db_version','1')")
        return ws

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            # Rollback journal (not WAL): workspaces often live in synced/network folders (spec §11.2).
            self._conn = sqlite3.connect(self.db_path, isolation_level=None, timeout=30, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=30000")
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @contextlib.contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Short IMMEDIATE transaction: the single-writer section of the coordinator."""
        c = self.conn
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
        except BaseException:
            c.execute("ROLLBACK")
            raise
        else:
            c.execute("COMMIT")

    def q(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, params))

    def q1(self, sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
        rows = self.q(sql, params)
        return rows[0] if rows else None

    # ---------- events ----------
    def event(self, c: sqlite3.Connection, run_id: Optional[str], etype: str, payload: dict) -> None:
        c.execute("INSERT INTO events(run_id,etype,json,ts) VALUES(?,?,?,?)",
                  (run_id, etype, json.dumps(payload, ensure_ascii=False), now_utc()))

    # ---------- CAS ----------
    def _cas_path(self, digest: str) -> Path:
        return self.artifacts_dir / digest[:2] / digest

    def put_bytes(self, data: bytes) -> tuple[str, Path]:
        digest = sha256_bytes(data)
        dest = self._cas_path(digest)
        if dest.exists():
            if sha256_bytes(dest.read_bytes()) == digest:
                return digest, dest
            dest.unlink()  # corrupt copy of an immutable object; rewrite
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.tmp_dir)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            if sha256_bytes(Path(tmp).read_bytes()) != digest:
                raise SRError("artifact_corrupt", "digest mismatch while writing artifact")
            os.replace(tmp, dest)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return digest, dest

    def register_artifact(self, c: sqlite3.Connection, digest: str, kind: str, size: int,
                          media_type: str = "application/octet-stream", paper_id: Optional[str] = None,
                          original_name: Optional[str] = None) -> str:
        artifact_id = new_id("art")
        rel = str(self._cas_path(digest).relative_to(self.root))
        c.execute("INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?,?)",
                  (artifact_id, digest, kind, media_type, size, rel, paper_id, original_name, now_utc()))
        return artifact_id

    def store_artifact(self, data: bytes, kind: str, media_type: str = "application/octet-stream",
                       paper_id: Optional[str] = None, original_name: Optional[str] = None,
                       c: Optional[sqlite3.Connection] = None) -> str:
        digest, _ = self.put_bytes(data)
        if c is not None:
            return self.register_artifact(c, digest, kind, len(data), media_type, paper_id, original_name)
        with self.tx() as c2:
            return self.register_artifact(c2, digest, kind, len(data), media_type, paper_id, original_name)

    def artifact_row(self, artifact_id: str) -> sqlite3.Row:
        row = self.q1("SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,))
        if row is None:
            raise SRError("not_found", f"artifact {artifact_id} not found")
        return row

    def artifact_path(self, artifact_id: str, verify: bool = True) -> Path:
        row = self.artifact_row(artifact_id)
        path = self.root / row["rel_path"]
        if verify:
            if not path.exists():
                raise SRError("artifact_corrupt", f"artifact {artifact_id} is missing on disk",
                              details={"artifact_id": artifact_id})
            if sha256_bytes(path.read_bytes()) != row["sha256"]:
                raise SRError("artifact_corrupt", f"artifact {artifact_id} failed its SHA-256 check",
                              details={"artifact_id": artifact_id})
        return path

    def read_artifact(self, artifact_id: str) -> bytes:
        return self.artifact_path(artifact_id).read_bytes()

    def read_json_artifact(self, artifact_id: str) -> Any:
        return json.loads(self.read_artifact(artifact_id).decode("utf-8"))

    # ---------- records ----------
    def put_record(self, c: sqlite3.Connection, rtype: str, record: dict) -> None:
        c.execute("INSERT INTO records(record_id,run_id,paper_version_id,rtype,json,validity,created_at) "
                  "VALUES(?,?,?,?,?,?,?)",
                  (record["record_id"], record.get("run_id"), record["paper_version_id"], rtype,
                   json.dumps(record, ensure_ascii=False), record.get("validity", "current"),
                   record["created_at"]))

    def update_record(self, c: sqlite3.Connection, record: dict) -> None:
        c.execute("UPDATE records SET json=?, validity=? WHERE record_id=?",
                  (json.dumps(record, ensure_ascii=False), record.get("validity", "current"), record["record_id"]))

    def records(self, run_id: str, rtype: str) -> list[dict]:
        return [json.loads(r["json"]) for r in
                self.q("SELECT json FROM records WHERE run_id=? AND rtype=? ORDER BY created_at, rowid",
                       (run_id, rtype))]

    def record(self, record_id: str) -> Optional[dict]:
        r = self.q1("SELECT json FROM records WHERE record_id=?", (record_id,))
        return json.loads(r["json"]) if r else None

    def run_dir(self, run_id: str) -> Path:
        d = self.runs_dir / run_id
        d.mkdir(parents=True, exist_ok=True)
        return d
