from __future__ import annotations

from pathlib import Path

import pytest

from omniagent import paths


def test_default_data_paths_live_outside_source_tree(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path / "Application Support" / "OmniAgent"))

    from omniagent import paths

    assert paths.data_root() == tmp_path / "Application Support" / "OmniAgent"
    assert paths.state_file().parent == paths.data_root()
    assert paths.checkpoints_dir().parent == paths.data_root()


def test_resolve_output_path_binds_relative_names_to_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path / "Application Support" / "OmniAgent"))
    workspace = tmp_path / "Application Support" / "OmniAgent" / "workspace"

    assert paths.resolve_output_path("screen_view.png", allow_source_relative=False) == workspace / "screen_view.png"
    assert paths.resolve_output_path("raporlar/ekran.png", allow_source_relative=False) == (
        workspace / "raporlar" / "ekran.png"
    )


@pytest.mark.parametrize("allow_source_relative", [False, True])
def test_resolve_output_path_keeps_absolute_and_home_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, allow_source_relative: bool,
) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path / "Application Support" / "OmniAgent"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    absolute = tmp_path / "baska" / "ekran.png"

    assert paths.resolve_output_path(str(absolute), allow_source_relative=allow_source_relative) == absolute
    # "~" önce genişler; sonuç mutlak olduğu için workspace'e bağlanmaz.
    assert paths.resolve_output_path("~/ekran.png", allow_source_relative=allow_source_relative) == (
        tmp_path / "home" / "ekran.png"
    )


def test_resolve_output_path_keeps_relative_names_for_source_tasks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path / "Application Support" / "OmniAgent"))

    resolved = paths.resolve_output_path("src/ekran.png", allow_source_relative=True)

    # Göreli kalır: çağıran süreç çalışma dizinine (kaynak deposu) bağlar.
    assert resolved == Path("src/ekran.png")
    assert not resolved.is_absolute()


def test_state_save_creates_missing_parent(tmp_path: Path) -> None:
    from omniagent.core.state import load_state, save_state

    destination = tmp_path / "nested" / "state.json"
    save_state(str(destination), {"episodic_memory": []})

    assert destination.is_file()
    assert load_state(str(destination)) == {"episodic_memory": []}


def test_legacy_runtime_data_is_copied_without_overwriting_existing_targets(tmp_path: Path) -> None:
    from omniagent import paths

    legacy = tmp_path / "legacy"
    target = tmp_path / "Application Support" / "OmniAgent"
    legacy.mkdir()
    (legacy / "cognitive_memory.json").write_text('{"episodic_memory":[{"legacy":true}]}', encoding="utf-8")
    (legacy / "user_memory.json").write_text('{"preferences":[{"key":"legacy"}]}', encoding="utf-8")
    (legacy / ".omni_runs").mkdir()
    (legacy / ".omni_runs" / "run-old.json").write_text('{"session_id":"run-old"}', encoding="utf-8")
    (legacy / ".omni_backups").mkdir()
    (legacy / ".omni_backups" / "one.bak").write_text("backup", encoding="utf-8")

    target.mkdir(parents=True)
    (target / "user_memory.json").write_text('{"preferences":[{"key":"new"}]}', encoding="utf-8")

    result = paths.migrate_legacy_runtime_data(legacy_root=legacy, target_root=target)

    assert result["cognitive_memory.json"] is True
    assert result["user_memory.json"] is False
    assert (target / "cognitive_memory.json").read_text(encoding="utf-8").startswith('{"episodic_memory"')
    assert '"new"' in (target / "user_memory.json").read_text(encoding="utf-8")
    assert (target / "checkpoints" / "run-old.json").read_text(encoding="utf-8") == '{"session_id":"run-old"}'
    assert (target / "backups" / "one.bak").read_text(encoding="utf-8") == "backup"
