"""iMessage ayar sözleşmesi: zorunlu alanlar, handle normalizasyonu, eşleştirme süresi, korumalı dosyalar."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict

import pytest

from omniagent import approval
from omniagent.integrations.imessage_settings import (
    ImessageConfigError, load_settings, memory_backend_if_paired, normalize_handle, pairing_expired, parse_pairing,
    parse_settings,
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


def test_memory_backend_is_known_only_when_imessage_is_paired(tmp_path: Path) -> None:
    path = tmp_path / "imessage.json"
    assert memory_backend_if_paired(path, BACKENDS) is None
    save_settings(path, parse_settings(valid_settings(), BACKENDS))
    assert memory_backend_if_paired(path, BACKENDS) == "opencode"
