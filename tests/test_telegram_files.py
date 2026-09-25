"""Telegram dosya alışverişi: gelen ekler göreve (görseller modele), send_file dosyayı sohbete taşır."""
import asyncio
import base64
import io
import json
import stat
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import pytest
from PIL import Image

from omniagent import tools
from omniagent.app import agent as main
from omniagent.app.tool_schema import TOOL_NAMES, route_tool_schemas
from omniagent.core.conversation import make_exchange
from omniagent.integrations import telegram
from omniagent.integrations.runtime import CURRENT_RUNTIME, DeliveryFailed, IntegrationRuntime
from omniagent.tools import ToolError, Toolbox

SETTINGS: telegram.TelegramSettings = {"chat_id": 123, "user_id": 456}


def _message(**fields: Any) -> Dict[str, Any]:
    return {"message": {"chat": {"id": 123, "type": "private"}, "from": {"id": 456}, **fields}}


def _png(path: Path, size: tuple[int, int]) -> Path:
    Image.new("RGB", size, "red").save(path)
    return path


def test_message_attachment_picks_largest_photo_and_classifies_files() -> None:
    photo = {"photo": [
        {"file_id": "small", "width": 90, "height": 90, "file_size": 1000},
        {"file_id": "large", "width": 1280, "height": 960, "file_size": 90000},
    ]}
    assert telegram.message_attachment(photo) == {
        "file_id": "large", "kind": "fotoğraf", "name": None, "size": 90000, "image": True,
    }
    document = telegram.message_attachment({"document": {
        "file_id": "d", "file_name": "rapor.pdf", "mime_type": "application/pdf", "file_size": 5,
    }})
    assert document is not None and document["kind"] == "belge" and not document["image"]
    assert document["name"] == "rapor.pdf"
    raw_image = telegram.message_attachment({"document": {"file_id": "i", "mime_type": "image/png"}})
    assert raw_image is not None and raw_image["image"]
    voice = telegram.message_attachment({"voice": {"file_id": "v", "file_size": 7}})
    assert voice is not None and voice["kind"] == "sesli mesaj"
    assert telegram.message_attachment({"text": "merhaba"}) is None


def test_safe_file_name_blocks_path_traversal() -> None:
    assert telegram.safe_file_name("../../etc/passwd") == "passwd"
    assert telegram.safe_file_name("..\\..\\x.txt") == "x.txt"
    assert telegram.safe_file_name("fatura: ekim?.pdf") == "fatura_ ekim_.pdf"
    assert telegram.safe_file_name("..") == "ek"


@pytest.mark.asyncio
async def test_download_saves_private_file_without_leaking_token(tmp_path: Path) -> None:
    requests: List[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path.endswith("/getFile"):
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "documents/file_3.pdf"}})
        return httpx.Response(200, content=b"%PDF-1.7")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        api = telegram.TelegramAPI("secret-token", client)
        path = await api.download("abc", tmp_path / "inbox", "../rapor.pdf")
    assert path.parent == tmp_path / "inbox"
    assert path.name.endswith("-rapor.pdf")
    assert path.read_bytes() == b"%PDF-1.7"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert requests[1] == "/file/botsecret-token/documents/file_3.pdf"

    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"ok": True, "result": {}}),
    )) as client:
        api = telegram.TelegramAPI("secret-token", client)
        with pytest.raises(telegram.TelegramError) as error:
            await api.download("abc", tmp_path / "inbox", None)
    assert "secret-token" not in str(error.value)


@pytest.mark.asyncio
async def test_send_document_uploads_file_with_caption(tmp_path: Path) -> None:
    captured: List[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/botsecret-token/sendDocument"
        captured.append(request.read())
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 9}})

    target = tmp_path / "özet.txt"
    target.write_text("içerik", encoding="utf-8")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await telegram.TelegramAPI("secret-token", client).send_document(123, target, "Haftalık özet")
    body = captured[0]
    assert "Haftalık özet".encode() in body and "içerik".encode() in body

    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(413, json={"ok": False, "description": "Request Entity Too Large"}),
    )) as client:
        with pytest.raises(telegram.TelegramError) as error:
            await telegram.TelegramAPI("secret-token", client).send_document(123, target)
    assert error.value.status == 413 and "secret-token" not in str(error.value)


