"""Real coordinator/runtime boundaries, scripted providers and temporary local files."""
import asyncio
import json
from contextlib import asynccontextmanager

import pytest

from omniagent.app import agent
from omniagent.core.conversation import make_exchange
from omniagent.core.evidence import EvidenceStore
from omniagent.core.conversation_policy import derive_request_contract


def turn(content="", calls=()):
    return {"content": content, "tool_calls": list(calls), "finish_reason": "stop",
            "usage": {"prompt_tokens": 11, "cached_tokens": 2, "completion_tokens": 7}}


def call(name, **arguments):
    return {"id": name + "-id", "name": name, "arguments": json.dumps(arguments)}


def route(kind, fields=(), observation=None):
    return turn(json.dumps({"route": kind, "required_fields": list(fields),
                            "needs_observation": kind != "chat" if observation is None else observation}))


def verified():
    return turn(json.dumps({"ok": True, "facts": [], "missing_fields": [], "unsupported_claims": []}))


@pytest.fixture
def coordinator():
    from omniagent.app import conversation
    return conversation


@pytest.fixture
def scripted(monkeypatch):
    scripts, requests = [], []
    async def stream(client, profile, messages, tools, session, emit, stop):
        requests.append((messages, tools, profile))
        item = scripts.pop(0)
        if callable(item):
            return await item(emit, stop)
        if isinstance(item, BaseException):
            raise item
        emit({"kind": "text_delta", "text": item["content"]})
        return item
    monkeypatch.setattr(agent, "_stream_completion", stream)
    monkeypatch.setattr(agent, "load_fallback_policy", lambda: {"backends": [], "allow_images": False})
    return scripts, requests


def options(tmp_path, **extra):
    return {"requested_backend": "ollama-cloud", "state_file": str(tmp_path / "state.json"),
            "should_stop": lambda: False, "history": [], **extra}


async def run(coordinator, tmp_path, goal, extra=None):
    events = []
    report = await coordinator.run_conversation_with_callback(
        goal, events.append, options(tmp_path, **(extra or {})), {"ollama-cloud": object()})
    return report, events


@pytest.mark.asyncio
async def test_implicit_current_models_requires_real_research(coordinator, scripted, tmp_path, monkeypatch):
    scripts, requests = scripted
    scripts.extend([route("investigate", ["model_names", "source_urls"]),
                    turn(calls=[call("web_search", query="OpenAI Anthropic latest models")]),
                    turn("OpenAI: GPT Example. Anthropic: Claude Example. https://example.org/releases\nKaynaklar arama özeti olduğu için eksik; tam sayfalar incelenmedi."),
                    turn(json.dumps({"ok": True, "facts": [
                        {"field": "model_names", "value": "GPT Example", "source": 0, "quote": "GPT Example"},
                        {"field": "model_names", "value": "Claude Example", "source": 0, "quote": "Claude Example"}],
                        "missing_fields": [], "unsupported_claims": []}))])
    monkeypatch.setattr(agent.Toolbox, "web_search", lambda self, **kwargs:
        "OpenAI introduced GPT Example. Anthropic introduced Claude Example. https://example.org/releases")
    report, events = await run(coordinator, tmp_path, "Have OpenAI and Anthropic released new models?")
    assert report["success"]
    assert report["metrics"]["tool_calls"] == 1
    assert report["metrics"]["turns"] == 4
    assert report["evidence"]["observations"][0]["tool"] == "web_search"
    assert requests[0][1] == []
    assert len([e for e in events if e["kind"] == "text_delta"]) == 1
    assert [e for e in events if e["kind"] == "run_finished"][0]["outcome"] == report["outcome"]


