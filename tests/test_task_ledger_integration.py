"""
TaskLedger ile main.py ajan döngüsü arasındaki uçtan uca deterministik entegrasyon testleri.
Modelin STATE yazmadığı durumlarda bile doğrulanmış gerçeklerin bağlamda korunduğunu kanıtlar.
"""
import json
from pathlib import Path
from typing import Any, Dict, List
import pytest

from omniagent.integrations.capabilities import CapabilityService
from omniagent.app import agent as main


@pytest.mark.asyncio
async def test_task_ledger_facts_injected_into_subsequent_turns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Model STATE: bloğu yazmasa bile, bir araç sonucunda bulunan gerçekler
    (ör. SATIR, GUN, MAAS) bir sonraki turda modelin promptuna scratchpad olarak enjekte edilir.
    """
    received_messages: List[List[Dict[str, Any]]] = []

    async def fake_model(
        clients: Any, messages: List[Dict[str, Any]], schemas: Any,
        session_id: str, backend: str, emit: Any, should_stop: Any
    ) -> Any:
        received_messages.append(list(messages))
        turn_index: int = len(received_messages)

        if turn_index == 1:
            # 1. Tur: Model STATE yazmaz, sadece aracı çağırır
            return {
                "content": "Komutu çalıştırıyorum.",
                "tool_calls": [{
                    "id": "call-1",
                    "name": "execute_shell",
                    "arguments": json.dumps({
                        "command": "printf 'GUN: Tuesday\\nSATIR: 2000'",
                        "use_sudo": False,
                        "timeout_seconds": None,
                    }),
                }],
                "finish_reason": "tool_calls",
                "usage": main.ZERO_USAGE,
            }, backend
        else:
            # 2. Tur: Model final cevabı verir
            return {
                "content": "Sonuç bulundu: Salı günü ve 2000 satır.",
                "tool_calls": [],
                "finish_reason": "stop",
                "usage": main.ZERO_USAGE,
            }, backend

    monkeypatch.setattr(main, "_call_model_with_retries", fake_model)
    service = CapabilityService(tmp_path)
    try:
        report = await main.run_agent_with_callback(
            "Günü ve satır sayısını bul",
            lambda event: None,
            {
                "requested_backend": None,
                "should_stop": lambda: False,
                "state_file": str(tmp_path / "memory.json"),
                "history": [],
                "integrations": service,
            },
            {"openai": object(), "openrouter": object()},
        )
    finally:
        await service.close()

    assert report["success"]
    assert len(received_messages) == 2

    # 1. Turda henüz araç çalışmadığı için scratchpad boştur
    turn1_msgs = received_messages[0]
    assert not any("### TASK SCRATCHPAD" in str(m.get("content")) for m in turn1_msgs)

    # 2. Turda ise araç çalışmış ve çıktıdan "gun: Tuesday" ile "satir: 2000" ayıklanmıştır
    turn2_msgs = received_messages[1]
    has_scratchpad: bool = any("### TASK SCRATCHPAD (Host-Verified Facts)" in str(m.get("content")) for m in turn2_msgs)
    assert has_scratchpad, "2. tur mesajlarında TASK SCRATCHPAD bulunamadı!"

    scratchpad_content = str([m.get("content") for m in turn2_msgs if "### TASK SCRATCHPAD" in str(m.get("content"))])
    assert "Tuesday" in scratchpad_content
    assert "2000" in scratchpad_content


@pytest.mark.asyncio
async def test_incomplete_task_preserves_host_ledger_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Görev zaman bütçesi veya kullanıcı durdurmasıyla yarım kalırsa,
    toplanan Host-Verified gerçekler sohbet geçmişi yanıtına (history_answer) aktarılır.
    """
    call_count: int = 0
    stop_requested: bool = False

    async def fake_model(
        clients: Any, messages: List[Dict[str, Any]], schemas: Any,
        session_id: str, backend: str, emit: Any, should_stop: Any
    ) -> Any:
        nonlocal call_count, stop_requested
        call_count += 1
        # 1. turun aracı çalıştıktan sonra 2. turda durdurulmasını sağla
        if call_count >= 2:
            stop_requested = True
        return {
            "content": "Veriyi çektim.",
            "tool_calls": [{
                "id": f"call-{call_count}",
                "name": "execute_shell",
                "arguments": json.dumps({
                    "command": "echo 'ilan kodu: IL-FINAL-99'",
                    "use_sudo": False,
                    "timeout_seconds": None,
                }),
            }],
            "finish_reason": "tool_calls",
            "usage": main.ZERO_USAGE,
        }, backend

    monkeypatch.setattr(main, "_call_model_with_retries", fake_model)
    service = CapabilityService(tmp_path)
    try:
        report = await main.run_agent_with_callback(
            "İlan kodunu oku",
            lambda event: None,
            {
                "requested_backend": None,
                "should_stop": lambda: stop_requested,
                "state_file": str(tmp_path / "memory.json"),
                "history": [],
                "integrations": service,
            },
            {"openai": object(), "openrouter": object()},
        )
    finally:
        await service.close()

    assert not report["success"]
    assert report["reason"] == "durduruldu"
    # Yarım kalan görevde toplanan gerçekler (IL-FINAL-99) exchange answer içinde yer almalı
    answer = report["exchange"]["answer"]
    assert "IL-FINAL-99" in answer
