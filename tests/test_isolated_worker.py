"""Isolated verification worker (claude -p) driven by the kernel, with a simulated event stream."""
import json
import os
from pathlib import Path

import pytest

from scholarly_review.adapters import worker
from scholarly_review.core.errors import SRError
from tests.conftest import core_not_checked, plan_and_start, understand_output
from tests.test_e2e import claims_by_key, run_to_end


def _gate_finding(k, pv, c2):
    from tests.conftest import block_with
    b = block_with(k, pv, "twice the training data")
    return {"output": {"check_type": "ml.ablation_attribution",
                       "evidence": [{"key": "e1", "kind": "manuscript", "content_summary": "data doubled",
                                     "source_refs": [{"block_id": b, "quote": "twice the training data of the baseline"}],
                                     "method": "read Sec. 2"}],
                       "assessments": [{"claim_id": c2, "outcome": "insufficient_evidence", "evidence_keys": ["e1"],
                                        "scope_checked": "main comparison"}],
                       "findings": [{"key": "f1", "kind": "concern",
                                     "proposition": "Main-table gain mixes the gate with doubled data.",
                                     "claim_ids": [c2], "evidence_keys": ["e1"], "potential_impact": "major",
                                     "impact_rationale": "core comparative claim", "confidence_basis": "Sec. 2",
                                     "issue_type": "attribution",
                                     "neutral_question": "Is the main-table comparison run with equal training data?"}],
                       "analyzed_block_ids": [b], "not_checked": []}}


def _prepare(k, paper, verify_worker="claude"):
    run_id, pv = plan_and_start(k, paper, host={"name": "claude", "verify_worker": verify_worker})
    first = k.claim(run_id)
    k.submit(first["task"]["task_token"], understand_output(k, pv))
    c2 = claims_by_key(k, run_id)["it improves accuracy"]
    empty_numeric = {"output": {"checks": [], "analyzed_block_ids": [], "not_extractable": []}}

    def verify_h(r):
        item = json.loads(Path(r["files"]["context"]).read_text())["verification_items"][0]
        return {"output": validated({"verification_items": [item]})}
    seen, last = run_to_end(k, run_id, pv, {"ml.ablation_attribution": lambda r: _gate_finding(k, pv, c2),
                                            "numeric.consistency": lambda r: empty_numeric, "verify": verify_h})
    return run_id, pv, seen, last


def _ctx_from_prompt(prompt):
    body = prompt.split("=== TASK CONTEXT (JSON) ===\n", 1)[1].split("\n\n=== MANUSCRIPT", 1)[0]
    return json.loads(body)


def fake_runner(result_for, *, tools=("StructuredOutput",), extra_call=None, denials=0, delay=0.0):
    """Simulates `claude -p --output-format stream-json` (bundle inline on stdin)."""
    import threading
    import time as _t
    calls = []
    state = {"now": 0, "peak": 0}
    lock = threading.Lock()

    def run(cmd, cwd, timeout, stdin_text):
        with lock:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        try:
            calls.append({"cmd": cmd, "cwd": cwd, "files": sorted(os.listdir(cwd)), "prompt": stdin_text})
            if delay:
                _t.sleep(delay)
            out = result_for(_ctx_from_prompt(stdin_text), stdin_text)
            content = [{"type": "tool_use", "name": "StructuredOutput", "input": out or {}}]
            if extra_call:
                content.insert(0, {"type": "tool_use", "name": extra_call, "input": {}})
            ev = [{"type": "system", "subtype": "init", "cwd": str(cwd), "tools": list(tools), "mcp_servers": [],
                   "model": "claude-test", "session_id": "s1", "claude_code_version": "9.9"},
                  {"type": "assistant", "message": {"content": content}},
                  {"type": "result", "is_error": False, "structured_output": out, "total_cost_usd": 0.12,
                   "usage": {"input_tokens": 10, "cache_creation_input_tokens": 40000, "output_tokens": 200},
                   "duration_ms": 5000, "permission_denials": [{"tool_name": "Read"}] * denials}]
            return [json.dumps(e) for e in ev]
        finally:
            with lock:
                state["now"] -= 1
    run.calls = calls
    run.state = state
    return run


def validated(ctx, prompt=None):
    item = ctx["verification_items"][0]
    return {"context_independence": "fresh", "blinded": True, "verifications": [{
        "finding_id": item["finding_id"], "method": "re-read Sec. 2/3", "result": "validated",
        "rationale": "data doubled for GateNet only", "confirmed_impact": "major"}]}


def test_host_claim_skips_verify_and_points_to_worker(kernel, paper):
    run_id, pv, seen, last = _prepare(kernel, paper)
    assert "verify" not in seen
    assert last["waiting_on_worker"] >= 1 and "sreview worker run" in last["next"]


