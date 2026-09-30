"""companion.db: tüm kanalların kanıtlı kişisel hafızası. İçerik: iMessage arşivi, Telegram/masaüstü kullanıcı
sözleri, kanal etiketli iş günlüğü, kanıtlı bilgiler (facts) ve anahtar-değer durumu.

Üç süreç (iMessage köprüsü, Telegram köprüsü, masaüstü) aynı dosyayı paylaşır: WAL, busy_timeout 5 sn, kısa işlemler.
Telegram, masaüstü ve öğrenme hattı kısa ömürlü bağlantı (opened_store) kullanır. iMessage kullanıcı mesajı ve imsg
imleci aynı işlemde yazılır: çökme bir mesajı ne kaybettirir ne de iki kez işletir. Deniz'in sohbet geçmişi yalnız
iMessage kanalıdır; Telegram/masaüstü sözleri yalnız hafızaya (öğrenme, arama) girer. Zaman damgaları mikrosaniyeli
ISO 8601 UTC metnidir; sözlük sırası zaman sırasıdır. Şema sürümü state.schema_version'dadır (Faz A dosyasında yalnız
PRAGMA user_version=1 vardır); göç bir kez ve tek işlemde yapılır, bozuk ya da ileri sürüm açık hatadır.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import statistics
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Set, Tuple, TypedDict

from omniagent.core.text_norm import ascii_fold
from omniagent.memory.user import sensitive_text

SCHEMA_VERSION: int = 2
SCHEMA_VERSION_KEY: str = "schema_version"
CURSOR_KEY: str = "imsg_cursor"
LATENCY_KEY: str = "recent_latencies_ms"
LATENCY_WINDOW: int = 50
UNANSWERED_SCAN_LIMIT: int = 50
BUSY_TIMEOUT_SECONDS: float = 5.0
# Kanallar: iMessage (Deniz) ve çalışma yüzleri Telegram ile masaüstü. Kullanıcı sözü kaydı (record_channel_message)
# yalnız çalışma yüzlerinden gelir; iMessage mesajları imleçle birlikte record_incoming ile arşivlenir.
CHANNELS: Tuple[str, ...] = ("imessage", "telegram", "desktop")
WORK_CHANNELS: Tuple[str, ...] = ("telegram", "desktop")
# Kanıtlı bilgi kategorileri; çekirdek profil bu sırayla gösterilir.
FACT_CATEGORIES: Tuple[str, ...] = ("kisi", "tercih", "plan", "durum", "olay")
MEMORY_CURSOR_KEY: str = "memory_cursor"
LEARNING_FAILURE_KEY: str = "memory_error"
RECALL_LIMIT: int = 8
SNIPPET_TOKENS: int = 24
_QUERY_TOKEN_LIMIT: int = 8
_NON_WORD: re.Pattern[str] = re.compile(r"[\W_]+")

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
def _sql_list(values: Tuple[str, ...]) -> str:
    """CHECK kısıtının tırnaklı değer listesi; yalnız modül sabitlerinden kurulur (kullanıcı girdisi değil). Saf."""
    return ", ".join(f"'{value}'" for value in values)


# Sürüm 2 (Faz B+): kanal sütunu, kanıtlı bilgiler, FTS5 arama ve Deniz'in hafıza araç çağrıları. Faz A'nın her açılışta
# kurduğu task_starts buraya taşındı; canlı dosyada tablo zaten var, IF NOT EXISTS ile idempotenttir. Dış içerikli FTS
# dizinleri tetiklerle kaynak tabloyla eşit tutulur.
_SCHEMA_V2: Tuple[str, ...] = (
    f"ALTER TABLE messages ADD COLUMN channel TEXT NOT NULL DEFAULT 'imessage' "
    f"CHECK (channel IN ({_sql_list(CHANNELS)}))",
    f"ALTER TABLE activity ADD COLUMN channel TEXT NOT NULL DEFAULT 'imessage' "
    f"CHECK (channel IN ({_sql_list(CHANNELS)}))",
    "CREATE INDEX IF NOT EXISTS messages_channel ON messages(channel, id)",
    "CREATE INDEX IF NOT EXISTS messages_evidence ON messages(direction, id)",
    """CREATE TABLE IF NOT EXISTS task_starts (
        message_id INTEGER PRIMARY KEY REFERENCES messages(id),
        goal TEXT NOT NULL
    )""",
    f"""CREATE TABLE IF NOT EXISTS facts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        statement TEXT NOT NULL,
        quote TEXT NOT NULL,
        message_id INTEGER NOT NULL REFERENCES messages(id),
        category TEXT NOT NULL CHECK (category IN ({_sql_list(FACT_CATEGORIES)})),
        status TEXT NOT NULL CHECK (status IN ('active', 'superseded', 'forgotten')),
        superseded_by INTEGER REFERENCES facts(id),
        follow_up_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS facts_status ON facts(status, category, created_at)",
    # Kanıt değişmezi veritabanında da korunur: bilgi yalnız kullanıcının kendi ('in') mesajına bağlanabilir.
    """CREATE TRIGGER IF NOT EXISTS facts_evidence_in BEFORE INSERT ON facts
        WHEN NOT EXISTS (SELECT 1 FROM messages WHERE id = NEW.message_id AND direction = 'in')
        BEGIN SELECT RAISE(ABORT, 'facts.message_id bir kullanıcı (in) mesajı olmalı'); END""",
    """CREATE TABLE IF NOT EXISTS chat_tool_calls (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        message_id INTEGER NOT NULL REFERENCES messages(id),
        name TEXT NOT NULL CHECK (name IN ('recall', 'forget')),
        arguments TEXT NOT NULL,
        result TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS chat_tool_calls_message ON chat_tool_calls(message_id, id)",
    "CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5("
    "text, content='messages', content_rowid='id', tokenize='unicode61 remove_diacritics 2')",
    """CREATE TRIGGER IF NOT EXISTS messages_fts_insert AFTER INSERT ON messages BEGIN
        INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text); END""",
    """CREATE TRIGGER IF NOT EXISTS messages_fts_delete AFTER DELETE ON messages BEGIN
        INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.id, old.text); END""",
    """CREATE TRIGGER IF NOT EXISTS messages_fts_update AFTER UPDATE OF text ON messages BEGIN
        INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.id, old.text);
        INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text); END""",
    # Faz A arşivindeki mesajlar bir kez dizine girer.
    "INSERT INTO messages_fts(messages_fts) VALUES ('rebuild')",
    "CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5("
    "statement, quote, content='facts', content_rowid='id', tokenize='unicode61 remove_diacritics 2')",
    """CREATE TRIGGER IF NOT EXISTS facts_fts_insert AFTER INSERT ON facts BEGIN
        INSERT INTO facts_fts(rowid, statement, quote) VALUES (new.id, new.statement, new.quote); END""",
    """CREATE TRIGGER IF NOT EXISTS facts_fts_delete AFTER DELETE ON facts BEGIN
        INSERT INTO facts_fts(facts_fts, rowid, statement, quote)
        VALUES ('delete', old.id, old.statement, old.quote); END""",
    """CREATE TRIGGER IF NOT EXISTS facts_fts_update AFTER UPDATE OF statement, quote ON facts BEGIN
        INSERT INTO facts_fts(facts_fts, rowid, statement, quote)
        VALUES ('delete', old.id, old.statement, old.quote);
        INSERT INTO facts_fts(rowid, statement, quote) VALUES (new.id, new.statement, new.quote); END""",
)
_ACTIVITY_COLUMNS: str = "kind, origin, channel, goal, rationale, outcome, success, started_at, finished_at, tokens"


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
    channel: str
    goal: str
    rationale: str
    outcome: str
    success: bool
    started_at: str
    finished_at: str
    tokens: int


class SchemaError(RuntimeError):
    """companion.db şeması bozuk ya da bu sürümün bildiğinden yeni; dosyaya dokunulmaz."""


class FactRecord(TypedDict):
    id: int
    statement: str
    quote: str
    message_id: int
    category: str
    status: str
    follow_up_at: Optional[str]
    created_at: str
    updated_at: str
    said_at: str


class NewFact(TypedDict):
    statement: str
    quote: str
    message_id: int
    category: str
    supersedes: Optional[int]
    follow_up_at: Optional[str]


class EvidenceMessage(TypedDict):
    id: int
    direction: str
    channel: str
    text: str
    created_at: str


class LearningStatus(TypedDict):
    pending: int
    last_in_at: Optional[str]
    failed_at: Optional[str]


class LearningFailure(TypedDict):
    at: str
    error_type: str
    reason: str


class RecallHit(TypedDict):
    """Arama sonucu: 'fact' (kanıtlı bilgi; text=ifade, quote=alıntı) ya da 'message' (text=birebir parça)."""

    kind: str
    ref: int
    direction: str
    channel: str
    created_at: str
    text: str
    quote: str


class ChatToolCall(TypedDict):
    """Deniz'in hafıza araç çağrısı (recall/forget): geçmişte gerçek çağrı + sonuç olarak gösterilir."""

    name: str
    arguments: str
    result: str


