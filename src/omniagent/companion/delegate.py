"""Sohbet katmanından mevcut ajana iş devri: koşuyu yürütür, ilerlemeyi kısa satırlara indirger.

Telegram köprüsüyle aynı sözleşme (telegram.py `_execute`): istemciler her iş başında kurulur, koşu host_task_lock
altında çalışır ve `unattended` verilmez; böylece onaylar kullanıcıya iMessage'dan sorulur ve sürekli modun otomatik
onay yolu (approval.AUTO_APPROVE_IN_CONTINUOUS_MODE) devreye girmez.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, TypedDict

from openai import AsyncOpenAI

from omniagent.app.agent import STATE_FILE, close_model_clients, create_model_clients, run_agent_with_callback
from omniagent.app.model_retry import REMOTE_MODEL_RETRY_SECONDS
from omniagent.app.types import RunOptions, RunReport
from omniagent.config import apply_model_preferences
from omniagent.core.conversation import Exchange, trim_history
from omniagent.core.events import AgentEvent
from omniagent.integrations.capabilities import CapabilityService
from omniagent.integrations.runtime import AnswerSink, DeliverSink
from omniagent.memory.personal import utc_iso
from omniagent.platform.macos.host_lock import host_task_lock

PROGRESS_LIMIT: int = 5


class TaskOutcome(TypedDict):
    goal: str
    report: RunReport
    started_at: str
    finished_at: str
    tokens: int


def progress_line(event: AgentEvent) -> Optional[str]:
    """Ajan olayını sohbet katmanının okuyacağı kısa satıra çevirir; önemsiz olaylar None. Saf."""
    if event["kind"] == "tool_started":
        return f"{event['name']}: {event['preview'][:120]}"
    if event["kind"] == "tool_finished":
        return ("tamam: " if event["ok"] else "başarısız: ") + event["text"][:160].replace("\n", " ")
    if event["kind"] == "notice" and event["level"] != "info":
        return f"uyarı: {event['text'][:160]}"
    if event["kind"] == "integration_status" and event["stage"] == "waiting_user":
        return "kullanıcının cevabı bekleniyor"
    return None


def run_options(answer: AnswerSink, deliver: DeliverSink, history: List[Exchange], images: List[str],
                should_stop: Callable[[], bool], integrations: CapabilityService) -> RunOptions:
    """iMessage işinin koşu seçenekleri; `unattended` bilerek yoktur. Saf."""
    options: RunOptions = {
        "integrations": integrations,
        "requested_backend": None,
        "should_stop": should_stop,
        "state_file": STATE_FILE,
        "history": trim_history(history),
        "answer": answer,
        "deliver": deliver,
        # Kullanıcı Mac başında değil: kısa ağ kopmalarında iş düşmez (Telegram ile aynı bütçe).
        "model_retry_seconds": REMOTE_MODEL_RETRY_SECONDS,
    }
    if images:
        options["images"] = images
    return options


async def run_task(goal: str, options: RunOptions, on_progress: Callable[[str], None]) -> TaskOutcome:
    """
    İşi mevcut ajanla koşturur. İlerleme satırları olay döngüsüne taşınarak on_progress'e verilir (araçlar olayları
    işçi iş parçacıklarından da yayınlar). Başka bir OmniAgent işi bilgisayarı kullanıyorsa HostBusyError.
    """
    apply_model_preferences()
    loop: asyncio.AbstractEventLoop = asyncio.get_running_loop()
    started_at: str = utc_iso(datetime.now(timezone.utc))

    def emit(event: AgentEvent) -> None:
        line: Optional[str] = progress_line(event)
        if line is not None:
            loop.call_soon_threadsafe(on_progress, line)

    clients: Dict[str, AsyncOpenAI] = await asyncio.to_thread(create_model_clients)
    try:
        with host_task_lock():
            report: RunReport = await run_agent_with_callback(goal, emit, options, clients)
    finally:
        await close_model_clients(clients)
    metrics = report["metrics"]
    return {"goal": goal, "report": report, "started_at": started_at,
            "finished_at": utc_iso(datetime.now(timezone.utc)),
            "tokens": int(metrics["prompt_tokens"]) + int(metrics["completion_tokens"])}
