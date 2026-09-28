"""Gerçek Tk bileşenlerinde akış, yeniden deneme ve bağlam temizleme duman testleri."""
import json
import os
from pathlib import Path
import time
from concurrent.futures import Future
from typing import Any, Iterator, List, Tuple

import pytest
from PIL import Image

from omniagent.ui import app as ui
from omniagent.app import agent as main
from omniagent.config import DEFAULT_BACKEND
from omniagent.core.conversation import make_exchange
from omniagent.core.events import AgentEvent, ArtifactReady
from omniagent.app.agent import ZERO_USAGE
from omniagent.app.agent import artifact_event_for_call, merge_artifact
from omniagent.integrations.capabilities import CapabilityService
from omniagent.tools import filesystem


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[ui.OmniUI]:
    if os.environ.get("OMNI_UI_TEST") != "1":
        pytest.skip("Gerçek Tk testi OMNI_UI_TEST=1 ile etkinleştirilir.")
    monkeypatch.setattr(ui, "create_model_clients", lambda: {})
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    class FakeHotkey:
        def __init__(self, callback: Any) -> None:
            self.callback = callback
            self.closed = False

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(ui, "GlobalVisibilityHotkey", FakeHotkey)
    window = ui.OmniUI()
    window.withdraw()
    window._text.configure(state="normal")
    yield window
    window._on_close()


def test_global_visibility_toggle_keeps_task_state(app: ui.OmniUI, monkeypatch: pytest.MonkeyPatch) -> None:
    applied: list[bool] = []
    monkeypatch.setattr(ui, "set_application_hidden", lambda hidden, window: applied.append(hidden))
    transcript = app._text.get("1.0", "end")
    future: Future[Any] = Future()
    app._agent_future = future
    app._task_status = "running"
    hotkey = app._visibility_hotkey
    assert hotkey is not None
    hotkey.callback()
    app._tick()
    assert applied == [True]
    assert app._visibility_hidden
    assert app._agent_future is future
    assert app._text.get("1.0", "end") == transcript
    hotkey.callback()
    app._tick()
    assert applied == [True, False]
    assert not app._visibility_hidden
    assert app._agent_future is future
    assert app._text.get("1.0", "end") == transcript
    future.cancel()


def _start(app: ui.OmniUI) -> None:
    app._handle_event({"kind": "turn_started", "turn": 1, "max_turns": 25,
                       "backend": DEFAULT_BACKEND, "model": "deneme"})


def _drain(app: ui.OmniUI) -> None:
    app._end_live_regions()
    for _ in range(200):
        app._typewriter_step()
        if not any(app._pending_text.values()):
            break
    assert not any(app._pending_text.values())


def test_stream_render_reset_and_long_answer(app: ui.OmniUI) -> None:
    _start(app)
    text = "# Başlık\n\n| Ad | Sayı |\n| --- | --- |\n| elma | 3 |\n\n```\nls *.py\n2 * 3\n```\n"
    for chunk in (text[:19], text[19:], "uzun cevap\n" * 300):
        app._handle_event({"kind": "text_delta", "text": chunk})
    assert not app._text.tag_ranges("md_h1")
    _drain(app)
    visible = app._text.get("1.0", "end")
    assert "ls *.py\n2 * 3" in visible
    assert visible.count("uzun cevap") == 300
    assert "| --- |" not in visible
    assert app._text.tag_ranges("md_h1")
    assert app._text.tag_ranges("md_table_head")
    assert app._text.tag_ranges("md_codeblock")
    assert "▌" not in visible
    snapshot = visible
    app._end_live_regions()
    assert app._text.get("1.0", "end") == snapshot
    app._handle_event({"kind": "stream_reset", "reason": "deneme"})
    assert not app._raw_text
    assert "uzun cevap" not in app._text.get("1.0", "end")
    app._handle_event({"kind": "text_delta", "text": "**Yeni** yanıt"})
    _drain(app)
    assert "Yeni yanıt" in app._text.get("1.0", "end")


