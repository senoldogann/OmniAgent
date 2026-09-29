"""native_macos.py testleri: yalnız macOS'a özgü davranışı ve zararsız düşüş yollarını doğrular."""
from __future__ import annotations

import sys
import subprocess

import pytest

from omniagent.ui import app as ui
from omniagent.ui import native_macos


def test_install_native_menu_bar_returns_false_off_darwin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(native_macos.sys, "platform", "linux")
    assert native_macos.install_native_menu_bar("OmniAgent Test") is False


def test_install_native_menu_bar_succeeds_on_darwin() -> None:
    if sys.platform != "darwin":
        pytest.skip("Yalnız macOS'ta anlamlı")
    # Tk daha sonra NSApplication'ı kendi sınıfıyla kurar; AppKit denemesini ayrı süreçte tut.
    result = subprocess.run(
        [sys.executable, "-c", "from omniagent.ui.native_macos import install_native_menu_bar; "
         "assert install_native_menu_bar('OmniAgent Test')"],
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_native_window_style_only_updates_titlebar() -> None:
    """Başlık çubuğu biçimlendirmesi Tk içeriğini örtecek efekt kurmaz."""
    class Window:
        def __init__(self) -> None:
            self.titles: list[str] = []

        def _style_native_titlebar(self, title: str) -> None:
            self.titles.append(title)

    window = Window()
    ui.OmniUI._style_native_window(window, "OmniAgent")  # type: ignore[arg-type]
    assert window.titles == ["OmniAgent"]
