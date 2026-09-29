"""
Başsız (headless) GUI ölçümü için görünmez ekran. Gerçek ajan döngüsü ve gerçek araç mantığı
(OCR ile metne tıklama, kaydırma, baştan sona okuma, otomatik gözlem, bitiş doğrulaması) çalışır;
yalnız en alt katman — ekran yakalama, fare, klavye — kullanıcının ekranı yerine görünmez bir
Chromium sayfasına bağlanır. Böylece GUI senaryoları kullanıcı ekrandayken de ölçülebilir
(`benchmark.py --headless`). Sınırlar: `<select>` açılır menüsü headless Chromium'da çizilmez;
Quartz yakalama, pencere kapsamı, gerçek kaydırma olayı yolu ve gerçek ön plan/pencere odağı burada sınanmaz.

Gerçek girdi katmanı (pyautogui işlevleri ve Quartz.CGEventPost) `install` sonunda kapatılır:
yamasız kalan bir yol kullanıcının ekranına sessizce dokunmak yerine `HeadlessInputBlocked` ile
açıkça başarısız olur. Erişilebilirlik (AX) araçları da kapatılır: gerçek masaüstünün AX ağacı
okunmaz/etkilenmez (şemadan çıkarılır, HeadlessToolbox'ta `AX_UNAVAILABLE` hatası verir). Kabuk aracı
kapatılmaz; Chrome oturumu senaryolarında şemadan çıkarılıp reddedildiği için `--headless` yalnız o
senaryolar içindir.
"""
import io
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, List, NoReturn, Optional, Tuple, TypeVar

import numpy as np
import pyautogui
import Quartz
from PIL import Image
from playwright.sync_api import Browser, Page, Playwright, sync_playwright

from omniagent.app import agent as main
from omniagent.app.tool_schema import AX_TOOL_NAMES, without_tools
from omniagent import tools
from omniagent.paths import resolve_output_path
from omniagent.tools.types import MOUSE_DRAG_STEPS

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
# Görünmez modda kapatılan gerçek girdi katmanı (pyautogui) işlevleri. pyautogui macOS'ta tüm
# olaylarını Quartz.CGEventPost ile gönderdiği için o alt katman da ayrıca kapatılır.
REAL_INPUT_FUNCTIONS: Tuple[str, ...] = (
    "moveTo", "click", "press", "hotkey", "write", "typewrite", "dragTo", "scroll",
)

_T = TypeVar("_T")