@pytest.mark.asyncio
async def test_explicit_research_overrides_bad_chat_and_no_receipt_fails(coordinator, scripted, tmp_path):
    scripts, requests = scripted
    scripts.extend([route("chat"), turn("GPT Imaginary was released today."), verified()])
    report, events = await run(coordinator, tmp_path, "Search the web for OpenAI model names")
    assert report["evidence"]["contract"]["route"] == "investigate"
    assert not report["success"]
    assert "GPT Imaginary" not in report["outcome"]
    assert "gözlem" in report["outcome"]


@pytest.mark.asyncio
async def test_ordinary_chat_tools_none_and_no_exclusive_lock(coordinator, scripted, tmp_path):
    scripts, requests = scripted
    scripts.extend([route("chat"), turn("Merhaba, buradayım.")])
    @asynccontextmanager
    async def lock():
        pytest.fail("chat must not acquire GUI ownership")
        yield
    report, events = await run(coordinator, tmp_path, "Merhaba", {"task_context": lock})
    assert report["outcome"] == "Merhaba, buradayım."
    assert all(tools == [] for _, tools, _ in requests)
    assert report["metrics"]["prompt_tokens"] == 22


@pytest.mark.asyncio
@pytest.mark.parametrize("empty", [False, True])
async def test_real_directory_receipt_and_empty_is_valid(coordinator, scripted, tmp_path, empty):
    scripts, _ = scripted
    directory = tmp_path / "Desktop"
    directory.mkdir()
    if not empty:
        (directory / "Middle Name").mkdir()
        (directory / "notes.txt").write_text("safe example")
    scripts.extend([route("investigate", ["directory_names"]),
                    turn(calls=[call("list_directory", path=str(directory))]),
                    turn("Dizin boş; 0 klasör var." if empty else "Klasör: Middle Name. Dosya: notes.txt."), verified()])
    report, _ = await run(coordinator, tmp_path, f"List directory names in {directory}")
    assert report["success"]
    receipt = json.loads(report["evidence"]["observations"][0]["text"])
    assert receipt["complete"] is True
    assert receipt["entries"] == [] if empty else any(e["name"] == "Middle Name" for e in receipt["entries"])


@pytest.mark.asyncio
async def test_followup_keeps_providers_and_subject(coordinator, scripted, tmp_path):
    scripts, requests = scripted
    scripts.extend([route("investigate", ["model_names", "source_urls"]), turn("Kaynaklar eksik."), verified()])
    history = [make_exchange("OpenAI ve Anthropic yeni model çıkardı mı?", "Araştırma eksik.", [])]
    report, _ = await run(coordinator, tmp_path, "No, give names", {"history": history})
    subject = report["evidence"]["contract"]["subject"]
    assert "OpenAI" in subject and "Anthropic" in subject and "No, give names" in subject
    assert "OpenAI" in json.dumps(requests[0][0])


@pytest.mark.asyncio
async def test_allowlist_denies_shell_and_dynamic_before_execution(coordinator, scripted, tmp_path, monkeypatch):
    scripts, _ = scripted
    scripts.extend([route("investigate"), turn(calls=[call("execute_shell", command="ls"),
                  call("discover_capabilities", query="research"), call("user_memory", action="add")]),
                  turn("İnceleme tamamlanamadı."), verified()])
    monkeypatch.setattr(agent.Toolbox, "execute_shell", lambda *args, **kwargs: pytest.fail("shell executed"))
    report, events = await run(coordinator, tmp_path, "Research OpenAI releases")
    assert not report["success"]
    assert report["metrics"]["tool_calls"] == 0
    assert not [e for e in events if e["kind"] == "tool_started"]


@pytest.mark.asyncio
async def test_small_budget_never_runs_extra_verifier(coordinator, scripted, tmp_path):
    scripts, requests = scripted
    scripts.extend([route("investigate"), turn("Unsupported release.")])
    report, _ = await run(coordinator, tmp_path, "Research OpenAI", {"max_iterations": 2})
    assert not report["success"] and len(requests) == 2
    assert "Unsupported release" not in report["outcome"]
    assert "OpenAI" in report["outcome"] and "devam" in report["outcome"]


