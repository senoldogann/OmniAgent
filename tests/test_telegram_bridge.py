"""Telegram uzaktan erişim, akış ve tek görev kilidinin regresyon testleri."""
import asyncio
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

import telegram_bridge as telegram
from conversation import make_exchange
from host_lock import HostBusyError, host_task_lock


class FakeAPI:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.edited: list[str] = []
        self.photos: list[Path] = []
        self.closed = False

    async def send(self, chat_id: int, text: str) -> int:
        assert chat_id == 123
        self.sent.append(text)
        return len(self.sent)

    async def edit(self, chat_id: int, message_id: int, text: str) -> None:
        assert chat_id == 123 and message_id > 0
        self.edited.append(text)

    async def send_photo(self, chat_id: int, path: Path) -> None:
        assert chat_id == 123
        self.photos.append(path)

    async def close(self) -> None:
        self.closed = True


def test_only_paired_private_sender_is_authorized() -> None:
    settings: telegram.TelegramSettings = {"chat_id": 123, "user_id": 456}
    message = {"chat": {"id": 123, "type": "private"}, "from": {"id": 456}}
    assert telegram.authorized(message, settings)
    assert not telegram.authorized({**message, "from": {"id": 999}}, settings)
    assert not telegram.authorized({**message, "chat": {"id": 123, "type": "group"}}, settings)
    assert not telegram.authorized({**message, "chat": {"id": 999, "type": "private"}}, settings)


@pytest.mark.asyncio
async def test_stream_edits_throttled_and_pages_long_output() -> None:
    api = FakeAPI()
    stream = telegram.TelegramStream(api, 123)
    await stream.append("Merhaba")
    assert api.sent == ["Merhaba"]
    await stream.append(" dünya")
    assert api.edited == []
    stream.last_edit = time.monotonic() - 2
    await stream.flush()
    assert api.edited == ["Merhaba dünya"]
    await stream.append("x" * telegram.PAGE_LIMIT)
    assert len(api.sent) == 2
    assert all(len(page) <= telegram.PAGE_LIMIT for page in api.sent + api.edited)
    assert stream.page == "x" * len("Merhaba dünya")


@pytest.mark.asyncio
async def test_bot_api_retries_provider_rate_limit_without_leaking_token() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, json={
                "ok": False, "description": "Too Many Requests",
                "parameters": {"retry_after": 0},
            })
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    api = telegram.TelegramAPI("secret-token", client)
    try:
        assert await api.send(123, "test") == 7
        assert calls == 2
    finally:
        await client.aclose()

    async def fail(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(fail))
    api = telegram.TelegramAPI("secret-token", client)
    try:
        with pytest.raises(telegram.TelegramError) as captured:
            await api.send(123, "test")
        assert captured.value.status == 401
        assert "secret-token" not in str(captured.value)
    finally:
        await client.aclose()


def test_host_lock_rejects_parallel_tasks_and_releases(tmp_path: Path) -> None:
    path = tmp_path / "host.lock"
    with host_task_lock(path):
        with pytest.raises(HostBusyError):
            with host_task_lock(path):
                pass
    with host_task_lock(path):
        pass


