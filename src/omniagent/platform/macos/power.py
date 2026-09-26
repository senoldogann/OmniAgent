"""
Telegram köprüsü açıkken Mac'in uyumasını engeller; uyuyan Mac'te köprü mesaj alamaz.

`caffeinate -s` yalnız prizdeyken geçerlidir: pilde Mac normal uyur (pil ve ısınma), ekran
uykusu hiç engellenmez. `-w <pid>` köprü süreci ölürse engeli kendiliğinden kaldırır.
"""
from __future__ import annotations

import logging
import subprocess
import sys
from typing import Optional

from omniagent.tools.system import child_environment

CAFFEINATE: str = "/usr/bin/caffeinate"
STOP_TIMEOUT_SECONDS: float = 2.0


def keep_awake_command(pid: int) -> list[str]:
    """`pid` yaşadıkça prizdeyken sistem uykusunu engelleyen komut. Saf."""
    return [CAFFEINATE, "-s", "-w", str(pid)]


def start_keep_awake(pid: int) -> Optional["subprocess.Popen[bytes]"]:
    """Uyku engelini başlatır; macOS dışında veya caffeinate çalışmazsa None döner."""
    if sys.platform != "darwin":
        return None
    try:
        return subprocess.Popen(
            keep_awake_command(pid), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, env=child_environment(),
        )
    except OSError as error:
        logging.warning("Uyku engeli başlatılamadı", extra={"error_type": type(error).__name__})
        return None


def stop_keep_awake(process: Optional["subprocess.Popen[bytes]"]) -> None:
    """Uyku engelini kaldırır (yeniden başlatmada yenisi kurulur, engeller birikmez)."""
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=STOP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