@pytest.mark.asyncio
async def test_cancellation_interrupts_model_without_publishing_draft(coordinator, scripted, tmp_path):
    scripts, requests = scripted
    stopped = asyncio.Event()
    async def waiting(emit, stop):
        emit({"kind": "text_delta", "text": "Unsupported draft"})
        stopped.set()
        await asyncio.sleep(10)
    scripts.append(waiting)
    report, events = await run(coordinator, tmp_path, "Research OpenAI", {"should_stop": stopped.is_set})
    assert not report["success"]
    assert report["metrics"]["elapsed_seconds"] < 1
    assert "Unsupported draft" not in json.dumps(events)


@pytest.mark.asyncio
async def test_missing_selected_provider_cannot_silently_switch(coordinator, scripted, tmp_path):
    scripts, requests = scripted
    events = []
    report = await coordinator.run_conversation_with_callback("Hello", events.append,
        options(tmp_path, requested_backend="openai"), {"ollama-cloud": object()})
    assert not report["success"] and not requests


@pytest.mark.asyncio
async def test_full_agent_publication_is_held_until_verified(coordinator, scripted, tmp_path, monkeypatch):
    scripts, requests = scripted
    scripts.extend([route("task", ["directory_names"]), verified()])
    entered = []
    @asynccontextmanager
    async def lock():
        entered.append(True)
        yield
    async def heavy(goal, emit, opts, clients):
        store = EvidenceStore(opts["state_file"])
        bundle = store.load(opts["evidence_run_id"])
        store.capture(bundle, "list_directory", {"ok": True, "result": json.dumps({
            "path": "/safe", "complete": True, "entries": [{"name": "Verified Folder", "type": "directory"}]})})
        for event in [{"kind": "text_delta", "text": "Unsupported inner draft"},
                      {"kind": "reasoning_delta", "text": "secret reasoning"},
                      {"kind": "notice", "level": "warning", "text": "Unsupported inner draft"},
                      {"kind": "run_finished", "outcome": "Unsupported inner draft", "success": True,
                       "reason": "done", "metrics": {}}]:
            emit(event)
        return {"outcome": "Verified Folder", "success": True, "reason": "done", "evidence": bundle,
                "exchange": make_exchange(goal, "Verified Folder", []), "metrics": {
                    "turns": 1, "tool_calls": 1, "elapsed_seconds": 0.1, "backend": "ollama-cloud",
                    "prompt_tokens": 5, "cached_tokens": 0, "completion_tokens": 5,
                    "model_seconds": .1, "tool_seconds": .01}}
    monkeypatch.setattr(coordinator, "run_agent_with_callback", heavy)
    report, events = await run(coordinator, tmp_path, "Perform substantial folder task", {"task_context": lock})
    assert entered and report["success"]
    assert "Unsupported" not in json.dumps(events)
    assert len([e for e in events if e["kind"] == "text_delta"]) == 1
    assert len([e for e in events if e["kind"] == "run_finished"]) == 1
    assert report["exchange"]["answer"] == "Verified Folder"


@pytest.mark.asyncio
async def test_one_correction_and_reverification_retain_middle_names(coordinator, scripted, tmp_path, monkeypatch):
    scripts, requests = scripted
    result = "x" * 8000 + "\nOpenAI released GPT Middle on 2026-09-29. https://example.org/middle\n" + "y" * 8000
    scripts.extend([route("investigate", ["model_names", "source_urls", "release_dates"]),
                    turn(calls=[call("fetch_raw", url="https://example.org/middle")]),
                    turn("A vague new release exists."),
                    turn(json.dumps({"ok": False, "facts": [], "missing_fields": ["model_names"], "unsupported_claims": []})),
                    turn("GPT Middle — 2026-09-29. https://example.org/middle"),
                    turn(json.dumps({"ok": True, "facts": [{"field": "model_names", "value": "GPT Middle",
                                     "source": 0, "quote": "OpenAI released GPT Middle on 2026-09-29."}],
                                     "missing_fields": [], "unsupported_claims": []}))])
    monkeypatch.setattr(agent.Toolbox, "fetch_raw", lambda self, **kwargs: result)
    report, events = await run(coordinator, tmp_path, "Research latest OpenAI model names and dates")
    assert report["success"] and report["metrics"]["turns"] == 6
    assert report["evidence"]["observations"][0]["text"] == result
    verifier_text = json.dumps(requests[-1][0])
    assert "GPT Middle" in verifier_text and "https://example.org/middle" in verifier_text
    final = [event["text"] for event in events if event["kind"] == "text_delta"]
    assert final == [report["outcome"]]


