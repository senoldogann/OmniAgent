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
from typing import Awaitable, Callable, Dict, List, NotRequired, Optional, Tuple, TypedDict

from openai import AsyncOpenAI

from omniagent.app.agent import call_model_with_retries
from omniagent.app.types import ToolCallDraft
from omniagent.companion.bubbles import (
    MAX_BUBBLE_CHARS, MAX_BUBBLES, bubble_delay, clean_line, split_complete_lines,
)
from omniagent.core.events import AgentEvent
from omniagent.memory.personal import ArchivedMessage, ChatToolCall

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
_QUERY_ATTRIBUTE = re.compile(r"""query\s*=\s*(?:"(?P<double>[^"]*)"|'(?P<single>[^']*)')""")
_FACT_ATTRIBUTE = re.compile(r"""fact_id\s*=\s*["']?#?(?P<id>\d{1,9})\b""")
# Kullanıcının unutma isteği ("şunu unut", "unutur musun", "hafızandan sil") ve modelin "unuttum" iddiası. İkisi aynı
# turdaysa ve forget çağrılmadıysa söz tutulmamıştır. "unutma", "unuttun mu" ve geçiştirme deyimleri ("unut gitsin",
# "boşver unut", "neyse unut") istek değildir: yanlış eşleşme zorunlu düzeltme çağrısıyla bilgi sildirebilirdi.
_FORGET_REQUEST = re.compile(
    r"(?<!boşver )(?<!boş ver )(?<!neyse )\bunut(?:ur\s+mu[sş]un|abilir\s+mi[sş]in)?\b(?!\s+gitsin)"
    r"|\b(?:hafızandan|aklından)\s+(?:sil|çıkar|at)\b", re.IGNORECASE)
_FORGET_CLAIM = re.compile(
    r"\b(?:unuttum|unutuyorum|unutacağım|unutayım|unutuldu|sildim|siliyorum|sileceğim|silindi)\b", re.IGNORECASE)
_MEMORY_TOOLS: frozenset[str] = frozenset({"recall", "forget"})
# Söz verip aracı çağırmayan modele düzeltme çağrısında verilen host notu.
PROMISE_CORRECTION: str = (
    "[HOST] Kullanıcıya bakacağını söyledin ama start_task aracını çağırmadın; hiçbir iş başlamadı. Şimdi yalnız "
    "start_task aracını, kullanıcının son isteğini tek başına anlaşılır anlatan bir goal ile çağır. Metin yazma."
)
# Unuttuğunu söyleyip forget çağırmayan modele düzeltme çağrısında verilen host notu.
FORGET_CORRECTION: str = (
    "[HOST] Kullanıcıya bir bilgiyi unuttuğunu söyledin ama forget aracını çağırmadın; hiçbir bilgi silinmedi. Kullanıcı "
    "KANITLI PROFİL'deki belirli bir bilgiyi unutmanı istediyse yalnız forget aracını o [#numara] ile çağır; belirli bir "
    "bilgiyi kastetmiyorsa hiçbir araç çağırma. Metin yazma."
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
RECALL_TOOL: Dict[str, object] = {
    "type": "function",
    "function": {
        "name": "recall",
        "description": (
            "Kullanıcıyla daha önce konuşulanlarda (iMessage, Telegram, masaüstü) ve kanıtlı bilgilerde arar; en çok 8 "
            "birebir parça tarihiyle döner. Hatırlamadığın bir şey sorulunca 'bakayım' demeden hemen çağır."
        ),
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Aranacak birkaç kelime (ör. 'İzmir', 'kızı adı')."}},
            "required": ["query"],
        },
    },
}
FORGET_TOOL: Dict[str, object] = {
    "type": "function",
    "function": {
        "name": "forget",
        "description": "Kullanıcı bir bilgiyi unutmanı isteyince KANITLI PROFİL'deki o bilgiyi [#numara] ile unutur.",
        "parameters": {
            "type": "object",
            "properties": {"fact_id": {"type": "integer", "description": "Profildeki [#numara]."}},
            "required": ["fact_id"],
        },
    },
}
CHAT_TOOLS: List[Dict[str, object]] = [START_TASK_TOOL, RECALL_TOOL, FORGET_TOOL]


