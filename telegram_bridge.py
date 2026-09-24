"""OmniAgent görevlerini eşleştirilmiş özel Telegram sohbetine akışla taşır."""
from __future__ import annotations

import argparse
import asyncio
import getpass
import html
import json
import os
import plistlib
import re
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, TypedDict

import httpx
from keyring.backends.macOS import Keyring
from openai import AsyncOpenAI

from config import BACKENDS
from conversation import Exchange, trim_history
from events import AgentEvent, tool_label
from host_lock import HostBusyError, host_task_lock
from integration_runtime import IntegrationStopped, data_root, read_json, save_json
from main import RunOptions, RunReport, STATE_FILE, close_model_clients, create_model_clients, run_agent_with_callback


TOKEN_SERVICE = "OmniAgent Telegram"
TOKEN_ACCOUNT = "bot_token"
PAGE_LIMIT = 3500
EDIT_INTERVAL = 1.1
POLL_SECONDS = 20


class TelegramError(RuntimeError):
    """Bot API veya yapılandırma hatası; tokenı hata metnine taşımaz."""

    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


class TelegramSettings(TypedDict):
    chat_id: int
    user_id: int


def settings_path() -> Path:
    return data_root() / "telegram.json"


def offset_path() -> Path:
    return data_root() / "telegram-offset.json"


def history_path() -> Path:
    return data_root() / "telegram-history.json"


def load_settings() -> TelegramSettings:
    """Yetkili özel sohbet ve kullanıcı kimliğini doğrular."""
    value = read_json(settings_path(), {})
    if not isinstance(value, dict):
        raise TelegramError("Telegram yapılandırması geçersiz; setup komutunu çalıştırın.")
    chat_id, user_id = value.get("chat_id"), value.get("user_id")
    if not isinstance(chat_id, int) or not isinstance(user_id, int) or chat_id <= 0 or user_id <= 0:
        raise TelegramError("Telegram eşleştirmesi eksik; setup komutunu çalıştırın.")
    return {"chat_id": chat_id, "user_id": user_id}


def load_token() -> str:
    """Bot tokenını yalnız macOS Keychain'den alır."""
    token = Keyring().get_password(TOKEN_SERVICE, TOKEN_ACCOUNT)
    if not token:
        raise TelegramError("Telegram bot tokenı Keychain'de yok; setup komutunu çalıştırın.")
    return token


def authorized(message: Dict[str, Any], settings: TelegramSettings) -> bool:
    """Yalnız eşleştirilmiş kişinin özel sohbetindeki mesajları kabul eder."""
    chat = message.get("chat")
    sender = message.get("from")
    return (
        isinstance(chat, dict) and isinstance(sender, dict)
        and chat.get("type") == "private"
        and chat.get("id") == settings["chat_id"]
        and sender.get("id") == settings["user_id"]
    )


def event_text(event: AgentEvent) -> str:
    """Tipli ajan olayunu kısa, okunur Telegram metnine dönüştürür."""
    kind = event["kind"]
    if kind == "run_started":
        return f"▶ {event['goal'][:500]}\nModel: {event['model']} · {event['backend']}\n"
    if kind == "turn_started":
        return f"\n↻ Tur {event['turn']}/{event['max_turns']} · {event['backend']}\n"
    if kind in ("text_delta", "reasoning_delta"):
        return event["text"]
    if kind == "tool_started":
        return f"\n⏺ {tool_label(event['name'])} {event['preview'][:250]}\n"
    if kind == "tool_output":
        return f"  {event['text'][:500]}"
    if kind == "tool_finished":
        mark = "✓" if event["ok"] else "✗"
        return f"\n{mark} {event['seconds']:.1f} sn · {event['text'][:800]}\n"
    if kind == "backend_changed":
        return f"\n↻ Model değişti: {event['backend']} · {event['reason'][:220]}\n"
    if kind == "notice":
        return f"\n! {event['text'][:500]}\n"
    if kind == "integration_status":
        progress = f" {event['completed']}/{event['total']}" if event["total"] else ""
        return f"\n◦ {event['stage']}{progress}: {event['text'][:300]}\n"
    if kind == "stream_reset":
        return f"\n↺ Akış sıfırlandı: {event['reason'][:200]}\n"
    if kind == "run_finished":
        metrics = event["metrics"]
        mark = "✓" if event["success"] else "✗"
        return (
            f"\n{mark} {event['outcome'][:1200]}\n"
            f"{metrics['elapsed_seconds']:.1f} sn · {metrics['turns']} tur · "
            f"{metrics['tool_calls']} araç · {metrics['backend']}\n"
            f"Token: giriş {metrics['prompt_tokens']}, önbellek {metrics['cached_tokens']}, "
            f"çıkış {metrics['completion_tokens']}\n"
        )
    return ""


