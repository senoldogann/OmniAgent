"""Komut rayları, sınırlı çıktı, korunan okuma ve görev kapanışı regresyonları."""
import json
import sys
from pathlib import Path

import pytest

import main
import tools
from integration_runtime import CURRENT_RUNTIME, CURRENT_SERVICE, IntegrationRuntime
from tools import TOOL_RUNTIME, ToolError, Toolbox, run_streaming_process


@pytest.mark.parametrize("command", [
    "rm -rf ~/", "rm -rf $HOME/", "rm -rf ${HOME}/",
    "rm -rf / ; echo x", "rm -rf /Users/alice",
    "rm --recursive --force --no-preserve-root /tmp/harmless",
    "sudo -n rm -rf /", "sudo -u root rm -rf /", "sudo --user=root rm -rf /",
    "echo önce; rm -r -f /", "rm -rf $TARGET/", "env TARGET=/ rm -rf $TARGET",
    "sh -c 'rm -rf /'", "sudo -u root sh -c 'rm -rf /'", "env -S 'rm -rf /'",
])
def test_catastrophic_targets_blocked_without_execution(command, monkeypatch):
    monkeypatch.setattr(tools, "run_streaming_process",
                        lambda *args: pytest.fail("Yıkıcı komut çalıştırılmamalı."))
    with pytest.raises(ToolError, match="yıkıcı") as error:
        Toolbox().execute_shell(command, False, None)
    assert error.value.code == "CATASTROPHIC_COMMAND_BLOCKED"


@pytest.mark.parametrize("command", [
    "echo x >>~/.zshrc", "echo x >$HOME/.zshrc",
    "printf x | tee ~/.zshrc", "printf x | tee -a ~/.ssh/config",
    "sed -i '' 's/a/b/' ~/.zshrc", "sed -i.bak 's/a/b/' ~/.zshrc",
    "cp /tmp/source ~/.zshrc", "mv /tmp/source ~/.zshrc",
    "cp -t ~/.ssh /tmp/source", "echo x > /etc/hosts", "echo x > $CONFIG",
    "cp /tmp/source $DEST", "sh -c 'echo x > ~/.zshrc'",
])
def test_sensitive_write_targets_blocked_without_execution(command, monkeypatch):
    monkeypatch.delenv("OMNI_ALLOW_SENSITIVE_WRITE", raising=False)
    monkeypatch.setattr(tools, "run_streaming_process",
                        lambda *args: pytest.fail("Korunan yola yazma çalıştırılmamalı."))
    with pytest.raises(ToolError) as error:
        Toolbox().execute_shell(command, False, None)
    assert error.value.code == "SENSITIVE_PATH_BLOCKED"


@pytest.mark.parametrize("command", [
    "rm -rf /tmp/omni-safe", "echo x >/tmp/omni-safe",
    "cp /tmp/source /tmp/target", "printf 'rm -rf /'",
])
def test_safe_commands_not_rejected_by_target_analysis(command):
    assert not tools._is_catastrophic_command(command)
    assert not tools._shell_writes_to_sensitive_path(command)


def test_read_guard_and_size_limit(tmp_path: Path, monkeypatch):
    target = tmp_path / "secret.txt"
    target.write_text("özel", encoding="utf-8")
    monkeypatch.setattr(tools, "_SENSITIVE_PATH_PREFIXES", (target,))
    monkeypatch.delenv("OMNI_ALLOW_SENSITIVE_READ", raising=False)
    box = Toolbox()
    for method in (box.read_file, box._read_full):
        with pytest.raises(ToolError) as error:
            method(str(target))
        assert error.value.code == "SENSITIVE_PATH_BLOCKED"
    monkeypatch.setenv("OMNI_ALLOW_SENSITIVE_READ", "1")
    assert box.read_file(str(target)) == "özel"
    monkeypatch.setattr(tools, "FILE_READ_MAX_BYTES", 3)
    with pytest.raises(ToolError) as error:
        box.read_file(str(target))
    assert error.value.code == "FILE_TOO_LARGE"
    with pytest.raises(ToolError) as error:
        box.read_file(str(tmp_path))
    assert error.value.code == "IS_DIRECTORY"


def test_stream_capture_caps_result_and_live_events(monkeypatch):
    monkeypatch.setattr(tools, "STREAM_STDOUT_MAX_BYTES", 1024)
    monkeypatch.setattr(tools, "STREAM_STDERR_MAX_BYTES", 512)
    events = []
    token = TOOL_RUNTIME.set({"emit_output": events.append, "should_stop": lambda: False})
    try:
        code = "import sys;sys.stdout.write('x'*100000);sys.stderr.write('y'*50000)"
        returncode, stdout, stderr = run_streaming_process([sys.executable, "-c", code], False, 5)
    finally:
        TOOL_RUNTIME.reset(token)
    assert returncode == 0
    assert len(stdout) < 1200 and len(stderr) < 700
    assert "1024 bayt sınırında kırpıldı" in stdout
    assert "512 bayt sınırında kırpıldı" in stderr
    assert any("kırpıldı" in item for item in events)
    assert sum(len(item.encode("utf-8")) for item in events) < 1800


