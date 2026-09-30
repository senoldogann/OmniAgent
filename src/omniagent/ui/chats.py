"""Masaüstü sohbetlerini kullanıcı veri klasöründe atomik olarak saklar."""
from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, NotRequired, Optional, Sequence, Tuple, TypedDict

from omniagent.core.conversation import Exchange
from omniagent.integrations.runtime import save_json
from omniagent.paths import data_root


class TranscriptSpan(TypedDict):
    text: str
    tags: List[str]


# Kalıcı transkripte YAZILMAYAN geçici Tk durumu. Seçim ve canlı bölge kimlikleri yalnız etiket
# olarak atılır (metin kalır); akış imleci, takılı 'akıyor' işareti ve dönen araç glifi ise
# etiketiyle birlikte metni de atılır: yüklenince kalıcı imleç/işaret gibi görünmesinler.
TRANSIENT_TAGS: frozenset[str] = frozenset({"sel"})
TRANSIENT_TEXT_TAGS: frozenset[str] = frozenset({"cursor", "bullet_streaming", "tool_spin"})
_REGION_TAG: re.Pattern[str] = re.compile(r"r\d+")
# Bir sohbette biten son görevin sonucu (kenar çubuğu durum noktası için). Eski kayıtlarda alan yoktur.
OUTCOME_DONE: str = "done"
OUTCOME_FAILED: str = "failed"
OUTCOME_STOPPED: str = "stopped"
CHAT_OUTCOMES: frozenset[str] = frozenset({OUTCOME_DONE, OUTCOME_FAILED, OUTCOME_STOPPED})


class ChatSummary(TypedDict):
    id: str
    title: str
    updated_at: str
    # Son biten görevin sonucu; görev başlayınca silinir, bitince yazılır (eski dizinlerde bulunmaz).
    last_outcome: NotRequired[str]


class ChatRecord(TypedDict):
    id: str
    title: str
    created_at: str
    updated_at: str
    history: List[Exchange]
    spans: List[TranscriptSpan]
    # Kullanıcı mesajlarının gönderim zamanları (ISO 8601), transkriptte görünme sırasıyla. Kullanıcı
    # mesajının altındaki saat satırı bundan kurulur; eski kayıtlarda alan yoktur (boş liste okunur).
    sends: List[str]
    last_outcome: NotRequired[str]


def chats_dir() -> Path:
    """Sohbetlerin kalıcı dizinini döndürür."""
    return data_root() / "desktop_chats"


def known_outcome(value: object) -> Optional[str]:
    """Diskten okunan değer bilinen bir görev sonucuysa onu, aksi halde (liste/sözlük dahil) None döndürür. Saf."""
    return value if isinstance(value, str) and value in CHAT_OUTCOMES else None


def parse_updated_at(chat_id: str, value: str) -> datetime:
    """
    Sohbetin zaman damgasını (ISO 8601) çözer. Tarihe göre gruplama buna dayanır: bozuk değer sessizce
    gizlenmez, hangi sohbetin bozuk olduğunu söyleyen açık bir ValueError verir. Saf.
    """
    try:
        return datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"Sohbet zaman damgası okunamadı: {chat_id}") from error


def summary_of(record: ChatRecord) -> ChatSummary:
    """Sohbet kaydının dizin satırını (varsa son görev sonucuyla birlikte) çıkarır. Saf."""
    summary: ChatSummary = {"id": record["id"], "title": record["title"], "updated_at": record["updated_at"]}
    if "last_outcome" in record:
        summary["last_outcome"] = record["last_outcome"]
    return summary


def with_outcome(summary: ChatSummary, outcome: Optional[str]) -> ChatSummary:
    """Dizin satırının son görev sonucu değiştirilmiş kopyası; None sonucu siler. Saf."""
    if outcome is not None and outcome not in CHAT_OUTCOMES:
        raise ValueError(f"Bilinmeyen görev sonucu: {outcome}")
    kept: ChatSummary = {"id": summary["id"], "title": summary["title"], "updated_at": summary["updated_at"]}
    if outcome is not None:
        kept["last_outcome"] = outcome
    return kept


