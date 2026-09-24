"""Ollama Cloud profilinin erişim ve geri düşme regresyon testleri."""
import io
import json
import pytest

import main


class _Response(io.BytesIO):
    """urlopen yanıtı gibi kapanabilen küçük test akışı."""


def test_ollama_cloud_ready_requires_installed_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Yerel sunucudaki farklı model varsayılan bulut profilini hazır göstermez."""
    expected = main.BACKENDS["ollama-cloud"]["model"]
    payload = json.dumps({"models": [{"name": "other:cloud"}, {"name": expected}]}).encode()
    monkeypatch.setattr(main, "urlopen", lambda url, timeout: _Response(payload))
    assert main.ollama_cloud_ready()
    monkeypatch.setattr(main, "urlopen", lambda url, timeout: _Response(
        b'{"models":[{"name":"other:cloud"}]}'
    ))
    assert not main.ollama_cloud_ready()


@pytest.mark.asyncio
async def test_cloud_client_only_when_local_model_is_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kurulu olmayan bulut modeline boş yere ağ isteği gönderilmez."""
    monkeypatch.setattr(main, "ollama_cloud_ready", lambda: False)
    absent = main.create_model_clients()
    try:
        assert "ollama-cloud" not in absent
    finally:
        await main.close_model_clients(absent)

    monkeypatch.setattr(main, "ollama_cloud_ready", lambda: True)
    present = main.create_model_clients()
    try:
        assert present["ollama-cloud"] is not None
        assert str(present["ollama-cloud"].base_url) == "http://127.0.0.1:11434/v1/"
    finally:
        await main.close_model_clients(present)


def test_backend_fallback_keeps_free_routes() -> None:
    """Ollama yoksa Luna, Luna yoksa ücretsiz OpenCode seçilir."""
    all_available = frozenset({"ollama-cloud", "openai", "zen-free"})
    assert main.attempt_plan("ollama-cloud", all_available) == (
        "ollama-cloud", "ollama-cloud", "openai", "zen-free",
    )
    assert main.attempt_plan("openai", all_available) == (
        "openai", "openai", "ollama-cloud", "zen-free",
    )
    assert main.attempt_plan("zen-free", all_available) == (
        "zen-free", "zen-free", "ollama-cloud", "openai",
    )
    assert main.attempt_plan("ollama-cloud", frozenset({"ollama-cloud", "zen-free"}))[-1] == "zen-free"
