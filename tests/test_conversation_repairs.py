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
    assert {"tool_started", "tool_finished", "integration_status", "provider_fallback"} <= {e["kind"] for e in consumer_events}
    assert api.buttons and api.buttons[0][0] == "real approval"
    final_pages = [*api.html_sent, *(api.edited[-1:] or api.sent[-1:])]
    assert sum(page.count("GROUNDED_FINAL_7431") for page in final_pages) == 1
    assert bridge.history[-1]["answer"] == "GROUNDED_FINAL_7431"
