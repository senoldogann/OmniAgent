"""Görev kapsamlı Python yardımcısı tekrar kullanımı (execute_python)."""
from __future__ import annotations
import json
from pathlib import Path
import pytest
from omniagent import tools
from omniagent.tools import ToolError, Toolbox

def test_task_py_save_and_reuse_with_json_input(monkeypatch) -> None:
    calls = []
    paths = []
    def fake(command, *_args):
        script_path = Path(command[1])
        input_data = None
        if len(command) > 2:
            input_data = Path(command[2]).read_text(encoding="utf-8")
        calls.append((script_path.read_text(encoding="utf-8"), input_data, command))
        paths.extend(Path(part) for part in command[1:] if part.startswith("/tmp/") or "omni_" in part)
        return 0, "ok", ""
    monkeypatch.setattr(tools, "run_streaming_process", fake)
    toolbox = Toolbox()
    saved = toolbox.execute_python("# omni:save toplam\nprint('hazir')")
    assert "kaydedildi" in saved
    reused = toolbox.execute_python('# omni:run toplam\n{"sayilar":[1,2,3]}')
    assert "STDOUT: ok" in reused
    assert calls[0][0] == "print('hazir')"
    assert calls[1][0] == calls[0][0]
    assert json.loads(calls[1][1]) == {"sayilar": [1, 2, 3]}
    assert not any('{"sayilar"' in part for part in calls[1][2])
    assert all(not path.exists() for path in paths)
    assert toolbox._task_py == {"toplam": calls[0][0]}
    assert Toolbox()._task_py == {}

def test_task_py_failed_save_does_not_register(monkeypatch) -> None:
    monkeypatch.setattr(tools, "run_streaming_process", lambda *args: (1, "", "boom"))
    toolbox = Toolbox()
    with pytest.raises(ToolError):
        toolbox.execute_python("# omni:save bozuk\nraise RuntimeError('boom')")
    assert toolbox._task_py == {}

def test_task_py_rejects_unknown_or_invalid_input(monkeypatch) -> None:
    monkeypatch.setattr(tools, "run_streaming_process", lambda *args: (0, "", ""))
    toolbox = Toolbox()
    with pytest.raises(ToolError):
        toolbox.execute_python("# omni:run yok")
    toolbox.execute_python("# omni:save yardimci\nprint(1)")
    with pytest.raises(ToolError):
        toolbox.execute_python("# omni:run yardimci\n{gecersiz}")

def test_task_py_timeout_is_recoverable(monkeypatch) -> None:
    def fake(_command, *_args):
        raise ToolError("zaman aşımı", "SHELL_TIMEOUT", True)
    monkeypatch.setattr(tools, "run_streaming_process", fake)
    with pytest.raises(ToolError) as error:
        Toolbox().execute_python("import time; time.sleep(999)")
    assert error.value.code == "PY_TIMEOUT"
    assert error.value.recoverable is True

def test_task_py_nonzero_exit_is_recoverable(monkeypatch) -> None:
    monkeypatch.setattr(tools, "run_streaming_process", lambda *args: (2, "", "SyntaxError"))
    with pytest.raises(ToolError) as error:
        Toolbox().execute_python("raise ValueError")
    assert error.value.code == "PY_EXIT"
    assert error.value.recoverable is True
