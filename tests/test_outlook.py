"""Graph toplu işlemleri: hız, kısmi hata ve yan etki sınırları."""
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from integration_runtime import IntegrationRuntime, IntegrationStopped, save_json
from outlook import OutlookAdapter, GraphError, parse_rule, mail_filter


class Auth:
    def __init__(self):
        self.account_id = "test-account"
        self.ready = True
        self.refreshes = 0
    async def token(self, runtime, force_refresh=False):
        runtime.check()
        self.refreshes += int(force_refresh)
        return "test-token"
    def status(self):
        return "ready"
    async def close(self):
        pass


def runtime(stop=lambda: False, answer=None):
    return IntegrationRuntime(lambda e: None, stop, answer)


def message(number, sender="ads@example.com"):
    return {"id": str(number), "parentFolderId": "inbox-id",
            "from": {"emailAddress": {"address": sender}},
            "receivedDateTime": "2020-01-01T00:00:00Z", "flag": {"flagStatus": "notFlagged"},
            "subject": "deneme"}


RULE = {"folder": "inbox", "senders": ["ads@example.com"], "older_than_days": 30,
        "exclude_senders": [], "keep_flagged": True, "semantic": ""}


@pytest.mark.asyncio
async def test_100_messages_five_batches_and_pagination(tmp_path: Path):
    batches = []
    moved = []
    def network(request):
        assert request.headers["Prefer"] == 'IdType="ImmutableId"'
        if request.url.path.endswith("/$batch"):
            payload = json.loads(request.content)["requests"]
            batches.append(payload)
            responses = []
            for row in payload:
                identifier = row["url"].split("/")[-2]
                moved.append(identifier)
                responses.append({"id": row["id"], "status": 201,
                                  "body": {"id": identifier, "parentFolderId": "trash"}})
            return httpx.Response(200, json={"responses": list(reversed(responses))})
        if request.url.path.endswith("/deleteditems"):
            return httpx.Response(200, json={"id": "trash"})
        if "page" in request.url.params:
            return httpx.Response(200, json={"value": [message(i) for i in range(50, 100)] +
                                                     [message(999, "keep@example.com")]})
        assert "$filter" in request.url.params
        return httpx.Response(200, json={"value": [message(i) for i in range(50)],
            "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages?page=2"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(network)) as http:
        adapter = OutlookAdapter(tmp_path, http, Auth())
        save_json(adapter.account_root() / "rule.json", RULE)
        result = await adapter.clean(runtime())
        assert result["moved"] == 100
        assert result["skipped"] == 1
        assert len(batches) == 5
        assert all(len(batch) == 20 for batch in batches)
        assert "999" not in moved


@pytest.mark.asyncio
async def test_rule_asked_once_and_semantic_choices_bounded(tmp_path: Path):
    answers = []
    async def answer(title, fields):
        answers.append(title)
        return {"folder": "inbox", "senders": "", "older_than_days": "30",
                "exclude_senders": "", "keep_flagged": True, "semantic": "reklam"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"value": [message(1)]}))) as http:
        adapter = OutlookAdapter(tmp_path, http, Auth())
        task = runtime(answer=answer)
        selected = await adapter.clean(task)
        await adapter.clean(task)
        assert len(answers) == 1
        assert selected["status"] == "classification_required"
        with pytest.raises(ValueError):
            await adapter.apply_selection(task, selected["selection_id"], ["invented"])
        with pytest.raises(ValueError):
            await adapter.apply_selection(runtime(), selected["selection_id"], ["1"])


@pytest.mark.asyncio
async def test_401_refresh_and_403_no_retry(tmp_path: Path):
    seen = []
    def network(request):
        seen.append(request)
        return httpx.Response(401 if len(seen) == 1 else 403)
    async with httpx.AsyncClient(transport=httpx.MockTransport(network)) as http:
        auth = Auth()
        adapter = OutlookAdapter(tmp_path, http, auth)
        with pytest.raises(GraphError) as error:
            await adapter.request(runtime(), "GET", "/me/messages")
        assert error.value.status == 403
        assert len(seen) == 2
        assert auth.refreshes == 1


@pytest.mark.asyncio
async def test_partial_batch_retry_only_throttled(tmp_path: Path):
    sizes = []
    def network(request):
        if request.url.path.endswith("/deleteditems"):
            return httpx.Response(200, json={"id": "trash"})
        payload = json.loads(request.content)["requests"]
        sizes.append(len(payload))
        if len(sizes) == 1:
            return httpx.Response(200, json={"responses": [
                {"id": "0", "status": 201, "body": {"id": "0"}},
                {"id": "1", "status": 429, "headers": {"Retry-After": "0"}},
                {"id": "2", "status": 403}]})
        return httpx.Response(200, json={"responses": [{"id": "0", "status": 201, "body": {"id": "1"}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(network)) as http:
        adapter = OutlookAdapter(tmp_path, http, Auth())
        result = await adapter.move(runtime(), [message(i) for i in range(3)])
        assert sizes == [3, 1]
        assert result["moved"] == 2 and result["failed"] == 1


@pytest.mark.asyncio
async def test_uncertain_move_reconciles_without_repeat(tmp_path: Path):
    batches = []
    def network(request):
        if request.url.path.endswith("/deleteditems"):
            return httpx.Response(200, json={"id": "trash"})
        if request.url.path.endswith("/$batch"):
            batches.append(request)
            raise httpx.ReadTimeout("deneme")
        return httpx.Response(200, json={"id": "1", "parentFolderId": "trash"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(network)) as http:
        adapter = OutlookAdapter(tmp_path, http, Auth())
        result = await adapter.move(runtime(), [message(1)])
        assert result["moved"] == 1 and len(batches) == 1


@pytest.mark.asyncio
async def test_stop_never_starts_next_batch(tmp_path: Path):
    stopped = False
    calls = []
    def network(request):
        nonlocal stopped
        if request.url.path.endswith("/deleteditems"):
            return httpx.Response(200, json={"id": "trash"})
        calls.append(request)
        stopped = True
        return httpx.Response(200, json={"responses": []})
    async with httpx.AsyncClient(transport=httpx.MockTransport(network)) as http:
        adapter = OutlookAdapter(tmp_path, http, Auth())
        with pytest.raises(IntegrationStopped):
            await adapter.move(runtime(lambda: stopped), [message(i) for i in range(40)])
        assert len(calls) == 1


@pytest.mark.asyncio
async def test_no_token_to_external_nextlink(tmp_path: Path):
    async with httpx.AsyncClient() as http:
        adapter = OutlookAdapter(tmp_path, http, Auth())
        with pytest.raises(ValueError):
            await adapter.request(runtime(), "GET", "https://example.com/steal")


def test_rule_validation_and_escaping():
    with pytest.raises(ValueError):
        parse_rule({})
    rule = parse_rule({"senders": "O'NEIL@example.com", "older_than_days": "3"})
    assert "o''neil@example.com" in mail_filter(rule, datetime.now(timezone.utc))



@pytest.mark.asyncio
async def test_outer_batch_429_reconciles_before_any_retry(tmp_path: Path):
    batches = []
    def network(request):
        if request.url.path.endswith("/deleteditems"):
            return httpx.Response(200, json={"id": "trash"})
        if request.url.path.endswith("/$batch"):
            batches.append(request)
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"id": "1", "parentFolderId": "trash"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(network)) as http:
        adapter = OutlookAdapter(tmp_path, http, Auth())
        result = await adapter.move(runtime(), [message(1)])
        assert result["moved"] == 1
        assert len(batches) == 1
