"""iMessage yol arkadaşının SQLite deposu: mesaj arşivi, etkinlik günlüğü ve anahtar-değer durumu.

Kullanıcı mesajı ve imsg imleci aynı işlemde yazılır: çökme bir mesajı ne kaybettirir ne de iki kez
işletir (aynı imsg satırı ikinci kez gelirse arşive girmez). Zaman damgaları mikrosaniyeli ISO 8601 UTC
metnidir; sözlük sırası zaman sırasıdır. Bağlantı köprü sürecinde tek iş parçacığından (asyncio döngüsü)
kullanılır; kurulum komutu aynı dosyayı ayrı süreçte yalnız durum okumak için açar (WAL).
"""
from __future__ import annotations

import json
import os
import sqlite3
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple, TypedDict

SCHEMA_VERSION: int = 1
CURSOR_KEY: str = "imsg_cursor"
LATENCY_KEY: str = "recent_latencies_ms"
LATENCY_WINDOW: int = 50
UNANSWERED_SCAN_LIMIT: int = 50
BUSY_TIMEOUT_SECONDS: float = 5.0

_SCHEMA_V1: Tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        imsg_rowid INTEGER UNIQUE,
        guid TEXT,
        direction TEXT NOT NULL CHECK (direction IN ('in', 'out')),
        kind TEXT NOT NULL CHECK (kind IN ('chat', 'proactive', 'task_report', 'question', 'file')),
        text TEXT NOT NULL,
        created_at TEXT NOT NULL,
        delivery TEXT CHECK (delivery IS NULL OR delivery IN ('pending', 'sent', 'unconfirmed'))
    )""",
    "CREATE INDEX IF NOT EXISTS messages_delivery ON messages(direction, delivery, created_at)",
    """CREATE TABLE IF NOT EXISTS activity (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,
        origin TEXT NOT NULL CHECK (origin IN ('user', 'autonomous')),
        goal TEXT NOT NULL,
        rationale TEXT NOT NULL,
        outcome TEXT NOT NULL,
        success INTEGER NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT NOT NULL,
        tokens INTEGER NOT NULL
    )""",
    "CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
)
# Sürüm kapısından bağımsız, her açılışta kurulan ek tablolar: yalnız ekler, mevcut veriye dokunmaz.
# task_starts: Deniz'in iş başlattığı balon → işin hedefi (geçmişte gerçek start_task çağrısı olarak gösterilir).
_ADDITIVE_TABLES: Tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS task_starts (
        message_id INTEGER PRIMARY KEY REFERENCES messages(id),
        goal TEXT NOT NULL
    )""",
)


class ArchivedMessage(TypedDict):
    id: int
    direction: str
    kind: str
    text: str
    created_at: str
    delivery: Optional[str]


class ActivityRecord(TypedDict):
    kind: str
    origin: str
    goal: str
    rationale: str
    outcome: str
    success: bool
    started_at: str
    finished_at: str
    tokens: int


def utc_iso(moment: datetime) -> str:
    """Saat dilimli zamanı mikrosaniyeli UTC ISO 8601 metnine çevirir; saat dilimsiz zaman hatadır. Saf."""
    if moment.tzinfo is None:
        raise ValueError(f"Saat dilimsiz zaman damgası: {moment!r}")
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def to_utc_iso(value: str) -> str:
    """imsg'nin ISO 8601 zamanını ('Z' ya da ofsetli) UTC metnine çevirir. Saf."""
    return utc_iso(datetime.fromisoformat(value))


def _message(row: sqlite3.Row) -> ArchivedMessage:
    return {
        "id": int(row["id"]), "direction": str(row["direction"]), "kind": str(row["kind"]),
        "text": str(row["text"]), "created_at": str(row["created_at"]),
        "delivery": None if row["delivery"] is None else str(row["delivery"]),
    }


def _activity(row: sqlite3.Row) -> ActivityRecord:
    return {
        "kind": str(row["kind"]), "origin": str(row["origin"]), "goal": str(row["goal"]),
        "rationale": str(row["rationale"]), "outcome": str(row["outcome"]), "success": bool(row["success"]),
        "started_at": str(row["started_at"]), "finished_at": str(row["finished_at"]), "tokens": int(row["tokens"]),
    }


def _row_id(cursor: sqlite3.Cursor) -> int:
    if cursor.lastrowid is None:
        raise RuntimeError("SQLite eklenen satırın kimliğini döndürmedi.")
    return int(cursor.lastrowid)


