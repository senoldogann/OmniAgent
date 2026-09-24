"""Eylem isteklerinde başarı kararının gerçek işlem kanıtına dayanması."""
import json
from pathlib import Path
from typing import Any

import pytest

import main
import state_manager as sm
from capabilities import CapabilityService


def _turn(content: str, calls: list[dict[str, str]] | None = None) -> dict[str, Any]:
    return {
        "content": content, "tool_calls": calls or [],
        "finish_reason": "tool_calls" if calls else "stop", "usage": main.ZERO_USAGE,
    }


@pytest.mark.asyncio
async def test_no_tool_action_claim_gets_one_recovery_then_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[list[dict[str, Any]]] = []

    async def fake_model(
        clients: Any, messages: Any, schemas: Any, session_id: str,
        backend: str, emit: Any, should_stop: Any,
    ) -> tuple[dict[str, Any], str]:
        seen.append(list(messages))
        return _turn("Dosya silindi."), backend

    monkeypatch.setattr(main, "_call_model_with_retries", fake_model)
    service = CapabilityService(tmp_path)
    try:
        report = await main.run_agent_with_callback(
            "Masaüstündeki gereksiz dosyayı sil", lambda event: None,
            {"requested_backend": None, "should_stop": lambda: False,
             "state_file": str(tmp_path / "memory.json"), "history": [],
             "integrations": service}, {"ollama-cloud": object()},
        )
    finally:
        await service.close()
    assert not report["success"]
    assert "işlem kanıtı" in report["reason"]
    assert len(seen) == 2
    assert report["metrics"]["tool_calls"] == 0


@pytest.mark.asyncio
async def test_read_only_probe_is_not_mutation_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("koru", encoding="utf-8")
    turns = 0

    async def fake_model(
        clients: Any, messages: Any, schemas: Any, session_id: str,
        backend: str, emit: Any, should_stop: Any,
    ) -> tuple[dict[str, Any], str]:
        nonlocal turns
        turns += 1
        if turns == 1:
            return _turn("", [{
                "id": "read-1", "name": "read_file",
                "arguments": json.dumps({"path": str(target)}),
            }]), backend
        return _turn("Dosya silindi."), backend

    monkeypatch.setattr(main, "_call_model_with_retries", fake_model)
    service = CapabilityService(tmp_path)
    try:
        report = await main.run_agent_with_callback(
            "Bu dosyayı sil", lambda event: None,
            {"requested_backend": None, "should_stop": lambda: False,
             "state_file": str(tmp_path / "memory.json"), "history": [],
             "integrations": service, "max_iterations": 2},
            {"ollama-cloud": object()},
        )
    finally:
        await service.close()
    assert not report["success"]
    assert target.read_text(encoding="utf-8") == "koru"


@pytest.mark.asyncio
async def test_informational_answer_needs_no_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_model(
        clients: Any, messages: Any, schemas: Any, session_id: str,
        backend: str, emit: Any, should_stop: Any,
    ) -> tuple[dict[str, Any], str]:
        return _turn("Bunun için önce API erişimi gerekir."), backend

    monkeypatch.setattr(main, "_call_model_with_retries", fake_model)
    service = CapabilityService(tmp_path)
    try:
        report = await main.run_agent_with_callback(
            "Outlook çöpünü nasıl silerim?", lambda event: None,
            {"requested_backend": None, "should_stop": lambda: False,
             "state_file": str(tmp_path / "memory.json"), "history": [],
             "integrations": service}, {"ollama-cloud": object()},
        )
    finally:
        await service.close()
    assert report["success"]
    assert report["metrics"]["tool_calls"] == 0


@pytest.mark.asyncio
async def test_successful_write_is_action_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "note.txt"
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
                "arguments": json.dumps({"path": str(target), "content": "Tamamlandı\n"}),
            }]), backend
        return _turn("Dosya oluşturuldu."), backend

    monkeypatch.setattr(main, "_call_model_with_retries", fake_model)
    service = CapabilityService(tmp_path)
    try:
        report = await main.run_agent_with_callback(
            "Bir not dosyası oluştur", lambda event: None,
            {"requested_backend": None, "should_stop": lambda: False,
             "state_file": str(tmp_path / "memory.json"), "history": [],
             "integrations": service}, {"ollama-cloud": object()},
        )
    finally:
        await service.close()
    assert report["success"]
    assert target.read_text(encoding="utf-8") == "Tamamlandı\n"


