"""Sohbet katmanı: akışta balon gönderimi, 4 balon sınırı, akış yeniden denemesinde çift balon olmaması, iş devri."""
import asyncio
import json
from typing import Callable, Dict, List, Tuple

import pytest
from openai import AsyncOpenAI

from omniagent.app.model_runtime import ZERO_USAGE
from omniagent.app.types import ModelTurn, ToolCallDraft
from omniagent.companion import chat
from omniagent.core.events import AgentEvent
from omniagent.memory.personal import ArchivedMessage


def scripted_model(chunks: List[str], tool_calls: List[ToolCallDraft]) -> Callable[..., object]:
    """Model çağrısı sınırında sahte sağlayıcı: parçaları akış olayı olarak yayınlar ve turu döndürür.
    'RESET' parçası akışın yeniden denendiğini (stream_reset) bildirir."""
    async def call(clients: Dict[str, AsyncOpenAI], messages: List[Dict[str, str]], tool_schemas: object,
                   session_id: str, backend: str, emit: Callable[[AgentEvent], None],
                   should_stop: Callable[[], bool]) -> Tuple[ModelTurn, str]:
        for chunk in chunks:
            if chunk == "RESET":
                emit({"kind": "stream_reset", "reason": "geçici hata"})
            else:
                emit({"kind": "text_delta", "text": chunk})
            await asyncio.sleep(0)
        turn: ModelTurn = {"content": "", "tool_calls": tool_calls, "finish_reason": "stop", "usage": ZERO_USAGE}
        return turn, backend
    return call


async def run(monkeypatch: pytest.MonkeyPatch, model: Callable[..., object]) -> Tuple[chat.ChatResult, List[str]]:
    monkeypatch.setattr(chat, "call_model_with_retries", model)
    monkeypatch.setattr(chat, "bubble_delay", lambda text: 0.0)
    sent: List[str] = []

    async def send(bubble: str) -> None:
        sent.append(bubble)

    result = await asyncio.wait_for(
        chat.respond({}, "openai", "sistem", [{"role": "user", "content": "selam"}], chat.CHAT_TOOLS, send,
                     lambda: False, "test"),
        timeout=5,
    )
    return result, sent


@pytest.mark.asyncio
async def test_first_line_is_sent_while_model_is_still_streaming(monkeypatch: pytest.MonkeyPatch) -> None:
    release = asyncio.Event()

    async def call(clients: Dict[str, AsyncOpenAI], messages: List[Dict[str, str]], tool_schemas: object,
                   session_id: str, backend: str, emit: Callable[[AgentEvent], None],
                   should_stop: Callable[[], bool]) -> Tuple[ModelTurn, str]:
        emit({"kind": "text_delta", "text": "selaam\nnasılsın"})
        await release.wait()
        emit({"kind": "text_delta", "text": " bugün"})
        return {"content": "", "tool_calls": [], "finish_reason": "stop", "usage": ZERO_USAGE}, backend

    monkeypatch.setattr(chat, "call_model_with_retries", call)
    monkeypatch.setattr(chat, "bubble_delay", lambda text: 0.0)
    sent: List[str] = []

    async def send(bubble: str) -> None:
        sent.append(bubble)
        release.set()

    result = await asyncio.wait_for(chat.respond({}, "openai", "sistem", [], chat.CHAT_TOOLS, send, lambda: False, "test"), timeout=5)
    assert sent == ["selaam", "nasılsın bugün"]
    assert result == {"bubbles": sent, "start_task": None}


@pytest.mark.asyncio
async def test_more_than_four_lines_merge_into_last_bubble(monkeypatch: pytest.MonkeyPatch) -> None:
    result, sent = await run(monkeypatch, scripted_model(["a\nb\n", "c\nd\ne"], []))
    assert sent == ["a", "b", "c", "d e"] and result["bubbles"] == sent


@pytest.mark.asyncio
async def test_stream_reset_does_not_resend_bubbles(monkeypatch: pytest.MonkeyPatch) -> None:
    _result, sent = await run(monkeypatch, scripted_model(["bir\n", "RESET", "bir\niki\n", "üç"], []))
    assert sent == ["bir", "iki", "üç"]


