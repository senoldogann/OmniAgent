"""Ekran gözleminin görsel ayrıntı ve tıklama koordinatlarını ayrı tuttuğunu sınar."""

import base64
import json
import random
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest
from PIL import Image, ImageDraw

from omniagent import tools
from omniagent.app import agent as main
from omniagent.app.agent import _screenshot_observation_with_digest
from omniagent.core.events import AgentEvent
from omniagent.integrations.capabilities import CapabilityService
from omniagent.paths import workspace_dir
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
    message, digest = await _screenshot_observation_with_digest(call, allow_source_relative=False)
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

    message, _digest = await _screenshot_observation_with_digest(call, allow_source_relative=False)

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

    message, _digest = await _screenshot_observation_with_digest(call, allow_source_relative=False)

    assert len(message["content"]) == 2
    assert _image_from_content(message["content"][1]).size == (1000, 1000)


SCREEN_GEOMETRY: tools.ScreenGeometry = {
    "point_width": 1600, "point_height": 900, "model_width": 1000, "model_height": 1000,
}


@pytest.fixture
def process_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """
    Görevin süreç dizini (kaynak deposu rolünde). Veri kökü ve kontrol noktaları da geçici
    dizinde tutulur: gerçek döngü koşan test kullanıcının veri klasörüne yazmaz.
    """
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr("omniagent.core.checkpoint.RUNS_DIR", tmp_path / "runs")
    directory: Path = tmp_path / "proje"
    directory.mkdir()
    monkeypatch.chdir(directory)
    return directory


async def _run_screenshot_turn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, goal: str,
) -> Tuple[List[Dict[str, Any]], List[AgentEvent]]:
    """
    Gerçek ajan döngüsünü betikli modelle koşturur: model ekran.png için take_screenshot çağırıp
    bitirir. Modelin ikinci çağrıda araç sonuçlarından sonra gördüğü mesajları ve döngünün
    yayınladığı olayları döner.
    """
    model_calls: List[List[Dict[str, Any]]] = []

    async def scripted_model(
        clients: Any, messages: List[Dict[str, Any]], schemas: Any, session_id: str,
        backend: str, emit: Any, should_stop: Any,
    ) -> Tuple[Dict[str, Any], str]:
        model_calls.append(list(messages))
        if len(model_calls) == 1:
            call = {"id": "shot", "name": "take_screenshot", "arguments": json.dumps({"filename": "ekran.png"})}
            return {"content": "", "tool_calls": [call], "finish_reason": "tool_calls",
                    "usage": main.ZERO_USAGE}, backend
        return {"content": "TAMAM", "tool_calls": [], "finish_reason": "stop", "usage": main.ZERO_USAGE}, backend

    monkeypatch.setattr(main, "_call_model_with_retries", scripted_model)
    events: List[AgentEvent] = []
    service = CapabilityService(tmp_path)
    try:
        await main.run_agent_with_callback(
            goal, events.append,
            {"requested_backend": "opencode", "should_stop": lambda: False,
             "state_file": str(tmp_path / "memory.json"), "history": [], "integrations": service},
            {"opencode": object()},
        )
    finally:
        await service.close()
    assert len(model_calls) >= 2, "Araç turundan sonra model ikinci kez çağrılmadı."
    second_call: List[Dict[str, Any]] = model_calls[1]
    last_tool_index: int = max(index for index, message in enumerate(second_call) if message["role"] == "tool")
    return second_call[last_tool_index + 1:], events


