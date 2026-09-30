"""Heartbeat uses real SQLite and fake model/transport boundaries, including races and restart state."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from omniagent.companion import heartbeat as module
from omniagent.companion import delegate
from omniagent.companion.autonomy import is_quiet_hour
from omniagent.integrations.imsg import DeliveryUnknown
from omniagent.integrations.runtime import DeliveryFailed
from omniagent.memory.personal import PersonalStore, utc_iso

NOW = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)
INTERVALS = {"base": 30, "jitter": 10, "min": 20, "max": 240}
SETTINGS = {"chat_backend": "chat", "memory_backend": "memory", "heartbeat_minutes": INTERVALS,
            "quiet_hours": {"start": "23:30", "end": "09:00"}}


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(module.presence, "snapshot", lambda: {"idle_seconds": 500, "locked": False,
                                                           "available": True, "foreground_app": "test"})
    monkeypatch.setattr(module, "host_owner", lambda: None)
    value = PersonalStore(tmp_path / "companion.db")
    yield value
    value.close()


def turn(action=None, arguments=None, content=""):
    return {"content": content, "tool_calls": ([] if action is None else
            [{"id": "1", "name": action, "arguments": json.dumps(arguments)}]),
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "cached_tokens": 0}, "finish_reason": "stop"}


def new_heartbeat(store, sent, started, now=NOW, report=None, situation=None):
    async def send(text):
        sent.append(text)
        store.record_outgoing(text, "proactive", utc_iso(now))
    async def start(goal, rationale):
        started.append((goal, rationale))
    return module.Heartbeat(store, SETTINGS, {}, "deniz yakın arkadaş", send, start,
                            situation or (lambda: {"running_goal": None, "pending_question": None}),
                            lambda: False, send_report=report, now=lambda: now)


def add_fact(store, follow=None):
    timestamp = utc_iso(NOW - timedelta(hours=1))
    message_id = store.record_incoming(1, "guid1", "yarın İzmir'e gitmeyi düşünüyorum", timestamp)
    result = store.commit_learning(0, message_id, [{"statement": "İzmir'e gitmeyi düşünüyor",
            "quote": "yarın İzmir'e gitmeyi düşünüyorum", "message_id": message_id, "category": "plan",
            "supersedes": None, "follow_up_at": follow}], timestamp)
    return result[0]


@pytest.mark.parametrize("hour, minute, expected", [(23,29,False),(23,30,True),(0,0,True),(8,59,True),(9,0,False)])
def test_quiet_hours_cross_midnight(hour, minute, expected):
    assert is_quiet_hour(NOW.replace(hour=hour, minute=minute), SETTINGS["quiet_hours"]) is expected


def test_next_wake_clamps_jitters_and_caps_future_followups():
    assert module.next_wake(NOW, 1, INTERVALS, uniform=lambda low, high: 0) == NOW + timedelta(minutes=20)
    assert module.next_wake(NOW, 500, INTERVALS, uniform=lambda low, high: 0) == NOW + timedelta(minutes=240)
    assert module.next_wake(NOW, None, INTERVALS, uniform=lambda low, high: high) == NOW + timedelta(minutes=40)
    due = NOW + timedelta(minutes=5)
    assert module.next_wake(NOW, 30, INTERVALS, due) == due
    assert module.next_wake(NOW, 30, INTERVALS, NOW - timedelta(minutes=1)) > NOW
    assert module.next_wake(NOW, float("nan"), INTERVALS, uniform=lambda *_: 0) == NOW + timedelta(minutes=30)


def test_allowed_actions_and_strict_ground_identifiers():
    assert module.allowed_actions(True, 0, None) == {"stay_quiet", "start_task"}
    assert module.allowed_actions(False, 2, "job") == {"stay_quiet"}
    assert module.allowed_actions(False, 0, None) == {"stay_quiet", "send_message", "start_task"}
    facts = [{"id": 1, "status": "active"}, {"id": 2, "status": "forgotten"}]
    assert module.grounds_supported([1], facts)
    assert module.grounds_supported([], facts)
    assert not module.grounds_supported([True], facts)
    assert not module.grounds_supported([2], facts)
    assert not module.grounds_supported([999], facts)


def test_rhythms_count_user_hourly_distribution_and_longer_silence():
    times = [NOW - timedelta(days=35), NOW - timedelta(days=3), NOW - timedelta(days=2), NOW - timedelta(days=1)]
    messages = [{"direction": "in", "created_at": utc_iso(moment)} for moment in times]
    messages.append({"direction": "out", "created_at": utc_iso(NOW)})
    rhythm = module.rhythms(messages, NOW + timedelta(hours=20))
    assert sum(rhythm["hourly_messages"]) == 3
    assert rhythm["hourly_messages"][12] == 3
    assert rhythm["unusually_silent"] and not rhythm["wrote_today"]
    assert module.rhythms([], NOW)["unusually_silent"] is False


@pytest.mark.asyncio
async def test_proactive_bubbles_validated_once_archived_and_count_as_one(store, monkeypatch):
    fact_id = add_fact(store)
    calls = []
    async def model(clients, messages, schemas, session, backend, emit, stop):
        calls.append((backend, schemas))
        if backend == "memory":
            return turn(content='{"supported":true}'), backend
        return turn("send_message", {"bubbles": ["izmir planı ne oldu", "hâlâ aklında mı"],
                                      "grounds": [fact_id], "next_check_minutes": 40}), backend
    monkeypatch.setattr(module, "call_model_with_retries", model)
    monkeypatch.setattr(module, "bubble_delay", lambda text: 0)
    sent, started = [], []
    heart = new_heartbeat(store, sent, started)
    await heart.tick()
    assert sent == ["izmir planı ne oldu", "hâlâ aklında mı"] and not started
    assert [backend for backend, _schemas in calls] == ["chat", "memory"]
    assert store.get_state("unanswered_proactive") == "1"
    assert [message["kind"] for message in store.recent_messages(3)] == ["chat", "proactive", "proactive"]
    assert datetime.fromisoformat(store.get_state("next_wake")) > NOW


@pytest.mark.parametrize("mode", ["unknown_id", "rejected", "invalid_verdict"])
@pytest.mark.asyncio
async def test_unsupported_message_dropped_with_activity_without_archive(store, monkeypatch, mode):
    fact_id = add_fact(store)
    async def model(clients, messages, schemas, session, backend, emit, stop):
        if backend == "memory":
            return turn(content='{"supported":false}' if mode == "rejected" else "evet"), backend
        return turn("send_message", {"bubbles": ["dün oradaydın"], "grounds": [999 if mode == "unknown_id" else fact_id],
                                      "next_check_minutes": 30}), backend
    monkeypatch.setattr(module, "call_model_with_retries", model)
    sent = []
    await new_heartbeat(store, sent, []).tick()
    assert not sent and len(store.recent_messages(10)) == 1
    assert store.recent_activity(1)[0]["kind"] == "dropped_message"


@pytest.mark.asyncio
async def test_new_user_message_during_validator_prevents_proactive_send(store, monkeypatch):
    fact_id = add_fact(store)
    async def model(clients, messages, schemas, session, backend, emit, stop):
        if backend == "memory":
            store.record_incoming(2, "g2", "orada mısın", utc_iso(NOW))
            return turn(content='{"supported":true}'), backend
        return turn("send_message", {"bubbles": ["hey nerdesin"], "grounds": [fact_id], "next_check_minutes": 30}), backend
    monkeypatch.setattr(module, "call_model_with_retries", model)
    sent = []
    await new_heartbeat(store, sent, []).tick()
    assert not sent


@pytest.mark.asyncio
async def test_forgetting_ground_during_validator_prevents_send(store, monkeypatch):
    fact_id = add_fact(store)
    async def model(clients, messages, schemas, session, backend, emit, stop):
        if backend == "memory":
            store.forget_fact(fact_id, utc_iso(NOW))
            return turn(content='{"supported":true}'), backend
        return turn("send_message", {"bubbles": ["izmir planı"], "grounds": [fact_id], "next_check_minutes": 30}), backend
    monkeypatch.setattr(module, "call_model_with_retries", model)
    sent = []
    await new_heartbeat(store, sent, []).tick()
    assert not sent


@pytest.mark.asyncio
async def test_reply_during_first_bubble_stops_batch_and_preserves_counter_reset(store, monkeypatch):
    async def model(clients, messages, schemas, session, backend, emit, stop):
        if backend == "memory":
            return turn(content='{"supported":true}'), backend
        return turn("send_message", {"bubbles": ["hey", "nerdesin"], "grounds": [], "next_check_minutes": 30}), backend
    monkeypatch.setattr(module, "call_model_with_retries", model)
    monkeypatch.setattr(module, "bubble_delay", lambda text: 0)
    sent = []
    heart = new_heartbeat(store, sent, [])
    async def send(text):
        sent.append(text)
        store.record_outgoing(text, "proactive", utc_iso(NOW))
        store.record_incoming(1, "new", "burdayım", utc_iso(NOW))
        store.set_state("unanswered_proactive", "0")
    heart.send_bubble = send
    await heart.tick()
    assert sent == ["hey"] and store.get_state("unanswered_proactive") == "0"


@pytest.mark.parametrize("state, value", [("proactive_enabled", "0"), ("muted_until", utc_iso(NOW + timedelta(hours=1)))])
@pytest.mark.asyncio
async def test_disabled_and_muted_heartbeat_never_calls_model(store, monkeypatch, state, value):
    store.set_state(state, value)
    async def forbidden(*args):
        raise AssertionError("skipped heartbeat made a model call")
    monkeypatch.setattr(module, "call_model_with_retries", forbidden)
    await new_heartbeat(store, [], []).tick()
    assert store.get_state("next_wake")


@pytest.mark.asyncio
async def test_chatting_skips_decision_and_two_unanswered_filters_tool_list(store, monkeypatch):
    store.record_incoming(1, "g1", "selam", utc_iso(NOW))
    calls = []
    async def model(clients, messages, schemas, session, backend, emit, stop):
        calls.append({schema["function"]["name"] for schema in schemas})
        return turn("stay_quiet", {"next_check_minutes": 30}), backend
    monkeypatch.setattr(module, "call_model_with_retries", model)
    heart = new_heartbeat(store, [], [])
    await heart.tick()
    assert calls == []
    heart.now = lambda: NOW + timedelta(hours=1)
    store.set_state("unanswered_proactive", "2")
    await heart.tick()
    assert calls == [{"stay_quiet", "start_task"}]


@pytest.mark.asyncio
async def test_quiet_hours_filter_send_but_allow_guarded_work_with_rationale(store, monkeypatch):
    schemas_seen = []
    async def model(clients, messages, schemas, session, backend, emit, stop):
        schemas_seen.append({schema["function"]["name"] for schema in schemas})
        return turn("start_task", {"goal": "takvime bak", "rationale": "kontrol listesinde isteniyor",
                                  "next_check_minutes": 30}), backend
    monkeypatch.setattr(module, "call_model_with_retries", model)
    started = []
    await new_heartbeat(store, [], started, NOW.replace(hour=0)).tick()
    assert schemas_seen == [{"stay_quiet", "start_task"}]
    assert started == [("takvime bak", "kontrol listesinde isteniyor")]


@pytest.mark.asyncio
async def test_task_started_during_decision_prevents_second_start(store, monkeypatch):
    situation = {"running_goal": None, "pending_question": None}
    async def model(*args):
        situation["running_goal"] = "user work"
        return turn("start_task", {"goal": "read notes", "rationale": "checklist", "next_check_minutes": 30}), "chat"
    monkeypatch.setattr(module, "call_model_with_retries", model)
    started = []
    await new_heartbeat(store, [], started, situation=lambda: situation).tick()
    assert not started


@pytest.mark.parametrize("decision", [turn(), turn("start_task", {"goal": "takvime bak", "rationale": "   "})])
@pytest.mark.asyncio
async def test_missing_tool_or_rationale_never_starts_work(store, monkeypatch, decision):
    async def model(*args):
        return decision, "chat"
    monkeypatch.setattr(module, "call_model_with_retries", model)
    started = []
    await new_heartbeat(store, [], started).tick()
    assert not started and store.get_state("next_wake")


@pytest.mark.asyncio
async def test_snapshot_contains_due_facts_lessons_and_private_presence_fields(store):
    identity = add_fact(store, follow=utc_iso(NOW - timedelta(minutes=5)))
    outcome = delegate.failure_outcome("rapor hazırla", "kullanıcı işi geldi, durduruldu", utc_iso(NOW - timedelta(minutes=5)),
                                       origin="autonomous", rationale="kontrol listesi")
    await delegate.write_lesson(store, outcome, {}, "memory", lambda: True)
    snapshot = await new_heartbeat(store, [], []).collect_snapshot()
    assert snapshot["due_facts"][0]["id"] == identity
    assert snapshot["recent_activity"][0]["kind"] == "lesson"
    assert set(snapshot["presence"]) == {"idle_seconds", "locked", "foreground_app", "available"}


@pytest.mark.asyncio
async def test_report_queue_survives_restart_waits_for_morning_and_user_can_force(store):
    sent = []
    async def send_report(outcome):
        sent.append(outcome["goal"])
    heart = new_heartbeat(store, [], [], NOW.replace(hour=0), report=send_report)
    first = delegate.failure_outcome("ilk", "onay gerekiyor", utc_iso(NOW - timedelta(hours=1)), origin="autonomous")
    second = delegate.failure_outcome("ikinci", "durdu", utc_iso(NOW), origin="autonomous")
    heart.queue_report(first)
    heart.queue_report(first)
    heart.queue_report(second)
    await heart.flush_reports()
    assert not sent and len(json.loads(store.get_state(module.QUEUED_REPORTS_KEY))) == 2
    reopened = new_heartbeat(store, [], [], NOW.replace(hour=0), report=send_report)
    await reopened.flush_reports(force=True)
    assert sent == ["ilk", "ikinci"]
    assert json.loads(store.get_state(module.QUEUED_REPORTS_KEY)) == []


@pytest.mark.asyncio
async def test_definite_report_failure_restores_fifo_unknown_does_not_retry(store):
    first = delegate.failure_outcome("ilk", "durdu", utc_iso(NOW), origin="autonomous")
    attempts = []
    async def fail(outcome):
        attempts.append(outcome["goal"])
        raise DeliveryFailed("not sent")
    heart = new_heartbeat(store, [], [], report=fail)
    heart.queue_report(first)
    with pytest.raises(DeliveryFailed):
        await heart.flush_reports()
    assert json.loads(store.get_state(module.QUEUED_REPORTS_KEY))[0]["goal"] == "ilk"
    async def unknown(outcome):
        attempts.append(outcome["goal"])
        raise DeliveryUnknown("maybe sent")
    heart.send_report = unknown
    await heart.flush_reports()
    await heart.flush_reports()
    assert attempts == ["ilk", "ilk"]
    assert json.loads(store.get_state(module.QUEUED_REPORTS_KEY)) == []
    assert store.get_state("morning_report_delivery")


@pytest.mark.asyncio
async def test_mute_defers_automatic_report_but_direct_user_input_can_flush(store):
    sent = []
    async def send_report(outcome):
        sent.append(outcome["goal"])
    heart = new_heartbeat(store, [], [], report=send_report)
    heart.queue_report(delegate.failure_outcome("gece işi", "onay gerekiyor", utc_iso(NOW), origin="autonomous"))
    store.set_state("muted_until", utc_iso(NOW + timedelta(hours=3)))
    await heart.flush_reports()
    assert not sent
    await heart.flush_reports(force=True)
    assert sent == ["gece işi"]
