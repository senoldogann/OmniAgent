"""
Başsız (headless) GUI ölçümü için görünmez ekran. Gerçek ajan döngüsü ve gerçek araç mantığı
(OCR ile metne tıklama, kaydırma, baştan sona okuma, otomatik gözlem, bitiş doğrulaması) çalışır;
yalnız en alt katman — ekran yakalama, fare, klavye — kullanıcının ekranı yerine görünmez bir
Chromium sayfasına bağlanır. Böylece GUI senaryoları kullanıcı ekrandayken de ölçülebilir
(`benchmark.py --headless`). Sınırlar: `<select>` açılır menüsü headless Chromium'da çizilmez;
Quartz yakalama, pencere kapsamı ve gerçek kaydırma olayı yolu burada sınanmaz.
"""
import io
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, List, Optional, Tuple, TypeVar

import numpy as np
import Quartz
from PIL import Image
from playwright.sync_api import Browser, Page, Playwright, sync_playwright

from omniagent.app import agent as main
from omniagent import tools

# Chrome içerik alanına yakın görünüm (nokta); Retina gibi 2x çizilir
VIEW_WIDTH: int = 1400
VIEW_HEIGHT: int = 860
GEOMETRY: tools.ScreenGeometry = {
    "point_width": VIEW_WIDTH, "point_height": VIEW_HEIGHT,
    "model_width": tools.MODEL_SCREEN_SIZE, "model_height": tools.MODEL_SCREEN_SIZE,
    "origin_x": 0, "origin_y": 0,
}
# Aracın tuş adları → Playwright tuş adları
KEY_NAMES: Dict[str, str] = {
    "enter": "Enter", "return": "Enter", "tab": "Tab", "escape": "Escape", "esc": "Escape",
    "space": "Space", "backspace": "Backspace", "delete": "Delete", "up": "ArrowUp", "down": "ArrowDown",
    "left": "ArrowLeft", "right": "ArrowRight", "pagedown": "PageDown", "pageup": "PageUp",
    "home": "Home", "end": "End", "cmd": "Meta", "command": "Meta", "ctrl": "Control",
    "control": "Control", "shift": "Shift", "option": "Alt", "alt": "Alt",
}

_T = TypeVar("_T")


class HeadlessPage:
    """
    Görünmez Chromium sayfası konnektörü. Sync Playwright nesneleri oluşturuldukları iş
    parçacığına bağlıdır; araçlar ise asyncio.to_thread ile farklı iş parçacıklarında koşar.
    Bu yüzden her çağrı tek işçili havuzda çalıştırılır.
    """

    def __init__(self) -> None:
        self._pool: ThreadPoolExecutor = ThreadPoolExecutor(max_workers=1)
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._page: Optional[Page] = None
        self._pool.submit(self._start).result()

    def _start(self) -> None:
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=True)
        self._page = self._browser.new_page(
            viewport={"width": VIEW_WIDTH, "height": VIEW_HEIGHT}, device_scale_factor=2,
        )

    def _run(self, action: Callable[[Page], _T]) -> _T:
        """Eylemi sayfanın iş parçacığında çalıştırır; hata çağırana aynen yükselir."""
        return self._pool.submit(lambda: action(self._require_page())).result()

    def _require_page(self) -> Page:
        if self._page is None:
            raise RuntimeError("Görünmez sayfa başlatılamadı.")
        return self._page

    def goto(self, url: str) -> None:
        self._run(lambda page: page.goto(url, wait_until="domcontentloaded"))

    def title_and_url(self) -> Tuple[str, str]:
        return self._run(lambda page: (page.title(), page.url))

    def png(self) -> bytes:
        return self._run(lambda page: page.screenshot())

    def click(self, x: float, y: float, button: str) -> None:
        self._run(lambda page: page.mouse.click(x, y, button=button))

    def move(self, x: float, y: float) -> None:
        self._run(lambda page: page.mouse.move(x, y))

    def wheel(self, delta_x: float, delta_y: float) -> None:
        self._run(lambda page: page.mouse.wheel(delta_x, delta_y))

    def type_text(self, text: str) -> None:
        self._run(lambda page: page.keyboard.type(text))

    def press(self, key: str) -> None:
        self._run(lambda page: page.keyboard.press(key))

    def close(self) -> None:
        def stop() -> None:
            if self._browser is not None:
                self._browser.close()
            if self._playwright is not None:
                self._playwright.stop()
        self._pool.submit(stop).result()
        self._pool.shutdown()


