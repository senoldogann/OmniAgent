"""Kalıcı kullanıcı hafızasının sınır, gizlilik ve kalıcılık testleri."""
import json
from pathlib import Path

import pytest

import user_memory
import main
from main import ToolCallDraft, build_tool_schemas
from tools import ToolError, Toolbox


def test_memory_tool_is_exposed_in_base_schema() -> None:
    names = [entry["function"]["name"] for entry in build_tool_schemas("genel")]
    assert "user_memory" in names


def test_memory_mutation_intent_is_explicit() -> None:
    assert main.memory_mutation_requested("Bunu hatırla: editörüm VS Code") is True
    assert main.memory_mutation_requested("Bu tercihi kalıcı hafızaya kaydet") is True
    assert main.memory_mutation_requested("Forget my saved editor preference") is True
    assert main.memory_mutation_requested("Hatırladığın editörümü kullan") is False
    assert main.memory_mutation_requested("GitHub'da araştırma yap") is False


def test_memory_mutation_requires_runtime_capability(tmp_path: Path) -> None:
    memory_file: Path = tmp_path / "user_memory.json"
    readonly = Toolbox(memory_file=str(memory_file), allow_memory_mutation=False)
    with pytest.raises(ToolError) as denied:
        readonly.user_memory(
            action="remember", key="editor", value="Visual Studio Code", category="preference",
        )
    assert denied.value.code == "MEMORY_MUTATION_NOT_ALLOWED"
    assert not memory_file.exists()

    allowed = Toolbox(memory_file=str(memory_file), allow_memory_mutation=True)
    remembered = json.loads(allowed.user_memory(
        action="remember", key="editor", value="Visual Studio Code", category="preference",
    ))
    assert remembered["ok"] is True


def test_memory_recall_remains_available_without_mutation_capability(tmp_path: Path) -> None:
    memory_file: Path = tmp_path / "user_memory.json"
    seeded = Toolbox(memory_file=str(memory_file), allow_memory_mutation=True)
    seeded.user_memory(
        action="remember", key="editor", value="Visual Studio Code", category="preference",
    )
    readonly = Toolbox(memory_file=str(memory_file), allow_memory_mutation=False)
    recalled = json.loads(readonly.user_memory(action="recall", query="editor"))
    assert recalled["preferences"][0]["value"] == "Visual Studio Code"


@pytest.mark.asyncio
async def test_memory_tool_dispatches_through_model_tool_boundary(tmp_path: Path) -> None:
    memory_file: Path = tmp_path / "user_memory.json"
    call: ToolCallDraft = {
        "id": "memory-1", "name": "user_memory",
        "arguments": json.dumps({
            "action": "remember", "key": "editor", "value": "Visual Studio Code",
            "query": None, "category": "preference",
        }),
    }
    result = await main.execute_tool(
        call, Toolbox(memory_file=str(memory_file), allow_memory_mutation=True), {}, lambda event: None, lambda: False,
    )
    assert result["ok"] is True
    assert memory_file.exists()


def test_memory_tool_persists_updates_and_forgets(tmp_path: Path) -> None:
    memory_file: Path = tmp_path / "user_memory.json"
    box: Toolbox = Toolbox(memory_file=str(memory_file), allow_memory_mutation=True)

    remembered = json.loads(box.user_memory(
        action="remember", key="project_root", value="/Users/dogan/Desktop/OmniAgent",
        category="path",
    ))
    assert remembered["record"]["key"] == "project_root"
    assert memory_file.exists()
    assert memory_file.stat().st_mode & 0o777 == 0o600

    box.user_memory(
        action="remember", key="PROJECT_ROOT", value="/tmp/OmniAgent",
        category="path",
    )
    recalled = json.loads(box.user_memory(action="recall", query="omni"))
    assert len(recalled["preferences"]) == 1
    assert recalled["preferences"][0]["value"] == "/tmp/OmniAgent"

    forgotten = json.loads(box.user_memory(action="forget", key="project_root"))
    assert forgotten["removed"] is True
    assert json.loads(box.user_memory(action="recall"))["preferences"] == []


def test_memory_rejects_secrets_without_writing(tmp_path: Path) -> None:
    memory_file: Path = tmp_path / "user_memory.json"
    box: Toolbox = Toolbox(memory_file=str(memory_file), allow_memory_mutation=True)

    with pytest.raises(ToolError) as failure:
        box.user_memory(action="remember", key="api_key", value="sk-test-0123456789012345")
    assert failure.value.code == "MEMORY_INVALID"
    assert not memory_file.exists()


def test_corrupt_memory_is_preserved(tmp_path: Path) -> None:
    memory_file: Path = tmp_path / "user_memory.json"
    memory_file.write_text("{bozuk", encoding="utf-8")
    box: Toolbox = Toolbox(memory_file=str(memory_file), allow_memory_mutation=True)

    with pytest.raises(ToolError) as failure:
        box.user_memory(action="recall")
    assert failure.value.code == "MEMORY_INVALID"
    assert memory_file.read_text(encoding="utf-8") == "{bozuk"


def test_memory_store_is_bounded_and_pure() -> None:
    state: user_memory.MemoryState = user_memory.empty_state()
    for index in range(user_memory.MAX_PREFERENCES + 3):
        state = user_memory.remember_preference(
            state, f"key_{index}", f"value_{index}", "preference", f"2026-01-01T00:00:{index:02d}+00:00",
        )

    assert len(state["preferences"]) == user_memory.MAX_PREFERENCES
    assert state["preferences"][0]["key"] == "key_3"
    assert next(record for record in state["preferences"] if record["key"] == "key_3")["value"] == "value_3"
    assert user_memory.search_preferences(state, "key_39")