@pytest.mark.asyncio
@pytest.mark.parametrize(("goal", "written_in_workspace"), [
    ("Ekran görüntüsü al", True),
    ("Projedeki kodu düzelt ve ekran görüntüsü al", False),
])
async def test_run_reads_screenshot_where_the_tool_wrote_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, process_dir: Path, goal: str, written_in_workspace: bool,
) -> None:
    """
    Gerçek ajan döngüsü ve gerçek Toolbox.take_screenshot: araç göreli adı kaynak görevi değilse
    workspace'e, kaynak görevinde süreç dizinine yazar; modele giden görüntü ve sohbet kartı aynı
    dosyadan gelmeli. Eskiden ikisi de süreç dizininde arıyordu: ek FileNotFoundError ile
    düşüyor, kart sessizce siliniyordu. Yalnız ekran yakalama katmanı sentetik kare verir.
    """
    monkeypatch.setattr(tools, "current_geometry", lambda: SCREEN_GEOMETRY)
    monkeypatch.setattr(
        tools, "grab_model_frame", lambda geometry, display_id=None: Image.new("RGB", (1600, 900), "white"),
    )
    expected: Path = (workspace_dir() if written_in_workspace else process_dir) / "ekran.png"

    after_tools, events = await _run_screenshot_turn(monkeypatch, tmp_path, goal)

    assert expected.is_file()
    attached = [
        message for message in after_tools
        if isinstance(message["content"], list) and any(part["type"] == "image_url" for part in message["content"])
    ]
    assert len(attached) == 1
    cards = [event for event in events if event["kind"] == "artifact_ready"]
    assert [Path(card["path"]) for card in cards] == [expected.resolve()]


@pytest.mark.asyncio
async def test_run_tells_model_when_screenshot_file_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, process_dir: Path,
) -> None:
    """
    Araç sonucu "modele iletilir" dese de dosya yoksa model görüntüyü görmediğini öğrenir ve
    arayüz uyarı alır; aksi hâlde model görmediği ekranı tarif eder.
    """
    monkeypatch.setattr(Toolbox, "take_screenshot", lambda self, filename: "kaydedildi")

    after_tools, events = await _run_screenshot_turn(monkeypatch, tmp_path, "Ekran görüntüsü al")

    failures = [
        message for message in after_tools
        if message["role"] == "user" and "Ekran görüntüsü modele eklenemedi" in str(message["content"])
    ]
    assert len(failures) == 1
    assert "FileNotFoundError" in failures[0]["content"]
    assert "tahmin yürütme" in failures[0]["content"]
    warnings = [event["text"] for event in events if event["kind"] == "notice" and event["level"] == "warning"]
    assert any("iliştirilemedi" in text for text in warnings)


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


@pytest.mark.asyncio
async def test_automatic_observation_uses_lossless_bmp_intermediate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Otomatik gözlemin geçici dosyası sıkıştırmasız BMP'dir (PNG kodlama/çözme süresi yok); kayıpsız olduğundan modele giden
    görüntü ve tekrar-tespit digest'i PNG ile aynı karede birebir aynıdır, geçici dosya işi bitince silinir.
    """
    pixels = random.Random(11).randbytes(320 * 200 * 3)
    frame = Image.frombytes("RGB", (320, 200), pixels)
    ImageDraw.Draw(frame).rectangle((10, 10, 150, 90), fill="white")
    written: List[str] = []

    def fake_screenshot(self: Toolbox, filename: str, detail: bool = True) -> str:
        written.append(filename)
        frame.save(filename)
        return "kaydedildi"

    monkeypatch.setattr(Toolbox, "take_screenshot", fake_screenshot)
    observation, _step, digest = await main._observe_after_actions(
        "auto-1", 0, "önizleme", Toolbox(), {}, lambda event: None, lambda: False,
    )

    assert [Path(name).suffix for name in written] == [".bmp"]
    assert not Path(written[0]).exists()
    reference = tmp_path / "reference.png"
    frame.save(reference)
    expected, expected_digest = await _screenshot_observation_with_digest(
        {"id": "ref", "name": "take_screenshot", "arguments": json.dumps({"filename": str(reference), "detail": False})},
        allow_source_relative=False,
    )
    assert digest == expected_digest
    assert observation["content"][1] == expected["content"][1]