def _valid_id(chat_id: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{32}", chat_id))


def _record_path(root: Path, chat_id: str) -> Path:
    if not _valid_id(chat_id):
        raise ValueError("Geçersiz sohbet kimliği")
    return root / f"{chat_id}.json"


def new_chat(goal: str) -> ChatRecord:
    """İlk hedefin kısa başlığıyla yeni sohbet kaydı oluşturur."""
    now: str = datetime.now(timezone.utc).isoformat()
    title: str = " ".join(goal.strip().split())[:64] or "Yeni sohbet"
    return {"id": uuid.uuid4().hex, "title": title, "created_at": now,
            "updated_at": now, "history": [], "spans": [], "sends": []}


def load_catalog(root: Optional[Path] = None) -> Tuple[List[ChatSummary], Optional[str]]:
    """Sıralı sohbet listesini ve son açık sohbeti doğrulayarak okur."""
    path: Path = (root or chats_dir()) / "index.json"
    if not path.exists():
        return [], None
    raw: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("chats"), list):
        raise ValueError("Sohbet dizini biçimi geçersiz")
    chats: List[ChatSummary] = []
    for item in raw["chats"]:
        if (isinstance(item, dict) and isinstance(item.get("id"), str)
                and _valid_id(item["id"]) and isinstance(item.get("title"), str)
                and isinstance(item.get("updated_at"), str)):
            parse_updated_at(item["id"], item["updated_at"])
            summary: ChatSummary = {"id": item["id"], "title": item["title"], "updated_at": item["updated_at"]}
            outcome: Optional[str] = known_outcome(item.get("last_outcome"))
            if outcome is not None:
                summary["last_outcome"] = outcome
            chats.append(summary)
    active: object = raw.get("active_id")
    active_id: Optional[str] = active if isinstance(active, str) and any(item["id"] == active for item in chats) else None
    return chats, active_id


def save_catalog(chats: Sequence[ChatSummary], active_id: Optional[str], root: Optional[Path] = None) -> None:
    """Listeyi ve seçimi atomik kaydeder."""
    save_json((root or chats_dir()) / "index.json", {"version": 1, "active_id": active_id, "chats": list(chats)})


