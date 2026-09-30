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
                {"requested_backend": "opencode", "should_stop": lambda: False,
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


def _messages_with_turns(turn_count: int) -> List[Dict[str, Any]]:
    """Sistem istemi + hedef + N araçlı asistan turundan oluşan mesaj listesi kurar."""
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": "sistem istemi"},
        {"role": "user", "content": "hedef"},
    ]
    for index in range(turn_count):
        messages.append({"role": "assistant", "content": f"asistan {index}", "tool_calls": []})
        messages.append({"role": "tool", "tool_call_id": f"t{index}", "content": f"çıktı {index}"})
    return messages


def test_compact_for_overflow_keeps_system_goal_and_last_turns() -> None:
    messages: List[Dict[str, Any]] = _messages_with_turns(6)
    compacted: List[Dict[str, Any]] = main._compact_for_overflow(messages)
    assert compacted[0] == messages[0]
    assert compacted[1] == messages[1]
    assert compacted[2]["role"] == "user"
    assert "BAĞLAM KOMPAKTLAMA" in compacted[2]["content"]
    assert "4 araç turu" in compacted[2]["content"]
    # Son iki asistan turu (araç mesajlarıyla) korunur.
    assert any(message.get("content") == "asistan 5" for message in compacted)
    assert any(message.get("content") == "asistan 4" for message in compacted)
    assert not any(message.get("content") == "asistan 3" for message in compacted)
    assert len(messages) == 14  # girdi değişmez


def test_compact_for_overflow_returns_same_list_when_short() -> None:
    messages: List[Dict[str, Any]] = _messages_with_turns(2)
    assert main._compact_for_overflow(messages) is messages


class _FakeRuntime:
    """_escalation_backend'in okuduğu alanları taşıyan yalın sahte çalışma anı."""

    def __init__(self, allowed: frozenset[str], images: bool = True) -> None:
        self.fallback_backends: frozenset[str] = allowed
        self.fallback_images: bool = images
        self.blocked_backends: set[str] = set()
        self.backend_cooldowns: Dict[str, float] = {}


def test_escalation_backend_respects_fallback_permission() -> None:
    available: frozenset[str] = frozenset({"ollama-cloud", "openai", "openrouter"})
    allowed: _FakeRuntime = _FakeRuntime(frozenset({"openai", "openrouter"}))
    assert main._escalation_backend("ollama-cloud", available, allowed, 0) == "openai"
    # Görüntülü istekte görüntü izni yoksa geçiş yapılmaz.
    images_denied: _FakeRuntime = _FakeRuntime(frozenset({"openai", "openrouter"}), images=False)
    assert main._escalation_backend("ollama-cloud", available, images_denied, 1) is None
    # İzin verilmeyen sağlayıcıya asla geçilmez.
    assert main._escalation_backend("ollama-cloud", available, _FakeRuntime(frozenset()), 0) is None
    # Merdivenin son basamağından öteye geçilmez.
    assert main._escalation_backend("openrouter", available, allowed, 0) is None


def test_escalation_backend_skips_blocked_and_cooling_profiles() -> None:
    available: frozenset[str] = frozenset({"ollama-cloud", "openai", "openrouter"})
    blocked: _FakeRuntime = _FakeRuntime(frozenset({"openai", "openrouter"}))
    blocked.blocked_backends.add("openai")
    assert main._escalation_backend("ollama-cloud", available, blocked, 0) == "openrouter"
    cooling: _FakeRuntime = _FakeRuntime(frozenset({"openai", "openrouter"}))
    cooling.backend_cooldowns["openai"] = time.monotonic() + 60.0
    assert main._escalation_backend("ollama-cloud", available, cooling, 0) == "openrouter"


def test_relevant_episodes_only_returns_strong_overlap() -> None:
    state: sm.StateDict = {"episodic_memory": []}
    metrics: sm.EpisodeMetrics = {"turns": 1, "tool_calls": 0, "elapsed_seconds": 1.0, "backend": "x",
                                  "prompt_tokens": 0, "cached_tokens": 0, "completion_tokens": 0}
    for goal in ("Linkedin ilanlarını topla", "Hava durumu", "Linkedin profilini güncelle"):
        state = sm.record_episode(state, goal, [], "tamam", True, metrics)
    found = sm.relevant_episodes(state, "Linkedin ilanlarını araştır", 2, 2)
    assert [item["goal"] for item in found] == ["Linkedin ilanlarını topla"]
    # Örtüşme yoksa hiçbir görev ipucu dönmez (hedef sapması koruması).
    assert sm.relevant_episodes(state, "Excel tablosu hazırla", 2, 2) == []
    assert sm.relevant_episodes(state, "Linkedin ilanlarını araştır", 2, 99) == []


def test_episodic_hint_text_frames_past_tasks_as_context() -> None:
    hints: List[sm.EpisodeSummary] = [
        {"timestamp": "2026-09-20T10:00:00+00:00", "goal": "Linkedin ilanlarını topla",
         "success": True, "outcome": "12 ilan kaydedildi"},
    ]
    text: str = sm.episodic_hint_text(hints)
    assert "HOST — BENZER GEÇMİŞ GÖREVLER" in text
    assert "başarılı: 12 ilan kaydedildi" in text
    assert sm.episodic_hint_text([]) == ""