def utc_iso(moment: datetime) -> str:
    """Saat dilimli zamanı mikrosaniyeli UTC ISO 8601 metnine çevirir; saat dilimsiz zaman hatadır. Saf."""
    if moment.tzinfo is None:
        raise ValueError(f"Saat dilimsiz zaman damgası: {moment!r}")
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def to_utc_iso(value: str) -> str:
    """imsg'nin ISO 8601 zamanını ('Z' ya da ofsetli) UTC metnine çevirir. Saf."""
    return utc_iso(datetime.fromisoformat(value))


def utc_now_iso() -> str:
    """Şimdiki zamanın mikrosaniyeli UTC ISO metni (kanal kayıtları ve öğrenme zamanları için)."""
    return utc_iso(datetime.now(timezone.utc))


def local_timezone() -> tzinfo:
    """Sistemin yerel saat dilimi; tarihlerin kullanıcıya gösterimi için."""
    zone: Optional[tzinfo] = datetime.now().astimezone().tzinfo
    if zone is None:
        raise RuntimeError("Yerel saat dilimi çözülemedi.")
    return zone


def evidence_fold(text: str) -> str:
    """
    Kanıt karşılaştırma biçimi: Türkçe harfler ASCII'ye (ı/İ → i), birleşik işaretler atılır, casefold; harf ve rakam
    dışı her şey (noktalama, iPhone'un kıvrık kesme işareti, emoji) boşluk olur, boşluklar teke iner. Saf.
    """
    return " ".join(_NON_WORD.sub(" ", ascii_fold(text)).split())