class FileAPI:
    """Bridge testleri için yalnız dosya yollarını kaydeden sahte Bot API."""

    def __init__(self, download_path: Path) -> None:
        self.download_path = download_path
        self.sent: List[str] = []
        self.documents: List[tuple[Path, str]] = []
        self.downloads: List[tuple[str, Optional[str]]] = []
        self.fail_document = False

    async def send(self, chat_id: int, text: str) -> int:
        self.sent.append(text)
        return len(self.sent)

    async def edit(self, chat_id: int, message_id: int, text: str) -> None:
        pass

    async def send_draft(self, chat_id: int, draft_id: int, rich_message: Dict[str, str]) -> None:
        pass

    async def send_html(self, chat_id: int, content: str) -> int:
        return 1

    async def edit_html(self, chat_id: int, message_id: int, content: str) -> None:
        pass

    async def download(self, file_id: str, directory: Path, preferred_name: Optional[str]) -> Path:
        self.downloads.append((file_id, preferred_name))
        return self.download_path

    async def send_document(self, chat_id: int, path: Path, caption: str = "") -> None:
        if self.fail_document:
            raise telegram.TelegramError("sendDocument: HTTP 413: too large", 413)
        self.documents.append((path, caption))


def _fake_run(seen: List[tuple[str, Dict[str, Any]]]) -> Any:
    async def run(goal: str, emit: Any, options: Any, clients: Any) -> Any:
        seen.append((goal, options))
        metrics = {"turns": 1, "tool_calls": 0, "elapsed_seconds": 0.1, "backend": "ollama-cloud",
                   "prompt_tokens": 1, "cached_tokens": 0, "completion_tokens": 1}
        emit({"kind": "run_finished", "success": True, "outcome": "tamam", "reason": "", "metrics": metrics})
        return {"outcome": "tamam", "success": True, "reason": "", "metrics": metrics,
                "exchange": make_exchange(goal, "tamam", [])}
    return run


@pytest.mark.asyncio
async def test_photo_caption_becomes_goal_and_image_reaches_agent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    photo = _png(tmp_path / "foto.png", (40, 20))
    api = FileAPI(photo)
    seen: List[tuple[str, Dict[str, Any]]] = []
    monkeypatch.setattr(telegram, "run_agent_with_callback", _fake_run(seen))
    bridge = telegram.TelegramBridge(api, SETTINGS)
    await bridge.handle(_message(
        photo=[{"file_id": "p", "width": 40, "height": 20, "file_size": 300}],
        caption="Bu hatayı düzelt",
    ))
    assert bridge.active is not None
    await bridge.active
    goal, options = seen[0]
    assert goal.startswith("Bu hatayı düzelt")
    assert str(photo) in goal and "fotoğraf" in goal
    assert options["images"] == [str(photo)]
    assert options["deliver"] == bridge.deliver

    await options["deliver"](photo, "işte")
    assert api.documents == [(photo, "işte")]
    api.fail_document = True
    with pytest.raises(DeliveryFailed):
        await options["deliver"](photo, "")


