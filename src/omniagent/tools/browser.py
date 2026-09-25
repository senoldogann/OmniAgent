"""
OmniAgent Tarayıcı ve Ağ Modülü (tools/browser.py)
Sovereign seviyesinde yüksek performanslı Playwright yönetimi ve Chrome entegrasyonu.
"""
import asyncio
import logging
import subprocess
import time
from typing import Dict, List, Optional, Tuple, Union
from urllib.parse import SplitResult, urlsplit

from playwright.async_api import (
    Browser, BrowserContext, Error as PlaywrightError,
    Page, Playwright, TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from .filesystem import clean_html
from .gui_input import _require_accessibility, press_key_spec, type_unicode_text
from .system import child_environment
from .types import (
    CHROME_LOAD_CHECKS, CHROME_SCRIPT_TIMEOUT_SECONDS,
    FETCH_ERROR_BODY_LIMIT, PAGE_ACTION_TIMEOUT_MS,
    PAGE_ELEMENT_LIMIT, PAGE_LOAD_TIMEOUT_MS,
    SHELL_STDOUT_LIMIT, BrowserAction, ToolError, clip_text,
)

_clip = clip_text

_PAGE_ELEMENTS_SCRIPT: str = """
(limit) => {
  const out = [];
  const q = (s) => s.replace(/\\\\/g, '\\\\\\\\').replace(/"/g, '\\\\"');
  const nodes = document.querySelectorAll('a[href], button, input:not([type=hidden]), textarea, select, [role=button], [role=link]');
  for (const el of nodes) {
    if (out.length >= limit) break;
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    const tag = el.tagName.toLowerCase();
    const label = (el.getAttribute('aria-label') || el.innerText || el.value || el.getAttribute('placeholder') || el.getAttribute('title') || '').trim().replace(/\\s+/g, ' ').slice(0, 60);
    let sel = null;
    if (el.id && /^[A-Za-z][\\w-]*$/.test(el.id)) sel = '#' + el.id;
    else if (el.getAttribute('name')) sel = `${tag}[name="${q(el.getAttribute('name'))}"]`;
    else if (el.getAttribute('aria-label')) sel = `${tag}[aria-label="${q(el.getAttribute('aria-label'))}"]`;
    else if (el.getAttribute('placeholder')) sel = `${tag}[placeholder="${q(el.getAttribute('placeholder'))}"]`;
    else if (label) sel = `${tag}:has-text("${q(label.slice(0, 40))}")`;
    else continue;
    const kind = tag === 'input' ? `input[${el.type}]` : tag;
    out.push(`${sel} — ${kind}${label ? ' "' + label + '"' : ''}`);
  }
  return out;
}
"""

_CHROME_TAB_APPLESCRIPT: str = """on run argv
set targetUrl to item 1 of argv
set targetOrigin to item 2 of argv
set loadChecks to (item 3 of argv) as integer
tell application "Google Chrome"
    if (count of windows) is 0 then make new window
    set windowId to id of front window
    set tabId to id of active tab of front window
    if targetUrl is not "" then
        repeat with windowItem in windows
            set matchingIds to id of (every tab of windowItem whose URL starts with targetOrigin)
            if matchingIds is not {} then
                set windowId to id of windowItem
                set tabId to item 1 of matchingIds
                exit repeat
            end if
        end repeat
    end if
    set targetWindow to window id windowId
    set tabIds to id of every tab of targetWindow
    repeat with position from 1 to count of tabIds
        if item position of tabIds is tabId then set active tab index of targetWindow to position
    end repeat
    set index of targetWindow to 1
    set targetTab to tab id tabId of targetWindow
    if targetUrl is not "" and (URL of targetTab) is not targetUrl then set URL of targetTab to targetUrl
    activate
    repeat loadChecks times
        if not (loading of targetTab) then exit repeat
        delay 0.1
    end repeat
    return (URL of targetTab) & linefeed & (title of targetTab) & linefeed & (loading of targetTab)
end tell
end run"""

def normalize_browser_key(value: str) -> str:
    aliases = {
        "enter": "Enter", "return": "Enter", "esc": "Escape", "escape": "Escape",
        "tab": "Tab", "space": "Space", "backspace": "Backspace", "delete": "Delete",
        "arrowup": "ArrowUp", "arrowdown": "ArrowDown", "arrowleft": "ArrowLeft", "arrowright": "ArrowRight",
        "cmd": "Meta", "command": "Meta", "meta": "Meta", "ctrl": "Control", "control": "Control",
        "alt": "Alt", "option": "Alt", "shift": "Shift",
    }
    return "+".join(aliases.get(p.strip().casefold(), p.strip()) for p in value.split("+"))

def _decode_http_output(value: bytes | str) -> str:
    """curl çıktısını decode hatasıyla düşürmeden metne çevirir."""
    if isinstance(value, str):
        return value
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return value.decode(encoding)
        except UnicodeDecodeError:
            continue
    return value.decode("utf-8", errors="replace")


def fetch_raw_content(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ToolError("fetch_raw yalnızca http(s) kabul eder.", "INVALID_URL", False)
    result = subprocess.run(
        [
            "curl", "--fail-with-body", "--show-error", "--silent", "--location", "--compressed",
            "--user-agent", "OmniAgent/0.1 (+https://localhost)",
            "--proto", "=http,https", "--proto-redir", "=http,https",
            "--retry", "2", "--retry-delay", "1", "--retry-max-time", "20",
            "--max-time", "15", "--", url,
        ],
        env=child_environment(), capture_output=True, text=False, timeout=25,
    )
    stdout = _decode_http_output(result.stdout)
    stderr = _decode_http_output(result.stderr)
    if result.returncode != 0:
        body = stdout.strip()
        shown = (
            _clip(body, FETCH_ERROR_BODY_LIMIT)
            if body.startswith(("{", "[")) or "<" not in body[:200]
            else _clip(clean_html(body), FETCH_ERROR_BODY_LIMIT)
        )
        raise ToolError(
            f"HTTP başarısız: url={url}, çıkış={result.returncode}, stderr={stderr.strip()}"
            f"{' yanıt=' + shown if shown else ''}",
            "FETCH_FAILED",
            True,
        )
    stripped = stdout.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        return _clip(stdout, SHELL_STDOUT_LIMIT)
    return clean_html(stdout)

def run_chrome_active_tab(url: Optional[str], applescript_available: Optional[bool]) -> Tuple[str, bool]:
    parsed = urlsplit(url) if url else None
    if parsed and (parsed.scheme not in ("https", "http") or not parsed.netloc):
        raise ToolError("Geçersiz URL.", "INVALID_URL", False)
    origin = f"{parsed.scheme}://{parsed.netloc}/" if parsed else ""
    
    fallback_reason = "önceki AppleScript hatası" if applescript_available is False else None
    result = None
    new_as_available = applescript_available is not False

    if applescript_available is not False:
        try:
            result = subprocess.run(["osascript", "-e", _CHROME_TAB_APPLESCRIPT, url or "", origin, str(CHROME_LOAD_CHECKS)],
                                    env=child_environment(), capture_output=True, text=True, timeout=CHROME_SCRIPT_TIMEOUT_SECONDS, check=False)
            if result.returncode != 0:
                new_as_available, fallback_reason = False, result.stderr.strip() or f"çıkış={result.returncode}"
            else:
                new_as_available = True
        except Exception as e:
            new_as_available, fallback_reason = False, type(e).__name__

    if fallback_reason:
        _require_accessibility()
        try:
            subprocess.run(["open", "-a", "Google Chrome"], env=child_environment(), capture_output=True, text=True, timeout=5)
        except Exception as e:
            raise ToolError(f"UI Fallback başarısız: {type(e).__name__}", "CHROME_SESSION_FAILED", True) from e
        if url:
            press_key_spec("cmd+l"); type_unicode_text(url); press_key_spec("enter")
            return f"Görünür Chrome sekmesi: {url}\nBaşlık: görünür UI fallback ({fallback_reason}); yükleme otomatik gözlemlenir.", new_as_available
        return f"Görünür Chrome öne getirildi.\nBaşlık: görünür UI fallback ({fallback_reason}); URL okunamadı.", new_as_available

    if result is None: raise ToolError("Chrome sekmesine erişilemedi.", "CHROME_SESSION_FAILED", True)
    lines = result.stdout.rstrip("\n").split("\n")
    if len(lines) < 3: raise ToolError(f"Yanıt hatalı: {result.stdout!r}", "CHROME_SESSION_FAILED", True)
    loading = "\nSayfa hâlâ yükleniyor." if lines[-1] == "true" else ""
    return f"Görünür Chrome sekmesi: {lines[0]}\nBaşlık: {' '.join(lines[1:-1])}{loading}", new_as_available

class HeadlessBrowserSession:
    def __init__(self) -> None:
        self.playwright_instance: Optional[Playwright] = None
        self.browser: Optional[Browser] = None
        self.browser_context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self._lock: asyncio.Lock = asyncio.Lock()

    async def get_page(self) -> Page:
        async with self._lock:
            if self.browser_context is None:
                self.playwright_instance = await async_playwright().start()
                self.browser = await self.playwright_instance.chromium.launch(headless=True)
                self.browser_context = await self.browser.new_context(
                    user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )
            if self.page is None or self.page.is_closed():
                self.page = await self.browser_context.new_page()
                self.page.set_default_timeout(PAGE_ACTION_TIMEOUT_MS)
                self.page.set_default_navigation_timeout(PAGE_LOAD_TIMEOUT_MS)
            return self.page

    async def browse(self, url: Optional[str], actions: List[BrowserAction]) -> str:
        page = await self.get_page()
        return await browse_page_actions(page, url, actions)

    async def close(self) -> None:
        async with self._lock:
            if self.browser: await self.browser.close()
            if self.playwright_instance: await self.playwright_instance.stop()
            self.browser = self.browser_context = self.page = None

async def browse_page_actions(page: Page, url: Optional[str], actions: List[BrowserAction]) -> str:
    if url:
        try: await page.goto(url, wait_until="domcontentloaded")
        except PlaywrightError as e: raise ToolError(f"Sayfa açılamadı: {url}, {e}", "PAGE_LOAD_FAILED", True) from e
    elif page.url == "about:blank":
        raise ToolError("Açık sayfa yok; url ver.", "NO_PAGE", False)

    for i, action in enumerate(actions):
        kind, selector, value = action.get("action"), str(action.get("selector") or ""), action.get("value")
        try:
            if kind == "click": await page.click(selector)
            elif kind == "fill" and value: await page.fill(selector, value)
            elif kind == "press" and value: await page.press(selector, normalize_browser_key(value))
            elif kind == "wait_for":
                state = value if value in ("visible", "hidden", "attached", "detached") else "visible"
                await page.locator(selector).wait_for(state=state)
            else:
                raise ToolError(f"Geçersiz eylem {i}: {action}", "INVALID_BROWSER_ACTION", False)
        except PlaywrightTimeoutError as e:
            elements = await page.evaluate(_PAGE_ELEMENTS_SCRIPT, PAGE_ELEMENT_LIMIT)
            raise ToolError(f"Zaman aşımı {i} ({kind} {selector}); Öğeler:\n" + "\n".join(elements), "BROWSER_ACTION_TIMEOUT", True) from e

    if actions: await page.wait_for_load_state("domcontentloaded")
    text = await asyncio.to_thread(clean_html, await page.content())
    elements = await page.evaluate(_PAGE_ELEMENTS_SCRIPT, PAGE_ELEMENT_LIMIT)
    return f"Tarayıcı: arka planda çalışan ayrı Chromium\nURL: {page.url}\nBaşlık: {await page.title()}\n\n{text}\n\nÖĞELER:\n" + "\n".join(elements)