class HeadlessInputBlocked(RuntimeError):
    """Görünmez modda gerçek fare/klavye girdisine uzanan yamasız bir yol kullanıldı."""


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
        self._actions: List[Dict[str, object]] = []
        self._call_id: Optional[str] = None
        self._pool.submit(self._start).result()

    def _start(self) -> None:
        self._playwright = sync_playwright().start()
        # Varsayılan: playwright'ın kendi indirdiği Chromium. Bazı ortamlarda (ör. bu depo pip
        # paketiyle uyumsuz, önceden kurulu tek bir Chromium sürümü sunan konteynerler) sürüm
        # uyuşmazlığı yüzünden "Executable doesn't exist" ile düşer; bu değişken ayarlıysa o
        # ikiliye açıkça yönlendirir. Üretimde/macOS'ta kullanılmaz (değişken tanımlı değildir).
        executable_path: Optional[str] = os.environ.get("OMNI_HEADLESS_CHROMIUM_PATH")
        self._browser = self._playwright.chromium.launch(
            headless=True, executable_path=executable_path or None,
        )
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

    def new_tab(self, url: Optional[str]) -> None:
        """Canlı Chrome'daki new_tab argümanını aynı görünmez oturumda uygular."""
        def open_page(_previous: Page) -> None:
            if self._browser is None:
                raise RuntimeError("Görünmez tarayıcı kapalı.")
            self._page = self._browser.new_page(
                viewport={"width": VIEW_WIDTH, "height": VIEW_HEIGHT}, device_scale_factor=2,
            )
            if url is not None:
                self._page.goto(url, wait_until="domcontentloaded")
        self._run(open_page)

    def title_and_url(self) -> Tuple[str, str]:
        return self._run(lambda page: (page.title(), page.url))

    def png(self) -> bytes:
        return self._run(lambda page: page.screenshot())

    def click(self, x: float, y: float, button: str, clicks: int) -> None:
        def action(page: Page) -> None:
            target = page.evaluate("""([x, y]) => {
              const node = document.elementFromPoint(x, y);
              return node?.closest('[data-benchmark-target]')?.getAttribute('data-benchmark-target') || null;
            }""", [x, y])
            before = page.locator("body").get_attribute("data-benchmark-state")
            # click_count artan tıklama durumuyla basma/bırakma çiftleri üretir (2 için dblclick olayı dahil)
            page.mouse.click(x, y, button=button, click_count=clicks)
            after = page.locator("body").get_attribute("data-benchmark-state")
            self._actions.append({"call_id": self._call_id, "action": "click", "target": target,
                                  "no_effect": before == after if before is not None else None})
        self._run(action)

    def move(self, x: float, y: float) -> None:
        self._run(lambda page: page.mouse.move(x, y))

    def drag(self, start: Tuple[float, float], end: Tuple[float, float], button: str) -> None:
        """Basılı tutup ara noktalardan sürükler ve bırakır; yalnız görünmez sayfada."""
        def action(page: Page) -> None:
            page.mouse.move(*start)
            page.mouse.down(button=button)
            page.mouse.move(*end, steps=MOUSE_DRAG_STEPS)
            page.mouse.up(button=button)
        self._run(action)

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

    def start_run(self) -> None:
        """Yeni koşunun yalnız güvenli hedef işaretlerini ve çağrı kimliklerini tutar."""
        self._actions = []
        self._call_id = None

    def set_call_id(self, call_id: Optional[str]) -> None:
        self._call_id = call_id

    def actions(self) -> List[Dict[str, object]]:
        return list(self._actions)


def to_page_point(x: int, y: int) -> Tuple[float, float]:
    """0-1000 model noktasını görünüm noktasına çevirir. Saf."""
    return x * VIEW_WIDTH / tools.MODEL_SCREEN_SIZE, y * VIEW_HEIGHT / tools.MODEL_SCREEN_SIZE


def playwright_key(spec: str) -> str:
    """'cmd+a' gibi araç tuş tanımını Playwright adına ('Meta+A') çevirir. Saf."""
    names: List[str] = []
    for part in (piece.strip().lower() for piece in spec.split("+")):
        names.append(KEY_NAMES.get(part, part.upper() if len(part) == 1 else part.capitalize()))
    return "+".join(names)


def _blocked_input(name: str) -> Callable[..., NoReturn]:
    """`name` adlı gerçek girdi işlevinin yerine konan, çağrılınca açıkça başarısız olan işlevi üretir. Saf."""

    def blocked(*arguments: object, **keywords: object) -> NoReturn:
        # Bağımsız değişkenler bilerek mesaja girmez: yazılan metin gizli olabilir.
        raise HeadlessInputBlocked(
            f"Görünmez modda gerçek fare/klavye girdisi engellendi: {name}. "
            "Girdi yolu headless_screen.install() içinde görünmez sayfaya yamalanmalı."
        )

    return blocked


def ax_unavailable(tool_name: str) -> tools.ToolError:
    """Görünmez modda kapalı AX aracı için açık hata: gerçek masaüstünün erişilebilirlik ağacı okunmaz/etkilenmez. Saf."""
    return tools.ToolError(
        f"{tool_name} görünmez modda kapalı: gerçek masaüstünün erişilebilirlik (AX) ağacı okunmaz ve etkilenmez. "
        "Sayfayla take_screenshot ve cua_click_text/cua_click_point/cua_fill_field ile çalış.",
        "AX_UNAVAILABLE", False,
    )


