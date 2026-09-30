"""imsg (openclaw/imsg) JSON-RPC istemcisi: iMessage alma ve gönderme.

`imsg rpc` alt süreci satır başına bir JSON-RPC 2.0 nesnesi konuşur (openclaw/imsg docs/rpc.md). Bu modül tek
bir süreç ömrünü yönetir; çöken süreci yeniden başlatmak köprünün işidir (integrations/imessage.py
ImsgSession). Gönderimin sonucu bilinmiyorsa (-32001) yeniden gönderilmez: çift mesaj, kayıp mesajdan kötüdür.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
from pathlib import Path
from typing import AsyncIterator, Dict, List, Optional, Tuple, TypedDict

PROTOCOL_VERSION: int = 1
DEBOUNCE_MS: int = 500
PAGE_LIMIT: int = 500
REQUEST_TIMEOUT_SECONDS: float = 60.0
STOP_TIMEOUT_SECONDS: float = 5.0
STREAM_LIMIT_BYTES: int = 16 * 1024 * 1024
STDERR_TAIL_CHARS: int = 2000
# launchd PATH'i Homebrew dizinlerini içermez; imsg bu dizinlerde de aranır.
IMSG_SEARCH_DIRS: Tuple[str, ...] = ("/opt/homebrew/bin", "/usr/local/bin")
# Yalnız ek taşıyan mesajın metni U+FFFC (nesne yer tutucusu) olabilir.
OBJECT_REPLACEMENT: str = "￼"
DELIVERY_UNKNOWN_CODE: int = -32001
DATABASE_UNAVAILABLE_CODE: int = -32002
LANE_BLOCKED_CODE: int = -32004


class ImsgError(Exception):
    """imsg ile ilgili hataların tabanı."""


class ImsgUnavailable(ImsgError):
    """imsg bulunamadı ya da Messages veritabanı okunamıyor (Full Disk Access)."""


class ImsgProcessError(ImsgError):
    """imsg rpc süreci başlatılamadı ya da beklenmedik biçimde kapandı."""


class DeliveryUnknown(ImsgError):
    """Gönderimin sonucu bilinmiyor; çift mesaj riski yüzünden yeniden gönderilmez."""


class ImsgRpcError(ImsgError):
    """imsg'nin döndürdüğü JSON-RPC hatası (kod, yöntem ve parametre adlarıyla; parametre değerleri yazılmaz)."""

    def __init__(self, method: str, code: int, message: str, params: Dict[str, object]) -> None:
        # Değerler (alıcı, mesaj metni, dosya yolu) hata metnine girmez: metin loglara, stderr'e ve zincirli izlere düşer.
        names: str = ", ".join(sorted(params))
        super().__init__(f"imsg {method} hatası {code}: {message[:300]} (parametre adları: {names})")
        self.method: str = method
        self.code: int = code


class Attachment(TypedDict):
    path: str
    mime_type: str


class IncomingMessage(TypedDict):
    rowid: int
    guid: str
    chat_id: int
    sender: str
    participants: List[str]
    is_from_me: bool
    is_group: bool
    text: str
    created_at: str
    attachments: List[Attachment]


class SendResult(TypedDict):
    ok: bool
    rowid: Optional[int]
    guid: Optional[str]


def imsg_command() -> List[str]:
    """`imsg rpc` komutu; imsg yoksa kurulum talimatıyla ImsgUnavailable."""
    search: str = os.pathsep.join([os.environ.get("PATH", ""), *IMSG_SEARCH_DIRS])
    found: Optional[str] = shutil.which("imsg", path=search)
    if found is None:
        raise ImsgUnavailable("imsg bulunamadı. Kurulum: brew install steipete/tap/imsg")
    return [found, "rpc"]


def _attachment(raw: object) -> Optional[Attachment]:
    """imsg ek nesnesini yol + MIME'e indirir; diskte olmayan ek None. Saf."""
    if not isinstance(raw, dict) or raw.get("missing") is True:
        return None
    path: object = raw.get("converted_path") or raw.get("original_path")
    mime: object = raw.get("converted_mime_type") or raw.get("mime_type")
    if not isinstance(path, str) or not path:
        return None
    return {"path": path, "mime_type": mime if isinstance(mime, str) else ""}


