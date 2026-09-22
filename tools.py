import asyncio
import subprocess
import os
import json
import logging
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
import cv2
from playwright.async_api import async_playwright, Browser, BrowserContext, Page
import pyautogui
from PIL import ImageGrab
from bs4 import BeautifulSoup
from typing import Any, Dict, List, Optional, Tuple

# pyautogui varsayılanı her çağrıdan sonra 0.1sn bekler (FAILSAFE tepki payı için).
# Bunu tam sıfırlamak yerine düşürüyoruz: uzun bir keyboard_type çağrısı karakter
# başına bu bekleme payını taşıdığı için varsayılanla saniyelerce sürebiliyor.
pyautogui.PAUSE = 0.02

# Araç sonuçları modele gider: her bayt token demektir. Uzun çıktılar kırpılır.
SHELL_STDOUT_LIMIT: int = 4000
SHELL_STDERR_LIMIT: int = 1000
FILE_READ_LIMIT: int = 8000
TYPED_TEXT_ECHO_LIMIT: int = 80
SCREENSHOT_MAX_EDGE: int = 1600
BACKUP_KEEP_PER_FILE: int = 5


def _clip(text: str, limit: int) -> str:
    """Metni belirtilen uzunlukta kırpıp kısaltma bilgisini ekler (model kopsa da bilir)."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[kısaltıldı, toplam {len(text)} karakter]"

# Ajanın kendi kendini iyileştirmesi için yapılandırılmış hata sınıfı
class ToolError(Exception):
    def __init__(self, message: str, code: str, recoverable: bool) -> None:
        super().__init__(message)
        self.code: str = code
        self.recoverable: bool = recoverable

# --- Güvenlik Rayları (Safety Rails) ---
# Bunlar bir sandbox DEĞİLDİR; bilinen en yıkıcı kalıpları engelleyen,
# en iyi çaba (best-effort) bir koruma katmanıdır.

PROJECT_ROOT: Path = Path(__file__).resolve().parent
BACKUP_DIR: Path = PROJECT_ROOT / ".omni_backups"

_SENSITIVE_PATH_PREFIXES: Tuple[Path, ...] = (
    Path.home() / ".ssh",
    Path.home() / ".aws",
    Path.home() / ".gnupg",
    Path.home() / ".zshrc",
    Path.home() / ".zprofile",
    Path.home() / ".zshenv",
    Path.home() / ".bashrc",
    Path.home() / ".bash_profile",
    Path.home() / ".profile",
    Path("/etc"),
    Path("/private/etc"),
    Path("/System"),
    Path("/Library"),
    Path("/usr"),
    Path("/bin"),
    Path("/sbin"),
    Path("/var/root"),
)

_CATASTROPHIC_SHELL_PATTERNS: Tuple[str, ...] = (
    r"rm\s+-\w*[rR]\w*[fF]\w*\s+(/|~|\$HOME|/\*)\s*$",
    r"rm\s+-\w*[fF]\w*[rR]\w*\s+(/|~|\$HOME|/\*)\s*$",
    r"\bmkfs\b",
    r"\bdd\b[^\n]*of=/dev/",
    r":\(\)\s*{\s*:\|:&\s*};\s*:",
    r"\bdiskutil\s+(erase|partition)",
    r">\s*/dev/(disk|sda|rdisk)",
    r"chmod\s+-R\s+(000|777)\s+/\s*$",
    r"\bsudo\s+rm\s+-\w*[rR]",
)

def _logical_path(path: Path) -> Path:
    """
    macOS firmlink tuzağını çözer: kullanıcı yolları çözümlenince
    `/System/Volumes/Data/...` altına düşer; bu bir SİSTEM yolu değil, veri
    biriminin arka plan yoludur. Karşılaştırmayı mantıksal yol üzerinden yapmak,
    `/System` korumasının yanlışlıkla `/home`, `/Users`, `/tmp` gibi yolları
    engellemesini (false positive) önler.
    """
    resolved: Path = path.expanduser().resolve()
    posix: str = resolved.as_posix()
    marker: str = "/System/Volumes/Data"
    if posix == marker:
        return Path("/")
    if posix.startswith(marker + "/"):
        return Path(posix[len(marker):])
    return resolved

def _is_sensitive_path(path: Path) -> bool:
    """Hedef yolun korunan sistem/kimlik dosyalarından biri olup olmadığını denetler."""
    resolved: Path = _logical_path(path)
    for raw_prefix in _SENSITIVE_PATH_PREFIXES:
        prefix: Path = _logical_path(raw_prefix.expanduser())
        if resolved == prefix or prefix in resolved.parents:
            return True
    return False

def _sensitive_write_allowed() -> bool:
    """
    Hassas yol koruması varsayılan olarak kapalı bırakılır. Kullanıcı, betiği
    ÇALIŞTIRMADAN ÖNCE kendi ortamında OMNI_ALLOW_SENSITIVE_WRITE=1 ayarladıysa
    bu çalıştırma boyunca gevşetilir. Ajan bunu kendi kendine açamaz:
    execute_shell alt-süreçlerinin `export` gibi ortam değişiklikleri bu Python
    sürecine geri sızmaz, ve hiçbir araç os.environ'ı programatik olarak
    değiştirmez — karar her zaman kullanıcıda kalır.
    """
    return os.environ.get("OMNI_ALLOW_SENSITIVE_WRITE") == "1"

def _is_catastrophic_command(command: str) -> bool:
    """Bilinen yıkıcı kabuk komutu kalıplarını tespit eder (en iyi çaba kontrolü)."""
    normalized: str = " ".join(command.split())
    return any(re.search(pattern, normalized, re.IGNORECASE) for pattern in _CATASTROPHIC_SHELL_PATTERNS)

# write_file/self_modify hassas yolları reddediyor; execute_shell'in de aynı korumaya
# ihtiyacı var, yoksa `echo x > ~/.ssh/...` gibi bir yönlendirme aynı korumayı atlatır.
_SHELL_REDIRECT_TARGET_PATTERN = re.compile(r"(?:>{1,2}|\btee\b(?:\s+-a)?)\s+(~?/?[^\s;|&<>]+)")

def _shell_writes_to_sensitive_path(command: str) -> bool:
    """Komut metnindeki `>`, `>>` veya `tee` hedeflerinden herhangi biri korunan bir yola mı yazıyor."""
    for match in _SHELL_REDIRECT_TARGET_PATTERN.finditer(command):
        target: str = match.group(1).strip("'\"")
        if not target:
            continue
        if _is_sensitive_path(Path(target)):
            return True
    return False

def _backup_file(path: Path) -> Path:
    """
    Üzerine yazılmadan önce mevcut dosyanın zaman damgalı yedeğini alır.
    Aynı dosya başına yalnızca son BACKUP_KEEP_PER_FILE yedek tutulur; uzun
    otonom oturumlarda disk sızıntısını önler.
    """
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp: str = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    backup_path: Path = BACKUP_DIR / f"{path.name}.{stamp}.bak"
    backup_path.write_bytes(path.read_bytes())
    existing: List[Path] = sorted(BACKUP_DIR.glob(f"{path.name}.*.bak"))
    stale: List[Path] = existing[:-BACKUP_KEEP_PER_FILE] if len(existing) > BACKUP_KEEP_PER_FILE else []
    for old in stale:
        old.unlink()
    return backup_path

# Bilgisayar Kullanım Ajansı (CUA) - macOS GUI etkileşimleri
class CUA:
    """
    macOS GUI etkileşimlerini yöneten konnektör.
    Simüle edilmiş tıklamalar yerine gerçek OS düzeyinde olaylar kullanır.
    """
    def __init__(self) -> None:
        self.active_apps: Dict[str, bool] = {}

    def get_app(self, app_name: str) -> str:
        """
        Uygulamayı osascript aracılığıyla aktif hale getirir ve odaklanır.
        """
        script: str = f'tell application {json.dumps(app_name)} to activate'
        result: subprocess.CompletedProcess[str] = subprocess.run(
            ["osascript", "-e", script], capture_output=True, text=True, timeout=8,
        )
        if result.returncode == 0:
            self.active_apps[app_name] = True
            return f"{app_name} başarıyla aktif edildi ve odaklanıldı."
        raise ToolError(f"Uygulama bulunamadı veya aktif edilemedi: {app_name}.", "APP_NOT_FOUND", False)

    def click_element(self, app_name: str, element_id: int) -> str:
        """
        Bir AX elementine gerçek tıklama işlemi gerçekleştirir.
        """
        if element_id < 1:
            raise ToolError(f"Geçersiz AX öğe numarası: {element_id}", "INVALID_ELEMENT", False)
        script: str = (
            f'tell application "System Events" to tell process {json.dumps(app_name)} '
            f'to click UI element {element_id} of front window'
        )
        result: subprocess.CompletedProcess[str] = subprocess.run(
            ["osascript", "-e", script], capture_output=True, text=True, timeout=8,
        )
        
        if result.returncode == 0:
            return f"{app_name} içindeki {element_id} numaralı elemente başarıyla tıklandı."
        
        raise ToolError(
            f"AX tıklama başarısız: uygulama={app_name}, öğe={element_id}, ayrıntı={result.stderr.strip()}",
            "AX_CLICK_FAILED", True,
        )

    def press_key(self, app_name: str, key: str) -> str:
        """
        Belirtilen uygulamada bir tuşa basar.
        """
        self.get_app(app_name)
        pyautogui.press(key)
        return f"{app_name} uygulamasında {key} tuşuna basıldı."

    def get_ax_state(self, app_name: str) -> str:
        """
        Uygulama penceresinin Erişilebilirlik (AX) durumunu getirir.
        """
        script: str = (
            f'tell application "System Events" to get name of every window '
            f'of process {json.dumps(app_name)}'
        )
        result: subprocess.CompletedProcess[str] = subprocess.run(
            ["osascript", "-e", script], capture_output=True, text=True, timeout=8,
        )
        if result.returncode != 0:
            raise ToolError(
                f"AX durumu okunamadı: uygulama={app_name}, ayrıntı={result.stderr.strip()}",
                "AX_READ_FAILED", True,
            )
        return f"{app_name} AX Durumu: {result.stdout.strip()}"

# Araç Kutusu (Toolbox) - Sistem ve Web araçları
class Toolbox:
    """
    Sistem komutları, web tarayıcı ve görüntü işleme araçlarını içeren konnektör.
    """
    def __init__(self) -> None:
        self.playwright_instance: Optional[Any] = None
        self.browser: Optional[Browser] = None
        self.browser_context: Optional[BrowserContext] = None
        self.cua: CUA = CUA()
        self._authority_active: bool = False
        self._browser_lock: asyncio.Lock = asyncio.Lock()

    async def _get_browser(self) -> BrowserContext:
        """
        Tarayıcı bağlamını başlatır veya mevcut olanı döner.
        Paralel araç çağrıları aynı anda tetiklerse çift başlatmayı önlemek için kilitlidir.
        """
        async with self._browser_lock:
            if self.browser_context is None:
                self.playwright_instance = await async_playwright().start()
                self.browser = await self.playwright_instance.chromium.launch(headless=True)
                self.browser_context = await self.browser.new_context(
                    user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )
            return self.browser_context

    def _clean_html(self, html: str) -> str:
        """
        HTML içeriğini temizleyerek sadece anlamlı metinleri bırakır.
        """
        soup: BeautifulSoup = BeautifulSoup(html, 'html.parser')
        for element in soup(["script", "style", "meta", "noscript", "header", "footer", "nav"]):
            element.decompose()
        text: str = soup.get_text(separator=' ')
        lines: List[str] = [line.strip() for line in text.splitlines() if line.strip()]
        return _clip(" ".join(lines), 5000)

    def execute_shell(self, command: str, use_sudo: bool) -> str:
        """
        Sistem kabuğunda komut çalıştırır.
        """
        if not command.strip():
            raise ToolError("Boş kabuk komutu çalıştırılamaz.", "EMPTY_COMMAND", False)
        if _is_catastrophic_command(command):
            raise ToolError(
                f"Bilinen yıkıcı komut kalıbıyla eşleşti, çalıştırma engellendi: {command}",
                "CATASTROPHIC_COMMAND_BLOCKED", False,
            )
        if _shell_writes_to_sensitive_path(command):
            if not _sensitive_write_allowed():
                raise ToolError(
                    f"Komut korunan bir sistem/kimlik yoluna yönlendirme yapıyor, engellendi: {command}. "
                    "Kullanıcı bilerek izin vermek isterse OMNI_ALLOW_SENSITIVE_WRITE=1 ile çalıştırmalı.",
                    "SENSITIVE_PATH_BLOCKED", False,
                )
            logging.warning("Hassas yola kabuk yönlendirmesine kullanıcı bayrağıyla izin verildi", extra={"command": command})
        if use_sudo:
            logging.warning("Sudo ile kabuk komutu çalıştırılıyor", extra={"command": command})
        full_cmd: str | List[str] = (
            ["sudo", "-n", "/bin/sh", "-c", command] if use_sudo else command
        )
        try:
            result: subprocess.CompletedProcess[str] = subprocess.run(
                full_cmd, shell=not use_sudo, capture_output=True, text=True,
                timeout=60, stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired as error:
            raise ToolError("Kabuk komutu 60 saniyede tamamlanmadı.", "SHELL_TIMEOUT", True) from error
        if result.returncode != 0:
            # Çoğu araç (npm, git, python, brew) asıl hatayı STDOUT'a basar; model
            # komutu sırf okumak için tekrar çalıştırmasın diye ikisi de taşınır.
            raise ToolError(
                f"Kabuk komutu başarısız: çıkış={result.returncode}, "
                f"stdout={_clip(result.stdout, 1000)}, stderr={_clip(result.stderr, 1000)}",
                "SHELL_EXIT", True,
            )
        return f"STDOUT: {_clip(result.stdout, SHELL_STDOUT_LIMIT)}\nSTDERR: {_clip(result.stderr, SHELL_STDERR_LIMIT)}\nÇıkış Kodu: {result.returncode}"

    def get_window_bounds(self, window_title: str) -> Tuple[int, int, int, int]:
        """
        Belirtilen başlığa sahip pencerenin koordinatlarını (x, y, w, h) döner.
        """
        script: str = (
            f'tell application "System Events" '
            f'to get position and size of window 1 of (first process whose name contains {json.dumps(window_title)})'
        )
        try:
            result: subprocess.CompletedProcess[str] = subprocess.run(
                ["osascript", "-e", script], capture_output=True, text=True, timeout=6,
            )
        except subprocess.TimeoutExpired as error:
            raise ToolError(f"Pencere sorgusu zaman aşımı: {window_title}", "WINDOW_TIMEOUT", True) from error
        if result.returncode != 0:
            raise ToolError(
                f"Pencere boyutları alınamadı: pencere={window_title}, ayrıntı={result.stderr.strip()}",
                "WINDOW_NOT_FOUND", True,
            )
        parts: List[int] = []
        for piece in result.stdout.split(','):
            piece = piece.strip()
            if piece:
                try:
                    parts.append(int(piece))
                except ValueError as error:
                    raise ToolError(
                        f"Pencere koordinatları çözümlenemedi: pencere={window_title}, ham={result.stdout!r}",
                        "WINDOW_PARSE", True,
                    ) from error
        if len(parts) != 4:
            raise ToolError(
                f"Pencere koordinatları eksik: pencere={window_title}, ham={result.stdout!r}",
                "WINDOW_PARSE", True,
            )
        return (parts[0], parts[1], parts[2], parts[3])

    def process_list(self) -> str:
        """
        Sistemdeki aktif süreçlerin yapılandırılmış listesini döner.
        Ham `ps` çıktısı onlarca KB olabilir ve her model turuna binen token
        maliyeti tur süresini doğrudan uzatır; bu yüzden özet + en ağır N süreç
        döndürülür. Tam ps çıktısı execute_shell'in model kırpımından bağımsız
        olarak doğrudan toplanır (kırpım süreç sayısını eksiltmesin).
        """
        result: subprocess.CompletedProcess[str] = subprocess.run(
            ["ps", "-eo", "pid,ppid,user,%cpu,%mem,comm"],
            capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            raise ToolError(
                f"Süreç listesi alınamadı: çıkış={result.returncode}, stderr={result.stderr.strip()}",
                "SHELL_EXIT", True,
            )
        process_lines: List[str] = [ln for ln in result.stdout.splitlines() if ln.strip()]
        if process_lines and process_lines[0].lstrip().startswith("PID"):
            process_lines = process_lines[1:]
        def cpu_key(line: str) -> float:
            parts: List[str] = line.split()
            try:
                return float(parts[3]) if len(parts) > 3 else 0.0
            except ValueError:
                return 0.0
        top: List[str] = sorted(process_lines, key=cpu_key, reverse=True)[:15]
        return (
            f"Toplam süreç sayısı: {len(process_lines)}\n"
            f"En ağır 15 süreç (CPU'ya göre):\n" + "\n".join(top)
        )

    def get_pointer_position(self) -> str:
        """
        Farenin mevcut ekran koordinatlarını (x, y) döner.
        """
        x, y = pyautogui.position()
        return f"Mevcut imleç konumu: x={x}, y={y}"

    def session_authority_status(self) -> str:
        """
        Mevcut oturumun yetki durumunu kontrol eder.
        """
        try:
            self.execute_shell("sudo -n true", True)
            self._authority_active = True
            return "Yetki Durumu: YÜKSEK (Sudo erişimi aktif)"
        except ToolError:
            self._authority_active = False
            return "Yetki Durumu: STANDART (Sudo erişimi kısıtlı)"

    def session_authority_end(self) -> str:
        """
        Yetki döngüsünü sonlandırır.
        """
        self._authority_active = False
        return "Yetki döngüsü sonlandırıldı."

    def deep_system_probe(self, target: str) -> str:
        """
        Sistem internals taraması yapar. Hedef: 'process' veya 'network'.
        """
        if target == "process":
            return self.process_list()
        if target == "network":
            return self.execute_shell("netstat -anp tcp | head -40", False)
        raise ToolError(f"Bilinmeyen tarama hedefi: {target}", "INVALID_PROBE", False)

    def take_screenshot(self, filename: str) -> str:
        """
        Ekran görüntüsü alır ve dosyaya kaydeder. Retina çözünürlüğü modele
        taşımak megabaytlık görsel yükü demek olduğu için uzun kenar
        SCREENSHOT_MAX_EDGE ile sınırlanır; eşleme (find_and_click) kendi
        tam çözünürlüklü görüntüsünü ayrı alır.
        """
        if _is_sensitive_path(Path(filename)):
            raise ToolError(
                f"Korunan bir sistem/kimlik yoluna ekran görüntüsü yazılamaz: {filename}",
                "SENSITIVE_PATH_BLOCKED", False,
            )
        screenshot = ImageGrab.grab()
        screenshot.thumbnail((SCREENSHOT_MAX_EDGE, SCREENSHOT_MAX_EDGE))
        screenshot.save(filename)
        return f"Ekran görüntüsü {filename} dosyasına kaydedildi."

    def find_and_click(self, template_path: str, confidence: float, window_title: Optional[str] = None) -> str:
        """
        OpenCV kullanarak ekranda bir şablon arar ve tıklar.
        window_title verilirse, arama sadece o pencerenin sınırları içinde yapılır.
        """
        if not 0 <= confidence <= 1:
            raise ToolError(f"Geçersiz güven eşiği: {confidence}", "INVALID_CONFIDENCE", False)
        
        full_screen: np.ndarray = np.array(ImageGrab.grab())
        
        if window_title:
            x, y, w, h = self.get_window_bounds(window_title)
            # Negatif/ekran dışı pencere koordinatları numpy'da sessizce sondan
            # sarılır ve yanlış bölgeye tıklanır; kırpımı ekrana sınırla.
            x0, y0 = max(x, 0), max(y, 0)
            x1, y1 = min(x + w, full_screen.shape[1]), min(y + h, full_screen.shape[0])
            if x1 <= x0 or y1 <= y0:
                raise ToolError(
                    f"Pencere kırpımı geçersiz (ekran dışı): x={x}, y={y}, w={w}, h={h}",
                    "WINDOW_OFFSCREEN", True,
                )
            screen_gray = cv2.cvtColor(full_screen[y0:y1, x0:x1], cv2.COLOR_RGB2GRAY)
            offset_x, offset_y = x0, y0
        else:
            screen_gray = cv2.cvtColor(full_screen, cv2.COLOR_RGB2GRAY)
            offset_x, offset_y = 0, 0

        template: Optional[np.ndarray] = cv2.imread(template_path, 0)
        if template is None:
            raise ToolError(f"Şablon dosyası bulunamadı: {template_path}", "TEMPLATE_MISSING", False)
        
        if template.shape[0] > screen_gray.shape[0] or template.shape[1] > screen_gray.shape[1]:
            raise ToolError("Şablon ekran görüntüsünden büyük.", "TEMPLATE_TOO_LARGE", False)
        
        res: np.ndarray = cv2.matchTemplate(screen_gray, template, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(res)
        
        if max_val < confidence:
            raise ToolError(
                f"Hedef bulunamadı: en yüksek güven={max_val:.2f}", "TARGET_MISSING", True,
            )
        
        h_t, w_t = template.shape[:2]
        cx, cy = max_loc[0] + w_t // 2 + offset_x, max_loc[1] + h_t // 2 + offset_y
        pyautogui.click(cx, cy)
        return f"Hedef ({cx}, {cy}) konumunda {max_val:.2f} güven ile bulundu ve tıklandı."

    def smart_click(self, app_name: str, element_id: Optional[int] = None, template_path: Optional[str] = None, confidence: float = 0.8) -> str:
        """
        Hibrit Tıklama Protokolü: AX -> Görsel Şablon sırasıyla dener.
        Başarısız katmanların nedenleri tek hatada biriktirilir ki model tek
        turda düzeltebilsin (boş CLICK_FAILED için ek keşif turu gerekmesin).
        """
        failures: List[str] = []
        # Katman 1: AX (Erişilebilirlik)
        if element_id:
            try:
                return self.cua_click(app_name, element_id)
            except ToolError as error:
                failures.append(f"AX: {error}")

        # Katman 2: Görsel Şablon
        if template_path:
            try:
                return self.find_and_click(template_path, confidence, window_title=app_name)
            except ToolError as error:
                failures.append(f"şablon: {error}")

        detail: str = "; ".join(failures) if failures else "hiç denenmedi (element_id/template_path verilmedi)"
        raise ToolError(
            f"Tüm tıklama yöntemleri başarısız oldu: {app_name}. Katman hataları: {detail}",
            "CLICK_FAILED", True,
        )

    def web_search(self, query: str) -> str:
        """
        DuckDuckGo üzerinden web araması yapar.
        `ddgs` paketi (duckduckgo_search'ün yeniden adlandırılmış hali) kullanılır;
        eski paket adı 2026 itibarıyla sonuç döndürmez hale geldi.
        Boş sonuç kararlı bir durumdur, yeniden denenmez (3x gidiş-dönüş israfı).
        """
        from ddgs import DDGS
        last_error: Optional[Exception] = None
        for attempt in range(1, 4):
            try:
                with DDGS() as ddgs:
                    results: List[Dict[str, Any]] = list(ddgs.text(query, max_results=5))
                if not results:
                    raise ToolError(
                        f"Web araması boş sonuç döndü: sorgu={query}",
                        "WEB_SEARCH_EMPTY", True,
                    )
                # Model turuna binen token'ı azalt: gövde metinlerini 200 karakterle sınırla.
                clipped: List[Dict[str, Any]] = [
                    {
                        "title": r.get("title", ""),
                        "href": r.get("href", ""),
                        "body": str(r.get("body", ""))[:200],
                    }
                    for r in results
                ]
                return json.dumps(clipped, indent=2, ensure_ascii=False)
            except ToolError:
                raise
            except Exception as error:
                last_error = error
                logging.warning(
                    "Web araması başarısız",
                    extra={"query": query, "attempt": attempt, "error_type": type(error).__name__},
                )
                if attempt == 3:
                    raise ToolError(
                        f"Web araması başarısız: sorgu={query}, ayrıntı={error}",
                        "WEB_SEARCH_FAILED", True,
                    ) from error
        raise AssertionError(f"Arama denemeleri sonuç vermedi: {last_error}")

    async def browse_url(self, url: str, action: str, selector: Optional[str] = None, text: Optional[str] = None) -> str:
        """
        Belirtilen URL'ye gider ve etkileşim kurar.
        """
        if action not in {"read", "click", "type"}:
            raise ToolError(f"Geçersiz tarayıcı işlemi: {action}", "INVALID_BROWSER_ACTION", False)
        if action in {"click", "type"} and not selector:
            raise ToolError("Tarayıcı işlemi için seçici gerekli.", "MISSING_SELECTOR", False)
        if action == "type" and text is None:
            raise ToolError("Yazma işlemi için metin gerekli.", "MISSING_TEXT", False)
        context: BrowserContext = await self._get_browser()
        page: Page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded")
            if action == "read":
                html: str = await page.content()
                # BeautifulSoup senkron çalışır; paralel araç turlarını kilitlemesin.
                return await asyncio.to_thread(self._clean_html, html)
            if action == "click" and selector:
                await page.click(selector)
                return "Seçiciye tıklandı."
            if action == "type" and selector and text is not None:
                await page.fill(selector, text)
                return "Metin girildi."
            raise AssertionError("Doğrulanmış tarayıcı işlemi işlenmedi.")
        finally:
            await page.close()

    def fetch_raw(self, url: str) -> str:
        """
        Curl kullanarak hızlı HTTP çekimi yapar ve içeriği temizler.
        JSON gövdeler HTML temizleyiciden geçirilmez (karakter kaybı olur);
        kalıcı hatalar (--retry-all-errors) tekrar denenmez.
        """
        result: subprocess.CompletedProcess[str] = subprocess.run(
            ["curl", "--fail", "--show-error", "--silent", "--location", "--compressed",
             "--retry", "2", "--retry-delay", "1", "--retry-max-time", "20",
             "--max-time", "15", "--", url],
            capture_output=True, text=True, timeout=25,
        )
        if result.returncode != 0:
            raise ToolError(
                f"HTTP çekimi başarısız: url={url}, çıkış={result.returncode}, stderr={result.stderr.strip()}",
                "FETCH_FAILED", True,
            )
        stripped: str = result.stdout.lstrip()
        if stripped.startswith("{") or stripped.startswith("["):
            return _clip(result.stdout, SHELL_STDOUT_LIMIT)
        return self._clean_html(result.stdout)

    def cua_get_app(self, app_name: str) -> str: 
        return self.cua.get_app(app_name)
    
    def cua_click(self, app_name: str, element_id: int) -> str: 
        return self.cua.click_element(app_name, element_id)
    
    def cua_press_key(self, app_name: str, key: str) -> str: 
        return self.cua.press_key(app_name, key)
    
    def cua_get_ax_state(self, app_name: str) -> str: 
        return self.cua.get_ax_state(app_name)
    
    def mouse_click(self, x: int, y: int, button: str) -> str:
        pyautogui.click(x=x, y=y, button=button)
        return f"({x}, {y}) konumuna {button} tıklandı."
    
    def mouse_move(self, x: int, y: int) -> str:
        pyautogui.moveTo(x, y)
        return f"Fare ({x}, {y}) konumuna taşındı."
    
    def keyboard_type(self, text: str) -> str:
        pyautogui.write(text)
        return f"Yazıldı ({len(text)} karakter): {_clip(text, TYPED_TEXT_ECHO_LIMIT)}"
    
    def keyboard_press(self, key: str) -> str:
        pyautogui.press(key)
        return f"Tuşa basıldı: {key}"
    
    def run_action_sequence(self, steps: List[Dict[str, Any]]) -> str:
        """
        Bir dizi fare/klavye eylemini (click/move/type/press) TEK araç çağrısında
        sırayla çalıştırır. Her adım için ayrı bir model turu (ve dolayısıyla ayrı
        bir LLM round-trip'i) gerekmesini önler — "tıkla, yaz, enter'a bas" gibi
        zincirler tek çağrıda biter.
        """
        executed: List[str] = []
        for index, step in enumerate(steps):
            action: object = step.get("action")
            try:
                if action == "click":
                    executed.append(self.mouse_click(int(step["x"]), int(step["y"]), str(step.get("button", "left"))))
                elif action == "move":
                    executed.append(self.mouse_move(int(step["x"]), int(step["y"])))
                elif action == "type":
                    executed.append(self.keyboard_type(str(step["text"])))
                elif action == "press":
                    executed.append(self.keyboard_press(str(step["key"])))
                else:
                    raise ToolError(f"Bilinmeyen eylem türü: {action}", "INVALID_ACTION", False)
            except (KeyError, TypeError, ValueError) as error:
                raise ToolError(
                    f"Eylem {index} ({action}) geçersiz parametrelerle başarısız: {error}",
                    "INVALID_ACTION_PARAMS", False,
                ) from error
        return "Eylem dizisi tamamlandı:\n" + "\n".join(executed)

    def execute_js(self, code: str) -> str:
        temp_path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".js", prefix="omni_", encoding="utf-8", delete=False,
            ) as source:
                temp_path = Path(source.name)
                source.write(code)
            result: subprocess.CompletedProcess[str] = subprocess.run(
                ["node", str(temp_path)], capture_output=True, text=True, timeout=20,
            )
            if result.returncode != 0:
                raise ToolError(
                    f"JS çalıştırma başarısız: çıkış={result.returncode}, stderr={result.stderr.strip()}",
                    "JS_EXIT", True,
                )
            return f"STDOUT: {_clip(result.stdout, SHELL_STDOUT_LIMIT)}\nSTDERR: {_clip(result.stderr, SHELL_STDERR_LIMIT)}\nÇıkış Kodu: 0"
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()
    
    def write_file(self, path: str, content: str) -> str:
        destination: Path = Path(path)
        if _is_sensitive_path(destination):
            raise ToolError(
                f"Korunan bir sistem/kimlik dosyasına yazma engellendi: {destination}",
                "SENSITIVE_PATH_BLOCKED", False,
            )
        if destination.suffix == ".py":
            try:
                compile(content, str(destination), "exec")
            except SyntaxError as error:
                raise ToolError(
                    f"Sözdizimi hatası nedeniyle yazma iptal edildi: {destination}, hata={error}",
                    "SYNTAX_INVALID", False,
                ) from error
        if destination.exists():
            _backup_file(destination)
        temp_path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=destination.parent,
                prefix=f".{destination.name}.", delete=False,
            ) as target:
                temp_path = Path(target.name)
                target.write(content)
                target.flush()
                os.fsync(target.fileno())
            if destination.exists():
                os.chmod(temp_path, destination.stat().st_mode & 0o777)
            os.replace(temp_path, destination)
            if self._read_full(path) != content:
                raise ToolError(f"Dosya doğrulaması başarısız: {path}", "VERIFY_FAILED", False)
            return f"Dosya başarıyla yazıldı: {path}"
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()
    
    def read_file(self, path: str) -> str:
        """
        Dosya okur ve model için kısaltılmış içeriği döner. Doğrulama yolları
        (_read_full) kırpma olmadan okur; aksi halde büyük dosya yazım doğrulaması
        yanlışlıkla başarısız sayılır.
        """
        return _clip(self._read_full(path), FILE_READ_LIMIT)

    def _read_full(self, path: str) -> str:
        """Doğrulama için kırpılmamış tam dosya içeriği okur."""
        with open(path, 'r', encoding='utf-8') as source:
            return source.read()
    
    def self_modify(self, file_path: str, new_content: str) -> str:
        self._read_full(file_path)
        # write_file zaten yazdıktan sonra doğrulama yapıp VERIFY_FAILED fırlatır;
        # burada ikinci bir tam okuma gereksiz.
        self.write_file(file_path, new_content)
        return f"Kaynak kod yazıldı ve doğrulandı: {file_path}"

    async def close_browser(self) -> None:
        """
        Tarayıcı kaynaklarını temizler.
        """
        if self.browser:
            await self.browser.close()
            self.browser = None
            self.browser_context = None
        if self.playwright_instance:
            await self.playwright_instance.stop()
            self.playwright_instance = None

    def get_available_tools(self) -> Dict[str, str]:
        return {
            "execute_shell": "Sistem kabuğunda komut çalıştırır. Parametreler: command (str), use_sudo (bool)",
            "process_list": "Sistemdeki aktif süreçlerin yapılandırılmış listesini döner.",
            "get_pointer_position": "Farenin mevcut ekran koordinatlarını (x, y) döner.",
            "session_authority_status": "Mevcut oturumun yetki durumunu kontrol eder.",
            "session_authority_end": "Yetki döngüsünü sonlandırır.",
            "deep_system_probe": "Sistem internals taraması yapar. Parametre: target (str)",
            "read_file": "Dosya okur.",
            "write_file": "Dosya yazar.",
            "web_search": "DuckDuckGo üzerinden web araması yapar.",
            "browse_url": "Tarayıcı etkileşimi kurar.",
            "fetch_raw": "Hızlı HTTP çekimi yapar.",
            "find_and_click": "Ekranda resim arar ve tıklar. Pencere bazlı çalışabilir.",
            "take_screenshot": "Ekran görüntüsü alır.",
            "cua_get_app": "Uygulamaya bağlanır.",
            "cua_click": "Uygulama elementine tıklar.",
            "cua_press_key": "Uygulama içinde tuşa basar.",
            "cua_get_ax_state": "Uygulama AX durumunu getirir.",
            "mouse_click": "Koordinat tabanlı tıklama.",
            "mouse_move": "Fareyi taşır.",
            "keyboard_type": "Metin yazar.",
            "keyboard_press": "Tuşa basar.",
            "execute_js": "Node.js ile JS çalıştırır.",
            "self_modify": "Kendi kaynak kodunu değiştirir.",
            "smart_click": "Hibrit tıklama protokolünü (AX -> Görsel -> Koordinat) kullanır.",
            "get_window_bounds": "Belirtilen pencerenin ekran sınırlarını döner.",
            "run_action_sequence": "Fare/klavye eylemler dizisini tek çağrıda sırayla çalıştırır."
        }