class ChatError(Exception):
    """Sohbet modeli kullanılabilir yanıt üretmedi (ne metin ne iş)."""


class ChatResult(TypedDict):
    bubbles: List[str]
    start_task: Optional[str]
    # Hafıza araçları (Faz B+) yalnız çağrıldıklarında bulunur; Faz A sözleşmesi ve sahteleri değişmez.
    recall: NotRequired[str]
    forget: NotRequired[List[int]]


def history_messages(history: List[ArchivedMessage], starts: Dict[int, str],
                     memory_calls: Dict[int, List[ChatToolCall]]) -> List[ChatMessage]:
    """
    Arşivi sohbet mesajlarına çevirir; art arda aynı yöndeki balonlar tek mesajda birleşir.
    - İş başlatan balon (`starts`: balon kimliği → hedef) gerçek start_task çağrısı ve sonucuyla gösterilir.
    - Hafıza çağrıları (`memory_calls`: yanıtın ilk balonu → recall/forget) o balondan ÖNCE gerçek çağrı ve sonuç
      olarak gösterilir. Böylece model "unuttum" ya da "hatırladım" dediği yerde aracı çağırdığını görür.
    - Çağrı kaydı olmayan "bakıyorum" sözleri (hazır TASK_ACK ya da modelin yazdığı) atılır: çağrısız söz geçmişte
      kalınca model aracı çağırmadan söz vermeyi taklit ediyordu.
    Saf.
    """
    messages: List[ChatMessage] = []
    for item in history:
        calls: List[ChatToolCall] = memory_calls.get(item["id"], [])
        if (item["direction"] == "out" and item["kind"] == "chat" and item["id"] not in starts and not calls
                and promises_action(item["text"])):
            continue
        if calls:
            messages = _with_memory_calls(messages, f"memory-{item['id']}", calls)
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


def recall_result(query: str, lines: List[str]) -> str:
    """recall aracının sonucu. Yalnız 'kullanıcı' satırları ve [#numara] bilgiler kanıttır; 'ajan' satırları Deniz'in
    kendi eski mesajlarıdır. Saf."""
    body: str = "\n".join(lines) if lines else "sonuç yok"
    return (f"[HAFIZA ARAMASI: {query[:100]}] Yalnız 'kullanıcı' satırları ve [#numara] bilgiler kullanıcı hakkında "
            f"kanıttır; 'ajan' satırları senin eski mesajlarındır.\n{body}")


def _with_memory_calls(messages: List[ChatMessage], prefix: str, calls: List[ChatToolCall]) -> List[ChatMessage]:
    """
    Hafıza çağrılarını sonuçlarıyla ekler. Çağrılar önceki asistan mesajına bağlanır; o mesaj yoksa ya da zaten çağrı
    taşıyorsa içeriksiz asistan mesajı açılır. Ardından her çağrının araç sonucu gelir. Girdiyi değiştirmez. Saf.
    """
    entries: List[Dict[str, object]] = [
        {"id": f"{prefix}-{index}", "type": "function",
         "function": {"name": call["name"], "arguments": call["arguments"]}}
        for index, call in enumerate(calls)
    ]
    results: List[ChatMessage] = [
        {"role": "tool", "tool_call_id": f"{prefix}-{index}", "content": call["result"]}
        for index, call in enumerate(calls)
    ]
    previous: Optional[ChatMessage] = messages[-1] if messages else None
    if previous is not None and previous["role"] == "assistant" and "tool_calls" not in previous:
        return [*messages[:-1], {**previous, "tool_calls": entries}, *results]
    return [*messages, {"role": "assistant", "content": "", "tool_calls": entries}, *results]


def recall_follow_up(messages: List[ChatMessage], bubbles: List[str], calls: List[ChatToolCall]) -> List[ChatMessage]:
    """İkinci turun girdisi: ilk turun balonları ve bu yanıttaki hafıza çağrıları, gerçek araç çağrısı + sonuç
    olarak. Saf."""
    base: List[ChatMessage] = ([*messages, {"role": "assistant", "content": "\n".join(bubbles)}] if bubbles
                               else list(messages))
    return _with_memory_calls(base, "memory-live", calls)


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


