"""Real subprocesses must stop before autonomous host ownership is released."""
import os
import subprocess
import sys
import threading
import time

import pytest

from omniagent.tools.process import run_preemptible_process
from omniagent.tools.types import TOOL_RUNTIME, ToolError


def test_guarded_process_is_reaped_when_user_preempts(tmp_path):
    pid_file = tmp_path / "pid"
    stopped = threading.Event()
    failures = []
    def run():
        token = TOOL_RUNTIME.set({"should_stop": stopped.is_set, "preemptible": True})
        try:
            run_preemptible_process([sys.executable, "-c",
                "import os,sys,time; from pathlib import Path; Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)",
                str(pid_file)], timeout=35)
        except ToolError as error:
            failures.append(error.code)
        finally:
            TOOL_RUNTIME.reset(token)
    worker = threading.Thread(target=run)
    worker.start()
    deadline = time.monotonic() + 3
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert pid_file.exists()
    pid = int(pid_file.read_text())
    stopped.set()
    worker.join(1)
    assert not worker.is_alive() and failures == ["STOPPED"]
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_guarded_process_preserves_binary_and_text_outputs():
    token = TOOL_RUNTIME.set({"should_stop": lambda: False, "preemptible": True})
    try:
        binary = run_preemptible_process([sys.executable, "-c", "print('hello')"], timeout=2)
        assert binary.stdout == b"hello\n" and binary.returncode == 0
        text = run_preemptible_process([sys.executable, "-c", "print('hello')"], timeout=2, text=True)
        assert text.stdout == "hello\n"
        with pytest.raises(subprocess.TimeoutExpired):
            run_preemptible_process([sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.1)
    finally:
        TOOL_RUNTIME.reset(token)


def test_user_tool_keeps_existing_subprocess_contract(monkeypatch):
    expected = subprocess.CompletedProcess(["tool"], 0, b"body", b"")
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return expected
    monkeypatch.setattr(subprocess, "run", run)
    assert run_preemptible_process(["tool"], timeout=25) is expected
    assert calls[0][1]["timeout"] == 25
