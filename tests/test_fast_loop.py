"""Fast Loop phase controller and semantic-progress tests."""
import json
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any, Dict, List, Tuple

import pytest

from omniagent.app import agent as main
from omniagent.app.progress import novel_read_output_progress
from omniagent.dev import benchmark
from omniagent.core.fast_loop import (
    FastLoopPolicy,
    FastLoopState,
    TurnSignal,
    advance_fast_loop,
    classify_semantic_progress,
    normalize_progress_signature,
)


def _signal(
    signature: str,
    *,
    semantic_progress: bool = False,
    unresolved: int = 3,
    delivery_ready: bool = False,
    visual_turn: bool = False,
) -> TurnSignal:
    return TurnSignal(
        signature=signature,
        semantic_progress=semantic_progress,
        unresolved_deliverables=unresolved,
        delivery_ready=delivery_ready,
        visual_turn=visual_turn,
    )


def test_default_policy_replans_nonvisual_after_two_stagnant_turns() -> None:
    state = FastLoopState()
    first = advance_fast_loop(state, _signal("same"))
    second = advance_fast_loop(first.state, _signal("same"))
    assert first.state.phase == "fast"
    assert second.state.phase == "conserve"
    assert second.request_replan is True


def test_visual_turn_keeps_three_turn_stagnation_tolerance() -> None:
    state = FastLoopState()
    first = advance_fast_loop(state, _signal("same", visual_turn=True))
    second = advance_fast_loop(first.state, _signal("same", visual_turn=True))
    third = advance_fast_loop(second.state, _signal("same", visual_turn=True))
    assert second.state.phase == "fast"
    assert third.state.phase == "conserve"
    assert third.request_replan is True


def test_stagnation_enters_conserve_then_requests_replan() -> None:
    policy = FastLoopPolicy(stagnation_window=2, delivery_stagnation_limit=2)
    state = FastLoopState()
    first = advance_fast_loop(state, _signal("same"), policy)
    second = advance_fast_loop(first.state, _signal("same"), policy)

    assert first.state.phase == "fast"
    assert second.state.phase == "conserve"
    assert second.request_replan is True
    assert second.state.replans == 1


def test_meaningful_progress_resets_stagnation_window() -> None:
    policy = FastLoopPolicy(stagnation_window=2, delivery_stagnation_limit=2)
    state = advance_fast_loop(FastLoopState(), _signal("same"), policy).state
    progressed = advance_fast_loop(
        state, _signal("new", semantic_progress=True, unresolved=2), policy,
    )
    after = advance_fast_loop(progressed.state, _signal("new", unresolved=2), policy)

    assert progressed.state.stagnant_turns == 0
    assert progressed.state.semantic_progress_events == 1
    assert after.state.phase == "fast"
    assert after.state.stagnant_turns == 1


def test_second_stagnation_after_replan_enters_delivery() -> None:
    policy = FastLoopPolicy(stagnation_window=2, delivery_stagnation_limit=2)
    state = FastLoopState()
    for _ in range(2):
        decision = advance_fast_loop(state, _signal("same"), policy)
        state = decision.state
    assert state.phase == "conserve"

    decision = advance_fast_loop(state, _signal("same"), policy)
    state = decision.state
    decision = advance_fast_loop(state, _signal("same"), policy)

    assert decision.state.phase == "delivery"
    assert decision.entered_delivery is True
    assert decision.request_replan is False


def test_delivery_stagnation_eventually_requests_bounded_stop() -> None:
    policy = FastLoopPolicy(stagnation_window=1, delivery_stagnation_limit=2)
    state = FastLoopState(phase="delivery")
    one = advance_fast_loop(state, _signal("same"), policy)
    two = advance_fast_loop(one.state, _signal("same"), policy)

    assert one.stop_reason is None
    assert two.stop_reason is not None
    assert "ilerleme" in two.stop_reason


def test_meaningful_progress_stays_in_fast_phase() -> None:
    policy = FastLoopPolicy(stagnation_window=3, delivery_stagnation_limit=2)
    decision = advance_fast_loop(
        FastLoopState(),
        _signal("fresh", semantic_progress=True),
        policy,
    )
    assert decision.state.phase == "fast"
    assert decision.stop_reason is None


def test_delivery_ready_skips_conserve() -> None:
    policy = FastLoopPolicy(stagnation_window=3, delivery_stagnation_limit=2)
    decision = advance_fast_loop(
        FastLoopState(),
        _signal("done-discovery", semantic_progress=True, unresolved=2, delivery_ready=True),
        policy,
    )
    assert decision.state.phase == "delivery"
    assert decision.entered_delivery is True


def test_equivalent_signatures_are_stable() -> None:
    first = normalize_progress_signature(
        tool_facts=[("chrome_active_tab", {"url": "https://example.com"})],
        result_facts=["ok"],
        observation_digest="abc",
        ledger_digest="ledger",
        unresolved_deliverables=3,
    )
    second = normalize_progress_signature(
        tool_facts=[("chrome_active_tab", {"url": "https://example.com"})],
        result_facts=["ok"],
        observation_digest="abc",
        ledger_digest="ledger",
        unresolved_deliverables=3,
    )
    changed = normalize_progress_signature(
        tool_facts=[("chrome_active_tab", {"url": "https://example.com/2"})],
        result_facts=["ok"],
        observation_digest="def",
        ledger_digest="ledger2",
        unresolved_deliverables=2,
    )
    assert first == second
    assert changed != first


