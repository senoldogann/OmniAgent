"""iMessage köprüsü: eşleşmiş tek kişiden gelen mesajları sohbet katmanına, işleri mevcut ajana taşır.

Tek süreç, tek asyncio döngüsü (Telegram köprüsüyle aynı model). imsg rpc alt süreci mesajları getirir; kullanıcı
mesajı ve imleç aynı SQLite işleminde arşivlenir (memory/personal.py), yanıt bundan sonra üretilir. Art arda gelen
balonlar burst_quiet_seconds sessizlikten sonra tek girdi olur; sohbet turları sırayla işlenir, iş arka planda
sürerken sohbet devam eder. Onaylar evet/hayır ile alınır; evet/hayır olmayan mesaj normal sohbete gider.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import subprocess
import sys
import threading
import time
import uuid
from contextlib import aclosing
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Protocol, Tuple, TypedDict, Union

from openai import AsyncOpenAI

from omniagent.app.agent import close_model_clients, create_model_clients
from omniagent.app.model_retry import ModelCallFailed
from omniagent.companion import chat, delegate, persona
from omniagent.config import BACKENDS, apply_model_preferences, apply_stored_api_keys
from omniagent.core.conversation import Exchange, trim_history
from omniagent.core.log_format import configure_stream_logging
from omniagent.fallback_policy import FallbackNotPermitted
from omniagent.integrations.capabilities import CapabilityService
from omniagent.integrations import imessage_setup
from omniagent.integrations.imessage_rules import (
    accepted, answer_polarity, field_prompt, image_paths, message_text, own_echo, parse_command, question_bubbles,
    status_lines, user_text,
)
from omniagent.integrations.imessage_settings import ImessageConfigError, ImessageSettings, load_settings
from omniagent.integrations.imsg import (
    DeliveryUnknown, ImsgClient, ImsgError, ImsgProcessError, ImsgRpcError, IncomingMessage, SendResult, imsg_command,
)
from omniagent.integrations.runtime import DeliveryFailed, IntegrationStopped, boolean_field, read_json, save_json
from omniagent.memory.personal import ArchivedMessage, PersonalStore, to_utc_iso, utc_iso
from omniagent.memory.user import load_memory, memory_prompt_block
from omniagent.paths import (
    companion_db_file, data_root, imessage_history_file, imessage_settings_file, persona_file, project_root,
    user_memory_file,
)
from omniagent.platform.macos.host_lock import HostBusyError, host_task_lock
from omniagent.platform.macos.launch_agent import LaunchAgentError
from omniagent.platform.macos.power import start_keep_awake, stop_keep_awake

CONFIRM_WINDOW_SECONDS: float = 60.0
UNANSWERED_MAX_AGE_SECONDS: float = 3600.0
INTERIM_AFTER_SECONDS: float = 300.0
SWEEP_SECONDS: float = 30.0
RESTART_DELAYS_SECONDS: Tuple[float, ...] = (1.0, 2.0, 4.0)
STABLE_CONNECTION_SECONDS: float = 60.0
CLOSE_TASK_TIMEOUT_SECONDS: float = 5.0
BUSY_TEXT: str = "elimde bir iş var şu an, bitince bakarım (ya da 'dur' yaz)"
# Model iş sözü verdi ama düzeltme çağrısında da iş başlatmadı: söz tutulamadığı dürüstçe söylenir.
PROMISE_FAILED_TEXT: str = "pardon, işi başlatamadım; bir daha yazar mısın?"
HOST_BUSY_TEXT: str = "bilgisayarda başka bir iş çalışıyor (masaüstü ya da telegram), o bitince tekrar söyler misin?"


class MessageTransport(Protocol):
    """Köprünün gönderim yüzü: canlıda ImsgSession, testlerde sahte taşıyıcı."""

    async def send_text(self, handle: str, text: str) -> SendResult: ...

    async def send_file(self, handle: str, path: Path) -> SendResult: ...


class QuestionPending(RuntimeError):
    """Aynı anda ikinci bir kullanıcı sorusu açılamaz."""


class PendingQuestion(TypedDict):
    title: str
    names: List[str]
    specs: Dict[str, object]
    index: int
    answers: Dict[str, object]
    future: "asyncio.Future[Dict[str, object]]"


class ModelReply(TypedDict):
    """Sohbet modelinin bir turu: sonuç ve gönderilen balonların arşiv kimlikleri (iş başlatan balonu eşlemek için)."""

    result: chat.ChatResult
    sent_ids: List[int]


def load_history(path: Path) -> List[Exchange]:
    """imessage-history.json'daki geçerli görev kayıtları (Telegram köprüsüyle aynı süzgeç)."""
    loaded: object = read_json(path, [])
    if not isinstance(loaded, list):
        raise ImessageConfigError(f"iMessage görev geçmişi liste değil: {path}")
    valid: List[Exchange] = [
        {"goal": entry["goal"], "answer": entry["answer"], "tools": [str(tool) for tool in entry["tools"]]}
        for entry in loaded
        if isinstance(entry, dict) and isinstance(entry.get("goal"), str) and isinstance(entry.get("answer"), str)
        and isinstance(entry.get("tools"), list) and all(isinstance(tool, str) for tool in entry["tools"])
    ]
    return trim_history(valid)


