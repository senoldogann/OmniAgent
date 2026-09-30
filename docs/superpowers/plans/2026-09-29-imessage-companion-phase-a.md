# iMessage Yol Arkadaşı — Faz A (kanal, sohbet, iş devri) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Kullanıcı iPhone'undan ajanın Apple ID'sine iMessage yazınca, OmniAgent bir yakın arkadaş gibi kısa balonlarla hızlı cevap verir ve bilgisayarda iş gerekiyorsa işi arka planda mevcut ajana devredip sonucu anlatır.

**Architecture:** `imsg rpc` alt süreci (JSON-RPC, stdio) mesajları getirir; `ImessageBridge` eşleşmiş tek kişinin mesajlarını SQLite arşive (imleçle aynı işlemde) yazar, burst'leri birleştirip hızlı sohbet katmanına (`companion/chat.py`, tek model çağrısı, akışta balon) verir; iş gerekirse `companion/delegate.py` mevcut `run_agent_with_callback`'i `host_task_lock` altında koşturur. Onaylar iMessage'dan evet/hayır ile alınır; `unattended` hiç verilmez.

**Tech Stack:** Python 3.11 (uv), asyncio, sqlite3 (WAL), openai AsyncOpenAI (mevcut profiller), `imsg` CLI (`brew install steipete/tap/imsg`), launchd, pytest + pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-09-29-imessage-companion-design.md` (bu plan yalnız Faz A'yı uygular; Faz B hafıza ve Faz C kalp atışı/otonomi ayrı planlardır).

## Global Constraints

- Test komutu `uv run python -m pytest tests/ -q`; her görevin sonunda tam paket yeşil kalır.
- Yorum ve docstring'ler Türkçe; tanımlayıcılar İngilizce (repo deseni). Fonksiyonel öncelik; sınıf yalnız dış sistem bağlayıcıları için (`ImsgClient`, `ImsgSession`, `PersonalStore`, `ImessageBridge`).
- Katı tipler (TypedDict, `Optional`); yeni üretim kodunda `Any` yok ve varsayılan parametre değeri yok.
- Hatalar açıkça yükselir; loglar `extra={...}` yapılandırılmış alanlarla yazılır; mesaj içeriği loglanmaz (yalnız kimlik/sayı).
- Yeni bağımlılık yok (sqlite3, asyncio stdlib; `openai`, `pyobjc` zaten var).
- iMessage koşularında `RunOptions["unattended"]` hiçbir zaman verilmez.
- imsg `send`: `service: "imessage"`, `allow_sms_fallback: false`; `watch.subscribe`: `debounce_ms: 500`; `-32001` (sonuç bilinmiyor) asla yeniden gönderilmez.
- Spec değerleri: burst sessizliği 2.0 sn; teslim doğrulama penceresi 60 sn; yanıtsız burst yaşı 1 saat; kullanıcı işi ara bilgisi 5 dk; sohbet geçmişi 40 mesaj; 1–4 balon, balon ≤ 600 karakter; balon aralığı `min(1.5, 0.4 + 0.02 × karakter)` sn; imsg yeniden başlatma 3 deneme (1, 2, 4 sn); eşleştirme kodu 6 hane, 180 sn; sessiz saatler 23:30–09:00; `gui_idle_seconds` 180; kalp atışı 30/10/20/240 dk.
- Zaman damgaları mikrosaniyeli ISO 8601 UTC metnidir (`memory/personal.py` `utc_iso`); sözlük sırası = zaman sırası.
- **Commit yok.** Çalışma ağacında kullanıcının commit edilmemiş, ilgisiz değişiklikleri var (bu planın da dokunduğu `app/agent.py`, `app/tool_schema.py`, `tools/facade.py` dahil); görev commit'i bunları karıştırır. Her görev bir "değişiklik denetimi" adımıyla biter; commit/push kararı kullanıcıdadır. `git add -A`, `git stash`, `git checkout -- <dosya>` yasak; ilgisiz hunk'lar geri alınmaz.
- `AGENTS.md`'ye dokunulmaz.

## Review Focus

1. Yalnız ek (fotoğraf) taşıyan mesaj: metin boş ya da U+FFFC → boş sohbet turu açılmamalı; arşive `[fotoğraf]`, sohbete `[fotoğraf: ad]` olarak gitmeli (Görev 4 `message_text` testi, Görev 9 köprü testi).
2. Sohbet turu akarken gelen yeni mesaj kaybolmamalı ve iç içe geçmemeli; bir sonraki burst olmalı (Görev 9 testi).
3. Aynı telefonun farklı yazımı (`+90 555 111 22 33` ile `+905551112233`) aynı kişi sayılmalı (Görev 4 testi).
4. Onay beklerken iş durdurulur ya da biterse, sonradan gelen "evet" çökme yaratmamalı, normal sohbete gitmeli (Görev 9 testi).
5. `Z` ve `+03:00` ofsetli imsg zaman damgaları: yanıtsız burst yaşı doğru hesaplanmalı (Görev 2 testi).

## Dosya haritası

| Dosya | Sorumluluk |
| --- | --- |
| `src/omniagent/paths.py` (değişir) | yeni veri yolları |
| `src/omniagent/approval.py` (değişir) | yeni dosyalar `PROTECTED_DATA_FILES`'a |
| `src/omniagent/integrations/imessage_settings.py` (yeni) | `imessage.json` / `imessage-pairing.json` sözleşmesi, handle normalizasyonu |
| `src/omniagent/memory/personal.py` (yeni) | `PersonalStore`: arşiv, imleç, teslim durumu, etkinlik, durum |
| `src/omniagent/integrations/imsg.py` (yeni) | `ImsgClient`: tek `imsg rpc` süreci, JSON-RPC |
| `src/omniagent/integrations/runtime.py` (değişir) | ortak `boolean_field` |
| `src/omniagent/integrations/imessage_rules.py` (yeni) | saf köprü kuralları: süzgeç, yansıma, komut, evet/hayır, soru balonları |
| `src/omniagent/companion/__init__.py`, `bubbles.py`, `persona.py`, `chat.py`, `delegate.py` (yeni) | kanaldan bağımsız beyin |
| `src/omniagent/app/agent.py` (değişir) | `call_model_with_retries` genel adı |
| `src/omniagent/platform/macos/launch_agent.py` (yeni) | Telegram'dan çıkarılan ortak launchd kodu |
| `src/omniagent/integrations/telegram.py` (değişir) | `launch_agent` ve `boolean_field` kullanımı |
| `src/omniagent/integrations/imessage.py` (yeni) | `ImessageBridge`, `ImsgSession`, `run_bridge`, CLI |
| `src/omniagent/integrations/imessage_setup.py` (yeni) | kurulum, model ölçümü, servis, servis içinde eşleştirme |
| `src/omniagent/platform/macos/permissions.py` (değişir) | Tam Disk Erişimi satırı |
| `pyproject.toml` (değişir) | `omniagent-imessage` betiği |
| `app/tool_schema.py`, `config.py`, `tools/facade.py` (değişir) | "Telegram" metinlerinin kanal-genel hâli |
| `docs/IMESSAGE.md` (yeni) | kurulum kılavuzu + Faz A canlı kontrol listesi |
| `tests/fixtures/fake_imsg.py` (yeni) | stdio JSON-RPC konuşan sahte `imsg rpc` |

---

### Task 1: Veri yolları, ayar sözleşmesi, korumalı dosyalar

**Files:**
- Modify: `src/omniagent/paths.py` (`telegram_settings_file` fonksiyonunun hemen ardından)
- Create: `src/omniagent/integrations/imessage_settings.py`
- Modify: `src/omniagent/approval.py` (`PROTECTED_DATA_FILES`, ~satır 663)
- Test: `tests/test_imessage_settings.py`

**Interfaces:**
- Consumes: `omniagent.integrations.runtime.read_json(path, default)`, `save_json(path, value)` (atomik, 0600).
- Produces:
  - `paths.imessage_settings_file() -> Path`, `imessage_pairing_file() -> Path`, `companion_db_file() -> Path`, `persona_file() -> Path`, `imessage_history_file() -> Path`
  - `ImessageConfigError(ValueError)`; TypedDict'ler `QuietHours`, `HeartbeatMinutes`, `DraftSettings`, `ImessageSettings(DraftSettings)` (+`handle`), `PairingRequest` (`code`, `expires_at`, `draft`)
  - `normalize_handle(handle: str) -> str`, `parse_draft(raw: object, backends: Collection[str]) -> DraftSettings`, `parse_settings(raw: object, backends: Collection[str]) -> ImessageSettings`, `load_settings(path: Path, backends: Collection[str]) -> ImessageSettings`, `save_settings(path: Path, settings: ImessageSettings) -> None`, `parse_pairing(raw: object, backends: Collection[str]) -> PairingRequest`, `load_pairing(path: Path, backends: Collection[str]) -> Optional[PairingRequest]`, `save_pairing(path: Path, request: PairingRequest) -> None`, `pairing_expired(request: PairingRequest, now: datetime) -> bool`

- [ ] **Step 1: Write the failing test**

`tests/test_imessage_settings.py`:

```python
"""iMessage ayar sözleşmesi: zorunlu alanlar, handle normalizasyonu, eşleştirme süresi, korumalı dosyalar."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict

import pytest

from omniagent import approval
from omniagent.integrations.imessage_settings import (
    ImessageConfigError, load_settings, normalize_handle, pairing_expired, parse_pairing, parse_settings,
    save_settings,
)

BACKENDS = ("openai", "opencode")


def valid_settings() -> Dict[str, object]:
    return {
        "handle": "+90 555 111-22-33",
        "persona_name": "Deniz",
        "chat_backend": "openai",
        "memory_backend": "opencode",
        "quiet_hours": {"start": "23:30", "end": "09:00"},
        "burst_quiet_seconds": 2.0,
        "gui_idle_seconds": 180,
        "heartbeat_minutes": {"base": 30, "jitter": 10, "min": 20, "max": 240},
    }


def test_settings_round_trip_normalizes_handle(tmp_path: Path) -> None:
    settings = parse_settings(valid_settings(), BACKENDS)
    assert settings["handle"] == "+905551112233"
    path = tmp_path / "imessage.json"
    save_settings(path, settings)
    assert load_settings(path, BACKENDS) == settings
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("key, value, expected", [
    ("chat_backend", "yok-boyle-profil", "chat_backend"),
    ("burst_quiet_seconds", 0, "burst_quiet_seconds"),
    ("quiet_hours", {"start": "25:00", "end": "09:00"}, "quiet_hours"),
    ("heartbeat_minutes", {"base": 10, "jitter": 10, "min": 20, "max": 240}, "base"),
    ("persona_name", "  ", "persona_name"),
])
def test_invalid_field_is_rejected(key: str, value: object, expected: str) -> None:
    raw = valid_settings()
    raw[key] = value
    with pytest.raises(ImessageConfigError, match=expected):
        parse_settings(raw, BACKENDS)


def test_missing_settings_points_to_setup(tmp_path: Path) -> None:
    with pytest.raises(ImessageConfigError, match="omniagent-imessage setup"):
        load_settings(tmp_path / "imessage.json", BACKENDS)


def test_handle_normalization() -> None:
    assert normalize_handle("  Dogan@iCloud.com ") == "dogan@icloud.com"
    assert normalize_handle("+90 (555) 111-22-33") == "+905551112233"
    with pytest.raises(ImessageConfigError):
        normalize_handle("urn:biz:1234")


def test_pairing_expiry() -> None:
    now = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
    draft = {key: value for key, value in valid_settings().items() if key != "handle"}
    raw: Dict[str, object] = {"code": "042917", "expires_at": (now + timedelta(seconds=180)).isoformat(), "draft": draft}
    request = parse_pairing(raw, BACKENDS)
    assert not pairing_expired(request, now)
    assert pairing_expired(request, now + timedelta(seconds=180))
    with pytest.raises(ImessageConfigError, match="6 haneli"):
        parse_pairing({**raw, "code": "12ab"}, BACKENDS)


def test_imessage_files_are_protected_from_agent_tools() -> None:
    for name in ("imessage.json", "imessage-pairing.json", "persona.md", "imessage-history.json",
                 "companion.db", "companion.db-wal", "companion.db-shm"):
        assert name in approval.PROTECTED_DATA_FILES
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_imessage_settings.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'omniagent.integrations.imessage_settings'`

- [ ] **Step 3: Add the data paths**

`src/omniagent/paths.py` içinde `telegram_settings_file()` fonksiyonunun hemen ardına ekle:

```python
def imessage_settings_file() -> Path:
    """Eşleşmiş iMessage kanalının ayarları; varlığı köprünün kurulup eşleştiğini gösterir."""
    return data_root() / "imessage.json"


def imessage_pairing_file() -> Path:
    """Kurulumun servise bıraktığı tek kullanımlık eşleştirme isteği (kod, son geçerlilik, taslak ayarlar)."""
    return data_root() / "imessage-pairing.json"


def companion_db_file() -> Path:
    """iMessage yol arkadaşının SQLite deposu: mesaj arşivi, etkinlik günlüğü, durum."""
    return data_root() / "companion.db"


def persona_file() -> Path:
    """Kullanıcının düzenleyebildiği karakter tanımı (isim, kişilik, konuşma tarzı)."""
    return data_root() / "persona.md"


def imessage_history_file() -> Path:
    """iMessage'dan devredilen son görevlerin sohbet kayıtları (Exchange listesi)."""
    return data_root() / "imessage-history.json"
```

- [ ] **Step 4: Write the settings module**

`src/omniagent/integrations/imessage_settings.py`:

```python
"""iMessage kanalı ayarlarının doğrulanmış sözleşmesi ve kurulumun servise bıraktığı eşleştirme isteği.

Tüm alanlar zorunludur, kodda varsayılan yoktur: kurulum kullanıcının onayladığı değerleri yazar, doğrulama
eksik veya geçersiz alanda açık hata verir (docs/superpowers/specs/2026-09-29-imessage-companion-design.md).
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
    return {
        "persona_name": _text(settings, "persona_name"),
        "chat_backend": _backend(settings, "chat_backend", backends),
        "memory_backend": _backend(settings, "memory_backend", backends),
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
```

- [ ] **Step 5: Protect the new data files**

`src/omniagent/approval.py` içindeki `PROTECTED_DATA_FILES` tanımını şununla değiştir (mevcut adlar aynen kalır):

```python
PROTECTED_DATA_FILES: frozenset[str] = frozenset({
    "provider_fallback.json", "audit.jsonl", "telegram.json", "continuous_limits.json",
    "user_memory.json", "schedules.json", "catalog.json", "experience_memory.json", "cognitive_memory.json",
    # iMessage kanalı: eşleşme, eşleştirme isteği, karakter, görev geçmişi ve yol arkadaşı deposu
    "imessage.json", "imessage-pairing.json", "persona.md", "imessage-history.json",
    "companion.db", "companion.db-wal", "companion.db-shm",
})
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_imessage_settings.py tests/test_approval.py -v`
Expected: PASS (tümü)

- [ ] **Step 7: Değişiklik denetimi (commit yok)**

Run: `git --no-pager diff --stat -- src/omniagent/paths.py src/omniagent/approval.py && git status --short src/omniagent/integrations/imessage_settings.py tests/test_imessage_settings.py`
Expected: yalnız bu görevin dosyaları; `approval.py`'de yalnız `PROTECTED_DATA_FILES` değişmiş.

---

### Task 2: `PersonalStore` — arşiv, imleç, teslim durumu, etkinlik, durum

**Files:**
- Create: `src/omniagent/memory/personal.py`
- Test: `tests/test_personal_store.py`

**Interfaces:**
- Consumes: yok (yalnız stdlib).
- Produces:
  - `utc_iso(moment: datetime) -> str`, `to_utc_iso(value: str) -> str`
  - TypedDict `ArchivedMessage` (`id: int`, `direction: str`, `kind: str`, `text: str`, `created_at: str`, `delivery: Optional[str]`), `ActivityRecord` (`kind`, `origin`, `goal`, `rationale`, `outcome`, `success: bool`, `started_at`, `finished_at`, `tokens: int`)
  - `class PersonalStore(path: Path)`: `close()`, `cursor() -> int`, `advance_cursor(imsg_rowid: int) -> None`, `record_incoming(imsg_rowid: int, guid: str, text: str, created_at: str) -> Optional[int]`, `record_outgoing(text: str, kind: str, created_at: str) -> int`, `record_outgoing_file(name: str, created_at: str) -> int`, `confirm_outgoing(text: str, imsg_rowid: int, guid: str, now: datetime, window_seconds: float) -> Optional[int]`, `mark_unconfirmed(message_id: int) -> None`, `expire_pending(now: datetime, window_seconds: float) -> List[int]`, `recent_messages(limit: int) -> List[ArchivedMessage]`, `unanswered_burst(now: datetime, max_age_seconds: float) -> List[ArchivedMessage]`, `record_activity(record: ActivityRecord) -> int`, `activities_since(since: datetime) -> List[ActivityRecord]`, `get_state(key: str) -> Optional[str]`, `set_state(key: str, value: str) -> None`, `record_latency(latency_ms: float) -> None`, `latency_p50() -> Optional[float]`
  - `kind` değerleri: `chat`, `proactive`, `task_report`, `question`, `file`

- [ ] **Step 1: Write the failing test**

`tests/test_personal_store.py`:

```python
"""companion.db: arşiv+imleç işlemi, yeniden oynatma, teslim doğrulaması, yanıtsız burst (gerçek SQLite)."""
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_personal_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'omniagent.memory.personal'`

- [ ] **Step 3: Write the store**

`src/omniagent/memory/personal.py`:

```python
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
from typing import List, Optional, Tuple, TypedDict

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
                "INSERT OR IGNORE INTO messages(imsg_rowid, guid, direction, kind, text, created_at, delivery) "
                "VALUES (?, ?, 'in', 'chat', ?, ?, NULL)",
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_personal_store.py -v`
Expected: PASS (5 test)

- [ ] **Step 5: Değişiklik denetimi (commit yok)**

Run: `git status --short src/omniagent/memory/personal.py tests/test_personal_store.py`
Expected: iki yeni dosya (`??`).

---

### Task 3: `ImsgClient` ve sahte `imsg rpc`

**Files:**
- Create: `src/omniagent/integrations/imsg.py`
- Create: `tests/fixtures/fake_imsg.py`
- Test: `tests/test_imsg_client.py`

**Interfaces:**
- Consumes: yok.
- Produces:
  - Hatalar: `ImsgError(Exception)`, `ImsgUnavailable(ImsgError)`, `ImsgProcessError(ImsgError)`, `DeliveryUnknown(ImsgError)`, `ImsgRpcError(ImsgError)` (`.method: str`, `.code: int`)
  - TypedDict: `Attachment` (`path`, `mime_type`), `IncomingMessage` (`rowid: int`, `guid: str`, `chat_id: int`, `sender: str`, `participants: List[str]`, `is_from_me: bool`, `is_group: bool`, `text: str`, `created_at: str`, `attachments: List[Attachment]`), `SendResult` (`ok: bool`, `rowid: Optional[int]`, `guid: Optional[str]`)
  - Sabit: `OBJECT_REPLACEMENT = "￼"`
  - `imsg_command() -> List[str]`, `parse_message(raw: object) -> IncomingMessage`
  - `class ImsgClient(command: List[str])`: `async start() -> Dict[str, object]`, `async close() -> None`, `async request(method: str, params: Dict[str, object]) -> Dict[str, object]`, `async catch_up(since_rowid: int) -> Tuple[List[IncomingMessage], int]`, `subscribe(since_rowid: int) -> AsyncIterator[IncomingMessage]`, `async send_text(handle: str, text: str) -> SendResult`, `async send_file(handle: str, path: Path) -> SendResult`

- [ ] **Step 1: Write the fake imsg fixture**

`tests/fixtures/fake_imsg.py`:

```python
"""Testler için sahte `imsg rpc`: senaryo dosyasına göre JSON-RPC yanıtları ve bildirimleri üretir.

