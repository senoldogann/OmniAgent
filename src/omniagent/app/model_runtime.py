"""Model istemcileri, provider metadata'sı ve streaming tamamlamaları."""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple, TypeVar
from urllib.request import urlopen

from openai import AsyncOpenAI, AsyncStream, Timeout
from openai.types.chat import ChatCompletionChunk

from omniagent.app.types import ModelTurn, ToolCallDraft
from omniagent.config import API_KEY_VARIABLES, BACKENDS, BackendProfile
from omniagent.core.events import EventSink, TokenUsage, preview_arguments


MODEL_REQUEST_TIMEOUT_SECONDS: float = 30.0
MODEL_CONNECT_TIMEOUT_SECONDS: float = 5.0
# İlk yanıt (HTTP başlığı) ve her akış parçası beklenirken durdurma isteğinin denetlenme aralığı (sn); runtime.wait ile
# aynı 50 ms.
STOP_POLL_SECONDS: float = 0.05
_Awaited = TypeVar("_Awaited")
ZERO_USAGE: TokenUsage = {
    "prompt_tokens": 0,
    "cached_tokens": 0,
    "completion_tokens": 0,
}
# Eksik anahtar uyarısı profil başına yalnız bir kez verilir. Kayıt kilitlidir ve dışarıdan
# enjekte edilebilir (bkz. create_model_clients), böylece testler arasında durum sızmaz.
_MISSING_KEY_WARNED: Set[str] = set()
_MISSING_KEY_LOCK: threading.Lock = threading.Lock()


def reset_missing_key_warnings() -> None:
    """Süreç düzeyindeki eksik-anahtar uyarı kaydını temizler (test izolasyonu için)."""
    with _MISSING_KEY_LOCK:
        _MISSING_KEY_WARNED.clear()


def merge_tool_call_delta(
    drafts: List[ToolCallDraft],
    index: int,
    call_id: Optional[str],
    name: Optional[str],
    arguments: Optional[str],
) -> List[ToolCallDraft]:
    """
    Streaming tool-call parçasını kararlı bir taslağa birleştirir. Ollama Cloud bazı
    modellerde ayrı tam çağrıları aynı index=0 ile akıtır; farklı id yeni çağrıdır.
    Araya hiç gelmemiş index atlarsa boş adlı "hayalet" taslak üretilmez.
    """
    if call_id:
        matching = next((slot for slot, draft in enumerate(drafts) if draft["id"] == call_id), None)
        if matching is not None:
            index = matching
        elif 0 <= index < len(drafts) and drafts[index]["id"]:
            return list(drafts) + [
                {"id": call_id, "name": name or "", "arguments": arguments or ""}
            ]
    if 0 <= index < len(drafts):
        current = drafts[index]
        updated: ToolCallDraft = {
            "id": call_id or current["id"],
            "name": current["name"] + (name or ""),
            "arguments": current["arguments"] + (arguments or ""),
        }
        return drafts[:index] + [updated] + drafts[index + 1:]
    return list(drafts) + [
        {"id": call_id or "", "name": name or "", "arguments": arguments or ""}
    ]


def ollama_cloud_ready(
    *,
    backends: Dict[str, BackendProfile] = BACKENDS,
    opener: Callable[..., Any] = urlopen,
) -> bool:
    """Yerel Ollama sunucusunda seçilen bulut modelinin kurulu olduğunu hızlıca doğrular."""
    try:
        with opener("http://127.0.0.1:11434/api/tags", timeout=0.3) as response:
            payload: Any = json.load(response)
    except (OSError, ValueError):
        return False
    models: Any = payload.get("models", []) if isinstance(payload, dict) else []
    return isinstance(models, list) and any(
        isinstance(model, dict)
        and model.get("name") == backends["ollama-cloud"]["model"]
        for model in models
    )


