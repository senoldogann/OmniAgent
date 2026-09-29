"""Masaüstü görünümünün kalıcılığı ve boş sohbet yerleşimi."""
from pathlib import Path

import pytest

from omniagent.ui import app as ui
from tests.test_ui_conversation import close_window
from omniagent.ui.appearance import (
    default_appearance, load_appearance, save_appearance, validate_appearance,
)


def test_appearance_round_trip_and_bounds(tmp_path: Path) -> None:
    path = tmp_path / "appearance.json"
    assert load_appearance(path) == default_appearance()
    selected = validate_appearance({
        "font_family": "Avenir Next", "font_size": 16,
        "transparent_window": True, "opacity": 0.78, "compact_sidebar": True,
    })
    save_appearance(selected, path)
    assert load_appearance(path) == selected
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError):
        validate_appearance({**selected, "opacity": 0.3})


def test_appearance_applies_to_open_ui_and_empty_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(ui, "create_model_clients", lambda: {})
    window = ui.OmniUI()
    window.withdraw()
    try:
        assert window._sidebar.cget("fg_color") == ui.SURFACE
        assert window._text.get("1.0", "end-1c") == ""
        assert window._empty_state.winfo_manager() == "place"
        assert window.stats_label.winfo_manager() == ""

        selected = {**default_appearance(), "font_size": 16,
                    "transparent_window": True, "opacity": 0.78, "compact_sidebar": True}
        assert window._apply_appearance(selected)
        assert window._body.actual("size") == ui.READING_SIZE + 3  # ayar 13 → 16: 3 punto kaydı
        assert float(window.attributes("-alpha")) == pytest.approx(0.78)
        assert load_appearance(tmp_path / "appearance.json") == selected
        window._text.configure(state="normal")
        window._render_goal("Bir görev")
        window._text.configure(state="disabled")
        assert window._empty_state.winfo_manager() == ""
    finally:
        close_window(window)
