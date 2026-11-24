"""Budget account ledger with atomic reservation (spec §10).

Pools are cumulative ceilings, not spend targets: an analysis task may only bring total
consumption up to L - verification_reserve - finalization_reserve; a verification task up to
L - finalization_reserve; finalization up to L. Discrete reserves are floored and the remainder
goes to analysis. All checks run inside the caller's IMMEDIATE transaction, so concurrent
workers cannot both read the same balance and start (E01).
"""
from __future__ import annotations

import json
import math
import sqlite3
from typing import Optional

from ..models.common import new_id, now_utc
from .errors import SRError

# dimension -> Budget field
DIM_LIMIT_KEY = {
    "tasks": "max_tasks",
    "model_requests": "max_model_requests",
    "total_tokens": "max_total_tokens",
    "input_tokens": "max_input_tokens",
    "output_tokens": "max_output_tokens",
    "search_requests": "max_search_requests",
    "external_fulltexts": "max_external_fulltexts",
    "download_bytes": "max_download_bytes",
    "cpu_seconds": "max_cpu_seconds",
    "gpu_seconds": "max_gpu_seconds",
}
# In interactive mode the kernel cannot observe the host's token use: these are targets only.
ADVISORY_DIMS_INTERACTIVE = {"total_tokens", "input_tokens", "output_tokens"}
POOLS = ("analysis", "verification", "finalization")
OPEN_STATES = ("reserved", "in_flight", "pending_reconciliation")


def pool_cap(limit: Optional[int], pool: str, vr: float, fr: float) -> Optional[int]:
    if limit is None:
        return None
    ver = math.floor(limit * vr)
    fin = math.floor(limit * fr)
    if pool == "analysis":
        return max(limit - ver - fin, 0)
    if pool == "verification":
        return max(limit - fin, 0)
    return limit


def create_account(c: sqlite3.Connection, budget: dict) -> str:
    acct = new_id("bacct")
    ts = now_utc()
    c.execute("INSERT INTO budget_accounts VALUES(?,?,?,?)", (acct, json.dumps(budget), ts, ts))
    return acct


def account_limits(c: sqlite3.Connection, account_id: str) -> dict:
    row = c.execute("SELECT limits_json FROM budget_accounts WHERE budget_account_id=?", (account_id,)).fetchone()
    if row is None:
        raise SRError("not_found", f"budget account {account_id} not found")
    return json.loads(row[0])


def consumption(c: sqlite3.Connection, account_id: str) -> dict:
    """Return settled / in-flight totals per dimension, plus totals per pool."""
    settled: dict[str, float] = {}
    open_: dict[str, float] = {}
    by_pool: dict[str, dict[str, float]] = {p: {} for p in POOLS}
    for row in c.execute("SELECT reserved_json, usage_json, billing_state, pool FROM ledger WHERE budget_account_id=?",
                         (account_id,)):
        reserved = json.loads(row[0])
        usage = json.loads(row[1]) if row[1] else {}
        pool = by_pool.setdefault(row[3], {})
        if row[2] in OPEN_STATES:
            for d in set(reserved) | set(usage):
                v = max(reserved.get(d, 0) or 0, usage.get(d, 0) or 0)
                open_[d] = open_.get(d, 0) + v
                pool[d] = pool.get(d, 0) + v
        else:  # settled or released
            for d, v in usage.items():
                settled[d] = settled.get(d, 0) + (v or 0)
                pool[d] = pool.get(d, 0) + (v or 0)
    return {"settled": settled, "in_flight": open_, "by_pool": by_pool}


def pool_allows(limit: int, pool: str, amount: float, total: float, by_pool: dict, vr: float, fr: float) -> tuple[bool, float]:
    """Spec §10.2: analysis may use at most its own share; every pool must leave the *unused* part of
    the later reserves intact; overall consumption never exceeds the limit. Returns (ok, ceiling)."""
    ver = math.floor(limit * vr)
    fin = math.floor(limit * fr)
    ver_left = max(ver - by_pool.get("verification", 0), 0)
    fin_left = max(fin - by_pool.get("finalization", 0), 0)
    if pool == "analysis":
        own_cap = limit - ver - fin
        ceiling = limit - ver_left - fin_left
        ok = by_pool.get("analysis", 0) + amount <= own_cap and total + amount <= ceiling
        return ok, min(own_cap, ceiling)
    if pool == "verification":
        ceiling = limit - fin_left
        return total + amount <= ceiling, ceiling
    return total + amount <= limit, limit


