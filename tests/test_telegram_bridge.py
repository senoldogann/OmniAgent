"""Telegram uzaktan erişim, akış ve tek görev kilidinin regresyon testleri."""
import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from openai import AsyncOpenAI

from omniagent.integrations import telegram
from omniagent.app import agent as main
from omniagent.app.model_retry import REMOTE_MODEL_RETRY_SECONDS
from omniagent.core.conversation import make_exchange
from omniagent.integrations.capabilities import CapabilityService
from omniagent.paths import workspace_dir
from omniagent.platform.macos.host_lock import HostBusyError, host_task_lock
from omniagent.tools import filesystem
from tests.test_fallback_egress import FakeProvider


class FakeAPI:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.edited: list[str] = []
        self.drafts: list[tuple[int, dict[str, str]]] = []
        self.html_sent: list[str] = []
        self.html_edited: list[str] = []
        self.draft_error: int | None = None
        self.reject_partial_markdown = False
        self.photos: list[Path] = []
        self.closed = False
        self.buttons: list[tuple[str, list[list[tuple[str, str]]]]] = []
        self.callback_answers: list[str] = []
        self.edited_buttons: list[tuple[str, list[list[tuple[str, str]]]]] = []
        self.commands: list[tuple[str, str]] = []

    async def send(self, chat_id: int, text: str) -> int:
        assert chat_id == 123
        self.sent.append(text)
        return len(self.sent)

    async def send_buttons(self, chat_id: int, text: str, buttons: list[list[tuple[str, str]]]) -> int:
        assert chat_id == 123 and all(len(data.encode()) <= 64 for row in buttons for _, data in row)
        self.sent.append(text)
        self.buttons.append((text, buttons))
        return len(self.sent)

    async def answer_callback(self, callback_id: str, text: str) -> None:
        assert callback_id
        self.callback_answers.append(text)

    async def edit_buttons(
        self, chat_id: int, message_id: int, text: str, buttons: list[list[tuple[str, str]]],
    ) -> None:
        assert chat_id == 123 and message_id > 0
        assert all(len(data.encode()) <= 64 for row in buttons for _, data in row)
        self.edited.append(text)
        self.edited_buttons.append((text, buttons))

    async def set_commands(self, commands: list[tuple[str, str]]) -> None:
        self.commands = commands

    async def edit(self, chat_id: int, message_id: int, text: str) -> None:
        assert chat_id == 123 and message_id > 0
        self.edited.append(text)

    async def send_html(self, chat_id: int, content: str) -> int:
        assert chat_id == 123
        self.html_sent.append(content)
        return 1

    async def edit_html(self, chat_id: int, message_id: int, content: str) -> None:
        assert chat_id == 123 and message_id > 0
        self.html_edited.append(content)

    async def send_draft(
        self, chat_id: int, draft_id: int, rich_message: dict[str, str],
    ) -> None:
        assert chat_id == 123 and draft_id > 0
        if self.draft_error is not None:
            raise telegram.TelegramError("draft unsupported", self.draft_error)
        if self.reject_partial_markdown and rich_message.get("markdown") == "**Yarım":
            raise telegram.TelegramError("unclosed markdown", 400)
        self.drafts.append((draft_id, rich_message))

    async def send_photo(self, chat_id: int, path: Path) -> None:
        assert chat_id == 123
        self.photos.append(path)

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_bridge_delivers_host_rejection_without_model_success_claim(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(telegram, "STATE_FILE", str(tmp_path / "memory.json"))
    api = FakeAPI()
    bridge = telegram.TelegramBridge(api, {"chat_id": 123, "user_id": 456})
    monkeypatch.setattr(telegram, "create_model_clients", lambda: {"ollama-cloud": object()})
    bridge.integrations = CapabilityService(tmp_path)
    target = tmp_path / "hedef.txt"
    target.write_text("koru", encoding="utf-8")
    other = tmp_path / "not.txt"
    turns = 0

    async def fake_model(
        clients: Any, messages: Any, schemas: Any, session_id: str,
        backend: str, emit: Any, should_stop: Any,
    ) -> tuple[dict[str, Any], str]:
        nonlocal turns
        turns += 1
        if turns == 1:
            return {
                "content": "", "tool_calls": [{
                    "id": "write-1", "name": "write_file",
                    "arguments": json.dumps({"path": str(other), "content": "alakasız"}),
                }], "finish_reason": "tool_calls", "usage": main.ZERO_USAGE,
            }, backend
        emit({"kind": "text_delta", "text": "Hedef silindi."})
        return {"content": "Hedef silindi.", "tool_calls": [],
                "finish_reason": "stop", "usage": main.ZERO_USAGE}, backend

    monkeypatch.setattr(main, "_call_model_with_retries", fake_model)
    try:
        await bridge.handle({"message": {
            "chat": {"id": 123, "type": "private"}, "from": {"id": 456},
            "text": f"sil: `{target}`",
        }})
        assert bridge.active is not None
        await bridge.active
    finally:
        await bridge.integrations.close()
    transcript = "\n".join(api.sent + api.edited + api.html_sent + api.html_edited)
    assert "Doğrulanmadı:" in transcript
    assert "Hedef silindi." not in transcript
    assert target.read_text(encoding="utf-8") == "koru"


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
        "chat": {"id": 123, "type": "private"}, "from": {"id": 456}, "text": "/mode surekli",
    }})
    assert bridge.run_mode == "continuous"
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
async def test_btw_and_approve_reach_only_active_continuous_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    api = FakeAPI()
    bridge = telegram.TelegramBridge(api, {"chat_id": 123, "user_id": 456})
    bridge.active = asyncio.create_task(asyncio.sleep(10))
    bridge.active_run_mode = "continuous"

    def message(user: int, content: str) -> dict[str, Any]:
        return {"message": {"chat": {"id": 123, "type": "private"},
                            "from": {"id": user}, "text": content}}

    try:
        await bridge.handle(message(999, "/btw Yetkisiz"))
        await bridge.handle(message(456, "/btw Önce taslak"))
        await bridge.handle(message(456, "/approve"))
        assert bridge._drain_control_messages() == ["/btw Önce taslak", "/approve"]
        assert not any("Yetkisiz" in text for text in api.sent)
    finally:
        bridge.active.cancel()
        await asyncio.gather(bridge.active, return_exceptions=True)