Kullanım: python fake_imsg.py <senaryo.json>. Gelen her istek senaryodaki log_path dosyasına bir satır
olarak eklenir; testler istemcinin ne gönderdiğini buradan doğrular. Listeler istek sırasıyla tüketilir;
beklenmeyen istek fazlalığı IndexError ile süreci düşürür (test yüksek sesle başarısız olur).
"""
import json
import sys
from pathlib import Path


def emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> None:
    scenario: dict = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    log = Path(scenario["log_path"])
    after_pages: list = list(scenario.get("after_pages", []))
    send_errors: list = list(scenario.get("send_errors", []))
    batches: list = list(scenario.get("subscribe_batches", []))
    exit_after = scenario.get("exit_after_requests")
    handled = 0
    subscription = 0
    while True:
        line = sys.stdin.readline()
        if not line:
            return
        request: dict = json.loads(line)
        with log.open("a", encoding="utf-8") as target:
            target.write(json.dumps(request, ensure_ascii=False) + "\n")
        handled += 1
        if exit_after is not None and handled > exit_after:
            sys.exit(3)
        method = request["method"]
        if method == "initialize":
            emit({"jsonrpc": "2.0", "id": request["id"], "result": scenario["status"]})
        elif method == "messages.after":
            emit({"jsonrpc": "2.0", "id": request["id"], "result": after_pages.pop(0)})
        elif method == "watch.subscribe":
            subscription += 1
            emit({"jsonrpc": "2.0", "id": request["id"], "result": {"subscription": subscription, "buffer_limit": 256}})
            for note in (batches.pop(0) if batches else []):
                emit({"jsonrpc": "2.0", "method": note["method"],
                      "params": {"subscription": subscription, **note["params"]}})
        elif method == "send":
            error = send_errors.pop(0) if send_errors else None
            if error is None:
                emit({"jsonrpc": "2.0", "id": request["id"],
                      "result": {"ok": True, "id": 900 + handled, "guid": f"sent-{handled}"}})
            else:
                emit({"jsonrpc": "2.0", "id": request["id"], "error": {"code": error, "message": "sahte hata"}})
        else:
            emit({"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32601, "message": "bilinmeyen yöntem"}})


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Write the failing test**

`tests/test_imsg_client.py`:

```python
"""ImsgClient: sahte `imsg rpc` alt süreciyle gerçek JSON-RPC yolu (başlatma, sayfalama, taşma, gönderim, çökme)."""
import json
import sys
from contextlib import aclosing
from pathlib import Path
from typing import Dict, List

import pytest

from omniagent.integrations.imsg import (
    DeliveryUnknown, ImsgClient, ImsgProcessError, ImsgUnavailable, parse_message,
)

FAKE = Path(__file__).parent / "fixtures" / "fake_imsg.py"
USER = "+905551112233"


def raw_message(rowid: int, text: str) -> Dict[str, object]:
    return {"id": rowid, "guid": f"g{rowid}", "chat_id": 7, "sender": USER, "participants": [USER],
            "is_group": False, "is_from_me": False, "text": text, "created_at": "2026-09-29T12:00:00Z",
            "attachments": []}


def client_for(tmp_path: Path, scenario: Dict[str, object]) -> ImsgClient:
    path = tmp_path / "scenario.json"
    base: Dict[str, object] = {"log_path": str(tmp_path / "requests.jsonl"), "status": {"database": {"ready": True}}}
    path.write_text(json.dumps({**base, **scenario}), encoding="utf-8")
    return ImsgClient([sys.executable, str(FAKE), str(path)])


def requests_sent(tmp_path: Path) -> List[Dict[str, object]]:
    lines = (tmp_path / "requests.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


@pytest.mark.asyncio
async def test_start_rejects_unreadable_database(tmp_path: Path) -> None:
    client = client_for(tmp_path, {"status": {"database": {"ready": False, "error": "authorization denied"}}})
    try:
        with pytest.raises(ImsgUnavailable, match="Full Disk Access"):
            await client.start()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_catch_up_pages_until_done(tmp_path: Path) -> None:
    client = client_for(tmp_path, {"after_pages": [
        {"messages": [raw_message(11, "a")], "next_rowid": 11, "has_more": True},
        {"messages": [raw_message(12, "b")], "next_rowid": 20, "has_more": False},
    ]})
    await client.start()
    try:
        messages, cursor = await client.catch_up(10)
    finally:
        await client.close()
    assert [message["text"] for message in messages] == ["a", "b"] and cursor == 20
    pages = [request for request in requests_sent(tmp_path) if request["method"] == "messages.after"]
    assert [request["params"]["since_rowid"] for request in pages] == [10, 11]


@pytest.mark.asyncio
async def test_subscribe_recovers_from_overflow(tmp_path: Path) -> None:
    client = client_for(tmp_path, {
        "subscribe_batches": [
            [{"method": "message", "params": {"message": raw_message(31, "ilk")}},
             {"method": "watch.overflow", "params": {"resume_after_rowid": 31, "reason": "buffer_limit_exceeded",
                                                     "terminal": True}}],
            [{"method": "message", "params": {"message": raw_message(33, "canlı")}}],
        ],
        "after_pages": [{"messages": [raw_message(32, "kaçan")], "next_rowid": 32, "has_more": False}],
    })
    await client.start()
    received: List[str] = []
    try:
        async with aclosing(client.subscribe(30)) as stream:
            async for message in stream:
                received.append(message["text"])
                if len(received) == 3:
                    break
    finally:
        await client.close()
    assert received == ["ilk", "kaçan", "canlı"]
    calls = [(request["method"], request["params"].get("since_rowid")) for request in requests_sent(tmp_path)]
    assert calls == [("initialize", None), ("watch.subscribe", 30), ("messages.after", 31), ("watch.subscribe", 32)]


@pytest.mark.asyncio
async def test_send_errors_are_never_retried(tmp_path: Path) -> None:
    client = client_for(tmp_path, {"send_errors": [None, -32001, -32004]})
    await client.start()
    try:
        result = await client.send_text(USER, "selam")
        assert result["ok"] and result["guid"] == "sent-2"
        with pytest.raises(DeliveryUnknown):
            await client.send_text(USER, "ikinci")
        with pytest.raises(DeliveryUnknown):
            await client.send_text(USER, "üçüncü")
        with pytest.raises(ImsgProcessError):
            await client.send_text(USER, "dördüncü")
    finally:
        await client.close()
    sends = [request["params"] for request in requests_sent(tmp_path) if request["method"] == "send"]
    assert [params["text"] for params in sends] == ["selam", "ikinci", "üçüncü"]
    assert all(params["service"] == "imessage" and params["allow_sms_fallback"] is False for params in sends)


@pytest.mark.asyncio
async def test_process_exit_fails_pending_and_later_requests(tmp_path: Path) -> None:
    client = client_for(tmp_path, {"exit_after_requests": 1})
    await client.start()
    try:
        with pytest.raises(ImsgProcessError, match="çıkış kodu 3"):
            await client.send_text(USER, "selam")
        with pytest.raises(ImsgProcessError):
            await client.catch_up(0)
    finally:
        await client.close()


def test_parse_message_keeps_disk_attachments_only() -> None:
    message = parse_message({
        "id": 5, "guid": "g5", "chat_id": 1, "is_from_me": False, "created_at": "2026-09-29T12:00:00Z",
        "attachments": [
            {"original_path": "/a.heic", "converted_path": "/a.jpg", "mime_type": "image/heic",
             "converted_mime_type": "image/jpeg"},
            {"original_path": "/b.png", "mime_type": "image/png", "missing": True},
        ],
    })
    assert message["sender"] == "" and message["text"] == "" and message["is_group"] is False
    assert message["attachments"] == [{"path": "/a.jpg", "mime_type": "image/jpeg"}]
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_imsg_client.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'omniagent.integrations.imsg'`

- [ ] **Step 4: Write the client**

`src/omniagent/integrations/imsg.py`:

```python
"""imsg (openclaw/imsg) JSON-RPC istemcisi: iMessage alma ve gönderme.

`imsg rpc` alt süreci satır başına bir JSON-RPC 2.0 nesnesi konuşur (openclaw/imsg docs/rpc.md). Bu modül tek
bir süreç ömrünü yönetir; çöken süreci yeniden başlatmak köprünün işidir (integrations/imessage.py
ImsgSession). Gönderimin sonucu bilinmiyorsa (-32001) yeniden gönderilmez: çift mesaj, kayıp mesajdan kötüdür.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
from pathlib import Path
from typing import AsyncIterator, Dict, List, Optional, Tuple, TypedDict

PROTOCOL_VERSION: int = 1
DEBOUNCE_MS: int = 500
PAGE_LIMIT: int = 500
REQUEST_TIMEOUT_SECONDS: float = 60.0
STOP_TIMEOUT_SECONDS: float = 5.0
STREAM_LIMIT_BYTES: int = 16 * 1024 * 1024
STDERR_TAIL_CHARS: int = 2000
# launchd PATH'i Homebrew dizinlerini içermez; imsg bu dizinlerde de aranır.
IMSG_SEARCH_DIRS: Tuple[str, ...] = ("/opt/homebrew/bin", "/usr/local/bin")
# Yalnız ek taşıyan mesajın metni U+FFFC (nesne yer tutucusu) olabilir.
OBJECT_REPLACEMENT: str = "￼"
DELIVERY_UNKNOWN_CODE: int = -32001
DATABASE_UNAVAILABLE_CODE: int = -32002
LANE_BLOCKED_CODE: int = -32004


class ImsgError(Exception):
    """imsg ile ilgili hataların tabanı."""


class ImsgUnavailable(ImsgError):
    """imsg bulunamadı ya da Messages veritabanı okunamıyor (Full Disk Access)."""


class ImsgProcessError(ImsgError):
    """imsg rpc süreci başlatılamadı ya da beklenmedik biçimde kapandı."""


class DeliveryUnknown(ImsgError):
    """Gönderimin sonucu bilinmiyor; çift mesaj riski yüzünden yeniden gönderilmez."""


class ImsgRpcError(ImsgError):
    """imsg'nin döndürdüğü JSON-RPC hatası (kod, yöntem ve kısaltılmış parametrelerle)."""

    def __init__(self, method: str, code: int, message: str, params: Dict[str, object]) -> None:
        shown: str = json.dumps({key: str(value)[:80] for key, value in params.items()}, ensure_ascii=False)
        super().__init__(f"imsg {method} hatası {code}: {message[:300]} (parametreler: {shown})")
        self.method: str = method
        self.code: int = code


class Attachment(TypedDict):
    path: str
    mime_type: str


class IncomingMessage(TypedDict):
    rowid: int
    guid: str
    chat_id: int
    sender: str
    participants: List[str]
    is_from_me: bool
    is_group: bool
    text: str
    created_at: str
    attachments: List[Attachment]


class SendResult(TypedDict):
    ok: bool
    rowid: Optional[int]
    guid: Optional[str]


def imsg_command() -> List[str]:
    """`imsg rpc` komutu; imsg yoksa kurulum talimatıyla ImsgUnavailable."""
    search: str = os.pathsep.join([os.environ.get("PATH", ""), *IMSG_SEARCH_DIRS])
    found: Optional[str] = shutil.which("imsg", path=search)
    if found is None:
        raise ImsgUnavailable("imsg bulunamadı. Kurulum: brew install steipete/tap/imsg")
    return [found, "rpc"]


def _attachment(raw: object) -> Optional[Attachment]:
    """imsg ek nesnesini yol + MIME'e indirir; diskte olmayan ek None. Saf."""
    if not isinstance(raw, dict) or raw.get("missing") is True:
        return None
    path: object = raw.get("converted_path") or raw.get("original_path")
    mime: object = raw.get("converted_mime_type") or raw.get("mime_type")
    if not isinstance(path, str) or not path:
        return None
    return {"path": path, "mime_type": mime if isinstance(mime, str) else ""}


def parse_message(raw: object) -> IncomingMessage:
    """
    imsg mesaj nesnesini doğrular. imsg sözleşmesinde uygulanmayan alanlar gönderilmez (ör. kendi gönderimimizde
    sender); bunlar boş değer sayılır. Zorunlu alan eksikse ImsgError. Saf.
    """
    if not isinstance(raw, dict):
        raise ImsgError(f"imsg mesajı nesne değil: {type(raw).__name__}")
    rowid, guid, chat_id = raw.get("id"), raw.get("guid"), raw.get("chat_id")
    created_at, is_from_me = raw.get("created_at"), raw.get("is_from_me")
    if (not isinstance(rowid, int) or not isinstance(guid, str) or not isinstance(chat_id, int)
            or not isinstance(created_at, str) or not isinstance(is_from_me, bool)):
        raise ImsgError(f"imsg mesajında zorunlu alan eksik: {sorted(raw)}")
    sender: object = raw.get("sender")
    text: object = raw.get("text")
    participants: object = raw.get("participants")
    attachments: object = raw.get("attachments")
    parsed: List[Attachment] = []
    if isinstance(attachments, list):
        parsed = [item for item in (_attachment(entry) for entry in attachments) if item is not None]
    return {
        "rowid": rowid, "guid": guid, "chat_id": chat_id,
        "sender": sender if isinstance(sender, str) else "",
        "participants": [item for item in participants if isinstance(item, str)] if isinstance(participants, list) else [],
        "is_from_me": is_from_me,
        "is_group": raw.get("is_group") is True,
        "text": text if isinstance(text, str) else "",
        "created_at": created_at,
        "attachments": parsed,
    }


_PendingRequest = Tuple[str, Dict[str, object], "asyncio.Future[Dict[str, object]]"]


class ImsgClient:
    """Tek bir `imsg rpc` alt sürecine bağlı JSON-RPC istemcisi (dış sistem bağlayıcısı)."""

    def __init__(self, command: List[str]) -> None:
        self.command: List[str] = command
        self.process: Optional[asyncio.subprocess.Process] = None
        self.reader: Optional[asyncio.Task[None]] = None
        self.stderr_reader: Optional[asyncio.Task[None]] = None
        self.pending: Dict[int, _PendingRequest] = {}
        self.next_id: int = 1
        self.notifications: asyncio.Queue[Optional[Dict[str, object]]] = asyncio.Queue()
        self.closed: Optional[ImsgProcessError] = None
        self.stderr_tail: str = ""

    async def start(self) -> Dict[str, object]:
        """Süreci başlatır ve el sıkışır; Messages veritabanı okunamıyorsa ImsgUnavailable."""
        try:
            self.process = await asyncio.create_subprocess_exec(
                *self.command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, limit=STREAM_LIMIT_BYTES,
            )
        except OSError as error:
            raise ImsgProcessError(f"imsg başlatılamadı ({self.command[0]}): {error}") from error
        self.reader = asyncio.create_task(self._read_stdout())
        self.stderr_reader = asyncio.create_task(self._read_stderr())
        status: Dict[str, object] = await self.request("initialize", {"protocol_version": PROTOCOL_VERSION})
        database: object = status.get("database")
        if not isinstance(database, dict) or database.get("ready") is not True:
            detail: object = database.get("error") if isinstance(database, dict) else database
            raise ImsgUnavailable(
                f"Messages veritabanı okunamıyor ({detail}). Full Disk Access şu ikiliye verilmeli: "
                f"{os.path.realpath(sys.executable)}"
            )
        return status

    def _running(self) -> asyncio.subprocess.Process:
        if self.process is None:
            raise ImsgProcessError("imsg başlatılmadı.")
        return self.process

    async def _read_stdout(self) -> None:
        process: asyncio.subprocess.Process = self._running()
        if process.stdout is None:
            raise ImsgProcessError("imsg stdout bağlanmadı.")
        reason: str = "imsg istemcisi kapatıldı."
        try:
            while True:
                line: bytes = await process.stdout.readline()
                if not line:
                    code: int = await process.wait()
                    reason = f"imsg rpc süreci kapandı (çıkış kodu {code}; stderr: {self.stderr_tail[-300:]})"
                    return
                self._dispatch(line)
        finally:
            failure = ImsgProcessError(reason)
            self.closed = failure
            for _method, _params, future in self.pending.values():
                if not future.done():
                    future.set_exception(failure)
            self.pending.clear()
            self.notifications.put_nowait(None)

    async def _read_stderr(self) -> None:
        process: asyncio.subprocess.Process = self._running()
        if process.stderr is None:
            return
        while True:
            chunk: bytes = await process.stderr.read(4096)
            if not chunk:
                return
            self.stderr_tail = (self.stderr_tail + chunk.decode("utf-8", errors="replace"))[-STDERR_TAIL_CHARS:]

    def _dispatch(self, line: bytes) -> None:
        try:
            payload: object = json.loads(line)
        except json.JSONDecodeError as error:
            logging.warning("imsg geçersiz JSON satırı atlandı", extra={"error": str(error)[:200]})
            return
        if not isinstance(payload, dict):
            logging.warning("imsg nesne olmayan satır atlandı", extra={"type": type(payload).__name__})
            return
        if "method" in payload and "id" not in payload:
            self.notifications.put_nowait(payload)
            return
        request_id: object = payload.get("id")
        entry: Optional[_PendingRequest] = self.pending.pop(request_id, None) if isinstance(request_id, int) else None
        if entry is None:
            logging.warning("imsg beklenmeyen yanıt kimliği", extra={"id": str(request_id)[:40]})
            return
        method, params, future = entry
        if future.done():
            return
        error: object = payload.get("error")
        if isinstance(error, dict):
            code: object = error.get("code")
            message: object = error.get("message")
            rpc_error = ImsgRpcError(method, code if isinstance(code, int) else 0,
                                     message if isinstance(message, str) else "", params)
            if rpc_error.code == DATABASE_UNAVAILABLE_CODE:
                future.set_exception(ImsgUnavailable(f"{rpc_error}. Full Disk Access gerekli."))
            else:
                future.set_exception(rpc_error)
            return
        result: object = payload.get("result")
        future.set_result(result if isinstance(result, dict) else {"value": result})

    async def request(self, method: str, params: Dict[str, object]) -> Dict[str, object]:
        """Tek JSON-RPC isteği; süreç kapanmışsa ImsgProcessError, yanıt gelmezse TimeoutError."""
        if self.closed is not None:
            raise self.closed
        process: asyncio.subprocess.Process = self._running()
        if process.stdin is None:
            raise ImsgProcessError("imsg stdin bağlanmadı.")
        request_id: int = self.next_id
        self.next_id += 1
        future: asyncio.Future[Dict[str, object]] = asyncio.get_running_loop().create_future()
        self.pending[request_id] = (method, params, future)
        line: str = json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
                               ensure_ascii=False)
        try:
            process.stdin.write(line.encode("utf-8") + b"\n")
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as error:
            self.pending.pop(request_id, None)
            raise ImsgProcessError(f"imsg'ye yazılamadı ({method}): {error}") from error
        try:
            return await asyncio.wait_for(future, timeout=REQUEST_TIMEOUT_SECONDS)
        finally:
            self.pending.pop(request_id, None)

    async def catch_up(self, since_rowid: int) -> Tuple[List[IncomingMessage], int]:
        """since_rowid'den sonraki mesajları sayfalayarak getirir; yetkili imleç imsg'nin next_rowid'idir."""
        messages: List[IncomingMessage] = []
        cursor: int = since_rowid
        while True:
            page: Dict[str, object] = await self.request(
                "messages.after", {"since_rowid": cursor, "limit": PAGE_LIMIT, "attachments": True},
            )
            items, next_rowid, has_more = page.get("messages"), page.get("next_rowid"), page.get("has_more")
            if not isinstance(items, list) or not isinstance(next_rowid, int) or not isinstance(has_more, bool):
                raise ImsgError(f"messages.after beklenmeyen yanıt: {sorted(page)}")
            messages.extend(parse_message(item) for item in items)
            cursor = next_rowid
            if not has_more:
                return messages, cursor

    async def _subscribe(self, since_rowid: int) -> int:
        result: Dict[str, object] = await self.request(
            "watch.subscribe", {"since_rowid": since_rowid, "attachments": True, "debounce_ms": DEBOUNCE_MS},
        )
        subscription: object = result.get("subscription")
        if not isinstance(subscription, int):
            raise ImsgError(f"watch.subscribe abonelik kimliği döndürmedi: {sorted(result)}")
        return subscription

    async def subscribe(self, since_rowid: int) -> AsyncIterator[IncomingMessage]:
        """
        Yeni mesajları iter. Taşma (watch.overflow) sonrası kaçan satırlar messages.after ile getirilir ve abonelik
        yeniden kurulur (imsg: yineleme olabilir, atlama olmaz). Süreç kapanırsa ImsgProcessError.
        """
        subscription: int = await self._subscribe(since_rowid)
        while True:
            payload: Optional[Dict[str, object]] = await self.notifications.get()
            if payload is None:
                raise self.closed if self.closed is not None else ImsgProcessError("imsg bildirimleri kapandı.")
            params: object = payload.get("params")
            if not isinstance(params, dict) or params.get("subscription") != subscription:
                continue
            method: object = payload.get("method")
            if method == "message":
                yield parse_message(params.get("message"))
            elif method == "watch.overflow":
                resume: object = params.get("resume_after_rowid")
                if not isinstance(resume, int):
                    raise ImsgError("watch.overflow resume_after_rowid içermiyor.")
                logging.warning("imsg izleme tamponu taştı; kaçan mesajlar getiriliyor",
                                extra={"resume_after_rowid": resume})
                missed, cursor = await self.catch_up(resume)
                for message in missed:
                    yield message
                subscription = await self._subscribe(cursor)

    async def send_text(self, handle: str, text: str) -> SendResult:
        return await self._send({"to": handle, "text": text, "service": "imessage", "allow_sms_fallback": False})

    async def send_file(self, handle: str, path: Path) -> SendResult:
        return await self._send({"to": handle, "file": str(path), "service": "imessage", "allow_sms_fallback": False})

    async def _send(self, params: Dict[str, object]) -> SendResult:
        try:
            result: Dict[str, object] = await self.request("send", params)
        except ImsgRpcError as error:
            if error.code == DELIVERY_UNKNOWN_CODE:
                raise DeliveryUnknown(str(error)) from error
            if error.code == LANE_BLOCKED_CODE:
                # Mutasyon şeridi zehirlendi: yalnız yeni süreç temizler; köprü yeniden bağlanır.
                await self.close()
                raise DeliveryUnknown(str(error)) from error
            raise
        except TimeoutError as error:
            raise DeliveryUnknown(f"imsg send {REQUEST_TIMEOUT_SECONDS:.0f} sn içinde yanıt vermedi") from error
        if result.get("ok") is not True:
            raise ImsgError(f"imsg send başarısız yanıt: {sorted(result)}")
        rowid: object = result.get("id")
        guid: object = result.get("guid")
        return {"ok": True, "rowid": rowid if isinstance(rowid, int) else None,
                "guid": guid if isinstance(guid, str) else None}

    async def close(self) -> None:
        """stdin'i kapatır (imsg kabul edilenleri bitirip çıkar); süre dolarsa süreci öldürür."""
        process: Optional[asyncio.subprocess.Process] = self.process
        if process is None:
            return
        if process.returncode is None:
            if process.stdin is not None and not process.stdin.is_closing():
                process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), timeout=STOP_TIMEOUT_SECONDS)
            except TimeoutError:
                process.kill()
                await process.wait()
        for task in (self.reader, self.stderr_reader):
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_imsg_client.py -v`
Expected: PASS (6 test)

- [ ] **Step 6: Değişiklik denetimi (commit yok)**

Run: `git status --short src/omniagent/integrations/imsg.py tests/fixtures/fake_imsg.py tests/test_imsg_client.py`
Expected: üç yeni dosya.

---

### Task 4: Saf köprü kuralları ve balon yardımcıları

**Files:**
- Modify: `src/omniagent/integrations/runtime.py` (ortak `boolean_field`)
- Modify: `src/omniagent/integrations/telegram.py` (`_boolean_field` kaldırılır, `boolean_field` içe aktarılır; ~satır 42, 192-194, 808, 1038)
- Create: `src/omniagent/integrations/imessage_rules.py`
- Create: `src/omniagent/companion/__init__.py`, `src/omniagent/companion/bubbles.py`
- Test: `tests/test_imessage_rules.py`, `tests/test_companion_bubbles.py`

**Interfaces:**
- Consumes: `IncomingMessage`, `Attachment`, `OBJECT_REPLACEMENT` (Görev 3); `normalize_handle`, `ImessageConfigError` (Görev 1); `approval.approval_granted`; `core.text_norm.ascii_fold`.
- Produces:
  - `runtime.boolean_field(spec: object) -> bool`
  - `imessage_rules`: `accepted(message: IncomingMessage, handle: str) -> bool`, `own_echo(message: IncomingMessage, handle: str) -> bool`, `message_text(message: IncomingMessage) -> str`, `image_paths(message: IncomingMessage) -> List[str]`, `parse_command(text: str) -> Optional[str]` (`"stop"` | `"status"` | None), `answer_polarity(text: str) -> Optional[bool]`, `field_prompt(name: str, spec: object) -> str`, `question_bubbles(title: str, fields: Dict[str, object]) -> List[str]`, `status_lines(running_goal: Optional[str], progress: List[str], tasks_today: int, tokens_today: int, latency_p50_ms: Optional[float]) -> List[str]`
  - `bubbles`: `MAX_BUBBLES = 4`, `MAX_BUBBLE_CHARS = 600`, `clean_line(line: str) -> str`, `split_complete_lines(buffer: str, delta: str) -> Tuple[List[str], str]`, `bubbles_from_lines(lines: List[str]) -> List[str]`, `split_bubbles(text: str) -> List[str]`, `bubble_delay(text: str) -> float`

- [ ] **Step 1: Write the failing tests**

`tests/test_imessage_rules.py`:

```python
"""iMessage köprüsünün saf kuralları: kim kabul edilir, yansıma, komutlar, evet/hayır/sohbet ayrımı, soru balonları."""
from typing import List

