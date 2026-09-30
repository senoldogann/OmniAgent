"""Tek öğrenme hattı: Kapı 1 (Türkçe normalizasyon), tetik ve bekleme, çıkarım + Kapı 2 turu, supersedes/follow_up
kuralları, başarısızlıkta sabit imleç, kilit. Gerçek SQLite; model çağrısı sınırda betikli sahte istemci."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Tuple

import pytest

from omniagent.app.model_runtime import ZERO_USAGE
from omniagent.app.types import ModelTurn, ToolCallDraft
from omniagent.core.events import AgentEvent
from omniagent.memory import learning
from omniagent.memory.personal import FactRecord, LearningStatus, opened_store, utc_iso
from omniagent.paths import memory_learning_lock_file
from omniagent.platform.macos.host_lock import host_task_lock

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)


def at(seconds: float) -> str:
    return utc_iso(NOW + timedelta(seconds=seconds))


def tool_turn(name: str, arguments: Dict[str, object]) -> ModelTurn:
    return {"content": "", "tool_calls": [{"id": f"{name}-1", "name": name,
                                           "arguments": json.dumps(arguments, ensure_ascii=False)}],
            "finish_reason": "tool_calls", "usage": ZERO_USAGE}


class ScriptedModel:
    """call_model_with_retries sınırında sahte model: turları sırayla döndürür, son istem mesajını saklar."""

    def __init__(self, turns: List[ModelTurn]) -> None:
        self.turns = turns
        self.prompts: List[str] = []

    async def __call__(self, clients: Dict[str, object], messages: List[Dict[str, object]], tool_schemas: object,
                       session_id: str, backend: str, emit: Callable[[AgentEvent], None],
                       should_stop: Callable[[], bool]) -> Tuple[ModelTurn, str]:
        self.prompts.append(str(messages[-1]["content"]))
        return self.turns.pop(0), backend


def patch_models(monkeypatch: pytest.MonkeyPatch, model: Callable[..., Awaitable[Tuple[ModelTurn, str]]]) -> None:
    """Öğrenme hattının model sınırını sahteler: istemci kurma/kapama ve call_model_with_retries."""
    async def no_close(clients: Dict[str, object]) -> None:
        return None

    monkeypatch.setattr(learning, "create_model_clients", lambda: {"openai": object()})
    monkeypatch.setattr(learning, "close_model_clients", no_close)
    monkeypatch.setattr(learning, "call_model_with_retries", model)


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    return tmp_path / "companion.db"


@pytest.mark.parametrize("quote, text, expected", [
    ("kızımın adı Ela", "Kızımın adı Ela, 5 yaşında", True),            # büyük/küçük harf
    ("IŞIK AÇIK KALDI", "ışık açık kaldı galiba", True),                 # Türkçe I/ı
    ("cuma İzmir'e gidiyorum", "cuma İzmir’e gidiyorum!", True),         # kıvrık kesme işareti, noktalama
    ("kızımın   adı\nEla", "kızımın adı Ela", True),                     # boşluk sıkıştırma
    ("şeker yemeyi bıraktım", "seker yemeyi biraktim", True),            # aksan
    ("cuma izmire gidiyorum", "cuma İzmir’e gidiyorum", False),         # kelimeler birleştirilmiş: birebir değil
    ("adı Ela", "kızımın adı Ela", False),                               # 3 kelimeden kısa
    ("a b c d", "a b c d e", False),                                     # 12 karakterden kısa
    ("ımın adı Ela", "kızımın adı Ela", False),                          # kelime ortasından başlayan alıntı
    ("kızımın adı Elif", "kızımın adı Ela", False),                      # uydurma
])
def test_quote_supported(quote: str, text: str, expected: bool) -> None:
    assert learning.quote_supported(quote, "in", text) is expected


def test_agent_messages_are_never_evidence() -> None:
    assert not learning.quote_supported("kızımın adı Ela", "out", "kızımın adı Ela")


def test_trigger_needs_twenty_messages_or_three_minutes_of_silence_and_backs_off() -> None:
    quiet: LearningStatus = {"pending": 3, "last_in_at": at(0), "failed_at": None}
    assert not learning.learning_due(quiet, NOW + timedelta(seconds=179))
    assert learning.learning_due(quiet, NOW + timedelta(seconds=180))
    assert learning.learning_due({"pending": 20, "last_in_at": at(0), "failed_at": None}, NOW + timedelta(seconds=1))
    assert not learning.learning_due({"pending": 0, "last_in_at": at(0), "failed_at": None}, NOW + timedelta(hours=1))
    failed: LearningStatus = {"pending": 3, "last_in_at": at(0), "failed_at": at(200)}
    assert not learning.learning_due(failed, NOW + timedelta(seconds=200 + 899))
    assert learning.learning_due(failed, NOW + timedelta(seconds=200 + 900))


def fact_record(fact_id: int, category: str) -> FactRecord:
    return {"id": fact_id, "statement": "x", "quote": "x", "message_id": 1, "category": category, "status": "active",
            "follow_up_at": None, "created_at": at(0), "updated_at": at(0), "said_at": at(0)}


def test_pure_rules_for_follow_up_supersedes_verdict_and_parsing() -> None:
    tz = timezone(timedelta(hours=3))
    said = utc_iso(datetime(2026, 9, 30, 9, 0, tzinfo=timezone.utc))
    assert learning.follow_up_value("2026-10-02T14:00", said, tz) == utc_iso(
        datetime(2026, 10, 2, 11, 0, tzinfo=timezone.utc))
    assert learning.follow_up_value("2026-10-02", said, tz) == utc_iso(datetime(2026, 10, 1, 21, 0, tzinfo=timezone.utc))
    assert learning.follow_up_value("2026-09-29T10:00", said, tz) is None          # mesajdan önce
    assert learning.follow_up_value("2027-10-01T10:00+03:00", said, tz) is None    # 1 yıldan uzak
    assert learning.follow_up_value("cuma", said, tz) is None and learning.follow_up_value(None, said, tz) is None
    active = {4: fact_record(4, "durum")}
    assert learning.supersedes_value(4, "durum", active) == 4
    assert learning.supersedes_value(4, "plan", active) is None
    assert learning.supersedes_value(9, "durum", active) is None
    assert learning.supersedes_value(None, "durum", active) is None

    def verdict(arguments: str) -> List[ToolCallDraft]:
        return [{"id": "v", "name": "verdict", "arguments": arguments}]

    assert learning.verdict_is_yes(verdict('{"answer": "evet"}'))
    for arguments in ('{"answer": "hayir"}', '{"answer": "emin_degilim"}', '{"answer": "Evet"}', "{bozuk", "{}"):
        assert not learning.verdict_is_yes(verdict(arguments))
    assert not learning.verdict_is_yes([])
    with pytest.raises(learning.LearningError, match="record_facts"):
        learning.parse_candidates([])
    with pytest.raises(learning.LearningError, match="JSON"):
        learning.parse_candidates([{"id": "x", "name": "record_facts", "arguments": "{bozuk"}])
    candidates, malformed = learning.parse_candidates([{"id": "x", "name": "record_facts", "arguments": json.dumps({
        "facts": [{"statement": "a"}, {"statement": "Kullanıcı sade kahve sever.", "quote": "kahveyi sade severim",
                                       "message_id": 3, "category": "tercih", "supersedes": True,
                                       "follow_up_at": 5}]})}])
    assert malformed == 1 and candidates[0]["supersedes"] is None and candidates[0]["follow_up_at"] is None


@pytest.mark.asyncio
async def test_round_keeps_only_verified_quotes_from_the_users_own_words(db: Path,
                                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    with opened_store(db) as store:
        ela = store.record_channel_message("telegram", "kızımın adı Ela, 5 yaşında", at(0))
        trip = store.record_incoming(1, "g1", "cuma İzmir’e gidiyorum", at(10))
        agent = store.record_outgoing("kızın Elif miydi?", "chat", at(11))
        secret = store.record_incoming(2, "g2", "wifi parolam kedi1234 unutma", at(20))
    assert trip is not None and secret is not None
    candidates: List[Dict[str, object]] = [
        {"statement": "Kullanıcının kızının adı Ela.", "quote": "kızımın adı Ela", "message_id": ela,
         "category": "kisi", "supersedes": None, "follow_up_at": None},
        {"statement": "Kullanıcının kızının adı Elif.", "quote": "kızımın adı Elif", "message_id": ela,
         "category": "kisi", "supersedes": None, "follow_up_at": None},
        {"statement": "Kullanıcının kızı Elif.", "quote": "kızın Elif miydi", "message_id": agent,
         "category": "kisi", "supersedes": None, "follow_up_at": None},
        {"statement": "Kullanıcının wifi parolası kedi1234.", "quote": "wifi parolam kedi1234", "message_id": secret,
         "category": "durum", "supersedes": None, "follow_up_at": None},
        {"statement": "Kullanıcı cuma İzmir'e gidiyor.", "quote": "cuma İzmir'e gidiyorum", "message_id": trip,
         "category": "plan", "supersedes": None, "follow_up_at": "2026-10-02T09:00"},
        {"statement": 5},
    ]
    model = ScriptedModel([tool_turn("record_facts", {"facts": candidates}), tool_turn("verdict", {"answer": "evet"}),
                           tool_turn("verdict", {"answer": "hayir"})])
    patch_models(monkeypatch, model)
    result = await learning.learn_if_due("openai", db, memory_learning_lock_file(), NOW + timedelta(minutes=5))
    assert result == {"status": "learned", "processed": 3, "accepted": 1}
    with opened_store(db) as store:
        facts = store.active_facts()
        assert store.memory_cursor() == secret and store.learning_failure() is None
    assert [(fact["statement"], fact["quote"], fact["message_id"]) for fact in facts] == [
        ("Kullanıcının kızının adı Ela.", "kızımın adı Ela", ela)]
    extraction = model.prompts[0]
    assert f"#{ela} · telegram ·" in extraction and f"#{trip} · imessage ·" in extraction
    assert "kızın Elif miydi" not in extraction and "kedi1234" not in extraction  # ajan sözü ve gizli bilgi istemde yok
    assert "Kullanıcının kızının adı Ela." in model.prompts[1] and "Kullanıcı cuma İzmir'e gidiyor." in model.prompts[2]
    assert len(model.prompts) == 3                                  # Kapı 1'e takılanlar doğrulayıcıya gitmez


@pytest.mark.asyncio
async def test_supersedes_needs_same_category_and_follow_up_stays_within_a_year(
        db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with opened_store(db) as store:
        home = store.record_channel_message("telegram", "İzmir’de yaşıyorum", at(0))
        inserted = store.commit_learning(0, home, [
            {"statement": "Kullanıcı İzmir'de yaşıyor.", "quote": "İzmir’de yaşıyorum", "message_id": home,
             "category": "durum", "supersedes": None, "follow_up_at": None}], at(1))
        moved = store.record_channel_message("telegram", "artık Ankara’da yaşıyorum, taşındım", at(100))
        dentist = store.record_channel_message("telegram", "cuma öğleden sonra dişçiye gideceğim", at(101))
        someday = store.record_channel_message("telegram", "bir gün Japonya'ya gideceğim kesin", at(102))
    assert inserted is not None
    old = inserted[0]
    candidates: List[Dict[str, object]] = [
        {"statement": "Kullanıcı Ankara'da yaşıyor.", "quote": "artık Ankara’da yaşıyorum", "message_id": moved,
         "category": "durum", "supersedes": old, "follow_up_at": None},
        {"statement": "Kullanıcı cuma dişçiye gidecek.", "quote": "cuma öğleden sonra dişçiye gideceğim",
         "message_id": dentist, "category": "plan", "supersedes": old, "follow_up_at": "2026-10-02T14:00"},
        {"statement": "Kullanıcı bir gün Japonya'ya gidecek.", "quote": "bir gün Japonya'ya gideceğim",
         "message_id": someday, "category": "plan", "supersedes": None, "follow_up_at": "2031-01-01T00:00"},
    ]
    yes = tool_turn("verdict", {"answer": "evet"})
    patch_models(monkeypatch, ScriptedModel([tool_turn("record_facts", {"facts": candidates}), yes, yes, yes]))
    now = NOW + timedelta(minutes=5)
    assert (await learning.learn_if_due("openai", db, memory_learning_lock_file(), now))["accepted"] == 3
    with opened_store(db) as store:
        facts = {fact["statement"]: fact for fact in store.active_facts()}
        replaced = store.connection.execute("SELECT status, superseded_by FROM facts WHERE id = ?", (old,)).fetchone()
    assert "Kullanıcı İzmir'de yaşıyor." not in facts
    assert (replaced["status"], replaced["superseded_by"]) == ("superseded", facts["Kullanıcı Ankara'da yaşıyor."]["id"])
    local = now.astimezone().tzinfo
    assert facts["Kullanıcı cuma dişçiye gidecek."]["follow_up_at"] == utc_iso(datetime(2026, 10, 2, 14, 0, tzinfo=local))
    assert facts["Kullanıcı bir gün Japonya'ya gidecek."]["follow_up_at"] is None


@pytest.mark.asyncio
async def test_failed_round_keeps_the_cursor_records_the_error_and_backs_off(db: Path,
                                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    with opened_store(db) as store:
        store.record_channel_message("telegram", "kızımın adı Ela", at(0))
    text_only: ModelTurn = {"content": "kızının adı Ela", "tool_calls": [], "finish_reason": "stop",
                            "usage": ZERO_USAGE}
    patch_models(monkeypatch, ScriptedModel([text_only]))
    lock = memory_learning_lock_file()
    assert await learning.learn_if_due("openai", db, lock, NOW + timedelta(minutes=5)) == {
        "status": "failed", "processed": 1, "accepted": 0}
    with opened_store(db) as store:
        failure = store.learning_failure()
        assert store.memory_cursor() == 0 and store.active_facts() == []
    assert failure is not None and failure["error_type"] == "LearningError" and "record_facts" in failure["reason"]
    # 15 dk dolmadan yeni model çağrısı yok (betik boş: çağrılsaydı IndexError).
    assert (await learning.learn_if_due("openai", db, lock, NOW + timedelta(minutes=10)))["status"] == "not_due"


@pytest.mark.asyncio
async def test_round_is_skipped_while_another_bridge_holds_the_lock(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with opened_store(db) as store:
        store.record_channel_message("telegram", "kızımın adı Ela", at(0))
    patch_models(monkeypatch, ScriptedModel([]))
    lock = memory_learning_lock_file()
    assert lock.name == "memory-learning.lock"
    with host_task_lock(lock):
        assert (await learning.learn_if_due("openai", db, lock, NOW + timedelta(minutes=5)))["status"] == "busy"
    with opened_store(db) as store:
        assert store.memory_cursor() == 0
