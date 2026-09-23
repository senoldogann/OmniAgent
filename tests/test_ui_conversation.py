"""Gerçek Tk bileşenlerinde akış, yeniden deneme ve bağlam temizleme duman testleri."""
import os
from concurrent.futures import Future
from typing import Iterator

import pytest

import ui
from config import DEFAULT_BACKEND
from conversation import make_exchange
from main import ZERO_USAGE


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> Iterator[ui.OmniUI]:
    if os.environ.get("OMNI_UI_TEST") != "1":
        pytest.skip("Gerçek Tk testi OMNI_UI_TEST=1 ile etkinleştirilir.")
    monkeypatch.setattr(ui, "create_model_clients", lambda: {})
    window = ui.OmniUI()
    window.withdraw()
    window._text.configure(state="normal")
    yield window
    window._on_close()


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
