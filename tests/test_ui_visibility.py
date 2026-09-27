"""Global görünürlük kısayolu ve UI'siz ekran yakalama sözleşmesi."""
from unittest.mock import Mock

import pytest

from omniagent.platform.macos.visibility import GlobalVisibilityHotkey, VisibilityHotkeyError
from omniagent.tools import screen
from omniagent.tools.types import ToolError


def _fake_carbon(register_status: int = 0) -> Mock:
    library = Mock()
    library.GetApplicationEventTarget.return_value = 9

    def install(*arguments: object) -> int:
        arguments[-1]._obj.value = 11  # type: ignore[attr-defined]
        return 0

    def register(*arguments: object) -> int:
        if register_status == 0:
            arguments[-1]._obj.value = 12  # type: ignore[attr-defined]
        return register_status

    def parameter(*arguments: object) -> int:
        arguments[-1]._obj.signature = 0x4F4D4E49  # type: ignore[attr-defined]
        arguments[-1]._obj.identifier = 1  # type: ignore[attr-defined]
        return 0

    library.InstallEventHandler.side_effect = install
    library.RegisterEventHotKey.side_effect = register
    library.GetEventParameter.side_effect = parameter
    return library


def test_global_hotkey_registers_callback_and_releases_native_resources() -> None:
    library = _fake_carbon()
    pressed: list[bool] = []
    hotkey = GlobalVisibilityHotkey(lambda: pressed.append(True), carbon=library)
    assert hotkey.available
    arguments = library.RegisterEventHotKey.call_args.args
    assert arguments[0:2] == (0x07, 1 << 8)
    assert arguments[4] == 1  # Genel kısayol başka uygulamada da geçerli.
    assert hotkey._handler_proc(None, None, None) == 0
    assert pressed == [True]
    hotkey.close()
    hotkey.close()
    library.UnregisterEventHotKey.assert_called_once()
    library.RemoveEventHandler.assert_called_once()


def test_failed_hotkey_registration_cleans_handler() -> None:
    library = _fake_carbon(register_status=-9876)
    with pytest.raises(VisibilityHotkeyError, match="kaydedilemedi"):
        GlobalVisibilityHotkey(lambda: None, carbon=library)
    library.RemoveEventHandler.assert_called_once()
    library.UnregisterEventHotKey.assert_not_called()


def _window(title: str, owner: str, pid: int, number: int) -> dict[str, object]:
    return {
        "kCGWindowName": title, "kCGWindowOwnerName": owner,
        "kCGWindowOwnerPID": pid, "kCGWindowNumber": number,
    }


def test_window_selection_uses_title_and_excludes_every_matching_pid() -> None:
    windows = [
        _window("Chrome", "Google Chrome", 20, 1),
        _window("OmniAgent — Ayarlar", "Python", 7, 2),
        _window("OmniAgent — Yanıt gerekiyor", "Python", 7, 3),
        _window("başka Python", "Python", 30, 4),
    ]
    assert screen._capture_window_ids(windows) == (1, 4)
    assert screen._capture_window_ids(windows[:1]) is None


def test_display_capture_filters_ui_and_does_not_fallback_to_raw_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    windows = [_window("OmniAgent", "Python", 7, 2), _window("Chrome", "Google Chrome", 20, 3)]
    filtered = object()
    monkeypatch.setattr(screen.Quartz, "CGWindowListCopyWindowInfo", lambda *_: windows)
    monkeypatch.setattr(screen.Quartz, "CGDisplayBounds", lambda *_: "bounds")
    from_array = Mock(return_value=filtered)
    raw = Mock(return_value=object())
    monkeypatch.setattr(screen.Quartz, "CGWindowListCreateImageFromArray", from_array)
    monkeypatch.setattr(screen.Quartz, "CGWindowListCreateImage", raw)
    assert screen._display_image(1, 0) is filtered
    assert from_array.call_args.args[1] == [3]
    raw.assert_not_called()

    from_array.return_value = None
    with pytest.raises(ToolError) as failure:
        screen._display_image(1, 0)
    assert failure.value.code == "SCREEN_CAPTURE_FILTER_FAILED"
    raw.assert_not_called()

    monkeypatch.setattr(screen.Quartz, "CGWindowListCopyWindowInfo", lambda *_: windows[1:])
    assert screen._display_image(1, 0) is raw.return_value


def test_chrome_capture_skips_tiny_helper_window(monkeypatch: pytest.MonkeyPatch) -> None:
    def chrome_window(number: int, width: int, height: int) -> dict[str, object]:
        return {
            screen.Quartz.kCGWindowOwnerName: "Google Chrome",
            screen.Quartz.kCGWindowOwnerPID: 50,
            screen.Quartz.kCGWindowNumber: number,
            screen.Quartz.kCGWindowLayer: 0,
            screen.Quartz.kCGWindowBounds: {"X": 0, "Y": 0, "Width": width, "Height": height},
        }

    windows = [chrome_window(10, 189, 22), chrome_window(11, 1710, 988)]
    monkeypatch.setattr(screen, "_require_screen_capture", lambda: None)
    monkeypatch.setattr(screen.Quartz, "CGWindowListCopyWindowInfo", lambda *_: windows)
    capture = Mock(return_value=object())
    monkeypatch.setattr(screen.Quartz, "CGWindowListCreateImageFromArray", capture)
    image, geometry = screen._front_app_window_image("Google Chrome", 0)
    assert image is capture.return_value
    assert geometry["point_width"] == 1710
    assert geometry["point_height"] == 988
    assert capture.call_args.args[1] == [11]
