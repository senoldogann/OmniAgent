"""Katalog bütçeleri, çağrı yetkisi ve iptal davranışı."""
import asyncio
import time
from pathlib import Path

import httpx
import pytest

from capabilities import CapabilityService, function_schema
from integration_runtime import CURRENT_RUNTIME, CURRENT_SERVICE, IntegrationRuntime, IntegrationStopped
from main import execute_tool, _execute_tool_calls
from tools import Toolbox


def runtime(stop=lambda: False):
    return IntegrationRuntime(lambda event: None, stop)


@pytest.mark.asyncio
async def test_local_lookup_fast_and_no_network(tmp_path: Path) -> None:
    def network(request):
        raise AssertionError("Yerel sorgu ağ kullanmamalı")
    async with httpx.AsyncClient(transport=httpx.MockTransport(network)) as http:
        service = CapabilityService(tmp_path, http)
        samples = []
        for _ in range(1000):
            start = time.perf_counter()
            assert service.local("outlook", ["clean"])[0]["id"] == "outlook"
            samples.append(time.perf_counter() - start)
        assert sorted(samples)[949] < 0.1
        assert service.local("bilinmeyen", []) == []


@pytest.mark.asyncio
async def test_discovery_cache_and_aggregate_budget(tmp_path: Path) -> None:
    calls = []
    async def network(request):
        calls.append(request)
        await asyncio.sleep(0.1)
        return httpx.Response(200, json={"servers": []})
    async with httpx.AsyncClient(transport=httpx.MockTransport(network)) as http:
        service = CapabilityService(tmp_path, http)
        task = runtime()
        task.discovery_remaining = 0.03
        started = time.monotonic()
        await service.discover(task, "x", [], True)
        assert time.monotonic() - started < 0.15
        await service.discover(task, "y", [], True)
        assert len(calls) == 1
        await service.discover(runtime(), "x", [], True)
        assert len(calls) == 1


@pytest.mark.asyncio
async def test_unknown_registry_not_trusted(tmp_path: Path) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(
        200, json={"servers": [{"server": {"name": "demo", "version": "1"}}]}))) as http:
        service = CapabilityService(tmp_path, http)
        result = await service.discover(runtime(), "demo", [], True)
        assert result["candidates"][0]["trusted"] is False
        assert result["selected"] is None


@pytest.mark.asyncio
async def test_dynamic_tools_must_be_published_and_ordered(tmp_path: Path) -> None:
    task = runtime()
    service = CapabilityService(tmp_path)
    order = []
    async def write(value):
        order.append(value)
        return {"ok": True}
    entry = {"schema": function_schema("demo_write", "deneme", {"value": {"type": "integer"}}),
             "execute": write, "readonly": False, "capability": "demo"}
    task.selected["demo_write"] = entry
    rt = CURRENT_RUNTIME.set(task)
    st = CURRENT_SERVICE.set(service)
    try:
        call = {"id": "1", "name": "demo_write", "arguments": '{"value":1}'}
        assert not (await execute_tool(call, Toolbox(), {}, lambda e: None, lambda: False))["ok"]
        task.published = dict(task.selected)
        calls = [dict(call, id=str(i), arguments='{"value":'+str(i)+'}') for i in range(3)]
        result = await _execute_tool_calls(calls, Toolbox(), {}, lambda e: None, lambda: False)
        assert all(item["ok"] for item in result)
        assert order == [0, 1, 2]
    finally:
        CURRENT_RUNTIME.reset(rt)
        CURRENT_SERVICE.reset(st)
        await service.close()


@pytest.mark.asyncio
async def test_wait_cancels_and_questions_pause_clock() -> None:
    stopped = False
    task = runtime(lambda: stopped)
    async def stop():
        nonlocal stopped
        await asyncio.sleep(0.01)
        stopped = True
    stop_task = asyncio.create_task(stop())
    with pytest.raises(IntegrationStopped):
        await task.wait(asyncio.sleep(10))
    await stop_task
    task = runtime()
    async def answer(title, fields):
        await asyncio.sleep(0.01)
        return {"ok": True}
    task.answer = answer
    assert await task.ask("deneme", {}) == {"ok": True}
    assert task.metrics["user_wait_seconds"] >= 0.01
