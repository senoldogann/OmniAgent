"""
OmniAgent GUI Girdi Modülü (tools/gui_input.py)
macOS Erişilebilirlik (AX) ağacı üzerinden yüksek hassasiyetli etkileşim,
Unicode metin girişi ve koordinat tabanlı fare kontrolü.
"""
import os
import time
import json
import subprocess
import numpy as np
import pyautogui
import Quartz
import AppKit
from typing import Callable, Dict, List, Optional, Tuple, Union, Any

from .types import (
    AX_ELEMENT_LIMIT, AX_LABEL_SEARCH_NODES, AX_MESSAGING_TIMEOUT_SECONDS,
    AX_NODE_LIMIT, AX_SCAN_BUDGET_SECONDS, MODEL_SCREEN_SIZE,
    UNICODE_CHUNK_DELAY_SECONDS, UNICODE_CHUNK_UNITS,
    ActionStep, AXElement, ToolError, ScreenGeometry,
    TYPED_TEXT_ECHO_LIMIT, MAX_WAIT_SECONDS, clip_text,
)

_clip = clip_text
from .screen import (
    current_geometry, parse_point, points_to_model,
    click_model_point, move_model_point, post_scroll,
    changed_region,
)

# AX Sabitleri
AX = Quartz
_AX_ACTIONABLE_ROLES = {"AXButton", "AXCheckBox", "AXRadioButton", "AXSwitch", "AXPopUpButton", "AXMenuItem", "AXTextField", "AXTextArea", "AXSearchField", "AXSlider", "AXStepper"}
_AX_TEXT_INPUT_ROLES = {"AXTextField", "AXTextArea", "AXSearchField"}
_AX_SCAN_ATTRIBUTES = ["AXRole", "AXEnabled", "AXPosition", "AXSize", "AXTitle", "AXDescription", "AXPlaceholderValue", "AXValue", "AXChildren"]

_KEY_ALIASES = {
    "enter": "Enter", "return": "Enter", "esc": "Escape", "escape": "Escape",
    "tab": "Tab", "space": "Space", "backspace": "Backspace", "delete": "Delete",
    "arrowup": "ArrowUp", "arrowdown": "ArrowDown", "arrowleft": "ArrowLeft", "arrowright": "ArrowRight",
    "cmd": "Meta", "command": "Meta", "meta": "Meta", "ctrl": "Control", "control": "Control",
    "alt": "Alt", "option": "Alt", "shift": "Shift",
}

def _require_accessibility() -> None:
    try:
        Quartz.AXUIElementCreateSystemWide()
    except Exception:
        raise ToolError("Erişilebilirlik izni yok. Lütfen Sistem Ayarları > Gizlilik ve Güvenlik > Erişilebilirlik kısmından izin verin.", "AX_PERMISSION", False)

def _ax_attribute(element: object, attribute: str) -> object:
    error, value = Quartz.AXUIElementCopyAttributeValue(element, attribute, None)
    return value if error == Quartz.kAXErrorSuccess else None

def _ax_present(value: object) -> object:
    if isinstance(value, Quartz.AXValueRef) and Quartz.AXValueGetType(value) == Quartz.kAXValueAXErrorType:
        return None
    return value

def _ax_point(value: object) -> Optional[Tuple[float, float]]:
    if not isinstance(value, Quartz.AXValueRef): return None
    ok, point = Quartz.AXValueGetValue(value, Quartz.kAXValueCGPointType, None)
    return (float(point.x), float(point.y)) if ok else None

def _ax_size(value: object) -> Optional[Tuple[float, float]]:
    if not isinstance(value, Quartz.AXValueRef): return None
    ok, size = Quartz.AXValueGetValue(value, Quartz.kAXValueCGSizeType, None)
    return (float(size.width), float(size.height)) if ok else None

def _ax_short_text(value: object) -> str:
    if not isinstance(value, str): return ""
    return " ".join(value.split())[:60]

def _ax_descendant_text(element: object) -> str:
    queue = [element]
    visited = 0
    while queue and visited < AX_LABEL_SEARCH_NODES:
        node = queue.pop(0)
        visited += 1
        children = _ax_attribute(node, "AXChildren")
        for child in (list(children) if children else []):
            if _ax_attribute(child, "AXRole") == "AXStaticText":
                text = _ax_short_text(_ax_attribute(child, "AXValue"))
                if text: return text
            queue.append(child)
    return ""

