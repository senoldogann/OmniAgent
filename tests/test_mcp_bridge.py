"""Gerçek SDK ile stdio/HTTP taşıma, yeniden kullanım ve kurulum sınırları."""
import asyncio
import json
import socket
import sys
from pathlib import Path

import httpx
import pytest

from omniagent.integrations import mcp as mcp_bridge
from omniagent.integrations.runtime import IntegrationRuntime
from omniagent.integrations.mcp import MCPBridge, install_package, load_skill, run_install

FIXTURE = Path(__file__).parent / "fixtures/mcp_server.py"


def runtime():
    return IntegrationRuntime(lambda event: None, lambda: False)


def entry(transport="stdio", port=0):
    return {"id": "test-server", "kind": "mcp", "title": "Deneme MCP", "trusted": True,
            "source": "yerel test", "version": "1.0.0", "transport": transport,
            "command": sys.executable, "args": [str(FIXTURE)],
            "url": f"http://127.0.0.1:{port}/mcp", "readonly_tools": ["toplam"]}


@pytest.mark.asyncio
async def test_stdio_real_sdk_reuses_connection(tmp_path: Path):
    async with httpx.AsyncClient() as http:
        bridge = MCPBridge(tmp_path, http)
        try:
            tools = await bridge.tools(entry(), runtime(), ["toplam"])
            first = next(iter(bridge.connections.values()))
            tool = next(iter(tools.values()))
            assert tool["readonly"] is True
            result = await tool["execute"](a=3, b=4)
            assert "7" in json.dumps(result)
            changed = dict(entry(), observed={"calls": 3}, connection="ready")
            await bridge.tools(changed, runtime(), ["toplam"])
            assert len(bridge.connections) == 1
            assert next(iter(bridge.connections.values())) is first
        finally:
            await bridge.close()
        assert first.task.done()


@pytest.mark.asyncio
async def test_streamable_http_real_sdk(tmp_path: Path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    process = await asyncio.create_subprocess_exec(sys.executable, str(FIXTURE), "--http", str(port),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    async with httpx.AsyncClient() as http:
        bridge = MCPBridge(tmp_path, http)
        try:
            for _ in range(100):
                try:
                    await http.get(f"http://127.0.0.1:{port}/mcp", timeout=0.2)
                    break
                except httpx.HTTPError:
                    await asyncio.sleep(0.02)
            tools = await bridge.tools(entry("streamable_http", port), runtime(), ["toplam"])
            result = await next(iter(tools.values()))["execute"](a=8, b=9)
            assert "17" in json.dumps(result)
        finally:
            await bridge.close()
            process.terminate()
            await process.wait()


@pytest.mark.asyncio
async def test_install_pin_reuse_and_failure_not_repeated(tmp_path: Path, monkeypatch):
    calls = []
    async def install(command, task, timeout):
        calls.append(command)
        if "--python" in command and "install" in command:
            executable = Path(command[command.index("--python") + 1])
            executable.parent.mkdir(parents=True, exist_ok=True)
            executable.touch()
    monkeypatch.setattr(mcp_bridge, "run_install", install)
    monkeypatch.setattr(mcp_bridge.shutil, "which", lambda name: "/usr/bin/uv")
    configured = dict(entry(), package={"ecosystem": "python", "name": "demo",
                                      "version": "1.0.0", "module": "demo"})
    task = runtime()
    first = await install_package(tmp_path, configured, task)
    second = await install_package(tmp_path, configured, runtime())
    assert first == second and len(calls) == 2
    with pytest.raises(ValueError):
        await install_package(tmp_path, dict(configured, package={**configured["package"], "version": "latest"}), runtime())
    async def fail(*args):
        raise RuntimeError("deneme")
    monkeypatch.setattr(mcp_bridge, "run_install", fail)
    configured["package"]["version"] = "2.0.0"
    task = runtime()
    with pytest.raises(RuntimeError):
        await install_package(tmp_path, configured, task)
    with pytest.raises(RuntimeError, match="zaten denendi"):
        await install_package(tmp_path, configured, task)


@pytest.mark.asyncio
async def test_install_timeout_and_skill_only_data(tmp_path: Path):
    with pytest.raises(TimeoutError):
        await run_install([sys.executable, "-c", "import time; time.sleep(10)"], runtime(), 0.03)
    skill = tmp_path / "SKILL.md"
    skill.write_text("Yalnız yöntem bilgisi", encoding="utf-8")
    async with httpx.AsyncClient() as http:
        content = await load_skill({"trusted": True, "skill_path": str(skill)}, http, runtime())
        assert content == "Yalnız yöntem bilgisi"
        with pytest.raises(ValueError):
            await load_skill({"trusted": False, "skill_path": str(skill)}, http, runtime())



@pytest.mark.asyncio
async def test_catalog_operation_maps_to_only_named_mcp_tool(tmp_path: Path):
    async with httpx.AsyncClient() as http:
        bridge = MCPBridge(tmp_path, http)
        try:
            configured = dict(entry(), operations=["math"], operation_tools={"math": ["toplam"]})
            tools = await bridge.tools(configured, runtime(), ["math"])
            assert len(tools) == 1
            assert "toplam" in next(iter(tools.values()))["schema"]["function"]["description"]
            with pytest.raises(ValueError, match="İstenen işlemler"):
                await bridge.tools(configured, runtime(), ["unknown"])
        finally:
            await bridge.close()