def textual_memory_calls(line: str) -> Tuple[Optional[str], List[int]]:
    """
    Metne yazılmış recall/forget etiketlerinin argümanları: ilk recall sorgusu ve forget kimlikleri. Etiketler
    textual_start_task'ta zaten silinir ve kullanıcıya gitmez; bu fonksiyon yalnız okur. Saf.
    """
    query: Optional[str] = None
    fact_ids: List[int] = []
    for match in _TEXTUAL_CALL.finditer(line):
        attributes: str = match.group("attrs") or ""
        if match.group("name") == "recall" and query is None:
            found = _QUERY_ATTRIBUTE.search(attributes)
            text: str = (found.group("double") or found.group("single") or "").strip() if found is not None else ""
            query = text or None
        elif match.group("name") == "forget":
            found_id = _FACT_ATTRIBUTE.search(attributes)
            if found_id is not None:
                fact_ids.append(int(found_id.group("id")))
    return query, fact_ids


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


def forget_requested(text: str) -> bool:
    """Kullanıcı bir bilgiyi unutmayı istiyor mu ('şunu unut', 'unutur musun', 'hafızandan sil')? Saf."""
    return _FORGET_REQUEST.search(text) is not None


def claims_forgotten(text: str) -> bool:
    """Balon bir unuttum/sildim iddiası mı? Saf."""
    return _FORGET_CLAIM.search(text) is not None


def _ignore_event(event: AgentEvent) -> None:
    """Düzeltme çağrısının akışı kullanıcıya gönderilmez."""


async def _correction_calls(
    clients: Dict[str, AsyncOpenAI], backend: str, system: str, messages: List[ChatMessage], bubbles: List[str],
    should_stop: Callable[[], bool], session_id: str, correction: str,
) -> List[ToolCallDraft]:
    """Verilen söz ve host düzeltme notuyla tek çağrı yapar; akış kullanıcıya gitmez, dönen araç çağrılarını verir."""
    request: List[ChatMessage] = [
        {"role": "system", "content": system}, *messages,
        {"role": "assistant", "content": "\n".join(bubbles)},
        {"role": "user", "content": correction},
    ]
    turn, _answered_by = await call_model_with_retries(
        clients, request, CHAT_TOOLS, session_id, backend, _ignore_event, should_stop,
    )
    return turn["tool_calls"]


async def recover_promised_task(
    clients: Dict[str, AsyncOpenAI], backend: str, system: str, messages: List[ChatMessage], bubbles: List[str],
    should_stop: Callable[[], bool], session_id: str,
) -> Optional[str]:
    """
    Model iş sözü verip start_task çağırmadıysa sözü tutturur: aynı bağlam, verilen söz ve düzeltme notuyla tek
    çağrı yapar, çıkan start_task hedefini döner (yine çağırmazsa ya da durdurulursa None). Akış kullanıcıya
    gitmez. `should_stop` çağıranın durdurma bayrağıdır (köprü kapanırken yeniden deneme beklemesi sürmez).
    """
    return parse_start_task(await _correction_calls(
        clients, backend, system, messages, bubbles, should_stop, session_id, PROMISE_CORRECTION,
    ))


async def recover_promised_forget(
    clients: Dict[str, AsyncOpenAI], backend: str, system: str, messages: List[ChatMessage], bubbles: List[str],
    should_stop: Callable[[], bool], session_id: str,
) -> List[int]:
    """
    Kullanıcı unutmayı istedi, model "unuttum" dedi ama forget çağırmadı: aynı bağlam, verilen söz ve FORGET_CORRECTION
    ile tek çağrı yapılır ve çıkan forget kimlikleri döner (yine çağırmazsa boş). Akış kullanıcıya gitmez.
    """
    return parse_memory_calls(await _correction_calls(
        clients, backend, system, messages, bubbles, should_stop, session_id, FORGET_CORRECTION,
    ))[1]