@pytest.mark.asyncio
async def test_task_only_turn_returns_goal(monkeypatch: pytest.MonkeyPatch) -> None:
    call: ToolCallDraft = {"id": "c1", "name": "start_task", "arguments": '{"goal": "masaüstündeki dosyaları listele"}'}
    result, sent = await run(monkeypatch, scripted_model([], [call]))
    assert sent == [] and result["start_task"] == "masaüstündeki dosyaları listele"


@pytest.mark.asyncio
async def test_empty_turn_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(chat.ChatError, match="boş yanıt"):
        await run(monkeypatch, scripted_model([], []))


def test_history_merges_consecutive_bubbles() -> None:
    history: List[ArchivedMessage] = [
        {"id": 1, "direction": "in", "kind": "chat", "text": "selam", "created_at": "t1", "delivery": None},
        {"id": 2, "direction": "in", "kind": "chat", "text": "naber", "created_at": "t2", "delivery": None},
        {"id": 3, "direction": "out", "kind": "chat", "text": "iyiyim", "created_at": "t3", "delivery": "sent"},
    ]
    assert chat.history_messages(history, {}) == [
        {"role": "user", "content": "selam\nnaber"}, {"role": "assistant", "content": "iyiyim"},
    ]


def test_history_shows_delegation_as_a_real_tool_call_and_drops_orphan_acks() -> None:
    """'bakıyorum' geçmişte yalnız gerçek start_task çağrısıyla görünür: çağrısız kalıbı model taklit ediyordu."""
    history: List[ArchivedMessage] = [
        {"id": 1, "direction": "in", "kind": "chat", "text": "masaüstümde ne var", "created_at": "t1", "delivery": None},
        {"id": 2, "direction": "out", "kind": "chat", "text": "bakıyorum hemen", "created_at": "t2", "delivery": "sent"},
        {"id": 3, "direction": "out", "kind": "task_report", "text": "3 dosya var", "created_at": "t3",
         "delivery": "sent"},
        {"id": 4, "direction": "in", "kind": "chat", "text": "bugün ne oldu", "created_at": "t4", "delivery": None},
        {"id": 5, "direction": "out", "kind": "chat", "text": chat.TASK_ACK, "created_at": "t5", "delivery": "sent"},
        {"id": 6, "direction": "out", "kind": "chat", "text": "dur bir bakayım", "created_at": "t6", "delivery": "sent"},
    ]
    messages = chat.history_messages(history, {2: "Masaüstündeki dosyaları listele"})
    call = messages[1]["tool_calls"][0]  # type: ignore[index]
    assert messages[1]["content"] == "bakıyorum hemen" and call["function"]["name"] == "start_task"
    assert json.loads(call["function"]["arguments"]) == {"goal": "Masaüstündeki dosyaları listele"}
    assert messages[2] == {"role": "tool", "tool_call_id": call["id"], "content": chat.TASK_STARTED_NOTE}
    assert messages[3] == {"role": "assistant", "content": "3 dosya var"}
    # İş kaydı olmayan "bakıyorum" sözleri (hazır ya da modelin yazdığı) geçmişe girmez.
    assert messages[-1] == {"role": "user", "content": "bugün ne oldu"}


def test_promise_detects_first_person_action_not_casual_bakalim() -> None:
    for text in ("bakıyorum hemen", "dur bir bakayım", "bi kontrol edeyim", "araştırıyorum şimdi", chat.TASK_ACK):
        assert chat.promises_action(text), text
    for text in ("göreceğiz bakalım", "hatırlayamadım şimdi", "valla hiç sorma", "baktım, safari açık",
                 "tekrar bakayım mı?"):
        assert not chat.promises_action(text), text