def reserve(c: sqlite3.Connection, *, account_id: str, run_id: str, task_id: Optional[str],
            attempt_id: Optional[str], pool: str, amounts: dict, interactive: bool = True,
            note: str = "") -> tuple[str, list[str]]:
    """Atomically check and reserve. Raises budget_exhausted; returns (entry_id, warnings)."""
    limits = account_limits(c, account_id)
    vr = limits.get("verification_reserve_ratio", 0.2)
    fr = limits.get("finalization_reserve_ratio", 0.1)
    cons = consumption(c, account_id)
    warnings: list[str] = []
    for dim, amount in amounts.items():
        if not amount:
            continue
        limit = limits.get(DIM_LIMIT_KEY[dim])
        if limit is None:
            continue
        used = cons["settled"].get(dim, 0) + cons["in_flight"].get(dim, 0)
        per_pool = {p: v.get(dim, 0) for p, v in cons["by_pool"].items()}
        ok, cap = pool_allows(limit, pool, amount, used, per_pool, vr, fr)
        if not ok:
            if interactive and dim in ADVISORY_DIMS_INTERACTIVE:
                warnings.append(f"advisory target exceeded: {dim} estimated {used + amount:.0f} > {cap} ({pool} pool)")
                continue
            raise SRError("budget_exhausted", f"budget exhausted for {dim} in {pool} pool",
                          details={"dimension": dim, "pool": pool, "used_or_reserved": used,
                                   "requested": amount, "pool_ceiling": cap, "limit": limit})
    entry_id = new_id("led")
    ts = now_utc()
    c.execute("INSERT INTO ledger VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (entry_id, account_id, run_id, task_id, attempt_id, pool, json.dumps(amounts), None,
               "reserved", "estimated", note, ts, ts))
    return entry_id, warnings


def settle(c: sqlite3.Connection, entry_id: str, usage: dict, basis: str = "estimated") -> None:
    c.execute("UPDATE ledger SET usage_json=?, billing_state='settled', amount_basis=?, updated_at=? WHERE entry_id=?",
              (json.dumps(usage), basis, now_utc(), entry_id))


def mark_unknown_outcome(c: sqlite3.Connection, entry_id: str) -> None:
    """Call issued but outcome unknown: keep the reservation, never treat as a free failure (E02)."""
    c.execute("UPDATE ledger SET billing_state='pending_reconciliation', amount_basis='unknown', updated_at=? "
              "WHERE entry_id=?", (now_utc(), entry_id))


def release(c: sqlite3.Connection, entry_id: str, usage: Optional[dict] = None) -> None:
    c.execute("UPDATE ledger SET usage_json=?, billing_state='released', updated_at=? WHERE entry_id=?",
              (json.dumps(usage or {}), now_utc(), entry_id))


def entry_for_attempt(c: sqlite3.Connection, attempt_id: str) -> Optional[sqlite3.Row]:
    return c.execute("SELECT * FROM ledger WHERE attempt_id=? ORDER BY created_at LIMIT 1", (attempt_id,)).fetchone()


def budget_report(c: sqlite3.Connection, account_id: str, interactive: bool = True) -> dict:
    limits = account_limits(c, account_id)
    cons = consumption(c, account_id)
    dims = {}
    for dim, key in DIM_LIMIT_KEY.items():
        limit = limits.get(key)
        s = cons["settled"].get(dim, 0)
        f = cons["in_flight"].get(dim, 0)
        if limit is None and not s and not f:
            continue
        dims[dim] = {
            "limit": limit,
            "settled": s,
            "in_flight": f,
            "remaining": None if limit is None else max(limit - s - f, 0),
            "enforcement": "advisory_estimate" if (interactive and dim in ADVISORY_DIMS_INTERACTIVE) else "kernel_gated",
        }
    unknown = c.execute("SELECT COUNT(*) FROM ledger WHERE budget_account_id=? AND billing_state='pending_reconciliation'",
                        (account_id,)).fetchone()[0]
    return {
        "budget_account_id": account_id,
        "dimensions": dims,
        "max_estimated_cost": limits.get("max_estimated_cost"),
        "currency": limits.get("currency"),
        "monetary_cost_observed": None,
        "pending_reconciliation_entries": unknown,
        "reserve_ratios": {"verification": limits.get("verification_reserve_ratio"),
                           "finalization": limits.get("finalization_reserve_ratio")},
    }


def revise_limits(c: sqlite3.Connection, account_id: str, revision: dict) -> dict:
    """New values are cumulative totals for the account, never a fresh per-run allowance."""
    limits = account_limits(c, account_id)
    cons = consumption(c, account_id)
    for key, value in revision.items():
        if key not in limits:
            raise SRError("invalid_input", f"unknown budget key '{key}'")
        dim = next((d for d, k in DIM_LIMIT_KEY.items() if k == key), None)
        if dim and value is not None:
            used = cons["settled"].get(dim, 0) + cons["in_flight"].get(dim, 0)
            if value < used:
                raise SRError("invalid_input", f"{key}={value} is below what is already consumed or reserved ({used})")
        limits[key] = value
    c.execute("UPDATE budget_accounts SET limits_json=?, updated_at=? WHERE budget_account_id=?",
              (json.dumps(limits), now_utc(), account_id))
    return limits