def hide_ax_tools() -> None:
    """
    Ajanın araç şemasından AX araçlarını çıkarır: model görünmez modda bu araçlara tur harcamasın. Çağrı yine
    de gelirse çalıştırma katmanı (allowed_tools) ve HeadlessToolbox açıkça reddeder. Sistem istemindeki öğe
    rehberi aynı şemadan türetilir (agent.route_system_prompt): araçlar şemadan çıkınca rehber de modele gitmez,
    model olmayan araca yönlendirilmez ve benchmark ölçümü bozulmaz. Süreç boyunca geçerlidir.
    """
    original_route = main.route_tool_schemas

    def route_without_ax(
        goal: Optional[str], allow_edit: bool, chrome_session: bool, can_send_files: bool, can_schedule: bool,
    ) -> List[Dict[str, Any]]:
        return without_tools(original_route(goal, allow_edit, chrome_session, can_send_files, can_schedule), AX_TOOL_NAMES)

    main.route_tool_schemas = route_without_ax


def block_real_input() -> None:
    """
    Gerçek girdi katmanını (pyautogui işlevleri ve Quartz.CGEventPost) kapatır: install() içinde
    yamalanmamış bir yol (ör. yeni bir çoklu tıklama ya da sürükleme aracı) kullanıcının ekranına
    sessizce dokunmak yerine HeadlessInputBlocked ile açıkça başarısız olur. Süreç boyunca geçerlidir;
    yalnız benchmark sürecinde çağrılmalıdır.
    """
    for name in REAL_INPUT_FUNCTIONS:
        setattr(pyautogui, name, _blocked_input(f"pyautogui.{name}"))
    Quartz.CGEventPost = _blocked_input("Quartz.CGEventPost")