class TelegramAPI:
    """Uzun yoklama ve mesaj düzenleme için dar Bot API bağlayıcısı."""

    def __init__(self, token: str, client: Optional[httpx.AsyncClient] = None) -> None:
        self.token = token
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(35.0, connect=5.0), trust_env=False,
        )
        self.owns_client = client is None

    async def close(self) -> None:
        if self.owns_client:
            await self.client.aclose()

    async def call(self, method: str, payload: Dict[str, Any]) -> Any:
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        for attempt in range(2):
            try:
                response = await self.client.post(url, json=payload)
                body = response.json()
            except (httpx.HTTPError, ValueError) as error:
                raise TelegramError(f"{method}: ağ veya yanıt hatası ({type(error).__name__}).") from None
            if not isinstance(body, dict):
                raise TelegramError(f"{method}: geçersiz Bot API yanıtı.")
            if response.status_code == 429 and attempt == 0:
                parameters = body.get("parameters", {})
                seconds = parameters.get("retry_after", 1) if isinstance(parameters, dict) else 1
                try:
                    delay = min(60, max(0, int(seconds)))
                except (TypeError, ValueError):
                    delay = 1
                await asyncio.sleep(delay)
                continue
            if response.status_code != 200 or not body.get("ok"):
                description = str(body.get("description", "istek başarısız"))[:200]
                raise TelegramError(f"{method}: HTTP {response.status_code}: {description}", response.status_code)
            return body.get("result")
        raise TelegramError(f"{method}: hız sınırı devam ediyor.")

    async def send(self, chat_id: int, text: str) -> int:
        result = await self.call("sendMessage", {"chat_id": chat_id, "text": text[:PAGE_LIMIT]})
        if not isinstance(result, dict) or not isinstance(result.get("message_id"), int):
            raise TelegramError("sendMessage: ileti kimliği eksik.")
        return result["message_id"]

    async def edit(self, chat_id: int, message_id: int, text: str) -> None:
        await self.call("editMessageText", {
            "chat_id": chat_id, "message_id": message_id, "text": text[:PAGE_LIMIT],
        })

    async def send_html(self, chat_id: int, content: str) -> int:
        """Eski Bot API için güvenli HTML biçimli ileti gönderir."""
        result = await self.call("sendMessage", {
            "chat_id": chat_id, "text": content, "parse_mode": "HTML",
        })
        if not isinstance(result, dict) or not isinstance(result.get("message_id"), int):
            raise TelegramError("sendMessage: ileti kimliği eksik.")
        return result["message_id"]

    async def edit_html(self, chat_id: int, message_id: int, content: str) -> None:
        await self.call("editMessageText", {
            "chat_id": chat_id, "message_id": message_id,
            "text": content, "parse_mode": "HTML",
        })

    async def send_draft(self, chat_id: int, draft_id: int, rich_message: Dict[str, str]) -> None:
        """Aynı taslak kimliğini güncelleyerek yerel akış animasyonunu sürdürür."""
        await self.call("sendRichMessageDraft", {
            "chat_id": chat_id, "draft_id": draft_id, "rich_message": rich_message,
        })

    async def send_photo(self, chat_id: int, path: Path) -> None:
        """Gerçek ekran görüntüsünü eşleştirilmiş sohbete dosya olarak iletir."""
        mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
        try:
            with path.open("rb") as source:
                response = await self.client.post(
                    f"https://api.telegram.org/bot{self.token}/sendPhoto",
                    data={"chat_id": str(chat_id)},
                    files={"photo": (path.name, source, mime)},
                )
                body = response.json()
        except (OSError, httpx.HTTPError, ValueError) as error:
            raise TelegramError(f"sendPhoto: {type(error).__name__}.") from None
        if response.status_code != 200 or not isinstance(body, dict) or not body.get("ok"):
            raise TelegramError(f"sendPhoto: HTTP {response.status_code}.", response.status_code)


