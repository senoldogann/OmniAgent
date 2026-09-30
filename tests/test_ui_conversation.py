"""Gerçek Tk bileşenlerinde akış, yeniden deneme ve bağlam temizleme duman testleri."""
import asyncio
import json
import logging
import os
from pathlib import Path
import stat
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from types import SimpleNamespace
from concurrent.futures import Future
from typing import Any, Callable, Iterator, List, Optional, Tuple

import pytest
from PIL import Image

from omniagent.ui import app as ui
from omniagent.app import agent as main
from omniagent.config import DEFAULT_BACKEND, register_secret
from omniagent.core.conversation import make_exchange
from omniagent.core.events import AgentEvent, ArtifactReady, ProviderFallback, provider_fallback_text
from omniagent.app.agent import ZERO_USAGE
from omniagent.app.agent import artifact_event_for_call, merge_artifact
from omniagent.integrations import runtime as integration_runtime
from omniagent.integrations.capabilities import CapabilityService
from omniagent.paths import workspace_dir
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
    close_window(window)


def pump_until(window: ui.OmniUI, condition: Callable[[], bool], timeout: float) -> bool:
    """Tk olay döngüsünü koşul sağlanana ya da süre dolana dek döndürür (pencere gizli olsa da geri çağrılar çalışır)."""
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        window.update()
        time.sleep(0.005)
    return condition()


def pump(window: ui.OmniUI, seconds: float) -> None:
    """Tk olay döngüsünü belirtilen süre döndürür."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        window.update()
        time.sleep(0.005)


def close_window(window: ui.OmniUI) -> None:
    """Pencereyi gerçek kapanış yolundan kapatır ve yok olana dek olay döngüsünü döndürür."""
    window._on_close()
    assert pump_until(window, lambda: window._destroyed, 15)


def test_global_visibility_toggle_keeps_task_state(app: ui.OmniUI, monkeypatch: pytest.MonkeyPatch) -> None:
    applied: list[bool] = []
    monkeypatch.setattr(ui, "set_application_hidden", lambda hidden, window: applied.append(hidden))
    # Sahte set_application_hidden gerçek NSApp durumunu değiştirmez; işletim sistemi durumu yerine son istek okunur.
    monkeypatch.setattr(ui, "application_is_hidden", lambda window: bool(applied and applied[-1]))
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


def test_stream_delta_is_visible_in_one_frame(app: ui.OmniUI) -> None:
    """Kısa model parçaları yapay daktilo gecikmesiyle geride bırakılmaz."""
    _start(app)
    app._handle_event({"kind": "text_delta", "text": "Model yanıtı hemen görünür."})
    assert app._typewriter_step()
    assert "Model yanıtı hemen görünür." in app._text.get("1.0", "end")
    assert not any(app._pending_text.values())


@pytest.mark.parametrize("label,expected", [
    ("Normal", "25 tur · 10 dk"),
    ("Uzun", "50 tur · 20 dk"),
    ("Otonom", "100 tur · 45 dk"),
    ("Sürekli", "kullanıcı onayı"),
])
def test_mode_hint_explains_selected_budget(app: ui.OmniUI, label: str, expected: str) -> None:
    app.mode_menu.set(label)
    app._update_mode_hint(label)
    assert expected in app.mode_hint.cget("text")


@pytest.mark.parametrize("label,mode", [
    ("Normal", "normal"), ("Uzun", "extended"),
    ("Otonom", "autonomous"), ("Sürekli", "continuous"),
])
def test_mode_selection_reaches_runner(
    app: ui.OmniUI, monkeypatch: pytest.MonkeyPatch, label: str, mode: str,
) -> None:
    """Composer seçimi gerçek ajan çağrısındaki RunOptions'a ulaşır."""
    captured: list[str] = []

    async def fake_run(goal: str, options: Any) -> Any:
        captured.append(options["run_mode"])
        return None

    monkeypatch.setattr(app, "_run_exclusive", fake_run)
    monkeypatch.setattr(ui, "set_dock_badge", lambda _badge: None)
    app.mode_menu.set(label)
    app.entry.insert(0, "Kısa görev")
    app._send_goal()
    assert app._agent_future is not None
    app._agent_future.result(timeout=3)
    assert captured == [mode]


def test_btw_during_continuous_run_queues_instruction_without_stopping(
    app: ui.OmniUI, monkeypatch: pytest.MonkeyPatch,
) -> None:
    future: Future[Any] = Future()
    app._agent_future = future
    app._active_run_mode = "continuous"
    stopped: list[bool] = []
    monkeypatch.setattr(app, "_request_stop", lambda: stopped.append(True))
    app.entry.insert(0, "/btw Önce raporu hazırla")
    app._on_primary_button()
    assert app._drain_control_messages() == ["/btw Önce raporu hazırla"]
    assert not stopped and app.entry.get() == ""
    future.set_result(None)


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
    event = artifact_event_for_call(
        call, {"ok": True, "result": "kaydedildi"}, tmp_path, "ekran resmi çek", allow_source_relative=False,
    )
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
    assert artifact_event_for_call(
        call, {"ok": True}, tmp_path, "sayfayı oku", allow_source_relative=False,
    ) is None
    assert artifact_event_for_call(
        call, {"ok": False}, tmp_path, "ekran resmi çek", allow_source_relative=False,
    ) is None
    image_path.unlink()
    assert artifact_event_for_call(
        call, {"ok": True}, tmp_path, "ekran resmi çek", allow_source_relative=False,
    ) is None


def test_photo_and_generated_file_get_structured_chat_cards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path / "data"))
    photo = tmp_path / "foto.jpg"
    photo.write_bytes(b"photo")
    photo_call = {"id": "photo-1", "name": "capture_photo", "arguments": "{}"}
    photo_event = artifact_event_for_call(
        photo_call, {"ok": True, "artifact_path": str(photo)}, tmp_path, "fotoğraf çek",
        allow_source_relative=False,
    )
    assert photo_event is not None
    assert photo_event["media_type"] == "image"
    document = tmp_path / "rapor.md"
    document.write_text("Rapor", encoding="utf-8")
    file_call = {"id": "write-1", "name": "write_file",
                 "arguments": json.dumps({"path": "rapor.md", "content": "Rapor"})}
    # Kaynak görevinde (allow_source_relative=True) göreli ad çalışma dizinine (cwd) bağlanır.
    file_event = artifact_event_for_call(
        file_call, {"ok": True}, tmp_path, "rapor oluştur", allow_source_relative=True,
    )
    assert file_event is not None
    assert file_event["path"] == str(document)
    assert file_event["media_type"] == "file"
    # Kaynak görevi değilse write_file göreli adı workspace'e yazar; kart da oradan okunur.
    generated = workspace_dir() / "rapor2.md"
    generated.parent.mkdir(parents=True)
    generated.write_text("Rapor", encoding="utf-8")
    workspace_event = artifact_event_for_call(
        {"id": "write-2", "name": "write_file",
         "arguments": json.dumps({"path": "rapor2.md", "content": "Rapor"})},
        {"ok": True}, tmp_path, "rapor oluştur", allow_source_relative=False,
    )
    assert workspace_event is not None
    assert workspace_event["path"] == str(generated.resolve())
    # send_file göreli yolu bayraktan bağımsız süreç dizininden okur.
    sent = tmp_path / "gonder.md"
    sent.write_text("Gönder", encoding="utf-8")
    send_event = artifact_event_for_call(
        {"id": "send-1", "name": "send_file", "arguments": json.dumps({"path": "gonder.md"})},
        {"ok": True}, tmp_path, "dosyayı gönder", allow_source_relative=False,
    )
    assert send_event is not None
    assert send_event["path"] == str(sent.resolve())


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


