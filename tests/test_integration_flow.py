"""Görev araç şemasının keşif sonrası daralmasını ve Graph yolunu doğrular."""
import json
from pathlib import Path

import httpx
import pytest

import main
from capabilities import CapabilityService
from integration_runtime import save_json
from outlook import OutlookAdapter
from tests.test_outlook import Auth, RULE, message


@pytest.mark.asyncio
async def test_discovery_to_outlook_batch_without_browser(tmp_path: Path, monkeypatch) -> None:
    batches = []
    model_calls = []

    def network(request: httpx.Request) -> httpx.Response:
        if request.url.host != "graph.microsoft.com":
            raise AssertionError("Hazır Outlook bağlantısı çevrimiçi keşfe gitmemeli.")
        if request.url.path.endswith("/$batch"):
            rows = json.loads(request.content)["requests"]
            batches.append(rows)
            return httpx.Response(200, json={"responses": [
                {"id": row["id"], "status": 201,
                 "body": {"id": row["url"].split("/")[-2], "parentFolderId": "trash"}}
                for row in rows]})
        if request.url.path.endswith("/deleteditems"):
            return httpx.Response(200, json={"id": "trash"})
        return httpx.Response(200, json={"value": [message(i) for i in range(100)]})

    async def fake_model(clients, messages, schemas, session_id, backend, emit, should_stop):
        names = [schema["function"]["name"] for schema in schemas]
        model_calls.append(names)
        if len(model_calls) == 1:
            assert "outlook_clean" not in names
            tool_calls = [{"id": "discovery", "name": "discover_capabilities",
                           "arguments": json.dumps({"query": "outlook", "operations": ["clean"],
                                                    "allow_online": True})}]
            return {"content": "", "tool_calls": tool_calls, "finish_reason": "tool_calls",
                    "usage": main.ZERO_USAGE}, backend
        if len(model_calls) == 2:
            assert "outlook_clean" in names
            assert "outlook_restore" not in names
            tool_calls = [{"id": "clean", "name": "outlook_clean",
                           "arguments": '{"reset_rule":false}'}]
            return {"content": "", "tool_calls": tool_calls, "finish_reason": "tool_calls",
                    "usage": main.ZERO_USAGE}, backend
        assert len(model_calls) == 3
        assert any("100" in item.get("content", "") for item in messages if item.get("role") == "tool")
        return {"content": "100 ileti Çöp Kutusu'na taşındı.", "tool_calls": [],
                "finish_reason": "stop", "usage": main.ZERO_USAGE}, backend

    monkeypatch.setattr(main, "_call_model_with_retries", fake_model)
    async with httpx.AsyncClient(transport=httpx.MockTransport(network)) as http:
        service = CapabilityService(tmp_path, http)
        service.outlook = OutlookAdapter(tmp_path, http, Auth())
        save_json(service.outlook.account_root() / "rule.json", RULE)
        report = await main.run_agent_with_callback(
            "Outlook hesabımdaki gereksiz postaları temizle", lambda event: None,
            {"requested_backend": None, "should_stop": lambda: False,
             "state_file": str(tmp_path / "memory.json"), "history": [], "integrations": service},
            {"opencode": object()})
        assert report["success"]
        assert report["metrics"]["turns"] == 3
        assert report["metrics"]["integrations"]["operations_ok"] == 100
        assert len(batches) == 5
        assert all(len(rows) == 20 for rows in batches)
        await service.close()
