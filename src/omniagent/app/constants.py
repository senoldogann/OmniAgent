"""Agent orchestration için paylaşılan çalışma bütçeleri ve görünüm sabitleri."""
from __future__ import annotations

from typing import Dict, Tuple

from omniagent.app.types import RunModeProfile
from omniagent.core.events import TokenUsage


MAX_ITERATIONS: int = 25
MAX_WALL_CLOCK_SECONDS: float = 600.0

RUN_MODE_PROFILES: Dict[str, RunModeProfile] = {
    "normal": {
        "label": "Normal",
        "max_iterations": 25,
        "max_wall_clock_seconds": 600.0,
    },
    "extended": {
        "label": "Uzun",
        "max_iterations": 50,
        "max_wall_clock_seconds": 1200.0,
    },
    "autonomous": {
        "label": "Otonom",
        "max_iterations": 100,
        "max_wall_clock_seconds": 2700.0,
    },
}

NO_PROGRESS_LIMIT: int = 4
FULL_DETAIL_TURNS: int = 2
TRIMMED_CONTENT_LIMIT: int = 400
READ_TRIMMED_TAIL_LIMIT: int = 600
TRIMMED_ARGS_LIMIT: int = 120

SOFT_UNCACHED_PROMPT_TOKEN_BUDGET: int = 60_000
SOFT_TOOL_CALL_BUDGET: int = 24
MAX_FINAL_LENGTH_RECOVERIES: int = 1
MAX_UNEXECUTED_TOOL_RECOVERIES: int = 1
MAX_SOURCE_ACTION_RECOVERIES: int = 1
MAX_ACTION_EVIDENCE_RECOVERIES: int = 1
MAX_NOVEL_SHELL_OUTPUTS: int = 8
MAX_NOVEL_READ_OUTPUTS: int = 12
TASK_LEDGER_LIMIT: int = 4000

MODEL_IMAGE_QUALITY: int = 90
TURKISH_WEEKDAYS: Tuple[str, ...] = (
    "Pazartesi",
    "Salı",
    "Çarşamba",
    "Perşembe",
    "Cuma",
    "Cumartesi",
    "Pazar",
)
ENGLISH_WEEKDAYS: Tuple[str, ...] = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)
ZERO_USAGE: TokenUsage = {
    "prompt_tokens": 0,
    "cached_tokens": 0,
    "completion_tokens": 0,
}