class PersonalStore:
    """companion.db bağlantısı (dış sistem bağlayıcısı)."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection: sqlite3.Connection = sqlite3.connect(path, timeout=BUSY_TIMEOUT_SECONDS)
        self.connection.row_factory = sqlite3.Row
        os.chmod(path, 0o600)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self._migrate()

    def close(self) -> None:
        self.connection.close()

    def _migrate(self) -> None:
        # BEGIN IMMEDIATE: kurulum komutu ile servis aynı anda ilk kez açarsa şema bir kez kurulur.
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            version: int = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise RuntimeError(f"companion.db şeması ({version}) bu sürümün bildiğinden ({SCHEMA_VERSION}) yeni.")
            if version == 0:
                for statement in _SCHEMA_V1:
                    self.connection.execute(statement)
                self.connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            for statement in _ADDITIVE_TABLES:
                self.connection.execute(statement)
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def _raise_cursor(self, imsg_rowid: int) -> None:
        # İmleç yalnız ileri gider: taşma sonrası yeniden oynatılan eski satır imleci geri almaz.
        self.connection.execute(
            "INSERT INTO state(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = "
            "CASE WHEN CAST(excluded.value AS INTEGER) > CAST(state.value AS INTEGER) "
            "THEN excluded.value ELSE state.value END",
            (CURSOR_KEY, str(imsg_rowid)),
        )

    def cursor(self) -> int:
        """Son işlenen imsg satırı (ROWID); hiç yoksa 0."""
        value: Optional[str] = self.get_state(CURSOR_KEY)
        return int(value) if value is not None else 0

    def advance_cursor(self, imsg_rowid: int) -> None:
        """Arşive girmeyen satırı (yetkisiz, grup, yansıma) imleçle geçer."""
        with self.connection:
            self._raise_cursor(imsg_rowid)

    def record_incoming(self, imsg_rowid: int, guid: str, text: str, created_at: str) -> Optional[int]:
        """Kullanıcı mesajını arşivler ve imleci aynı işlemde ilerletir; aynı satır yeniden gelirse None."""
        with self.connection:
            inserted = self.connection.execute(
                "INSERT INTO messages(imsg_rowid, guid, direction, kind, text, created_at, delivery) "
                "VALUES (?, ?, 'in', 'chat', ?, ?, NULL) ON CONFLICT(imsg_rowid) DO NOTHING",
                (imsg_rowid, guid, text, created_at),
            )
            self._raise_cursor(imsg_rowid)
        return _row_id(inserted) if inserted.rowcount == 1 else None

    def record_outgoing(self, text: str, kind: str, created_at: str) -> int:
        """Gönderilecek balonu 'pending' arşivler; izlemede görülünce confirm_outgoing 'sent' yapar."""
        with self.connection:
            inserted = self.connection.execute(
                "INSERT INTO messages(imsg_rowid, guid, direction, kind, text, created_at, delivery) "
                "VALUES (NULL, NULL, 'out', ?, ?, ?, 'pending')",
                (kind, text, created_at),
            )
        return _row_id(inserted)

    def record_task_start(self, message_id: int, goal: str) -> None:
        """İş başlatan balonu işin hedefiyle eşler."""
        with self.connection:
            self.connection.execute("INSERT INTO task_starts(message_id, goal) VALUES (?, ?)", (message_id, goal))

    def task_starts(self, message_ids: List[int]) -> Dict[int, str]:
        """Verilen balonlardan iş başlatanların hedefleri (balon kimliği → hedef)."""
        if not message_ids:
            return {}
        marks: str = ",".join("?" * len(message_ids))
        rows = self.connection.execute(
            f"SELECT message_id, goal FROM task_starts WHERE message_id IN ({marks})", message_ids,
        ).fetchall()
        return {int(row["message_id"]): str(row["goal"]) for row in rows}

    def record_outgoing_file(self, name: str, created_at: str) -> int:
        """Gönderilen dosyayı arşivler; ekin yansıması metinle eşlenemediği için teslim takibi yoktur."""
        with self.connection:
            inserted = self.connection.execute(
                "INSERT INTO messages(imsg_rowid, guid, direction, kind, text, created_at, delivery) "
                "VALUES (NULL, NULL, 'out', 'file', ?, ?, NULL)",
                (f"[dosya: {name}]", created_at),
            )
        return _row_id(inserted)

    def confirm_outgoing(self, text: str, imsg_rowid: int, guid: str, now: datetime,
                         window_seconds: float) -> Optional[int]:
        """İzlemede görülen kendi gönderimimizi pencere içindeki en eski eşleşen bekleyen balonla eşler."""
        since: str = utc_iso(now - timedelta(seconds=window_seconds))
        with self.connection:
            row = self.connection.execute(
                "SELECT id FROM messages WHERE direction = 'out' AND delivery = 'pending' AND text = ? "
                "AND created_at >= ? ORDER BY id LIMIT 1",
                (text, since),
            ).fetchone()
            if row is None:
                return None
            self.connection.execute(
                "UPDATE messages SET delivery = 'sent', imsg_rowid = ?, guid = ? WHERE id = ?",
                (imsg_rowid, guid, int(row["id"])),
            )
            self._raise_cursor(imsg_rowid)
        return int(row["id"])

    def mark_unconfirmed(self, message_id: int) -> None:
        """Gönderimi hata veren ya da sonucu bilinmeyen balonu işaretler."""
        with self.connection:
            self.connection.execute(
                "UPDATE messages SET delivery = 'unconfirmed' WHERE id = ? AND delivery = 'pending'", (message_id,),
            )

    def expire_pending(self, now: datetime, window_seconds: float) -> List[int]:
        """Pencere içinde izlemede görülmeyen balonları 'unconfirmed' yapar ve kimliklerini döndürür."""
        before: str = utc_iso(now - timedelta(seconds=window_seconds))
        with self.connection:
            rows = self.connection.execute(
                "SELECT id FROM messages WHERE direction = 'out' AND delivery = 'pending' AND created_at < ? "
                "ORDER BY id",
                (before,),
            ).fetchall()
            expired: List[int] = [int(row["id"]) for row in rows]
            self.connection.executemany(
                "UPDATE messages SET delivery = 'unconfirmed' WHERE id = ?", [(message_id,) for message_id in expired],
            )
        return expired

    def recent_messages(self, limit: int) -> List[ArchivedMessage]:
        """Son `limit` mesaj, eskiden yeniye."""
        rows = self.connection.execute(
            "SELECT id, direction, kind, text, created_at, delivery FROM messages ORDER BY id DESC LIMIT ?", (limit,),
        ).fetchall()
        return [_message(row) for row in reversed(rows)]

    def unanswered_burst(self, now: datetime, max_age_seconds: float) -> List[ArchivedMessage]:
        """
        Arşivin sonundaki, ardından ajan mesajı gelmemiş kullanıcı mesajları (çökme/yeniden başlatma sırasında
        yanıtlanmamış burst). En yenisi max_age_seconds'tan eskiyse boş: eski konuşma yeniden açılmaz.
        """
        tail: List[ArchivedMessage] = []
        for message in reversed(self.recent_messages(UNANSWERED_SCAN_LIMIT)):
            if message["direction"] != "in":
                break
            tail.insert(0, message)
        if not tail:
            return []
        newest: datetime = datetime.fromisoformat(tail[-1]["created_at"])
        return tail if (now - newest).total_seconds() <= max_age_seconds else []

    def record_activity(self, record: ActivityRecord) -> int:
        """Ajanın gerçek bir eylemini (iş, mesaj) günlüğe yazar."""
        with self.connection:
            inserted = self.connection.execute(
                "INSERT INTO activity(kind, origin, goal, rationale, outcome, success, started_at, finished_at, "
                "tokens) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (record["kind"], record["origin"], record["goal"], record["rationale"], record["outcome"],
                 int(record["success"]), record["started_at"], record["finished_at"], record["tokens"]),
            )
        return _row_id(inserted)

    def activities_since(self, since: datetime) -> List[ActivityRecord]:
        """`since` anından sonra başlayan etkinlikler, eskiden yeniye."""
        rows = self.connection.execute(
            "SELECT kind, origin, goal, rationale, outcome, success, started_at, finished_at, tokens FROM activity "
            "WHERE started_at >= ? ORDER BY id",
            (utc_iso(since),),
        ).fetchall()
        return [_activity(row) for row in rows]

    def get_state(self, key: str) -> Optional[str]:
        row = self.connection.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def set_state(self, key: str, value: str) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO state(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def record_latency(self, latency_ms: float) -> None:
        """Burst bitişinden ilk balona geçen süreyi son LATENCY_WINDOW ölçüm içinde saklar."""
        raw: Optional[str] = self.get_state(LATENCY_KEY)
        values: List[float] = [float(value) for value in json.loads(raw)] if raw is not None else []
        self.set_state(LATENCY_KEY, json.dumps((values + [round(latency_ms, 1)])[-LATENCY_WINDOW:]))

    def latency_p50(self) -> Optional[float]:
        """Son ölçümlerin medyanı (ms); ölçüm yoksa None."""
        raw: Optional[str] = self.get_state(LATENCY_KEY)
        if raw is None:
            return None
        values: List[float] = [float(value) for value in json.loads(raw)]
        return float(statistics.median(values)) if values else None
