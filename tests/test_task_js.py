"""Görev kapsamlı JavaScript yardımcısı tekrar kullanımı."""
from __future__ import annotations
import json
from pathlib import Path
import pytest
from omniagent import tools
from omniagent.tools import ToolError, Toolbox

def test_task_js_save_and_reuse_with_json_input(monkeypatch) -> None:
    calls = []
    paths = []
    def fake(command, *_args):
        script_path = Path(command[-2] if "--require" in command else command[1])
        input_data = Path(command[-1]).read_text(encoding="utf-8") if "--require" in command else None
        calls.append((script_path.read_text(encoding="utf-8"), input_data, command))
        paths.extend(Path(part) for part in command if part.startswith("/tmp/") or "omni_" in part)
        return 0, "ok", ""
    monkeypatch.setattr(tools, "run_streaming_process", fake)
    toolbox = Toolbox()
    saved = toolbox.execute_js("// omni:save total\nconsole.log(process.argv[2] || 'ready')")
    assert "kaydedildi" in saved
    reused = toolbox.execute_js('// omni:run total\n{"numbers":[1,2,3]}')
    assert "STDOUT: ok" in reused
    assert calls[0][0] == "console.log(process.argv[2] || 'ready')"
    assert calls[1][0] == calls[0][0]
    assert json.loads(calls[1][1]) == {"numbers": [1, 2, 3]}
    assert not any('{"numbers"' in part for part in calls[1][2])
    assert all(not path.exists() for path in paths)
    assert toolbox._task_js == {"total": calls[0][0]}
    assert Toolbox()._task_js == {}

def test_task_js_failed_save_does_not_register(monkeypatch) -> None:
    monkeypatch.setattr(tools, "run_streaming_process", lambda *args: (1, "", "boom"))
    toolbox = Toolbox()
    with pytest.raises(ToolError):
        toolbox.execute_js("// omni:save broken\nthrow Error('boom')")
    assert toolbox._task_js == {}

def test_task_js_rejects_unknown_or_invalid_input(monkeypatch) -> None:
    monkeypatch.setattr(tools, "run_streaming_process", lambda *args: (0, "", ""))
    toolbox = Toolbox()
    with pytest.raises(ToolError):
        toolbox.execute_js("// omni:run missing")
    toolbox.execute_js("// omni:save helper\nconsole.log(1)")
    with pytest.raises(ToolError):
        toolbox.execute_js("// omni:run helper\n{invalid}")
