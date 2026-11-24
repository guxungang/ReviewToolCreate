"""Budget ledger (E01, E10) and configuration resolution."""
import threading

import pytest

from tests.conftest import core_not_checked, run_claims

from scholarly_review.core import budget as bud
from scholarly_review.core.config import resolve_config
from scholarly_review.core.errors import SRError
from scholarly_review.core.store import Workspace
from tests.conftest import plan_and_start, understand_output


def test_pool_caps_floor_reserves():
    assert bud.pool_cap(12, "analysis", 0.2, 0.1) == 9
    assert bud.pool_cap(12, "verification", 0.2, 0.1) == 11
    assert bud.pool_cap(12, "finalization", 0.2, 0.1) == 12
    assert bud.pool_cap(None, "analysis", 0.2, 0.1) is None


def test_e01_concurrent_reservations_never_overspend(tmp_path):
    root = tmp_path / "ws"
    ws = Workspace.open(root)
    with ws.tx() as c:
        acct = bud.create_account(c, {"max_tasks": 10, "verification_reserve_ratio": 0.2,
                                      "finalization_reserve_ratio": 0.1})
    ws.close()
    ok, denied = [], []
    barrier = threading.Barrier(12)

    def worker(i):
        w = Workspace.open(root)
        barrier.wait()
        try:
            with w.tx() as c:
                bud.reserve(c, account_id=acct, run_id="r", task_id=f"t{i}", attempt_id=f"a{i}",
                            pool="analysis", amounts={"tasks": 1})
            ok.append(i)
        except SRError as e:
            assert e.category == "budget_exhausted"
            denied.append(i)
        finally:
            w.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(ok) == 7 and len(denied) == 5  # analysis pool = 10 - 2 - 1


def test_budget_exhaustion_skips_optional_and_blocks_required(kernel, paper):
    k = kernel
    run_id, pv = plan_and_start(k, paper, effort="quick", budget={"max_tasks": 3})
    k.submit(k.claim(run_id)["task"]["task_token"], understand_output(k, pv))
    # analysis pool for max_tasks=3 is 3 - 0 - 0 = 3; numeric uses the 2nd, one more check, then stop
    seen = 0
    while True:
        r = k.claim(run_id)
        if r["task"] is None:
            break
        t = r["task"]
        out = ({"output": {"checks": [], "analyzed_block_ids": []}} if t["task_type"] == "numeric_extract" else
               {"output": {"check_type": t["check_type"], "evidence": [],
                           "assessments": core_not_checked(t["check_type"], run_claims(k, run_id)), "findings": [],
                           "analyzed_block_ids": []}})
        k.submit(t["task_token"], out)
        seen += 1
    st = k.status(run_id)
    assert st["state"] == "partial" and st["reason"] == "budget"
    states = {(t["required"], t["state"], t["reason"]) for t in st["tasks"]["items"]}
    assert (1, "blocked", "budget") in states
    assert st["budget"]["dimensions"]["tasks"]["settled"] <= 3


def test_e10_resume_keeps_ledger_and_time(kernel, paper):
    k = kernel
    run_id, pv = plan_and_start(k, paper)
    r = k.claim(run_id)
    assert k.pause(run_id)["state"] == "pausing"
    k.submit(r["task"]["task_token"], understand_output(k, pv))
    assert k._run(run_id)["state"] == "paused"
    before = k.status(run_id)
    k.resume(run_id)
    after = k.status(run_id)
    assert after["budget"]["dimensions"]["tasks"]["settled"] == before["budget"]["dimensions"]["tasks"]["settled"] == 1
    assert after["active_seconds"] >= before["active_seconds"]


def test_budget_revision_is_cumulative(kernel, paper):
    k = kernel
    run_id, pv = plan_and_start(k, paper)
    k.submit(k.claim(run_id)["task"]["task_token"], understand_output(k, pv))
    with pytest.raises(SRError):
        k.revise_budget(run_id, {"max_tasks": 0}, "user: lower it")
    with pytest.raises(SRError):
        k.revise_budget(run_id, {"max_tasks": 100}, "")
    assert k.revise_budget(run_id, {"max_tasks": 100}, "user said: allow 100 tasks")["limits"]["max_tasks"] == 100


def test_config_merge_order_null_and_zero():
    cfg, prov = resolve_config({"budget": {"max_tasks": 30}},
                               {"input": {"manuscript": "x.pdf"}, "effort": "quick",
                                "budget": {"max_total_tokens": None, "max_search_requests": 0}})
    assert cfg.budget.max_tasks == 30 and prov["max_tasks"] == "project_config"
    assert cfg.budget.max_total_tokens is None and prov["max_total_tokens"] == "user_explicit"
    assert cfg.budget.max_search_requests == 0
    assert cfg.budget.max_active_seconds == 600 and cfg.budget.max_gpu_seconds == 0
    assert cfg.scope.domain_profile == "ml-v1" and cfg.outputs.audience == "author_private"
    rcfg, _ = resolve_config(None, {"input": {"manuscript": "x"}, "mode": "reviewer"})
    assert rcfg.outputs.audience == "reviewer_private"


@pytest.mark.parametrize("params,category", [
    ({"execution_mode": "managed"}, "missing_capability"),
    ({"permissions": {"classification": "local_only"}}, "policy_denied"),
    ({"budget": {"require_request_bounded_cost": True}}, "missing_capability"),
    ({"budget": {"unknown_key": 1}}, "schema_invalid"),
    ({"effort": "huge"}, "invalid_input"),
])
def test_config_refuses_unsupported_requirements(params, category):
    with pytest.raises(SRError) as ei:
        resolve_config(None, {"input": {"manuscript": "x"}, **params})
    assert ei.value.category == category


def test_verification_overflow_does_not_eat_analysis_share(tmp_path):
    """Real-run regression: verification spending beyond its reserve used to block analysis early."""
    ws = Workspace.open(tmp_path / "ws")
    with ws.tx() as c:
        acct = bud.create_account(c, {"max_tasks": 12, "verification_reserve_ratio": 0.2,
                                      "finalization_reserve_ratio": 0.1})
        for i in range(7):
            bud.reserve(c, account_id=acct, run_id="r", task_id=f"a{i}", attempt_id=None, pool="analysis", amounts={"tasks": 1})
        for i in range(3):
            bud.reserve(c, account_id=acct, run_id="r", task_id=f"v{i}", attempt_id=None, pool="verification", amounts={"tasks": 1})
        bud.reserve(c, account_id=acct, run_id="r", task_id="a7", attempt_id=None, pool="analysis", amounts={"tasks": 1})
        with pytest.raises(SRError):  # 12th unit is the untouched finalization reserve
            bud.reserve(c, account_id=acct, run_id="r", task_id="a8", attempt_id=None, pool="analysis", amounts={"tasks": 1})
        bud.reserve(c, account_id=acct, run_id="r", task_id="fin", attempt_id=None, pool="finalization", amounts={"tasks": 1})
    ws.close()
