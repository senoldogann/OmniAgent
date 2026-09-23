"""Kullanıcıya ait kalıcı tercih, yol ve karar hafızası.

Bu depoda yalnızca kullanıcı tarafından açıkça istenen kararlı bilgiler saklanır. Kayıtlar
her görevde otomatik olarak model istemine enjekte edilmez; model yalnızca `user_memory`
aracı ile gerektiğinde bunları okur. Parola, token, API anahtarı ve benzeri gizli
bilgiler reddedilir.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple, TypedDict, cast


MAX_PREFERENCES: int = 50
MAX_KEY_LENGTH: int = 80
MAX_VALUE_LENGTH: int = 600
MAX_QUERY_LENGTH: int = 120
MAX_TIMESTAMP_LENGTH: int = 64
CATEGORIES: Tuple[str, ...] = ("preference", "path", "decision")

_SENSITIVE_TERMS: Tuple[str, ...] = (
    "password", "passcode", "secret", "token", "api key", "api-key", "apikey",
    "credential", "private key", "oauth", "şifre", "parola", "gizli", "kimlik bilgisi",
    "erişim belirteci", "anahtar",
)
_SECRET_VALUE_PATTERN = re.compile(
    r"(?i)(?:sk|pk|ghp|gho|github_pat|xox[baprs])[-_][A-Za-z0-9_-]{16,}"
)


class PreferenceRecord(TypedDict):
    """Bir kullanıcı hafızası kaydının doğrulanmış JSON biçimi."""

    key: str
    value: str
    category: str
    created_at: str
    updated_at: str


class MemoryState(TypedDict):
    """Kalıcı kullanıcı hafızasının sınırlı durumu."""

    preferences: List[PreferenceRecord]


def empty_state() -> MemoryState:
    """Boş hafıza durumu döndürür."""
    return {"preferences": []}


def utc_timestamp() -> str:
    """Kayıt zaman damgasını üretir."""
    return datetime.now(timezone.utc).isoformat()


def _text(value: object, field: str, limit: int) -> str:
    """Metin alanını güvenli biçimde normalize eder."""
    if not isinstance(value, str):
        raise ValueError(f"{field} metin olmalı.")
    normalized: str = value.strip()
    if not normalized:
        raise ValueError(f"{field} boş olamaz.")
    if len(normalized) > limit:
        raise ValueError(f"{field} en fazla {limit} karakter olabilir.")
    if any(ord(character) < 32 for character in normalized):
        raise ValueError(f"{field} kontrol karakteri içeremez.")
    return normalized


def _safe_memory_text(key: str, value: str) -> None:
    """Kimlik bilgisi benzeri değerlerin kalıcılaştırılmasını engeller."""
    combined: str = f"{key} {value}".casefold()
    if any(term in combined for term in _SENSITIVE_TERMS) or _SECRET_VALUE_PATTERN.search(value):
        raise ValueError("Parola, token, API anahtarı veya kimlik bilgisi kalıcı hafızaya yazılamaz.")


def _category(value: object) -> str:
    """Geçerli hafıza kategorisini normalize eder."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("category belirtilmelidir.")
    normalized: str = value.strip().casefold()
    if normalized not in CATEGORIES:
        raise ValueError(f"Geçersiz hafıza kategorisi: {normalized}. İzin verilenler: {', '.join(CATEGORIES)}")
    return normalized


def _record(value: object, index: int) -> PreferenceRecord:
    """JSON girdisini doğrulanmış kayda çevirir."""
    if not isinstance(value, dict):
        raise ValueError(f"preferences[{index}] nesne olmalı.")
    key: str = _text(value.get("key"), "key", MAX_KEY_LENGTH)
    stored_value: str = _text(value.get("value"), "value", MAX_VALUE_LENGTH)
    _safe_memory_text(key, stored_value)
    created_at: str = _text(value.get("created_at"), "created_at", MAX_TIMESTAMP_LENGTH)
    updated_at: str = _text(value.get("updated_at"), "updated_at", MAX_TIMESTAMP_LENGTH)
    return {
        "key": key,
        "value": stored_value,
        "category": _category(value.get("category")),
        "created_at": created_at,
        "updated_at": updated_at,
    }


