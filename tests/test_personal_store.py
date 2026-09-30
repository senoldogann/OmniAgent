"""companion.db: arşiv+imleç işlemi, yeniden oynatma, teslim doğrulaması, yanıtsız burst (gerçek SQLite)."""
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional

import pytest

from omniagent.memory import personal
from omniagent.memory.personal import PersonalStore, SchemaError, opened_store, to_utc_iso, utc_iso
from omniagent.memory.personal import NewFact

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)


def at(seconds: float) -> str:
    return utc_iso(NOW + timedelta(seconds=seconds))


@pytest.fixture
def store(tmp_path: Path) -> Iterator[PersonalStore]:
    opened = PersonalStore(tmp_path / "companion.db")
    yield opened
    opened.close()


def test_incoming_is_archived_once_and_cursor_never_goes_back(store: PersonalStore) -> None:
    assert store.record_incoming(41, "g41", "selam", at(0)) is not None
    assert store.cursor() == 41
    assert store.record_incoming(41, "g41", "selam", at(0)) is None
    store.advance_cursor(12)
    assert store.cursor() == 41
    assert [message["text"] for message in store.recent_messages(10)] == ["selam"]


def test_outgoing_is_confirmed_by_echo_or_marked_unconfirmed(store: PersonalStore) -> None:
    sent = store.record_outgoing("naber", "chat", at(0))
    stale = store.record_outgoing("görüşürüz", "chat", at(0))
    assert store.confirm_outgoing("naber", 50, "g50", NOW + timedelta(seconds=5), 60.0) == sent
    assert store.confirm_outgoing("naber", 51, "g51", NOW + timedelta(seconds=6), 60.0) is None
    assert store.expire_pending(NOW + timedelta(seconds=61), 60.0) == [stale]
    deliveries = {message["id"]: message["delivery"] for message in store.recent_messages(10)}
    assert deliveries == {sent: "sent", stale: "unconfirmed"}
    assert store.cursor() == 50


def test_unanswered_burst_handles_z_and_offset_timestamps(store: PersonalStore) -> None:
    store.record_outgoing("dün konuştuk", "chat", at(-7200))
    store.record_incoming(60, "g60", "orda mısın", to_utc_iso("2026-09-29T11:59:00Z"))
    store.record_incoming(61, "g61", "?", to_utc_iso("2026-09-29T14:59:30+03:00"))
    assert [message["text"] for message in store.unanswered_burst(NOW, 3600.0)] == ["orda mısın", "?"]
    assert store.unanswered_burst(NOW + timedelta(hours=2), 3600.0) == []
    store.record_outgoing("burdayım", "chat", at(1))
    assert store.unanswered_burst(NOW + timedelta(seconds=2), 3600.0) == []


def test_store_is_private_persistent_and_tracks_latency(tmp_path: Path) -> None:
    path = tmp_path / "companion.db"
    first = PersonalStore(path)
    first.record_incoming(7, "g7", "kalıcı mı", at(0))
    for latency in (900.0, 2100.0, 1500.0):
        first.record_latency(latency)
    first.close()
    assert path.stat().st_mode & 0o777 == 0o600
    reopened = PersonalStore(path)
    try:
        assert reopened.cursor() == 7
        assert reopened.latency_p50() == 1500.0
    finally:
        reopened.close()


def test_naive_timestamp_is_rejected() -> None:
    with pytest.raises(ValueError, match="Saat dilimsiz"):
        to_utc_iso("2026-09-29T12:00:00")


def test_invalid_incoming_row_raises_and_keeps_cursor(store: PersonalStore) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        store.record_incoming(5, "g5", None, at(0))  # type: ignore[arg-type]
    assert store.cursor() == 0
    assert store.recent_messages(10) == []


def test_task_start_links_the_ack_bubble_to_its_goal(store: PersonalStore) -> None:
    """İş başlatan balon hedefle eşlenir; geçmiş bunu gerçek araç çağrısı olarak gösterebilsin."""
    ack = store.record_outgoing("bakıyorum hemen", "chat", at(0))
    chatter = store.record_outgoing("naber", "chat", at(1))
    store.record_task_start(ack, "Masaüstündeki dosyaları listele")
    assert store.task_starts([ack, chatter]) == {ack: "Masaüstündeki dosyaları listele"}
    assert store.task_starts([]) == {}


