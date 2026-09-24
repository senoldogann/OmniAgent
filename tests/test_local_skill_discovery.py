"""Kurulu skill keşfi; dosya yöntemi olarak kalır, araç yetkisi vermez."""
from __future__ import annotations
import pytest
from capabilities import CapabilityService, local_skill_entries
from integration_runtime import IntegrationRuntime

@pytest.mark.asyncio
async def test_local_skill_discovery_uses_no_network_and_no_tools(tmp_path, monkeypatch) -> None:
    folder = tmp_path / "skills"
    item = folder / "calendar-helper"
    item.mkdir(parents=True)
    (item / "SKILL.md").write_text("# Takvim yöntemi\nAdımları doğrula.", encoding="utf-8")
    monkeypatch.setenv("OMNI_SKILLS_DIRS", str(folder))
    entries = local_skill_entries()
    assert len(entries) == 1
    assert entries[0]["version"]
    service = CapabilityService(root=tmp_path / "data")
    runtime = IntegrationRuntime(lambda event: None, lambda: False)
    try:
        result = await service.discover(runtime, "calendar-helper", [], False)
        assert result["selected"] is None
        assert result["guidance"]
        assert result["tools"] == []
        assert "Takvim yöntemi" in result["reason"]
        assert runtime.metrics["network_requests"] == 0
    finally:
        await service.close()

def test_large_local_skill_is_ignored(tmp_path, monkeypatch) -> None:
    item = tmp_path / "huge"
    item.mkdir()
    (item / "SKILL.md").write_bytes(b"x" * 64001)
    monkeypatch.setenv("OMNI_SKILLS_DIRS", str(tmp_path))
    assert local_skill_entries() == []


@pytest.mark.asyncio
async def test_catalog_inventory_has_no_network_or_activation(tmp_path, monkeypatch) -> None:
    item = tmp_path / "skills" / "mail-method"
    item.mkdir(parents=True)
    (item / "SKILL.md").write_text("# Mail", encoding="utf-8")
    monkeypatch.setenv("OMNI_SKILLS_DIRS", str(tmp_path / "skills"))
    service = CapabilityService(root=tmp_path / "data")
    runtime = IntegrationRuntime(lambda event: None, lambda: False)
    try:
        result = await service.discover(runtime, "catalog", [], True)
        assert result["total"] == 2
        assert {entry["id"] for entry in result["installed"]} == {"outlook", "skill:mail-method"}
        assert runtime.metrics["network_requests"] == 0
        assert runtime.selected == {}
    finally:
        await service.close()


def test_skill_symlink_outside_configured_root_is_ignored(tmp_path, monkeypatch) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text("# Dış kaynak", encoding="utf-8")
    root = tmp_path / "skills"
    root.mkdir()
    (root / "linked").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("OMNI_SKILLS_DIRS", str(root))
    assert local_skill_entries() == []


@pytest.mark.asyncio
async def test_method_skill_never_outranks_executable_outlook(tmp_path, monkeypatch) -> None:
    item = tmp_path / "skills" / "outlook"
    item.mkdir(parents=True)
    (item / "SKILL.md").write_text("# Outlook yöntemleri", encoding="utf-8")
    monkeypatch.setenv("OMNI_SKILLS_DIRS", str(tmp_path / "skills"))
    service = CapabilityService(root=tmp_path / "data")
    try:
        assert service.local("outlook", ["clean"])[0]["id"] == "outlook"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_skill_installed_after_service_start_is_usable(tmp_path, monkeypatch) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    monkeypatch.setenv("OMNI_SKILLS_DIRS", str(root))
    service = CapabilityService(root=tmp_path / "data")
    item = root / "fresh-tool"
    item.mkdir()
    (item / "SKILL.md").write_text("# Yeni yöntem", encoding="utf-8")
    runtime = IntegrationRuntime(lambda event: None, lambda: False)
    try:
        result = await service.discover(runtime, "fresh-tool", [], False)
        assert result["selected"] is None
        assert result["guidance"]
        assert runtime.metrics["network_requests"] == 0
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_skill_does_not_block_online_tool_discovery(tmp_path, monkeypatch) -> None:
    item = tmp_path / "skills" / "calendar-helper"
    item.mkdir(parents=True)
    (item / "SKILL.md").write_text("# Takvim yöntemi", encoding="utf-8")
    monkeypatch.setenv("OMNI_SKILLS_DIRS", str(tmp_path / "skills"))
    service = CapabilityService(root=tmp_path / "data")
    runtime = IntegrationRuntime(lambda event: None, lambda: False)
    calls = 0

    async def fake_search(query, active_runtime):
        nonlocal calls
        calls += 1
        return [{"id": "calendar-api", "kind": "mcp", "trusted": False,
                 "connection": "review_required", "source": "https://example.com"}]

    monkeypatch.setattr(service, "remote_search", fake_search)
    try:
        result = await service.discover(runtime, "calendar-helper", ["list"], True)
        assert result["selected"] is None
        assert result["guidance"]
        assert result["candidates"][0]["id"] == "calendar-api"
        assert calls == 1
        assert runtime.selected == {}
    finally:
        await service.close()