class TelegramStream:
    """Ajan olaylarını saniyede en çok bir düzenlemeyle sayfalı canlı metne çevirir."""

    def __init__(self, api: TelegramAPI, chat_id: int) -> None:
        self.api = api
        self.chat_id = chat_id
        self.page = ""
        self.message_id: Optional[int] = None
        self.sent = ""
        self.last_edit = 0.0

    async def append(self, text: str) -> None:
        remaining = text
        while remaining:
            space = PAGE_LIMIT - len(self.page)
            if space == 0:
                await self.flush(force=True)
                self.page = ""
                self.message_id = None
                self.sent = ""
                space = PAGE_LIMIT
            piece, remaining = remaining[:space], remaining[space:]
            self.page += piece
            if remaining:
                await self.flush(force=True)
        await self.flush()

    async def show(self, text: str, force: bool = False) -> None:
        """Kompakt görünümde aynı mesajı yeniler; uzun finali sayfalara böler."""
        self.page = text[:PAGE_LIMIT]
        await self.flush(force=force)
        if force and len(text) > PAGE_LIMIT:
            remaining = text[PAGE_LIMIT:]
            self.page = ""
            self.message_id = None
            self.sent = ""
            await self.append(remaining)

    async def flush(self, force: bool = False) -> None:
        if not self.page or self.page == self.sent:
            return
        now = time.monotonic()
        if self.message_id is not None and not force and now - self.last_edit < EDIT_INTERVAL:
            return
        if self.message_id is None:
            self.message_id = await self.api.send(self.chat_id, self.page)
        else:
            await self.api.edit(self.chat_id, self.message_id, self.page)
        self.sent = self.page
        self.last_edit = time.monotonic()


def _legacy_markdown_html(markdown: str) -> str:
    """Yaygın Markdown'ı eski Telegram HTML biçimine güvenli biçimde çevirir."""
    tokens: list[str] = []

    def inline(raw: str) -> str:
        raw = raw.replace("\x00", "�")

        def reserve(value: str) -> str:
            key = f"OMNITOKEN{len(tokens)}END"
            tokens.append(value)
            return key

        raw = re.sub(
            r"`([^`\n]+)`",
            lambda match: reserve("<code>" + html.escape(match.group(1)) + "</code>"),
            raw,
        )
        raw = re.sub(
            r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)",
            lambda match: reserve(
                '<a href="' + html.escape(match.group(2), quote=True) + '">'
                + html.escape(match.group(1)) + "</a>"
            ),
            raw,
        )
        escaped = html.escape(raw)
        escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)
        escaped = re.sub(r"~~(.+?)~~", r"<s>\1</s>", escaped)
        escaped = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", escaped)
        for index, value in enumerate(tokens):
            escaped = escaped.replace(f"OMNITOKEN{index}END", value)
        tokens.clear()
        return escaped

    result: list[str] = []
    code_lines: list[str] = []
    fenced = False
    for line in markdown.split("\n"):
        if line.lstrip().startswith("```"):
            if fenced:
                result.append("<pre>" + html.escape("\n".join(code_lines)) + "</pre>")
                code_lines = []
                fenced = False
            else:
                fenced = True
            continue
        if fenced:
            code_lines.append(line)
            continue
        if not line.strip():
            continue
        stripped = line.lstrip()
        if re.match(r"^#{1,6}\s+", stripped):
            line = re.sub(r"^#{1,6}\s+", "", stripped)
            result.append("<b>" + inline(line) + "</b>")
        elif re.match(r"^[-*+]\s+", stripped):
            line = re.sub(r"^[-*+]\s+", "", stripped)
            result.append("• " + inline(line))
        else:
            result.append(inline(line))
    if fenced:
        result.append("<pre>" + html.escape("\n".join(code_lines)) + "</pre>")
    return "\n".join(result)


def _markdown_pages(text: str) -> list[str]:
    """Yanıtı Telegram ileti sınırını aşmadan satır başlarından böler."""
    pages: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= PAGE_LIMIT:
            pages.append(remaining)
            break
        cut = remaining.rfind("\n", 0, PAGE_LIMIT + 1)
        if cut < PAGE_LIMIT // 2:
            cut = PAGE_LIMIT
        pages.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    return pages or ["Yanıt yok."]


