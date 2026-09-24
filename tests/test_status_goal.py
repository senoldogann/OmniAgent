"""Koşullu durum hedefinin gözlemle tamamlandığını doğrular."""
import json
from pathlib import Path
from typing import Any

import pytest

import main
import state_manager as sm


def test_status_goal_uses_last_successful_json_observation() -> None:
    goal = "status 'ready' olana kadar kontrol et"
    pending = sm.make_step_record("fetch_raw", "{}", True, '{"status":"pending"}')
    ready = sm.make_step_record("fetch_raw", "{}", True, '{"status":"ready"}')
    assert "pending" in str(main.unmet_wait_status(goal, [pending]))
    assert main.unmet_wait_status(goal, [pending, ready]) is None
    assert main.unmet_wait_status("Mevcut durumu raporla", [pending]) is None


@pytest.mark.asyncio
async def test_agent_does_not_report_success_when_wait_condition_is_pending(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    calls = 0

    async def fake_model(
        clients: Any, messages: Any, schemas: Any, session_id: str, backend: str,
        emit: Any, should_stop: Any,
    ) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            return {
                "content": "STATE:\nFACTS: status pending\nREMAINING: ready bekle",
                "tool_calls": [{
                    "id": "read", "name": "fetch_raw",
                    "arguments": json.dumps({"url": "https://example.test/status"}),
                }],
                "finish_reason": "tool_calls", "usage": main.ZERO_USAGE,
            }, backend
        return {
            "content": "Durum hâlâ pending.", "tool_calls": [],
            "finish_reason": "stop", "usage": main.ZERO_USAGE,
        }, backend

    async def fake_tools(calls: Any, toolbox: Any, cache: Any, emit: Any, stop: Any) -> Any:
        return [{"ok": True, "result": '{"status":"pending"}'}]

    monkeypatch.setattr(main, "_call_model_with_retries", fake_model)
    monkeypatch.setattr(main, "_execute_tool_calls", fake_tools)
    events: list[dict[str, Any]] = []
    report = await main.run_agent_with_callback(
        "https://example.test/status adresinde status 'ready' olana kadar kontrol et",
        events.append,
        {"requested_backend": "ollama-cloud", "should_stop": lambda: False,
         "state_file": str(tmp_path / "memory.json"), "history": []},
        {"ollama-cloud": object()},
    )
    assert not report["success"]
    assert "son doğrulanan status pending" in [
        event for event in events if event["kind"] == "run_finished"
    ][0]["reason"]
