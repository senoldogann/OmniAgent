"""Üst durum göstergesinin Tk açmadan doğrulanması."""
from __future__ import annotations
from omniagent.ui import app as ui

class FakeLabel:
    def __init__(self) -> None:
        self.value = {}
    def configure(self, **kwargs) -> None:
        self.value = kwargs

class FakeUI:
    def __init__(self) -> None:
        self._spinner_index = 1
        self.task_status_label = FakeLabel()
        self._task_status = "idle"

def test_status_pill_shows_elapsed_and_completion() -> None:
    app = FakeUI()
    ui.OmniUI._set_task_status(app, "running", 4.8)
    assert app.task_status_label.value["text"] == "✢ 4 sn"
    ui.OmniUI._set_task_status(app, "done", 8.3)
    assert app.task_status_label.value == {"text": "✓ 8 sn", "text_color": ui.SUCCESS}


def test_stopped_background_task_removes_menu_spinner(monkeypatch) -> None:
    shown = []
    hidden = []
    app = FakeUI()
    app.state = lambda: "normal"
    app.focus_displayof = lambda: None
    app._task_status = "stopped"
    app._badge_pending = False
    app._menu_status = type("FakeMenu", (), {
        "show": lambda self, *args: shown.append(args),
        "hide": lambda self: hidden.append(True),
    })()
    monkeypatch.setattr(ui, "app_is_active", lambda: False)
    ui.OmniUI._sync_menu_status(app)
    assert not shown
    assert hidden == [True]