def recall_query(text: str) -> str:
    """
    Serbest aramayı FTS5 MATCH ifadesine çevirir. Her kelime önek aramasıdır (Türkçe ek: 'İzmir' 'İzmir’e'yi bulur) ve
    iki yazımla aranır: evidence_fold biçimi ("kizim") ve i→ı biçimi ("kızım"), çünkü unicode61 dizini ı'yı korur,
    İ/I'yı i'ye indirir. Kelimeler OR ile bağlanır, bm25 en çok eşleşeni öne alır; en az 2 harfli ilk
    _QUERY_TOKEN_LIMIT kelime kullanılır. Kelimeler yalnız harf/rakam olduğu için FTS5 sözdizimine kaçamaz.
    Kullanılabilir kelime yoksa boş metin. Saf.
    """
    tokens: List[str] = [token for token in evidence_fold(text).split() if len(token) >= 2][:_QUERY_TOKEN_LIMIT]
    phrases: List[str] = []
    for token in tokens:
        for variant in (token, token.replace("i", "ı")):
            phrase: str = f'"{variant}"*'
            if phrase not in phrases:
                phrases.append(phrase)
    return " OR ".join(phrases)


def _message(row: sqlite3.Row) -> ArchivedMessage:
    return {
        "id": int(row["id"]), "direction": str(row["direction"]), "kind": str(row["kind"]),
        "text": str(row["text"]), "created_at": str(row["created_at"]),
        "delivery": None if row["delivery"] is None else str(row["delivery"]),
    }


