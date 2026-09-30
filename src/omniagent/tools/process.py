"""Cooperative process cancellation for autonomous tools, with child ownership retained until exit."""
from __future__ import annotations

import os
import signal
import subprocess
import time
from typing import Any, Dict, List, Optional

from .types import TOOL_RUNTIME, ToolError

POLL_SECONDS = 0.1


def run_preemptible_process(command: List[str], *, env: Optional[Dict[str, str]] = None,
                            capture_output: bool = True, text: bool = False,
                            timeout: float, check: bool = False) -> subprocess.CompletedProcess[Any]:
    """Ordinary callers retain subprocess.run; guarded jobs kill and reap their process group before returning."""
    runtime = TOOL_RUNTIME.get()
    if runtime is None or not runtime.get("preemptible", False):
        return subprocess.run(command, env=env, capture_output=capture_output, text=text, timeout=timeout, check=check)
    if runtime["should_stop"]():
        raise ToolError("Otonom işlem kullanıcı işi için durduruldu.", "STOPPED", False)
    process = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=text, start_new_session=True)
    deadline = time.monotonic() + timeout

    def kill_and_reap() -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()

    try:
        while True:
            if runtime["should_stop"]():
                kill_and_reap()
                raise ToolError("Otonom işlem kullanıcı işi için durduruldu.", "STOPPED", False)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                kill_and_reap()
                raise subprocess.TimeoutExpired(command, timeout)
            try:
                stdout, stderr = process.communicate(timeout=min(POLL_SECONDS, remaining))
            except subprocess.TimeoutExpired:
                continue
            result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
            if check:
                result.check_returncode()
            return result
    finally:
        if process.poll() is None:
            kill_and_reap()