class TelegramDraftStream:
    """Geçici zengin taslağı akıtır; finali sıkı aralıklı biçimli mesaj yapar."""

    def __init__(self, api: TelegramAPI, chat_id: int, fallback: TelegramStream) -> None:
        self.api = api
        self.chat_id = chat_id
        self.fallback = fallback
        self.draft_id = secrets.randbelow(2**31 - 1) + 1
        self.native = True
        self.finished = False
        self.visible = ""
        self.rich_message: Dict[str, str] = {"html": "<tg-thinking>Düşünüyor…</tg-thinking>"}
        self.sent = ""
        self.last_draft = 0.0

    async def show(
        self, text: str, *, rich_message: Optional[Dict[str, str]] = None,
        force: bool = False, immediate: bool = False,
    ) -> None:
        if force:
            await self.finish(text)
            return
        if self.finished:
            return
        self.visible = text[:PAGE_LIMIT]
        self.rich_message = rich_message or {"markdown": self.visible}
        await self.tick(immediate=immediate)

    async def tick(self, immediate: bool = False) -> None:
        if self.finished:
            return
        if not self.native:
            await self.fallback.flush()
            return
        now = time.monotonic()
        if self.sent == self.visible and now - self.last_draft < 4.0:
            return
        if not immediate and self.sent != self.visible and now - self.last_draft < EDIT_INTERVAL:
            return
        try:
            await self.api.send_draft(self.chat_id, self.draft_id, self.rich_message)
        except TelegramError as error:
            # Yarım Markdown (örn. kapanmamış ** veya bağlantı) taslağı reddedilebilir.
            if error.status == 400 and "markdown" in self.rich_message:
                plain = {"html": html.escape(self.visible)}
                try:
                    await self.api.send_draft(self.chat_id, self.draft_id, plain)
                except TelegramError as plain_error:
                    error = plain_error
                else:
                    self.sent = self.visible
                    self.last_draft = time.monotonic()
                    return
            if error.status not in (400, 404):
                raise error
            self.native = False
            await self.fallback.show(self.visible or "⏳ Düşünüyor…")
            return
        self.sent = self.visible
        self.last_draft = time.monotonic()

    async def finish(self, answer: str) -> None:
        if self.finished:
            return
        self.finished = True
        for page in _markdown_pages(answer.strip()):
            formatted = _legacy_markdown_html(page)
            try:
                if self.fallback.message_id is None:
                    self.fallback.message_id = await self.api.send_html(self.chat_id, formatted)
                else:
                    await self.api.edit_html(self.chat_id, self.fallback.message_id, formatted)
            except TelegramError as error:
                if error.status not in (400, 404):
                    raise
                await self.fallback.show(page, force=True)
            self.fallback.page = page
            self.fallback.sent = page
            self.fallback.message_id = None


def _compact_tool_status(name: str, preview: str, output: str = "") -> tuple[str, Dict[str, str]]:
    """Araç olayunu kısa canlı durum ve güvenli zengin içeriğe çevirir."""
    labels = {
        "web_search": "Web’de arıyor…",
        "browse_url": "Sayfayı açıyor…",
        "fetch_raw": "Web içeriğini okuyor…",
        "execute_shell": "Komut çalıştırıyor…",
        "execute_js": "Kod çalıştırıyor…",
        "read_file": "Dosya okuyor…",
        "write_file": "Dosyayı yazıyor…",
    }
    label = labels.get(name, f"{tool_label(name)} çalışıyor…")
    detail = preview.strip()[:240]
    tail = output.strip()[-300:]
    visible = "⏳ " + label
    if detail:
        visible += "\n" + detail
    if tail:
        visible += "\n" + tail
    rich = "<tg-thinking>" + html.escape(label) + "</tg-thinking>"
    if detail:
        rich += "\n<pre>" + html.escape(detail) + "</pre>"
    if tail:
        rich += "\n<pre>" + html.escape(tail) + "</pre>"
    return visible, {"html": rich}


