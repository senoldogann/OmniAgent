"""Kanalların kanıtlı hafızaya ince kayıt katmanı ve ana ajanın okuma yüzü (companion.db).

Telegram köprüsü ve masaüstü, kullanıcının kendi sözlerini ('in' mesajı) ve görev raporlarını (kanal etiketli iş
günlüğü) buradan yazar. Her çağrı kısa ömürlü bağlantı açar ve kapatır (üç süreç aynı dosyayı paylaşır). Olay
döngüsünden çağıranlar asyncio.to_thread kullanır; bağlantı bir await boyunca açık kalmaz. Gizli bilgi süzgecine
(memory.user.sensitive_text) takılan kullanıcı sözü hiç yazılmaz; iş günlüğünde gizli metin maskelenir. Kayıt hatası
görevi durdurmaz: yapılandırılmış hata logu yazılır ve süreç içi durum bayrağı dolar (Telegram /status gösterir). Görev
kullanıcının asıl işidir, hafıza yan kayıttır. Ana ajan KANITLI PROFİL bloğunu ve personal_memory aracını buradan okur.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Type, TypedDict

from omniagent.app.types import RunReport
from omniagent.integrations.runtime import AnswerSink, boolean_field
from omniagent.memory.personal import (
    RECALL_LIMIT, ActivityRecord, FactRecord, PersonalStore, RecallHit, SchemaError, local_timezone, opened_store,
    utc_iso, utc_now_iso,
)
from omniagent.memory.profile import MemoryCommand, agent_profile_block, forget_reply, memory_list_text, recall_lines
from omniagent.memory.user import sensitive_text
from omniagent.paths import COMPANION_DB_NAME, companion_db_file

MAX_RECORDED_CHARS: int = 4000
MAX_OUTCOME_CHARS: int = 2000
HIDDEN_TEXT: str = "(gizli bilgi içerdiği için kaydedilmedi)"
_STORE_ERRORS: Tuple[Type[Exception], ...] = (sqlite3.Error, OSError, SchemaError)


class RecordFailure(TypedDict):
    """Süreç içi son kanıtlı hafıza hatası: kanal, işlem (message/task/profile), hata türü, zaman."""

    channel: str
    operation: str
    error_type: str
    at: str


class PersonalMemoryUnavailable(RuntimeError):
    """companion.db henüz yok ya da açılamıyor (personal_memory aracı)."""


# Süreç içi kayıt sağlığı. Son kayıt ya da okuma başarısızsa "last" dolar, sonraki başarılı kayıt temizler; /status
# bunu okur. Spec kayıt fonksiyonlarına `-> None` imzası verdiği için bayrak modül durumudur; yazımlar GIL altında
# atomiktir.
_record_health: Dict[str, RecordFailure] = {}


def companion_db_beside(memory_file: str) -> Path:
    """
    Görevin kullanıcı hafızası dosyasının yanındaki companion.db. Üretimde paths.companion_db_file() ile aynı dosyadır.
    Görevin state_file'ı başka yerdeyse (test, ölçüm) kişisel hafıza da oradan okunur ve gerçek veriye dokunulmaz. Saf.
    """
    return Path(memory_file).with_name(COMPANION_DB_NAME)


def _mark_failure(channel: str, operation: str, error: Exception) -> None:
    _record_health["last"] = {"channel": channel, "operation": operation, "error_type": type(error).__name__,
                              "at": utc_now_iso()}
    logging.error("Kanıtlı hafıza işlemi başarısız; görev sürüyor",
                  extra={"channel": channel, "operation": operation, "error_type": type(error).__name__})


def _mark_success() -> None:
    _record_health.pop("last", None)


def last_record_failure() -> Optional[RecordFailure]:
    """Son kayıt/okuma başarısızsa ayrıntısı; sorun yoksa None."""
    return _record_health.get("last")


def record_failure_line(tz: tzinfo) -> Optional[str]:
    """/status satırı, ör. 'Kanıtlı hafıza kaydı başarısız (14:02, telegram, OperationalError)'; sorun yoksa None."""
    failure: Optional[RecordFailure] = last_record_failure()
    if failure is None:
        return None
    moment: datetime = datetime.fromisoformat(failure["at"]).astimezone(tz)
    what: str = "okunamadı" if failure["operation"] == "profile" else "kaydı başarısız"
    return f"Kanıtlı hafıza {what} ({moment:%H:%M}, {failure['channel']}, {failure['error_type']})"


def record_user_message(channel: str, text: str, created_at: str) -> None:
    """
    Kullanıcının Telegram/masaüstü sözünü 'in' mesajı olarak yazar; bu, öğrenme hattının ve aramanın girdisidir. Gizli
    bilgi süzgecine takılan metin hiç yazılmaz, yalnız sayısı loglanır. Depo hatası yükselmez: loglanır, bayrak dolar.
    Geçersiz kanal programlama hatasıdır ve yükselir (ValueError).
    """
    cleaned: str = text.strip()
    if not cleaned:
        return
    if sensitive_text(cleaned):
        logging.info("Gizli bilgi süzgeci kullanıcı sözünü kanıtlı hafızaya yazmadı",
                     extra={"channel": channel, "filtered": 1})
        return
    try:
        with opened_store(companion_db_file()) as store:
            message_id: int = store.record_channel_message(channel, cleaned[:MAX_RECORDED_CHARS], created_at)
    except _STORE_ERRORS as error:
        _mark_failure(channel, "message", error)
        return
    _mark_success()
    logging.info("Kullanıcı sözü kanıtlı hafızaya yazıldı", extra={"channel": channel, "message_id": message_id})


async def record_user_message_async(channel: str, text: str) -> None:
    """Olay döngüsünden kayıt: zaman şimdidir, bağlantı iş parçacığında açılıp kapanır (await boyunca açık kalmaz)."""
    await asyncio.to_thread(record_user_message, channel, text, utc_now_iso())


def record_task(channel: str, goal: str, outcome: str, success: bool, started_at: str, finished_at: str,
                tokens: int) -> None:
    """
    Görevi iş günlüğüne yazar (kind=task, origin=user, kanal etiketli). Deniz'in [DURUM] bloğu bunu okur. Gizli bilgi
    taşıyan hedef ya da sonuç maskelenir. Depo hatası görevi durdurmaz: loglanır, bayrak dolar.
    """
    record: ActivityRecord = {
        "kind": "task", "origin": "user", "channel": channel,
        "goal": HIDDEN_TEXT if sensitive_text(goal) else goal[:MAX_RECORDED_CHARS], "rationale": "",
        "outcome": HIDDEN_TEXT if sensitive_text(outcome) else outcome[:MAX_OUTCOME_CHARS],
        "success": success, "started_at": started_at, "finished_at": finished_at, "tokens": tokens,
    }
    try:
        with opened_store(companion_db_file()) as store:
            activity_id: int = store.record_activity(record)
    except _STORE_ERRORS as error:
        _mark_failure(channel, "task", error)
        return
    _mark_success()
    logging.info("Görev iş günlüğüne yazıldı", extra={"channel": channel, "activity_id": activity_id,
                                                      "success": success})


def record_report(channel: str, report: RunReport) -> None:
    """Biten koşunun raporunu iş günlüğüne yazar: başlangıç = bitiş − koşu süresi, token = istem + tamamlama."""
    finished: datetime = datetime.now(timezone.utc)
    metrics = report["metrics"]
    started: datetime = finished - timedelta(seconds=float(metrics["elapsed_seconds"]))
    record_task(channel, report["exchange"]["goal"], report["outcome"], report["success"], utc_iso(started),
                utc_iso(finished), int(metrics["prompt_tokens"]) + int(metrics["completion_tokens"]))


def record_failed_task(channel: str, goal: str, error_type: str, started_at: str, tokens: int) -> None:
    """Rapor üretemeyen görevi kaydeder; istisnanın özel içerik taşıyabilen metni arşive girmez."""
    record_task(channel, goal, f"Görev tamamlanamadı ({error_type}).", False, started_at, utc_now_iso(), tokens)


def answer_evidence(title: str, fields: Dict[str, object], values: Dict[str, object]) -> Optional[str]:
    """
    Soru yanıtındaki kullanıcı sözleri: serbest metin alanlarına yazılmış değerler, satır satır. Şunlar kanıt değildir:
    onay kutusu (boolean) yanıtı, seçeneklerden (choices) seçilen değer ve gizli bilgi soran soru (başlık, alan adı ya da
    etiket süzgece takılır). Kanıt yoksa None. Saf.
    """
    if sensitive_text(title):
        return None
    parts: List[str] = []
    for name, spec in fields.items():
        if name.startswith("_") or not isinstance(spec, dict) or boolean_field(spec):
            continue
        value: object = values.get(name)
        label: object = spec.get("label")
        choices: object = spec.get("choices")
        if not isinstance(value, str) or not value.strip():
            continue
        if sensitive_text(name) or (isinstance(label, str) and sensitive_text(label)):
            continue
        if isinstance(choices, list) and value.strip() in [str(choice) for choice in choices]:
            continue
        parts.append(value.strip())
    return "\n".join(parts) if parts else None


def recording_answer(channel: str, sink: AnswerSink) -> AnswerSink:
    """
    Soru kanalını sarar: yanıt aynen döner. İçindeki kullanıcı sözleri (answer_evidence) iş parçacığında kanıtlı
    hafızaya yazılır. Kayıt hatası yanıtı etkilemez.
    """
    async def answer(title: str, fields: Dict[str, object]) -> Dict[str, object]:
        values: Dict[str, object] = await sink(title, fields)
        evidence: Optional[str] = answer_evidence(title, fields, values)
        if evidence is not None:
            await record_user_message_async(channel, evidence)
        return values

    return answer


def load_agent_profile(db_path: Path) -> str:
    """
    Ana ajan isteminin KANITLI PROFİL bloğu (USER MEMORY'nin ardından); dosya ya da bilgi yoksa boş. Okuma hatası görevi
    durdurmaz (hafıza yan kayıttır): loglanır, durum bayrağı dolar, blok boş kalır.
    """
    if not db_path.is_file():
        return ""
    try:
        with opened_store(db_path) as store:
            facts: List[FactRecord] = store.active_facts()
    except _STORE_ERRORS as error:
        _mark_failure("ana ajan", "profile", error)
        return ""
    return agent_profile_block(facts, local_timezone())


def _recall(db_path: Path, query: str) -> List[RecallHit]:
    try:
        with opened_store(db_path) as store:
            return store.recall(query, RECALL_LIMIT)
    except _STORE_ERRORS as error:
        raise PersonalMemoryUnavailable(f"Kanıtlı hafıza açılamadı: {type(error).__name__}: {error}") from error


def _forget(db_path: Path, fact_id: int) -> bool:
    try:
        with opened_store(db_path) as store:
            return store.forget_fact(fact_id, utc_now_iso())
    except _STORE_ERRORS as error:
        raise PersonalMemoryUnavailable(f"Kanıtlı hafıza açılamadı: {type(error).__name__}: {error}") from error


def personal_memory_action(db_path: Path, action: str, query: Optional[str], fact_id: Optional[int]) -> str:
    """
    Ana ajanın personal_memory aracı.
    - recall: sorguyla etkin bilgilerde ve tüm kanalların mesajlarında arar; en çok RECALL_LIMIT birebir parça döner,
      satırlar yönle etiketlidir.
    - forget: etkin bilgiyi unutur. Onay kapısı araç katmanındadır (tools/facade.py, app/tool_execution.py).
    Geçersiz argüman ya da bulunamayan bilgi ValueError verir; dosya yoksa ya da açılamıyorsa PersonalMemoryUnavailable.
    """
    if not db_path.is_file():
        raise PersonalMemoryUnavailable(f"Kanıtlı kişisel hafıza henüz yok: {db_path.name}")
    if action == "recall":
        if query is None or not query.strip():
            raise ValueError("recall için query zorunludur.")
        lines: List[str] = recall_lines(_recall(db_path, query.strip()), local_timezone())
        return (f"KANITLI HAFIZA ARAMASI: {query.strip()[:100]}\n" + ("\n".join(lines) if lines else "sonuç yok")
                + "\n(Yalnız 'kullanıcı' satırları ve [#numara] bilgiler kullanıcı hakkında kanıttır; 'ajan' satırları "
                  "değildir.)")
    if action == "forget":
        if fact_id is None:
            raise ValueError("forget için fact_id zorunludur.")
        if not _forget(db_path, fact_id):
            raise ValueError(f"#{fact_id} numaralı etkin bilgi yok.")
        return f"#{fact_id} unutuldu; artık istemde ve aramada görünmez."
    raise ValueError("action recall ya da forget olmalı.")


def memory_command_reply(store: PersonalStore, command: MemoryCommand, tz: tzinfo) -> str:
    """/hafıza listesi ya da unut N sonucu. Kullanıcının doğrudan komutudur, onay istemez."""
    if command["action"] == "forget" and command["fact_id"] is not None:
        return forget_reply(command["fact_id"], store.forget_fact(command["fact_id"], utc_now_iso()))
    return memory_list_text(store.active_facts(), tz)


def run_memory_command(db_path: Path, command: MemoryCommand) -> str:
    """memory_command_reply'ı kısa ömürlü bağlantıyla çalıştırır (Telegram). Depo hatası loglanır ve açık metin olarak
    kullanıcıya döner."""
    try:
        with opened_store(db_path) as store:
            return memory_command_reply(store, command, local_timezone())
    except _STORE_ERRORS as error:
        logging.error("Hafıza komutu çalıştırılamadı",
                      extra={"action": command["action"], "error_type": type(error).__name__})
        return f"Kanıtlı hafıza açılamadı: {type(error).__name__}"
