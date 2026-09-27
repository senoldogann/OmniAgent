"""Yerel dosya tesliminde araç başarısı ile hedefin gerçek son durumunu ayırır."""
import json
from pathlib import Path
from typing import Any

import pytest

from omniagent.app import agent as main
from omniagent.app.file_delivery import capture_file_contract, receipt_for_call
from omniagent.integrations.capabilities import CapabilityService


async def _run_calls(
    goal: str, calls: list[tuple[str, dict[str, Any]]],
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    turns = 0
    events: list[dict[str, Any]] = []

    async def fake_model(
        clients: Any, messages: Any, schemas: Any, session_id: str,
        backend: str, emit: Any, should_stop: Any,
    ) -> tuple[dict[str, Any], str]:
        nonlocal turns
        turns += 1
        if turns == 1 and calls:
            if any(name == "edit_file" for name, _ in calls):
                assert "edit_file" in {schema["function"]["name"] for schema in schemas}
            return {
                "content": "",
                "tool_calls": [
                    {"id": f"call-{index}", "name": name,
                     "arguments": json.dumps(arguments)}
                    for index, (name, arguments) in enumerate(calls)
                ],
                "finish_reason": "tool_calls", "usage": main.ZERO_USAGE,
            }, backend
        emit({"kind": "text_delta", "text": "Görev tamamlandı."})
        return {
            "content": "Görev tamamlandı.", "tool_calls": [],
            "finish_reason": "stop", "usage": main.ZERO_USAGE,
        }, backend

    monkeypatch.setattr(main, "_call_model_with_retries", fake_model)
    service = CapabilityService(tmp_path)
    try:
        report = await main.run_agent_with_callback(
            goal, events.append,
            {"requested_backend": None, "should_stop": lambda: False,
             "state_file": str(tmp_path / "memory.json"), "history": [],
             "integrations": service}, {"ollama-cloud": object()},
        )
    finally:
        await service.close()
    return report, events


@pytest.mark.asyncio
async def test_missing_file_rm_f_is_not_claimed_as_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "yok.txt"
    report, events = await _run_calls(
        f"sil: `{target}`", [("execute_shell", {"command": f"rm -f '{target}'"})],
        tmp_path, monkeypatch,
    )
    assert not report["success"]
    assert report["outcome"].startswith("Doğrulanmadı:")
    assert "Görev tamamlandı." not in str(events)


@pytest.mark.asyncio
async def test_one_of_two_files_deleted_is_not_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second = tmp_path / "bir.txt", tmp_path / "iki.txt"
    first.write_text("bir", encoding="utf-8")
    second.write_text("iki", encoding="utf-8")
    report, _ = await _run_calls(
        f"sil: `{first}` ve `{second}`",
        [("execute_shell", {"command": f"rm -f '{first}'"})],
        tmp_path, monkeypatch,
    )
    assert not report["success"]
    assert not first.exists()
    assert second.read_text(encoding="utf-8") == "iki"


@pytest.mark.asyncio
async def test_pathless_file_delete_is_not_proved_by_unrelated_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    other = tmp_path / "not.txt"
    report, _ = await _run_calls(
        "Bu dosyayı sil", [("write_file", {"path": str(other), "content": "alakasız"})],
        tmp_path, monkeypatch,
    )
    assert other.read_text(encoding="utf-8") == "alakasız"
    assert not report["success"]
    assert report["metrics"]["turns"] == 2


@pytest.mark.asyncio
async def test_removing_move_source_does_not_prove_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, destination = tmp_path / "kaynak.txt", tmp_path / "hedef.txt"
    source.write_text("kaynak", encoding="utf-8")
    destination.write_text("eski", encoding="utf-8")
    report, _ = await _run_calls(
        f"move `{source}` to `{destination}`",
        [("execute_shell", {"command": f"rm -f '{source}'"})],
        tmp_path, monkeypatch,
    )
    assert not report["success"]
    assert destination.read_text(encoding="utf-8") == "eski"


@pytest.mark.asyncio
async def test_same_content_write_is_not_a_completed_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "not.txt"
    target.write_text("aynı", encoding="utf-8")
    report, _ = await _run_calls(
        f"edit `{target}`", [("write_file", {"path": str(target), "content": "aynı"})],
        tmp_path, monkeypatch,
    )
    assert not report["success"]


@pytest.mark.asyncio
async def test_two_relative_files_deleted_with_matching_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    first, second = tmp_path / "bir.txt", tmp_path / "iki.txt"
    first.write_text("bir", encoding="utf-8")
    second.write_text("iki", encoding="utf-8")
    report, _ = await _run_calls(
        "sil: ./bir.txt ve ./iki.txt",
        [("execute_shell", {"command": "rm -f ./bir.txt ./iki.txt"})],
        tmp_path, monkeypatch,
    )
    assert report["success"]
    assert not first.exists() and not second.exists()


@pytest.mark.asyncio
async def test_move_file_into_directory_checks_effective_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, directory = tmp_path / "kaynak.txt", tmp_path / "arsiv"
    source.write_text("taşınan içerik", encoding="utf-8")
    directory.mkdir()
    report, _ = await _run_calls(
        f"move `{source}` to `{directory}`",
        [("execute_shell", {"command": f"mv '{source}' '{directory}'"})],
        tmp_path, monkeypatch,
    )
    assert report["success"]
    assert not source.exists()
    assert (directory / source.name).read_text(encoding="utf-8") == "taşınan içerik"


@pytest.mark.asyncio
async def test_moved_symlink_is_verified_as_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    referent = tmp_path / "gercek.txt"
    referent.write_text("hedef", encoding="utf-8")
    source, destination = tmp_path / "bag.txt", tmp_path / "tasinan-bag.txt"
    source.symlink_to(referent)
    report, _ = await _run_calls(
        f"move `{source}` to `{destination}`",
        [("execute_shell", {"command": f"mv '{source}' '{destination}'"})],
        tmp_path, monkeypatch,
    )
    assert report["success"]
    assert not source.is_symlink()
    assert destination.is_symlink()
    assert destination.readlink() == referent


@pytest.mark.asyncio
async def test_edit_target_requires_changed_content_and_matching_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "not.txt"
    target.write_text("eski", encoding="utf-8")
    report, _ = await _run_calls(
        f"edit `{target}`",
        [("write_file", {"path": str(target), "content": "yeni" + "x" * 500})],
        tmp_path, monkeypatch,
    )
    assert report["success"]
    assert target.read_text(encoding="utf-8").startswith("yeni")


@pytest.mark.asyncio
async def test_ordinary_text_edit_routes_edit_file_and_verifies_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "not.txt"
    target.write_text("eski satır\n", encoding="utf-8")
    report, _ = await _run_calls(
        f"`{target}` dosyasını düzenle",
        [("edit_file", {"path": str(target), "old_text": "eski", "new_text": "yeni"})],
        tmp_path, monkeypatch,
    )
    assert report["success"]
    assert target.read_text(encoding="utf-8") == "yeni satır\n"


@pytest.mark.asyncio
async def test_move_to_archive_is_not_a_delete_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target, archive = tmp_path / "hedef.txt", tmp_path / "arsiv.txt"
    target.write_text("koru", encoding="utf-8")
    report, _ = await _run_calls(
        f"sil: `{target}`",
        [("execute_shell", {"command": f"mv '{target}' '{archive}'"})],
        tmp_path, monkeypatch,
    )
    assert not report["success"]
    assert archive.read_text(encoding="utf-8") == "koru"


@pytest.mark.asyncio
async def test_symlink_edit_is_unverified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    referent = tmp_path / "gercek.txt"
    referent.write_text("eski", encoding="utf-8")
    alias = tmp_path / "bag.txt"
    alias.symlink_to(referent)
    report, _ = await _run_calls(
        f"edit `{alias}`",
        [("write_file", {"path": str(alias), "content": "yeni"})],
        tmp_path, monkeypatch,
    )
    assert not report["success"]


@pytest.mark.asyncio
async def test_readme_reference_removal_is_not_file_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    other = tmp_path / "not.txt"
    report, events = await _run_calls(
        "Remove /tmp/a from README",
        [("write_file", {"path": str(other), "content": "alakasız"})],
        tmp_path, monkeypatch,
    )
    assert not report["success"]
    assert "Görev tamamlandı." not in str(events)
    assert report["metrics"]["turns"] == 2


@pytest.mark.asyncio
async def test_method_question_only_does_not_create_file_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    goal = "How can I delete ./not.txt?"
    assert capture_file_contract(goal, tmp_path) is None
    report, _ = await _run_calls(goal, [], tmp_path, monkeypatch)
    assert report["success"]


@pytest.mark.asyncio
async def test_method_question_followed_by_delete_uses_question_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "not.txt"
    target.write_text("sil", encoding="utf-8")
    report, _ = await _run_calls(
        "How can I delete ./not.txt? Please delete it now.",
        [("execute_shell", {"command": "rm -f ./not.txt"})],
        tmp_path, monkeypatch,
    )
    assert report["success"]
    assert not target.exists()


def test_shell_receipt_distinguishes_delete_move_and_expansion(tmp_path: Path) -> None:
    def call(command: str) -> dict[str, str]:
        return {"id": "1", "name": "execute_shell",
                "arguments": json.dumps({"command": command})}

    success: dict[str, Any] = {"tool_call_id": "1", "ok": True, "result": "Çıkış Kodu: 0"}
    deleted = receipt_for_call(call("rm -f ./a.txt"), success, tmp_path)
    moved = receipt_for_call(call("mv ./a.txt ./b.txt"), success, tmp_path)
    assert deleted[0]["kind"] == "delete"
    assert moved[0]["kind"] == "move"
    assert not receipt_for_call(call("rm -f $TARGET"), success, tmp_path)
    assert not receipt_for_call(call("rm -f ./a.txt; echo bitti"), success, tmp_path)


def test_web_update_is_not_treated_as_local_file_edit(tmp_path: Path) -> None:
    assert capture_file_contract("Update https://example.com/api/v1.2", tmp_path) is None
    reference = capture_file_contract("Remove /tmp/a from README", tmp_path)
    assert reference is not None and reference["kind"] == "unsupported"
