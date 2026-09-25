"""Kendi kendine kurtarma sinyalleri, kullanıcı hafızası bağlamı ve yarım görevin sürdürülebilmesi."""
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any, Dict, List

import pytest

from omniagent.app import agent as main
from omniagent.core import state as sm
from omniagent.memory import user as user_memory
from omniagent.integrations.capabilities import CapabilityService
from omniagent.tools import ToolError, Toolbox


class ErrorHandler(BaseHTTPRequestHandler):
    """Hatanın nedenini gövdede açıklayan bir API gibi davranır."""

    def do_GET(self) -> None:
        body: bytes = json.dumps({"error": "format parametresi gerekli: ?format=v2"}).encode()
        self.send_response(400)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def test_fetch_error_includes_response_body() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), ErrorHandler)
    Thread(target=server.serve_forever, daemon=True).start()
    try:
        with pytest.raises(ToolError) as error:
            Toolbox().fetch_raw(f"http://127.0.0.1:{server.server_port}/rapor")
    finally:
        server.shutdown()
    assert error.value.code == "FETCH_FAILED"
    assert "format parametresi gerekli" in str(error.value)


def test_shell_timeout_returns_partial_output_and_accepts_longer_limit() -> None:
    started: float = time.monotonic()
    with pytest.raises(ToolError) as error:
        Toolbox().execute_shell("echo basladi; sleep 5; echo bitti", False, 1)
    assert error.value.code == "SHELL_TIMEOUT"
    assert "basladi" in str(error.value) and "bitti" not in str(error.value)
    assert time.monotonic() - started < 4
    assert "tamam" in Toolbox().execute_shell("sleep 1; echo tamam", False, 3)
    with pytest.raises(ToolError) as invalid:
        Toolbox().execute_shell("echo x", False, 5000)
    assert invalid.value.code == "INVALID_TIMEOUT"


async def _scripted_run(goal: str, script: List[Dict[str, Any]], systems: List[str], tmp_path: Path,
                        max_iterations: int) -> main.RunReport:
    turns = iter(script)

    async def fake_model(clients, messages, schemas, session_id, backend, emit, should_stop):
        systems.append(str(messages[0]["content"]))
        return next(turns), backend

    service = CapabilityService(tmp_path / "entegrasyon")
    try:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(main, "_call_model_with_retries", fake_model)
            return await main.run_agent_with_callback(
                goal, lambda event: None,
                {"requested_backend": None, "should_stop": lambda: False,
                 "state_file": str(tmp_path / "memory.json"), "history": [], "integrations": service,
                 "max_iterations": max_iterations},
                {"opencode": object()},
            )
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_saved_user_memory_reaches_model_and_history_is_searchable(tmp_path: Path) -> None:
    memory_file: Path = tmp_path / "user_memory.json"
    user_memory.save_memory(str(memory_file), user_memory.remember_preference(
        user_memory.empty_state(), "rapor_klasoru", "/tmp/raporlar", "path", "2026-09-24T10:00:00+00:00",
    ))
    systems: List[str] = []
    first = await _scripted_run("Linkedin iş ilanlarını araştır", [
        {"content": "5 ilan bulundu", "tool_calls": [], "finish_reason": "stop", "usage": main.ZERO_USAGE},
    ], systems, tmp_path, 5)
    assert first["success"]
    assert "### USER MEMORY (saved by the user)" in systems[0]
    assert "[path] rapor_klasoru: /tmp/raporlar" in systems[0]

    history: Dict[str, Any] = json.loads(
        Toolbox(history_file=str(tmp_path / "memory.json")).user_memory(action="history", query="linkedin ilanları")
    )
    assert history["episodes"][0]["goal"] == "Linkedin iş ilanlarını araştır"
    assert history["episodes"][0]["outcome"] == "5 ilan bulundu"


def test_episode_search_ranks_by_turkish_stems() -> None:
    state: sm.StateDict = {"episodic_memory": []}
    metrics: sm.EpisodeMetrics = {"turns": 1, "tool_calls": 0, "elapsed_seconds": 1.0, "backend": "x",
                                  "prompt_tokens": 0, "cached_tokens": 0, "completion_tokens": 0}
    for goal in ("Masaüstündeki PDF'leri listele", "Raporları Documents klasörüne kaydet", "Hava durumu"):
        state = sm.record_episode(state, goal, [], "tamam", True, metrics)
    found = sm.search_episodes(state, "rapor klasörü", 5)
    assert [item["goal"] for item in found] == ["Raporları Documents klasörüne kaydet"]
    assert len(sm.search_episodes(state, "", 2)) == 2


@pytest.mark.asyncio
async def test_unfinished_task_keeps_state_ledger_for_continue(tmp_path: Path) -> None:
    probe: Path = tmp_path / "probe.txt"
    probe.write_text("ok", encoding="utf-8")
    ledger: str = "STATE:\nFACTS: aday1=uygun, aday2=elendi\nREMAINING: aday3 kontrolü, rapor yazımı"
    script = [{
        "content": ledger,
        "tool_calls": [{"id": f"r{index}", "name": "read_file", "arguments": json.dumps({"path": str(probe)})}],
        "finish_reason": "tool_calls", "usage": main.ZERO_USAGE,
    } for index in range(3)]
    report = await _scripted_run("Adayları kontrol edip rapor yaz", script, [], tmp_path, 2)
    assert not report["success"]
    answer: str = report["exchange"]["answer"]
    assert answer.startswith("[Görev tamamlanamadı:")
    assert "REMAINING: aday3 kontrolü, rapor yazımı" in answer
