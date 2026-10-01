"""Agent katmanları arasında paylaşılan tip sözleşmeleri."""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, AsyncContextManager, Awaitable, Callable, List, NotRequired, Optional, TypedDict

from omniagent.core import state as sm
from omniagent.core.conversation import Exchange
from omniagent.core.events import TokenUsage
from omniagent.core.evidence import EvidenceBundle, RequestContract
from omniagent.integrations.capabilities import CapabilityService
from omniagent.integrations.runtime import AnswerSink, DeliverSink


if TYPE_CHECKING:
    from omniagent.app.conversation import ConversationDecision
    from omniagent.core.activity import ActivitySession


class RunModeProfile(TypedDict):
    label: str
    max_iterations: int
    max_wall_clock_seconds: float


class ToolResult(TypedDict, total=False):
    tool_call_id: str
    ok: bool
    result: str
    error_type: str
    error: str
    code: str
    recoverable: bool
    artifact_path: str
    completed_steps: int


class ToolCallDraft(TypedDict):
    """Modelin istediği araç çağrısı (akış parçalarından birleştirilmiş)."""

    id: str
    name: str
    arguments: str


class ModelTurn(TypedDict):
    """Bir model turunun akıştan birleştirilmiş sonucu."""

    content: str
    tool_calls: List[ToolCallDraft]
    finish_reason: Optional[str]
    usage: TokenUsage


class AutonomyGuards(TypedDict):
    """Host enforced gates only for companion initiated work; approval is never implied."""
    gui_gate: Callable[[], Awaitable[None]]
    mark_gui_input: Callable[[], None]
    guarded_roots: List[Path]
    quiet_now: Callable[[], bool]
    deferred_approvals: List[str]


class RunOptions(TypedDict):
    activity_session: NotRequired[ActivitySession]
    activity_origin: NotRequired[str]
    requested_backend: Optional[str]
    # Host owned factory: only the task route may acquire exclusive ownership.
    task_context: NotRequired[Callable[[], AsyncContextManager[object]]]
    conversation_decision: NotRequired[ConversationDecision]
    conversation_persona: NotRequired[str]
    conversation_memory: NotRequired[str]
    request_contract: NotRequired[RequestContract]
    evidence_run_id: NotRequired[str]
    should_stop: Callable[[], bool]
    state_file: str
    memory_file: NotRequired[str]
    experience_file: NotRequired[str]
    history: List[Exchange]
    integrations: NotRequired[CapabilityService]
    answer: NotRequired[AnswerSink]
    deliver: NotRequired[DeliverSink]
    images: NotRequired[List[str]]
    scheduled_run: NotRequired[bool]
    run_mode: NotRequired[str]
    max_iterations: NotRequired[int]
    max_wall_clock_seconds: NotRequired[float]
    max_total_tokens: NotRequired[int]
    unattended: NotRequired[bool]
    autonomy: NotRequired[AutonomyGuards]
    pop_control_messages: NotRequired[Callable[[], List[str]]]
    # Model çağrısı yeniden deneme bütçesi (sn). Sürekli/gözetimsiz görevde kalan görev süresi (çağrı başına
    # en çok 30 dk) kullanılır ve bu değer yok sayılır; verilmezse etkileşimli 60 sn
    # (bkz. model_retry.interactive_model_retry_seconds ve unattended_model_retry_seconds).
    model_retry_seconds: NotRequired[float]


class RunReport(TypedDict):
    """Bir görevin sonucu ve ölçümleri."""

    outcome: str
    success: bool
    reason: str
    metrics: sm.EpisodeMetrics
    exchange: Exchange
    evidence: NotRequired[EvidenceBundle]
