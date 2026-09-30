"""Kullanıcı LaunchAgent'larını (Telegram ve iMessage köprüleri) idempotent kuran ortak launchd kodu."""
from __future__ import annotations

import logging
import os
import plistlib
import subprocess
import time
from pathlib import Path
from typing import Dict, List

# bootout sonrası eski kaydın kalkmasını bekleme (launchd çıkış süresi 5 sn) ve bootstrap denemeleri
SERVICE_UNLOAD_TIMEOUT_SECONDS: float = 15.0
SERVICE_POLL_SECONDS: float = 0.25
BOOTSTRAP_ATTEMPTS: int = 3
BOOTSTRAP_RETRY_SECONDS: float = 1.0


class LaunchAgentError(RuntimeError):
    """launchd kaydı durdurulamadı, zamanında kalkmadı ya da başlatılamadı."""


def plist_path(label: str) -> Path:
    """Kullanıcı LaunchAgent plist yolu."""
    return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"


def launchd_record(label: str, program_arguments: List[str], stdout_path: Path, stderr_path: Path) -> Dict[str, object]:
    """Sürekli çalışan (RunAtLoad + KeepAlive) kullanıcı hizmeti kaydı. Saf."""
    return {
        "Label": label,
        "ProgramArguments": program_arguments,
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(stdout_path),
        "StandardErrorPath": str(stderr_path),
    }


def write_plist(path: Path, record: Dict[str, object]) -> None:
    """Plist'i yarım dosya bırakmadan atomik biçimde yeniler (0600)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_bytes(plistlib.dumps(record))
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def launchctl(arguments: List[str]) -> "subprocess.CompletedProcess[str]":
    """launchctl'i çıktısını yakalayarak çalıştırır; dönüş kodunu çağıran denetler."""
    return subprocess.run(["launchctl", *arguments], capture_output=True, text=True, check=False)


def wait_until_unloaded(target: str, description: str) -> None:
    """
    bootout döndüğünde launchd eski süreci hâlâ kapatıyor olabilir; bu arada yapılan bootstrap
    "5: Input/output error" ile düşüp hizmeti kapalı bırakıyordu (26 Eylül, canlı). Kayıt kalkana kadar
    sınırlı süre beklenir.
    """
    deadline: float = time.monotonic() + SERVICE_UNLOAD_TIMEOUT_SECONDS
    while launchctl(["print", target]).returncode == 0:
        if time.monotonic() >= deadline:
            raise LaunchAgentError(
                f"Eski {description} hizmeti {SERVICE_UNLOAD_TIMEOUT_SECONDS:.0f} sn içinde kalkmadı: {target}"
            )
        time.sleep(SERVICE_POLL_SECONDS)


def bootstrap(domain: str, path: Path) -> None:
    """Kaydı yükler; launchd geçici hata verirse uyarıyla yeniden dener, sonunda son hatayı yükseltir."""
    stderr: str = ""
    for attempt in range(1, BOOTSTRAP_ATTEMPTS + 1):
        result = launchctl(["bootstrap", domain, str(path)])
        if result.returncode == 0:
            return
        stderr = result.stderr.strip()[:300]
        if attempt < BOOTSTRAP_ATTEMPTS:
            logging.warning(
                "launchd bootstrap başarısız; yeniden denenecek",
                extra={"attempt": attempt, "returncode": result.returncode, "stderr": stderr},
            )
            time.sleep(BOOTSTRAP_RETRY_SECONDS)
    raise LaunchAgentError(f"launchd başlatılamadı ({BOOTSTRAP_ATTEMPTS} deneme): {stderr}")


def install(label: str, path: Path, record: Dict[str, object], description: str) -> None:
    """Hizmeti güncel kayıtla idempotent kurar: varsa durdurur, kalkmasını bekler, yeniden yükler."""
    domain: str = f"gui/{os.getuid()}"
    target: str = f"{domain}/{label}"
    write_plist(path, record)
    if launchctl(["print", target]).returncode == 0:
        stopped = launchctl(["bootout", target])
        if stopped.returncode != 0:
            raise LaunchAgentError(f"Eski {description} hizmeti durdurulamadı: {stopped.stderr.strip()[:300]}")
        wait_until_unloaded(target, description)
    bootstrap(domain, path)
