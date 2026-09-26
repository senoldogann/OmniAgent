"""
OmniAgent GUI Girdi Modülü (tools/gui_input.py)
macOS Erişilebilirlik (AX) ağacı üzerinden yüksek hassasiyetli etkileşim,
Unicode metin girişi ve koordinat tabanlı fare kontrolü.

AX işlevleri (AXIsProcessTrusted, AXUIElement*, AXValue*) pyobjc'de HIServices'tedir ve
ApplicationServices modülünden gelir; Quartz modülü bunları içermez.
"""
import json
import subprocess
import time
from typing import Dict, List, Optional, Tuple

import AppKit
import ApplicationServices as AX
import pyautogui
import Quartz

from .screen import (
    _check_in_model_space, _raise_if_stopped, accessibility_help,
    click_model_point, current_geometry, drag_model_points, move_model_point,
    multi_click_model_point, parse_point, points_to_model, require_unlocked_screen,
)
from .system import child_environment
from .types import (
    AX_ELEMENT_LIMIT, AX_LABEL_SEARCH_NODES, AX_MESSAGING_TIMEOUT_SECONDS,
    AX_NODE_LIMIT, AX_SCAN_BUDGET_SECONDS, MAX_WAIT_SECONDS,
    TYPED_TEXT_ECHO_LIMIT, UNICODE_CHUNK_DELAY_SECONDS, UNICODE_CHUNK_UNITS,
    ActionStep, AXElement, ScreenGeometry, ToolError, clip_text,
)

_clip = clip_text

# Listeye giren etkileşimli roller. Satır (AXRow) ve bağlantı (AXLink) Mail/Notlar/Finder
# listelerinin ve web içeriğinin asıl hedefleridir; etiketsiz satırın metni alt öğeden okunur.
_AX_ACTIONABLE_ROLES: frozenset[str] = frozenset({
    "AXButton", "AXCheckBox", "AXRadioButton", "AXPopUpButton", "AXMenuButton", "AXComboBox",
    "AXTextField", "AXTextArea", "AXLink", "AXMenuItem", "AXDisclosureTriangle", "AXSlider",
    "AXIncrementor", "AXRow",
})
_AX_TEXT_INPUT_ROLES: frozenset[str] = frozenset({"AXTextField", "AXTextArea", "AXComboBox"})
_AX_SCAN_ATTRIBUTES: List[str] = [
    "AXRole", "AXChildren", "AXTitle", "AXDescription", "AXValue",
    "AXPlaceholderValue", "AXPosition", "AXSize", "AXEnabled",
]

# Modelin yaygın yazımlarını pyautogui'nin macOS tuş adlarına çevirir. Playwright adları
# ("Meta", "Control", "ArrowUp") burada geçersizdir: pyautogui bilinmeyen tuşu sessizce atlar
# ve "cmd+c" yalnız "c" yazar.
_KEY_ALIASES: Dict[str, str] = {
    "cmd": "command", "⌘": "command", "meta": "command", "control": "ctrl",
    "opt": "option", "⌥": "option", "esc": "escape", "del": "delete",
    "arrowup": "up", "arrowdown": "down", "arrowleft": "left", "arrowright": "right",
}


def _require_accessibility() -> None:
    """
    Sentetik fare/klavye olayları ve AX okuma erişilebilirlik izni ister. İzin yoksa
    macOS olayları SESSİZCE düşürür; araç 'başarılı' deyip hiçbir şey yapmasın diye
    açık hata verilir. Kilitli ekrana olay gönderilmez (bkz. require_unlocked_screen).
    """
    if not AX.AXIsProcessTrusted():
        raise ToolError(accessibility_help(), "AX_PERMISSION", False)
    require_unlocked_screen()


def _ax_present(value: object) -> object:
    """CopyMultipleAttributeValues eksik öznitelikleri AXError tipli AXValue olarak döner; onları None yapar."""
    if isinstance(value, AX.AXValueRef) and AX.AXValueGetType(value) == AX.kAXValueAXErrorType:
        return None
    return value


