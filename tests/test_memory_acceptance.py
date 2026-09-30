"""Faz B+ kabul testleri (ek madde 6 a–d) ve spec ölçüt 3. Gerçek SQLite, gerçek iMessage/Telegram köprüleri ve gerçek
ajan döngüsü kullanılır; model çağrıları sınırda betikli sahte istemcidir."""
import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Tuple

import pytest
from openai import AsyncOpenAI

from omniagent.app import agent as main
from omniagent.app.types import ModelTurn
from omniagent.companion import chat
from omniagent.core.events import AgentEvent
from omniagent.integrations import imessage, telegram
from omniagent.integrations.capabilities import CapabilityService
from omniagent.memory import channels, learning
from omniagent.memory.personal import PersonalStore, opened_store, utc_iso
from tests.test_imessage_bridge import HANDLE, FakeTransport, incoming, settings, settle
from tests.test_memory_learning import ScriptedModel, patch_models, tool_turn
from tests.test_telegram_bridge import FakeAPI

ELA_FACT: Dict[str, object] = {"statement": "Kullanıcının kızının adı Ela.", "quote": "kızımın adı Ela",
                               "category": "kisi", "supersedes": None, "follow_up_at": None}


def update(text: str) -> Dict[str, Any]:
    return {"message": {"chat": {"id": 123, "type": "private"}, "from": {"id": 456}, "text": text}}


class ClosableClient:
    """Sahte model istemcisi: yalnız kapatılabilir. Köprü her görev başında istemcileri yeniler ve öncekileri kapatır;
    model çağrısının kendisi sınırda sahtedir."""

    async def close(self) -> None:
        return None


def telegram_bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> telegram.TelegramBridge:
    """Telegram köprüsü: yalıtılmış veri kökü ve sahte Bot API; ajan döngüsü gerçek, model sınırda sahte."""
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(telegram, "STATE_FILE", str(tmp_path / "cognitive_memory.json"))
    monkeypatch.setattr(telegram, "create_model_clients", lambda: {"ollama-cloud": ClosableClient()})
    monkeypatch.setattr(channels, "_record_health", {})
    bridge = telegram.TelegramBridge(FakeAPI(), {"chat_id": 123, "user_id": 456})
    bridge.integrations = CapabilityService(tmp_path)
    return bridge


async def run_goal(bridge: telegram.TelegramBridge, text: str) -> None:
    await bridge.handle(update(text))
    active = bridge.active
    assert active is not None
    await active


async def close_bridge(bridge: telegram.TelegramBridge) -> None:
    if bridge.integrations is not None:
        await bridge.integrations.close()


def final_answer(text: str) -> Callable[..., Awaitable[Tuple[Dict[str, Any], str]]]:
    async def model(clients: Any, messages: Any, schemas: Any, session_id: str, backend: str, emit: Any,
                    should_stop: Any) -> Tuple[Dict[str, Any], str]:
        emit({"kind": "text_delta", "text": text})
        return {"content": text, "tool_calls": [], "finish_reason": "stop", "usage": main.ZERO_USAGE}, backend
    return model


class DenizCapture:
    """chat.respond sınırında sahte Deniz: sistem istemini saklar, tek balonla cevap verir."""

    def __init__(self, bubble: str) -> None:
        self.bubble = bubble
        self.systems: List[str] = []

    async def __call__(self, clients: Dict[str, AsyncOpenAI], backend: str, system: str,
                       messages: List[Dict[str, object]], tools: List[Dict[str, object]],
                       send_bubble: Callable[[str], Awaitable[None]],
                       should_stop: Callable[[], bool], session_id: str) -> chat.ChatResult:
        self.systems.append(system)
        await send_bubble(self.bubble)
        return {"bubbles": [self.bubble], "start_task": None}


def assert_evidence_invariant(database: Path) -> None:
    """Spec ölçüt 3: her bilgi bir 'in' mesajına bağlıdır ve alıntısı o mesajın normalize metninde birebir geçer."""
    connection = sqlite3.connect(database)
    try:
        rows = connection.execute(
            "SELECT f.quote, m.direction, m.text FROM facts f JOIN messages m ON m.id = f.message_id").fetchall()
    finally:
        connection.close()
    assert rows and all(learning.quote_supported(str(quote), str(direction), str(text))
                        for quote, direction, text in rows)


