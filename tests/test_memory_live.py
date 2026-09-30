"""Canlı kanıtlı hafıza duman testleri: gerçek çıkarım ve Kapı 2 (doğrulayıcı). Yalnız OMNI_LIVE_COMPANION=1 ve
OMNI_LIVE_MEMORY_BACKEND=<profil> verildiğinde çalışır (CI'da atlanır; anahtarlar Keychain'den okunur)."""
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from omniagent.app.agent import close_model_clients, create_model_clients
from omniagent.config import apply_stored_api_keys
from omniagent.memory import learning
from omniagent.memory.personal import evidence_fold, opened_store, utc_iso

pytestmark = pytest.mark.skipif(
    os.environ.get("OMNI_LIVE_COMPANION") != "1",
    reason="canlı model testi: OMNI_LIVE_COMPANION=1 ve OMNI_LIVE_MEMORY_BACKEND gerekir",
)


def candidate(statement: str, quote: str) -> learning.Candidate:
    return {"statement": statement, "quote": quote, "message_id": 1, "category": "kisi", "supersedes": None,
            "follow_up_at": None}


@pytest.mark.asyncio
async def test_live_round_learns_quoted_facts_only_from_user_words(tmp_path: Path,
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    apply_stored_api_keys()
    backend: str = os.environ["OMNI_LIVE_MEMORY_BACKEND"]
    database = tmp_path / "companion.db"
    start = datetime.now(timezone.utc) - timedelta(minutes=10)
    with opened_store(database) as store:
        store.record_channel_message("telegram", "kızımın adı Ela, bu yıl okula başladı", utc_iso(start))
        store.record_incoming(1, "g1", "selam naber", utc_iso(start + timedelta(seconds=5)))
        store.record_incoming(2, "g2", "cuma İzmir’e gidiyorum, annemleri göreceğim",
                              utc_iso(start + timedelta(seconds=10)))
        store.record_outgoing("kızının adı Elif miydi?", "chat", utc_iso(start + timedelta(seconds=15)))
    result = await learning.learn_if_due(backend, database, tmp_path / "memory-learning.lock",
                                         datetime.now(timezone.utc))
    assert result["status"] == "learned", result
    with opened_store(database) as store:
        facts = store.active_facts()
        texts = {item["id"]: item["text"] for item in store.pending_evidence(0, 10)}
    assert any("ela" in evidence_fold(fact["quote"]) for fact in facts)
    assert not any("elif" in evidence_fold(fact["statement"]) for fact in facts)   # ajan sözünden bilgi yok
    assert all(learning.quote_supported(fact["quote"], "in", texts[fact["message_id"]]) for fact in facts)


@pytest.mark.asyncio
async def test_live_second_gate_rejects_inference_and_accepts_plain_support() -> None:
    apply_stored_api_keys()
    backend: str = os.environ["OMNI_LIVE_MEMORY_BACKEND"]
    clients = create_model_clients()
    source = "kızımın adı Ela, bu yıl okula başladı"
    try:
        supported = await learning.verify(clients, backend, candidate("Kullanıcının kızının adı Ela.",
                                                                      "kızımın adı Ela"), source)
        invented = await learning.verify(clients, backend, candidate("Kullanıcının kızı yedi yaşında.",
                                                                     "bu yıl okula başladı"), source)
    finally:
        await close_model_clients(clients)
    assert supported is True
    assert invented is False
