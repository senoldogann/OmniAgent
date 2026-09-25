"""Model erişim hatasında aynı profilin her turda yeniden denenmesini önler."""
import json
from pathlib import Path
from typing import Any

import pytest

from omniagent.app import agent as main
from omniagent.integrations.runtime import CURRENT_RUNTIME, IntegrationRuntime


@pytest.mark.asyncio
async def test_access_failure_is_quarantined_for_rest_of_task(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    attempts: list[str] = []

    class QuotaError(Exception):
        status_code = 402

    async def fake_stream(
        client: Any, profile: Any, messages: Any, schemas: Any, session_id: str,
        emit: Any, should_stop: Any,
    ) -> Any:
        attempts.append(profile["provider"])
        if profile["provider"] == "ollama-cloud":
            raise QuotaError("kota doldu")
        return {
            "content": "tamam", "tool_calls": [], "finish_reason": "stop",
            "usage": main.ZERO_USAGE,
        }

    monkeypatch.setattr(main, "APIStatusError", QuotaError)
    monkeypatch.setattr(main, "_stream_completion", fake_stream)
    runtime = IntegrationRuntime(lambda event: None, lambda: False)
    token = CURRENT_RUNTIME.set(runtime)
    try:
        clients = {"ollama-cloud": object(), "openai": None}
        first, used = await main._call_model_with_retries(
            clients, [], [], "oturum", "ollama-cloud", lambda event: None, lambda: False,
        )
        assert first["content"] == "tamam" and used == "openai"
        assert runtime.blocked_backends == {"ollama-cloud"}
        second, used = await main._call_model_with_retries(
            clients, [], [], "oturum", "openai", lambda event: None, lambda: False,
        )
        assert second["content"] == "tamam" and used == "openai"
        assert attempts == ["ollama-cloud", "openai", "openai"]
    finally:
        CURRENT_RUNTIME.reset(token)


@pytest.mark.asyncio
async def test_runner_persists_access_fallback_without_restarting_original(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    target = tmp_path / "data.txt"
    target.write_text("gözlem", encoding="utf-8")
    requested: list[str] = []

    async def fake_model(
        clients: Any, messages: Any, schemas: Any, session_id: str,
        backend: str, emit: Any, should_stop: Any,
    ) -> Any:
        requested.append(backend)
        if len(requested) == 1:
            runtime = CURRENT_RUNTIME.get()
            assert runtime is not None
            runtime.blocked_backends.add("ollama-cloud")
            return {
                "content": "STATE: dosya okunacak",
                "tool_calls": [{"id": "one", "name": "read_file",
                                "arguments": json.dumps({"path": str(target)})}],
                "finish_reason": "tool_calls", "usage": main.ZERO_USAGE,
            }, "openai"
        return {
            "content": "gözlem okundu", "tool_calls": [], "finish_reason": "stop",
            "usage": main.ZERO_USAGE,
        }, "openai"

    monkeypatch.setattr(main, "_call_model_with_retries", fake_model)
    events: list[Any] = []
    report = await main.run_agent_with_callback(
        "Dosyayı oku", events.append,
        {"requested_backend": "ollama-cloud", "should_stop": lambda: False,
         "state_file": str(tmp_path / "state.json"), "history": []},
        {"ollama-cloud": object(), "openai": None},
    )
    assert report["success"]
    assert requested == ["ollama-cloud", "openai"]
    assert report["metrics"]["backend"] == "openai"
    changed = [event for event in events if event["kind"] == "backend_changed"]
    assert changed and "yeniden denenmeyecek" in changed[0]["reason"]


@pytest.mark.asyncio
async def test_retry_after_uses_other_provider_without_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[str] = []

    class RateError(Exception):
        status_code = 429
        response = type("Response", (), {"headers": {"retry-after": "120"}})()

    async def fake_stream(
        client: Any, profile: Any, messages: Any, schemas: Any, session_id: str,
        emit: Any, should_stop: Any,
    ) -> Any:
        attempts.append(profile["provider"])
        if profile["provider"] == "ollama-cloud":
            raise RateError("bekle")
        return {"content": "tamam", "tool_calls": [], "finish_reason": "stop",
                "usage": main.ZERO_USAGE}

    async def fail_sleep(seconds: float) -> None:
        raise AssertionError(f"Gereksiz bekleme: {seconds}")

    monkeypatch.setattr(main, "APIStatusError", RateError)
    monkeypatch.setattr(main, "_stream_completion", fake_stream)
    monkeypatch.setattr(main.asyncio, "sleep", fail_sleep)
    runtime = IntegrationRuntime(lambda event: None, lambda: False)
    token = CURRENT_RUNTIME.set(runtime)
    try:
        turn, used = await main._call_model_with_retries(
            {"ollama-cloud": object(), "openai": None}, [], [], "oturum",
            "ollama-cloud", lambda event: None, lambda: False,
        )
        assert turn["content"] == "tamam" and used == "openai"
        assert attempts == ["ollama-cloud", "openai"]
        assert runtime.blocked_backends == {"ollama-cloud"}
    finally:
        CURRENT_RUNTIME.reset(token)


def test_retry_after_parses_seconds_and_invalid_value() -> None:
    response = type("Response", (), {"headers": {"retry-after": "2.5"}})()
    error = type("RateError", (), {"response": response})()
    assert main.retry_after_seconds(error) == 2.5
    response.headers["retry-after"] = "bozuk"
    assert main.retry_after_seconds(error) == 1.0


@pytest.mark.asyncio
async def test_two_blocked_providers_reach_third_ladder_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Erişim hatası alan iki sağlayıcı karantinaya alınır, merdivenin üçüncü basamağı işi bitirir."""
    attempts: list[str] = []

    class QuotaError(Exception):
        status_code = 402

    async def fake_stream(
        client: Any, profile: Any, messages: Any, schemas: Any, session_id: str,
        emit: Any, should_stop: Any,
    ) -> Any:
        attempts.append(profile["provider"])
        if profile["provider"] in ("ollama-cloud", "openai"):
            raise QuotaError("kota doldu")
        return {"content": "üçüncü profil çalıştı", "tool_calls": [],
                "finish_reason": "stop", "usage": main.ZERO_USAGE}

    monkeypatch.setattr(main, "APIStatusError", QuotaError)
    monkeypatch.setattr(main, "_stream_completion", fake_stream)
    runtime = IntegrationRuntime(lambda event: None, lambda: False)
    token = CURRENT_RUNTIME.set(runtime)
    try:
        turn, used = await main._call_model_with_retries(
            {"ollama-cloud": object(), "openai": object(), "openrouter": object()}, [], [],
            "oturum", "ollama-cloud", lambda event: None, lambda: False,
        )
        assert turn["content"] == "üçüncü profil çalıştı"
        assert used == "openrouter"
        assert attempts == ["ollama-cloud", "openai", "openrouter"]
        assert runtime.blocked_backends == {"ollama-cloud", "openai"}
    finally:
        CURRENT_RUNTIME.reset(token)
