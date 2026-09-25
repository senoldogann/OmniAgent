"""
OmniAgent Ekran ve Görüntü Modülü (tools/screen.py)
Yüksek performanslı ekran yakalama, çoklu monitör yönetimi ve optimize edilmiş durulma (settle) tespiti.
"""
import logging
import os
import sys
import time
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np
from PIL import Image
import pyautogui
import Quartz
import cv2

from .system import parent_process_name
from .types import (
    ACCESSIBILITY_SETTINGS_URL,
    MODEL_SCREEN_SIZE,
    MOUSE_DRAG_HOLD_SECONDS,
    MOUSE_DRAG_STEP_SECONDS,
    MOUSE_DRAG_STEPS,
    MOUSE_MULTI_CLICK_GAP_SECONDS,
    SCREENSHOT_MAX_EDGE,
    SETTLE_CHANGED_RATIO,
    SETTLE_FRAME_EDGE,
    SETTLE_MAX_SECONDS,
    SETTLE_PIXEL_DELTA,
    SETTLE_POLL_SECONDS,
    SETTLE_QUIET_SECONDS,
    SETTLE_REACTION_SECONDS,
    SCREEN_SETTINGS_URL,
    TOOL_RUNTIME,
    ScreenGeometry,
    ToolError,
    ToolRuntime,
)

BUNDLE_APP_NAMES: Dict[str, str] = {
    "com.apple.Terminal": "Terminal",
    "com.googlecode.iterm2": "iTerm2",
    "com.microsoft.VSCode": "Visual Studio Code",
    "com.apple.dt.Xcode": "Xcode",
    "com.jetbrains.pycharm": "PyCharm",
    "dev.warp.Warp-Stable": "Warp",
    "com.github.wez.wezterm": "WezTerm",
    "co.zeit.hyper": "Hyper",
    "com.freebuff.desktop": "Freebuff",
}

_SCREEN_CAPTURE_REQUESTED: List[bool] = [False]

def model_space_size(point_width: int, point_height: int, max_edge: int) -> Tuple[int, int]:
    scale = min(1.0, max_edge / max(point_width, point_height))
    return (round(point_width * scale), round(point_height * scale))

def model_to_points(x: float, y: float, geometry: ScreenGeometry) -> Tuple[int, int]:
    return (
        int(geometry.get("origin_x", 0)) + round(x * geometry["point_width"] / geometry["model_width"]),
        int(geometry.get("origin_y", 0)) + round(y * geometry["point_height"] / geometry["model_height"]),
    )

def points_to_model(x: float, y: float, geometry: ScreenGeometry) -> Tuple[int, int]:
    return (
        round((x - int(geometry.get("origin_x", 0))) * geometry["model_width"] / geometry["point_width"]),
        round((y - int(geometry.get("origin_y", 0))) * geometry["model_height"] / geometry["point_height"]),
    )

def current_geometry() -> ScreenGeometry:
    pw, ph = pyautogui.size()
    return {"point_width": pw, "point_height": ph, "model_width": MODEL_SCREEN_SIZE, "model_height": MODEL_SCREEN_SIZE}

def active_display_ids() -> List[int]:
    status, raw_ids, count = Quartz.CGGetActiveDisplayList(32, None, None)
    if status != Quartz.kCGErrorSuccess or not raw_ids or count < 1:
        raise ToolError("Etkin ekran listesi okunamadı.", "DISPLAY_LIST_FAILED", True)
    main_id = int(Quartz.CGMainDisplayID())
    ids = [int(v) for v in list(raw_ids)[:count]]
    return sorted(ids, key=lambda v: (v != main_id, float(Quartz.CGDisplayBounds(v).origin.y), float(Quartz.CGDisplayBounds(v).origin.x), v))