def scan_ax_elements(root: object) -> Tuple[List[AXElement], List[object], bool]:
    started = time.monotonic()
    stack = [root]
    elements, refs = [], []
    visited = 0
    while stack:
        if len(elements) >= AX_ELEMENT_LIMIT or visited >= AX_NODE_LIMIT or time.monotonic() - started > AX_SCAN_BUDGET_SECONDS:
            return elements, refs, True
        node = stack.pop()
        visited += 1
        error, values = Quartz.AXUIElementCopyMultipleAttributeValues(node, _AX_SCAN_ATTRIBUTES, 0, None)
        if error != Quartz.kAXErrorSuccess: continue
        
        attrs = dict(zip(_AX_SCAN_ATTRIBUTES, values))
        children = attrs["AXChildren"]
        if children: stack.extend(reversed(list(children)))
        
        role = str(attrs["AXRole"] or "")
        if role not in _AX_ACTIONABLE_ROLES: continue
        pos = _ax_point(attrs["AXPosition"])
        size = _ax_size(attrs["AXSize"])
        if pos is None or size is None or size[0] <= 0 or size[1] <= 0: continue
        
        label = next((t for t in (_ax_short_text(attrs[k]) for k in ("AXTitle", "AXDescription", "AXPlaceholderValue")) if t), "")
        value = _ax_short_text(attrs["AXValue"]) if role in _AX_TEXT_INPUT_ROLES else ""
        if not label and not value: label = _ax_descendant_text(node)
        
        elements.append({"role": role, "label": label, "value": value, "enabled": attrs["AXEnabled"] is not False, "center_x": pos[0] + size[0] / 2, "center_y": pos[1] + size[1] / 2})
        refs.append(node)
    return elements, refs, False

def format_ax_listing(app_name: str, window_title: str, elements: List[AXElement], geometry: ScreenGeometry, truncated: bool) -> str:
    lines = [f"{app_name} · {window_title!r} · {len(elements)} öğe ({geometry['model_width']}×{geometry['model_height']})"]
    for i, el in enumerate(elements, 1):
        x, y = points_to_model(el["center_x"], el["center_y"], geometry)
        val_str = f" değer={el['value']!r}" if el['value'] else ""
        pasif_str = " (pasif)" if not el['enabled'] else ""
        lines.append(f"[{i}] {el['role'].removeprefix('AX')} {el['label']!r}{val_str}{pasif_str} @({x},{y})")
    if truncated: lines.append("…liste kısaltıldı.")
    return "\n".join(lines)

def _bundle_name(pid: int) -> str:
    running = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    if running is None or running.bundleURL() is None: return ""
    return str(running.bundleURL().lastPathComponent()).removesuffix(".app")

def _app_pid(app_name: str) -> int:
    wanted = app_name.casefold()
    windows = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements, Quartz.kCGNullWindowID)
    for w in list(windows) if windows else []:
        if str(w.get(Quartz.kCGWindowOwnerName) or "").casefold() == wanted: return int(w[Quartz.kCGWindowOwnerPID])
    for _name, pid in ( ( _bundle_name(int(w[Quartz.kCGWindowOwnerPID])), int(w[Quartz.kCGWindowOwnerPID]) ) for w in list(windows) if windows ):
        if _bundle_name(pid).casefold() == wanted: return pid
    raise ToolError(f"Uygulama bulunamadı: {app_name}", "APP_NOT_RUNNING", True)

def _visible_app_owners() -> List[Tuple[str, int]]:
    windows = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionAll | Quartz.kCGWindowListExcludeDesktopElements, Quartz.kCGNullWindowID)
    owners = []
    for window in list(windows) if windows else []:
        if int(window.get(Quartz.kCGWindowLayer) or 0) != 0: continue
        owner = (str(window.get(Quartz.kCGWindowOwnerName) or ""), int(window[Quartz.kCGWindowOwnerPID]))
        if owner not in owners: owners.append(owner)
    return owners

def _check_in_model_space(x: int, y: int, geometry: ScreenGeometry) -> bool:
    return 0 <= x < geometry["model_width"] and 0 <= y < geometry["model_height"]

