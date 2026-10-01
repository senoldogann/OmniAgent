"""Host enforced autonomy: side effects remain behind approval and user work wins."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from omniagent import approval
from omniagent.app import agent
from omniagent.app.tool_execution import deletion_command, execute_tool
from omniagent.companion.autonomy import is_quiet_hour
from omniagent.companion import delegate
from omniagent.integrations.capabilities import CapabilityService
from omniagent.integrations.runtime import CURRENT_RUNTIME, IntegrationRuntime, IntegrationStopped
from omniagent.platform.macos.host_lock import (
    HostBusyError, async_host_task_lock_preempting, host_owner, host_task_lock,
    host_task_lock_preempting, preemptible_host_task_lock, preemption_requested,
)
from omniagent.platform.macos.presence import GuiGate


@pytest.mark.parametrize("code", [
    "rm -rf /tmp/a", "rmdir /tmp/a", "unlink file", "find /tmp -type f -delete", "trash file",
    "git clean -fd", "os.remove(path)", "os.unlink(path)", "shutil.rmtree(path)",
    "Path(path).unlink()", "target.rmdir()",
])
def test_conventional_deletion_patterns(code):
    assert deletion_command(code)


@pytest.mark.parametrize("code", ["ls ~/Desktop", "print('hello')", "mkdir /tmp/a", "git status"])
def test_readonly_and_nondelete_commands(code):
    assert not deletion_command(code)


def test_real_process_autonomy_yields_to_user(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    code = '''
import time
from omniagent.platform.macos.host_lock import preemptible_host_task_lock, preemption_requested
since = time.time()
with preemptible_host_task_lock():
    print('ready', flush=True)
    deadline = time.monotonic()+4
    while not preemption_requested(since):
        if time.monotonic()>deadline: raise RuntimeError('missing request')
        time.sleep(.02)
print('yielded', flush=True)
'''
    child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, env={**os.environ, "PYTHONPATH": str(Path("src").resolve())})
    try:
        assert child.stdout.readline().strip() == "ready"
        assert host_owner() == "autonomous"
        started = time.monotonic()
        with host_task_lock_preempting(timeout_seconds=2):
            assert host_owner() == "user"
        assert time.monotonic() - started < 2
        stdout, stderr = child.communicate(timeout=3)
        assert child.returncode == 0, stderr
        assert stdout.strip() == "yielded"
        assert not (tmp_path / "host-preempt.request").exists()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


def test_user_job_cannot_be_preempted(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    with host_task_lock():
        started = time.monotonic()
        with pytest.raises(HostBusyError):
            with host_task_lock_preempting(timeout_seconds=1):
                pass
        assert .9 <= time.monotonic() - started < 1.5
    assert not (tmp_path / "host-preempt.request").exists()


@pytest.mark.asyncio
async def test_same_event_loop_preemption_does_not_block_autonomous_shutdown(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    ready = asyncio.Event()
    async def autonomous():
        since = time.time()
        with preemptible_host_task_lock():
            ready.set()
            while not preemption_requested(since):
                await asyncio.sleep(.02)
    task = asyncio.create_task(autonomous())
    await ready.wait()
    async with async_host_task_lock_preempting(timeout_seconds=1):
        assert host_owner() == "user"
    await task


@pytest.mark.asyncio
async def test_cancelled_user_lock_waiter_does_not_target_next_autonomous_owner(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    with preemptible_host_task_lock():
        waiting = asyncio.create_task(_wait_lock())
        while not (tmp_path / "host-preempt.request").exists():
            await asyncio.sleep(.01)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
    with preemptible_host_task_lock():
        since = time.time()
        await asyncio.sleep(.15)
        assert not preemption_requested(since)
    async with async_host_task_lock_preempting(timeout_seconds=.2):
        assert host_owner() == "user"


async def _wait_lock():
    async with async_host_task_lock_preempting(timeout_seconds=1):
        pass


def guards(root, *, quiet=False, gate=None, marker=None):
    async def open_gate():
        pass
    return {"gui_gate": gate or open_gate, "mark_gui_input": marker or (lambda: None),
            "guarded_roots": [root], "quiet_now": lambda: quiet, "deferred_approvals": []}


class ProbeToolbox:
    memory_mutation_allowed = False
    last_capture_path = None
    def __init__(self):
        self.executed = []
    def execute_shell(self, **kwargs):
        self.executed.append(kwargs)
        return "ran"
    execute_python = execute_shell
    capture_photo = execute_shell
    write_file = execute_shell
    edit_file = execute_shell
    cua_press_key = execute_shell


async def run_probe(name, arguments, runtime, box):
    token = CURRENT_RUNTIME.set(runtime)
    try:
        return await execute_tool({"id": "probe", "name": name, "arguments": json.dumps(arguments)},
                                  box, {}, lambda event: None, runtime.should_stop)
    finally:
        CURRENT_RUNTIME.reset(token)


@pytest.mark.parametrize("name, arguments, category", [
    ("execute_shell", {"command": "rm /tmp/file"}, "deletion"),
    ("execute_python", {"code": "os.remove(path)"}, "deletion"),
    ("capture_photo", {}, "camera"),
    ("write_file", {"path": "OWN_SOURCE", "content": "x"}, "self_modification"),
    ("edit_file", {"path": "OWN_SOURCE", "old_text": "a", "new_text": "b"}, "self_modification"),
])
@pytest.mark.asyncio
async def test_guard_denial_prevents_side_effects(tmp_path, monkeypatch, name, arguments, category):
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path / "data"))
    source = tmp_path / "source"
    source.mkdir()
    arguments = {key: str(source / "app.py") if value == "OWN_SOURCE" else value for key, value in arguments.items()}
    asked = []
    async def deny(title, fields):
        asked.append(title)
        return {approval.APPROVAL_FIELD: False}
    runtime = IntegrationRuntime(lambda event: None, lambda: False, deny)
    runtime.autonomy = guards(source)
    box = ProbeToolbox()
    result = await run_probe(name, arguments, runtime, box)
    assert result["code"] == approval.APPROVAL_DENIED_CODE
    assert not box.executed and len(asked) == 1
    audit = [json.loads(line) for line in (tmp_path / "data" / "audit.jsonl").read_text().splitlines()]
    assert audit[-1]["category"] == category and audit[-1]["decision"] == "denied"


@pytest.mark.asyncio
async def test_quiet_approval_is_deferred_without_sending(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    async def must_not_send(*args):
        raise AssertionError("quiet hour approval sent")
    runtime = IntegrationRuntime(lambda event: None, lambda: False, must_not_send)
    runtime.autonomy = guards(tmp_path / "source", quiet=True)
    box = ProbeToolbox()
    result = await run_probe("capture_photo", {}, runtime, box)
    assert result["code"] == approval.APPROVAL_TIMEOUT_CODE
    assert runtime.autonomy["deferred_approvals"] and not box.executed


@pytest.mark.asyncio
async def test_autonomy_never_inherits_approval_bypass(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(approval, "AUTO_APPROVE_IN_CONTINUOUS_MODE", True)
    monkeypatch.setattr(approval, "should_auto_approve", lambda *args: True)
    runtime = IntegrationRuntime(lambda event: None, lambda: False)
    runtime.unattended = True  # Even corrupted options cannot bypass tool guards.
    runtime.autonomy = guards(tmp_path / "source")
    box = ProbeToolbox()
    result = await run_probe("capture_photo", {}, runtime, box)
    assert result["code"] == approval.APPROVAL_UNAVAILABLE_CODE and not box.executed


@pytest.mark.asyncio
async def test_user_run_does_not_gain_autonomy_deletion_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    runtime = IntegrationRuntime(lambda event: None, lambda: False)
    box = ProbeToolbox()
    assert (await run_probe("execute_shell", {"command": "rm /tmp/file"}, runtime, box))["ok"]
    assert len(box.executed) == 1


@pytest.mark.asyncio
async def test_gui_mark_precedes_tool_and_human_input_during_settle_is_visible(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    clock = [100.0]
    idle = [100.0]
    stop = [False]
    monkeypatch.setattr("omniagent.platform.macos.presence.time", SimpleNamespace(monotonic=lambda: clock[0]))
    gate = GuiGate(10, lambda: stop[0], lambda: {"idle_seconds": idle[0], "locked": False,
                                              "available": True, "foreground_app": "test"}, poll_seconds=.01)
    runtime = IntegrationRuntime(lambda event: None, lambda: stop[0])
    runtime.autonomy = guards(tmp_path, gate=gate, marker=gate.mark_input)
    box = ProbeToolbox()
    def action(**kwargs):
        assert gate.last_agent_input == 100
        clock[0] = 105
        idle[0] = .5  # Human input during tool settle, after the agent event at100.
        return "done"
    box.cua_press_key = action
    assert (await run_probe("cua_press_key", {"key": "enter"}, runtime, box))["ok"]
    assert gate.last_agent_input == 100
    waiting = asyncio.create_task(gate())
    await asyncio.sleep(.03)
    assert not waiting.done()
    stop[0] = True
    with pytest.raises(IntegrationStopped):
        await waiting


@pytest.mark.asyncio
async def test_gui_gate_discounts_own_hid_without_erasing_observed_user_input(monkeypatch):
    clock = [100.0]
    signals = {"idle_seconds": 100.0, "locked": False, "available": True, "foreground_app": None}
    monkeypatch.setattr("omniagent.platform.macos.presence.time", SimpleNamespace(monotonic=lambda: clock[0]))
    gate = GuiGate(10, lambda: False, lambda: signals)
    await gate()
    gate.mark_input()
    clock[0] = 101
    signals["idle_seconds"] = 1.0  # Our event at100.
    await gate()  # No artificial180s wait after own action.
    clock[0] = 105
    signals["idle_seconds"] = .5  # Human event at104.5.
    gate.mark_input()  # A later agent event must retain the observed human event.
    assert gate.last_user_input == 104.5


@pytest.mark.asyncio
async def test_explicit_autonomous_deletion_goal_asks_before_model(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    calls = []
    async def deny(title, fields):
        calls.append(title)
        return {approval.APPROVAL_FIELD: False}
    async def must_not_call(*args):
        raise AssertionError("model ran before deletion approval")
    monkeypatch.setattr(agent, "_call_model_with_retries", must_not_call)
    service = CapabilityService(tmp_path)
    try:
        report = await agent.run_agent_with_callback(f"sil: {tmp_path / 'file'}", lambda event: None,
            {"requested_backend": None, "should_stop": lambda: False, "state_file": str(tmp_path / "state.json"),
             "history": [], "integrations": service, "answer": deny, "autonomy": guards(tmp_path / "source")},
            {"ollama-cloud": object()})
    finally:
        await service.close()
    assert not report["success"] and len(calls) == 1
    assert "onaylamadı" in report["outcome"]


def test_autonomy_has_no_budget_and_rejects_unattended():
    options = {"autonomy": guards(Path("/tmp/source")), "max_iterations": 1,
               "max_wall_clock_seconds": .001, "max_total_tokens": 1}
    mode, iterations, wall = agent.resolve_run_limits(options)
    assert iterations == 0 and wall == float("inf") and mode == "normal"
    with pytest.raises(ValueError, match="unattended"):
        agent.resolve_run_limits({**options, "unattended": True})


@pytest.mark.asyncio
async def test_autonomous_delegate_requires_rationale_and_uses_combined_preemption_stop(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(delegate, "create_model_clients", lambda: {})
    waiting = []
    seen = []
    async def run(goal, emit, options, clients):
        seen.append(options)
        async with options["task_context"]():
            waiting.append(asyncio.create_task(_wait_lock()))
            deadline = time.monotonic() + 1
            while not options["should_stop"]():
                assert time.monotonic() < deadline
                await asyncio.sleep(.02)
        return delegate.failure_outcome(goal, "kullanıcı işi geldi, durduruldu",
                                        "2026-09-30T10:00:00+00:00")["report"]
    monkeypatch.setattr(delegate, "run_agent_with_callback", run)
    options = {"requested_backend": None, "should_stop": lambda: False, "state_file": str(tmp_path / "state"),
               "history": [], "autonomy": guards(tmp_path / "source")}
    with pytest.raises(ValueError, match="gerekçe"):
        await delegate.run_task("iş", options, lambda line: None, origin="autonomous")
    outcome = await delegate.run_task("iş", options, lambda line: None, origin="autonomous", rationale="kontrol listesi")
    await waiting[0]
    assert outcome["origin"] == "autonomous" and outcome["rationale"] == "kontrol listesi"
    assert "unattended" not in seen[0] and not outcome["report"]["success"]


@pytest.mark.asyncio
async def test_successful_autonomous_tool_logs_started_and_terminal_action(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    runtime = IntegrationRuntime(lambda event: None, lambda: False)
    runtime.autonomy = guards(tmp_path / "source")
    box = ProbeToolbox()
    assert (await run_probe("execute_shell", {"command": "ls /tmp"}, runtime, box))["ok"]
    audit = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert [row["decision"] for row in audit] == ["started", "succeeded"]
    assert all(row["category"] == "autonomous_action" for row in audit)
