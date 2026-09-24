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
        self.drafts: list[tuple[int, dict[str, str]]] = []
        self.rich: list[str] = []
        self.draft_error: int | None = None
        self.rich_error: int | None = None
        self.reject_partial_markdown = False
        self.photos: list[Path] = []
        self.closed = False

    async def send(self, chat_id: int, text: str) -> int:
        assert chat_id == 123
        self.sent.append(text)
        return len(self.sent)

    async def edit(self, chat_id: int, message_id: int, text: str) -> None:
        assert chat_id == 123 and message_id > 0
        self.edited.append(text)

    async def send_draft(
        self, chat_id: int, draft_id: int, rich_message: dict[str, str],
    ) -> None:
        assert chat_id == 123 and draft_id > 0
        if self.draft_error is not None:
            raise telegram.TelegramError("draft unsupported", self.draft_error)
        if self.reject_partial_markdown and rich_message.get("markdown") == "**Yarım":
            raise telegram.TelegramError("unclosed markdown", 400)
        self.drafts.append((draft_id, rich_message))

    async def send_rich(self, chat_id: int, markdown: str) -> int:
        assert chat_id == 123
        if self.rich_error is not None:
            raise telegram.TelegramError("rich unsupported", self.rich_error)
        self.rich.append(markdown)
        return len(self.rich)

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
    await bridge.handle({"message": {
        "chat": {"id": 123, "type": "private"}, "from": {"id": 456}, "text": "/verbose on",
    }})
    assert bridge.verbose
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
async def test_compact_reply_uses_one_message_without_debug_details(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    api = FakeAPI()
    bridge = telegram.TelegramBridge(api, {"chat_id": 123, "user_id": 456})

    async def fake_run(goal: str, emit: Any, options: Any, clients: Any) -> Any:
        emit({"kind": "run_started", "goal": goal, "backend": "ollama-cloud", "model": "gemma4:cloud"})
        emit({"kind": "turn_started", "turn": 1, "max_turns": 25,
              "backend": "ollama-cloud", "model": "gemma4:cloud"})
        emit({"kind": "text_delta", "text": "Bugün "})
        emit({"kind": "text_delta", "text": "Perşembe."})
        metrics = {
            "turns": 1, "tool_calls": 0, "elapsed_seconds": 0.9, "backend": "ollama-cloud",
            "prompt_tokens": 3700, "cached_tokens": 3600, "completion_tokens": 12,
        }
        emit({"kind": "run_finished", "success": True, "outcome": "Bugün Perşembe.",
              "reason": "", "metrics": metrics})
        return {"outcome": "Bugün Perşembe.", "success": True, "reason": "",
                "metrics": metrics, "exchange": make_exchange(goal, "Bugün Perşembe.", [])}

    monkeypatch.setattr(telegram, "run_agent_with_callback", fake_run)
    await bridge.handle({"message": {
        "chat": {"id": 123, "type": "private"}, "from": {"id": 456},
        "text": "Bugün günlerden ne?",
    }})
    task = bridge.active
    assert task is not None
    await task
    assert api.sent == []
    assert api.rich == ["Bugün Perşembe."]
    assert api.drafts
    assert len({draft_id for draft_id, _ in api.drafts}) == 1
    transcript = "\n".join(api.sent + api.edited + api.rich)
    assert "Model:" not in transcript
    assert "Token:" not in transcript
    assert "Tur 1" not in transcript
    assert "Bugün günlerden ne?" not in transcript


@pytest.mark.asyncio
async def test_compact_tool_turn_hides_state_and_keeps_one_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    api = FakeAPI()
    bridge = telegram.TelegramBridge(api, {"chat_id": 123, "user_id": 456})

    async def fake_run(goal: str, emit: Any, options: Any, clients: Any) -> Any:
        emit({"kind": "turn_started", "turn": 1, "max_turns": 25,
              "backend": "ollama-cloud", "model": "gemma4:cloud"})
        emit({"kind": "text_delta", "text": "STATE:"})
        emit({"kind": "text_delta", "text": " gizli çalışma kaydı"})
        emit({"kind": "tool_started", "call_id": "1", "index": 0,
              "name": "execute_js", "preview": "2+2"})
        emit({"kind": "tool_finished", "call_id": "1", "ok": True,
              "text": "4", "seconds": 0.1})
        emit({"kind": "turn_started", "turn": 2, "max_turns": 25,
              "backend": "ollama-cloud", "model": "gemma4:cloud"})
        emit({"kind": "text_delta", "text": "Sonuç: 4"})
        metrics = {
            "turns": 2, "tool_calls": 1, "elapsed_seconds": 1.1, "backend": "ollama-cloud",
            "prompt_tokens": 100, "cached_tokens": 0, "completion_tokens": 10,
        }
        emit({"kind": "run_finished", "success": True, "outcome": "Sonuç: 4",
              "reason": "", "metrics": metrics})
        return {"outcome": "Sonuç: 4", "success": True, "reason": "",
                "metrics": metrics, "exchange": make_exchange(goal, "Sonuç: 4", [])}

    monkeypatch.setattr(telegram, "run_agent_with_callback", fake_run)
    await bridge.handle({"message": {
        "chat": {"id": 123, "type": "private"}, "from": {"id": 456}, "text": "2+2",
    }})
    task = bridge.active
    assert task is not None
    await task
    assert api.sent == []
    assert api.rich == ["Sonuç: 4"]
    assert any("Kod çalıştırıyor" in draft.get("html", "") for _, draft in api.drafts)
    assert "STATE:" not in "\n".join(api.sent + api.edited + api.rich)


@pytest.mark.asyncio
async def test_native_draft_animates_tools_and_preserves_markdown() -> None:
    api = FakeAPI()
    fallback = telegram.TelegramStream(api, 123)
    live = telegram.TelegramDraftStream(api, 123, fallback)
    presenter = telegram.CompactPresenter(live)
    await presenter.event({"kind": "turn_started", "turn": 1, "max_turns": 25,
                           "backend": "test", "model": "test"})
    assert api.drafts[-1][1] == {"html": "<tg-thinking>Düşünüyor…</tg-thinking>"}
    await presenter.event({"kind": "tool_started", "call_id": "1", "index": 0,
                           "name": "execute_shell", "preview": "printf '<secret>'"})
    live.last_draft = time.monotonic() - 2
    await live.tick()
    status = api.drafts[-1][1]["html"]
    assert "Komut çalıştırıyor" in status
    assert "&lt;secret&gt;" in status
    await presenter.event({"kind": "tool_output", "call_id": "1", "text": "çalışıyor\\n"})
    live.last_draft = time.monotonic() - 2
    await live.tick()
    assert "çalışıyor" in api.drafts[-1][1]["html"]
    await presenter.event({"kind": "run_finished", "success": True,
                           "outcome": "**Kalın** [kaynak](https://example.com)",
                           "reason": "", "metrics": {}})
    assert api.rich == ["**Kalın** [kaynak](https://example.com)"]
    assert api.sent == []
    assert len({draft_id for draft_id, _ in api.drafts}) == 1


@pytest.mark.asyncio
async def test_old_bot_api_falls_back_to_existing_message_stream() -> None:
    api = FakeAPI()
    api.draft_error = 404
    api.rich_error = 404
    fallback = telegram.TelegramStream(api, 123)
    live = telegram.TelegramDraftStream(api, 123, fallback)
    await live.show("⏳ Düşünüyor…")
    assert not live.native
    assert api.sent == ["⏳ Düşünüyor…"]
    await live.finish("**Yanıt**")
    assert api.edited == ["**Yanıt**"]


@pytest.mark.asyncio
async def test_partial_markdown_uses_plain_draft_then_rich_final() -> None:
    api = FakeAPI()
    api.reject_partial_markdown = True
    live = telegram.TelegramDraftStream(api, 123, telegram.TelegramStream(api, 123))
    await live.show("**Yarım")
    assert live.native
    assert api.drafts[-1][1] == {"html": "**Yarım"}
    await live.finish("**Yarım**")
    assert api.rich == ["**Yarım**"]
    assert api.sent == []


@pytest.mark.asyncio
async def test_bot_api_rich_payload_contract() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path.rsplit("/", 1)[-1], __import__("json").loads(request.content)))
        result: Any = {"message_id": 9} if calls[-1][0] == "sendRichMessage" else True
        return httpx.Response(200, json={"ok": True, "result": result})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    api = telegram.TelegramAPI("secret-token", client)
    try:
        await api.send_draft(123, 7, {"html": "<tg-thinking>Düşünüyor…</tg-thinking>"})
        assert await api.send_rich(123, "**Kalın**") == 9
    finally:
        await client.aclose()
    assert calls == [
        ("sendRichMessageDraft", {"chat_id": 123, "draft_id": 7,
                                  "rich_message": {"html": "<tg-thinking>Düşünüyor…</tg-thinking>"}}),
        ("sendRichMessage", {"chat_id": 123, "rich_message": {"markdown": "**Kalın**"}}),
    ]


@pytest.mark.asyncio
async def test_draft_heartbeat_and_rate_limit() -> None:
    api = FakeAPI()
    live = telegram.TelegramDraftStream(api, 123, telegram.TelegramStream(api, 123))
    await live.show("ilk")
    assert len(api.drafts) == 1
    await live.show("ikinci")
    assert len(api.drafts) == 1
    live.last_draft = time.monotonic() - 2
    await live.tick()
    assert api.drafts[-1][1] == {"markdown": "ikinci"}
    live.last_draft = time.monotonic() - 5
    await live.tick()
    assert len(api.drafts) == 3


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
