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
from omniagent.memory.personal import ArchivedMessage, ChatToolCall


def scripted_model(chunks: List[str], tool_calls: List[ToolCallDraft]) -> Callable[..., object]:
    """Model çağrısı sınırında sahte sağlayıcı: parçaları akış olayı olarak yayınlar ve turu döndürür.
    'RESET' parçası akışın yeniden denendiğini (stream_reset) bildirir."""
    async def call(clients: Dict[str, AsyncOpenAI], messages: List[Dict[str, str]], tool_schemas: object,
                   session_id: str, backend: str, emit: Callable[[AgentEvent], None],
                   should_stop: Callable[[], bool]) -> Tuple[ModelTurn, str]:
        final_chunks: List[str] = []
        for chunk in chunks:
            if chunk == "RESET":
                final_chunks = []
                emit({"kind": "stream_reset", "reason": "geçici hata"})
            else:
                final_chunks.append(chunk)
                emit({"kind": "text_delta", "text": chunk})
            await asyncio.sleep(0)
        turn: ModelTurn = {"content": "".join(final_chunks), "tool_calls": tool_calls,
                           "finish_reason": "stop", "usage": ZERO_USAGE}
        return turn, backend
    return call


async def run(monkeypatch: pytest.MonkeyPatch, model: Callable[..., object]) -> Tuple[chat.ChatResult, List[str]]:
    monkeypatch.setattr(chat, "call_model_with_retries", model)
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
async def test_no_reply_is_sent_until_model_finishes(monkeypatch: pytest.MonkeyPatch) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def call(clients: Dict[str, AsyncOpenAI], messages: List[Dict[str, str]], tool_schemas: object,
                   session_id: str, backend: str, emit: Callable[[AgentEvent], None],
                   should_stop: Callable[[], bool]) -> Tuple[ModelTurn, str]:
        emit({"kind": "text_delta", "text": "selaam\nnasılsın"})
        started.set()
        await release.wait()
        emit({"kind": "text_delta", "text": " bugün"})
        return {"content": "selaam\nnasılsın bugün", "tool_calls": [],
                "finish_reason": "stop", "usage": ZERO_USAGE}, backend

    monkeypatch.setattr(chat, "call_model_with_retries", call)
    sent: List[str] = []

    async def send(message: str) -> None:
        sent.append(message)

    task = asyncio.create_task(
        chat.respond({}, "openai", "sistem", [], chat.CHAT_TOOLS, send, lambda: False, "test")
    )
    await asyncio.wait_for(started.wait(), 1)
    await asyncio.sleep(0)
    assert sent == []
    release.set()
    result = await asyncio.wait_for(task, 5)
    assert sent == ["selaam\nnasılsın bugün"]
    assert result == {"bubbles": sent, "start_task": None}


@pytest.mark.asyncio
async def test_multiline_and_more_than_four_paragraphs_stay_in_one_final_message(monkeypatch: pytest.MonkeyPatch) -> None:
    text = "bir\n\niki\n\nüç\n\ndört\n\nbeş"
    result, sent = await run(monkeypatch, scripted_model([text], []))
    assert sent == [text] and result["bubbles"] == [text]


@pytest.mark.asyncio
async def test_reply_longer_than_600_characters_is_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    text = "başlangıç\n" + ("x" * 900) + "\nhttps://example.test/source"
    result, sent = await run(monkeypatch, scripted_model([text], []))
    assert sent == [text] and result["bubbles"] == [text]


@pytest.mark.asyncio
async def test_stream_reset_discards_unverified_draft_and_sends_only_final(monkeypatch: pytest.MonkeyPatch) -> None:
    _result, sent = await run(monkeypatch, scripted_model(
        ["asla görünmemeli\n", "RESET", "bir\niki\n", "üç"], []
    ))
    assert sent == ["bir\niki\nüç"]


