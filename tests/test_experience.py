"""Deneyim belleği: doğrulanmış kurtarmadan ders çıkarma ve yalnız aynı hatada hatırlatma."""
import json
import stat
from pathlib import Path
from typing import Any, Dict, List

import pytest

from omniagent.memory import experience
from omniagent.app import agent as main
from omniagent.integrations.capabilities import CapabilityService
from omniagent.core.events import AgentEvent

TOOL_SCRIPT: str = """#!/bin/sh
case "$*" in
  "ozet kuzey --birim=adet") echo "OZET: 42";;
  "--help") echo "kullanim: veri-araci ozet <bolge> --birim=adet";;
  *) echo "hata: E17 birim eksik" >&2; exit 2;;
esac
"""


def _install_tool(directory: Path) -> Path:
    """Hata mesajı çözümü söylemeyen yerel bir komut satırı aracı kurar."""
    directory.mkdir(parents=True)
    tool: Path = directory / "veri-araci"
    tool.write_text(TOOL_SCRIPT, encoding="utf-8")
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    return tool


def _shell_turn(call_id: str, command: str) -> Dict[str, Any]:
    return {
        "content": "STATE: veri-araci deneniyor",
        "tool_calls": [{"id": call_id, "name": "execute_shell",
                        "arguments": json.dumps({"command": command, "use_sudo": False, "timeout_seconds": None})}],
        "finish_reason": "tool_calls", "usage": main.ZERO_USAGE,
    }


def _final(text: str) -> Dict[str, Any]:
    return {"content": text, "tool_calls": [], "finish_reason": "stop", "usage": main.ZERO_USAGE}


def _last_tool_messages(messages: List[Dict[str, Any]]) -> str:
    """Son asistan turundan sonra modele giden araç sonuçları."""
    tail: List[str] = []
    for message in reversed(messages):
        if message.get("role") == "assistant":
            break
        if message.get("role") == "tool":
            tail.append(str(message["content"]))
    return "\n".join(reversed(tail))


async def _run(goal: str, script: List[Dict[str, Any]], seen: List[str], tmp_path: Path,
               events: List[AgentEvent]) -> main.RunReport:
    """Modeli betikli yanıtlarla taklit eder; araçlar (kabuk, dosya) gerçekten çalışır."""
    turns = iter(script)

    async def fake_model(clients, messages, schemas, session_id, backend, emit, should_stop):
        seen.append(_last_tool_messages(messages))
        return next(turns), backend

    service = CapabilityService(tmp_path / "entegrasyon")
    try:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(main, "_call_model_with_retries", fake_model)
            return await main.run_agent_with_callback(
                goal, events.append,
                {"requested_backend": None, "should_stop": lambda: False,
                 "state_file": str(tmp_path / "memory.json"), "history": [], "integrations": service,
                 "experience_file": str(tmp_path / "deneyim.json")},
                {"opencode": object()},
            )
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_verified_recovery_is_learned_and_reminded_only_on_same_failure(tmp_path: Path) -> None:
    first_tool: Path = _install_tool(tmp_path / "kosu1")
    seen: List[str] = []
    events: List[AgentEvent] = []
    first = await _run("veri-araci ile kuzey özetini bul", [
        _shell_turn("a1", f"{first_tool} ozet kuzey"),
        _shell_turn("a2", f"{first_tool} ozet kuzey"),
        _shell_turn("a3", f"{first_tool} --help"),
        _shell_turn("a4", f"{first_tool} ozet kuzey --birim=adet"),
        _final("OZET: 42"),
    ], seen, tmp_path, events)

    assert first["success"]
    # Aynı hata ikinci kez oluşunca model aynı yaklaşımı tekrarlamaması için uyarılır.
    assert "TEKRARLANAN HATA" not in seen[1] and "TEKRARLANAN HATA" in seen[2]
    lessons = experience.load_experience(str(tmp_path / "deneyim.json"))["lessons"]
    assert len(lessons) == 1
    # Keşif adımı (--help) düzeltme sayılmaz; gerçek değişiklik öğrenilir.
    assert lessons[0]["fixed_call"].endswith("ozet kuzey --birim=adet")
    assert first["metrics"]["experience_candidates"] == 1

    second_tool: Path = _install_tool(tmp_path / "kosu2")
    seen = []
    second = await _run("veri-araci ile kuzey özetini tekrar bul", [
        _shell_turn("b1", f"{second_tool} ozet kuzey"),
        _shell_turn("b2", f"{second_tool} ozet kuzey --birim=adet"),
        _final("OZET: 42"),
    ], seen, tmp_path, events)

    assert second["success"]
    assert "DENEYİM BELLEĞİ" in seen[1] and "--birim=adet" in seen[1]
    assert second["metrics"]["experience_hints"] == 1
    assert any(event["kind"] == "notice" and "Deneyim belleği" in event["text"] for event in events)
    updated = experience.load_experience(str(tmp_path / "deneyim.json"))["lessons"]
    assert (updated[0]["uses"], updated[0]["helped"]) == (1, 1)

    seen = []
    third = await _run("dosyayı oku", [
        {"content": "", "tool_calls": [{"id": "c1", "name": "read_file",
                                        "arguments": json.dumps({"path": str(tmp_path / "yok.txt")})}],
         "finish_reason": "tool_calls", "usage": main.ZERO_USAGE},
        _final("dosya yok"),
    ], seen, tmp_path, events)
    # Farklı araç/hata: ders hatırlatılmaz, başarılı yol etkilenmez.
    assert third["metrics"]["experience_hints"] == 0 and "DENEYİM BELLEĞİ" not in seen[1]


