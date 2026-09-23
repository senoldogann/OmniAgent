"""Pure performance-first controller for OmniAgent's model/tool loop."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import Any, Iterable, Literal, Mapping, Optional, Sequence, Tuple


ExecutionPhase = Literal["fast", "conserve", "delivery"]


@dataclass(frozen=True)
class FastLoopPolicy:
    """Thresholds are host policy, not prompt-only suggestions."""

    stagnation_window: int = 3
    delivery_stagnation_limit: int = 3
    soft_uncached_prompt_tokens: int = 60_000
    soft_tool_calls: int = 24


@dataclass(frozen=True)
class FastLoopState:
    phase: ExecutionPhase = "fast"
    stagnant_turns: int = 0
    delivery_stagnant_turns: int = 0
    replans: int = 0
    semantic_progress_events: int = 0
    transitions: int = 0
    last_signature: Optional[str] = None
    pressure_seen: bool = False


@dataclass(frozen=True)
class TurnSignal:
    """Bounded facts the orchestrator can derive without extra model work."""

    signature: str
    semantic_progress: bool
    unresolved_deliverables: int
    uncached_prompt_tokens: int
    tool_calls: int
    delivery_ready: bool = False


@dataclass(frozen=True)
class FastLoopDecision:
    state: FastLoopState
    request_replan: bool = False
    entered_delivery: bool = False
    stop_reason: Optional[str] = None
    notice: Optional[str] = None


def _canonical(value: Any) -> Any:
    """Keep signature inputs deterministic and bounded enough for every-turn use."""
    if isinstance(value, Mapping):
        return {str(key): _canonical(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value[:64]]
    if isinstance(value, str):
        return value[:1024]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:1024]


def normalize_progress_signature(
    *,
    tool_facts: Sequence[Tuple[str, Mapping[str, Any]]],
    result_facts: Sequence[str],
    observation_digest: Optional[str],
    ledger_digest: str,
    unresolved_deliverables: int,
) -> str:
    """Hash bounded semantic inputs; never retain raw screenshots in controller state."""
    payload = {
        "tools": [(name, _canonical(arguments)) for name, arguments in tool_facts[:32]],
        "results": [str(value)[:1024] for value in result_facts[:32]],
        "observation": observation_digest,
        "ledger": ledger_digest[:256],
        "unresolved": max(0, int(unresolved_deliverables)),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def classify_semantic_progress(
    *,
    ledger_changed: bool,
    has_ledger: bool,
    previous_signature: Optional[str],
    signature: str,
    all_failed: bool,
) -> bool:
    """Tool başarısını gerçek görev ilerlemesinden ayırır; ilk tur yalnız bootstrap istisnasıdır."""
    if ledger_changed:
        return True
    if all_failed:
        return False
    # Modelin STATE yazmadığı ilk araç turunun başlamasına izin ver; bundan sonra farklı URL,
    # tıklama veya sonuç imzaları tek başına ilerleme sayılmaz. Böylece model STATE'i atlayarak
    # farklı sayfalarda gezinip stagnation penceresini sürekli sıfırlayamaz.
    if not has_ledger and previous_signature is None:
        return bool(signature)
    return False


def advance_fast_loop(
    state: FastLoopState,
    signal: TurnSignal,
    policy: FastLoopPolicy = FastLoopPolicy(),
) -> FastLoopDecision:
    """Advance the execution phase from host-observed semantic progress."""
    progressed = bool(signal.semantic_progress)
    pressure = (
        signal.uncached_prompt_tokens >= policy.soft_uncached_prompt_tokens
        or signal.tool_calls >= policy.soft_tool_calls
    )

    stagnant = 0 if progressed else state.stagnant_turns + 1
    delivery_stagnant = (
        0 if progressed
        else state.delivery_stagnant_turns + 1 if state.phase == "delivery"
        else state.delivery_stagnant_turns
    )
    progress_events = state.semantic_progress_events + int(progressed)

    updated = replace(
        state,
        stagnant_turns=stagnant,
        delivery_stagnant_turns=delivery_stagnant,
        semantic_progress_events=progress_events,
        last_signature=signal.signature,
        pressure_seen=state.pressure_seen or pressure,
    )

    if state.phase == "delivery":
        if progressed:
            return FastLoopDecision(updated)
        if delivery_stagnant >= policy.delivery_stagnation_limit:
            return FastLoopDecision(
                updated,
                stop_reason=(
                    f"delivery aşamasında {delivery_stagnant} tur anlamlı ilerleme yok; "
                    "kalan zorunlu iş tamamlanamadı"
                ),
                notice="Teslim aşaması ilerlemiyor; bütçe tüketmek yerine görev kontrollü durduruldu.",
            )
        return FastLoopDecision(updated)

    if signal.delivery_ready:
        delivery = replace(
            updated,
            phase="delivery",
            stagnant_turns=0,
            delivery_stagnant_turns=0,
            transitions=updated.transitions + int(updated.phase != "delivery"),
        )
        return FastLoopDecision(
            delivery,
            entered_delivery=True,
            notice="Zorunlu iş teslim aşamasına hazır; opsiyonel keşif kapatılıyor.",
        )

    if state.phase == "fast" and pressure:
        conserve = replace(
            updated,
            phase="conserve",
            stagnant_turns=0,
            transitions=updated.transitions + 1,
        )
        return FastLoopDecision(
            conserve,
            notice="Çalışma bütçesi baskısı: opsiyonel keşif azaltılıyor, zorunlu iş korunuyor.",
        )

    if state.phase == "fast" and stagnant >= policy.stagnation_window:
        conserve = replace(
            updated,
            phase="conserve",
            stagnant_turns=0,
            replans=updated.replans + 1,
            transitions=updated.transitions + 1,
        )
        return FastLoopDecision(
            conserve,
            request_replan=True,
            notice="Anlamlı ilerleme durdu; en kısa kalan yol için bir kez yeniden planlanıyor.",
        )

    if state.phase == "conserve" and stagnant >= policy.stagnation_window:
        delivery = replace(
            updated,
            phase="delivery",
            stagnant_turns=0,
            delivery_stagnant_turns=0,
            transitions=updated.transitions + 1,
        )
        return FastLoopDecision(
            delivery,
            entered_delivery=True,
            notice="Yeniden planlamadan sonra da ilerleme yok; yalnız zorunlu teslim işine geçiliyor.",
        )

    return FastLoopDecision(updated)
