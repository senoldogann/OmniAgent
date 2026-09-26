"""Uzaktan kullanım: kilitli ekrana yazılmaz, uyuyan ekran uyandırılır, köprü Mac'i uyanık tutar."""
import logging
import os
import subprocess
import sys
import time
from types import SimpleNamespace
from typing import Any, Iterator, List

import pytest

from omniagent import tools
from omniagent.platform.macos import power
from omniagent.tools import ToolError, Toolbox, gui_input, screen
from omniagent.tools.types import ScreenSession

UNLOCKED: ScreenSession = {"locked": False, "on_console": True, "asleep": False}
LOCKED: ScreenSession = {"locked": True, "on_console": True, "asleep": False}


def _quartz(session: Any, asleep: int = 0) -> SimpleNamespace:
    return SimpleNamespace(CGSessionCopyCurrentDictionary=lambda: session,
                           CGDisplayIsAsleep=lambda display: asleep, CGMainDisplayID=lambda: 1)


def test_session_state_is_read_from_the_session_dictionary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(screen, "Quartz", _quartz({"CGSSessionScreenIsLocked": 1, "kCGSSessionOnConsoleKey": 1}, 1))
    assert screen.screen_session() == {"locked": True, "on_console": True, "asleep": True}
    monkeypatch.setattr(screen, "Quartz", _quartz({"kCGSSessionOnConsoleKey": 0}))
    assert screen.screen_session() == {"locked": False, "on_console": False, "asleep": False}
    # GUI oturumu yok ya da API hata verdi: bilinmeyen durum ekran araçlarını engellemez
    monkeypatch.setattr(screen, "Quartz", _quartz(None))
    assert screen.screen_session() == UNLOCKED

    def broken() -> None:
        raise RuntimeError("oturum yok")

    monkeypatch.setattr(screen, "Quartz", SimpleNamespace(CGSessionCopyCurrentDictionary=broken))
    assert screen.screen_session() == UNLOCKED


def test_locked_screen_stops_input_before_anything_is_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kilit ekranında yazılan metin parola alanına gider: hiçbir olay gönderilmeden durulmalı."""
    typed: List[str] = []
    monkeypatch.setattr(screen, "screen_session", lambda: LOCKED)
    monkeypatch.setattr(gui_input, "AX", SimpleNamespace(AXIsProcessTrusted=lambda: True))
    monkeypatch.setattr(tools, "screen_capture_granted", lambda request=False: False)
    monkeypatch.setattr(tools, "type_unicode_text", lambda text: typed.append(text))
    with pytest.raises(ToolError) as error:
        Toolbox().cua_type_text("parola123")
    assert error.value.code == "SCREEN_LOCKED" and not error.value.recoverable
    assert "parola denemeyin" in str(error.value) and typed == []

    monkeypatch.setattr(tools, "screen_capture_granted", lambda request=False: True)
    with pytest.raises(ToolError, match="kilitli"):
        tools._require_screen_capture()
    monkeypatch.setattr(screen, "screen_session", lambda: {**UNLOCKED, "on_console": False})
    with pytest.raises(ToolError, match="başka bir kullanıcı"):
        gui_input._require_accessibility()
    monkeypatch.setattr(screen, "screen_session", lambda: UNLOCKED)
    gui_input._require_accessibility()
    tools._require_screen_capture()


@pytest.fixture
def launched(monkeypatch: pytest.MonkeyPatch) -> Iterator[List[List[str]]]:
    commands: List[List[str]] = []
    monkeypatch.setattr(screen.subprocess, "Popen", lambda command, **options: commands.append(command))
    yield commands


def _states(monkeypatch: pytest.MonkeyPatch, *states: ScreenSession) -> None:
    sequence = iter(states)
    monkeypatch.setattr(screen, "screen_session", lambda: next(sequence))


def test_sleeping_display_is_woken_only_when_unlocked(monkeypatch: pytest.MonkeyPatch, launched: List[List[str]]) -> None:
    asleep = {**UNLOCKED, "asleep": True}
    _states(monkeypatch, asleep, UNLOCKED, UNLOCKED)
    screen.require_unlocked_screen()
    assert launched == [["/usr/bin/caffeinate", "-u", "-t", "2"]]

    # Ekran açılınca kilit ekranı çıktı: yine de yazılmaz
    _states(monkeypatch, asleep, UNLOCKED, LOCKED)
    with pytest.raises(ToolError, match="kilitli"):
        screen.require_unlocked_screen()

    # Kilitli ve uyuyan ekran boşuna aydınlatılmaz
    launched.clear()
    _states(monkeypatch, {**LOCKED, "asleep": True})
    with pytest.raises(ToolError, match="kilitli"):
        screen.require_unlocked_screen()
    assert launched == []


def test_keep_awake_is_tied_to_the_bridge_process(monkeypatch: pytest.MonkeyPatch) -> None:
    assert power.keep_awake_command(4242) == ["/usr/bin/caffeinate", "-s", "-w", "4242"]
    started: List[List[str]] = []
    monkeypatch.setattr(power.sys, "platform", "darwin")
    monkeypatch.setattr(power.subprocess, "Popen", lambda command, **options: started.append(command) or "süreç")
    assert power.start_keep_awake(4242) == "süreç" and started == [power.keep_awake_command(4242)]

    def missing(command: List[str], **options: Any) -> None:
        raise FileNotFoundError(command[0])

    monkeypatch.setattr(power.subprocess, "Popen", missing)
    assert power.start_keep_awake(4242) is None
    monkeypatch.setattr(power.sys, "platform", "linux")
    assert power.start_keep_awake(4242) is None


def test_stopping_keep_awake_ends_the_process() -> None:
    process = subprocess.Popen(["sleep", "30"])
    power.stop_keep_awake(process)
    assert process.returncode is not None
    power.stop_keep_awake(process)  # bitmiş süreç ve None sessizce geçilir
    power.stop_keep_awake(None)


@pytest.mark.skipif(sys.platform != "darwin", reason="caffeinate ve pmset yalnız macOS'ta")
def test_real_keep_awake_assertion_is_created_and_released() -> None:
    process = power.start_keep_awake(os.getpid())
    assert process is not None
    owner = f"pid {process.pid}(caffeinate)"
    try:
        deadline = time.monotonic() + 5
        assertions = ""
        while owner not in assertions and time.monotonic() < deadline:
            time.sleep(0.2)
            assertions = subprocess.run(["pmset", "-g", "assertions"], capture_output=True, text=True).stdout
        held = [line for line in assertions.splitlines() if owner in line]
        assert held and "PreventSystemSleep" in held[0], assertions[-1500:]
    finally:
        power.stop_keep_awake(process)
    assert process.returncode is not None


@pytest.mark.skipif(sys.platform != "darwin", reason="gerçek Quartz oturum sözlüğü yalnız macOS'ta")
def test_real_session_dictionary_is_read_without_fallback(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        state = screen.screen_session()
    assert "Oturum durumu okunamadı" not in caplog.text
    assert set(state) == {"locked", "on_console", "asleep"} and all(isinstance(value, bool) for value in state.values())
