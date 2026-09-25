from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from omniagent.integrations import telegram


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


def test_install_service_reloads_existing_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plist = tmp_path / "com.omniagent.telegram.plist"
    commands: list[list[str]] = []

    monkeypatch.setattr(telegram, "load_settings", lambda: {"chat_id": 1, "user_id": 1})
    monkeypatch.setattr(telegram, "load_token", lambda: "secret")
    monkeypatch.setattr(telegram, "service_plist_path", lambda: plist)
    monkeypatch.setattr(telegram, "data_root", lambda: tmp_path / "data")
    monkeypatch.setattr(telegram.os, "getuid", lambda: 501)

    def fake_run(command: list[str], **_: object) -> SimpleNamespace:
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(telegram.subprocess, "run", fake_run)

    telegram.install_service()

    assert ["launchctl", "bootout", "gui/501/com.omniagent.telegram"] in commands
    assert ["launchctl", "bootstrap", "gui/501", str(plist)] in commands
    assert plist.is_file()
