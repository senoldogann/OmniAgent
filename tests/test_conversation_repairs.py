"""Regression gates across the real coordinator and authenticated presenters."""
import asyncio
import json
import time
from contextlib import asynccontextmanager

import pytest

from omniagent.app import agent
from omniagent.core.conversation import make_exchange
from omniagent.core.evidence import EvidenceStore
from tests.test_shared_conversation import coordinator, scripted, options, run, route, turn, call, verified


@pytest.mark.asyncio
@pytest.mark.parametrize("goal", [
    "Please read /tmp/report.txt", "List files in ~/Desktop", "Search for OpenAI release dates",
    "Lütfen /tmp/report.txt dosyasını oku", "~/Desktop'taki dosyaları listele",
    "OpenAI çıkış tarihlerini araştır", "OpenAI için web'de ara",
    "/tmp/report.txt dosyasını okur musun?", "Masaüstündeki dosyaları listeler misin?",
])
async def test_explicit_targets_override_mistaken_chat_without_fabrication(coordinator, scripted, tmp_path, goal):
    scripts, _ = scripted
    scripts.extend([route("chat"), turn("FABRICATED file contents"), verified()])
    report, events = await run(coordinator, tmp_path, goal)
    assert report["evidence"]["contract"]["route"] == "investigate"
    assert report["evidence"]["contract"]["subject"] == goal
    assert not report["success"] and report["metrics"]["tool_calls"] == 0
    assert "FABRICATED" not in json.dumps(events)


@pytest.mark.asyncio
@pytest.mark.parametrize("goal", [
    "Explain how to research OpenAI releases", "The phrase 'search the web' means what?",
    'Translate "read /tmp/report.txt" into Turkish', "Research is interesting, isn't it?",
    "Araştırma yapmak neden önemli?", "'dosyayı oku' ifadesini açıkla",
])
async def test_explanation_and_quoted_requests_remain_semantic_chat(coordinator, scripted, tmp_path, goal):
    scripts, _ = scripted
    scripts.extend([route("chat"), turn("Explanation.")])
    report, _ = await run(coordinator, tmp_path, goal)
    assert report["success"] and report["evidence"]["contract"]["route"] == "chat"


def failed_report(opts):
    return {"outcome": "Unverified engine text", "success": False, "reason": "effect failed",
            "evidence": EvidenceStore(opts["state_file"]).load(opts["evidence_run_id"]),
            "exchange": make_exchange("task", "Unverified engine text", []),
            "metrics": {"tool_calls": 0, "tool_seconds": 0, "model_seconds": 0, "backend": "ollama-cloud"}}


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_task_context_acquisition_obeys_shared_budget_and_stop(coordinator, scripted, tmp_path, monkeypatch, cancel):
    stopped = asyncio.Event()
    cleaned = []
    engine_calls = []
    @asynccontextmanager
    async def lock():
        try:
            if cancel:
                stopped.set()
            await asyncio.sleep(.12)
            yield
        finally:
            cleaned.append(True)
    async def engine(goal, emit, opts, clients):
        engine_calls.append(opts)
        return failed_report(opts)
    monkeypatch.setattr(coordinator, "run_agent_with_callback", engine)
    started = time.monotonic()
    report, _ = await run(coordinator, tmp_path, "task", {"run_mode": "extended", "task_context": lock,
        "max_wall_clock_seconds": .02 if not cancel else 1, "should_stop": stopped.is_set})
    assert not engine_calls
    assert time.monotonic() - started < .1
    assert cleaned and not report["success"]