def geometry_for_display_id(display_id: int) -> ScreenGeometry:
    if display_id not in active_display_ids():
        raise ToolError(f"Ekran artık bağlı değil: id={display_id}. Yeniden görüntü al.", "DISPLAY_DISCONNECTED", True)
    bounds = Quartz.CGDisplayBounds(display_id)
    return {
        "point_width": round(bounds.size.width), "point_height": round(bounds.size.height),
        "model_width": MODEL_SCREEN_SIZE, "model_height": MODEL_SCREEN_SIZE,
        "origin_x": round(bounds.origin.x), "origin_y": round(bounds.origin.y),
    }

def geometry_for_display_index(display_index: int) -> Tuple[int, ScreenGeometry]:
    ids = active_display_ids()
    if isinstance(display_index, bool) or not isinstance(display_index, int) or not 1 <= display_index <= len(ids):
        raise ToolError(f"Ekran numarası 1-{len(ids)} aralığında olmalı: {display_index!r}", "INVALID_DISPLAY", False)
    did = ids[display_index - 1]
    return did, geometry_for_display_id(did)

def parse_point(value: object) -> Tuple[int, int]:
    if isinstance(value, (list, tuple)) and len(value) == 2 and all(isinstance(i, (int, float)) and not isinstance(i, bool) for i in value):
        return round(value[0]), round(value[1])
    raise ToolError(f"point [x, y] biçiminde iki sayı olmalı; alınan: {value!r}", "INVALID_POINT", False)

def screen_capture_owner() -> Dict[str, str]:
    bundle = os.environ.get("__CFBundleIdentifier", "").strip()
    return {"bundle_id": bundle, "app_name": BUNDLE_APP_NAMES.get(bundle, ""), "parent_process": parent_process_name(), "python": sys.executable}

def _permission_help(title: str, settings_url: str, pane: str, requested: bool) -> str:
    """
    İzin hatası metni: "Terminal/Python" gibi belirsiz ifade yerine izni alacak uygulamayı adı ve
    bundle kimliğiyle, yoksa eklenecek tam python yolunu ve ayar sayfasını açan komutu yazar.
    """
    owner = screen_capture_owner()
    app = owner["app_name"] or owner["parent_process"] or "bu süreci başlatan uygulama"
    identity = f" ({owner['bundle_id']})" if owner["bundle_id"] else ""
    lines = [
        f"{title} macOS izni süreci başlatan uygulamaya verir:",
        f'1) Ayarlar sayfasını açın: open "{settings_url}"',
        f"   (Sistem Ayarları > Gizlilik ve Güvenlik > {pane})",
        f"2) Listede '{app}'{identity} varsa anahtarını açın.",
        f"   Yoksa '+' ile şu python ikilisini ekleyin (⌘⇧G ile yolu yapıştırın): {owner['python']}",
        "3) İzni verdikten sonra Terminal'i/arayüzü tamamen kapatıp yeniden açın; macOS izni",
        "   çalışan sürece hemen uygulamaz.",
    ]
    if requested:
        lines.append("Bu süreç izni bir kez sistem istemiyle de sordu; istemi onaylamak uygulamayı listeye ekler.")
    return "\n".join(lines)

def screen_capture_help() -> str:
    return _permission_help("Ekran kaydı izni yok.", SCREEN_SETTINGS_URL, "Ekran ve Sistem Sesi Kaydı", True)

def accessibility_help() -> str:
    return _permission_help(
        "Erişilebilirlik izni yok: izin olmadan macOS fare/klavye olaylarını sessizce düşürür.",
        ACCESSIBILITY_SETTINGS_URL, "Erişilebilirlik", False,
    )

def _request_screen_capture_once() -> None:
    if _SCREEN_CAPTURE_REQUESTED[0]: return
    _SCREEN_CAPTURE_REQUESTED[0] = True
    try: Quartz.CGRequestScreenCaptureAccess()
    except Exception as error:
        logging.warning("Ekran kaydı izin istemi gösterilemedi", extra={"error_type": type(error).__name__})