@pytest.mark.asyncio
async def test_model_failure_after_partial_stream_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    async def call(clients: Dict[str, AsyncOpenAI], messages: List[Dict[str, str]], tool_schemas: object,
                   session_id: str, backend: str, emit: Callable[[AgentEvent], None],
                   should_stop: Callable[[], bool]) -> Tuple[ModelTurn, str]:
        emit({"kind": "text_delta", "text": "doğrulanmamış taslak\n"})
        raise chat.ChatError("provider failed")

    monkeypatch.setattr(chat, "call_model_with_retries", call)
    sent: List[str] = []

    async def send(message: str) -> None:
        sent.append(message)

    with pytest.raises(chat.ChatError, match="provider failed"):
        await chat.respond({}, "openai", "sistem", [], chat.CHAT_TOOLS, send, lambda: False, "test")
    assert sent == []


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
    assert chat.history_messages(history, {}, {}) == [
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
    messages = chat.history_messages(history, {2: "Masaüstündeki dosyaları listele"}, {})
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
        return {"content": "hallettim", "tool_calls": [], "finish_reason": "stop", "usage": ZERO_USAGE}, backend

    monkeypatch.setattr(chat, "call_model_with_retries", call)
    history = chat.history_messages([
        {"id": 1, "direction": "in", "kind": "chat", "text": "masaüstümde ne var", "created_at": "t1", "delivery": None},
        {"id": 2, "direction": "out", "kind": "chat", "text": "bakıyorum", "created_at": "t2", "delivery": "sent"},
    ], {2: "Masaüstünü listele"}, {})

    async def send(bubble: str) -> None:
        return None

    await chat.respond({}, "openai", "sistem", history + [{"role": "user", "content": "[İŞ RAPORU] bitti"}], [],
                       send, lambda: False, "test")
    assert all("tool_calls" not in message and message["role"] != "tool" for message in seen[0])
    assert {"role": "assistant", "content": "bakıyorum"} in seen[0]


@pytest.mark.asyncio
async def test_toolless_turn_ignores_returned_and_textual_action_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[ToolCallDraft] = [
        {"id": "s1", "name": "start_task", "arguments": json.dumps({"goal": "sil"})},
        {"id": "f1", "name": "forget", "arguments": json.dumps({"fact_id": 4})},
        {"id": "m1", "name": "mute", "arguments": json.dumps({"hours": 24})},
    ]
    model = scripted_model(['rapor <call:recall query="Ela" />'], calls)
    monkeypatch.setattr(chat, "call_model_with_retries", model)
    sent: List[str] = []

    async def send(message: str) -> None:
        sent.append(message)

    result = await chat.respond({}, "openai", "sistem", [], [], send, lambda: False, "report")
    assert sent == ['rapor <call:recall query="Ela" />']
    assert result == {"bubbles": sent, "start_task": None}


@pytest.mark.asyncio
async def test_memory_tool_calls_are_returned_and_textual_ones_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    recall_call: ToolCallDraft = {"id": "r1", "name": "recall", "arguments": json.dumps({"query": "İzmir"})}
    forget_call: ToolCallDraft = {"id": "f1", "name": "forget", "arguments": json.dumps({"fact_id": "#3"})}
    result, sent = await run(monkeypatch, scripted_model(["bi bakayım\n"], [recall_call, forget_call]))
    assert sent == ["bi bakayım"] and result["start_task"] is None
    assert result.get("recall") == "İzmir" and result.get("forget") == [3]
    line = 'tamam unuttum <call:forget fact_id="4" />\n'
    result, sent = await run(monkeypatch, scripted_model([line, "RESET", line], []))
    assert sent == ["tamam unuttum"] and result.get("forget") == [4] and "recall" not in result
    result, sent = await run(monkeypatch, scripted_model(["<call:recall query='Ela' />"], []))
    assert sent == [] and result.get("recall") == "Ela"      # yalnız araçlı tur boş yanıt hatası değildir


def test_history_shows_memory_calls_before_the_bubble_that_used_them() -> None:
    history: List[ArchivedMessage] = [
        {"id": 1, "direction": "in", "kind": "chat", "text": "ben nereye gidiyordum", "created_at": "t1",
         "delivery": None},
        {"id": 2, "direction": "out", "kind": "chat", "text": "dur bir bakayım", "created_at": "t2", "delivery": "sent"},
        {"id": 3, "direction": "out", "kind": "chat", "text": "cuma İzmir'e", "created_at": "t3", "delivery": "sent"},
    ]
    calls: Dict[int, List[ChatToolCall]] = {
        2: [{"name": "recall", "arguments": '{"query": "İzmir"}', "result": "[HAFIZA ARAMASI: İzmir] sonuç"}]}
    messages = chat.history_messages(history, {}, calls)
    assert messages[0] == {"role": "user", "content": "ben nereye gidiyordum"}
    call = messages[1]["tool_calls"][0]  # type: ignore[index]
    assert messages[1]["content"] == "" and call["function"]["name"] == "recall"
    assert messages[2] == {"role": "tool", "tool_call_id": call["id"], "content": "[HAFIZA ARAMASI: İzmir] sonuç"}
    # Çağrısı olan söz balonu sahipsiz söz sayılmaz; sonraki balonla birleşir.
    assert messages[3] == {"role": "assistant", "content": "dur bir bakayım\ncuma İzmir'e"}


def test_recall_follow_up_is_a_real_tool_exchange() -> None:
    base: List[chat.ChatMessage] = [{"role": "user", "content": "nereye gidiyordum"}]
    result_text = chat.recall_result("İzmir", ['kullanıcı · telegram · 29.09.2026: "cuma İzmir’e gidiyorum"'])
    calls: List[ChatToolCall] = [{"name": "recall", "arguments": '{"query": "İzmir"}', "result": result_text}]
    follow = chat.recall_follow_up(base, ["bi bakayım"], calls)
    assert follow[0] == base[0] and follow[1]["content"] == "bi bakayım"
    assert follow[1]["tool_calls"][0]["function"]["name"] == "recall"  # type: ignore[index]
    assert follow[2]["role"] == "tool" and "cuma İzmir’e gidiyorum" in str(follow[2]["content"])
    assert "'ajan' satırları" in str(follow[2]["content"])
    assert chat.recall_follow_up(base, [], calls)[1]["content"] == ""
    assert base == [{"role": "user", "content": "nereye gidiyordum"}]    # girdi değişmez


def test_forget_claim_counts_only_when_the_user_asked() -> None:
    assert chat.forget_requested("kızımın adını unut") and chat.forget_requested("bunu unutur musun")
    assert chat.forget_requested("hafızandan sil şunu")
    assert not chat.forget_requested("unutma bunu") and not chat.forget_requested("unuttun mu")
    assert chat.claims_forgotten("tamam unuttum") and chat.claims_forgotten("sildim gitti")
    assert not chat.claims_forgotten("unutmam merak etme")


@pytest.mark.asyncio
async def test_forget_promise_is_recovered_with_one_tool_only_call(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: List[List[Dict[str, object]]] = []

    async def model(clients: Dict[str, AsyncOpenAI], messages: List[Dict[str, object]], tool_schemas: object,
                    session_id: str, backend: str, emit: Callable[[AgentEvent], None],
                    should_stop: Callable[[], bool]) -> Tuple[ModelTurn, str]:
        seen.append(messages)
        call: ToolCallDraft = {"id": "f", "name": "forget", "arguments": '{"fact_id": 7}'}
        return {"content": "", "tool_calls": [call], "finish_reason": "tool_calls", "usage": ZERO_USAGE}, backend

    monkeypatch.setattr(chat, "call_model_with_retries", model)
    assert await chat.recover_promised_forget({}, "openai", "sistem", [{"role": "user", "content": "Ela'yı unut"}],
                                              ["tamam unuttum"], lambda: False, "test") == [7]
    assert seen[0][-2] == {"role": "assistant", "content": "tamam unuttum"}
    assert seen[0][-1] == {"role": "user", "content": chat.FORGET_CORRECTION}


def test_dismissive_idioms_are_not_forget_requests_and_correction_has_an_exit() -> None:
    """'boşver unut gitsin' gibi deyim unutma isteği değildir; düzeltme notu hiçbir şey silmemeye izin verir."""
    for text in ("boşver unut gitsin", "neyse unut", "unut gitsin ya", "boş ver unut"):
        assert not chat.forget_requested(text), text
    assert chat.forget_requested("kızımın adını unut") and chat.forget_requested("bunu unutur musun")
    assert "hiçbir araç çağırma" in chat.FORGET_CORRECTION


@pytest.mark.asyncio
@pytest.mark.parametrize("finish_reason,cancel", [("stopped", False), ("stop", True)])
async def test_canceled_completed_turn_has_no_draft_or_effects(monkeypatch, finish_reason, cancel):
    calls = [
        {"id": "s", "name": "start_task", "arguments": '{"goal":"Delete files"}'},
        {"id": "f", "name": "forget", "arguments": '{"fact_id":12}'},
        {"id": "m", "name": "mute", "arguments": '{"hours":24}'},
        {"id": "p", "name": "set_proactive", "arguments": '{"enabled":false}'},
    ]
    async def model(*args):
        args[-2]({"kind": "text_delta", "text": "Draft"})
        return {"content": 'Draft\n<call:forget fact_id="12" />\n<call:start_task goal="Delete files" />\n<call:mute hours="24" />\n<call:set_proactive enabled="false" />',
                "tool_calls": calls, "finish_reason": finish_reason, "usage": ZERO_USAGE}, "openai"
    monkeypatch.setattr(chat, "call_model_with_retries", model)
    sent = []
    async def send(text):
        sent.append(text)
    result = await chat.respond({}, "openai", "system", [], chat.CHAT_TOOLS, send, lambda: cancel, "cancel")
    assert result == {"bubbles": [], "start_task": None}
    assert sent == []


@pytest.mark.asyncio
async def test_cancel_between_final_chunks_stops_delivery_and_effects(monkeypatch):
    monkeypatch.setattr(chat, "call_model_with_retries", scripted_model(["word " * 1800], [
        {"id": "s", "name": "start_task", "arguments": '{"goal":"Delete files"}'}]))
    sent = []
    async def send(text):
        sent.append(text)
    result = await chat.respond({}, "openai", "system", [], chat.CHAT_TOOLS, send, lambda: bool(sent), "cancel")
    assert len(sent) == 1 and len(sent[0]) <= 3500
    assert result == {"bubbles": sent, "start_task": None}


@pytest.mark.asyncio
async def test_large_final_chunks_reconstruct_exact_content(monkeypatch):
    text = "\n\n".join((f"İzmir-Çınar-{n}. " + "word " * 900 + "https://example.test/Çınar") for n in range(6))
    result, sent = await run(monkeypatch, scripted_model([text], []))
    assert len(sent) > 4 and all(len(item) <= 3500 for item in sent)
    assert "".join(sent) == text and result["bubbles"] == sent


@pytest.mark.parametrize("text", ["Ç🧭" * 5000, "name Çınar. https://example.test/" + "a" * 3490 + " end", "\n\n".join("paragraph " * 600 for _ in range(8))])
def test_final_chunk_helper_preserves_tokens_and_unicode(text):
    from omniagent.companion.bubbles import final_chunks
    chunks = final_chunks(text)
    assert "".join(chunks) == text.strip()
    assert all(0 < len(chunk) <= 3500 for chunk in chunks)


@pytest.mark.asyncio
@pytest.mark.parametrize("finish_reason", ["length", "content_filter"])
async def test_incomplete_model_finish_sends_no_partial_or_effects(monkeypatch, finish_reason):
    async def model(*args):
        return {"content": 'Partial draft <call:forget fact_id="12" />', "tool_calls": [
            {"id":"s", "name":"start_task", "arguments":'{"goal":"Delete files"}'}],
            "finish_reason":finish_reason, "usage":ZERO_USAGE}, "openai"
    monkeypatch.setattr(chat, "call_model_with_retries", model)
    sent = []
    async def send(text):
        sent.append(text)
    with pytest.raises(chat.ChatError, match=finish_reason):
        await chat.respond({}, "openai", "system", [], chat.CHAT_TOOLS, send, lambda: False, "incomplete")
    assert sent == []


@pytest.mark.asyncio
async def test_authenticated_final_preserves_indentation_tabs_and_repeated_spaces(monkeypatch):
    text = '```python\nif ready:\n    send("Çınar  Projects")\n\treturn True\n```'
    result, sent = await run(monkeypatch, scripted_model([text], []))
    assert sent == [text]
    assert result == {"bubbles": [text], "start_task": None}


@pytest.mark.asyncio
async def test_authenticated_marker_removal_preserves_unrelated_whitespace(monkeypatch):
    text = ('başlangıç\n'
            '    Çınar  Projects\t<call:start_task goal="List Projects" />  bitti\n'
            '\trecall  <call:recall query="Çınar  Projects" />\tsonuç\n'
            '    forget  <call:forget fact_id="12" />  son\n'
            'bitiş')
    expected = ('başlangıç\n'
                '    Çınar  Projects\t  bitti\n'
                '\trecall  \tsonuç\n'
                '    forget    son\n'
                'bitiş')
    result, sent = await run(monkeypatch, scripted_model([text], []))
    assert sent == [expected]
    assert result == {"bubbles": [expected], "start_task": "List Projects", "recall": "Çınar  Projects", "forget": [12]}
    assert "<call:" not in sent[0]