class CompactPresenter:
    """Kısa görünümde taslak durumunu ve biçimli son yanıtı yönetir."""

    def __init__(self, stream: TelegramDraftStream) -> None:
        self.stream = stream
        self.turn_text = ""
        self.hide_turn = False
        self.finished = False
        self.active_tool: Optional[tuple[str, str, str, str]] = None

    async def event(self, event: AgentEvent) -> None:
        kind = event["kind"]
        if kind in ("run_started", "turn_started", "stream_reset"):
            self.turn_text = ""
            self.hide_turn = False
            self.active_tool = None
            await self.stream.show(
                "⏳ Düşünüyor…",
                rich_message={"html": "<tg-thinking>Düşünüyor…</tg-thinking>"},
                immediate=True,
            )
        elif kind == "text_delta" and not self.hide_turn:
            self.turn_text += event["text"]
            visible = self.turn_text.lstrip()
            if "STATE:".startswith(visible.upper()):
                return
            if visible.upper().startswith("STATE:"):
                self.hide_turn = True
                return
            await self.stream.show(self.turn_text)
        elif kind in ("tool_call_preview", "tool_started"):
            self.hide_turn = True
            name = event["name"]
            preview = event["preview"]
            call_id = event.get("call_id", "") if kind == "tool_started" else ""
            self.active_tool = (call_id, name, preview, "")
            visible, rich = _compact_tool_status(name, preview)
            await self.stream.show(
                visible, rich_message=rich, immediate=kind == "tool_started",
            )
        elif kind == "tool_output" and self.active_tool is not None:
            call_id, name, preview, output = self.active_tool
            if event["call_id"] != call_id:
                return
            output = (output + event["text"])[-300:]
            self.active_tool = (call_id, name, preview, output)
            visible, rich = _compact_tool_status(name, preview, output)
            await self.stream.show(visible, rich_message=rich)
        elif kind == "tool_finished":
            if self.active_tool is not None and event["call_id"] == self.active_tool[0]:
                self.active_tool = None
                label = "Tamamlandı" if event["ok"] else "Araç başarısız"
                await self.stream.show(
                    ("✓ " if event["ok"] else "⚠️ ") + label + " · düşünüyor…",
                    rich_message={"html": "<tg-thinking>" + label + "</tg-thinking>"},
                )
        elif kind == "integration_status":
            progress = f" {event['completed']}/{event['total']}" if event["total"] else ""
            label = event["stage"] + progress
            await self.stream.show(
                "⏳ " + label,
                rich_message={"html": "<tg-thinking>" + html.escape(label) + "</tg-thinking>"},
            )
        elif kind == "run_finished":
            self.finished = True
            answer = str(event["outcome"]).strip() or str(event["reason"]).strip() or "Yanıt yok."
            await self.stream.finish(answer if event["success"] else f"⚠️ {answer}")

    async def finish(self, report: RunReport) -> None:
        if not self.finished:
            answer = str(report["outcome"]).strip() or str(report.get("reason", "")).strip() or "Yanıt yok."
            await self.stream.finish(answer if report["success"] else f"⚠️ {answer}")