@pytest.mark.asyncio
async def test_bridge_streams_real_event_contract_and_stop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    api = FakeAPI()
    bridge = telegram.TelegramBridge(api, {"chat_id": 123, "user_id": 456})

    image = tmp_path / "screen.png"
    image.write_bytes(b"PNG")

    async def fake_run(goal: str, emit: Any, options: Any, clients: Any) -> Any:
        emit({"kind": "run_started", "goal": goal, "backend": "ollama-cloud", "model": "gemma4:cloud"})
        emit({"kind": "tool_started", "call_id": "screen", "index": 0,
              "name": "take_screenshot", "preview": str(image)})
        emit({"kind": "tool_finished", "call_id": "screen", "ok": True,
              "text": "Ekran alındı", "seconds": 0.1})
        emit({"kind": "tool_started", "call_id": "1", "index": 0, "name": "execute_js", "preview": "2+2"})
        emit({"kind": "tool_finished", "call_id": "1", "ok": True, "text": "4", "seconds": 0.1})
        emit({"kind": "text_delta", "text": "Sonuç: 4"})
        metrics = {
            "turns": 1, "tool_calls": 1, "elapsed_seconds": 0.2, "backend": "ollama-cloud",
            "prompt_tokens": 100, "cached_tokens": 50, "completion_tokens": 10,
        }
        emit({"kind": "run_finished", "success": True, "outcome": "Sonuç: 4", "reason": "", "metrics": metrics})
        return {
            "outcome": "Sonuç: 4", "success": True, "reason": "",
            "metrics": metrics, "exchange": make_exchange(goal, "Sonuç: 4", []),
        }

    monkeypatch.setattr(telegram, "run_agent_with_callback", fake_run)
    update = {"message": {
        "chat": {"id": 123, "type": "private"}, "from": {"id": 456}, "text": "2+2 hesapla",
    }}
    await bridge.handle({"message": {
        "chat": {"id": 123, "type": "private"}, "from": {"id": 456}, "text": "/mode long",
    }})
    assert bridge.run_mode == "extended"
    await bridge.handle(update)
    task = bridge.active
    assert task is not None
    await task
    assert bridge.active is None
    transcript = "\n".join(api.sent + api.edited)
    assert "2+2 hesapla" in transcript
    assert "Node" in transcript
    assert "Sonuç: 4" in transcript
    assert "Token: giriş 100" in transcript
    assert telegram.history_path().exists()
    assert api.photos == [image]

    await bridge.handle({"message": {
        "chat": {"id": 123, "type": "private"}, "from": {"id": 999}, "text": "yasak",
    }})
    assert bridge.active is None
    assert "yasak" not in "\n".join(api.sent)


@pytest.mark.asyncio
async def test_stop_while_user_answer_pending(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    api = FakeAPI()
    bridge = telegram.TelegramBridge(api, {"chat_id": 123, "user_id": 456})
    bridge.pending_answer = asyncio.get_running_loop().create_future()
    bridge.active = asyncio.create_task(asyncio.sleep(10))
    await bridge.handle({"message": {
        "chat": {"id": 123, "type": "private"}, "from": {"id": 456}, "text": "/stop",
    }})
    assert bridge.stop_event.is_set()
    assert isinstance(bridge.pending_answer.exception(), telegram.IntegrationStopped)
    bridge.active.cancel()
    await asyncio.gather(bridge.active, return_exceptions=True)


@pytest.mark.asyncio
async def test_poll_offset_prevents_replaying_command(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(telegram, "create_model_clients", lambda: {})
    update = {"update_id": 77, "message": {
        "chat": {"id": 123, "type": "private"}, "from": {"id": 456}, "text": "görev",
    }}

    class PollAPI(FakeAPI):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def call(self, method: str, payload: dict[str, Any]) -> Any:
            self.calls += 1
            if self.calls == 1:
                return [update]
            raise telegram.TelegramError("getUpdates: HTTP 401", 401)

    first = telegram.TelegramBridge(PollAPI(), {"chat_id": 123, "user_id": 456})
    seen: list[str] = []

    async def remember(item: dict[str, Any]) -> None:
        seen.append(item["message"]["text"])

    first.handle = remember  # type: ignore[method-assign]
    with pytest.raises(telegram.TelegramError):
        await first.run()
    assert seen == ["görev"]

    second = telegram.TelegramBridge(PollAPI(), {"chat_id": 123, "user_id": 456})
    second.handle = remember  # type: ignore[method-assign]
    with pytest.raises(telegram.TelegramError):
        await second.run()
    assert seen == ["görev"]


@pytest.mark.asyncio
async def test_question_waits_for_authorized_reply(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    api = FakeAPI()
    bridge = telegram.TelegramBridge(api, {"chat_id": 123, "user_id": 456})
    waiting = asyncio.create_task(bridge.answer("Hangi gönderen?", {
        "sender": {"type": "string"},
    }))
    await asyncio.sleep(0)
    assert not waiting.done()
    await bridge.handle({"message": {
        "chat": {"id": 123, "type": "private"}, "from": {"id": 999},
        "text": "saldırgan",
    }})
    assert not waiting.done()
    await bridge.handle({"message": {
        "chat": {"id": 123, "type": "private"}, "from": {"id": 456},
        "text": "newsletter@example.com",
    }})
    assert await waiting == {"sender": "newsletter@example.com"}
