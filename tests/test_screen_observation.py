"""Ekran gözleminin görsel ayrıntı ve tıklama koordinatlarını ayrı tuttuğunu sınar."""

import base64
import json
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from omniagent.app.agent import _screenshot_observation_with_digest
from omniagent.tools import Toolbox


def _image_from_content(item: dict) -> Image.Image:
    encoded = item["image_url"]["url"].split(",", 1)[1]
    return Image.open(BytesIO(base64.b64decode(encoded)))


@pytest.mark.asyncio
async def test_screen_observation_keeps_coordinate_map_and_readable_aspect(tmp_path: Path) -> None:
    source = Image.new("RGB", (1600, 900), "white")
    ImageDraw.Draw(source).rectangle((0, 0, 799, 899), fill="red")
    target = tmp_path / "screen.png"
    source.save(target)

    call = {"id": "shot", "name": "take_screenshot", "arguments": json.dumps({"filename": str(target)})}
    message, digest = await _screenshot_observation_with_digest(call)
    content = message["content"]

    assert message["role"] == "user"
    assert len(content) == 3
    assert _image_from_content(content[1]).size == (1000, 1000)
    assert _image_from_content(content[2]).size == (1600, 900)
    assert "1600×900" in content[0]["text"]
    assert "tıklama koordinat haritasıdır" in content[0]["text"]
    assert len(digest) == 64


@pytest.mark.asyncio
async def test_square_screen_observation_avoids_duplicate_image(tmp_path: Path) -> None:
    target = tmp_path / "square.png"
    Image.new("RGB", (800, 800), "white").save(target)
    call = {"id": "shot", "name": "take_screenshot", "arguments": json.dumps({"filename": str(target)})}

    message, _digest = await _screenshot_observation_with_digest(call)

    assert len(message["content"]) == 2
    assert _image_from_content(message["content"][1]).size == (1000, 1000)


@pytest.mark.asyncio
async def test_fast_observation_uses_only_coordinate_image(tmp_path: Path) -> None:
    target = tmp_path / "wide.png"
    Image.new("RGB", (1600, 900), "white").save(target)
    call = {
        "id": "shot",
        "name": "take_screenshot",
        "arguments": json.dumps({"filename": str(target), "detail": False}),
    }

    message, _digest = await _screenshot_observation_with_digest(call)

    assert len(message["content"]) == 2
    assert _image_from_content(message["content"][1]).size == (1000, 1000)


def test_visible_text_tool_returns_model_space_positions(monkeypatch: pytest.MonkeyPatch) -> None:
    box = Toolbox()
    lines = [{
        "text": "JAHREİN ANTALYA ZİYARETİ",
        "confidence": 0.98,
        "box": {"left": 100, "top": 200, "width": 300, "height": 40},
        "words": [],
    }]
    monkeypatch.setattr(box, "_screen_text", lambda: (lines, {}))

    result = box.cua_read_visible_text()

    assert "JAHREİN ANTALYA ZİYARETİ" in result
    assert "@(250, 220)" in result
