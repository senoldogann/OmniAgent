"""Kod değişikliği istenen görevlerde plan metninin başarı sayılmasını önler."""
import json
from pathlib import Path
from typing import Any

import pytest

import main
from capabilities import CapabilityService
from conversation import make_exchange


def test_source_change_intent_inherits_short_followup() -> None:
    history = [make_exchange(
        "İkinci monitör desteğini ekle",
        "tools.py kodunu değiştirip test edeceğim",
        [],
    )]
    assert main.source_change_expected("tamam başla.", history)
    assert main.source_change_expected("evet dene", history)
    assert main.source_change_expected("o halde implement et", history)
    assert main.source_change_expected("Monitör desteği ekle", [])
    assert not main.source_change_expected("Bu kod nasıl çalışıyor?", [])
    assert not main.source_change_expected("Dosyayı oku", [])


@pytest.mark.asyncio
async def test_plan_only_source_task_is_not_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    history = [make_exchange("Özelliği ekle", "tools.py kodunu değiştireceğim", [])]
    calls: list[int] = []

    async def fake_model(
        clients: Any, messages: Any, schemas: Any, session_id: str,
        backend: str, emit: Any, should_stop: Any,
    ) -> tuple[dict[str, Any], str]:
        calls.append(len(messages))
        if len(calls) == 2:
            assert "hiçbir write_file" in str(messages[-1]["content"])
        return {
            "content": "Plan hazır; başlayabilirim.",
            "tool_calls": [], "finish_reason": "stop", "usage": main.ZERO_USAGE,
        }, backend

    monkeypatch.setattr(main, "_call_model_with_retries", fake_model)
    service = CapabilityService(tmp_path)
    try:
        report = await main.run_agent_with_callback(
            "tamam başla.", lambda event: None,
            {"requested_backend": None, "should_stop": lambda: False,
             "state_file": str(tmp_path / "memory.json"), "history": history,
             "integrations": service},
            {"ollama-cloud": object()},
        )
    finally:
        await service.close()
    assert not report["success"]
    assert "hiçbir dosya" in report["reason"]
    assert report["metrics"]["turns"] == 2


@pytest.mark.asyncio
async def test_source_task_succeeds_after_real_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    history = [make_exchange("Özelliği ekle", "tools.py kodunu değiştireceğim", [])]
    target = tmp_path / "new_feature.py"
    calls: list[int] = []

    async def fake_model(
        clients: Any, messages: Any, schemas: Any, session_id: str,
        backend: str, emit: Any, should_stop: Any,
    ) -> tuple[dict[str, Any], str]:
        calls.append(len(messages))
        if len(calls) == 1:
            return {
                "content": "Plan hazır.",
                "tool_calls": [], "finish_reason": "stop", "usage": main.ZERO_USAGE,
            }, backend
        if len(calls) == 2:
            return {
                "content": "", "tool_calls": [{
                    "id": "write-1", "name": "write_file",
                    "arguments": json.dumps({"path": str(target), "content": "VALUE = 1\n"}),
                }],
                "finish_reason": "tool_calls", "usage": main.ZERO_USAGE,
            }, backend
        return {
            "content": "Kod yazıldı.",
            "tool_calls": [], "finish_reason": "stop", "usage": main.ZERO_USAGE,
        }, backend

    monkeypatch.setattr(main, "_call_model_with_retries", fake_model)
    service = CapabilityService(tmp_path)
    try:
        report = await main.run_agent_with_callback(
            "tamam başla.", lambda event: None,
            {"requested_backend": None, "should_stop": lambda: False,
             "state_file": str(tmp_path / "memory.json"), "history": history,
             "integrations": service},
            {"ollama-cloud": object()},
        )
    finally:
        await service.close()
    assert report["success"]
    assert target.read_text(encoding="utf-8") == "VALUE = 1\n"
    assert report["metrics"]["turns"] == 3