def phase_a_file(path: Path) -> None:
    """Faz A'nın canlı dosyası: v1 şeması (user_version 1), her açılışta kurulan task_starts, bir mesaj ve bir iş."""
    connection = sqlite3.connect(path)
    for statement in personal._SCHEMA_V1:
        connection.execute(statement)
    connection.execute("CREATE TABLE task_starts (message_id INTEGER PRIMARY KEY REFERENCES messages(id), "
                       "goal TEXT NOT NULL)")
    connection.execute("INSERT INTO messages(imsg_rowid, guid, direction, kind, text, created_at, delivery) "
                       "VALUES (7, 'g7', 'in', 'chat', 'cuma İzmir’e gidiyorum', ?, NULL)", (at(0),))
    connection.execute("INSERT INTO activity(kind, origin, goal, rationale, outcome, success, started_at, "
                       "finished_at, tokens) VALUES ('task', 'user', 'rapor hazırla', '', 'hazır', 1, ?, ?, 120)",
                       (at(0), at(5)))
    connection.execute("PRAGMA user_version = 1")
    connection.commit()
    connection.close()


def test_phase_a_file_migrates_once_to_v2(tmp_path: Path) -> None:
    path = tmp_path / "companion.db"
    phase_a_file(path)
    with opened_store(path) as store:
        assert store.get_state("schema_version") == "2"
        assert store.connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert store.connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert [(item["text"], item["direction"]) for item in store.recent_messages(10)] == [
            ("cuma İzmir’e gidiyorum", "in")]
        assert [(task["goal"], task["channel"]) for task in store.recent_tasks(5)] == [("rapor hazırla", "imessage")]
        indexed = store.connection.execute(
            "SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?", ('"izmir"*',)).fetchall()
        assert [int(row[0]) for row in indexed] == [1]
    # Göç ikinci kez çalışsaydı ALTER TABLE 'duplicate column' ile düşerdi.
    with opened_store(path) as reopened:
        assert reopened.get_state("schema_version") == "2"


@pytest.mark.parametrize("stored, problem", [("iki", "bozuk"), ("3", "yeni")])
def test_corrupt_or_newer_schema_fails_loudly(tmp_path: Path, stored: str, problem: str) -> None:
    path = tmp_path / "companion.db"
    with opened_store(path) as store:
        store.set_state("schema_version", stored)
    with pytest.raises(SchemaError, match=problem):
        PersonalStore(path)


def test_other_channels_never_enter_deniz_history(store: PersonalStore) -> None:
    store.record_incoming(1, "g1", "orda mısın", at(0))
    store.record_channel_message("telegram", "raporu hazırla", at(1))
    store.record_channel_message("desktop", "masaüstünü topla", at(2))
    assert [item["text"] for item in store.recent_messages(10)] == ["orda mısın"]
    assert [item["text"] for item in store.unanswered_burst(NOW + timedelta(seconds=3), 3600.0)] == ["orda mısın"]
    with pytest.raises(ValueError, match="telegram, desktop"):
        store.record_channel_message("imessage", "iMessage arşivi record_incoming'dedir", at(3))


def test_activity_carries_its_channel(store: PersonalStore) -> None:
    for index, channel in enumerate(("imessage", "telegram", "desktop")):
        store.record_activity({"kind": "task", "origin": "user", "channel": channel, "goal": f"iş {index}",
                               "rationale": "", "outcome": "tamam", "success": index != 1, "started_at": at(index),
                               "finished_at": at(index + 1), "tokens": 10})
    store.record_activity({"kind": "lesson", "origin": "autonomous", "channel": "imessage", "goal": "ders",
                           "rationale": "", "outcome": "not", "success": True, "started_at": at(5),
                           "finished_at": at(5), "tokens": 0})
    assert [(task["goal"], task["channel"], task["success"]) for task in store.recent_tasks(2)] == [
        ("iş 1", "telegram", False), ("iş 2", "desktop", True)]
    assert [item["channel"] for item in store.activities_since(NOW)] == ["imessage", "telegram", "desktop", "imessage"]


def fact(statement: str, quote: str, message_id: int, category: str, supersedes: Optional[int]) -> NewFact:
    return {"statement": statement, "quote": quote, "message_id": message_id, "category": category,
            "supersedes": supersedes, "follow_up_at": None}