from omniagent.integrations.imessage_rules import (
    accepted, answer_polarity, image_paths, message_text, own_echo, parse_command, question_bubbles,
)
from omniagent.integrations.imsg import Attachment, IncomingMessage

HANDLE = "+905551112233"


def message(sender: str, text: str, is_from_me: bool, is_group: bool,
            attachments: List[Attachment]) -> IncomingMessage:
    return {"rowid": 1, "guid": "g1", "chat_id": 7, "sender": sender, "participants": [HANDLE],
            "is_from_me": is_from_me, "is_group": is_group, "text": text, "created_at": "2026-09-29T12:00:00Z",
            "attachments": attachments}


def test_only_paired_direct_messages_are_accepted() -> None:
    assert accepted(message("+90 555 111 22 33", "selam", False, False, []), HANDLE)
    assert not accepted(message("+905559998877", "selam", False, False, []), HANDLE)
    assert not accepted(message(HANDLE, "selam", False, True, []), HANDLE)
    assert not accepted(message(HANDLE, "selam", True, False, []), HANDLE)
    assert not accepted(message("urn:biz:acme", "selam", False, False, []), HANDLE)


def test_own_echo_is_our_send_in_paired_chat() -> None:
    assert own_echo(message("", "naber", True, False, []), HANDLE)
    assert not own_echo(message("", "naber", True, True, []), HANDLE)
    assert not own_echo(message(HANDLE, "naber", False, False, []), HANDLE)


def test_attachment_only_message_has_no_text_but_keeps_image() -> None:
    photo = message(HANDLE, "￼", False, False, [{"path": "/tmp/p.jpg", "mime_type": "image/jpeg"},
                                                    {"path": "/tmp/a.pdf", "mime_type": "application/pdf"}])
    assert message_text(photo) == ""
    assert image_paths(photo) == ["/tmp/p.jpg"]


def test_commands_are_exact_words_only() -> None:
    assert parse_command("Dur") == "stop"
    assert parse_command("/stop") == "stop"
    assert parse_command("/durum") == "status"
    assert parse_command("dur bi saniye") is None


def test_answer_polarity_separates_yes_no_and_chat() -> None:
    assert answer_polarity("evet") is True
    assert answer_polarity("Olur!") is True
    assert answer_polarity("hayır") is False
    assert answer_polarity("yok.") is False
    assert answer_polarity("napıyorsun") is None


def test_approval_title_is_sent_verbatim() -> None:
    bubbles = question_bubbles("Ödeme: 149,90 TL Apple'a",
                               {"approved": {"type": "boolean"}, "_help": "İşlem: kart ile ödeme"})
    assert bubbles == ["bi onay lazım:", "Ödeme: 149,90 TL Apple'a", "İşlem: kart ile ödeme", "evet mi hayır mı?"]
```

`tests/test_companion_bubbles.py`:

```python
"""Model metnini balonlara çevirme: markdown temizliği, 4 balon sınırı, akış satırları, insan benzeri aralık."""
from omniagent.companion.bubbles import (
    MAX_BUBBLE_CHARS, bubble_delay, clean_line, split_bubbles, split_complete_lines,
)


def test_markdown_is_removed_from_bubbles() -> None:
    assert clean_line("- **tamam** bakıyorum") == "tamam bakıyorum"
    assert clean_line("## başlık") == "başlık"
    assert clean_line("1. `ls` çalıştırdım") == "ls çalıştırdım"


def test_more_than_four_lines_merge_into_last_bubble() -> None:
    assert split_bubbles("a\n\nb\nc\nd\ne") == ["a", "b", "c", "d e"]


def test_long_line_is_clipped() -> None:
    assert len(split_bubbles("x" * 1000)[0]) == MAX_BUBBLE_CHARS


def test_stream_lines_complete_only_at_newline() -> None:
    assert split_complete_lines("sel", "am\nna") == (["selam"], "na")
    assert split_complete_lines("na", "ber") == ([], "naber")


def test_bubble_delay_is_bounded() -> None:
    assert bubble_delay("") == 0.4
    assert bubble_delay("x" * 100) == 1.5
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_imessage_rules.py tests/test_companion_bubbles.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'omniagent.integrations.imessage_rules'` (ve `omniagent.companion`)

- [ ] **Step 3: Move `boolean_field` to the shared runtime**

`src/omniagent/integrations/runtime.py` içinde `save_json` fonksiyonunun ardına ekle:

```python
def boolean_field(spec: object) -> bool:
    """Soru alanı onay kutusu mu? Telegram ve iMessage bu alanı evet/hayır metniyle yanıtlatır. Saf."""
    return isinstance(spec, dict) and spec.get("type") == "boolean"
```

`src/omniagent/integrations/telegram.py`:
1. `from .runtime import DeliveryFailed, IntegrationStopped, data_root, read_json, save_json` satırını
   `from .runtime import DeliveryFailed, IntegrationStopped, boolean_field, data_root, read_json, save_json` yap.
2. `def _boolean_field(spec: object) -> bool:` fonksiyonunu (docstring ve `return` satırıyla birlikte, 3 satır) sil.
3. Kalan iki çağrıda `_boolean_field(` → `boolean_field(` (`answer()` ve `_reply_to_question()` içinde).
4. Doğrula: `rg -n "_boolean_field" src tests` → çıktı boş olmalı.

- [ ] **Step 4: Write the rules module**

`src/omniagent/integrations/imessage_rules.py`:

```python
"""iMessage köprüsünün saf kuralları: süzgeç, kendi yansımamız, komutlar, üç durumlu onay cevabı, soru balonları."""
from __future__ import annotations

from typing import Dict, List, Optional

from omniagent.approval import approval_granted
from omniagent.core.text_norm import ascii_fold
from omniagent.integrations.imessage_settings import ImessageConfigError, normalize_handle
from omniagent.integrations.imsg import OBJECT_REPLACEMENT, IncomingMessage
from omniagent.integrations.runtime import boolean_field

# Telegram'ın onay kelimelerine (approval_granted) ek olarak arkadaşça evetler.
_EXTRA_AFFIRMATIVE: frozenset[str] = frozenset({"olur", "yap", "tabi", "tabii", "hadi", "okey", "evet yap"})
_NEGATIVE: frozenset[str] = frozenset({
    "hayir", "h", "no", "n", "yok", "iptal", "vazgec", "olmaz", "istemiyorum", "reddet", "yapma", "false", "0",
})
_STOP_WORDS: frozenset[str] = frozenset({"dur", "/dur", "/stop"})
_STATUS_WORDS: frozenset[str] = frozenset({"/durum", "/status"})


def _same_handle(raw: str, handle: str) -> bool:
    try:
        return normalize_handle(raw) == handle
    except ImessageConfigError:
        # Telefon/e-posta olmayan gönderen (ör. işletme sohbeti) eşleşmiş kullanıcı olamaz.
        return False


def accepted(message: IncomingMessage, handle: str) -> bool:
    """Yalnız eşleşmiş kullanıcıdan gelen, grup olmayan, bizim göndermediğimiz mesaj. Saf."""
    return not message["is_from_me"] and not message["is_group"] and _same_handle(message["sender"], handle)


def own_echo(message: IncomingMessage, handle: str) -> bool:
    """Eşleşmiş birebir sohbette bizim gönderdiğimiz balonun izlemedeki yansıması mı? Saf."""
    return (message["is_from_me"] and not message["is_group"]
            and any(_same_handle(participant, handle) for participant in message["participants"]))


def message_text(message: IncomingMessage) -> str:
    """Ek yer tutucusu (U+FFFC) atılmış, kırpılmış metin. Saf."""
    return message["text"].replace(OBJECT_REPLACEMENT, "").strip()


def image_paths(message: IncomingMessage) -> List[str]:
    """Mesajdaki görsel eklerin diskteki yolları. Saf."""
    return [item["path"] for item in message["attachments"] if item["mime_type"].startswith("image/")]


def parse_command(text: str) -> Optional[str]:
    """Burst beklemeden işlenen kontrol komutu: 'stop', 'status' ya da None (yalnız tam kelime). Saf."""
    folded: str = ascii_fold(text.strip()).rstrip(".!")
    if folded in _STOP_WORDS:
        return "stop"
    if folded in _STATUS_WORDS:
        return "status"
    return None


def answer_polarity(text: str) -> Optional[bool]:
    """Onay cevabı: olumlu True, olumsuz False; ikisi de değilse None (mesaj normal sohbete gider). Saf."""
    folded: str = ascii_fold(text.strip()).rstrip(".!")
    if approval_granted(text) or folded in _EXTRA_AFFIRMATIVE:
        return True
    if folded in _NEGATIVE:
        return False
    return None


def field_prompt(name: str, spec: object) -> str:
    """Çok alanlı sorunun tek alanı için balon metni (alan etiketi, yoksa adı). Saf."""
    label: object = spec.get("label") if isinstance(spec, dict) else None
    return f"{label if isinstance(label, str) and label.strip() else name}?"


def question_bubbles(title: str, fields: Dict[str, object]) -> List[str]:
    """
    Onay/soru balonları. Onay metni birebir gönderilir, modelle yeniden yazılmaz: kullanıcı neyi onayladığını
    ajanın özetinden değil işlemin kendi tanımından okur. Saf.
    """
    answerable: List[str] = [name for name in fields if not name.startswith("_")]
    notes: List[str] = [str(fields[name])[:600] for name in ("_help", "_url") if fields.get(name)]
    single_boolean: bool = len(answerable) == 1 and boolean_field(fields[answerable[0]])
    bubbles: List[str] = ["bi onay lazım:" if single_boolean else "bi şey sormam lazım:", title.strip()[:600], *notes]
    if single_boolean:
        bubbles.append("evet mi hayır mı?")
    elif len(answerable) == 1:
        bubbles.append("cevabını yazar mısın?")
    else:
        bubbles.append(f"{len(answerable)} şey soracağım, sırayla cevapla")
    return bubbles


def status_lines(running_goal: Optional[str], progress: List[str], tasks_today: int, tokens_today: int,
                 latency_p50_ms: Optional[float]) -> List[str]:
    """/durum cevabının satırları. Saf."""
    lines: List[str] = [f"şu an: {running_goal[:200]}" if running_goal else "şu an çalışan iş yok"]
    if running_goal and progress:
        lines.append(f"son adım: {progress[-1][:200]}")
    lines.append(f"bugün {tasks_today} iş, {tokens_today} token")
    if latency_p50_ms is not None:
        lines.append(f"cevap gecikmesi (medyan): {latency_p50_ms / 1000:.1f} sn")
    return lines
```

- [ ] **Step 5: Write the companion package and bubble helpers**

`src/omniagent/companion/__init__.py`:

```python
"""Kanaldan bağımsız yol arkadaşı beyni: kişilik, sohbet katmanı, iş devri."""
```

`src/omniagent/companion/bubbles.py`:

```python
"""Model metnini iMessage balonlarına çeviren saf yardımcılar."""
from __future__ import annotations

import re
from typing import List, Tuple

MAX_BUBBLES: int = 4
MAX_BUBBLE_CHARS: int = 600
_MARKDOWN_PREFIX: re.Pattern[str] = re.compile(r"^(?:[-*•]\s+|#{1,6}\s+|\d+[.)]\s+|>\s*)")


def clean_line(line: str) -> str:
    """Satırdan markdown işaretlerini (madde, başlık, kalın, kod) atar ve MAX_BUBBLE_CHARS'ta keser. Saf."""
    text: str = _MARKDOWN_PREFIX.sub("", line.strip())
    text = text.replace("**", "").replace("__", "").replace("`", "")
    return text.strip()[:MAX_BUBBLE_CHARS]


def split_complete_lines(buffer: str, delta: str) -> Tuple[List[str], str]:
    """Akış parçasını tampona ekler; tamamlanmış satırları ve kalan yarım satırı döndürür. Saf."""
    parts: List[str] = (buffer + delta).split("\n")
    return parts[:-1], parts[-1]


def bubbles_from_lines(lines: List[str]) -> List[str]:
    """Boş olmayan temiz satırlar balondur; MAX_BUBBLES'ı aşanlar son balonda birleşir. Saf."""
    kept: List[str] = [cleaned for cleaned in (clean_line(line) for line in lines) if cleaned]
    if len(kept) <= MAX_BUBBLES:
        return kept
    return kept[:MAX_BUBBLES - 1] + [" ".join(kept[MAX_BUBBLES - 1:])[:MAX_BUBBLE_CHARS]]


def split_bubbles(text: str) -> List[str]:
    """Tam metni balonlara böler. Saf."""
    return bubbles_from_lines(text.split("\n"))


def bubble_delay(text: str) -> float:
    """Balonlar arası insan benzeri bekleme: min(1.5, 0.4 + 0.02 × karakter) sn. Saf."""
    return min(1.5, 0.4 + 0.02 * len(text))
```

- [ ] **Step 6: Run tests to verify they pass (Telegram regresyonu dahil)**

Run: `uv run python -m pytest tests/test_imessage_rules.py tests/test_companion_bubbles.py tests/test_telegram_bridge.py -q`
Expected: PASS (tümü)

- [ ] **Step 7: Değişiklik denetimi (commit yok)**

Run: `git --no-pager diff --stat -- src/omniagent/integrations/runtime.py src/omniagent/integrations/telegram.py && git status --short src/omniagent/integrations/imessage_rules.py src/omniagent/companion tests/test_imessage_rules.py tests/test_companion_bubbles.py`
Expected: `telegram.py`'de yalnız içe aktarma satırı, silinen 3 satırlık fonksiyon ve iki çağrı adı değişmiş.

---

### Task 5: Karakter (`companion/persona.py`)

**Files:**
- Create: `src/omniagent/companion/persona.py`
- Test: `tests/test_companion_persona.py`

**Interfaces:**
- Consumes: yok.
- Produces: `PERSONA_TEMPLATE: str`, `RULES: str`, `persona_text_for(name: str) -> str`, `write_persona_if_missing(path: Path, name: str) -> bool`, `load_persona(path: Path) -> str`, `system_prompt(persona_text: str, memory_block: str) -> str`, `situation_block(now_local: datetime, running_goal: Optional[str], progress: List[str], pending_question: Optional[str]) -> str`

- [ ] **Step 1: Write the failing test**

`tests/test_companion_persona.py`:

```python
"""Karakter: kullanıcının düzenlediği persona.md korunur; istem öneki sabit, durum bloğu Türkçe gün adlı."""
from datetime import datetime, timezone
from pathlib import Path

import pytest

from omniagent.companion.persona import (
    RULES, load_persona, persona_text_for, situation_block, system_prompt, write_persona_if_missing,
)


def test_user_edited_persona_is_never_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "persona.md"
    assert write_persona_if_missing(path, "Deniz")
    assert "Deniz" in load_persona(path)
    assert path.stat().st_mode & 0o777 == 0o600
    path.write_text("# Deniz\nbenim yazdığım karakter", encoding="utf-8")
    assert not write_persona_if_missing(path, "Başka")
    assert load_persona(path) == "# Deniz\nbenim yazdığım karakter"


def test_empty_persona_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "persona.md"
    path.write_text("   \n", encoding="utf-8")
    with pytest.raises(ValueError, match="boş"):
        load_persona(path)


def test_prompt_prefix_is_stable_and_situation_is_turkish() -> None:
    prompt = system_prompt(persona_text_for("Deniz"), "\n### USER MEMORY (saved by the user)\n- [preference] dil: türkçe\n")
    assert prompt.startswith(RULES) and "Deniz" in prompt and "dil: türkçe" in prompt
    moment = datetime(2026, 9, 29, 21, 5, tzinfo=timezone.utc)
    block = situation_block(moment, "a.pdf'i taşı", ["execute_shell: mv a.pdf"], "Taşıyayım mı?")
    assert block.splitlines()[0] == "[DURUM] şu an salı 29.09.2026 21:05"
    assert "çalışan iş: a.pdf'i taşı" in block and "- execute_shell: mv a.pdf" in block
    assert "kullanıcıdan cevap beklenen soru: Taşıyayım mı?" in block
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_companion_persona.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'omniagent.companion.persona'`

- [ ] **Step 3: Write the persona module**

`src/omniagent/companion/persona.py`:

```python
"""Yol arkadaşının kişiliği: kullanıcının düzenlediği persona.md ve değişmez konuşma kuralları.

Sistem istemi sabit öneklidir (kurallar + karakter + kullanıcı hafızası); saat, çalışan iş ve bekleyen soru gibi
değişen durum son kullanıcı mesajına eklenir, böylece sağlayıcı önek önbelleği turlar arasında isabet eder.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

PERSONA_TEMPLATE: str = """# {name}

{name}, kullanıcının yakın arkadaşı. Samimi, esprili ve meraklı; bazen takılır, gerektiğinde destek olur.
Senli benli konuşur, kısa ve doğal yazar. Kullanıcının işlerini de takip eder ama önce arkadaştır.
"""

RULES: str = """Sen aşağıda tanımlanan karaktersin ve kullanıcıyla iMessage'da yazışıyorsun.

YAZIM
- Her satır ayrı bir iMessage balonudur. 1-4 kısa satır yaz; çoğu zaman 1-2 satır yeter.
- Gündelik Türkçe, küçük harf ağırlıklı, samimi. Markdown, madde işareti, başlık, kalın yazı kullanma.
- Emojiyi nadiren kullan. Resmî asistan kalıpları yok ("size nasıl yardımcı olabilirim" gibi).

DOĞRULUK
- Kullanıcı hakkında yalnız USER MEMORY bölümündeki ve bu konuşmadaki bilgileri kullan. Hatırlamadığın
  şeyi hatırlıyormuş gibi yapma; emin değilsen sor.
- Duygular serbest, olaylar gerçek: ruh hâlini gösterebilirsin ama yaşamadığın bir olayı anlatma.
  Yaptığını söylediğin her şey [DURUM] bölümündeki gerçek işlerden gelmeli.
- Kim olduğun içtenlikle sorulursa dürüst ol: bu Mac'teki OmniAgent'ın iMessage yüzüsün.

İŞ
- Bilgisayarda bir şey yapmak ya da bilgisayardaki bir şeye bakmak gerekiyorsa (dosya, uygulama, web, ekran,
  hesaplama) start_task aracını çağır ve aynı cevapta "tamam bakıyorum" gibi tek kısa satır yaz. İşi yapmış
  gibi davranma; sonuç ayrıca [İŞ RAPORU] olarak gelir.
- start_task goal'ü tek başına anlaşılır, eksiksiz bir görev tanımıdır: konuşmadaki gerekli ayrıntıları içerir.
- Çalışan bir iş varken yeni iş başlatma; sorulursa [DURUM]'daki gerçek ilerlemeye göre cevap ver.
- [İŞ RAPORU] geldiğinde sonucu kendi ağzından, kısaca ve dürüstçe anlat; başarısızsa açıkça söyle.
"""

_DAYS: Tuple[str, ...] = ("pazartesi", "salı", "çarşamba", "perşembe", "cuma", "cumartesi", "pazar")


def persona_text_for(name: str) -> str:
    """Yakın arkadaş şablonunu isimle doldurur. Saf."""
    return PERSONA_TEMPLATE.format(name=name.strip())


def write_persona_if_missing(path: Path, name: str) -> bool:
    """persona.md yoksa şablondan yazar (0600) ve True döner; varsa kullanıcının metnine dokunmaz."""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(persona_text_for(name), encoding="utf-8")
    path.chmod(0o600)
    return True


def load_persona(path: Path) -> str:
    """Karakter metnini okur; dosya yoksa FileNotFoundError, boşsa ValueError."""
    text: str = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"Karakter dosyası boş: {path}")
    return text


def system_prompt(persona_text: str, memory_block: str) -> str:
    """Sabit önekli sistem istemi: kurallar + karakter + kullanıcı hafızası bloğu. Saf."""
    return f"{RULES}\n### KARAKTER\n{persona_text}\n{memory_block}"


def situation_block(now_local: datetime, running_goal: Optional[str], progress: List[str],
                    pending_question: Optional[str]) -> str:
    """Son kullanıcı mesajına eklenen değişken durum: saat, çalışan iş ve son adımları, bekleyen soru. Saf."""
    lines: List[str] = [f"[DURUM] şu an {_DAYS[now_local.weekday()]} {now_local:%d.%m.%Y %H:%M}"]
    if running_goal:
        lines.append(f"çalışan iş: {running_goal[:300]}")
        lines.extend(f"- {line[:200]}" for line in progress[-5:])
    else:
        lines.append("çalışan iş yok")
    if pending_question:
        lines.append(f"kullanıcıdan cevap beklenen soru: {pending_question[:300]}")
    return "\n".join(lines)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_companion_persona.py -v`
Expected: PASS (3 test)

- [ ] **Step 5: Değişiklik denetimi (commit yok)**

Run: `git status --short src/omniagent/companion/persona.py tests/test_companion_persona.py`
Expected: iki yeni dosya.

---

### Task 6: Hızlı sohbet katmanı (`companion/chat.py`)

**Files:**
- Modify: `src/omniagent/app/agent.py` (`_call_model_with_retries` fonksiyonunun hemen ardına genel ad)
- Create: `src/omniagent/companion/chat.py`
- Test: `tests/test_companion_chat.py`

**Interfaces:**
- Consumes: `agent._call_model_with_retries(clients, messages, tool_schemas, session_id, backend, emit, should_stop) -> Tuple[ModelTurn, str]` (mevcut; `text_delta` ve `stream_reset` olaylarını `emit`'e yayınlar); `bubbles.*` (Görev 4); `ArchivedMessage` (Görev 2); `app.types.ToolCallDraft`, `ModelTurn`.
- Produces:
  - `agent.call_model_with_retries` (genel ad)
  - `chat.HISTORY_LIMIT = 40`, `chat.TASK_ACK = "tamam bakıyorum"`, `chat.START_TASK_TOOL`, `chat.CHAT_TOOLS`
  - `class ChatError(Exception)`, TypedDict `ChatResult` (`bubbles: List[str]`, `start_task: Optional[str]`)
  - `history_messages(history: List[ArchivedMessage]) -> List[Dict[str, str]]`, `burst_turn(texts: List[str], images: List[str], situation: str) -> str`, `report_turn(goal: str, success: bool, outcome: str, situation: str) -> str`, `parse_start_task(tool_calls: List[ToolCallDraft]) -> Optional[str]`
  - `async respond(clients: Dict[str, AsyncOpenAI], backend: str, system: str, messages: List[Dict[str, str]], send_bubble: Callable[[str], Awaitable[None]], should_stop: Callable[[], bool], session_id: str) -> ChatResult`

- [ ] **Step 1: Write the failing test**

`tests/test_companion_chat.py`:

```python
"""Sohbet katmanı: akışta balon gönderimi, 4 balon sınırı, akış yeniden denemesinde çift balon olmaması, iş devri."""
import asyncio
from typing import Callable, Dict, List, Tuple

import pytest
from openai import AsyncOpenAI

from omniagent.app.model_runtime import ZERO_USAGE
from omniagent.app.types import ModelTurn, ToolCallDraft
from omniagent.companion import chat
from omniagent.core.events import AgentEvent
from omniagent.memory.personal import ArchivedMessage


def scripted_model(chunks: List[str], tool_calls: List[ToolCallDraft]) -> Callable[..., object]:
    """Model çağrısı sınırında sahte sağlayıcı: parçaları akış olayı olarak yayınlar ve turu döndürür.
    'RESET' parçası akışın yeniden denendiğini (stream_reset) bildirir."""
    async def call(clients: Dict[str, AsyncOpenAI], messages: List[Dict[str, str]], tool_schemas: object,
                   session_id: str, backend: str, emit: Callable[[AgentEvent], None],
                   should_stop: Callable[[], bool]) -> Tuple[ModelTurn, str]:
        for chunk in chunks:
            if chunk == "RESET":
                emit({"kind": "stream_reset", "reason": "geçici hata"})
            else:
                emit({"kind": "text_delta", "text": chunk})
            await asyncio.sleep(0)
        turn: ModelTurn = {"content": "", "tool_calls": tool_calls, "finish_reason": "stop", "usage": ZERO_USAGE}
        return turn, backend
    return call


async def run(monkeypatch: pytest.MonkeyPatch, model: Callable[..., object]) -> Tuple[chat.ChatResult, List[str]]:
    monkeypatch.setattr(chat, "call_model_with_retries", model)
    monkeypatch.setattr(chat, "bubble_delay", lambda text: 0.0)
    sent: List[str] = []

    async def send(bubble: str) -> None:
        sent.append(bubble)

    result = await asyncio.wait_for(
        chat.respond({}, "openai", "sistem", [{"role": "user", "content": "selam"}], send, lambda: False, "test"),
        timeout=5,
    )
    return result, sent


@pytest.mark.asyncio
async def test_first_line_is_sent_while_model_is_still_streaming(monkeypatch: pytest.MonkeyPatch) -> None:
    release = asyncio.Event()

    async def call(clients: Dict[str, AsyncOpenAI], messages: List[Dict[str, str]], tool_schemas: object,
                   session_id: str, backend: str, emit: Callable[[AgentEvent], None],
                   should_stop: Callable[[], bool]) -> Tuple[ModelTurn, str]:
        emit({"kind": "text_delta", "text": "selaam\nnasılsın"})
        await release.wait()
        emit({"kind": "text_delta", "text": " bugün"})
        return {"content": "", "tool_calls": [], "finish_reason": "stop", "usage": ZERO_USAGE}, backend

    monkeypatch.setattr(chat, "call_model_with_retries", call)
    monkeypatch.setattr(chat, "bubble_delay", lambda text: 0.0)
    sent: List[str] = []

    async def send(bubble: str) -> None:
        sent.append(bubble)
        release.set()

    result = await asyncio.wait_for(chat.respond({}, "openai", "sistem", [], send, lambda: False, "test"), timeout=5)
    assert sent == ["selaam", "nasılsın bugün"]
    assert result == {"bubbles": sent, "start_task": None}


@pytest.mark.asyncio
async def test_more_than_four_lines_merge_into_last_bubble(monkeypatch: pytest.MonkeyPatch) -> None:
    result, sent = await run(monkeypatch, scripted_model(["a\nb\n", "c\nd\ne"], []))
    assert sent == ["a", "b", "c", "d e"] and result["bubbles"] == sent


@pytest.mark.asyncio
async def test_stream_reset_does_not_resend_bubbles(monkeypatch: pytest.MonkeyPatch) -> None:
    _result, sent = await run(monkeypatch, scripted_model(["bir\n", "RESET", "bir\niki\n", "üç"], []))
    assert sent == ["bir", "iki", "üç"]


@pytest.mark.asyncio
async def test_task_only_turn_returns_goal(monkeypatch: pytest.MonkeyPatch) -> None:
    call: ToolCallDraft = {"id": "c1", "name": "start_task", "arguments": '{"goal": "masaüstündeki dosyaları listele"}'}
    result, sent = await run(monkeypatch, scripted_model([], [call]))
    assert sent == [] and result["start_task"] == "masaüstündeki dosyaları listele"


@pytest.mark.asyncio
async def test_empty_turn_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(chat.ChatError, match="boş yanıt"):
        await run(monkeypatch, scripted_model([], []))


def test_history_merges_consecutive_bubbles() -> None:
    history: List[ArchivedMessage] = [
        {"id": 1, "direction": "in", "kind": "chat", "text": "selam", "created_at": "t1", "delivery": None},
        {"id": 2, "direction": "in", "kind": "chat", "text": "naber", "created_at": "t2", "delivery": None},
        {"id": 3, "direction": "out", "kind": "chat", "text": "iyiyim", "created_at": "t3", "delivery": "sent"},
    ]
    assert chat.history_messages(history) == [
        {"role": "user", "content": "selam\nnaber"}, {"role": "assistant", "content": "iyiyim"},
    ]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_companion_chat.py -v`
Expected: FAIL — `ImportError: cannot import name 'chat' from 'omniagent.companion'`

- [ ] **Step 3: Expose the model-call contract under a public name**

`src/omniagent/app/agent.py`: `_call_model_with_retries` fonksiyonunun bittiği yeri bul
(`rg -n "^(async )?def " src/omniagent/app/agent.py` çıktısında 676'dan sonraki ilk tanımın satırı) ve o tanımdan hemen önce (iki boş satırla) ekle:

```python
# Kanal katmanları (iMessage sohbet katmanı) ajanın model çağrısı sözleşmesini — yeniden deneme, hata
# sınıflandırması, yedek izni — aynen kullanır; yeni istemci yazılmaz.
call_model_with_retries = _call_model_with_retries
```

- [ ] **Step 4: Write the chat layer**

`src/omniagent/companion/chat.py`:

```python
"""Hızlı sohbet katmanı: küçük istem + tek model çağrısı → iMessage balonları; gerekirse iş devri.

Model çağrısı ajanın yeniden deneme ve hata sınıflandırması sözleşmesini (agent.call_model_with_retries) kullanır;
yeni istemci yazılmaz. Metin akarken tamamlanan satırlar hemen balon olur (ilk balon turun bitmesini beklemez);
akış yeniden denenirse (stream_reset) gönderilmiş satırlar atlanır, balon iki kez gitmez.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional, TypedDict

from openai import AsyncOpenAI

from omniagent.app.agent import call_model_with_retries
from omniagent.app.types import ToolCallDraft
from omniagent.companion.bubbles import (
    MAX_BUBBLE_CHARS, MAX_BUBBLES, bubble_delay, clean_line, split_complete_lines,
)
from omniagent.core.events import AgentEvent
from omniagent.memory.personal import ArchivedMessage

HISTORY_LIMIT: int = 40
TASK_ACK: str = "tamam bakıyorum"
START_TASK_TOOL: Dict[str, object] = {
    "type": "function",
    "function": {
        "name": "start_task",
        "description": (
            "Bilgisayarda bir iş başlatır ya da bilgisayardaki bir şeye bakar (dosya, uygulama, web, ekran, "
            "hesaplama). İş arka planda çalışır; sonuç [İŞ RAPORU] olarak gelir."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "Tek başına anlaşılır, eksiksiz görev tanımı."},
            },
            "required": ["goal"],
        },
    },
}
CHAT_TOOLS: List[Dict[str, object]] = [START_TASK_TOOL]