def _ax_point(value: object) -> Optional[Tuple[float, float]]:
    """AXPosition değerini (x, y) nokta çiftine çevirir."""
    if not isinstance(value, AX.AXValueRef):
        return None
    ok, point = AX.AXValueGetValue(value, AX.kAXValueCGPointType, None)
    return (float(point.x), float(point.y)) if ok else None


def _ax_size(value: object) -> Optional[Tuple[float, float]]:
    """AXSize değerini (genişlik, yükseklik) çiftine çevirir."""
    if not isinstance(value, AX.AXValueRef):
        return None
    ok, size = AX.AXValueGetValue(value, AX.kAXValueCGSizeType, None)
    return (float(size.width), float(size.height)) if ok else None


def _ax_attribute(element: object, attribute: str) -> object:
    """Tek bir AX özniteliğini okur; öğede yoksa None. Yanıtsız uygulama açık hata verir."""
    error, value = AX.AXUIElementCopyAttributeValue(element, attribute, None)
    if error == AX.kAXErrorSuccess:
        return value
    if error == AX.kAXErrorCannotComplete:
        raise ToolError("Uygulama erişilebilirlik sorgusuna yanıt vermiyor (meşgul olabilir).", "AX_TIMEOUT", True)
    return None


def _ax_short_text(value: object) -> str:
    """AX metin değerini tek satırlık kısa metne çevirir (metin olmayanlar boş)."""
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:60]


def _ax_descendant_text(element: object) -> str:
    """Etiketsiz öğeler (örn. liste satırları) için ilk alt metni sınırlı genişlikte arar."""
    queue: List[object] = [element]
    visited: int = 0
    while queue and visited < AX_LABEL_SEARCH_NODES:
        node: object = queue.pop(0)
        visited += 1
        children: object = _ax_attribute(node, "AXChildren")
        for child in list(children) if children else []:
            if _ax_attribute(child, "AXRole") == "AXStaticText":
                text: str = _ax_short_text(_ax_attribute(child, "AXValue"))
                if text:
                    return text
            queue.append(child)
    return ""


def scan_ax_elements(root: object) -> Tuple[List[AXElement], List[object], bool]:
    """
    Pencere ağacını ekran sırasıyla (derinlik öncelikli) tarar; etkileşimli öğeleri ve
    AX referanslarını döner. Üçüncü değer, öğe/düğüm/süre limiti yüzünden taramanın
    kısaltıldığını bildirir (sessiz kırpma yok).
    """
    started: float = time.monotonic()
    stack: List[object] = [root]
    elements: List[AXElement] = []
    refs: List[object] = []
    visited: int = 0
    while stack:
        if (len(elements) >= AX_ELEMENT_LIMIT or visited >= AX_NODE_LIMIT
                or time.monotonic() - started > AX_SCAN_BUDGET_SECONDS):
            return elements, refs, True
        node: object = stack.pop()
        visited += 1
        error, values = AX.AXUIElementCopyMultipleAttributeValues(node, _AX_SCAN_ATTRIBUTES, 0, None)
        if error == AX.kAXErrorInvalidUIElement:
            # Tarama sırasında kaybolan öğe (dinamik arayüz): listede yeri yok
            continue
        if error == AX.kAXErrorCannotComplete:
            raise ToolError("Uygulama erişilebilirlik sorgusuna yanıt vermiyor (meşgul olabilir).", "AX_TIMEOUT", True)
        if error != AX.kAXErrorSuccess:
            raise ToolError(f"AX öznitelikleri okunamadı: hata kodu={error}", "AX_READ_FAILED", True)
        attrs: Dict[str, object] = {
            name: _ax_present(value) for name, value in zip(_AX_SCAN_ATTRIBUTES, values, strict=True)
        }
        children: object = attrs["AXChildren"]
        if children:
            stack.extend(reversed(list(children)))
        role: str = str(attrs["AXRole"] or "")
        if role not in _AX_ACTIONABLE_ROLES:
            continue
        position: Optional[Tuple[float, float]] = _ax_point(attrs["AXPosition"])
        size: Optional[Tuple[float, float]] = _ax_size(attrs["AXSize"])
        if position is None or size is None or size[0] <= 0 or size[1] <= 0:
            continue
        label: str = next(
            (text for text in (_ax_short_text(attrs[key]) for key in ("AXTitle", "AXDescription", "AXPlaceholderValue")) if text),
            "",
        )
        value: str = _ax_short_text(attrs["AXValue"]) if role in _AX_TEXT_INPUT_ROLES else ""
        if not label and not value:
            label = _ax_descendant_text(node)
        elements.append({
            "role": role, "label": label, "value": value,
            "enabled": attrs["AXEnabled"] is not False,
            "center_x": position[0] + size[0] / 2, "center_y": position[1] + size[1] / 2,
        })
        refs.append(node)
    return elements, refs, False