@pytest.mark.asyncio
async def test_promised_task_is_recovered_with_one_tool_only_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Söz verip aracı çağırmayan model, düzeltme çağrısında hedefi verir; metin kullanıcıya gitmez."""
    seen: List[List[Dict[str, object]]] = []
    stops: List[Callable[[], bool]] = []

    def recorder(model: Callable[..., object]) -> Callable[..., object]:
        async def call(clients: Dict[str, AsyncOpenAI], messages: List[Dict[str, object]], tool_schemas: object,
                       session_id: str, backend: str, emit: Callable[[AgentEvent], None],
                       should_stop: Callable[[], bool]) -> object:
            seen.append(messages)
            stops.append(should_stop)
            return await model(clients, messages, tool_schemas, session_id, backend, emit, should_stop)  # type: ignore[operator]
        return call

    def bridge_is_closing() -> bool:
        return False

    goal_call: ToolCallDraft = {"id": "c1", "name": "start_task",
                                "arguments": json.dumps({"goal": "Fenerbahçe'nin bu yılki başkanını bul"})}
    monkeypatch.setattr(chat, "call_model_with_retries", recorder(scripted_model(["yazılmamalı"], [goal_call])))
    goal = await chat.recover_promised_task({}, "openai", "sistem", [{"role": "user", "content": "fb başkanı kim"}],
                                            ["bakıyorum hemen"], bridge_is_closing, "test")
    assert goal == "Fenerbahçe'nin bu yılki başkanını bul"
    assert seen[0][-2] == {"role": "assistant", "content": "bakıyorum hemen"}
    assert seen[0][-1] == {"role": "user", "content": chat.PROMISE_CORRECTION}
    # Düzeltme çağrısı da köprünün durdurma bayrağını izler (kapanan köprüde yeniden deneme beklemesi sürmez).
    assert stops == [bridge_is_closing]
    monkeypatch.setattr(chat, "call_model_with_retries", scripted_model(["yine söz"], []))
    assert await chat.recover_promised_task({}, "openai", "sistem", [], ["bakıyorum"], bridge_is_closing, "test") is None


def test_textual_tool_call_is_stripped_and_its_goal_captured() -> None:
    """gemma4 bazen aracı metne yazıyor: etiket kullanıcıya gitmez, start_task hedefi yakalanır."""
    assert chat.textual_start_task('bir bakayım hemen <call:start_task goal="FB başkanını bul."> </call>') == (
        "bir bakayım hemen", "FB başkanını bul.")
    assert chat.textual_start_task("bakıyorum <call:start_task goal='fb başkanı kim araştır' />") == (
        "bakıyorum", "fb başkanı kim araştır")
    assert chat.textual_start_task('<call:web_search query="x" />') == ("", None)
    assert chat.textual_start_task("selam naber") == ("selam naber", None)


@pytest.mark.asyncio
async def test_textual_call_in_stream_starts_the_task_without_leaking_markup(monkeypatch: pytest.MonkeyPatch) -> None:
    result, sent = await run(monkeypatch, scripted_model(
        ['bakıyorum hemen <call:start_task goal="Masaüstünü listele" />\n'], []))
    assert sent == ["bakıyorum hemen"] and result["start_task"] == "Masaüstünü listele"


@pytest.mark.asyncio
async def test_toolless_turn_sends_history_without_tool_call_structure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Araçsız turda (iş raporu) geçmişteki araç çağrıları düz metne iner: bazı sağlayıcılar araçsız istekte
    tool_calls/tool mesajı görünce isteği reddeder."""
    seen: List[List[Dict[str, object]]] = []

    async def call(clients: Dict[str, AsyncOpenAI], messages: List[Dict[str, object]], tool_schemas: object,
                   session_id: str, backend: str, emit: Callable[[AgentEvent], None],
                   should_stop: Callable[[], bool]) -> Tuple[ModelTurn, str]:
        seen.append(messages)
        emit({"kind": "text_delta", "text": "hallettim\n"})
        return {"content": "", "tool_calls": [], "finish_reason": "stop", "usage": ZERO_USAGE}, backend

    monkeypatch.setattr(chat, "call_model_with_retries", call)
    monkeypatch.setattr(chat, "bubble_delay", lambda text: 0.0)
    history = chat.history_messages([
        {"id": 1, "direction": "in", "kind": "chat", "text": "masaüstümde ne var", "created_at": "t1", "delivery": None},
        {"id": 2, "direction": "out", "kind": "chat", "text": "bakıyorum", "created_at": "t2", "delivery": "sent"},
    ], {2: "Masaüstünü listele"})

    async def send(bubble: str) -> None:
        return None

    await chat.respond({}, "openai", "sistem", history + [{"role": "user", "content": "[İŞ RAPORU] bitti"}], [],
                       send, lambda: False, "test")
    assert all("tool_calls" not in message and message["role"] != "tool" for message in seen[0])
    assert {"role": "assistant", "content": "bakıyorum"} in seen[0]
