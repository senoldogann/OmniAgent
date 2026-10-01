"""
OmniAgent Tarayıcı ve Ağ Modülü (tools/browser.py)
Sovereign seviyesinde yüksek performanslı Playwright yönetimi ve Chrome entegrasyonu.
"""
import asyncio
import logging
import os
import re
import shutil
import subprocess
import time
from typing import Awaitable, Callable, List, Optional, Tuple
from urllib.parse import urlsplit

from playwright.async_api import (
    Browser, BrowserContext, Error as PlaywrightError,
    Page, Playwright, TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from omniagent.approval import (
    AMOUNT_LINES_LIMIT, NO_CIRCUMVENTION_NOTE, TARGET_CHANGED_CODE, ApprovalRequest, ClickTarget, amount_lines,
    click_financial_reason, financial_cta_reason, gui_click_request, is_generic_commit_label,
    communication_click_label, gui_communication_request,
)
from omniagent.core.text_norm import curl_http_statuses
from .process import run_preemptible_process, read_retry_delay, check_read_stop

from .bot_wall import (
    AccessChallenge, access_challenge_error, bypass_note, classify_access_challenge, is_bypass_enabled,
    is_local_app_response, local_response_notice, rate_limit_challenge, widget_notice,
)
from .filesystem import clean_html
from .foreground import require_front_app
from .gui_input import _require_accessibility, press_key_spec, type_unicode_text
from .system import approval_gate_async, child_environment
from .types import (
    APP_ACTIVATION_WAIT_SECONDS, CHROME_APP_NAME, CHROME_LOAD_WAIT_SECONDS, CHROME_SCRIPT_TIMEOUT_SECONDS,
    FETCH_ERROR_BODY_LIMIT, PAGE_ACTION_TIMEOUT_MS,
    PAGE_ELEMENT_LIMIT, PAGE_LOAD_TIMEOUT_MS,
    ApprovalRefused, BrowserAction, ToolError, clip_text,
)

_clip = clip_text

# Ajanın ürün belirteci. BİLEREK BOŞ: bot duvarları ürün adı taşıyan istemcileri engellediği için curl
# ve Playwright User-Agent'ı normal bir Chrome gibi görünür. Bot duvarı modu tools/bot_wall.py'dedir.
AGENT_PRODUCT_TOKEN: str = ""
# curl'ün motor sürümü yok: sabit ve güncel bir Chrome UA'sı kullanılır.
STANDARD_CHROME_USER_AGENT: str = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

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
set loadSeconds to (item 3 of argv) as integer
set createNewTab to item 4 of argv is "true"
tell application "Google Chrome"
    if (count of windows) is 0 then make new window
    set windowId to id of front window
    set tabId to id of active tab of front window
    if createNewTab then
        set newTab to make new tab at end of tabs of front window with properties {URL:targetUrl}
        set tabId to id of newTab
    else if targetUrl is not "" then
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
    set waitStartedAt to current date
    repeat while (loading of targetTab)
        if ((current date) - waitStartedAt) >= loadSeconds then exit repeat
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


def _raise_if_access_wall(
    tool: str, url: str, raw_html: str, challenge: Optional[AccessChallenge],
) -> None:
    """
    İçeriğin yerini alan doğrulama/engel sayfasında davranış iki moda ayrılır. BYPASS modu (varsayılan):
    hata YÜKSETMEZ, belirteci günlüğe yazar ve sayfa içeriği kullanılmaya devam eder; modele gidecek
    strateji/seçici yönergesini _challenge_note ekler. Sıkı mod (bot_wall.BYPASS_ENABLED = False):
    kurtarılamaz ToolError yükseltir. Sayfa sıradansa ya da yerel adresin kendi erişim-reddi yanıtıysa
    (bkz. is_local_app_response) her iki modda da susar.
    """
    if challenge is None or challenge["kind"] != "wall" or is_local_app_response(url, challenge):
        return
    if is_bypass_enabled():
        logging.info(
            "Bot doğrulaması algılandı; bypass ile devam ediliyor",
            extra={"tool": tool, "host": urlsplit(url).hostname, "signal": challenge["signal"]},
        )
        return
    raise access_challenge_error(url, challenge, raw_html)


def _local_response_note(url: str, challenge: Optional[AccessChallenge]) -> str:
    """Loopback adreste hata sayılmayan erişim-reddi sayfası için sonuç sonuna eklenecek not; yoksa boş metin. Saf."""
    if challenge is not None and is_local_app_response(url, challenge):
        return f"\n\n{local_response_notice(challenge)}"
    return ""


def _challenge_note(url: str, challenge: Optional[AccessChallenge]) -> str:
    """
    Araç sonucunun sonuna eklenen, okumayı engellemeyen not. Bypass modunda duvar için uygulanacak
    strateji ve seçiciler, gömülü CAPTCHA bileşeni için bileşen notu döner. Sıkı modda duvar zaten hata
    olarak yükseldiğinden yalnız gömülü bileşen/yerel erişim-reddi notları kalır. Saf.
    """
    if challenge is None:
        return ""
    if challenge["kind"] == "wall":
        # Yerel adresin kendi 'Access denied' yanıtı bot duvarı değildir: bypass yönergesi verilmez
        # (uygulamanın kendi yetki hatası olabilir), yalnız bilgilendirici yerel not eklenir.
        if is_local_app_response(url, challenge) or not is_bypass_enabled():
            return _local_response_note(url, challenge)
        return f"\n\n{bypass_note(challenge)}"
    return f"\n\n{widget_notice(challenge)}"


def _curl_http_status(stderr: str) -> int:
    """
    curl hata çıktısından HTTP durum kodunu okur (--fail-with-body: 'curl: (22) The requested URL returned error: 429');
    bulunamazsa 0. Çapa memory/experience.py ile ortaktır (core/text_norm.curl_http_statuses). Saf.
    """
    statuses: List[int] = curl_http_statuses(stderr)
    return statuses[0] if statuses else 0


def _error_body_challenge(body: str, visible: str, http_status: int) -> Optional[AccessChallenge]:
    """
    HTTP hata yanıtının HTML gövdesini sınıflandırır: sağlayıcı/metin kalıbı öncelikli; kalıp yoksa HTTP 429 gövdesi
    (hız sınırı) ayrı bir engel işaretidir. Yalnız gömülü CAPTCHA bileşeni engel yerine geçmez. Saf.
    """
    challenge: Optional[AccessChallenge] = classify_access_challenge(body, visible)
    if challenge is not None and challenge["kind"] == "wall":
        return challenge
    return rate_limit_challenge(http_status) or challenge


# curl'ün geçici AĞ hatası çıkış kodları: 6 (adres çözülemedi), 7 (bağlanılamadı), 35 (TLS el sıkışma), 52 (boş yanıt),
# 56 (okuma hatası). Zaman aşımı (28) BİLEREK yok: yanıtsız sunucuda her deneme 15 sn yer, en kötü bekleme 47 sn'ye çıkardı;
# karar modelde kalır. fetch_raw yalnız bunlarda yeniden dener; curl'ün kendi --retry'ı KULLANILMAZ çünkü
# HTTP 408/429/5xx'i de yeniden dener (hız sınırını ağırlaştırır). Çıkış 22 (HTTP hata durumu: 429/5xx dahil) ve
# engel/doğrulama sayfaları (HTTP 200 dahil) ASLA yeniden denenmez: sunucunun kararıdır.
_CURL_TRANSIENT_EXIT_CODES: frozenset[int] = frozenset({6, 7, 35, 52, 56})
FETCH_MAX_RETRIES: int = 2
FETCH_RETRY_WAIT_SECONDS: float = 1.0


def _run_curl(url: str) -> subprocess.CompletedProcess[bytes]:
    """curl'ü tek deneme olarak çalıştırır; çıktı ham baytlardır."""
    return run_preemptible_process(
        [
            "curl", "--fail-with-body", "--show-error", "--silent", "--location", "--compressed",
            "--user-agent", STANDARD_CHROME_USER_AGENT,
            "--proto", "=http,https", "--proto-redir", "=http,https",
            "--max-time", "15", "--", url,
        ],
        env=child_environment(), capture_output=True, text=False, timeout=25,
    )


def _fetch_with_retries(url: str) -> subprocess.CompletedProcess[bytes]:
    """
    curl'ü çalıştırır; yalnız geçici ağ hatası çıkış kodlarında (_CURL_TRANSIENT_EXIT_CODES) en çok FETCH_MAX_RETRIES kez,
    FETCH_RETRY_WAIT_SECONDS bekleyerek yeniden dener; her yeniden deneme yapısal uyarı loglar. Sonuncusu da başarısızsa
    onun sonucu döner (çağıran son hatayı yükseltir).
    """
    check_read_stop()
    result: subprocess.CompletedProcess[bytes] = _run_curl(url)
    for attempt in range(1, FETCH_MAX_RETRIES + 1):
        if result.returncode not in _CURL_TRANSIENT_EXIT_CODES:
            break
        logging.warning(
            "Geçici ağ hatası; fetch_raw yeniden denenecek",
            extra={"host": urlsplit(url).hostname, "exit_code": result.returncode, "attempt": attempt,
                   "max_retries": FETCH_MAX_RETRIES, "wait_seconds": FETCH_RETRY_WAIT_SECONDS},
        )
        read_retry_delay(FETCH_RETRY_WAIT_SECONDS)
        check_read_stop()
        result = _run_curl(url)
    return result


def fetch_raw_content(url: str) -> str:
    try:
        parsed = urlsplit(url)
    except ValueError as error:  # ör. 'http://[abc': kapanmayan IPv6 köşeli parantezi ham ValueError'dı
        raise ToolError(f"fetch_raw adresi ayrıştırılamadı: {url[:200]!r} ({error}).", "INVALID_URL", False) from error
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ToolError("fetch_raw yalnızca http(s) kabul eder.", "INVALID_URL", False)
    result = _fetch_with_retries(url)
    stdout = _decode_http_output(result.stdout)
    stderr = _decode_http_output(result.stderr)
    if result.returncode != 0:
        body = stdout.strip()
        is_markup: bool = not body.startswith(("{", "[")) and "<" in body[:200]
        visible: str = clean_html(body) if is_markup else body
        challenge: Optional[AccessChallenge] = (
            _error_body_challenge(body, visible, _curl_http_status(stderr)) if is_markup else None
        )
        if is_markup:
            _raise_if_access_wall("fetch_raw", url, body, challenge)
        shown = _clip(visible, FETCH_ERROR_BODY_LIMIT)
        # Bypass modunda engelin ne olduğu ve ne yapılacağı hata metnine eklenir: model engeli başka
        # araçla denemek yerine stratejiyi uygular (ör. browse_url ile sayfayı açıp kutuyu tıklar).
        bypass_hint: str = f"\n\n{bypass_note(challenge)}" if challenge is not None and is_bypass_enabled() else ""
        raise ToolError(
            f"HTTP başarısız: url={url}, çıkış={result.returncode}, stderr={stderr.strip()}"
            f"{' yanıt=' + shown if shown else ''}{bypass_hint}",
            "FETCH_FAILED",
            True,
        )
    # Capture successful bodies in full; event/model/archive limits apply downstream.
    stripped = stdout.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        return stdout
    text: str = clean_html(stdout, limit=None)
    challenge: Optional[AccessChallenge] = classify_access_challenge(stdout, text)
    _raise_if_access_wall("fetch_raw", url, stdout, challenge)
    return text + _challenge_note(url, challenge)

# osascript stderr'inde Apple Events otomasyon reddi: kalıcıdır (kullanıcı ayarı değişene kadar her çağrı aynı hatayı verir)
_AUTOMATION_DENIED: re.Pattern[str] = re.compile(r"[(]-174[34][)]")
_AUTOMATION_HELP: str = (
    "Sistem Ayarları > Gizlilik ve Güvenlik > Otomasyon altında OmniAgent'ı çalıştıran uygulamaya Google Chrome izni ver"
)
_APPLESCRIPT_UNAVAILABLE: str = (
    f"AppleScript bu oturumda kullanılamıyor (Chrome otomasyon izni reddedildi ya da osascript çalıştırılamadı); {_AUTOMATION_HELP}"
)


def automation_denied(stderr: str) -> bool:
    """osascript hatası Apple Events otomasyon reddini (-1743/-1744) bildiriyor mu; kalıcı bir durumdur. Saf."""
    return _AUTOMATION_DENIED.search(stderr) is not None


def has_control_character(text: str) -> bool:
    """Metinde NUL/satır sonu gibi kontrol karakteri var mı (subprocess ValueError'ı ve adrese Enter sızması). Saf."""
    return any(ord(character) < 32 or ord(character) == 127 for character in text)


def run_chrome_active_tab(
    url: Optional[str], applescript_available: Optional[bool], new_tab: bool = False,
) -> Tuple[str, Optional[bool]]:
    """
    Açık Chrome sekmesini AppleScript ile bulur/gezdirir; dönen ikinci değer yeni bayrak değeridir.
    applescript_available: None=kalıcı sonuç yok, True=çalıştı, False=KALICI olarak yok (otomasyon izni reddi
    -1743/-1744 ya da osascript çalıştırılamadı); False iken AppleScript denenmez, görünür UI yoluna geçilir.
    Geçici betik hatası yalnız o çağrıyı görünür UI yoluna düşürür, bayrağı değiştirmez. Süre aşımı HATADIR:
    betik gezinmeyi bekleme döngüsünden ÖNCE yaptığı için UI'den yeniden gezinmek çift sekme/çift yükleme
    üretir ve yanıtsız ya da modal Chrome'a körlemesine tuş gönderilirdi. UI yolunda tuşlar yalnız Chrome
    ön plana geldikten sonra gider.
    """
    if url and has_control_character(url):
        raise ToolError(f"Chrome adresi kontrol karakteri içeremez: {url!r}", "INVALID_URL", False)
    try:
        parsed = urlsplit(url) if url else None
    except ValueError as error:  # ör. 'http://[oops/': kapanmayan IPv6 köşeli parantezi
        raise ToolError(f"Chrome adresi ayrıştırılamadı: {url!r} ({error})", "INVALID_URL", False) from error
    if parsed and (parsed.scheme not in ("https", "http") or not parsed.netloc):
        raise ToolError("Chrome sekmesi için http(s) adresi ver.", "INVALID_URL", False)
    if new_tab and not url:
        raise ToolError("Yeni Chrome sekmesi için http(s) adresi ver.", "INVALID_URL", False)
    origin = f"{parsed.scheme}://{parsed.netloc}/" if parsed else ""

    available: Optional[bool] = applescript_available
    fallback_reason: Optional[str] = _APPLESCRIPT_UNAVAILABLE if applescript_available is False else None
    result: Optional[subprocess.CompletedProcess[str]] = None

    if applescript_available is not False:
        try:
            result = run_preemptible_process(
                ["osascript", "-e", _CHROME_TAB_APPLESCRIPT, url or "", origin,
                 str(CHROME_LOAD_WAIT_SECONDS), str(new_tab).lower()],
                env=child_environment(), capture_output=True, text=True,
                timeout=CHROME_SCRIPT_TIMEOUT_SECONDS, check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise ToolError(
                f"Chrome betiği {CHROME_SCRIPT_TIMEOUT_SECONDS:.0f} sn içinde yanıt vermedi (url={url}, new_tab={new_tab}). "
                "Sekme açılmış ve sayfa yükleniyor olabilir: take_screenshot ile durumu gör; aynı çağrıyı new_tab=true ile "
                "TEKRARLAMA (ikinci sekme açar). Chrome'da ya da ekranda açık bir izin/iletişim penceresi varsa kullanıcıya bildir.",
                "CHROME_SCRIPT_TIMEOUT", True,
            ) from error
        except OSError as error:
            available, fallback_reason = False, f"osascript çalıştırılamadı ({type(error).__name__}: {error})"
        else:
            if result.returncode == 0:
                available = True
            elif automation_denied(result.stderr):
                available = False
                fallback_reason = f"Chrome otomasyon izni yok ({result.stderr.strip()}); {_AUTOMATION_HELP}"
            else:
                fallback_reason = result.stderr.strip() or f"çıkış={result.returncode}"

    if fallback_reason:
        logging.warning(
            "Chrome AppleScript kullanılamadı; görünür UI yoluna geçiliyor",
            extra={"reason": fallback_reason, "applescript_available": available},
        )
        _require_accessibility()
        try:
            activated = subprocess.run(["open", "-a", CHROME_APP_NAME], env=child_environment(), capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise ToolError(f"Açık Chrome görünür UI fallback başarısız: {type(e).__name__}", "CHROME_SESSION_FAILED", True) from e
        if activated.returncode != 0:
            # Chrome öne gelmediyse ⌘L + URL + Enter öndeki başka uygulamaya (ör. Terminal) yazılırdı
            raise ToolError(
                f"Açık Chrome görünür UI fallback başarısız: {activated.stderr.strip() or activated.returncode}",
                "CHROME_SESSION_FAILED", True,
            )
        # `open -a` Chrome'un öne geldiğini beklemeden döner (soğuk açılış saniyeler sürer): tuşlar yalnız Chrome önde iken gider
        require_front_app(CHROME_APP_NAME, APP_ACTIVATION_WAIT_SECONDS)
        if url:
            if new_tab:
                press_key_spec("cmd+t")
            press_key_spec("cmd+l"); type_unicode_text(url); press_key_spec("enter")
            kind = "Yeni Chrome sekmesi" if new_tab else "Görünür Chrome sekmesi"
            return f"{kind}: {url}\nBaşlık: görünür UI fallback ({fallback_reason}); yükleme otomatik gözlemle doğrulanacak.", available
        return f"Görünür Chrome öne getirildi.\nBaşlık: görünür UI fallback ({fallback_reason}); etkin URL AppleScript olmadan okunamadı.", available

    if result is None: raise ToolError("Açık Chrome sekmesine erişilemedi.", "CHROME_SESSION_FAILED", True)
    lines = result.stdout.rstrip("\n").split("\n")
    if len(lines) < 3: raise ToolError(f"Chrome sekme yanıtı beklenmeyen biçimde: {result.stdout!r}", "CHROME_SESSION_FAILED", True)
    loading = "\nSayfa hâlâ yükleniyor." if lines[-1] == "true" else ""
    kind = "Yeni Chrome sekmesi" if new_tab else "Görünür Chrome sekmesi"
    return f"{kind}: {lines[0]}\nBaşlık: {' '.join(lines[1:-1])}{loading}", available

# Playwright tarayıcı ikilisi kurulu değilse her browse_url çağrısı ham "Executable doesn't exist"
# hatasıyla düşüyordu; model de kurulum komutlarını deneyip boşa tur harcıyordu. Eksik motoru
# tanıyıp kısa ve uygulanabilir bir ToolError üret (Toolbox.browse_url salt okumada fetch_raw'a düşer).
_BROWSER_MISSING_MARKERS: Tuple[str, ...] = (
    "executable doesn't exist",
    "browser executable",
    "please run the following command",
)


def browser_launch_error(error: PlaywrightError) -> ToolError:
    """Playwright başlatma hatasını eksik motor / diğer hata diye sınıflar. Saf."""
    rendered: str = str(error)
    if any(marker in rendered.casefold() for marker in _BROWSER_MISSING_MARKERS):
        return ToolError(
            "Tarayıcı motoru (Playwright Chromium) bu kurulumda yok; browse_url açılamaz. "
            "Statik sayfaları fetch_raw ile oku; kurulum komutunu kendi başına deneme. "
            "Etkileşimli sayfa gerçekten zorunluysa bunu kullanıcıya bildir.",
            "BROWSER_UNAVAILABLE",
            False,
        )
    return ToolError(
        f"Tarayıcı başlatılamadı: {_clip(rendered, 400)}", "BROWSER_LAUNCH_FAILED", True,
    )


def browser_user_agent(chromium_version: str) -> str:
    """Motorun gerçek sürümünü taşıyan standart Chrome User-Agent'ı üretir; ürün belirteci eklenmez. Saf."""
    return (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{chromium_version} Safari/537.36"
    )


class HeadlessBrowserSession:
    def __init__(self) -> None:
        self.playwright_instance: Optional[Playwright] = None
        self.browser: Optional[Browser] = None
        self.browser_context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self._lock: asyncio.Lock = asyncio.Lock()

    async def _discard(self) -> None:
        """Yarıda kalan Playwright başlatmasını temizler (kilidi çağıran taraf tutar)."""
        instance: Optional[Playwright] = self.playwright_instance
        self.playwright_instance = self.browser = self.browser_context = self.page = None
        if instance is not None:
            try:
                await instance.stop()
            except Exception:  # Sürücü ölmüşse temizlik hatası başlatma hatasını gölgelemesin
                logging.debug("Playwright örneği kapatılamadı", exc_info=True)

    async def get_page(self) -> Page:
        async with self._lock:
            if self.browser_context is None:
                try:
                    self.playwright_instance = await async_playwright().start()
                    if USE_CHROME_PROFILE:
                        # Persistent context: ajanın KENDİ profil dizini. Çerezler, giriş durumu ve eklentiler
                        # oturumlar arasında korunur; bot algılama sistemleri normal bir kullanıcı profili görür.
                        # Kullanıcının GERÇEK Chrome profili BİLEREK kullanılmaz: Chrome açıkken o dizin kilitlenir
                        # (ProcessSingleton) ve kullanıcının oturumu riske girer. Profil ilk kez oluşurken gerçek
                        # profilden en iyi çaba kopyalanır (seed_chrome_profile), sonra ajan kendi kopyasıyla çalışır.
                        # Not: persistent context'te Browser nesnesi yoktur; self.browser None kalır.
                        profile_path: str = get_chrome_profile_path()
                        os.makedirs(profile_path, exist_ok=True)
                        seed_chrome_profile(profile_path)
                        # channel="chrome" sistem Chrome'unu kullanır; headless=True başsız çalışır
                        self.browser_context = await self.playwright_instance.chromium.launch_persistent_context(
                            user_data_dir=profile_path,
                            headless=True,
                            channel="chrome",
                            args=[
                                "--disable-blink-features=AutomationControlled",
                                "--disable-features=IsolateOrigins,site-per-process",
                                "--no-sandbox",
                                "--disable-setuid-sandbox",
                            ],
                            ignore_default_args=["--enable-automation"],
                        )
                    else:
                        self.browser = await self.playwright_instance.chromium.launch(
                            headless=True,
                            args=[
                                "--disable-blink-features=AutomationControlled",
                                "--disable-features=IsolateOrigins,site-per-process",
                            ],
                            ignore_default_args=["--enable-automation"],
                        )
                        self.browser_context = await self.browser.new_context(
                            user_agent=browser_user_agent(self.browser.version),
                        )
                    # Stealth: navigator.webdriver ve diğer fingerprint'leri gizle
                    await self.browser_context.add_init_script(_STEALTH_INIT_SCRIPT)
                except PlaywrightError as error:
                    # Eksik motor kalıcı bir durumdur; yarım kalan sürücüyü bırakma ki
                    # sonraki çağrı aynı hatayı hızlı ve deterministik biçimde versin.
                    await self._discard()
                    raise browser_launch_error(error) from error
            if self.page is None or self.page.is_closed():
                # Persistent context'te pages listesi direkt context'tedir
                if USE_CHROME_PROFILE and self.browser_context.pages:
                    self.page = self.browser_context.pages[0]
                else:
                    self.page = await self.browser_context.new_page()
                self.page.set_default_timeout(PAGE_ACTION_TIMEOUT_MS)
                self.page.set_default_navigation_timeout(PAGE_LOAD_TIMEOUT_MS)
            return self.page

    async def browse(
        self, url: Optional[str], actions: List[BrowserAction],
        progress: Optional[Callable[[str], None]] = None,
    ) -> str:
        page = await self.get_page()
        return await browse_page_actions(page, url, actions, progress)

    async def close(self) -> None:
        async with self._lock:
            # Önce context: persistent context'te Browser nesnesi yoktur (self.browser None) ve
            # profil dizininin kilidi ancak context kapanınca bırakılır.
            if self.browser_context: await self.browser_context.close()
            if self.browser: await self.browser.close()
            if self.playwright_instance: await self.playwright_instance.stop()
            self.browser = self.browser_context = self.page = None

def _progress(sink: Optional[Callable[[str], None]], text: str) -> None:
    """İlerleme geri çağrısını güvenle çağırır; yayın hatası tarayıcı akışını bozmasın."""
    if sink is None:
        return
    try:
        sink(text)
    except Exception:  # Arayüz/köprü hatası tarayıcı eylemini düşürmemeli.
        logging.debug("Tarayıcı ilerleme olayı yayınlanamadı", exc_info=True)


# Meta-refresh gibi anında yönlenen sayfada Playwright içeriği "sayfa yönleniyor" hatasıyla
# okutmaz; yönlenme bitince yeniden okumak yeni sayfayı verir. Bu kadar yeniden denemeden sonra
# hata olduğu gibi yükselir.
_PAGE_CONTENT_RETRIES: int = 2
_PAGE_NAVIGATING_MARKER: str = "page is navigating"


async def _read_page_content(page: Page) -> str:
    """
    Sayfanın HTML'ini okur. Sayfa o an yönleniyorsa yönlenme bitene dek bekleyip uyarıyla yeniden
    dener; son deneme hatayı olduğu gibi yükseltir. Başka hiçbir Playwright hatası yutulmaz.
    """
    for attempt in range(1, _PAGE_CONTENT_RETRIES + 1):
        try:
            return await page.content()
        except PlaywrightError as error:
            if _PAGE_NAVIGATING_MARKER not in str(error):
                raise
            logging.warning("Sayfa yönleniyor, içerik yeniden okunacak", extra={"attempt": attempt})
            await page.wait_for_load_state("domcontentloaded")
    return await page.content()


async def _inspect_page(
    page: Page, progress: Optional[Callable[[str], None]],
) -> Tuple[str, Optional[AccessChallenge]]:
    """
    Sayfanın temiz metnini ve varsa doğrulama belirtecini okur. Bypass modunda duvar sayfası hata değildir:
    ilerlemeye "bypass ile devam" satırı yazılır ve içerik modele verilir (strategi notu _challenge_note'ta).
    Sıkı modda engel sayfası ToolError olarak yükseltilir. Dönen belirteç her iki modda da gömülü CAPTCHA
    bileşeni ya da yerel adreste hata sayılmayan erişim-reddi sayfasıdır.
    """
    content: str = await _read_page_content(page)
    text: str = await asyncio.to_thread(clean_html, content)
    challenge: Optional[AccessChallenge] = classify_access_challenge(content, text)
    try:
        _raise_if_access_wall("browse_url", page.url, content, challenge)
    except ToolError:
        _progress(progress, f"✗ erişim engeli veya bot doğrulaması: {page.url}\n")
        raise
    if challenge is not None and challenge["kind"] == "wall":
        _progress(progress, f"⚠ bot duvarı ({challenge['signal']}); bypass ile devam ediliyor\n")
    return text, challenge


# Bot algılamayı atlatmak için stealth init scripti: navigator.webdriver, plugin listesi, dil
# ve webgl vendor gibi sıkı kontrol edilen özellikleri normal bir Chrome'a benzetir.
_STEALTH_INIT_SCRIPT: str = """
// navigator.webdriver'ı gizle
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

// window.chrome mockla
window.chrome = {
  runtime: {},
  loadTimes: function() {},
  csi: function() {},
  app: { isInstalled: false }
};

// Permissions query'yi override et
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
  parameters.name === 'notifications' ?
    Promise.resolve({ state: Notification.permission }) :
    originalQuery(parameters)
);

// Pluginleri normal Chrome gibi göster
Object.defineProperty(navigator, 'plugins', {
  get: () => [
    { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
    { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
    { name: 'Native Client', filename: 'internal-nacl-plugin', description: '' }
  ],
});

// Dilleri normal Chrome gibi ayarla
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });

// WebGL vendor'ı normal Chrome gibi ayarla (bot korumalar sıkça bunu kontrol eder)
const getParameterOriginal = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function(parameter) {
  if (parameter === 37445) return 'Intel Inc.';
  if (parameter === 37446) return 'Intel Iris OpenGL Engine';
  return getParameterOriginal.call(this, parameter);
};

// Connection RTT'sini normal bir kullanıcı gibi göster
Object.defineProperty(navigator.connection, 'rtt', { get: () => 100 });
"""

# Persistent context için varsayılan profil dizini (macOS): ajanın KENDİ kopyası. Kullanıcının gerçek
# Chrome profili buraya kopyalanır (seed_chrome_profile), kendisi açılmaz: Chrome açıkken gerçek dizin
# kilitlenir (ProcessSingleton: "profile directory is already in use") ve kullanıcının oturumu riske girer.
_DEFAULT_CHROME_PROFILE_PATH: str = (
    "~/Library/Application Support/OmniAgent/chrome-profile"
)
# Kopyalanacak kaynak: kullanıcının gerçek Chrome profili (yalnız İLK çalıştırmada, en iyi çaba, salt okunur).
_USER_CHROME_PROFILE_PATH: str = "~/Library/Application Support/Google/Chrome"
# Çerez/giriş/tercih dosyaları: bot doğrulaması boş profil ister istemez şüpheli bulur, bu yüzden taşınır.
_SEED_SUBPATHS: Tuple[str, ...] = (
    "Local State",
    "Default/Preferences",
    "Default/Cookies",
    "Default/Network/Cookies",
    "Default/Login Data",
    "Default/Web Data",
    "Default/Local Storage",
    "Default/Session Storage",
)

# Persistent context kullanılsın mı? Bot duvarlarını aşan gerçek profil izlenimi için varsayılan AÇIK.
USE_CHROME_PROFILE: bool = True
# Profil gerçek Chrome'dan tohumlansın mı (kapatmak için False: boş/izole profil).
SEED_FROM_USER_CHROME_PROFILE: bool = True
CHROME_PROFILE_PATH: Optional[str] = None  # None ise varsayılan kullanılır


def get_chrome_profile_path() -> str:
    """Persistent context için kullanılacak profil dizinini döner (ajanın kendi kopyası; gerçek profil değil)."""
    raw: str = CHROME_PROFILE_PATH or _DEFAULT_CHROME_PROFILE_PATH
    return os.path.expanduser(raw)


def _seed_enabled() -> bool:
    """
    Tohumlama açık mı? SEED_FROM_USER_CHROME_PROFILE sabittir; OMNI_SEED_CHROME_PROFILE ortam değişkeni
    onu geçersiz kılar (0/false/off = kapalı, 1/true/on = açık). Testler kapalı tutar: geliştiricinin
    gerçek çerezleri test kopyasına taşınmaz. Saf.
    """
    override: str = os.environ.get("OMNI_SEED_CHROME_PROFILE", "").strip().casefold()
    if override in ("0", "false", "no", "off"):
        return False
    if override in ("1", "true", "yes", "on"):
        return True
    return SEED_FROM_USER_CHROME_PROFILE


def seed_chrome_profile(profile_path: str) -> None:
    """
    Ajanın profil dizini ilk kez oluşuyorsa kullanıcının gerçek Chrome profilinden çerez/giriş/tercih
    dosyalarını (_SEED_SUBPATHS) kopyalar; böylece "yeni kurulmuş boş profil" izi oluşmaz. Yalnız hedef
    dizinde 'Local State' yokken (yani hiç tohumlanmamışsa) çalışır, kaynak dizine ASLA yazmaz/silmez ve
    hatalar en iyi çaba olarak günlüğe yazılır (Chrome açıkken bir dosya okunamayabilir; başlatma sürer).
    """
    if not _seed_enabled():
        return
    source: str = os.path.expanduser(_USER_CHROME_PROFILE_PATH)
    if not os.path.isdir(source) or os.path.exists(os.path.join(profile_path, "Local State")):
        return
    for relative in _SEED_SUBPATHS:
        origin: str = os.path.join(source, relative)
        if not os.path.exists(origin):
            continue
        destination: str = os.path.join(profile_path, relative)
        try:
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            if os.path.isdir(origin):
                shutil.copytree(origin, destination, dirs_exist_ok=True)
            else:
                shutil.copy2(origin, destination)
        except (OSError, shutil.Error) as error:
            logging.debug("Chrome profili kopyalanamadı", extra={"path": relative, "error": str(error)})



# click hedefinin kullanıcıya görünen adayları (görünen metin, aria-label, value, title, alt)
_CLICK_LABELS_SCRIPT: str = """
(element) => [element.innerText, element.getAttribute('aria-label'), element.value,
  element.getAttribute('title'), element.getAttribute('alt')]
  .filter((value) => typeof value === 'string' && value.trim() !== '')
  .map((value) => value.trim().slice(0, 200))
"""
# Onay isteğinde gösterilecek tutar satırları sayfa metninin bu kadar karakterinden çıkarılır
_PAGE_AMOUNT_SCAN_CHARS: int = 20000


async def _click_labels(page: Page, selector: str) -> List[str]:
    """click hedefinin etiketleri; öğe yoksa Playwright zaman aşımı yükselir (click de aynı hatayı verirdi)."""
    raw: List[str] = await page.locator(selector).first.evaluate(_CLICK_LABELS_SCRIPT)
    return [" ".join(label.split()) for label in raw]


async def confirm_click_target(page: Page, selector: str) -> None:
    """
    click'ten ÖNCE seçicinin gerçek etiketlerini okur. İnsan/bot doğrulama kutularına ("I'm not a robot"
    gibi) tıklamak artık SERBEST — bypass modunda ajan bunları tıklayabilir. Ödeme/sipariş onayı
    gibiyse host onayı ister; onay beklerken sayfa değiştiyse tıklamaz (ApprovalRefused).
    Host bağlamı yoksa hiçbir şey yapmaz ve DOM okunmaz.
    """
    gate: Optional[Callable[[ApprovalRequest], Awaitable[None]]] = approval_gate_async()
    if gate is None:
        return
    labels: List[str] = await _click_labels(page, selector)

    if not any(financial_cta_reason(item) is not None or is_generic_commit_label(item)
               or communication_click_label(item) for item in labels):
        return
    page_text: str = await page.evaluate(
        "(limit) => (document.body ? document.body.innerText : '').slice(0, limit)", _PAGE_AMOUNT_SCAN_CHARS,
    )
    context: List[str] = page_text.splitlines()
    found: Optional[Tuple[str, str]] = next(
        ((item, reason) for item in labels if (reason := click_financial_reason(item, context)) is not None), None,
    )
    if found is None:
        label = next((item for item in labels if communication_click_label(item)), None)
        if label is None:
            return
        draft = await page.locator("textarea, [contenteditable='true']").evaluate_all(
            "els => els.map(el => el.value || el.innerText || '').filter(Boolean).join('\\n\\n')",
        )
        request = gui_communication_request("browse_url", label, page.url, draft)
    else:
        label, reason = found
        request = None
    target: ClickTarget = {
        "tool": "browse_url", "label": label, "requested": selector, "where": page.url,
        "amounts": amount_lines(context, AMOUNT_LINES_LIMIT),
    }
    try:
        await gate(request if request is not None else gui_click_request(target, reason))
    except ToolError as error:
        raise ApprovalRefused(
            f"{error} Tıklanmayan öğe: {label!r} ({selector}). {NO_CIRCUMVENTION_NOTE}", error.code, error.recoverable,
        ) from error
    if label not in await _click_labels(page, selector):
        raise ApprovalRefused(
            f"Onay beklenirken sayfa değişti; {label!r} öğesi artık bu seçicide değil. Tıklanmadı.",
            TARGET_CHANGED_CODE, True,
        )


async def browse_page_actions(
    page: Page, url: Optional[str], actions: List[BrowserAction],
    progress: Optional[Callable[[str], None]] = None,
) -> str:
    """
    Sayfayı açar, sırayla eylemleri uygular ve metin + etkileşimli öğe listesini döner.

    `progress` verilirse gezinme ve her eylem adımı canlı olarak bildirilir: browse_url arka
    plandaki ayrı Chromium'da çalışırken kullanıcı ajanın ne yaptığını görebilsin.
    """
    if url:
        _progress(progress, f"→ sayfa açılıyor: {url}\n")
        try: await page.goto(url, wait_until="domcontentloaded")
        except PlaywrightError as e:
            _progress(progress, f"✗ sayfa açılamadı: {url}\n")
            raise ToolError(f"Sayfa açılamadı: url={url}, ayrıntı={e}", "PAGE_LOAD_FAILED", True) from e
    elif page.url == "about:blank":
        raise ToolError("Açık sayfa yok; ilk çağrıda url ver.", "NO_PAGE", False)
    else:
        _progress(progress, f"→ mevcut sayfa: {page.url}\n")

    if actions:
        # Eylemler bir doğrulama/engel sayfasına tıklamasın veya yazmasın: önce sayfayı denetle.
        await _inspect_page(page, progress)

    for i, action in enumerate(actions):
        kind, selector, value = action.get("action"), str(action.get("selector") or ""), action.get("value")
        _progress(progress, f"→ eylem {i + 1}/{len(actions)}: {kind} {selector}\n")
        try:
            if kind == "click":
                await confirm_click_target(page, selector)
                await page.click(selector)
            # Boş metinle fill alanı temizler; yalnız değer hiç yoksa geçersizdir
            elif kind == "fill" and value is not None:
                _progress(progress, f"  yazılan değer: {str(value)[:120]}\n")
                await page.fill(selector, value)
            elif kind == "press" and value is not None: await page.press(selector, normalize_browser_key(value))
            elif kind == "wait_for":
                state = value if value in ("visible", "hidden", "attached", "detached") else "visible"
                await page.locator(selector).wait_for(state=state)
            else:
                raise ToolError(
                    f"Geçersiz tarayıcı eylemi {i}: {action} (click: selector; fill/press: selector + value).",
                    "INVALID_BROWSER_ACTION", False,
                )
        except PlaywrightTimeoutError as e:
            _progress(progress, f"✗ eylem {i + 1} başarısız: {kind} {selector}\n")
            elements = await page.evaluate(_PAGE_ELEMENTS_SCRIPT, PAGE_ELEMENT_LIMIT)
            raise ToolError(
                f"Tarayıcı eylemi {i} ({kind} {selector}) {PAGE_ACTION_TIMEOUT_MS}ms içinde yapılamadı "
                "(öğe yok/görünmez). Sayfadaki öğeler:\n" + "\n".join(elements),
                "BROWSER_ACTION_TIMEOUT", True,
            ) from e

    if actions: await page.wait_for_load_state("domcontentloaded")
    text, challenge = await _inspect_page(page, progress)
    elements = await page.evaluate(_PAGE_ELEMENTS_SCRIPT, PAGE_ELEMENT_LIMIT)
    title = " ".join((await page.title()).split())
    _progress(progress, f"✓ sayfa hazır: {title or '(başlıksız)'} — {len(elements)} etkileşimli öğe\n")
    # Gömülü CAPTCHA (veya yerel erişim-reddi) notu SONA eklenir: experience._read_browser_result ilk 5 satır düzenini bekler.
    notice: str = _challenge_note(page.url, challenge)
    return (
        "Tarayıcı: arka planda çalışan ayrı Chromium; açık Google Chrome oturumunda görünmez.\n"
        f"URL: {page.url}\nBaşlık: {title}\n\nSAYFA METNİ:\n{text}\n\n"
        'ÖĞELER (seçici — tür "etiket"):\n' + "\n".join(elements) + notice
    )