def create_model_clients(
    *,
    backends: Dict[str, BackendProfile] = BACKENDS,
    api_key_variables: Dict[str, str] = API_KEY_VARIABLES,
    ollama_ready: Callable[[], bool] = ollama_cloud_ready,
    warned: Optional[Set[str]] = None,
) -> Dict[str, AsyncOpenAI]:
    """
    Anahtarı mevcut API profilleri için sıcak istemcileri oluşturur. `warned` uyarılmış
    profilleri tutar; verilmezse süreç düzeyindeki kayıt kullanılır. Testler kendi set'ini
    geçerek uyarı durumunun testler arasında sızmasını önler.
    """
    timeout = Timeout(
        MODEL_REQUEST_TIMEOUT_SECONDS,
        connect=MODEL_CONNECT_TIMEOUT_SECONDS,
    )
    clients: Dict[str, AsyncOpenAI] = {
        name: AsyncOpenAI(
            api_key=profile["api_key"],
            base_url=profile["base_url"],
            timeout=timeout,
            max_retries=0,
        )
        for name, profile in backends.items()
        if profile["api_key"] and name != "ollama-cloud"
    }

    warned_state: Set[str] = _MISSING_KEY_WARNED if warned is None else warned
    with _MISSING_KEY_LOCK:
        warned_state.difference_update(clients)
        for name in backends:
            if name in clients or name == "ollama-cloud" or name in warned_state:
                continue
            warned_state.add(name)
            logging.warning(
                "Model profili kullanılamıyor: API anahtarı tanımlı değil",
                extra={"backend": name, "variable": api_key_variables.get(name, "")},
            )

    if ollama_ready():
        profile = backends["ollama-cloud"]
        clients["ollama-cloud"] = AsyncOpenAI(
            api_key="ollama",
            base_url=profile["base_url"],
            timeout=timeout,
            max_retries=0,
        )
    return clients


async def close_model_clients(
    clients: Optional[Dict[str, AsyncOpenAI]],
) -> None:
    """API bağlantı havuzlarını kapatır."""
    if clients:
        await asyncio.gather(*(client.close() for client in clients.values()))


def model_request_overrides(
    profile: BackendProfile,
    session_id: str,
) -> Tuple[Dict[str, str], Dict[str, Any]]:
    """Provider'a özgü request metadata'sını shared profile'ı değiştirmeden kurar."""
    headers = dict(profile["extra_headers"])
    body = dict(profile["extra_body"])
    if profile["session_header"] is not None:
        headers[profile["session_header"]] = session_id
    if "openrouter.ai" in profile["base_url"].casefold():
        body["session_id"] = session_id
    return headers, body


def token_usage(usage: Any) -> TokenUsage:
    """SDK usage nesnesini kararlı uygulama metriğine dönüştürür."""
    details = getattr(usage, "prompt_tokens_details", None)
    return {
        "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
        "cached_tokens": int(getattr(details, "cached_tokens", 0) or 0),
        "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
    }


async def _await_unless_stopped(operation: Awaitable[_Awaited], should_stop: Callable[[], bool]) -> Optional[_Awaited]:
    """
    Model akışının bekleyen adımını (istek açma ya da sonraki akış parçası) beklerken should_stop'u STOP_POLL_SECONDS'ta
    bir denetler: sunucu başlığı ya da bir sonraki parçayı geç gönderirse ya da akış duraksarsa Durdur, istemci zaman
    aşımı (30 sn) dolana dek etkisiz kalırdı. Durdurulunca iş iptal edilir (bağlantı kapanana dek beklenir) ve None
    döner; çağıran 'stopped' turu üretir. İşin kendi hatası (akış bittiyse StopAsyncIteration dahil) ve dışarıdan gelen
    iptal olduğu gibi yükselir.
    """
    pending: asyncio.Future[_Awaited] = asyncio.ensure_future(operation)
    try:
        while not pending.done():
            if should_stop():
                return None
            await asyncio.wait({pending}, timeout=STOP_POLL_SECONDS)
        return pending.result()
    finally:
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


async def _open_stream_unless_stopped(
    client: AsyncOpenAI, request: Dict[str, Any], should_stop: Callable[[], bool],
) -> Optional[AsyncStream[ChatCompletionChunk]]:
    """Akış isteğini açar; ilk yanıt (HTTP başlığı) beklenirken Durdur yoklanır (bkz. _await_unless_stopped). Durdurulursa None."""
    return await _await_unless_stopped(client.chat.completions.create(**request), should_stop)


