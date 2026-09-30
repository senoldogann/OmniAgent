"""Ortak launchd kodu: eski kayıt durdurulamazsa LaunchAgentError; plist 0600 yazılır (Telegram ve iMessage)."""
from pathlib import Path
from types import SimpleNamespace
from typing import List

import pytest

from omniagent.platform.macos import launch_agent


def test_install_reports_bootout_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    commands: List[List[str]] = []

    def fake_run(command: List[str], **_: object) -> SimpleNamespace:
        commands.append(command)
        if command[1] == "print":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=5, stdout="", stderr="Operation not permitted")

    monkeypatch.setattr(launch_agent.subprocess, "run", fake_run)
    monkeypatch.setattr(launch_agent.os, "getuid", lambda: 501)
    record = launch_agent.launchd_record("com.example.test", ["/usr/bin/true"], tmp_path / "out.log",
                                         tmp_path / "err.log")
    with pytest.raises(launch_agent.LaunchAgentError, match="Operation not permitted"):
        launch_agent.install("com.example.test", tmp_path / "test.plist", record, "Deneme")
    assert [command[1] for command in commands] == ["print", "bootout"]
    assert (tmp_path / "test.plist").stat().st_mode & 0o777 == 0o600
    assert record["KeepAlive"] is True and record["ProgramArguments"] == ["/usr/bin/true"]