@pytest.mark.asyncio
async def test_a_telegram_words_become_a_quoted_fact_deniz_knows(tmp_path: Path,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    database = tmp_path / "companion.db"
    bridge = telegram_bridge(tmp_path, monkeypatch)
    monkeypatch.setattr(main, "_call_model_with_retries", final_answer("Not aldım."))
    try:
        await run_goal(bridge, "kızımın adı Ela")
    finally:
        await close_bridge(bridge)
    with opened_store(database) as store:
        [source] = store.pending_evidence(0, 10)
    patch_models(monkeypatch, ScriptedModel([
        tool_turn("record_facts", {"facts": [{**ELA_FACT, "message_id": source["id"]}]}),
        tool_turn("verdict", {"answer": "evet"}),
    ]))
    result = await learning.learn_if_due("openai", database, tmp_path / "memory-learning.lock",
                                         datetime.now(timezone.utc) + timedelta(seconds=181))
    assert result == {"status": "learned", "processed": 1, "accepted": 1}
    capture = DenizCapture("Ela tabii")
    monkeypatch.setattr(chat, "respond", capture)
    store = PersonalStore(database)
    try:
        deniz = imessage.ImessageBridge(FakeTransport(), settings(), store, {}, "# Deniz\nyakın arkadaş", "test")
        await deniz.on_message(incoming(1, "kızımın adı neydi", HANDLE))
        await settle(deniz)
    finally:
        store.close()
    assert "Kullanıcının kızının adı Ela." in capture.systems[0] and '— "kızımın adı Ela"' in capture.systems[0]
    assert_evidence_invariant(database)


@pytest.mark.asyncio
async def test_b_words_told_to_deniz_are_recalled_verbatim_by_the_telegram_agent(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = tmp_path / "companion.db"
    bridge = telegram_bridge(tmp_path, monkeypatch)
    monkeypatch.setattr(chat, "respond", DenizCapture("süper, iyi yolculuklar"))
    store = PersonalStore(database)
    try:
        deniz = imessage.ImessageBridge(FakeTransport(), settings(), store, {}, "# Deniz\nyakın arkadaş", "test")
        await deniz.on_message(incoming(1, "cuma İzmir’e gidiyorum", HANDLE))
        await settle(deniz)
    finally:
        store.close()
    offered: List[List[str]] = []
    tool_outputs: List[str] = []

    async def agent_model(clients: Any, messages: Any, schemas: Any, session_id: str, backend: str, emit: Any,
                          should_stop: Any) -> Tuple[Dict[str, Any], str]:
        offered.append([schema["function"]["name"] for schema in schemas])
        outputs = [str(message.get("content")) for message in messages if message.get("role") == "tool"]
        if not outputs:
            return {"content": "", "tool_calls": [{"id": "pm-1", "name": "personal_memory", "arguments": json.dumps(
                {"action": "recall", "query": "İzmir", "fact_id": None})}],
                "finish_reason": "tool_calls", "usage": main.ZERO_USAGE}, backend
        tool_outputs.extend(outputs)
        emit({"kind": "text_delta", "text": "Cuma İzmir'e gidiyorsun."})
        return {"content": "Cuma İzmir'e gidiyorsun.", "tool_calls": [], "finish_reason": "stop",
                "usage": main.ZERO_USAGE}, backend

    monkeypatch.setattr(main, "_call_model_with_retries", agent_model)
    try:
        await run_goal(bridge, "İzmir'e ne zaman gidiyordum?")
    finally:
        await close_bridge(bridge)
    assert "personal_memory" in offered[0]
    assert any("kullanıcı · imessage ·" in output and '"cuma İzmir’e gidiyorum"' in output for output in tool_outputs)


@pytest.mark.asyncio
async def test_c_secret_telegram_goal_never_enters_messages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bridge = telegram_bridge(tmp_path, monkeypatch)
    monkeypatch.setattr(main, "_call_model_with_retries", final_answer("Sırları sohbetle paylaşma; Ayarlar'a gir."))
    try:
        await run_goal(bridge, "GitHub token'ım ghp_0123456789abcdefghij, bunu .env dosyasına yaz")
        await run_goal(bridge, "kızımın adı Ela")
    finally:
        await close_bridge(bridge)
    connection = sqlite3.connect(tmp_path / "companion.db")
    try:
        texts = [str(row[0]) for row in connection.execute("SELECT text FROM messages ORDER BY id")]
        goals = [str(row[0]) for row in connection.execute("SELECT goal FROM activity ORDER BY id")]
        indexed = connection.execute("SELECT count(*) FROM messages_fts WHERE messages_fts MATCH ?",
                                     ('"ghp"*',)).fetchone()[0]
    finally:
        connection.close()
    assert texts == ["kızımın adı Ela"] and indexed == 0
    assert goals == [channels.HIDDEN_TEXT, "kızımın adı Ela"]
    assert all("ghp_" not in text for text in texts + goals)
    assert channels.last_record_failure() is None       # süzmek bir hata değildir


@pytest.mark.asyncio
async def test_d_two_bridges_triggering_together_process_each_message_once(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """İki köprü: aynı kilit dosyasında iki bağımsız flock (macOS'ta iki sürecin kilidiyle aynı anlam)."""
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    database, lock = tmp_path / "companion.db", tmp_path / "memory-learning.lock"
    start = datetime.now(timezone.utc) - timedelta(minutes=10)
    with opened_store(database) as store:
        ids = [store.record_channel_message("telegram", f"{index}. not: kızımın adı Ela",
                                            utc_iso(start + timedelta(seconds=index))) for index in range(3)]
    extraction_inputs: List[str] = []

    async def model(clients: Any, messages: List[Dict[str, object]], tool_schemas: Any, session_id: str,
                    backend: str, emit: Callable[[AgentEvent], None],
                    should_stop: Callable[[], bool]) -> Tuple[ModelTurn, str]:
        if tool_schemas[0]["function"]["name"] == "record_facts":
            extraction_inputs.append(str(messages[-1]["content"]))
            await asyncio.sleep(0.2)   # ilk köprü kilidi tutarken ikinci köprü dener
            return tool_turn("record_facts", {"facts": [{**ELA_FACT, "message_id": ids[0]}]}), backend
        return tool_turn("verdict", {"answer": "evet"}), backend

    patch_models(monkeypatch, model)
    now = datetime.now(timezone.utc)
    results = await asyncio.gather(learning.learn_if_due("openai", database, lock, now),
                                   learning.learn_if_due("openai", database, lock, now))
    assert sorted(result["status"] for result in results) == ["busy", "learned"]
    assert len(extraction_inputs) == 1 and all(f"#{message_id} ·" in extraction_inputs[0] for message_id in ids)
    assert (await learning.learn_if_due("openai", database, lock, now + timedelta(minutes=1)))["status"] == "not_due"
    with opened_store(database) as store:
        assert len(store.active_facts()) == 1
        # Kilidi atlayan bayat bir tur eski imleçle yazmaya kalkarsa reddedilir (karşılaştır-ve-yaz).
        assert store.commit_learning(0, ids[-1], [], utc_iso(now)) is None
    assert_evidence_invariant(database)