def load_chat(chat_id: str, root: Optional[Path] = None) -> ChatRecord:
    """Bir sohbetin transkriptini ve model bağlamını doğrulayarak yükler."""
    raw: object = json.loads(_record_path(root or chats_dir(), chat_id).read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("id") != chat_id:
        raise ValueError("Sohbet kaydı biçimi geçersiz")
    history: List[Exchange] = []
    raw_history: object = raw.get("history", [])
    if not isinstance(raw_history, list):
        raise ValueError("Sohbet bağlamı biçimi geçersiz")
    for entry in raw_history:
        if (isinstance(entry, dict) and isinstance(entry.get("goal"), str)
                and isinstance(entry.get("answer"), str) and isinstance(entry.get("tools"), list)):
            history.append({"goal": entry["goal"], "answer": entry["answer"],
                            "tools": [item for item in entry["tools"] if isinstance(item, str)]})
    spans: List[TranscriptSpan] = []
    raw_spans: object = raw.get("spans", [])
    if not isinstance(raw_spans, list):
        raise ValueError("Sohbet transkripti biçimi geçersiz")
    for item in raw_spans:
        if isinstance(item, dict) and isinstance(item.get("text"), str) and isinstance(item.get("tags"), list):
            spans.append({"text": item["text"], "tags": [tag for tag in item["tags"] if isinstance(tag, str)]})
    raw_sends: object = raw.get("sends", [])
    if not isinstance(raw_sends, list):
        raise ValueError("Sohbet gönderim zamanları biçimi geçersiz")
    sends: List[str] = [stamp for stamp in raw_sends if isinstance(stamp, str) and stamp]
    # Eski kayıtlarda kalmış geçici Tk durumu (seçim, akış imleci, takılı işaretler) yüklerken
    # temizlenir; dosya bir sonraki kayıtta düzelir.
    record: ChatRecord = {
        "id": chat_id, "title": str(raw.get("title", "Yeni sohbet")),
        "created_at": str(raw.get("created_at", "")), "updated_at": str(raw.get("updated_at", "")),
        "history": history, "spans": sanitize_spans(spans), "sends": sends,
    }
    outcome: Optional[str] = known_outcome(raw.get("last_outcome"))
    if outcome is not None:
        record["last_outcome"] = outcome
    return record


def save_chat(record: ChatRecord, root: Optional[Path] = None) -> None:
    """Sohbet içeriğini tek dosyaya atomik yazar."""
    save_json(_record_path(root or chats_dir(), record["id"]), record)


def rename_chat(chat_id: str, title: str, chats: Sequence[ChatSummary],
                active_id: Optional[str], root: Optional[Path] = None) -> List[ChatSummary]:
    """Başlığı kayıt ve listede birlikte günceller; liste hatasında kaydı geri alır."""
    clean: str = " ".join(title.split())
    if not clean or len(clean) > 64:
        raise ValueError("Sohbet adı 1–64 karakter olmalı")
    if not any(item["id"] == chat_id for item in chats):
        raise ValueError("Sohbet listede bulunamadı")
    base: Path = root or chats_dir()
    record: ChatRecord = load_chat(chat_id, base)
    old: ChatRecord = dict(record)  # type: ignore[assignment]
    now: str = datetime.now(timezone.utc).isoformat()
    record["title"] = clean
    record["updated_at"] = now
    updated: List[ChatSummary] = [
        {**item, "title": clean, "updated_at": now} if item["id"] == chat_id else dict(item)
        for item in chats
    ]
    save_chat(record, base)
    try:
        save_catalog(updated, active_id, base)
    except OSError:
        save_chat(old, base)
        raise
    return updated


def delete_chat(chat_id: str, chats: Sequence[ChatSummary],
                active_id: Optional[str], root: Optional[Path] = None) -> Tuple[List[ChatSummary], Optional[str]]:
    """Sohbeti geri alınabilir çöp dizinine taşır ve listeyi atomik günceller."""
    if not any(item["id"] == chat_id for item in chats):
        raise ValueError("Sohbet listede bulunamadı")
    base: Path = root or chats_dir()
    source: Path = _record_path(base, chat_id)
    trash: Path = base / "trash" / source.name
    trash.parent.mkdir(parents=True, exist_ok=True)
    if trash.exists():
        raise FileExistsError("Bu sohbetin çöp kaydı zaten var")
    remaining: List[ChatSummary] = [dict(item) for item in chats if item["id"] != chat_id]
    next_active: Optional[str] = (remaining[0]["id"] if remaining else None) if active_id == chat_id else active_id
    os.replace(source, trash)
    try:
        save_catalog(remaining, next_active, base)
    except OSError:
        os.replace(trash, source)
        raise
    return remaining, next_active


def delete_chats(chat_ids: Sequence[str], chats: Sequence[ChatSummary],
                 active_id: Optional[str], root: Optional[Path] = None) -> Tuple[List[ChatSummary], Optional[str]]:
    """Seçili sohbetleri tek katalog güncellemesiyle geri alınabilir biçimde taşır."""
    selected = list(dict.fromkeys(chat_ids))
    known = {item["id"] for item in chats}
    if not selected or any(chat_id not in known for chat_id in selected):
        raise ValueError("Seçilen sohbetler listede bulunamadı")
    base = root or chats_dir()
    moves = [(_record_path(base, chat_id), _record_path(base / "trash", chat_id)) for chat_id in selected]
    if any(not source.exists() or trash.exists() for source, trash in moves):
        raise FileExistsError("Sohbet kaydı veya çöp dosyası çakışıyor")
    (base / "trash").mkdir(parents=True, exist_ok=True)
    remaining = [dict(item) for item in chats if item["id"] not in selected]
    next_active = (remaining[0]["id"] if remaining else None) if active_id in selected else active_id
    moved: list[tuple[Path, Path]] = []
    try:
        for source, trash in moves:
            os.replace(source, trash)
            moved.append((source, trash))
        save_catalog(remaining, next_active, base)
    except OSError:
        for source, trash in reversed(moved):
            os.replace(trash, source)
        raise
    return remaining, next_active


def restore_chat(chat_id: str, chats: Sequence[ChatSummary],
                 active_id: Optional[str], root: Optional[Path] = None) -> List[ChatSummary]:
    """Son silinen sohbeti eski içeriğiyle geri getirir."""
    base: Path = root or chats_dir()
    source: Path = _record_path(base / "trash", chat_id)
    target: Path = _record_path(base, chat_id)
    if target.exists() or any(item["id"] == chat_id for item in chats):
        raise FileExistsError("Sohbet zaten mevcut")
    os.replace(source, target)
    try:
        record: ChatRecord = load_chat(chat_id, base)
        parse_updated_at(chat_id, record["updated_at"])  # bozuk zaman damgası kataloğa girmesin (geri alma yolu)
        updated: List[ChatSummary] = [summary_of(record), *[dict(item) for item in chats]]
        save_catalog(updated, active_id, base)
    except (OSError, ValueError, json.JSONDecodeError):
        os.replace(target, source)
        raise
    return updated


def restore_chats(chat_ids: Sequence[str], chats: Sequence[ChatSummary],
                  active_id: Optional[str], root: Optional[Path] = None) -> List[ChatSummary]:
    """Son toplu silmenin tüm kayıtlarını tek katalog güncellemesiyle geri yükler."""
    selected = list(dict.fromkeys(chat_ids))
    if not selected:
        return list(chats)
    base = root or chats_dir()
    moves = [(_record_path(base / "trash", chat_id), _record_path(base, chat_id)) for chat_id in selected]
    if any(not source.exists() or target.exists() for source, target in moves):
        raise FileExistsError("Geri yüklenecek sohbet bulunamadı veya zaten mevcut")
    moved: list[tuple[Path, Path]] = []
    try:
        for source, target in moves:
            os.replace(source, target)
            moved.append((source, target))
        restored = [load_chat(chat_id, base) for chat_id in selected]
        for record in restored:
            parse_updated_at(record["id"], record["updated_at"])  # bozuk zaman damgası kataloğa girmesin
        updated: List[ChatSummary] = [summary_of(record) for record in restored] + [dict(item) for item in chats]
        save_catalog(updated, active_id, base)
    except (OSError, ValueError, json.JSONDecodeError):
        for source, target in reversed(moved):
            os.replace(target, source)
        raise
    return updated


def sanitize_spans(spans: Sequence[TranscriptSpan]) -> List[TranscriptSpan]:
    """
    Geçici Tk durumunu parçalardan çıkarır ve komşu, aynı etiketli parçaları birleştirir:
    seçim ('sel') ve canlı bölge kimlikleri ('rN') etiketten; akış imleci, takılı 'akıyor'
    işareti ve dönen araç glifi etiketiyle birlikte metninden atılır. Girdiyi değiştirmez;
    hem kayıtta hem yüklemede kullanılır (idempotent). Saf.
    """
    cleaned: List[TranscriptSpan] = []
    for span in spans:
        if TRANSIENT_TEXT_TAGS.intersection(span["tags"]):
            continue
        tags: List[str] = [
            tag for tag in span["tags"] if tag not in TRANSIENT_TAGS and not _REGION_TAG.fullmatch(tag)
        ]
        if cleaned and cleaned[-1]["tags"] == tags:
            cleaned[-1] = {"text": cleaned[-1]["text"] + span["text"], "tags": tags}
        else:
            cleaned.append({"text": span["text"], "tags": tags})
    return cleaned


def spans_from_dump(items: Sequence[Tuple[str, str, str]]) -> List[TranscriptSpan]:
    """Tk metin dökümünü kalıcı parçalara çevirir; geçici Tk durumu sanitize_spans ile atılır."""
    active: List[str] = []
    raw: List[TranscriptSpan] = []
    for kind, value, _index in items:
        if kind == "tagon":
            if value not in active:
                active.append(value)
        elif kind == "tagoff":
            if value in active:
                active.remove(value)
        elif kind == "text" and value:
            raw.append({"text": value, "tags": list(active)})
    return sanitize_spans(raw)