def _visible(app: ui.OmniUI) -> str:
    """Katlanmış (elide) bölümler hariç, ekranda görünen transkript metni."""
    return str(app._text.tk.call(str(app._text), "get", "-displaychars", "1.0", "end-1c"))


def _frame(app: ui.OmniUI) -> None:
    """
    Kuyruktaki olayları ve kirli araç bloklarını tek karede çizer (zamanlayıcıya girmeden). Kare
    transkripti salt okunura döndürür; testler olayları doğrudan işlediği için yazılabilir geri açılır.
    """
    app._process_frame(time.monotonic(), False)
    app._text.configure(state="normal")


def _run_shell(app: ui.OmniUI, call: str, index: int, command: str, output: str) -> None:
    app._handle_event({"kind": "tool_started", "call_id": call, "index": index,
                       "name": "execute_shell", "preview": command})
    app._handle_event({"kind": "tool_finished", "call_id": call, "ok": True, "seconds": 0.1,
                       "text": f"STDOUT: {output}\nSTDERR: \nÇıkış Kodu: 0"})


def test_consecutive_tools_share_one_group_that_folds_when_the_answer_continues(app: ui.OmniUI) -> None:
    _start(app)
    _run_shell(app, "a", 0, "echo bir", "bir")
    _run_shell(app, "b", 1, "echo iki", "iki")
    _frame(app)
    group = app._tool_group
    assert group is not None and len(group["views"]) == 2
    visible = _visible(app)
    assert "2 araç çalıştırıldı" in visible and "echo bir" in visible and "echo iki" in visible
    # Asistan konuşmaya devam edince grup başlığa katlanır; yeni araç yeni grup açar.
    app._handle_event({"kind": "turn_started", "turn": 2, "max_turns": 25,
                       "backend": DEFAULT_BACKEND, "model": "deneme"})
    app._handle_event({"kind": "text_delta", "text": "Sonuç hazır."})
    _drain(app)
    visible = _visible(app)
    assert app._tool_group is None
    assert "2 araç çalıştırıldı" in visible and "echo bir" not in visible and "Sonuç hazır." in visible
    _run_shell(app, "c", 0, "echo üç", "üç")
    _frame(app)
    assert app._tool_group is not None and app._tool_group is not group
    assert "1 araç çalıştırıldı" in _visible(app)


def test_user_toggled_group_stays_open_when_the_run_ends(app: ui.OmniUI) -> None:
    _start(app)
    _run_shell(app, "a", 0, "echo bir", "bir")
    _frame(app)
    group = app._tool_group
    assert group is not None
    app._toggle_fold("g", group["id"])  # kullanıcı başlığa tıkladı: kapattı
    assert "echo bir" not in _visible(app)
    app._toggle_fold("g", group["id"])  # yeniden açtı
    app._handle_event({"kind": "text_delta", "text": "bitti"})
    _drain(app)
    assert "echo bir" in _visible(app)  # elle seçim otomatik katlamaya karşı korunur


def test_running_tool_is_open_and_finished_tool_folds_with_a_show_all_link(app: ui.OmniUI) -> None:
    _start(app)
    app._handle_event({"kind": "tool_started", "call_id": "c1", "index": 0, "name": "execute_shell",
                       "preview": "seq 1 12"})
    app._handle_event({"kind": "tool_output", "call_id": "c1", "text": "çıktı 01\n"})
    _frame(app)
    visible = _visible(app)
    assert "$ seq 1 12" in visible and "çıktı 01" in visible
    output = "\n".join(f"çıktı {number:02d}" for number in range(1, 13))
    app._handle_event({"kind": "tool_finished", "call_id": "c1", "ok": True, "seconds": 0.2,
                       "text": f"STDOUT: {output}\nSTDERR: \nÇıkış Kodu: 0"})
    _frame(app)
    view = app._tools_by_call["c1"]
    assert "$ seq 1 12" not in _visible(app)  # bitince satır kapanır
    app._toggle_fold("t", view["fold_id"])
    visible = _visible(app)
    assert "çıktı 08" in visible and "çıktı 09" not in visible and "tümünü göster" in visible
    app._toggle_fold("m", view["more_id"])
    visible = _visible(app)
    assert "çıktı 12" in visible and "tümünü göster" not in visible and "daha az" in visible


def test_dropped_stream_removes_unstarted_rows_and_their_empty_group(app: ui.OmniUI) -> None:
    _start(app)
    app._handle_event({"kind": "tool_call_preview", "index": 0, "name": "execute_shell", "preview": "ls"})
    _frame(app)
    assert "1 araç çalışıyor" in _visible(app)
    app._handle_event({"kind": "stream_reset", "reason": "deneme"})
    assert app._tool_group is None and not app._tool_groups
    assert "araç çalış" not in _visible(app) and "Kabuk" not in _visible(app)


def test_run_end_settles_running_tools_and_folds_the_last_group(app: ui.OmniUI) -> None:
    _start(app)
    app._handle_event({"kind": "tool_started", "call_id": "c1", "index": 0, "name": "execute_shell",
                       "preview": "sleep 99"})
    _frame(app)
    assert app._text.tag_ranges("tool_spin")
    app._on_run_done("", None)
    assert not app._text.tag_ranges("tool_spin")  # dönen glif takılı kalmaz
    assert app._tools_by_call["c1"]["status"] == "error"
    visible = _visible(app)
    assert "1 araç çalıştırıldı" in visible and "sleep 99" not in visible