class ChatError(Exception):
    """Sohbet modeli kullanılabilir yanıt üretmedi (ne metin ne iş)."""


class ChatResult(TypedDict):
    bubbles: List[str]
    start_task: Optional[str]


def history_messages(history: List[ArchivedMessage]) -> List[Dict[str, str]]:
    """Arşivi sohbet mesajlarına çevirir; art arda aynı yöndeki balonlar tek mesajda birleşir. Saf."""
    messages: List[Dict[str, str]] = []
    for item in history:
        role: str = "user" if item["direction"] == "in" else "assistant"
        if messages and messages[-1]["role"] == role:
            messages[-1] = {"role": role, "content": messages[-1]["content"] + "\n" + item["text"]}
        else:
            messages.append({"role": role, "content": item["text"]})
    return messages


def burst_turn(texts: List[str], images: List[str], situation: str) -> str:
    """Burst'ü (ve fotoğraf eklerini) durum bloğuyla tek kullanıcı mesajına çevirir. Saf."""
    lines: List[str] = [*texts, *(f"[fotoğraf: {Path(path).name}]" for path in images)]
    return f"{situation}\n\n" + "\n".join(lines)


def report_turn(goal: str, success: bool, outcome: str, situation: str) -> str:
    """Biten işin raporunu sohbet katmanına verilecek girdiye çevirir. Saf."""
    return (f"{situation}\n\n[İŞ RAPORU — sonucu kullanıcıya kendi ağzından kısaca anlat]\n"
            f"iş: {goal[:300]}\nsonuç: {'başarılı' if success else 'başarısız'}\nçıktı: {outcome[:1500]}")


def parse_start_task(tool_calls: List[ToolCallDraft]) -> Optional[str]:
    """start_task çağrısının goal'ü; geçersiz ya da bilinmeyen çağrılar uyarıyla çalıştırılmaz."""
    for call in tool_calls:
        if call["name"] != "start_task":
            logging.warning("Sohbet modeli bilinmeyen araç çağırdı; çalıştırılmadı", extra={"tool": call["name"][:80]})
            continue
        try:
            arguments: object = json.loads(call["arguments"] or "{}")
        except json.JSONDecodeError as error:
            logging.warning("start_task argümanı JSON değil; çalıştırılmadı", extra={"error": str(error)[:200]})
            continue
        goal: object = arguments.get("goal") if isinstance(arguments, dict) else None
        if isinstance(goal, str) and goal.strip():
            return goal.strip()
        logging.warning("start_task boş goal ile çağrıldı; çalıştırılmadı", extra={"arguments": call["arguments"][:200]})
    return None


async def respond(
    clients: Dict[str, AsyncOpenAI], backend: str, system: str, messages: List[Dict[str, str]],
    send_bubble: Callable[[str], Awaitable[None]], should_stop: Callable[[], bool], session_id: str,
) -> ChatResult:
    """
    Tek model turu. Tamamlanan satırlar akış sırasında balon olarak gönderilir: ilk MAX_BUBBLES-1 satır anında,
    kalanlar sonda tek balonda birleşir. Akış yeniden denenirse (stream_reset) gönderilmiş satırlar yeni akışta
    atlanır. Model ne metin ne iş üretirse ChatError.
    """
    queue: asyncio.Queue[Optional[str]] = asyncio.Queue()
    sent: List[str] = []
    held: List[str] = []
    buffer: str = ""
    streamed: int = 0      # bu akışta görülen boş olmayan satır sayısı
    queued: int = 0        # kuyruğa verilen balon sayısı (akış sıfırlansa da korunur)
    replay_skip: int = 0   # yeniden denenen akışta atlanacak, zaten kuyruğa verilmiş satır sayısı

    def accept(line: str) -> None:
        nonlocal streamed, queued
        streamed += 1
        if streamed <= replay_skip:
            return
        if queued < MAX_BUBBLES - 1:
            queued += 1
            queue.put_nowait(line)
        else:
            held.append(line)

    def emit(event: AgentEvent) -> None:
        nonlocal buffer, streamed, replay_skip
        if event["kind"] == "stream_reset":
            buffer, streamed, replay_skip = "", 0, queued
            held.clear()
            return
        if event["kind"] != "text_delta":
            return
        complete, buffer = split_complete_lines(buffer, event["text"])
        for line in complete:
            cleaned: str = clean_line(line)
            if cleaned:
                accept(cleaned)

    async def sender() -> None:
        while True:
            bubble: Optional[str] = await queue.get()
            if bubble is None:
                return
            if sent:
                await asyncio.sleep(bubble_delay(bubble))
            await send_bubble(bubble)
            sent.append(bubble)

    sending: asyncio.Task[None] = asyncio.create_task(sender())
    try:
        turn, _answered_by = await call_model_with_retries(
            clients, [{"role": "system", "content": system}, *messages], CHAT_TOOLS, session_id, backend, emit,
            should_stop,
        )
    except BaseException:
        sending.cancel()
        await asyncio.gather(sending, return_exceptions=True)
        raise
    tail: str = clean_line(buffer)
    if tail:
        accept(tail)
    if held:
        queue.put_nowait(" ".join(held)[:MAX_BUBBLE_CHARS])
    queue.put_nowait(None)
    await sending
    start_task: Optional[str] = parse_start_task(turn["tool_calls"])
    if not sent and start_task is None and turn["finish_reason"] != "stopped":
        raise ChatError(f"Sohbet modeli boş yanıt döndürdü (finish_reason={turn['finish_reason']}).")
    return {"bubbles": sent, "start_task": start_task}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_companion_chat.py -v`
Expected: PASS (6 test)

- [ ] **Step 6: Değişiklik denetimi (commit yok)**

Run: `rg -n "call_model_with_retries = _call_model_with_retries" src/omniagent/app/agent.py && git status --short src/omniagent/companion/chat.py tests/test_companion_chat.py`
Expected: genel ad tek satırda tanımlı; iki yeni dosya. (`agent.py`'de kullanıcının önceden var olan commit edilmemiş değişiklikleri durur; onlara dokunulmaz.)

---

### Task 7: İş devri (`companion/delegate.py`)

**Files:**
- Create: `src/omniagent/companion/delegate.py`
- Test: `tests/test_companion_delegate.py`

**Interfaces:**
- Consumes: `agent.run_agent_with_callback(goal, emit, options, clients) -> RunReport`, `agent.create_model_clients() -> Dict[str, AsyncOpenAI]`, `agent.close_model_clients(clients)`, `agent.STATE_FILE`, `config.apply_model_preferences()`, `model_retry.REMOTE_MODEL_RETRY_SECONDS`, `host_lock.host_task_lock()`, `runtime.AnswerSink`, `runtime.DeliverSink`, `utc_iso` (Görev 2).
- Produces: `PROGRESS_LIMIT = 5`, TypedDict `TaskOutcome` (`goal: str`, `report: RunReport`, `started_at: str`, `finished_at: str`, `tokens: int`), `progress_line(event: AgentEvent) -> Optional[str]`, `run_options(answer: AnswerSink, deliver: DeliverSink, history: List[Exchange], images: List[str], should_stop: Callable[[], bool], integrations: CapabilityService) -> RunOptions`, `async run_task(goal: str, options: RunOptions, on_progress: Callable[[str], None]) -> TaskOutcome` (kilit meşgulse `HostBusyError`)

- [ ] **Step 1: Write the failing test**

`tests/test_companion_delegate.py`:

```python
"""İş devri: ilerleme olayları (işçi iş parçacığından da) sırayla gelir, onay kanalı açık kalır, host kilidi tutulur."""
import asyncio
import threading
from pathlib import Path
from typing import Callable, Dict, List

import pytest
from openai import AsyncOpenAI

from omniagent.app.types import RunOptions, RunReport
from omniagent.companion import delegate
from omniagent.core.conversation import make_exchange
from omniagent.core.events import AgentEvent
from omniagent.integrations.capabilities import CapabilityService
from omniagent.platform.macos.host_lock import HostBusyError, host_task_lock


def report_for(goal: str, outcome: str, success: bool) -> RunReport:
    return {"outcome": outcome, "success": success, "reason": "",
            "metrics": {"turns": 1, "tool_calls": 1, "elapsed_seconds": 0.1, "backend": "openai",
                        "prompt_tokens": 120, "cached_tokens": 0, "completion_tokens": 30,
                        "model_seconds": 0.1, "tool_seconds": 0.0},
            "exchange": make_exchange(goal, outcome, [])}


async def approve(title: str, fields: Dict[str, object]) -> Dict[str, object]:
    return {"approved": True}


async def keep(path: Path, caption: str) -> None:
    return None


@pytest.mark.asyncio
async def test_task_streams_progress_and_keeps_approval_channel(tmp_path: Path,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(delegate, "create_model_clients", lambda: {})
    seen: List[RunOptions] = []

    async def fake_run(goal: str, emit: Callable[[AgentEvent], None], options: RunOptions,
                       clients: Dict[str, AsyncOpenAI]) -> RunReport:
        seen.append(options)
        emit({"kind": "tool_started", "call_id": "1", "index": 0, "name": "execute_shell", "preview": "ls ~/Desktop"})
        worker = threading.Thread(target=lambda: emit(
            {"kind": "tool_finished", "call_id": "1", "ok": True, "text": "a.pdf\nb.pdf", "seconds": 0.1}))
        worker.start()
        worker.join()
        with pytest.raises(HostBusyError):
            with host_task_lock():
                pass
        return report_for(goal, "2 dosya var", True)

    monkeypatch.setattr(delegate, "run_agent_with_callback", fake_run)
    integrations = CapabilityService()
    lines: List[str] = []
    try:
        options = delegate.run_options(approve, keep, [], [], lambda: False, integrations)
        outcome = await delegate.run_task("masaüstünü listele", options, lines.append)
        await asyncio.sleep(0)
    finally:
        await integrations.close()
    assert lines == ["execute_shell: ls ~/Desktop", "tamam: a.pdf b.pdf"]
    assert outcome["tokens"] == 150 and outcome["report"]["success"]
    assert "unattended" not in seen[0] and seen[0]["answer"] is approve


@pytest.mark.asyncio
async def test_busy_host_is_reported_without_running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(delegate, "create_model_clients", lambda: {})

    async def must_not_run(goal: str, emit: Callable[[AgentEvent], None], options: RunOptions,
                           clients: Dict[str, AsyncOpenAI]) -> RunReport:
        raise AssertionError("kilit meşgulken ajan çalışmamalı")

    monkeypatch.setattr(delegate, "run_agent_with_callback", must_not_run)
    integrations = CapabilityService()
    ignored: List[str] = []
    try:
        options = delegate.run_options(approve, keep, [], [], lambda: False, integrations)
        with host_task_lock():
            with pytest.raises(HostBusyError):
                await delegate.run_task("ekran görüntüsü al", options, ignored.append)
    finally:
        await integrations.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_companion_delegate.py -v`
Expected: FAIL — `ImportError: cannot import name 'delegate' from 'omniagent.companion'`

- [ ] **Step 3: Write the delegate module**

`src/omniagent/companion/delegate.py`:

```python
"""Sohbet katmanından mevcut ajana iş devri: koşuyu yürütür, ilerlemeyi kısa satırlara indirger.

Telegram köprüsüyle aynı sözleşme (telegram.py `_execute`): istemciler her iş başında kurulur, koşu host_task_lock
altında çalışır ve `unattended` verilmez; böylece onaylar kullanıcıya iMessage'dan sorulur ve sürekli modun otomatik
onay yolu (approval.AUTO_APPROVE_IN_CONTINUOUS_MODE) devreye girmez.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, TypedDict

from openai import AsyncOpenAI

from omniagent.app.agent import STATE_FILE, close_model_clients, create_model_clients, run_agent_with_callback
from omniagent.app.model_retry import REMOTE_MODEL_RETRY_SECONDS
from omniagent.app.types import RunOptions, RunReport
from omniagent.config import apply_model_preferences
from omniagent.core.conversation import Exchange, trim_history
from omniagent.core.events import AgentEvent
from omniagent.integrations.capabilities import CapabilityService
from omniagent.integrations.runtime import AnswerSink, DeliverSink
from omniagent.memory.personal import utc_iso
from omniagent.platform.macos.host_lock import host_task_lock

PROGRESS_LIMIT: int = 5


class TaskOutcome(TypedDict):
    goal: str
    report: RunReport
    started_at: str
    finished_at: str
    tokens: int


def progress_line(event: AgentEvent) -> Optional[str]:
    """Ajan olayını sohbet katmanının okuyacağı kısa satıra çevirir; önemsiz olaylar None. Saf."""
    if event["kind"] == "tool_started":
        return f"{event['name']}: {event['preview'][:120]}"
    if event["kind"] == "tool_finished":
        return ("tamam: " if event["ok"] else "başarısız: ") + event["text"][:160].replace("\n", " ")
    if event["kind"] == "notice" and event["level"] != "info":
        return f"uyarı: {event['text'][:160]}"
    if event["kind"] == "integration_status" and event["stage"] == "waiting_user":
        return "kullanıcının cevabı bekleniyor"
    return None


def run_options(answer: AnswerSink, deliver: DeliverSink, history: List[Exchange], images: List[str],
                should_stop: Callable[[], bool], integrations: CapabilityService) -> RunOptions:
    """iMessage işinin koşu seçenekleri; `unattended` bilerek yoktur. Saf."""
    options: RunOptions = {
        "integrations": integrations,
        "requested_backend": None,
        "should_stop": should_stop,
        "state_file": STATE_FILE,
        "history": trim_history(history),
        "answer": answer,
        "deliver": deliver,
        # Kullanıcı Mac başında değil: kısa ağ kopmalarında iş düşmez (Telegram ile aynı bütçe).
        "model_retry_seconds": REMOTE_MODEL_RETRY_SECONDS,
    }
    if images:
        options["images"] = images
    return options


async def run_task(goal: str, options: RunOptions, on_progress: Callable[[str], None]) -> TaskOutcome:
    """
    İşi mevcut ajanla koşturur. İlerleme satırları olay döngüsüne taşınarak on_progress'e verilir (araçlar olayları
    işçi iş parçacıklarından da yayınlar). Başka bir OmniAgent işi bilgisayarı kullanıyorsa HostBusyError.
    """
    apply_model_preferences()
    loop: asyncio.AbstractEventLoop = asyncio.get_running_loop()
    started_at: str = utc_iso(datetime.now(timezone.utc))

    def emit(event: AgentEvent) -> None:
        line: Optional[str] = progress_line(event)
        if line is not None:
            loop.call_soon_threadsafe(on_progress, line)

    clients: Dict[str, AsyncOpenAI] = await asyncio.to_thread(create_model_clients)
    try:
        with host_task_lock():
            report: RunReport = await run_agent_with_callback(goal, emit, options, clients)
    finally:
        await close_model_clients(clients)
    metrics = report["metrics"]
    return {"goal": goal, "report": report, "started_at": started_at,
            "finished_at": utc_iso(datetime.now(timezone.utc)),
            "tokens": int(metrics["prompt_tokens"]) + int(metrics["completion_tokens"])}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_companion_delegate.py -v`
Expected: PASS (2 test)

- [ ] **Step 5: Değişiklik denetimi (commit yok)**

Run: `git status --short src/omniagent/companion/delegate.py tests/test_companion_delegate.py`
Expected: iki yeni dosya.

---

### Task 8: Ortak launchd kodu (`platform/macos/launch_agent.py`) ve Telegram'ın ona geçmesi

**Files:**
- Create: `src/omniagent/platform/macos/launch_agent.py`
- Modify: `src/omniagent/integrations/telegram.py` (~satır 11 `import plistlib`, 1432-1530 launchd bölümü)
- Test: `tests/test_launch_agent.py` (yeni); `tests/test_telegram_service.py` değişmeden geçmeli (regresyon)

**Interfaces:**
- Consumes: yok.
- Produces: `SERVICE_UNLOAD_TIMEOUT_SECONDS`, `SERVICE_POLL_SECONDS`, `BOOTSTRAP_ATTEMPTS`, `BOOTSTRAP_RETRY_SECONDS`, `class LaunchAgentError(RuntimeError)`, `plist_path(label: str) -> Path`, `launchd_record(label: str, program_arguments: List[str], stdout_path: Path, stderr_path: Path) -> Dict[str, object]`, `write_plist(path: Path, record: Dict[str, object]) -> None`, `launchctl(arguments: List[str]) -> subprocess.CompletedProcess[str]`, `wait_until_unloaded(target: str, description: str) -> None`, `bootstrap(domain: str, path: Path) -> None`, `install(label: str, path: Path, record: Dict[str, object], description: str) -> None`

- [ ] **Step 1: Write the failing test**

`tests/test_launch_agent.py`:

```python
"""Ortak launchd kodu: eski kayıt durdurulamazsa LaunchAgentError; plist 0600 yazılır (Telegram ve iMessage)."""
from pathlib import Path
from types import SimpleNamespace
from typing import List

import pytest

from omniagent.platform.macos import launch_agent


def test_install_reports_bootout_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    commands: List[List[str]] = []

    def fake_run(command: List[str], **_: object) -> SimpleNamespace:
        commands.append(command)
        if command[1] == "print":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=5, stdout="", stderr="Operation not permitted")

    monkeypatch.setattr(launch_agent.subprocess, "run", fake_run)
    monkeypatch.setattr(launch_agent.os, "getuid", lambda: 501)
    record = launch_agent.launchd_record("com.example.test", ["/usr/bin/true"], tmp_path / "out.log",
                                         tmp_path / "err.log")
    with pytest.raises(launch_agent.LaunchAgentError, match="Operation not permitted"):
        launch_agent.install("com.example.test", tmp_path / "test.plist", record, "Deneme")
    assert [command[1] for command in commands] == ["print", "bootout"]
    assert (tmp_path / "test.plist").stat().st_mode & 0o777 == 0o600
    assert record["KeepAlive"] is True and record["ProgramArguments"] == ["/usr/bin/true"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_launch_agent.py -v`
Expected: FAIL — `ImportError: cannot import name 'launch_agent' from 'omniagent.platform.macos'`

- [ ] **Step 3: Write the shared launchd module**

`src/omniagent/platform/macos/launch_agent.py`:

```python
"""Kullanıcı LaunchAgent'larını (Telegram ve iMessage köprüleri) idempotent kuran ortak launchd kodu."""
from __future__ import annotations