@pytest.mark.asyncio
async def test_second_grounding_failure_renders_real_sources(coordinator, scripted, tmp_path, monkeypatch):
    scripts, requests = scripted
    fail = turn(json.dumps({"ok": False, "facts": [], "missing_fields": [], "unsupported_claims": ["invented"]}))
    scripts.extend([route("investigate", ["model_names"]), turn(calls=[call("fetch_raw", url="https://example.org")]),
                    turn("Unsupported One"), fail, turn("Unsupported Two"), fail])
    monkeypatch.setattr(agent.Toolbox, "fetch_raw", lambda self, **kwargs: "OpenAI announced GPT Real.")
    report, events = await run(coordinator, tmp_path, "Research OpenAI models")
    assert not report["success"] and len(requests) == 6
    assert "GPT Real" in report["outcome"]
    assert "Unsupported" not in json.dumps(events)


@pytest.mark.asyncio
async def test_verifier_cannot_promote_model_prose_to_source(coordinator, scripted, tmp_path, monkeypatch):
    scripts, requests = scripted
    forged = turn(json.dumps({"ok": True, "facts": [{"field": "model_names", "value": "GPT Invented",
                         "source": 0, "quote": "GPT Invented"}], "missing_fields": [], "unsupported_claims": []}))
    scripts.extend([route("investigate", ["model_names"]), turn(calls=[call("fetch_raw", url="https://example.org")]),
                    turn("GPT Invented"), forged, turn("GPT Invented"), forged])
    monkeypatch.setattr(agent.Toolbox, "fetch_raw", lambda self, **kwargs: "GPT Actual")
    report, events = await run(coordinator, tmp_path, "Research OpenAI model names")
    assert not report["success"]
    assert "GPT Invented" not in report["outcome"] and "GPT Actual" in report["outcome"]


@pytest.mark.asyncio
async def test_six_actual_tool_call_limit(coordinator, scripted, tmp_path, monkeypatch):
    scripts, requests = scripted
    calls = [call("fetch_raw", url=f"https://example.org/{number}") for number in range(7)]
    scripts.extend([route("investigate"), turn(calls=calls)])
    observed = []
    monkeypatch.setattr(agent.Toolbox, "fetch_raw", lambda self, url: observed.append(url) or f"Read {url}")
    report, events = await run(coordinator, tmp_path, "Research sources")
    assert len(observed) == report["metrics"]["tool_calls"] == 6
    assert len(report["evidence"]["observations"]) == 6
    assert not report["success"] and "devam" in report["outcome"]


@pytest.mark.asyncio
async def test_bounded_directory_cannot_claim_total(coordinator, scripted, tmp_path):
    scripts, requests = scripted
    for index in range(3):
        (tmp_path / f"folder{index}").mkdir()
    scripts.extend([route("investigate", ["directory_names"]),
                    turn(calls=[call("list_directory", path=str(tmp_path), max_entries=1)]),
                    turn("One folder is the complete total."),
                    turn(json.dumps({"ok": False, "facts": [], "missing_fields": [], "unsupported_claims": ["total"]})),
                    turn("Still total one."),
                    turn(json.dumps({"ok": False, "facts": [], "missing_fields": [], "unsupported_claims": ["total"]}))])
    report, _ = await run(coordinator, tmp_path, "List directory names")
    assert not report["success"] and not report["evidence"]["complete"]
    assert json.loads(report["evidence"]["observations"][0]["text"])["complete"] is False
    assert "eksik" in report["outcome"]