async def stream_completion(
    client: AsyncOpenAI,
    profile: BackendProfile,
    messages: List[Dict[str, Any]],
    tool_schemas: List[Dict[str, Any]],
    session_id: str,
    emit: EventSink,
    should_stop: Callable[[], bool],
) -> ModelTurn:
    """Bir model tamamlamasını stream eder ve tool-call parçalarını birleştirir."""
    if client is None:
        raise RuntimeError(
            f"'{profile['provider']}' profili için API istemcisi kurulmadı."
        )

    headers, extra_body = model_request_overrides(profile, session_id)
    request: Dict[str, Any] = {
        "model": profile["model"],
        "messages": [({**entry, "content": ""} if entry.get("role") == "assistant"
                       and entry.get("tool_calls") and entry.get("content") is None else entry)
                      for entry in messages] if profile["provider"] == "ollama-cloud" else messages,
        (
            "max_completion_tokens"
            if profile["provider"] == "openai"
            else "max_tokens"
        ): profile["max_tokens"],
        "extra_headers": headers,
        "extra_body": extra_body,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    # Araçsız turda (iMessage iş raporu) ne boş araç listesi ne tool_choice gönderilir: ikisi de sağlayıcıda istek hatası.
    if tool_schemas:
        request["tools"] = tool_schemas
        if profile["provider"] != "ollama-cloud":
            request["tool_choice"] = "auto"

    stream: Optional[AsyncStream[ChatCompletionChunk]] = await _open_stream_unless_stopped(client, request, should_stop)
    if stream is None:
        # Kullanıcı ilk yanıt beklenirken durdurdu: agent._stopped_turn ile aynı biçim
        return {"content": "", "tool_calls": [], "finish_reason": "stopped", "usage": ZERO_USAGE}
    content_parts: List[str] = []
    drafts: List[ToolCallDraft] = []
    previews: Dict[int, str] = {}
    # Sağlayıcı index=0 değerini birden çok farklı çağrı için yeniden kullanabiliyor.
    stream_slots: Dict[int, int] = {}
    finish_reason: Optional[str] = None
    usage: TokenUsage = ZERO_USAGE
    try:
        while True:
            try:
                chunk: Optional[ChatCompletionChunk] = await _await_unless_stopped(stream.__anext__(), should_stop)
            except StopAsyncIteration:
                break
            if chunk is None or should_stop():
                finish_reason = "stopped"
                break
            if chunk.usage is not None:
                usage = token_usage(chunk.usage)
            if not chunk.choices:
                continue

            choice: Any = chunk.choices[0]
            delta: Any = choice.delta
            if delta.content:
                content_parts.append(delta.content)
                emit({"kind": "text_delta", "text": delta.content})

            reasoning: object = (delta.model_extra or {}).get("reasoning_content")
            if isinstance(reasoning, str) and reasoning:
                emit({"kind": "reasoning_delta", "text": reasoning})

            for call_delta in delta.tool_calls or []:
                function: Any = call_delta.function
                provider_index: int = call_delta.index
                slot: int = (
                    provider_index if call_delta.id
                    else stream_slots.get(provider_index, provider_index)
                )
                drafts = merge_tool_call_delta(
                    drafts,
                    slot,
                    call_delta.id,
                    function.name if function is not None else None,
                    function.arguments if function is not None else None,
                )
                actual_slot: int = (
                    next(i for i, draft in enumerate(drafts) if draft["id"] == call_delta.id)
                    if call_delta.id else min(slot, len(drafts) - 1)
                )
                stream_slots[provider_index] = actual_slot
                draft = drafts[actual_slot]
                # A partial value can expose a configured token prefix before it matches.
                # Keep the tool name visible; wait for complete JSON before previewing values.
                try:
                    json.loads(draft["arguments"])
                except (ValueError, RecursionError):
                    preview = ""
                else:
                    preview = preview_arguments(draft["name"], draft["arguments"])
                if previews.get(actual_slot) != preview:
                    previews[actual_slot] = preview
                    emit(
                        {
                            "kind": "tool_call_preview",
                            "index": actual_slot,
                            "name": draft["name"],
                            "preview": preview,
                        }
                    )
            if choice.finish_reason:
                finish_reason = choice.finish_reason
    finally:
        await stream.close()

    return {
        "content": "".join(content_parts),
        "tool_calls": drafts,
        "finish_reason": finish_reason,
        "usage": usage,
    }