@pytest.mark.asyncio
async def test_btw_sent_immediately_after_start_is_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    api = FakeAPI()
    bridge = telegram.TelegramBridge(api, {"chat_id": 123, "user_id": 456})
    bridge.run_mode = "continuous"
    received: list[str] = []

    async def fake_run(goal: str, emit: Any, options: Any, clients: Any) -> Any:
        received.extend(options["pop_control_messages"]())
        return {"outcome": "bitti", "success": True, "reason": "", "metrics": {
            "turns": 1, "tool_calls": 0, "elapsed_seconds": 0.1, "backend": "ollama-cloud",
            "prompt_tokens": 0, "cached_tokens": 0, "completion_tokens": 0,
        }, "exchange": make_exchange(goal, "bitti", [])}

    monkeypatch.setattr(telegram, "run_agent_with_callback", fake_run)

    def message(text: str) -> dict[str, Any]:
        return {"message": {"chat": {"id": 123, "type": "private"},
                            "from": {"id": 456}, "text": text}}

    await bridge.handle(message("Bir plan oluştur"))
    task = bridge.active
    assert task is not None
    await bridge.handle(message("/btw Önce taslak"))
    await task
    assert received == ["/btw Önce taslak"]


@pytest.mark.asyncio
async def test_bridge_rebuilds_model_clients_at_every_task_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hazır profiller görevler arasında değişir (Ollama sonradan açılıp kapanabilir): istemciler her görev başında yenilenir."""
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    ollama = {"ready": True}
    monkeypatch.setattr(main, "ollama_cloud_ready", lambda: ollama["ready"])
    # Anahtarsız profiller: hazır küme yalnız Ollama'nın durumuna bağlı kalır.
    monkeypatch.setattr(main, "BACKENDS", {name: {**profile, "api_key": None} for name, profile in main.BACKENDS.items()})
    # Gerçek fabrika: dosya düzeyindeki yalıtım fixture'ını bu testte geçersiz kılar.
    monkeypatch.setattr(telegram, "create_model_clients", main.create_model_clients)
    api = FakeAPI()
    bridge = telegram.TelegramBridge(api, {"chat_id": 123, "user_id": 456})
    seen: list[dict[str, Any]] = []

    async def fake_run(goal: str, emit: Any, options: Any, clients: Any) -> Any:
        seen.append(clients)
        return {"outcome": "bitti", "success": True, "reason": "", "metrics": {
            "turns": 1, "tool_calls": 0, "elapsed_seconds": 0.1, "backend": "ollama-cloud",
            "prompt_tokens": 0, "cached_tokens": 0, "completion_tokens": 0,
        }, "exchange": make_exchange(goal, "bitti", [])}

    monkeypatch.setattr(telegram, "run_agent_with_callback", fake_run)
    for ready in (True, False):
        ollama["ready"] = ready
        await bridge.handle({"message": {"chat": {"id": 123, "type": "private"},
                                         "from": {"id": 456}, "text": "Merhaba de"}})
        assert bridge.active is not None
        await bridge.active
    assert [sorted(clients) for clients in seen] == [["ollama-cloud"], []]
    assert seen[0]["ollama-cloud"].is_closed() and bridge.clients is seen[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("backends,request_count", [("none", 0), ("openai", 1)])
async def test_telegram_auto_task_reaches_openai_only_with_permission_when_ollama_is_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backends: str, request_count: int,
) -> None:
    """
    Gerçek ajan + gerçek SDK + yerel HTTP: köprü Ollama hazır değilken /model auto (varsayılan) ile görev alırsa
    yalnız openai hazır diye veri OpenAI'a gitmez; izin verilmişse tam bir istek gider ve kullanıcı bilgilendirilir.
    """
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(telegram, "STATE_FILE", str(tmp_path / "memory.json"))
    monkeypatch.setenv("OMNI_FALLBACK_BACKENDS", backends)
    provider = FakeProvider(200)
    monkeypatch.setattr(
        telegram, "create_model_clients",
        lambda: {"openai": AsyncOpenAI(base_url=provider.url, api_key="x", max_retries=0)},
    )
    api = FakeAPI()
    bridge = telegram.TelegramBridge(api, {"chat_id": 123, "user_id": 456})
    bridge.integrations = CapabilityService(tmp_path)
    try:
        await bridge.handle({"message": {"chat": {"id": 123, "type": "private"},
                                         "from": {"id": 456}, "text": "Merhaba de"}})
        assert bridge.active is not None
        await bridge.active
    finally:
        await bridge.integrations.close()
        await main.close_model_clients(bridge.clients)
        provider.close()
    assert len(provider.bodies) == request_count
    transcript = "\n".join(api.sent + api.edited + api.html_sent + api.html_edited)
    if request_count:
        assert "Yedek sağlayıcıya geçildi: ollama-cloud → openai" in transcript
    else:
        assert "yedek sağlayıcı izni yok" in transcript and "Yedek sağlayıcıya geçildi" not in transcript


@pytest.mark.asyncio
async def test_telegram_tasks_get_the_remote_model_retry_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kullanıcı makinenin başında değil: Telegram görevi kısa ağ kopmasında model çağrısında uzak bütçeyle bekler."""
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    api = FakeAPI()
    bridge = telegram.TelegramBridge(api, {"chat_id": 123, "user_id": 456})
    budgets: list[float] = []

    async def fake_run(goal: str, emit: Any, options: Any, clients: Any) -> Any:
        budgets.append(options["model_retry_seconds"])
        return {"outcome": "bitti", "success": True, "reason": "", "metrics": {
            "turns": 1, "tool_calls": 0, "elapsed_seconds": 0.1, "backend": "ollama-cloud",
            "prompt_tokens": 0, "cached_tokens": 0, "completion_tokens": 0,
        }, "exchange": make_exchange(goal, "bitti", [])}

    monkeypatch.setattr(telegram, "run_agent_with_callback", fake_run)
    await bridge.handle({"message": {"chat": {"id": 123, "type": "private"},
                                     "from": {"id": 456}, "text": "Merhaba de"}})
    task = bridge.active
    assert task is not None
    await task
    assert budgets == [REMOTE_MODEL_RETRY_SECONDS] and REMOTE_MODEL_RETRY_SECONDS > 60.0


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
    assert api.html_sent == ["Bugün Perşembe."]
    assert api.drafts
    assert len({draft_id for draft_id, _ in api.drafts}) == 1
    transcript = "\n".join(api.sent + api.edited + api.html_sent)
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
    # Canlı iş günlüğü adımı kalıcı iletide gösterir; son yanıt ayrı iletidir.
    assert api.html_sent[-1] == "Sonuç: 4"
    assert any("🟨 Node" in text for text in api.html_sent + api.html_edited)
    assert any("Kod çalıştırıyor" in draft.get("html", "") for _, draft in api.drafts)
    assert "STATE:" not in "\n".join(api.sent + api.edited + api.html_sent)


