"""
src-layout refactor'ünde kaybolan GUI davranışlarının regresyon testleri: AX işlevleri
ApplicationServices'ten gelir, izin/tuş/koordinat hataları sessiz kalmaz, AX taraması satırları
ve bağlantıları listeler, şablon tıklaması kaydedilen görüntünün piksel uzayını çevirir.
"""
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pytest
from PIL import Image

from omniagent import tools
from omniagent.tools import ToolError, Toolbox, gui_input, screen
from omniagent.tools.types import TOOL_RUNTIME

GEOMETRY: tools.ScreenGeometry = {"point_width": 1000, "point_height": 500, "model_width": 1000, "model_height": 1000}


# --- Erişilebilirlik izni ve AX modülü ---

@pytest.mark.skipif(sys.platform != "darwin", reason="Gerçek pyobjc yalnız macOS'ta")
def test_ax_functions_resolve_from_the_real_module() -> None:
    """Quartz AX işlevlerini içermez; hepsi gui_input.AX (ApplicationServices) üzerinden çözülmeli."""
    for name in (
        "AXIsProcessTrusted", "AXUIElementCreateApplication", "AXUIElementCopyAttributeValue",
        "AXUIElementCopyMultipleAttributeValues", "AXUIElementSetMessagingTimeout",
        "AXUIElementSetAttributeValue", "AXUIElementPerformAction", "AXValueGetValue", "AXValueGetType",
        "AXValueRef", "kAXErrorSuccess", "kAXErrorCannotComplete", "kAXErrorInvalidUIElement",
        "kAXValueAXErrorType", "kAXValueCGPointType", "kAXValueCGSizeType",
    ):
        assert hasattr(gui_input.AX, name), name


def test_missing_accessibility_permission_is_an_explicit_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """İzin yokken macOS olayları sessizce düşürür; araç açık hata ve ayar yolunu vermeli."""
    monkeypatch.setattr(gui_input, "AX", SimpleNamespace(AXIsProcessTrusted=lambda: False))
    with pytest.raises(ToolError) as error:
        gui_input._require_accessibility()
    assert error.value.code == "AX_PERMISSION"
    assert "Privacy_Accessibility" in str(error.value)
    assert sys.executable in str(error.value)
    monkeypatch.setattr(gui_input, "AX", SimpleNamespace(AXIsProcessTrusted=lambda: True))
    gui_input._require_accessibility()


# --- Klavye ---

class FakeKeyboard:
    """pyautogui'nin macOS tuş tablosunu ve basışları taklit eder."""

    def __init__(self) -> None:
        self.platformModule = SimpleNamespace(keyboardMapping={
            "command": 55, "ctrl": 59, "shift": 56, "option": 58, "enter": 36, "up": 126,
            "escape": 53, "delete": 51, "c": 8, "t": 17, "meta": None,
        })
        self.calls: List[Tuple[str, ...]] = []

    def press(self, key: str) -> None:
        self.calls.append(("press", key))

    def hotkey(self, *keys: str) -> None:
        self.calls.append(("hotkey", *keys))


def test_shortcuts_use_pyautogui_names_not_playwright_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """'cmd' → 'command' olmalı: 'Meta' pyautogui'de yok ve cmd+c yalnız 'c' yazıyordu."""
    keyboard = FakeKeyboard()
    monkeypatch.setattr(gui_input, "pyautogui", keyboard)
    assert gui_input.key_names("Cmd+Shift+T") == ["command", "shift", "t"]
    assert gui_input.key_names("ctrl+ArrowUp") == ["ctrl", "up"]
    gui_input.press_key_spec("cmd+c")
    gui_input.press_key_spec("enter")
    gui_input.press_key_spec("meta+c")
    assert keyboard.calls == [("hotkey", "command", "c"), ("press", "enter"), ("hotkey", "command", "c")]


def test_unknown_key_fails_instead_of_silently_pressing_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    keyboard = FakeKeyboard()
    monkeypatch.setattr(gui_input, "pyautogui", keyboard)
    with pytest.raises(ToolError) as error:
        gui_input.press_key_spec("hyper+x")
    assert error.value.code == "INVALID_KEY"
    assert keyboard.calls == []


def test_typed_text_clears_modifier_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """Takılı kalmış cmd yazımı kısayola çevirmesin: her olayın bayrakları sıfırlanır."""
    posted: List[Dict[str, Any]] = []
    fake = SimpleNamespace(
        kCGHIDEventTap="tap",
        CGEventCreateKeyboardEvent=lambda source, code, down: {"down": down, "flags": None, "text": None},
        CGEventSetFlags=lambda event, flags: event.update(flags=flags),
        CGEventKeyboardSetUnicodeString=lambda event, units, text: event.update(text=text),
        CGEventPost=lambda tap, event: posted.append(dict(event)),
    )
    monkeypatch.setattr(gui_input, "Quartz", fake)
    gui_input._post_unicode_chunk("ğü")
    assert posted == [{"down": True, "flags": 0, "text": "ğü"}, {"down": False, "flags": 0, "text": "ğü"}]