def test_signature_normalization_ignores_dict_key_order() -> None:
    a = normalize_progress_signature(
        tool_facts=[("tool", {"b": 2, "a": 1})],
        result_facts=["ok"],
        observation_digest=None,
        ledger_digest="same",
        unresolved_deliverables=1,
    )
    b = normalize_progress_signature(
        tool_facts=[("tool", {"a": 1, "b": 2})],
        result_facts=["ok"],
        observation_digest=None,
        ledger_digest="same",
        unresolved_deliverables=1,
    )
    assert a == b


def test_changing_tool_signatures_without_ledger_do_not_fake_progress() -> None:
    """Farklı URL/araç gezmek, STATE ilerlemiyorsa runaway döngüyü sıfırlamamalı."""
    assert classify_semantic_progress(
        ledger_changed=False,
        has_ledger=False,
        previous_signature=None,
        signature="first",
        all_failed=False,
    ) is True
    assert classify_semantic_progress(
        ledger_changed=False,
        has_ledger=False,
        previous_signature="first",
        signature="different-url",
        all_failed=False,
    ) is False
    assert classify_semantic_progress(
        ledger_changed=True,
        has_ledger=True,
        previous_signature="first",
        signature="same-tool",
        all_failed=False,
    ) is True


def test_deterministic_delivery_progress_counts_without_ledger_delta() -> None:
    assert classify_semantic_progress(
        ledger_changed=False,
        deterministic_progress=True,
        has_ledger=True,
        previous_signature="before",
        signature="after",
        all_failed=False,
    ) is True


def test_sequence_read_progress_uses_new_content_including_partial_result() -> None:
    """Çok adımlı okuma, ekran aynı yere dönse bile yeni veri geldiyse ilerlemedir."""
    call = {
        "id": "r1", "name": "run_action_sequence",
        "arguments": json.dumps({"steps": [
            {"action": "click_text", "text": "İlan A"},
            {"action": "read_scrollable", "point": [600, 500]},
            {"action": "click_text", "text": "İlan B"},
        ]}),
    }
    partial = {"tool_call_id": "r1", "ok": False, "completed_steps": 2,
               "error": "Tamamlanan adımlar: İlan A; Aylık maaş: 5200 €"}
    seen, progressed = novel_read_output_progress([call], [partial], frozenset())
    assert progressed
    assert seen
    same_seen, repeated = novel_read_output_progress([call], [partial], seen)
    assert same_seen == seen
    assert not repeated

    click_only = {**partial, "completed_steps": 1}
    assert not novel_read_output_progress([call], [click_only], frozenset())[1]


@pytest.mark.asyncio
async def test_repeated_identical_poll_stops_at_the_fast_loop_bound(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """
    Aynı 'pending' yanıtını yoklayan görev Fast Loop tarafından birkaç turda sınırlı durdurulur.
    Görev defteri olguları tur damgası taşır: damga karşılaştırmaya girerse her tur 'yeni olgu' sayılır,
    ilerleme hiç durmaz ve görev üst sınıra (100 tur) kadar sürerdi. Gerçek fetch_raw (curl) yerel
    benchmark sunucusuna gider, model betiklidir.
    """
    server = ThreadingHTTPServer(("127.0.0.1", 0), benchmark.BenchmarkHandler)
    Thread(target=server.serve_forever, daemon=True).start()
    url: str = f"http://127.0.0.1:{server.server_port}/stagnation/fastloop"
    polls: List[int] = []

    async def scripted_model(
        clients: Any, messages: Any, schemas: Any, session_id: str, backend: str,
        emit: Any, should_stop: Any,
    ) -> Tuple[Dict[str, Any], str]:
        polls.append(len(polls) + 1)
        call: Dict[str, str] = {"id": f"poll-{len(polls)}", "name": "fetch_raw", "arguments": json.dumps({"url": url})}
        return {"content": "STATE: bekleniyor", "tool_calls": [call], "finish_reason": "tool_calls",
                "usage": main.ZERO_USAGE}, backend

    monkeypatch.setattr(main, "_call_model_with_retries", scripted_model)
    try:
        report = await main.run_agent_with_callback(
            f"Yalnız fetch_raw kullan. {url} adresindeki status 'ready' olana kadar aynı endpoint'i kontrol etmeye "
            "devam et. status='pending' iken görevi başarılı bitirme.",
            [].append,
            {"requested_backend": "ollama-cloud", "should_stop": lambda: False,
             "state_file": str(tmp_path / "state.json"), "history": [], "run_mode": "autonomous"},
            {"ollama-cloud": object()},
        )
    finally:
        server.shutdown()
        server.server_close()

    assert report["success"] is False
    assert "ilerleme" in report["reason"]
    assert report["metrics"]["turns"] <= 15
