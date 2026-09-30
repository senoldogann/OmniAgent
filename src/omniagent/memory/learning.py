"""Kanallar arası kanıtlı hafızanın tek öğrenme hattı (spec Bileşen 6 ve ek "Faz B+").

Girdi yalnız kullanıcının kendi sözleridir: memory_cursor'dan sonraki 'in' mesajları (iMessage, Telegram, masaüstü).
Gizli bilgi süzgecine takılan mesaj modele hiç gönderilmez.

Tetik: PENDING_TRIGGER işlenmemiş mesaj ya da son kullanıcı mesajından SILENCE_SECONDS sessizlik. Başarısız turdan
sonra RETRY_AFTER_FAILURE_SECONDS beklenir.

Aynı anda tek tur çalışır. memory-learning.lock bloklamadan alınır; alamayan köprü turu atlar. İmleç kilit alındıktan
sonra okunur, sonuç karşılaştır-ve-yaz ile imleçle aynı işlemde yazılır.

Bir tur şöyle ilerler:
1. Çıkarım record_facts aracıyla yapılandırılmış çıktı verir (memory_backend).
2. Kapı 1 (quote_supported) ve gizli bilgi süzgeci deterministiktir.
3. Kapı 2'de doğrulayıcı modelden yalnız verdict='evet' kabul edilir.

Başarısızlıkta (model hatası, araç çağrısı yok, bozuk JSON, profil hazır değil) imleç ilerlemez ve hata state'e yazılır
(/durum). Model beklerken veritabanı bağlantısı açık tutulmaz. Loglar yalnız kimlik ve sayı taşır.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple, TypedDict

from openai import AsyncOpenAI

from omniagent.app.agent import call_model_with_retries, close_model_clients, create_model_clients
from omniagent.app.constants import TURKISH_WEEKDAYS
from omniagent.app.model_retry import ModelCallFailed
from omniagent.app.types import ToolCallDraft
from omniagent.config import apply_model_preferences
from omniagent.core.events import AgentEvent
from omniagent.fallback_policy import FallbackNotPermitted
from omniagent.memory.personal import (
    FACT_CATEGORIES, EvidenceMessage, FactRecord, LearningFailure, LearningStatus, NewFact, evidence_fold,
    opened_store, utc_iso,
)
from omniagent.memory.user import sensitive_text
from omniagent.platform.macos.host_lock import HostBusyError, host_task_lock

SILENCE_SECONDS: float = 180.0
PENDING_TRIGGER: int = 20
BATCH_LIMIT: int = 20
RETRY_AFTER_FAILURE_SECONDS: float = 900.0
POLL_SECONDS: float = 30.0
MIN_QUOTE_WORDS: int = 3
MIN_QUOTE_CHARS: int = 12
MAX_STATEMENT_CHARS: int = 300
MAX_PROMPT_MESSAGE_CHARS: int = 2000
ACTIVE_CONTEXT_LIMIT: int = 150
FOLLOW_UP_MAX_DAYS: int = 365
SESSION_ID: str = "memory-learning"
RECORD_FACTS: str = "record_facts"
VERDICT: str = "verdict"
YES: str = "evet"

EXTRACTION_SYSTEM: str = """Kullanıcının kendi mesajlarından, kullanıcı hakkında kalıcı ve kanıtlanabilir bilgiler çıkarıyorsun.
Kurallar:
- Yalnız [MESAJLAR] listesini kullan. Her bilgi tek bir mesaja dayanır; message_id o mesajın #numarasıdır.
- quote: o mesajdan BİREBİR kopyalanmış, en az 3 kelimelik kesintisiz parça. Kelimeleri değiştirme, düzeltme,
  özetleme, sırasını bozma.
- statement: bilginin tek cümlelik, üçüncü şahıs ifadesi (ör. "Kullanıcının kızının adı Ela."). Alıntının
  söylemediği hiçbir şeyi ekleme; tahmin ve yorum yok.
- category: kisi (aile, arkadaş, tanıdık), tercih (sevdiği, sevmediği, alışkanlığı), plan (niyet, randevu,
  yolculuk), durum (şu anki hâli: iş, sağlık, yaşadığı yer), olay (yaşanmış olay).