# --- Fare ---

def test_out_of_range_points_and_bad_buttons_never_click(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ekran dışı nokta pyautogui FAILSAFE'ini tetikler veya yanlış yere tıklar: önce hata verilir."""
    clicks: List[Any] = []
    monkeypatch.setattr(screen.pyautogui, "click", lambda *args, **kwargs: clicks.append(args))
    monkeypatch.setattr(screen.pyautogui, "moveTo", lambda *args, **kwargs: clicks.append(args))
    for call in (
        lambda: screen.click_model_point(1000, 10, "left", GEOMETRY),
        lambda: screen.move_model_point(-1, 10, GEOMETRY),
        lambda: screen.multi_click_model_point(10, 1000, "left", 2, GEOMETRY),
        lambda: screen.drag_model_points((10, 10), (10, 1200), "left", GEOMETRY),
    ):
        with pytest.raises(ToolError) as error:
            call()
        assert error.value.code == "COORDINATE_OUT_OF_RANGE"
    with pytest.raises(ToolError) as error:
        screen.click_model_point(10, 10, "thumb", GEOMETRY)
    assert error.value.code == "INVALID_BUTTON"
    assert clicks == []
    assert screen.click_model_point(999, 999, "right", GEOMETRY) == "(999, 999) konumuna right tıklandı."


def test_wait_step_is_bounded_and_stoppable() -> None:
    with pytest.raises(ToolError) as error:
        gui_input._run_action_step({"action": "wait", "seconds": 600}, GEOMETRY)
    assert error.value.code == "INVALID_WAIT"
    token = TOOL_RUNTIME.set({"emit_output": lambda text: None, "should_stop": lambda: True, "approved": False})
    started = time.monotonic()
    try:
        with pytest.raises(ToolError) as stopped:
            gui_input._run_action_step({"action": "wait", "seconds": 5}, GEOMETRY)
    finally:
        TOOL_RUNTIME.reset(token)
    assert stopped.value.code == "STOPPED"
    assert time.monotonic() - started < 1


# --- AX ağacı ---

class ValueRef:
    """AXValueRef taklidi: tür + yük."""

    def __init__(self, kind: str, payload: object = None) -> None:
        self.kind = kind
        self.payload = payload


class Node:
    def __init__(self, **attrs: object) -> None:
        self.attrs = attrs


def _fake_ax(copy_error: Optional[int] = None, trusted: bool = True) -> SimpleNamespace:
    missing = ValueRef("error")

    def copy_multiple(node: Node, names: List[str], options: int, _: None) -> Tuple[int, List[object]]:
        if copy_error is not None:
            return copy_error, []
        return 0, [node.attrs.get(name, missing) for name in names]

    def copy_one(node: Node, name: str, _: None) -> Tuple[int, object]:
        return (0, node.attrs[name]) if name in node.attrs else (-25205, None)

    return SimpleNamespace(
        AXIsProcessTrusted=lambda: trusted, AXValueRef=ValueRef,
        kAXValueAXErrorType="error", kAXValueCGPointType="point", kAXValueCGSizeType="size",
        AXValueGetType=lambda value: value.kind,
        AXValueGetValue=lambda value, kind, _: (value.kind == kind, value.payload),
        kAXErrorSuccess=0, kAXErrorCannotComplete=-25204, kAXErrorInvalidUIElement=-25202,
        AXUIElementCopyMultipleAttributeValues=copy_multiple, AXUIElementCopyAttributeValue=copy_one,
        AXUIElementSetAttributeValue=lambda element, name, value: -25202,
        AXUIElementPerformAction=lambda element, action: -25206,
    )


def _point(x: float, y: float) -> ValueRef:
    return ValueRef("point", SimpleNamespace(x=x, y=y))


def _size(width: float, height: float) -> ValueRef:
    return ValueRef("size", SimpleNamespace(width=width, height=height))


def test_scan_lists_rows_and_links_with_descendant_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mail/Notlar satırları (AXRow) ve bağlantılar (AXLink) listede olmalı; etiketsiz satırın metni alt öğeden gelir."""
    monkeypatch.setattr(gui_input, "AX", _fake_ax())
    text = Node(AXRole="AXStaticText", AXValue="Ekim faturası")
    row = Node(AXRole="AXRow", AXPosition=_point(0, 100), AXSize=_size(400, 20), AXChildren=[text])
    link = Node(AXRole="AXLink", AXTitle="Ayrıntılar", AXPosition=_point(10, 10), AXSize=_size(80, 20))
    window = Node(AXRole="AXWindow", AXChildren=[row, link])
    elements, refs, truncated = gui_input.scan_ax_elements(window)
    assert [(item["role"], item["label"]) for item in elements] == [("AXRow", "Ekim faturası"), ("AXLink", "Ayrıntılar")]
    assert refs == [row, link] and not truncated
    assert elements[0]["enabled"] is True  # eksik AXEnabled "pasif" sayılmaz


def test_unresponsive_app_is_reported_not_silently_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gui_input, "AX", _fake_ax(copy_error=-25204))
    with pytest.raises(ToolError) as error:
        gui_input.scan_ax_elements(Node(AXRole="AXWindow"))
    assert error.value.code == "AX_TIMEOUT"
    monkeypatch.setattr(gui_input, "AX", _fake_ax(copy_error=-25201))
    with pytest.raises(ToolError) as failed:
        gui_input.scan_ax_elements(Node(AXRole="AXWindow"))
    assert failed.value.code == "AX_READ_FAILED"


def test_click_element_never_reports_a_click_it_did_not_make(monkeypatch: pytest.MonkeyPatch) -> None:
    """AXPress yoksa ve konum okunamazsa 'tıklandı' denmez; bayat öğe açık hata verir."""
    monkeypatch.setattr(gui_input, "AX", _fake_ax())
    clicked: List[Any] = []
    monkeypatch.setattr(gui_input.pyautogui, "click", lambda *args: clicked.append(args))
    cua = gui_input.CUA()
    with pytest.raises(ToolError) as error:
        cua.click_element("Notes", 1)
    assert error.value.code == "AX_NO_SNAPSHOT"
    cua._snapshots["notes"] = [Node(AXRole="AXButton")]
    with pytest.raises(ToolError) as stale:
        cua.click_element("Notes", 1)
    assert stale.value.code == "AX_STALE"
    assert clicked == []
    cua._snapshots["notes"] = [Node(AXRole="AXButton", AXPosition=_point(10, 20), AXSize=_size(40, 10))]
    assert "fare ile tıklandı" in cua.click_element("Notes", 1)
    assert clicked == [(30, 25)]


# --- Şablon tıklaması ---

def test_template_click_maps_saved_screenshot_pixels_to_model_space(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """
    Kaydedilen ekran görüntüsü gerçek en-boy oranındadır (burada 2000×1000); şablon ondan kırpılır.
    Eşleşme merkezi (1500, 250) piksel → ortak uzayda (750, 250) olmalı.
    """
    pixels = np.random.default_rng(7).integers(0, 255, (1000, 2000), dtype=np.uint8)
    Image.fromarray(pixels[230:270, 1480:1520]).save(tmp_path / "hedef.png")
    monkeypatch.setattr(tools, "_require_accessibility", lambda: None)
    monkeypatch.setattr(tools, "screen_capture_granted", lambda request=False: False)
    monkeypatch.setattr(tools, "current_geometry", lambda: GEOMETRY)
    monkeypatch.setattr(tools, "grab_model_frame", lambda geometry, display_id=None: Image.fromarray(pixels).convert("RGB"))
    clicked: List[Tuple[int, int]] = []
    monkeypatch.setattr(tools, "click_model_point", lambda x, y, button, geometry: clicked.append((x, y)) or "tıklandı")
    box = Toolbox()
    monkeypatch.setattr(box.cua, "window_bounds", lambda app_name: (0.0, 0.0, 1000.0, 500.0))
    assert "şablon güveni" in box._click_template("Finder", str(tmp_path / "hedef.png"), 0.9)
    assert clicked == [(750, 250)]


def test_screenshot_rejects_display_index_inside_window_scope(tmp_path: Path) -> None:
    box = Toolbox()
    box._screen_scope_app = "Google Chrome"
    with pytest.raises(ToolError) as error:
        box.take_screenshot(str(tmp_path / "x.png"), 2)
    assert error.value.code == "DISPLAY_SCOPE_CONFLICT"


def test_empty_ax_tree_points_model_to_text_click() -> None:
    """Electron penceresi yalnız etiketsiz pencere düğmelerini verir; model OCR ile tıklamaya yönlendirilir."""
    buttons: List[Dict[str, Any]] = [
        {"role": "AXButton", "label": "", "value": "", "enabled": True, "center_x": x, "center_y": 20.0}
        for x in (14.0, 34.0, 54.0)
    ]
    listing = gui_input.format_ax_listing("Claude", "Claude", buttons, GEOMETRY, False)
    assert gui_input.AX_EMPTY_HINT in listing
    labeled: List[Dict[str, Any]] = [{**buttons[0], "label": "Ayarlar"}]
    assert gui_input.AX_EMPTY_HINT not in gui_input.format_ax_listing("Notlar", "Notlar", labeled, GEOMETRY, False)