class CUA:
    def __init__(self) -> None:
        self._snapshots: Dict[str, List[object]] = {}

    def get_app(self, app_name: str) -> str:
        script = f'tell application {json.dumps(app_name)} to activate'
        res = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=8)
        if res.returncode == 0: return f"{app_name} aktif edildi."
        raise ToolError(f"Uygulama başlatılamadı: {app_name}", "APP_NOT_FOUND", False)

    def _front_window(self, app_name: str) -> object:
        _require_accessibility()
        app = Quartz.AXUIElementCreateApplication(_app_pid(app_name))
        for attr in ("AXFocusedWindow", "AXMainWindow"):
            win = _ax_attribute(app, attr)
            if win: return win
        wins = _ax_attribute(app, "AXWindows")
        if wins: return list(wins)[0]
        raise ToolError(f"{app_name} için pencere bulunamadı.", "AX_NO_WINDOW", True)

    def list_elements(self, app_name: str, geometry: Optional[ScreenGeometry] = None) -> str:
        win = self._front_window(app_name)
        els, refs, truncated = scan_ax_elements(win)
        self._snapshots[app_name.casefold()] = refs
        title = _ax_short_text(_ax_attribute(win, "AXTitle"))
        return format_ax_listing(app_name, title, els, geometry or current_geometry(), truncated)

    def click_element(self, app_name: str, element_id: int) -> str:
        _require_accessibility()
        refs = self._snapshots.get(app_name.casefold())
        if not refs or not 1 <= element_id <= len(refs): raise ToolError("Geçersiz öğe.", "INVALID_ELEMENT", True)
        el = refs[element_id - 1]
        role = _ax_attribute(el, "AXRole")
        if role in _AX_TEXT_INPUT_ROLES:
            Quartz.AXUIElementSetAttributeValue(el, "AXFocused", True)
            return f"[{element_id}] odaklandı."
        if Quartz.AXUIElementPerformAction(el, "AXPress") == Quartz.kAXErrorSuccess:
            return f"[{element_id}] tıklandı."
        pos = _ax_point(_ax_attribute(el, "AXPosition"))
        size = _ax_size(_ax_attribute(el, "AXSize"))
        if pos and size:
            pyautogui.click(round(pos[0] + size[0] / 2), round(pos[1] + size[1] / 2))
        return f"[{element_id}] fare ile tıklandı."

    def window_bounds(self, app_name: str) -> Tuple[float, float, float, float]:
        win = self._front_window(app_name)
        p = _ax_point(_ax_attribute(win, "AXPosition"))
        s = _ax_size(_ax_attribute(win, "AXSize"))
        return (p[0], p[1], s[0], s[1])

def unicode_chunks(text: str, max_units: int) -> List[str]:
    chunks, current, units = [], "", 0
    for c in text:
        u = 2 if ord(c) > 0xFFFF else 1
        if current and units + u > max_units:
            chunks.append(current); current, units = "", 0
        current += c; units += u
    if current: chunks.append(current)
    return chunks

def _post_unicode_chunk(chunk: str) -> None:
    units = len(chunk.encode("utf-16-le")) // 2
    event = Quartz.CGEventCreateKeyboardEvent(None, 0, True)
    Quartz.CGEventKeyboardSetUnicodeString(event, units, chunk)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
    event = Quartz.CGEventCreateKeyboardEvent(None, 0, False)
    Quartz.CGEventKeyboardSetUnicodeString(event, units, chunk)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)

def type_unicode_text(text: str) -> None:
    for i, line in enumerate(text.replace("\r\n", "\n").split("\n")):
        if i > 0: pyautogui.press("enter")
        for chunk in unicode_chunks(line, UNICODE_CHUNK_UNITS):
            _post_unicode_chunk(chunk)
            time.sleep(UNICODE_CHUNK_DELAY_SECONDS)

def press_key_spec(spec: str) -> str:
    if len(spec) == 1:
        type_unicode_text(spec)
        return f"Yazıldı: {spec}"
    names = [_KEY_ALIASES.get(part.strip().lower(), part.strip().lower()) for part in spec.split("+")]
    pyautogui.hotkey(*names)
    return f"Basıldı: {spec}"

def _run_action_step(step: ActionStep, geometry: ScreenGeometry) -> str:
    act = step.get("action")
    if act == "click":
        x, y = parse_point(step["point"])
        return click_model_point(x, y, str(step.get("button") or "left"), geometry)
    if act == "move":
        x, y = parse_point(step["point"])
        return move_model_point(x, y, geometry)
    if act == "type":
        text = str(step["text"])
        type_unicode_text(text)
        return f"Yazıldı: {_clip(text, TYPED_TEXT_ECHO_LIMIT)}"
    if act == "press":
        return press_key_spec(str(step["key"]))
    if act == "wait":
        time.sleep(float(step["seconds"]))
        return f"{step['seconds']}sn beklendi."
    raise ToolError(f"Geçersiz eylem: {act}", "INVALID_ACTION", False)