def _send_failure_fields(error: ImsgError) -> Dict[str, object]:
    """
    Gönderim hatasının loglanabilir alanları. Hata metni yazılmaz: ImsgRpcError metni alıcıyı ve gönderilen
    metnin ilk karakterlerini içerir, mesaj içeriği loga girmez. Saf.
    """
    fields: Dict[str, object] = {"error_type": type(error).__name__}
    if isinstance(error, ImsgRpcError):
        fields["method"] = error.method
        fields["code"] = error.code
    return fields


class ImessageBridge:
    """Eşleşmiş tek iMessage sohbetini yönetir: burst → sohbet katmanı → balonlar; işleri ajana devreder."""

    def __init__(self, transport: MessageTransport, settings: ImessageSettings, store: PersonalStore,
                 chat_clients: Dict[str, AsyncOpenAI], persona_text: str, session_id: str) -> None:
        self.transport: MessageTransport = transport
        self.settings: ImessageSettings = settings
        self.store: PersonalStore = store
        self.chat_clients: Dict[str, AsyncOpenAI] = chat_clients
        self.persona_text: str = persona_text
        self.session_id: str = session_id
        self.burst_ids: List[int] = []
        self.burst_texts: List[str] = []
        self.burst_images: List[str] = []
        self.burst_timer: Optional[asyncio.Task[None]] = None
        self.chat_lock: asyncio.Lock = asyncio.Lock()
        self.task: Optional[asyncio.Task[None]] = None
        self.task_goal: str = ""
        self.task_progress: List[str] = []
        self.stop_event: threading.Event = threading.Event()
        self.question: Optional[PendingQuestion] = None
        self.integrations: Optional[CapabilityService] = None
        self.history: List[Exchange] = load_history(imessage_history_file())
        self.closing: bool = False

    # --- gelen satırlar ---

    def _ingest(self, message: IncomingMessage) -> Optional[Tuple[int, str, List[str]]]:
        """
        İzlemedeki satırı sınıflar: kendi yansımamız teslim doğrulamasıdır; eşleşmemiş/grup mesajının içeriği
        hiçbir yere yazılmaz, yalnız imleç ilerler. Yeni kullanıcı mesajı arşivlenir ve (kimlik, metin, görseller)
        döner; yeniden oynatılan satır None.
        """
        handle: str = self.settings["handle"]
        if own_echo(message, handle):
            confirmed: Optional[int] = self.store.confirm_outgoing(
                message_text(message), message["rowid"], message["guid"], datetime.now(timezone.utc),
                CONFIRM_WINDOW_SECONDS,
            )
            if confirmed is None:
                self.store.advance_cursor(message["rowid"])
            return None
        if not accepted(message, handle):
            self.store.advance_cursor(message["rowid"])
            logging.info("iMessage: eşleşmemiş ya da grup mesajı yok sayıldı",
                         extra={"rowid": message["rowid"], "is_group": message["is_group"]})
            return None
        text: str = user_text(message)
        images: List[str] = image_paths(message)
        archived: str = text or ("[fotoğraf]" if images else "")
        if not archived:
            self.store.advance_cursor(message["rowid"])
            return None
        message_id: Optional[int] = self.store.record_incoming(
            message["rowid"], message["guid"], archived, to_utc_iso(message["created_at"]),
        )
        if message_id is None:
            return None
        return message_id, text, images

    def archive_backlog(self, message: IncomingMessage) -> None:
        """Bağlantı kopukken gelen satırı yanıtlamadan arşivler; yanıtsız son burst'ü answer_unanswered ele alır."""
        self._ingest(message)

    async def on_message(self, message: IncomingMessage) -> None:
        """Canlı izlemedeki satır: komut anında işlenir, bekleyen soruya cevap olur ya da burst'e eklenir."""
        ingested = self._ingest(message)
        if ingested is None:
            return
        message_id, text, images = ingested
        command: Optional[str] = parse_command(text) if text else None
        if command is not None:
            await self._command(command)
            return
        if text and self.question is not None and await self._answer_question(text):
            return
        self.burst_ids.append(message_id)
        if text:
            self.burst_texts.append(text)
        self.burst_images.extend(images)
        self._restart_burst_timer()

    async def answer_unanswered(self) -> None:
        """
        Açılışta ve yeniden bağlanınca: son 1 saatteki yanıtsız burst'ü (çökme sırasında gelen) bir kez yanıtlar.
        Bekleyen burst ya da süren tur varsa atlanır; onlar zaten yanıtlanacak.
        """
        if self.burst_ids or self.chat_lock.locked():
            return
        tail: List[ArchivedMessage] = self.store.unanswered_burst(
            datetime.now(timezone.utc), UNANSWERED_MAX_AGE_SECONDS,
        )
        if tail:
            await self._guarded_chat_turn([item["id"] for item in tail], [item["text"] for item in tail], [], None)

    # --- komutlar ve sorular ---

    async def _command(self, command: str) -> None:
        if command == "stop":
            if self.task is None and self.question is None:
                await self._send("şu an çalışan bir iş yok", "chat")
                return
            self.stop_event.set()
            self._cancel_question(IntegrationStopped("Kullanıcı tarafından durduruldu."))
            await self._send("tamam, durduruyorum", "chat")
            return
        midnight: datetime = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        today = self.store.activities_since(midnight)
        lines: List[str] = status_lines(self.task_goal or None, self.task_progress, len(today),
                                        sum(item["tokens"] for item in today), self.store.latency_p50())
        await self._send("\n".join(lines), "chat")

    def _cancel_question(self, error: BaseException) -> None:
        question: Optional[PendingQuestion] = self.question
        if question is not None and not question["future"].done():
            question["future"].set_exception(error)

    async def _answer_question(self, text: str) -> bool:
        """
        Bekleyen soruya cevap. Onay sorusunda evet/hayır olmayan mesaj False döner ve sohbete gider (soru [DURUM]'da
        görünmeye devam eder). Çok alanlı soruda alanlar sırayla sorulur. Soru kapanmışsa False.
        """
        question: Optional[PendingQuestion] = self.question
        if question is None or question["future"].done():
            return False
        name: str = question["names"][question["index"]]
        value: object
        if boolean_field(question["specs"][name]):
            polarity: Optional[bool] = answer_polarity(text)
            if polarity is None:
                return False
            value = polarity
        else:
            value = text
        question["answers"][name] = value
        question["index"] += 1
        if question["index"] < len(question["names"]):
            upcoming: str = question["names"][question["index"]]
            await self._send(field_prompt(upcoming, question["specs"][upcoming]), "question")
            return True
        question["future"].set_result(dict(question["answers"]))
        await self._send("tamam 👍", "chat")
        return True

    async def answer(self, title: str, fields: Dict[str, object]) -> Dict[str, object]:
        """AnswerSink: soruyu balonlarla sorar ve cevabı bekler (süre sınırını IntegrationRuntime.ask uygular)."""
        if self.question is not None:
            raise QuestionPending("Zaten kullanıcıdan cevap bekleniyor.")
        names: List[str] = [name for name in fields if not name.startswith("_")]
        if not names:
            raise ValueError(f"Cevaplanacak alan yok: {title[:120]}")
        future: asyncio.Future[Dict[str, object]] = asyncio.get_running_loop().create_future()
        self.question = {"title": title, "names": names, "specs": {name: fields[name] for name in names},
                         "index": 0, "answers": {}, "future": future}
        try:
            for bubble in question_bubbles(title, fields):
                await self._send(bubble, "question")
            if len(names) > 1:
                await self._send(field_prompt(names[0], fields[names[0]]), "question")
            return await future
        finally:
            self.question = None

    async def deliver(self, path: Path, caption: str) -> None:
        """DeliverSink: dosyayı (varsa açıklamasıyla) eşleşmiş sohbete gönderir; hata DeliveryFailed."""
        try:
            if caption.strip():
                await self._send(caption.strip(), "chat")
            await self.transport.send_file(self.settings["handle"], path)
        except ImsgError as error:
            raise DeliveryFailed(f"iMessage dosya gönderimi başarısız: {error}") from error
        self.store.record_outgoing_file(path.name, utc_iso(datetime.now(timezone.utc)))

    # --- gönderim ---

    async def _send(self, text: str, kind: str) -> int:
        """
        Balonu arşive 'pending' yazıp gönderir ve arşiv kimliğini döner. Sonucu bilinmeyen gönderim (DeliveryUnknown:
        -32001, -32004, zaman aşımı) hata değildir: balon 'unconfirmed' işaretlenir, uyarı loglanır ve yeniden
        gönderilmez (çift mesaj kayıp mesajdan kötüdür); akış sürer. Kesin hata (süreç, RPC) 'unconfirmed'
        işaretlenip yükseltilir.
        """
        message_id: int = self.store.record_outgoing(text, kind, utc_iso(datetime.now(timezone.utc)))
        try:
            await self.transport.send_text(self.settings["handle"], text)
        except DeliveryUnknown as error:
            self.store.mark_unconfirmed(message_id)
            logging.warning("iMessage balonunun teslimi bilinmiyor; yeniden gönderilmiyor",
                            extra={"message_id": message_id, **_send_failure_fields(error)})
        except ImsgError as error:
            self.store.mark_unconfirmed(message_id)
            logging.warning("iMessage balonu gönderilemedi",
                            extra={"message_id": message_id, **_send_failure_fields(error)})
            raise
        return message_id

    # --- sohbet ---

    def _restart_burst_timer(self) -> None:
        if self.burst_timer is not None and not self.burst_timer.done():
            self.burst_timer.cancel()
        self.burst_timer = asyncio.create_task(self._flush_burst_after(self.settings["burst_quiet_seconds"]))

    async def _flush_burst_after(self, delay: float) -> None:
        await asyncio.sleep(delay)
        burst_end: float = time.monotonic()
        ids, texts, images = self.burst_ids, self.burst_texts, self.burst_images
        self.burst_ids, self.burst_texts, self.burst_images = [], [], []
        # Tur bu görevde sürer; yeni mesaj yeni zamanlayıcı kurar, bu turu iptal etmez.
        self.burst_timer = None
        await self._guarded_chat_turn(ids, texts, images, burst_end)

    async def _guarded_chat_turn(self, ids: List[int], texts: List[str], images: List[str],
                                 burst_end: Optional[float]) -> None:
        try:
            await self._chat_turn(ids, texts, images, burst_end)
        except Exception:
            # Arka plan görev sınırı: hata kök nedeniyle (traceback) loglanır, köprü sonraki mesajları işlemeye devam eder.
            logging.exception("iMessage sohbet turu başarısız", extra={"message_ids": ids})

    async def _chat_turn(self, ids: List[int], texts: List[str], images: List[str],
                         burst_end: Optional[float]) -> None:
        async with self.chat_lock:
            recent: List[ArchivedMessage] = self.store.recent_messages(chat.HISTORY_LIMIT + len(ids))
            history: List[ArchivedMessage] = [item for item in recent if item["id"] not in ids][-chat.HISTORY_LIMIT:]
            turn: str = chat.burst_turn(texts, images, self._situation())
            messages: List[chat.ChatMessage] = self._history(history) + [{"role": "user", "content": turn}]
            reply: Optional[ModelReply] = await self._respond(messages, chat.CHAT_TOOLS, burst_end, "chat")
            if reply is not None:
                await self._delegate(reply, messages, images)

    def _history(self, history: List[ArchivedMessage]) -> List[chat.ChatMessage]:
        """Arşivi, iş başlatan balonları gerçek araç çağrısı olarak gösteren sohbet geçmişine çevirir."""
        return chat.history_messages(history, self.store.task_starts([item["id"] for item in history]))

    async def _respond(self, messages: List[chat.ChatMessage], tools: List[Dict[str, object]], burst_end: Optional[float],
                       kind: str) -> Optional[ModelReply]:
        """
        Sohbet katmanını çağırır, balonları gönderir ve ilk balon gecikmesini ölçer. İş başlatmak bu adımın işi
        değildir: karar çağıranındır (`_delegate`; yalnız kullanıcı turundan iş başlar). Model hatasını dürüstçe
        söyler ve None döner.
        """
        first_sent: List[float] = []
        sent_ids: List[int] = []

        async def send_bubble(bubble: str) -> None:
            sent_ids.append(await self._send(bubble, kind))
            if not first_sent:
                first_sent.append(time.monotonic())

        try:
            result: chat.ChatResult = await chat.respond(
                self.chat_clients, self.settings["chat_backend"], self._system_prompt(), messages, tools, send_bubble,
                self._is_closing, self.session_id,
            )
        except (ModelCallFailed, FallbackNotPermitted, chat.ChatError) as error:
            logging.error("Sohbet modeli yanıt veremedi",
                          extra={"backend": self.settings["chat_backend"], "error_type": type(error).__name__,
                                 "error": str(error)[:300]})
            await self._send(f"şu an cevap veremiyorum, model hatası: {str(error)[:160]}", "chat")
            return None
        if burst_end is not None and first_sent:
            latency_ms: float = (first_sent[0] - burst_end) * 1000
            self.store.record_latency(latency_ms)
            logging.info("iMessage ilk balon gecikmesi",
                         extra={"latency_ms": round(latency_ms), "backend": self.settings["chat_backend"]})
        return {"result": result, "sent_ids": sent_ids}

    async def _delegate(self, reply: ModelReply, messages: List[chat.ChatMessage], images: List[str]) -> None:
        """
        Kullanıcı turunun iş kararı; iş yalnız buradan başlar. Model start_task çağırdıysa iş başlar; "bakıyorum"
        deyip çağırmadıysa söz tek düzeltme çağrısıyla tutturulur, tutturulamazsa dürüstçe söylenir. Rapor turu
        buraya gelmez (bkz. `_finish_task`).
        """
        result: chat.ChatResult = reply["result"]
        goal: Optional[str] = result["start_task"]
        if goal is None and any(chat.promises_action(bubble) for bubble in result["bubbles"]):
            goal = await self._recover_promise(messages, result["bubbles"])
            if goal is None:
                await self._send(PROMISE_FAILED_TEXT, "chat")
                return
        if goal is None:
            return
        sent_ids: List[int] = list(reply["sent_ids"])
        if not result["bubbles"]:
            sent_ids.append(await self._send(chat.TASK_ACK, "chat"))
        if await self._start_task(goal, images):
            self.store.record_task_start(sent_ids[-1], goal)

    async def _recover_promise(self, messages: List[chat.ChatMessage], bubbles: List[str]) -> Optional[str]:
        """Söz verilip çağrılmayan işi tek düzeltme çağrısıyla kurtarır; model hatası da tutulamamış söz sayılır."""
        try:
            goal: Optional[str] = await chat.recover_promised_task(
                self.chat_clients, self.settings["chat_backend"], self._system_prompt(), messages, bubbles,
                self._is_closing, self.session_id,
            )
        except (ModelCallFailed, FallbackNotPermitted) as error:
            logging.error("İş sözü düzeltme çağrısı başarısız",
                          extra={"backend": self.settings["chat_backend"], "error_type": type(error).__name__})
            return None
        if goal is None:
            logging.warning("Sohbet modeli iş sözü verdi, düzeltme çağrısında da iş başlatmadı",
                            extra={"bubbles": len(bubbles)})
        return goal

    def _system_prompt(self) -> str:
        return persona.system_prompt(self.persona_text, memory_prompt_block(load_memory(str(user_memory_file()))))

    def _situation(self) -> str:
        pending: Optional[str] = self.question["title"] if self.question is not None else None
        return persona.situation_block(datetime.now().astimezone(), self.task_goal or None, self.task_progress,
                                       pending)

    def _is_closing(self) -> bool:
        return self.closing

    # --- iş ---

    async def _start_task(self, goal: str, images: List[str]) -> bool:
        """İşi başlatır; çalışan iş varsa kullanıcıya söyler ve False döner."""
        if self.task is not None:
            await self._send(BUSY_TEXT, "chat")
            return False
        self.stop_event.clear()
        self.task_goal = goal
        self.task_progress = []
        self.task = asyncio.create_task(self._run_task(goal, images))
        return True

    async def _run_task(self, goal: str, images: List[str]) -> None:
        """
        İş sınırı: işi koşturur, sonucu ya da hatayı kullanıcıya bildirir ve etkinlik günlüğüne yazar. Arka plan görevi
        olduğu için koşu, bildirim ve rapor hataları burada loglanır ("Task exception was never retrieved" olarak
        sızmaz); başarısız, meşgul ve çöken işler de etkinliğe yazılır.
        """
        started_at: str = utc_iso(datetime.now(timezone.utc))
        interim: asyncio.Task[None] = asyncio.create_task(self._interim_after(INTERIM_AFTER_SECONDS))
        result: Union[delegate.TaskOutcome, str]  # koşu sonucu ya da kullanıcıya söylenecek hata metni
        try:
            if self.integrations is None:
                self.integrations = CapabilityService()
            options = delegate.run_options(self.answer, self.deliver, self.history, images, self.stop_event.is_set,
                                           self.integrations)
            result = await delegate.run_task(goal, options, self._on_progress)
        except HostBusyError:
            result = HOST_BUSY_TEXT
        except Exception as error:
            # İş sınırı: kök neden (traceback) loglanır ve kullanıcıya açıkça söylenir.
            logging.exception("iMessage işi beklenmedik hatayla bitti", extra={"goal_chars": len(goal)})
            result = f"iş yarıda kaldı: {type(error).__name__}: {str(error)[:200]}"
        finally:
            interim.cancel()
            await asyncio.gather(interim, return_exceptions=True)
            self._cancel_question(IntegrationStopped("İş bitti."))
            self.task, self.task_goal, self.task_progress = None, "", []
            self.stop_event.clear()
        try:
            if isinstance(result, str):
                await self._fail_task(goal, result, started_at)
            else:
                await self._finish_task(result)
        except Exception:
            # Bildirim ya da rapor hatası da iş sınırında kalır: kök neden loglanır, köprü sonraki işlere devam eder.
            logging.exception("iMessage iş sonucu bildirilemedi", extra={"goal_chars": len(goal)})

    async def _fail_task(self, goal: str, failure: str, started_at: str) -> None:
        """Koşamayan ya da çöken işi (meşgul bilgisayar, beklenmedik hata) etkinlik günlüğüne yazar ve kullanıcıya söyler."""
        self.store.record_activity({
            "kind": "task", "origin": "user", "goal": goal, "rationale": "", "outcome": failure[:2000],
            "success": False, "started_at": started_at, "finished_at": utc_iso(datetime.now(timezone.utc)),
            "tokens": 0,
        })
        await self._send(failure, "chat")

    async def _finish_task(self, outcome: delegate.TaskOutcome) -> None:
        report = outcome["report"]
        self.history = trim_history(self.history + [report["exchange"]])
        save_json(imessage_history_file(), self.history)
        self.store.record_activity({
            "kind": "task", "origin": "user", "goal": outcome["goal"], "rationale": "",
            "outcome": report["outcome"][:2000], "success": report["success"], "started_at": outcome["started_at"],
            "finished_at": outcome["finished_at"], "tokens": outcome["tokens"],
        })
        async with self.chat_lock:
            history: List[ArchivedMessage] = self.store.recent_messages(chat.HISTORY_LIMIT)
            turn: str = chat.report_turn(outcome["goal"], report["success"], report["outcome"], self._situation())
            # Rapor turu araçsızdır: model yalnız sonucu anlatır; iş başlatamaz, raporu araç çağrısıyla da yutamaz.
            reply: Optional[ModelReply] = await self._respond(
                self._history(history) + [{"role": "user", "content": turn}], [], None, "task_report",
            )
        if reply is not None and reply["result"]["start_task"] is not None:
            # Rapor yalnız anlatılır (spec §5): girdisi önceki işin çıktısıdır (web içeriği olabilir); kullanıcı mesajı
            # olmadan iş başlatmak dolaylı istem enjeksiyonu yolu olur. Söz koruması da yalnız kullanıcı turundadır.
            # Hedef metni loglanmaz, yalnız sayı.
            logging.warning("İş raporu turunda sohbet modeli yeni iş istedi; yok sayıldı", extra={"ignored_tasks": 1})

    async def _interim_after(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
        last: str = self.task_progress[-1] if self.task_progress else "devam ediyor"
        await self._send(f"hâlâ üzerindeyim: {last[:160]}", "chat")

    def _on_progress(self, line: str) -> None:
        self.task_progress = (self.task_progress + [line])[-delegate.PROGRESS_LIMIT:]

    # --- bakım ---

    async def sweep_forever(self) -> None:
        """İzlemede görülmeyen balonları süre dolunca 'unconfirmed' yapar ve uyarır."""
        while True:
            await asyncio.sleep(SWEEP_SECONDS)
            expired: List[int] = self.store.expire_pending(datetime.now(timezone.utc), CONFIRM_WINDOW_SECONDS)
            if expired:
                logging.warning("iMessage balonları izlemede görülmedi (teslim doğrulanamadı)",
                                extra={"message_ids": expired})

    async def close(self) -> None:
        """Çalışan işi durdurur, bekleyen soruyu iptal eder, zamanlayıcıyı ve entegrasyonları kapatır."""
        self.closing = True
        self.stop_event.set()
        self._cancel_question(IntegrationStopped("Köprü kapanıyor."))
        timer: Optional[asyncio.Task[None]] = self.burst_timer
        if timer is not None:
            timer.cancel()
            await asyncio.gather(timer, return_exceptions=True)
        task: Optional[asyncio.Task[None]] = self.task
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=CLOSE_TASK_TIMEOUT_SECONDS)
            except TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if self.integrations is not None:
            await self.integrations.close()


