"""Cooperative process cancellation for autonomous tools, with child ownership retained until exit."""
from __future__ import annotations

import os
import signal
import subprocess
from threading import Event, Thread
import time
from typing import Any, Callable, Dict, List, Optional

from .types import TOOL_RUNTIME, ToolError

POLL_SECONDS = 0.1


def run_preemptible_process(command: List[str], *, env: Optional[Dict[str, str]] = None,
                            capture_output: bool = True, text: bool = False,
                            timeout: float, check: bool = False,
                            input: bytes | str | None = None,
                            max_output_bytes: int | None = None) -> subprocess.CompletedProcess[Any]:
    """Ordinary callers retain subprocess.run; guarded jobs kill and reap their process group before returning."""
    runtime = TOOL_RUNTIME.get()
    if max_output_bytes is not None:
        if type(max_output_bytes) is not int or max_output_bytes < 1:
            raise ValueError("max_output_bytes must be positive")
        should_stop = runtime["should_stop"] if runtime is not None and runtime.get("preemptible", False) else lambda: False
        if should_stop():
            raise ToolError("Otonom işlem kullanıcı işi için durduruldu.", "STOPPED", False)
        return _run_bounded_process(command, env=env, text=text, timeout=timeout,
                                    check=check, input=input, limit=max_output_bytes,
                                    should_stop=should_stop)
    if runtime is None or not runtime.get("preemptible", False):
        arguments = {} if input is None else {"input": input}
        return subprocess.run(command, env=env, capture_output=capture_output, text=text, timeout=timeout, check=check, **arguments)
    if runtime["should_stop"]():
        raise ToolError("Otonom işlem kullanıcı işi için durduruldu.", "STOPPED", False)
    process = subprocess.Popen(command, env=env, stdin=subprocess.PIPE if input is not None else None,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
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
                stdout, stderr = process.communicate(input=input, timeout=min(POLL_SECONDS, remaining))
            except subprocess.TimeoutExpired:
                input = None
                continue
            result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
            if check:
                result.check_returncode()
            return result
    finally:
        if process.poll() is None:
            kill_and_reap()


def _run_bounded_process(command: List[str], *, env: Optional[Dict[str, str]], text: bool,
                         timeout: float, check: bool, input: bytes | str | None,
                         limit: int, should_stop: Callable[[], bool]) -> subprocess.CompletedProcess[Any]:
    """Capture a private protocol in memory; own its pipe threads through cleanup."""
    process = subprocess.Popen(command, env=env, stdin=subprocess.PIPE if input is not None else None,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               start_new_session=True)
    chunks: List[bytes] = []
    overflow = Event()

    def read_output() -> None:
        size = 0
        with process.stdout:
            while chunk := process.stdout.read1(65536):
                available = max(0, limit - size)
                if available:
                    chunks.append(chunk[:available])
                size += len(chunk)
                if size > limit:
                    overflow.set()

    def write_input() -> None:
        try:
            process.stdin.write(input.encode() if isinstance(input, str) else input)
            process.stdin.flush()
        except OSError:
            pass
        finally:
            try:
                process.stdin.close()
            except OSError:
                pass

    reader = Thread(target=read_output)
    writer = Thread(target=write_input) if input is not None else None
    deadline = time.monotonic() + timeout
    try:
        reader.start()
        if writer is not None:
            writer.start()
        while True:
            if should_stop():
                raise ToolError("Otonom işlem kullanıcı işi için durduruldu.", "STOPPED", False)
            if overflow.is_set():
                raise ToolError("Okuma sonucu boyut sınırını aştı.", "OUTPUT_TOO_LARGE", False)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            try:
                process.wait(timeout=min(POLL_SECONDS, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
    finally:
        # A finished leader may leave descendants holding its pipes. Reap the
        # entire owned group before waiting for readers/writers, on every path.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        if reader.ident is not None:
            reader.join()
        else:
            process.stdout.close()
        if writer is not None and writer.ident is not None:
            writer.join()
        elif process.stdin is not None:
            process.stdin.close()
    if overflow.is_set():
        raise ToolError("Okuma sonucu boyut sınırını aştı.", "OUTPUT_TOO_LARGE", False)
    stdout = b"".join(chunks)
    result = subprocess.CompletedProcess(command, process.returncode,
                                         stdout.decode() if text else stdout, "" if text else b"")
    if check:
        result.check_returncode()
    return result


def check_read_stop() -> None:
    runtime = TOOL_RUNTIME.get()
    if runtime is not None and runtime.get("cancellable_read") and runtime["should_stop"]():
        raise ToolError("Okuma kullanıcı veya süre sınırı nedeniyle durduruldu.", "STOPPED", False)


def read_retry_delay(seconds: float) -> None:
    runtime = TOOL_RUNTIME.get()
    if runtime is None or not runtime.get("cancellable_read"):
        time.sleep(seconds)
        return
    deadline = time.monotonic() + seconds
    while True:
        check_read_stop()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(.05, remaining))
