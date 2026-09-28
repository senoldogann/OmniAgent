"""native_macos.py testleri: yalnız macOS'a özgü davranışı ve zararsız düşüş yollarını doğrular."""
from __future__ import annotations

import sys

import pytest

from omniagent.ui import native_macos


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