@pytest.mark.asyncio
async def test_lock_wait_deducted_from_engine_limits_and_failure_releases(coordinator, scripted, tmp_path, monkeypatch):
    released, limits = [], []
    @asynccontextmanager
    async def lock():
        await asyncio.sleep(.04)
        try:
            yield
        finally:
            released.append(True)
    async def engine(goal, emit, opts, clients):
        limits.append(opts["max_wall_clock_seconds"])
        raise RuntimeError("engine failure")
    monkeypatch.setattr(coordinator, "run_agent_with_callback", engine)
    report, _ = await run(coordinator, tmp_path, "task", {"run_mode": "extended", "task_context": lock,
                                                        "max_wall_clock_seconds": .2})
    assert 0 < limits[0] < .18
    assert released and not report["success"] and "engine failure" in report["reason"]


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["save", "publication"])
async def test_stop_at_final_boundary_keeps_saved_bundle_and_consistent_single_final(
    coordinator, scripted, tmp_path, monkeypatch, boundary,
):
    scripts, _ = scripted
    scripts.extend([route("chat"), turn("UNPUBLISHED Hello")])
    stopped = asyncio.Event()
    original_save = EvidenceStore.save
    def save(self, bundle, **kwargs):
        result = original_save(self, bundle, **kwargs)
        if boundary == "save" and not kwargs.get("create_only"):
            stopped.set()
        return result
    monkeypatch.setattr(EvidenceStore, "save", save)
    if boundary == "publication":
        original_metrics = coordinator.ConversationBudget.metrics
        def metrics(self):
            result = original_metrics(self)
            stopped.set()
            return result
        monkeypatch.setattr(coordinator.ConversationBudget, "metrics", metrics)
    report, events = await run(coordinator, tmp_path, "Hello", {"should_stop": stopped.is_set})
    assert not report["success"] and "UNPUBLISHED" not in json.dumps(events)
    assert report["exchange"]["answer"] == report["outcome"]
    assert len([e for e in events if e["kind"] == "text_delta"]) == 1
    terminal = [e for e in events if e["kind"] == "run_finished"]
    assert len(terminal) == 1 and not terminal[0]["success"]
    saved = EvidenceStore(options(tmp_path)["state_file"]).load(report["evidence"]["run_id"])
    assert not saved["complete"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["chat", "investigate", "routing"])
@pytest.mark.parametrize("finish", ["length", "content_filter"])
@pytest.mark.parametrize("with_calls", [False, True])
async def test_incomplete_provider_turn_never_publishes_or_executes(coordinator, scripted, tmp_path, kind, finish, with_calls):
    scripts, requests = scripted
    incomplete = route("chat") if kind == "routing" else turn("TRUNCATED unsupported", calls=(
        [call("write_file", path="/tmp/no", content="no")] if with_calls else []))
    incomplete["finish_reason"] = finish
    scripts.extend(([route(kind)] if kind != "routing" else []) + [incomplete])
    report, events = await run(coordinator, tmp_path, "Hello" if kind != "investigate" else "Search for OpenAI")
    assert not report["success"]
    assert report["metrics"]["tool_calls"] == 0 and "TRUNCATED" not in json.dumps(events)
    assert len(requests) == (1 if kind == "routing" else 2)
    assert ("max_tokens" if finish == "length" else "filtres") in report["reason"]


@pytest.mark.asyncio
async def test_small_token_allowance_truncation_stays_failed_and_counts_usage(coordinator, scripted, tmp_path):
    scripts, requests = scripted
    cutoff = turn("TRUNCATED token-limited prose")
    cutoff["finish_reason"] = "length"
    scripts.extend([route("chat"), cutoff])
    report, events = await run(coordinator, tmp_path, "Hello", {"max_total_tokens": 3000})
    assert not report["success"] and len(requests) == report["metrics"]["turns"] == 2
    assert report["metrics"]["completion_tokens"] == 14
    assert requests[1][2]["max_tokens"] < agent.BACKENDS["ollama-cloud"]["max_tokens"]
    assert "TRUNCATED" not in json.dumps(events)


@pytest.mark.asyncio
async def test_mistaken_chat_read_path_uses_real_receipt(coordinator, scripted, tmp_path):
    scripts, _ = scripted
    target = tmp_path / "report.txt"
    target.write_text("Verified actual file")
    scripts.extend([route("chat"), turn(calls=[call("read_file", path=str(target))]),
                    turn("Verified actual file"), verified()])
    report, _ = await run(coordinator, tmp_path, f"Please read {target}")
    assert report["success"] and report["metrics"]["tool_calls"] == 1
    assert "Verified actual file" in report["evidence"]["observations"][0]["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", ["length", "content_filter"])
async def test_read_generation_truncation_retains_real_sources_without_draft(coordinator, scripted, tmp_path, finish):
    scripts, requests = scripted
    target = tmp_path / "report.txt"
    target.write_text("ACTUAL observed file")
    cutoff = turn("UNVERIFIED truncated read answer")
    cutoff["finish_reason"] = finish
    scripts.extend([route("investigate"), turn(calls=[call("read_file", path=str(target))]), cutoff])
    report, events = await run(coordinator, tmp_path, f"Read {target}", {"max_total_tokens": 12000})
    assert not report["success"] and report["metrics"]["tool_calls"] == 1
    assert len(requests) == report["metrics"]["turns"] == 3
    assert "ACTUAL observed file" in report["outcome"] and "UNVERIFIED" not in json.dumps(events)
    assert EvidenceStore(options(tmp_path)["state_file"]).load(report["evidence"]["run_id"])["observations"]