@pytest.mark.asyncio
async def test_smaller_total_deadline_cancels_wait(coordinator, scripted, tmp_path):
    scripts, requests = scripted
    async def slow(emit, stop):
        await asyncio.sleep(10)
    scripts.append(slow)
    report, events = await run(coordinator, tmp_path, "Research OpenAI", {"max_wall_clock_seconds": .03})
    assert not report["success"] and report["metrics"]["elapsed_seconds"] < .5
    assert report["metrics"]["turns"] == 1
    assert len([e for e in events if e["kind"] == "run_finished"]) == 1


@pytest.mark.asyncio
async def test_explicit_token_limit_rejects_oversized_request_before_model(coordinator, scripted, tmp_path):
    scripts, requests = scripted
    report, events = await run(coordinator, tmp_path, "Merhaba", {"max_total_tokens": 10})
    assert not report["success"] and requests == []
    assert report["metrics"]["turns"] == 0 and report["metrics"]["prompt_tokens"] == 0
    assert "token" in report["reason"].lower()


@pytest.mark.asyncio
async def test_selected_extended_mode_skips_quick_classifier(coordinator, scripted, tmp_path, monkeypatch):
    seen = []
    @asynccontextmanager
    async def lock():
        yield
    async def heavy(goal, emit, opts, clients):
        seen.append(opts)
        return {"outcome": "Failed", "success": False, "reason": "effect failed",
                "exchange": make_exchange(goal, "Failed", []), "evidence": EvidenceStore(opts["state_file"]).load(opts["evidence_run_id"]),
                "metrics": {"turns": 0, "tool_calls": 0, "elapsed_seconds": 0, "backend": "ollama-cloud",
                    "prompt_tokens": 0, "cached_tokens": 0, "completion_tokens": 0, "model_seconds": 0, "tool_seconds": 0}}
    monkeypatch.setattr(coordinator, "run_agent_with_callback", heavy)
    report, events = await run(coordinator, tmp_path, "Hello", {"task_context": lock, "run_mode": "extended",
                                    "max_iterations": 19, "max_wall_clock_seconds": 321})
    scripts, requests = scripted
    assert not requests and seen[0]["run_mode"] == "extended"
    assert seen[0]["max_iterations"] == 19 and seen[0]["max_wall_clock_seconds"] <= 321
    assert not report["success"]


@pytest.mark.asyncio
async def test_host_decision_handoff_classifies_only_once(coordinator, scripted, tmp_path):
    scripts, requests = scripted
    scripts.extend([route("chat"), turn("Merhaba.")])
    opts = options(tmp_path)
    events = []
    clients = {"ollama-cloud": object()}
    decision = await coordinator.decide_conversation("Merhaba", events.append, opts, clients)
    opts["conversation_decision"] = decision
    report = await coordinator.run_conversation_with_callback("Merhaba", events.append, opts, clients)
    assert report["success"] and len(requests) == 2
    assert report["metrics"]["turns"] == 2
    again = await coordinator.run_conversation_with_callback("Merhaba", events.append, opts, clients)
    assert not again["success"] and len(requests) == 2


@pytest.mark.asyncio
async def test_chat_decision_needing_observation_cannot_publish_chat_facts(coordinator, scripted, tmp_path):
    scripts, requests = scripted
    scripts.extend([route("chat", observation=True), turn("Imaginary files exist.")])
    report, events = await run(coordinator, tmp_path, "What files exist here?")
    assert report["evidence"]["contract"]["route"] == "investigate"
    assert not report["success"] and "Imaginary" not in report["outcome"]