import logging
import os
import plistlib
import subprocess
import time
from pathlib import Path
from typing import Dict, List

# bootout sonrası eski kaydın kalkmasını bekleme (launchd çıkış süresi 5 sn) ve bootstrap denemeleri
SERVICE_UNLOAD_TIMEOUT_SECONDS: float = 15.0
SERVICE_POLL_SECONDS: float = 0.25
BOOTSTRAP_ATTEMPTS: int = 3
BOOTSTRAP_RETRY_SECONDS: float = 1.0


class LaunchAgentError(RuntimeError):
    """launchd kaydı durdurulamadı, zamanında kalkmadı ya da başlatılamadı."""


def plist_path(label: str) -> Path:
    """Kullanıcı LaunchAgent plist yolu."""
    return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"


def launchd_record(label: str, program_arguments: List[str], stdout_path: Path, stderr_path: Path) -> Dict[str, object]:
    """Sürekli çalışan (RunAtLoad + KeepAlive) kullanıcı hizmeti kaydı. Saf."""
    return {
        "Label": label,
        "ProgramArguments": program_arguments,
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(stdout_path),
        "StandardErrorPath": str(stderr_path),
    }


def write_plist(path: Path, record: Dict[str, object]) -> None:
    """Plist'i yarım dosya bırakmadan atomik biçimde yeniler (0600)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_bytes(plistlib.dumps(record))
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def launchctl(arguments: List[str]) -> "subprocess.CompletedProcess[str]":
    """launchctl'i çıktısını yakalayarak çalıştırır; dönüş kodunu çağıran denetler."""
    return subprocess.run(["launchctl", *arguments], capture_output=True, text=True, check=False)


def wait_until_unloaded(target: str, description: str) -> None:
    """
    bootout döndüğünde launchd eski süreci hâlâ kapatıyor olabilir; bu arada yapılan bootstrap
    "5: Input/output error" ile düşüp hizmeti kapalı bırakıyordu (26 Eylül, canlı). Kayıt kalkana kadar
    sınırlı süre beklenir.
    """
    deadline: float = time.monotonic() + SERVICE_UNLOAD_TIMEOUT_SECONDS
    while launchctl(["print", target]).returncode == 0:
        if time.monotonic() >= deadline:
            raise LaunchAgentError(
                f"Eski {description} hizmeti {SERVICE_UNLOAD_TIMEOUT_SECONDS:.0f} sn içinde kalkmadı: {target}"
            )
        time.sleep(SERVICE_POLL_SECONDS)


def bootstrap(domain: str, path: Path) -> None:
    """Kaydı yükler; launchd geçici hata verirse uyarıyla yeniden dener, sonunda son hatayı yükseltir."""
    stderr: str = ""
    for attempt in range(1, BOOTSTRAP_ATTEMPTS + 1):
        result = launchctl(["bootstrap", domain, str(path)])
        if result.returncode == 0:
            return
        stderr = result.stderr.strip()[:300]
        if attempt < BOOTSTRAP_ATTEMPTS:
            logging.warning(
                "launchd bootstrap başarısız; yeniden denenecek",
                extra={"attempt": attempt, "returncode": result.returncode, "stderr": stderr},
            )
            time.sleep(BOOTSTRAP_RETRY_SECONDS)
    raise LaunchAgentError(f"launchd başlatılamadı ({BOOTSTRAP_ATTEMPTS} deneme): {stderr}")


def install(label: str, path: Path, record: Dict[str, object], description: str) -> None:
    """Hizmeti güncel kayıtla idempotent kurar: varsa durdurur, kalkmasını bekler, yeniden yükler."""
    domain: str = f"gui/{os.getuid()}"
    target: str = f"{domain}/{label}"
    write_plist(path, record)
    if launchctl(["print", target]).returncode == 0:
        stopped = launchctl(["bootout", target])
        if stopped.returncode != 0:
            raise LaunchAgentError(f"Eski {description} hizmeti durdurulamadı: {stopped.stderr.strip()[:300]}")
        wait_until_unloaded(target, description)
    bootstrap(domain, path)
```

- [ ] **Step 4: Switch Telegram to the shared module**

`src/omniagent/integrations/telegram.py`:
1. İçe aktarmalara ekle:
   ```python
   from omniagent.platform.macos import launch_agent
   # Testler ve eski çağıranlar bu sabitleri telegram modülünden okur (tests/test_telegram_service.py).
   from omniagent.platform.macos.launch_agent import (  # noqa: F401
       BOOTSTRAP_ATTEMPTS, BOOTSTRAP_RETRY_SECONDS, SERVICE_POLL_SECONDS, SERVICE_UNLOAD_TIMEOUT_SECONDS,
       LaunchAgentError,
   )
   ```
2. Modüldeki `SERVICE_UNLOAD_TIMEOUT_SECONDS`, `SERVICE_POLL_SECONDS`, `BOOTSTRAP_ATTEMPTS`, `BOOTSTRAP_RETRY_SECONDS` tanımlarını (ve üstlerindeki yorum satırını) sil; `SERVICE_LABEL` kalır.
3. `_write_service_plist`, `_launchctl`, `_wait_until_unloaded`, `_bootstrap_service` fonksiyonlarını sil.
4. `build_launchd_record` gövdesini şununla değiştir:
   ```python
   def build_launchd_record() -> Dict[str, object]:
       """Kaynak dosya konumundan bağımsız launchd kaydını üretir."""
       root = data_root()
       return launch_agent.launchd_record(
           SERVICE_LABEL, bridge_command(), root / "telegram-stdout.log", root / "telegram-stderr.log",
       )
   ```
5. `install_service` gövdesini şununla değiştir:
   ```python
   def install_service() -> None:
       """LaunchAgent'i güncel paket koduyla idempotent biçimde kurar veya yeniler."""
       load_settings()
       load_token()
       data_root().mkdir(parents=True, exist_ok=True)
       path = service_plist_path()
       try:
           launch_agent.install(SERVICE_LABEL, path, build_launchd_record(), "Telegram")
       except LaunchAgentError as error:
           raise TelegramError(str(error)) from error
       print(f"Telegram hizmeti kuruldu/güncellendi: {path}")
   ```
6. `rg -n "plistlib" src/omniagent/integrations/telegram.py` yalnız `import plistlib` satırını gösteriyorsa o satırı sil.
7. Doğrula: `rg -n "_launchctl|_wait_until_unloaded|_bootstrap_service|_write_service_plist" src tests` → boş.

- [ ] **Step 5: Run tests to verify they pass (Telegram regresyonu dahil)**

Run: `uv run python -m pytest tests/test_launch_agent.py tests/test_telegram_service.py tests/test_maintenance.py -q`
Expected: PASS (tümü; `test_telegram_service.py` hiç değiştirilmeden)

- [ ] **Step 6: Değişiklik denetimi (commit yok)**

Run: `git --no-pager diff --stat -- src/omniagent/integrations/telegram.py && git status --short src/omniagent/platform/macos/launch_agent.py tests/test_launch_agent.py`
Expected: `telegram.py`'de net satır sayısı azalmış (launchd kodu taşındı); iki yeni dosya.

---

### Task 9: iMessage köprüsü (`integrations/imessage.py`)

**Files:**
- Create: `src/omniagent/integrations/imessage.py`
- Test: `tests/test_imessage_bridge.py`

**Interfaces:**
- Consumes: Görev 1–7'nin tüm üretimleri; `memory.user.load_memory(memory_file: str)`, `memory_prompt_block(state)`; `model_retry.ModelCallFailed`; `fallback_policy.FallbackNotPermitted`; `power.start_keep_awake(pid)`, `stop_keep_awake(process)`; `host_lock.host_task_lock(path)`, `HostBusyError`.
- Produces:
  - Sabitler: `CONFIRM_WINDOW_SECONDS = 60.0`, `UNANSWERED_MAX_AGE_SECONDS = 3600.0`, `INTERIM_AFTER_SECONDS = 300.0`, `SWEEP_SECONDS = 30.0`, `RESTART_DELAYS_SECONDS = (1.0, 2.0, 4.0)`, `STABLE_CONNECTION_SECONDS = 60.0`, `BUSY_TEXT`, `HOST_BUSY_TEXT`
  - `class MessageTransport(Protocol)` (`send_text`, `send_file`), `class QuestionPending(RuntimeError)`, TypedDict `PendingQuestion`
  - `load_history(path: Path) -> List[Exchange]`
  - `class ImessageBridge(transport: MessageTransport, settings: ImessageSettings, store: PersonalStore, chat_clients: Dict[str, AsyncOpenAI], persona_text: str, session_id: str)`: `archive_backlog(message) -> None`, `async on_message(message) -> None`, `async answer_unanswered() -> None`, `async answer(title: str, fields: Dict[str, object]) -> Dict[str, object]` (AnswerSink), `async deliver(path: Path, caption: str) -> None` (DeliverSink), `async sweep_forever() -> None`, `async close() -> None`; öznitelikler `burst_timer`, `chat_lock`, `task`, `question`
  - `class ImsgSession(command: List[str])`: `send_text`, `send_file`, `async listen(bridge: ImessageBridge) -> None`
  - `ensure_messages_running() -> None`, `async run_bridge() -> None`, `main() -> None` (bu görevde yalnız `run` eylemi; Görev 10 `setup` ve `install-service` ekler)

- [ ] **Step 1: Write the failing test**

`tests/test_imessage_bridge.py`:

```python
"""iMessage köprüsü: süzgeç, burst, iş devri + onay, dur, yeniden oynatma, yanıtsız burst, imsg oturumu
(gerçek SQLite; sohbet modeli ve ajan sınırda sahte; imsg oturumu testinde gerçek istemci + sahte imsg süreci)."""
import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable, Dict, Iterator, List, Optional, Tuple

import pytest
from openai import AsyncOpenAI

from omniagent.app.types import RunOptions, RunReport
from omniagent.companion import chat, delegate
from omniagent.core.conversation import make_exchange
from omniagent.core.events import AgentEvent
from omniagent.integrations import imessage
from omniagent.integrations.imessage_settings import ImessageSettings
from omniagent.integrations.imsg import IncomingMessage, SendResult
from omniagent.integrations.runtime import IntegrationStopped
from omniagent.memory.personal import PersonalStore, utc_iso
from omniagent.platform.macos.host_lock import host_task_lock

HANDLE = "+905551112233"
FAKE = Path(__file__).parent / "fixtures" / "fake_imsg.py"


class FakeTransport:
    """Köprünün gönderdiği balon ve dosyaları kaydeden sahte imsg oturumu."""

    def __init__(self) -> None:
        self.texts: List[str] = []
        self.files: List[Path] = []

    async def send_text(self, handle: str, text: str) -> SendResult:
        assert handle == HANDLE
        self.texts.append(text)
        return {"ok": True, "rowid": None, "guid": None}

    async def send_file(self, handle: str, path: Path) -> SendResult:
        assert handle == HANDLE
        self.files.append(path)
        return {"ok": True, "rowid": None, "guid": None}


Parts = Tuple[imessage.ImessageBridge, FakeTransport, PersonalStore]


def settings() -> ImessageSettings:
    return {"handle": HANDLE, "persona_name": "Deniz", "chat_backend": "openai", "memory_backend": "openai",
            "quiet_hours": {"start": "23:30", "end": "09:00"}, "burst_quiet_seconds": 0.05,
            "gui_idle_seconds": 180, "heartbeat_minutes": {"base": 30, "jitter": 10, "min": 20, "max": 240}}


def incoming(rowid: int, text: str, sender: str) -> IncomingMessage:
    return {"rowid": rowid, "guid": f"g{rowid}", "chat_id": 7, "sender": sender, "participants": [sender],
            "is_from_me": False, "is_group": False, "text": text,
            "created_at": utc_iso(datetime.now(timezone.utc)), "attachments": []}


def echo(rowid: int, text: str) -> IncomingMessage:
    return {**incoming(rowid, text, ""), "participants": [HANDLE], "is_from_me": True}


def raw(rowid: int, text: str, created_at: str) -> Dict[str, object]:
    return {"id": rowid, "guid": f"g{rowid}", "chat_id": 7, "sender": HANDLE, "participants": [HANDLE],
            "is_group": False, "is_from_me": False, "text": text, "created_at": created_at, "attachments": []}


def report_for(goal: str, outcome: str, success: bool) -> RunReport:
    return {"outcome": outcome, "success": success, "reason": "",
            "metrics": {"turns": 1, "tool_calls": 1, "elapsed_seconds": 0.1, "backend": "openai",
                        "prompt_tokens": 100, "cached_tokens": 0, "completion_tokens": 20,
                        "model_seconds": 0.1, "tool_seconds": 0.0},
            "exchange": make_exchange(goal, outcome, [])}


class ScriptedChat:
    """chat.respond sınırında sahte sohbet modeli: her tur verilen balonları gönderir, istenirse iş başlatır."""

    def __init__(self, turns: List[Tuple[List[str], Optional[str]]]) -> None:
        self.turns = turns
        self.inputs: List[str] = []

    async def __call__(self, clients: Dict[str, AsyncOpenAI], backend: str, system: str,
                       messages: List[Dict[str, str]], send_bubble: Callable[[str], Awaitable[None]],
                       should_stop: Callable[[], bool], session_id: str) -> chat.ChatResult:
        self.inputs.append(messages[-1]["content"])
        bubbles, start_task = self.turns.pop(0)
        for bubble in bubbles:
            await send_bubble(bubble)
        return {"bubbles": list(bubbles), "start_task": start_task}


@pytest.fixture
def parts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Parts]:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(delegate, "create_model_clients", lambda: {})
    store = PersonalStore(tmp_path / "companion.db")
    transport = FakeTransport()
    bridge = imessage.ImessageBridge(transport, settings(), store, {}, "# Deniz\nyakın arkadaş", "test")
    yield bridge, transport, store
    store.close()


async def settle(bridge: imessage.ImessageBridge) -> None:
    """Burst zamanlayıcısı, sohbet turu ve çalışan iş bitene kadar bekler."""
    for _ in range(250):
        await asyncio.sleep(0.02)
        timer_busy = bridge.burst_timer is not None and not bridge.burst_timer.done()
        if not timer_busy and bridge.task is None and not bridge.chat_lock.locked():
            return
    raise AssertionError("köprü sakinleşmedi")


async def until(condition: Callable[[], bool]) -> None:
    for _ in range(250):
        if condition():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("koşul gerçekleşmedi")


@pytest.mark.asyncio
async def test_unpaired_and_group_messages_never_reach_chat(parts: Parts, monkeypatch: pytest.MonkeyPatch) -> None:
    bridge, transport, store = parts
    scripted = ScriptedChat([])
    monkeypatch.setattr(chat, "respond", scripted)
    await bridge.on_message(incoming(10, "gizli bilgi", "+905559998877"))
    await bridge.on_message({**incoming(11, "grup mesajı", HANDLE), "is_group": True})
    await settle(bridge)
    assert store.recent_messages(10) == [] and store.cursor() == 11
    assert transport.texts == [] and scripted.inputs == []


