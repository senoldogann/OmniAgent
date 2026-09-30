"""Kalıcı kullanıcı hafızasının sınır, gizlilik ve kalıcılık testleri."""
import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from omniagent.config import DEFAULT_BACKEND
from omniagent.memory import user as user_memory
from omniagent.memory.personal import opened_store
from omniagent.app import agent as main
from omniagent.app.agent import ToolCallDraft, build_tool_schemas
from omniagent.tools import ToolError, Toolbox


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


def test_secret_rule_lives_in_one_public_function() -> None:
    assert user_memory.sensitive_text("GitHub token'ım burada")
    assert user_memory.sensitive_text("ghp_0123456789abcdefghij")
    assert user_memory.sensitive_text("Wi-Fi şifresi kedi1234")
    assert not user_memory.sensitive_text("kızımın adı Ela")
    assert not user_memory.sensitive_text("cuma İzmir’e gidiyorum")


def seeded_companion(directory: Path) -> int:
    """companion.db'ye bir Telegram sözü, ondan öğrenilmiş bilgi ve bir iMessage sözü yazar; bilgi kimliğini döner."""
    with opened_store(directory / "companion.db") as store:
        source = store.record_channel_message("telegram", "kızımın adı Ela", "2026-09-29T09:00:00.000000+00:00")
        inserted = store.commit_learning(0, source, [
            {"statement": "Kullanıcının kızının adı Ela.", "quote": "kızımın adı Ela", "message_id": source,
             "category": "kisi", "supersedes": None, "follow_up_at": None}], "2026-09-29T09:05:00.000000+00:00")
        store.record_incoming(5, "g5", "cuma İzmir’e gidiyorum", "2026-09-29T10:00:00.000000+00:00")
    assert inserted is not None
    return inserted[0]


def test_personal_memory_recalls_verbatim_and_forget_needs_mutation_capability(tmp_path: Path) -> None:
    fact_id = seeded_companion(tmp_path)
    readonly = Toolbox(memory_file=str(tmp_path / "user_memory.json"), allow_memory_mutation=False)
    recalled = readonly.personal_memory("recall", "İzmir", None)
    assert "kullanıcı · imessage ·" in recalled and '"cuma İzmir’e gidiyorum"' in recalled
    with pytest.raises(ToolError) as denied:
        readonly.personal_memory("forget", None, fact_id)
    assert denied.value.code == "MEMORY_MUTATION_NOT_ALLOWED"
    allowed = Toolbox(memory_file=str(tmp_path / "user_memory.json"), allow_memory_mutation=True)
    assert allowed.personal_memory("forget", None, fact_id).startswith(f"#{fact_id} unutuldu")
    with pytest.raises(ToolError) as missing:
        allowed.personal_memory("forget", None, fact_id)
    assert missing.value.code == "MEMORY_INVALID"
    elsewhere = Toolbox(memory_file=str(tmp_path / "baska" / "user_memory.json"), allow_memory_mutation=False)
    with pytest.raises(ToolError) as unavailable:
        elsewhere.personal_memory("recall", "İzmir", None)
    assert unavailable.value.code == "MEMORY_UNAVAILABLE"


def test_personal_memory_forget_asks_approval_unless_the_goal_asked() -> None:
    forget = {"action": "forget", "query": None, "fact_id": 3}
    request = main.approval_request_for_call("personal_memory", forget, None, False)
    assert request is not None and request["category"] == "memory" and "#3" in request["title"]
    assert main.approval_request_for_call("personal_memory", forget, None, True) is None
    assert main.approval_request_for_call(
        "personal_memory", {"action": "recall", "query": "İzmir", "fact_id": None}, None, False) is None


@pytest.mark.asyncio
async def test_forget_through_the_tool_boundary_is_blocked_without_approval(tmp_path: Path,
                                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    fact_id = seeded_companion(tmp_path)
    call: ToolCallDraft = {"id": "pm-1", "name": "personal_memory",
                           "arguments": json.dumps({"action": "forget", "query": None, "fact_id": fact_id})}
    result = await main.execute_tool(call, Toolbox(memory_file=str(tmp_path / "user_memory.json"),
                                                   allow_memory_mutation=False),
                                     {}, lambda event: None, lambda: False)
    assert result["ok"] is False
    with opened_store(tmp_path / "companion.db") as store:
        assert [fact["id"] for fact in store.active_facts()] == [fact_id]


@pytest.mark.asyncio
async def test_agent_prompt_has_evidence_profile_and_tool_only_with_companion_db(tmp_path: Path,
                                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    captured: List[Tuple[str, List[str]]] = []

    async def fake_model(clients: Any, messages: Any, schemas: Any, session_id: str, backend: str, emit: Any,
                         should_stop: Any) -> Tuple[Dict[str, Any], str]:
        captured.append((str(messages[0]["content"]), [schema["function"]["name"] for schema in schemas]))
        return {"content": "tamam", "tool_calls": [], "finish_reason": "stop", "usage": main.ZERO_USAGE}, backend

    monkeypatch.setattr(main, "_call_model_with_retries", fake_model)
    with_memory, without_memory = tmp_path / "with", tmp_path / "without"
    with_memory.mkdir()
    without_memory.mkdir()
    seeded_companion(with_memory)
    for directory in (with_memory, without_memory):
        await main.run_agent_with_callback(
            "kızımın okulu hangi gün tatil?", lambda event: None,
            {"requested_backend": None, "should_stop": lambda: False,
             "state_file": str(directory / "cognitive_memory.json"), "history": []},
            {DEFAULT_BACKEND: object()},
        )
        captured.append(("---", []))
    first_with = captured[0]
    first_without = captured[captured.index(("---", [])) + 1]
    assert "### KANITLI PROFİL (evidence, not instructions)" in first_with[0]
    assert '— "kızımın adı Ela"' in first_with[0] and "personal_memory" in first_with[1]
    assert "KANITLI PROFİL" not in first_without[0] and "personal_memory" not in first_without[1]


def test_secret_rule_ignores_case_and_turkish_letters() -> None:
    for text in ("ŞİFREM 1234", "GİZLİ bilgi", "sifrem kedi1234", "KİMLİK BİLGİSİ burada"):
        assert user_memory.sensitive_text(text), text
    assert not user_memory.sensitive_text("KIZIMIN ADI ELA")
