"""Sağlayıcı isteklerinin ve kurulum alt süreçlerinin regresyon denetimi."""

import pytest

from omniagent.config import API_KEY_VARIABLES, BACKENDS
from omniagent.integrations.runtime import IntegrationRuntime
from omniagent.integrations import mcp as mcp_bridge
from omniagent.app.agent import _stream_completion
from omniagent.integrations.mcp import run_install


class EmptyStream:
    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration

    async def close(self):
        return None


class FakeCompletions:
    def __init__(self):
        self.kwargs = None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        return EmptyStream()


class FakeClient:
    def __init__(self):
        self.chat = type("Chat", (), {"completions": FakeCompletions()})()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend,token_field", [
    ("openai", "max_completion_tokens"),
    ("opencode", "max_tokens"),
    ("ollama-cloud", "max_tokens"),
])
async def test_model_token_limit_matches_provider(backend, token_field):
    client = FakeClient()
    await _stream_completion(
        client, BACKENDS[backend], [{"role": "user", "content": "Merhaba"}],
        [{"type": "function", "function": {"name": "ping", "parameters": {}}}],
        "session", lambda event: None, lambda: False,
    )
    kwargs = client.chat.completions.kwargs
    assert kwargs[token_field] == BACKENDS[backend]["max_tokens"]
    assert ("max_tokens" in kwargs) != ("max_completion_tokens" in kwargs)
    if backend == "ollama-cloud":
        assert "tool_choice" not in kwargs
    else:
        assert kwargs["tool_choice"] == "auto"
    if backend == "openai" and BACKENDS[backend]["model"] in ("gpt-6-luna", "gpt-6-sol"):
        assert kwargs["extra_body"]["reasoning_effort"] == "none"


@pytest.mark.asyncio
async def test_mcp_installer_does_not_inherit_provider_keys(monkeypatch):
    captured = {}

    class CompletedProcess:
        returncode = 0

        async def wait(self):
            return 0

    async def fake_create(*command, **kwargs):
        captured.update(kwargs)
        return CompletedProcess()

    monkeypatch.setenv(API_KEY_VARIABLES["openai"], "test-secret")
    monkeypatch.setattr(mcp_bridge.asyncio, "create_subprocess_exec", fake_create)
    await run_install(["fake-installer"], IntegrationRuntime(lambda event: None, lambda: False), 1)
    assert API_KEY_VARIABLES["openai"] not in captured["env"]
    assert captured["env"]["PATH"]


def test_local_helper_process_does_not_inherit_provider_keys(monkeypatch):
    from subprocess import CompletedProcess

    from omniagent import tools

    captured = {}

    def fake_run(command, **kwargs):
        captured.update(kwargs)
        return CompletedProcess(command, 0, stdout="Python\n", stderr="")

    monkeypatch.setenv(API_KEY_VARIABLES["openai"], "test-secret")
    monkeypatch.setattr(tools.subprocess, "run", fake_run)
    assert tools.parent_process_name() == "Python"
    assert API_KEY_VARIABLES["openai"] not in captured["env"]