def screen_capture_granted(request: bool = False) -> bool:
    try:
        if Quartz.CGPreflightScreenCaptureAccess(): return True
    except Exception as error:
        logging.warning("Ekran kaydı izni sorgulanamadı", extra={"error_type": type(error).__name__})
        return False
    if not request: return False
    _request_screen_capture_once()
    try: return bool(Quartz.CGPreflightScreenCaptureAccess())
    except Exception: return False

def _require_screen_capture() -> None:
    if screen_capture_granted(request=True): return
    raise ToolError(screen_capture_help(), "SCREEN_CAPTURE_PERMISSION", False)

def _display_image(display_id: int, resolution: int) -> object:
    image = Quartz.CGWindowListCreateImage(Quartz.CGDisplayBounds(display_id), Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID, resolution)
    if image is None: raise ToolError(f"Ekran görüntüsü alınamadı: id={display_id}.", "SCREEN_CAPTURE_FAILED", True)
    return image

def _main_display_image(resolution: int) -> object:
    return _display_image(int(Quartz.CGMainDisplayID()), resolution)

def _bounds_intersect(first: Dict[str, float], second: Dict[str, float]) -> bool:
    return (float(first.get("X", 0.0)) < float(second.get("X", 0.0)) + float(second.get("Width", 0.0))
            and float(second.get("X", 0.0)) < float(first.get("X", 0.0)) + float(first.get("Width", 0.0))
            and float(first.get("Y", 0.0)) < float(second.get("Y", 0.0)) + float(second.get("Height", 0.0))
            and float(second.get("Y", 0.0)) < float(first.get("Y", 0.0)) + float(first.get("Height", 0.0)))

def _front_app_window_image(app_name: str, image_option: int) -> Tuple[object, ScreenGeometry]:
    _require_screen_capture()
    windows = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements, Quartz.kCGNullWindowID)
    wanted = app_name.casefold()
    entries = list(windows) if windows else []
    for pos, window in enumerate(entries):
        if int(window.get(Quartz.kCGWindowLayer) or 0) != 0: continue
        if str(window.get(Quartz.kCGWindowOwnerName) or "").casefold() != wanted: continue
        bounds = window.get(Quartz.kCGWindowBounds)
        if not bounds: continue
        b = dict(bounds)
        left, top, width, height = float(b.get("X", 0.0)), float(b.get("Y", 0.0)), float(b.get("Width", 0.0)), float(b.get("Height", 0.0))
        if width < 2 or height < 2: continue
        window_id = int(window.get(Quartz.kCGWindowNumber) or 0)
        if window_id <= 0: continue
        owner_pid = int(window.get(Quartz.kCGWindowOwnerPID) or 0)
        popups = [int(other.get(Quartz.kCGWindowNumber) or 0) for other in entries[:pos]
                  if int(other.get(Quartz.kCGWindowOwnerPID) or 0) == owner_pid and _bounds_intersect(dict(other.get(Quartz.kCGWindowBounds) or {}), b)]
        image = Quartz.CGWindowListCreateImageFromArray(Quartz.CGRectMake(left, top, width, height), popups + [window_id], image_option)
        if image is None: continue
        return image, {"point_width": round(width), "point_height": round(height), "model_width": MODEL_SCREEN_SIZE, "model_height": MODEL_SCREEN_SIZE, "origin_x": round(left), "origin_y": round(top)}
    raise ToolError(f"{app_name} için ekranda yakalanabilir pencere bulunamadı.", "WINDOW_CAPTURE_FAILED", True)

def _draw_scaled(image: object, width: int, height: int, color_space: object, channels: int, bitmap_info: int) -> bytes:
    buffer = bytearray(width * height * channels)
    context = Quartz.CGBitmapContextCreate(buffer, width, height, 8, width * channels, color_space, bitmap_info)
    if context is None:
        raise ToolError(f"Ekran karesi için çizim bağlamı kurulamadı ({width}×{height}, {channels} kanal).", "SCREEN_CAPTURE_FAILED", True)
    Quartz.CGContextSetInterpolationQuality(context, Quartz.kCGInterpolationHigh)
    Quartz.CGContextDrawImage(context, Quartz.CGRectMake(0, 0, width, height), image)
    return bytes(buffer)

