"""Hızlı sohbet katmanı: küçük istem + tek model çağrısı → iMessage balonları; gerekirse iş devri.

Model çağrısı ajanın yeniden deneme ve hata sınıflandırması sözleşmesini (agent.call_model_with_retries) kullanır;
yeni istemci yazılmaz. Metin akarken tamamlanan satırlar hemen balon olur (ilk balon turun bitmesini beklemez);
akış yeniden denenirse (stream_reset) gönderilmiş satırlar atlanır, balon iki kez gitmez.
"""
from __future__ import annotations

import asyncio
import json
import re
import logging
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional, Tuple, TypedDict

from openai import AsyncOpenAI

from omniagent.app.agent import call_model_with_retries
from omniagent.app.types import ToolCallDraft
from omniagent.companion.bubbles import (
    MAX_BUBBLE_CHARS, MAX_BUBBLES, bubble_delay, clean_line, split_complete_lines,
)
from omniagent.core.events import AgentEvent
from omniagent.memory.personal import ArchivedMessage

HISTORY_LIMIT: int = 40
TASK_ACK: str = "tamam bakıyorum"
# Geçmişte iş başlatma turunun araç sonucu: model "bakıyorum" sözünün gerçek bir araç çağrısıyla geldiğini görür.
TASK_STARTED_NOTE: str = "İş başlatıldı; sonucu [İŞ RAPORU] olarak gelecek."
# Sohbet modeline giden mesaj: rol ve içerik; iş başlatan asistan mesajı ayrıca tool_calls, araç sonucu tool_call_id taşır.
ChatMessage = Dict[str, object]
# Aracı çağırmadan verilen iş sözü: birinci tekil şimdiki/gelecek/istek kipi ("bakıyorum", "bi kontrol edeyim").
# "bakalım" gibi ortak kip ve "bakayım mı?" gibi teklif söz değildir.
_PROMISE = re.compile(
    r"\b(?:bak(?:ıyorum|ayım|arım|acağım)|kontrol\s+ed(?:iyorum|eyim|erim|eceğim)|"
    r"araştır(?:ıyorum|ayım|ırım|acağım)|halled(?:iyorum|eyim|erim|eceğim)|"
    r"ilgilen(?:iyorum|eyim|irim|eceğim)|aç(?:ıyorum|ayım|arım|acağım))\b(?!\s*m[ıiuü]\b)",
    re.IGNORECASE,
)
# Metne yazılmış araç çağrısı (gemma4 bazen gerçek çağrı yerine böyle yazıyor): <call:ad attr="..."> [</call>] ya da />.
_TEXTUAL_CALL = re.compile(r"<call:(?P<name>\w+)(?P<attrs>[^>]*?)/?>(?:\s*</call>)?|</call>")
_GOAL_ATTRIBUTE = re.compile(r"""goal\s*=\s*(?:"(?P<double>[^"]*)"|'(?P<single>[^']*)')""")
# Söz verip aracı çağırmayan modele düzeltme çağrısında verilen host notu.
PROMISE_CORRECTION: str = (
    "[HOST] Kullanıcıya bakacağını söyledin ama start_task aracını çağırmadın; hiçbir iş başlamadı. Şimdi yalnız "
    "start_task aracını, kullanıcının son isteğini tek başına anlaşılır anlatan bir goal ile çağır. Metin yazma."
)
START_TASK_TOOL: Dict[str, object] = {
    "type": "function",
    "function": {
        "name": "start_task",
        "description": (
            "Bilgisayarda bir iş başlatır ya da bilgisayardaki bir şeye bakar (dosya, uygulama, web, ekran, "
            "hesaplama). İş arka planda çalışır; sonuç [İŞ RAPORU] olarak gelir."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "Tek başına anlaşılır, eksiksiz görev tanımı."},
            },
            "required": ["goal"],
        },
    },
}
CHAT_TOOLS: List[Dict[str, object]] = [START_TASK_TOOL]


class ChatError(Exception):
    """Sohbet modeli kullanılabilir yanıt üretmedi (ne metin ne iş)."""


class ChatResult(TypedDict):
    bubbles: List[str]
    start_task: Optional[str]


