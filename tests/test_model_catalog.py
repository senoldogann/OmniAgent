"""Model keşfi ve kalıcı tercihlerin ağ harcamadan doğrulanması."""

from pathlib import Path
from typing import Any

import httpx
import pytest

from omniagent import model_catalog as catalog


def test_preferences_are_atomic_and_ignore_corruption(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    catalog.save_model_preferences({"openai": "gpt-6-luna", "openrouter": "x/y:free"})
    assert catalog.load_model_preferences() == {
        "openai": "gpt-6-luna", "openrouter": "x/y:free",
    }
    assert catalog.preferences_path().stat().st_mode & 0o077 == 0
    catalog.preferences_path().write_text("{broken", encoding="utf-8")
    assert catalog.load_model_preferences() == {}
    with pytest.raises(ValueError):
        catalog.save_model_preferences({"openai": "model\nsecret"})


def test_model_lists_filter_specialized_models() -> None:
    rows = {"data": [
        {"id": "gpt-6-luna"}, {"id": "gpt-6-astra"},
        {"id": "gpt-4o-mini"}, {"id": "gpt-4o-realtime"},
        {"id": "text-embedding-3-small"}, {"id": "../bad"},
    ]}
    assert catalog._listed_ids("openai", rows) == ("gpt-4o-mini", "gpt-6-luna")
    assert catalog._listed_ids("ollama-cloud", {
        "models": [
            {"name": "gemma4:cloud"}, {"name": "gpt-oss:20b-cloud"}, {"name": "llama3:latest"},
        ],
    }) == ("gemma4:cloud", "gpt-oss:20b-cloud")


def test_ollama_cloud_keeps_remote_registered_models() -> None:
    """
    Ad son eki olmayan bulut kaydı (`ollama pull` sonrası düz etiket) gizlenmemeli; yerel
    sohbet modeli ve gömme modeli ise bulut profiline girmemeli.
    """
    rows = {"models": [
        {"name": "gemma4:cloud", "remote_model": "gemma4:31b", "remote_host": "https://ollama.com"},
        {"name": "satici/deepseek-v41-uncensored:latest", "remote_model": "deepseek-v4.1-flash",
         "remote_host": "https://ollama.com:443", "capabilities": ["completion", "tools"]},
        {"name": "mxbai-embed-large:latest", "capabilities": ["embedding"]},
        {"name": "orcarouter/Qwen3.8-27B-Uncensored:iq2_xxs",
         "capabilities": ["completion", "vision", "tools"]},
        {"name": "../bad"},
    ]}
    assert catalog._listed_ids("ollama-cloud", rows) == (
        "gemma4:cloud", "satici/deepseek-v41-uncensored:latest",
    )


def test_remote_targets_expose_ollama_com_model_name() -> None:
    """Ayarlar notu, kullanıcının aradığı gerçek ollama.com adını gösterebilmeli."""
    payload = {"models": [
        {"name": "gemma4:cloud", "remote_model": "gemma4:31b"},
        {"name": "gpt-oss:20b-cloud", "remote_model": "gpt-oss:20b-cloud"},
        {"name": "gemma4:cloud", "remote_model": "  "},
        {"name": "llama3:latest"},
    ]}
    assert catalog._remote_targets("ollama-cloud", payload) == {"gemma4:cloud": "gemma4:31b"}
    assert catalog._remote_targets("openai", payload) == {}


@pytest.mark.asyncio
async def test_ollama_cloud_listing_caches_remote_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"models": [
        {"name": "satici/model:latest", "remote_model": "deepseek-v4.1-flash",
         "capabilities": ["completion"]},
        {"name": "mxbai-embed-large:latest", "capabilities": ["embedding"]},
    ]}))
    monkeypatch.setattr(catalog.httpx, "AsyncClient", lambda **kw: real_client(transport=transport, **kw))
    catalog._CACHE.clear()
    assert await catalog.list_provider_models(
        "ollama-cloud", "http://127.0.0.1:11434/v1/", None,
    ) == ("satici/model:latest",)
    assert catalog.cached_remote_targets("ollama-cloud", None) == {
        "satici/model:latest": "deepseek-v4.1-flash",
    }
    catalog._CACHE.clear()
    assert catalog.cached_remote_targets("ollama-cloud", None) == {}


@pytest.mark.asyncio
async def test_openrouter_uses_tools_filter_and_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"data": [{"id": "provider/tool-model"}]})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(catalog.httpx, "AsyncClient", lambda **kw: real_client(transport=transport, **kw))
    catalog._CACHE.clear()
    base = "https://openrouter.ai/api/v1"
    assert await catalog.list_provider_models("openrouter", base, "test-key") == ("provider/tool-model",)
    assert await catalog.list_provider_models("openrouter", base, "test-key") == ("provider/tool-model",)
    assert len(requests) == 1
    assert requests[0].url.params["supported_parameters"] == "tools"
    assert requests[0].headers["Authorization"] == "Bearer test-key"
    assert "test-key" not in repr(catalog._CACHE)


@pytest.mark.asyncio
async def test_missing_key_and_auth_failure_are_clear(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(catalog.ModelCatalogError, match="API anahtarını"):
        await catalog.list_provider_models("openai", "https://api.openai.com/v1", None)
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(401, json={"error": "secret"}))
    monkeypatch.setattr(catalog.httpx, "AsyncClient", lambda **kw: real_client(transport=transport, **kw))
    with pytest.raises(catalog.ModelCatalogError, match="yetkisi"):
        await catalog.list_provider_models(
            "openai", "https://api.openai.com/v1", "test-key", refresh=True,
        )


def test_ollama_endpoint_rejects_remote_host() -> None:
    with pytest.raises(catalog.ModelCatalogError):
        catalog._endpoint("ollama-cloud", "https://remote.example/v1")


@pytest.mark.asyncio
async def test_opencode_public_model_list_without_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"data": [{"id": "qwen3.8-flash"}]})
    )
    monkeypatch.setattr(catalog.httpx, "AsyncClient", lambda **kw: real_client(transport=transport, **kw))
    catalog._CACHE.clear()
    assert await catalog.list_provider_models(
        "opencode", "https://opencode.ai/zen/go/v1", None,
    ) == ("qwen3.8-flash",)
