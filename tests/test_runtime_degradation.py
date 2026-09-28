"""Canlı kayıtta görülen üç çalışma-zamanı bozulmasının regresyon testleri.

1. Paketlenmiş uygulama Finder/launchd ile açıldığında PATH kısalıyor; Homebrew araçları
   (node, brew, playwright) bulunamıyordu.
2. `web_search` varsayılan motor kümesi erişilemeyen startpage'i seçip tüm metin aramalarını
   düşürüyordu.
3. Playwright Chromium ikilisi yokken her `browse_url` ham bir Playwright hatası veriyor,
   model de kurulum komutlarını deneyip boşa tur harcıyordu.
"""
from __future__ import annotations

import os
from typing import Any

import pytest
from playwright.async_api import Error as PlaywrightError

from omniagent import tools
from omniagent.tools import ToolError, Toolbox, browser
from omniagent.tools import system as tool_system
from omniagent.tools import web as tool_web


def test_extended_path_keeps_order_and_skips_missing_directories(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any,
) -> None:
    """PATH öneki korunur, var olmayan dizin eklenmez, tekrar oluşmaz."""
    missing = tmp_path / "hic-yok"
    monkeypatch.setattr(tool_system, "_TOOL_PATH_DIRECTORIES", (str(missing),))

    parts = tool_system.extended_path("/usr/bin:/bin").split(os.pathsep)

    assert parts[:2] == ["/usr/bin", "/bin"]
    assert str(missing) not in parts
    assert len(parts) == len(set(parts))


def test_extended_path_adds_existing_tool_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any,
) -> None:
    """Homebrew gibi mevcut araç dizini PATH'in sonuna eklenir."""
    brew_bin = tmp_path / "brew" / "bin"
    brew_bin.mkdir(parents=True)
    monkeypatch.setattr(tool_system, "_TOOL_PATH_DIRECTORIES", (str(brew_bin),))

    assert str(brew_bin) in tool_system.extended_path("/usr/bin").split(os.pathsep)


def test_child_environment_extends_path_without_leaking_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Alt sürece verilen ortam PATH'i genişletir ama API anahtarlarını taşımaz."""
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("OPENAI_API_KEY", "sizmis-olmamali")

    environment = tool_system.child_environment()

    assert environment["PATH"].startswith("/usr/bin:/bin")
    assert "OPENAI_API_KEY" not in environment


def test_web_search_falls_back_to_next_backend_when_first_engine_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Birinci motor kümesi erişilemezse ikinci küme denenir ve sonuç döner."""
    calls: list[dict[str, Any]] = []

    class FlakyDDGS:
        def __enter__(self) -> "FlakyDDGS":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def text(self, query: str, **kwargs: Any) -> list[dict[str, str]]:
            calls.append(kwargs)
            if kwargs["backend"] == tool_web.SEARCH_BACKENDS[0]:
                raise RuntimeError("ConnectError: startpage erişilemedi")
            return [{"title": "Bulundu", "body": "gövde", "href": "https://example.com/ok"}]

        def news(self, query: str, **kwargs: Any) -> list[dict[str, str]]:
            raise AssertionError("metin modunda haber yolu çağrılmamalı")

    monkeypatch.setattr(tools, "DDGS", FlakyDDGS)

    result = Toolbox().web_search("dayanıklı arama")

    assert [kwargs["backend"] for kwargs in calls] == list(tool_web.SEARCH_BACKENDS[:2])
    assert "https://example.com/ok" in result


def test_web_search_reports_failure_after_all_backends_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tüm motor kümeleri hata verirse son hata açıkça raporlanır."""

    class DeadDDGS:
        def __enter__(self) -> "DeadDDGS":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def text(self, query: str, **kwargs: Any) -> list[dict[str, str]]:
            raise RuntimeError("ag erisilemedi")

    monkeypatch.setattr(tools, "DDGS", DeadDDGS)

    with pytest.raises(ToolError) as info:
        Toolbox().web_search("kapali ag")

    assert info.value.code == "WEB_SEARCH_FAILED"
    assert "ag erisilemedi" in str(info.value)


def test_browser_launch_error_maps_missing_engine() -> None:
    """Eksik Playwright ikilisi kalıcı ve kurtarılamaz; diğer başlatma hatası geçici sayılır."""
    missing = browser.browser_launch_error(
        PlaywrightError("Executable doesn't exist at /x/chromium_headless_shell-1243")
    )
    assert missing.code == "BROWSER_UNAVAILABLE"
    assert missing.recoverable is False

    other = browser.browser_launch_error(PlaywrightError("bağlantı koptu"))
    assert other.code == "BROWSER_LAUNCH_FAILED"
    assert other.recoverable is True


@pytest.mark.asyncio
async def test_browse_url_read_only_falls_back_to_fetch_raw(monkeypatch: pytest.MonkeyPatch) -> None:
    """Motor yokken salt okuma isteği fetch_raw ile sonuçlanır; ham kurulum hatası sızmaz."""

    async def unavailable(self: Toolbox) -> Any:
        raise ToolError("motor yok", "BROWSER_UNAVAILABLE", False)

    monkeypatch.setattr(Toolbox, "_get_page", unavailable)
    monkeypatch.setattr(tools, "fetch_raw_content", lambda url: f"STATIK GOVDE {url}")

    result = await Toolbox().browse_url("https://example.com/haber", [])

    assert "TARAYICI MOTORU YOK" in result
    assert "STATIK GOVDE https://example.com/haber" in result


@pytest.mark.asyncio
async def test_browse_url_with_actions_does_not_silently_degrade(monkeypatch: pytest.MonkeyPatch) -> None:
    """Etkileşim isteyen çağrı sessizce fetch_raw'a düşmez; motor hatası yukarı çıkar."""

    async def unavailable(self: Toolbox) -> Any:
        raise ToolError("motor yok", "BROWSER_UNAVAILABLE", False)

    monkeypatch.setattr(Toolbox, "_get_page", unavailable)

    with pytest.raises(ToolError) as info:
        await Toolbox().browse_url(
            "https://example.com", [{"action": "click", "selector": "#gonder", "value": None}],
        )

    assert info.value.code == "BROWSER_UNAVAILABLE"
