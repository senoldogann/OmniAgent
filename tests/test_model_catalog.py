"""Model keşfi ve kalıcı tercihlerin ağ harcamadan doğrulanması."""

from pathlib import Path
from typing import Any

import httpx
import pytest

import model_catalog as catalog


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
        "models": [{"name": "gemma4:cloud"}, {"name": "llama3:latest"}],
    }) == ("gemma4:cloud",)


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