@pytest.mark.asyncio
async def test_document_without_caption_uses_default_request_and_no_image(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    document = tmp_path / "rapor.pdf"
    document.write_bytes(b"%PDF")
    api = FileAPI(document)
    seen: List[tuple[str, Dict[str, Any]]] = []
    monkeypatch.setattr(telegram, "run_agent_with_callback", _fake_run(seen))
    bridge = telegram.TelegramBridge(api, SETTINGS)
    await bridge.handle(_message(document={"file_id": "d", "file_name": "rapor.pdf", "mime_type": "application/pdf"}))
    assert bridge.active is not None
    await bridge.active
    goal, options = seen[0]
    assert goal.startswith("Gönderdiğim dosyayı incele")
    assert "images" not in options
    assert api.downloads == [("d", "rapor.pdf")]


@pytest.mark.asyncio
async def test_oversized_or_busy_attachment_does_not_start_task(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    api = FileAPI(tmp_path / "x")
    bridge = telegram.TelegramBridge(api, SETTINGS)
    await bridge.handle(_message(video={"file_id": "v", "file_size": telegram.DOWNLOAD_LIMIT_BYTES + 1}))
    assert bridge.active is None and api.downloads == []
    assert "20 MB" in api.sent[-1]

    loop_future = asyncio.get_running_loop().create_future()
    bridge.pending_answer = loop_future
    await bridge.handle(_message(voice={"file_id": "s", "file_size": 10}))
    assert bridge.active is None and api.downloads == []
    assert "metin" in api.sent[-1]
    loop_future.cancel()
    bridge.pending_answer = None

    # Yetkisiz göndericinin eki indirilmez
    await bridge.handle({"message": {"chat": {"id": 123, "type": "private"}, "from": {"id": 999},
                                     "document": {"file_id": "evil"}}})
    assert api.downloads == []


def test_send_file_schema_only_when_channel_exists() -> None:
    names = lambda schemas: [schema["function"]["name"] for schema in schemas]
    assert "send_file" not in names(route_tool_schemas("rapor.pdf dosyasını gönder", False, False))
    assert "send_file" in names(route_tool_schemas("rapor.pdf dosyasını gönder", False, False, True))
    assert "send_file" in names(route_tool_schemas("açık Chrome oturumunu kullan", False, True, True))
    assert "send_file" in TOOL_NAMES


@pytest.mark.asyncio
async def test_send_file_tool_validates_and_delivers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    delivered: List[tuple[Path, str]] = []

    async def deliver(path: Path, caption: str) -> None:
        if caption == "hata":
            raise DeliveryFailed("ağ yok")
        delivered.append((path, caption))

    box = Toolbox()
    with pytest.raises(ToolError) as error:
        await box.send_file(str(tmp_path / "yok.txt"))
    assert error.value.code == "DELIVERY_UNAVAILABLE"

    token = CURRENT_RUNTIME.set(IntegrationRuntime(lambda event: None, lambda: False, None, deliver))
    try:
        target = tmp_path / "rapor.txt"
        target.write_text("hazır", encoding="utf-8")
        result = await box.send_file(str(target), "  Rapor \n hazır ")
        assert "gönderildi" in result
        assert delivered == [(target, "Rapor hazır")]
        for bad, code in ((tmp_path / "yok.txt", "FILE_NOT_FOUND"), (tmp_path / "bos.txt", "FILE_EMPTY")):
            (tmp_path / "bos.txt").touch()
            with pytest.raises(ToolError) as error:
                await box.send_file(str(bad))
            assert error.value.code == code
        monkeypatch.setattr(tools.facade, "DELIVERY_MAX_BYTES", 3)
        with pytest.raises(ToolError) as error:
            await box.send_file(str(target))
        assert error.value.code == "FILE_TOO_LARGE"
        monkeypatch.undo()
        with pytest.raises(ToolError) as error:
            await box.send_file(str(target), "hata")
        assert error.value.code == "DELIVERY_FAILED" and error.value.recoverable
    finally:
        CURRENT_RUNTIME.reset(token)


def test_attachment_image_keeps_aspect_ratio_and_bad_image_is_reported(tmp_path: Path) -> None:
    wide = _png(tmp_path / "wide.png", (2000, 1000))
    message = main.user_message_with_images("Bunu incele", [str(wide)])
    text_part, image_part = message["content"]
    assert text_part == {"type": "text", "text": "Bunu incele"}
    encoded = image_part["image_url"]["url"].split(",", 1)[1]
    with Image.open(io.BytesIO(base64.b64decode(encoded))) as decoded:
        assert decoded.size == (1000, 500)

    broken = tmp_path / "bozuk.png"
    broken.write_bytes(b"not an image")
    fallback = main.user_message_with_images("Bunu incele", [str(broken)])
    assert isinstance(fallback["content"], str)
    assert "bozuk.png" in fallback["content"] and "verilemedi" in fallback["content"]
    assert main.user_message_with_images("Sade", []) == {"role": "user", "content": "Sade"}


@pytest.mark.asyncio
async def test_agent_sees_attached_image_and_sends_file_end_to_end(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    photo = _png(tmp_path / "ekran.png", (300, 200))
    report_file = tmp_path / "cevap.txt"
    report_file.write_text("çözüm", encoding="utf-8")
    delivered: List[tuple[Path, str]] = []
    calls: List[List[str]] = []

    async def deliver(path: Path, caption: str) -> None:
        delivered.append((path, caption))

    async def fake_model(clients, messages, schemas, session_id, backend, emit, should_stop):
        calls.append([schema["function"]["name"] for schema in schemas])
        if len(calls) == 1:
            first_user = next(item for item in messages if item.get("role") == "user")
            assert any(part.get("type") == "image_url" for part in first_user["content"])
            assert "send_file" in calls[0]
            tool_calls = [{"id": "s", "name": "send_file",
                           "arguments": json.dumps({"path": str(report_file), "caption": "Çözüm"})}]
            return {"content": "", "tool_calls": tool_calls, "finish_reason": "tool_calls",
                    "usage": main.ZERO_USAGE}, backend
        return {"content": "Dosyayı gönderdim.", "tool_calls": [], "finish_reason": "stop",
                "usage": main.ZERO_USAGE}, backend

    monkeypatch.setattr(main, "_call_model_with_retries", fake_model)
    report = await main.run_agent_with_callback(
        "Ekteki hatayı çöz ve cevap.txt dosyasını bana gönder", lambda event: None,
        {"requested_backend": None, "should_stop": lambda: False,
         "state_file": str(tmp_path / "memory.json"), "history": [],
         "deliver": deliver, "images": [str(photo)]},
        {"opencode": object()})
    assert delivered == [(report_file, "Çözüm")]
    assert report["success"], report