@pytest.mark.asyncio
async def test_failed_side_effect_invalidates_read_cache(monkeypatch):
    task = IntegrationRuntime(lambda event: None, lambda: False)
    token = CURRENT_RUNTIME.set(task)
    cache = {"stale": {"ok": True}}
    monkeypatch.setattr(Toolbox, "execute_shell",
                        lambda self, command, use_sudo: (_ for _ in ()).throw(
                            ToolError("deneme", "SHELL_EXIT", True)))
    try:
        call = {"id": "1", "name": "execute_shell",
                "arguments": json.dumps({"command": "false", "use_sudo": False})}
        result = await main.execute_tool(call, Toolbox(), cache, lambda event: None, lambda: False)
        assert not result["ok"] and cache == {}
    finally:
        CURRENT_RUNTIME.reset(token)


@pytest.mark.asyncio
async def test_cleanup_failures_still_publish_finished(tmp_path: Path, monkeypatch):
    events = []
    closed = []
    class Outlook:
        def release(self, runtime):
            closed.append("outlook")
            raise RuntimeError("Outlook temizliği başarısız")
    class Service:
        outlook = Outlook()
        async def close(self):
            closed.append("service")
            raise RuntimeError("servis kapanamadı")
    async def close_browser(self):
        closed.append("browser")
        raise RuntimeError("tarayıcı kapanamadı")
    async def fake_model(*args):
        return {"content": "TAMAM", "tool_calls": [], "finish_reason": "stop",
                "usage": main.ZERO_USAGE}, "opencode"
    def failed_save(*args):
        closed.append("memory")
        raise RuntimeError("kayıt başarısız")
    monkeypatch.setattr(main, "_call_model_with_retries", fake_model)
    monkeypatch.setattr(main.sm, "save_state", failed_save)
    monkeypatch.setattr(Toolbox, "close_browser", close_browser)
    monkeypatch.setattr(main, "CapabilityService", Service)
    result = await main.run_agent_with_callback(
        "deneme", events.append,
        {"requested_backend": None, "should_stop": lambda: False,
         "state_file": str(tmp_path / "memory.json"), "history": []},
        {"opencode": object()})
    assert result["success"]
    assert closed == ["memory", "browser", "outlook", "service"]
    assert events[-1]["kind"] == "run_finished"
    assert len([event for event in events if event["kind"] == "notice" and event["level"] == "warning"]) == 4
    assert CURRENT_RUNTIME.get() is None and CURRENT_SERVICE.get() is None


@pytest.mark.asyncio
async def test_broken_state_still_finishes(tmp_path: Path):
    state = tmp_path / "memory.json"
    state.write_text("{bozuk", encoding="utf-8")
    events = []
    result = await main.run_agent_with_callback(
        "deneme", events.append,
        {"requested_backend": None, "should_stop": lambda: False,
         "state_file": str(state), "history": []},
        {"opencode": object()})
    assert not result["success"]
    assert events[-1]["kind"] == "run_finished"


def test_save_json_fsyncs_before_replace(tmp_path: Path, monkeypatch):
    import integration_runtime
    actual = integration_runtime.os.fsync
    called = []
    def tracked(fd):
        called.append(fd)
        return actual(fd)
    monkeypatch.setattr(integration_runtime.os, "fsync", tracked)
    target = tmp_path / "state.json"
    integration_runtime.save_json(target, {"x": 1})
    assert called
    assert json.loads(target.read_text()) == {"x": 1}


def test_background_child_cannot_hold_output_pipe_open():
    import time
    code = ("import subprocess,sys; "
            "subprocess.Popen([sys.executable,'-c','import time;time.sleep(5)']); "
            "print('parent done')")
    started = time.monotonic()
    with pytest.raises(ToolError) as error:
        run_streaming_process([sys.executable, "-c", code], False, 5)
    assert error.value.code == "SHELL_OUTPUT_STALLED"
    assert time.monotonic() - started < 2.5


def test_many_output_lines_use_few_ui_events():
    events = []
    token = TOOL_RUNTIME.set({"emit_output": events.append, "should_stop": lambda: False})
    try:
        code = "import sys;sys.stdout.write(('x'+chr(10))*2000)"
        returncode, stdout, _ = run_streaming_process([sys.executable, "-c", code], False, 5)
    finally:
        TOOL_RUNTIME.reset(token)
    assert returncode == 0
    assert stdout.count("x") == 2000
    assert "".join(events) == stdout
    assert len(events) < 20


@pytest.mark.asyncio
async def test_missing_backend_still_publishes_finished(tmp_path: Path):
    events = []
    result = await main.run_agent_with_callback(
        "deneme", events.append,
        {"requested_backend": None, "should_stop": lambda: False,
         "state_file": str(tmp_path / "memory.json"), "history": []},
        {})
    assert not result["success"]
    assert events[-1]["kind"] == "run_finished"