def test_user_bubble_is_rebuilt_from_hidden_text_after_reload(app: ui.OmniUI) -> None:
    goal = "uzun hedef\nikinci satır"
    assert app._ensure_chat(goal)
    chat_id = app._active_chat_id
    assert chat_id is not None
    app._text.configure(state="normal")
    app._render_goal(goal)
    app._text.configure(state="disabled")
    assert len(app._bubbles) == 1 and app._bubbles[0]["label"].cget("text") == goal
    assert goal in app._text.get("1.0", "end")  # kopyalama ve kayıt gizli kaynak metinden yapılır
    app._save_current_chat()
    saved = [span for span in ui.load_chat(chat_id)["spans"] if "user_msg" in span["tags"]]
    assert [span["text"] for span in saved] == [goal]
    app._new_chat()
    assert not app._bubbles
    app._open_chat(chat_id)
    assert len(app._bubbles) == 1 and app._bubbles[0]["label"].cget("text") == goal
    assert app._text.window_names()
    app._save_current_chat()  # yüklenen sohbet yeniden kaydedilince aynı kalır
    again = [span for span in ui.load_chat(chat_id)["spans"] if "user_msg" in span["tags"]]
    assert again == saved


def test_user_bubble_footer_shows_send_time_and_copies_only_that_message(app: ui.OmniUI) -> None:
    """
    Kullanıcı mesajının altında gönderim saati ve o mesajı kopyalayan simge görünür; saat kayda
    yazıldığı için sohbet yeniden açıldığında da aynı değerle kurulur.
    """
    assert app._ensure_chat("saat ve kopyalama")
    chat_id = app._active_chat_id
    assert chat_id is not None
    with app._writable_transcript():
        app._render_goal("saat ve kopyalama")
    bubble = app._bubbles[0]
    assert bubble["time"].cget("text") == datetime.now().astimezone().strftime("%H:%M")
    assert bubble["foot"].master is bubble["frame"] and bubble["copy"].master is bubble["foot"]
    assert bubble["foot"].winfo_manager() == "grid"
    # Kopyalama simgeyi yeşil onaya çevirir ve yalnız bu mesajı panoya koyar.
    app.clipboard_clear()
    app._copy_message("saat ve kopyalama", bubble["copy"])
    assert app.clipboard_get() == "saat ve kopyalama"
    assert bubble["copy"].cget("image") == str(app._copy_photo_done)
    app._restore_message_copy(bubble["copy"])
    assert bubble["copy"].cget("image") == str(app._copy_photo)
    sent_text: str = bubble["time"].cget("text")
    assert app._save_current_chat()
    stamps = ui.load_chat(chat_id)["sends"]
    assert len(stamps) == 1 and ui.sent_time_text(stamps[0]) == sent_text
    app._new_chat()
    assert not app._bubbles
    app._open_chat(chat_id)
    assert app._bubbles[0]["time"].cget("text") == sent_text


def test_user_bubble_footer_without_a_saved_stamp_keeps_only_the_copy_icon(app: ui.OmniUI) -> None:
    """Eski kayıtlarda gönderim saati yoktur: kabarcık saatsiz kurulur, kopyalama yine çalışır."""
    record: ui.ChatRecord = ui.new_chat("eski kayıt")
    record["spans"] = [{"text": "eski hedef\n", "tags": ["user_msg", "user_line"]}]
    ui.save_chat(record)
    app._open_chat(record["id"], save_current=False)
    bubble = app._bubbles[0]
    assert bubble["label"].cget("text") == "eski hedef\n" and bubble["time"].cget("text") == ""
    app._copy_message(bubble["label"].cget("text"), bubble["copy"])
    assert app.clipboard_get() == "eski hedef\n"


def test_reloaded_tool_groups_are_collapsed_and_still_toggle(app: ui.OmniUI) -> None:
    assert app._ensure_chat("araç")
    chat_id = app._active_chat_id
    assert chat_id is not None
    app._text.configure(state="normal")
    app._render_goal("araç")
    _start(app)
    _run_shell(app, "a", 0, "echo bir", "bir")
    _frame(app)
    app._handle_event({"kind": "text_delta", "text": "tamam"})
    _drain(app)
    app._save_current_chat()
    app._new_chat()
    app._open_chat(chat_id)
    assert "1 araç çalıştırıldı" in _visible(app) and "echo bir" not in _visible(app)
    head_tags = [tag for tag in app._text.tag_names() if tag.startswith("gh")]
    assert len(head_tags) == 1
    ident = int(head_tags[0][2:])
    app._toggle_fold("g", ident)  # kayıttan gelen bölüm bellekteki görünüm olmadan da açılır
    assert "echo bir" in _visible(app)


def test_legacy_chat_records_still_open_in_the_new_layout(app: ui.OmniUI) -> None:
    """Eski düzenle (⏺/⎿, bantlı hedef) kaydedilmiş sohbet yeni sütunda okunur kalır; kabarcığa dönüşmez."""
    record = ui.new_chat("eski")
    record["history"] = [make_exchange("eski hedef", "Yanıt", [])]
    record["spans"] = [
        {"text": "› ", "tags": ["goal_prompt"]}, {"text": "eski hedef\n", "tags": ["goal"]},
        {"text": "⏺ ", "tags": ["bullet_text"]}, {"text": "Yanıt\n", "tags": ["assistant"]},
        {"text": "⏺ ", "tags": ["bullet_ok"]}, {"text": "Kabuk", "tags": ["tool_name"]},
        {"text": " · 0.4sn\n", "tags": ["meta"]}, {"text": "$ ", "tags": ["command_prompt"]},
        {"text": "ls\n", "tags": ["command"]}, {"text": "⎿  ", "tags": ["gutter"]},
        {"text": "a\n", "tags": ["output"]}, {"text": "✓ Tamamlandı\n", "tags": ["summary_ok"]},
    ]
    ui.save_chat(record)
    app._open_chat(record["id"], save_current=False)
    visible = _visible(app)
    assert "eski hedef" in visible and "Kabuk · 0.4sn" in visible and "✓ Tamamlandı" in visible
    assert not app._bubbles
    assert app._text.tag_cget("goal", "background") == ui.SURFACE_RAISED


def test_content_column_follows_the_window_width(app: ui.OmniUI, monkeypatch: pytest.MonkeyPatch) -> None:
    text = app._text
    monkeypatch.setattr(app._transcript_frame, "winfo_width", lambda: 1400)
    app._text.configure(state="normal")
    app._render_goal("kabarcık sütunla birlikte daralır")
    app._text.configure(state="disabled")
    app._apply_column_layout()
    available = 1400 - app._scrollbar.winfo_reqwidth()
    assert app._column_width == ui.COLUMN_MAX_WIDTH
    assert int(text.grid_info()["padx"]) == ui.column_side(available) > ui.COLUMN_MIN_SIDE
    wide = int(app._bubbles[0]["label"].cget("wraplength"))
    monkeypatch.setattr(app._transcript_frame, "winfo_width", lambda: 700)
    app._apply_column_layout()
    assert app._column_side == ui.COLUMN_MIN_SIDE and app._column_width < ui.COLUMN_MAX_WIDTH
    narrow = int(app._bubbles[0]["label"].cget("wraplength"))
    assert narrow < wide


