"""Fast Loop phase controller and semantic-progress tests."""
from fast_loop import (
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
    uncached: int = 0,
    tool_calls: int = 0,
    delivery_ready: bool = False,
) -> TurnSignal:
    return TurnSignal(
        signature=signature,
        semantic_progress=semantic_progress,
        unresolved_deliverables=unresolved,
        uncached_prompt_tokens=uncached,
        tool_calls=tool_calls,
        delivery_ready=delivery_ready,
    )


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


def test_pressure_threshold_enters_conserve_without_aborting() -> None:
    policy = FastLoopPolicy(
        stagnation_window=3,
        delivery_stagnation_limit=2,
        soft_uncached_prompt_tokens=100,
        soft_tool_calls=4,
    )
    decision = advance_fast_loop(
        FastLoopState(),
        _signal("fresh", semantic_progress=True, uncached=100, tool_calls=2),
        policy,
    )
    assert decision.state.phase == "conserve"
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
