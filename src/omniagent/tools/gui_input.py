"""
OmniAgent GUI Girdi Modülü (tools/gui_input.py)
macOS Erişilebilirlik (AX) ağacı üzerinden yüksek hassasiyetli etkileşim,
Unicode metin girişi ve koordinat tabanlı fare kontrolü.

AX işlevleri (AXIsProcessTrusted, AXUIElement*, AXValue*) pyobjc'de HIServices'tedir ve
ApplicationServices modülünden gelir; Quartz modülü bunları içermez.
"""
import logging
import os
import subprocess
import threading
import time
import unicodedata
from dataclasses import dataclass
from typing import Callable, Dict, List, Literal, Optional, Tuple, TypeVar

import AppKit
import ApplicationServices as AX
import CoreFoundation
import numpy as np
import pyautogui
import Quartz

from omniagent.platform.macos import screen_text as st

from .ax_snapshot import (
    AX_ERROR_API_DISABLED, AX_ERROR_CANNOT_COMPLETE, AX_ERROR_INVALID_ELEMENT,
    AX_ERROR_SUCCESS, ApprovedStep,
    AX_NODE_READER, COMMIT_ROLES, CapturedSnapshot, Effect, EffectProbe, Element, Frame, LiveElement, PressStep, Snapshot,
    SnapshotLimits, TEXT_INPUT_ROLES, ax_frame, ax_list, ax_read, ax_text, ax_value_text, capture_snapshot,
    changed_signals, clip_label, frame_center, intersect, normalize_snapshot_id, normalize_text, poll_effect,
    approved_press_outcome, press_outcome, quantized_frame, read_live_element, remember_snapshot_labels, role_name,
    running_application, stale_reason,
)
from .screen import (
    _check_in_model_space, _post_mouse_event, _raise_if_stopped, _require_screen_capture, accessibility_help,
    click_model_point, current_geometry, drag_model_points, gray_frame, move_model_point,
    multi_click_model_point, parse_point, points_to_model, require_unlocked_screen,
    screen_capture_granted,
)
from .system import child_environment
from .types import (
    ACTIVATION_POLL_SECONDS, ACTIVATION_SETTLE_SECONDS, ACTIVATION_TIMEOUT_SECONDS, AX_ELEMENT_LIMIT, AX_LABEL_SEARCH_NODES,
    AX_MESSAGING_TIMEOUT_SECONDS, AX_NODE_LIMIT, AX_SCAN_BUDGET_SECONDS, AX_WEB_READY_SECONDS, AX_WEB_RETRY_SECONDS,
    CLICK_EVENT_GAP_SECONDS, CUA_ACTIVATE_TIMEOUT_SECONDS, ELEMENT_LABEL_MARGIN, ELEMENT_LABEL_MIN_RADIUS,
    EFFECT_POLL_SECONDS, EFFECT_SCREEN_EDGE, EFFECT_TIMEOUT_SECONDS, FOCUS_SETTLE_SECONDS, HIT_TEST_ANCESTOR_DEPTH,
    MAX_WAIT_SECONDS, SELECT_ALL_TIMEOUT_SECONDS,
    TEXT_READBACK_TIMEOUT_SECONDS, TYPED_TEXT_ECHO_LIMIT, UNICODE_CHUNK_DELAY_SECONDS, UNICODE_CHUNK_UNITS,
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


# Etiketsiz metin alanları için fazladan okunan öznitelikler. Bunlar yalnız alan etiketsizken
# çağrılır; genel tarama maliyeti artmaz. Canlı kayıtta Mail'in "To/Konu/Gövde" alanları listede
# `TextField ''` olarak çıkıyor, model de alıcı adresini yanlış alana yazıyordu.
_AX_LABEL_RESOLVE_LIMIT: int = 12


def _ax_linked_label(element: object) -> str:
    """Etiketsiz denetimin ilişkili etiket metnini (AXTitleUIElement) okur. Saf."""
    linked: object = _ax_attribute(element, "AXTitleUIElement")
    if linked is None:
        return ""
    for attribute in ("AXValue", "AXTitle", "AXDescription"):
        try:
            text: str = _ax_short_text(_ax_attribute(linked, attribute))
        except (TypeError, ValueError):  # İlişkili öğe gerçek bir AXUIElement değilse
            return ""
        if text:
            return text
    return ""


def _ax_extra_label(element: object) -> str:
    """Etiketsiz alanı tanımlayıcı özniteliklerden etiketler (AXIdentifier, AXHelp). Saf."""
    for attribute in ("AXIdentifier", "AXHelp"):
        text: str = _ax_short_text(_ax_attribute(element, attribute))
        if text:
            return text
    return ""


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
    label_resolves: int = 0
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
        if not label and not value and role in _AX_TEXT_INPUT_ROLES and label_resolves < _AX_LABEL_RESOLVE_LIMIT:
            # Etiketsiz metin alanı: ilişkili etiketi, olmazsa tanımlayıcıyı oku. Böylece
            # "To/Konu/Gövde" alanları ayırt edilebilir; aksi hâlde üçü de 'TextField '' görünür.
            label_resolves += 1
            label = _ax_linked_label(node) or _ax_extra_label(node)
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
    running = running_application(pid)
    if running is None or running.bundleURL() is None:
        return ""
    return str(running.bundleURL().lastPathComponent()).removesuffix(".app")


def _owner_names(owners: List[Tuple[str, int]]) -> List[str]:
    """Pencere sahiplerinin boş olmayan, tekil, sıralı adları (en çok 20); hata iletilerinde modele verilir. Saf."""
    return sorted({name for name, _pid in owners if name})[:20]


def _folded_app_name(name: str) -> str:
    """Ad karşılaştırması için yalnız harf/rakam, aksansız, küçük harf: 'T3 Code (Nightly)' → 't3codenightly'. Saf."""
    decomposed: str = unicodedata.normalize("NFKD", name)
    return "".join(character for character in decomposed if character.isalnum()).casefold()


# NSApplicationActivationPolicyRegular: Dock'ta görünen normal uygulama (arka plan servisi değil).
_REGULAR_ACTIVATION_POLICY: int = 0


def _dock_app_owners() -> List[Tuple[str, int]]:
    """
    Penceresi olan ve Dock'ta görünen (normal) uygulamalar. Arka plan servisleri (ör. CursorUIViewService, AutoFill)
    ad eşleştirmesine ve modele verilen listeye girmez: canlı hatada (29 Eylül) 'Cursor' tahmini bu servise eşlendi.
    """
    owners: List[Tuple[str, int]] = []
    for name, pid in _visible_app_owners():
        running = running_application(pid)
        if running is not None and int(running.activationPolicy()) == _REGULAR_ACTIVATION_POLICY:
            owners.append((name, pid))
    return owners


def match_running_app(app_name: str, owners: List[Tuple[str, int]]) -> Optional[Tuple[str, int]]:
    """
    İstenen adı çalışan uygulamalardan birine (gerçek ad, pid) eşler: önce büyük/küçük harfsiz tam ad, sonra harf/rakam
    dışı atılmış ön ek YALNIZ bir uygulamayla eşleşiyorsa o ('T3 Code' → 'T3 Code (Nightly)'). Eşleşme yoksa ya da
    belirsizse None: çağıran adı olduğu gibi (kapalı uygulamayı açmak için) dener. Saf.
    """
    unique: List[Tuple[str, int]] = sorted({(name, pid) for name, pid in owners if name})
    wanted: str = app_name.strip().casefold()
    for name, pid in unique:
        if name.casefold() == wanted:
            return name, pid
    folded: str = _folded_app_name(app_name)
    if not folded:
        return None
    candidates: List[Tuple[str, int]] = [
        (name, pid) for name, pid in unique if _folded_app_name(name).startswith(folded)
    ]
    return candidates[0] if len({name for name, _pid in candidates}) == 1 else None


def resolve_running_app(app_name: str) -> Tuple[str, Optional[int]]:
    """Adı Dock'taki çalışan bir uygulamanın (gerçek ad, pid) ikilisine çözer; çözülemezse (ad, None): açılacak uygulama."""
    matched: Optional[Tuple[str, int]] = match_running_app(app_name, _dock_app_owners())
    return matched if matched is not None else (app_name, None)


def _running_apps_text() -> str:
    """Modelin yanlış uygulama adını düzeltebilmesi için Dock'taki çalışan uygulamaların listesi."""
    names: List[str] = _owner_names(_dock_app_owners())
    return f"Çalışan uygulamalar: {', '.join(names) if names else 'yok'}."


def restore_minimized_window(pid: int) -> bool:
    """
    Uygulamanın BÜTÜN pencereleri küçültülmüşse ilkini geri açar (AXMinimized=false) ve True döner; ekranda en az bir
    penceresi varsa dokunmaz. Küçültülmüş pencere ekran görüntüsünde ve OCR'da görünmez (pencere listesi
    kCGWindowListOptionAll ile onu yine de 'pencere var' sayar): içeriği okunacak uygulama önce geri açılmalıdır.
    Geri açma reddedilirse açık hata.
    """
    windows: List[object] = ax_list(_ax_attribute(AX.AXUIElementCreateApplication(pid), "AXWindows"))
    if not windows or not all(bool(_ax_attribute(window, "AXMinimized")) for window in windows):
        return False
    code: int = int(AX.AXUIElementSetAttributeValue(windows[0], "AXMinimized", False))
    if code != AX_ERROR_SUCCESS:
        raise ToolError(f"Küçültülmüş pencere geri açılamadı (AX hata kodu {code}).", "WINDOW_RESTORE_FAILED", True)
    return True


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
    available: str = ", ".join(_owner_names(owners))
    raise ToolError(
        f"Penceresi olan çalışan uygulama bulunamadı: {app_name}. Açık uygulamalar: {available}. "
        "Kapalıysa önce cua_get_app ile başlat.",
        "APP_NOT_RUNNING", True,
    )


# --- Öğe tabanlı eylemler (ax_snapshot.py anlık görüntüsü üzerinde): eylem merdiveni ve doğrulama ---

_T = TypeVar("_T")
# Ön plandaki pencereyi ararken yok sayılan yardımcı/şeffaf/küçük pencereler
_FRONT_WINDOW_MIN_ALPHA: float = 0.01
_FRONT_WINDOW_MIN_WIDTH: float = 100.0
_FRONT_WINDOW_MIN_HEIGHT: float = 50.0
# Ekran probuna katılan uygulama penceresinin en küçük kenarı (gölge/yardımcı pencereler dışarıda kalır)
_PROBE_WINDOW_MIN_EDGE: float = 20.0
_ACTION_PRESS: str = "AXPress"
_ACTION_FOCUS: str = "AXFocused"
_KEYCODE_RETURN: int = 36
_FOCUS_LABEL_LIMIT: int = 40
# Host onayından geçmiş finansal tıklamanın sonuç notu: etki doğrulanamasa da ikinci tetikleme yapılmaz
_APPROVED_NO_RETRY_NOTE: str = (
    "Onaylı ödeme/sipariş adımı olduğu için ikinci kez tetiklenmedi (çift işlem riski); aynı düğmeye yeniden basma."
)


@dataclass(frozen=True)
class ResolvedElement:
    """Çözümlenmiş hedef: anlık görüntü + öğe + AX referansı + eylem öncesi canlı durum."""
    captured: CapturedSnapshot
    element: Element
    ref: object
    live: LiveElement


@dataclass(frozen=True)
class InputState:
    """Ön plan eskalasyonundan önce kaydedilen girdi durumu: imleç konumu ve ön plandaki uygulama."""
    cursor: Tuple[float, float]
    front_pid: Optional[int]


def _process_alive(pid: int) -> bool:
    """Süreç hâlâ çalışıyor mu (kapanmış uygulamanın eski anlık görüntüsü her turda hata üretmesin)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # var ama başka kullanıcıya ait; çalışıyor
    return True


def _action_label(element: Element) -> str:
    """
    Eylemden (tıklama/yazma) önce öğenin insan okunur adı (sonuç metinleri bunu kullanır). Host onay kapısı ad
    adaylarının tamamına ax_snapshot.element_gate_labels ile bakar (Toolbox.cua_click_element). Parola alanında
    değer asla kullanılmaz (Element.value zaten boştur).
    """
    return element["label"] or element["description"] or element["value"] or role_name(element["role"], element["subrole"])


def _describe(element: Element) -> str:
    """Sonuç metinlerinin ortak öneki: '[N] rol "ad"'."""
    return f"[{element['index']}] {role_name(element['role'], element['subrole'])} {_action_label(element)!r}"


def _log_gui_action(tool: str, layer: str, effect: str, element: Element, started: float) -> None:
    """Hangi katmanın hangi etkiyle çalıştığını yapısal alanlarla kaydeder (saha ölçümü: katman başarı oranı, süre)."""
    logging.info("GUI eylemi tamamlandı", extra={
        "gui_tool": tool, "gui_layer": layer, "gui_effect": effect, "gui_role": element["role"],
        "gui_seconds": round(time.monotonic() - started, 3),
    })


def _front_app_owner() -> Optional[Tuple[str, int]]:
    """
    Ekrandaki en öndeki normal pencerenin sahibi (uygulama adı, pid); yoksa None. NSWorkspace CLI süreçlerinde
    ilk okumada donduğu için pencere sunucusunun z-sırası kullanılır (şeffaf ve küçük yardımcı pencereler atlanır).
    """
    windows: object = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements, Quartz.kCGNullWindowID,
    )
    for window in list(windows) if windows else []:
        if int(window.get(Quartz.kCGWindowLayer) or 0) != 0:
            continue
        if float(window.get(Quartz.kCGWindowAlpha) or 0.0) < _FRONT_WINDOW_MIN_ALPHA:
            continue
        bounds: Dict[str, float] = dict(window.get(Quartz.kCGWindowBounds) or {})
        if float(bounds.get("Width", 0.0)) < _FRONT_WINDOW_MIN_WIDTH or float(bounds.get("Height", 0.0)) < _FRONT_WINDOW_MIN_HEIGHT:
            continue
        return (str(window.get(Quartz.kCGWindowOwnerName) or ""), int(window[Quartz.kCGWindowOwnerPID]))
    return None


def _activate_pid(pid: int) -> bool:
    """
    Uygulamayı pid'e özel olarak ön plana alır (AXFrontmost) ve pencere yığınında en üste gelene kadar durum
    yoklaması yapar. Ölçüm: 4-70 ms. NSRunningApplication.activate başka uygulama etkinken hiçbir şey yapmaz
    (macOS işbirlikçi etkinleştirme), osascript ise aynı adlı başka örneği (kullanıcının Chrome'u) hedefler.
    """
    AX.AXUIElementSetAttributeValue(AX.AXUIElementCreateApplication(pid), "AXFrontmost", True)
    deadline: float = time.monotonic() + ACTIVATION_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        _raise_if_stopped()
        owner: Optional[Tuple[str, int]] = _front_app_owner()
        if owner is not None and owner[1] == pid:
            return True
        time.sleep(ACTIVATION_POLL_SECONDS)
    owner = _front_app_owner()
    return owner is not None and owner[1] == pid


def _capture_input_state() -> InputState:
    """İmleç konumunu ve ön plandaki uygulamayı kaydeder (eskalasyondan sonra geri yüklenir)."""
    location: object = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    owner: Optional[Tuple[str, int]] = _front_app_owner()
    return InputState((float(location.x), float(location.y)), owner[1] if owner is not None else None)


def _restore_input_state(state: InputState, target_pid: int) -> str:
    """
    İmleci ve önceki ön plan uygulamasını geri yükler; sonucu modele söylenecek kısa metinle döner. Geri
    yükleme başarısızlığı eylemin sonucunu bozmaz (tıklama zaten yapıldı) ama metinde açıkça belirtilir.
    """
    Quartz.CGWarpMouseCursorPosition(state.cursor)
    Quartz.CGAssociateMouseAndMouseCursorPosition(True)
    if state.front_pid is None or state.front_pid == target_pid:
        return "İmleç eski konumuna döndürüldü."
    previous: str = _bundle_name(state.front_pid) or f"pid {state.front_pid}"
    if _activate_pid(state.front_pid):
        return f"İmleç ve önceki ön plan uygulaması ({previous}) geri yüklendi."
    return f"İmleç geri alındı ama önceki ön plan uygulaması ({previous}) geri yüklenemedi."


def _probe_read(read: Callable[[], _T], fallback: _T) -> Tuple[_T, bool]:
    """Hafif probe okuması: yanıtsız uygulama (AX_TIMEOUT) 'okunamadı' sayılır ve yedek değer döner; başka hata yükselir."""
    try:
        return read(), True
    except ToolError as error:
        if error.code != "AX_TIMEOUT":
            raise
        return fallback, False


def _focus_signature(application: object) -> str:
    """Uygulamanın odaktaki öğesinin imzası (rol|ad|çerçeve); odak yoksa boş."""
    focused: object = ax_read(application, "AXFocusedUIElement")
    if focused is None:
        return ""
    label: str = clip_label(
        ax_text(ax_read(focused, "AXTitle")) or ax_text(ax_read(focused, "AXDescription")), _FOCUS_LABEL_LIMIT,
    )
    frame: Optional[Frame] = ax_frame(ax_read(focused, "AXPosition"), ax_read(focused, "AXSize"))
    role: str = ax_text(ax_read(focused, "AXRole"))
    return f"{role}|{label}|{quantized_frame(frame)}" if frame is not None else f"{role}|{label}"


def focused_text_value(app_name: Optional[str]) -> Optional[str]:
    """Koordinatla yazılmış alanın gerçek AX değeri; okunamaz/parola alanında None.

    Bu değer kaydetme/yayınlama kanıtı değil, yalnız hedef alanın geri okumasıdır.
    """
    if not app_name:
        return None
    try:
        application = AX.AXUIElementCreateApplication(_app_pid(app_name))
        focused = ax_read(application, "AXFocusedUIElement")
        if focused is None or ax_text(ax_read(focused, "AXRole")) not in TEXT_INPUT_ROLES:
            return None
        if ax_text(ax_read(focused, "AXSubrole")) == "AXSecureTextField":
            return None
        value = ax_read(focused, "AXValue")
        return value if isinstance(value, str) else None
    except ToolError:
        return None


def _window_stack_image(pid: int, window_frame: Frame) -> Optional[object]:
    """
    Uygulamanın pencere yığınının (ana pencere + üzerindeki açılır pencereler) renkli CGImage'i (pencere çerçevesi
    kadar). YALNIZ bu pid'in pencereleri yakalanır: başka uygulamaların pikselleri karışmaz, pencere örtülü olsa da
    yakalanır. Pencere bulunamaz ya da yakalama başarısız olursa None.
    """
    listing: object = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements, Quartz.kCGNullWindowID,
    )
    window_ids: List[int] = []
    for window in list(listing) if listing else []:
        if int(window[Quartz.kCGWindowOwnerPID]) != pid:
            continue
        bounds: Dict[str, float] = dict(window.get(Quartz.kCGWindowBounds) or {})
        frame: Frame = {
            "x": float(bounds.get("X", 0.0)), "y": float(bounds.get("Y", 0.0)),
            "w": float(bounds.get("Width", 0.0)), "h": float(bounds.get("Height", 0.0)),
        }
        if min(frame["w"], frame["h"]) >= _PROBE_WINDOW_MIN_EDGE and intersect(frame, window_frame) is not None:
            window_ids.append(int(window[Quartz.kCGWindowNumber]))
    if not window_ids:
        return None
    return Quartz.CGWindowListCreateImageFromArray(
        Quartz.CGRectMake(window_frame["x"], window_frame["y"], window_frame["w"], window_frame["h"]),
        window_ids, Quartz.kCGWindowImageBoundsIgnoreFraming,
    )


def _window_stack_frame(pid: int, window_frame: Frame) -> Optional[np.ndarray]:
    """Pencere yığınının küçük gri karesi (etki probu için; bkz. _window_stack_image); yakalanamazsa None."""
    image: Optional[object] = _window_stack_image(pid, window_frame)
    return gray_frame(image, EFFECT_SCREEN_EDGE) if image is not None else None


def read_effect_probe(captured: CapturedSnapshot, ref: object, previous_target: Optional[LiveElement]) -> EffectProbe:
    """
    Eylem etkisini anlamak için hafif durum parmak izi (pencere başlığı/sayısı, odak, hedefin canlı durumu ve
    ekran). Tam anlık görüntü yerine kullanılır: 0,2-1,5 sn'lik tarama 300 ms'lik doğrulama bütçesine sığmaz.
    Okunamayan sinyaller `unreadable`a yazılır; hedef okunamazsa önceki durum korunur (yanlış 'kayboldu' üretmez).
    """
    pid: int = captured.snapshot["pid"]
    title, title_ok = _probe_read(lambda: ax_text(ax_read(captured.window, "AXTitle")), "")
    count, count_ok = _probe_read(lambda: len(list(ax_read(captured.application, "AXWindows") or [])), 0)
    focus, focus_ok = _probe_read(lambda: _focus_signature(captured.application), "")
    target, target_ok = _probe_read(lambda: read_live_element(ref, AX_NODE_READER), previous_target)
    window_frame, frame_ok = _probe_read(
        lambda: ax_frame(ax_read(captured.window, "AXPosition"), ax_read(captured.window, "AXSize")), None,
    )
    screen: Optional[np.ndarray] = None
    screen_ok: bool = True
    if window_frame is not None and screen_capture_granted():
        screen = _window_stack_frame(pid, window_frame)
        screen_ok = screen is not None
    signals: List[Tuple[str, bool]] = [
        ("pencere başlığı", title_ok), ("pencere sayısı", count_ok), ("odak", focus_ok), ("hedef öğe", target_ok),
        ("pencere çerçevesi", frame_ok), ("ekran", screen_ok),
    ]
    return {
        "window_title": title, "window_count": count, "focus": focus, "target": target, "screen": screen,
        "unreadable": tuple(name for name, ok in signals if not ok),
    }


def _poll_target_effect(target: ResolvedElement, before: EffectProbe) -> Tuple[Effect, Tuple[str, ...]]:
    """Eylemden sonra en çok EFFECT_TIMEOUT_SECONDS boyunca hedefin etkisini yoklar (bkz. ax_snapshot.poll_effect)."""
    return poll_effect(
        lambda: read_effect_probe(target.captured, target.ref, before["target"]), before,
        EFFECT_TIMEOUT_SECONDS, EFFECT_POLL_SECONDS, time.monotonic, time.sleep, _raise_if_stopped,
    )


def _settled_probe(target: ResolvedElement, live: LiveElement) -> EffectProbe:
    """
    Tıklama öncesi taban çizgisi: etkinleştirmenin kendi yol açtığı değişimler (başlık çubuğu yeniden çizimi,
    odak) tıklamanın etkisi sanılmasın diye ardışık iki probe aynı olana kadar (en çok ACTIVATION_SETTLE_SECONDS) beklenir.
    """
    previous: EffectProbe = read_effect_probe(target.captured, target.ref, live)
    deadline: float = time.monotonic() + ACTIVATION_SETTLE_SECONDS
    while time.monotonic() < deadline:
        _raise_if_stopped()
        time.sleep(EFFECT_POLL_SECONDS)
        current: EffectProbe = read_effect_probe(target.captured, target.ref, previous["target"])
        if not changed_signals(previous, current):
            return current
        previous = current
    return previous


def _ax_action_name(element: Element) -> Optional[str]:
    """Öğe için arka plan AX eylemi: metin girdisinde odak, AXPress destekliyorsa AXPress; yoksa None."""
    if element["role"] in TEXT_INPUT_ROLES:
        return _ACTION_FOCUS
    return _ACTION_PRESS if _ACTION_PRESS in element["actions"] else None


def _perform_ax_action(ref: object, action: str) -> int:
    """AX eylemini uygular ve AXError kodunu döner (kod tek başına güvenilir değildir; etki ayrıca doğrulanır)."""
    if action == _ACTION_FOCUS:
        return int(AX.AXUIElementSetAttributeValue(ref, _ACTION_FOCUS, True))
    return int(AX.AXUIElementPerformAction(ref, action))


def _click_point(live_frame: Frame, window_frame: Optional[Frame]) -> Optional[Tuple[float, float]]:
    """Öğenin (pencerenin görünür kısmıyla kesişen) merkezi; hiç görünür kısmı yoksa None. Saf."""
    visible: Optional[Frame] = live_frame if window_frame is None else intersect(live_frame, window_frame)
    return frame_center(visible) if visible is not None else None


def element_region(visible: Frame, window: Frame, margin: int, minimum_radius: int) -> st.TextBox:
    """
    Öğenin görünür çerçevesini pencere görüntüsünün 0-1000 uzayında, kenar paylı bir OCR bölgesine çevirir; küçük
    simgelerin de okunabilmesi için yarıçap en az `minimum_radius` olur. Saf.
    """
    scale_x: float = 1000.0 / window["w"]
    scale_y: float = 1000.0 / window["h"]
    center_x: float = (visible["x"] + visible["w"] / 2 - window["x"]) * scale_x
    center_y: float = (visible["y"] + visible["h"] / 2 - window["y"]) * scale_y
    radius_x: float = max(visible["w"] / 2 * scale_x + margin, minimum_radius)
    radius_y: float = max(visible["h"] / 2 * scale_y + margin, minimum_radius)
    return st.region_around((round(center_x), round(center_y)), round(radius_x), round(radius_y))


def text_in_region(lines: List[st.TextLine], region: st.TextBox) -> Optional[str]:
    """Merkezi bölgenin içinde kalan OCR satırlarının metni (okuma sırasıyla, boşlukla birleşik); yoksa None. Saf."""
    text: str = " ".join(line["text"] for line in st.lines_within(lines, region)).strip()
    return text or None


def _hit_matches(ref: object, x: float, y: float) -> bool:
    """
    Noktadaki (tüm uygulamalar arasında en üstteki) öğe hedefin kendisi ya da torunu mu? Hayır ise nokta başka
    bir pencere/katmanın altında kalıyor: oraya tıklanırsa YANLIŞ öğe tıklanır. Web'de hedefin içindeki metin
    düğümü döner, bu yüzden ata zinciri yürünür.
    """
    error, hit = AX.AXUIElementCopyElementAtPosition(AX.AXUIElementCreateSystemWide(), x, y, None)
    if error != AX.kAXErrorSuccess or hit is None:
        return False
    node: object = hit
    for _ in range(HIT_TEST_ANCESTOR_DEPTH):
        if node is None:
            return False
        if node == ref:
            return True
        node = ax_read(node, "AXParent")
    return False


def _move_pointer(x: float, y: float) -> None:
    """Sistem genelinde fare imlecini noktaya taşır (üzerine gelme/hover etkileri tıklamadan ÖNCE oturur)."""
    _post_mouse_event(Quartz.kCGEventMouseMoved, x, y, Quartz.kCGMouseButtonLeft, 0)


def _click_pointer(x: float, y: float) -> None:
    """Sol tuşa basıp bırakır (ölçüm: CGEventPostToPid fare tıklaması Chrome'da etkisiz; sistem geneli olay gerekir)."""
    _post_mouse_event(Quartz.kCGEventLeftMouseDown, x, y, Quartz.kCGMouseButtonLeft, 1)
    time.sleep(CLICK_EVENT_GAP_SECONDS)
    _post_mouse_event(Quartz.kCGEventLeftMouseUp, x, y, Quartz.kCGMouseButtonLeft, 1)


def _post_key_to_pid(pid: int, keycode: int, flags: int) -> None:
    """Tuş basma/bırakmayı YALNIZ pid'e gönderir (odak değişse bile başka uygulamaya sızmaz)."""
    for key_down in (True, False):
        event = Quartz.CGEventCreateKeyboardEvent(None, keycode, key_down)
        Quartz.CGEventSetFlags(event, flags)
        Quartz.CGEventPostToPid(pid, event)
        time.sleep(UNICODE_CHUNK_DELAY_SECONDS)


def _post_text_to_pid(pid: int, text: str) -> None:
    """Metni klavye düzeninden bağımsız Unicode olarak YALNIZ pid'e yazar; satır sonları gerçek Enter tuşudur."""
    for line_index, line in enumerate(text.replace("\r\n", "\n").split("\n")):
        if line_index > 0:
            _post_key_to_pid(pid, _KEYCODE_RETURN, 0)
        for chunk in unicode_chunks(line, UNICODE_CHUNK_UNITS):
            units: int = len(chunk.encode("utf-16-le")) // 2
            for key_down in (True, False):
                event = Quartz.CGEventCreateKeyboardEvent(None, 0, key_down)
                Quartz.CGEventSetFlags(event, 0)
                Quartz.CGEventKeyboardSetUnicodeString(event, units, chunk)
                Quartz.CGEventPostToPid(pid, event)
            time.sleep(UNICODE_CHUNK_DELAY_SECONDS)


def _await_value(ref: object, expected: str) -> bool:
    """
    AXValue'nun beklenen metne dönmesini kısa süre yoklar. Yazımdan hemen sonraki okuma ESKİ değeri döner
    (ölçüldü: AX ağacı asenkron güncellenir), tek okuma yanlış olumsuz üretirdi.
    """
    wanted: str = normalize_text(expected)
    deadline: float = time.monotonic() + TEXT_READBACK_TIMEOUT_SECONDS
    while True:
        _raise_if_stopped()
        if normalize_text(ax_value_text(ax_read(ref, "AXValue"))) == wanted:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(EFFECT_POLL_SECONDS)


def _await_focus(application: object, ref: object) -> bool:
    """
    Alan odağının yerleştiğini doğrular; odak yerleşmeden gönderilen tuşlar başka alana gider. Önce hedefin AXFocused'u
    True olmalı (yoksa False: hiçbir tuş gönderilmez). Ardından uygulama düzeyi odak (AXFocusedUIElement) hedefe
    geçene kadar en çok FOCUS_SETTLE_SECONDS beklenir: ölçüldü, yalnız hedef özniteliğine güvenmek ilk tuşun
    (tümünü seç) kaybolmasına ve eski metnin sonuna yazılmasına yol açtı.
    """
    deadline: float = time.monotonic() + TEXT_READBACK_TIMEOUT_SECONDS
    while ax_read(ref, "AXFocused") is not True:
        _raise_if_stopped()
        if time.monotonic() >= deadline:
            return False
        time.sleep(EFFECT_POLL_SECONDS)
    settle_deadline: float = time.monotonic() + FOCUS_SETTLE_SECONDS
    while time.monotonic() < settle_deadline:
        _raise_if_stopped()
        if ax_read(application, "AXFocusedUIElement") == ref:
            break
        time.sleep(ACTIVATION_POLL_SECONDS)
    return True


def _select_all(ref: object, name: str) -> None:
    """
    Alanın tüm metnini AXSelectedTextRange ile seçer ve seçimin oturduğunu (AXSelectedText == AXValue) yoklayarak
    doğrular. Klavye kısayolu (Cmd+A) BİLEREK kullanılmaz: tuş kodu fiziksel konumdur, AZERTY gibi düzenlerde aynı
    kod Cmd+Q olur; ayrıca Chrome'da asenkron uygulandığı için (ölçüldü: 77 ms'ye kadar gecikti) beklenmeden yazılan
    metin eski değerin SONUNA eklendi. Alan boşsa seçilecek bir şey yoktur. Seçim oturmazsa hiçbir şey yazılmaz.
    """
    raw: object = ax_read(ref, "AXValue")
    if not isinstance(raw, str) or not raw:
        return
    units: int = len(raw.encode("utf-16-le")) // 2
    code: int = int(AX.AXUIElementSetAttributeValue(
        ref, "AXSelectedTextRange", AX.AXValueCreate(AX.kAXValueCFRangeType, CoreFoundation.CFRange(0, units)),
    ))
    deadline: float = time.monotonic() + SELECT_ALL_TIMEOUT_SECONDS
    while code == AX_ERROR_SUCCESS:
        _raise_if_stopped()
        selected: object = ax_read(ref, "AXSelectedText")
        if isinstance(selected, str) and normalize_text(selected) == normalize_text(raw):
            return
        if time.monotonic() >= deadline:
            break
        time.sleep(ACTIVATION_POLL_SECONDS)
    raise ToolError(
        f"{name}: tümünü seçme doğrulanamadı (kod {code}); metin eskisinin sonuna eklenmesin diye hiçbir karakter "
        "yazılmadı. Alanı cua_fill_field ile koordinattan doldurmayı dene.",
        "SELECT_ALL_FAILED", True,
    )


def _current_text(ref: object) -> str:
    """Alanın şu anki değeri (başarısızlık mesajında modele gösterilir); okunamazsa boş. Parola alanında çağrılmaz."""
    return clip_label(ax_value_text(ax_read(ref, "AXValue")), TYPED_TEXT_ECHO_LIMIT)


def _stale_error(index: int, reason: str) -> ToolError:
    """Bayat öğe hatası (STALE_ELEMENT): model listeyi yenileyip yeniden denemeli."""
    return ToolError(
        f"[{index}] öğesi bayat: {reason}. cua_snapshot ile listeyi yenile ve güncel liste kimliğiyle yeniden dene.",
        "STALE_ELEMENT", True,
    )


# Uygulama adı AppleScript kaynağına gömülmez, argv ile geçer: kaçış/enjeksiyon sorunu olmaz ve ASCII dışı harfler
# (Fotoğraflar, Sistem Ayarları) betik metnine \uXXXX olarak girmez (browser.py'deki Chrome betiğiyle aynı yöntem).
_ACTIVATE_APPLESCRIPT: str = "on run argv\ntell application (item 1 of argv) to activate\nend run"


class CUA:
    """
    macOS GUI konnektörü: uygulama etkinleştirme ve erişilebilirlik (AX) ağacı üzerinden öğe listeleme/tıklama
    (eski numaralı liste) ile öğe tabanlı anlık görüntü: cua_snapshot -> indeksli tıklama/yazma -> doğrulama.
    """

    def __init__(self) -> None:
        # Uygulama adı (casefold) -> son listedeki AX referansları (öğe numarası = indeks + 1)
        self._snapshots: Dict[str, List[object]] = {}
        # Öğe tabanlı depo: pid -> o uygulamanın SON anlık görüntüsü (eskiler geçersizdir), sayaç ve web hazırlığı.
        # Araçlar worker thread'lerinde koşar; depo değişimleri kilitle korunur.
        self._lock: threading.Lock = threading.Lock()
        self._stored: Dict[int, CapturedSnapshot] = {}
        self._next_snapshot: int = 1
        self._web_ready: Dict[int, bool] = {}
        self._last_pid: Optional[int] = None

    def get_app(self, app_name: str) -> str:
        """Uygulamayı osascript ile başlatır/öne getirir; öne gelmesini ve penceresini Toolbox.cua_get_app doğrular."""
        try:
            result: subprocess.CompletedProcess[str] = subprocess.run(
                ["osascript", "-e", _ACTIVATE_APPLESCRIPT, app_name],
                env=child_environment(), capture_output=True, text=True, timeout=CUA_ACTIVATE_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as error:
            # Canlı hata (29 Eylül): uydurma adla (yüklü olmayan 'Cursor') zaman aşımı "ekran görüntüsüne bak" diyordu;
            # model masaüstü görüntüsünden "uygulama açık değil" sonucuna vardı. Hata artık çalışan uygulamaları verir.
            raise ToolError(
                f"Uygulama {CUA_ACTIVATE_TIMEOUT_SECONDS:.0f} sn içinde etkinleşmedi: {app_name}. {_running_apps_text()} "
                "Aradığın bunlardan biriyse cua_get_app'i listedeki tam adla çağır; listede yoksa ve yüklüyse açılışı "
                "sürüyor olabilir, aynı adla bir kez daha dene. Ekran görüntüsü yalnız ekrandaki pencereleri gösterir: "
                "bir uygulamanın kapalı olduğunu kanıtlamaz.",
                "APP_ACTIVATE_TIMEOUT", True,
            ) from error
        if result.returncode == 0:
            return f"{app_name} aktif edildi ve öne getirildi."
        raise ToolError(
            f"Uygulama bulunamadı veya aktif edilemedi: {app_name}, ayrıntı={result.stderr.strip()}. "
            f"{_running_apps_text()}",
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

    def _listed_element(self, app_name: str, element_id: int) -> object:
        """Son numaralı listedeki öğenin AX referansı; liste yoksa ya da numara geçersizse açık hata."""
        refs: Optional[List[object]] = self._snapshots.get(app_name.casefold())
        if refs is None:
            raise ToolError(f"{app_name} için öğe listesi yok; önce cua_get_ax_state çağır.", "AX_NO_SNAPSHOT", True)
        if not 1 <= element_id <= len(refs):
            raise ToolError(f"Geçersiz öğe numarası: {element_id} (geçerli 1-{len(refs)}).", "INVALID_ELEMENT", True)
        return refs[element_id - 1]

    def element_labels(self, app_name: str, element_id: int) -> List[str]:
        """
        Son numaralı listedeki öğenin tıklama anındaki görünen ad adayları (başlık, açıklama, değer; hiçbiri yoksa ilk
        alt metin). Host onay kapısı bunlara bakar; eski numaralı liste yolu için (yeni yol: Snapshot etiketleri).
        Basılınca işlem tamamlayabilen roller (COMMIT_ROLES) dışında (metin alanı, onay kutusu…) boş liste döner:
        onların adı bir düğme etiketi değildir.
        """
        _require_accessibility()
        element: object = self._listed_element(app_name, element_id)
        if _ax_attribute(element, "AXRole") not in COMMIT_ROLES:
            return []
        labels: List[str] = [
            text for text in (_ax_short_text(_ax_attribute(element, attribute))
                              for attribute in ("AXTitle", "AXDescription", "AXValue")) if text
        ]
        if labels:
            return labels
        descendant: str = _ax_descendant_text(element)
        return [descendant] if descendant else []

    def click_element(self, app_name: str, element_id: int) -> str:
        """
        Son listedeki öğeye tıklar: metin alanları AXFocused ile odaklanır, diğerleri
        AXPress alır. AX eylemi desteklenmiyorsa hibrit protokolün koordinat katmanı
        olarak öğe merkezine gerçek fare tıklaması yapılır (sonuçta açıkça belirtilir).
        """
        _require_accessibility()
        element: object = self._listed_element(app_name, element_id)
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

    # --- Öğe tabanlı anlık görüntü, indeksli tıklama ve yazma ---

    def capture(self, pid: int, app_name: str, limits: SnapshotLimits) -> Snapshot:
        """
        pid'in penceresinin öğe tabanlı anlık görüntüsünü alır ve o uygulamanın SON anlık görüntüsü olarak
        saklar (öncekiler geçersiz olur). Chromium uygulamasında web erişilebilirliği ilk yakalamada açılır (~2 sn).
        """
        _require_accessibility()
        with self._lock:
            number: int = self._next_snapshot
            self._next_snapshot += 1
            # Hiç yakalanmamışsa web ağacı için uzun, önceki yakalama hazır bulmadıysa kısa bekleme; hazırsa yok.
            # Örtülü Chrome'da her turda 4 sn beklememek için ikinci ve sonrası kısa tutulur.
            ready: Optional[bool] = self._web_ready.get(pid)
            web_wait: float = AX_WEB_READY_SECONDS if ready is None else (0.0 if ready else AX_WEB_RETRY_SECONDS)
        captured: CapturedSnapshot = capture_snapshot(pid, app_name, f"s{number}", limits, web_wait)
        with self._lock:
            self._stored[pid] = captured
            self._last_pid = pid
            # None (Chromium değil) sayılır hazır: web beklemesi gerekmez
            self._web_ready[pid] = captured.snapshot["stats"]["web_ready"] is not False
        remember_snapshot_labels(captured.snapshot)
        return captured.snapshot

    def observation_target(self, scope_app: Optional[str]) -> Optional[Tuple[int, str]]:
        """
        Otomatik gözlemdeki AX özetinin hedefi (pid, uygulama adı): modelin son anlık görüntü aldığı uygulama;
        yoksa görsel kapsam uygulaması; ikisi de yoksa None (AX yolu kullanılmıyor, özet eklenmez).
        """
        with self._lock:
            last: Optional[CapturedSnapshot] = self._stored.get(self._last_pid) if self._last_pid is not None else None
        if last is not None and _process_alive(last.snapshot["pid"]):
            return last.snapshot["pid"], last.snapshot["app"]
        if scope_app is not None:
            return _app_pid(scope_app), scope_app
        return None

    def _captured_snapshot(self, snapshot_id: str) -> CapturedSnapshot:
        """
        Kimliği (bkz. normalize_snapshot_id) uyuşan SON anlık görüntü; hiç anlık görüntü yoksa AX_NO_SNAPSHOT, kimlik
        bayat/bilinmiyorsa geçerli kimlikleri listeleyen STALE_ELEMENT.
        """
        wanted: str = normalize_snapshot_id(snapshot_id)
        with self._lock:
            stored: List[CapturedSnapshot] = list(self._stored.values())
        if not stored:
            raise ToolError("Henüz anlık görüntü yok; önce cua_snapshot çağır.", "AX_NO_SNAPSHOT", True)
        match: Optional[CapturedSnapshot] = next((item for item in stored if item.snapshot["id"] == wanted), None)
        if match is None:
            known: str = ", ".join(f"{item.snapshot['id']} ({item.snapshot['app']})" for item in stored)
            raise ToolError(
                f"Liste {wanted!r} geçerli değil (daha yeni bir liste alındı). Geçerli listeler: {known}. "
                "cua_snapshot çağırıp güncel listenin kimliğiyle indeks ver.",
                "STALE_ELEMENT", True,
            )
        return match

    def snapshot_owner(self, snapshot_id: str) -> Tuple[int, str]:
        """Anlık görüntünün sahibi uygulama (pid, ad): ön plan/hassas hedef denetimi için; AX'e dokunmaz."""
        captured: CapturedSnapshot = self._captured_snapshot(snapshot_id)
        return captured.snapshot["pid"], captured.snapshot["app"]

    def resolve_element(self, snapshot_id: str, index: int) -> ResolvedElement:
        """
        (liste kimliği, indeks) çiftini canlı öğeye çözer. Kimlik, o uygulamanın SON anlık görüntüsüyle
        uyuşmalı; öğe yeniden okunur: yok olduysa ya da rol/ad/konum uyuşmuyorsa STALE_ELEMENT (yanlış öğeye
        eylem yapılmaz). Model rakamı 's' öneksiz verirse ('3') kabul edilir.
        """
        match: CapturedSnapshot = self._captured_snapshot(snapshot_id)
        elements: List[Element] = match.snapshot["elements"]
        if isinstance(index, bool) or not isinstance(index, int) or not 1 <= index <= len(elements):
            raise ToolError(f"Geçersiz öğe numarası: {index!r} (geçerli 1-{len(elements)}).", "INVALID_ELEMENT", True)
        element: Element = elements[index - 1]
        ref: object = match.refs[index - 1]
        # Yanıtsız uygulama eylem/okuma çağrılarını varsayılan ~6 sn bloklamasın (uygulama düzeyi ayar öğeye geçmeyebilir)
        AX.AXUIElementSetMessagingTimeout(ref, AX_MESSAGING_TIMEOUT_SECONDS)
        live: Optional[LiveElement] = read_live_element(ref, AX_NODE_READER)
        if live is None:
            raise _stale_error(index, "öğe artık yok (arayüz değişti)")
        reason: Optional[str] = stale_reason(element, live)
        if reason is not None:
            raise _stale_error(index, reason)
        return ResolvedElement(match, element, ref, live)

    def prepare_target(self, snapshot_id: str, index: int) -> ResolvedElement:
        """İzin denetimi + öğe çözümleme + pasif öğe reddi; eylem basamaklarının ve onay kapısının ortak girişi."""
        _require_accessibility()
        target: ResolvedElement = self.resolve_element(snapshot_id, index)
        if not target.live["enabled"]:
            raise ToolError(
                f"{_describe(target.element)} pasif (disabled); işlem yapılmadı. "
                "Koordinat veya klavye ile yeniden tıklama. Formun hata/karakter sayacı ve "
                "alan değerlerini incele; nedenini düzeltip yeni öğe listesi al.",
                "ELEMENT_DISABLED", True,
            )
        return target

    def click_snapshot_element(self, snapshot_id: str, index: int) -> str:
        """
        Anlık görüntüdeki öğeye tıklar; eylem MERDİVENİ:
        1) ax_press (arka plan): AXPress (metin girdisinde AXFocused) -> ≤300 ms durum yoklamasıyla doğrula.
           Dönüş kodu güvenilmez, bu yüzden etki ayrıca doğrulanır. Etki 'bilinmiyor' ise ESKALASYON YOK (çift tetikleme).
        2) fg_click (ön plan, geçici): etki yok ya da eylem desteklenmiyorsa imleç/ön plan kaydedilir, uygulama
           öne alınır, vuruş testi hedefi doğrularsa fare tıklanır, etki doğrulanır ve girdi durumu geri yüklenir.
        3) ACTION_INEFFECTIVE: hiçbir katman etki yaratmadıysa açık hata.
        Host onay kapısı çağıran katmandadır (Toolbox.cua_click_element): ödeme/sipariş düğmesi için click_approved_element.
        """
        return self.click_resolved_element(self.prepare_target(snapshot_id, index))

    def click_resolved_element(self, target: ResolvedElement) -> str:
        """Çözümlenmiş hedefe sıradan tıklama merdiveni (bkz. click_snapshot_element)."""
        started: float = time.monotonic()
        element: Element = target.element
        index: int = element["index"]
        name: str = _describe(element)
        action: Optional[str] = _ax_action_name(element)
        if action == _ACTION_FOCUS and target.live["focused"]:
            return f"{name}: zaten odaklı; işlem gerekmedi."
        reason: str = "öğe AXPress eylemini desteklemiyor"
        if action is not None:
            before: EffectProbe = read_effect_probe(target.captured, target.ref, target.live)
            code: int = _perform_ax_action(target.ref, action)
            effect: Optional[Effect] = None
            signals: Tuple[str, ...] = ()
            if code in (AX_ERROR_SUCCESS, AX_ERROR_CANNOT_COMPLETE):
                effect, signals = _poll_target_effect(target, before)
            step: PressStep = press_outcome(code, effect)
            if step == "stale":
                raise _stale_error(index, "öğe eylem sırasında kayboldu")
            if step == "permission":
                raise ToolError(accessibility_help(), "AX_PERMISSION", False)
            verb: str = "odaklandı" if action == _ACTION_FOCUS else "tıklandı"
            if step == "done":
                _log_gui_action("cua_click_element", "ax_press", "changed", element, started)
                return f"{name}: {action} ile {verb} (katman: ax_press, arka plan); etki doğrulandı ({', '.join(signals)})."
            if step == "ambiguous":
                _log_gui_action("cua_click_element", "ax_press", "unknown", element, started)
                return (
                    f"{name}: {action} uygulandı (kod {code}) ama etki doğrulanamadı (okunamayan AX sinyali var). "
                    "Ekranı gözlemle; etkiyi görmeden körlemesine yeniden tıklama."
                )
            reason = (
                f"{action} etkisiz (kod {code}, {round(EFFECT_TIMEOUT_SECONDS * 1000)} ms içinde değişiklik yok)"
                if code in (AX_ERROR_SUCCESS, AX_ERROR_CANNOT_COMPLETE)
                else f"{action} desteklenmedi (kod {code})"
            )
        return self._foreground_click(target, name, reason, started)

    def _foreground_press(self, target: ResolvedElement, name: str) -> Tuple[Effect, Tuple[str, ...], str]:
        """
        Ön plan tıklaması çekirdeği: girdi durumunu kaydet -> etkinleştir -> vuruş testi -> tıkla -> doğrula -> geri
        yükle. (etki, değişen sinyaller, geri yükleme notu) döner; etkinleştirme/bayatlık/örtülme/görünürlük sorunları
        ToolError'dur (tıklama yapılmaz). Sonucu biçimleme çağıranındır: sıradan tıklama etkisizi hata sayar, onaylı
        finansal tıklama saymaz (bkz. _foreground_click, _approved_foreground_click).
        """
        pid: int = target.captured.snapshot["pid"]
        state: InputState = _capture_input_state()
        outcome: Tuple[Effect, Tuple[str, ...]]
        try:
            if not _activate_pid(pid):
                raise ToolError(f"{name}: uygulama ön plana alınamadı; fare tıklaması yapılmadı.", "APP_ACTIVATION_FAILED", True)
            live: Optional[LiveElement] = read_live_element(target.ref, AX_NODE_READER)
            if live is None:
                raise _stale_error(target.element["index"], "öğe artık yok (arayüz değişti)")
            window_frame: Optional[Frame] = ax_frame(
                ax_read(target.captured.window, "AXPosition"), ax_read(target.captured.window, "AXSize"),
            )
            point: Optional[Tuple[float, float]] = _click_point(live["frame"], window_frame)
            if point is None:
                raise ToolError(
                    f"{name}: öğe pencerenin görünür alanı dışında; kaydırıp cua_snapshot ile yenile.",
                    "ELEMENT_OFFSCREEN", True,
                )
            if not _hit_matches(target.ref, point[0], point[1]):
                raise ToolError(
                    f"{name}: hedef noktada başka bir pencere/katman üstte (yanlış öğeye tıklanabilirdi); tıklanmadı. "
                    "Engelleyen pencereyi kapat ya da farklı bir yöntem dene.",
                    "ELEMENT_OBSCURED", True,
                )
            # Önce imleç hedefe taşınır ve durum oturunca taban çizgisi alınır: hover/etkinleştirme değişimleri
            # tıklamanın etkisi sanılmasın; sonra basılıp bırakılır.
            _move_pointer(point[0], point[1])
            before: EffectProbe = _settled_probe(target, live)
            _click_pointer(point[0], point[1])
            outcome = _poll_target_effect(target, before)
        finally:
            restore_note: str = _restore_input_state(state, pid)
        return outcome[0], outcome[1], restore_note

    def _foreground_click(self, target: ResolvedElement, name: str, reason: str, started: float) -> str:
        """Eskalasyon basamağı (sıradan tıklama): ön plan tıklaması + etki değerlendirmesi (bkz. click_resolved_element)."""
        effect, signals, restore_note = self._foreground_press(target, name)
        _log_gui_action("cua_click_element", "fg_click", effect, target.element, started)
        if effect == "changed":
            return (
                f"{name}: {reason} → ön plana alınıp fare tıklaması yapıldı (katman: fg_click, ön plan geçici); "
                f"etki doğrulandı ({', '.join(signals)}). {restore_note}"
            )
        if effect == "unchanged":
            raise ToolError(
                f"{name}: hiçbir katman etki yaratmadı ({reason}; fare tıklaması da {round(EFFECT_TIMEOUT_SECONDS * 1000)} ms "
                f"içinde değişiklik göstermedi). {restore_note} Öğe yalnız görsel yolla çalışıyor olabilir: "
                "cua_click_text veya take_screenshot noktasıyla cua_click_point dene.",
                "ACTION_INEFFECTIVE", True,
            )
        return (
            f"{name}: {reason} → fare tıklaması yapıldı (katman: fg_click) ama etki doğrulanamadı (okunamayan AX "
            f"sinyali var). Ekranı gözlemle; körlemesine yeniden tıklama. {restore_note}"
        )

    def click_approved_element(self, target: ResolvedElement) -> str:
        """
        Host onayından geçmiş ödeme/sipariş düğmesine TEK tetikleme. Sıradan merdivenden farkı: AXPress uygulandıktan
        sonra etki doğrulanamadıysa ya da etkisiz göründüyse ikinci bir tetikleme (fg_click) YAPILMAZ: etki yalnız
        gecikmiş olabilir ve ikinci tıklama çift ödeme/sipariş olurdu; sonuç 'etki doğrulanamadı, sonraki gözlemi
        kontrol et' olarak döner. Ön plan tıklaması yalnız AX eylemi HİÇ uygulanamadıysa (öğe AXPress'i
        desteklemiyor) ilk ve tek tetiklemedir.
        """
        started: float = time.monotonic()
        element: Element = target.element
        name: str = _describe(element)
        action: Optional[str] = _ax_action_name(element)
        if action is None:
            return self._approved_foreground_click(target, name, "öğe AXPress eylemini desteklemiyor", started)
        before: EffectProbe = read_effect_probe(target.captured, target.ref, target.live)
        code: int = _perform_ax_action(target.ref, action)
        effect: Optional[Effect] = None
        signals: Tuple[str, ...] = ()
        if code in (AX_ERROR_SUCCESS, AX_ERROR_CANNOT_COMPLETE):
            effect, signals = _poll_target_effect(target, before)
        step: ApprovedStep = approved_press_outcome(code, effect)
        if step == "stale":
            raise _stale_error(element["index"], "öğe eylem sırasında kayboldu")
        if step == "permission":
            raise ToolError(accessibility_help(), "AX_PERMISSION", False)
        if step == "foreground":
            return self._approved_foreground_click(target, name, f"{action} desteklenmedi (kod {code})", started)
        _log_gui_action("cua_click_element", "ax_press", "changed" if step == "done" else "unknown", element, started)
        if step == "done":
            return (
                f"{name}: {action} ile tıklandı (katman: ax_press, arka plan); etki doğrulandı ({', '.join(signals)}). "
                "Host onaylı adım tek kez tetiklendi."
            )
        return (
            f"{name}: {action} uygulandı (kod {code}) ama etki doğrulanamadı; sonraki gözlemi kontrol et. "
            f"{_APPROVED_NO_RETRY_NOTE}"
        )

    def _approved_foreground_click(self, target: ResolvedElement, name: str, reason: str, started: float) -> str:
        """Onaylı finansal tıklamanın ön plan basamağı: etki doğrulanamazsa hata değil 'kontrol et' sonucu (yeniden basma yok)."""
        effect, signals, restore_note = self._foreground_press(target, name)
        _log_gui_action("cua_click_element", "fg_click", "changed" if effect == "changed" else "unknown", target.element, started)
        if effect == "changed":
            return (
                f"{name}: {reason} → ön plana alınıp fare tıklaması yapıldı (katman: fg_click, ön plan geçici); "
                f"etki doğrulandı ({', '.join(signals)}). Host onaylı adım tek kez tetiklendi. {restore_note}"
            )
        return (
            f"{name}: {reason} → fare tıklaması yapıldı (katman: fg_click) ama etki doğrulanamadı; sonraki gözlemi "
            f"kontrol et. {_APPROVED_NO_RETRY_NOTE} {restore_note}"
        )

    def element_visible_text(self, target: ResolvedElement) -> Optional[str]:
        """
        Adı olmayan öğenin ekranda görünen metni: öğenin görünür çerçevesi, uygulamanın pencere yığınının
        görüntüsünden (örtülü pencere de yakalanır) ROI OCR ile okunur; çerçevede metin yoksa None (simge). Ekran
        kaydı izni ya da görüntü yoksa açık hata: etiket okunamazken öğe sessizce 'sıradan' sayılmaz.
        """
        _require_screen_capture()
        window_frame: Optional[Frame] = ax_frame(
            ax_read(target.captured.window, "AXPosition"), ax_read(target.captured.window, "AXSize"),
        )
        visible: Optional[Frame] = None if window_frame is None else intersect(target.live["frame"], window_frame)
        if window_frame is None or visible is None:
            raise ToolError(
                f"{_describe(target.element)}: öğe pencerenin görünür alanı dışında; etiketi okunamadı, tıklanmadı.",
                "ELEMENT_OFFSCREEN", True,
            )
        image: Optional[object] = _window_stack_image(target.captured.snapshot["pid"], window_frame)
        if image is None:
            raise ToolError(
                f"{_describe(target.element)}: pencere görüntüsü alınamadı; etiketi okunamadı, tıklanmadı.",
                "SCREEN_CAPTURE_FAILED", True,
            )
        region: st.TextBox = element_region(visible, window_frame, ELEMENT_LABEL_MARGIN, ELEMENT_LABEL_MIN_RADIUS)
        try:
            return text_in_region(st.recognize_region(image, region), region)
        except st.TextRecognitionError as error:
            raise ToolError(str(error), "OCR_FAILED", True) from error

    def set_snapshot_text(self, snapshot_id: str, index: int, text: str) -> str:
        """
        Metin alanının içeriğini DEĞİŞTİRİR (Enter'a basmaz); her basamak geri okumayla doğrulanır:
        1) ax_value (arka plan): AXValue yaz -> yoklamalı geri oku.
        2) ax_selected_text (arka plan): yalnız alan boşsa AXSelectedText yaz -> geri oku.
        3) fg_type (ön plan, geçici): uygulamayı öne al, AXFocused (doğrulanmadan yazılmaz), tümünü seç, pid'e Unicode yaz.
        Parola alanında değer okunamadığı için doğrulama yoktur ve yazılan metin sonuçta gösterilmez.
        """
        if not isinstance(text, str):
            raise ToolError("text metin olmalı.", "INVALID_TEXT", False)
        started: float = time.monotonic()
        target: ResolvedElement = self.prepare_target(snapshot_id, index)
        element: Element = target.element
        name: str = _describe(element)
        if element["role"] not in TEXT_INPUT_ROLES:
            raise ToolError(
                f"{name} bir metin alanı değil; tıklamak için cua_click_element kullan.", "NOT_EDITABLE", True,
            )
        secure: bool = element["secure"]
        shown: str = "gizli alan: metin gösterilmiyor" if secure else _clip(text, TYPED_TEXT_ECHO_LIMIT)
        attempts: List[str] = []
        code: int = int(AX.AXUIElementSetAttributeValue(target.ref, "AXValue", text))
        if code == AX_ERROR_INVALID_ELEMENT:
            raise _stale_error(index, "öğe yazım sırasında kayboldu")
        if code == AX_ERROR_SUCCESS:
            if secure:
                _log_gui_action("cua_set_text_element", "ax_value", "unknown", element, started)
                return (
                    f"{name}: gizli alana {len(text)} karakter yazıldı (katman: ax_value, arka plan); "
                    "değer okunmadığı için doğrulanamaz."
                )
            if _await_value(target.ref, text):
                _log_gui_action("cua_set_text_element", "ax_value", "changed", element, started)
                return (
                    f"{name}: {len(text)} karakter yazıldı, geri okunup doğrulandı (katman: ax_value, arka plan): {shown}"
                )
            attempts.append("AXValue yazıldı ama geri okumada değişmedi")
        else:
            attempts.append(f"AXValue yazılamadı (kod {code})")
        if not target.live["value"] and not secure:
            code = int(AX.AXUIElementSetAttributeValue(target.ref, "AXSelectedText", text))
            if code == AX_ERROR_SUCCESS and _await_value(target.ref, text):
                _log_gui_action("cua_set_text_element", "ax_selected_text", "changed", element, started)
                return (
                    f"{name}: {len(text)} karakter yazıldı, geri okunup doğrulandı "
                    f"(katman: ax_selected_text, arka plan): {shown}"
                )
            attempts.append(
                "AXSelectedText yazıldı ama geri okumada değişmedi" if code == AX_ERROR_SUCCESS
                else f"AXSelectedText yazılamadı (kod {code})"
            )
        outcome, restore_note = self._foreground_type(target, name, text, secure)
        _log_gui_action(
            "cua_set_text_element", "fg_type", {"verified": "changed", "unverifiable": "unknown"}.get(outcome, "unchanged"),
            element, started,
        )
        reason: str = "; ".join(attempts)
        if outcome == "verified":
            return (
                f"{name}: {reason} → ön plana alınıp yazıldı, geri okunup doğrulandı (katman: fg_type, ön plan geçici): "
                f"{shown}. {restore_note}"
            )
        if outcome == "unverifiable":
            return (
                f"{name}: {reason} → ön plana alınıp gizli alana {len(text)} karakter yazıldı (katman: fg_type); "
                f"değer okunmadığı için doğrulanamaz. {restore_note}"
            )
        current: str = "" if secure else f" Alanın şu anki değeri: {_current_text(target.ref)!r}."
        raise ToolError(
            f"{name}: hiçbir katman metni yazamadı ({reason}; ön plan yazımı da geri okumada tutmadı).{current} "
            f"{restore_note} Alanı cua_fill_field ile koordinattan doldurmayı dene.",
            "ACTION_INEFFECTIVE", True,
        )

    def _foreground_type(
        self, target: ResolvedElement, name: str, text: str, secure: bool,
    ) -> Tuple[Literal["verified", "unverifiable", "failed"], str]:
        """
        Metin merdiveninin ön plan basamağı. Odak doğrulanmadan HİÇBİR tuş gönderilmez (metin başka alana
        gitmesin). Tuşlar yalnız hedef pid'e gider; girdi durumu her koşulda geri yüklenir.
        """
        pid: int = target.captured.snapshot["pid"]
        state: InputState = _capture_input_state()
        outcome: Literal["verified", "unverifiable", "failed"] = "failed"
        try:
            if not _activate_pid(pid):
                raise ToolError(f"{name}: uygulama ön plana alınamadı; yazılmadı.", "APP_ACTIVATION_FAILED", True)
            if int(AX.AXUIElementSetAttributeValue(target.ref, _ACTION_FOCUS, True)) == AX_ERROR_INVALID_ELEMENT:
                raise _stale_error(target.element["index"], "öğe odaklanırken kayboldu")
            if not _await_focus(target.captured.application, target.ref):
                raise ToolError(
                    f"{name}: alan odaklanamadı; metin yanlış alana gitmesin diye hiçbir tuş gönderilmedi.",
                    "ELEMENT_NOT_FOCUSED", True,
                )
            _select_all(target.ref, name)
            _post_text_to_pid(pid, text)
            outcome = "unverifiable" if secure else ("verified" if _await_value(target.ref, text) else "failed")
        finally:
            restore_note: str = _restore_input_state(state, pid)
        return outcome, restore_note


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