def test_tick_reschedules_after_frame_failure(app: ui.OmniUI, monkeypatch: pytest.MonkeyPatch) -> None:
    """Kare istisna verse de döngü kurulur; istisna yutulmaz (Tk report_callback_exception'a iner)."""
    timers = []

    def broken(_now: float) -> None:
        raise RuntimeError("kare bozuldu")

    monkeypatch.setattr(app, "after", lambda delay, callback: timers.append((delay, callback)))
    monkeypatch.setattr(app, "_animate", broken)
    with pytest.raises(RuntimeError, match="kare bozuldu"):
        app._tick()
    assert timers[-1][0] == ui.ERROR_FRAME_MS
    app._closing = True
    with pytest.raises(RuntimeError):
        app._tick()
    assert len(timers) == 1  # kapanırken yeniden kurulmaz


def test_frame_failure_reaches_tk_reporter_and_the_loop_keeps_running(
    app: ui.OmniUI, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """Gerçek olay döngüsünde: kare istisnası günlüğe düşer, sonraki kareler yine çalışır."""
    calls: List[float] = []
    original = app._animate

    def flaky(now: float) -> None:
        calls.append(now)
        if len(calls) == 1:
            raise RuntimeError("tek seferlik kare hatası")
        original(now)

    monkeypatch.setattr(app, "_animate", flaky)
    with caplog.at_level(logging.ERROR):
        deadline = time.monotonic() + 3
        while len(calls) < 3 and time.monotonic() < deadline:
            app.update()
            time.sleep(0.01)
    assert len(calls) >= 3
    assert any(record.error_type == "RuntimeError" for record in caplog.records  # type: ignore[attr-defined]
               if hasattr(record, "error_type"))


def test_failing_event_keeps_transcript_read_only_and_is_not_repeated(
    app: ui.OmniUI, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app._text.configure(state="disabled")
    app._post({"kind": "notice", "level": "info", "text": "zehirli olay"})
    calls: List[str] = []

    def explode(event: AgentEvent) -> None:
        calls.append(event["kind"])
        raise KeyError("olay işlenemedi")

    monkeypatch.setattr(app, "_handle_event", explode)
    with pytest.raises(KeyError):
        app._process_frame(time.monotonic(), False)
    assert str(app._text.cget("state")) == "disabled" and app._inbox.empty()
    app._process_frame(time.monotonic(), False)
    assert calls == ["notice"]  # aynı olay ikinci karede tekrar denenmez


def test_callback_errors_are_logged_with_backoff_and_reported_once(
    app: ui.OmniUI, caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.ERROR):
        for _ in range(5):
            app.report_callback_exception(ValueError, ValueError("aynı hata"), None)
    assert [record.occurrences for record in caplog.records] == [1, 2, 4]  # type: ignore[attr-defined]
    assert caplog.records[0].error_type == "ValueError"  # type: ignore[attr-defined]
    notices = []
    while not app._inbox.empty():
        notices.append(app._inbox.get_nowait()["event"])
    assert len(notices) == 1 and "Arayüz hatası (ValueError)" in notices[0]["text"]  # type: ignore[typeddict-item]


def test_callback_error_reports_never_leak_known_secrets(
    app: ui.OmniUI, caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "sk-deneme-gizli-deger-1234567890"
    register_secret("ui-test-secret", secret)
    try:
        with caplog.at_level(logging.ERROR):
            app.report_callback_exception(ValueError, ValueError(f"istek başarısız: {secret}"), None)
    finally:
        register_secret("ui-test-secret", "")
    assert secret not in caplog.text
    assert all(secret not in json.dumps(vars(record), default=str) for record in caplog.records)
    assert secret not in app._inbox.get_nowait()["event"]["text"]  # type: ignore[typeddict-item]


def test_ui_log_is_private_structured_and_rotates(tmp_path: Path) -> None:
    root = logging.getLogger()
    previous_level = root.level
    handler = ui.configure_ui_logging(tmp_path / "ui.log")
    try:
        logging.warning("deneme uyarısı", extra={"error_type": "OSError", "occurrences": 3})
        handler.flush()
    finally:
        root.removeHandler(handler)
        handler.close()
        root.setLevel(previous_level)
    line = (tmp_path / "ui.log").read_text(encoding="utf-8").splitlines()[-1]
    assert "WARNING" in line and "deneme uyarısı" in line
    assert json.loads(line[line.index("{"):]) == {"error_type": "OSError", "occurrences": 3}
    rotating = ui.PrivateRotatingFileHandler(tmp_path / "dondu.log", maxBytes=1500, backupCount=2, encoding="utf-8")
    logger = logging.getLogger("omniagent.test.ui_log_rotation")
    logger.propagate = False
    logger.addHandler(rotating)
    try:
        for _ in range(8):
            logger.warning("dolgu " + "x" * 500)
    finally:
        logger.removeHandler(rotating)
        rotating.close()
    files = sorted(tmp_path.glob("dondu.log*"))
    assert len(files) == 3  # ana dosya + 2 yedek
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in [*files, tmp_path / "ui.log"])


def test_provider_fallback_is_a_persistent_warning_naming_the_processor(app: ui.OmniUI) -> None:
    """İstek içeriği yedek sağlayıcıya gidiyorsa (ekran görüntüsü sayısıyla) transkriptte kalıcı uyarı çıkar."""
    event: ProviderFallback = {
        "kind": "provider_fallback", "from_backend": "ollama-cloud", "to_backend": "openai",
        "to_model": "gpt-deneme", "processor": "OpenAI", "reason": "kota doldu", "image_count": 2,
    }
    app._handle_event(event)
    visible = _visible(app)
    assert provider_fallback_text(event) in visible
    assert "Yedek sağlayıcıya geçildi" in visible and "OpenAI tarafından işlenecek" in visible
    assert "2 ekran görüntüsü dahil" in visible
    assert "openai" in app.model_label.cget("text")
    saved = ui.spans_from_dump(app._text.dump("1.0", "end-1c", text=True, tag=True))
    assert any("notice_warning" in span["tags"] and "OpenAI tarafından" in span["text"] for span in saved)


def test_wheel_over_the_gutter_scrolls_the_transcript(app: ui.OmniUI) -> None:
    """Sütun dışındaki/gömülü pencere üstündeki tekerlek olayı transkripte iletilir; içerik sığıyorsa sondayız."""
    app._text.configure(state="normal")
    app._new_region([("satır\n" * 300, ("assistant",))])
    app._text.configure(state="disabled")
    assert app._scroll_transcript(SimpleNamespace(delta=3)) == "break"  # type: ignore[arg-type]
    app.update_idletasks()
    assert isinstance(app._stick_to_end, bool)
    app._scroll_to_end()
    app._after_user_scroll()
    app.update_idletasks()
    assert app._stick_to_end


# --- Kararlılık: kapanış, kayıt yazıcısı, açılış sırası, gizli mod ve yanıt penceresi ---

def test_mac_quit_hook_runs_on_close_saves_chat_and_closes_connections(app: ui.OmniUI) -> None:
    """⌘Q / Dock > Çık Tk varsayılanı (SystemExit) yerine _on_close'a gider: kayıt yazılır, bağlantılar kapanır."""
    closed: List[str] = []

    class FakeClient:
        async def close(self) -> None:
            closed.append("istemci")

    app._clients = {"ollama-cloud": FakeClient()}
    assert app._ensure_chat("Kapanış sohbeti")
    chat_id = app._active_chat_id
    assert chat_id is not None
    with app._writable_transcript():
        app._render_goal("Kapanış sohbeti")
        app._new_region([("Son yanıt\n", ("assistant",))])
    if sys.platform == "darwin":
        assert app.tk.call("info", "commands", "::tk::mac::Quit")
        app.tk.call("::tk::mac::Quit")
    else:
        app._on_close()
    assert pump_until(app, lambda: app._destroyed, 10)
    assert closed == ["istemci"]
    assert any("Son yanıt" in span["text"] for span in ui.load_chat(chat_id)["spans"])


def test_close_does_not_block_the_tk_thread_while_the_agent_winds_down(
    app: ui.OmniUI, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Çalışan ajanı bekleme (eskiden 3 sn) Tk thread'inde durmaz: pencere hemen gizlenir, kapanış arkada biter."""
    monkeypatch.setattr(ui, "CLOSE_AGENT_WAIT_SECONDS", 2.0, raising=False)
    stuck: Future[None] = Future()  # durdurma isteğini yok sayan ajan: asla kendiliğinden bitmez
    app._agent_future = stuck
    withdrawn: List[bool] = []
    real_withdraw = app.withdraw

    def recording_withdraw() -> None:
        withdrawn.append(True)
        real_withdraw()

    monkeypatch.setattr(app, "withdraw", recording_withdraw)
    ticks: List[float] = []

    def tick() -> None:
        ticks.append(time.monotonic())
        if not app._destroyed:
            app.after(50, tick)

    app.after(50, tick)
    started = time.monotonic()
    app._on_close()
    assert time.monotonic() - started < 1.2  # ajan beklemesi (2 sn; eskiden 3 sn) Tk thread'inde geçmez
    assert withdrawn == [True] and not app._destroyed  # pencere hemen gizlendi; bağlantıları kapatma işi sürüyor
    assert pump_until(app, lambda: app._destroyed, 8)
    assert len(ticks) >= 10  # bekleme boyunca Tk olay döngüsü döndü (bloklansaydı geri çağrılar gelmezdi)
    assert stuck.cancelled()  # süre dolunca ajan açıkça iptal edildi, sessizce bırakılmadı


def _saved_text(chat_id: str) -> str:
    return "".join(span["text"] for span in ui.load_chat(chat_id)["spans"])


def test_close_waits_for_the_last_chat_write_but_never_cancels_it(
    app: ui.OmniUI, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Yazıcı kapanışta takılırsa son kayıt beklenir ama İPTAL EDİLMEZ: yazıcı thread'i işini pencere yok olduktan sonra bitirir."""
    monkeypatch.setattr(ui, "CLOSE_FLUSH_WAIT_SECONDS", 0.3, raising=False)
    assert app._ensure_chat("Kapanış kaydı")
    chat_id = app._active_chat_id
    assert chat_id is not None
    release = threading.Event()
    real_save = ui.save_chat

    def slow_save(record: ui.ChatRecord, root: Path) -> None:
        assert release.wait(10)
        real_save(record, root)

    monkeypatch.setattr(ui, "save_chat", slow_save)
    with app._writable_transcript():
        app._new_region([("ara metin\n", ("assistant",))])
    app._request_chat_save()  # yazıcı bu kaydı yazarken takılı kalır
    with app._writable_transcript():
        app._new_region([("SON METİN\n", ("assistant",))])
    app._on_close()  # son anlık görüntü, takılı yazmanın ARKASINDA kuyruğa girer
    assert pump_until(app, lambda: app._destroyed, 8)
    release.set()
    deadline = time.monotonic() + 5
    while "SON METİN" not in _saved_text(chat_id) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert "SON METİN" in _saved_text(chat_id)


def test_close_still_closes_the_connections_when_the_agent_ends_with_an_error(app: ui.OmniUI) -> None:
    """Durdurulan ajan hatayla biterse bile model bağlantıları kapatılır (eskiden istisna kapatmayı atlatıyordu)."""
    closed: List[str] = []

    class FakeClient:
        async def close(self) -> None:
            closed.append("istemci")

    app._clients = {"ollama-cloud": FakeClient()}
    crashing: Future[None] = Future()
    app._agent_future = crashing
    threading.Timer(0.3, lambda: crashing.set_exception(RuntimeError("model çöktü"))).start()
    app._on_close()
    assert pump_until(app, lambda: app._destroyed, 8)
    assert closed == ["istemci"]


def test_autosave_runs_on_the_writer_thread_and_last_snapshot_wins(
    app: ui.OmniUI, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kayıt (JSON + fsync) Tk thread'ini bloklamaz; sıralı yazıcıda yazılır ve son anlık görüntü kazanır."""
    assert app._ensure_chat("Yazıcı sohbeti")
    chat_id = app._active_chat_id
    assert chat_id is not None
    release = threading.Event()
    writers: List[str] = []
    written: List[str] = []
    real_save = ui.save_chat

    def slow_save(record: ui.ChatRecord, root: Path) -> None:
        writers.append(threading.current_thread().name)
        written.append("".join(span["text"] for span in record["spans"]))
        assert release.wait(5)
        real_save(record, root)

    monkeypatch.setattr(ui, "save_chat", slow_save)
    with app._writable_transcript():
        app._new_region([("ilk metin\n", ("assistant",))])
    started = time.monotonic()
    app._request_chat_save()
    assert time.monotonic() - started < 0.5  # yazıcı bloklu olsa da Tk beklemez
    with app._writable_transcript():
        app._new_region([("ikinci metin\n", ("assistant",))])
    app._request_chat_save()
    assert app._chat_write is not None and not app._chat_write.done()
    with app._writable_transcript():
        app._new_region([("üçüncü metin\n", ("assistant",))])
    release.set()
    assert app._save_current_chat()  # üçüncü anlık görüntü; FIFO: önceki iki yazma da sırayla biter
    assert set(writers) == {"chat-writer_0"} and len(written) == 3
    assert ("ilk metin" in written[0] and "ikinci metin" not in written[0]
            and "ikinci metin" in written[1] and "üçüncü metin" not in written[1] and "üçüncü metin" in written[2])
    assert "üçüncü metin" in _saved_text(chat_id)  # diskteki son durum SON anlık görüntüdür


def test_background_write_failure_is_reported_and_retried(
    app: ui.OmniUI, monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert app._ensure_chat("Hatalı kayıt")
    real_save = ui.save_chat

    def failing_save(record: ui.ChatRecord, root: Path) -> None:
        raise OSError("disk dolu")

    monkeypatch.setattr(ui, "save_chat", failing_save)
    app._request_chat_save()
    assert pump_until(app, lambda: "Sohbet kaydedilemedi" in app._chat_notice.cget("text"), 3)
    assert app._chat_dirty  # sonraki karede yeniden denenir
    monkeypatch.setattr(ui, "save_chat", real_save)
    app._chat_last_save = 0.0
    assert pump_until(app, lambda: app._chat_notice.cget("text") == "", 5)
    assert not app._chat_dirty


def test_keychain_and_clients_load_after_first_paint_off_the_tk_thread(
    app: ui.OmniUI, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keychain okuması (ACL istemi Tk'yi kilitlerdi) ilk boyamadan SONRA ve arka plan thread'inde yapılır."""
    gate = threading.Event()
    seen: List[Tuple[str, bool]] = []
    fake_clients: dict[str, object] = {"ollama-cloud": object()}

    def slow_keychain() -> None:
        seen.append((threading.current_thread().name, app._first_paint_done))
        assert gate.wait(5)

    monkeypatch.setattr(ui, "apply_stored_api_keys", slow_keychain)
    monkeypatch.setattr(ui, "create_model_clients", lambda: fake_clients)
    assert app._clients == {} and not seen
    app.begin_background_startup()
    assert app._startup_pending
    assert pump_until(app, lambda: bool(seen), 3)
    assert seen == [("omni-startup", True)]
    # İşçi bloklu iken arayüz yanıt verir; görev gönderme ve Ayarlar Keychain okunana dek bekletilir.
    app.entry.insert(0, "hedef")
    app._send_goal()
    assert app._agent_future is None and app.entry.get() == "hedef"
    assert ui.KEYCHAIN_HINT in app.mode_hint.cget("text")
    app._open_settings()
    assert app._settings_window is None
    gate.set()
    assert pump_until(app, lambda: not app._startup_pending, 3)
    assert app._clients is fake_clients
    assert app.mode_hint.cget("text") == "25 tur · 10 dk sınırı"  # ipucu seçili modun sınırına döndü


def test_startup_result_waits_for_the_running_task_and_closes_the_unused_clients(app: ui.OmniUI) -> None:
    """Görev sürerken gelen açılış sonucu çalışan istemcileri değiştirmez; kullanılmayacak istemciler kapatılır."""
    closed: List[str] = []

    class FakeClient:
        async def close(self) -> None:
            closed.append("kullanılmayan")

    running: Future[None] = Future()
    app._agent_future = running
    kept: dict[str, object] = {"ollama-cloud": object()}
    app._clients = kept
    finished: Future[dict[str, object]] = Future()
    finished.set_result({"openai": FakeClient()})
    app._startup_pending = True
    app._apply_startup(finished)  # type: ignore[arg-type]
    assert app._clients is kept and app._clients_stale and not app._startup_pending
    assert pump_until(app, lambda: closed == ["kullanılmayan"], 3)
    running.set_result(None)
    app._agent_future = None


def test_keychain_failure_opens_the_gate_and_is_reported(
    app: ui.OmniUI, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    def broken() -> None:
        raise RuntimeError("keychain reddedildi")

    monkeypatch.setattr(ui, "apply_stored_api_keys", broken)
    with caplog.at_level(logging.ERROR):
        app.begin_background_startup()
        assert pump_until(app, lambda: bool(caplog.records), 3)
    assert not app._startup_pending  # kapı açıldı: görev başında istemciler yeniden kurulur
    assert caplog.records[0].error_type == "RuntimeError"  # type: ignore[attr-defined]


@pytest.fixture
def detached_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Gizli (withdrawn) ana pencerenin geçici (transient) alt penceresi, içinde CTkScrollableFrame varsa Tk'de olay
    döngüsünü kilitler (deneyle doğrulandı; simge durumundaki ana pencere için de geçerli). Testlerin ana penceresi
    gizli olduğundan geçicilik bağı kaldırılır; üretimde ana pencere yanıt penceresinden önce öne alınır.
    """
    monkeypatch.setattr(ui.ctk.CTkToplevel, "transient", lambda self, master=None: "")
    monkeypatch.setattr(ui, "create_confirmation_popup", lambda *args: None)


def _answer_fields(timeout: Optional[float]) -> dict[str, object]:
    fields: dict[str, object] = {"onay": {"type": "boolean", "label": "Devam edilsin mi?", "default": False}}
    if timeout is not None:
        fields[ui.INPUT_TIMEOUT_FIELD] = timeout
    return fields


def _label_texts(widget: tk.Misc) -> List[str]:
    texts: List[str] = []
    for child in widget.winfo_children():
        if isinstance(child, ui.ctk.CTkLabel):
            texts.append(str(child.cget("text")))
        texts.extend(_label_texts(child))
    return texts


def test_hidden_app_defers_the_answer_window_and_notifies_without_content(
    app: ui.OmniUI, monkeypatch: pytest.MonkeyPatch, detached_windows: None,
) -> None:
    """Gizliyken yeni pencere açmak macOS'ta tüm uygulamayı görünür yapar: pencere ertelenir, bildirim verilir."""
    hidden = [True]
    notified: List[object] = []
    bounced: List[bool] = []
    unhidden: List[bool] = []
    monkeypatch.setattr(ui, "application_is_hidden", lambda window: hidden[0])
    monkeypatch.setattr(ui, "notify_input_required", lambda launch: notified.append(launch) or True)
    monkeypatch.setattr(ui, "request_user_attention", lambda: bounced.append(True))
    monkeypatch.setattr(ui, "set_application_hidden", lambda value, window: unhidden.append(value))
    app._input_futures["r1"] = Future()
    app._handle_event({"kind": "user_input_required", "request_id": "r1",
                       "title": "Onay: dosyayı sil", "fields": _answer_fields(900.0)})
    assert len(notified) == 1 and bounced == [True]
    assert not app._input_windows and "r1" in app._deferred_inputs
    assert unhidden == []  # gizli pencere kendiliğinden açılmaz (gizlilik)
    hidden[0] = False  # kullanıcı ⌘⇧X ile geri getirdi
    assert pump_until(app, lambda: "r1" in app._input_windows, 3)
    assert not app._deferred_inputs
    assert unhidden == [False]  # pencere açılmadan önce uygulama öne alındı
    assert any("Yanıt süresi 15 dk" in text for text in _label_texts(app._input_windows["r1"]))


@pytest.mark.parametrize("backgrounded,expected", [(True, []), (False, [])])
def test_visible_app_shows_the_answer_window_without_raising_main_window(
    app: ui.OmniUI, monkeypatch: pytest.MonkeyPatch, detached_windows: None, backgrounded: bool, expected: List[bool],
) -> None:
    raised: List[Tuple[bool, int]] = []
    notified: List[object] = []
    monkeypatch.setattr(ui, "application_is_hidden", lambda window: False)
    monkeypatch.setattr(ui, "is_backgrounded", lambda state, focused, active: backgrounded)
    monkeypatch.setattr(ui, "notify_input_required", lambda launch: notified.append(launch) or True)
    # Öne alma, o an açık yanıt penceresi sayısıyla kaydedilir: pencere AÇILMADAN önce yapılmalı.
    monkeypatch.setattr(ui, "set_application_hidden",
                        lambda value, window: raised.append((value, len(app._input_windows))))
    app._input_futures["r2"] = Future()
    app._handle_event({"kind": "user_input_required", "request_id": "r2",
                       "title": "Onay", "fields": _answer_fields(None)})
    assert "r2" in app._input_windows and not notified
    assert raised == [(value, 0) for value in expected]


def test_answer_window_shows_the_deadline_from_the_request_time(app: ui.OmniUI, detached_windows: None) -> None:
    requested_at = datetime(2026, 9, 29, 14, 17)
    app._input_futures["r3"] = Future()
    app._show_input("r3", "Onay", _answer_fields(900.0), requested_at)
    assert "Yanıt süresi 15 dk · son saat 14:32 · süre dolarsa işlem yapılmaz." in _label_texts(
        app._input_windows["r3"])
    app._show_input("yaşamayan", "Onay", _answer_fields(900.0), requested_at)
    assert list(app._input_windows) == ["r3"]  # süresi dolmuş (yaşamayan) istek için pencere açılmaz


def test_answer_window_closes_and_warns_when_the_request_times_out(
    app: ui.OmniUI, monkeypatch: pytest.MonkeyPatch, detached_windows: None,
) -> None:
    """Onay zaman aşımında (görev işlemsiz sürer) pencere açık kalmaz; kullanıcı transkriptte uyarıyı görür."""
    monkeypatch.setattr(ui, "application_is_hidden", lambda window: False)
    monkeypatch.setattr(ui, "is_backgrounded", lambda state, focused, active: False)
    request = asyncio.run_coroutine_threadsafe(
        app._request_input("Onay gerekiyor", _answer_fields(900.0)), app._loop)
    assert pump_until(app, lambda: bool(app._input_windows), 3)
    request.cancel()  # IntegrationRuntime.wait zaman aşımında bekleyen görevi böyle iptal eder
    assert pump_until(app, lambda: not app._input_windows, 3)
    assert pump_until(app, lambda: "Yanıt penceresi kapandı" in app._text.get("1.0", "end"), 3)


def test_deferred_answer_request_is_dropped_with_a_warning_when_it_times_out(
    app: ui.OmniUI, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gizliyken bekleyen (henüz pencere açılmamış) istek zaman aşımında düşer; kullanıcı uyarıyı transkriptte görür."""
    monkeypatch.setattr(ui, "application_is_hidden", lambda window: True)
    monkeypatch.setattr(ui, "notify_input_required", lambda launch: True)
    monkeypatch.setattr(ui, "request_user_attention", lambda: None)
    request = asyncio.run_coroutine_threadsafe(app._request_input("Onay", _answer_fields(900.0)), app._loop)
    assert pump_until(app, lambda: bool(app._deferred_inputs), 3)
    assert not app._input_windows
    request.cancel()  # IntegrationRuntime.wait zaman aşımında bekleyen görevi böyle iptal eder
    assert pump_until(app, lambda: not app._deferred_inputs, 3)
    assert pump_until(app, lambda: "Yanıt penceresi kapandı" in app._text.get("1.0", "end"), 3)


def test_closing_app_keeps_deferred_answer_windows_deferred(
    app: ui.OmniUI, monkeypatch: pytest.MonkeyPatch, detached_windows: None,
) -> None:
    """Kapanırken (pencere gizlendi) ertelenmiş yanıt penceresi geri açılıp gizli pencereyi görünür yapmaz."""
    monkeypatch.setattr(ui, "application_is_hidden", lambda window: False)
    monkeypatch.setattr(ui, "set_application_hidden", lambda value, window: None)
    app._input_futures["r9"] = Future()
    app._deferred_inputs["r9"] = {"title": "Onay", "fields": _answer_fields(None), "requested_at": datetime.now()}
    app._closing = True
    app._last_menu_check = 0.0
    app._animate(time.monotonic())
    assert "r9" in app._deferred_inputs and not app._input_windows
    app._closing = False  # kontrol: kapanmıyorsa aynı kare pencereyi açar
    app._last_menu_check = 0.0
    app._animate(time.monotonic())
    assert "r9" in app._input_windows and not app._deferred_inputs


def test_visibility_toggle_follows_the_real_application_state(
    app: ui.OmniUI, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dock/⌘Tab ile açılınca ya da simge durumunda bayrak eskir: ilk ⌘⇧X gerçek duruma göre çalışır."""
    applied: List[bool] = []
    monkeypatch.setattr(ui, "set_application_hidden", lambda hidden, window: applied.append(hidden))
    monkeypatch.setattr(ui, "application_is_hidden", lambda window: True)
    app._visibility_hidden = False  # bayrak eskimiş
    app._toggle_visibility()
    assert applied == [False] and not app._visibility_hidden
    monkeypatch.setattr(ui, "application_is_hidden", lambda window: False)
    monkeypatch.setattr(app, "state", lambda: "iconic")
    app._toggle_visibility()
    assert applied == [False, False]  # simge durumundaki pencere geri getirilir
    monkeypatch.setattr(app, "state", lambda: "normal")
    app._toggle_visibility()
    assert applied == [False, False, True] and app._visibility_hidden


def test_runtime_ask_tells_the_answer_sink_how_long_the_user_has() -> None:
    """ask() süre sınırını yanıt alıcısına üst veri alanı olarak iletir: arayüz son saati gösterir, süre dolunca pencereyi kapatır."""
    seen: List[dict[str, object]] = []

    async def sink(title: str, fields: dict[str, object]) -> dict[str, object]:
        seen.append(fields)
        return {"onay": True}

    runtime = integration_runtime.IntegrationRuntime(lambda event: None, lambda: False, sink)
    original: dict[str, object] = {"onay": {"type": "boolean", "label": "Devam?"}}
    asyncio.run(runtime.ask("Onay", original, 900.0))
    asyncio.run(runtime.ask("Soru", original, None))
    assert seen[0] == {**original, integration_runtime.INPUT_TIMEOUT_FIELD: 900.0}
    assert seen[1] == original  # süre sınırı yoksa alanlar değişmez (kurulum akışları)
    assert original == {"onay": {"type": "boolean", "label": "Devam?"}}  # çağıranın sözlüğü değiştirilmez


@pytest.mark.parametrize("hidden,backgrounded,expected", [
    (True, True, "notify"), (True, False, "notify"), (False, True, "raise"), (False, False, "none"),
])
def test_input_alert_policy_table(hidden: bool, backgrounded: bool, expected: str) -> None:
    assert ui.input_alert_action(hidden, backgrounded) == expected


def test_input_notification_has_fixed_text_and_no_task_content(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    calls: List[Tuple[Tuple[object, ...], dict[str, object]]] = []
    assert ui.notify_input_required(lambda *args, **options: calls.append((args, options)))
    args, options = calls[0]
    assert args[0] == ["/usr/bin/osascript", "-e",
                       'display notification "Yanıtınız bekleniyor. Pencereyi göstermek için ⌘⇧X." '
                       'with title "OmniAgent"']
    env = options["env"]
    assert isinstance(env, dict) and env["PATH"] == "/usr/bin:/bin"
    monkeypatch.setattr(sys, "platform", "linux")
    assert not ui.notify_input_required(lambda *args, **options: None)


def test_input_notification_failure_is_logged_with_structured_fields(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """osascript başlatılamazsa bildirim atlanır ama sessizce yutulmaz: neden yapısal alanlarla günlüğe yazılır."""
    monkeypatch.setattr(sys, "platform", "darwin")

    def broken_launch(*args: object, **options: object) -> object:
        raise FileNotFoundError("osascript bulunamadı")

    with caplog.at_level(logging.WARNING):
        assert not ui.notify_input_required(broken_launch)
    record = caplog.records[0]
    assert record.getMessage() == "Yanıt bekleyen istek bildirimi başlatılamadı"
    assert record.error_type == "FileNotFoundError" and "osascript bulunamadı" in record.error  # type: ignore[attr-defined]


@pytest.mark.parametrize("success,stopped,error,expected", [
    (True, False, "", "done"), (False, False, "", "failed"), (True, True, "", "stopped"), (False, False, "Boom", "failed"),
])
def test_finished_run_records_its_outcome_for_the_sidebar_dot(
    app: ui.OmniUI, monkeypatch: pytest.MonkeyPatch, success: bool, stopped: bool, error: str, expected: str,
) -> None:
    monkeypatch.setattr(ui, "set_dock_badge", lambda badge: None)
    assert app._ensure_chat("Sonuç sohbeti")
    chat_id = app._active_chat_id
    assert chat_id is not None
    app._active_goal = "Sonuç sohbeti"
    if stopped:
        app._stop_event.set()
    report = None if error else {"success": success, "exchange": make_exchange("Sonuç sohbeti", "cevap", [])}
    app._on_run_done(error, report)  # type: ignore[arg-type]
    assert app._chat_index[0]["last_outcome"] == expected
    assert ui.load_catalog()[0][0]["last_outcome"] == expected  # dizine yazıldı
    dot = app._chat_rows[chat_id]["marker"]
    assert (dot.cget("text"), dot.cget("text_color")) == (("●", ui.ERROR) if expected == "failed" else ("", ui.TEXT_FAINT))


def test_new_run_clears_the_old_outcome_and_shows_the_running_dot(
    app: ui.OmniUI, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ui, "set_dock_badge", lambda badge: None)

    async def hang(goal: str, options: object) -> None:
        await asyncio.sleep(30)

    monkeypatch.setattr(app, "_run_exclusive", hang)
    assert app._ensure_chat("Tekrar denenen görev")
    chat_id = app._active_chat_id
    assert chat_id is not None
    app._on_run_done("Boom", None)
    assert app._chat_index[0]["last_outcome"] == "failed"
    app.entry.insert(0, "Tekrar denenen görev")
    app._send_goal()
    assert "last_outcome" not in app._chat_index[0]
    assert app._chat_rows[chat_id]["look"]["tone"] == "running"
    assert app._chat_rows[chat_id]["marker"].cget("text_color") == ui.ACCENT
    assert app._agent_future is not None
    app._agent_future.cancel()


def test_failed_chat_creation_keeps_the_previous_failed_dot(
    app: ui.OmniUI, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Görev başlatılamazsa (dizin yazılamadı) önceki sonucun kırmızı noktası silinmez."""
    monkeypatch.setattr(ui, "set_dock_badge", lambda badge: None)
    assert app._ensure_chat("Yeniden denenecek görev")
    chat_id = app._active_chat_id
    assert chat_id is not None
    app._on_run_done("Boom", None)
    assert app._chat_index[0]["last_outcome"] == "failed"

    def broken_catalog(chats: object, active_id: object) -> None:
        raise OSError("disk dolu")

    monkeypatch.setattr(ui, "save_catalog", broken_catalog)
    app.entry.insert(0, "Yeniden denenecek görev")
    app._send_goal()
    assert app._agent_future is None and app.entry.get() == "Yeniden denenecek görev"  # görev başlamadı
    assert app._chat_index[0]["last_outcome"] == "failed" and app._chat_record is not None
    assert app._chat_record["last_outcome"] == "failed"
    marker = app._chat_rows[chat_id]["marker"]
    assert (marker.cget("text"), marker.cget("text_color")) == ("●", ui.ERROR)