def test_worker_delivers_host_rejection_without_model_success_claim(
    app: ui.OmniUI, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "hedef.txt"
    target.write_text("koru", encoding="utf-8")
    other = tmp_path / "not.txt"
    app._clients = {"ollama-cloud": object()}
    monkeypatch.setattr(ui, "STATE_FILE", str(tmp_path / "memory.json"))
    turns = 0

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
                    "arguments": json.dumps({"path": str(other), "content": "alakasız"}),
                }], "finish_reason": "tool_calls", "usage": main.ZERO_USAGE,
            }, backend
        emit({"kind": "text_delta", "text": "Hedef silindi."})
        return {"content": "Hedef silindi.", "tool_calls": [],
                "finish_reason": "stop", "usage": main.ZERO_USAGE}, backend

    monkeypatch.setattr(main, "_call_model_with_retries", fake_model)
    app.entry.insert(0, f"sil: `{target}`")
    app._send_goal()
    deadline = time.monotonic() + 8
    while app._agent_future is not None and time.monotonic() < deadline:
        app.update()
        time.sleep(0.01)
    app.update()
    _drain(app)
    assert app._agent_future is None
    visible = app._text.get("1.0", "end")
    assert "Doğrulanmadı:" in visible
    assert "Hedef silindi." not in visible
    assert target.read_text(encoding="utf-8") == "koru"


def test_history_delivery_clear_and_pending_completion(app: ui.OmniUI) -> None:
    app._active_goal = "hedef"
    future = Future()
    future.set_result({
        "outcome": "yanıt", "success": True,
        "metrics": {"turns": 1, "tool_calls": 0, "elapsed_seconds": 0,
                    "backend": DEFAULT_BACKEND, **ZERO_USAGE},
        "exchange": make_exchange("hedef", "yanıt", []),
    })
    app._agent_future = future
    app._on_agent_future_done(future)
    app._clear_transcript()
    assert app._agent_future is future
    item = app._inbox.get_nowait()
    app._on_run_done(item["error"], item["report"])
    assert len(app._history) == 1
    assert app.context_label.cget("text") == "bağlam: 1 mesaj"
    app._clear_transcript()
    assert not app._history
    assert not app._raw_text
    assert app.context_label.cget("text") == "bağlam: 0 mesaj"


def test_batched_tool_output_keeps_line_summary(app: ui.OmniUI) -> None:
    _start(app)
    app._handle_event({"kind": "tool_started", "call_id": "batch", "index": 0,
                       "name": "execute_shell", "preview": "seq 1 5"})
    app._handle_event({"kind": "tool_output", "call_id": "batch", "text": "1\n2\n3\n4\n5\n"})
    view = app._tools_by_call["batch"]
    assert view["line_count"] == 5
    assert view["head"][:2] == ["1\n", "2\n"]
    assert view["tail"][-1] == "5\n"


def test_format_run_stats_shows_exact_totals_and_waits() -> None:
    metrics = {
        "turns": 8, "tool_calls": 7, "elapsed_seconds": 30.25, "backend": "opencode",
        "prompt_tokens": 29593, "cached_tokens": 23040, "completion_tokens": 561,
        "model_seconds": 22.8, "tool_seconds": 7.4,
        "integrations": {"network_requests": 2, "wait_seconds": 1.5},
    }
    lines = ui.format_run_stats(metrics)
    assert "30.2 sn" in lines[0]
    assert "model 22.8 sn" in lines[0]
    assert "Giriş 29.593 (önbellek 23.040, yeni 6.553)" in lines[1]
    assert "toplam 30.154 token" in lines[1]
    assert "ağ isteği 2" in lines[2]