class TelegramBridge:
    """Tek özel sohbetten görev başlatır, soruları yanıtlar ve Esc eşdeğeri durdurur."""

    def __init__(self, api: TelegramAPI, settings: TelegramSettings) -> None:
        self.api = api
        self.settings = settings
        saved = read_json(offset_path(), {"offset": 0})
        self.offset = int(saved.get("offset", 0)) if isinstance(saved, dict) else 0
        self.clients: Dict[str, Optional[AsyncOpenAI]] = {}
        loaded_history = read_json(history_path(), [])
        safe_history = [
            entry for entry in loaded_history
            if isinstance(entry, dict) and isinstance(entry.get("goal"), str)
            and isinstance(entry.get("answer"), str) and isinstance(entry.get("tools"), list)
            and all(isinstance(tool, str) for tool in entry["tools"])
        ] if isinstance(loaded_history, list) else []
        self.history: list[Exchange] = trim_history(safe_history)
        self.active: Optional[asyncio.Task[None]] = None
        self.stop_event = threading.Event()
        self.pending_answer: Optional[asyncio.Future[Dict[str, Any]]] = None
        self.pending_fields: Dict[str, Any] = {}
        self.backend: Optional[str] = None
        self.run_mode = "normal"
        self.verbose = False
        self.goal = ""

    async def answer(self, title: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        if self.pending_answer is not None:
            raise TelegramError("Zaten bir kullanıcı yanıtı bekleniyor.")
        future: asyncio.Future[Dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.pending_answer = future
        self.pending_fields = fields
        names = ", ".join(fields)
        await self.api.send(
            self.settings["chat_id"],
            f"❔ {title[:1800]}\nAlanlar: {names}\n"
            "Tek alan için yanıtı yazın; birden çok alan için JSON nesnesi gönderin. /stop iptal eder.",
        )
        try:
            return await future
        finally:
            self.pending_answer = None
            self.pending_fields = {}

    async def _execute(self, goal: str) -> None:
        queue: asyncio.Queue[AgentEvent] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def emit(event: AgentEvent) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, event)

        options: RunOptions = {
            "requested_backend": self.backend,
            "should_stop": self.stop_event.is_set,
            "state_file": STATE_FILE,
            "history": trim_history(self.history),
            "answer": self.answer,
            "run_mode": self.run_mode,
        }

        async def work() -> RunReport:
            with host_task_lock():
                return await run_agent_with_callback(goal, emit, options, self.clients)

        worker = asyncio.create_task(work())
        stream = TelegramStream(self.api, self.settings["chat_id"])
        live = TelegramDraftStream(self.api, self.settings["chat_id"], stream)
        compact = CompactPresenter(live)
        verbose = self.verbose
        screenshot_paths: Dict[str, Path] = {}
        saw_finished = False
        try:
            while not worker.done() or not queue.empty():
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=0.25)
                except TimeoutError:
                    if verbose:
                        await stream.flush()
                    else:
                        await live.tick()
                    continue
                saw_finished = saw_finished or event["kind"] == "run_finished"
                if event["kind"] == "tool_started" and event["name"] == "take_screenshot":
                    screenshot_paths[event["call_id"]] = Path(event["preview"]).expanduser()
                if verbose:
                    rendered = event_text(event)
                    if rendered:
                        await stream.append(rendered)
                else:
                    await compact.event(event)
                if event["kind"] == "tool_finished" and event["ok"]:
                    image = screenshot_paths.pop(event["call_id"], None)
                    if image is not None and image.is_file():
                        try:
                            await self.api.send_photo(self.settings["chat_id"], image)
                        except TelegramError as error:
                            await stream.append(f"\n! Ekran görüntüsü gönderilemedi: {error}\n")
            report = await worker
            # Aynı turdaki call_soon_threadsafe olaylarını son sayfadan önce işle.
            await asyncio.sleep(0)
            while not queue.empty():
                event = queue.get_nowait()
                saw_finished = saw_finished or event["kind"] == "run_finished"
                if verbose:
                    rendered = event_text(event)
                    if rendered:
                        await stream.append(rendered)
                else:
                    await compact.event(event)
            if verbose:
                if not saw_finished:
                    await stream.append(
                        f"\n{'✓' if report['success'] else '✗'} {report['outcome'][:1200]}\n"
                    )
            else:
                await compact.finish(report)
            self.history = trim_history(self.history + [report["exchange"]])
            save_json(history_path(), self.history)
        except (HostBusyError, TelegramError) as error:
            try:
                if verbose:
                    await stream.append(f"\n✗ {error}\n")
                else:
                    await live.finish(f"⚠️ {error}")
            except TelegramError:
                pass
        except Exception as error:
            try:
                message = f"Görev hatası: {type(error).__name__}: {str(error)[:300]}"
                if verbose:
                    await stream.append(f"\n✗ {message}\n")
                else:
                    await live.finish(f"⚠️ {message}")
            except TelegramError:
                pass
        finally:
            if not worker.done():
                self.stop_event.set()
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
            self.stop_event.clear()
            self.goal = ""
            self.active = None
            try:
                await stream.flush(force=True)
            except TelegramError:
                pass

    async def _reply_to_question(self, text: str) -> None:
        future = self.pending_answer
        if future is None or future.done():
            return
        try:
            if len(self.pending_fields) == 1:
                name = next(iter(self.pending_fields))
                value = {name: text}
            else:
                value = json.loads(text)
                if not isinstance(value, dict) or not all(name in value for name in self.pending_fields):
                    raise ValueError("Gerekli alanları içeren JSON nesnesi bekleniyor.")
        except ValueError as error:
            await self.api.send(self.settings["chat_id"], f"Yanıt biçimi hatalı: {error}")
            return
        future.set_result(value)
        await self.api.send(self.settings["chat_id"], "Yanıt alındı; görev sürüyor.")

    async def handle(self, update: Dict[str, Any]) -> None:
        message = update.get("message")
        if not isinstance(message, dict) or not authorized(message, self.settings):
            return
        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            return
        text = text.strip()
        chat_id = self.settings["chat_id"]
        if text == "/stop":
            if self.active is None:
                await self.api.send(chat_id, "Çalışan görev yok.")
            else:
                self.stop_event.set()
                if self.pending_answer is not None and not self.pending_answer.done():
                    self.pending_answer.set_exception(IntegrationStopped("Kullanıcı tarafından durduruldu."))
                await self.api.send(chat_id, "Durdurma istendi; çalışan işlem iptal ediliyor.")
            return
        if text == "/status":
            state = f"Çalışıyor: {self.goal[:400]}" if self.active is not None else "Hazır."
            await self.api.send(chat_id, state)
            return
        if text in ("/start", "/help"):
            await self.api.send(
                chat_id,
                "Hedefinizi yazın. /stop durdurur, /status durumu gösterir. "
                "/verbose on ayrıntılı akışı açar; /verbose off kısa yanıtı kullanır. "
                "/model <profil> ve /mode <normal|long|autonomous> sonraki görevi ayarlar.",
            )
            return
        if self.pending_answer is not None:
            await self._reply_to_question(text)
            return
        if text in ("/verbose on", "/verbose off"):
            self.verbose = text.endswith("on")
            await self.api.send(chat_id, "Sonraki görev: ayrıntılı akış." if self.verbose else "Sonraki görev: kısa görünüm.")
            return
        if text.startswith("/model "):
            selected = text.split(None, 1)[1].strip()
            if selected != "auto" and selected not in BACKENDS:
                await self.api.send(chat_id, "Bilinmeyen model profili.")
                return
            self.backend = None if selected == "auto" else selected
            await self.api.send(chat_id, f"Sonraki görev modeli: {selected}.")
            return
        if text.startswith("/mode "):
            selected = text.split(None, 1)[1].strip()
            if selected not in ("normal", "long", "extended", "autonomous"):
                await self.api.send(chat_id, "Mod: normal, long veya autonomous.")
                return
            self.run_mode = "extended" if selected == "long" else selected
            await self.api.send(chat_id, f"Sonraki görev modu: {selected}.")
            return
        if self.active is not None:
            await self.api.send(chat_id, "Bir görev çalışıyor. /stop veya /status kullanın.")
            return
        self.goal = text
        self.stop_event.clear()
        self.active = asyncio.create_task(self._execute(text))

    async def run(self) -> None:
        self.clients = create_model_clients()
        try:
            failures = 0
            while True:
                try:
                    updates = await self.api.call("getUpdates", {
                        "offset": self.offset, "timeout": POLL_SECONDS,
                        "allowed_updates": ["message"],
                    })
                except TelegramError as error:
                    if error.status is not None and error.status < 500 and error.status != 429:
                        raise
                    failures += 1
                    await asyncio.sleep(min(8, 2 ** min(failures - 1, 3)))
                    continue
                failures = 0
                if not isinstance(updates, list):
                    raise TelegramError("getUpdates: liste bekleniyor.")
                for update in updates:
                    if not isinstance(update, dict) or not isinstance(update.get("update_id"), int):
                        continue
                    update_id = update["update_id"]
                    if update_id < self.offset:
                        continue
                    self.offset = update_id + 1
                    # Tekrarlanan uzaktan komut yan etkiyi yeniden başlatmasın.
                    save_json(offset_path(), {"offset": self.offset})
                    await self.handle(update)
        finally:
            self.stop_event.set()
            if self.active is not None:
                try:
                    await asyncio.wait_for(self.active, timeout=5)
                except (TimeoutError, asyncio.CancelledError):
                    self.active.cancel()
                    await asyncio.gather(self.active, return_exceptions=True)
            await close_model_clients(self.clients)
            await self.api.close()


