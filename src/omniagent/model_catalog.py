"""Sağlayıcı model kataloğu ve kullanıcı model tercihleri."""

from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit

import httpx

from omniagent.integrations.runtime import data_root, read_json, save_json


PROFILES: frozenset[str] = frozenset(
    ("ollama-cloud", "openai", "opencode", "opencode-think", "openrouter")
)
MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+@-]{0,159}$")
CACHE_SECONDS = 15 * 60
# Önbellek değeri: (zaman, model adları, bulut modellerinde ad→ollama.com adı eşlemesi).
_CACHE: Dict[tuple[str, str], tuple[float, tuple[str, ...], Dict[str, str]]] = {}


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


def _is_cloud_entry(entry: Dict[str, Any], name: str) -> bool:
    """
    Yerel Ollama kaydının ollama.com üzerinden çalışıp çalışmadığı.

    Modern Ollama bir bulut modelini iki biçimde bildirir: ad son ekiyle (`gemma4:cloud`,
    `gpt-oss:20b-cloud`) ya da `remote_model`/`remote_host` alanlarıyla. Kullanıcı
    `ollama pull` ile kaydettiğinde ad düz bir etiket kalabilir (ör. `satici/model:latest`
    + remote_model `deepseek-v4.1-flash`); yalnız ad son ekine bakan eski süzgeç bu tür
    bulut modellerini listeden tamamen gizliyordu.
    """
    for field in ("remote_model", "remote_host"):
        value: Any = entry.get(field)
        if isinstance(value, str) and value.strip():
            return True
    tag: str = name.rsplit(":", 1)[-1].casefold()
    return tag == "cloud" or tag.endswith("-cloud")


def _is_chat_entry(entry: Dict[str, Any]) -> bool:
    """Sunucu yetenek listesi verdiyse yalnız sohbet edebilen kayıtları geçirir."""
    capabilities: Any = entry.get("capabilities")
    if not isinstance(capabilities, list) or not capabilities:
        # Eski sunucular yetenek bildirmez; bilinmeyeni dışlamak yerine kabul ederiz.
        return True
    return "completion" in capabilities


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
        if provider == "ollama-cloud":
            if not (_is_cloud_entry(entry, candidate) and _is_chat_entry(entry)):
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


def _remote_targets(provider: str, payload: Any) -> Dict[str, str]:
    """Bulut kaydının ollama.com'daki gerçek adını verir (ör. gemma4:cloud → gemma4:31b)."""
    if provider != "ollama-cloud" or not isinstance(payload, dict):
        return {}
    entries: Any = payload.get("models")
    if not isinstance(entries, list):
        return {}
    targets: Dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name: Any = entry.get("name")
        remote: Any = entry.get("remote_model")
        if not isinstance(name, str) or not isinstance(remote, str):
            continue
        remote = remote.strip()
        if remote and remote != name:
            targets[name] = remote
    return targets


def cached_models(provider: str, key: Optional[str]) -> tuple[str, ...]:
    """Geçerli önbelleği ağ çağrısı yapmadan döner."""
    cache_key = (provider, hashlib.sha256((key or "").encode()).hexdigest())
    record = _CACHE.get(cache_key)
    return record[1] if record and time.monotonic() - record[0] < CACHE_SECONDS else ()


def cached_remote_targets(provider: str, key: Optional[str]) -> Dict[str, str]:
    """Son listedeki bulut modellerinin ollama.com adlarını döner; ağ çağrısı yapmaz."""
    cache_key = (provider, hashlib.sha256((key or "").encode()).hexdigest())
    record = _CACHE.get(cache_key)
    if not record or time.monotonic() - record[0] >= CACHE_SECONDS:
        return {}
    return dict(record[2])


async def list_provider_models(
    provider: str, base_url: str, key: Optional[str], *, refresh: bool = False,
) -> tuple[str, ...]:
    """Model ID'lerini kısa zaman aşımıyla getirir; anahtarı asla hata metnine koymaz."""
    if provider in ("openai", "openrouter") and not key:
        raise ModelCatalogError("Önce API anahtarını girin.")
    cached = cached_models(provider, key)
    if cached and not refresh:
        return cached
    endpoint = _endpoint(provider, base_url)
    headers = {"Authorization": "Bearer " + key} if key and provider != "ollama-cloud" else {}
    params = {"supported_parameters": "tools"} if provider == "openrouter" else None
    targets: Dict[str, str] = {}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0), follow_redirects=False) as client:
            response = await client.get(endpoint, headers=headers, params=params)
            response.raise_for_status()
            payload: Any = response.json()
            models = _listed_ids(provider, payload)
            listed: List[str] = list(models)
            targets = {
                name: remote for name, remote in _remote_targets(provider, payload).items()
                if name in listed
            }
    except (httpx.HTTPError, ValueError) as error:
        if isinstance(error, httpx.HTTPStatusError) and error.response.status_code in (401, 403):
            raise ModelCatalogError("API anahtarı veya model listeleme yetkisi geçersiz.") from None
        raise ModelCatalogError("Model listesi alınamadı; bağlantıyı denetleyin.") from None
    if not models:
        raise ModelCatalogError("Bu sağlayıcıda seçilebilir model bulunamadı.")
    digest = hashlib.sha256((key or "").encode()).hexdigest()
    _CACHE[(provider, digest)] = (time.monotonic(), models, targets)
    return models
