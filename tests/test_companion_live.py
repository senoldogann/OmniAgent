"""Canlı sohbet katmanı duman testleri: gerçek model çağrısı. Yalnız OMNI_LIVE_COMPANION=1 ve
OMNI_LIVE_CHAT_BACKEND=<profil> verildiğinde çalışır (CI'da atlanır; anahtarlar Keychain'den okunur)."""
import os
from datetime import datetime
from typing import List, Tuple

import pytest

from omniagent.app.agent import close_model_clients, create_model_clients
from omniagent.companion import chat, persona
from omniagent.config import apply_stored_api_keys

pytestmark = pytest.mark.skipif(
    os.environ.get("OMNI_LIVE_COMPANION") != "1",
    reason="canlı model testi: OMNI_LIVE_COMPANION=1 ve OMNI_LIVE_CHAT_BACKEND gerekir",
)


async def live_turn(text: str) -> Tuple[chat.ChatResult, List[str]]:
    apply_stored_api_keys()
    backend: str = os.environ["OMNI_LIVE_CHAT_BACKEND"]
    clients = create_model_clients()
    sent: List[str] = []

    async def send(bubble: str) -> None:
        sent.append(bubble)

    try:
        system = persona.system_prompt(persona.persona_text_for("Deniz"), "")
        situation = persona.situation_block(datetime.now().astimezone(), None, [], None)
        result = await chat.respond(clients, backend, system,
                                    [{"role": "user", "content": chat.burst_turn([text], [], situation)}],
                                    chat.CHAT_TOOLS, send, lambda: False, "live-test")
    finally:
        await close_model_clients(clients)
    return result, sent


@pytest.mark.asyncio
async def test_greeting_is_short_casual_and_not_a_task() -> None:
    result, sent = await live_turn("selaam naber")
    assert 1 <= len(sent) <= 4 and result["start_task"] is None
    assert not any(marker in bubble for bubble in sent for marker in ("**", "# ", "- "))


@pytest.mark.asyncio
async def test_computer_request_is_delegated_with_a_self_contained_goal() -> None:
    result, sent = await live_turn("masaüstümdeki dosyaları listeler misin")
    assert result["start_task"] is not None and len(result["start_task"]) > 10
    assert len(sent) <= 2
