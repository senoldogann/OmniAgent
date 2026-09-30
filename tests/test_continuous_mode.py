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
from omniagent.core.events import AWAITING_APPROVAL_CODE, AWAITING_DIRECTION_CODE, AgentEvent
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
async def test_plain_text_approval_request_opens_real_confirmation_before_next_model_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = ContinuousRun([
        text_turn("Hazırladığım yorum: Merhaba. Bu yorumu sizin adınıza paylaşmamı ister misiniz?"),
        text_turn("Onay verilmedi; taslak olarak kaldı."),
    ], [{"onay": False}], 0.0)
    await session.run(tmp_path, monkeypatch, {})
    assert len(session.questions) == 1
    assert "Merhaba" in session.questions[0]
    assert "ONAYLAMADI" in tool_messages(session.model_inputs[1])


@pytest.mark.asyncio
async def test_continuous_loop_without_state_does_not_treat_repeated_reads_as_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = tmp_path / "unchanged.txt"
    probe.write_text("ok", encoding="utf-8")
    session = ContinuousRun([
        tool_turn(call(f"r{i}", "read_file", {"path": str(probe)})) for i in range(10)
    ], [], 0.0)
    await session.run(tmp_path, monkeypatch, {})
    assert any("ardışık ilerlemesiz tur" in str(event.get("text", "")) for event in session.events)


@pytest.mark.asyncio
async def test_unattended_confirmation_still_reaches_user_and_never_runs_on_silence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = ContinuousRun([
        tool_turn(call("a1", "ask_user", {"question": "Taslağı yayınlayayım mı?", "kind": "confirm"})),
    ], [{"onay": False}], 0.0)
    await session.run(tmp_path, monkeypatch, {"unattended": True})
    assert session.questions == ["Taslağı yayınlayayım mı?"]
    assert "ONAYLAMADI" in tool_messages(session.model_inputs[1])


def test_bare_gui_actions_are_not_goal_evidence():
    evidence = main.record_goal_evidence({}, [
        call("click", "cua_click_point", {"point": [100, 100]}),
        call("fill", "cua_fill_field", {"point": [100, 100], "text": "taslak"}),
        call("shot", "take_screenshot", {}),
        call("read", "cua_snapshot", {"app": "Google Chrome"}),
    ], [{"tool_call_id": name, "ok": True, "result": "ok"} for name in ("click", "fill", "shot", "read")])
    assert set(evidence) == {"read"}


@pytest.mark.asyncio
async def test_unverified_gui_success_is_not_streamed_as_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim = "Bio optimize edildi. Yorum bıraktım ve gönderiyi yayınladım."
    session = ContinuousRun([text_turn(claim)], [], 0.0)
    original_model = session.model

    async def streaming_model(*args):
        args[5]({"kind": "text_delta", "text": claim})
        return await original_model(*args)

    monkeypatch.setattr(session, "model", streaming_model)
    await session.run(tmp_path, monkeypatch, {})
    assert not any(event["kind"] == "text_delta" and claim in event["text"] for event in session.events)
    assert any("SONUÇ DOĞRULAMA" in str(message.get("content", "")) for message in session.model_inputs[1])


@pytest.mark.asyncio
async def test_stopped_continuous_history_does_not_preserve_false_model_state_as_fact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    false_claim = "profilim 1000 yeni takipçi kazandı"
    session = ContinuousRun([text_turn("STATE:\nconfirmed: " + false_claim + "\nremaining: yok")], [], 0.0)
    report = await session.run(tmp_path, monkeypatch, {})
    assert not report["success"]
    assert false_claim not in report["exchange"]["answer"]


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
async def test_repeated_goal_report_with_same_evidence_is_not_asked_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Aynı kanıtla yinelenen bildirim kullanıcıya ikinci kez sorulmaz (canlı kayıtta soruluyordu)."""
    product = tmp_path / "urun.md"
    session = ContinuousRun([
        tool_turn(call("w1", "write_file", {"path": str(product), "content": "ürün"})),
        tool_turn(call("g1", "report_goal_met", {"summary": "Ürün yayında.",
                                                 "evidence_call_ids": ["w1"]})),
        tool_turn(call("g2", "report_goal_met", {"summary": "Ürün gerçekten yayında.",
                                                 "evidence_call_ids": ["w1"]})),
    ], [{"yanit": "hayır, satış kanıtı yok"}], 0.0)
    report = await session.run(tmp_path, monkeypatch, {})

    assert not report["success"]
    assert len(session.questions) == 1
    assert "zaten değerlendirildi" in tool_messages(session.model_inputs[3])


@pytest.mark.asyncio
async def test_goal_report_with_new_evidence_is_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Yeni başarılı çağrı içeren bildirim yine değerlendirilir; yalnız tekrar reddedilir."""
    session = ContinuousRun([
        tool_turn(call("w1", "write_file", {"path": str(tmp_path / "a.md"), "content": "a"})),
        tool_turn(call("g1", "report_goal_met", {"summary": "İlk iddia.",
                                                 "evidence_call_ids": ["w1"]})),
        tool_turn(call("w2", "write_file", {"path": str(tmp_path / "b.md"), "content": "b"})),
        tool_turn(call("g2", "report_goal_met", {"summary": "İkinci iddia.",
                                                 "evidence_call_ids": ["w2"]})),
    ], [{"yanit": "hayır"}, {"yanit": "evet"}], 0.0)
    report = await session.run(tmp_path, monkeypatch, {})

    assert report["success"]
    assert len(session.questions) == 2 and "İkinci iddia." in session.questions[1]


