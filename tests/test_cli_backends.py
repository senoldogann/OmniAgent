"""Oturumlu Codex ve ücretsiz OpenCode bağlayıcısının regresyon testleri."""
import base64
import io
import json
from pathlib import Path
from typing import Any, Dict, List

import pytest
from PIL import Image

import cli_backends
import main


def _image_url() -> str:
    """Küçük sentetik görseli veri URL'sine çevirir."""
    frame = Image.new("RGB", (8, 8), "red")
    buffer = io.BytesIO()
    frame.save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,model", [
    ("codex", "gpt-6-luna"),
    ("opencode", "muse-spark-1.3-contributor-free"),
])
async def test_cli_model_uses_isolated_client_and_keeps_images(
    monkeypatch: pytest.MonkeyPatch, provider: str, model: str,
) -> None:
    """Görsel eklenir, istem metni dosya argümanı sanılmaz; yerel araç izinleri reddedilir."""
    seen: Dict[str, Any] = {}

    async def fake_process(command, env, input_text, should_stop):
        seen["command"] = command
        seen["input"] = input_text
        if provider == "opencode":
            config = json.loads(Path(env["OPENCODE_CONFIG"]).read_text())
            assert config["agent"]["build"]["permission"]["*"] == "deny"
            prompt_index = next(index for index, arg in enumerate(command) if "KONUŞMA:" in arg)
            assert command.index("-f") > prompt_index
            assert command[-1].endswith(".png")
            output = [
                {"type": "text", "part": {"text": '{"content":"Kırmızı","tool_calls":[]}' }},
                {"type": "step_finish", "part": {"tokens": {
                    "input": 20, "output": 5, "cache": {"read": 10}
                }}},
            ]
        else:
            schema = json.loads(Path(command[command.index("--output-schema") + 1]).read_text())
            assert schema["properties"]["tool_calls"]["type"] == "array"
            assert command[-1] == "-" and input_text is not None
            assert "-i" in command
            output = [
                {"type": "item.completed", "item": {
                    "type": "agent_message", "text": '{"content":"Kırmızı","tool_calls":[]}'
                }},
                {"type": "turn.completed", "usage": {
                    "input_tokens": 30, "cached_input_tokens": 10, "output_tokens": 5
                }},
            ]
        return "\n".join(json.dumps(event) for event in output).encode(), b"", 0, False

    monkeypatch.setattr(cli_backends, "_process", fake_process)
    messages: List[Dict[str, Any]] = [{
        "role": "user",
        "content": [
            {"type": "text", "text": "Renk?"},
            {"type": "image_url", "image_url": {"url": _image_url()}},
        ],
    }]
    result = await cli_backends.run_cli_model(provider, model, messages, [], lambda event: None, lambda: False)
    assert result["content"] == "Kırmızı"
    assert result["tool_calls"] == []
    assert result["usage"]["prompt_tokens"] == 30


@pytest.mark.asyncio
async def test_cli_model_tool_calls_preserve_json_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI metni gerçek OmniAgent araç çağrısına çevrilir."""
    async def fake_process(command, env, input_text, should_stop):
        answer = {"content": "STATE: hesaplanıyor", "tool_calls": [
            {"name": "execute_js", "arguments": '{"code":"console.log(10)"}'}
        ]}
        line = json.dumps({"type": "item.completed", "item": {
            "type": "agent_message", "text": json.dumps(answer)
        }})
        return line.encode(), b"", 0, False

    monkeypatch.setattr(cli_backends, "_process", fake_process)
    emitted = []
    result = await cli_backends.run_cli_model(
        "codex", "gpt-6-luna", [{"role": "user", "content": "Topla"}], [],
        emitted.append, lambda: False,
    )
    assert result["finish_reason"] == "tool_calls"
    assert result["tool_calls"][0]["name"] == "execute_js"
    assert json.loads(result["tool_calls"][0]["arguments"]) == {"code": "console.log(10)"}
    assert emitted == [{"kind": "text_delta", "text": "STATE: hesaplanıyor"}]


def test_cli_model_fallback_prefers_available_unbilled_route() -> None:
    """Luna sınırına ulaştığında Muse; Muse çalışmazsa Luna denenir."""
    available = frozenset({"openai", "zen-free"})
    assert main.attempt_plan("openai", available)[-1] == "zen-free"
    assert main.attempt_plan("zen-free", available)[-1] == "openai"


@pytest.mark.asyncio
async def test_quota_error_skips_same_paid_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """402 durumunda aynı paralı modele tekrar gitmeden ücretsiz modele geçilir."""
    attempts: List[str] = []

    class QuotaError(Exception):
        status_code = 402

    async def fake_stream(client, profile, messages, schemas, session_id, emit, should_stop):
        attempts.append(profile["model"])
        if profile["model"] == "qwen3.8-flash":
            raise QuotaError("bakiye yok")
        return {"content": "tamam", "tool_calls": [], "finish_reason": "stop",
                "usage": main.ZERO_USAGE}

    monkeypatch.setattr(main, "APIStatusError", QuotaError)
    monkeypatch.setattr(main, "_stream_completion", fake_stream)
    turn, backend = await main._call_model_with_retries(
        {"opencode": object(), "zen-free": None}, [], [], "oturum",
        "opencode", lambda event: None, lambda: False,
    )
    assert turn["content"] == "tamam" and backend == "zen-free"
    assert attempts == ["qwen3.8-flash", "muse-spark-1.3-contributor-free"]