def parse_message(raw: object) -> IncomingMessage:
    """
    imsg mesaj nesnesini doğrular. imsg sözleşmesinde uygulanmayan alanlar gönderilmez (ör. kendi gönderimimizde
    sender); bunlar boş değer sayılır. Zorunlu alan eksikse ImsgError. Saf.
    """
    if not isinstance(raw, dict):
        raise ImsgError(f"imsg mesajı nesne değil: {type(raw).__name__}")
    rowid, guid, chat_id = raw.get("id"), raw.get("guid"), raw.get("chat_id")
    created_at, is_from_me = raw.get("created_at"), raw.get("is_from_me")
    if (not isinstance(rowid, int) or not isinstance(guid, str) or not isinstance(chat_id, int)
            or not isinstance(created_at, str) or not isinstance(is_from_me, bool)):
        raise ImsgError(f"imsg mesajında zorunlu alan eksik: {sorted(raw)}")
    sender: object = raw.get("sender")
    text: object = raw.get("text")
    participants: object = raw.get("participants")
    attachments: object = raw.get("attachments")
    parsed: List[Attachment] = []
    if isinstance(attachments, list):
        parsed = [item for item in (_attachment(entry) for entry in attachments) if item is not None]
    return {
        "rowid": rowid, "guid": guid, "chat_id": chat_id,
        "sender": sender if isinstance(sender, str) else "",
        "participants": [item for item in participants if isinstance(item, str)] if isinstance(participants, list) else [],
        "is_from_me": is_from_me,
        "is_group": raw.get("is_group") is True,
        "text": text if isinstance(text, str) else "",
        "created_at": created_at,
        "attachments": parsed,
    }


_PendingRequest = Tuple[str, Dict[str, object], "asyncio.Future[Dict[str, object]]"]