@pytest.mark.asyncio
async def test_waiting_user_status_is_readable_and_reply_resumes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    api = FakeAPI()
    live = telegram.TelegramDraftStream(api, 123, telegram.TelegramStream(api, 123))
    presenter = telegram.CompactPresenter(live)
    waiting: dict[str, Any] = {
        "kind": "integration_status", "stage": "waiting_user",
        "text": "Hangi yöntemi seçiyorsunuz?", "completed": 0, "total": 0,
    }
    await presenter.event(waiting)
    assert "Yanıtınız bekleniyor" in api.drafts[-1][1]["html"]
    assert "waiting_user" not in str(api.drafts[-1][1])
    assert "waiting_user" not in telegram.event_text(waiting)

    bridge = telegram.TelegramBridge(api, {"chat_id": 123, "user_id": 456})
    bridge.pending_answer = asyncio.get_running_loop().create_future()
    bridge.pending_fields = {"yanit": {"type": "string", "label": "Yanıtınız"}}
    await bridge.handle({"message": {
        "chat": {"id": 123, "type": "private"}, "from": {"id": 456},
        "text": "Mikro-SaaS",
    }})
    assert bridge.pending_answer.result() == {"yanit": "Mikro-SaaS"}
    assert api.sent[-1] == "Yanıt alındı; görev sürüyor."

    resumed: dict[str, Any] = {
        "kind": "integration_status", "stage": "resumed",
        "text": "Göreve devam ediliyor", "completed": 0, "total": 0,
    }
    await presenter.event(resumed)
    assert "Göreve devam ediliyor" in api.drafts[-1][1]["html"]


def tap(data: str, user_id: int) -> dict[str, Any]:
    """Eşleşmiş sohbetteki bir bot iletisinin butonuna dokunuş (callback_query güncellemesi)."""
    return {"callback_query": {
        "id": f"cb-{data}", "from": {"id": user_id}, "data": data,
        "message": {"message_id": 7, "chat": {"id": 123, "type": "private"}, "text": "❔ Soru"},
    }}


def text_message(text: str) -> dict[str, Any]:
    return {"message": {"chat": {"id": 123, "type": "private"}, "from": {"id": 456}, "text": text}}


@pytest.mark.asyncio
async def test_permission_question_is_answered_by_button_and_stale_or_foreign_taps_do_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """İzin sorusu Onayla/Reddet butonlarıyla gelir; dokunuş yanıtlar ve iletiyi kararla günceller."""
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    api = FakeAPI()
    bridge = telegram.TelegramBridge(api, {"chat_id": 123, "user_id": 456})
    fields: dict[str, Any] = {"onay": {"type": "boolean", "label": "Onaylıyorum", "default": False}}
    first = asyncio.create_task(bridge.answer("Dosyalar silinsin mi?", fields))
    await asyncio.sleep(0)
    text, rows = api.buttons[-1]
    assert "Dosyalar silinsin mi?" in text
    assert [label for label, _ in rows[0]] == ["✅ Onayla", "❌ Reddet"]
    approve: str = rows[0][0][1]
    await bridge.handle(tap(approve, 999))
    assert not first.done() and api.callback_answers == []
    await bridge.handle(tap(approve, 456))
    assert await first == {"onay": True}
    assert api.edited[-1] == "❔ Soru\n\n→ ✅ Onayla"
    # Önceki sorunun butonu yeni soruyu yanıtlamaz.
    second = asyncio.create_task(bridge.answer("Tekrar silinsin mi?", fields))
    await asyncio.sleep(0)
    await bridge.handle(tap(approve, 456))
    assert not second.done() and "artık geçerli değil" in api.callback_answers[-1]
    await bridge.handle(tap(api.buttons[-1][1][0][1][1], 456))
    assert await second == {"onay": False}