def to_page_point(x: int, y: int) -> Tuple[float, float]:
    """0-1000 model noktasını görünüm noktasına çevirir. Saf."""
    return x * VIEW_WIDTH / tools.MODEL_SCREEN_SIZE, y * VIEW_HEIGHT / tools.MODEL_SCREEN_SIZE


def playwright_key(spec: str) -> str:
    """'cmd+a' gibi araç tuş tanımını Playwright adına ('Meta+A') çevirir. Saf."""
    names: List[str] = []
    for part in (piece.strip().lower() for piece in spec.split("+")):
        names.append(KEY_NAMES.get(part, part.upper() if len(part) == 1 else part.capitalize()))
    return "+".join(names)


def install(page: HeadlessPage) -> None:
    """
    Araçların en alt katmanını görünmez sayfaya bağlar ve ajanın araç kutusunu HeadlessToolbox
    yapar. Süreç boyunca geçerlidir; yalnız benchmark sürecinde çağrılmalıdır.
    """

    def gray(edge: int) -> np.ndarray:
        image: Image.Image = Image.open(io.BytesIO(page.png())).convert("L")
        image.thumbnail((edge, edge))
        return np.asarray(image, dtype=np.uint8)

    def click(x: int, y: int, button: str, geometry: tools.ScreenGeometry) -> str:
        tools._check_in_model_space(x, y, geometry)
        page.click(*to_page_point(x, y), button)
        return f"({x}, {y}) konumuna {button} tıklandı."

    def move(x: int, y: int, geometry: tools.ScreenGeometry) -> str:
        tools._check_in_model_space(x, y, geometry)
        page.move(*to_page_point(x, y))
        return f"Fare ({x}, {y}) konumuna taşındı."

    def press(spec: str) -> str:
        if len(spec) == 1:
            page.type_text(spec)
            return f"Tuş yazıldı: {spec}"
        page.press(playwright_key(spec))
        return f"Tuşa basıldı: {spec}"

    def scroll(delta_x: float, delta_y: float) -> None:
        page.wheel(delta_x, delta_y)

    class HeadlessToolbox(tools.Toolbox):
        """Gerçek Toolbox mantığı; yalnız görüntü kaynağı ve geometri görünmez sayfadır."""

        def _settle_frame(self) -> np.ndarray:
            return gray(tools.SETTLE_FRAME_EDGE)

        def _input_geometry(self) -> tools.ScreenGeometry:
            return GEOMETRY

        def _scope_image(self, image_option: int) -> Tuple[object, tools.ScreenGeometry]:
            provider = Quartz.CGDataProviderCreateWithCFData(page.png())
            image = Quartz.CGImageCreateWithPNGDataProvider(provider, None, True, Quartz.kCGRenderingIntentDefault)
            return image, GEOMETRY

        def _scope_gray(self) -> np.ndarray:
            return gray(tools.SCROLL_DIFF_EDGE)

        def take_screenshot(self, filename: str, display_index: Optional[int] = None) -> str:
            note: str = ""
            if self._pending_input is not None:
                waited: float = tools.wait_for_screen_settle(
                    self._pending_input["baseline"], self._pending_input["at"], self._settle_frame,
                )
                self._pending_input = None
                note = f" Son eylemden sonra ekranın durulması {waited:.1f}sn beklendi."
            frame: Image.Image = Image.open(io.BytesIO(page.png())).convert("RGB").resize(
                (VIEW_WIDTH, VIEW_HEIGHT), Image.Resampling.LANCZOS,
            )
            frame.save(filename)
            self._visual_geometry = GEOMETRY
            return (f"Ekran görüntüsü {filename} dosyasına kaydedildi ({VIEW_WIDTH}×{VIEW_HEIGHT}, gerçek "
                    "en-boy oranı). Sana 1000×1000 kare olarak gösterilir; o görüntüdeki koordinatlar tıklama "
                    "araçlarıyla aynı uzaydadır." + note)

        def chrome_active_tab(self, url: Optional[str]) -> str:
            if url is not None:
                page.goto(url)
            title, current = page.title_and_url()
            # Gezinmeden sonraki gözlem sayfanın durulmasını beklesin (boyut farkı = tepki görüldü)
            self._pending_input = {"at": time.monotonic(), "baseline": np.zeros((1, 1), dtype=np.uint8)}
            return f"Görünür Chrome sekmesi: {current}\nBaşlık: {title}"

    tools.screen_capture_granted = lambda request=False: True
    tools._require_screen_capture = lambda: None
    tools._require_accessibility = lambda: None
    tools.click_model_point = click
    tools.move_model_point = move
    tools.type_unicode_text = page.type_text
    tools.press_key_spec = press
    tools.post_scroll = scroll
    main.Toolbox = HeadlessToolbox
