"""Büyük dosyanın dar kapsamlı düzenlemesi ve görev bazlı araç şeması."""
import json
from pathlib import Path
from typing import Any

import pytest

import main
from capabilities import CapabilityService
from tools import ToolError, Toolbox


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
