"""Masaüstü görev durumu testleri."""
import desktop_status

def test_background_detection() -> None:
    assert desktop_status.is_backgrounded("iconic", True, True)
    assert desktop_status.is_backgrounded("normal", True, False)
    assert not desktop_status.is_backgrounded("normal", False, True)
    assert desktop_status.is_backgrounded("normal", False, None)

def test_notification_fixed_text(monkeypatch) -> None:
    monkeypatch.setattr(desktop_status.sys, "platform", "darwin")
    calls = []
    assert desktop_status.notify_finished(True, False, 12.9, lambda *a, **k: calls.append((a, k)))
    args, options = calls[0]
    assert args[0] == ["/usr/bin/osascript", "-e",
                       'display notification "Görev 12 saniyede tamamlandı." with title "OmniAgent"']
    assert options["env"]["PATH"] == "/usr/bin:/bin"
    assert not desktop_status.notify_finished(False, True, 2)


def test_menu_bar_item_lifetime(monkeypatch) -> None:
    from types import SimpleNamespace
    import sys
    monkeypatch.setattr(desktop_status.sys, "platform", "darwin")
    titles = []
    removed = []
    button = SimpleNamespace(setTitle_=titles.append, setToolTip_=lambda text: None)
    item = SimpleNamespace(button=lambda: button)
    bar = SimpleNamespace(statusItemWithLength_=lambda length: item,
                          removeStatusItem_=removed.append)
    appkit = SimpleNamespace(NSStatusBar=SimpleNamespace(systemStatusBar=lambda: bar),
                             NSVariableStatusItemLength=-1)
    monkeypatch.setitem(sys.modules, "AppKit", appkit)
    status = desktop_status.MenuBarTaskStatus()
    status.show("✻ ✢", "Çalışıyor")
    status.show("✻ ✓", "Bitti")
    assert titles == ["✻ ✢", "✻ ✓"]
    status.hide()
    assert removed == [item]
