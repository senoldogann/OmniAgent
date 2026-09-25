"""Sesli mesajların yazıya çevrilmesi: OpenAI uç noktası, model düşüşü ve Telegram köprüsü akışı."""
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import pytest
from openai import AsyncOpenAI

from omniagent.core.conversation import make_exchange
from omniagent.integrations import telegram, transcription


def _voice(tmp_path: Path) -> Path:
    path = tmp_path / "20260925-090000-ab12-file_7.oga"
    path.write_bytes(b"OggS" + b"\x00" * 64)
    return path


def _factory(handler: Any, requests: List[httpx.Request]) -> Any:
    def recorded(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    def build(api_key: str, base_url: str) -> AsyncOpenAI:
        assert api_key == "sk-test" and base_url.startswith("https://")
        return AsyncOpenAI(api_key=api_key, base_url=base_url, max_retries=0,
                           http_client=httpx.AsyncClient(transport=httpx.MockTransport(recorded)))
    return build


def _model(request: httpx.Request) -> str:
    body = request.read().decode("latin-1")
    return body.split('name="model"\r\n\r\n', 1)[1].split("\r\n", 1)[0]


@pytest.fixture
def with_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transcription, "load_api_key", lambda variable: "sk-test")
    monkeypatch.delenv("OMNI_TRANSCRIBE_MODEL", raising=False)


@pytest.mark.asyncio
async def test_voice_is_uploaded_as_ogg_with_the_recommended_model(tmp_path: Path, with_key: None) -> None:
    requests: List[httpx.Request] = []
    handler = lambda request: httpx.Response(200, json={"text": "  Masaüstündeki   dosyaları listele "})
    text = await transcription.transcribe_audio(_voice(tmp_path), _factory(handler, requests))
    assert text == "Masaüstündeki dosyaları listele"
    assert requests[0].url.path.endswith("/audio/transcriptions")
    assert _model(requests[0]) == "gpt-transcribe"
    assert 'filename="20260925-090000-ab12-file_7.ogg"' in requests[0].read().decode("latin-1")


@pytest.mark.asyncio
async def test_unknown_model_falls_back_to_whisper(tmp_path: Path, with_key: None) -> None:
    requests: List[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if _model(request) == "gpt-transcribe":
            return httpx.Response(404, json={"error": {"message": "model not found", "code": "model_not_found"}})
        return httpx.Response(200, json={"text": "yarın saat dokuzda hatırlat"})

    assert await transcription.transcribe_audio(_voice(tmp_path), _factory(handler, requests)) == "yarın saat dokuzda hatırlat"
    assert [_model(request) for request in requests] == ["gpt-transcribe", "whisper-1"]


@pytest.mark.asyncio
async def test_auth_errors_and_silence_are_reported_without_retry(tmp_path: Path, with_key: None) -> None:
    requests: List[httpx.Request] = []
    unauthorized = lambda request: httpx.Response(401, json={"error": {"message": "bad key"}})
    with pytest.raises(transcription.TranscriptionFailed) as error:
        await transcription.transcribe_audio(_voice(tmp_path), _factory(unauthorized, requests))
    assert "401" in str(error.value) and "sk-test" not in str(error.value)
    assert len(requests) == 1
    silent = lambda request: httpx.Response(200, json={"text": "   "})
    with pytest.raises(transcription.TranscriptionFailed, match="anlaşılır konuşma"):
        await transcription.transcribe_audio(_voice(tmp_path), _factory(silent, []))


@pytest.mark.asyncio
async def test_without_openai_key_nothing_is_sent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transcription, "load_api_key", lambda variable: None)
    with pytest.raises(transcription.TranscriptionUnavailable, match="OpenAI API anahtarı"):
        await transcription.transcribe_audio(_voice(tmp_path), lambda key, url: pytest.fail("istemci kurulmamalı"))


def test_model_order_respects_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNI_TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe")
    assert transcription.transcribe_models() == ("gpt-4o-mini-transcribe", "whisper-1")
    monkeypatch.setenv("OMNI_TRANSCRIBE_MODEL", "whisper-1")
    assert transcription.transcribe_models() == ("whisper-1",)


class VoiceAPI:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.sent: List[str] = []

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
        return self.path


def _voice_message(caption: Optional[str] = None) -> Dict[str, Any]:
    message: Dict[str, Any] = {"chat": {"id": 123, "type": "private"}, "from": {"id": 456},
                               "voice": {"file_id": "v1", "file_size": 68}}
    if caption is not None:
        message["caption"] = caption
    return {"message": message}


def _recorder(goals: List[str]) -> Any:
    async def run(goal: str, emit: Any, options: Any, clients: Any) -> Any:
        goals.append(goal)
        metrics = {"turns": 1, "tool_calls": 0, "elapsed_seconds": 0.1, "backend": "ollama-cloud",
                   "prompt_tokens": 1, "cached_tokens": 0, "completion_tokens": 1}
        emit({"kind": "run_finished", "success": True, "outcome": "tamam", "reason": "", "metrics": metrics})
        return {"outcome": "tamam", "success": True, "reason": "", "metrics": metrics,
                "exchange": make_exchange(goal, "tamam", [])}
    return run


@pytest.mark.asyncio
async def test_voice_command_is_transcribed_echoed_and_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    goals: List[str] = []
    monkeypatch.setattr(telegram, "run_agent_with_callback", _recorder(goals))

    async def transcribe(path: Path) -> str:
        return "Masaüstündeki dosyaları listele"

    monkeypatch.setattr(telegram, "transcribe_audio", transcribe)
    api = VoiceAPI(_voice(tmp_path))
    bridge = telegram.TelegramBridge(api, {"chat_id": 123, "user_id": 456})
    await bridge.handle(_voice_message())
    assert bridge.active is not None
    await bridge.active
    assert goals == ["Masaüstündeki dosyaları listele"]
    assert "🎙️ Anlaşılan: Masaüstündeki dosyaları listele" in api.sent


@pytest.mark.asyncio
async def test_voice_without_transcription_path_explains_and_does_not_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    goals: List[str] = []
    monkeypatch.setattr(telegram, "run_agent_with_callback", _recorder(goals))

    async def unavailable(path: Path) -> str:
        raise transcription.TranscriptionUnavailable("OpenAI API anahtarı gerekli.")

    monkeypatch.setattr(telegram, "transcribe_audio", unavailable)
    api = VoiceAPI(_voice(tmp_path))
    bridge = telegram.TelegramBridge(api, {"chat_id": 123, "user_id": 456})
    await bridge.handle(_voice_message())
    assert bridge.active is None and goals == []
    assert api.sent[-1].startswith("🎙️ OpenAI API anahtarı gerekli.")


@pytest.mark.asyncio
async def test_voice_with_caption_uses_caption_without_transcribing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    goals: List[str] = []
    monkeypatch.setattr(telegram, "run_agent_with_callback", _recorder(goals))
    monkeypatch.setattr(telegram, "transcribe_audio", lambda path: pytest.fail("yazıya çevrilmemeli"))
    api = VoiceAPI(_voice(tmp_path))
    bridge = telegram.TelegramBridge(api, {"chat_id": 123, "user_id": 456})
    await bridge.handle(_voice_message("Bu kaydı masaüstüne kaydet"))
    assert bridge.active is not None
    await bridge.active
    assert goals[0].startswith("Bu kaydı masaüstüne kaydet") and "sesli mesaj" in goals[0]
