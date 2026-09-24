"""macOS Dock rozeti ve arka plan görev bildirimi."""
from __future__ import annotations
import subprocess
import sys
from typing import Any, Callable, Optional

def set_dock_badge(label: Optional[str]) -> None:
    """Dock rozetini günceller; destek yoksa görevi kesmez."""
    if sys.platform != "darwin":
        return
    try:
        import AppKit
        AppKit.NSApplication.sharedApplication().dockTile().setBadgeLabel_(label)
    except Exception:
        return

def app_is_active() -> Optional[bool]:
    """Etkinlik durumunu okur; Tk yedeği için bilinmeyende None döndürür."""
    if sys.platform != "darwin":
        return None
    try:
        import AppKit
        return bool(AppKit.NSApplication.sharedApplication().isActive())
    except Exception:
        return None

def is_backgrounded(state: str, focused: bool, app_active: Optional[bool]) -> bool:
    """Küçültülmüş veya etkin olmayan pencereyi arka plan sayar."""
    if state == "iconic":
        return True
    return not app_active if app_active is not None else not focused

def notify_finished(success: bool, stopped: bool, seconds: float,
                    launch: Optional[Callable[..., object]] = None) -> bool:
    """Görev içeriğini içermeyen macOS bildirimini başlatır."""
    if sys.platform != "darwin" or stopped:
        return False
    state = "tamamlandı" if success else "tamamlanamadı"
    duration = max(0, min(int(seconds), 86400))
    script = f'display notification "Görev {duration} saniyede {state}." with title "OmniAgent"'
    try:
        (launch or subprocess.Popen)(["/usr/bin/osascript", "-e", script],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     env={"PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8"}, close_fds=True)
    except OSError:
        return False
    return True


class MenuBarTaskStatus:
    """Görev boyunca macOS menü çubuğunda geçici durum öğesi tutar."""

    def __init__(self) -> None:
        self._bar: Optional[Any] = None
        self._item: Optional[Any] = None
        self._displayed: Optional[tuple[str, str]] = None

    def show(self, title: str, tooltip: str) -> None:
        if sys.platform != "darwin":
            return
        try:
            import AppKit
            if self._item is None:
                self._bar = AppKit.NSStatusBar.systemStatusBar()
                self._item = self._bar.statusItemWithLength_(AppKit.NSVariableStatusItemLength)
            if self._displayed == (title, tooltip):
                return
            button = self._item.button()
            button.setTitle_(title)
            button.setToolTip_(tooltip)
            self._displayed = (title, tooltip)
        except Exception:
            self.hide()

    def hide(self) -> None:
        if self._item is None:
            return
        try:
            self._bar.removeStatusItem_(self._item)
        except Exception:
            pass
        self._item = None
        self._bar = None
        self._displayed = None