def screenshot_size(geometry: ScreenGeometry) -> Tuple[int, int]:
    return model_space_size(geometry["point_width"], geometry["point_height"], SCREENSHOT_MAX_EDGE)

def rgb_frame(image: object, size: Tuple[int, int]) -> Image.Image:
    w, h = size
    pixels = _draw_scaled(image, w, h, Quartz.CGColorSpaceCreateWithName(Quartz.kCGColorSpaceSRGB), 4, Quartz.kCGImageAlphaNoneSkipLast)
    return Image.frombuffer("RGBX", (w, h), pixels, "raw", "RGBX", 0, 1).convert("RGB")

def gray_frame(image: object, max_edge: int) -> np.ndarray:
    w, h = model_space_size(Quartz.CGImageGetWidth(image), Quartz.CGImageGetHeight(image), max_edge)
    pixels = _draw_scaled(image, w, h, Quartz.CGColorSpaceCreateDeviceGray(), 1, Quartz.kCGImageAlphaNone)
    return np.frombuffer(pixels, dtype=np.uint8).reshape(h, w)

def grab_model_frame(geometry: ScreenGeometry, display_id: Optional[int] = None) -> Image.Image:
    _require_screen_capture()
    image = _main_display_image(Quartz.kCGWindowImageDefault) if display_id is None else _display_image(display_id, Quartz.kCGWindowImageDefault)
    return rgb_frame(image, screenshot_size(geometry))

def grab_app_window_frame(app_name: str) -> Tuple[Image.Image, ScreenGeometry]:
    image, geometry = _front_app_window_image(app_name, Quartz.kCGWindowImageBoundsIgnoreFraming)
    return rgb_frame(image, screenshot_size(geometry)), geometry

def settle_app_frame(app_name: str) -> np.ndarray:
    image, geometry = _front_app_window_image(app_name, Quartz.kCGWindowImageBoundsIgnoreFraming)
    w, h = model_space_size(geometry["point_width"], geometry["point_height"], SETTLE_FRAME_EDGE)
    pixels = _draw_scaled(image, w, h, Quartz.CGColorSpaceCreateDeviceGray(), 1, Quartz.kCGImageAlphaNone)
    return np.frombuffer(pixels, dtype=np.uint8).reshape(h, w)

def settle_frame() -> np.ndarray:
    image = _main_display_image(Quartz.kCGWindowImageNominalResolution)
    w, h = model_space_size(Quartz.CGImageGetWidth(image), Quartz.CGImageGetHeight(image), SETTLE_FRAME_EDGE)
    pixels = _draw_scaled(image, w, h, Quartz.CGColorSpaceCreateDeviceGray(), 1, Quartz.kCGImageAlphaNone)
    return np.frombuffer(pixels, dtype=np.uint8).reshape(h, w)

def settle_display_frame(display_id: int) -> np.ndarray:
    image = _display_image(display_id, Quartz.kCGWindowImageNominalResolution)
    w, h = model_space_size(Quartz.CGImageGetWidth(image), Quartz.CGImageGetHeight(image), SETTLE_FRAME_EDGE)
    pixels = _draw_scaled(image, w, h, Quartz.CGColorSpaceCreateDeviceGray(), 1, Quartz.kCGImageAlphaNone)
    return np.frombuffer(pixels, dtype=np.uint8).reshape(h, w)

def frame_change_ratio(previous: np.ndarray, current: np.ndarray, pixel_delta: int) -> float:
    if previous.shape != current.shape: return 1.0
    return float(np.mean(np.abs(current.astype(np.int16) - previous.astype(np.int16)) > pixel_delta))

def _raise_if_stopped() -> None:
    runtime = TOOL_RUNTIME.get()
    if runtime is not None and runtime["should_stop"]():
        raise ToolError("Kullanıcı tarafından durduruldu.", "STOPPED", False)

