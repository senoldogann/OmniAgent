"""Gerçek Tk bileşenlerinde akış, yeniden deneme ve bağlam temizleme duman testleri."""
import json
import os
from pathlib import Path
import time
from concurrent.futures import Future
from typing import Any, Iterator

import pytest

from omniagent.ui import app as ui
from omniagent.app import agent as main
from omniagent.config import DEFAULT_BACKEND
from omniagent.core.conversation import make_exchange
from omniagent.app.agent import ZERO_USAGE


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> Iterator[ui.OmniUI]:
    if os.environ.get("OMNI_UI_TEST") != "1":
        pytest.skip("Gerçek Tk testi OMNI_UI_TEST=1 ile etkinleştirilir.")
    monkeypatch.setattr(ui, "create_model_clients", lambda: {})
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
    assert "toplam 120 token" in app.stats_label.cget("text")
    assert "✓ Tamamlandı" in app._text.get("1.0", "end")


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
