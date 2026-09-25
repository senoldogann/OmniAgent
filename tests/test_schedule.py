"""Zamanlanmış görevler: yerel saat hesabı, plan deposu, araç, şema kapısı ve Telegram zamanlayıcısı."""
import asyncio
import json
import os
import stat
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterator, List

import pytest

from omniagent.app import agent as main
from omniagent.app.tool_schema import TOOL_NAMES, route_tool_schemas, scheduling_goal
from omniagent.core import schedule
from omniagent.core.conversation import make_exchange
from omniagent.integrations import telegram
from omniagent.platform.macos.host_lock import host_task_lock
from omniagent.tools import ToolError, Toolbox


@pytest.fixture
def helsinki(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Yaz saati uygulayan yerel saat dilimi (EEST +03 → 25 Ekim 2026'da EET +02)."""
    monkeypatch.setenv("TZ", "Europe/Helsinki")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def local(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute).astimezone()


# --- Saf zaman hesabı ---

def test_daily_and_weekly_next_run_are_strictly_after_now(helsinki: None) -> None:
    daily = schedule.build_spec("daily", "9:00", None, None, None)
    assert schedule.next_occurrence(daily, local(2026, 9, 25, 8)) == local(2026, 9, 25, 9)
    assert schedule.next_occurrence(daily, local(2026, 9, 25, 9)) == local(2026, 9, 26, 9)
    weekly = schedule.build_spec("weekly", "10:30", ["mon", "wed"], None, None)
    assert weekly["weekdays"] == [0, 2]
    # 29 Eylül 2026 Salı → sonraki Çarşamba 30 Eylül 10:30
    assert schedule.next_occurrence(weekly, local(2026, 9, 29, 12)) == local(2026, 9, 30, 10, 30)


def test_daily_time_stays_local_across_daylight_saving_change(helsinki: None) -> None:
    """25 Ekim 2026'da saatler geri alınır: her gün 09:00 yine yerel 09:00'dır (UTC 06:00 → 07:00)."""
    daily = schedule.build_spec("daily", "09:00", None, None, None)
    before = schedule.next_occurrence(daily, local(2026, 10, 23, 10))
    after = schedule.next_occurrence(daily, local(2026, 10, 24, 10))
    assert before is not None and after is not None
    assert before.utcoffset() == timedelta(hours=3) and before.hour == 9
    assert after.utcoffset() == timedelta(hours=2) and after.hour == 9


def test_interval_once_and_validation(helsinki: None) -> None:
    now = local(2026, 9, 25, 12)
    interval = schedule.build_spec("interval", None, None, None, 120)
    assert schedule.next_occurrence(interval, now) == now + timedelta(minutes=120)
    once = schedule.build_spec("once", None, None, "2026-09-26T14:30", None)
    assert schedule.next_occurrence(once, now) == local(2026, 9, 26, 14, 30)
    assert schedule.next_occurrence(once, local(2026, 9, 27, 0)) is None
    for arguments in (
        ("hourly", None, None, None, None), ("daily", "25:00", None, None, None),
        ("weekly", "09:00", [], None, None), ("weekly", "09:00", ["funday"], None, None),
        ("interval", None, None, None, 5), ("interval", None, None, None, True),
        ("once", None, None, "yarın", None),
    ):
        with pytest.raises(schedule.ScheduleError):
            schedule.build_spec(*arguments)


def test_add_advance_and_describe(helsinki: None) -> None:
    now = local(2026, 9, 25, 8)
    daily = schedule.build_spec("daily", "09:00", None, None, None)
    records, record = schedule.add_schedule([], "  Gündemi   özetle ", daily, now, "a1b2c3")
    assert record["goal"] == "Gündemi özetle"
    assert schedule.describe_record(record) == "[a1b2c3] her gün 09:00 · sonraki 25.09 09:00 · Gündemi özetle"
    assert schedule.due_schedules(records, local(2026, 9, 25, 8, 59)) == []
    assert schedule.due_schedules(records, local(2026, 9, 25, 9)) == [record]
    advanced = schedule.advance_schedule(records, "a1b2c3", local(2026, 9, 25, 9))
    assert advanced[0]["next_run"].startswith("2026-09-26T09:00") and advanced[0]["runs"] == 1
    once = schedule.build_spec("once", None, None, "2026-09-25T10:00", None)
    records, single = schedule.add_schedule(records, "Toplantıyı hatırlat", once, now, "ffff01")
    assert [item["id"] for item in schedule.advance_schedule(records, "ffff01", local(2026, 9, 25, 10))] == ["a1b2c3"]
    with pytest.raises(schedule.ScheduleError):
        schedule.add_schedule([], "Geçmiş", once, local(2026, 9, 25, 11), "x")
    with pytest.raises(schedule.ScheduleError):
        schedule.add_schedule([record] * schedule.MAX_SCHEDULES, "Fazla", daily, now, "y")
    assert schedule.describe_spec(schedule.build_spec("interval", None, None, None, 90)) == "her 90 dakikada bir"
    assert schedule.describe_spec(schedule.build_spec("interval", None, None, None, 120)) == "her 2 saatte bir"


def test_missed_runs_within_grace_still_run_older_ones_are_skipped(helsinki: None) -> None:
    daily = schedule.build_spec("daily", "09:00", None, None, None)
    _, record = schedule.add_schedule([], "Özet", daily, local(2026, 9, 25, 8), "id1")
    kept, skipped = schedule.skip_missed([record], local(2026, 9, 25, 11))
    assert skipped == [] and kept == [record]
    kept, skipped = schedule.skip_missed([record], local(2026, 9, 25, 16))
    assert skipped == [record]
    assert kept[0]["next_run"].startswith("2026-09-26T09:00")


def test_store_roundtrip_is_private_and_skips_invalid(tmp_path: Path, helsinki: None) -> None:
    path = tmp_path / "schedules.json"
    daily = schedule.build_spec("daily", "09:00", None, None, None)
    records, _ = schedule.add_schedule([], "Özet", daily, local(2026, 9, 25, 8), "id1")
    schedule.save_schedules(path, records)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.write_text(json.dumps(records + [{"id": 3}]), encoding="utf-8")
    assert schedule.load_schedules(path) == records


# --- Araç ve şema ---

def test_schedule_tool_adds_lists_and_removes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    box = Toolbox()
    assert box.schedule_task("list") == "Planlanmış görev yok."
    added = box.schedule_task("add", "Hava durumunu özetle", "daily", "07:45", None, None, None, None)
    assert "Plan eklendi" in added and "her gün 07:45" in added
    stored = json.loads((tmp_path / "schedules.json").read_text(encoding="utf-8"))
    assert stored[0]["goal"] == "Hava durumunu özetle"
    assert "Hava durumunu özetle" in box.schedule_task("list")
    with pytest.raises(ToolError) as invalid:
        box.schedule_task("add", "x", "interval", None, None, None, 1, None)
    assert invalid.value.code == "INVALID_SCHEDULE"
    with pytest.raises(ToolError) as missing:
        box.schedule_task("remove", schedule_id="yok")
    assert missing.value.code == "SCHEDULE_NOT_FOUND"
    assert "Plan silindi" in box.schedule_task("remove", schedule_id=stored[0]["id"])
    assert box.schedule_task("list") == "Planlanmış görev yok."


@pytest.mark.parametrize("goal, expected", [
    ("Her sabah 9'da gündemi özetle", True),
    ("her 2 saatte bir sitemin açık olduğunu kontrol et", True),
    ("Yarın 14:30'da toplantıyı hatırlat", True),
    ("Hafta içi saat 18'de yedek al", True),
    ("every day at 9 send me the news", True),
    ("Planlanmış görevlerimi göster", True),
    ("Masaüstündeki dosyaları listele", False),
    ("Chrome'da ilanlara bak ve kodlarını yaz", False),
])
def test_scheduling_intent(goal: str, expected: bool) -> None:
    assert scheduling_goal(goal) is expected


def test_schedule_schema_only_when_allowed() -> None:
    names = lambda schemas: [schema["function"]["name"] for schema in schemas]
    assert "schedule_task" not in names(route_tool_schemas("Her sabah özetle", False, False, True))
    assert "schedule_task" in names(route_tool_schemas("Her sabah özetle", False, False, True, True))
    assert "schedule_task" in TOOL_NAMES


async def _schemas_seen(goal: str, tmp_path: Path, options: Dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> List[str]:
    seen: List[str] = []

    async def fake_model(clients, messages, schemas, session_id, backend, emit, should_stop):
        seen.extend(schema["function"]["name"] for schema in schemas)
        return {"content": "Tamam.", "tool_calls": [], "finish_reason": "stop", "usage": main.ZERO_USAGE}, backend

    monkeypatch.setattr(main, "_call_model_with_retries", fake_model)
    await main.run_agent_with_callback(
        goal, lambda event: None,
        {"requested_backend": None, "should_stop": lambda: False,
         "state_file": str(tmp_path / "memory.json"), "history": [], **options},
        {"opencode": object()},
    )
    return seen


@pytest.mark.asyncio
async def test_agent_offers_scheduling_only_with_paired_bridge_and_not_inside_a_scheduled_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    goal = "Her sabah 9'da gündemi özetle"
    assert "schedule_task" not in await _schemas_seen(goal, tmp_path, {}, monkeypatch)
    (tmp_path / "telegram.json").write_text('{"chat_id": 1, "user_id": 2}', encoding="utf-8")
    assert "schedule_task" in await _schemas_seen(goal, tmp_path, {}, monkeypatch)
    assert "schedule_task" not in await _schemas_seen(goal, tmp_path, {"scheduled_run": True}, monkeypatch)
    assert "schedule_task" not in await _schemas_seen("Dosyaları listele", tmp_path, {}, monkeypatch)


# --- Telegram zamanlayıcısı ---

class ScheduleAPI:
    def __init__(self) -> None:
        self.sent: List[str] = []

    async def send(self, chat_id: int, text: str) -> int:
        self.sent.append(text)
        return len(self.sent)

    async def edit(self, chat_id: int, message_id: int, text: str) -> None:
        pass

    async def send_draft(self, chat_id: int, draft_id: int, rich_message: Dict[str, str]) -> None:
        pass

    async def send_html(self, chat_id: int, content: str) -> int:
        return 1

    async def edit_html(self, chat_id: int, message_id: int, content: str) -> None:
        pass


def _write_daily(tmp_path: Path, next_run: datetime) -> None:
    spec = schedule.build_spec("daily", next_run.strftime("%H:%M"), None, None, None)
    record = {"id": "abc123", "goal": "Gündemi özetle", "spec": spec,
              "created_at": next_run.isoformat(), "next_run": next_run.isoformat(), "last_run": None, "runs": 0}
    schedule.save_schedules(tmp_path / "schedules.json", [record])


def _fake_run(seen: List[Dict[str, Any]]) -> Any:
    async def run(goal: str, emit: Any, options: Any, clients: Any) -> Any:
        seen.append({"goal": goal, **options})
        metrics = {"turns": 1, "tool_calls": 0, "elapsed_seconds": 0.1, "backend": "ollama-cloud",
                   "prompt_tokens": 1, "cached_tokens": 0, "completion_tokens": 1}
        emit({"kind": "run_finished", "success": True, "outcome": "özet", "reason": "", "metrics": metrics})
        return {"outcome": "özet", "success": True, "reason": "", "metrics": metrics,
                "exchange": make_exchange(goal, "özet", [])}
    return run


@pytest.mark.asyncio
async def test_scheduler_starts_due_task_once_and_advances_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, helsinki: None,
) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    now = local(2026, 9, 25, 9, 0)
    _write_daily(tmp_path, now - timedelta(minutes=1))
    seen: List[Dict[str, Any]] = []
    monkeypatch.setattr(telegram, "run_agent_with_callback", _fake_run(seen))
    api = ScheduleAPI()
    bridge = telegram.TelegramBridge(api, {"chat_id": 123, "user_id": 456})
    await bridge.scheduler_tick(now)
    assert bridge.active is not None
    await bridge.scheduler_tick(now)  # görev sürerken ikinci kez başlamaz
    await bridge.active
    assert len(seen) == 1 and seen[0]["goal"] == "Gündemi özetle" and seen[0]["scheduled_run"] is True
    assert any(text.startswith("⏰ Zamanlanmış görev başlıyor [abc123]") for text in api.sent)
    stored = schedule.load_schedules(tmp_path / "schedules.json")
    assert stored[0]["runs"] == 1 and stored[0]["next_run"].startswith("2026-09-26T08:59")
    await bridge.scheduler_tick(now + timedelta(minutes=1))
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_scheduler_skips_stale_runs_and_waits_for_a_busy_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, helsinki: None,
) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    now = local(2026, 9, 25, 18, 0)
    seen: List[Dict[str, Any]] = []
    monkeypatch.setattr(telegram, "run_agent_with_callback", _fake_run(seen))
    api = ScheduleAPI()
    bridge = telegram.TelegramBridge(api, {"chat_id": 123, "user_id": 456})
    _write_daily(tmp_path, local(2026, 9, 25, 9, 0))
    await bridge.scheduler_tick(now)
    assert bridge.active is None and seen == []
    assert api.sent[-1].startswith("⏰ Kaçırıldı")
    assert schedule.load_schedules(tmp_path / "schedules.json")[0]["next_run"].startswith("2026-09-26T09:00")

    _write_daily(tmp_path, now - timedelta(minutes=2))
    with host_task_lock():
        await bridge.scheduler_tick(now)
    assert bridge.active is None and seen == []
    await bridge.scheduler_tick(now)
    assert bridge.active is not None
    await bridge.active
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_schedule_commands_list_and_remove(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    api = ScheduleAPI()
    bridge = telegram.TelegramBridge(api, {"chat_id": 123, "user_id": 456})
    message = lambda text: {"message": {"chat": {"id": 123, "type": "private"}, "from": {"id": 456}, "text": text}}
    await bridge.handle(message("/schedules"))
    assert api.sent[-1].startswith("Planlanmış görev yok")
    _write_daily(tmp_path, datetime.now().astimezone() + timedelta(hours=1))
    await bridge.handle(message("/schedules"))
    assert "[abc123]" in api.sent[-1] and "Gündemi özetle" in api.sent[-1]
    await bridge.handle(message("/unschedule yok"))
    assert api.sent[-1].startswith("Plan bulunamadı")
    await bridge.handle(message("/unschedule abc123"))
    assert api.sent[-1].startswith("Plan silindi")
    assert schedule.load_schedules(tmp_path / "schedules.json") == []
    assert bridge.active is None


class YieldingAPI(ScheduleAPI):
    """Her gönderimde olay döngüsüne dönen API: eşzamanlı mesajların araya girmesini sınar."""

    async def send(self, chat_id: int, text: str) -> int:
        await asyncio.sleep(0)
        return await super().send(chat_id, text)


@pytest.mark.asyncio
async def test_scheduler_and_user_message_never_run_two_tasks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, helsinki: None,
) -> None:
    """Zamanlayıcı ile kullanıcı mesajı aynı anda gelirse tek görev çalışır, diğeri 'görev çalışıyor' alır."""
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    now = local(2026, 9, 25, 9, 0)
    _write_daily(tmp_path, now - timedelta(minutes=1))
    seen: List[Dict[str, Any]] = []
    monkeypatch.setattr(telegram, "run_agent_with_callback", _fake_run(seen))
    api = YieldingAPI()
    bridge = telegram.TelegramBridge(api, {"chat_id": 123, "user_id": 456})
    user = {"message": {"chat": {"id": 123, "type": "private"}, "from": {"id": 456}, "text": "Başka bir görev"}}
    await asyncio.gather(bridge.scheduler_tick(now), bridge.handle(user))
    assert bridge.active is not None
    await bridge.active
    assert [item["goal"] for item in seen] == ["Gündemi özetle"]
    assert "Bir görev çalışıyor. /stop veya /status kullanın." in api.sent
