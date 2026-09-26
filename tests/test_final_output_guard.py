"""Kanıtsız final iddiası kullanıcıya akmadan host kararıyla değiştirilir."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, List, Tuple

import pytest

from omniagent.app import agent as main
from omniagent.core.events import AgentEvent
from omniagent.integrations.capabilities import CapabilityService


ScriptTurn = Tuple[str, List[dict[str, str]], List[AgentEvent]]


async def _run_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, goal: str,
    script: List[ScriptTurn],
) -> Tuple[main.RunReport, List[AgentEvent]]:
    """Modelin gerçekten akıttığı parçaları ve host olaylarını ayrı ayrı gözler."""
    events: List[AgentEvent] = []
    index = 0

    async def fake_model(
        clients: Any, messages: Any, schemas: Any, session_id: str,
        backend: str, emit: Any, should_stop: Any,
    ) -> Tuple[main.ModelTurn, str]:
        nonlocal index
        content, calls, deltas = script[min(index, len(script) - 1)]
        index += 1
        for delta in deltas:
            emit(delta)
        return {
            "content": content, "tool_calls": calls,
            "finish_reason": "tool_calls" if calls else "stop", "usage": main.ZERO_USAGE,
        }, backend

    monkeypatch.setattr(main, "_call_model_with_retries", fake_model)
    service = CapabilityService(tmp_path)
    try:
        report = await main.run_agent_with_callback(
            goal, events.append,
            {"requested_backend": None, "should_stop": lambda: False,
             "state_file": str(tmp_path / "memory.json"), "history": [],
             "integrations": service, "max_iterations": 5},
            {"ollama-cloud": object()},
        )
    finally:
        await service.close()
    return report, events


def _visible_text(events: List[AgentEvent]) -> str:
    """Tüm kullanıcı yüzeylerinin ortak `text_delta` akışı."""
    return "".join(event["text"] for event in events if event["kind"] == "text_delta")


def _final_text_precedes_model_finished(events: List[AgentEvent], expected: str) -> None:
    text_index = next(
        index for index, event in enumerate(events)
        if event["kind"] == "text_delta" and event["text"] == expected
    )
    assert events[text_index + 1]["kind"] == "model_finished"


@pytest.mark.asyncio
async def test_rejected_action_claim_never_reaches_user_surfaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    false_claim = "Dosya silindi."
    turn: ScriptTurn = (
        false_claim, [], [
            {"kind": "reasoning_delta", "text": false_claim},
            {"kind": "text_delta", "text": false_claim},
        ],
    )

    report, events = await _run_script(
        tmp_path, monkeypatch, "Masaüstündeki gereksiz dosyayı sil", [turn, turn],
    )

    assert not report["success"]
    assert false_claim not in _visible_text(events)
    assert not any(
        event["kind"] == "reasoning_delta" and false_claim in event["text"]
        for event in events
    )
    assert false_claim not in report["outcome"]
    assert report["outcome"].startswith("Doğrulanmadı:")
    assert false_claim not in report["exchange"]["answer"]
    assert [event["kind"] for event in events].count("model_finished") == 2
    _final_text_precedes_model_finished(events, report["outcome"])
    finished = next(event for event in events if event["kind"] == "run_finished")
    assert finished["outcome"] == report["outcome"]


@pytest.mark.asyncio
async def test_failed_history_omits_unverified_model_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("gerçek içerik", encoding="utf-8")
    read_call = {"id": "read-1", "name": "read_file",
                 "arguments": json.dumps({"path": str(target)})}
    false_claim = "Dosya silindi."
    claim_turn: ScriptTurn = (
        false_claim, [], [{"kind": "text_delta", "text": false_claim}],
    )
    script: List[ScriptTurn] = [
        (f"STATE:\nFACTS: {false_claim}", [read_call],
         [{"kind": "text_delta", "text": f"STATE:\nFACTS: {false_claim}"}]),
        claim_turn, claim_turn,
    ]

    report, events = await _run_script(tmp_path, monkeypatch, "Bu dosyayı sil", script)

    assert not report["success"]
    assert false_claim not in _visible_text(events)
    assert false_claim not in report["exchange"]["answer"]
    assert "Doğrulanmadı" in report["exchange"]["answer"]


@pytest.mark.asyncio
async def test_textual_tool_call_is_not_delivered_as_completed_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pseudo_call = 'call:write_file {"path":"/tmp/olmayan.txt","content":"bitti"}'
    turn: ScriptTurn = (pseudo_call, [], [{"kind": "text_delta", "text": pseudo_call}])

    report, events = await _run_script(
        tmp_path, monkeypatch, "Projede hesaplama kodunu değiştir", [turn, turn],
    )

    assert not report["success"]
    assert pseudo_call not in _visible_text(events)
    assert pseudo_call not in report["outcome"]
    assert "gerçek araç çağrısı" in report["reason"] or "yalnız metin" in report["reason"]


@pytest.mark.asyncio
async def test_rejected_source_claim_is_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    false_claim = "Kod değişikliğini yaptım."
    turn: ScriptTurn = (false_claim, [], [{"kind": "text_delta", "text": false_claim}])

    report, events = await _run_script(
        tmp_path, monkeypatch, "Projede hesaplama kodunu değiştir", [turn, turn],
    )

    assert not report["success"]
    assert false_claim not in _visible_text(events)
    assert false_claim not in report["outcome"]
    assert "write_file" in report["reason"] or "dosya" in report["reason"]


@pytest.mark.asyncio
async def test_unmet_wait_status_cannot_publish_ready_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    false_claim = "Status ready; görev tamamlandı."
    turn: ScriptTurn = (false_claim, [], [{"kind": "text_delta", "text": false_claim}])

    report, events = await _run_script(
        tmp_path, monkeypatch,
        "status 'ready' olana kadar aynı adresi kontrol et", [turn],
    )

    assert not report["success"]
    assert "beklenen status" in report["reason"]
    assert false_claim not in _visible_text(events)
    assert report["outcome"].startswith("Doğrulanmadı:")


@pytest.mark.asyncio
async def test_guarded_tool_turn_text_is_hidden_but_valid_final_is_delivered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "not.txt"
    call = {"id": "write-1", "name": "write_file",
            "arguments": json.dumps({"path": str(target), "content": "Merhaba\n"})}
    claim = "Dosyayı oluşturdum."
    script: List[ScriptTurn] = [
        ("STATE: Dosyayı yazdım", [call],
         [{"kind": "text_delta", "text": "STATE: Dosyayı yazdım"}]),
        (claim, [], [{"kind": "text_delta", "text": claim}]),
    ]

    report, events = await _run_script(
        tmp_path, monkeypatch, f"{target} dosyasını oluştur", script,
    )

    assert report["success"]
    assert target.read_text(encoding="utf-8") == "Merhaba\n"
    assert _visible_text(events) == claim
    _final_text_precedes_model_finished(events, claim)


@pytest.mark.asyncio
async def test_provider_reset_discards_previously_buffered_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "not.txt"
    call = {"id": "write-1", "name": "write_file",
            "arguments": json.dumps({"path": str(target), "content": "Merhaba\n"})}
    claim = "Dosyayı oluşturdum."
    script: List[ScriptTurn] = [
        ("STATE: yazılıyor", [call], [{"kind": "text_delta", "text": "STATE: yazılıyor"}]),
        (claim, [], [
            {"kind": "text_delta", "text": "ESKİ AKIŞ"},
            {"kind": "stream_reset", "reason": "sağlayıcı yeniden denendi"},
            {"kind": "text_delta", "text": claim},
        ]),
    ]

    report, events = await _run_script(
        tmp_path, monkeypatch, f"{target} dosyasını oluştur", script,
    )

    assert report["success"]
    assert "ESKİ AKIŞ" not in _visible_text(events)
    assert _visible_text(events) == claim
    assert any(event["kind"] == "stream_reset" for event in events)


@pytest.mark.asyncio
async def test_honest_failure_is_delivered_and_information_still_streams(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = "Yapamadım: erişim izni yok."
    failed: ScriptTurn = (failure, [], [{"kind": "text_delta", "text": failure}])
    report, events = await _run_script(
        tmp_path, monkeypatch, "Bu dosyayı sil", [failed],
    )
    assert not report["success"]
    assert report["outcome"] == failure
    assert _visible_text(events) == failure
    _final_text_precedes_model_finished(events, failure)

    answer = "4"
    simple: ScriptTurn = (answer, [], [{"kind": "text_delta", "text": answer}])
    report, events = await _run_script(
        tmp_path, monkeypatch, "İki artı iki kaçtır?", [simple],
    )
    assert report["success"]
    assert _visible_text(events) == answer
    _final_text_precedes_model_finished(events, answer)