def test_failed_task_does_not_learn_and_unhelpful_lesson_is_pruned() -> None:
    def shell(command: str) -> str:
        return json.dumps({"command": command, "use_sudo": False, "timeout_seconds": None})

    failure: str = "ToolError: Kabuk komutu başarısız: çıkış=2, stdout=, stderr=hata: E17"
    tracker = experience.new_tracker()
    tracker, _ = experience.observe_result(experience.empty_state(), tracker, "execute_shell",
                                           shell("/a/veri-araci ozet"), False, failure, 1)
    tracker, _ = experience.observe_result(experience.empty_state(), tracker, "execute_shell",
                                           shell("/a/veri-araci ozet --birim=adet"), True, "ok", 2)
    assert experience.finish_task(experience.empty_state(), tracker, False, "t0") == experience.empty_state()
    learned = experience.finish_task(experience.empty_state(), tracker, True, "t0")
    assert len(learned["lessons"]) == 1

    state = learned
    for attempt in range(experience.PRUNE_MIN_USES):
        task = experience.new_tracker()
        task, observed = experience.observe_result(state, task, "execute_shell",
                                                   shell("/b/veri-araci ozet"), False, failure, 1)
        assert observed["lesson_id"] is not None
        task, _ = experience.observe_result(state, task, "execute_shell",
                                            shell("/b/veri-araci ozet --birim=adet"), False, failure, 2)
        state = experience.finish_task(state, task, False, f"t{attempt + 1}")
    # Üç hatırlatmada da işe yaramayan ders silinir.
    assert state["lessons"] == []


def test_lesson_file_masks_secrets(tmp_path: Path) -> None:
    command: str = "curl -H 'Authorization: Bearer abcdefghijklmnop123456' https://api.example.com/v1/items"
    candidate = experience.pair_candidate(
        "execute_shell", json.dumps({"command": command, "use_sudo": False, "timeout_seconds": None}),
        "ToolError: HTTP 401", json.dumps({"command": command + " --fail", "use_sudo": False, "timeout_seconds": None}),
    )
    assert candidate is not None
    path: Path = tmp_path / "deneyim.json"
    experience.save_experience(str(path), experience.merge_candidates(experience.empty_state(), [candidate], "t"))
    stored: str = path.read_text(encoding="utf-8")
    assert "abcdefghijklmnop123456" not in stored
    assert path.stat().st_mode & 0o777 == 0o600