def history_messages(history: List[ArchivedMessage], starts: Dict[int, str]) -> List[ChatMessage]:
    """
    Arşivi sohbet mesajlarına çevirir; art arda aynı yöndeki balonlar tek mesajda birleşir. İş başlatan balon
    (`starts`: balon kimliği → hedef) gerçek start_task çağrısı ve sonucuyla gösterilir; iş kaydı olmayan "bakıyorum"
    sözleri (hazır TASK_ACK ya da modelin yazdığı) atılır. Çağrısız söz geçmişte kalınca model aracı çağırmadan
    "bakıyorum" demeyi taklit ediyor ve hiçbir iş başlamıyordu. Saf.
    """
    messages: List[ChatMessage] = []
    for item in history:
        if (item["direction"] == "out" and item["kind"] == "chat" and item["id"] not in starts
                and promises_action(item["text"])):
            continue
        role: str = "user" if item["direction"] == "in" else "assistant"
        previous: Optional[ChatMessage] = messages[-1] if messages else None
        if previous is not None and previous["role"] == role and "tool_calls" not in previous:
            messages[-1] = {"role": role, "content": f"{previous['content']}\n{item['text']}"}
        else:
            messages.append({"role": role, "content": item["text"]})
        if item["id"] in starts:
            call_id: str = f"start-{item['id']}"
            arguments: str = json.dumps({"goal": starts[item["id"]]}, ensure_ascii=False)
            messages[-1] = {**messages[-1], "tool_calls": [
                {"id": call_id, "type": "function", "function": {"name": "start_task", "arguments": arguments}},
            ]}
            messages.append({"role": "tool", "tool_call_id": call_id, "content": TASK_STARTED_NOTE})
    return messages


def burst_turn(texts: List[str], images: List[str], situation: str) -> str:
    """Burst'ü (ve fotoğraf eklerini) durum bloğuyla tek kullanıcı mesajına çevirir. Saf."""
    lines: List[str] = [*texts, *(f"[fotoğraf: {Path(path).name}]" for path in images)]
    return f"{situation}\n\n" + "\n".join(lines)


def report_turn(goal: str, success: bool, outcome: str, situation: str) -> str:
    """Biten işin raporunu sohbet katmanına verilecek girdiye çevirir. Saf."""
    return (f"{situation}\n\n[İŞ RAPORU — sonucu kullanıcıya kendi ağzından kısaca anlat]\n"
            f"iş: {goal[:300]}\nsonuç: {'başarılı' if success else 'başarısız'}\nçıktı: {outcome[:1500]}")


def textual_start_task(line: str) -> Tuple[str, Optional[str]]:
    """
    Satırdaki metinsel araç çağrısı etiketlerini siler; start_task etiketinin goal'ünü döner (yoksa None).
    Etiket kullanıcıya balon olarak gitmez; bilinmeyen araç etiketi silinir ve çalıştırılmaz. Saf.
    """
    goal: Optional[str] = None
    for match in _TEXTUAL_CALL.finditer(line):
        attribute = _GOAL_ATTRIBUTE.search(match.group("attrs") or "")
        if goal is None and match.group("name") == "start_task" and attribute is not None:
            found: str = (attribute.group("double") or attribute.group("single") or "").strip()
            goal = found or None
    visible: str = " ".join(_TEXTUAL_CALL.sub(" ", line).split())
    return visible, goal


def without_tool_calls(messages: List[ChatMessage]) -> List[ChatMessage]:
    """
    Araçsız tur için geçmişi düz metne indirir: tool mesajları atılır, asistan mesajlarından tool_calls kalkar.
    Bazı sağlayıcılar (Anthropic yönlendirmeli profiller) araç tanımı olmayan istekte araç çağrısı geçmişini reddeder. Saf.
    """
    return [
        {key: value for key, value in message.items() if key != "tool_calls"}
        for message in messages if message["role"] != "tool"
    ]


def promises_action(text: str) -> bool:
    """Balon bilgisayarda bir şey yapma sözü mü ('bakıyorum', 'bi kontrol edeyim')? Saf."""
    return _PROMISE.search(text) is not None


def _ignore_event(event: AgentEvent) -> None:
    """Düzeltme çağrısının akışı kullanıcıya gönderilmez."""


async def recover_promised_task(
    clients: Dict[str, AsyncOpenAI], backend: str, system: str, messages: List[ChatMessage], bubbles: List[str],
    should_stop: Callable[[], bool], session_id: str,
) -> Optional[str]:
    """
    Model iş sözü verip start_task çağırmadıysa sözü tutturur: aynı bağlam, verilen söz ve düzeltme notuyla tek
    çağrı yapar, çıkan start_task hedefini döner (yine çağırmazsa ya da durdurulursa None). Akış kullanıcıya
    gitmez. `should_stop` çağıranın durdurma bayrağıdır (köprü kapanırken yeniden deneme beklemesi sürmez).
    """
    correction: List[ChatMessage] = [
        {"role": "system", "content": system}, *messages,
        {"role": "assistant", "content": "\n".join(bubbles)},
        {"role": "user", "content": PROMISE_CORRECTION},
    ]
    turn, _answered_by = await call_model_with_retries(
        clients, correction, CHAT_TOOLS, session_id, backend, _ignore_event, should_stop,
    )
    return parse_start_task(turn["tool_calls"])


