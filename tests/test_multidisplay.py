"""Çoklu ekran görüntüsü ile koordinatların aynı global uzayda kalmasını sınar."""
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image

import main
import tools
from tools import ToolError, Toolbox, model_to_points


def _bounds(x: int, y: int, width: int, height: int) -> Any:
    return SimpleNamespace(
        origin=SimpleNamespace(x=x, y=y),
        size=SimpleNamespace(width=width, height=height),
    )


def _fake_quartz(monkeypatch: pytest.MonkeyPatch) -> None:
    bounds = {1: _bounds(0, 0, 1710, 1112), 2: _bounds(0, -1080, 1920, 1080)}
    fake = SimpleNamespace(
        kCGErrorSuccess=0,
        CGGetActiveDisplayList=lambda maximum, ids, count: (0, (2, 1), 2),
        CGMainDisplayID=lambda: 1,
        CGDisplayBounds=lambda display_id: bounds[display_id],
    )
    monkeypatch.setattr(tools, "Quartz", fake)


def test_second_display_geometry_includes_negative_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_quartz(monkeypatch)
    assert tools.active_display_ids() == [1, 2]
    display_id, geometry = tools.geometry_for_display_index(2)
    assert display_id == 2
    assert (geometry["point_width"], geometry["point_height"]) == (1920, 1080)
    assert (geometry["origin_x"], geometry["origin_y"]) == (0, -1080)
    assert model_to_points(500, 500, geometry) == (960, -540)
    with pytest.raises(ToolError) as missing:
        tools.geometry_for_display_index(3)
    assert missing.value.code == "INVALID_DISPLAY"


def test_screenshot_and_actions_keep_selected_display(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_quartz(monkeypatch)
    monkeypatch.setattr(tools, "screen_capture_granted", lambda request=False: False)
    monkeypatch.setattr(tools, "_require_accessibility", lambda: None)
    captured: list[tuple[int | None, dict[str, int]]] = []
    clicked: list[dict[str, int]] = []

    def fake_grab(geometry: tools.ScreenGeometry, display_id: int | None = None) -> Image.Image:
        captured.append((display_id, dict(geometry)))
        return Image.new("RGB", (1000, 1000), "white")

    monkeypatch.setattr(tools, "grab_model_frame", fake_grab)
    monkeypatch.setattr(
        tools, "click_model_point",
        lambda x, y, button, geometry: clicked.append(dict(geometry)) or "tıklandı",
    )
    box = Toolbox()
    first = box.take_screenshot(str(tmp_path / "second.png"), 2)
    assert "Ekran 2" in first
    assert captured[-1][0] == 2
    box.cua_click_point([500, 500])
    box.run_action_sequence([{"action": "click", "point": [100, 100]}])
    assert all(item["origin_y"] == -1080 for item in clicked)

    box.take_screenshot(str(tmp_path / "second-again.png"))
    assert captured[-1][0] == 2
    box.take_screenshot(str(tmp_path / "main.png"), 1)
    assert captured[-1][0] == 1
    assert captured[-1][1]["origin_y"] == 0

    shot_schema = next(
        item["function"]["parameters"]
        for item in main.build_tool_schemas()
        if item["function"]["name"] == "take_screenshot"
    )
    assert shot_schema["required"] == ["filename"]
    assert "display_index" in shot_schema["properties"]


def test_display_rearrangement_requires_fresh_screenshot(monkeypatch: pytest.MonkeyPatch) -> None:
    box = Toolbox()
    box._visual_display_id = 2
    box._visual_geometry = {
        "point_width": 1920, "point_height": 1080,
        "model_width": 1000, "model_height": 1000,
        "origin_x": 0, "origin_y": -1080,
    }
    monkeypatch.setattr(
        tools, "geometry_for_display_id",
        lambda display_id: {**box._visual_geometry, "origin_y": 1112},
    )
    with pytest.raises(ToolError) as changed:
        box._input_geometry()
    assert changed.value.code == "DISPLAY_GEOMETRY_CHANGED"