def load_memory(memory_file: str) -> MemoryState:
    """Hafıza dosyasını yükler; bozuk veya geçersiz kayıtları sessizce silmez."""
    path: Path = Path(memory_file)
    if not path.exists():
        return empty_state()
    with path.open("r", encoding="utf-8") as source:
        loaded: object = json.load(source)
    if not isinstance(loaded, dict) or not isinstance(loaded.get("preferences"), list):
        raise ValueError(f"Hafıza dosyasında geçersiz yapı: {path} ('preferences' listesi bekleniyor)")
    if len(loaded["preferences"]) > MAX_PREFERENCES:
        raise ValueError(f"Hafıza dosyası en fazla {MAX_PREFERENCES} kayıt içerebilir: {path}")
    records: List[PreferenceRecord] = [
        _record(item, index) for index, item in enumerate(loaded["preferences"])
    ]
    return {"preferences": records}


def save_memory(memory_file: str, state: MemoryState) -> None:
    """Hafızayı atomik biçimde ve 0600 izinleriyle yazar."""
    path: Path = Path(memory_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", delete=False,
        ) as target:
            temporary_path = Path(target.name)
            json.dump(state, target, ensure_ascii=False, separators=(",", ":"))
            target.flush()
            os.fsync(target.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def remember_preference(
    state: MemoryState, key: str, value: str, category: str, timestamp: str,
) -> MemoryState:
    """Aynı anahtarı günceller ve yeni durum döndürür; girdi durumunu değiştirmez."""
    normalized_key: str = _text(key, "key", MAX_KEY_LENGTH)
    normalized_value: str = _text(value, "value", MAX_VALUE_LENGTH)
    _safe_memory_text(normalized_key, normalized_value)
    normalized_category: str = _category(category)
    normalized_timestamp: str = _text(timestamp, "timestamp", MAX_TIMESTAMP_LENGTH)
    key_folded: str = normalized_key.casefold()
    updated: List[PreferenceRecord] = []
    previous: Optional[PreferenceRecord] = None
    for record in state["preferences"]:
        if record["key"].casefold() == key_folded:
            previous = record
        else:
            updated.append(cast(PreferenceRecord, dict(record)))
    record: PreferenceRecord = {
        "key": normalized_key,
        "value": normalized_value,
        "category": normalized_category,
        "created_at": previous["created_at"] if previous is not None else normalized_timestamp,
        "updated_at": normalized_timestamp,
    }
    result: MemoryState = {"preferences": (updated + [record])[-MAX_PREFERENCES:]}
    return result


def forget_preference(state: MemoryState, key: str) -> Tuple[MemoryState, bool]:
    """Anahtarı büyük/küçük harf duyarsız siler ve yeni durum ile silme sonucunu döndürür."""
    normalized_key: str = _text(key, "key", MAX_KEY_LENGTH)
    key_folded: str = normalized_key.casefold()
    remaining: List[PreferenceRecord] = [
        cast(PreferenceRecord, dict(record)) for record in state["preferences"] if record["key"].casefold() != key_folded
    ]
    return {"preferences": remaining}, len(remaining) != len(state["preferences"])


def search_preferences(state: MemoryState, query: str = "") -> List[PreferenceRecord]:
    """Anahtar/değer metninde güvenli arama yapar; en yeni kayıtları önce döndürür."""
    normalized_query: str = query.strip() if isinstance(query, str) else ""
    if len(normalized_query) > MAX_QUERY_LENGTH:
        raise ValueError(f"query en fazla {MAX_QUERY_LENGTH} karakter olabilir.")
    query_folded: str = normalized_query.casefold()
    matches: List[PreferenceRecord] = []
    for record in reversed(state["preferences"]):
        haystack: str = f"{record['key']} {record['value']} {record['category']}".casefold()
        if not query_folded or query_folded in haystack:
            matches.append(cast(PreferenceRecord, dict(record)))
    return matches