def _activity(row: sqlite3.Row) -> ActivityRecord:
    return {
        "kind": str(row["kind"]), "origin": str(row["origin"]), "channel": str(row["channel"]),
        "goal": str(row["goal"]), "rationale": str(row["rationale"]), "outcome": str(row["outcome"]),
        "success": bool(row["success"]), "started_at": str(row["started_at"]),
        "finished_at": str(row["finished_at"]), "tokens": int(row["tokens"]),
    }


def _fact(row: sqlite3.Row) -> FactRecord:
    return {
        "id": int(row["id"]), "statement": str(row["statement"]), "quote": str(row["quote"]),
        "message_id": int(row["message_id"]), "category": str(row["category"]), "status": str(row["status"]),
        "follow_up_at": None if row["follow_up_at"] is None else str(row["follow_up_at"]),
        "created_at": str(row["created_at"]), "updated_at": str(row["updated_at"]), "said_at": str(row["said_at"]),
    }


def _row_id(cursor: sqlite3.Cursor) -> int:
    if cursor.lastrowid is None:
        raise RuntimeError("SQLite eklenen satırın kimliğini döndürmedi.")
    return int(cursor.lastrowid)


class PersonalStore:
    """companion.db bağlantısı (dış sistem bağlayıcısı)."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path: Path = path
        self.connection: sqlite3.Connection = sqlite3.connect(path, timeout=BUSY_TIMEOUT_SECONDS)
        self.connection.row_factory = sqlite3.Row
        os.chmod(path, 0o600)
        try:
            # Üç süreç aynı dosyayı paylaşır: kilitli dosyada 5 sn beklenir, sonra açık hata.
            self.connection.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_SECONDS * 1000)}")
            self.connection.execute("PRAGMA journal_mode=WAL")
            self._migrate()
        except BaseException:
            self.connection.close()
            raise

    def close(self) -> None:
        self.connection.close()

    def _stored_version(self) -> int:
        """
        Kayıtlı şema sürümü: state.schema_version (v2+); yoksa Faz A'nın PRAGMA user_version değeri (0 = boş dosya).
        Sayı olmayan kayıt bozuk şemadır: SchemaError.
        """
        has_state: bool = self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'state'",
        ).fetchone() is not None
        raw: Optional[str] = self.get_state(SCHEMA_VERSION_KEY) if has_state else None
        if raw is None:
            return int(self.connection.execute("PRAGMA user_version").fetchone()[0])
        if not raw.isdigit():
            raise SchemaError(f"companion.db şema sürümü bozuk: {raw!r}")
        return int(raw)

    def _checked_version(self) -> int:
        version: int = self._stored_version()
        if version > SCHEMA_VERSION:
            raise SchemaError(f"companion.db şeması ({version}) bu sürümün bildiğinden ({SCHEMA_VERSION}) yeni.")
        return version

    def _migrate(self) -> None:
        """
        Şemayı SCHEMA_VERSION'a bir kez taşır. Güncel dosyada yazma kilidi alınmaz (üç süreç kısa bağlantılarla sık
        açar). Göç BEGIN IMMEDIATE altında sürüm yeniden okunarak yapılır: iki süreç aynı anda açarsa göç bir kez
        uygulanır; hata olursa hiçbir değişiklik kalmaz. PRAGMA user_version da güncellenir: eski (Faz A) kod daha
        yeni dosyayı açmayı reddeder.
        """
        if self._checked_version() == SCHEMA_VERSION:
            return
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            version: int = self._checked_version()
            if version < 1:
                for statement in _SCHEMA_V1:
                    self.connection.execute(statement)
            if version < 2:
                for statement in _SCHEMA_V2:
                    self.connection.execute(statement)
            self._put_state(SCHEMA_VERSION_KEY, str(SCHEMA_VERSION))
            self.connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
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

    def record_channel_message(self, channel: str, text: str, created_at: str) -> int:
        """
        Telegram/masaüstü kullanıcı sözünü 'in' mesajı olarak yazar (imsg satırı yok). iMessage arşivi imleçle birlikte
        record_incoming'dedir; başka kanal ValueError.
        """
        if channel not in WORK_CHANNELS:
            raise ValueError(f"Kanal kaydı yalnız {', '.join(WORK_CHANNELS)} için: {channel!r}")
        with self.connection:
            inserted = self.connection.execute(
                "INSERT INTO messages(imsg_rowid, guid, direction, kind, text, created_at, delivery, channel) "
                "VALUES (NULL, NULL, 'in', 'chat', ?, ?, NULL, ?)",
                (text, created_at, channel),
            )
        return _row_id(inserted)

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
        """iMessage sohbetinin son `limit` mesajı, eskiden yeniye; diğer kanalların sözleri Deniz'in geçmişine girmez."""
        rows = self.connection.execute(
            "SELECT id, direction, kind, text, created_at, delivery FROM messages WHERE channel = 'imessage' "
            "ORDER BY id DESC LIMIT ?", (limit,),
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
        """Ajanın gerçek bir eylemini (iş, mesaj, ders) kanalıyla günlüğe yazar."""
        with self.connection:
            inserted = self.connection.execute(
                f"INSERT INTO activity({_ACTIVITY_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (record["kind"], record["origin"], record["channel"], record["goal"], record["rationale"],
                 record["outcome"], int(record["success"]), record["started_at"], record["finished_at"],
                 record["tokens"]),
            )
        return _row_id(inserted)

    def activities_since(self, since: datetime) -> List[ActivityRecord]:
        """`since` anından sonra başlayan etkinlikler (tüm kanallar), eskiden yeniye."""
        rows = self.connection.execute(
            f"SELECT {_ACTIVITY_COLUMNS} FROM activity WHERE started_at >= ? ORDER BY id", (utc_iso(since),),
        ).fetchall()
        return [_activity(row) for row in rows]

    def recent_tasks(self, limit: int) -> List[ActivityRecord]:
        """Tüm kanalların son `limit` görevi (kind='task'), eskiden yeniye: Deniz'in [DURUM] bloğu."""
        rows = self.connection.execute(
            f"SELECT {_ACTIVITY_COLUMNS} FROM activity WHERE kind = 'task' ORDER BY id DESC LIMIT ?", (limit,),
        ).fetchall()
        return [_activity(row) for row in reversed(rows)]

    # --- kanıtlı bilgiler ve arama ---

    def memory_cursor(self) -> int:
        """Öğrenme hattının son işlediği messages.id; hiç yoksa 0."""
        value: Optional[str] = self.get_state(MEMORY_CURSOR_KEY)
        return int(value) if value is not None else 0

    def active_facts(self) -> List[FactRecord]:
        """Etkin bilgiler (unutulan ve yerini yenisine bırakan hariç), kimlik sırasıyla; said_at kanıt mesajının zamanı."""
        rows = self.connection.execute(
            "SELECT f.id, f.statement, f.quote, f.message_id, f.category, f.status, f.follow_up_at, f.created_at, "
            "f.updated_at, m.created_at AS said_at FROM facts f JOIN messages m ON m.id = f.message_id "
            "WHERE f.status = 'active' ORDER BY f.id",
        ).fetchall()
        return [_fact(row) for row in rows]

    def forget_fact(self, fact_id: int, now: str) -> bool:
        """Etkin bilgiyi 'forgotten' yapar: istemden ve aramadan çıkar. Etkin değilse (yok, unutulmuş) False."""
        with self.connection:
            updated = self.connection.execute(
                "UPDATE facts SET status = 'forgotten', updated_at = ? WHERE id = ? AND status = 'active'",
                (now, fact_id),
            )
        return updated.rowcount == 1

    def recall(self, query: str, limit: int) -> List[RecallHit]:
        """
        FTS5 ile önce etkin bilgilerde, sonra tüm kanalların mesajlarında arar; en çok `limit` birebir parça. Bilgisi
        dönen mesaj ayrıca listelenmez; gizli bilgi süzgecine takılan mesaj hiç döndürülmez. Yön korunur: ajanın kendi
        mesajı ('out') kullanıcı hakkında kanıt değildir, çağıran bunu etiketler.
        """
        match: str = recall_query(query)
        if not match:
            return []
        fact_rows = self.connection.execute(
            "SELECT f.id, f.statement, f.quote, f.message_id, m.channel, m.created_at FROM facts_fts "
            "JOIN facts f ON f.id = facts_fts.rowid JOIN messages m ON m.id = f.message_id "
            "WHERE facts_fts MATCH ? AND f.status = 'active' ORDER BY bm25(facts_fts) LIMIT ?",
            (match, limit),
        ).fetchall()
        hits: List[RecallHit] = [
            {"kind": "fact", "ref": int(row["id"]), "direction": "in", "channel": str(row["channel"]),
             "created_at": str(row["created_at"]), "text": str(row["statement"]), "quote": str(row["quote"])}
            for row in fact_rows
        ]
        sources: Set[int] = {int(row["message_id"]) for row in fact_rows}
        message_rows = self.connection.execute(
            "SELECT m.id, m.direction, m.channel, m.created_at, m.text, "
            f"snippet(messages_fts, 0, '', '', '…', {SNIPPET_TOKENS}) AS part FROM messages_fts "
            "JOIN messages m ON m.id = messages_fts.rowid WHERE messages_fts MATCH ? "
            # Unutulan bilginin kaynak mesajı da aramadan çıkar: yoksa 'unut' sonrası söz birebir geri gelirdi.
            "AND m.id NOT IN (SELECT message_id FROM facts WHERE status = 'forgotten') "
            "ORDER BY bm25(messages_fts) LIMIT ?",
            (match, limit * 2),
        ).fetchall()
        for row in message_rows:
            if len(hits) >= limit:
                break
            if int(row["id"]) in sources or sensitive_text(str(row["text"])):
                continue
            hits.append({"kind": "message", "ref": int(row["id"]), "direction": str(row["direction"]),
                         "channel": str(row["channel"]), "created_at": str(row["created_at"]),
                         "text": str(row["part"]), "quote": ""})
        return hits[:limit]

    # --- öğrenme hattı ---

    def learning_status(self) -> LearningStatus:
        """
        Öğrenme tetiğinin girdisi: imleçten sonraki kullanıcı mesajı sayısı (tüm kanallar), son kullanıcı mesajının
        zamanı ve varsa son başarısız turun zamanı.
        """
        pending_row = self.connection.execute(
            "SELECT COUNT(*) AS pending FROM messages WHERE direction = 'in' AND id > ?", (self.memory_cursor(),),
        ).fetchone()
        last_row = self.connection.execute(
            "SELECT MAX(created_at) AS last_in FROM messages WHERE direction = 'in'",
        ).fetchone()
        failure: Optional[LearningFailure] = self.learning_failure()
        return {
            "pending": int(pending_row["pending"]),
            "last_in_at": None if last_row["last_in"] is None else str(last_row["last_in"]),
            "failed_at": None if failure is None else failure["at"],
        }

    def pending_evidence(self, cursor: int, limit: int) -> List[EvidenceMessage]:
        """Öğrenme girdisi: `cursor`'dan sonraki kullanıcı ('in') mesajları, tüm kanallar, eskiden yeniye."""
        rows = self.connection.execute(
            "SELECT id, direction, channel, text, created_at FROM messages WHERE direction = 'in' AND id > ? "
            "ORDER BY id LIMIT ?", (cursor, limit),
        ).fetchall()
        return [{"id": int(row["id"]), "direction": str(row["direction"]), "channel": str(row["channel"]),
                 "text": str(row["text"]), "created_at": str(row["created_at"])} for row in rows]

    def commit_learning(self, expected_cursor: int, new_cursor: int, facts: List[NewFact],
                        now: str) -> Optional[List[int]]:
        """
        Turun sonucunu tek işlemde yazar: bilgiler eklenir, `supersedes` hedefi hâlâ etkin ve aynı kategorideyse
        'superseded' olur, imleç ilerler, son öğrenme hatası silinir. Karşılaştır-ve-yaz: imleç `expected_cursor`
        değilse (başka bir tur yazmış) hiçbir şey yazılmaz ve None döner. Eklenen bilgi kimlikleri döner.
        """
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            if self.memory_cursor() != expected_cursor:
                self.connection.rollback()
                return None
            inserted: List[int] = []
            for fact in facts:
                cursor = self.connection.execute(
                    "INSERT INTO facts(statement, quote, message_id, category, status, superseded_by, follow_up_at, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, 'active', NULL, ?, ?, ?)",
                    (fact["statement"], fact["quote"], fact["message_id"], fact["category"], fact["follow_up_at"],
                     now, now),
                )
                fact_id: int = _row_id(cursor)
                inserted.append(fact_id)
                if fact["supersedes"] is not None:
                    self.connection.execute(
                        "UPDATE facts SET status = 'superseded', superseded_by = ?, updated_at = ? "
                        "WHERE id = ? AND status = 'active' AND category = ?",
                        (fact_id, now, fact["supersedes"], fact["category"]),
                    )
            self._put_state(MEMORY_CURSOR_KEY, str(new_cursor))
            self.connection.execute("DELETE FROM state WHERE key = ?", (LEARNING_FAILURE_KEY,))
            self.connection.commit()
            return inserted
        except BaseException:
            self.connection.rollback()
            raise

    def record_learning_failure(self, failure: LearningFailure) -> None:
        """Son öğrenme hatası (/durum gösterir; tetik bekleme süresini buradan hesaplar)."""
        self.set_state(LEARNING_FAILURE_KEY, json.dumps(failure, ensure_ascii=False))

    def learning_failure(self) -> Optional[LearningFailure]:
        """Son öğrenme hatası; yoksa None. Kayıt bozuksa ValueError (sessizce yok sayılmaz)."""
        raw: Optional[str] = self.get_state(LEARNING_FAILURE_KEY)
        if raw is None:
            return None
        value: object = json.loads(raw)
        if not isinstance(value, dict) or not all(isinstance(value.get(key), str) for key in ("at", "error_type",
                                                                                             "reason")):
            raise ValueError("companion.db öğrenme hatası kaydı bozuk.")
        return {"at": str(value["at"]), "error_type": str(value["error_type"]), "reason": str(value["reason"])}

    # --- Deniz'in hafıza araç çağrıları ---

    def record_chat_tool_call(self, message_id: int, call: ChatToolCall) -> None:
        """Hafıza çağrısını, sonucunu kullanan yanıtın ilk balonuna bağlar (geçmişte gerçek çağrı olarak görünür)."""
        with self.connection:
            self.connection.execute(
                "INSERT INTO chat_tool_calls(message_id, name, arguments, result) VALUES (?, ?, ?, ?)",
                (message_id, call["name"], call["arguments"], call["result"]),
            )

    def chat_tool_calls(self, message_ids: List[int]) -> Dict[int, List[ChatToolCall]]:
        """Verilen balonlara bağlı hafıza çağrıları (balon kimliği → çağrılar, kayıt sırasıyla)."""
        if not message_ids:
            return {}
        marks: str = ",".join("?" * len(message_ids))
        rows = self.connection.execute(
            f"SELECT message_id, name, arguments, result FROM chat_tool_calls WHERE message_id IN ({marks}) "
            "ORDER BY id", message_ids,
        ).fetchall()
        calls: Dict[int, List[ChatToolCall]] = {}
        for row in rows:
            calls.setdefault(int(row["message_id"]), []).append(
                {"name": str(row["name"]), "arguments": str(row["arguments"]), "result": str(row["result"])})
        return calls

    def get_state(self, key: str) -> Optional[str]:
        row = self.connection.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def _put_state(self, key: str, value: str) -> None:
        # Açık bir işlemin içinde çağrılır; işlemi çağıran yönetir (with bloğu ya da BEGIN IMMEDIATE).
        self.connection.execute(
            "INSERT INTO state(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def set_state(self, key: str, value: str) -> None:
        with self.connection:
            self._put_state(key, value)

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


@contextmanager
def opened_store(path: Path) -> Iterator[PersonalStore]:
    """
    Kısa ömürlü bağlantı (Telegram, masaüstü, öğrenme hattı): açar, verir, her durumda kapatır. Bir await boyunca açık
    tutulmaz; olay döngüsünden çağıranlar bunu asyncio.to_thread içinde kullanır.
    """
    store = PersonalStore(path)
    try:
        yield store
    finally:
        store.close()
