"""iMessage kanalı ayarlarının doğrulanmış sözleşmesi ve kurulumun servise bıraktığı eşleştirme isteği.

Temel alanlar zorunludur, kodda varsayılan yoktur: kurulum kullanıcının onayladığı değerleri yazar, doğrulama
eksik veya geçersiz alanda açık hata verir (docs/superpowers/specs/2026-09-29-imessage-companion-design.md).
Döküm alanlarının bulunmadığı eski eşleşmelerde döküm açıkça kapalıdır (None); model varsayılmaz.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Collection, Dict, Optional, TypedDict

from omniagent.integrations.runtime import read_json, save_json

_CLOCK: re.Pattern[str] = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_EMAIL: re.Pattern[str] = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE: re.Pattern[str] = re.compile(r"^\+?\d{7,15}$")
_PHONE_SEPARATORS: re.Pattern[str] = re.compile(r"[\s\-().]")
PAIRING_CODE: re.Pattern[str] = re.compile(r"^\d{6}$")


class ImessageConfigError(ValueError):
    """iMessage ayarı eksik, geçersiz ya da kullanılamaz (eşleşme yok, profil hazır değil)."""


class QuietHours(TypedDict):
    start: str
    end: str


class HeartbeatMinutes(TypedDict):
    base: int
    jitter: int
    min: int
    max: int


class DraftSettings(TypedDict):
    """Eşleştirmeden önce bilinen alanlar; handle eşleştirmede eklenir."""

    persona_name: str
    chat_backend: str
    memory_backend: str
    transcribe_backend: Optional[str]
    transcribe_model: Optional[str]
    quiet_hours: QuietHours
    burst_quiet_seconds: float
    gui_idle_seconds: int
    heartbeat_minutes: HeartbeatMinutes


class ImessageSettings(DraftSettings):
    handle: str


class PairingRequest(TypedDict):
    code: str
    expires_at: str
    draft: DraftSettings


def normalize_handle(handle: str) -> str:
    """
    iMessage adresini karşılaştırılabilir biçime indirir: e-posta küçük harfe, telefon ayraçsız rakamlara
    (varsa baştaki '+' korunur). Tanınmayan biçim ImessageConfigError'dır. Saf.
    """
    text: str = handle.strip()
    if "@" in text:
        folded: str = text.casefold()
        if not _EMAIL.match(folded):
            raise ImessageConfigError(f"Tanınmayan iMessage e-posta adresi: {handle!r}")
        return folded
    digits: str = _PHONE_SEPARATORS.sub("", text)
    if not _PHONE.match(digits):
        raise ImessageConfigError(f"Tanınmayan iMessage adresi: {handle!r}")
    return digits


def _mapping(raw: object, field: str) -> Dict[str, object]:
    if not isinstance(raw, dict):
        raise ImessageConfigError(f"iMessage ayarı '{field}' nesne olmalı.")
    return raw


def _text(raw: Dict[str, object], key: str) -> str:
    value: object = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ImessageConfigError(f"iMessage ayarı '{key}' boş olmayan metin olmalı.")
    return value.strip()


def _integer(raw: Dict[str, object], key: str, minimum: int, maximum: int) -> int:
    value: object = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ImessageConfigError(f"iMessage ayarı '{key}' {minimum}-{maximum} arası tam sayı olmalı.")
    return value


def _seconds(raw: Dict[str, object], key: str, minimum: float, maximum: float) -> float:
    value: object = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not minimum <= float(value) <= maximum:
        raise ImessageConfigError(f"iMessage ayarı '{key}' {minimum}-{maximum} arası sayı olmalı.")
    return float(value)


def _backend(raw: Dict[str, object], key: str, backends: Collection[str]) -> str:
    name: str = _text(raw, key)
    if name not in backends:
        raise ImessageConfigError(
            f"iMessage ayarı '{key}' bilinmeyen model profili: {name}. Profiller: {', '.join(sorted(backends))}"
        )
    return name


def _quiet_hours(raw: object) -> QuietHours:
    hours: Dict[str, object] = _mapping(raw, "quiet_hours")
    start: str = _text(hours, "start")
    end: str = _text(hours, "end")
    if not _CLOCK.match(start) or not _CLOCK.match(end):
        raise ImessageConfigError("iMessage ayarı 'quiet_hours' SS:DD biçiminde başlangıç ve bitiş ister.")
    return {"start": start, "end": end}


def _heartbeat(raw: object) -> HeartbeatMinutes:
    minutes: Dict[str, object] = _mapping(raw, "heartbeat_minutes")
    lower: int = _integer(minutes, "min", 5, 1440)
    upper: int = _integer(minutes, "max", lower, 1440)
    base: int = _integer(minutes, "base", lower, upper)
    return {"base": base, "jitter": _integer(minutes, "jitter", 0, 120), "min": lower, "max": upper}


def parse_draft(raw: object, backends: Collection[str]) -> DraftSettings:
    """Eşleştirme öncesi alanları doğrular ve normalize eder; geçersizde ImessageConfigError. Saf."""
    settings: Dict[str, object] = _mapping(raw, "imessage")
    transcribe_backend: Optional[str] = None
    transcribe_model: Optional[str] = None
    if settings.get("transcribe_backend") is not None:
        transcribe_backend = _backend(settings, "transcribe_backend", backends)
        transcribe_model = _text(settings, "transcribe_model")
    elif settings.get("transcribe_model") is not None:
        raise ImessageConfigError("iMessage ayarı 'transcribe_model' için 'transcribe_backend' de seçilmeli.")
    return {
        "persona_name": _text(settings, "persona_name"),
        "chat_backend": _backend(settings, "chat_backend", backends),
        "memory_backend": _backend(settings, "memory_backend", backends),
        "transcribe_backend": transcribe_backend,
        "transcribe_model": transcribe_model,
        "quiet_hours": _quiet_hours(settings.get("quiet_hours")),
        "burst_quiet_seconds": _seconds(settings, "burst_quiet_seconds", 0.5, 10.0),
        "gui_idle_seconds": _integer(settings, "gui_idle_seconds", 30, 3600),
        "heartbeat_minutes": _heartbeat(settings.get("heartbeat_minutes")),
    }


def parse_settings(raw: object, backends: Collection[str]) -> ImessageSettings:
    """Tam ayarları (handle dahil) doğrular. Saf."""
    draft: DraftSettings = parse_draft(raw, backends)
    handle: str = normalize_handle(_text(_mapping(raw, "imessage"), "handle"))
    return {
        "persona_name": draft["persona_name"],
        "chat_backend": draft["chat_backend"],
        "memory_backend": draft["memory_backend"],
        "transcribe_backend": draft["transcribe_backend"],
        "transcribe_model": draft["transcribe_model"],
        "quiet_hours": draft["quiet_hours"],
        "burst_quiet_seconds": draft["burst_quiet_seconds"],
        "gui_idle_seconds": draft["gui_idle_seconds"],
        "heartbeat_minutes": draft["heartbeat_minutes"],
        "handle": handle,
    }


def load_settings(path: Path, backends: Collection[str]) -> ImessageSettings:
    """Eşleşmiş ayarları okur; dosya yoksa kurulumu işaret eden ImessageConfigError."""
    if not path.exists():
        raise ImessageConfigError(f"iMessage eşleşmesi yok: {path}. Önce 'omniagent-imessage setup' çalıştırın.")
    return parse_settings(read_json(path, None), backends)


def memory_backend_if_paired(path: Path, backends: Collection[str]) -> Optional[str]:
    """
    Kanıtlı hafıza öğrenme hattının modeli (memory_backend). iMessage kurulmamışsa (dosya yok) None döner ve Telegram
    köprüsünde öğrenme kapalıdır; kodda varsayılan model yok. Geçersiz ayar ImessageConfigError verir.
    """
    if not path.exists():
        return None
    return load_settings(path, backends)["memory_backend"]


def save_settings(path: Path, settings: ImessageSettings) -> None:
    """Ayarları atomik ve 0600 izinle yazar."""
    save_json(path, settings)


def parse_pairing(raw: object, backends: Collection[str]) -> PairingRequest:
    """Eşleştirme isteğini doğrular: 6 haneli kod, saat dilimli son geçerlilik, geçerli taslak. Saf."""
    request: Dict[str, object] = _mapping(raw, "imessage-pairing")
    code: str = _text(request, "code")
    if not PAIRING_CODE.match(code):
        raise ImessageConfigError("Eşleştirme kodu 6 haneli olmalı.")
    expires_at: str = _text(request, "expires_at")
    try:
        moment: datetime = datetime.fromisoformat(expires_at)
    except ValueError as error:
        raise ImessageConfigError(f"Eşleştirme son geçerlilik zamanı geçersiz: {expires_at!r}") from error
    if moment.tzinfo is None:
        raise ImessageConfigError("Eşleştirme son geçerlilik zamanı saat dilimi içermeli.")
    return {"code": code, "expires_at": expires_at, "draft": parse_draft(request.get("draft"), backends)}


def load_pairing(path: Path, backends: Collection[str]) -> Optional[PairingRequest]:
    """Bekleyen eşleştirme isteği; yoksa None."""
    if not path.exists():
        return None
    return parse_pairing(read_json(path, None), backends)


def save_pairing(path: Path, request: PairingRequest) -> None:
    """Eşleştirme isteğini atomik ve 0600 izinle yazar."""
    save_json(path, request)


def pairing_expired(request: PairingRequest, now: datetime) -> bool:
    """Eşleştirme kodunun süresi doldu mu? Saf."""
    return now >= datetime.fromisoformat(request["expires_at"])
