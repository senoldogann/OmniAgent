"""Çift tıklama ve sürükle-bırak: eylem yönlendirmesi ve macOS'a giden Quartz olay dizisi."""
from types import SimpleNamespace
from typing import Any, List, Tuple

import pytest

from omniagent import tools
from omniagent.tools import ToolError, Toolbox, screen

GEOMETRY: tools.ScreenGeometry = {"point_width": 2000, "point_height": 1000, "model_width": 1000, "model_height": 1000}


def _toolbox(monkeypatch: pytest.MonkeyPatch) -> Toolbox:
    monkeypatch.setattr(tools, "_require_accessibility", lambda: None)
    monkeypatch.setattr(tools, "screen_capture_granted", lambda request=False: False)
    monkeypatch.setattr(tools, "current_geometry", lambda: GEOMETRY)
    return Toolbox()


def test_click_routes_single_double_and_drag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tek tıklama eski yolu korur; clicks=2 çoklu tıklamaya, drag sürüklemeye gider."""
    calls: List[Tuple[Any, ...]] = []
    monkeypatch.setattr(tools, "click_model_point", lambda x, y, button, geometry: calls.append(("click", x, y, button)) or "tek")
    monkeypatch.setattr(tools, "multi_click_model_point",
                        lambda x, y, button, clicks, geometry: calls.append(("multi", x, y, button, clicks)) or "çift")
    monkeypatch.setattr(tools, "drag_model_points",
                        lambda start, end, button, geometry: calls.append(("drag", start, end, button)) or "sürüklendi")
    box = _toolbox(monkeypatch)
    result = box.run_action_sequence([
        {"action": "click", "point": [10, 20]},
        {"action": "click", "point": [30, 40], "clicks": 2},
        {"action": "drag", "point": [50, 60], "to": [700, 800]},
    ])
    assert calls == [
        ("click", 10, 20, "left"),
        ("multi", 30, 40, "left", 2),
        ("drag", (50, 60), (700, 800), "left"),
    ]
    assert "sürüklendi" in result


@pytest.mark.parametrize("step", [
    {"action": "click", "point": [1, 2], "clicks": 5},
    {"action": "drag", "point": [1, 2]},
    {"action": "drag", "point": [1, 2], "to": "3,4"},
])
def test_bad_mouse_steps_fail_explicitly(monkeypatch: pytest.MonkeyPatch, step: dict) -> None:
    """Geçersiz tıklama sayısı veya eksik/bozuk hedef nokta sessizce çalışmaz."""
    monkeypatch.setattr(tools, "multi_click_model_point", lambda *args: pytest.fail("çalışmamalı"))
    monkeypatch.setattr(tools, "drag_model_points", lambda *args: pytest.fail("çalışmamalı"))
    with pytest.raises(ToolError) as error:
        _toolbox(monkeypatch).run_action_sequence([step])
    assert error.value.code in ("INVALID_ACTION_PARAMS", "INVALID_POINT")


def _fake_quartz(monkeypatch: pytest.MonkeyPatch) -> List[Tuple[str, Any, Any, int]]:
    """Quartz olaylarını (tür, konum, düğme, tıklama durumu) olarak kaydeder."""
    posted: List[Tuple[str, Any, Any, int]] = []

    def create(source: object, kind: str, point: Tuple[float, float], button: str) -> dict:
        return {"kind": kind, "point": point, "button": button, "state": 0}

    def set_field(event: dict, field: str, value: int) -> None:
        assert field == "clickState"
        event["state"] = value

    fake = SimpleNamespace(
        kCGEventLeftMouseDown="down", kCGEventLeftMouseUp="up", kCGEventLeftMouseDragged="dragged",
        kCGMouseButtonLeft="L", kCGEventRightMouseDown="rdown", kCGEventRightMouseUp="rup",
        kCGEventRightMouseDragged="rdragged", kCGMouseButtonRight="R",
        kCGEventOtherMouseDown="odown", kCGEventOtherMouseUp="oup", kCGEventOtherMouseDragged="odragged",
        kCGMouseButtonCenter="C", kCGMouseEventClickState="clickState", kCGHIDEventTap="tap",
        CGEventCreateMouseEvent=create, CGEventSetIntegerValueField=set_field,
        CGEventPost=lambda tap, event: posted.append((event["kind"], event["point"], event["button"], event["state"])),
    )
    monkeypatch.setattr(screen, "Quartz", fake)
    monkeypatch.setattr(screen.pyautogui, "moveTo", lambda x, y: None)
    monkeypatch.setattr(screen.time, "sleep", lambda seconds: None)
    return posted


def test_double_click_sets_increasing_click_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """macOS çift tıklamayı yalnız tıklama durumu 2 olan ikinci basma/bırakmadan anlar."""
    posted = _fake_quartz(monkeypatch)
    text = screen.multi_click_model_point(100, 200, "left", 2, GEOMETRY)
    assert [(kind, state) for kind, _, _, state in posted] == [("down", 1), ("up", 1), ("down", 2), ("up", 2)]
    assert all(point == (200, 200) and button == "L" for _, point, button, _ in posted)
    assert text.startswith("Çift tıklandı")


def test_drag_holds_moves_through_intermediate_points_and_releases(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sürükleme: başlangıçta bas, ara sürükleme olaylarıyla git, hedefte bırak."""
    posted = _fake_quartz(monkeypatch)
    screen.drag_model_points((100, 100), (600, 900), "left", GEOMETRY)
    kinds = [kind for kind, _, _, _ in posted]
    assert kinds[0] == "down" and kinds[-1] == "up"
    assert kinds[1:-1] == ["dragged"] * screen.MOUSE_DRAG_STEPS
    assert posted[0][1] == (200, 100)
    assert posted[-2][1] == (1200, 900)
    assert posted[-1][1] == (1200, 900)


def test_unknown_mouse_button_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_quartz(monkeypatch)
    with pytest.raises(ToolError) as error:
        screen.multi_click_model_point(1, 1, "thumb", 2, GEOMETRY)
    assert error.value.code == "INVALID_BUTTON"
