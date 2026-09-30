"""companion.db: arşiv+imleç işlemi, yeniden oynatma, teslim doğrulaması, yanıtsız burst (gerçek SQLite)."""
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

import pytest

from omniagent.memory.personal import PersonalStore, to_utc_iso, utc_iso

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