# Electron/web görünümlü uygulamalar (Claude, VS Code, Slack…) AX ağacında yalnız pencere düğmelerini
# verir. Canlı Telegram görevinde model burada öğe arayıp bulamayınca hiç tıklamadan vazgeçti (26 Eylül).
AX_EMPTY_HINT: str = (
    "Bu pencerenin erişilebilirlik ağacı içerik vermiyor (Electron/web görünümlü uygulama olabilir); "
    "aradığın öğe bu listede çıkmaz. Görünür metne (menü, düğme, sekme, ayar adı) cua_click_text ile "
    "tıkla; bulamazsa sonuçtaki benzer metinlerden seç. Metni olmayan ikona take_screenshot "
    "görüntüsündeki noktayla tıkla. Uygulama ayarları çoğu macOS uygulamasında cmd+, ile açılır."
)


def format_ax_listing(
    app_name: str, window_title: str, elements: List[AXElement], geometry: ScreenGeometry, truncated: bool,
) -> str:
    """AX öğelerini modele gidecek kompakt listeye çevirir (koordinatlar ortak uzayda). Saf."""
    lines: List[str] = [
        f"{app_name} · pencere {window_title!r} · {len(elements)} öğe "
        f"(koordinatlar ekran görüntüsü/tıklama uzayında, {geometry['model_width']}×{geometry['model_height']})"
    ]
    for index, element in enumerate(elements, start=1):
        x, y = points_to_model(element["center_x"], element["center_y"], geometry)
        line: str = f"[{index}] {element['role'].removeprefix('AX')} {element['label']!r}"
        if element["value"]:
            line += f" değer={element['value']!r}"
        if not element["enabled"]:
            line += " (pasif)"
        lines.append(f"{line} @({x},{y})")
    if truncated:
        lines.append("…liste öğe/süre limitiyle kısaltıldı; aranan öğe yoksa take_screenshot kullan.")
    if not any(element["label"] or element["value"] for element in elements):
        lines.append(AX_EMPTY_HINT)
    return "\n".join(lines)


def _visible_app_owners() -> List[Tuple[str, int]]:
    """Normal pencereleri olan uygulamalar (ad, pid), önden arkaya; pencere sunucusundan her zaman günceldir."""
    windows: object = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionAll | Quartz.kCGWindowListExcludeDesktopElements, Quartz.kCGNullWindowID,
    )
    owners: List[Tuple[str, int]] = []
    for window in list(windows) if windows else []:
        if int(window.get(Quartz.kCGWindowLayer) or 0) != 0:
            continue
        owner: Tuple[str, int] = (str(window.get(Quartz.kCGWindowOwnerName) or ""), int(window[Quartz.kCGWindowOwnerPID]))
        if owner not in owners:
            owners.append(owner)
    return owners


def _bundle_name(pid: int) -> str:
    """Uygulamanın yerelleştirilmemiş paket adı (örn. 'Notlar' için 'Notes')."""
    running: object = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    if running is None or running.bundleURL() is None:
        return ""
    return str(running.bundleURL().lastPathComponent()).removesuffix(".app")


