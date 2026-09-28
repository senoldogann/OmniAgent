"""Masaüstü görünüm tercihlerini doğrular ve kullanıcı veri dizininde saklar."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping, TypedDict

from omniagent.integrations.runtime import save_json
from omniagent.paths import data_root


class AppearanceSettings(TypedDict):
    font_family: str
    font_size: int
    transparent_window: bool
    opacity: float
    compact_sidebar: bool


def appearance_path() -> Path:
    return data_root() / "appearance.json"


def default_appearance() -> AppearanceSettings:
    return {
        "font_family": "",
        "font_size": 13,
        "transparent_window": False,
        "opacity": 0.90,
        "compact_sidebar": False,
    }


def validate_appearance(values: Mapping[str, object]) -> AppearanceSettings:
    """Görünüm değerlerini güvenli ve okunabilir sınırlar içinde tutar."""
    family = values.get("font_family")
    size = values.get("font_size")
    transparent = values.get("transparent_window")
    opacity = values.get("opacity")
    compact = values.get("compact_sidebar")
    if not isinstance(family, str) or len(family) > 80 or any(ord(char) < 32 for char in family):
        raise ValueError("Yazı tipi ailesi geçersiz.")
    if isinstance(size, bool) or not isinstance(size, int) or not 11 <= size <= 18:
        raise ValueError("Yazı boyutu 11 ile 18 arasında olmalı.")
    if not isinstance(transparent, bool) or not isinstance(compact, bool):
        raise ValueError("Görünüm anahtarları geçersiz.")
    if isinstance(opacity, bool) or not isinstance(opacity, (int, float)) or not 0.65 <= float(opacity) <= 1.0:
        raise ValueError("Opaklık %65 ile %100 arasında olmalı.")
    return {
        "font_family": family.strip(),
        "font_size": size,
        "transparent_window": transparent,
        "opacity": round(float(opacity), 2),
        "compact_sidebar": compact,
    }


def load_appearance(path: Path | None = None) -> AppearanceSettings:
    source = path or appearance_path()
    if not source.exists():
        return default_appearance()
    raw: object = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Görünüm kaydı JSON nesnesi olmalı.")
    return validate_appearance(raw)


def save_appearance(settings: AppearanceSettings, path: Path | None = None) -> None:
    save_json(path or appearance_path(), validate_appearance(settings))
