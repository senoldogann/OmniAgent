"""Presence signals only: no window titles or personal content are collected."""
from __future__ import annotations

import asyncio
import math
import time
from typing import Awaitable, Callable, Optional, TypedDict

from omniagent.integrations.runtime import IntegrationStopped


class Presence(TypedDict):
    idle_seconds: Optional[float]
    locked: bool
    foreground_app: Optional[str]
    available: bool


def snapshot() -> Presence:
    """Read macOS presence; unavailable/ambiguous session information locks the GUI gate."""
    try:
        import Quartz
        from AppKit import NSWorkspace
        idle = float(Quartz.CGEventSourceSecondsSinceLastEventType(
            Quartz.kCGEventSourceStateCombinedSessionState, Quartz.kCGAnyInputEventType))
        session = Quartz.CGSessionCopyCurrentDictionary()
        if not session or not math.isfinite(idle) or idle < 0:
            raise ValueError("Presence unavailable")
        locked = bool(session.get("CGSSessionScreenIsLocked", False)) or not bool(
            session.get("kCGSSessionOnConsoleKey", session.get("kCGSessionOnConsoleKey", session.get("CGSessionOnConsoleKey", False))))
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        return {"idle_seconds": idle, "locked": locked,
                "foreground_app": str(app.localizedName()) if app else None, "available": True}
    except (ImportError, AttributeError, ValueError, RuntimeError):
        return {"idle_seconds": None, "locked": True, "foreground_app": None, "available": False}


class GuiGate:
    """Wait for an idle unlocked Mac; discount only input attributable to the preceding agent action."""
    def __init__(self, idle_seconds: float, should_stop: Callable[[], bool],
                 read_presence: Optional[Callable[[], Presence]] = None, poll_seconds: float = 30.0) -> None:
        self.idle_seconds = idle_seconds
        self.should_stop = should_stop
        self.read_presence = read_presence or snapshot
        self.poll_seconds = poll_seconds
        self.last_agent_input: Optional[float] = None
        self.last_user_input: Optional[float] = None

    def mark_input(self) -> None:
        if self.should_stop():
            raise IntegrationStopped("Kullanıcı tarafından durduruldu.")
        now = time.monotonic()
        signals = self.read_presence()
        idle = signals["idle_seconds"]
        if signals["available"] and idle is not None:
            latest = now - idle
            if self.last_agent_input is None or latest > self.last_agent_input + 0.1:
                self.last_user_input = max(self.last_user_input or latest, latest)
        self.last_agent_input = time.monotonic()

    async def __call__(self) -> None:
        while True:
            if self.should_stop():
                raise IntegrationStopped("Kullanıcı tarafından durduruldu.")
            now = time.monotonic()
            signals = await asyncio.to_thread(self.read_presence)
            idle = signals["idle_seconds"]
            safe = signals["available"] and not signals["locked"] and idle is not None
            if safe:
                # HID reports time since the *latest* input. A later event than our last action
                # is user activity. Persist it; a subsequent agent action must not erase that wait.
                latest_input = now - idle
                agent_only = self.last_agent_input is not None and latest_input <= self.last_agent_input + 0.1
                if not agent_only:
                    self.last_user_input = max(self.last_user_input or latest_input, latest_input)
                user_idle = now - self.last_user_input if self.last_user_input is not None else idle
                if user_idle >= self.idle_seconds:
                    return
            # Short stop checks between expensive presence probes satisfy <=500 ms preemption.
            deadline = time.monotonic() + self.poll_seconds
            while time.monotonic() < deadline:
                if self.should_stop():
                    raise IntegrationStopped("Kullanıcı tarafından durduruldu.")
                await asyncio.sleep(min(0.25, max(0, deadline - time.monotonic())))
