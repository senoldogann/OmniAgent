"""Metne dökülen sahte araç çağrılarının gerçekten çalışmış sayılmasını önler."""
import json
from pathlib import Path
from typing import Any

import pytest

import main
from capabilities import CapabilityService


@pytest.mark.asyncio
async def test_textual_tool_call_cannot_finish_as_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    async def fake_model(
        clients: Any, messages: Any, schemas: Any, session_id: str,
        backend: str, emit: Any, should_stop: Any,
    ) -> tuple[dict[str, Any], str]:
        calls.append(len(messages))
        if len(calls) == 1:
            return {
                "content": '⏺ <tool_call|>call:execute_js{code:"console.log(1)"}',
                "tool_calls": [], "finish_reason": "stop", "usage": main.ZERO_USAGE,
            }, backend
        assert "hiçbir araç çalışmadı" in str(messages[-1]["content"])
        return {
            "content": "Tamamlandı", "tool_calls": [], "finish_reason": "stop",
            "usage": main.ZERO_USAGE,
        }, backend

    monkeypatch.setattr(main, "_call_model_with_retries", fake_model)
    service = CapabilityService(tmp_path)
    events: list[dict[str, Any]] = []
    try:
        report = await main.run_agent_with_callback(
            "JavaScript komutunu çalıştır", events.append,
            {"requested_backend": None, "should_stop": lambda: False,
             "state_file": str(tmp_path / "memory.json"), "history": [], "integrations": service},
            {"ollama-cloud": object()},
        )
    finally:
        await service.close()
    assert not report["success"]
    assert "gerçek araç çağrısı yapılmadı" in report["reason"]
    assert report["metrics"]["tool_calls"] == 0
    assert report["metrics"]["turns"] == 2
    assert events[-1]["kind"] == "run_finished"


@pytest.mark.asyncio
async def test_textual_tool_call_recovery_accepts_real_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "probe.txt"
    target.write_text("ok", encoding="utf-8")
    calls: list[int] = []

    async def fake_model(
        clients: Any, messages: Any, schemas: Any, session_id: str,
        backend: str, emit: Any, should_stop: Any,
    ) -> tuple[dict[str, Any], str]:
        calls.append(len(messages))
        if len(calls) == 1:
            return {
                "content": '⏺ <tool_call|>call:read_file{path:"probe.txt"}',
                "tool_calls": [], "finish_reason": "stop", "usage": main.ZERO_USAGE,
            }, backend
        if len(calls) == 2:
            return {
                "content": "", "tool_calls": [{
                    "id": "real-read", "name": "read_file",
                    "arguments": json.dumps({"path": str(target)}),
                }], "finish_reason": "tool_calls", "usage": main.ZERO_USAGE,
            }, backend
        return {
            "content": "Dosya okundu", "tool_calls": [], "finish_reason": "stop",
            "usage": main.ZERO_USAGE,
        }, backend

    monkeypatch.setattr(main, "_call_model_with_retries", fake_model)
    service = CapabilityService(tmp_path)
    try:
        report = await main.run_agent_with_callback(
            "Dosyayı oku", lambda event: None,
            {"requested_backend": None, "should_stop": lambda: False,
             "state_file": str(tmp_path / "memory.json"), "history": [], "integrations": service},
            {"ollama-cloud": object()},
        )
    finally:
        await service.close()
    assert report["success"]
    assert report["metrics"]["tool_calls"] == 1
    assert report["metrics"]["turns"] == 3
