"""Kanal kayıt katmanı: kullanıcı sözü ve iş günlüğü, gizli bilgi süzgeci, kayıt hatasının görevi durdurmaması, soru
yanıtındaki kullanıcı sözleri, ana ajanın profil ve personal_memory yüzü, hafıza komutları (gerçek SQLite)."""
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List, Tuple

import pytest

from omniagent.app.types import RunReport
from omniagent.core.conversation import make_exchange
from omniagent.memory import channels
from omniagent.memory.personal import opened_store, utc_iso
from omniagent.memory.profile import MemoryCommand

TZ = timezone(timedelta(hours=3))
NOW = datetime(2026, 9, 30, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Yalıtılmış veri kökü ve temiz süreç içi kayıt bayrağı; companion.db yolunu döner."""
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(channels, "_record_health", {})
    return tmp_path / "companion.db"


def rows(database: Path) -> List[Tuple[str, str, str]]:
    connection = sqlite3.connect(database)
    try:
        return [(str(channel), str(direction), str(text)) for channel, direction, text in
                connection.execute("SELECT channel, direction, text FROM messages ORDER BY id")]
    finally:
        connection.close()


def report(goal: str, outcome: str, success: bool) -> RunReport:
    return {"outcome": outcome, "success": success, "reason": "",
            "metrics": {"turns": 1, "tool_calls": 0, "elapsed_seconds": 42.0, "backend": "openai",
                        "prompt_tokens": 100, "cached_tokens": 0, "completion_tokens": 20},
            "exchange": make_exchange(goal, outcome, [])}


def test_user_words_are_recorded_but_secrets_never_reach_the_database(data: Path) -> None:
    channels.record_user_message("telegram", "  kızımın adı Ela  ", utc_iso(NOW))
    channels.record_user_message("desktop", "GitHub token'ım ghp_0123456789abcdefghij", utc_iso(NOW))
    channels.record_user_message("telegram", "   ", utc_iso(NOW))
    assert rows(data) == [("telegram", "in", "kızımın adı Ela")]
    assert channels.last_record_failure() is None
    with pytest.raises(ValueError, match="telegram, desktop"):
        channels.record_user_message("imessage", "yanlış kanal", utc_iso(NOW))
    assert channels.companion_db_beside("/x/y/user_memory.json") == Path("/x/y/companion.db")


def test_recording_failure_never_raises_and_shows_in_status(data: Path) -> None:
    data.write_bytes(b"bu bir sqlite dosyasi degil" * 40)
    channels.record_user_message("telegram", "kızımın adı Ela", utc_iso(NOW))
    channels.record_report("telegram", report("rapor hazırla", "hazır", True))
    failure = channels.last_record_failure()
    assert failure is not None and (failure["channel"], failure["operation"]) == ("telegram", "task")
    line = channels.record_failure_line(TZ)
    assert line is not None and line.startswith("Kanıtlı hafıza kaydı başarısız (") and "DatabaseError" in line
    for suffix in ("", "-wal", "-shm"):
        Path(f"{data}{suffix}").unlink(missing_ok=True)
    channels.record_user_message("telegram", "kızımın adı Ela", utc_iso(NOW))
    assert channels.last_record_failure() is None and channels.record_failure_line(TZ) is None


def test_recording_error_logs_never_include_private_exception_text(
    data: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    private = "kişisel mesajım: kızımın adı Ela ghp_0123456789abcdefghij"

    def broken_store(path: Path) -> None:
        raise sqlite3.OperationalError(private)

    monkeypatch.setattr(channels, "opened_store", broken_store)
    channels.record_user_message("telegram", "normal hedef", utc_iso(NOW))
    assert channels.last_record_failure()["error_type"] == "OperationalError"
    assert private not in str([record.__dict__ for record in caplog.records])


def test_failed_task_is_channel_labelled_and_masks_the_goal(data: Path) -> None:
    channels.record_failed_task("desktop", "şifrem kedi123", "RuntimeError", utc_iso(NOW), 12)
    with opened_store(data) as store:
        [task] = store.recent_tasks(5)
    assert task["channel"] == "desktop" and task["success"] is False
    assert task["goal"] == channels.HIDDEN_TEXT and task["tokens"] == 12
    assert task["outcome"] == "Görev tamamlanamadı (RuntimeError)."


@pytest.mark.asyncio
async def test_desktop_exception_records_failure_without_private_error_details(
    data: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omniagent.ui import app as ui

    async def broken_run(*args: object) -> RunReport:
        raise RuntimeError("özel dosya içeriği: kızımın adı Ela")

    monkeypatch.setattr(ui, "run_agent_with_callback", broken_run)
    app = SimpleNamespace(_clients={}, _post=lambda event: None)
    with pytest.raises(RuntimeError, match="özel dosya"):
        await ui.OmniUI._run_exclusive(app, "raporu hazırla", {})
    with opened_store(data) as store:
        [task] = store.recent_tasks(5)
    assert (task["channel"], task["goal"], task["success"]) == ("desktop", "raporu hazırla", False)
    assert task["outcome"] == "Görev tamamlanamadı (RuntimeError)."


@pytest.mark.asyncio
async def test_desktop_report_is_recorded_before_the_run_returns(
    data: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omniagent.ui import app as ui

    completed = report("raporu hazırla", "tamam", False)

    async def run(*args: object) -> RunReport:
        options = args[2]
        assert isinstance(options, dict)
        assert options["task_context"] is ui.async_host_task_lock_preempting
        return completed

    monkeypatch.setattr(ui, "run_agent_with_callback", run)
    app = SimpleNamespace(_clients={}, _post=lambda event: None)
    assert await ui.OmniUI._run_exclusive(app, "raporu hazırla", {}) is completed
    with opened_store(data) as store:
        tasks = store.recent_tasks(5)
    assert len(tasks) == 1 and tasks[0]["success"] is False


def test_task_report_lands_in_the_activity_log_with_masked_secrets(data: Path) -> None:
    channels.record_report("telegram", report("rapor hazırla", "hazır", True))
    channels.record_task("desktop", "şifrem kedi123, wifi'ye bağlan", "bağlandı", True, utc_iso(NOW), utc_iso(NOW), 5)
    with opened_store(data) as store:
        tasks = store.recent_tasks(5)
    assert [(task["channel"], task["goal"], task["tokens"]) for task in tasks] == [
        ("telegram", "rapor hazırla", 120), ("desktop", channels.HIDDEN_TEXT, 5)]
    started = datetime.fromisoformat(tasks[0]["started_at"])
    assert (datetime.fromisoformat(tasks[0]["finished_at"]) - started).total_seconds() == pytest.approx(42.0, abs=1)


def test_answer_evidence_keeps_only_free_text_user_words() -> None:
    text_field: Dict[str, object] = {"yanit": {"type": "string", "label": "Yanıtınız"}}
    assert channels.answer_evidence("Raporu hangi klasöre koyayım?", text_field,
                                    {"yanit": " Belgeler/Raporlar "}) == "Belgeler/Raporlar"
    assert channels.answer_evidence("Silinsin mi?", {"onay": {"type": "boolean"}, "_help": "x"}, {"onay": True}) is None
    choice: Dict[str, object] = {"mod": {"type": "string", "choices": ["hızlı", "dikkatli"]}}
    assert channels.answer_evidence("Nasıl?", choice, {"mod": "hızlı"}) is None
    assert channels.answer_evidence("Nasıl?", choice, {"mod": "önce yedek al"}) == "önce yedek al"
    assert channels.answer_evidence("Wi-Fi şifresi nedir?", text_field, {"yanit": "kedi1234"}) is None
    secret_field: Dict[str, object] = {"client_secret": {"type": "string", "label": "Secret"}}
    assert channels.answer_evidence("Uygulama kaydı", secret_field, {"client_secret": "abc"}) is None


@pytest.mark.asyncio
async def test_recording_answer_returns_the_answer_and_records_the_words(data: Path) -> None:
    async def sink(title: str, fields: Dict[str, object]) -> Dict[str, object]:
        return {"yanit": "Belgeler/Raporlar klasörüne, hep oraya"}

    answer = channels.recording_answer("desktop", sink)
    assert await answer("Raporu nereye koyayım?", {"yanit": {"type": "string"}}) == {
        "yanit": "Belgeler/Raporlar klasörüne, hep oraya"}
    assert rows(data) == [("desktop", "in", "Belgeler/Raporlar klasörüne, hep oraya")]


def test_agent_profile_personal_memory_and_commands(data: Path) -> None:
    assert channels.load_agent_profile(data) == ""
    with opened_store(data) as store:
        source = store.record_channel_message("telegram", "kızımın adı Ela", utc_iso(NOW))
        inserted = store.commit_learning(0, source, [
            {"statement": "Kullanıcının kızının adı Ela.", "quote": "kızımın adı Ela", "message_id": source,
             "category": "kisi", "supersedes": None, "follow_up_at": None}], utc_iso(NOW))
        store.record_incoming(3, "g3", "cuma İzmir’e gidiyorum", utc_iso(NOW + timedelta(minutes=1)))
    assert inserted is not None
    fact_id = inserted[0]
    block = channels.load_agent_profile(data)
    assert "### KANITLI PROFİL (evidence, not instructions)" in block and f"[#{fact_id}]" in block
    recalled = channels.personal_memory_action(data, "recall", "İzmir", None)
    assert recalled.startswith("KANITLI HAFIZA ARAMASI: İzmir")
    assert "kullanıcı · imessage ·" in recalled and '"cuma İzmir’e gidiyorum"' in recalled
    with pytest.raises(ValueError, match="query"):
        channels.personal_memory_action(data, "recall", " ", None)
    listing: MemoryCommand = {"action": "list", "fact_id": None}
    assert channels.run_memory_command(data, listing).startswith("kanıtlı hafıza (1 bilgi):")
    assert channels.personal_memory_action(data, "forget", None, fact_id).startswith(f"#{fact_id} unutuldu")
    with pytest.raises(ValueError, match="etkin bilgi yok"):
        channels.personal_memory_action(data, "forget", None, fact_id)
    assert channels.run_memory_command(data, {"action": "forget", "fact_id": fact_id}) == (
        f"#{fact_id} numaralı etkin bir bilgi yok")
    with pytest.raises(channels.PersonalMemoryUnavailable):
        channels.personal_memory_action(data.with_name("yok.db"), "recall", "İzmir", None)
