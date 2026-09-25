"""Agent katmanları arasında paylaşılan tip sözleşmeleri."""
from __future__ import annotations

from typing import Callable, List, NotRequired, Optional, TypedDict

from omniagent.core import state as sm
from omniagent.core.conversation import Exchange
from omniagent.core.events import TokenUsage
from omniagent.integrations.capabilities import CapabilityService
from omniagent.integrations.runtime import AnswerSink


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


class RunOptions(TypedDict):
    requested_backend: Optional[str]
    should_stop: Callable[[], bool]
    state_file: str
    memory_file: NotRequired[str]
    experience_file: NotRequired[str]
    history: List[Exchange]
    integrations: NotRequired[CapabilityService]
    answer: NotRequired[AnswerSink]
    run_mode: NotRequired[str]
    max_iterations: NotRequired[int]
    max_wall_clock_seconds: NotRequired[float]


class RunReport(TypedDict):
    """Bir görevin sonucu ve ölçümleri."""

    outcome: str
    success: bool
    reason: str
    metrics: sm.EpisodeMetrics
    exchange: Exchange
