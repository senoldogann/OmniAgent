"""Hızlı sohbet katmanı: küçük istem + tek model çağrısı → doğrulanmış iMessage finali; gerekirse iş devri.

Model çağrısı ajanın yeniden deneme ve hata sınıflandırması sözleşmesini (agent.call_model_with_retries) kullanır;
yeni istemci yazılmaz. Stream parçaları kullanıcıya gönderilmez; yalnız başarıyla tamamlanmış ModelTurn.content
tek semantik final olarak yayınlanır. Retry/reset taslakları ve başarısız akış parçaları kullanıcıya sızmaz.
"""
from __future__ import annotations

import base64
import json
import re
import logging
import math
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, NotRequired, Optional, Tuple, TypedDict

from openai import AsyncOpenAI

from omniagent.app.agent import call_model_with_retries
from omniagent.app.model_retry import ModelCallFailed
from omniagent.app.policy import final_verdict
from omniagent.app.types import ModelTurn, ToolCallDraft
from omniagent.approval import append_audit
from omniagent.config import QUALITY_LADDER
from omniagent.fallback_policy import (
    count_image_parts, load_fallback_policy, permitted_fallbacks, provider_fallback_audit, provider_fallback_event,
)
from omniagent.memory.personal import utc_now_iso
from omniagent.paths import data_root
from omniagent.companion.bubbles import final_chunks, final_message
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
MUTE_TOOL: Dict[str, object] = {
    "type": "function", "function": {"name": "mute", "description": "Kullanıcı isterse proaktifliği saat boyunca susturur.",
        "parameters": {"type": "object", "properties": {"hours": {"type": "number", "exclusiveMinimum": 0}},
                       "required": ["hours"]}},
}
PROACTIVE_TOOL: Dict[str, object] = {
    "type": "function", "function": {"name": "set_proactive", "description": "Kullanıcının isteğiyle proaktifliği açar veya kapatır.",
        "parameters": {"type": "object", "properties": {"enabled": {"type": "boolean"}}, "required": ["enabled"]}},
}
CHAT_TOOLS: List[Dict[str, object]] = [START_TASK_TOOL, RECALL_TOOL, FORGET_TOOL, MUTE_TOOL, PROACTIVE_TOOL]


class ChatError(Exception):
    """Sohbet modeli kullanılabilir yanıt üretmedi (ne metin ne iş)."""


class ChatResult(TypedDict):
    bubbles: List[str]
    start_task: Optional[str]
    # Hafıza araçları (Faz B+) yalnız çağrıldıklarında bulunur; Faz A sözleşmesi ve sahteleri değişmez.
    recall: NotRequired[str]
    forget: NotRequired[List[int]]
    mute: NotRequired[float]
    proactive: NotRequired[bool]


def control_calls(tool_calls: List[ToolCallDraft]) -> Dict[str, object]:
    result: Dict[str, object] = {}
    for call in tool_calls:
        if call["name"] not in {"mute", "set_proactive"}:
            continue
        arguments = _call_arguments(call)
        if arguments is None:
            continue
        if call["name"] == "mute":
            value = arguments.get("hours")
            if isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value) and 0 < value <= 8760:
                result["mute"] = float(value)
        elif isinstance(arguments.get("enabled"), bool):
            result["proactive"] = arguments["enabled"]
    return result


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


def image_turn(text: str, paths: List[Path]) -> ChatMessage:
    """Only prepared JPEGs enter this turn; the archive retains text markers."""
    if not paths:
        return {"role": "user", "content": text}
    parts: List[Dict[str, object]] = [{"type": "text", "text": text}]
    for path in paths:
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        parts.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}})
    return {"role": "user", "content": parts}


def image_rejected(error: ModelCallFailed) -> bool:
    return error.kind == "permanent" and bool(re.search(
        r"(?:image|vision|multimodal).{0,90}(?:not support|unsupported|not allowed|not available)|"
        r"(?:not support|unsupported).{0,90}(?:image|vision|multimodal)", str(error), re.IGNORECASE,
    ))