@pytest.mark.asyncio
async def test_provider_retry_is_counted_as_actual_model_turn(coordinator, scripted, tmp_path, monkeypatch):
    from openai import APIConnectionError
    import httpx
    scripts, requests = scripted
    scripts.extend([route("investigate"), APIConnectionError(request=httpx.Request("POST", "https://example.org")),
                    turn(calls=[call("fetch_raw", url="https://example.org")]), turn("Verified source. https://example.org"), verified()])
    monkeypatch.setattr(agent.Toolbox, "fetch_raw", lambda self, **kwargs: "Verified source.")
    report, _ = await run(coordinator, tmp_path, "Research sources")
    assert report["success"] and report["metrics"]["turns"] == len(requests) == 5
    assert report["metrics"]["prompt_tokens"] == 44


@pytest.mark.asyncio
async def test_failed_real_tool_receipt_never_becomes_invented_answer(coordinator, scripted, tmp_path, monkeypatch):
    from omniagent.tools import ToolError
    scripts, requests = scripted
    scripts.extend([route("investigate", ["model_names"]), turn(calls=[call("fetch_raw", url="https://example.org")]),
                    turn("Invented GPT"), verified()])
    def failed(*args, **kwargs):
        raise ToolError("Source unavailable", "FETCH_FAILED", True)
    monkeypatch.setattr(agent.Toolbox, "fetch_raw", failed)
    report, _ = await run(coordinator, tmp_path, "Research OpenAI model names")
    assert not report["success"] and report["metrics"]["tool_calls"] == 1
    assert "Invented" not in report["outcome"] and "Source unavailable" in report["outcome"]
    assert not report["evidence"]["observations"][0]["ok"]


@pytest.mark.asyncio
async def test_operation_timeout_is_inside_total_and_counts_actual_attempt(coordinator, scripted, tmp_path, monkeypatch):
    from omniagent.app import conversation_budget
    monkeypatch.setattr(conversation_budget, "OPERATION_SECONDS", .02)
    scripts, requests = scripted
    async def slow(emit, stop):
        await asyncio.sleep(10)
    scripts.append(slow)
    report, events = await run(coordinator, tmp_path, "Hello")
    assert not report["success"] and report["metrics"]["turns"] == 1
    assert report["metrics"]["elapsed_seconds"] < .5


@pytest.mark.asyncio
async def test_seven_turn_limit_with_three_generation_and_three_grounding_turns(coordinator, scripted, tmp_path, monkeypatch):
    scripts, requests = scripted
    fail = turn(json.dumps({"ok": False, "facts": [], "missing_fields": [], "unsupported_claims": ["unsupported"]}))
    scripts.extend([route("investigate"), turn(calls=[call("fetch_raw", url="https://example.org/one")]),
                    turn(calls=[call("fetch_raw", url="https://example.org/two")]), turn("Unsupported"),
                    fail, turn("Still unsupported"), fail])
    monkeypatch.setattr(agent.Toolbox, "fetch_raw", lambda self, url: "Supported observation: " + url)
    report, events = await run(coordinator, tmp_path, "Research sources")
    assert not report["success"] and report["metrics"]["turns"] == len(requests) == 7
    assert "Supported observation" in report["outcome"]


@pytest.mark.asyncio
async def test_failed_decision_handoff_keeps_attempt_metrics_without_reclassification(coordinator, scripted, tmp_path):
    scripts, requests = scripted
    scripts.append(RuntimeError("Routing provider unavailable"))
    opts = options(tmp_path)
    clients = {"ollama-cloud": object()}
    decision = await coordinator.decide_conversation("Hello", lambda event: None, opts, clients)
    assert decision.error == "Routing provider unavailable"
    report = await coordinator.run_conversation_with_callback("Hello", lambda event: None,
        {**opts, "conversation_decision": decision}, clients)
    assert not report["success"] and report["metrics"]["turns"] == len(requests) == 1