async def pair(api: TelegramAPI, nonce: str, timeout: float = 180.0) -> TelegramSettings:
    """Yerel ekrandaki tek kullanımlık kodu gönderen özel sohbeti yetkilendirir."""
    deadline = time.monotonic() + timeout
    offset = -1
    while time.monotonic() < deadline:
        updates = await api.call("getUpdates", {
            "offset": offset, "timeout": min(POLL_SECONDS, max(1, int(deadline - time.monotonic()))),
            "allowed_updates": ["message"],
        })
        if not isinstance(updates, list):
            continue
        for update in updates:
            if not isinstance(update, dict) or not isinstance(update.get("update_id"), int):
                continue
            offset = update["update_id"] + 1
            message = update.get("message")
            if not isinstance(message, dict) or message.get("text") != f"/pair {nonce}":
                continue
            chat, sender = message.get("chat"), message.get("from")
            if not isinstance(chat, dict) or not isinstance(sender, dict) or chat.get("type") != "private":
                continue
            chat_id, user_id = chat.get("id"), sender.get("id")
            if isinstance(chat_id, int) and isinstance(user_id, int) and chat_id > 0 and user_id > 0:
                save_json(offset_path(), {"offset": offset})
                return {"chat_id": chat_id, "user_id": user_id}
    raise TelegramError("Eşleştirme süresi doldu. setup komutunu yeniden çalıştırın.")


