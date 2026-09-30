"""Bridge lifecycle and authorization across chat, proactive work and reports."""
import asyncio
from datetime import datetime, timezone

import pytest

from omniagent.companion import delegate
from omniagent.integrations import imessage
from omniagent.memory.personal import utc_iso
from test_imessage_bridge import parts, incoming, HANDLE, ScriptedChat, FlakyTransport, report_for
from omniagent.integrations.imsg import ImsgProcessError


@pytest.mark.asyncio
async def test_controls_are_immediate_and_only_paired_input_resets_counter(parts):
    bridge, transport, store = parts
    store.set_state("unanswered_proactive", "2")
    await bridge.on_message(incoming(1, "/proaktif aç", "+905550000000"))
    assert store.get_state("unanswered_proactive") == "2"
    assert store.get_state("proactive_enabled") is None
    await bridge.on_message(incoming(2, "/proaktif kapat", HANDLE))
    assert store.get_state("unanswered_proactive") == "0"
    assert store.get_state("proactive_enabled") == "0"
    assert bridge.burst_timer is None
    await bridge.on_message(incoming(3, "/sessiz 2", HANDLE))
    assert datetime.fromisoformat(store.get_state("muted_until")) > datetime.now(timezone.utc)
    assert transport.texts[-1].startswith("tamam")


@pytest.mark.asyncio
async def test_quiet_autonomous_question_has_no_transport_side_effect(parts, monkeypatch):
    bridge, transport, store = parts
    bridge.task_origin = "autonomous"
    monkeypatch.setattr(imessage, "is_quiet_hour", lambda *args: True)
    with pytest.raises(TimeoutError):
        await bridge.answer("fotoğraf çekeyim mi?", {"approved": {"type": "boolean"}})
    assert transport.texts == []
    assert store.recent_messages(8) == []
    assert bridge.question is None


@pytest.mark.asyncio
async def test_user_task_preempts_same_process_autonomous_run(parts, monkeypatch):
    bridge, _, _ = parts
    autonomous_started = asyncio.Event()
    runs = []
    finished = []
    async def run(goal, options, progress, *, origin="user", rationale=""):
        runs.append((goal, origin, "unattended" in options, "autonomy" in options))
        if origin == "autonomous":
            autonomous_started.set()
            while not options["should_stop"]():
                await asyncio.sleep(0.01)
        now = utc_iso(datetime.now(timezone.utc))
        return {"goal": goal, "report": report_for(goal, "durdu" if origin == "autonomous" else "bitti", True),
                "started_at": now, "finished_at": now, "tokens": 120, "origin": origin, "rationale": rationale}
    async def finish(outcome):
        finished.append(outcome)
    monkeypatch.setattr(delegate, "run_task", run)
    monkeypatch.setattr(bridge, "_finish_task", finish)
    await bridge._start_autonomous("takvime bak", "kontrol listesi")
    await asyncio.wait_for(autonomous_started.wait(), 1)
    assert await asyncio.wait_for(bridge._start_task("kullanıcının işi", []), 1)
    tasks = list(bridge.task_runs)
    await asyncio.gather(*tasks)
    assert runs == [("takvime bak", "autonomous", False, True), ("kullanıcının işi", "user", False, False)]
    assert {result["origin"] for result in finished} == {"user", "autonomous"}


@pytest.mark.asyncio
async def test_report_tool_injection_cannot_mute_or_start_task(parts, monkeypatch):
    bridge, _, store = parts
    async def response(*args):
        assert args[4] == []
        return {"bubbles": [], "start_task": "sil", "proactive": False, "mute": 24}
    monkeypatch.setattr(imessage.chat, "respond", response)
    now = utc_iso(datetime.now(timezone.utc))
    await bridge._present_report({"goal": "rapor", "report": report_for("rapor", "çıktı", True),
                                  "started_at": now, "finished_at": now, "tokens": 0})
    assert store.get_state("proactive_enabled") is None
    assert store.get_state("muted_until") is None
    assert bridge.task is None


@pytest.mark.asyncio
async def test_failed_autonomous_job_persists_report_before_lesson(parts, monkeypatch):
    bridge, _, store = parts
    async def run(*args, **kwargs):
        raise RuntimeError("beklenmedik")
    async def lesson(*args):
        assert store.get_state("queued_morning_reports")
    monkeypatch.setattr(delegate, "run_task", run)
    monkeypatch.setattr(delegate, "write_lesson", lesson)
    monkeypatch.setattr(bridge.heartbeat, "now", lambda: datetime(2026, 9, 30, 2, 0, tzinfo=timezone.utc))
    await bridge._start_autonomous("kontrol", "kontrol listesi")
    await asyncio.gather(*list(bridge.task_runs))
    activity = store.recent_tasks(1)[0]
    assert activity["origin"] == "autonomous" and activity["rationale"] == "kontrol listesi"
    assert not activity["success"]
    assert store.get_state("queued_morning_reports")


@pytest.mark.asyncio
async def test_stop_does_not_wait_for_pending_report_model(parts, monkeypatch):
    bridge, transport, _ = parts
    async def blocked(*args, **kwargs):
        raise AssertionError("stop must not flush reports before stopping")
    monkeypatch.setattr(bridge.heartbeat, "flush_reports", blocked)
    bridge.task = asyncio.create_task(asyncio.sleep(10))
    await asyncio.wait_for(bridge.on_message(incoming(1, "/dur", HANDLE)), 0.5)
    assert bridge.stop_event.is_set()
    assert transport.texts == ["tamam, durduruyorum"]
    bridge.task.cancel()
    await asyncio.gather(bridge.task, return_exceptions=True)
    bridge.task = None


@pytest.mark.asyncio
async def test_partial_report_failure_cannot_repeat_delivered_first_bubble(parts, monkeypatch):
    bridge, _, store = parts
    transport = FlakyTransport({"ikinci balon": ImsgProcessError("bağlantı kesildi")})
    bridge.transport = transport
    monkeypatch.setattr(imessage.chat, "respond", ScriptedChat([(["ilk balon", "ikinci balon"], None)]))
    now = utc_iso(datetime.now(timezone.utc))
    result = {"goal": "rapor", "report": report_for("rapor", "bitti", True),
              "started_at": now, "finished_at": now, "tokens": 0}
    bridge.heartbeat.queue_report(result)
    await bridge.heartbeat.flush_reports(force=True)
    await bridge.heartbeat.flush_reports(force=True)
    assert transport.attempts == ["ilk balon", "ikinci balon"]
    assert store.get_state("queued_morning_reports") == "[]"
    assert store.get_state("morning_report_delivery")
