"""Oturumlu Codex ve ücretsiz OpenCode bağlayıcısının regresyon testleri."""
import asyncio
import base64
import io
import json
import os
import sys
import time
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
            assert input_text is not None and "KONUŞMA:" in input_text
            assert all("KONUŞMA:" not in arg for arg in command)
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
@pytest.mark.parametrize("status", [401, 402, 403])
async def test_access_error_skips_same_paid_backend(
    monkeypatch: pytest.MonkeyPatch, status: int,
) -> None:
    """Kimlik/izin/bakiye hatasında aynı sağlayıcıyı beklemeden yedeğe geçilir."""
    attempts: List[str] = []

    class AccessError(Exception):
        status_code = status

    async def fake_stream(client, profile, messages, schemas, session_id, emit, should_stop):
        attempts.append(profile["model"])
        if profile["model"] == "qwen3.8-flash":
            raise AccessError("erişim yok")
        return {"content": "tamam", "tool_calls": [], "finish_reason": "stop",
                "usage": main.ZERO_USAGE}

    monkeypatch.setattr(main, "APIStatusError", AccessError)
    monkeypatch.setattr(main, "_stream_completion", fake_stream)
    turn, backend = await main._call_model_with_retries(
        {"opencode": object(), "zen-free": None}, [], [], "oturum",
        "opencode", lambda event: None, lambda: False,
    )
    assert turn["content"] == "tamam" and backend == "zen-free"
    assert attempts == ["qwen3.8-flash", "muse-spark-1.3-contributor-free"]


@pytest.mark.asyncio
@pytest.mark.parametrize("request_stop", [False, True])
async def test_cli_process_timeout_and_stop_reap_child(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, request_stop: bool,
) -> None:
    """Süre sınırı ve Esc, alt süreci açık bırakmadan hızlıca sonlandırır."""
    pid_file = tmp_path / "child.pid"
    script = (
        "import os,sys,time; "
        "from pathlib import Path; "
        "Path(sys.argv[1]).write_text(str(os.getpid())); "
        "time.sleep(30)"
    )
    monkeypatch.setattr(cli_backends, "TIMEOUT_SECONDS", 5.0 if request_stop else 0.2)
    started = time.monotonic()
    should_stop = lambda: request_stop and time.monotonic() - started >= 0.2
    _, _, _, stopped = await cli_backends._process(
        [sys.executable, "-c", script, str(pid_file)], dict(os.environ), None, should_stop,
    )
    assert stopped == request_stop
    assert time.monotonic() - started < 3.0
    child_pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


@pytest.mark.asyncio
async def test_cli_task_cancellation_reaps_child(tmp_path: Path) -> None:
    """Event loop iptali de alt süreci bırakmaz."""
    pid_file = tmp_path / "cancelled.pid"
    script = (
        "import os,sys,time; "
        "from pathlib import Path; "
        "Path(sys.argv[1]).write_text(str(os.getpid())); "
        "time.sleep(30)"
    )
    task = asyncio.create_task(cli_backends._process(
        [sys.executable, "-c", script, str(pid_file)], dict(os.environ), None, lambda: False,
    ))
    for _ in range(30):
        if pid_file.exists():
            break
        await asyncio.sleep(0.05)
    assert pid_file.exists()
    child_pid = int(pid_file.read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)