async def setup() -> None:
    token = getpass.getpass("BotFather tokenı (Keychain'e kaydedilir): ").strip()
    if ":" not in token:
        raise TelegramError("BotFather tokenı geçersiz görünüyor.")
    api = TelegramAPI(token)
    try:
        identity = await api.call("getMe", {})
        name = identity.get("username", "bot") if isinstance(identity, dict) else "bot"
        nonce = secrets.token_urlsafe(12)
        print(f"Telegram'da @{name} botuna /pair {nonce} gönderin (3 dakika).")
        settings = await pair(api, nonce)
        Keyring().set_password(TOKEN_SERVICE, TOKEN_ACCOUNT, token)
        save_json(settings_path(), settings)
        await api.send(settings["chat_id"], "OmniAgent eşleştirildi. /help yazarak başlayabilirsiniz.")
        print("Eşleştirme tamamlandı; token yalnız Keychain'de.")
    finally:
        await api.close()


def install_service() -> None:
    """Kullanıcı hesabında yeniden girişte başlayan launchd hizmetini kurar."""
    load_settings()
    load_token()
    label = "com.omniagent.telegram"
    domain = f"gui/{os.getuid()}"
    existing = subprocess.run(
        ["launchctl", "print", f"{domain}/{label}"],
        capture_output=True, text=True, check=False,
    )
    if existing.returncode == 0:
        print("Telegram hizmeti zaten çalışıyor; yeniden kurulum yapılmadı.")
        return
    path = Path.home() / "Library/LaunchAgents" / f"{label}.plist"
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "Label": label,
        "ProgramArguments": [sys.executable, str(Path(__file__).resolve()), "run"],
        "WorkingDirectory": str(Path(__file__).resolve().parent),
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(data_root() / "telegram-stdout.log"),
        "StandardErrorPath": str(data_root() / "telegram-stderr.log"),
    }
    data_root().mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(record))
    path.chmod(0o600)
    result = subprocess.run(
        ["launchctl", "bootstrap", domain, str(path)],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise TelegramError(f"launchd başlatılamadı: {result.stderr.strip()[:300]}")
    print(f"Telegram hizmeti kuruldu: {path}")


async def run_bridge() -> None:
    """Tek yoklayıcıyı çalıştırır; iki süreç aynı komutu iki kez işlemez."""
    settings = load_settings()
    with host_task_lock(data_root() / "telegram-bridge.lock"):
        api = TelegramAPI(load_token())
        bridge = TelegramBridge(api, settings)
        await bridge.run()


def main() -> None:
    parser = argparse.ArgumentParser(description="OmniAgent Telegram köprüsü")
    parser.add_argument("action", choices=("setup", "run", "install-service"))
    arguments = parser.parse_args()
    try:
        if arguments.action == "setup":
            asyncio.run(setup())
        elif arguments.action == "install-service":
            install_service()
        else:
            asyncio.run(run_bridge())
    except (TelegramError, HostBusyError, KeyboardInterrupt) as error:
        print(f"Telegram: {error}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