@pytest.mark.asyncio
async def test_stalled_run_recovers_without_asking_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = call("r1", "read_file", {"path": str(tmp_path / "yok.txt")})
    session = ContinuousRun([tool_turn(missing)] * 6, [], 0.0)
    report = await session.run(tmp_path, monkeypatch, {})
    assert report["reason"] == "durduruldu"
    assert session.questions == []
    after_recovery = session.model_inputs[-1]
    assert any(message["role"] == "user"
               and "Aynı başarısız çağrıyı" in str(message["content"])
               for message in after_recovery)


@pytest.mark.asyncio
async def test_first_failed_call_immediately_requests_an_alternative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = tmp_path / "yok.txt"
    available = tmp_path / "var.txt"
    available.write_text("ok", encoding="utf-8")
    session = ContinuousRun([
        tool_turn(call("r1", "read_file", {"path": str(missing)})),
        tool_turn(call("r2", "read_file", {"path": str(available)})),
    ], [], 0.0)
    report = await session.run(tmp_path, monkeypatch, {})
    assert report["reason"] == "durduruldu"
    assert session.questions == []
    next_turn = "\n".join(str(message.get("content", "")) for message in session.model_inputs[1])
    assert "HOST — ARAÇ HATASI" in next_turn
    assert "read_file:" in next_turn
    assert "farklı bir araç/yöntem seç" in next_turn
    assert sum(message.get("role") == "user" and str(message.get("content", "")).startswith("HOST — ARAÇ HATASI")
               for message in session.model_inputs[2]) == 1


@pytest.mark.asyncio
async def test_identical_failed_call_is_blocked_until_another_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = tmp_path / "yok.txt"
    available = tmp_path / "var.txt"
    available.write_text("ok", encoding="utf-8")
    session = ContinuousRun([
        tool_turn(call("r1", "read_file", {"path": str(missing)})),
        tool_turn(call("r2", "read_file", {"path": str(missing)})),
        tool_turn(call("r3", "read_file", {"path": str(available)})),
        tool_turn(call("r4", "read_file", {"path": str(missing)})),
    ], [], 0.0)
    await session.run(tmp_path, monkeypatch, {})
    blocked = tool_messages(session.model_inputs[2])
    assert "REPEATED_FAILED_CALL" in blocked or "RepeatedFailedCall" in blocked
    final = tool_messages(session.model_inputs[4])
    assert final.count("RepeatedFailedCall") == 1
    assert session.questions == []


@pytest.mark.asyncio
async def test_idle_reports_recover_without_asking_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = ContinuousRun([text_turn("İlerleme raporu")] * 3, [], 0.0)
    report = await session.run(tmp_path, monkeypatch, {})
    assert report["reason"] == "durduruldu"
    assert session.questions == []
    assert any(message["role"] == "user" and "3 ardışık yanıtta hiçbir araç çalışmadı" in str(message["content"])
               for message in session.model_inputs[-1])