def test_isolated_worker_can_confirm_major(kernel, paper):
    k = kernel
    run_id, pv, _, _ = _prepare(k, paper)
    runner = fake_runner(validated)
    out = worker.run_verify_workers(k, run_id, runner=runner, binary="claude")
    assert out["workers"] and all(w["status"] == "submitted" for w in out["workers"])
    call = runner.calls[0]
    assert call["files"] == []  # nothing on disk: the bundle travels inline on stdin
    assert "=== MANUSCRIPT (annotated blocks) ===" in call["prompt"] and "blinded verification" in call["prompt"]
    assert "--tools" in call["cmd"] and call["cmd"][call["cmd"].index("--tools") + 1] == ""
    assert "--strict-mcp-config" in call["cmd"] and "--no-session-persistence" in call["cmd"]
    assert not Path(call["cwd"]).exists()
    gate = next(f for f in k.ws.records(run_id, "finding") if f["proposition"].startswith("Main-table"))
    assert gate["disposition"] == "confirmed" and gate["impact"] == "major"
    v = next(v for v in k.ws.records(run_id, "verification") if v["finding_id"] == gate["finding_id"])
    assert v["context_independence"] == "fresh" and v["blinded"] is True
    assert v["method"].startswith("isolated worker")
    assert any("not a statistically independent" in x for x in v["limitations"])
    assert k.claim(run_id)["task"] is None  # host loop can finish afterwards


@pytest.mark.parametrize("kw", [{"tools": ("Read", "Bash", "StructuredOutput")}, {"extra_call": "Read"},
                                {"denials": 1}])
def test_unproven_isolation_is_recorded_inherited(kernel, paper, kw):
    k = kernel
    run_id, pv, _, _ = _prepare(k, paper)
    worker.run_verify_workers(k, run_id, runner=fake_runner(validated, **kw), binary="claude")
    gate = next(f for f in k.ws.records(run_id, "finding") if f["proposition"].startswith("Main-table"))
    assert gate["disposition"] == "unresolved"
    v = next(v for v in k.ws.records(run_id, "verification") if v["finding_id"] == gate["finding_id"])
    assert v["context_independence"] == "inherited"


def test_worker_without_result_releases_the_attempt(kernel, paper):
    k = kernel
    run_id, pv, _, _ = _prepare(k, paper)
    out = worker.run_verify_workers(k, run_id, runner=fake_runner(lambda ctx, p: None), binary="claude", max_tasks=1)
    assert out["workers"][0]["status"] == "no_result"
    nxt = k.claim(run_id, worker=True)  # not stuck behind a 30-minute lease
    assert nxt["task"] is not None and nxt["task"]["task_type"] == "verify"


def test_host_cannot_claim_fresh_through_normal_submit(kernel, paper):
    k = kernel
    run_id, pv, _, _ = _prepare(k, paper, verify_worker="none")  # host verifies itself
    gate = next(f for f in k.ws.records(run_id, "finding") if f["proposition"].startswith("Main-table"))
    assert gate["disposition"] == "unresolved"


def test_worker_command_requires_opt_in(kernel, paper):
    run_id, pv = plan_and_start(kernel, paper)
    with pytest.raises(SRError):
        worker.run_verify_workers(kernel, run_id, runner=fake_runner(validated), binary="claude")


def test_worker_proof_rejected_for_tasks_not_assigned_to_workers(kernel, paper):
    run_id, pv = plan_and_start(kernel, paper, host={"name": "claude", "verify_worker": "claude"})
    r = kernel.claim(run_id)
    with pytest.raises(SRError):
        kernel.submit(r["task"]["task_token"], understand_output(kernel, pv), worker_proof={"isolated": True})


# ---------------------------------------------------------------- analysis workers

def _analysis_result(k, pv, bad_first=None):
    seen = {"understand": 0}

    def result_for(ctx, prompt):
        t = ctx["task_type"]
        if prompt.startswith("You plan literature searches"):
            return {"queries": [{"provider": "openalex", "query": "sparse gating text classification"}]}
        if t == "literature":
            assert "=== LITERATURE SEARCHES (run by the kernel) ===" in prompt
            return {"check_type": ctx["check_type"], "evidence": [], "assessments": [], "findings": [],
                    "analyzed_block_ids": [], "not_checked": ["closest prior work: searches failed"]}
        if t == "understand":
            seen["understand"] += 1
            if bad_first and seen["understand"] == 1:
                return {"summary": {}}  # schema-invalid on purpose
            return understand_output(k, pv)["output"]
        if t == "numeric_extract":
            return {"checks": [], "analyzed_block_ids": [], "not_extractable": []}
        if t == "check":
            return {"check_type": ctx["check_type"], "evidence": [],
                    "assessments": core_not_checked(ctx["check_type"], ctx["claims"]), "findings": [],
                    "analyzed_block_ids": [], "not_checked": []}
        if t == "verify":
            return validated(ctx)
        raise AssertionError(t)
    return result_for