def test_finished_run_keeps_stats_in_footer(app: ui.OmniUI) -> None:
    metrics = {
        "turns": 2, "tool_calls": 1, "elapsed_seconds": 5.1, "backend": "opencode",
        "prompt_tokens": 100, "cached_tokens": 80, "completion_tokens": 20,
        "model_seconds": 4.0, "tool_seconds": 1.0,
    }
    app._handle_event({"kind": "run_finished", "success": True, "outcome": "tamam",
                       "reason": "", "metrics": metrics})
    assert "5.1 sn" in app.stats_label.cget("text")
    assert "toplam 120 token" not in app.stats_label.cget("text")
    assert "toplam 120 token" in app._text.get("1.0", "end")
    assert "✓ Tamamlandı" in app._text.get("1.0", "end")


def test_requested_screenshot_appears_inline_and_clear_removes_card(
    app: ui.OmniUI, tmp_path: Path,
) -> None:
    image_path = tmp_path / "ekran.png"
    Image.new("RGB", (1400, 900), "#d97757").save(image_path)
    call = {"id": "shot-1", "name": "take_screenshot",
            "arguments": json.dumps({"filename": str(image_path)})}
    event = artifact_event_for_call(call, {"ok": True, "result": "kaydedildi"}, tmp_path, "ekran resmi çek")
    assert event is not None
    assert event["tool"] == "take_screenshot"
    app._handle_event(event)
    assert len(app._artifact_widgets) == 1
    assert len(app._artifact_images) == 1
    assert "ekran.png" in app._text.get("1.0", "end")
    assert app._text.window_names()
    app._clear_transcript()
    assert not app._artifact_widgets
    assert not app._artifact_images
    assert "ekran.png" not in app._text.get("1.0", "end")


def test_internal_or_failed_screenshot_has_no_chat_artifact(tmp_path: Path) -> None:
    image_path = tmp_path / "ekran.png"
    image_path.write_bytes(b"test")
    call = {"id": "shot-1", "name": "take_screenshot",
            "arguments": json.dumps({"filename": str(image_path)})}
    assert artifact_event_for_call(call, {"ok": True}, tmp_path, "sayfayı oku") is None
    assert artifact_event_for_call(call, {"ok": False}, tmp_path, "ekran resmi çek") is None
    image_path.unlink()
    assert artifact_event_for_call(call, {"ok": True}, tmp_path, "ekran resmi çek") is None


def test_photo_and_generated_file_get_structured_chat_cards(tmp_path: Path) -> None:
    photo = tmp_path / "foto.jpg"
    photo.write_bytes(b"photo")
    photo_call = {"id": "photo-1", "name": "capture_photo", "arguments": "{}"}
    photo_event = artifact_event_for_call(
        photo_call, {"ok": True, "artifact_path": str(photo)}, tmp_path, "fotoğraf çek",
    )
    assert photo_event is not None
    assert photo_event["media_type"] == "image"
    document = tmp_path / "rapor.md"
    document.write_text("Rapor", encoding="utf-8")
    file_call = {"id": "write-1", "name": "write_file",
                 "arguments": json.dumps({"path": "rapor.md", "content": "Rapor"})}
    file_event = artifact_event_for_call(file_call, {"ok": True}, tmp_path, "rapor oluştur")
    assert file_event is not None
    assert file_event["path"] == str(document)
    assert file_event["media_type"] == "file"


def test_artifact_merge_keeps_latest_screenshot_and_one_card_per_path() -> None:
    def card(call_id: str, tool: str, path: str) -> ArtifactReady:
        return {"kind": "artifact_ready", "call_id": call_id, "tool": tool, "path": path,
                "title": "Çıktı", "media_type": "file"}

    merged: Tuple[ArtifactReady, ...] = ()
    for item in (card("s1", "take_screenshot", "/a.png"), card("w1", "write_file", "/r.md"),
                 card("s2", "take_screenshot", "/b.png"), card("w2", "write_file", "/r.md"),
                 card("p1", "capture_photo", "/f1.jpg"), card("p2", "capture_photo", "/f2.jpg")):
        merged = merge_artifact(merged, item)
    assert [item["call_id"] for item in merged] == ["s2", "w2", "p1", "p2"]


