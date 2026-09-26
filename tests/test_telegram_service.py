from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from omniagent.integrations import telegram

TARGET: str = "gui/501/com.omniagent.telegram"
EIO: SimpleNamespace = SimpleNamespace(returncode=5, stdout="", stderr="Bootstrap failed: 5: Input/output error")
OK: SimpleNamespace = SimpleNamespace(returncode=0, stdout="", stderr="")
MISSING: SimpleNamespace = SimpleNamespace(returncode=113, stdout="", stderr="Could not find service")


def _service_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """install_service'in dokunduğu kullanıcı yollarını ve kimliklerini geçici dizine çevirir."""
    plist = tmp_path / "com.omniagent.telegram.plist"
    monkeypatch.setattr(telegram, "load_settings", lambda: {"chat_id": 1, "user_id": 1})
    monkeypatch.setattr(telegram, "load_token", lambda: "secret")
    monkeypatch.setattr(telegram, "service_plist_path", lambda: plist)
    monkeypatch.setattr(telegram, "data_root", lambda: tmp_path / "data")
    monkeypatch.setattr(telegram.os, "getuid", lambda: 501)
    return plist


def test_launchd_record_runs_installed_package_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(telegram, "data_root", lambda: tmp_path / "data")

    record = telegram.build_launchd_record()

    assert record["ProgramArguments"] == [
        sys.executable,
        "-m",
        "omniagent.integrations.telegram",
        "run",
    ]
    assert "WorkingDirectory" not in record


def test_install_service_waits_for_old_job_and_retries_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Canlı hata (26 Eylül): bootout döndüğünde launchd eski köprüyü hâlâ kapatıyordu; hemen yapılan
    bootstrap EIO verip hizmeti kapalı bıraktı. Kurulum kayıt kalkana kadar bekler, sonra yeniden dener.
    """
    plist = _service_paths(tmp_path, monkeypatch)
    commands: list[list[str]] = []
    sleeps: list[float] = []
    teardown_polls = [OK, OK, MISSING]
    bootstrap_results = [EIO, OK]

    def fake_run(command: list[str], **_: object) -> SimpleNamespace:
        commands.append(command)
        if command[1] == "print":
            return teardown_polls.pop(0) if ["launchctl", "bootout", TARGET] in commands else OK
        if command[1] == "bootstrap":
            return bootstrap_results.pop(0)
        return OK

    monkeypatch.setattr(telegram.subprocess, "run", fake_run)
    monkeypatch.setattr(telegram.time, "sleep", sleeps.append)

    telegram.install_service()

    bootout = commands.index(["launchctl", "bootout", TARGET])
    prints = [index for index, command in enumerate(commands) if command[1] == "print"]
    bootstraps = [index for index, command in enumerate(commands) if command[1] == "bootstrap"]
    assert commands[bootstraps[-1]] == ["launchctl", "bootstrap", "gui/501", str(plist)]
    assert len(bootstraps) == 2
    assert bootout < prints[-1] < bootstraps[0]
    assert teardown_polls == [] and bootstrap_results == []
    assert sleeps == [telegram.SERVICE_POLL_SECONDS] * 2 + [telegram.BOOTSTRAP_RETRY_SECONDS]
    assert plist.is_file()


def test_install_service_reports_last_bootstrap_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _service_paths(tmp_path, monkeypatch)
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> SimpleNamespace:
        commands.append(command)
        return MISSING if command[1] == "print" else EIO

    monkeypatch.setattr(telegram.subprocess, "run", fake_run)
    monkeypatch.setattr(telegram.time, "sleep", lambda _seconds: None)

    with pytest.raises(telegram.TelegramError, match="Input/output error"):
        telegram.install_service()
    assert sum(command[1] == "bootstrap" for command in commands) == telegram.BOOTSTRAP_ATTEMPTS


def test_bridge_process_runs_from_project_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """launchd köprüyü salt okunur `/` dizininde başlatır; modelin göreli yolları proje kökünde çözülmeli."""
    project = tmp_path / "proje"
    project.mkdir()
    seen: list[Path] = []

    async def fake_bridge(announce: bool) -> None:
        seen.append(Path.cwd().resolve())

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(telegram, "project_root", lambda: project)
    monkeypatch.setattr(telegram, "run_bridge", fake_bridge)
    monkeypatch.setattr(telegram, "apply_stored_api_keys", lambda: None)
    monkeypatch.setattr(telegram.sys, "argv", ["omniagent-telegram", "run"])

    telegram.main()

    assert seen == [project.resolve()]