@pytest.mark.asyncio
async def test_text_question_offers_its_choices_as_buttons(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Hedef onayı gibi seçenekli metin sorusu seçenekleri buton olarak sunar; eksik olanı yazmak yine çalışır."""
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    api = FakeAPI()
    bridge = telegram.TelegramBridge(api, {"chat_id": 123, "user_id": 456})
    fields: dict[str, Any] = {"yanit": {"type": "string", "label": "'evet' ya da eksik olan", "default": "",
                                        "choices": ["Evet"]}}
    waiting = asyncio.create_task(bridge.answer("Hedef gerçekleşti mi?", fields))
    await asyncio.sleep(0)
    rows = api.buttons[-1][1]
    assert [label for label, _ in rows[0]] == ["Evet"]
    await bridge.handle(tap(rows[0][0][1], 456))
    assert await waiting == {"yanit": "Evet"}


@pytest.mark.asyncio
async def test_provider_picker_selects_a_provider_then_its_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """/provider hazır sağlayıcıları, dokunulanın kataloğundaki modelleri sayfalı sunar; seçim tercihe yazılır."""
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(telegram, "apply_model_preferences", lambda: ())
    catalog = tuple(f"gpt-test-{index}" for index in range(10))

    async def fake_models(provider: str, base_url: str, key: str | None) -> tuple[str, ...]:
        assert provider == telegram.BACKENDS["openai"]["provider"]
        return catalog

    monkeypatch.setattr(telegram, "list_provider_models", fake_models)
    api = FakeAPI()
    bridge = telegram.TelegramBridge(api, {"chat_id": 123, "user_id": 456})
    bridge.clients = {"ollama-cloud": object(), "openai": object()}  # type: ignore[dict-item]
    await bridge.handle(text_message("/provider"))
    labels = [label for row in api.buttons[-1][1] for label, _ in row]
    assert labels[0] == "✓ Otomatik" and any(label.startswith("openai") for label in labels)
    assert not any(label.startswith("openrouter") for label in labels)
    await bridge.handle(tap("prov:openai", 456))
    rows = api.edited_buttons[-1][1]
    models = [label for row in rows for label, _ in row if not label.startswith(("◀", "▶"))]
    assert models[0] == "✓ " + telegram.BACKENDS["openai"]["model"] and len(models) == telegram.MODEL_PAGE_SIZE
    next_page = [data for row in rows for label, data in row if label.startswith("▶")][0]
    await bridge.handle(tap(next_page, 456))
    page_two = [(label, data) for row in api.edited_buttons[-1][1] for label, data in row
                if not label.startswith(("◀", "▶"))]
    assert page_two[-1][0] == "gpt-test-9"
    await bridge.handle(tap(page_two[-1][1], 456))
    assert bridge.backend == "openai" and "gpt-test-9" in api.callback_answers[-1]
    assert json.loads((tmp_path / "model_preferences.json").read_text(encoding="utf-8"))["openai"] == "gpt-test-9"
    # Kapanan seçicinin butonu ikinci kez işlemez.
    await bridge.handle(tap(page_two[-1][1], 456))
    assert "artık geçerli değil" in api.callback_answers[-1]
    await bridge.handle(text_message("/model"))
    await bridge.handle(tap("prov:auto", 456))
    assert bridge.backend is None


@pytest.mark.asyncio
async def test_mode_picker_sets_the_next_task(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Argümansız /mode modları buton olarak sunar; dokunuş sonraki görevin modunu ayarlar."""
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    api = FakeAPI()
    bridge = telegram.TelegramBridge(api, {"chat_id": 123, "user_id": 456})
    await bridge.handle(text_message("/mode"))
    assert [label for row in api.buttons[-1][1] for label, _ in row][0] == "✓ Normal"
    await bridge.handle(tap("mode:continuous", 456))
    assert bridge.run_mode == "continuous"


@pytest.mark.asyncio
async def test_new_clears_history_and_status_reports_next_task_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """/new sohbet geçmişini sıfırlar; boştayken /status sonraki görevin modelini, modunu ve geçmişi gösterir."""
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    api = FakeAPI()
    bridge = telegram.TelegramBridge(api, {"chat_id": 123, "user_id": 456})
    bridge.history = [make_exchange("eski hedef", "eski yanıt", [])]
    await bridge.handle(text_message("/status"))
    assert api.sent[-1].startswith("Hazır.") and "Otomatik" in api.sent[-1] and "1 konuşma" in api.sent[-1]
    await bridge.handle(text_message("/new"))
    assert bridge.history == [] and "temizlendi" in api.sent[-1]
    assert telegram.read_json(telegram.history_path(), None) == []


@pytest.mark.asyncio
async def test_continuous_goal_prompt_has_buttons_and_approve_tap_closes_the_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Sürekli oturumun onay istemi butonlu kalıcı iletidir; Onayla oturumu kapatır, başka oturumun butonu işlemez."""
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(telegram, "STATE_FILE", str(tmp_path / "memory.json"))
    monkeypatch.setattr(telegram, "create_model_clients", lambda: {"ollama-cloud": object()})
    monkeypatch.setattr(filesystem, "BACKUP_DIR", tmp_path / "backups")
    api = FakeAPI()
    bridge = telegram.TelegramBridge(api, {"chat_id": 123, "user_id": 456})
    bridge.integrations = CapabilityService(tmp_path)
    bridge.run_mode = "continuous"
    turns = 0

    async def fake_model(
        clients: Any, messages: Any, schemas: Any, session_id: str,
        backend: str, emit: Any, should_stop: Any,
    ) -> tuple[dict[str, Any], str]:
        nonlocal turns
        turns += 1
        calls: dict[int, dict[str, str]] = {
            1: {"id": "w1", "name": "write_file",
                "arguments": json.dumps({"path": str(tmp_path / "plan.md"), "content": "plan"})},
            2: {"id": "g1", "name": "report_goal_met",
                "arguments": json.dumps({"summary": "Plan hazır.", "evidence_call_ids": ["w1"]})},
        }
        if turns in calls:
            return {"content": "", "tool_calls": [calls[turns]], "finish_reason": "tool_calls",
                    "usage": main.ZERO_USAGE}, backend
        return {"content": "", "tool_calls": [], "finish_reason": "stopped", "usage": main.ZERO_USAGE}, backend

    monkeypatch.setattr(main, "_call_model_with_retries", fake_model)
    try:
        await bridge.handle(text_message("Plan yaz"))
        task = bridge.active
        assert task is not None
        for _ in range(100):
            if api.buttons:
                break
            await asyncio.sleep(0.05)
        text, rows = api.buttons[-1]
        assert "Plan hazır." in text and "/approve" in text
        assert [label for label, _ in rows[0]] == ["✅ Onayla", "⏹ Durdur"]
        assert "Yanıtınız bekleniyor" in api.drafts[-1][1]["html"]
        await bridge.handle(text_message("/status"))
        assert "Çalışıyor: Plan yaz" in api.sent[-1] and "Token:" in api.sent[-1]
        assert any("✍️ Yazıyor" in text for text in api.html_sent + api.html_edited)
        await bridge.handle(tap("ctl:eskioturum:approve", 456))
        assert "artık açık değil" in api.callback_answers[-1] and not task.done()
        await bridge.handle(tap(rows[0][0][1], 456))
        await asyncio.wait_for(task, timeout=5)
    finally:
        await bridge.integrations.close()
    assert turns == 2
    assert "Plan hazır." in "\n".join(api.sent + api.edited + api.html_sent + api.html_edited)


async def finished_run(goal: str, emit: Any, options: Any, clients: Any) -> Any:
    """Tek metin yanıtı üreten ajan koşusu (sahte)."""
    metrics = {"turns": 1, "tool_calls": 0, "elapsed_seconds": 0.1, "backend": "ollama-cloud",
               "prompt_tokens": 10, "cached_tokens": 0, "completion_tokens": 5}
    emit({"kind": "run_finished", "success": True, "outcome": "Bitti.", "reason": "", "metrics": metrics})
    return {"outcome": "Bitti.", "success": True, "reason": "", "metrics": metrics,
            "exchange": make_exchange(goal, "Bitti.", [])}


@pytest.mark.asyncio
async def test_control_prompt_falls_back_to_plain_text_when_its_buttons_cannot_be_sent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """Butonlu istem gönderimi ağ/5xx ile düşse de saatlerdir süren oturum ölmez: uyarı + düz metin, koşu biter."""
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(telegram, "create_model_clients", lambda: {"ollama-cloud": object()})
    api = FakeAPI()

    async def failing_buttons(chat_id: int, text: str, buttons: list[list[tuple[str, str]]]) -> int:
        raise telegram.TelegramError("sendMessage: HTTP 503: Service Unavailable", 503)

    api.send_buttons = failing_buttons
    bridge = telegram.TelegramBridge(api, {"chat_id": 123, "user_id": 456})
    prompt = "Hedef doğrulandı. Onay için /approve yazın."

    async def fake_run(goal: str, emit: Any, options: Any, clients: Any) -> Any:
        emit({"kind": "notice", "level": "info", "text": prompt, "code": telegram.AWAITING_APPROVAL_CODE})
        return await finished_run(goal, emit, options, clients)

    monkeypatch.setattr(telegram, "run_agent_with_callback", fake_run)
    with caplog.at_level("WARNING"):
        await bridge.handle(text_message("Bir şey yap"))
        assert bridge.active is not None
        await asyncio.wait_for(bridge.active, timeout=10)
    assert prompt in api.sent and "Bitti." in api.html_sent
    assert not any("Görev hatası" in text or "503" in text for text in [*api.sent, *api.edited, *api.html_sent])
    warning = next(record for record in caplog.records
                   if record.getMessage() == "Oturum istemi butonlarla gönderilemedi; düz metin gönderiliyor")
    assert warning.status == 503 and warning.code == telegram.AWAITING_APPROVAL_CODE


@pytest.mark.asyncio
async def test_run_token_is_cleared_when_the_run_ends(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Biten koşunun belirteci kalmaz: yeni koşu belirteç alana dek eski oturum butonları hiçbir göreve bağlanamaz."""
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(telegram, "create_model_clients", lambda: {"ollama-cloud": object()})
    api = FakeAPI()
    bridge = telegram.TelegramBridge(api, {"chat_id": 123, "user_id": 456})
    during: list[str] = []

    async def fake_run(goal: str, emit: Any, options: Any, clients: Any) -> Any:
        during.append(bridge.run_token)
        return await finished_run(goal, emit, options, clients)

    monkeypatch.setattr(telegram, "run_agent_with_callback", fake_run)
    await bridge.handle(text_message("Bir şey yap"))
    assert bridge.active is not None
    await asyncio.wait_for(bridge.active, timeout=10)
    assert len(during) == 1 and during[0] != "" and bridge.run_token == ""


@pytest.mark.asyncio
async def test_expired_callback_query_does_not_skip_the_message_update(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """Süresi geçmiş buton sorgusu (answerCallbackQuery reddi) iletiyi kararla güncellemeyi atlatmaz."""
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    api = FakeAPI()

    async def expired(callback_id: str, text: str) -> None:
        raise telegram.TelegramError("answerCallbackQuery: HTTP 400: query is too old", 400)

    api.answer_callback = expired
    bridge = telegram.TelegramBridge(api, {"chat_id": 123, "user_id": 456})
    fields: dict[str, Any] = {"onay": {"type": "boolean", "label": "Onaylıyorum", "default": False}}
    waiting = asyncio.create_task(bridge.answer("Dosyalar silinsin mi?", fields))
    await asyncio.sleep(0)
    approve: str = api.buttons[-1][1][0][0][1]
    with caplog.at_level("WARNING"):
        await bridge.handle(tap(approve, 456))
    assert await asyncio.wait_for(waiting, timeout=10) == {"onay": True}
    assert api.edited[-1] == "❔ Soru\n\n→ ✅ Onayla"
    assert any(record.getMessage() == "Buton dokunuşu yanıtlanamadı" for record in caplog.records)


@pytest.mark.asyncio
async def test_non_decimal_digits_in_picker_buttons_are_stale_not_a_crash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Sayfa/model dizini yalnız ondalık rakamdır: '²' gibi isdigit() rakamı int() hatasıyla köprüyü düşürmez."""
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(telegram, "apply_model_preferences", lambda: ())

    async def fake_models(provider: str, base_url: str, key: str | None) -> tuple[str, ...]:
        return ("gpt-test-1",)

    monkeypatch.setattr(telegram, "list_provider_models", fake_models)
    api = FakeAPI()
    bridge = telegram.TelegramBridge(api, {"chat_id": 123, "user_id": 456})
    await bridge.handle(tap("prov:openai", 456))
    picker = bridge.model_picker
    assert picker is not None
    await bridge.handle(tap(f"page:{picker['token']}:²", 456))
    assert "artık geçerli değil" in api.callback_answers[-1]
    await bridge.handle(tap(f"pm:{picker['token']}:²", 456))
    assert "artık geçerli değil" in api.callback_answers[-1] and bridge.model_picker is picker


PROVIDER_SWITCH: dict[str, Any] = {
    "kind": "provider_fallback", "from_backend": "ollama-cloud", "to_backend": "openai",
    "to_model": "gpt-6-luna", "processor": "OpenAI", "reason": "hız sınırı (HTTP 429)", "image_count": 2,
}


@pytest.mark.asyncio
async def test_provider_fallback_is_one_persistent_message_per_target_in_compact_view() -> None:
    """Kısa görünümde gizlilik olayı taslakta kaybolmaz: görev başına hedef sağlayıcı başına tek kalıcı ileti."""
    api = FakeAPI()
    live = telegram.TelegramDraftStream(api, 123, telegram.TelegramStream(api, 123))
    presenter = telegram.CompactPresenter(live)
    await presenter.event(PROVIDER_SWITCH)
    await presenter.event(PROVIDER_SWITCH)
    assert len(api.sent) == 1
    assert "2 ekran görüntüsü" in api.sent[0] and "OpenAI" in api.sent[0] and "hız sınırı" in api.sent[0]
    await presenter.event({**PROVIDER_SWITCH, "to_backend": "openrouter", "processor": "OpenRouter"})
    assert len(api.sent) == 2 and "openrouter" in api.sent[1]
    # Aynı hedefe farklı görüntü seviyesi (metin ↔ ekran görüntüsü) yeni kalıcı ileti üretir; aynı seviye
    # (ekran görüntülü olan zaten bildirildi) tekrarlanmaz.
    await presenter.event({**PROVIDER_SWITCH, "image_count": 0})
    await presenter.event({**PROVIDER_SWITCH, "image_count": 5})
    assert len(api.sent) == 3 and "ekran görüntüsü dahil" not in api.sent[2]
    # Ayrıntılı görünüm aynı olayı satır içi gösterir.
    assert "Yedek sağlayıcıya geçildi" in telegram.event_text(PROVIDER_SWITCH)


@pytest.mark.asyncio
async def test_provider_fallback_notice_failure_does_not_abort_the_task(
    caplog: pytest.LogCaptureFixture,
) -> None:
    api = FakeAPI()

    async def failing_send(chat_id: int, text: str) -> int:
        raise telegram.TelegramError("sendMessage: ağ hatası", None)

    api.send = failing_send
    live = telegram.TelegramDraftStream(api, 123, telegram.TelegramStream(api, 123))
    presenter = telegram.CompactPresenter(live)
    with caplog.at_level("WARNING"):
        await presenter.event(PROVIDER_SWITCH)
    assert any(record.getMessage() == "Yedek sağlayıcı bildirimi gönderilemedi" for record in caplog.records)


@pytest.mark.asyncio
async def test_provider_fallback_notice_is_retried_after_a_failed_send(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Gönderim düşerse hedef 'bildirildi' sayılmaz: gizlilik olayı sonraki olayda yeniden denenir, ulaşınca tekrarlanmaz."""
    api = FakeAPI()
    deliver = api.send
    attempts = 0

    async def flaky_send(chat_id: int, text: str) -> int:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise telegram.TelegramError("sendMessage: ağ hatası", None)
        return await deliver(chat_id, text)

    api.send = flaky_send
    live = telegram.TelegramDraftStream(api, 123, telegram.TelegramStream(api, 123))
    presenter = telegram.CompactPresenter(live)
    with caplog.at_level("WARNING"):
        await presenter.event(PROVIDER_SWITCH)
    assert api.sent == []
    warning = next(
        record for record in caplog.records if record.getMessage() == "Yedek sağlayıcı bildirimi gönderilemedi"
    )
    assert warning.to_backend == "openai"
    await presenter.event(PROVIDER_SWITCH)
    await presenter.event(PROVIDER_SWITCH)
    assert len(api.sent) == 1 and "OpenAI" in api.sent[0] and attempts == 2


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
    # İlk ileti canlı iş günlüğüdür (kabuk adımı kod bloğunda); son yanıt ayrı iletidir.
    assert api.html_sent[0].startswith("💻 Kabuk\n<pre>")
    assert api.html_sent[-1] == '<b>Kalın</b> <a href="https://example.com">kaynak</a>'
    assert api.sent == []
    assert len({draft_id for draft_id, _ in api.drafts}) == 1


@pytest.mark.asyncio
async def test_draft_connection_error_does_not_abort_task() -> None:
    api = FakeAPI()
    fallback = telegram.TelegramStream(api, 123)
    live = telegram.TelegramDraftStream(api, 123, fallback)
    attempts = 0
    original = api.send_draft

    async def flaky_draft(
        chat_id: int, draft_id: int, rich_message: dict[str, str],
    ) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise telegram.TelegramError("sendRichMessageDraft: ağ veya yanıt hatası (ConnectError).")
        await original(chat_id, draft_id, rich_message)

    api.send_draft = flaky_draft
    await live.show("⏳ Düşünüyor…")
    assert live.native
    assert live.last_draft > 0
    assert api.sent == []
    live.last_draft = time.monotonic() - 2
    await live.tick()
    assert api.drafts[-1][1] == {"markdown": "⏳ Düşünüyor…"}
    await live.finish("Yanıt")
    assert api.html_sent == ["Yanıt"]


@pytest.mark.asyncio
async def test_old_bot_api_falls_back_to_existing_message_stream() -> None:
    api = FakeAPI()
    api.draft_error = 404
    fallback = telegram.TelegramStream(api, 123)
    live = telegram.TelegramDraftStream(api, 123, fallback)
    await live.show("⏳ Düşünüyor…")
    assert not live.native
    assert api.sent == ["⏳ Düşünüyor…"]
    await live.finish("**Yanıt**")
    assert api.html_edited == ["<b>Yanıt</b>"]


def test_legacy_markdown_formatter_escapes_input_and_formats_common_syntax() -> None:
    source = "# Başlık\n*   **OpenAI:** [haber](https://example.com?a=1&b=2)\n`kod <x>`"
    actual = telegram._legacy_markdown_html(source)
    assert "<b>Başlık</b>" in actual
    assert "• <b>OpenAI:</b>" in actual
    assert '<a href="https://example.com?a=1&amp;b=2">haber</a>' in actual
    assert "<code>kod &lt;x&gt;</code>" in actual
    assert "<x>" not in actual
    assert "\n\n" not in telegram._legacy_markdown_html("Özet\n\n\n**Başlık**")


@pytest.mark.asyncio
async def test_partial_markdown_uses_plain_draft_then_rich_final() -> None:
    api = FakeAPI()
    api.reject_partial_markdown = True
    live = telegram.TelegramDraftStream(api, 123, telegram.TelegramStream(api, 123))
    await live.show("**Yarım")
    assert live.native
    assert api.drafts[-1][1] == {"html": "**Yarım"}
    await live.finish("**Yarım**")
    assert api.html_sent == ["<b>Yarım</b>"]
    assert api.sent == []


@pytest.mark.asyncio
async def test_bot_api_rich_payload_contract() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path.rsplit("/", 1)[-1], __import__("json").loads(request.content)))
        result: Any = {"message_id": 9} if calls[-1][0] in ("sendRichMessage", "sendMessage") else True
        return httpx.Response(200, json={"ok": True, "result": result})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    api = telegram.TelegramAPI("secret-token", client)
    try:
        await api.send_draft(123, 7, {"html": "<tg-thinking>Düşünüyor…</tg-thinking>"})
        assert await api.send_html(123, "<b>Kalın</b>") == 9
        await api.edit_html(123, 9, "<b>Düzenle</b>")
    finally:
        await client.aclose()
    assert calls == [
        ("sendRichMessageDraft", {"chat_id": 123, "draft_id": 7,
                                  "rich_message": {"html": "<tg-thinking>Düşünüyor…</tg-thinking>"}}),
        ("sendMessage", {"chat_id": 123, "text": "<b>Kalın</b>", "parse_mode": "HTML"}),
        ("editMessageText", {"chat_id": 123, "message_id": 9,
                             "text": "<b>Düzenle</b>", "parse_mode": "HTML"}),
    ]


