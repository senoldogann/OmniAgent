"""Sesli composer ve transkript kopyalama davranışının hafif testleri."""
from __future__ import annotations

import os
from typing import Any

import pytest

from omniagent.ui import app as ui


class FakeText:
    def __init__(self, value: str) -> None:
        self.value = value

    def get(self, _start: str, _end: str) -> str:
        return self.value


class FakeButton:
    def __init__(self) -> None:
        self.options: dict[str, Any] = {}

    def configure(self, **options: Any) -> None:
        self.options.update(options)


class FakeClipboardUI:
    def __init__(self, value: str) -> None:
        self._text = FakeText(value)
        self.copy_btn = FakeButton()
        self.clipboard = "eski pano"
        self.after_callback: Any = None
        self.updated = False

    def clipboard_clear(self) -> None:
        self.clipboard = ""

    def clipboard_append(self, value: str) -> None:
        self.clipboard = value

    def update_idletasks(self) -> None:
        self.updated = True

    def after(self, _delay: int, callback: Any) -> None:
        self.after_callback = callback

    def _restore_copy_button(self) -> None:
        ui.OmniUI._restore_copy_button(self)  # type: ignore[arg-type]


class FakeEntry:
    def __init__(self, value: str = "") -> None:
        self.value = value
        self.state = "normal"
        self.focused = False

    def configure(self, **options: str) -> None:
        if "state" in options:
            self.state = options["state"]

    def delete(self, _start: int, _end: str) -> None:
        self.value = ""

    def insert(self, _index: int, value: str) -> None:
        self.value = value

    def get(self) -> str:
        return self.value

    def focus_set(self) -> None:
        self.focused = True


class FakeComposerUI:
    def __init__(self) -> None:
        self.entry = FakeEntry("mevcut taslak")
        self._voice_base_text = "mevcut taslak"
        self._voice_partial_text = ""

    def _replace_entry_text(self, text: str, disabled: bool = False) -> None:
        ui.OmniUI._replace_entry_text(self, text, disabled)  # type: ignore[arg-type]


def test_copy_transcript_copies_every_visible_line_and_restores_button() -> None:
    app = FakeClipboardUI("satır bir\nsatır iki\n")
    ui.OmniUI._copy_transcript(app)  # type: ignore[arg-type]
    assert app.clipboard == "satır bir\nsatır iki\n"
    assert app.updated is True
    assert app.copy_btn.options["fg_color"] == ui.SUCCESS
    assert app.after_callback is not None
    app.after_callback()
    assert app.copy_btn.options["fg_color"] == "transparent"


def test_copy_empty_transcript_preserves_clipboard() -> None:
    app = FakeClipboardUI("  \n")
    ui.OmniUI._copy_transcript(app)  # type: ignore[arg-type]
    assert app.clipboard == "eski pano"
    assert app.after_callback is None


def test_partial_replaces_voice_segment_and_final_keeps_one_copy() -> None:
    app = FakeComposerUI()
    ui.OmniUI._insert_voice_partial(app, "ilk")  # type: ignore[arg-type]
    assert app.entry.value == "mevcut taslak ilk"
    assert app.entry.state == "disabled"
    ui.OmniUI._insert_voice_partial(app, "ilk ve ikinci")  # type: ignore[arg-type]
    assert app.entry.value == "mevcut taslak ilk ve ikinci"
    ui.OmniUI._insert_voice_text(app, "ilk ve ikinci")  # type: ignore[arg-type]
    assert app.entry.value == "mevcut taslak ilk ve ikinci"
    assert app.entry.state == "normal"
    assert app._voice_partial_text == ""


@pytest.mark.skipif(os.environ.get("OMNI_UI_TEST") != "1", reason="Gerçek Tk testi OMNI_UI_TEST=1 ile etkinleştirilir.")
def test_svg_icons_are_used_by_real_controls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ui, "create_model_clients", lambda: {})
    window = ui.OmniUI()
    try:
        window.withdraw()
        assert ui.MICROPHONE_SVG.strip().startswith("<svg")
        assert ui.COPY_SVG.strip().startswith("<svg")
        assert window.voice_btn.cget("text") == ""
        assert window.copy_btn.cget("text") == ""
        assert window.voice_btn.cget("image") is not None
        assert window.copy_btn.cget("image") is not None
    finally:
        window._on_close()