- Selamlaşma, soru, bilgisayara verilen iş ("şunu yap", "dosyayı aç"), geçici ruh hâli ve önemsiz ayrıntı bilgi
  değildir. [ETKİN BİLGİLER]'de zaten olanı yeniden ekleme.
- Parola, şifre, token, API anahtarı, kart, IBAN ve kimlik numarası gibi gizli bilgileri asla çıkarma.
- Mesaj etkin bir bilgiyi düzeltiyor ya da güncelliyorsa supersedes o bilginin numarasıdır (aynı kategori);
  değilse null.
- plan ya da olay bir tarih/saat içeriyorsa follow_up_at o andır, yerel saatle "YYYY-AA-GGTSS:DD"; yoksa null.
  "cuma", "yarın" gibi ifadeleri mesajın tarihine ve gününe göre çöz.
- Emin olmadığın bilgiyi ekleme; bilgi yoksa facts boş liste olsun.
- Cevabın yalnız record_facts aracına tek çağrıdır; metin yazma."""

VERIFY_SYSTEM: str = """Bir alıntının, kullanıcı hakkındaki bir ifadeyi ek çıkarım olmadan destekleyip desteklemediğini denetliyorsun.
İfadedeki her bilgi alıntıda açıkça söyleniyorsa: evet. İfade alıntıda olmayan bir ayrıntı, tahmin, yorum ya da
genelleme içeriyorsa ya da alıntı kullanıcıyı değil başkasını anlatıyorsa: hayir. Emin değilsen: emin_degilim.
Mesajın tamamı yalnız bağlamdır; ifadeyi alıntının kendisi desteklemelidir.
Cevabın yalnız verdict aracına tek çağrıdır; metin yazma."""

RECORD_FACTS_TOOL: Dict[str, object] = {
    "type": "function",
    "function": {
        "name": RECORD_FACTS,
        "description": "Mesajlardan çıkarılan kanıtlı bilgileri kaydeder; bilgi yoksa boş liste.",
        "parameters": {
            "type": "object",
            "properties": {
                "facts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "statement": {"type": "string"},
                            "quote": {"type": "string"},
                            "message_id": {"type": "integer"},
                            "category": {"type": "string", "enum": list(FACT_CATEGORIES)},
                            "supersedes": {"type": ["integer", "null"]},
                            "follow_up_at": {"type": ["string", "null"]},
                        },
                        "required": ["statement", "quote", "message_id", "category", "supersedes", "follow_up_at"],
                    },
                },
            },
            "required": ["facts"],
        },
    },
}
VERDICT_TOOL: Dict[str, object] = {
    "type": "function",
    "function": {
        "name": VERDICT,
        "description": "Denetim sonucu: evet, hayir ya da emin_degilim.",
        "parameters": {
            "type": "object",
            "properties": {"answer": {"type": "string", "enum": [YES, "hayir", "emin_degilim"]}},
            "required": ["answer"],
        },
    },
}


class LearningError(Exception):
    """Öğrenme turu kullanılabilir çıktı üretmedi (araç çağrısı yok, bozuk JSON, profil hazır değil); imleç ilerlemez."""


class Candidate(TypedDict):
    statement: str
    quote: str
    message_id: int
    category: str
    supersedes: Optional[int]
    follow_up_at: Optional[str]


class GateCounts(TypedDict):
    candidates: int
    malformed: int
    first_gate: int
    second_gate: int


class RoundResult(TypedDict):
    status: str
    processed: int
    accepted: int


class LearningSnapshot(TypedDict):
    cursor: int
    batch: List[EvidenceMessage]
    active: List[FactRecord]


def quote_supported(quote: str, direction: str, text: str) -> bool:
    """
    Kapı 1: kanıt kullanıcının kendi ('in') mesajıdır; alıntı en az MIN_QUOTE_WORDS kelime ve MIN_QUOTE_CHARS
    karakterdir; iki taraf evidence_fold ile aynı biçimde normalize edildikten sonra alıntı mesaj metninde kelime
    sınırında geçer. Normalizasyon: NFKD, birleşik işaretler atılır, casefold, Türkçe ı/İ → i, noktalama → boşluk,
    boşluk sıkıştırma. Saf.
    """
    if direction != "in":
        return False
    folded: str = evidence_fold(quote)
    if len(folded.split()) < MIN_QUOTE_WORDS or len(folded) < MIN_QUOTE_CHARS:
        return False
    return f" {folded} " in f" {evidence_fold(text)} "


def learning_due(status: LearningStatus, now: datetime) -> bool:
    """
    Tetik: işlenmemiş kullanıcı mesajı varken PENDING_TRIGGER'a ulaşıldı ya da herhangi bir kanaldaki son kullanıcı
    mesajından beri SILENCE_SECONDS geçti. Son başarısız turdan beri RETRY_AFTER_FAILURE_SECONDS geçmediyse beklenir;
    böylece kalıcı bir hata (ör. profil hazır değil) her yoklamada model çağırmaz. Saf.
    """
    if status["pending"] == 0 or status["last_in_at"] is None:
        return False
    failed_at: Optional[str] = status["failed_at"]
    if failed_at is not None and (now - datetime.fromisoformat(failed_at)).total_seconds() < RETRY_AFTER_FAILURE_SECONDS:
        return False
    quiet: float = (now - datetime.fromisoformat(status["last_in_at"])).total_seconds()
    return status["pending"] >= PENDING_TRIGGER or quiet >= SILENCE_SECONDS


def extraction_messages(batch: List[EvidenceMessage], active: List[FactRecord],
                        tz: tzinfo) -> List[Dict[str, object]]:
    """
    Çıkarım istemi. İçerik: etkin bilgiler (numara ve kategoriyle, en yeni ACTIVE_CONTEXT_LIMIT) ve yalnız kullanıcı
    mesajları (numara, kanal, yerel tarih ve gün). Ajan mesajı bu isteme hiç girmez. Saf.
    """
    recent: List[FactRecord] = sorted(active, key=lambda fact: (fact["updated_at"], fact["id"]))[-ACTIVE_CONTEXT_LIMIT:]
    facts_text: str = "\n".join(f"[#{fact['id']}] ({fact['category']}) {fact['statement']}" for fact in recent)
    lines: List[str] = []
    for message in batch:
        moment: datetime = datetime.fromisoformat(message["created_at"]).astimezone(tz)
        lines.append(f"#{message['id']} · {message['channel']} · {moment:%d.%m.%Y %H:%M} "
                     f"{TURKISH_WEEKDAYS[moment.weekday()]}: {message['text'][:MAX_PROMPT_MESSAGE_CHARS]}")
    return [
        {"role": "system", "content": EXTRACTION_SYSTEM},
        {"role": "user", "content": f"[ETKİN BİLGİLER]\n{facts_text or '(yok)'}\n\n[MESAJLAR]\n" + "\n".join(lines)},
    ]


def _candidate(item: object) -> Optional[Candidate]:
    """Tek adayı doğrular. Zorunlu alanı bozuk aday None döner (sayılır, işlenmez); isteğe bağlı alan bozuksa yalnız o
    alan düşer. Saf."""
    if not isinstance(item, dict):
        return None
    statement: object = item.get("statement")
    quote: object = item.get("quote")
    message_id: object = item.get("message_id")
    category: object = item.get("category")
    if not (isinstance(statement, str) and statement.strip() and isinstance(quote, str) and quote.strip()
            and isinstance(message_id, int) and not isinstance(message_id, bool) and isinstance(category, str)):
        return None
    supersedes: object = item.get("supersedes")
    follow_up_at: object = item.get("follow_up_at")
    return {
        "statement": statement.strip(), "quote": quote.strip(), "message_id": message_id, "category": category,
        "supersedes": supersedes if isinstance(supersedes, int) and not isinstance(supersedes, bool) else None,
        "follow_up_at": follow_up_at if isinstance(follow_up_at, str) else None,
    }


def parse_candidates(tool_calls: List[ToolCallDraft]) -> Tuple[List[Candidate], int]:
    """
    record_facts çağrısındaki adaylar ve biçimi bozuk aday sayısı. Çağrı yoksa ya da argüman JSON değilse
    LearningError yükselir: tur başarısız olur, imleç ilerlemez. Hata metni içerik taşımaz. Saf.
    """
    calls: List[ToolCallDraft] = [call for call in tool_calls if call["name"] == RECORD_FACTS]
    if not calls:
        raise LearningError("Çıkarım modeli record_facts aracını çağırmadı.")
    try:
        arguments: object = json.loads(calls[0]["arguments"] or "{}")
    except json.JSONDecodeError as error:
        raise LearningError(f"record_facts argümanı JSON değil: {error.msg} (sütun {error.colno})") from error
    raw_facts: object = arguments.get("facts") if isinstance(arguments, dict) else None
    if not isinstance(raw_facts, list):
        raise LearningError("record_facts 'facts' listesi içermiyor.")
    candidates: List[Candidate] = []
    malformed: int = 0
    for item in raw_facts:
        candidate: Optional[Candidate] = _candidate(item)
        if candidate is None:
            malformed += 1
        else:
            candidates.append(candidate)
    return candidates, malformed


def passes_first_gate(candidate: Candidate, sources: Dict[int, EvidenceMessage], known: Set[str]) -> bool:
    """
    Kapı 1 ve gizli bilgi süzgeci (deterministik). Aday şu koşulların hepsini sağlamalıdır:
    - kategori geçerli;
    - kanıt bu turun bir kullanıcı mesajı ve alıntı onda birebir geçiyor (quote_supported);
    - ifade kısa ve zaten bilinen bir bilginin tekrarı değil;
    - ifade ve alıntı gizli bilgi taşımıyor.
    Saf.
    """
    source: Optional[EvidenceMessage] = sources.get(candidate["message_id"])
    if source is None or candidate["category"] not in FACT_CATEGORIES:
        return False
    if len(candidate["statement"]) > MAX_STATEMENT_CHARS or evidence_fold(candidate["statement"]) in known:
        return False
    if sensitive_text(candidate["statement"]) or sensitive_text(candidate["quote"]):
        return False
    return quote_supported(candidate["quote"], source["direction"], source["text"])


def follow_up_value(raw: Optional[str], said_at: str, tz: tzinfo) -> Optional[str]:
    """
    follow_up_at, mesaj zamanından sonra ve en çok FOLLOW_UP_MAX_DAYS gün içindeyse UTC ISO olarak döner; değilse ya
    da çözülemezse None döner ve yalnız bu alan düşer. Saat dilimsiz değer kullanıcının yerel saatidir (tz). Saf.
    """
    if raw is None or not raw.strip():
        return None
    try:
        moment: datetime = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=tz)
    said: datetime = datetime.fromisoformat(said_at)
    if not said < moment <= said + timedelta(days=FOLLOW_UP_MAX_DAYS):
        return None
    return utc_iso(moment)


def supersedes_value(target: Optional[int], category: str, active: Dict[int, FactRecord]) -> Optional[int]:
    """supersedes yalnız hedef etkin ve aynı kategorideyse tutulur; değilse yalnız bu alan düşer. Saf."""
    fact: Optional[FactRecord] = active.get(target) if target is not None else None
    return target if fact is not None and fact["category"] == category else None


def verification_messages(candidate: Candidate, source_text: str) -> List[Dict[str, object]]:
    """Kapı 2 istemi: kaynak mesajın tamamı (bağlam), alıntı ve ifade; JSON dizgisi olarak verilir. Saf."""
    return [
        {"role": "system", "content": VERIFY_SYSTEM},
        {"role": "user", "content": (
            "mesajın tamamı (kullanıcının kendi sözü): "
            f"{json.dumps(source_text[:MAX_PROMPT_MESSAGE_CHARS], ensure_ascii=False)}\n"
            f"alıntı: {json.dumps(candidate['quote'], ensure_ascii=False)}\n"
            f"ifade: {json.dumps(candidate['statement'], ensure_ascii=False)}"
        )},
    ]


def verdict_is_yes(tool_calls: List[ToolCallDraft]) -> bool:
    """Doğrulayıcı yalnız verdict(answer='evet') ile kabul eder. Metin, eksik çağrı, bozuk JSON, 'hayir' ve
    'emin_degilim' ret sayılır. Saf."""
    for call in tool_calls:
        if call["name"] != VERDICT:
            continue
        try:
            arguments: object = json.loads(call["arguments"] or "{}")
        except json.JSONDecodeError:
            return False
        return isinstance(arguments, dict) and arguments.get("answer") == YES
    return False


def _ignore_event(event: AgentEvent) -> None:
    """Öğrenme çağrılarının akışı hiçbir yüzeye gitmez."""


def _never_stop() -> bool:
    # Durdurma, köprü kapanırken görevin iptaliyle olur (CancelledError); model çağrısının kendi bayrağı gerekmez.
    return False


async def verify(clients: Dict[str, AsyncOpenAI], backend: str, candidate: Candidate, source_text: str) -> bool:
    """Kapı 2: 'alıntı ifadeyi ek çıkarım olmadan destekliyor mu?' Model hatası yükselir (tur başarısız sayılır)."""
    turn, _answered_by = await call_model_with_retries(
        clients, verification_messages(candidate, source_text), [VERDICT_TOOL], SESSION_ID, backend, _ignore_event,
        _never_stop,
    )
    return verdict_is_yes(turn["tool_calls"])


async def learn_batch(clients: Dict[str, AsyncOpenAI], backend: str, batch: List[EvidenceMessage],
                      active: List[FactRecord], tz: tzinfo) -> Tuple[List[NewFact], GateCounts]:
    """
    Turun model kısmı. Gizli bilgi taşıyan mesajlar isteme girmez. Sıra: çıkarım (record_facts), Kapı 1 + gizli bilgi
    süzgeci, Kapı 2 (her aday için doğrulayıcı). Kabul edilen adaylarda supersedes ve follow_up_at kurala göre
    süzülür. Model hatası ya da kullanılamaz çıkarım yükselir; kapı retleri yalnız sayılır.
    """
    usable: List[EvidenceMessage] = [message for message in batch if not sensitive_text(message["text"])]
    if not usable:
        return [], {"candidates": 0, "malformed": 0, "first_gate": 0, "second_gate": 0}
    turn, _answered_by = await call_model_with_retries(
        clients, extraction_messages(usable, active, tz), [RECORD_FACTS_TOOL], SESSION_ID, backend, _ignore_event,
        _never_stop,
    )
    candidates, malformed = parse_candidates(turn["tool_calls"])
    sources: Dict[int, EvidenceMessage] = {message["id"]: message for message in usable}
    by_id: Dict[int, FactRecord] = {fact["id"]: fact for fact in active}
    known: Set[str] = {evidence_fold(fact["statement"]) for fact in active}
    accepted: List[NewFact] = []
    first_gate: int = 0
    second_gate: int = 0
    for candidate in candidates:
        if not passes_first_gate(candidate, sources, known):
            first_gate += 1
            continue
        source: EvidenceMessage = sources[candidate["message_id"]]
        if not await verify(clients, backend, candidate, source["text"]):
            second_gate += 1
            continue
        known = known | {evidence_fold(candidate["statement"])}
        accepted.append({
            "statement": candidate["statement"], "quote": candidate["quote"], "message_id": candidate["message_id"],
            "category": candidate["category"],
            "supersedes": supersedes_value(candidate["supersedes"], candidate["category"], by_id),
            "follow_up_at": follow_up_value(candidate["follow_up_at"], source["created_at"], tz),
        })
    return accepted, {"candidates": len(candidates) + malformed, "malformed": malformed, "first_gate": first_gate,
                      "second_gate": second_gate}


def _read_status(db_path: Path) -> LearningStatus:
    with opened_store(db_path) as store:
        return store.learning_status()


def _read_snapshot(db_path: Path) -> LearningSnapshot:
    with opened_store(db_path) as store:
        cursor: int = store.memory_cursor()
        return {"cursor": cursor, "batch": store.pending_evidence(cursor, BATCH_LIMIT), "active": store.active_facts()}


def _commit(db_path: Path, snapshot: LearningSnapshot, facts: List[NewFact], now: str) -> Optional[List[int]]:
    with opened_store(db_path) as store:
        return store.commit_learning(snapshot["cursor"], snapshot["batch"][-1]["id"], facts, now)


def _record_failure(db_path: Path, failure: LearningFailure) -> None:
    with opened_store(db_path) as store:
        store.record_learning_failure(failure)


def _result(status: str, processed: int, accepted: int) -> RoundResult:
    return {"status": status, "processed": processed, "accepted": accepted}


async def _learn_with_clients(backend: str, snapshot: LearningSnapshot,
                              tz: tzinfo) -> Tuple[List[NewFact], GateCounts]:
    """
    Model istemcileri tur başına kurulur ve kapanır. Köprünün sohbet istemcileri paylaşılmaz, çünkü Telegram onları
    görev başında yeniler. Model tercihleri iş devrinde olduğu gibi güncel okunur.
    """
    apply_model_preferences()
    clients: Dict[str, AsyncOpenAI] = await asyncio.to_thread(create_model_clients)
    try:
        if backend not in clients:
            raise LearningError(f"memory_backend profili hazır değil: {backend}")
        return await learn_batch(clients, backend, snapshot["batch"], snapshot["active"], tz)
    finally:
        await close_model_clients(clients)


async def _locked_round(backend: str, db_path: Path, now: datetime) -> RoundResult:
    """Kilit altındaki tur: imleç ve girdi kilitten sonra okunur; sonuç imleçle aynı işlemde yazılır."""
    snapshot: LearningSnapshot = await asyncio.to_thread(_read_snapshot, db_path)
    batch: List[EvidenceMessage] = snapshot["batch"]
    if not batch:
        return _result("not_due", 0, 0)
    local: Optional[tzinfo] = now.astimezone().tzinfo
    if local is None:
        raise RuntimeError("Yerel saat dilimi çözülemedi.")
    try:
        facts, counts = await _learn_with_clients(backend, snapshot, local)
    except (LearningError, ModelCallFailed, FallbackNotPermitted) as error:
        failure: LearningFailure = {
            "at": utc_iso(now), "error_type": type(error).__name__,
            # Model hata metni istem parçası taşıyabilir: yalnız kendi (içeriksiz) gerekçelerimiz saklanır.
            "reason": str(error)[:200] if isinstance(error, LearningError) else "",
        }
        await asyncio.to_thread(_record_failure, db_path, failure)
        logging.error("Hafıza öğrenme turu başarısız; imleç ilerlemedi",
                      extra={"backend": backend, "error_type": failure["error_type"], "batch": len(batch),
                             "cursor": snapshot["cursor"]})
        return _result("failed", len(batch), 0)
    inserted: Optional[List[int]] = await asyncio.to_thread(_commit, db_path, snapshot, facts, utc_iso(now))
    if inserted is None:
        logging.warning("Hafıza öğrenme sonucu yazılmadı: imleç bu sırada ilerlemiş",
                        extra={"backend": backend, "cursor": snapshot["cursor"]})
        return _result("stale", len(batch), 0)
    logging.info("Hafıza öğrenme turu tamamlandı",
                 extra={"backend": backend, "processed": len(batch), "candidates": counts["candidates"],
                        "malformed": counts["malformed"], "rejected_first_gate": counts["first_gate"],
                        "rejected_second_gate": counts["second_gate"], "accepted": len(inserted),
                        "cursor": batch[-1]["id"]})
    return _result("learned", len(batch), len(inserted))


async def learn_if_due(backend: str, db_path: Path, lock_path: Path, now: datetime) -> RoundResult:
    """
    Tetik uygunsa tek tur çalıştırır. Kilit bloklamadan alınır: başka köprü öğreniyorsa 'busy' döner ve tur atlanır.
    Sonuç durumları: not_due, busy, learned, failed, stale (imleç başka bir yazımla ilerlemiş).
    """
    status: LearningStatus = await asyncio.to_thread(_read_status, db_path)
    if not learning_due(status, now):
        return _result("not_due", 0, 0)
    try:
        with host_task_lock(lock_path):
            return await _locked_round(backend, db_path, now)
    except HostBusyError:
        logging.info("Hafıza öğrenme turu atlandı: başka köprü öğreniyor", extra={"backend": backend})
        return _result("busy", 0, 0)


async def learning_loop(backend: Callable[[], Optional[str]], db_path: Path, lock_path: Path) -> None:
    """
    Köprünün arka plan görevi: POLL_SECONDS'ta bir tetiği denetler ve gerekiyorsa bir tur çalıştırır.
    - backend() None ise öğrenme kapalıdır: memory_backend yapılandırılmamış, iMessage kurulu değil.
    - Tek turun beklenmeyen hatası döngüyü durdurmaz; traceback ile loglanır.
    - İptal (köprü kapanışı) döngüyü bitirir.
    """
    while True:
        await asyncio.sleep(POLL_SECONDS)
        try:
            selected: Optional[str] = backend()
            if selected is not None:
                await learn_if_due(selected, db_path, lock_path, datetime.now(timezone.utc))
        except Exception:
            logging.exception("Hafıza öğrenme turu beklenmedik hatayla bitti")