@pytest.mark.asyncio
async def test_screen_observation_cache_reuses_encoded_image_on_return(tmp_path: Path) -> None:
    """Aynı ekrana dönüşte (A→B→A) görüntü yeniden kodlanmaz: içerik adresli önbellekten döner."""
    image: Path = tmp_path / f"ekran-{time.monotonic_ns()}.png"
    image.write_bytes(b"sahne-icerik")
    encodes: List[int] = []

    def fake_encode(path: str, detail: bool = True):
        encodes.append(1)
        return (f"kare-{detail}", None if not detail else "referans", (1000, 1000))

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(main, "encode_screen_observation", fake_encode)
    try:
        call: main.ToolCallDraft = {
            "id": "1", "name": "take_screenshot",
            "arguments": json.dumps({"filename": str(image), "detail": True}),
        }
        first, digest_a = await main._screenshot_observation_with_digest(call, allow_source_relative=True)
        # Başka ekran (B) kodlanır, sonra A'ya dönüşte önbellek isabet eder.
        other: Path = tmp_path / "ekran-b.png"
        other.write_bytes(b"baska-sahne")
        call_b: main.ToolCallDraft = {
            "id": "2", "name": "take_screenshot",
            "arguments": json.dumps({"filename": str(other), "detail": True}),
        }
        await main._screenshot_observation_with_digest(call_b, allow_source_relative=True)
        returned, digest_again = await main._screenshot_observation_with_digest(
            call, allow_source_relative=True,
        )
        assert len(encodes) == 2
        assert digest_again == digest_a
        assert returned["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,kare-")
        assert first["content"][1]["image_url"]["url"] == returned["content"][1]["image_url"]["url"]
    finally:
        monkeypatch.undo()


@pytest.mark.asyncio
async def test_model_failure_auto_resumes_progressed_task_once(tmp_path: Path) -> None:
    """İlerlemiş görevde model çağrısı kesilirse kısa serinlemeden sonra aynı tur TEK kez daha denenir."""
    calls: List[int] = []
    failure: main.ModelCallFailed = main.ModelCallFailed(
        "sağlayıcı kesintisi", backend="opencode", kind="transient", attempts=2, waited_seconds=30.0,
    )
    probe: Path = tmp_path / "a.txt"
    probe.write_text("içerik", encoding="utf-8")
    turns = iter([
        {"content": "okudum", "tool_calls": [
            {"id": "r1", "name": "read_file", "arguments": json.dumps({"path": str(probe)})},
        ], "finish_reason": "tool_calls", "usage": main.ZERO_USAGE},
        {"content": "bitti", "tool_calls": [], "finish_reason": "stop", "usage": main.ZERO_USAGE},
    ])

    async def flaky_model(clients, messages, schemas, session_id, backend, emit, should_stop):
        calls.append(1)
        if len(calls) == 2:  # ikinci model çağrısında sağlayıcı düşer; üçüncü deneme kurtarır
            raise failure
        return next(turns), backend

    service = CapabilityService(tmp_path / "entegrasyon")
    try:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(main, "_call_model_with_retries", flaky_model)
            patch.setattr(main, "AUTO_RESUME_COOLDOWN_SECONDS", 0.01)
            report: main.RunReport = await main.run_agent_with_callback(
                "Dosyayı okuyup özetle", lambda event: None,
                {"requested_backend": "opencode", "should_stop": lambda: False,
                 "state_file": str(tmp_path / "memory.json"), "history": [], "integrations": service,
                 "max_iterations": 5},
                {"opencode": object()},
            )
    finally:
        await service.close()
    assert len(calls) == 3
    assert report["success"]
    assert report["exchange"]["answer"] == "bitti"


@pytest.mark.asyncio
async def test_model_failure_does_not_auto_resume_first_turn(tmp_path: Path) -> None:
    """İlk turda model yanıt vermezse yeniden denemez: kullanıcı hatayı anında görür."""
    failure: main.ModelCallFailed = main.ModelCallFailed(
        "sağlayıcı kesintisi", backend="opencode", kind="access", attempts=1, waited_seconds=0.0,
    )

    async def dead_model(clients, messages, schemas, session_id, backend, emit, should_stop):
        raise failure

    service = CapabilityService(tmp_path / "entegrasyon")
    try:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(main, "_call_model_with_retries", dead_model)
            patch.setattr(main, "AUTO_RESUME_COOLDOWN_SECONDS", 0.01)
            report: main.RunReport = await main.run_agent_with_callback(
                "Basit bir soru", lambda event: None,
                {"requested_backend": "opencode", "should_stop": lambda: False,
                 "state_file": str(tmp_path / "memory.json"), "history": [], "integrations": service,
                 "max_iterations": 5},
                {"opencode": object()},
            )
    finally:
        await service.close()
    assert not report["success"]
    assert "Model çağrısı başarısız" in report["exchange"]["answer"]
