from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from omniagent.app import agent as main
from omniagent import tools
from omniagent.tools import Toolbox
from omniagent.tools.browser import fetch_raw_content
from omniagent.tools.types import ToolError


class _FakeDDGS:
    calls: list[tuple[str, str, dict[str, Any]]] = []

    def __enter__(self) -> "_FakeDDGS":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def news(self, query: str, **kwargs: Any) -> list[dict[str, str]]:
        self.calls.append(("news", query, kwargs))
        now = datetime.now(timezone.utc)
        return [
            {
                "date": now.isoformat(),
                "title": "World update",
                "body": "Current world event",
                "url": "https://example.com/news",
                "source": "Example News",
            },
            {
                "date": (now - timedelta(days=8)).isoformat(),
                "title": "Old world update",
                "body": "Stale world event",
                "url": "https://example.com/old-news",
                "source": "Old News",
            },
        ]

    def text(self, query: str, **kwargs: Any) -> list[dict[str, str]]:
        self.calls.append(("text", query, kwargs))
        return [{
            "title": "Text result",
            "body": "Fallback body",
            "href": "https://example.com/text",
        }]


def test_web_search_uses_current_ddgs_news_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeDDGS.calls.clear()
    monkeypatch.setattr(tools, "DDGS", _FakeDDGS)

    payload = json.loads(Toolbox().web_search(
        "last 3 days world news summary September 22-25 2026",
        category="auto",
        freshness_days=3,
    ))

    assert _FakeDDGS.calls == [(
        "news",
        "world news",
        {"timelimit": "w", "max_results": 12, "backend": "bing,duckduckgo,yahoo"},
    )]
    assert [item["url"] for item in payload] == ["https://example.com/news"]
    assert payload[0]["source"] == "Example News"


def test_web_search_falls_back_to_text_when_news_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    class EmptyNews(_FakeDDGS):
        def news(self, query: str, **kwargs: Any) -> list[dict[str, str]]:
            self.calls.append(("news", query, kwargs))
            return []

    EmptyNews.calls = []
    monkeypatch.setattr(tools, "DDGS", EmptyNews)

    payload = json.loads(Toolbox().web_search("dünya haberleri son 3 gün"))

    assert [call[0] for call in EmptyNews.calls] == ["news", "text"]
    assert payload[0]["url"] == "https://example.com/text"


def test_fetch_raw_decodes_non_utf8_body_without_crashing(monkeypatch: pytest.MonkeyPatch) -> None:
    body = "<html><body>Schröder — München</body></html>".encode("cp1252")
    completed = subprocess.CompletedProcess(
        args=["curl"], returncode=0, stdout=body, stderr=b"",
    )
    monkeypatch.setattr("omniagent.tools.browser.subprocess.run", lambda *args, **kwargs: completed)

    result = fetch_raw_content("https://example.com")

    assert "Schröder" in result
    assert "München" in result


@pytest.mark.asyncio
async def test_tool_failures_do_not_switch_model_backend(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any,
) -> None:
    turns = [
        {
            "content": "STATE: search one",
            "tool_calls": [{"id": "1", "name": "web_search", "arguments": json.dumps({"query": "q1"})}],
            "finish_reason": "tool_calls",
            "usage": main.ZERO_USAGE,
        },
        {
            "content": "STATE: search two",
            "tool_calls": [{"id": "2", "name": "web_search", "arguments": json.dumps({"query": "q2"})}],
            "finish_reason": "tool_calls",
            "usage": main.ZERO_USAGE,
        },
        {
            "content": "Araştırma kaynağı erişilemedi.",
            "tool_calls": [],
            "finish_reason": "stop",
            "usage": main.ZERO_USAGE,
        },
    ]
    requested_backends: list[str] = []

    async def fake_model(
        clients: Any, messages: Any, schemas: Any, session_id: str, backend: str,
        emit: Any, should_stop: Any,
    ) -> tuple[dict[str, Any], str]:
        requested_backends.append(backend)
        return turns.pop(0), backend

    def fail_search(self: Toolbox, query: str, category: str = "auto", freshness_days: int | None = None) -> str:
        raise ToolError("search unavailable", "WEB_SEARCH_FAILED", True)

    monkeypatch.setattr(main, "_call_model_with_retries", fake_model)
    monkeypatch.setattr(Toolbox, "web_search", fail_search)

    events: list[Any] = []
    report = await main.run_agent_with_callback(
        "Web'de son 3 günü araştır ve rapor ver",
        events.append,
        {
            "requested_backend": "ollama-cloud",
            "should_stop": lambda: False,
            "state_file": str(tmp_path / "state.json"),
            "history": [],
        },
        {"ollama-cloud": object(), "openai": object()},
    )

    assert requested_backends == ["ollama-cloud", "ollama-cloud", "ollama-cloud"]
    assert not [event for event in events if event["kind"] == "backend_changed"]
    assert report["metrics"]["backend"] == "ollama-cloud"