@pytest.mark.asyncio
async def test_bot_api_retries_connection_establishment_once() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("temporary connection failure", request=request)
        return httpx.Response(200, json={"ok": True, "result": {"id": 1}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    api = telegram.TelegramAPI("secret-token", client)
    try:
        assert await api.call("getMe", {}) == {"id": 1}
    finally:
        await client.aclose()
    assert attempts == 2


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


@pytest.mark.asyncio
async def test_bridge_reuses_integration_connections_between_tasks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    bridge = telegram.TelegramBridge(FakeAPI(), {"chat_id": 123, "user_id": 456})
    services: list[Any] = []

    async def fake_run(goal: str, emit: Any, options: Any, clients: Any) -> Any:
        services.append(options["integrations"])
        metrics = {"turns": 1, "tool_calls": 0, "elapsed_seconds": 0.1,
                   "backend": "ollama-cloud", "prompt_tokens": 1,
                   "cached_tokens": 0, "completion_tokens": 1}
        emit({"kind": "run_finished", "success": True, "outcome": goal,
              "reason": "", "metrics": metrics})
        return {"outcome": goal, "success": True, "reason": "",
                "metrics": metrics, "exchange": make_exchange(goal, goal, [])}

    monkeypatch.setattr(telegram, "run_agent_with_callback", fake_run)
    try:
        await bridge._execute("Birinci görev")
        await bridge._execute("İkinci görev")
        assert len(services) == 2
        assert services[0] is services[1] is bridge.integrations
        assert not bridge.integrations.closed
    finally:
        if bridge.integrations is not None:
            await bridge.integrations.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(("goal", "expected"), [
    ("Önde açık olan IDE daki ajanın kullanım limiti ne kadar kalmış kontrol eder misin", []),
    ("Ekran görüntüsü alıp gönderir misin", ["ikinci.png"]),
])
async def test_compact_sends_only_requested_final_screenshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, goal: str, expected: list[str],
) -> None:
    """Kısa görünüm modelin gözlem görüntülerini göndermez; görüntü istenirse yalnız sonuncusu bir kez gider."""
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    api = FakeAPI()
    bridge = telegram.TelegramBridge(api, {"chat_id": 123, "user_id": 456})
    images = [tmp_path / "birinci.png", tmp_path / "ikinci.png"]
    for image in images:
        image.write_bytes(b"PNG")

    async def fake_run(goal: str, emit: Any, options: Any, clients: Any) -> Any:
        for index, image in enumerate(images):
            emit({"kind": "tool_started", "call_id": f"ekran{index}", "index": 0,
                  "name": "take_screenshot", "preview": str(image)})
            emit({"kind": "tool_finished", "call_id": f"ekran{index}", "ok": True,
                  "text": "Ekran alındı", "seconds": 0.1})
        metrics = {
            "turns": 3, "tool_calls": 2, "elapsed_seconds": 1.0, "backend": "ollama-cloud",
            "prompt_tokens": 100, "cached_tokens": 0, "completion_tokens": 10,
        }
        emit({"kind": "run_finished", "success": True, "outcome": "Tamam.", "reason": "", "metrics": metrics})
        return {"outcome": "Tamam.", "success": True, "reason": "",
                "metrics": metrics, "exchange": make_exchange(goal, "Tamam.", [])}

    monkeypatch.setattr(telegram, "run_agent_with_callback", fake_run)
    await bridge.handle({"message": {
        "chat": {"id": 123, "type": "private"}, "from": {"id": 456}, "text": goal,
    }})
    task = bridge.active
    assert task is not None
    await task
    assert [path.name for path in api.photos] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(("goal", "written_in_workspace"), [
    ("Ekran görüntüsü alıp gönderir misin", True),
    ("Projedeki kodu düzelt ve ekran görüntüsü gönder", False),
])
async def test_bridge_reads_relative_screenshot_where_the_tool_wrote_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, goal: str, written_in_workspace: bool,
) -> None:
    """
    Araç göreli ekran görüntüsü adını kaynak görevi değilse workspace'e, kaynak görevinde süreç
    dizinine yazar; köprü fotoğrafı aynı yerden okumalı. Ajanın kendi otomatik gözlemi ve bitiş
    doğrulaması da take_screenshot olayıdır ama önizlemesi dosya adı değil etiket taşır: köprü
    onları kullanıcı görüntüsü sanıp gerçek fotoğrafı ezmemeli, sahte "bulunamadı" uyarısı yazmamalı.
    """
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path / "data"))
    process_dir = tmp_path / "proje"
    process_dir.mkdir()
    monkeypatch.chdir(process_dir)
    written = (workspace_dir() if written_in_workspace else process_dir) / "screen_view.png"
    written.parent.mkdir(parents=True, exist_ok=True)
    written.write_bytes(b"PNG")
    api = FakeAPI()
    bridge = telegram.TelegramBridge(api, {"chat_id": 123, "user_id": 456})

    async def fake_run(goal: str, emit: Any, options: Any, clients: Any) -> Any:
        emit({"kind": "tool_started", "call_id": "ekran", "index": 0,
              "name": "take_screenshot", "preview": "screen_view.png"})
        emit({"kind": "tool_finished", "call_id": "ekran", "ok": True,
              "text": "Ekran alındı", "seconds": 0.1})
        for index, label in enumerate(
            (main.AUTO_OBSERVATION_PREVIEW, main.VERIFICATION_OBSERVATION_PREVIEW), start=1,
        ):
            emit({"kind": "tool_started", "call_id": f"gozlem{index}", "index": index,
                  "name": "take_screenshot", "preview": label})
            emit({"kind": "tool_finished", "call_id": f"gozlem{index}", "ok": True,
                  "text": "Ekran alındı", "seconds": 0.1})
        metrics = {
            "turns": 2, "tool_calls": 1, "elapsed_seconds": 1.0, "backend": "ollama-cloud",
            "prompt_tokens": 100, "cached_tokens": 0, "completion_tokens": 10,
        }
        emit({"kind": "run_finished", "success": True, "outcome": "Tamam.", "reason": "", "metrics": metrics})
        return {"outcome": "Tamam.", "success": True, "reason": "",
                "metrics": metrics, "exchange": make_exchange(goal, "Tamam.", [])}

    monkeypatch.setattr(telegram, "run_agent_with_callback", fake_run)
    await bridge.handle({"message": {
        "chat": {"id": 123, "type": "private"}, "from": {"id": 456}, "text": goal,
    }})
    task = bridge.active
    assert task is not None
    await task
    assert [path.resolve() for path in api.photos] == [written.resolve()]
    posted: list[str] = [*api.sent, *api.edited, *api.html_sent, *api.html_edited]
    assert not any("bulunamadı" in text for text in posted)