def wait_for_screen_settle(baseline: np.ndarray, input_at: float, frame_source: Optional[Callable[[], np.ndarray]] = None) -> float:
    started = time.monotonic()
    reference = baseline
    reacted = False
    last_change = started
    source = frame_source or settle_frame
    while True:
        _raise_if_stopped()
        frame = source()
        now = time.monotonic()
        if frame_change_ratio(reference, frame, SETTLE_PIXEL_DELTA) > SETTLE_CHANGED_RATIO:
            reacted, last_change, reference = True, now, frame
        if (reacted and now - last_change >= SETTLE_QUIET_SECONDS) or (not reacted and now >= input_at + SETTLE_REACTION_SECONDS) or now >= input_at + SETTLE_MAX_SECONDS:
            return time.monotonic() - started
        time.sleep(SETTLE_POLL_SECONDS)

def _check_in_model_space(x: int, y: int, geometry: ScreenGeometry) -> None:
    """Koordinatın ortak uzayda olduğunu doğrular (ekran dışı tıklama FAILSAFE'i tetiklemesin)."""
    if not (0 <= x < geometry["model_width"] and 0 <= y < geometry["model_height"]):
        raise ToolError(
            f"Koordinat ekran dışında: ({x}, {y}). Geçerli aralık x 0-{geometry['model_width'] - 1}, "
            f"y 0-{geometry['model_height'] - 1} (ekran görüntüsü/AX listesi uzayı).",
            "COORDINATE_OUT_OF_RANGE", True,
        )

def click_model_point(x: int, y: int, button: str, geometry: ScreenGeometry) -> str:
    """Ortak uzaydaki noktaya fare tıklaması yapar."""
    if button not in ("left", "right", "middle"):
        raise ToolError(f"Geçersiz fare tuşu: {button} (left/right/middle).", "INVALID_BUTTON", False)
    _check_in_model_space(x, y, geometry)
    px, py = model_to_points(x, y, geometry)
    pyautogui.click(px, py, button=button)
    return f"({x}, {y}) konumuna {button} tıklandı."

def move_model_point(x: int, y: int, geometry: ScreenGeometry) -> str:
    """Fareyi ortak uzaydaki noktaya taşır."""
    _check_in_model_space(x, y, geometry)
    px, py = model_to_points(x, y, geometry)
    pyautogui.moveTo(px, py)
    return f"Fare ({x}, {y}) konumuna taşındı."

def _mouse_event_types(button: str) -> Tuple[int, int, int, int]:
    """Düğmenin (basma, bırakma, sürükleme, düğme kodu) Quartz olay türleri."""
    if button == "left":
        return (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp,
                Quartz.kCGEventLeftMouseDragged, Quartz.kCGMouseButtonLeft)
    if button == "right":
        return (Quartz.kCGEventRightMouseDown, Quartz.kCGEventRightMouseUp,
                Quartz.kCGEventRightMouseDragged, Quartz.kCGMouseButtonRight)
    if button == "middle":
        return (Quartz.kCGEventOtherMouseDown, Quartz.kCGEventOtherMouseUp,
                Quartz.kCGEventOtherMouseDragged, Quartz.kCGMouseButtonCenter)
    raise ToolError(f"Geçersiz fare tuşu: {button} (left/right/middle).", "INVALID_BUTTON", False)

def _post_mouse_event(kind: int, px: float, py: float, button_code: int, click_state: int = 0) -> None:
    event = Quartz.CGEventCreateMouseEvent(None, kind, (px, py), button_code)
    if click_state:
        Quartz.CGEventSetIntegerValueField(event, Quartz.kCGMouseEventClickState, click_state)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)

