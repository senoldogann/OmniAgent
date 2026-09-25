"""Büyük dosyanın dar kapsamlı düzenlemesi ve görev bazlı araç şeması."""
import json
import stat
from pathlib import Path
from typing import Any

import pytest

from omniagent.app import agent as main
from omniagent.integrations.capabilities import CapabilityService
from omniagent.tools import ToolError, Toolbox
from omniagent.tools import filesystem


def test_edit_preserves_large_file_and_changes_unique_snippet(tmp_path: Path) -> None:
    target = tmp_path / "large.py"
    original = "# giriş\n" + ("VALUE = 1\n" * 1400) + "TARGET = 'old'\n" + "# son\n"
    target.write_text(original, encoding="utf-8")
    box = Toolbox()
    assert len(box.read_file(str(target))) < len(original)
    result = box.edit_file(str(target), "TARGET = 'old'\n", "TARGET = 'new'\n")
    assert "doğrulandı" in result
    assert target.read_text(encoding="utf-8") == original.replace("TARGET = 'old'", "TARGET = 'new'")


def test_edit_rejects_ambiguous_text_and_invalid_python(tmp_path: Path) -> None:
    target = tmp_path / "script.py"
    target.write_text("VALUE = 1\nVALUE = 1\n", encoding="utf-8")
    box = Toolbox()
    with pytest.raises(ToolError) as duplicate:
        box.edit_file(str(target), "VALUE = 1", "VALUE = 2")
    assert duplicate.value.code == "EDIT_MATCH_COUNT"
    with pytest.raises(ToolError) as invalid:
        box.edit_file(str(target), "VALUE = 1\nVALUE = 1\n", "def broken(\n")
    assert invalid.value.code == "SYNTAX_INVALID"
    assert target.read_text(encoding="utf-8") == "VALUE = 1\nVALUE = 1\n"


def test_backups_are_namespaced_by_full_source_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    backup_dir = tmp_path / "backups"
    monkeypatch.setattr(filesystem, "BACKUP_DIR", backup_dir)
    first = tmp_path / "one" / "config.py"
    second = tmp_path / "two" / "config.py"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_text("VALUE = 1\n", encoding="utf-8")
    second.write_text("VALUE = 2\n", encoding="utf-8")

    filesystem.write_file_content(str(first), "VALUE = 10\n")
    filesystem.write_file_content(str(second), "VALUE = 20\n")

    backups = sorted(path.name for path in backup_dir.glob("*.bak"))
    assert len(backups) == 2
    first_namespace = backups[0].rsplit(".", 2)[0]
    second_namespace = backups[1].rsplit(".", 2)[0]
    assert first_namespace != second_namespace


def test_write_preserves_existing_file_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(filesystem, "BACKUP_DIR", tmp_path / "backups")
    target = tmp_path / "script.sh"
    target.write_text("#!/bin/sh\necho old\n", encoding="utf-8")
    target.chmod(0o755)

    filesystem.write_file_content(str(target), "#!/bin/sh\necho new\n")

    assert stat.S_IMODE(target.stat().st_mode) == 0o755


def test_edit_schema_is_scoped_to_source_tasks() -> None:
    ordinary = {schema["function"]["name"] for schema in main.build_tool_schemas("Posta kutumu oku")}
    source = {schema["function"]["name"] for schema in main.build_tool_schemas(
        "Kaynak kodu düzelt", allow_edit=True,
    )}
    assert "edit_file" not in ordinary
    assert "edit_file" in source
    assert "edit_file" in main.TOOL_NAMES
    assert "edit_file" in main._SIDE_EFFECT_TOOLS


@pytest.mark.asyncio
async def test_real_edit_satisfies_source_action_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "feature.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    calls = 0

    async def fake_model(
        clients: Any, messages: Any, schemas: Any, session_id: str,
        backend: str, emit: Any, should_stop: Any,
    ) -> tuple[dict[str, Any], str]:
        nonlocal calls
        calls += 1
        assert "edit_file" in {schema["function"]["name"] for schema in schemas}
        if calls == 1:
            return {
                "content": "", "tool_calls": [{
                    "id": "edit-1", "name": "edit_file",
                    "arguments": json.dumps({
                        "path": str(target), "old_text": "VALUE = 1\n",
                        "new_text": "VALUE = 2\n",
                    }),
                }],
                "finish_reason": "tool_calls", "usage": main.ZERO_USAGE,
            }, backend
        return {
            "content": "Değişiklik tamamlandı.",
            "tool_calls": [], "finish_reason": "stop", "usage": main.ZERO_USAGE,
        }, backend

    monkeypatch.setattr(main, "_call_model_with_retries", fake_model)
    service = CapabilityService(tmp_path)
    try:
        report = await main.run_agent_with_callback(
            "feature.py kodunu düzelt", lambda event: None,
            {"requested_backend": None, "should_stop": lambda: False,
             "state_file": str(tmp_path / "memory.json"), "history": [],
             "integrations": service},
            {"ollama-cloud": object()},
        )
    finally:
        await service.close()
    assert report["success"]
    assert target.read_text(encoding="utf-8") == "VALUE = 2\n"
    assert report["metrics"]["turns"] == 2