class ImsgSession:
    """Geçerli imsg istemcisini tutar ve çöken süreci uyarıyla yeniden başlatır (dış sistem bağlayıcısı)."""

    def __init__(self, command: List[str]) -> None:
        self.command: List[str] = command
        self.client: Optional[ImsgClient] = None

    def _current(self) -> ImsgClient:
        if self.client is None:
            raise ImsgProcessError("imsg bağlantısı yeniden kuruluyor.")
        return self.client

    async def send_text(self, handle: str, text: str) -> SendResult:
        return await self._current().send_text(handle, text)

    async def send_file(self, handle: str, path: Path) -> SendResult:
        return await self._current().send_file(handle, path)

    async def listen(self, bridge: ImessageBridge) -> None:
        """
        Bağlan → kaçanları yanıtlamadan arşivle → yanıtsız son burst'ü yanıtla → canlı izle. imsg süreci düşerse
        RESTART_DELAYS_SECONDS ile yeniden dener; art arda tükenirse ImsgProcessError yükselir (launchd yeniden
        başlatır). En az STABLE_CONNECTION_SECONDS süren bağlantı sayacı sıfırlar.
        """
        failures: int = 0
        while True:
            client = ImsgClient(self.command)
            connected_at: float = time.monotonic()
            try:
                await client.start()
                self.client = client
                missed, cursor = await client.catch_up(bridge.store.cursor())
                for message in missed:
                    bridge.archive_backlog(message)
                bridge.store.advance_cursor(cursor)
                await bridge.answer_unanswered()
                async with aclosing(client.subscribe(bridge.store.cursor())) as stream:
                    async for message in stream:
                        try:
                            await bridge.on_message(message)
                        except ImsgProcessError:
                            raise
                        except ImsgError as error:
                            # Komut/soru yanıtının gönderimi reddedildi: işlem zaten uygulandı (dur, cevap) ve hata
                            # _send'de de uyarıldı; köprü ve çalışan iş düşmez. Yalnız süreç hatası yeniden bağlanır.
                            logging.warning("iMessage mesajı işlenirken imsg hatası; dinleme sürüyor",
                                            extra={"rowid": message["rowid"], **_send_failure_fields(error)})
            except ImsgProcessError as error:
                if time.monotonic() - connected_at >= STABLE_CONNECTION_SECONDS:
                    failures = 0
                failures += 1
                if failures > len(RESTART_DELAYS_SECONDS):
                    raise
                logging.warning("imsg süreci düştü; yeniden başlatılıyor",
                                extra={"attempt": failures, "error": str(error)[:300]})
                await asyncio.sleep(RESTART_DELAYS_SECONDS[failures - 1])
            finally:
                self.client = None
                await client.close()


