"""Sağlayıcı model kataloğu ve kullanıcı model tercihleri."""

from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlsplit, urlunsplit

import httpx

from integration_runtime import data_root, read_json, save_json


PROFILES: frozenset[str] = frozenset(
    ("ollama-cloud", "openai", "opencode", "opencode-think", "openrouter")
)
MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+@-]{0,159}$")
CACHE_SECONDS = 15 * 60
_CACHE: Dict[tuple[str, str], tuple[float, tuple[str, ...]]] = {}


class ModelCatalogError(RuntimeError):
    """Model listesi güvenle alınamadığında kullanıcıya gösterilen hata."""


def preferences_path() -> Path:
    """Model tercihlerinin kullanıcıya özel dosyası."""
    return data_root() / "model_preferences.json"


def valid_model_id(value: str) -> bool:
    """Model adını, istek gövdesi ve kalıcı kayıt için sınırlar."""
    return bool(MODEL_ID.fullmatch(value))


def load_model_preferences() -> Dict[str, str]:
    """Bozuk veya eski kayıtları uygulamayı durdurmadan yok sayar."""
    try:
        stored: Any = read_json(preferences_path(), {})
    except (OSError, ValueError):
        return {}
    if not isinstance(stored, dict):
        return {}
    return {
        name: value for name, value in stored.items()
        if name in PROFILES and isinstance(value, str) and valid_model_id(value)
    }


def save_model_preferences(values: Dict[str, str]) -> None:
    """Tercihleri atomik olarak yazar; API anahtarları burada saklanmaz."""
    if any(name not in PROFILES or not valid_model_id(value) for name, value in values.items()):
        raise ValueError("Geçersiz profil veya model adı.")
    save_json(preferences_path(), values)


def _endpoint(provider: str, base_url: str) -> str:
    """Yalnız yapılandırılmış sağlayıcının model listeleme yolunu oluşturur."""
    parsed = urlsplit(base_url)
    if provider == "ollama-cloud":
        if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost"):
            raise ModelCatalogError("Ollama model listesi yalnız yerel sunucudan alınır.")
        return urlunsplit((parsed.scheme, parsed.netloc, "/api/tags", "", ""))
    if provider not in ("openai", "opencode", "openrouter") or parsed.scheme != "https":
        raise ModelCatalogError("Bu sağlayıcı için model listeleme desteklenmiyor.")
    return base_url.rstrip("/") + "/models"


def _listed_ids(provider: str, payload: Any) -> tuple[str, ...]:
    """Sağlayıcı yanıtını araçlı sohbet için makul model kimliklerine indirger."""
    if not isinstance(payload, dict):
        raise ModelCatalogError("Model listesi beklenen biçimde değil.")
    entries: Any = payload.get("models" if provider == "ollama-cloud" else "data")
    if not isinstance(entries, list):
        raise ModelCatalogError("Model listesi beklenen biçimde değil.")
    values: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        candidate: Any = entry.get("name" if provider == "ollama-cloud" else "id")
        if not isinstance(candidate, str) or not valid_model_id(candidate):
            continue
        if provider == "ollama-cloud" and not candidate.endswith(":cloud"):
            continue
        if provider == "openai":
            # Modeller API'si ses, görsel ve gömme modellerini de listeler.
            if not (candidate.startswith("gpt-") or candidate.startswith(("o3", "o4"))):
                continue
            if any(term in candidate for term in (
                "audio", "realtime", "image", "search", "transcribe", "tts", "codex", "pro"
            )) or candidate.startswith("gpt-6-astra"):
                continue
        values.add(candidate)
    return tuple(sorted(values, key=str.casefold))


def cached_models(provider: str, key: Optional[str]) -> tuple[str, ...]:
    """Geçerli önbelleği ağ çağrısı yapmadan döner."""
    cache_key = (provider, hashlib.sha256((key or "").encode()).hexdigest())
    record = _CACHE.get(cache_key)
    return record[1] if record and time.monotonic() - record[0] < CACHE_SECONDS else ()


async def list_provider_models(
    provider: str, base_url: str, key: Optional[str], *, refresh: bool = False,
) -> tuple[str, ...]:
    """Model ID'lerini kısa zaman aşımıyla getirir; anahtarı asla hata metnine koymaz."""
    if provider != "ollama-cloud" and not key:
        raise ModelCatalogError("Önce API anahtarını girin.")
    cached = cached_models(provider, key)
    if cached and not refresh:
        return cached
    endpoint = _endpoint(provider, base_url)
    headers = {"Authorization": "Bearer " + key} if key and provider != "ollama-cloud" else {}
    params = {"supported_parameters": "tools"} if provider == "openrouter" else None
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0), follow_redirects=False) as client:
            response = await client.get(endpoint, headers=headers, params=params)
            response.raise_for_status()
            models = _listed_ids(provider, response.json())
    except (httpx.HTTPError, ValueError) as error:
        if isinstance(error, httpx.HTTPStatusError) and error.response.status_code in (401, 403):
            raise ModelCatalogError("API anahtarı veya model listeleme yetkisi geçersiz.") from None
        raise ModelCatalogError("Model listesi alınamadı; bağlantıyı denetleyin.") from None
    if not models:
        raise ModelCatalogError("Bu sağlayıcıda seçilebilir model bulunamadı.")
    digest = hashlib.sha256((key or "").encode()).hexdigest()
    _CACHE[(provider, digest)] = (time.monotonic(), models)
    return models