def test_artifact_card_survives_file_vanishing_during_render(
    app: ui.OmniUI, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_path = tmp_path / "ekran.png"
    Image.new("RGB", (40, 30), "#d97757").save(image_path)
    real_stat = Path.stat
    checks: List[Path] = []

    def vanishing_stat(self: Path, **options: bool) -> os.stat_result:
        # İlk denetimden sonra dosya silinmiş gibi davranır (çizim sırasındaki yarış).
        if self == image_path:
            checks.append(self)
            if len(checks) > 1:
                raise FileNotFoundError(str(self))
        return real_stat(self, **options)

    monkeypatch.setattr(Path, "stat", vanishing_stat)
    app._render_artifact(str(image_path), "Ekran görüntüsü", "image")
    assert "ekran.png" in app._text.get("1.0", "end")


@pytest.mark.asyncio
async def test_run_emits_cards_at_finish_for_outputs_that_still_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    report_file = tmp_path / "rapor.md"
    scratch_file = tmp_path / "gecici.py"
    turns: List[List[Tuple[str, Path, str]]] = [
        [("w1", report_file, "taslak"), ("w2", scratch_file, "print(1)")],
        [("w3", report_file, "son hâl")],
    ]
    calls = 0

    async def fake_model(
        clients: Any, messages: Any, schemas: Any, session_id: str,
        backend: str, emit: Any, should_stop: Any,
    ) -> tuple[dict[str, Any], str]:
        nonlocal calls
        calls += 1
        if calls <= len(turns):
            return {
                "content": "", "tool_calls": [
                    {"id": call_id, "name": "write_file",
                     "arguments": json.dumps({"path": str(path), "content": content})}
                    for call_id, path, content in turns[calls - 1]
                ],
                "finish_reason": "tool_calls", "usage": ZERO_USAGE,
            }, backend
        # Geçici betik görev bitmeden silinir; kalıcı çıktı sayılmaz.
        scratch_file.unlink(missing_ok=True)
        return {"content": "Rapor hazır.", "tool_calls": [], "finish_reason": "stop",
                "usage": ZERO_USAGE}, backend

    monkeypatch.setattr(main, "_call_model_with_retries", fake_model)
    monkeypatch.setattr(filesystem, "BACKUP_DIR", tmp_path / "backups")
    events: List[AgentEvent] = []
    service = CapabilityService(tmp_path)
    try:
        await main.run_agent_with_callback(
            "rapor.md raporunu hazırla", events.append,
            {"requested_backend": None, "should_stop": lambda: False,
             "state_file": str(tmp_path / "memory.json"), "history": [],
             "integrations": service},
            {"ollama-cloud": object()},
        )
    finally:
        await service.close()
    cards = [event for event in events if event["kind"] == "artifact_ready"]
    assert [(card["tool"], card["path"]) for card in cards] == [("write_file", str(report_file))]
    assert [event["kind"] for event in events][-2:] == ["artifact_ready", "run_finished"]


def test_idle_tick_skips_transcript_redraw(app: ui.OmniUI, monkeypatch: pytest.MonkeyPatch) -> None:
    """Boş arayüzde zamanlayıcı çalışır, büyük transkript yeniden çizilmez."""
    redraws = []
    timers = []
    app._tools_by_call["eski"] = {"status": "running", "tail": []}
    app._last_running_refresh = 0.0
    monkeypatch.setattr(app._text, "configure", lambda **kwargs: redraws.append(kwargs))
    monkeypatch.setattr(app, "after", lambda delay, callback: timers.append((delay, callback)))
    app._tick()
    assert redraws == []
    assert timers and timers[-1][0] == ui.IDLE_FRAME_MS