def ensure_messages_running() -> None:
    """Messages.app'i odak çalmadan arka planda başlatır; AppleScript gönderimi uygulamayı öne getirmesin."""
    result = subprocess.run(["open", "-g", "-a", "Messages"], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise ImessageConfigError(f"Messages arka planda açılamadı: {result.stderr.strip()[:300]}")


async def _serve(store: PersonalStore) -> None:
    """Eşleşmiş köprüyü çalıştırır: ayarlar, karakter, sohbet istemcileri, imsg oturumu, uyku engeli."""
    settings: ImessageSettings = load_settings(imessage_settings_file(), BACKENDS.keys())
    apply_model_preferences()
    persona_text: str = persona.load_persona(persona_file())
    chat_clients: Dict[str, AsyncOpenAI] = await asyncio.to_thread(create_model_clients)
    session = ImsgSession(imsg_command())
    bridge = ImessageBridge(session, settings, store, chat_clients, persona_text, f"imessage-{uuid.uuid4().hex}")
    keep_awake: Optional["subprocess.Popen[bytes]"] = None
    sweeper: Optional[asyncio.Task[None]] = None
    try:
        if settings["chat_backend"] not in chat_clients:
            raise ImessageConfigError(
                f"Sohbet profili '{settings['chat_backend']}' kullanılamıyor (API anahtarı ya da Ollama yok). "
                f"Hazır profiller: {', '.join(sorted(chat_clients)) or 'yok'}"
            )
        ensure_messages_running()
        keep_awake = start_keep_awake(os.getpid())
        sweeper = asyncio.create_task(bridge.sweep_forever())
        await session.listen(bridge)
    finally:
        stop_keep_awake(keep_awake)
        if sweeper is not None:
            sweeper.cancel()
            await asyncio.gather(sweeper, return_exceptions=True)
        await bridge.close()
        await close_model_clients(chat_clients)


async def run_bridge() -> None:
    """Tek köprü sürecini çalıştırır; eşleşme yoksa önce servisin içinde eşleştirir."""
    with host_task_lock(data_root() / "imessage-bridge.lock"):
        store = PersonalStore(companion_db_file())
        try:
            if not imessage_settings_file().exists():
                await imessage_setup.pair_from_service(store)
            await _serve(store)
        finally:
            store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="OmniAgent iMessage köprüsü")
    parser.add_argument("action", choices=("setup", "run", "install-service"))
    arguments = parser.parse_args()
    if arguments.action == "run":
        # Servis günlüğü launchd'nin imessage-stderr.log dosyasına gider: INFO satırları (gecikme ölçümü) ve extra
        # alanları dahil. Anahtar uygulaması da loglayabildiği için ondan önce kurulur.
        configure_stream_logging(sys.stderr, logging.INFO)
    # Arka plan servisi kabuk ortamını miras almaz: Ayarlar'da kayıtlı anahtarları uygula.
    apply_stored_api_keys()
    try:
        if arguments.action == "setup":
            asyncio.run(imessage_setup.setup())
        elif arguments.action == "install-service":
            imessage_setup.install_service()
        else:
            # launchd süreci salt okunur '/' dizininde başlatır; köprü Telegram gibi proje kökünde çalışır.
            os.chdir(project_root())
            asyncio.run(run_bridge())
    except (ImsgError, ImessageConfigError, HostBusyError, LaunchAgentError, FileNotFoundError,
            KeyboardInterrupt) as error:
        print(f"iMessage: {error}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