def multi_click_model_point(x: int, y: int, button: str, clicks: int, geometry: ScreenGeometry) -> str:
    """
    Çift/üçlü tıklama. pyautogui macOS'ta her tıklamayı ayrı tek tıklama olarak gönderir
    (kCGMouseEventClickState hep 1): Finder dosyayı açmaz, metin kelime/satır seçmez.
    Burada her basma/bırakma çiftine artan tıklama durumu yazılır.
    """
    down, up, _, code = _mouse_event_types(button)
    _check_in_model_space(x, y, geometry)
    px, py = model_to_points(x, y, geometry)
    pyautogui.moveTo(px, py)
    for state in range(1, clicks + 1):
        _post_mouse_event(down, px, py, code, state)
        _post_mouse_event(up, px, py, code, state)
        time.sleep(MOUSE_MULTI_CLICK_GAP_SECONDS)
    name = {2: "Çift", 3: "Üçlü"}.get(clicks, f"{clicks}x")
    return f"{name} tıklandı: ({x}, {y}) -> ({px}, {py})"

def drag_model_points(start: Tuple[int, int], end: Tuple[int, int], button: str, geometry: ScreenGeometry) -> str:
    """
    Basılı tutup sürükler ve bırakır (dosya taşıma, kaydırıcı, metin seçimi, pencere taşıma).
    Basıştan sonra kısa bekleme ve ara sürükleme olayları uygulamanın sürüklemeyi tanıması
    içindir; tek sıçramalı olayda Finder ve web sürükle-bırak alanları bırakmayı yok sayar.
    """
    down, up, dragged, code = _mouse_event_types(button)
    _check_in_model_space(start[0], start[1], geometry)
    _check_in_model_space(end[0], end[1], geometry)
    sx, sy = model_to_points(start[0], start[1], geometry)
    ex, ey = model_to_points(end[0], end[1], geometry)
    pyautogui.moveTo(sx, sy)
    _post_mouse_event(down, sx, sy, code, 1)
    time.sleep(MOUSE_DRAG_HOLD_SECONDS)
    for step in range(1, MOUSE_DRAG_STEPS + 1):
        ratio = step / MOUSE_DRAG_STEPS
        _post_mouse_event(dragged, sx + (ex - sx) * ratio, sy + (ey - sy) * ratio, code)
        time.sleep(MOUSE_DRAG_STEP_SECONDS)
    time.sleep(MOUSE_DRAG_HOLD_SECONDS)
    _post_mouse_event(up, ex, ey, code, 1)
    return f"Sürüklendi: ({start[0]}, {start[1]}) -> ({end[0]}, {end[1]})"

def post_scroll(dx: float, dy: float) -> None:
    event = Quartz.CGEventCreateScrollWheelEvent(None, Quartz.kCGScrollEventUnitPixel, 2, round(-dy * 10), round(-dx * 10))
    Quartz.CGEventSetIntegerValueField(event, Quartz.kCGScrollWheelEventIsContinuous, 1)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)

def changed_region(before: np.ndarray, after: np.ndarray, anchor: Tuple[int, int], min_share: float) -> Optional[Tuple[int, int, int, int]]:
    changed = (np.abs(after.astype(np.int16) - before.astype(np.int16)) > SETTLE_PIXEL_DELTA).astype(np.uint8)
    if not changed.any(): return None
    grown = cv2.dilate(changed, np.ones((9, 9), np.uint8))
    count, labels, stats, _ = cv2.connectedComponentsWithStats(grown, connectivity=8)
    min_area = min_share * grown.shape[0] * grown.shape[1]
    components = [
        (int(stats[l, cv2.CC_STAT_LEFT]), int(stats[l, cv2.CC_STAT_TOP]), int(stats[l, cv2.CC_STAT_WIDTH]), int(stats[l, cv2.CC_STAT_HEIGHT]), int(stats[l, cv2.CC_STAT_AREA]), l)
        for l in range(1, count) if stats[l, cv2.CC_STAT_AREA] >= min_area
    ]
    if not components: return None
    ax, ay = anchor
    around_anchor = [item for item in components if item[0] <= ax < item[0] + item[2] and item[1] <= ay < item[1] + item[3]]
    chosen = max(around_anchor or components, key=lambda item: item[4])[5]
    rows, cols = np.nonzero(changed & (labels == chosen))
    return (int(cols.min()), int(rows.min()), int(cols.max()) + 1, int(rows.max()) + 1)
