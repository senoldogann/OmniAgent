"""Açık yerel yol silme hedefinde araç başarısı ve gerçek son durum ayrımı."""
import json
from pathlib import Path
import shlex
from typing import Any

import pytest

from omniagent.app import agent as main
from omniagent.app import policy
from omniagent.integrations.capabilities import CapabilityService


def _turn(content: str, calls: list[dict[str, str]] | None = None) -> dict[str, Any]:
    return {
        "content": content,
        "tool_calls": calls or [],
        "finish_reason": "tool_calls" if calls else "stop",
        "usage": main.ZERO_USAGE,
    }


@pytest.mark.parametrize("goal_template", (
    "`{target}` dosyasını sil", "sil: {target})", "sil: {target}.",
))
@pytest.mark.asyncio
async def test_unrelated_write_cannot_prove_explicit_file_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, goal_template: str,
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
            return _turn("", [{
                "id": "write-1", "name": "write_file",
                "arguments": json.dumps({"path": str(other), "content": "hazır\n"}),
            }]), backend
        emit({"kind": "text_delta", "text": "Hedef dosya silindi."})
        return _turn("Hedef dosya silindi."), backend

    monkeypatch.setattr(main, "_call_model_with_retries", fake_model)
    service = CapabilityService(tmp_path)
    try:
        report = await main.run_agent_with_callback(
            goal_template.format(target=target), events.append,
            {"requested_backend": None, "should_stop": lambda: False,
             "state_file": str(tmp_path / "memory.json"), "history": [],
             "integrations": service}, {"ollama-cloud": object()},
        )
    finally:
        await service.close()
    assert target.read_text(encoding="utf-8") == "koru"
    assert other.read_text(encoding="utf-8") == "hazır\n"
    assert turns == 3
    assert not report["success"]
    assert report["outcome"].startswith("Doğrulanmadı:")
    assert "Hedef dosya silindi." not in str(events)
    assert "Hedef dosya silindi." not in report["exchange"]["answer"]


def test_only_one_adjacent_absolute_deletion_target_is_selected(tmp_path: Path) -> None:
    target = tmp_path / "hedef.txt"
    assert policy.explicit_deletion_target(f"sil: {target}") == target
    assert policy.explicit_deletion_target(f"sil \"{target}\"") == target
    assert policy.explicit_deletion_target(f"`{target}` dosyasını sil") == target
    assert policy.explicit_deletion_target(f"delete {target}") == target
    assert policy.explicit_deletion_target(f"sil ({target})") == target
    assert policy.explicit_deletion_target(f"sil {target}.") == target
    assert policy.explicit_deletion_target(f"sil: {target} ve /tmp/diger.txt") is None
    assert policy.explicit_deletion_target("sil: goreli.txt") is None
    assert policy.explicit_deletion_target("sil: https://example.com/a") is None
    assert policy.explicit_deletion_target(f"Bu dosyayı sil ve sonucu {target} yoluna yaz") is None


def test_broken_symlink_and_stat_error_do_not_prove_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "bag"
    goal = f"sil: `{target}`"
    target.symlink_to(tmp_path / "olmayan")
    assert policy.unmet_explicit_deletion(goal) is not None
    target.unlink()
    assert policy.unmet_explicit_deletion(goal) is None

    original_lstat = Path.lstat

    def denied_lstat(path: Path) -> Any:
        if path == target:
            raise PermissionError("izin yok")
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", denied_lstat)
    assert policy.unmet_explicit_deletion(goal) is not None


def test_literal_trailing_punctuation_path_is_also_checked(tmp_path: Path) -> None:
    target = tmp_path / "hedef."
    target.write_text("koru", encoding="utf-8")
    assert policy.unmet_explicit_deletion(f"sil: {target}") is not None
    assert policy.unmet_explicit_deletion(f"sil: {target})") is not None


@pytest.mark.asyncio
async def test_recovery_can_remove_explicit_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "hedef.txt"
    target.write_text("sil", encoding="utf-8")
    other = tmp_path / "not.txt"
    turns = 0

    async def fake_model(
        clients: Any, messages: Any, schemas: Any, session_id: str,
        backend: str, emit: Any, should_stop: Any,
    ) -> tuple[dict[str, Any], str]:
        nonlocal turns
        turns += 1
        if turns == 1:
            return _turn("", [{
                "id": "write-1", "name": "write_file",
                "arguments": json.dumps({"path": str(other), "content": "hazır\n"}),
            }]), backend
        if turns == 3:
            return _turn("", [{
                "id": "delete-1", "name": "execute_shell",
                "arguments": json.dumps({"command": f"rm -f {shlex.quote(str(target))}"}),
            }]), backend
        return _turn("Hedef dosya silindi."), backend

    monkeypatch.setattr(main, "_call_model_with_retries", fake_model)
    service = CapabilityService(tmp_path)
    try:
        report = await main.run_agent_with_callback(
            f"sil: `{target}`", lambda event: None,
            {"requested_backend": None, "should_stop": lambda: False,
             "state_file": str(tmp_path / "memory.json"), "history": [],
             "integrations": service}, {"ollama-cloud": object()},
        )
    finally:
        await service.close()
    assert turns == 4
    assert not target.exists()
    assert report["success"]


@pytest.mark.asyncio
async def test_no_evidence_then_unrelated_write_shares_one_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "hedef.txt"
    target.write_text("koru", encoding="utf-8")
    other = tmp_path / "not.txt"
    turns = 0

    async def fake_model(
        clients: Any, messages: Any, schemas: Any, session_id: str,
        backend: str, emit: Any, should_stop: Any,
    ) -> tuple[dict[str, Any], str]:
        nonlocal turns
        turns += 1
        if turns == 2:
            return _turn("", [{
                "id": "write-1", "name": "write_file",
                "arguments": json.dumps({"path": str(other), "content": "hazır\n"}),
            }]), backend
        return _turn("Hedef dosya silindi."), backend

    monkeypatch.setattr(main, "_call_model_with_retries", fake_model)
    service = CapabilityService(tmp_path)
    try:
        report = await main.run_agent_with_callback(
            f"sil: `{target}`", lambda event: None,
            {"requested_backend": None, "should_stop": lambda: False,
             "state_file": str(tmp_path / "memory.json"), "history": [],
             "integrations": service}, {"ollama-cloud": object()},
        )
    finally:
        await service.close()
    assert turns == 3
    assert target.exists()
    assert not report["success"]
    assert report["outcome"].startswith("Doğrulanmadı:")