@pytest.mark.asyncio
async def test_handoff_combines_adapter_cancel_without_new_model_call(coordinator, scripted, tmp_path):
    scripts, requests = scripted
    scripts.append(route("chat"))
    opts = options(tmp_path)
    clients = {"ollama-cloud": object()}
    decision = await coordinator.decide_conversation("Hello", lambda event: None, opts, clients)
    report = await coordinator.run_conversation_with_callback("Hello", lambda event: None,
        {**opts, "should_stop": lambda: True, "conversation_decision": decision}, clients)
    assert not report["success"] and len(requests) == 1


@pytest.mark.asyncio
async def test_oversized_model_context_renders_full_evidence_without_extra_call(coordinator, scripted, tmp_path, monkeypatch):
    scripts, requests = scripted
    result = "x" * 41000 + "\nGPT Beyond Context — https://example.org/beyond\n" + "y" * 41000
    scripts.extend([route("investigate", ["model_names", "source_urls"]),
                    turn(calls=[call("fetch_raw", url="https://example.org/beyond")])])
    monkeypatch.setattr(agent.Toolbox, "fetch_raw", lambda self, **kwargs: result)
    report, _ = await run(coordinator, tmp_path, "Research OpenAI model names")
    assert not report["success"] and len(requests) == 2
    assert "GPT Beyond Context" in report["outcome"] and "https://example.org/beyond" in report["outcome"]
    assert report["evidence"]["observations"][0]["text"] == result


def test_required_artifact_text_api_preserves_authoritative_middle(tmp_path):
    from omniagent.core.evidence import MAX_OBSERVATION_BYTES
    store = EvidenceStore(tmp_path / "state.json")
    bundle = store.create(derive_request_contract("Read names", route="investigate"))
    result = "a" * MAX_OBSERVATION_BYTES + "MIDDLE REQUIRED" + "b" * MAX_OBSERVATION_BYTES
    store.capture(bundle, "read_file", {"ok": True, "result": result})
    assert store.observation_text(store.load(bundle["run_id"]), 0) == result


def test_continuous_research_uses_shared_routing_and_effectful_requests_still_force_task():
    from omniagent.app.conversation_routing import force_task
    assert not force_task("Son iki gündeki yapay zeka haberlerini araştır ve rapor ver", {"run_mode": "continuous", "history": []})
    assert force_task("Ekranda ne görüyorsun?", {"run_mode": "continuous", "history": []})
    assert force_task("Dosyayı sil", {"run_mode": "continuous", "history": [], "autonomy": {"enabled": True}})


@pytest.mark.asyncio
async def test_continuous_candidate_is_not_success_when_verification_budget_expires(coordinator, scripted, tmp_path, monkeypatch):
    from omniagent.app.conversation_budget import ConversationExhausted
    scripts, requests = scripted
    scripts.append(route("task"))
    async def heavy(goal, emit, opts, clients):
        store = EvidenceStore(opts["state_file"])
        bundle = store.load(opts["evidence_run_id"])
        store.capture(bundle, "read_file", {"ok": True, "result": "Only a local draft exists"})
        return {"outcome": "Published and sold", "success": True, "reason": "done", "evidence": bundle,
                "exchange": make_exchange(goal, "Published and sold", []), "metrics": {}}
    async def exhausted(*args, **kwargs):
        raise ConversationExhausted("verification budget exhausted")
    monkeypatch.setattr(coordinator, "run_agent_with_callback", heavy)
    monkeypatch.setattr(coordinator, "ground_answer", exhausted)
    report, events = await run(coordinator, tmp_path, "Perform substantial work", {"run_mode": "continuous"})
    assert not report["success"]
    assert not report["evidence"]["complete"]
    assert not any(e.get("kind") == "text_delta" and "Published and sold" in e.get("text", "") for e in events)
