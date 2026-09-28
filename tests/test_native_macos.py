"""native_macos.py testleri: yalnız macOS'a özgü davranışı ve zararsız düşüş yollarını doğrular."""
from __future__ import annotations

import os
import sys
from typing import Iterator

import pytest

from omniagent.ui import app as ui
from omniagent.ui import native_macos


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> Iterator["ui.OmniUI"]:
    if os.environ.get("OMNI_UI_TEST") != "1":
        pytest.skip("Gerçek Tk testi OMNI_UI_TEST=1 ile etkinleştirilir.")
    monkeypatch.setattr(ui, "create_model_clients", lambda: {})
    window: ui.OmniUI = ui.OmniUI()
    window.withdraw()
    yield window
    window._on_close()


def test_apply_vibrancy_returns_false_off_darwin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(native_macos.sys, "platform", "linux")
    assert native_macos.apply_vibrancy("hiçbir pencere", "sidebar") is False


def test_apply_vibrancy_returns_false_when_window_title_not_found() -> None:
    if sys.platform != "darwin":
        pytest.skip("Yalnız macOS'ta anlamlı")
    missing_title = "kesinlikle var olmayan bir pencere başlığı — test-native-macos-12345"
    assert native_macos.apply_vibrancy(missing_title, "sidebar") is False


def test_install_native_menu_bar_returns_false_off_darwin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(native_macos.sys, "platform", "linux")
    assert native_macos.install_native_menu_bar("OmniAgent Test") is False


def test_install_native_menu_bar_succeeds_on_darwin() -> None:
    if sys.platform != "darwin":
        pytest.skip("Yalnız macOS'ta anlamlı")
    assert native_macos.install_native_menu_bar("OmniAgent Test") is True


def test_apply_vibrancy_is_idempotent_across_repeated_calls(app: "ui.OmniUI") -> None:
    """Tekrarlanan <Map> olaylarında (küçültme/⌘X) aynı pencereye ikinci bir view eklenmemeli."""
    import AppKit

    # Benzersiz başlık: tam pakette başka bir testten kalabilecek aynı adlı ("OmniAgent")
    # bir pencereyle karışmasın — apply_vibrancy zaten başlığa göre eşleşiyor.
    title = f"test-native-macos-idempotent-{id(app)}"
    app.title(title)
    assert native_macos.apply_vibrancy(title, "sidebar") is True
    assert native_macos.apply_vibrancy(title, "sidebar") is True

    matches = [
        window for window in AppKit.NSApplication.sharedApplication().windows()
        if str(window.title()) == title
    ]
    assert len(matches) == 1
    vibrancy_views = [
        view for view in matches[0].contentView().subviews()
        if str(view.identifier()) == native_macos._VIBRANCY_IDENTIFIER
    ]
    assert len(vibrancy_views) == 1