def _no_network(url, hosts, max_bytes):
    from scholarly_review.core.errors import SRError as E
    raise E("provider_timeout", "offline test", retryable=True)


def test_analysis_workers_run_every_task_including_literature(kernel, paper):
    k = kernel
    run_id, pv = plan_and_start(k, paper, host={"name": "claude", "analysis_worker": "claude",
                                                "verify_worker": "claude"})
    first = k.claim(run_id)
    assert first["task"] is None and first["waiting_on_worker"] >= 1  # even `understand` goes to a worker
    runner = fake_runner(_analysis_result(k, pv), delay=0.05)
    out = worker.run_workers(k, run_id, runner=runner, binary="claude", max_tasks=50, search_fetch=_no_network)
    types = [w["task_type"] for w in out["workers"]]
    assert types[0] == "understand" and {"numeric_extract", "check", "literature"} <= set(types)
    assert all(w["status"] == "submitted" for w in out["workers"]), out["workers"]
    lit = next(w for w in out["workers"] if w["task_type"] == "literature")
    assert lit["input_tokens"] == 2 * 40010  # query planning + judging
    assert runner.state["peak"] == 2  # standard: max_concurrency 2
    planning = next(c["prompt"] for c in runner.calls if c["prompt"].startswith("You plan literature searches"))
    assert "=== MANUSCRIPT" not in planning  # query planning never sees manuscript text
    assert k.ws.q1("SELECT COUNT(*) AS n FROM searches WHERE run_id=?", (run_id,))["n"] == 1
    assert k.claim(run_id)["task"] is None and k.status(run_id)["state"] in ("completed", "partial")


def test_auto_runs_everything_in_one_command(kernel, paper):
    from scholarly_review.adapters import auto
    k = kernel
    params = {"input": {"manuscript": paper}, "mode": "author", "effort": "quick",
              "scope": {"domain_profile": "ml-v1"}, "outputs": {"language": "en"}}
    runner = fake_runner(_analysis_result_lazy(k))
    res = auto.auto(k, params=params, runner=runner, binary="claude", search_fetch=_no_network)
    assert res["done"] is True and res["state"] in ("completed", "partial")
    assert Path(res["files"]["report.md"]).exists()
    assert res["usage"]["worker_calls"] >= 4 and res["plan"]["effort"] == "quick"
    assert len(json.dumps(res)) < 6000  # compact: the host reads this, not the bundles
    again = auto.auto(k, run_id=res["run_id"], runner=runner, binary="claude")
    assert again["done"] is True


def test_auto_stops_at_the_time_slice(kernel, paper):
    from scholarly_review.adapters import auto
    k = kernel
    params = {"input": {"manuscript": paper}, "mode": "author", "effort": "quick",
              "scope": {"domain_profile": "ml-v1"}, "outputs": {"language": "en"}}
    runner = fake_runner(_analysis_result_lazy(k), delay=0.0)
    res = auto.auto(k, params=params, runner=runner, binary="claude", search_fetch=_no_network, slice_seconds=0)
    assert res["done"] is False and res["next"].startswith("sreview auto --run")


def _analysis_result_lazy(k):
    """Like _analysis_result, but finds the paper version from the task context at call time."""
    def result_for(ctx, prompt):
        if ctx["task_type"] == "understand":
            pv = k.ws.q1("SELECT paper_version_id FROM runs ORDER BY created_at DESC LIMIT 1")["paper_version_id"]
            return _analysis_result(k, pv)(ctx, prompt)
        return _analysis_result(k, None)(ctx, prompt)
    return result_for


def test_rejected_worker_result_is_retried_with_the_errors(kernel, paper):
    k = kernel
    run_id, pv = plan_and_start(k, paper, host={"name": "claude", "analysis_worker": "claude"})
    runner = fake_runner(_analysis_result(k, pv, bad_first=True))
    out = worker.run_workers(k, run_id, runner=runner, binary="claude", max_tasks=2, parallel=1)
    assert [w["status"] for w in out["workers"]] == ["schema_invalid", "submitted"]
    assert "previous attempt for this task was rejected" in runner.calls[1]["prompt"]
    assert "previous attempt" not in runner.calls[0]["prompt"]
