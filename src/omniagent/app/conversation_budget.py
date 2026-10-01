"""Host accounting for conversation model attempts, waits and actual tool calls."""
from __future__ import annotations

import asyncio
import json
import math
import time
from typing import Awaitable, Callable, TypeVar

from omniagent.core.events import TokenUsage
from omniagent.integrations.runtime import IntegrationStopped

T = TypeVar("T")
QUICK_TURNS = 7
QUICK_TOOLS = 6
OPERATION_SECONDS = 30.0
QUICK_SECONDS = 120.0


class ConversationExhausted(RuntimeError):
    pass


class ConversationBudget:
    def __init__(self, should_stop: Callable[[], bool], iterations: int, seconds: float,
                 tokens: int | None = None) -> None:
        if iterations < 1 or seconds <= 0 or math.isnan(seconds) or (tokens is not None and tokens < 1):
            raise ValueError("Konuşma bütçesi pozitif olmalı")
        self.started = time.monotonic()
        self.user_turns, self.user_seconds, self.token_limit = iterations, seconds, tokens
        self.max_turns, self.seconds = min(QUICK_TURNS, iterations), min(QUICK_SECONDS, seconds)
        self.should_stop = should_stop
        self.turns = self.tools = 0
        self.usage: TokenUsage = {"prompt_tokens": 0, "cached_tokens": 0, "completion_tokens": 0}
        self.model_seconds = self.tool_seconds = 0.0
        self.phase = "route"
        self.phase_limits = {"route": 1, "generate": 3, "verify": 3}
        self.phase_turns: dict[str, int] = {}
        self.backend = ""
        self.reserved_tokens = 0
        self.current_reservation = 0

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.seconds - (time.monotonic() - self.started))

    @property
    def tokens(self) -> int:
        return self.usage["prompt_tokens"] + self.usage["completion_tokens"]

    def check(self, *, model: bool = False) -> None:
        if self.should_stop():
            raise IntegrationStopped("Kullanıcı tarafından durduruldu.")
        if self.remaining_seconds <= 0:
            raise ConversationExhausted("Süre sınırına ulaşıldı.")
        if self.token_limit is not None and self.tokens >= self.token_limit:
            raise ConversationExhausted("Token sınırına ulaşıldı.")
        if model and (self.turns >= self.max_turns or
                      self.phase_turns.get(self.phase, 0) >= self.phase_limits.get(self.phase, self.max_turns)):
            raise ConversationExhausted("Model turu sınırına ulaşıldı.")

    def model_attempt(self, backend: str) -> None:
        self.check(model=True)
        self.turns += 1
        self.phase_turns[self.phase] = self.phase_turns.get(self.phase, 0) + 1
        self.backend = backend

    def completion_limit(self, messages, schemas, requested: int) -> int:
        if self.token_limit is None:
            return requested
        # Bytes are a conservative upper bound for text-tokenized request bodies;
        # reserve wrapper overhead as well. Unknown failed usage remains reserved,
        # rather than pretending a retry was free. Report metrics retain measured
        # usage only; this reservation controls admission, not billing claims.
        prompt_bound = len(json.dumps([messages, schemas], ensure_ascii=False).encode("utf-8")) + 512
        available = self.token_limit - self.tokens - self.reserved_tokens - prompt_bound
        if available <= 0:
            raise ConversationExhausted("İstek mevcut token bütçesine sığmadı.")
        limit = min(requested, available)
        self.current_reservation = prompt_bound + limit
        self.reserved_tokens += self.current_reservation
        return limit

    def model_usage(self, usage: TokenUsage) -> None:
        self.reserved_tokens -= self.current_reservation
        self.current_reservation = 0
        for key in self.usage:
            self.usage[key] += usage[key]

    def use_full_task_limits(self) -> None:
        # Keep the original selected limits, less time/turns/tokens already spent.
        self.max_turns, self.seconds = self.user_turns, self.user_seconds
        self.phase = "task"

    def operation_deadline(self) -> float:
        return time.monotonic() + min(OPERATION_SECONDS, self.remaining_seconds)

    async def wait(self, operation: Awaitable[T], *, model: bool = False, tool: bool = False,
                   bounded_operation: bool = True, deadline: float | None = None) -> T:
        task = asyncio.ensure_future(operation)
        started = time.monotonic()
        timeout = min(OPERATION_SECONDS, self.remaining_seconds) if bounded_operation else self.remaining_seconds
        if deadline is not None:
            timeout = min(timeout, max(0.0, deadline - started))
        try:
            self.check(model=model)
            while not task.done():
                self.check()
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    raise ConversationExhausted("İşlem zaman sınırına ulaştı.")
                await asyncio.wait({task}, timeout=min(.05, remaining))
            self.check()
            if time.monotonic() - started >= timeout:
                raise ConversationExhausted("İşlem zaman sınırına ulaştı.")
            return task.result()
        finally:
            try:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            finally:
                # Actual owned work includes cancellation cleanup, even when a
                # second cancellation interrupts the parent's wait.
                elapsed = time.monotonic() - started
                if model:
                    self.model_seconds += elapsed
                if tool:
                    self.tool_seconds += elapsed

    def metrics(self):
        return {"turns": self.turns, "tool_calls": self.tools,
                "elapsed_seconds": round(time.monotonic() - self.started, 3), "backend": self.backend,
                **self.usage, "model_seconds": round(self.model_seconds, 3),
                "tool_seconds": round(self.tool_seconds, 3)}
