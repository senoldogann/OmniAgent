"""Sohbet sınırları, mesaj sırası ve başarısız görev raporları."""
from copy import deepcopy
from datetime import date
from pathlib import Path

import pytest

import main
from config import DEFAULT_BACKEND
from conversation import make_exchange, to_messages, tool_digest, trim_history
from state_manager import make_step_record


def test_run_mode_budgets_are_bounded_and_overridable() -> None:
    base = {"requested_backend": None, "should_stop": lambda: False,
            "state_file": "/tmp/omni-test-memory.json", "history": []}
    assert main.resolve_run_limits(base) == ("normal", main.MAX_ITERATIONS, main.MAX_WALL_CLOCK_SECONDS)
    assert main.resolve_run_limits({**base, "run_mode": "extended"}) == ("extended", 50, 1200.0)
    assert main.resolve_run_limits({**base, "run_mode": "autonomous"}) == ("autonomous", 100, 2700.0)
    assert main.resolve_run_limits({**base, "run_mode": "extended", "max_iterations": 7}) == ("extended", 7, 1200.0)


def test_run_mode_rejects_invalid_budget() -> None:
    base = {"requested_backend": None, "should_stop": lambda: False,
            "state_file": "/tmp/omni-test-memory.json", "history": []}
    with pytest.raises(ValueError):
        main.resolve_run_limits({**base, "run_mode": "unknown"})
    with pytest.raises(ValueError):
        main.resolve_run_limits({**base, "max_iterations": 0})


def test_empty_history_and_order() -> None:
    assert to_messages([]) == []
    history = [make_exchange("ilk", "bir", []), make_exchange("ikinci", "iki", [])]
    assert to_messages(history) == [
        {"role": "user", "content": "ilk"}, {"role": "assistant", "content": "bir"},
        {"role": "user", "content": "ikinci"}, {"role": "assistant", "content": "iki"}]


def test_trim_limits_and_input_independence() -> None:
    history = [make_exchange(str(i), "a" * 2000, []) for i in range(12)]
    original = deepcopy(history)
    trimmed = trim_history(history)
    assert len(trimmed) == 8
    assert trimmed[0]["goal"] == "4"
    assert all(len(e["answer"]) == 1200 for e in trimmed)
    trimmed[0]["tools"].append("yeni")
    assert history == original
    assert trim_history(history, 0) == []
    assert trim_history(history, -1) == []
    assert trim_history(history, 1, 0)[0]["answer"] == ""
    assert len(trim_history(history, 1, 5)[0]["answer"]) == 5


def test_tool_digest_bounds_and_failure_labels() -> None:
    steps = [make_step_record("read_file", '{"path":"/tmp/' + str(i) + '"}', True, "sonuç")
             for i in range(8)]
    steps += [make_step_record("write_file", '{"path":"/tmp/x","content":"GİZLİ GÖVDE"}', False, "hata")]
    digest = tool_digest(steps)
    assert len(digest) == 5
    assert digest[-1] == "başarısız: write_file /tmp/x"
    assert all("GİZLİ GÖVDE" not in item for item in digest)
    assert tool_digest([make_step_record("read_file", "kırpılmış {", False, "")]) == ["başarısız: read_file"]
    messages = to_messages([make_exchange("hedef", "yanıt", steps)])
    assert "[önceki görevde kullanılan:" in messages[-1]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["success", "error", "stop", "limit"])
async def test_agent_history_and_all_outcomes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str) -> None:
    history = [make_exchange("önceki", "önceki yanıt", [])]
    received = []

    async def fake_model(clients, messages, schemas, session_id, backend, emit, should_stop):
        received.extend(deepcopy(messages))
        if mode == "error":
            raise RuntimeError("deneme hatası")
        return {"content": "yeni yanıt", "tool_calls": [], "finish_reason": "stop",
                "usage": main.ZERO_USAGE}, backend

    monkeypatch.setattr(main, "_call_model_with_retries", fake_model)
    if mode == "limit":
        monkeypatch.setattr(main, "MAX_ITERATIONS", 0)
    events = []
    report = await main.run_agent_with_callback(
        "yeni hedef", events.append,
        {"requested_backend": None, "should_stop": lambda: mode == "stop",
         "state_file": str(tmp_path / "memory.json"), "history": history},
        {DEFAULT_BACKEND: object()},
    )
    assert report["exchange"]["goal"] == "yeni hedef"
    assert report["exchange"]["answer"]
    assert report["success"] == (mode == "success")
    assert "reason" in report
    assert events[-1]["kind"] == "run_finished"
    assert report["metrics"]["model_seconds"] >= 0
    assert report["metrics"]["tool_seconds"] >= 0
    if received:
        assert received[0] == {"role": "system", "content": main.build_system_prompt(date.today(), None, "")}
        assert received[1:3] == to_messages(history)
        assert received[-1] == {"role": "user", "content": "yeni hedef"}
    if mode == "error":
        assert "deneme hatası" in report["exchange"]["answer"]
    if mode == "stop":
        assert "durduruldu" in report["exchange"]["answer"]