class ImsgClient:
    """Tek bir `imsg rpc` alt sürecine bağlı JSON-RPC istemcisi (dış sistem bağlayıcısı)."""

    def __init__(self, command: List[str]) -> None:
        self.command: List[str] = command
        self.process: Optional[asyncio.subprocess.Process] = None
        self.reader: Optional[asyncio.Task[None]] = None
        self.stderr_reader: Optional[asyncio.Task[None]] = None
        self.pending: Dict[int, _PendingRequest] = {}
        self.next_id: int = 1
        self.notifications: asyncio.Queue[Optional[Dict[str, object]]] = asyncio.Queue()
        self.closed: Optional[ImsgProcessError] = None
        self.stderr_tail: str = ""

    async def start(self) -> Dict[str, object]:
        """Süreci başlatır ve el sıkışır; Messages veritabanı okunamıyorsa ImsgUnavailable."""
        try:
            self.process = await asyncio.create_subprocess_exec(
                *self.command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, limit=STREAM_LIMIT_BYTES,
            )
        except OSError as error:
            raise ImsgProcessError(f"imsg başlatılamadı ({self.command[0]}): {error}") from error
        self.reader = asyncio.create_task(self._read_stdout())
        self.stderr_reader = asyncio.create_task(self._read_stderr())
        status: Dict[str, object] = await self.request("initialize", {"protocol_version": PROTOCOL_VERSION})
        database: object = status.get("database")
        if not isinstance(database, dict) or database.get("ready") is not True:
            detail: object = database.get("error") if isinstance(database, dict) else database
            raise ImsgUnavailable(
                f"Messages veritabanı okunamıyor ({detail}). Full Disk Access şu ikiliye verilmeli: "
                f"{os.path.realpath(sys.executable)}"
            )
        return status

    def _running(self) -> asyncio.subprocess.Process:
        if self.process is None:
            raise ImsgProcessError("imsg başlatılmadı.")
        return self.process

    async def _read_stdout(self) -> None:
        process: asyncio.subprocess.Process = self._running()
        if process.stdout is None:
            raise ImsgProcessError("imsg stdout bağlanmadı.")
        reason: str = "imsg istemcisi kapatıldı."
        try:
            while True:
                line: bytes = await process.stdout.readline()
                if not line:
                    code: int = await process.wait()
                    reason = f"imsg rpc süreci kapandı (çıkış kodu {code}; stderr: {self.stderr_tail[-300:]})"
                    return
                self._dispatch(line)
        finally:
            failure = ImsgProcessError(reason)
            self.closed = failure
            for _method, _params, future in self.pending.values():
                if not future.done():
                    future.set_exception(failure)
            self.pending.clear()
            self.notifications.put_nowait(None)

    async def _read_stderr(self) -> None:
        process: asyncio.subprocess.Process = self._running()
        if process.stderr is None:
            return
        while True:
            chunk: bytes = await process.stderr.read(4096)
            if not chunk:
                return
            self.stderr_tail = (self.stderr_tail + chunk.decode("utf-8", errors="replace"))[-STDERR_TAIL_CHARS:]

    def _dispatch(self, line: bytes) -> None:
        try:
            payload: object = json.loads(line)
        except json.JSONDecodeError as error:
            logging.warning("imsg geçersiz JSON satırı atlandı", extra={"error": str(error)[:200]})
            return
        if not isinstance(payload, dict):
            logging.warning("imsg nesne olmayan satır atlandı", extra={"type": type(payload).__name__})
            return
        if "method" in payload and "id" not in payload:
            self.notifications.put_nowait(payload)
            return
        request_id: object = payload.get("id")
        entry: Optional[_PendingRequest] = self.pending.pop(request_id, None) if isinstance(request_id, int) else None
        if entry is None:
            logging.warning("imsg beklenmeyen yanıt kimliği", extra={"id": str(request_id)[:40]})
            return
        method, params, future = entry
        if future.done():
            return
        error: object = payload.get("error")
        if isinstance(error, dict):
            code: object = error.get("code")
            message: object = error.get("message")
            rpc_error = ImsgRpcError(method, code if isinstance(code, int) else 0,
                                      message if isinstance(message, str) else "", params)
            if rpc_error.code == DATABASE_UNAVAILABLE_CODE:
                future.set_exception(ImsgUnavailable(f"{rpc_error}. Full Disk Access gerekli."))
            else:
                future.set_exception(rpc_error)
            return
        result: object = payload.get("result")
        future.set_result(result if isinstance(result, dict) else {"value": result})

    async def request(self, method: str, params: Dict[str, object]) -> Dict[str, object]:
        """Tek JSON-RPC isteği; süreç kapanmışsa ImsgProcessError, yanıt gelmezse TimeoutError."""
        if self.closed is not None:
            raise self.closed
        process: asyncio.subprocess.Process = self._running()
        if process.stdin is None:
            raise ImsgProcessError("imsg stdin bağlanmadı.")
        request_id: int = self.next_id
        self.next_id += 1
        future: asyncio.Future[Dict[str, object]] = asyncio.get_running_loop().create_future()
        self.pending[request_id] = (method, params, future)
        line: str = json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
                               ensure_ascii=False)
        try:
            process.stdin.write(line.encode("utf-8") + b"\n")
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as error:
            self.pending.pop(request_id, None)
            raise ImsgProcessError(f"imsg'ye yazılamadı ({method}): {error}") from error
        try:
            return await asyncio.wait_for(future, timeout=REQUEST_TIMEOUT_SECONDS)
        finally:
            self.pending.pop(request_id, None)

    async def catch_up(self, since_rowid: int) -> Tuple[List[IncomingMessage], int]:
        """since_rowid'den sonraki mesajları sayfalayarak getirir; yetkili imleç imsg'nin next_rowid'idir."""
        messages: List[IncomingMessage] = []
        cursor: int = since_rowid
        while True:
            page: Dict[str, object] = await self.request(
                "messages.after", {"since_rowid": cursor, "limit": PAGE_LIMIT, "attachments": True},
            )
            items, next_rowid, has_more = page.get("messages"), page.get("next_rowid"), page.get("has_more")
            if not isinstance(items, list) or not isinstance(next_rowid, int) or not isinstance(has_more, bool):
                raise ImsgError(f"messages.after beklenmeyen yanıt: {sorted(page)}")
            messages.extend(parse_message(item) for item in items)
            cursor = next_rowid
            if not has_more:
                return messages, cursor

    async def _subscribe(self, since_rowid: int) -> int:
        result: Dict[str, object] = await self.request(
            "watch.subscribe", {"since_rowid": since_rowid, "attachments": True, "debounce_ms": DEBOUNCE_MS},
        )
        subscription: object = result.get("subscription")
        if not isinstance(subscription, int):
            raise ImsgError(f"watch.subscribe abonelik kimliği döndürmedi: {sorted(result)}")
        return subscription

    async def subscribe(self, since_rowid: int) -> AsyncIterator[IncomingMessage]:
        """
        Yeni mesajları iter. Taşma (watch.overflow) sonrası kaçan satırlar messages.after ile getirilir ve abonelik
        yeniden kurulur (imsg: yineleme olabilir, atlama olmaz). Tanınmayan bildirim yönteminde terminal=True ise
        ImsgError ile başarısız, aksi halde uyarı kaydedilerek beklemeye devam. Süreç kapanırsa ImsgProcessError.
        """
        subscription: int = await self._subscribe(since_rowid)
        while True:
            payload: Optional[Dict[str, object]] = await self.notifications.get()
            if payload is None:
                raise self.closed if self.closed is not None else ImsgProcessError("imsg bildirimleri kapandı.")
            params: object = payload.get("params")
            if not isinstance(params, dict) or params.get("subscription") != subscription:
                continue
            method: object = payload.get("method")
            if method == "message":
                yield parse_message(params.get("message"))
            elif method == "watch.overflow":
                resume: object = params.get("resume_after_rowid")
                if not isinstance(resume, int):
                    raise ImsgError("watch.overflow resume_after_rowid içermiyor.")
                logging.warning("imsg izleme tamponu taştı; kaçan mesajlar getiriliyor",
                                extra={"resume_after_rowid": resume})
                missed, cursor = await self.catch_up(resume)
                for message in missed:
                    yield message
                subscription = await self._subscribe(cursor)
            else:
                if params.get("terminal") is True:
                    raise ImsgError(f"imsg aboneliği sonlandı: {method}")
                logging.warning("imsg tanınmayan bildirim atlandı", extra={"method": str(method)[:80]})

    async def send_text(self, handle: str, text: str) -> SendResult:
        return await self._send({"to": handle, "text": text, "service": "imessage", "allow_sms_fallback": False})

    async def send_file(self, handle: str, path: Path) -> SendResult:
        return await self._send({"to": handle, "file": str(path), "service": "imessage", "allow_sms_fallback": False})

    async def _send(self, params: Dict[str, object]) -> SendResult:
        try:
            result: Dict[str, object] = await self.request("send", params)
        except ImsgRpcError as error:
            if error.code == DELIVERY_UNKNOWN_CODE:
                raise DeliveryUnknown(str(error)) from error
            if error.code == LANE_BLOCKED_CODE:
                # Mutasyon şeridi zehirlendi: yalnız yeni süreç temizler; köprü yeniden bağlanır.
                await self.close()
                raise DeliveryUnknown(str(error)) from error
            raise
        except TimeoutError as error:
            raise DeliveryUnknown(f"imsg send {REQUEST_TIMEOUT_SECONDS:.0f} sn içinde yanıt vermedi") from error
        if result.get("ok") is not True:
            raise ImsgError(f"imsg send başarısız yanıt: {sorted(result)}")
        rowid: object = result.get("id")
        guid: object = result.get("guid")
        return {"ok": True, "rowid": rowid if isinstance(rowid, int) else None,
                "guid": guid if isinstance(guid, str) else None}

    async def close(self) -> None:
        """stdin'i kapatır (imsg kabul edilenleri bitirip çıkar); süre dolarsa süreci öldürür."""
        process: Optional[asyncio.subprocess.Process] = self.process
        if process is None:
            return
        if process.returncode is None:
            if process.stdin is not None and not process.stdin.is_closing():
                process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), timeout=STOP_TIMEOUT_SECONDS)
            except TimeoutError:
                process.kill()
                await process.wait()
        for task in (self.reader, self.stderr_reader):
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