def parse_start_task(tool_calls: List[ToolCallDraft]) -> Optional[str]:
    """start_task çağrısının goal'ü; geçersiz ya da bilinmeyen çağrılar uyarıyla çalıştırılmaz."""
    for call in tool_calls:
        if call["name"] != "start_task":
            if call["name"] not in _MEMORY_TOOLS:
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


def _call_arguments(call: ToolCallDraft) -> Optional[Dict[str, object]]:
    """Araç çağrısının JSON argümanları; çözülemezse uyarıyla None (argüman metni loglanmaz)."""
    try:
        arguments: object = json.loads(call["arguments"] or "{}")
    except json.JSONDecodeError as error:
        logging.warning("Sohbet aracı argümanı JSON değil; çalıştırılmadı",
                        extra={"tool": call["name"][:80], "error": error.msg})
        return None
    if not isinstance(arguments, dict):
        logging.warning("Sohbet aracı argümanı nesne değil; çalıştırılmadı", extra={"tool": call["name"][:80]})
        return None
    return arguments


def _fact_id(value: object) -> Optional[int]:
    """forget kimliği: pozitif tam sayı ya da '12'/'#12' metni; değilse None. Saf."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.strip().lstrip("#").isdigit():
        number: int = int(value.strip().lstrip("#"))
        return number if number > 0 else None
    return None


def parse_memory_calls(tool_calls: List[ToolCallDraft]) -> Tuple[Optional[str], List[int]]:
    """Gerçek recall (ilk geçerli sorgu) ve forget (tekil kimlikler) çağrıları; geçersiz argümanlı çağrı uyarıyla
    atlanır."""
    query: Optional[str] = None
    fact_ids: List[int] = []
    for call in tool_calls:
        if call["name"] not in _MEMORY_TOOLS:
            continue
        arguments: Optional[Dict[str, object]] = _call_arguments(call)
        if arguments is None:
            continue
        if call["name"] == "recall":
            value: object = arguments.get("query")
            if query is None and isinstance(value, str) and value.strip():
                query = value.strip()
            elif query is None:
                logging.warning("recall boş sorguyla çağrıldı; çalıştırılmadı",
                                extra={"arguments_chars": len(call["arguments"])})
            continue
        fact_id: Optional[int] = _fact_id(arguments.get("fact_id"))
        if fact_id is None:
            logging.warning("forget geçersiz kimlikle çağrıldı; çalıştırılmadı",
                            extra={"arguments_chars": len(call["arguments"])})
        elif fact_id not in fact_ids:
            fact_ids.append(fact_id)
    return query, fact_ids


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
    textual_recalls: List[str] = []  # metne yazılmış recall sorguları
    textual_forgets: List[int] = []  # metne yazılmış forget kimlikleri (akış yeniden denense de bir kez)

    def visible_text(line: str) -> str:
        """Metinsel araç çağrılarını ayıklar (argümanlarını saklar) ve kalan satırı temizler."""
        visible, goal = textual_start_task(line)
        if goal is not None:
            textual_goals.append(goal)
        query, fact_ids = textual_memory_calls(line)
        if query is not None:
            textual_recalls.append(query)
        for fact_id in fact_ids:
            if fact_id not in textual_forgets:
                textual_forgets.append(fact_id)
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
    recall, forget = parse_memory_calls(turn["tool_calls"])
    if recall is None and textual_recalls:
        logging.info("Sohbet modeli hafıza aramasını metin içinde çağırdı", extra={"calls": len(textual_recalls)})
        recall = textual_recalls[0]
    if not forget and textual_forgets:
        logging.info("Sohbet modeli unutmayı metin içinde çağırdı", extra={"calls": len(textual_forgets)})
        forget = textual_forgets
    if not sent and start_task is None and recall is None and not forget and turn["finish_reason"] != "stopped":
        raise ChatError(f"Sohbet modeli boş yanıt döndürdü (finish_reason={turn['finish_reason']}).")
    result: ChatResult = {"bubbles": sent, "start_task": start_task}
    if recall is not None:
        result["recall"] = recall
    if forget:
        result["forget"] = forget
    return result