def _app_pid(app_name: str) -> int:
    """
    Uygulamanın PID'ini pencere sunucusundan bulur (yerel ad veya paket adıyla).
    NSWorkspace listesi ana run loop dönmeyen süreçlerde (CLI) güncellenmediği için kullanılmaz.
    """
    wanted: str = app_name.casefold()
    owners: List[Tuple[str, int]] = _visible_app_owners()
    for name, pid in owners:
        if name.casefold() == wanted:
            return pid
    for _name, pid in owners:
        if _bundle_name(pid).casefold() == wanted:
            return pid
    available: str = ", ".join(sorted({name for name, _ in owners if name})[:20])
    raise ToolError(
        f"Penceresi olan çalışan uygulama bulunamadı: {app_name}. Açık uygulamalar: {available}. "
        "Kapalıysa önce cua_get_app ile başlat.",
        "APP_NOT_RUNNING", True,
    )


class CUA:
    """
    macOS GUI konnektörü: uygulama etkinleştirme ve erişilebilirlik (AX) ağacı
    üzerinden öğe listeleme/tıklama. Öğe numaraları son listeye göredir.
    """

    def __init__(self) -> None:
        # Uygulama adı (casefold) -> son listedeki AX referansları (öğe numarası = indeks + 1)
        self._snapshots: Dict[str, List[object]] = {}

    def get_app(self, app_name: str) -> str:
        """Uygulamayı osascript ile başlatır/öne getirir."""
        script: str = f'tell application {json.dumps(app_name)} to activate'
        result: subprocess.CompletedProcess[str] = subprocess.run(
            ["osascript", "-e", script], env=child_environment(), capture_output=True, text=True, timeout=8,
        )
        if result.returncode == 0:
            return f"{app_name} aktif edildi ve öne getirildi."
        raise ToolError(
            f"Uygulama bulunamadı veya aktif edilemedi: {app_name}, ayrıntı={result.stderr.strip()}",
            "APP_NOT_FOUND", False,
        )

    def _front_window(self, app_name: str) -> object:
        """Uygulamanın odaktaki (yoksa ana, yoksa ilk) penceresinin AX öğesini döner."""
        _require_accessibility()
        application: object = AX.AXUIElementCreateApplication(_app_pid(app_name))
        # Yanıtsız uygulamada her AX çağrısı varsayılan ~6 sn bloklamasın
        AX.AXUIElementSetMessagingTimeout(application, AX_MESSAGING_TIMEOUT_SECONDS)
        for attribute in ("AXFocusedWindow", "AXMainWindow"):
            window: object = _ax_attribute(application, attribute)
            if window is not None:
                return window
        windows: object = _ax_attribute(application, "AXWindows")
        if windows:
            return list(windows)[0]
        raise ToolError(
            f"{app_name} uygulamasının bu masaüstünde erişilebilir penceresi yok (başka bir alanda "
            "veya küçültülmüş olabilir); önce cua_get_app ile öne getir.",
            "AX_NO_WINDOW", True,
        )

    def list_elements(self, app_name: str, geometry: Optional[ScreenGeometry] = None) -> str:
        """Öndeki pencerenin etkileşimli öğelerini son görülen ekran uzayında listeler."""
        window: object = self._front_window(app_name)
        elements, refs, truncated = scan_ax_elements(window)
        self._snapshots[app_name.casefold()] = refs
        title: str = _ax_short_text(_ax_attribute(window, "AXTitle"))
        return format_ax_listing(app_name, title, elements, geometry or current_geometry(), truncated)

    def _element_center(self, element: object) -> Tuple[float, float]:
        """Öğenin merkezini nokta koordinatında döner."""
        position: Optional[Tuple[float, float]] = _ax_point(_ax_attribute(element, "AXPosition"))
        size: Optional[Tuple[float, float]] = _ax_size(_ax_attribute(element, "AXSize"))
        if position is None or size is None:
            raise ToolError("Öğenin konumu okunamadı; cua_get_ax_state ile listeyi yenile.", "AX_STALE", True)
        return (position[0] + size[0] / 2, position[1] + size[1] / 2)

    def click_element(self, app_name: str, element_id: int) -> str:
        """
        Son listedeki öğeye tıklar: metin alanları AXFocused ile odaklanır, diğerleri
        AXPress alır. AX eylemi desteklenmiyorsa hibrit protokolün koordinat katmanı
        olarak öğe merkezine gerçek fare tıklaması yapılır (sonuçta açıkça belirtilir).
        """
        _require_accessibility()
        refs: Optional[List[object]] = self._snapshots.get(app_name.casefold())
        if refs is None:
            raise ToolError(f"{app_name} için öğe listesi yok; önce cua_get_ax_state çağır.", "AX_NO_SNAPSHOT", True)
        if not 1 <= element_id <= len(refs):
            raise ToolError(f"Geçersiz öğe numarası: {element_id} (geçerli 1-{len(refs)}).", "INVALID_ELEMENT", True)
        element: object = refs[element_id - 1]
        role: object = _ax_attribute(element, "AXRole")
        if role in _AX_TEXT_INPUT_ROLES:
            error: int = AX.AXUIElementSetAttributeValue(element, "AXFocused", True)
            if error == AX.kAXErrorSuccess:
                return f"[{element_id}] metin alanı odaklandı; run_action_sequence 'type' ile yazabilirsin."
        else:
            error = AX.AXUIElementPerformAction(element, "AXPress")
            if error == AX.kAXErrorSuccess:
                return f"[{element_id}] öğesine AXPress ile tıklandı."
        if error == AX.kAXErrorInvalidUIElement:
            raise ToolError("Öğe artık geçerli değil (arayüz değişti); cua_get_ax_state ile listeyi yenile.", "AX_STALE", True)
        center_x, center_y = self._element_center(element)
        pyautogui.click(round(center_x), round(center_y))
        return f"[{element_id}] öğesi AX eylemini desteklemediği için (hata kodu={error}) merkezine fare ile tıklandı."

    def window_bounds(self, app_name: str) -> Tuple[float, float, float, float]:
        """Öndeki pencerenin (x, y, genişlik, yükseklik) sınırlarını nokta koordinatında döner."""
        window: object = self._front_window(app_name)
        position: Optional[Tuple[float, float]] = _ax_point(_ax_attribute(window, "AXPosition"))
        size: Optional[Tuple[float, float]] = _ax_size(_ax_attribute(window, "AXSize"))
        if position is None or size is None:
            raise ToolError(f"Pencere sınırları okunamadı: {app_name}", "WINDOW_BOUNDS_FAILED", True)
        return (position[0], position[1], size[0], size[1])


