"""Silme fiillerinin eylem kanıtı ve açık yol kontrolüne yönlenmesi."""
import json
from pathlib import Path
from typing import Any

import pytest

from omniagent.app import agent as main
from omniagent.app import policy
from omniagent.core import state as sm
from omniagent.integrations.capabilities import CapabilityService


@pytest.mark.parametrize("verb", ("remove", "kaldır"))
def test_removal_verbs_open_action_and_deletion_gates(verb: str) -> None:
    goal = f"{verb}: /tmp/hedef.txt"
    assert policy.action_execution_expected(goal)
    assert policy.explicit_deletion_target(goal) == Path("/tmp/hedef.txt")
    status = sm.make_step_record(
        "execute_shell", json.dumps({"command": "git status --short"}), True, "Çıkış Kodu: 0",
    )
    assert not policy.has_action_evidence(goal, [status])


def test_polite_removal_request_opens_action_gate() -> None:
    assert policy.action_execution_expected("How about you remove /tmp/hedef.txt?")
    assert policy.action_execution_expected(
        "How can you remove /tmp/hedef.txt for me? Please do it now."
    )


@pytest.mark.parametrize("goal", (
    "How to remove a file?",
    "How can I remove /tmp/hedef.txt?",
    "How do I remove /tmp/hedef.txt? Please explain.",
))
def test_english_method_question_does_not_require_execution(goal: str) -> None:
    assert not policy.action_execution_expected(goal)
    assert policy.explicit_deletion_target(goal) is None


@pytest.mark.parametrize("goal", (
    "How to remove /tmp/hedef.txt? Please remove it now.",
    "How can I remove /tmp/hedef.txt? Could you remove it for me now?",
    "How to remove /tmp/hedef.txt? Please do it now.",
))
def test_method_question_followed_by_removal_requires_execution(goal: str) -> None:
    assert policy.action_execution_expected(goal)
    assert policy.explicit_deletion_target(goal) == Path("/tmp/hedef.txt")


def test_method_question_followed_by_open_checks_only_open_action() -> None:
    goal = "How to remove /tmp/hedef.txt? Please open the terminal."
    opened = sm.make_step_record("cua_get_app", "{}", True, "Terminal önde")
    assert policy.action_execution_expected(goal)
    assert policy.explicit_deletion_target(goal) is None
    assert policy.has_action_evidence(goal, [opened])


def test_repeated_path_in_method_and_command_is_one_deletion_target() -> None:
    goal = "How to remove /tmp/hedef.txt? Please remove /tmp/hedef.txt now."
    assert policy.explicit_deletion_target(goal) == Path("/tmp/hedef.txt")


def test_unrelated_file_reference_does_not_bind_method_path() -> None:
    goal = "How to remove /tmp/hedef.txt? Please remove the other file."
    assert policy.action_execution_expected(goal)
    assert policy.explicit_deletion_target(goal) is None


@pytest.mark.parametrize("goal", (
    "How to remove /tmp/a from README? Please remove /tmp/a from README now.",
    "How to remove /tmp/a from README? Please remove it now.",
    "How to remove /tmp/a? Please remove it from the list.",
    "Remove /tmp/a from README.",
))
def test_removing_a_reference_does_not_require_file_deletion(goal: str) -> None:
    assert policy.action_execution_expected(goal)
    assert policy.explicit_deletion_target(goal) is None


def test_file_at_path_is_an_explicit_deletion_target() -> None:
    goal = "How to remove /tmp/a? Please remove the file at /tmp/a now."
    assert policy.action_execution_expected(goal)
    assert policy.explicit_deletion_target(goal) == Path("/tmp/a")


@pytest.mark.parametrize("goal", (
    "Remove /tmp/a from disk.",
    "How to remove /tmp/a from this machine? Please remove it now.",
))
def test_removing_file_from_local_storage_checks_path(goal: str) -> None:
    assert policy.action_execution_expected(goal)
    assert policy.explicit_deletion_target(goal) == Path("/tmp/a")


@pytest.mark.parametrize("verb", ("remove", "kaldır"))
@pytest.mark.parametrize("repeated_method_path", (False, True))
@pytest.mark.asyncio
async def test_unrelated_write_cannot_prove_removal_verb(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str,
    repeated_method_path: bool,
) -> None:
    target = tmp_path / "hedef.txt"
    target.write_text("koru", encoding="utf-8")
    other = tmp_path / "not.txt"
    turns = 0
    events: list[dict[str, Any]] = []

    async def fake_model(
        clients: Any, messages: Any, schemas: Any, session_id: str,
        backend: str, emit: Any, should_stop: Any,
    ) -> tuple[dict[str, Any], str]:
        nonlocal turns
        turns += 1
        if turns == 1:
            return {
                "content": "", "tool_calls": [{
                    "id": "write-1", "name": "write_file",
                    "arguments": json.dumps({"path": str(other), "content": "hazır\n"}),
                }], "finish_reason": "tool_calls", "usage": main.ZERO_USAGE,
            }, backend
        emit({"kind": "text_delta", "text": "Hedef silindi."})
        return {
            "content": "Hedef silindi.", "tool_calls": [],
            "finish_reason": "stop", "usage": main.ZERO_USAGE,
        }, backend

    monkeypatch.setattr(main, "_call_model_with_retries", fake_model)
    service = CapabilityService(tmp_path)
    try:
        goal = (
            f"How to {verb} `{target}`? Please {verb} `{target}` now."
            if repeated_method_path else f"{verb}: `{target}`"
        )
        report = await main.run_agent_with_callback(
            goal, events.append,
            {"requested_backend": None, "should_stop": lambda: False,
             "state_file": str(tmp_path / "memory.json"), "history": [],
             "integrations": service}, {"ollama-cloud": object()},
        )
    finally:
        await service.close()
    assert turns == 3
    assert target.read_text(encoding="utf-8") == "koru"
    assert not report["success"]
    assert report["outcome"].startswith("Doğrulanmadı:")
    assert "Hedef silindi." not in str(events)
