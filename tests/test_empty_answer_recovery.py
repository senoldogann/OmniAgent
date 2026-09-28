"""Boş model yanıtının tek başına görevi bitirmesini önleyen sınırlı kurtarma testleri.

28 Eylül başsız Chrome ölçümünde başarısız koşu araçsız, tamamen boş bir model yanıtıyla
bitiyordu. Boş yanıt sağlayıcı aksaklığı da olabileceği için host bir kez gerçek yanıt veya
araç çağrısı ister; hâlâ boş gelirse görev doğrulanmamış sayılır.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, List

import pytest

from omniagent.app import agent as main

GOAL: str = "Kısaca durumu özetle"
ZERO: dict = main.ZERO_USAGE


def _empty() -> dict:
    return {"content": "", "tool_calls": [], "finish_reason": "stop", "usage": ZERO}


def _answer(text: str) -> dict:
    return {"content": text, "tool_calls": [], "finish_reason": "stop", "usage": ZERO}


async def _run(
    monkeypatch: pytest.MonkeyPatch, script: List[dict], tmp_path: Path, events: List[dict],
) -> Any:
    calls: List[int] = []

    async def fake_model(
        clients: Any, messages: Any, schemas: Any, session_id: str,
        backend: str, emit: Any, should_stop: Any,
    ) -> tuple[dict, str]:
        calls.append(len(messages))
        if len(calls) > len(script):
            return _empty(), backend
        return script[len(calls) - 1], backend

    monkeypatch.setattr(main, "_call_model_with_retries", fake_model)
    return await main.run_agent_with_callback(
        GOAL, events.append,
        {"requested_backend": None, "should_stop": lambda: False,
         "state_file": str(tmp_path / "memory.json"), "history": []},
        {"ollama-cloud": object()},
    )


@pytest.mark.asyncio
async def test_one_empty_answer_is_recovered_and_task_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """İlk tur boş, ikinci tur gerçek yanıt: görev başarıyla ve iki turda biter."""
    events: List[dict] = []
    report = await _run(monkeypatch, [_empty(), _answer("Durum özeti hazır.")], tmp_path, events)

    assert report["success"]
    assert report["metrics"]["turns"] == 2
    assert report["outcome"] == "Durum özeti hazır."
    assert any(
        event["kind"] == "notice" and "boş yanıt" in event["text"].casefold()
        for event in events
    )


@pytest.mark.asyncio
async def test_repeated_empty_answers_end_as_unverified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kurtarma hakkı bitince hâlâ boş yanıt gelirse görev doğrulanmamış sayılır; sonsuz tekrar yok."""
    events: List[dict] = []
    report = await _run(monkeypatch, [_empty(), _empty(), _empty()], tmp_path, events)

    assert not report["success"]
    assert report["reason"] == "model boş yanıt döndü"
    assert report["metrics"]["turns"] == 2