@pytest.mark.asyncio
async def test_forced_continuous_keeps_selected_limits_and_failed_engine_status(coordinator, scripted, tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(agent, "load_continuous_limits", lambda path: {"max_hours": .25, "max_total_tokens": 50000})
    @asynccontextmanager
    async def lock():
        yield
    async def engine(goal, emit, opts, clients):
        seen.append(opts)
        return failed_report(opts)
    monkeypatch.setattr(coordinator, "run_agent_with_callback", engine)
    report, _ = await run(coordinator, tmp_path, "task", {"run_mode": "continuous", "task_context": lock})
    assert not scripted[1] and 120 < seen[0]["max_wall_clock_seconds"] <= 900
    assert seen[0]["run_mode"] == "continuous" and seen[0]["max_total_tokens"] == 50000
    assert not report["success"] and report["reason"] == "effect failed"


@pytest.mark.asyncio
async def test_cancelled_full_task_keeps_captured_receipts_and_releases_context(coordinator, scripted, tmp_path, monkeypatch):
    stopped, released = asyncio.Event(), []
    @asynccontextmanager
    async def lock():
        try:
            yield
        finally:
            released.append(True)
    async def engine(goal, emit, opts, clients):
        store = EvidenceStore(opts["state_file"])
        bundle = store.load(opts["evidence_run_id"])
        store.capture(bundle, "read_file", {"ok": True, "result": "CAPTURED before stop"})
        stopped.set()
        await asyncio.sleep(10)
    monkeypatch.setattr(coordinator, "run_agent_with_callback", engine)
    report, events = await run(coordinator, tmp_path, "task", {"run_mode": "extended", "task_context": lock,
                                                            "should_stop": stopped.is_set})
    assert released and not report["success"]
    assert "CAPTURED before stop" in report["outcome"]
    saved = EvidenceStore(options(tmp_path)["state_file"]).load(report["evidence"]["run_id"])
    assert saved["observations"] == report["evidence"]["observations"]
    assert len(saved["observations"]) == 1 and not saved["complete"]
    assert len([e for e in events if e["kind"] == "run_finished"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", ["turns", "tokens"])
async def test_completed_effect_keeps_verified_verdict_when_presentation_budget_spent(
    coordinator, scripted, tmp_path, monkeypatch, limit,
):
    target = tmp_path / "sample.txt"
    @asynccontextmanager
    async def lock():
        yield
    async def engine(goal, emit, opts, clients):
        agent._MODEL_ATTEMPT.get()("ollama-cloud")
        if limit == "tokens":
            agent._MODEL_USAGE.get()({"prompt_tokens": 900, "completion_tokens": 100, "cached_tokens": 0})
        target.write_text("verified actual effect")
        store = EvidenceStore(opts["state_file"])
        bundle = store.load(opts["evidence_run_id"])
        store.capture(bundle, "write_file", {"ok": True, "result": "Created verified sample.txt"},
                      arguments=json.dumps({"path": str(target)}))
        report = failed_report(opts)
        return {**report, "success": True, "reason": "verified done", "outcome": "Created verified sample.txt", "evidence": bundle}
    monkeypatch.setattr(coordinator, "run_agent_with_callback", engine)
    extra = {"max_iterations": 1} if limit == "turns" else {"max_total_tokens": 1000}
    report, events = await run(coordinator, tmp_path, "Create sample.txt", {
        "run_mode": "extended", "task_context": lock, **extra})
    assert target.read_text() == "verified actual effect"
    assert report["success"] and report["metrics"]["turns"] == 1
    assert not scripted[1]
    assert "Created verified sample.txt" in report["outcome"]
    assert "sunum" in report["outcome"].casefold()
    assert "İşlem tamamlanamadı" not in report["outcome"] and "yeni bir çalışma" not in report["outcome"]
    assert report["exchange"]["answer"] == report["outcome"]
    terminal = [e for e in events if e["kind"] == "run_finished"]
    assert len(terminal) == 1 and terminal[0]["success"] and terminal[0]["outcome"] == report["outcome"]


@pytest.mark.asyncio
@pytest.mark.parametrize("overrun", ["tokens", "deadline"])
async def test_actual_engine_overrun_stays_failed_with_latest_receipts(coordinator, scripted, tmp_path, monkeypatch, overrun):
    @asynccontextmanager
    async def lock():
        yield
    async def engine(goal, emit, opts, clients):
        agent._MODEL_ATTEMPT.get()("ollama-cloud")
        if overrun == "tokens":
            agent._MODEL_USAGE.get()({"prompt_tokens": 900, "completion_tokens": 101, "cached_tokens": 0})
        store = EvidenceStore(opts["state_file"])
        bundle = store.load(opts["evidence_run_id"])
        store.capture(bundle, "read_file", {"ok": True, "result": "ACTUAL overrun receipt"})
        report = {**failed_report(opts), "success": True, "reason": "done", "evidence": bundle}
        if overrun == "deadline":
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                # Even an engine suppressing cancellation cannot promote overrun.
                return report
        return report
    monkeypatch.setattr(coordinator, "run_agent_with_callback", engine)
    extra = {"max_total_tokens": 1000} if overrun == "tokens" else {"max_wall_clock_seconds": .02}
    report, events = await run(coordinator, tmp_path, "task", {"run_mode": "extended", "task_context": lock, **extra})
    assert not report["success"] and "ACTUAL overrun receipt" in report["outcome"]
    assert not scripted[1]
    assert not [e for e in events if e["kind"] == "run_finished"][0]["success"]


@pytest.mark.asyncio
async def test_quick_read_timeout_keeps_receipt_and_full_subject_continuation(coordinator, scripted, tmp_path):
    scripts, requests = scripted
    target = tmp_path / "report.txt"
    target.write_text("ACTUAL captured identifiers: OpenAI and Anthropic")
    goal = f"Please read {target} and list the exact identifiers for OpenAI and Anthropic"
    async def waiting(emit, stop):
        emit({"kind": "text_delta", "text": "UNVERIFIED timeout draft"})
        await asyncio.sleep(10)
    scripts.extend([route("investigate"), turn(calls=[call("read_file", path=str(target))]), waiting])
    report, events = await run(coordinator, tmp_path, goal, {"max_wall_clock_seconds": .05})
    assert not report["success"] and report["metrics"]["tool_calls"] == 1
    assert len(requests) == report["metrics"]["turns"] == 3
    assert "ACTUAL captured identifiers" in report["outcome"]
    assert "UNVERIFIED" not in json.dumps(events)
    assert f"Aynı konuda tam ajanla yeni bir çalışma başlatarak devam edebiliriz: {goal}" in report["outcome"]
    assert report["exchange"]["answer"] == report["outcome"]
    terminal = [e for e in events if e["kind"] == "run_finished"]
    assert len(terminal) == 1 and terminal[0]["outcome"] == report["outcome"] and not terminal[0]["success"]


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [True, False], ids=["stop", "timeout"])
async def test_real_engine_interruption_keeps_tool_and_timing_accounting_once(
    coordinator, scripted, tmp_path, monkeypatch, cancel,
):
    scripts, requests = scripted
    target = tmp_path / "source.txt"
    target.write_text("REAL observed receipt")
    stopped = asyncio.Event()
    inner_metrics = []
    original_filter = coordinator._private_emit
    def observing_filter(emit):
        filtered = original_filter(emit)
        def observe(event):
            if event["kind"] == "run_finished":
                inner_metrics.append(dict(event["metrics"]))
            filtered(event)
        return observe
    monkeypatch.setattr(coordinator, "_private_emit", observing_filter)
    async def first(emit, stop):
        await asyncio.sleep(.015)
        return turn(calls=[call("read_file", path=str(target))])
    async def waiting(emit, stop):
        if cancel:
            stopped.set()
        await asyncio.sleep(10)
    scripts.extend([first, waiting])
    @asynccontextmanager
    async def lock():
        yield
    extra = {} if cancel else {"max_wall_clock_seconds": .09}
    report, events = await run(coordinator, tmp_path, f"Inspect this file: {target}", {
        "run_mode": "extended", "task_context": lock, "should_stop": stopped.is_set, **extra})
    assert not report["success"] and len(report["evidence"]["observations"]) == 1
    assert len(requests) == report["metrics"]["turns"] == 2
    assert len(inner_metrics) == 1 and inner_metrics[0]["tool_calls"] == 1
    assert report["metrics"]["tool_calls"] == 1
    assert report["metrics"]["model_seconds"] == inner_metrics[0]["model_seconds"] > 0
    assert report["metrics"]["tool_seconds"] == inner_metrics[0]["tool_seconds"]
    assert report["metrics"]["prompt_tokens"] == 11 and report["metrics"]["completion_tokens"] == 7
    assert len([e for e in events if e["kind"] == "tool_started"]) == 1
    assert len([e for e in events if e["kind"] == "tool_finished"]) == 1
    terminal = [e for e in events if e["kind"] == "run_finished"]
    assert len(terminal) == 1 and terminal[0]["metrics"] == report["metrics"] and not terminal[0]["success"]


@pytest.mark.asyncio
@pytest.mark.parametrize("verbose", [False, True])
async def test_real_telegram_consumers_receive_one_verified_final_and_controls(
    coordinator, scripted, tmp_path, monkeypatch, verbose,
):
    from omniagent.integrations import telegram
    from omniagent.core.events import AWAITING_APPROVAL_CODE
    from tests.test_telegram_bridge import memory_bridge
    bridge, api = memory_bridge(monkeypatch, tmp_path)
    bridge.verbose = verbose
    bridge.backend = "ollama-cloud"
    assert telegram.run_agent_with_callback is coordinator.run_conversation_with_callback
    scripts, _ = scripted
    scripts.extend([route("task"), verified()])
    consumer_events = []
    tracked_events = []
    original_track = bridge._track
    def track(event):
        tracked_events.append(event)
        original_track(event)
    monkeypatch.setattr(bridge, "_track", track)
    original_compact = telegram.CompactPresenter.event
    original_verbose = telegram.event_text
    async def compact(self, event):
        consumer_events.append(event)
        await original_compact(self, event)
    def detailed(event):
        consumer_events.append(event)
        return original_verbose(event)
    monkeypatch.setattr(telegram.CompactPresenter, "event", compact)
    if verbose:
        monkeypatch.setattr(telegram, "event_text", detailed)
    @asynccontextmanager
    async def lock():
        yield
    monkeypatch.setattr(telegram, "async_host_task_lock_preempting", lock)
    async def engine(goal, emit, opts, clients):
        store = EvidenceStore(opts["state_file"])
        bundle = store.load(opts["evidence_run_id"])
        store.capture(bundle, "read_file", {"ok": True, "result": "GROUNDED_FINAL_7431"})
        for event in [
            {"kind": "text_delta", "text": "UNTRUSTED draft"},
            {"kind": "reasoning_delta", "text": "UNTRUSTED reasoning"},
            {"kind": "stream_reset", "reason": "UNTRUSTED reset"},
            {"kind": "run_started", "goal": "UNTRUSTED duplicate"},
            {"kind": "notice", "level": "warning", "text": "UNTRUSTED legacy outcome"},
            {"kind": "run_finished", "outcome": "UNTRUSTED terminal"},
            {"kind": "tool_started", "call_id": "read", "index": 0, "name": "read_file", "preview": "/safe"},
            {"kind": "tool_finished", "call_id": "read", "ok": True, "text": "real receipt", "seconds": .01},
            {"kind": "integration_status", "stage": "resumed", "text": "real status", "completed": 1, "total": 1},
            {"kind": "notice", "level": "info", "code": AWAITING_APPROVAL_CODE, "text": "real approval"},
            {"kind": "provider_fallback", "from_backend": "openai", "to_backend": "ollama-cloud",
             "to_model": "test", "processor": "test", "reason": "real fallback", "image_count": 0},
        ]:
            emit(event)
        report = failed_report(opts)
        return {**report, "success": True, "reason": "done", "outcome": "GROUNDED_FINAL_7431", "evidence": bundle}
    monkeypatch.setattr(coordinator, "run_agent_with_callback", engine)
    try:
        await bridge._execute("Perform substantial safe task")
    finally:
        await bridge.integrations.close()
    transcript = json.dumps([api.sent, api.edited, api.html_sent, api.html_edited, api.drafts])
    assert "UNTRUSTED" not in transcript and "UNTRUSTED" not in json.dumps(consumer_events)
    assert len([e for e in consumer_events if e["kind"] == "run_started"]) == 1
    assert len([e for e in consumer_events if e["kind"] == "text_delta"]) == 1
    assert len([e for e in consumer_events if e["kind"] == "run_finished"]) == 1
    terminal = [e for e in tracked_events if e["kind"] == "run_finished"]
    assert len(terminal) == 1 and terminal[0]["success"] and terminal[0]["outcome"] == "GROUNDED_FINAL_7431"
    assert {"tool_started", "tool_finished", "integration_status", "provider_fallback"} <= {e["kind"] for e in consumer_events}
    assert api.buttons and api.buttons[0][0] == "real approval"
    final_pages = [*api.html_sent, *(api.edited[-1:] or api.sent[-1:])]
    assert sum(page.count("GROUNDED_FINAL_7431") for page in final_pages) == 1
    assert bridge.history[-1]["answer"] == "GROUNDED_FINAL_7431"