@pytest.mark.asyncio
async def test_repeated_successful_reads_exhaust_recovery_and_require_new_direction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = tmp_path / "probe.txt"
    probe.write_text("ok", encoding="utf-8")
    high_usage = {"prompt_tokens": 70_000, "cached_tokens": 0, "completion_tokens": 1_000}
    script = [
        {**tool_turn(call(f"r{index}", "read_file", {"path": str(probe)})), "usage": high_usage}
        for index in range(26)
    ]
    session = ContinuousRun(script, [{"yanit": ""}], 0.0)
    report = await session.run(tmp_path, monkeypatch, {})
    assert report["reason"] == "ilerleme yok: yeni yön verilmedi"
    assert report["metrics"]["turns"] < 26
    assert len(session.questions) == 1
    assert not any("HOST FAST LOOP" in str(message.get("content", ""))
                   for turn in session.model_inputs for message in turn)
    assert not any(event["kind"] == "notice" and "bütçesi baskısı" in event["text"].lower()
                   for event in session.events)


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
async def test_unattended_question_is_deferred_and_btw_reaches_next_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    controls: List[str] = []
    session = ContinuousRun([
        tool_turn(call("a1", "ask_user", {"question": "Hangi hesap?", "kind": "text"})),
        text_turn("Bağımsız adımları sürdürüyorum."),
    ], [], 0.0)
    original_model = session.model

    async def model(*args: Any) -> Tuple[Turn, str]:
        result = await original_model(*args)
        if len(session.model_inputs) == 1:
            controls.append("/btw Önce yerel taslağı hazırla")
        return result

    monkeypatch.setattr(session, "model", model)

    def pop_controls() -> List[str]:
        ready = list(controls)
        controls.clear()
        return ready

    await session.run(tmp_path, monkeypatch, {"unattended": True, "pop_control_messages": pop_controls})
    assert session.questions == []
    assert "aynı soruyu yineleme" in tool_messages(session.model_inputs[1])
    assert any("Önce yerel taslağı hazırla" in str(message.get("content", ""))
               for message in session.model_inputs[1])


@pytest.mark.asyncio
async def test_unattended_proven_goal_waits_for_approve_without_model_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = ContinuousRun([
        tool_turn(call("w1", "write_file", {"path": str(tmp_path / "plan.md"), "content": "plan"})),
        tool_turn(call("g1", "report_goal_met", {"summary": "Plan hazır.",
                                                 "evidence_call_ids": ["w1"]})),
    ], [], 0.0)

    def pop_controls() -> List[str]:
        if any(event.get("code") == "GOAL_AWAITING_APPROVAL" for event in session.events):
            return ["/approve"]
        return []

    report = await asyncio.wait_for(session.run(
        tmp_path, monkeypatch, {"unattended": True, "pop_control_messages": pop_controls},
    ), timeout=5)
    assert report["success"] and report["outcome"] == "Plan hazır."
    assert session.questions == []
    assert len(session.model_inputs) == 2


@pytest.mark.asyncio
async def test_unattended_proven_goal_closes_when_approve_never_arrives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Onay bekleyen oturum host kilidini süresiz tutmaz; sessizlik başarı değildir."""
    monkeypatch.setattr(main, "GOAL_APPROVAL_TIMEOUT_SECONDS", 0.5)
    session = ContinuousRun([
        tool_turn(call("w1", "write_file", {"path": str(tmp_path / "plan.md"), "content": "plan"})),
        tool_turn(call("g1", "report_goal_met", {"summary": "Plan hazır.",
                                                 "evidence_call_ids": ["w1"]})),
    ], [], 0.0)

    def pop_controls() -> List[str]:
        return []

    report = await asyncio.wait_for(session.run(
        tmp_path, monkeypatch, {"unattended": True, "pop_control_messages": pop_controls},
    ), timeout=5)
    assert not report["success"] and report["outcome"] == "Plan hazır."
    assert report["reason"] == "hedef kullanıcı tarafından onaylanmadı"
    assert len(session.model_inputs) == 2
    prompts: List[str] = [str(event.get("text")) for event in session.events
                          if event["kind"] == "notice" and event.get("code") == AWAITING_APPROVAL_CODE]
    assert len(prompts) == 1 and "/approve" in prompts[0]


@pytest.mark.asyncio
async def test_unattended_idle_run_parks_until_btw(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = ContinuousRun([
        tool_turn(call("a1", "ask_user", {"question": "Hangi hesap?", "kind": "text"})),
        text_turn("Rapor 1"), text_turn("Rapor 2"), text_turn("Rapor 3"),
        text_turn("Yeni yön geldi."),
    ], [], 0.0)
    delivered = False

    def pop_controls() -> List[str]:
        nonlocal delivered
        parked = any(event["kind"] == "notice" and "Bağımsız adımlar tükendi" in event["text"]
                     for event in session.events)
        if parked and not delivered:
            delivered = True
            return ["/btw Eksik hesap olmadan plan yap"]
        return []

    report = await asyncio.wait_for(session.run(
        tmp_path, monkeypatch, {"unattended": True, "pop_control_messages": pop_controls},
    ), timeout=5)
    assert report["reason"] == "durduruldu"
    assert delivered and session.questions == []
    assert any(event["kind"] == "notice" and event.get("code") == AWAITING_DIRECTION_CODE
               for event in session.events)
    assert any("Eksik hesap olmadan plan yap" in str(message.get("content", ""))
               for message in session.model_inputs[4])


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
