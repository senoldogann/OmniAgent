"""Sağlayıcı isteklerinin ve kurulum alt süreçlerinin regresyon denetimi."""

from types import SimpleNamespace

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
async def test_ollama_same_index_stream_yields_two_calls_and_previews():
    """Sağlayıcının index=0 tekrarını gerçek iki çağrı ve iki önizleme olarak ayırır."""
    class TwoCallStream:
        def __init__(self):
            self.chunks = iter([
                SimpleNamespace(usage=None, choices=[SimpleNamespace(
                    delta=SimpleNamespace(content=None, model_extra={}, tool_calls=[
                        SimpleNamespace(index=0, id=call_id, function=SimpleNamespace(
                            name="read_file", arguments=arguments,
                        )),
                    ]), finish_reason=finish,
                )])
                for call_id, arguments, finish in [
                    ("first", '{"path":"README.md"}', None),
                    ("second", '{"path":"Makefile"}', "tool_calls"),
                ]
            ])

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self.chunks)
            except StopIteration:
                raise StopAsyncIteration

        async def close(self):
            return None

    class TwoCallCompletions(FakeCompletions):
        async def create(self, **kwargs):
            return TwoCallStream()

    client = FakeClient()
    client.chat.completions = TwoCallCompletions()
    events = []
    turn = await _stream_completion(
        client, BACKENDS["ollama-cloud"], [{"role": "user", "content": "İki dosyayı oku"}],
        [{"type": "function", "function": {"name": "read_file", "parameters": {}}}],
        "session", events.append, lambda: False,
    )
    assert [call["id"] for call in turn["tool_calls"]] == ["first", "second"]
    assert [call["name"] for call in turn["tool_calls"]] == ["read_file", "read_file"]
    assert [event["index"] for event in events if event["kind"] == "tool_call_preview"] == [0, 1]


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