# --- Klavye: düzenden bağımsız Unicode yazım ve tuş kombinasyonları ---

def unicode_chunks(text: str, max_units: int) -> List[str]:
    """Metni UTF-16 birim sayısı max_units'i aşmayan parçalara böler; vekil çiftler bölünmez. Saf."""
    chunks: List[str] = []
    current: str = ""
    current_units: int = 0
    for char in text:
        char_units: int = 2 if ord(char) > 0xFFFF else 1
        if current and current_units + char_units > max_units:
            chunks.append(current)
            current, current_units = "", 0
        current += char
        current_units += char_units
    if current:
        chunks.append(current)
    return chunks


def _post_unicode_chunk(chunk: str) -> None:
    """Bir Unicode parçasını tek tuş-bas/bırak olay çifti olarak gönderir (tuş kodu yok sayılır)."""
    units: int = len(chunk.encode("utf-16-le")) // 2
    for key_down in (True, False):
        event = Quartz.CGEventCreateKeyboardEvent(None, 0, key_down)
        # Takılı kalmış bir değiştirici (cmd vb.) yazımı kısayola çevirmesin
        Quartz.CGEventSetFlags(event, 0)
        Quartz.CGEventKeyboardSetUnicodeString(event, units, chunk)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def type_unicode_text(text: str) -> None:
    """
    Metni klavye düzeninden bağımsız yazar. pyautogui.write ABD tuş kodlarıyla bastığı
    için Fince/Türkçe düzende noktalamayı bozuyor, eşlemesinde olmayan karakterleri
    (ç ğ ı ö ş ü) SESSİZCE atlıyor ve karakter başına ~20ms harcıyordu. Satır sonları
    gerçek Enter tuşu olarak gönderilir.
    """
    for line_index, line in enumerate(text.replace("\r\n", "\n").split("\n")):
        if line_index > 0:
            pyautogui.press("enter")
        for chunk in unicode_chunks(line, UNICODE_CHUNK_UNITS):
            _post_unicode_chunk(chunk)
            time.sleep(UNICODE_CHUNK_DELAY_SECONDS)


