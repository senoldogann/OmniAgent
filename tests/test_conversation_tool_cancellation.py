"""Own local worker/child cleanup before a quick-read terminal is published."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from omniagent.app import conversation_budget
from omniagent.tools import browser
from omniagent.tools.process import run_preemptible_process
from omniagent.tools.types import TOOL_RUNTIME
from tests.test_shared_conversation import coordinator, scripted, run, route, turn, call


@pytest.fixture(autouse=True)
def isolated_data(monkeypatch, tmp_path):
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [True, False], ids=["stop", "operation-deadline"])
async def test_quick_fetch_reaps_local_child_and_worker_before_terminal(coordinator, scripted, tmp_path, monkeypatch, cancel):
    scripts, _ = scripted
    pid_path = tmp_path / "child.pid"
    completed, stopped = threading.Event(), threading.Event()
    preemptible = []
    def local_transport(url):
        preemptible.append(TOOL_RUNTIME.get()["preemptible"])
        try:
            return run_preemptible_process([sys.executable, "-c",
                "import os,time,pathlib; pathlib.Path(" + repr(str(pid_path)) + ").write_text(str(os.getpid())); time.sleep(10)"], timeout=15)
        finally:
            completed.set()
    monkeypatch.setattr(browser, "_run_curl", local_transport)
    monkeypatch.setattr(conversation_budget, "OPERATION_SECONDS", .15)
    scripts.extend([route("investigate"), turn(calls=[call("fetch_raw", url="https://example.invalid")])])
    async def cancel_after_spawn():
        while not pid_path.exists():
            await asyncio.sleep(.005)
        stopped.set()
    canceller = asyncio.create_task(cancel_after_spawn()) if cancel else None
    terminal_completed = []
    original_run = coordinator.run_conversation_with_callback
    from tests.test_shared_conversation import options
    def emit(event):
        if event["kind"] == "run_finished":
            terminal_completed.append(completed.is_set())
    try:
        report = await original_run("Read https://example.invalid", emit,
            options(tmp_path, should_stop=stopped.is_set), {"ollama-cloud": object()})
        was_complete = completed.is_set()
    finally:
        if canceller is not None:
            canceller.cancel()
            await asyncio.gather(canceller, return_exceptions=True)
        if not completed.is_set() and pid_path.exists():
            import signal
            try:
                os.kill(int(pid_path.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass
        await asyncio.to_thread(completed.wait, 2)
    assert not report["success"] and preemptible == [True]
    assert was_complete and terminal_completed == [True]
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_path.read_text()), 0)


@pytest.mark.asyncio
async def test_fetch_retry_wait_stops_owned_thread_before_next_attempt(coordinator, scripted, tmp_path, monkeypatch):
    scripts, _ = scripted
    started, completed, stopped = threading.Event(), threading.Event(), threading.Event()
    attempts = []
    def transient(url):
        attempts.append(url)
        started.set()
        return subprocess.CompletedProcess(["local fake curl"], 6, b"", b"temporary local failure")
    monkeypatch.setattr(browser, "_run_curl", transient)
    real_fetch = coordinator.Toolbox.fetch_raw
    def observed(self, url):
        try:
            return real_fetch(self, url)
        finally:
            completed.set()
    monkeypatch.setattr(coordinator.Toolbox, "fetch_raw", observed)
    scripts.extend([route("investigate"), turn(calls=[call("fetch_raw", url="https://example.invalid")])])
    async def stop():
        while not started.is_set():
            await asyncio.sleep(.005)
        stopped.set()
    canceller = asyncio.create_task(stop())
    try:
        report, _ = await run(coordinator, tmp_path, "Read https://example.invalid", {"should_stop": stopped.is_set})
        was_complete = completed.is_set()
    finally:
        await canceller
        await asyncio.to_thread(completed.wait, 3)
    assert not report["success"] and was_complete
    assert len(attempts) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [True, False], ids=["stop", "operation-deadline"])
async def test_quick_search_owns_isolated_local_worker_until_reaped(coordinator, scripted, tmp_path, monkeypatch, cancel):
    from omniagent.tools import readonly_worker
    scripts, _ = scripted
    pid_path = tmp_path / "search.pid"
    stopped = threading.Event()
    def local_command():
        return [sys.executable, "-c", "import os,time,pathlib; pathlib.Path(" + repr(str(pid_path))
                + ").write_text(str(os.getpid())); time.sleep(10)"]
    monkeypatch.setattr(readonly_worker, "worker_command", local_command)
    monkeypatch.setattr(conversation_budget, "OPERATION_SECONDS", .15)
    scripts.extend([route("investigate"), turn(calls=[call("web_search", query="OpenAI releases")])])
    async def stop():
        while not pid_path.exists():
            await asyncio.sleep(.005)
        stopped.set()
    canceller = asyncio.create_task(stop()) if cancel else None
    try:
        report, _ = await run(coordinator, tmp_path, "Search for OpenAI releases", {"should_stop": stopped.is_set})
        assert not report["success"] and report["metrics"]["tool_calls"] == 1
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid_path.read_text()), 0)
    finally:
        if canceller is not None:
            canceller.cancel()
            await asyncio.gather(canceller, return_exceptions=True)


def test_search_worker_protocol_preserves_existing_search_and_uses_owned_fd_streams(tmp_path):
    script = """
import sys
from omniagent.tools import readonly_worker as worker
class Search:
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def text(self, query, **kwargs):
        print('PRIVATE client diagnostic')
        return [{'title': query, 'href': 'https://example.invalid/release', 'body': 'Actual local observation'}]
worker.DDGS = Search
sys.stdin = sys.stdout = sys.stderr = None
worker.main()
"""
    result = subprocess.run([sys.executable, "-c", script], input=json.dumps({"query": "OpenAI literal"}).encode(),
                            capture_output=True, timeout=5)
    assert result.returncode == 0 and result.stderr == b""
    response = json.loads(result.stdout)
    assert response["ok"] is True
    source = json.loads(response["result"])[0]
    assert source["title"] == "OpenAI literal" and source["url"] == "https://example.invalid/release"
    assert b"PRIVATE" not in result.stdout


def test_frozen_internal_dispatch_rejects_effect_request_before_ui_startup(tmp_path):
    script = """
import sys
from omniagent.ui import app
def no_ui(*args): raise AssertionError('worker reached UI startup')
app.OmniUI = app.configure_ui_logging = no_ui
sys.argv = ['OmniAgent', '--internal-readonly-worker']
sys.frozen = True
sys.stdin = sys.stdout = sys.stderr = None
app.main()
"""
    result = subprocess.run([sys.executable, "-c", script], input=b'{"query":"x","command":"forbidden"}',
                            capture_output=True, timeout=5)
    assert result.returncode == 0
    response = json.loads(result.stdout)
    assert response["ok"] is False and response["code"] == "INVALID_ARGUMENTS"
