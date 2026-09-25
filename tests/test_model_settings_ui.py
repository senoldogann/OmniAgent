"""Ayarlar model seçiminin görünürlüğü ve görev sınırı davranışı."""

import os
from concurrent.futures import Future
from pathlib import Path
from typing import Iterator

import customtkinter as ctk
import pytest

from omniagent.platform.macos import api_keys
from omniagent import config
from omniagent import model_catalog
from omniagent.ui import app as ui


def _widgets(root: object, kind: type) -> Iterator[object]:
    for child in root.winfo_children():
        if isinstance(child, kind):
            yield child
        yield from _widgets(child, kind)


@pytest.mark.skipif(os.environ.get("OMNI_UI_TEST") != "1", reason="Gerçek Tk testi isteğe bağlı")
def test_settings_lists_profiles_and_persists_selected_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(ui, "create_model_clients", lambda: {})
    monkeypatch.setattr(api_keys, "stored_key", lambda variable: None)
    for variable in api_keys.KEY_VARIABLES:
        monkeypatch.delenv(variable, raising=False)
        config.set_api_key(variable, None)

    async def fake_models(provider: str, base_url: str, key: str | None, *, refresh: bool = False):
        return ("gemma4:cloud",) if provider == "ollama-cloud" else ("gpt-4o-mini",)

    monkeypatch.setattr(ui, "list_provider_models", fake_models)
    original_model = config.BACKENDS["openai"]["model"]
    original_body = dict(config.BACKENDS["openai"]["extra_body"])
    app = ui.OmniUI()
    app.withdraw()
    try:
        app._open_settings()
        window = app._settings_window
        assert window is not None
        selectors = list(_widgets(window, ctk.CTkComboBox))
        assert len(selectors) == len(config.BACKENDS)
        selectors[1].set("gpt-4o-mini")
        save = next(
            button for button in _widgets(window, ctk.CTkButton)
            if button.cget("text") == "Kaydet"
        )
        save.invoke()
        assert model_catalog.load_model_preferences()["openai"] == "gpt-4o-mini"
        assert config.BACKENDS["openai"]["model"] == "gpt-4o-mini"
        assert config.BACKENDS["openai"]["extra_body"] == {}
        running: Future[None] = Future()
        app._agent_future = running
        selectors[1].set("gpt-6-sol")
        save.invoke()
        assert config.BACKENDS["openai"]["model"] == "gpt-4o-mini"
        assert model_catalog.load_model_preferences()["openai"] == "gpt-6-sol"
        running.set_result(None)
        app._rebuild_clients()
        assert config.BACKENDS["openai"]["model"] == "gpt-6-sol"
        assert config.BACKENDS["openai"]["extra_body"] == {"reasoning_effort": "none"}
        app._agent_future = None
    finally:
        config.BACKENDS["openai"]["model"] = original_model
        config.BACKENDS["openai"]["extra_body"] = original_body
        app._on_close()

def test_saved_choice_updates_runtime_and_openai_request_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from omniagent import config

    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    original_model = config.BACKENDS["openai"]["model"]
    original_body = dict(config.BACKENDS["openai"]["extra_body"])
    try:
        model_catalog.save_model_preferences({"openai": "gpt-4o-mini"})
        assert config.apply_model_preferences() == ("openai",)
        assert config.BACKENDS["openai"]["model"] == "gpt-4o-mini"
        assert config.BACKENDS["openai"]["extra_body"] == {}
        config.set_backend_model("openai", "gpt-6-luna")
        assert config.BACKENDS["openai"]["extra_body"] == {"reasoning_effort": "none"}
    finally:
        config.BACKENDS["openai"]["model"] = original_model
        config.BACKENDS["openai"]["extra_body"] = original_body