def install(page: HeadlessPage) -> None:
    """
    Araçların en alt katmanını görünmez sayfaya bağlar, ajanın araç kutusunu HeadlessToolbox yapar
    ve yamasız kalan gerçek girdi yollarını kapatır (block_real_input). Süreç boyunca geçerlidir;
    yalnız benchmark sürecinde çağrılmalıdır.
    """

    def gray(edge: int) -> np.ndarray:
        image: Image.Image = Image.open(io.BytesIO(page.png())).convert("L")
        image.thumbnail((edge, edge))
        return np.asarray(image, dtype=np.uint8)

    def click(x: int, y: int, button: str, geometry: tools.ScreenGeometry) -> str:
        tools._check_in_model_space(x, y, geometry)
        page.click(*to_page_point(x, y), button, 1)
        return f"({x}, {y}) konumuna {button} tıklandı."

    def multi_click(x: int, y: int, button: str, clicks: int, geometry: tools.ScreenGeometry) -> str:
        tools._check_in_model_space(x, y, geometry)
        page.click(*to_page_point(x, y), button, clicks)
        label: str = {2: "Çift", 3: "Üçlü"}.get(clicks, f"{clicks}x")
        return f"{label} tıklandı: ({x}, {y})"

    def drag(start: Tuple[int, int], end: Tuple[int, int], button: str, geometry: tools.ScreenGeometry) -> str:
        tools._check_in_model_space(start[0], start[1], geometry)
        tools._check_in_model_space(end[0], end[1], geometry)
        page.drag(to_page_point(*start), to_page_point(*end), button)
        return f"Sürüklendi: ({start[0]}, {start[1]}) -> ({end[0]}, {end[1]})"

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

        def _require_input_target(self) -> None:
            """Klavye görünmez sayfaya gider; gerçek ön plan uygulaması bu ölçümde anlamsızdır (ve gerçek Mac'te önde Terminal varken tüm yazma adımlarını düşürürdü)."""
            return None

        def _scope_image(self, image_option: int) -> Tuple[object, tools.ScreenGeometry]:
            provider = Quartz.CGDataProviderCreateWithCFData(page.png())
            image = Quartz.CGImageCreateWithPNGDataProvider(provider, None, True, Quartz.kCGRenderingIntentDefault)
            return image, GEOMETRY

        def _scope_gray(self) -> np.ndarray:
            return gray(tools.SCROLL_DIFF_EDGE)

        def take_screenshot(
            self, filename: str, display_index: Optional[int] = None, detail: bool = True,
        ) -> str:
            # Gerçek Toolbox ile aynı çözümleme: host, eki ve sohbet kartını dosyanın yazıldığı yerden okur.
            target: Path = resolve_output_path(filename, allow_source_relative=self._allow_source_relative_writes)
            note: str = ""
            if self._pending_input is not None:
                waited: float = tools.wait_for_screen_settle(
                    self._pending_input["baseline"], self._pending_input["at"], self._settle_frame,
                )
                self._pending_input = None
                page._actions.append({"call_id": page._call_id, "action": "settle", "seconds": round(waited, 2)})
                note = f" Son eylemden sonra ekranın durulması {waited:.1f}sn beklendi."
            frame: Image.Image = Image.open(io.BytesIO(page.png())).convert("RGB").resize(
                (VIEW_WIDTH, VIEW_HEIGHT), Image.Resampling.LANCZOS,
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            frame.save(target)
            self._visual_geometry = GEOMETRY
            detail_note = (
                " ve oranı korunmuş ayrıntı görüntüsü"
                if detail else ""
            )
            return (f"Ekran görüntüsü {target} dosyasına kaydedildi ({VIEW_WIDTH}×{VIEW_HEIGHT}, gerçek "
                    f"en-boy oranı). Modele 1000×1000 koordinat haritası{detail_note} "
                    "iletilir; tıklama noktaları koordinat haritasındadır." + note)

        def chrome_active_tab(self, url: Optional[str], new_tab: bool = False) -> str:
            if new_tab:
                page.new_tab(url)
            elif url is not None:
                page.goto(url)
            title, current = page.title_and_url()
            # Gezinmeden sonraki gözlem sayfanın durulmasını beklesin (boyut farkı = tepki görüldü)
            self._pending_input = {"at": time.monotonic(), "baseline": np.zeros((1, 1), dtype=np.uint8)}
            return f"Görünür Chrome sekmesi: {current}\nBaşlık: {title}"

        # AX araçları görünmez modda kapalıdır: gerçek masaüstünün erişilebilirlik ağacına dokunulmaz.
        def cua_snapshot(self, app: Optional[str]) -> str:
            raise ax_unavailable("cua_snapshot")

        def cua_click_element(self, snapshot: str, index: int) -> str:
            raise ax_unavailable("cua_click_element")

        def cua_set_text_element(self, snapshot: str, index: int, text: str) -> str:
            raise ax_unavailable("cua_set_text_element")

        def cua_get_ax_state(self, app_name: str) -> str:
            raise ax_unavailable("cua_get_ax_state")

        def cua_click(self, app_name: str, element_id: int) -> str:
            raise ax_unavailable("cua_click")

        def smart_click(
            self, app_name: str, element_id: Optional[int], template_path: Optional[str], confidence: float,
        ) -> str:
            raise ax_unavailable("smart_click")

        def observation_ax_summary(self) -> Optional[str]:
            return None

    tools.screen_capture_granted = lambda request=False: True
    tools._require_screen_capture = lambda: None
    tools._require_accessibility = lambda: None
    tools.click_model_point = click
    tools.multi_click_model_point = multi_click
    tools.drag_model_points = drag
    tools.move_model_point = move
    tools.type_unicode_text = page.type_text
    tools.press_key_spec = press
    tools.post_scroll = scroll
    main.Toolbox = HeadlessToolbox
    hide_ax_tools()
    # En sonda: yamalar kurulduktan sonra yamasız kalan gerçek girdi yolları kapatılır.
    block_real_input()
