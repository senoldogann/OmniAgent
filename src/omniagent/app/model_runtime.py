"""Model istemcileri, provider metadata'sı ve streaming tamamlamaları."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.request import urlopen

from openai import AsyncOpenAI, Timeout

from omniagent.app.types import ModelTurn, ToolCallDraft
from omniagent.config import API_KEY_VARIABLES, BACKENDS, BackendProfile
from omniagent.core.events import EventSink, TokenUsage, preview_arguments


MODEL_REQUEST_TIMEOUT_SECONDS: float = 30.0
MODEL_CONNECT_TIMEOUT_SECONDS: float = 5.0
ZERO_USAGE: TokenUsage = {
    "prompt_tokens": 0,
    "cached_tokens": 0,
    "completion_tokens": 0,
}
_MISSING_KEY_WARNED: set[str] = set()


def merge_tool_call_delta(
    drafts: List[ToolCallDraft],
    index: int,
    call_id: Optional[str],
    name: Optional[str],
    arguments: Optional[str],
) -> List[ToolCallDraft]:
    """Streaming tool-call parçasını kararlı bir taslağa birleştirir."""
    padded: List[ToolCallDraft] = drafts + [
        {"id": "", "name": "", "arguments": ""}
        for _ in range(index + 1 - len(drafts))
    ]
    current = padded[index]
    updated: ToolCallDraft = {
        "id": call_id or current["id"],
        "name": current["name"] + (name or ""),
        "arguments": current["arguments"] + (arguments or ""),
    }
    return padded[:index] + [updated] + padded[index + 1:]


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
) -> Dict[str, AsyncOpenAI]:
    """Anahtarı mevcut API profilleri için sıcak istemcileri oluşturur."""
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

    _MISSING_KEY_WARNED.difference_update(clients)
    for name in backends:
        if name in clients or name == "ollama-cloud" or name in _MISSING_KEY_WARNED:
            continue
        _MISSING_KEY_WARNED.add(name)
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
        "messages": messages,
        "tools": tool_schemas,
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
    if profile["provider"] != "ollama-cloud":
        request["tool_choice"] = "auto"

    stream: Any = await client.chat.completions.create(**request)
    content_parts: List[str] = []
    drafts: List[ToolCallDraft] = []
    previews: Dict[int, str] = {}
    finish_reason: Optional[str] = None
    usage: TokenUsage = ZERO_USAGE
    try:
        async for chunk in stream:
            if should_stop():
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
                drafts = merge_tool_call_delta(
                    drafts,
                    call_delta.index,
                    call_delta.id,
                    function.name if function is not None else None,
                    function.arguments if function is not None else None,
                )
                draft = drafts[call_delta.index]
                preview = preview_arguments(draft["name"], draft["arguments"])
                if previews.get(call_delta.index) != preview:
                    previews[call_delta.index] = preview
                    emit(
                        {
                            "kind": "tool_call_preview",
                            "index": call_delta.index,
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