def parse_start_task(tool_calls: List[ToolCallDraft]) -> Optional[str]:
    """start_task çağrısının goal'ü; geçersiz ya da bilinmeyen çağrılar uyarıyla çalıştırılmaz."""
    for call in tool_calls:
        if call["name"] != "start_task":
            logging.warning("Sohbet modeli bilinmeyen araç çağırdı; çalıştırılmadı", extra={"tool": call["name"][:80]})
            continue
        try:
            arguments: object = json.loads(call["arguments"] or "{}")
        except json.JSONDecodeError as error:
            logging.warning("start_task argümanı JSON değil; çalıştırılmadı", extra={"error": str(error)[:200]})
            continue
        goal: object = arguments.get("goal") if isinstance(arguments, dict) else None
        if isinstance(goal, str) and goal.strip():
            return goal.strip()
        # Argüman metni loglanmaz: kullanıcı mesajından türeyen içerik içerebilir; yalnızca tür ve uzunluk yazılır.
        logging.warning(
            "start_task boş goal ile çağrıldı; çalıştırılmadı",
            extra={"goal_type": type(goal).__name__, "arguments_chars": len(call["arguments"])},
        )
    return None


async def respond(
    clients: Dict[str, AsyncOpenAI], backend: str, system: str, messages: List[ChatMessage],
    tools: List[Dict[str, object]], send_bubble: Callable[[str], Awaitable[None]], should_stop: Callable[[], bool],
    session_id: str,
) -> ChatResult:
    """
    Tek model turu. Tamamlanan satırlar akış sırasında balon olarak gönderilir: ilk MAX_BUBBLES-1 satır anında,
    kalanlar sonda tek balonda birleşir. Akış yeniden denenirse (stream_reset) gönderilmiş satırlar yeni akışta
    atlanır. Model ne metin ne iş üretirse ChatError.
    """
    queue: asyncio.Queue[Optional[str]] = asyncio.Queue()
    sent: List[str] = []
    held: List[str] = []
    buffer: str = ""
    streamed: int = 0      # bu akışta görülen boş olmayan satır sayısı
    queued: int = 0        # kuyruğa verilen balon sayısı (akış sıfırlansa da korunur)
    replay_skip: int = 0   # yeniden denenen akışta atlanacak, zaten kuyruğa verilmiş satır sayısı
    textual_goals: List[str] = []  # metne yazılmış start_task çağrılarının hedefleri

    def visible_text(line: str) -> str:
        """Metinsel araç çağrısını ayıklar (hedefini saklar) ve kalan satırı temizler."""
        visible, goal = textual_start_task(line)
        if goal is not None:
            textual_goals.append(goal)
        return clean_line(visible)

    def accept(line: str) -> None:
        nonlocal streamed, queued
        streamed += 1
        if streamed <= replay_skip:
            return
        if queued < MAX_BUBBLES - 1:
            queued += 1
            queue.put_nowait(line)
        else:
            held.append(line)

    def emit(event: AgentEvent) -> None:
        nonlocal buffer, streamed, replay_skip
        if event["kind"] == "stream_reset":
            buffer, streamed, replay_skip = "", 0, queued
            held.clear()
            return
        if event["kind"] != "text_delta":
            return
        complete, buffer = split_complete_lines(buffer, event["text"])
        for line in complete:
            cleaned: str = visible_text(line)
            if cleaned:
                accept(cleaned)

    async def sender() -> None:
        while True:
            bubble: Optional[str] = await queue.get()
            if bubble is None:
                return
            if sent:
                await asyncio.sleep(bubble_delay(bubble))
            await send_bubble(bubble)
            sent.append(bubble)

    sending: asyncio.Task[None] = asyncio.create_task(sender())
    try:
        turn, _answered_by = await call_model_with_retries(
            clients, [{"role": "system", "content": system}, *(messages if tools else without_tool_calls(messages))],
            tools, session_id, backend, emit, should_stop,
        )
    except BaseException:
        sending.cancel()
        await asyncio.gather(sending, return_exceptions=True)
        raise
    tail: str = visible_text(buffer)
    if tail:
        accept(tail)
    if held:
        queue.put_nowait(" ".join(held)[:MAX_BUBBLE_CHARS])
    queue.put_nowait(None)
    await sending
    start_task: Optional[str] = parse_start_task(turn["tool_calls"])
    if start_task is None and textual_goals:
        logging.info("Sohbet modeli aracı metin içinde çağırdı; iş başlatılıyor", extra={"calls": len(textual_goals)})
        start_task = textual_goals[0]
    if not sent and start_task is None and turn["finish_reason"] != "stopped":
        raise ChatError(f"Sohbet modeli boş yanıt döndürdü (finish_reason={turn['finish_reason']}).")
    return {"bubbles": sent, "start_task": start_task}
