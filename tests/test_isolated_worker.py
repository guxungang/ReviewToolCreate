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
    body = prompt.split("=== TASK CONTEXT (JSON) ===\n", 1)[1].split("\n\n=== LITERATURE SEARCHES", 1)[0]
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
    system = call["cmd"][call["cmd"].index("--append-system-prompt") + 1]
    assert "=== MANUSCRIPT (annotated blocks) ===" in system and "=== MANUSCRIPT" not in call["prompt"]
    assert "blinded verification" in call["prompt"]
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
    # the one planned query fails without network and is re-run once on the other permitted provider
    assert k.ws.q1("SELECT COUNT(*) AS n FROM searches WHERE run_id=?", (run_id,))["n"] == 2
    assert any("re-ran on" in n for n in lit["search_problems"])
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


def test_failed_searches_fall_back_to_another_permitted_provider(kernel, paper):
    """2026-11-24 public-paper run: four arXiv queries all timed out or were rate limited, OpenAlex never tried."""
    import json as _json
    from scholarly_review.adapters import worker
    from scholarly_review.core.errors import SRError as _E
    k = kernel
    run_id, pv = plan_and_start(k, paper, effort="quick")
    calls = []

    def fetch(url, hosts, max_bytes):
        calls.append(url.split("/")[2])
        if "arxiv" in url:
            raise _E("provider_timeout", "network failure contacting export.arxiv.org: timed out", retryable=True)
        return _json.dumps({"results": [{"id": "https://openalex.org/W1", "title": "Prior work",
                                         "publication_date": "2015-01-01"}], "meta": {"count": 1}}).encode()

    qs = [{"provider": "arxiv", "query": q} for q in
          ("sparse gating text classification", "mixture of experts routing", "conditional computation", "gated layers")]
    results, notes = worker._search_all(k, run_id, qs, fetch, ["arxiv", "openalex"], sleep=lambda s: None)
    ok = [r for r in results if r["status"] != "failed"]
    assert len(ok) == 4 and all(r["provider"] == "openalex" for r in ok)
    assert calls.count("export.arxiv.org") == 2  # circuit breaker: arXiv is skipped after two failures
    assert any("skipped after repeated failures" in n for n in notes)
    # a provider the run does not permit is never used, even as a fallback
    results, notes = worker._search_all(k, run_id, qs[:1], fetch, ["arxiv"], sleep=lambda s: None)
    assert [r["provider"] for r in results] == ["arxiv"] and results[0]["status"] == "failed"


def fake_codex_runner(result_for, *, tool_item=None, fenced=False):
    """Simulates `codex exec --json ... -` : JSONL events on stdout, final answer in --output-last-message."""
    calls = []

    def run(cmd, cwd, timeout, stdin_text):
        calls.append({"cmd": cmd, "cwd": cwd, "prompt": stdin_text})
        prompt = stdin_text.split("\n\nYou are running under `codex exec`", 1)[0]
        out = result_for(_ctx_from_prompt(prompt), prompt)
        text = json.dumps(out)
        if fenced:
            text = "```json\n" + text + "\n```"
        Path(cmd[cmd.index("--output-last-message") + 1]).write_text(text)
        ev = [{"type": "thread.started", "thread_id": "th1"}, {"type": "turn.started"}]
        if tool_item:
            ev.append({"type": "item.completed", "item": {"id": "i0", "type": tool_item, "command": "cat x"}})
        ev += [{"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": text}},
               {"type": "turn.completed", "usage": {"input_tokens": 30000, "cached_input_tokens": 0, "output_tokens": 900}}]
        return [json.dumps(e) for e in ev]
    run.calls = calls
    return run


def test_codex_engine_runs_the_whole_review(kernel, paper):
    from scholarly_review.adapters import auto
    k = kernel
    params = {"input": {"manuscript": paper}, "mode": "author", "effort": "quick", "host": {"name": "codex"},
              "scope": {"domain_profile": "ml-v1"}, "outputs": {"language": "en"}}
    runner = fake_codex_runner(_analysis_result_lazy(k), fenced=True)
    res = auto.auto(k, params=params, runner=runner, binary="codex", search_fetch=_no_network)
    assert res["done"] is True and Path(res["files"]["report.md"]).exists()
    call = runner.calls[0]
    assert call["cmd"][:2] == ["codex", "exec"] and call["cmd"][-1] == "-"
    assert call["cmd"][call["cmd"].index("--sandbox") + 1] == "read-only"
    assert call["cmd"][call["cmd"].index("-C") + 1] == str(call["cwd"])
    assert "Answer with ONE JSON object only" in call["prompt"] and '"properties"' in call["prompt"]
    assert res["usage"]["worker_input_tokens"] >= 30000 and res["usage"]["worker_cost_usd"] == 0


def test_codex_verify_is_fresh_only_without_tool_use(kernel, paper):
    k = kernel
    run_id, pv, _, _ = _prepare(k, paper, verify_worker="codex")
    out = worker.run_workers(k, run_id, runner=fake_codex_runner(validated), binary="codex")
    assert all(w["status"] == "submitted" and w["isolated"] for w in out["workers"])
    v = k.ws.records(run_id, "verification")[0]
    assert v["context_independence"] == "fresh" and v["blinded"] is True

    run2, pv2, _, _ = _prepare(k, paper, verify_worker="codex")
    out = worker.run_workers(k, run2, runner=fake_codex_runner(validated, tool_item="command_execution"),
                             binary="codex")
    assert out["workers"][0]["isolated"] is False and "command_execution" in out["workers"][0]["problems"][0]
    v = k.ws.records(run2, "verification")[0]
    assert v["context_independence"] == "inherited"  # a shell call could have read anything


def test_cost_shape_shared_prefix_warmup_and_batched_verification(kernel, paper):
    """2026-11-24 standard runs cost $7-8 per paper: 25 one-finding verification calls, each re-sending the
    whole manuscript at cache-write price. Now: manuscript-first prompts (shared cacheable prefix), the first
    call of each task type runs alone, and findings are verified in batches."""
    import threading
    from scholarly_review.core.kernel import VERIFY_BATCH
    k = kernel
    order, lock = [], threading.Lock()
    inner = _analysis_result_lazy(k)

    def result_for(ctx, prompt):
        with lock:
            order.append(("start", ctx["task_type"]))
        out = inner(ctx, prompt)
        with lock:
            order.append(("end", ctx["task_type"]))
        return out
    runner = fake_runner(result_for, delay=0.02)
    run_id, pv = plan_and_start(k, paper, host={"name": "claude", "analysis_worker": "claude", "verify_worker": "claude"})
    worker.run_workers(k, run_id, runner=runner, binary="claude", max_tasks=50, search_fetch=_no_network)
    checks = [c for c in runner.calls if "=== TASK CONTEXT" in c["prompt"]
              and _ctx_from_prompt(c["prompt"])["task_type"] == "check"]
    systems = {c["cmd"][c["cmd"].index("--append-system-prompt") + 1] for c in checks}
    assert len(checks) > 2 and len(systems) == 1  # one identical, cacheable system prompt (manuscript)
    assert "=== MANUSCRIPT" in next(iter(systems)) and all("=== MANUSCRIPT" not in c["prompt"] for c in checks)
    first_end = order.index(("end", "check"))
    assert [e for e in order[:first_end] if e[1] == "check"] == [("start", "check")]  # warm-up call ran alone
    sizes = [len(json.loads(t["payload_json"])["finding_ids"])
             for t in k.ws.q("SELECT payload_json FROM tasks WHERE run_id=? AND type='verify'", (run_id,))]
    assert all(n <= VERIFY_BATCH for n in sizes)