async def _chat_completion(
    clients: Dict[str, AsyncOpenAI], messages: List[ChatMessage], tools: List[Dict[str, object]],
    session_id: str, backend: str, emit: Callable[[AgentEvent], None], should_stop: Callable[[], bool],
) -> Tuple[ModelTurn, str]:
    """An explicitly rejected image may move only to an image permitted fallback."""
    try:
        return await call_model_with_retries(clients, messages, tools, session_id, backend, emit, should_stop)
    except ModelCallFailed as error:
        count = count_image_parts(messages)
        if not count or not image_rejected(error):
            raise
        policy = load_fallback_policy()
        permitted = permitted_fallbacks(frozenset(policy["backends"]), policy["allow_images"], count)
        for candidate in QUALITY_LADDER:
            if candidate == backend or candidate not in clients or candidate not in permitted:
                continue
            event = provider_fallback_event(backend, candidate, "sohbet profili görseli desteklemiyor", count)
            append_audit(data_root() / "audit.jsonl", provider_fallback_audit(event, utc_now_iso()))
            emit(event)
            try:
                return await call_model_with_retries(clients, messages, tools, session_id, candidate, emit, should_stop)
            except ModelCallFailed as fallback_error:
                if not image_rejected(fallback_error):
                    raise
        raise error


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
    Etiket kullanıcıya balon olarak gitmez; bilinmeyen araç etiketi silinir ve çalıştırılmaz.
    Etiket dışındaki boşluk, girinti ve sekmeler aynen korunur. Saf.
    """
    goal: Optional[str] = None
    for match in _TEXTUAL_CALL.finditer(line):
        attribute = _GOAL_ATTRIBUTE.search(match.group("attrs") or "")
        if goal is None and match.group("name") == "start_task" and attribute is not None:
            found: str = (attribute.group("double") or attribute.group("single") or "").strip()
            goal = found or None
    visible: str = _TEXTUAL_CALL.sub("", line)
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
    turn, _answered_by = await _chat_completion(
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
    session_id: str, *, presentation: Optional[Callable[[str], str]] = None,
) -> ChatResult:
    """
    Tek model turu. Stream yalnız geçici taslaktır; kullanıcıya semantik cevap ancak model turu başarıyla
    tamamlandıktan sonra, final ModelTurn.content üzerinden tek kez gönderilir. Metinsel tool fallback'i yalnız
    authenticated user turn'lerde (tools açıkken) yorumlanır.
    """
    def emit(_event: AgentEvent) -> None:
        # Partial text, reset ve retry akışı kullanıcıya gönderilmez. Final otorite ModelTurn.content'tir.
        return

    turn, _answered_by = await _chat_completion(
        clients, [{"role": "system", "content": system}, *(messages if tools else without_tool_calls(messages))],
        tools, session_id, backend, emit, should_stop,
    )

    if turn["finish_reason"] == "stopped" or should_stop():
        return {"bubbles": [], "start_task": None}
    if turn["finish_reason"] in {"length", "content_filter"}:
        _complete, reason = final_verdict(turn["content"], turn["finish_reason"])
        raise ChatError(f"Sohbet yanıtı tamamlanmadı ({turn['finish_reason']}): {reason}.")

    textual_goals: List[str] = []
    textual_recalls: List[str] = []
    textual_forgets: List[int] = []
    content = final_message(turn["content"])

    if tools and content:
        visible_lines: List[str] = []
        for line in content.split("\n"):
            query, fact_ids = textual_memory_calls(line)
            visible, goal = textual_start_task(line)
            if goal is not None:
                textual_goals.append(goal)
            if query is not None:
                textual_recalls.append(query)
            for fact_id in fact_ids:
                if fact_id not in textual_forgets:
                    textual_forgets.append(fact_id)
            visible_lines.append(visible)
        content = final_message("\n".join(visible_lines))

    sent: List[str] = []
    # Adapt complete native display before splitting; canonical model content stays private.
    display = presentation(content) if presentation is not None else content
    for chunk in final_chunks(display, normalize=presentation is None):
        if should_stop():
            return {"bubbles": sent, "start_task": None}
        await send_bubble(chunk)
        sent.append(chunk)

    if should_stop():
        return {"bubbles": sent, "start_task": None}
    if tools:
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
        controls = control_calls(turn["tool_calls"])
    else:
        start_task, recall, forget, controls = None, None, [], {}
    if not sent and start_task is None and recall is None and not forget and not controls and turn["finish_reason"] != "stopped":
        raise ChatError(f"Sohbet modeli boş yanıt döndürdü (finish_reason={turn['finish_reason']}).")
    result: ChatResult = {"bubbles": sent, "start_task": start_task}
    if recall is not None:
        result["recall"] = recall
    if forget:
        result["forget"] = forget
    if "mute" in controls:
        result["mute"] = float(controls["mute"])
    if "proactive" in controls:
        result["proactive"] = bool(controls["proactive"])
    return result