def _is_known_key(name: str) -> bool:
    """Tuş adının bu platformun tuş eşlemesinde gerçekten karşılığı var mı."""
    mapping: Dict[str, Optional[int]] = pyautogui.platformModule.keyboardMapping
    return name in mapping and mapping[name] is not None


def key_names(spec: str) -> List[str]:
    """'Cmd+Shift+T' gibi tanımı pyautogui tuş adlarına çevirir. Saf."""
    return [_KEY_ALIASES.get(part.strip().lower(), part.strip().lower()) for part in spec.split("+")]


def press_key_spec(spec: str) -> str:
    """
    Tek tuşu ya da 'cmd+shift+t' gibi kombinasyonu basar. Tek karakterlik tuşlar
    (/, @, ö…) düzenden bağımsız Unicode olarak yazılır. Bilinmeyen tuş adı açık hata
    verir; pyautogui bilinmeyen tuşları sessizce yok sayıp 'basıldı' dedirtiyordu.
    """
    if len(spec) == 1:
        type_unicode_text(spec)
        return f"Tuş yazıldı: {spec}"
    names: List[str] = key_names(spec)
    unknown: List[str] = [name for name in names if not _is_known_key(name)]
    if unknown:
        raise ToolError(
            f"Bilinmeyen tuş: {unknown} (istenen: {spec!r}). Örnekler: enter, tab, escape, space, "
            "backspace, delete, up, down, pageup, f5, cmd+c, cmd+shift+t.",
            "INVALID_KEY", False,
        )
    if len(names) == 1:
        pyautogui.press(names[0])
    else:
        pyautogui.hotkey(*names)
    return f"Tuşa basıldı: {spec}"


def _run_action_step(step: ActionStep, geometry: ScreenGeometry) -> str:
    """Tek bir fare/klavye adımını çalıştırır; eksik/yanlış alan KeyError/TypeError/ValueError verir."""
    action: object = step.get("action")
    if action == "click":
        x, y = parse_point(step["point"])
        button: str = str(step.get("button") or "left")
        clicks: object = step.get("clicks") or 1
        if isinstance(clicks, bool) or clicks not in (1, 2, 3):
            raise ToolError(f"clicks 1, 2 veya 3 olmalı: {clicks!r}", "INVALID_ACTION_PARAMS", False)
        if clicks == 1:
            return click_model_point(x, y, button, geometry)
        return multi_click_model_point(x, y, button, int(clicks), geometry)
    if action == "drag":
        start, end = parse_point(step["point"]), parse_point(step["to"])
        return drag_model_points(start, end, str(step.get("button") or "left"), geometry)
    if action == "move":
        x, y = parse_point(step["point"])
        return move_model_point(x, y, geometry)
    if action == "type":
        text: str = str(step["text"])
        type_unicode_text(text)
        return f"Yazıldı ({len(text)} karakter): {_clip(text, TYPED_TEXT_ECHO_LIMIT)}"
    if action == "press":
        return press_key_spec(str(step["key"]))
    if action == "wait":
        seconds: float = float(step["seconds"])
        if not 0 < seconds <= MAX_WAIT_SECONDS:
            raise ToolError(f"Bekleme süresi 0-{MAX_WAIT_SECONDS}sn aralığında olmalı: {seconds}", "INVALID_WAIT", False)
        deadline: float = time.monotonic() + seconds
        while time.monotonic() < deadline:
            _raise_if_stopped()
            time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
        return f"{seconds}sn beklendi."
    raise ToolError(f"Bilinmeyen eylem türü: {action} (click/drag/move/type/press/wait).", "INVALID_ACTION", False)
