"""İş devri: ilerleme olayları (işçi iş parçacığından da) sırayla gelir, onay kanalı açık kalır, host kilidi tutulur."""
import asyncio
import threading
from pathlib import Path
from typing import Callable, Dict, List

import pytest
from openai import AsyncOpenAI

from omniagent.app.types import RunOptions, RunReport
from omniagent.companion import delegate
from omniagent.core.conversation import make_exchange
from omniagent.core.events import AgentEvent
from omniagent.integrations.capabilities import CapabilityService
from omniagent.platform.macos.host_lock import HostBusyError, host_task_lock


def report_for(goal: str, outcome: str, success: bool) -> RunReport:
    return {"outcome": outcome, "success": success, "reason": "",
            "metrics": {"turns": 1, "tool_calls": 1, "elapsed_seconds": 0.1, "backend": "openai",
                        "prompt_tokens": 120, "cached_tokens": 0, "completion_tokens": 30,
                        "model_seconds": 0.1, "tool_seconds": 0.0},
            "exchange": make_exchange(goal, outcome, [])}


async def approve(title: str, fields: Dict[str, object]) -> Dict[str, object]:
    return {"approved": True}


async def keep(path: Path, caption: str) -> None:
    return None


@pytest.mark.asyncio
async def test_task_streams_progress_and_keeps_approval_channel(tmp_path: Path,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(delegate, "create_model_clients", lambda: {})
    seen: List[RunOptions] = []

    async def fake_run(goal: str, emit: Callable[[AgentEvent], None], options: RunOptions,
                       clients: Dict[str, AsyncOpenAI]) -> RunReport:
        seen.append(options)
        emit({"kind": "tool_started", "call_id": "1", "index": 0, "name": "execute_shell", "preview": "ls ~/Desktop"})
        worker = threading.Thread(target=lambda: emit(
            {"kind": "tool_finished", "call_id": "1", "ok": True, "text": "a.pdf\nb.pdf", "seconds": 0.1}))
        worker.start()
        worker.join()
        with pytest.raises(HostBusyError):
            with host_task_lock():
                pass
        return report_for(goal, "2 dosya var", True)

    monkeypatch.setattr(delegate, "run_agent_with_callback", fake_run)
    integrations = CapabilityService()
    lines: List[str] = []
    try:
        options = delegate.run_options(approve, keep, [], [], lambda: False, integrations)
        outcome = await delegate.run_task("masaüstünü listele", options, lines.append)
        await asyncio.sleep(0)
    finally:
        await integrations.close()
    assert lines == ["execute_shell: ls ~/Desktop", "tamam: a.pdf b.pdf"]
    assert outcome["tokens"] == 150 and outcome["report"]["success"]
    assert "unattended" not in seen[0] and seen[0]["answer"] is approve


@pytest.mark.asyncio
async def test_busy_host_is_reported_without_running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(delegate, "create_model_clients", lambda: {})

    async def must_not_run(goal: str, emit: Callable[[AgentEvent], None], options: RunOptions,
                           clients: Dict[str, AsyncOpenAI]) -> RunReport:
        raise AssertionError("kilit meşgulken ajan çalışmamalı")

    monkeypatch.setattr(delegate, "run_agent_with_callback", must_not_run)
    integrations = CapabilityService()
    ignored: List[str] = []
    try:
        options = delegate.run_options(approve, keep, [], [], lambda: False, integrations)
        with host_task_lock():
            with pytest.raises(HostBusyError):
                await delegate.run_task("ekran görüntüsü al", options, ignored.append)
    finally:
        await integrations.close()
