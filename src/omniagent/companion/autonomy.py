"""Autonomous run options and deterministic quiet hour calculation."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable, Dict

import omniagent
from omniagent.app.types import AutonomyGuards
from omniagent.platform.macos.presence import GuiGate


def is_quiet_hour(now: datetime, hours: Dict[str, str]) -> bool:
    start = tuple(map(int, hours["start"].split(":")))
    end = tuple(map(int, hours["end"].split(":")))
    current = (now.hour, now.minute)
    if start == end:
        return False
    return start <= current < end if start < end else current >= start or current < end


def make_autonomy_guards(gui_idle_seconds: float, should_stop: Callable[[], bool],
                         quiet_hours: Dict[str, str]) -> AutonomyGuards:
    gate = GuiGate(gui_idle_seconds, should_stop)
    return {"gui_gate": gate, "mark_gui_input": gate.mark_input,
            "guarded_roots": [Path(omniagent.__file__).resolve().parent],
            "quiet_now": lambda: is_quiet_hour(datetime.now().astimezone(), quiet_hours),
            "deferred_approvals": []}