@pytest.mark.asyncio
async def test_unresolvable_screenshot_path_does_not_abort_the_task(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """
    Model '~olmayan_kullanici/a.png' gibi çözülemeyen bir ekran görüntüsü yolu verirse araç tarafında bu
    kurtarılabilir bir hatadır; olay döngüsündeki RuntimeError Telegram görevini iptal etmemeli.
    """
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    api = FakeAPI()
    bridge = telegram.TelegramBridge(api, {"chat_id": 123, "user_id": 456})

    async def fake_run(goal: str, emit: Any, options: Any, clients: Any) -> Any:
        emit({"kind": "tool_started", "call_id": "ekran", "index": 0,
              "name": "take_screenshot", "preview": "~olmayan_kullanici_xyz/a.png"})
        emit({"kind": "tool_finished", "call_id": "ekran", "ok": False,
              "text": "Yol çözülemedi", "seconds": 0.1})
        metrics = {
            "turns": 2, "tool_calls": 1, "elapsed_seconds": 1.0, "backend": "ollama-cloud",
            "prompt_tokens": 100, "cached_tokens": 0, "completion_tokens": 10,
        }
        emit({"kind": "run_finished", "success": True, "outcome": "Tamam.", "reason": "", "metrics": metrics})
        return {"outcome": "Tamam.", "success": True, "reason": "",
                "metrics": metrics, "exchange": make_exchange(goal, "Tamam.", [])}

    monkeypatch.setattr(telegram, "run_agent_with_callback", fake_run)
    with caplog.at_level("WARNING"):
        await bridge.handle({"message": {
            "chat": {"id": 123, "type": "private"}, "from": {"id": 456}, "text": "Ekran görüntüsü alıp gönderir misin",
        }})
        task = bridge.active
        assert task is not None
        await task
    posted: list[str] = [*api.sent, *api.edited, *api.html_sent, *api.html_edited]
    assert "Tamam." in posted and not any("Görev hatası" in text for text in posted)
    warning = next(
        record for record in caplog.records if record.getMessage() == "Ekran görüntüsü yolu çözülemedi"
    )
    assert warning.error_type == "RuntimeError" and warning.call_id == "ekran"


@pytest.mark.asyncio
@pytest.mark.parametrize("verbose", [False, True], ids=["kompakt", "ayrıntılı"])
async def test_partial_report_notice_and_run_finished_show_the_report_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, verbose: bool,
) -> None:
    """
    Ajan, boş çıktılı başarısız görevin kısmi raporunu hem uyarı (masaüstü ve CLI run_finished çıktısını
    göstermez) hem run_finished çıktısı olarak yayınlar: Telegram iki görünümde de raporu tek kez göstermeli.
    """
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    api = FakeAPI()
    bridge = telegram.TelegramBridge(api, {"chat_id": 123, "user_id": 456})
    bridge.verbose = verbose
    marker = "RAPOR7431"
    report = f"Görev tamamlanamadı.\nBulgu: {marker}\nBulgular araç çıktılarından otomatik alındı."

    async def fake_run(goal: str, emit: Any, options: Any, clients: Any) -> Any:
        metrics = {
            "turns": 3, "tool_calls": 2, "elapsed_seconds": 1.0, "backend": "ollama-cloud",
            "prompt_tokens": 100, "cached_tokens": 0, "completion_tokens": 10,
        }
        emit({"kind": "notice", "level": "warning", "text": report})
        emit({"kind": "run_finished", "success": False, "outcome": report, "reason": "ilerleme yok", "metrics": metrics})
        return {"outcome": report, "success": False, "reason": "ilerleme yok",
                "metrics": metrics, "exchange": make_exchange(goal, report, [])}

    monkeypatch.setattr(telegram, "run_agent_with_callback", fake_run)
    await bridge.handle({"message": {
        "chat": {"id": 123, "type": "private"}, "from": {"id": 456}, "text": "Bir şey araştır",
    }})
    task = bridge.active
    assert task is not None
    await task
    # Kullanıcının sohbette gördüğü son hâl: ara düzenlemeler değil, her iletinin son metni
    final_pages = [*api.html_sent, *(api.edited[-1:] or api.sent[-1:])]
    assert sum(page.count(marker) for page in final_pages) == 1, final_pages
