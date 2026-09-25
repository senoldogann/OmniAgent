"""Kalıcı skill kurulumunun kaynak, bütünlük ve yeniden kullanım testleri."""
from pathlib import Path

import httpx
import pytest

from capabilities import CapabilityService, skill_install_entry
from integration_runtime import IntegrationRuntime
from skill_install import install_skill, parse_skill_source


COMMIT = "a" * 40
SKILL = b"---\nname: demo\n---\nUse Outlook's official API.\n"
EXAMPLE = b"example"


def fake_github(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/commits/HEAD"):
        return httpx.Response(200, json={"sha": COMMIT})
    if "/git/trees/" in path:
        return httpx.Response(200, json={"tree": [
            {"path": "skills/demo/SKILL.md", "type": "blob", "size": len(SKILL)},
            {"path": "skills/demo/examples.txt", "type": "blob", "size": len(EXAMPLE)},
        ]})
    if path.endswith("/skills/demo/SKILL.md"):
        return httpx.Response(200, content=SKILL)
    if path.endswith("/skills/demo/examples.txt"):
        return httpx.Response(200, content=EXAMPLE)
    raise AssertionError(f"Beklenmeyen istek: {request.url}")


def runtime() -> IntegrationRuntime:
    return IntegrationRuntime(lambda event: None, lambda: False)


def test_source_requires_exact_slug() -> None:
    assert parse_skill_source("https://skills.sh/acme/tools/demo") == (
        "acme", "tools", "demo", "https://skills.sh/acme/tools/demo"
    )
    for source in ("https://evil.test/acme/tools/demo", "acme/tools/../demo",
                   "acme/tools", "acme/tools/demo/extra", "acme/../demo"):
        with pytest.raises(ValueError):
            parse_skill_source(source)


@pytest.mark.asyncio
async def test_install_persists_and_reuses_without_network(tmp_path: Path) -> None:
    calls = []
    def handler(request):
        calls.append(str(request.url))
        return fake_github(request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        service = CapabilityService(tmp_path, http)
        result = await skill_install_entry(service, runtime())["execute"]("acme/tools/demo")
        assert result["reused"] is False
        assert "Use Outlook's official API" in result["guidance"]
        assert (tmp_path / "skills/demo/SKILL.md").read_bytes() == SKILL
        assert service.local("demo", [])[0]["id"] == "skill:demo"
        initial_calls = len(calls)
        reused = await install_skill(tmp_path, "acme/tools/demo", http, runtime())
        assert reused["reused"] is True
        assert len(calls) == initial_calls
        assert result["commit"] == COMMIT
        assert (tmp_path / "skills/demo/INSTALL.json").exists()
        other_session = CapabilityService(tmp_path, http)
        assert other_session.local("demo", [])[0]["id"] == "skill:demo"


@pytest.mark.asyncio
async def test_collision_and_failed_install_do_not_replace_existing(tmp_path: Path) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake_github)) as http:
        await install_skill(tmp_path, "acme/tools/demo", http, runtime())
        with pytest.raises(ValueError, match="zaten kurulu"):
            await install_skill(tmp_path, "other/tools/demo", http, runtime())
        assert (tmp_path / "skills/demo/SKILL.md").read_bytes() == SKILL


@pytest.mark.asyncio
async def test_bad_size_leaves_no_partial_install(tmp_path: Path) -> None:
    def handler(request):
        response = fake_github(request)
        if request.url.path.endswith("/skills/demo/SKILL.md"):
            return httpx.Response(200, content=SKILL + b"x")
        return response
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(ValueError, match="boyutuyla"):
            await install_skill(tmp_path, "acme/tools/demo", http, runtime())
    assert not (tmp_path / "skills/demo").exists()
    assert not list((tmp_path / "skills").glob(".skill-install-*"))


@pytest.mark.asyncio
async def test_installer_rejects_traversal_and_symlink(tmp_path: Path) -> None:
    def handler(request):
        if "/git/trees/" in request.url.path:
            return httpx.Response(200, json={"tree": [
                {"path": "skills/demo/SKILL.md", "type": "blob", "size": len(SKILL)},
                {"path": "skills/demo/../escape", "type": "blob", "size": 1},
            ]})
        return fake_github(request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(ValueError, match="güvensiz"):
            await install_skill(tmp_path, "acme/tools/demo", http, runtime())
    assert not (tmp_path / "skills/demo").exists()


def test_install_goal_opens_only_on_explicit_request() -> None:
    from main import skill_install_goal
    assert skill_install_goal("skills.sh üzerinden Outlook skillini indir ve kur")
    assert skill_install_goal("Please install this skill")
    assert not skill_install_goal("Kurulu skillleri göster")
    assert not skill_install_goal("Outlook skillini kullan")
    assert not skill_install_goal("Postalarımı sil")