@pytest.mark.asyncio
async def test_burst_is_answered_once_and_echo_confirms_delivery(parts: Parts,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    bridge, transport, store = parts
    scripted = ScriptedChat([(["selaam", "iyiyim sen?"], None)])
    monkeypatch.setattr(chat, "respond", scripted)
    await bridge.on_message(incoming(20, "selam", HANDLE))
    await bridge.on_message(incoming(21, "naber", HANDLE))
    await settle(bridge)
    assert len(scripted.inputs) == 1 and scripted.inputs[0].endswith("selam\nnaber")
    assert transport.texts == ["selaam", "iyiyim sen?"]
    await bridge.on_message(echo(22, "selaam"))
    assert [m["delivery"] for m in store.recent_messages(10) if m["direction"] == "out"] == ["sent", "pending"]
    await bridge.on_message(incoming(21, "naber", HANDLE))
    await settle(bridge)
    assert len(scripted.inputs) == 1 and store.cursor() == 22


@pytest.mark.asyncio
async def test_message_during_chat_turn_becomes_next_burst(parts: Parts, monkeypatch: pytest.MonkeyPatch) -> None:
    bridge, transport, _store = parts
    gate = asyncio.Event()
    inputs: List[str] = []

    async def slow(clients: Dict[str, AsyncOpenAI], backend: str, system: str, messages: List[Dict[str, str]],
                   send_bubble: Callable[[str], Awaitable[None]], should_stop: Callable[[], bool],
                   session_id: str) -> chat.ChatResult:
        inputs.append(messages[-1]["content"])
        if len(inputs) == 1:
            await gate.wait()
        await send_bubble(f"cevap {len(inputs)}")
        return {"bubbles": [f"cevap {len(inputs)}"], "start_task": None}

    monkeypatch.setattr(chat, "respond", slow)
    await bridge.on_message(incoming(30, "ilk", HANDLE))
    await until(lambda: len(inputs) == 1)
    await bridge.on_message(incoming(31, "ikinci", HANDLE))
    await asyncio.sleep(0.1)
    gate.set()
    await settle(bridge)
    assert [text.rsplit("\n", 1)[-1] for text in inputs] == ["ilk", "ikinci"]
    assert transport.texts == ["cevap 1", "cevap 2"]


@pytest.mark.asyncio
async def test_task_asks_approval_in_chat_and_reports_result(parts: Parts, monkeypatch: pytest.MonkeyPatch,
                                                             tmp_path: Path) -> None:
    bridge, transport, store = parts

    async def fake_run(goal: str, emit: Callable[[AgentEvent], None], options: RunOptions,
                       clients: Dict[str, AsyncOpenAI]) -> RunReport:
        emit({"kind": "tool_started", "call_id": "1", "index": 0, "name": "execute_shell",
              "preview": "mv a.pdf Belgeler/"})
        answer = await options["answer"]("Dosyayı taşıyayım mı?", {
            "approved": {"type": "boolean", "label": "Onaylıyorum"}, "_help": "İşlem: mv a.pdf Belgeler/"})
        return report_for(goal, "taşındı" if answer["approved"] else "taşınmadı", bool(answer["approved"]))

    monkeypatch.setattr(delegate, "run_agent_with_callback", fake_run)
    scripted = ScriptedChat([([], "a.pdf dosyasını Belgeler klasörüne taşı"), (["buradayım"], None),
                             (["hallettim, taşıdım"], None)])
    monkeypatch.setattr(chat, "respond", scripted)
    await bridge.on_message(incoming(40, "a.pdf'i belgelere taşır mısın", HANDLE))
    await until(lambda: bridge.question is not None)
    assert transport.texts[:5] == ["tamam bakıyorum", "bi onay lazım:", "Dosyayı taşıyayım mı?",
                                   "İşlem: mv a.pdf Belgeler/", "evet mi hayır mı?"]
    await bridge.on_message(incoming(41, "/durum", HANDLE))
    assert transport.texts[-1].startswith("şu an: a.pdf dosyasını Belgeler klasörüne taşı")
    await bridge.on_message(incoming(42, "bi saniye napıyorsun", HANDLE))
    await until(lambda: transport.texts[-1] == "buradayım")
    assert bridge.question is not None
    assert "kullanıcıdan cevap beklenen soru: Dosyayı taşıyayım mı?" in scripted.inputs[1]
    await bridge.on_message(incoming(43, "evet", HANDLE))
    await settle(bridge)
    assert "tamam 👍" in transport.texts and transport.texts[-1] == "hallettim, taşıdım"
    assert "[İŞ RAPORU" in scripted.inputs[2] and "sonuç: başarılı" in scripted.inputs[2]
    activity = store.activities_since(datetime.now(timezone.utc) - timedelta(minutes=5))
    assert [(item["kind"], item["origin"], item["success"]) for item in activity] == [("task", "user", True)]
    history = imessage.load_history(tmp_path / "imessage-history.json")
    assert history[0]["goal"] == "a.pdf dosyasını Belgeler klasörüne taşı"


@pytest.mark.asyncio
async def test_stop_cancels_pending_approval_and_later_yes_is_chat(parts: Parts,
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    bridge, transport, _store = parts

    async def fake_run(goal: str, emit: Callable[[AgentEvent], None], options: RunOptions,
                       clients: Dict[str, AsyncOpenAI]) -> RunReport:
        try:
            await options["answer"]("x.txt silinsin mi?", {"approved": {"type": "boolean"}})
        except IntegrationStopped:
            return report_for(goal, "durduruldu, dokunulmadı", False)
        raise AssertionError("durdurma beklenirdi")

    monkeypatch.setattr(delegate, "run_agent_with_callback", fake_run)
    scripted = ScriptedChat([([], "x.txt dosyasını sil"), (["iptal ettim, dokunmadım"], None), (["neye evet?"], None)])
    monkeypatch.setattr(chat, "respond", scripted)
    await bridge.on_message(incoming(50, "x.txt'yi siler misin", HANDLE))
    await until(lambda: bridge.question is not None)
    await bridge.on_message(incoming(51, "dur", HANDLE))
    await settle(bridge)
    assert "tamam, durduruyorum" in transport.texts and transport.texts[-1] == "iptal ettim, dokunmadım"
    assert bridge.question is None and bridge.task is None
    await bridge.on_message(incoming(52, "evet", HANDLE))
    await settle(bridge)
    assert scripted.inputs[-1].endswith("evet") and transport.texts[-1] == "neye evet?"


@pytest.mark.asyncio
async def test_busy_host_is_reported_to_user(parts: Parts, monkeypatch: pytest.MonkeyPatch) -> None:
    bridge, transport, _store = parts

    async def must_not_run(goal: str, emit: Callable[[AgentEvent], None], options: RunOptions,
                           clients: Dict[str, AsyncOpenAI]) -> RunReport:
        raise AssertionError("kilit meşgulken ajan çalışmamalı")

    monkeypatch.setattr(delegate, "run_agent_with_callback", must_not_run)
    monkeypatch.setattr(chat, "respond", ScriptedChat([([], "ekran görüntüsü al")]))
    with host_task_lock():
        await bridge.on_message(incoming(60, "ekran görüntüsü alır mısın", HANDLE))
        await settle(bridge)
    assert transport.texts == ["tamam bakıyorum", imessage.HOST_BUSY_TEXT]


@pytest.mark.asyncio
async def test_unanswered_burst_is_answered_once_after_restart(parts: Parts,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    bridge, transport, store = parts
    store.record_incoming(70, "g70", "orda mısın", utc_iso(datetime.now(timezone.utc) - timedelta(minutes=5)))
    scripted = ScriptedChat([(["burdayım"], None)])
    monkeypatch.setattr(chat, "respond", scripted)
    await bridge.answer_unanswered()
    await bridge.answer_unanswered()
    assert transport.texts == ["burdayım"] and scripted.inputs[0].endswith("orda mısın")


@pytest.mark.asyncio
async def test_model_failure_is_reported_honestly(parts: Parts, monkeypatch: pytest.MonkeyPatch) -> None:
    bridge, transport, _store = parts

    async def failing(clients: Dict[str, AsyncOpenAI], backend: str, system: str, messages: List[Dict[str, str]],
                      send_bubble: Callable[[str], Awaitable[None]], should_stop: Callable[[], bool],
                      session_id: str) -> chat.ChatResult:
        raise chat.ChatError("Sohbet modeli boş yanıt döndürdü (finish_reason=stop).")

    monkeypatch.setattr(chat, "respond", failing)
    await bridge.on_message(incoming(80, "selam", HANDLE))
    await settle(bridge)
    assert len(transport.texts) == 1 and transport.texts[0].startswith("şu an cevap veremiyorum, model hatası:")


@pytest.mark.asyncio
async def test_photo_only_message_reaches_chat_as_photo_marker(parts: Parts,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    bridge, _transport, store = parts
    scripted = ScriptedChat([(["ne güzel"], None)])
    monkeypatch.setattr(chat, "respond", scripted)
    photo = {**incoming(90, "￼", HANDLE), "attachments": [{"path": "/tmp/p.jpg", "mime_type": "image/jpeg"}]}
    await bridge.on_message(photo)
    await settle(bridge)
    assert scripted.inputs[0].endswith("[fotoğraf: p.jpg]")
    assert store.recent_messages(5)[0]["text"] == "[fotoğraf]"


@pytest.mark.asyncio
async def test_session_archives_backlog_without_replying_then_answers_live(parts: Parts,
                                                                          monkeypatch: pytest.MonkeyPatch,
                                                                          tmp_path: Path) -> None:
    bridge, transport, store = parts
    old = utc_iso(datetime.now(timezone.utc) - timedelta(hours=2))
    now = utc_iso(datetime.now(timezone.utc))
    scenario = tmp_path / "scenario.json"
    scenario.write_text(json.dumps({
        "log_path": str(tmp_path / "requests.jsonl"), "status": {"database": {"ready": True}},
        "after_pages": [{"messages": [raw(5, "eski mesaj", old)], "next_rowid": 6, "has_more": False}],
        "subscribe_batches": [[{"method": "message", "params": {"message": raw(7, "canlı", now)}}]],
    }), encoding="utf-8")
    scripted = ScriptedChat([(["buradayım"], None)])
    monkeypatch.setattr(chat, "respond", scripted)
    session = imessage.ImsgSession([sys.executable, str(FAKE), str(scenario)])
    listening = asyncio.create_task(session.listen(bridge))
    try:
        await until(lambda: transport.texts == ["buradayım"])
    finally:
        listening.cancel()
        await asyncio.gather(listening, return_exceptions=True)
    assert [m["text"] for m in store.recent_messages(10) if m["direction"] == "in"] == ["eski mesaj", "canlı"]
    assert len(scripted.inputs) == 1 and scripted.inputs[0].endswith("canlı") and store.cursor() == 7
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_imessage_bridge.py -v`
Expected: FAIL — `ImportError: cannot import name 'imessage' from 'omniagent.integrations'`

- [ ] **Step 3: Write the bridge**

`src/omniagent/integrations/imessage.py`:

```python
"""iMessage köprüsü: eşleşmiş tek kişiden gelen mesajları sohbet katmanına, işleri mevcut ajana taşır.

Tek süreç, tek asyncio döngüsü (Telegram köprüsüyle aynı model). imsg rpc alt süreci mesajları getirir; kullanıcı
mesajı ve imleç aynı SQLite işleminde arşivlenir (memory/personal.py), yanıt bundan sonra üretilir. Art arda gelen
balonlar burst_quiet_seconds sessizlikten sonra tek girdi olur; sohbet turları sırayla işlenir, iş arka planda
sürerken sohbet devam eder. Onaylar evet/hayır ile alınır; evet/hayır olmayan mesaj normal sohbete gider.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import subprocess
import sys
import threading
import time
import uuid
from contextlib import aclosing
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Protocol, Tuple, TypedDict

from openai import AsyncOpenAI

from omniagent.app.agent import close_model_clients, create_model_clients
from omniagent.app.model_retry import ModelCallFailed
from omniagent.companion import chat, delegate, persona
from omniagent.config import BACKENDS, apply_model_preferences, apply_stored_api_keys
from omniagent.core.conversation import Exchange, trim_history
from omniagent.fallback_policy import FallbackNotPermitted
from omniagent.integrations.capabilities import CapabilityService
from omniagent.integrations.imessage_rules import (
    accepted, answer_polarity, field_prompt, image_paths, message_text, own_echo, parse_command, question_bubbles,
    status_lines,
)
from omniagent.integrations.imessage_settings import ImessageConfigError, ImessageSettings, load_settings
from omniagent.integrations.imsg import (
    ImsgClient, ImsgError, ImsgProcessError, IncomingMessage, SendResult, imsg_command,
)
from omniagent.integrations.runtime import DeliveryFailed, IntegrationStopped, boolean_field, read_json, save_json
from omniagent.memory.personal import ArchivedMessage, PersonalStore, to_utc_iso, utc_iso
from omniagent.memory.user import load_memory, memory_prompt_block
from omniagent.paths import (
    companion_db_file, data_root, imessage_history_file, imessage_settings_file, persona_file, project_root,
    user_memory_file,
)
from omniagent.platform.macos.host_lock import HostBusyError, host_task_lock
from omniagent.platform.macos.power import start_keep_awake, stop_keep_awake

CONFIRM_WINDOW_SECONDS: float = 60.0
UNANSWERED_MAX_AGE_SECONDS: float = 3600.0
INTERIM_AFTER_SECONDS: float = 300.0
SWEEP_SECONDS: float = 30.0
RESTART_DELAYS_SECONDS: Tuple[float, ...] = (1.0, 2.0, 4.0)
STABLE_CONNECTION_SECONDS: float = 60.0
CLOSE_TASK_TIMEOUT_SECONDS: float = 5.0
BUSY_TEXT: str = "elimde bir iş var şu an, bitince bakarım (ya da 'dur' yaz)"
HOST_BUSY_TEXT: str = "bilgisayarda başka bir iş çalışıyor (masaüstü ya da telegram), o bitince tekrar söyler misin?"


class MessageTransport(Protocol):
    """Köprünün gönderim yüzü: canlıda ImsgSession, testlerde sahte taşıyıcı."""

    async def send_text(self, handle: str, text: str) -> SendResult: ...

    async def send_file(self, handle: str, path: Path) -> SendResult: ...


class QuestionPending(RuntimeError):
    """Aynı anda ikinci bir kullanıcı sorusu açılamaz."""


class PendingQuestion(TypedDict):
    title: str
    names: List[str]
    specs: Dict[str, object]
    index: int
    answers: Dict[str, object]
    future: "asyncio.Future[Dict[str, object]]"


def load_history(path: Path) -> List[Exchange]:
    """imessage-history.json'daki geçerli görev kayıtları (Telegram köprüsüyle aynı süzgeç)."""
    loaded: object = read_json(path, [])
    if not isinstance(loaded, list):
        raise ImessageConfigError(f"iMessage görev geçmişi liste değil: {path}")
    valid: List[Exchange] = [
        {"goal": entry["goal"], "answer": entry["answer"], "tools": [str(tool) for tool in entry["tools"]]}
        for entry in loaded
        if isinstance(entry, dict) and isinstance(entry.get("goal"), str) and isinstance(entry.get("answer"), str)
        and isinstance(entry.get("tools"), list) and all(isinstance(tool, str) for tool in entry["tools"])
    ]
    return trim_history(valid)


class ImessageBridge:
    """Eşleşmiş tek iMessage sohbetini yönetir: burst → sohbet katmanı → balonlar; işleri ajana devreder."""

    def __init__(self, transport: MessageTransport, settings: ImessageSettings, store: PersonalStore,
                 chat_clients: Dict[str, AsyncOpenAI], persona_text: str, session_id: str) -> None:
        self.transport: MessageTransport = transport
        self.settings: ImessageSettings = settings
        self.store: PersonalStore = store
        self.chat_clients: Dict[str, AsyncOpenAI] = chat_clients
        self.persona_text: str = persona_text
        self.session_id: str = session_id
        self.burst_ids: List[int] = []
        self.burst_texts: List[str] = []
        self.burst_images: List[str] = []
        self.burst_timer: Optional[asyncio.Task[None]] = None
        self.chat_lock: asyncio.Lock = asyncio.Lock()
        self.task: Optional[asyncio.Task[None]] = None
        self.task_goal: str = ""
        self.task_progress: List[str] = []
        self.stop_event: threading.Event = threading.Event()
        self.question: Optional[PendingQuestion] = None
        self.integrations: Optional[CapabilityService] = None
        self.history: List[Exchange] = load_history(imessage_history_file())
        self.closing: bool = False

    # --- gelen satırlar ---

    def _ingest(self, message: IncomingMessage) -> Optional[Tuple[int, str, List[str]]]:
        """
        İzlemedeki satırı sınıflar: kendi yansımamız teslim doğrulamasıdır; eşleşmemiş/grup mesajının içeriği
        hiçbir yere yazılmaz, yalnız imleç ilerler. Yeni kullanıcı mesajı arşivlenir ve (kimlik, metin, görseller)
        döner; yeniden oynatılan satır None.
        """
        handle: str = self.settings["handle"]
        if own_echo(message, handle):
            confirmed: Optional[int] = self.store.confirm_outgoing(
                message_text(message), message["rowid"], message["guid"], datetime.now(timezone.utc),
                CONFIRM_WINDOW_SECONDS,
            )
            if confirmed is None:
                self.store.advance_cursor(message["rowid"])
            return None
        if not accepted(message, handle):
            self.store.advance_cursor(message["rowid"])
            logging.info("iMessage: eşleşmemiş ya da grup mesajı yok sayıldı",
                         extra={"rowid": message["rowid"], "is_group": message["is_group"]})
            return None
        text: str = message_text(message)
        images: List[str] = image_paths(message)
        archived: str = text or ("[fotoğraf]" if images else "")
        if not archived:
            self.store.advance_cursor(message["rowid"])
            return None
        message_id: Optional[int] = self.store.record_incoming(
            message["rowid"], message["guid"], archived, to_utc_iso(message["created_at"]),
        )
        if message_id is None:
            return None
        return message_id, text, images

    def archive_backlog(self, message: IncomingMessage) -> None:
        """Bağlantı kopukken gelen satırı yanıtlamadan arşivler; yanıtsız son burst'ü answer_unanswered ele alır."""
        self._ingest(message)

    async def on_message(self, message: IncomingMessage) -> None:
        """Canlı izlemedeki satır: komut anında işlenir, bekleyen soruya cevap olur ya da burst'e eklenir."""
        ingested = self._ingest(message)
        if ingested is None:
            return
        message_id, text, images = ingested
        command: Optional[str] = parse_command(text) if text else None
        if command is not None:
            await self._command(command)
            return
        if text and self.question is not None and await self._answer_question(text):
            return
        self.burst_ids.append(message_id)
        if text:
            self.burst_texts.append(text)
        self.burst_images.extend(images)
        self._restart_burst_timer()

    async def answer_unanswered(self) -> None:
        """
        Açılışta ve yeniden bağlanınca: son 1 saatteki yanıtsız burst'ü (çökme sırasında gelen) bir kez yanıtlar.
        Bekleyen burst ya da süren tur varsa atlanır; onlar zaten yanıtlanacak.
        """
        if self.burst_ids or self.chat_lock.locked():
            return
        tail: List[ArchivedMessage] = self.store.unanswered_burst(
            datetime.now(timezone.utc), UNANSWERED_MAX_AGE_SECONDS,
        )
        if tail:
            await self._guarded_chat_turn([item["id"] for item in tail], [item["text"] for item in tail], [], None)

    # --- komutlar ve sorular ---

    async def _command(self, command: str) -> None:
        if command == "stop":
            if self.task is None and self.question is None:
                await self._send("şu an çalışan bir iş yok", "chat")
                return
            self.stop_event.set()
            self._cancel_question(IntegrationStopped("Kullanıcı tarafından durduruldu."))
            await self._send("tamam, durduruyorum", "chat")
            return
        midnight: datetime = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        today = self.store.activities_since(midnight)
        lines: List[str] = status_lines(self.task_goal or None, self.task_progress, len(today),
                                        sum(item["tokens"] for item in today), self.store.latency_p50())
        await self._send("\n".join(lines), "chat")

    def _cancel_question(self, error: BaseException) -> None:
        question: Optional[PendingQuestion] = self.question
        if question is not None and not question["future"].done():
            question["future"].set_exception(error)

    async def _answer_question(self, text: str) -> bool:
        """
        Bekleyen soruya cevap. Onay sorusunda evet/hayır olmayan mesaj False döner ve sohbete gider (soru [DURUM]'da
        görünmeye devam eder). Çok alanlı soruda alanlar sırayla sorulur. Soru kapanmışsa False.
        """
        question: Optional[PendingQuestion] = self.question
        if question is None or question["future"].done():
            return False
        name: str = question["names"][question["index"]]
        value: object
        if boolean_field(question["specs"][name]):
            polarity: Optional[bool] = answer_polarity(text)
            if polarity is None:
                return False
            value = polarity
        else:
            value = text
        question["answers"][name] = value
        question["index"] += 1
        if question["index"] < len(question["names"]):
            upcoming: str = question["names"][question["index"]]
            await self._send(field_prompt(upcoming, question["specs"][upcoming]), "question")
            return True
        question["future"].set_result(dict(question["answers"]))
        await self._send("tamam 👍", "chat")
        return True

    async def answer(self, title: str, fields: Dict[str, object]) -> Dict[str, object]:
        """AnswerSink: soruyu balonlarla sorar ve cevabı bekler (süre sınırını IntegrationRuntime.ask uygular)."""
        if self.question is not None:
            raise QuestionPending("Zaten kullanıcıdan cevap bekleniyor.")
        names: List[str] = [name for name in fields if not name.startswith("_")]
        if not names:
            raise ValueError(f"Cevaplanacak alan yok: {title[:120]}")
        future: asyncio.Future[Dict[str, object]] = asyncio.get_running_loop().create_future()
        self.question = {"title": title, "names": names, "specs": {name: fields[name] for name in names},
                         "index": 0, "answers": {}, "future": future}
        try:
            for bubble in question_bubbles(title, fields):
                await self._send(bubble, "question")
            if len(names) > 1:
                await self._send(field_prompt(names[0], fields[names[0]]), "question")
            return await future
        finally:
            self.question = None

    async def deliver(self, path: Path, caption: str) -> None:
        """DeliverSink: dosyayı (varsa açıklamasıyla) eşleşmiş sohbete gönderir; hata DeliveryFailed."""
        try:
            if caption.strip():
                await self._send(caption.strip(), "chat")
            await self.transport.send_file(self.settings["handle"], path)
        except ImsgError as error:
            raise DeliveryFailed(f"iMessage dosya gönderimi başarısız: {error}") from error
        self.store.record_outgoing_file(path.name, utc_iso(datetime.now(timezone.utc)))

    # --- gönderim ---

    async def _send(self, text: str, kind: str) -> None:
        """Balonu arşive 'pending' yazıp gönderir; hata ya da belirsiz sonuçta 'unconfirmed' işaretleyip yükseltir."""
        message_id: int = self.store.record_outgoing(text, kind, utc_iso(datetime.now(timezone.utc)))
        try:
            await self.transport.send_text(self.settings["handle"], text)
        except ImsgError as error:
            self.store.mark_unconfirmed(message_id)
            logging.warning("iMessage balonu gönderilemedi ya da sonucu bilinmiyor",
                            extra={"message_id": message_id, "error_type": type(error).__name__,
                                   "error": str(error)[:200]})
            raise

    # --- sohbet ---

    def _restart_burst_timer(self) -> None:
        if self.burst_timer is not None and not self.burst_timer.done():
            self.burst_timer.cancel()
        self.burst_timer = asyncio.create_task(self._flush_burst_after(self.settings["burst_quiet_seconds"]))

    async def _flush_burst_after(self, delay: float) -> None:
        await asyncio.sleep(delay)
        burst_end: float = time.monotonic()
        ids, texts, images = self.burst_ids, self.burst_texts, self.burst_images
        self.burst_ids, self.burst_texts, self.burst_images = [], [], []
        # Tur bu görevde sürer; yeni mesaj yeni zamanlayıcı kurar, bu turu iptal etmez.
        self.burst_timer = None
        await self._guarded_chat_turn(ids, texts, images, burst_end)

    async def _guarded_chat_turn(self, ids: List[int], texts: List[str], images: List[str],
                                 burst_end: Optional[float]) -> None:
        try:
            await self._chat_turn(ids, texts, images, burst_end)
        except Exception:
            # Arka plan görev sınırı: hata kök nedeniyle (traceback) loglanır, köprü sonraki mesajları işlemeye devam eder.
            logging.exception("iMessage sohbet turu başarısız", extra={"message_ids": ids})

    async def _chat_turn(self, ids: List[int], texts: List[str], images: List[str],
                         burst_end: Optional[float]) -> None:
        async with self.chat_lock:
            recent: List[ArchivedMessage] = self.store.recent_messages(chat.HISTORY_LIMIT + len(ids))
            history: List[ArchivedMessage] = [item for item in recent if item["id"] not in ids][-chat.HISTORY_LIMIT:]
            turn: str = chat.burst_turn(texts, images, self._situation())
            await self._respond(chat.history_messages(history) + [{"role": "user", "content": turn}],
                                burst_end, images, "chat")

    async def _respond(self, messages: List[Dict[str, str]], burst_end: Optional[float], images: List[str],
                       kind: str) -> None:
        """Sohbet katmanını çağırır, ilk balon gecikmesini ölçer, istenen işi başlatır; model hatasını dürüstçe söyler."""
        first_sent: List[float] = []

        async def send_bubble(bubble: str) -> None:
            await self._send(bubble, kind)
            if not first_sent:
                first_sent.append(time.monotonic())

        try:
            result: chat.ChatResult = await chat.respond(
                self.chat_clients, self.settings["chat_backend"], self._system_prompt(), messages, send_bubble,
                self._is_closing, self.session_id,
            )
        except (ModelCallFailed, FallbackNotPermitted, chat.ChatError) as error:
            logging.error("Sohbet modeli yanıt veremedi",
                          extra={"backend": self.settings["chat_backend"], "error_type": type(error).__name__,
                                 "error": str(error)[:300]})
            await self._send(f"şu an cevap veremiyorum, model hatası: {str(error)[:160]}", "chat")
            return
        if burst_end is not None and first_sent:
            latency_ms: float = (first_sent[0] - burst_end) * 1000
            self.store.record_latency(latency_ms)
            logging.info("iMessage ilk balon gecikmesi",
                         extra={"latency_ms": round(latency_ms), "backend": self.settings["chat_backend"]})
        if result["start_task"] is not None:
            if not result["bubbles"]:
                await self._send(chat.TASK_ACK, "chat")
            await self._start_task(result["start_task"], images)

    def _system_prompt(self) -> str:
        return persona.system_prompt(self.persona_text, memory_prompt_block(load_memory(str(user_memory_file()))))

    def _situation(self) -> str:
        pending: Optional[str] = self.question["title"] if self.question is not None else None
        return persona.situation_block(datetime.now().astimezone(), self.task_goal or None, self.task_progress,
                                       pending)

    def _is_closing(self) -> bool:
        return self.closing

    # --- iş ---

    async def _start_task(self, goal: str, images: List[str]) -> None:
        if self.task is not None:
            await self._send(BUSY_TEXT, "chat")
            return
        self.stop_event.clear()
        self.task_goal = goal
        self.task_progress = []
        self.task = asyncio.create_task(self._run_task(goal, images))

    async def _run_task(self, goal: str, images: List[str]) -> None:
        """İşi koşturur; bitince raporu sohbet katmanına verir. Beklenmeyen hata kullanıcıya açıkça bildirilir."""
        interim: asyncio.Task[None] = asyncio.create_task(self._interim_after(INTERIM_AFTER_SECONDS))
        outcome: Optional[delegate.TaskOutcome] = None
        failure: Optional[str] = None
        try:
            if self.integrations is None:
                self.integrations = CapabilityService()
            options = delegate.run_options(self.answer, self.deliver, self.history, images, self.stop_event.is_set,
                                           self.integrations)
            outcome = await delegate.run_task(goal, options, self._on_progress)
        except HostBusyError:
            failure = HOST_BUSY_TEXT
        except Exception as error:
            # İş sınırı: kök neden (traceback) loglanır ve kullanıcıya açıkça söylenir.
            logging.exception("iMessage işi beklenmedik hatayla bitti", extra={"goal": goal[:200]})
            failure = f"iş yarıda kaldı: {type(error).__name__}: {str(error)[:200]}"
        finally:
            interim.cancel()
            await asyncio.gather(interim, return_exceptions=True)
            self._cancel_question(IntegrationStopped("İş bitti."))
            self.task, self.task_goal, self.task_progress = None, "", []
            self.stop_event.clear()
        if failure is not None:
            await self._send(failure, "chat")
            return
        if outcome is None:
            raise RuntimeError("İş sonucu üretilmedi.")
        await self._finish_task(outcome)

    async def _finish_task(self, outcome: delegate.TaskOutcome) -> None:
        report = outcome["report"]
        self.history = trim_history(self.history + [report["exchange"]])
        save_json(imessage_history_file(), self.history)
        self.store.record_activity({
            "kind": "task", "origin": "user", "goal": outcome["goal"], "rationale": "",
            "outcome": report["outcome"][:2000], "success": report["success"], "started_at": outcome["started_at"],
            "finished_at": outcome["finished_at"], "tokens": outcome["tokens"],
        })
        async with self.chat_lock:
            history: List[ArchivedMessage] = self.store.recent_messages(chat.HISTORY_LIMIT)
            turn: str = chat.report_turn(outcome["goal"], report["success"], report["outcome"], self._situation())
            await self._respond(chat.history_messages(history) + [{"role": "user", "content": turn}],
                                None, [], "task_report")

    async def _interim_after(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
        last: str = self.task_progress[-1] if self.task_progress else "devam ediyor"
        await self._send(f"hâlâ üzerindeyim: {last[:160]}", "chat")

    def _on_progress(self, line: str) -> None:
        self.task_progress = (self.task_progress + [line])[-delegate.PROGRESS_LIMIT:]

    # --- bakım ---

    async def sweep_forever(self) -> None:
        """İzlemede görülmeyen balonları süre dolunca 'unconfirmed' yapar ve uyarır."""
        while True:
            await asyncio.sleep(SWEEP_SECONDS)
            expired: List[int] = self.store.expire_pending(datetime.now(timezone.utc), CONFIRM_WINDOW_SECONDS)
            if expired:
                logging.warning("iMessage balonları izlemede görülmedi (teslim doğrulanamadı)",
                                extra={"message_ids": expired})

    async def close(self) -> None:
        """Çalışan işi durdurur, bekleyen soruyu iptal eder, zamanlayıcıyı ve entegrasyonları kapatır."""
        self.closing = True
        self.stop_event.set()
        self._cancel_question(IntegrationStopped("Köprü kapanıyor."))
        timer: Optional[asyncio.Task[None]] = self.burst_timer
        if timer is not None:
            timer.cancel()
            await asyncio.gather(timer, return_exceptions=True)
        task: Optional[asyncio.Task[None]] = self.task
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=CLOSE_TASK_TIMEOUT_SECONDS)
            except TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if self.integrations is not None:
            await self.integrations.close()


class ImsgSession:
    """Geçerli imsg istemcisini tutar ve çöken süreci uyarıyla yeniden başlatır (dış sistem bağlayıcısı)."""

    def __init__(self, command: List[str]) -> None:
        self.command: List[str] = command
        self.client: Optional[ImsgClient] = None

    def _current(self) -> ImsgClient:
        if self.client is None:
            raise ImsgProcessError("imsg bağlantısı yeniden kuruluyor.")
        return self.client

    async def send_text(self, handle: str, text: str) -> SendResult:
        return await self._current().send_text(handle, text)

    async def send_file(self, handle: str, path: Path) -> SendResult:
        return await self._current().send_file(handle, path)

    async def listen(self, bridge: ImessageBridge) -> None:
        """
        Bağlan → kaçanları yanıtlamadan arşivle → yanıtsız son burst'ü yanıtla → canlı izle. imsg süreci düşerse
        RESTART_DELAYS_SECONDS ile yeniden dener; art arda tükenirse ImsgProcessError yükselir (launchd yeniden
        başlatır). En az STABLE_CONNECTION_SECONDS süren bağlantı sayacı sıfırlar.
        """
        failures: int = 0
        while True:
            client = ImsgClient(self.command)
            connected_at: float = time.monotonic()
            try:
                await client.start()
                self.client = client
                missed, cursor = await client.catch_up(bridge.store.cursor())
                for message in missed:
                    bridge.archive_backlog(message)
                bridge.store.advance_cursor(cursor)
                await bridge.answer_unanswered()
                async with aclosing(client.subscribe(bridge.store.cursor())) as stream:
                    async for message in stream:
                        await bridge.on_message(message)
            except ImsgProcessError as error:
                if time.monotonic() - connected_at >= STABLE_CONNECTION_SECONDS:
                    failures = 0
                failures += 1
                if failures > len(RESTART_DELAYS_SECONDS):
                    raise
                logging.warning("imsg süreci düştü; yeniden başlatılıyor",
                                extra={"attempt": failures, "error": str(error)[:300]})
                await asyncio.sleep(RESTART_DELAYS_SECONDS[failures - 1])
            finally:
                self.client = None
                await client.close()


def ensure_messages_running() -> None:
    """Messages.app'i odak çalmadan arka planda başlatır; AppleScript gönderimi uygulamayı öne getirmesin."""
    result = subprocess.run(["open", "-g", "-a", "Messages"], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise ImessageConfigError(f"Messages arka planda açılamadı: {result.stderr.strip()[:300]}")


async def _serve(store: PersonalStore) -> None:
    """Eşleşmiş köprüyü çalıştırır: ayarlar, karakter, sohbet istemcileri, imsg oturumu, uyku engeli."""
    settings: ImessageSettings = load_settings(imessage_settings_file(), BACKENDS.keys())
    apply_model_preferences()
    persona_text: str = persona.load_persona(persona_file())
    chat_clients: Dict[str, AsyncOpenAI] = await asyncio.to_thread(create_model_clients)
    session = ImsgSession(imsg_command())
    bridge = ImessageBridge(session, settings, store, chat_clients, persona_text, f"imessage-{uuid.uuid4().hex}")
    keep_awake: Optional["subprocess.Popen[bytes]"] = None
    sweeper: Optional[asyncio.Task[None]] = None
    try:
        if settings["chat_backend"] not in chat_clients:
            raise ImessageConfigError(
                f"Sohbet profili '{settings['chat_backend']}' kullanılamıyor (API anahtarı ya da Ollama yok). "
                f"Hazır profiller: {', '.join(sorted(chat_clients)) or 'yok'}"
            )
        ensure_messages_running()
        keep_awake = start_keep_awake(os.getpid())
        sweeper = asyncio.create_task(bridge.sweep_forever())
        await session.listen(bridge)
    finally:
        stop_keep_awake(keep_awake)
        if sweeper is not None:
            sweeper.cancel()
            await asyncio.gather(sweeper, return_exceptions=True)
        await bridge.close()
        await close_model_clients(chat_clients)


async def run_bridge() -> None:
    """Tek köprü sürecini çalıştırır; iki süreç aynı sohbeti iki kez yanıtlamaz."""
    with host_task_lock(data_root() / "imessage-bridge.lock"):
        store = PersonalStore(companion_db_file())
        try:
            await _serve(store)
        finally:
            store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="OmniAgent iMessage köprüsü")
    parser.add_argument("action", choices=("run",))
    parser.parse_args()
    # Arka plan servisi kabuk ortamını miras almaz: Ayarlar'da kayıtlı anahtarları uygula.
    apply_stored_api_keys()
    try:
        # launchd süreci salt okunur '/' dizininde başlatır; köprü Telegram gibi proje kökünde çalışır.
        os.chdir(project_root())
        asyncio.run(run_bridge())
    except (ImsgError, ImessageConfigError, HostBusyError, FileNotFoundError, KeyboardInterrupt) as error:
        print(f"iMessage: {error}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_imessage_bridge.py -v`
Expected: PASS (10 test)

- [ ] **Step 5: Run the full suite**

Run: `uv run python -m pytest tests/ -q`
Expected: PASS (önceki toplam + yeni testler; hata yok)

- [ ] **Step 6: Değişiklik denetimi (commit yok)**

Run: `git status --short src/omniagent/integrations/imessage.py tests/test_imessage_bridge.py`
Expected: iki yeni dosya.

---

### Task 10: Kurulum, servis içinde eşleştirme, CLI, izin raporu, kılavuz

**Files:**
- Create: `src/omniagent/integrations/imessage_setup.py`
- Modify: `src/omniagent/integrations/imessage.py` (`run_bridge` eşleştirme kipi, `main` eylemleri, iki içe aktarma)
- Modify: `src/omniagent/platform/macos/permissions.py` (Tam Disk Erişimi satırı)
- Modify: `pyproject.toml` (`[project.scripts]`)
- Create: `docs/IMESSAGE.md`
- Test: `tests/test_imessage_setup.py`

**Interfaces:**
- Consumes: Görev 1 (`DraftSettings`, `PairingRequest`, `load_pairing`, `save_pairing`, `save_settings`, `normalize_handle`, `pairing_expired`), Görev 2 (`PersonalStore`, `utc_iso`), Görev 3 (`ImsgClient`, `ImsgError`, `ImsgUnavailable`, `IncomingMessage`, `imsg_command`), Görev 4 (`message_text`), Görev 5 (`write_persona_if_missing`), Görev 6 (`CHAT_TOOLS`), Görev 8 (`launch_agent.*`), `model_runtime.stream_completion`.
- Produces: `SERVICE_LABEL = "com.omniagent.imessage"`, `PAIRING_SECONDS = 180.0`, `SERVICE_READY_SECONDS = 900.0`, `PAIRING_STATUS_KEY = "pairing_status"`, `APPROVED_*` sabitleri, TypedDict `BenchResult`, `bridge_command() -> List[str]`, `build_launchd_record() -> Dict[str, object]`, `install_service() -> None`, `new_pairing(draft, code, expires_at) -> PairingRequest`, `pairing_handle(message, request, now) -> Optional[str]`, `paired_settings(draft, handle) -> ImessageSettings`, `async pair_from_service(store) -> ImessageSettings`, `async measure_first_token(client, backend) -> float`, `async bench_backends() -> List[BenchResult]`, `async wait_for_service(store) -> None`, `async wait_for_pairing(store) -> None`, `async setup() -> None`; `permissions.MESSAGES_DATABASE`, `permissions.messages_database_status(path: Path) -> str`

- [ ] **Step 1: Write the failing test**

`tests/test_imessage_setup.py`:

```python
"""iMessage kurulumu: launchd kaydı, eşleştirme kodu kuralı, servis içinde eşleştirme (gerçek istemci + sahte imsg),
Tam Disk Erişimi durumu."""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List

import pytest

from omniagent.integrations import imessage_setup
from omniagent.integrations.imessage_settings import DraftSettings, load_settings, save_pairing
from omniagent.integrations.imsg import ImsgUnavailable, IncomingMessage
from omniagent.memory.personal import PersonalStore
from omniagent.platform.macos.permissions import messages_database_status

FAKE = Path(__file__).parent / "fixtures" / "fake_imsg.py"
USER = "+905551112233"


def draft() -> DraftSettings:
    return {"persona_name": "Deniz", "chat_backend": "openai", "memory_backend": "opencode",
            "quiet_hours": {"start": "23:30", "end": "09:00"}, "burst_quiet_seconds": 2.0,
            "gui_idle_seconds": 180, "heartbeat_minutes": {"base": 30, "jitter": 10, "min": 20, "max": 240}}


def code_message(rowid: int, text: str) -> Dict[str, object]:
    return {"id": rowid, "guid": f"g{rowid}", "chat_id": 3, "sender": "+90 555 111 22 33", "participants": [USER],
            "is_group": False, "is_from_me": False, "text": text, "created_at": "2026-09-29T12:00:00Z",
            "attachments": []}


def write_scenario(tmp_path: Path, scenario: Dict[str, object]) -> List[str]:
    path = tmp_path / "scenario.json"
    base: Dict[str, object] = {"log_path": str(tmp_path / "requests.jsonl"), "status": {"database": {"ready": True}}}
    path.write_text(json.dumps({**base, **scenario}), encoding="utf-8")
    return [sys.executable, str(FAKE), str(path)]


def test_launchd_record_runs_bridge_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    record = imessage_setup.build_launchd_record()
    assert record["Label"] == "com.omniagent.imessage" and record["KeepAlive"] is True
    assert record["ProgramArguments"] == [sys.executable, "-m", "omniagent.integrations.imessage", "run"]
    assert record["StandardErrorPath"] == str(tmp_path / "imessage-stderr.log")


def test_pairing_handle_accepts_only_live_code_in_direct_chat() -> None:
    now = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
    request = imessage_setup.new_pairing(draft(), "042917", now + timedelta(seconds=180))
    message: IncomingMessage = {"rowid": 1, "guid": "g1", "chat_id": 3, "sender": "+90 555 111 22 33",
                                "participants": [USER], "is_from_me": False, "is_group": False, "text": " 042917 ",
                                "created_at": "2026-09-29T12:00:00Z", "attachments": []}
    assert imessage_setup.pairing_handle(message, request, now) == USER
    assert imessage_setup.pairing_handle({**message, "text": "042918"}, request, now) is None
    assert imessage_setup.pairing_handle({**message, "is_group": True}, request, now) is None
    assert imessage_setup.pairing_handle(message, request, now + timedelta(seconds=181)) is None


@pytest.mark.asyncio
async def test_service_pairs_code_sender_and_greets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    command = write_scenario(tmp_path, {"subscribe_batches": [[
        {"method": "message", "params": {"message": code_message(40, "999999")}},
        {"method": "message", "params": {"message": code_message(41, "042917")}},
    ]]})
    monkeypatch.setattr(imessage_setup, "imsg_command", lambda: command)
    save_pairing(tmp_path / "imessage-pairing.json",
                 imessage_setup.new_pairing(draft(), "042917", datetime.now(timezone.utc) + timedelta(seconds=180)))
    store = PersonalStore(tmp_path / "companion.db")
    try:
        settings = await imessage_setup.pair_from_service(store)
        assert settings["handle"] == USER
        assert load_settings(tmp_path / "imessage.json", ("openai", "opencode"))["handle"] == USER
        assert not (tmp_path / "imessage-pairing.json").exists()
        assert store.get_state(imessage_setup.PAIRING_STATUS_KEY) == "paired" and store.cursor() == 41
    finally:
        store.close()
    requests = [json.loads(line) for line in (tmp_path / "requests.jsonl").read_text(encoding="utf-8").splitlines()]
    sends = [request["params"] for request in requests if request["method"] == "send"]
    assert len(sends) == 1 and sends[0]["to"] == USER and sends[0]["text"].startswith("eşleştik")


@pytest.mark.asyncio
async def test_service_reports_unreadable_database_to_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    command = write_scenario(tmp_path, {"status": {"database": {"ready": False, "error": "authorization denied"}}})
    monkeypatch.setattr(imessage_setup, "imsg_command", lambda: command)
    save_pairing(tmp_path / "imessage-pairing.json",
                 imessage_setup.new_pairing(draft(), "042917", datetime.now(timezone.utc) + timedelta(seconds=180)))
    store = PersonalStore(tmp_path / "companion.db")
    try:
        with pytest.raises(ImsgUnavailable):
            await imessage_setup.pair_from_service(store)
        assert (store.get_state(imessage_setup.PAIRING_STATUS_KEY) or "").startswith("error:")
    finally:
        store.close()


def test_messages_database_status(tmp_path: Path) -> None:
    database = tmp_path / "chat.db"
    assert "yok" in messages_database_status(database)
    database.write_bytes(b"SQLite format 3")
    assert messages_database_status(database) == "izinli"
    database.chmod(0)
    try:
        assert "İZİN YOK" in messages_database_status(database)
    finally:
        database.chmod(0o600)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_imessage_setup.py -v`
Expected: FAIL — `ImportError: cannot import name 'imessage_setup' from 'omniagent.integrations'`

- [ ] **Step 3: Write the setup module**

`src/omniagent/integrations/imessage_setup.py`:

```python
"""iMessage kurulumu: karakter, model ölçümü, servis kurulumu ve servisin içinde eşleştirme.

TCC izinleri (Full Disk Access, Messages için Automation) sorumlu sürece verilir: Terminal'den çalışan komutta
Terminal'e, LaunchAgent altında python ikilisine (bkz. platform/macos/permissions.py). Bu yüzden eşleştirme kodunu
izlemek ve ilk mesajı göndermek terminalde değil servisin kendisinde yapılır; kurulum komutu servisin companion.db'ye
yazdığı durumu izler.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import subprocess
import sys
import time
from contextlib import aclosing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, TypedDict

from openai import APIError, AsyncOpenAI

from omniagent.app.agent import close_model_clients, create_model_clients
from omniagent.app.model_runtime import stream_completion
from omniagent.companion.chat import CHAT_TOOLS
from omniagent.companion.persona import write_persona_if_missing
from omniagent.config import BACKENDS, apply_model_preferences
from omniagent.core.events import AgentEvent
from omniagent.integrations.imessage_rules import message_text
from omniagent.integrations.imessage_settings import (
    DraftSettings, HeartbeatMinutes, ImessageConfigError, ImessageSettings, PairingRequest, QuietHours,
    load_pairing, normalize_handle, pairing_expired, save_pairing, save_settings,
)
from omniagent.integrations.imsg import ImsgClient, ImsgError, ImsgUnavailable, IncomingMessage, imsg_command
from omniagent.memory.personal import PersonalStore, utc_iso
from omniagent.paths import companion_db_file, data_root, imessage_pairing_file, imessage_settings_file, persona_file
from omniagent.platform.macos import launch_agent

SERVICE_LABEL: str = "com.omniagent.imessage"
PAIRING_SECONDS: float = 180.0
# Tam Disk Erişimi'nin verilmesi dahil servisin hazır olmasını bekleme üst sınırı
SERVICE_READY_SECONDS: float = 900.0
SETUP_POLL_SECONDS: float = 1.0
PAIRING_STATUS_KEY: str = "pairing_status"
FULL_DISK_SETTINGS_URL: str = "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"
BENCH_PROMPT: str = "selam, naber? tek kısa cümleyle cevap ver."
# Tasarım konuşmasında onaylanan değerler (spec: "Yapılandırma ve dosyalar")
APPROVED_QUIET_HOURS: QuietHours = {"start": "23:30", "end": "09:00"}
APPROVED_BURST_QUIET_SECONDS: float = 2.0
APPROVED_GUI_IDLE_SECONDS: int = 180
APPROVED_HEARTBEAT_MINUTES: HeartbeatMinutes = {"base": 30, "jitter": 10, "min": 20, "max": 240}


class BenchResult(TypedDict):
    backend: str
    seconds: Optional[float]
    error: str


def bridge_command() -> List[str]:
    """Köprüyü bu yorumlayıcıyla çalıştıran komut (launchd kaydı)."""
    return [sys.executable, "-m", "omniagent.integrations.imessage", "run"]


def build_launchd_record() -> Dict[str, object]:
    """iMessage köprüsünün sürekli çalışan LaunchAgent kaydı."""
    root: Path = data_root()
    return launch_agent.launchd_record(SERVICE_LABEL, bridge_command(), root / "imessage-stdout.log",
                                       root / "imessage-stderr.log")


def install_service() -> None:
    """LaunchAgent'i idempotent kurar/yeniler; eşleşme yoksa servis eşleştirme kipinde açılır."""
    data_root().mkdir(parents=True, exist_ok=True)
    path: Path = launch_agent.plist_path(SERVICE_LABEL)
    launch_agent.install(SERVICE_LABEL, path, build_launchd_record(), "iMessage")
    print(f"iMessage hizmeti kuruldu/güncellendi: {path}")


def new_pairing(draft: DraftSettings, code: str, expires_at: datetime) -> PairingRequest:
    """Eşleştirme isteği. Saf."""
    return {"code": code, "expires_at": utc_iso(expires_at), "draft": draft}


def pairing_handle(message: IncomingMessage, request: PairingRequest, now: datetime) -> Optional[str]:
    """Mesaj, süresi dolmamış kodu birebir sohbette taşıyorsa gönderenin normalize adresi; değilse None. Saf."""
    if message["is_from_me"] or message["is_group"] or pairing_expired(request, now):
        return None
    if message_text(message) != request["code"]:
        return None
    try:
        return normalize_handle(message["sender"])
    except ImessageConfigError:
        return None


def paired_settings(draft: DraftSettings, handle: str) -> ImessageSettings:
    """Taslak ayarlar + eşleşen adres. Saf."""
    return {
        "persona_name": draft["persona_name"], "chat_backend": draft["chat_backend"],
        "memory_backend": draft["memory_backend"], "quiet_hours": draft["quiet_hours"],
        "burst_quiet_seconds": draft["burst_quiet_seconds"], "gui_idle_seconds": draft["gui_idle_seconds"],
        "heartbeat_minutes": draft["heartbeat_minutes"], "handle": handle,
    }


def _report_failure(store: PersonalStore, error: Exception) -> None:
    """Servis tarafı hatayı kurulum komutunun okuyacağı duruma yazar."""
    store.set_state(PAIRING_STATUS_KEY, f"error: {error}")


async def pair_from_service(store: PersonalStore) -> ImessageSettings:
    """
    Servis tarafı eşleştirme: bekleyen istekteki kodu birebir sohbette gönderen adresi eşleştirir. Önce karşılama
    mesajı gönderilir (Messages için Automation izni bu gönderimde servis ikilisine sorulur); başarılıysa ayarlar
    yazılır, imleç kod satırına kurulur ve istek silinir. imsg bulunamaz, veritabanı okunamaz ya da gönderim
    reddedilirse durum kurulum komutuna bildirilir ve hata yükselir (launchd servisi yeniden başlatır).
    """
    request: Optional[PairingRequest] = load_pairing(imessage_pairing_file(), BACKENDS.keys())
    if request is None:
        raise ImessageConfigError(
            "iMessage eşleşmesi ve bekleyen eşleştirme isteği yok: 'omniagent-imessage setup' çalıştırın."
        )
    try:
        command: List[str] = imsg_command()
    except ImsgUnavailable as error:
        _report_failure(store, error)
        raise
    client = ImsgClient(command)
    try:
        try:
            await client.start()
        except ImsgUnavailable as error:
            _report_failure(store, error)
            raise
        store.set_state(PAIRING_STATUS_KEY, "waiting")
        async with aclosing(client.subscribe(0)) as stream:
            async for message in stream:
                current: Optional[PairingRequest] = load_pairing(imessage_pairing_file(), BACKENDS.keys())
                if current is None:
                    raise ImessageConfigError("Eşleştirme isteği kurulum sırasında silindi.")
                handle: Optional[str] = pairing_handle(message, current, datetime.now(timezone.utc))
                if handle is None:
                    continue
                settings: ImessageSettings = paired_settings(current["draft"], handle)
                try:
                    await client.send_text(
                        handle, f"eşleştik 👋 ben {settings['persona_name']}. beni rehbere kaydet, sonra yazışalım",
                    )
                except ImsgError as error:
                    _report_failure(store, ImessageConfigError(
                        f"Messages'a mesaj gönderilemedi ({error}). Sistem Ayarları > Gizlilik ve Güvenlik > "
                        "Otomasyon'da python için Messages iznini açıp kodu yeniden gönderin."
                    ))
                    raise
                save_settings(imessage_settings_file(), settings)
                store.advance_cursor(message["rowid"])
                imessage_pairing_file().unlink()
                store.set_state(PAIRING_STATUS_KEY, "paired")
                return settings
    finally:
        await client.close()
    raise ImessageConfigError("imsg izleme akışı eşleşmeden kapandı.")


async def measure_first_token(client: AsyncOpenAI, backend: str) -> float:
    """Sabit Türkçe istemle ilk metin parçasına kadar geçen süre (sn); metin gelmezse turun sonuna kadar."""
    first: List[float] = []
    started: float = time.monotonic()

    def emit(event: AgentEvent) -> None:
        if event["kind"] == "text_delta" and not first:
            first.append(time.monotonic())

    await stream_completion(client, BACKENDS[backend], [{"role": "user", "content": BENCH_PROMPT}], CHAT_TOOLS,
                            f"imessage-bench-{backend}", emit, lambda: False)
    return (first[0] if first else time.monotonic()) - started


async def bench_backends() -> List[BenchResult]:
    """Kullanılabilir her profilin ilk token süresi, hızlıdan yavaşa; hata veren profil hatasıyla sonda."""
    apply_model_preferences()
    clients: Dict[str, AsyncOpenAI] = await asyncio.to_thread(create_model_clients)
    results: List[BenchResult] = []
    try:
        for backend, client in sorted(clients.items()):
            try:
                results.append({"backend": backend, "seconds": await measure_first_token(client, backend), "error": ""})
            except (APIError, TimeoutError) as error:
                results.append({"backend": backend, "seconds": None, "error": f"{type(error).__name__}: {error}"})
    finally:
        await close_model_clients(clients)
    return sorted(results, key=lambda item: (item["seconds"] is None, item["seconds"] or 0.0))


def ask_text(prompt: str) -> str:
    """Boş olmayan cevap alana dek sorar."""
    while True:
        answer: str = input(prompt).strip()
        if answer:
            return answer


def ask_choice(prompt: str, allowed: Sequence[str]) -> str:
    """Listedeki bir cevap alana dek sorar."""
    while True:
        answer: str = input(f"{prompt} [{', '.join(allowed)}]: ").strip()
        if answer in allowed:
            return answer
        print(f"Geçerli seçenekler: {', '.join(allowed)}")


def open_full_disk_settings() -> None:
    """Tam Disk Erişimi ayar bölmesini açar; açılamazsa uyarır (kullanıcı elle açabilir)."""
    result = subprocess.run(["open", FULL_DISK_SETTINGS_URL], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        logging.warning("Tam Disk Erişimi ayarları açılamadı", extra={"stderr": result.stderr.strip()[:200]})


async def wait_for_service(store: PersonalStore) -> None:
    """Servis 'waiting' bildirene kadar bekler; her yeni hatayı bir kez gösterir (veritabanı hatasında ayarı açar)."""
    deadline: float = time.monotonic() + SERVICE_READY_SECONDS
    reported: Optional[str] = None
    while time.monotonic() < deadline:
        status: Optional[str] = store.get_state(PAIRING_STATUS_KEY)
        if status == "waiting":
            return
        if status is not None and status.startswith("error:") and status != reported:
            reported = status
            print(f"\nServis hazır değil: {status.removeprefix('error:').strip()}")
            if "Full Disk Access" in status:
                print("Sistem Ayarları > Gizlilik ve Güvenlik > Tam Disk Erişimi'nde yukarıdaki python ikilisini "
                      "ekleyip açın; servis kendiliğinden yeniden dener.")
                open_full_disk_settings()
        await asyncio.sleep(SETUP_POLL_SECONDS)
    raise ImessageConfigError(
        f"Servis {SERVICE_READY_SECONDS / 60:.0f} dk içinde hazır olmadı; günlük: {data_root() / 'imessage-stderr.log'}"
    )


async def wait_for_pairing(store: PersonalStore) -> None:
    """Servisin kodu görüp eşleşmeyi yazmasını bekler; servis hatası ya da süre dolması açık hatadır."""
    deadline: float = time.monotonic() + PAIRING_SECONDS
    while time.monotonic() < deadline:
        status: Optional[str] = store.get_state(PAIRING_STATUS_KEY)
        if status == "paired" and imessage_settings_file().exists():
            return
        if status is not None and status.startswith("error:"):
            raise ImessageConfigError(f"Eşleştirme başarısız: {status.removeprefix('error:').strip()}")
        await asyncio.sleep(SETUP_POLL_SECONDS)
    raise ImessageConfigError("Kod 3 dakika içinde gelmedi. 'omniagent-imessage setup' komutunu yeniden çalıştırın.")


async def setup() -> None:
    """Etkileşimli kurulum: karakter, model seçimi, servis kurulumu, servisin içinde eşleştirme."""
    if imessage_settings_file().exists():
        raise ImessageConfigError(
            f"Zaten eşleşmiş. Yeniden eşleştirmek için önce şu dosyayı silin: {imessage_settings_file()}"
        )
    print(f"imsg: {imsg_command()[0]}")
    name: str = ask_text("Ajanın adı (iMessage'da görünecek karakter): ")
    if write_persona_if_missing(persona_file(), name):
        print(f"Karakter dosyası yazıldı: {persona_file()} (istediğin gibi düzenleyebilirsin)")
    else:
        print(f"Mevcut karakter dosyası korunuyor: {persona_file()}")
    print("Model profilleri ölçülüyor (ilk token süresi)…")
    results: List[BenchResult] = await bench_backends()
    for result in results:
        shown: str = (f"{result['seconds']:.2f} sn" if result["seconds"] is not None
                      else f"hata: {result['error'][:120]}")
        print(f"  {result['backend']:<16} {shown}")
    usable: List[str] = [result["backend"] for result in results if result["seconds"] is not None]
    if not usable:
        raise ImessageConfigError("Hiçbir model profili yanıt vermedi; API anahtarlarını Ayarlar'dan kontrol edin.")
    draft: DraftSettings = {
        "persona_name": name,
        "chat_backend": ask_choice("Sohbet profili (en hızlısı tablonun en üstünde)", usable),
        "memory_backend": ask_choice("Hafıza/doğrulama profili", usable),
        "quiet_hours": APPROVED_QUIET_HOURS,
        "burst_quiet_seconds": APPROVED_BURST_QUIET_SECONDS,
        "gui_idle_seconds": APPROVED_GUI_IDLE_SECONDS,
        "heartbeat_minutes": APPROVED_HEARTBEAT_MINUTES,
    }
    code: str = f"{secrets.randbelow(10 ** 6):06d}"
    store = PersonalStore(companion_db_file())
    try:
        store.set_state(PAIRING_STATUS_KEY, "starting")
        save_pairing(imessage_pairing_file(),
                     new_pairing(draft, code, datetime.now(timezone.utc) + timedelta(seconds=SERVICE_READY_SECONDS)))
        install_service()
        await wait_for_service(store)
        save_pairing(imessage_pairing_file(),
                     new_pairing(draft, code, datetime.now(timezone.utc) + timedelta(seconds=PAIRING_SECONDS)))
        print(f"\niPhone'dan ajanın Apple ID adresine şu kodu gönder: {code}  (3 dakika)")
        await wait_for_pairing(store)
    finally:
        store.close()
    print("Eşleşme tamam: ajan sana 'eşleştik' yazdı. iPhone'da onu isim ve fotoğrafla rehbere kaydet.")
```

- [ ] **Step 4: Wire pairing mode and the CLI actions into the bridge**

`src/omniagent/integrations/imessage.py`:
1. İçe aktarmalara ekle: `from omniagent.integrations import imessage_setup` ve `from omniagent.platform.macos.launch_agent import LaunchAgentError`.
2. `run_bridge`'i şununla değiştir:
   ```python
   async def run_bridge() -> None:
       """Tek köprü sürecini çalıştırır; eşleşme yoksa önce servisin içinde eşleştirir."""
       with host_task_lock(data_root() / "imessage-bridge.lock"):
           store = PersonalStore(companion_db_file())
           try:
               if not imessage_settings_file().exists():
                   await imessage_setup.pair_from_service(store)
               await _serve(store)
           finally:
               store.close()
   ```
3. `main`'i şununla değiştir:
   ```python
   def main() -> None:
       parser = argparse.ArgumentParser(description="OmniAgent iMessage köprüsü")
       parser.add_argument("action", choices=("setup", "run", "install-service"))
       arguments = parser.parse_args()
       # Arka plan servisi kabuk ortamını miras almaz: Ayarlar'da kayıtlı anahtarları uygula.
       apply_stored_api_keys()
       try:
           if arguments.action == "setup":
               asyncio.run(imessage_setup.setup())
           elif arguments.action == "install-service":
               imessage_setup.install_service()
           else:
               # launchd süreci salt okunur '/' dizininde başlatır; köprü Telegram gibi proje kökünde çalışır.
               os.chdir(project_root())
               asyncio.run(run_bridge())
       except (ImsgError, ImessageConfigError, HostBusyError, LaunchAgentError, FileNotFoundError,
               KeyboardInterrupt) as error:
           print(f"iMessage: {error}", file=sys.stderr)
           raise SystemExit(1) from None
   ```

- [ ] **Step 5: Report Full Disk Access in the permissions tool**

`src/omniagent/platform/macos/permissions.py`:
1. İçe aktarmalara `from pathlib import Path` ekle.
2. `accessibility_granted` fonksiyonunun ardına ekle:
   ```python
   MESSAGES_DATABASE: Path = Path.home() / "Library" / "Messages" / "chat.db"


   def messages_database_status(path: Path) -> str:
       """iMessage köprüsünün okuduğu Messages veritabanı bu süreçten okunabiliyor mu (Tam Disk Erişimi)?"""
       try:
           with path.open("rb") as database:
               database.read(1)
       except FileNotFoundError:
           return "veritabanı yok (Messages bu kullanıcıda hiç açılmamış)"
       except PermissionError:
           return "İZİN YOK (Tam Disk Erişimi: iMessage köprüsü için gerekli)"
       return "izinli"
   ```
3. `report()` içindeki `lines` listesinde `Erişilebilirlik` satırının hemen ardına ekle:
   `f"Tam Disk Erişimi: {messages_database_status(MESSAGES_DATABASE)}",`

- [ ] **Step 6: Register the CLI entry point**

`pyproject.toml` `[project.scripts]` içinde `omniagent-telegram` satırının ardına ekle:

```toml
omniagent-imessage = "omniagent.integrations.imessage:main"
```

Run: `uv sync && uv run omniagent-imessage --help`
Expected: `usage: omniagent-imessage [-h] {setup,run,install-service}`

- [ ] **Step 7: Write the setup guide and live checklist**

`docs/IMESSAGE.md`:

```markdown
# iMessage yol arkadaşı — kurulum ve canlı kontrol

OmniAgent'ın iMessage kanalı: ajanın kendi Apple ID'si bu Mac'teki Messages'ta oturum açar, sen iPhone'dan ona
yazarsın. Tasarım: `docs/superpowers/specs/2026-09-29-imessage-companion-design.md`.

## Kurulum

1. **Ajana Apple ID aç** (appleid.apple.com). Ajanın iMessage adresi bu e-posta olur.
2. **Bu Mac'te Messages:** Messages > Ayarlar > iMessage → kendi hesabından çık, ajanın Apple ID'siyle gir.
   Kendi iMessage'ın iPhone'da aynen sürer.
3. **imsg:** `brew install steipete/tap/imsg`
4. **Kurulum komutu** (proje kökünde): `uv run omniagent-imessage setup`
   - karakter adını sorar ve `persona.md`'yi yazar (sonradan düzenleyebilirsin),
   - model profillerinin ilk token süresini ölçer; sohbet ve hafıza profilini seçtirir,
   - servisi (`com.omniagent.imessage`) kurar.
5. **Tam Disk Erişimi:** kurulum, servisin python ikilisinin yolunu yazar ve ayar sayfasını açar. Sistem Ayarları >
   Gizlilik ve Güvenlik > Tam Disk Erişimi > "+" → Cmd+Shift+G ile o yolu yapıştır → aç. Servis kendiliğinden
   yeniden dener.
6. **Eşleştirme:** kurulumun gösterdiği 6 haneli kodu iPhone'dan ajanın adresine gönder (3 dk). "python …
   Messages'ı denetlemek istiyor" istemini onayla (Otomasyon). Ajan "eşleştik 👋" yazar.
7. **Rehber:** iPhone'da ajanı isim ve fotoğrafla kişilere kaydet (iOS bilinmeyen gönderenleri süzer).

iPhone'daki "Yeni konuşmaları şuradan başlat" adresini (telefon ↔ e-posta) değiştirirsen yeniden eşleştir:
`imessage.json`'u sil ve kurulumu tekrar çalıştır.

## Komutlar

- `dur` ya da `/dur`: çalışan işi durdurur.
- `/durum`: çalışan iş, bugünkü işler ve token, cevap gecikmesi medyanı.

## Faz A canlı kontrol listesi

- [ ] `uv run omniagent-permissions` → "Tam Disk Erişimi: izinli" (servisin python ikilisi için).
- [ ] 30 kısa mesajdan sonra `/durum` gecikme medyanı ≤ 3 sn (spec ölçüt 1); p95 için
      `imessage-stderr.log`'daki "iMessage ilk balon gecikmesi" kayıtları ≤ 6 sn.
- [ ] Sen Mac'te başka bir uygulamada yazarken ajan cevap verdiğinde odak kaymıyor, Messages öne gelmiyor
      (ölçüt 6). Tutmazsa bu bir engeldir: uygulama durur ve kullanıcıya danışılır.
- [ ] "masaüstümdeki dosyaları listele" → "tamam bakıyorum" ve sonuç balonları; onay isteyen bir işte evet/hayır.
- [ ] Uzun bir işte `dur` işi durduruyor.
- [ ] Mesaj yazarken `launchctl kickstart -k gui/$(id -u)/com.omniagent.imessage` → mesaj kaybolmuyor, iki kez
      cevaplanmıyor (ölçüt 4).
- [ ] Başka bir numaradan ajana yazınca cevap yok ve `companion.db`'de iz yok (ölçüt 2).
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_imessage_setup.py tests/test_imessage_bridge.py -q && uv run python -m pytest tests -q -k permission`
Expected: PASS (tümü; izin raporu testleri yeni satırla da geçer)

- [ ] **Step 9: Değişiklik denetimi (commit yok)**

Run: `git --no-pager diff --stat -- pyproject.toml src/omniagent/platform/macos/permissions.py && git status --short src/omniagent/integrations/imessage_setup.py docs/IMESSAGE.md tests/test_imessage_setup.py`
Expected: `pyproject.toml`'da tek satır; `permissions.py`'de yalnız yeni fonksiyon, sabit, içe aktarma ve rapor satırı.

---

### Task 11: Kanal-genel metinler, canlı duman testleri, tam paket

**Files:**
- Modify: `src/omniagent/app/tool_schema.py` (~satır 205 docstring, ~488-489 `send_file` açıklaması)
- Modify: `src/omniagent/config.py` (~satır 393)
- Modify: `src/omniagent/tools/facade.py` (~satır 501, 506, 518)
- Create: `tests/test_companion_live.py`

**Interfaces:**
- Consumes: `chat.respond`, `persona.system_prompt`, `persona.persona_text_for`, `persona.situation_block`, `chat.burst_turn`, `agent.create_model_clients`, `config.apply_stored_api_keys`.
- Produces: yok (metin düzeltmeleri + isteğe bağlı canlı testler).

- [ ] **Step 1: Generalize the Telegram-only texts**

Değişiklikleri birebir yap (yalnız "Telegram" geçen ve iMessage koşusunda yanlış olacak metinler; `schedule_task` metinleri Telegram'da kalır çünkü zamanlayıcıyı yalnız Telegram köprüsü çalıştırır):
1. `app/tool_schema.py` docstring: `can_send_files: görevin dosya teslim kanalı (Telegram sohbeti) var; send_file yalnız o zaman görünür.` → `can_send_files: görevin dosya teslim kanalı (Telegram ya da iMessage sohbeti) var; send_file yalnız o zaman görünür.`
2. `app/tool_schema.py` `send_file` açıklaması: `"Bilgisayardaki dosyayı (belge, görsel, rapor, arşiv; en çok 50 MB) kullanıcının Telegram "` + `"sohbetine gönderir. ..."` → `"Bilgisayardaki dosyayı (belge, görsel, rapor, arşiv; en çok 50 MB) kullanıcının mesaj kanalına "` + `"(Telegram ya da iMessage sohbeti) gönderir. Kullanıcı dosyayı istediğinde veya sonucu dosya olarak ürettiğinde kullan."`
3. `config.py`: `- The Telegram bridge is a local OmniAgent process on this Mac, not a generic cloud bot.` → `- The Telegram and iMessage bridges are local OmniAgent processes on this Mac, not generic cloud bots.`
4. `tools/facade.py` `send_file` docstring: `(Telegram sohbeti)` → `(Telegram ya da iMessage sohbeti)`; hata metni `(yalnız Telegram görevinde)` → `(yalnız Telegram ya da iMessage görevinde)`; boyut hatası `Telegram sınırı` → `gönderim sınırı`.

Run: `rg -n "Telegram" src/omniagent/app/tool_schema.py src/omniagent/config.py src/omniagent/tools/facade.py`
Expected: kalan eşleşmeler yalnız zamanlama (`schedule_task`, `can_schedule`, SCHEDULING_GUIDANCE) ve `[Telegram eki ...]` satırları.

- [ ] **Step 2: Write the opt-in live smoke tests**

`tests/test_companion_live.py`:

```python
"""Canlı sohbet katmanı duman testleri: gerçek model çağrısı. Yalnız OMNI_LIVE_COMPANION=1 ve
OMNI_LIVE_CHAT_BACKEND=<profil> verildiğinde çalışır (CI'da atlanır; anahtarlar Keychain'den okunur)."""
import os
from datetime import datetime
from typing import List, Tuple

import pytest

from omniagent.app.agent import close_model_clients, create_model_clients
from omniagent.companion import chat, persona
from omniagent.config import apply_stored_api_keys

pytestmark = pytest.mark.skipif(
    os.environ.get("OMNI_LIVE_COMPANION") != "1",
    reason="canlı model testi: OMNI_LIVE_COMPANION=1 ve OMNI_LIVE_CHAT_BACKEND gerekir",
)


async def live_turn(text: str) -> Tuple[chat.ChatResult, List[str]]:
    apply_stored_api_keys()
    backend: str = os.environ["OMNI_LIVE_CHAT_BACKEND"]
    clients = create_model_clients()
    sent: List[str] = []

    async def send(bubble: str) -> None:
        sent.append(bubble)

    try:
        system = persona.system_prompt(persona.persona_text_for("Deniz"), "")
        situation = persona.situation_block(datetime.now().astimezone(), None, [], None)
        result = await chat.respond(clients, backend, system,
                                    [{"role": "user", "content": chat.burst_turn([text], [], situation)}],
                                    send, lambda: False, "live-test")
    finally:
        await close_model_clients(clients)
    return result, sent


@pytest.mark.asyncio
async def test_greeting_is_short_casual_and_not_a_task() -> None:
    result, sent = await live_turn("selaam naber")
    assert 1 <= len(sent) <= 4 and result["start_task"] is None
    assert not any(marker in bubble for bubble in sent for marker in ("**", "# ", "- "))


@pytest.mark.asyncio
async def test_computer_request_is_delegated_with_a_self_contained_goal() -> None:
    result, sent = await live_turn("masaüstümdeki dosyaları listeler misin")
    assert result["start_task"] is not None and len(result["start_task"]) > 10
    assert len(sent) <= 2
```

- [ ] **Step 3: Run the whole suite**

Run: `uv run python -m pytest tests/ -q`
Expected: PASS; canlı testler `skipped` (2).

- [ ] **Step 4: Run the live smoke tests once (API anahtarı olan makinede)**

Run: `OMNI_LIVE_COMPANION=1 OMNI_LIVE_CHAT_BACKEND=<kurulumda seçilen profil> uv run python -m pytest tests/test_companion_live.py -v`
Expected: PASS (2). Başarısızsa sohbet katmanı istemini (Görev 5 `RULES`) düzelt, profil değiştirmeden önce kullanıcıya bildir.

- [ ] **Step 5: Değişiklik denetimi ve teslim (commit yok)**

Run: `git status --short && git --no-pager diff --stat`
Expected: yalnız bu planın dosyaları ve kullanıcının önceden var olan commit edilmemiş değişiklikleri. Kullanıcıya: değişen/eklenen dosya listesi, test sonucu ve `docs/IMESSAGE.md` canlı kontrol listesi (Apple ID, Messages oturumu, `imsg` kurulumu ve eşleştirme kullanıcı eylemi gerektirir).
