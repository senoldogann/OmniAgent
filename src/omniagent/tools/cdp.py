"""
OmniAgent Chrome DevTools Protocol (CDP) Hibrit Köprüsü (tools/cdp.py)
Kullanıcının çalışan Google Chrome oturumuna remote debugging portu (varsayılan 9222)
üzerinden bağlanarak doğrudan DOM ve sekme etkileşimi sağlar.
Port kapalı olduğunda hiçbir hata vermeden şeffaf bir şekilde None döner.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import URLError
from urllib.request import Request, urlopen
import json

from playwright.async_api import Browser, BrowserContext, Error as PlaywrightError, Page, Playwright, async_playwright

from .types import ToolError

DEFAULT_CDP_PORT: int = 9222
CDP_PROBE_TIMEOUT_SECONDS: float = 0.15


def is_cdp_available(port: int = DEFAULT_CDP_PORT, timeout: float = CDP_PROBE_TIMEOUT_SECONDS) -> bool:
    """
    Chrome remote debugging portunun açık ve yanıt verir durumda olduğunu denetler.
    Saf fonksiyon; bağlantı yoksa anında False döner.
    """
    url: str = f"http://127.0.0.1:{port}/json/version"
    try:
        request = Request(url, headers={"User-Agent": "OmniAgent-Probe"})
        with urlopen(request, timeout=timeout) as response:
            if response.status == 200:
                data: Dict[str, Any] = json.loads(response.read().decode("utf-8"))
                return bool(data.get("webSocketDebuggerUrl") or "Browser" in data)
    except (URLError, TimeoutError, OSError, json.JSONDecodeError):
        return False
    return False


def list_cdp_tabs(port: int = DEFAULT_CDP_PORT, timeout: float = CDP_PROBE_TIMEOUT_SECONDS) -> List[Dict[str, str]]:
    """
    Açık Chrome sekmelerinin özet listesini döner: id, title, url, webSocketDebuggerUrl.
    Hata veya bağlantı yokluğunda boş liste döner.
    """
    url: str = f"http://127.0.0.1:{port}/json/list"
    try:
        request = Request(url, headers={"User-Agent": "OmniAgent-Probe"})
        with urlopen(request, timeout=timeout) as response:
            if response.status == 200:
                entries: List[Dict[str, Any]] = json.loads(response.read().decode("utf-8"))
                return [
                    {
                        "id": str(item.get("id", "")),
                        "title": str(item.get("title", "")),
                        "url": str(item.get("url", "")),
                        "type": str(item.get("type", "")),
                    }
                    for item in entries
                    if item.get("type") == "page"
                ]
    except (URLError, TimeoutError, OSError, json.JSONDecodeError):
        return []
    return []


class ChromeCDPSession:
    """
    Çalışan canlı Chrome'a Playwright CDP üzerinden bağlanan hafif oturum yöneticisi.
    Bağlantı kesildiğinde veya port kapalıyken şeffaf bir şekilde devre dışı kalır.
    """
    def __init__(self, port: int = DEFAULT_CDP_PORT) -> None:
        self.port: int = port
        self.playwright: Optional[Playwright] = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self._lock: asyncio.Lock = asyncio.Lock()

    async def is_connected(self) -> bool:
        """CDP oturumunun canlı ve kullanılabilir olduğunu doğrular."""
        return self.browser is not None and self.browser.is_connected()

    async def connect(self) -> bool:
        """
        Remote debugging portu açıksa canlı Chrome'a bağlanır.
        Bağlantı kurulamazsa sessizce False döner.
        """
        async with self._lock:
            if await self.is_connected():
                return True
            if not is_cdp_available(self.port):
                return False
            try:
                if self.playwright is None:
                    self.playwright = await async_playwright().start()
                endpoint: str = f"http://127.0.0.1:{self.port}"
                self.browser = await self.playwright.chromium.connect_over_cdp(endpoint)
                contexts = self.browser.contexts
                self.context = contexts[0] if contexts else await self.browser.new_context()
                return True
            except (PlaywrightError, OSError, Exception) as error:
                logging.debug("CDP bağlantısı kurulamadı", extra={"port": self.port, "error": str(error)})
                await self.disconnect()
                return False

    async def find_tab(self, url_prefix: Optional[str] = None) -> Optional[Page]:
        """
        Belirtilen URL önekine sahip açık sekmeyi, yoksa ilk etkin sekmeyi bulur.
        """
        if not await self.connect() or self.context is None:
            return None
        pages: List[Page] = self.context.pages
        if not pages:
            return None
        if url_prefix:
            normalized_prefix: str = url_prefix.casefold()
            for page in pages:
                if page.url.casefold().startswith(normalized_prefix):
                    return page
        return pages[0]

    async def click_text(self, text: str, page: Optional[Page] = None) -> bool:
        """
        Sayfa içinde belirtilen metne sahip öğeyi bulup doğrudan tıklar.
        Öğe bulunamazsa False döner (böylece Vision OCR fallback çalışır).
        """
        target_page: Optional[Page] = page or await self.find_tab()
        if target_page is None:
            return False
        try:
            locator = target_page.get_by_text(text, exact=True).first
            if await locator.is_visible(timeout=500):
                await locator.click(timeout=1000)
                return True
            # Birebir bulunamazsa gevşek eşleşme
            fuzzy = target_page.get_by_text(text, exact=False).first
            if await fuzzy.is_visible(timeout=300):
                await fuzzy.click(timeout=1000)
                return True
        except (PlaywrightError, Exception):
            pass
        return False

    async def fill_field(self, selector: str, text: str, page: Optional[Page] = None) -> bool:
        """
        Form alanını seçiciyle bulup metin yazar (Enter'a basmaz).
        """
        target_page: Optional[Page] = page or await self.find_tab()
        if target_page is None:
            return False
        try:
            locator = target_page.locator(selector).first
            if await locator.is_visible(timeout=500):
                await locator.fill(text, timeout=1000)
                return True
        except (PlaywrightError, Exception):
            pass
        return False

    async def submit_text(self, selector: str, text: str, page: Optional[Page] = None) -> bool:
        """
        Alana yazar ve Enter'a basar.
        """
        target_page: Optional[Page] = page or await self.find_tab()
        if target_page is None:
            return False
        try:
            locator = target_page.locator(selector).first
            if await locator.is_visible(timeout=500):
                await locator.fill(text, timeout=1000)
                await locator.press("Enter", timeout=1000)
                return True
        except (PlaywrightError, Exception):
            pass
        return False

    async def disconnect(self) -> None:
        """CDP bağlantısını kapatır ve kaynakları temizler."""
        if self.browser:
            try:
                await self.browser.close()
            except Exception:
                pass
            self.browser = None
            self.context = None
        if self.playwright:
            try:
                await self.playwright.stop()
            except Exception:
                pass
            self.playwright = None