def test_learning_commit_supersedes_forgets_and_guards_the_cursor(store: PersonalStore) -> None:
    home = store.record_channel_message("telegram", "İzmir’de yaşıyorum", at(0))
    agent = store.record_outgoing("İzmir güzeldir", "chat", at(1))
    first = store.commit_learning(0, home, [fact("Kullanıcı İzmir'de yaşıyor.", "İzmir’de yaşıyorum", home,
                                                 "durum", None)], at(2))
    assert first is not None and store.memory_cursor() == home
    moved = store.record_channel_message("telegram", "artık Ankara’da yaşıyorum", at(3))
    second = store.commit_learning(home, moved, [
        fact("Kullanıcı Ankara'da yaşıyor.", "artık Ankara’da yaşıyorum", moved, "durum", first[0])], at(4))
    assert second is not None
    assert [(item["id"], item["statement"], item["said_at"]) for item in store.active_facts()] == [
        (second[0], "Kullanıcı Ankara'da yaşıyor.", at(3))]
    # Eski imleçle ikinci yazım (başka köprünün bayat turu) reddedilir, hiçbir şey eklenmez.
    assert store.commit_learning(home, moved, [fact("Tekrar.", "artık Ankara’da yaşıyorum", moved, "durum", None)],
                                 at(5)) is None
    assert len(store.active_facts()) == 1
    # Kanıt değişmezi veritabanında da korunur: ajanın mesajına bilgi bağlanamaz, imleç de ilerlemez.
    with pytest.raises(sqlite3.IntegrityError, match=r"kullanıcı \(in\)"):
        store.commit_learning(moved, moved, [fact("Uydurma.", "İzmir güzeldir", agent, "durum", None)], at(6))
    assert store.memory_cursor() == moved
    assert store.forget_fact(second[0], at(7)) is True
    assert store.forget_fact(second[0], at(8)) is False
    assert store.active_facts() == []


def test_recall_is_fts_over_facts_and_all_channels_with_direction_labels(store: PersonalStore) -> None:
    trip = store.record_incoming(10, "g10", "cuma İzmir’e gidiyorum", at(0))
    assert trip is not None
    store.record_outgoing("İzmir'de hava güzel olacak", "chat", at(1))
    store.record_channel_message("telegram", "kizimin adi Ada, İzmir’de okuyor", at(2))
    store.record_incoming(11, "g11", "İzmir evinin wifi şifresi 1234", at(3))
    learned = store.commit_learning(0, trip, [fact("Kullanıcı cuma İzmir'e gidiyor.", "cuma İzmir’e gidiyorum", trip,
                                                   "plan", None)], at(4))
    assert learned is not None
    hits = store.recall("İzmir", 8)
    assert (hits[0]["kind"], hits[0]["ref"], hits[0]["quote"]) == ("fact", learned[0], "cuma İzmir’e gidiyorum")
    messages = {(hit["direction"], hit["channel"], hit["text"]) for hit in hits[1:]}
    assert ("out", "imessage", "İzmir'de hava güzel olacak") in messages
    assert ("in", "telegram", "kizimin adi Ada, İzmir’de okuyor") in messages
    assert all("şifresi" not in hit["text"] for hit in hits)                  # gizli bilgi dönmez
    assert all(hit["ref"] != trip for hit in hits if hit["kind"] == "message")  # bilgisi dönen mesaj tekrar etmez
    assert [hit["text"] for hit in store.recall("kızımın", 8)] == ["kizimin adi Ada, İzmir’de okuyor"]
    assert store.recall("?!", 8) == [] and len(store.recall("İzmir", 1)) == 1
    store.forget_fact(learned[0], at(5))
    after = store.recall("İzmir", 8)
    assert all(hit["kind"] == "message" for hit in after) and all(hit["ref"] != trip for hit in after)


def test_learning_status_counts_user_words_on_all_channels_and_keeps_failures(store: PersonalStore) -> None:
    assert store.learning_status() == {"pending": 0, "last_in_at": None, "failed_at": None}
    first = store.record_incoming(20, "g20", "selam", at(0))
    assert first is not None
    store.record_outgoing("selaam", "chat", at(1))
    last = store.record_channel_message("desktop", "raporu Belgeler'e koy", at(2))
    assert store.learning_status() == {"pending": 2, "last_in_at": at(2), "failed_at": None}
    assert [(item["id"], item["channel"]) for item in store.pending_evidence(0, 10)] == [
        (first, "imessage"), (last, "desktop")]
    assert [item["id"] for item in store.pending_evidence(first, 10)] == [last]
    store.record_learning_failure({"at": at(3), "error_type": "LearningError", "reason": "araç çağrılmadı"})
    assert store.learning_failure() == {"at": at(3), "error_type": "LearningError", "reason": "araç çağrılmadı"}
    assert store.learning_status()["failed_at"] == at(3)
    assert store.commit_learning(0, last, [], at(4)) == []
    assert store.learning_failure() is None and store.learning_status()["pending"] == 0


def test_chat_tool_calls_round_trip_in_order(store: PersonalStore) -> None:
    bubble = store.record_outgoing("buldum", "chat", at(0))
    store.record_chat_tool_call(bubble, {"name": "recall", "arguments": '{"query": "İzmir"}', "result": "sonuç yok"})
    store.record_chat_tool_call(bubble, {"name": "forget", "arguments": '{"fact_id": 3}', "result": "#3 unutuldu"})
    assert [call["name"] for call in store.chat_tool_calls([bubble, 999])[bubble]] == ["recall", "forget"]
    assert store.chat_tool_calls([]) == {}