def test_action_intent_ignores_explanatory_questions() -> None:
    assert main.action_execution_expected("Outlook çöpünü sil")
    assert main.action_execution_expected("Google Chrome'u aç")
    assert main.action_execution_expected("Outlook'a git")
    assert not main.action_execution_expected("Outlook çöpünü nasıl silerim?")
    assert not main.action_execution_expected("İkinci monitör nasıl çalışır?")


def test_discovery_and_navigation_do_not_prove_deletion() -> None:
    steps = [
        sm.make_step_record("discover_capabilities", '{"query":"outlook"}', True, "Seçenek bulundu"),
        sm.make_step_record("chrome_active_tab", '{"url":"https://outlook.live.com"}', True, "Açıldı"),
        sm.make_step_record("browse_url", '{"url":"https://outlook.live.com","actions":[]}', True, "Okundu"),
    ]
    assert not main.has_action_evidence("Outlook çöpünü sil", steps)
    assert main.has_action_evidence("Google Chrome'u aç", steps)


@pytest.mark.asyncio
async def test_explicit_failure_answer_is_not_success_even_after_a_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    turns = 0
    target = tmp_path / "probe.txt"

    async def fake_model(
        clients: Any, messages: Any, schemas: Any, session_id: str,
        backend: str, emit: Any, should_stop: Any,
    ) -> tuple[dict[str, Any], str]:
        nonlocal turns
        turns += 1
        if turns == 1:
            return _turn("", [{
                "id": "write-1", "name": "write_file",
                "arguments": json.dumps({"path": str(target), "content": "deneme\n"}),
            }]), backend
        return _turn("Yapamadım: istenen işlem tamamlanamadı."), backend

    monkeypatch.setattr(main, "_call_model_with_retries", fake_model)
    service = CapabilityService(tmp_path)
    try:
        report = await main.run_agent_with_callback(
            "Bir dosya oluştur", lambda event: None,
            {"requested_backend": None, "should_stop": lambda: False,
             "state_file": str(tmp_path / "memory.json"), "history": [],
             "integrations": service}, {"ollama-cloud": object()},
        )
    finally:
        await service.close()
    assert not report["success"]
    assert "başarısız" in report["reason"]


def test_recovered_failure_sentence_is_not_misread_as_final_failure() -> None:
    success, reason = main.final_verdict(
        "İlk deneme başarısızdı ama ikinci yöntemle işlem tamamlandı.", "stop",
    )
    assert success and not reason


def test_polite_action_and_project_repair_open_the_right_tools() -> None:
    assert main.action_execution_expected("Outlook çöp kutusunu boşaltır mısın?")
    assert main.action_execution_expected("Tüm kodu kontrol et eksik varsa tamamla")
    assert main.source_change_expected("Tüm kodu kontrol et eksik varsa tamamla", [])
    assert main.source_change_expected("Bu projeyi hızlandır ve sorunları gider", [])
    names = {schema["function"]["name"] for schema in main.build_tool_schemas(
        "Tüm kodu kontrol et eksik varsa tamamla",
        allow_edit=main.source_change_expected("Tüm kodu kontrol et eksik varsa tamamla", []),
    )}
    assert "edit_file" in names
    assert not main.action_execution_expected("Kod nasıl çalışıyor?")


def test_read_only_shell_probe_is_not_deletion_evidence() -> None:
    read_steps = [sm.make_step_record(
        "execute_shell",
        json.dumps({"command": "ls /tmp; echo bitti", "use_sudo": False, "timeout_seconds": None}),
        True, "STDOUT: bitti",
    )]
    assert not main.has_action_evidence("Bu dosyayı sil", read_steps)
    assert main.has_action_evidence("Dosyaları listele", read_steps)
    write_steps = [sm.make_step_record(
        "execute_shell",
        json.dumps({"command": "rm -f /tmp/deneme.txt", "use_sudo": False, "timeout_seconds": None}),
        True, "Çıkış Kodu: 0",
    )]
    assert main.has_action_evidence("Bu dosyayı sil", write_steps)
    redirected = json.dumps({"command": "echo yeni > /tmp/deneme.txt"})
    assert not main._obviously_read_only_shell(redirected)
