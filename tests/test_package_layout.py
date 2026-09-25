from __future__ import annotations

import tomllib
from importlib.util import find_spec
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_project_is_installable_src_package_with_entry_points() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert project.get("tool", {}).get("uv", {}).get("package") is not False
    assert project["build-system"]["build-backend"]
    scripts = project["project"]["scripts"]
    assert scripts == {
        "omniagent": "omniagent.cli:main",
        "omniagent-ui": "omniagent.ui.app:main",
        "omniagent-telegram": "omniagent.integrations.telegram:main",
        "omniagent-benchmark": "omniagent.dev.benchmark:main",
        "omniagent-permissions": "omniagent.platform.macos.permissions:main",
    }
    assert (ROOT / "src" / "omniagent" / "__init__.py").is_file()
    assert find_spec("omniagent") is not None


def test_script_targets_exist_and_root_has_no_production_python_files() -> None:
    import importlib

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    for target in project["project"]["scripts"].values():
        module_name, attribute = target.split(":", 1)
        module = importlib.import_module(module_name)
        entry = getattr(module, attribute, None)
        assert callable(entry), f"Entry point callable bulunamadı: {target}"

    assert list(ROOT.glob("*.py")) == []


def test_production_source_has_no_assert_or_bare_except() -> None:
    import ast

    violations: list[str] = []
    for path in sorted((ROOT / "src" / "omniagent").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assert):
                violations.append(f"{path.relative_to(ROOT)}:{node.lineno}: assert")
            elif isinstance(node, ast.Try):
                for handler in node.handlers:
                    if handler.type is None:
                        violations.append(
                            f"{path.relative_to(ROOT)}:{handler.lineno}: bare except"
                        )
    assert violations == []
