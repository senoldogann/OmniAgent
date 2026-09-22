import base64

# The full content of tools.py provided in previous turns, corrected
content = r'''import subprocess
import os
import json
import logging
import tempfile
from pathlib import Path
import numpy as np
import cv2
from playwright.async_api import async_playwright, Browser, BrowserContext, Page
import pyautogui
from PIL import ImageGrab
from bs4 import BeautifulSoup
from typing import Any, Dict, List, Optional, Tuple

# Ajanın kendi kendini iyileştirmesi için yapılandırılmış hata sınıfı
class ToolError(Exception):
    def __init__(self, message: str, code: str, recoverable: bool) -> None:
        super().__init__(message)
        self.code: str = code
        self.recoverable: bool = recoverable

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
            ["osascript", "-e", script], capture_output=True, text=True, timeout=15,
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
            ["osascript", "-e", script], capture_output=True, text=True, timeout=15,
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
            ["osascript", "-e", script], capture_output=True, text=True, timeout=15,
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

    async def _get_browser(self) -> BrowserContext:
        """
        Tarayıcı bağlamını başlatır veya mevcut olanı döner.
        """
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
        return " ".join(lines)[:15000]

    def execute_shell(self, command: str, use_sudo: bool) -> str:
        """
        Sistem kabuğunda komut çalıştırır.
        """
        if not command.strip():
            raise ToolError("Boş kabuk komutu çalıştırılamaz.", "EMPTY_COMMAND", False)
        full_cmd: str | List[str] = (
            ["sudo", "-n", "/bin/sh", "-c", command] if use_sudo else command
        )
        try:
            result: subprocess.CompletedProcess[str] = subprocess.run(
                full_cmd, shell=not use_sudo, capture_output=True, text=True,
                timeout=300, stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired as error:
            raise ToolError("Kabuk komutu 300 saniyede tamamlanmadı.", "SHELL_TIMEOUT", True) from error
        if result.returncode != 0:
            raise ToolError(
                f"Kabuk komutu başarısız: çıkış={result.returncode}, stderr={result.stderr.strip()}",
                "SHELL_EXIT", True,
            )
        return f"STDOUT: {result.stdout}\\nSTDERR: {result.stderr}\\nÇıkış Kodu: {result.returncode}"

    def get_window_bounds(self, window_title: str) -> Tuple[int, int, int, int]:
        """
        Belirtilen başlığa sahip pencerenin koordinatlarını (x, y, w, h) döner.
        """
        script: str = (
            f'tell application "System Events" '
            f'to get position and size of window 1 of (first process whose name contains "{window_title}")'
        )
        try:
            result: subprocess.CompletedProcess[str] = subprocess.run(
                ["osascript", "-e", script], capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                raise ToolError(f"Pencere boyutları alınamadı: {result.stderr.strip()}", "WINDOW_NOT_FOUND", True)
            
            # Result format: "0, 0, 450, 600" (approx)
            parts: List[int] = [int(p.strip()) for p in result.stdout.split(',')]
            return tuple(parts)
        except Exception as e:
            raise ToolError(f"Pencere sınırları belirlenemedi: {e}", "WINDOW_BOUNDS_FAILED", True)

    def process_list(self) -> str:
        """
        Sistemdeki aktif süreçlerin yapılandırılmış listesini döner.
        """
        result = self.execute_shell("ps -eo pid,ppid,user,%cpu,%mem,comm", False)
        return result

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
            return self.execute_shell("netstat -anp tcp", False)
        raise ToolError(f"Bilinmeyen tarama hedefi: {target}", "INVALID_PROBE", False)

    def take_screenshot(self, filename: str) -> str:
        """
        Ekran görüntüsü alır ve dosyaya kaydeder.
        """
        screenshot = ImageGrab.grab()
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
            screen_gray = cv2.cvtColor(full_screen[y:y+h, x:x+w], cv2.COLOR_RGB2GRAY)
            offset_x, offset_y = x, y
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
        Hibrit Tıklama Protokolü: AX -> Görsel Şablon -> Koordinat sırasıyla dener.
        """
        # Katman 1: AX (Erişilebilirlik)
        if element_id:
            try:
                return self.cua_click(app_name, element_id)
            except ToolError:
                pass
        
        # Katman 2: Görsel Şablon
        if template_path:
            try:
                return self.find_and_click(template_path, confidence, window_title=app_name)
            except ToolError:
                pass
        
        raise ToolError(f"Tüm tıklama yöntemleri başarısız oldu: {app_name}", "CLICK_FAILED", True)

    def web_search(self, query: str) -> str:
        """
        DuckDuckGo üzerinden web araması yapar.
        """
        from duckduckgo_search import DDGS
        for attempt in range(1, 4):
            try:
                with DDGS() as ddgs:
                    results: List[Dict[str, Any]] = list(ddgs.text(query, max_results=8))
                return json.dumps(results, indent=2, ensure_ascii=False)
            except Exception as error:
                logging.warning(
                    "Web araması başarısız",
                    extra={"query": query, "attempt": attempt, "error_type": type(error).__name__},
                )
                if attempt == 3:
                    raise ToolError(
                        f"Web araması başarısız: sorgu={query}, ayrıntı={error}",
                        "WEB_SEARCH_FAILED", True,
                    ) from error
        raise AssertionError("Arama denemeleri sonuç vermedi.")

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
                return self._clean_html(html)
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
        """
        result: subprocess.CompletedProcess[str] = subprocess.run(
            ["curl", "--fail", "--show-error", "--silent", "--location",
             "--retry", "2", "--retry-all-errors", "--max-time", "15",
             "--", url],
            capture_output=True, text=True, timeout=50,
        )
        if result.returncode != 0:
            raise ToolError(
                f"HTTP çekimi başarısız: url={url}, çıkış={result.returncode}, stderr={result.stderr.strip()}",
                "FETCH_FAILED", True,
            )
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
        return f"Yazıldı: {text}"
    
    def keyboard_press(self, key: str) -> str:
        pyautogui.press(key)
        return f"Tuşa basıldı: {key}"
    
    def execute_js(self, code: str) -> str:
        temp_path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".js", prefix="omni_", encoding="utf-8", delete=False,
            ) as source:
                temp_path = Path(source.name)
                source.write(code)
            result: subprocess.CompletedProcess[str] = subprocess.run(
                ["node", str(temp_path)], capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                raise ToolError(
                    f"JS çalıştırma başarısız: çıkış={result.returncode}, stderr={result.stderr.strip()}",
                    "JS_EXIT", True,
                )
            return f"STDOUT: {result.stdout}\\nSTDERR: {result.stderr}\\nÇıkış Kodu: 0"
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()
    
    def write_file(self, path: str, content: str) -> str:
        destination: Path = Path(path)
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
            if self.read_file(path) != content:
                raise ToolError(f"Dosya doğrulaması başarısız: {path}", "VERIFY_FAILED", False)
            return f"Dosya başarıyla yazıldı: {path}"
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()
    
    def read_file(self, path: str) -> str:
        with open(path, 'r', encoding='utf-8') as source:
            return source.read()
    
    def self_modify(self, file_path: str, new_content: str) -> str:
        self.read_file(file_path)
        self.write_file(file_path, new_content)
        if self.read_file(file_path) != new_content:
            raise ToolError(
                f"Kaynak kod doğrulaması başarısız: {file_path}", "VERIFY_FAILED", False,
            )
        return f"Kaynak kod yazıldı ve doğrulandı: {file_path}"

    async def close_browser(self) -> None:
        \"\"\"
        Tarayıcı kaynaklarını temizler.
        \"\"\"
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
            "get_window_bounds": "Belirtilen pencerenin ekran sınırlarını döner."
        }
'''

with open('/Users/dogan/Desktop/OmniAgent/tools.py', 'w', encoding='utf-8') as f:
    f.write(content)
print("tools.py updated successfully")
