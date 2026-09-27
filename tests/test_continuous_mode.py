"""Sürekli mod: son yanıt görevi bitirmez; görev yalnız kanıt + kullanıcı onayıyla başarılı biter."""
import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from omniagent.app import agent as main
from omniagent.app.continuous import (
    CONTINUE_PROMPT, WINDOW_MARKER, parse_continuous_limits, window_messages,
)
from omniagent.app.types import RunReport
from omniagent.core.events import AgentEvent
from omniagent.integrations import runtime as runtime_module
from omniagent.integrations.capabilities import CapabilityService
from omniagent.tools import filesystem

Message = Dict[str, Any]
Turn = Dict[str, Any]


def tool_turn(*calls: Dict[str, str]) -> Turn:
    return {"content": "", "tool_calls": list(calls), "finish_reason": "tool_calls", "usage": main.ZERO_USAGE}


def text_turn(text: str) -> Turn:
    return {"content": text, "tool_calls": [], "finish_reason": "stop", "usage": main.ZERO_USAGE}


def call(call_id: str, name: str, arguments: Dict[str, Any]) -> Dict[str, str]:
    return {"id": call_id, "name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}


class ContinuousRun:
    """Betikli modelle gerçek ajan döngüsünü sürekli modda koşturur; betik bitince kullanıcı durdurur."""

    def __init__(self, script: List[Turn], answers: List[Dict[str, Any]], answer_delay: float) -> None:
        self.script = script
        self.answers = list(answers)
        self.answer_delay = answer_delay
        self.questions: List[str] = []
        self.model_inputs: List[List[Message]] = []
        self.schema_names: List[str] = []
        self.events: List[AgentEvent] = []

    async def model(
        self, clients: Any, messages: List[Message], schemas: List[Dict[str, Any]], session_id: str,
        backend: str, emit: Any, should_stop: Any,
    ) -> Tuple[Turn, str]:
        self.model_inputs.append(messages)
        self.schema_names = [schema["function"]["name"] for schema in schemas]
        if len(self.model_inputs) > len(self.script):
            return {"content": "", "tool_calls": [], "finish_reason": "stopped", "usage": main.ZERO_USAGE}, backend
        return self.script[len(self.model_inputs) - 1], backend

    async def answer(self, title: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        self.questions.append(title)
        await asyncio.sleep(self.answer_delay)
        return self.answers.pop(0)

    async def run(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra: Dict[str, Any]) -> RunReport:
        monkeypatch.setattr(main, "_call_model_with_retries", self.model)
        monkeypatch.setattr(filesystem, "BACKUP_DIR", tmp_path / "backups")
        service = CapabilityService(tmp_path)
        try:
            return await main.run_agent_with_callback(
                "Durmadan para kazanmanın yolunu bul", self.events.append,
                {"requested_backend": None, "should_stop": lambda: False,
                 "state_file": str(tmp_path / "memory.json"), "history": [],
                 "integrations": service, "answer": self.answer, "run_mode": "continuous",
                 "max_wall_clock_seconds": 60.0, **extra},
                {"ollama-cloud": object()},
            )
        finally:
            await service.close()


def tool_messages(messages: List[Message]) -> str:
    return "\n".join(str(message.get("content", "")) for message in messages if message.get("role") == "tool")


@pytest.mark.asyncio
async def test_final_answer_continues_until_user_confirms_proven_goal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    product = tmp_path / "urun.md"
    session = ContinuousRun([
        tool_turn(call("w1", "write_file", {"path": str(product), "content": "ürün"})),
        text_turn("İlk sürüm hazır; sırada yayın var."),
        tool_turn(call("g1", "report_goal_met", {"summary": "Ürün yayında, ilk satış alındı.",
                                                 "evidence_call_ids": ["w1"]})),
    ], [{"yanit": "evet"}], 0.0)
    report = await session.run(tmp_path, monkeypatch, {})
    assert report["success"]
    assert report["metrics"]["turns"] == 3
    assert "report_goal_met" in session.schema_names
    assert any(message["role"] == "user" and str(message["content"]).startswith(CONTINUE_PROMPT)
               for message in session.model_inputs[2])
    assert len(session.questions) == 1 and "Ürün yayında, ilk satış alındı." in session.questions[0]
    assert session.events[-1]["kind"] == "run_finished" and session.events[-1]["success"]


@pytest.mark.asyncio
async def test_unproven_or_rejected_goal_keeps_the_run_going(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = ContinuousRun([
        tool_turn(call("g1", "report_goal_met", {"summary": "Para kazanıldı.", "evidence_call_ids": ["yok"]})),
        tool_turn(call("w1", "write_file", {"path": str(tmp_path / "plan.md"), "content": "plan"})),
        tool_turn(call("g2", "report_goal_met", {"summary": "Plan hazır.", "evidence_call_ids": ["w1"]})),
    ], [{"yanit": "Hayır: satış kanıtı yok"}], 0.0)
    report = await session.run(tmp_path, monkeypatch, {})
    assert not report["success"]
    assert report["reason"] == "durduruldu"
    assert len(session.questions) == 1 and "Plan hazır." in session.questions[0]
    assert "başarılı bir araç çağrısı değil" in tool_messages(session.model_inputs[1])
    rejection = tool_messages(session.model_inputs[3])
    assert "ONAYLAMADI" in rejection and "Hayır: satış kanıtı yok" in rejection


@pytest.mark.asyncio
async def test_stalled_run_asks_user_for_direction_instead_of_stopping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = call("r1", "read_file", {"path": str(tmp_path / "yok.txt")})
    session = ContinuousRun([tool_turn(missing)] * 6, [{"yanit": "Başka bir yol dene"}], 0.0)
    report = await session.run(tmp_path, monkeypatch, {})
    assert report["reason"] == "durduruldu"
    assert len(session.questions) == 1 and "takıldı" in session.questions[0]
    after_question = session.model_inputs[-1]
    assert any(message["role"] == "user"
               and str(message["content"]).startswith("KULLANICI YÖNLENDİRMESİ: Başka bir yol dene")
               for message in after_question)


@pytest.mark.asyncio
async def test_ask_user_waits_past_timeout_and_refuses_secret_questions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_module, "APPROVAL_TIMEOUT_SECONDS", 0.05)
    session = ContinuousRun([
        tool_turn(call("a1", "ask_user", {"question": "Ödemeler için IBAN numaran nedir?", "kind": "text"})),
        tool_turn(call("a2", "ask_user", {"question": "Stripe API anahtarını yazar mısın?", "kind": "text"})),
    ], [{"yanit": "TR00 0000 0000"}], 0.2)
    await session.run(tmp_path, monkeypatch, {})
    assert session.questions == ["Ödemeler için IBAN numaran nedir?"]
    assert "Kullanıcı yanıtı" in tool_messages(session.model_inputs[1])
    assert "⚙ Ayarlar" in tool_messages(session.model_inputs[2])


@pytest.mark.asyncio
async def test_token_cap_ends_continuous_run_with_limit_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    usage = {"prompt_tokens": 6000, "cached_tokens": 0, "completion_tokens": 0}
    session = ContinuousRun([{**text_turn("Rapor"), "usage": usage}] * 3, [], 0.0)
    report = await session.run(tmp_path, monkeypatch, {"max_total_tokens": 10_000})
    assert not report["success"]
    assert report["reason"].startswith("sınır doldu")
    assert report["metrics"]["turns"] == 2


@pytest.mark.asyncio
async def test_continuous_mode_requires_an_interactive_channel(tmp_path: Path) -> None:
    report = await main.run_agent_with_callback(
        "Durmadan çalış", lambda event: None,
        {"requested_backend": None, "should_stop": lambda: False,
         "state_file": str(tmp_path / "memory.json"), "history": [], "run_mode": "continuous"},
        {"ollama-cloud": object()},
    )
    assert not report["success"]
    assert "Sürekli mod" in report["outcome"]


def test_context_window_keeps_goal_and_newest_turns_at_turn_boundaries() -> None:
    head: List[Message] = [{"role": "system", "content": "s"}, {"role": "user", "content": "hedef"}]
    turns: List[Message] = []
    for number in range(45):
        turns += [{"role": "assistant", "content": f"t{number}", "tool_calls": [{"id": f"c{number}"}]},
                  {"role": "tool", "tool_call_id": f"c{number}", "content": "ok"}]
    windowed, dropped = window_messages(head + turns, 2, 40, 20, 0)
    assert dropped == 25
    assert windowed[:2] == head
    assert str(windowed[2]["content"]).startswith(WINDOW_MARKER)
    assert windowed[3]["content"] == "t25" and windowed[-1]["role"] == "tool"
    assert window_messages(windowed, 2, 40, 20, dropped) == (windowed, 25)


def test_continuous_limits_parse_turkish_numbers_and_reject_invalid_values() -> None:
    assert parse_continuous_limits("8,5", "20.000.000") == {"max_hours": 8.5, "max_total_tokens": 20_000_000}
    with pytest.raises(ValueError):
        parse_continuous_limits("0", "20000")
    with pytest.raises(ValueError):
        parse_continuous_limits("8", "çok")
