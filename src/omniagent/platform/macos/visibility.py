"""OmniAgent penceresi için macOS genel görünürlük kısayolu."""
from __future__ import annotations

import ctypes
import logging
import sys
from typing import Any, Callable, Optional


_COMMAND_MODIFIER = 1 << 8
# Salt ⌘X, sistemin Kes kısayoluyla birebir çakışıyordu: T3 Code gibi bir uygulamada metin
# keserken pencere de gizlenip gösteriliyordu. Shift eklenip çakışma kaldırıldı.
_SHIFT_MODIFIER = 1 << 9
_X_KEY_CODE = 0x07
_KEYBOARD_EVENT_CLASS = 0x6B657962  # 'keyb'
_HOTKEY_PRESSED = 5
_HOTKEY_SIGNATURE = 0x4F4D4E49  # 'OMNI'
_EVENT_NOT_HANDLED = -9874


class _EventTypeSpec(ctypes.Structure):
    _fields_ = [("event_class", ctypes.c_uint32), ("event_kind", ctypes.c_uint32)]


class _EventHotKeyID(ctypes.Structure):
    _fields_ = [("signature", ctypes.c_uint32), ("identifier", ctypes.c_uint32)]


_EventHandler = ctypes.CFUNCTYPE(
    ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
)


class VisibilityHotkeyError(RuntimeError):
    """Sistem genelindeki görünürlük kısayolu kullanılamıyor."""


class GlobalVisibilityHotkey:
    """Carbon kaydını yaşatır; kapatılırken native kaynakları serbest bırakır."""

    def __init__(self, on_press: Callable[[], None], carbon: Optional[Any] = None) -> None:
        self._carbon: Optional[Any] = None
        self._handler_proc: Optional[Any] = None
        self._handler_ref = ctypes.c_void_p()
        self._hotkey_ref = ctypes.c_void_p()
        self.available: bool = False
        if sys.platform != "darwin" and carbon is None:
            return
        library = carbon or ctypes.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")
        self._carbon = library
        library.GetApplicationEventTarget.restype = ctypes.c_void_p
        library.InstallEventHandler.argtypes = [
            ctypes.c_void_p, _EventHandler, ctypes.c_uint32,
            ctypes.POINTER(_EventTypeSpec), ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        library.InstallEventHandler.restype = ctypes.c_int32
        library.RegisterEventHotKey.argtypes = [
            ctypes.c_uint32, ctypes.c_uint32, _EventHotKeyID,
            ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p),
        ]
        library.RegisterEventHotKey.restype = ctypes.c_int32
        library.UnregisterEventHotKey.argtypes = [ctypes.c_void_p]
        library.UnregisterEventHotKey.restype = ctypes.c_int32
        library.RemoveEventHandler.argtypes = [ctypes.c_void_p]
        library.RemoveEventHandler.restype = ctypes.c_int32
        library.GetEventParameter.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p,
        ]
        library.GetEventParameter.restype = ctypes.c_int32

        def handle(_next: object, event: object, _data: object) -> int:
            hotkey_id = _EventHotKeyID()
            status = library.GetEventParameter(
                event, 0x2D2D2D2D, 0x686B6964, None,  # '----', 'hkid'
                ctypes.sizeof(hotkey_id), None, ctypes.byref(hotkey_id),
            )
            if status or hotkey_id.signature != _HOTKEY_SIGNATURE or hotkey_id.identifier != 1:
                return _EVENT_NOT_HANDLED
            try:
                on_press()
            except Exception:
                logging.exception("Görünürlük kısayolu işlenemedi")
            return 0

        self._handler_proc = _EventHandler(handle)
        event_spec = _EventTypeSpec(_KEYBOARD_EVENT_CLASS, _HOTKEY_PRESSED)
        target = library.GetApplicationEventTarget()
        status = library.InstallEventHandler(
            target, self._handler_proc, 1, ctypes.byref(event_spec),
            None, ctypes.byref(self._handler_ref),
        )
        if status:
            raise VisibilityHotkeyError(f"⌘X olay dinleyicisi kurulamadı (OSStatus {status}).")
        status = library.RegisterEventHotKey(
            _X_KEY_CODE, _COMMAND_MODIFIER | _SHIFT_MODIFIER, _EventHotKeyID(_HOTKEY_SIGNATURE, 1),
            target, 1, ctypes.byref(self._hotkey_ref),
        )
        if status:
            self.close()
            raise VisibilityHotkeyError(f"Genel ⌘⇧X kaydedilemedi (OSStatus {status}).")
        self.available = True

    def close(self) -> None:
        """Önce hotkey'i, sonra geri çağrıyı kaldır; ikinci çağrı zararsızdır."""
        library = self._carbon
        if library is not None and self._hotkey_ref.value:
            library.UnregisterEventHotKey(self._hotkey_ref)
            self._hotkey_ref = ctypes.c_void_p()
        if library is not None and self._handler_ref.value:
            library.RemoveEventHandler(self._handler_ref)
            self._handler_ref = ctypes.c_void_p()
        self.available = False
        self._handler_proc = None


def set_application_hidden(hidden: bool, window: Any) -> None:
    """Tk ana thread'inde bütün macOS pencerelerini gizle veya geri getir."""
    if sys.platform != "darwin":
        window.withdraw() if hidden else window.deiconify()
        return
    import AppKit

    app = AppKit.NSApplication.sharedApplication()
    if hidden:
        app.hide_(None)
    else:
        app.unhide_(None)
        window.deiconify()
        window.lift()
        app.activate()


def application_is_hidden(window: Any) -> bool:
    """Uygulamanın GERÇEK gizli durumunu işletim sisteminden okur (bayrak tutmaz); Tk ana thread'inde çağrılır."""
    if sys.platform != "darwin":
        return window.state() == "withdrawn"
    import AppKit

    return bool(AppKit.NSApplication.sharedApplication().isHidden())
