"""Authoritative observations survive archives, restart and bounded presentation."""
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from omniagent.core.conversation_policy import (
    NATURAL_STYLE_POLICY, check_grounded_answer, derive_request_contract,
    presentation_context, render_evidence, required_identifiers,
)
from omniagent.core.evidence import EvidenceStore, MAX_OBSERVATION_BYTES, MAX_RUN_BYTES


def make_bundle(tmp_path, fields=("directory_names", "source_urls")):
    store = EvidenceStore(tmp_path / "state.json")
    bundle = store.create(derive_request_contract("List desktop folders and sources", required_fields=fields))
    return store, bundle


def test_middle_of_long_result_survives_reload_and_render(tmp_path):
    store, bundle = make_bundle(tmp_path)
    text = "x" * 18000 + "\nProjects/\nhttps://example.org/releases\n" + "y" * 18000
    store.capture(bundle, "execute_shell", {"ok": True, "result": text})
    loaded = store.load(bundle["run_id"])
    assert loaded["observations"][0]["text"] == text
    assert "Projects" in render_evidence(loaded)
    assert "https://example.org/releases" in render_evidence(loaded)
    assert loaded["complete"]


def test_masks_known_unknown_and_typed_secrets_before_storage(tmp_path, monkeypatch):
    from omniagent import config
    monkeypatch.setattr(config, "secret_values", lambda: ("known-secret-value",))
    store, bundle = make_bundle(tmp_path)
    text = "ordinary\n" + "x" * 23000 + "\npassword: UNKNOWN-PASSWORD\nknown-secret-value"
    store.capture(bundle, "cua_type_text", {"ok": True, "result": text},
                  arguments='{"text":"typed-secret-value"}')
    disk = store.path_for(bundle["run_id"]).read_text()
    assert "known-secret-value" not in disk
    assert "UNKNOWN-PASSWORD" not in disk
    assert "typed-secret-value" not in disk
    assert "[gizli]" in disk
    assert "UNKNOWN-PASSWORD" not in presentation_context(bundle)


def test_store_private_atomic_replace_preserves_previous_on_failure(tmp_path, monkeypatch):
    store, bundle = make_bundle(tmp_path)
    original = store.path_for(bundle["run_id"]).read_bytes()
    assert store.directory.stat().st_mode & 0o777 == 0o700
    assert store.path_for(bundle["run_id"]).stat().st_mode & 0o777 == 0o600
    def reject_replace(*args, **kwargs):
        raise OSError("replace failed")
    monkeypatch.setattr(os, "replace", reject_replace)
    bundle["limitations"].append("interrupted write")
    with pytest.raises(OSError):
        store.save(bundle)
    assert store.path_for(bundle["run_id"]).read_bytes() == original
    assert not list(store.directory.glob("*.tmp"))


@pytest.mark.parametrize("run_id", ["../../private", "invalid", "", "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA"])
def test_rejects_invalid_ids(tmp_path, run_id):
    store = EvidenceStore(tmp_path / "state.json")
    with pytest.raises(ValueError):
        store.load(run_id)


def test_rejects_symlink_escape_and_malformed_version(tmp_path):
    store, bundle = make_bundle(tmp_path)
    path = store.path_for(bundle["run_id"])
    path.unlink()
    path.symlink_to(tmp_path / "outside.json")
    with pytest.raises(ValueError):
        store.save(bundle)
    path.unlink()
    store.save(bundle)
    changed = json.loads(path.read_text())
    changed["version"] = 999
    path.write_text(json.dumps(changed))
    with pytest.raises(ValueError):
        store.load(bundle["run_id"])
    path.unlink()
    store.directory.rmdir()
    store.directory.symlink_to(tmp_path)
    with pytest.raises(ValueError):
        EvidenceStore(tmp_path / "state.json")


def test_delivery_unknown_and_failures_survive_json_reload(tmp_path):
    store, bundle = make_bundle(tmp_path)
    store.capture(bundle, "web_search", {"ok": False, "error_type": "Timeout", "error": "provider unavailable"})
    bundle["delivery_status"] = "unknown"
    store.save(bundle)
    loaded = store.load(bundle["run_id"])
    assert loaded["delivery_status"] == "unknown"
    assert loaded["observations"][0]["ok"] is False
    assert "Timeout: provider unavailable" in render_evidence(loaded)
    assert "eksik" in render_evidence(loaded).lower()
    assert json.loads(json.dumps({"evidence": loaded}))["evidence"] == loaded


def test_required_identifiers_are_field_aware_and_groundchecked(tmp_path):
    store, bundle = make_bundle(tmp_path, ("source_urls", "model_names"))
    store.capture(bundle, "web_search", {"ok": True, "result": "model: Claude Example\nReleased at https://example.org/models"},
                  source_reference="https://example.org/models")
    assert set(required_identifiers(bundle)) == {"Claude Example", "https://example.org/models"}
    assert check_grounded_answer("A new model exists", bundle)["missing_identifiers"]
    assert check_grounded_answer(render_evidence(bundle), bundle)["ok"]
    bundle["contract"]["required_fields"] = ["action_outcome"]
    assert required_identifiers(bundle) == []
    check = check_grounded_answer("Done", bundle, claims=["all files deleted"])
    assert check["unsupported_claims"] == ["all files deleted"]


def test_crop_search_snippets_and_budget_are_explicitly_incomplete(tmp_path):
    store, bundle = make_bundle(tmp_path)
    store.capture(bundle, "execute_shell", {"ok": True, "result": "Projects\n…[kısaltıldı, toplam 100000 karakter]"})
    assert not bundle["observations"][0]["complete"]
    store.capture(bundle, "web_search", {"ok": True, "result": "A title and snippet"})
    assert bundle["observations"][1]["completeness"] == "snippet"
    store.capture(bundle, "execute_shell", {"ok": True, "result": "a" * (MAX_OBSERVATION_BYTES + 100)})
    observation = bundle["observations"][2]
    assert not observation["complete"]
    assert len(observation["text"].encode()) <= MAX_OBSERVATION_BYTES
    assert Path(observation["artifact_path"]).read_text() == "a" * (MAX_OBSERVATION_BYTES + 100)
    assert Path(observation["artifact_path"]).stat().st_mode & 0o777 == 0o600
    store.capture(bundle, "execute_shell", {"ok": True, "result": "z" * (MAX_RUN_BYTES + 100)})
    assert not bundle["complete"]
    assert "devam" in render_evidence(bundle).lower()
    assert sum(p.stat().st_size for p in store.directory.iterdir()) <= MAX_RUN_BYTES


def test_presentation_cap_does_not_silently_drop_identifiers(tmp_path):
    store, bundle = make_bundle(tmp_path, ("source_urls",))
    text = "a" * 90000 + "\nhttps://example.org/middle\n" + "b" * 90000
    store.capture(bundle, "web_fetch", {"ok": True, "result": text})
    context = presentation_context(bundle)
    assert len(context) <= 80000
    assert "https://example.org/middle" in context
    assert "incomplete" in context.lower() or "artifact" in context.lower()
    assert "https://example.org/middle" in render_evidence(bundle)


def test_cleanup_removes_only_old_positively_delivered_runs(tmp_path):
    store, delivered = make_bundle(tmp_path)
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=8)).isoformat()
    store.mark_delivered(delivered["run_id"], delivered_at=old)
    pending = store.create(delivered["contract"])
    pending["created_at"] = old
    store.save(pending)
    unknown = store.create(delivered["contract"])
    unknown["created_at"] = old
    unknown["delivery_status"] = "unknown"
    unknown["delivered_at"] = old
    store.save(unknown)
    fresh = store.create(delivered["contract"])
    store.mark_delivered(fresh["run_id"])
    assert store.cleanup(now=now) == 1
    assert not store.path_for(delivered["run_id"]).exists()
    for item in (pending, unknown, fresh):
        assert store.load(item["run_id"])


def test_shared_style_and_contract_remain_channel_neutral():
    contract = derive_request_contract("List folders", route="investigate", required_fields=("directory_names",))
    assert contract == {"subject": "List folders", "route": "investigate", "required_fields": ["directory_names"], "needs_observation": True}
    assert "required" in NATURAL_STYLE_POLICY.lower()
    assert "source" in NATURAL_STYLE_POLICY.lower()
    with pytest.raises(ValueError):
        derive_request_contract("List folders", route="arbitrary")


@pytest.mark.asyncio
async def test_agent_captures_raw_results_before_step_and_model_clipping(tmp_path, monkeypatch):
    from omniagent.app import agent
    from omniagent.integrations.capabilities import CapabilityService
    text = "a" * 22000 + "\nProjects/\nhttps://example.org/middle\npassword: do-not-store\n" + "b" * 22000
    calls = 0
    async def model(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"content": "", "tool_calls": [{"id": "call-1", "name": "execute_shell", "arguments": '{"command":"ls -F ~/Desktop"}'}],
                    "finish_reason": "tool_calls", "usage": agent.ZERO_USAGE}, "openai"
        assert "do-not-store" not in str(args[1])
        return {"content": "Klasör: Projects", "tool_calls": [], "finish_reason": "stop", "usage": agent.ZERO_USAGE}, "openai"
    async def execute(*args, **kwargs):
        return [{"ok": True, "result": text}]
    monkeypatch.setattr(agent, "_call_model_with_retries", model)
    monkeypatch.setattr(agent, "_execute_tool_calls", execute)
    service = CapabilityService(tmp_path)
    try:
        report = await agent.run_agent_with_callback("List directory names", lambda event: None,
            {"requested_backend": "openai", "should_stop": lambda: False, "state_file": str(tmp_path / "state.json"),
             "history": [], "integrations": service}, {"openai": object()})
    finally:
        await service.close()
    bundle = report["evidence"]
    source = bundle["observations"][0]["text"]
    assert "Projects/" in source
    assert "https://example.org/middle" in source
    assert "do-not-store" not in source
    assert len(source) > 40000
    assert EvidenceStore(tmp_path / "state.json").load(bundle["run_id"]) == bundle


@pytest.mark.asyncio
async def test_automatic_observation_captures_receipt_before_archive_limit(tmp_path, monkeypatch):
    from omniagent.app import agent
    store, bundle = make_bundle(tmp_path)
    raw = "camera metadata " + "a" * 14000 + " middle fact"
    async def tool(*args, **kwargs):
        return {"ok": False, "error_type": "ImageError", "error": raw}
    monkeypatch.setattr(agent, "_run_tool_with_events", tool)
    token = agent._EVIDENCE_CAPTURE.set(lambda call, result: store.capture(bundle, call["name"], result))
    try:
        _, step, digest = await agent._observe_after_actions("test-capture", 0, "preview", object(), {}, lambda event: None, lambda: False)
    finally:
        agent._EVIDENCE_CAPTURE.reset(token)
    assert "middle fact" in bundle["observations"][0]["text"]
    assert not digest
    assert not step["ok"]


@pytest.mark.asyncio
async def test_agent_startup_failure_and_store_failure_still_finish(tmp_path, monkeypatch):
    from omniagent.app import agent
    def unavailable(*args, **kwargs):
        raise OSError("unwritable evidence")
    monkeypatch.setattr(agent.EvidenceStore, "save", unavailable)
    events = []
    report = await agent.run_agent_with_callback("Read folders", events.append,
        {"requested_backend": "openai", "should_stop": lambda: False, "state_file": str(tmp_path / "state.json"), "history": []}, {})
    assert not report["success"]
    assert not report["evidence"]["complete"]
    assert events[-1]["kind"] == "run_finished"


@pytest.mark.parametrize("marker", ["…[stdout çıktısı 1048576 bayt sınırında kırpıldı]", "…liste öğe/süre sınırıyla kısaltıldı; aranan öğe yoksa yenile"])
def test_existing_tool_limit_markers_are_incomplete(tmp_path, marker):
    store, bundle = make_bundle(tmp_path)
    store.capture(bundle, "execute_shell", {"ok": True, "result": "Projects/\n" + marker})
    assert not bundle["complete"]


def test_duplicate_run_creation_cannot_erase_pending_evidence(tmp_path):
    store, bundle = make_bundle(tmp_path)
    store.capture(bundle, "execute_shell", {"ok": True, "result": "Projects/"})
    with pytest.raises(FileExistsError):
        store.create(bundle["contract"], bundle["run_id"])
    assert store.load(bundle["run_id"])["observations"][0]["text"] == "Projects/"


def test_source_reference_budget_is_honest_and_unicode_capture_is_byte_bounded(tmp_path):
    store, bundle = make_bundle(tmp_path)
    store.capture(bundle, "execute_shell", {"ok": True, "result": "İ" * MAX_OBSERVATION_BYTES}, source_reference="a" * MAX_RUN_BYTES)
    assert not bundle["complete"]
    observation = bundle["observations"][0]
    assert len(observation["text"].encode()) <= MAX_OBSERVATION_BYTES
    assert "kayıt" in render_evidence(bundle)
    assert store.load(bundle["run_id"])


def test_loading_rejects_artifact_symlink_and_wrong_identity(tmp_path):
    store, bundle = make_bundle(tmp_path)
    store.capture(bundle, "read_file", {"ok": True, "result": "a" * (MAX_OBSERVATION_BYTES + 1)})
    artifact = Path(bundle["observations"][0]["artifact_path"])
    artifact.unlink()
    outside = tmp_path / "outside.txt"
    outside.write_text("private outside data")
    artifact.symlink_to(outside)
    with pytest.raises(ValueError):
        store.load(bundle["run_id"])
    artifact.unlink()
    store.path_for(bundle["run_id"]).write_text(json.dumps({**bundle, "run_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"}))
    with pytest.raises(ValueError):
        store.load(bundle["run_id"])


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery_status", ["pending", "unknown", "delivered"])
async def test_agent_handoff_appends_only_pending_matching_run(tmp_path, delivery_status):
    from omniagent.app import agent
    store, bundle = make_bundle(tmp_path)
    store.capture(bundle, "execute_shell", {"ok": True, "result": "Projects/"})
    bundle["delivery_status"] = delivery_status
    if delivery_status == "delivered":
        bundle["delivered_at"] = datetime.now(timezone.utc).isoformat()
    store.save(bundle)
    original = store.path_for(bundle["run_id"]).read_bytes()
    report = await agent.run_agent_with_callback(bundle["contract"]["subject"], lambda event: None,
        {"requested_backend": "openai", "should_stop": lambda: False, "state_file": str(tmp_path / "state.json"), "history": [],
         "request_contract": bundle["contract"], "evidence_run_id": bundle["run_id"]}, {})
    if delivery_status == "pending":
        assert report["evidence"]["run_id"] == bundle["run_id"]
        assert report["evidence"]["observations"][0]["text"] == "Projects/"
    else:
        assert report["evidence"]["run_id"] != bundle["run_id"]
        assert store.path_for(bundle["run_id"]).read_bytes() == original


def test_oversized_capture_retains_head_tail_and_omitted_middle_marker(tmp_path):
    store, bundle = make_bundle(tmp_path, ("source_urls",))
    text = "HEAD-RECEIPT\n" + "x" * MAX_RUN_BYTES + "\nTAIL-RECEIPT https://example.org/tail"
    store.capture(bundle, "fetch_raw", {"ok": True, "result": text})
    observation = bundle["observations"][0]
    assert "HEAD-RECEIPT" in observation["text"]
    assert "TAIL-RECEIPT https://example.org/tail" in observation["text"]
    assert "orta bölüm" in observation["text"] and "kırpıldı" in observation["text"]
    assert "artifact_path" not in observation
    assert not observation["complete"]
    assert len(json.dumps(observation, ensure_ascii=False, separators=(",", ":")).encode()) <= MAX_OBSERVATION_BYTES


def test_directory_identifiers_require_listing_provenance(tmp_path):
    store, bundle = make_bundle(tmp_path, ("directory_names",))
    store.capture(bundle, "execute_shell", {"ok": True, "result": "inspection finished\n/Users/example/Desktop"},
                  arguments=json.dumps({"command": "echo 'inspection finished'; pwd"}))
    store.capture(bundle, "execute_shell", {"ok": True, "result": "ÇIKIŞ_KODU: 0\nSTDOUT:\nProjects/\nPhotos/\nnotes.txt\nSTDERR:"},
                  arguments=json.dumps({"command": "ls -F ~/Desktop"}))
    assert required_identifiers(bundle) == ["Projects", "Photos"]
    assert check_grounded_answer("Projects ve Photos", bundle)["ok"]


@pytest.mark.asyncio
async def test_real_tool_events_mask_output_chunks_and_keep_execution_arguments(monkeypatch):
    from omniagent import config
    from omniagent.app.tool_execution import _run_tool_with_events, _tool_result_to_message
    from omniagent.core.events import argument_tag
    from omniagent.tools import TOOL_RUNTIME
    monkeypatch.setattr(config, "secret_values", lambda: ("configured-secret-value",))
    events = []
    executed = []
    snapshots = []
    class Tools:
        memory_mutation_allowed = False
        async def execute_shell(self, command):
            executed.append(command)
            sink = TOOL_RUNTIME.get()["emit_output"]
            for text in ("Password: UNKNOWN-", "VALUE\nconfigured-secret-value\n", "safe result\n"):
                sink(text)
                snapshots.append(str(events))
            return "Password: UNKNOWN-VALUE\nconfigured-secret-value\nsafe result"
    call = {"id": "raw-call", "name": "execute_shell", "arguments": json.dumps({"command": "printf ordinary"})}
    result = await _run_tool_with_events(0, call, "ordinary", Tools(), {}, events.append, lambda: False)
    assert executed == ["printf ordinary"]
    assert result["result"].startswith("Password: UNKNOWN-VALUE")
    assert all("UNKNOWN" not in snapshot and "configured-secret" not in snapshot for snapshot in snapshots)
    assert "UNKNOWN-VALUE" not in str(events)
    assert "configured-secret-value" not in str(events)
    assert "safe result" in str(events)
    assert "UNKNOWN-VALUE" not in str(_tool_result_to_message(call, result))
    assert events[0]["argument_tag"] == argument_tag(call["name"], call["arguments"])


@pytest.mark.asyncio
async def test_typed_tool_result_label_and_started_preview_are_private():
    from omniagent.app.tool_execution import _run_tool_with_events, _tool_result_to_message
    from omniagent.app.agent import _assistant_entry, ZERO_USAGE
    from omniagent.tools import TOOL_RUNTIME
    events = []
    executed = []
    typed = "opaque-user-value"
    class Tools:
        memory_mutation_allowed = False
        async def cua_type_text(self, text):
            executed.append(text)
            TOOL_RUNTIME.get()["emit_output"](text)
            return f"Metin yazıldı ({len(text)} karakter): {text}"
    call = {"id": "typed-call", "name": "cua_type_text", "arguments": json.dumps({"text": typed})}
    result = await _run_tool_with_events(0, call, typed, Tools(), {}, events.append, lambda: False)
    turn = {"content": "", "tool_calls": [call], "finish_reason": "tool_calls", "usage": ZERO_USAGE}
    assert executed == [typed]
    assert call["arguments"] == json.dumps({"text": typed})
    assert typed not in str(events)
    assert typed not in str(_tool_result_to_message(call, result))
    assert typed not in str(_assistant_entry(turn))
    assert json.loads(_assistant_entry(turn)["tool_calls"][0]["function"]["arguments"])["text"] == "[gizli]"


@pytest.mark.asyncio
async def test_streamed_partial_typed_arguments_never_enter_preview_events():
    from types import SimpleNamespace
    from omniagent.app.agent import _stream_completion
    from omniagent.config import BACKENDS
    from omniagent.core.events import preview_arguments
    typed = "opaque-user-value"
    chunks = []
    for index, fragment in enumerate(('{"text":"opaque-user-', 'value"}')):
        chunks.append(SimpleNamespace(usage=None, choices=[SimpleNamespace(
            delta=SimpleNamespace(content=None, model_extra={}, tool_calls=[SimpleNamespace(
                index=0, id="typed-stream" if index == 0 else None,
                function=SimpleNamespace(name="cua_type_text" if index == 0 else None, arguments=fragment))]),
            finish_reason="tool_calls" if index == 1 else None)]))
    class Stream:
        def __init__(self): self.chunks = iter(chunks)
        def __aiter__(self): return self
        async def __anext__(self):
            try: return next(self.chunks)
            except StopIteration: raise StopAsyncIteration
        async def close(self): pass
    class Completions:
        async def create(self, **kwargs): return Stream()
    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    events = []
    turn = await _stream_completion(client, BACKENDS["openai"], [{"role": "user", "content": "Fill the field"}], [],
                                    "session", events.append, lambda: False)
    assert turn["tool_calls"][0]["arguments"] == json.dumps({"text": typed}, separators=(",", ":"))
    assert "opaque-user" not in str(events)
    assert "opaque-user" not in preview_arguments("cua_type_text", '{"text":"opaque-user-')


@pytest.mark.asyncio
async def test_agent_real_tool_pipeline_masks_typed_history_without_changing_execution(tmp_path, monkeypatch):
    from omniagent.app import agent
    from omniagent.integrations.capabilities import CapabilityService
    from omniagent.tools import Toolbox, ToolError
    typed = "opaque-user-value"
    executed = []
    observed_messages = []
    async def type_text(self, text):
        executed.append(text)
        return f"Metin yazıldı ({len(text)} karakter): {text}"
    async def screenshot(self, filename, detail=False):
        raise ToolError("No camera available", "SCREEN_UNAVAILABLE", False)
    async def model(*args, **kwargs):
        observed_messages.append(str(args[1]))
        return {"content": "" if len(observed_messages) == 1 else "İşlem denendi.",
                "tool_calls": [{"id": "typed-call", "name": "cua_type_text", "arguments": json.dumps({"text": typed})}] if len(observed_messages) == 1 else [],
                "finish_reason": "tool_calls" if len(observed_messages) == 1 else "stop", "usage": agent.ZERO_USAGE}, "openai"
    monkeypatch.setattr(Toolbox, "cua_type_text", type_text)
    monkeypatch.setattr(Toolbox, "take_screenshot", screenshot)
    monkeypatch.setattr(agent, "_call_model_with_retries", model)
    service = CapabilityService(tmp_path)
    events = []
    try:
        report = await agent.run_agent_with_callback("Chrome sekmesindeki alanı incele", events.append,
            {"requested_backend": "openai", "should_stop": lambda: False, "state_file": str(tmp_path / "state.json"),
             "history": [], "integrations": service, "max_iterations": 2}, {"openai": object()})
    finally:
        await service.close()
    assert executed == [typed], report
    assert len(observed_messages) == 2
    assert typed not in observed_messages[1]
    assert typed not in str(events)
    assert typed not in json.dumps(report["evidence"])


@pytest.mark.asyncio
async def test_real_automatic_observation_and_hosted_results_mask_before_events(tmp_path):
    from omniagent.app import agent
    from omniagent.tools import ToolError
    events = []
    class Tools:
        memory_mutation_allowed = False
        async def take_screenshot(self, filename, detail=False):
            raise ToolError("Password: unknown-camera-value", "SCREEN_ERROR", False)
    observation, _, _ = await agent._observe_after_actions("auto-private", 0, "Ekran okunuyor", Tools(), {}, events.append, lambda: False)
    assert "unknown-camera-value" not in str(events)
    assert "unknown-camera-value" not in str(observation)
    async def ask(*args):
        return {"onay": False, "yanit": "Password: unknown-hosted-value"}
    from types import SimpleNamespace
    call = {"id": "hosted-private", "name": "report_goal_met", "arguments": json.dumps({
        "summary": "İnceleme tamamlandı, kaynak okundu.", "evidence_call_ids": ["source"]})}
    result, _, _, asked = await agent.resolve_goal_report(call, 0, {"source": "read source"}, SimpleNamespace(ask=ask), events.append, frozenset(), 0)
    assert asked
    assert "unknown-hosted-value" in result["result"]  # original receipt still reaches capture
    assert "unknown-hosted-value" not in str(events)


@pytest.mark.asyncio
async def test_tool_output_buffer_overflow_does_not_flush_partial_secret():
    from omniagent.app.tool_execution import OUTPUT_PRESENTATION_BUFFER_LIMIT, _run_tool_with_events
    from omniagent.tools import TOOL_RUNTIME
    events = []
    class Tools:
        memory_mutation_allowed = False
        async def execute_shell(self, command):
            sink = TOOL_RUNTIME.get()["emit_output"]
            sink("Password: unsafe-partial")
            assert not any(event["kind"] == "tool_output" for event in events)
            sink("x" * OUTPUT_PRESENTATION_BUFFER_LIMIT)
            sink("secret-tail")
            return "safe full receipt"
    call = {"id": "overflow-output", "name": "execute_shell", "arguments": '{"command":"fixture"}'}
    result = await _run_tool_with_events(0, call, "fixture", Tools(), {}, events.append, lambda: False)
    assert result["result"] == "safe full receipt"
    assert "unsafe-partial" not in str(events) and "secret-tail" not in str(events)
    output = [event for event in events if event["kind"] == "tool_output"]
    assert len(output) == 1 and "gösterim sınırını" in output[0]["text"]


def test_structured_directory_facts_and_shell_compound_commands():
    from omniagent.core.evidence import new_evidence_bundle
    from datetime import datetime, timezone
    bundle = new_evidence_bundle(derive_request_contract("List folders", required_fields=("directory_names",)))
    observation = {"tool": "list_directory", "source_type": "tool", "source_reference": "/Desktop", "observed_at": datetime.now(timezone.utc).isoformat(),
                   "text": json.dumps({"entries": [{"name": "Projects", "type": "directory"}, {"name": "notes.txt", "type": "file"}]}),
                   "ok": True, "status": "ok", "complete": True, "completeness": "full"}
    bundle["observations"].append(observation)
    bundle["observations"].append({**observation, "tool": "execute_shell", "source_reference": '{"command":"ls -F; echo fake/"}', "text": "fake/"})
    assert required_identifiers(bundle) == ["Projects"]


def test_presentation_arguments_mask_nested_format_secrets_and_keep_valid_json():
    from omniagent.core.evidence import sanitize_presentation_arguments
    original = {"path": "/safe/path", "password": "opaque-format-value", "nested": {"header": "Bearer sk-example-token-1234567890"}}
    masked = sanitize_presentation_arguments("some_tool", json.dumps(original))
    parsed = json.loads(masked)
    assert parsed["path"] == "/safe/path"
    assert parsed["password"] == "[gizli]"
    assert "opaque-format-value" not in masked
    assert "sk-example-token" not in masked
    assert original["password"] == "opaque-format-value"


@pytest.mark.asyncio
async def test_hosted_notice_and_confirmation_mask_full_text_before_clipping():
    from omniagent.app import agent
    from types import SimpleNamespace
    events = []
    questions = []
    secret = "opaque-summary-value" * 15
    summary = "İnceleme tamamlandı. Password: " + secret
    call = {"id": "hosted-notice", "name": "report_goal_met", "arguments": json.dumps({"summary": summary, "evidence_call_ids": ["source"]})}
    async def ask(question, *args):
        questions.append(question)
        return {"onay": False}
    runtime = SimpleNamespace(ask=ask)
    await agent.resolve_goal_report(call, 0, {"source": "read source"}, runtime, events.append, frozenset(), 0, defer_confirmation=True)
    await agent.resolve_goal_report(call, 0, {"source": "Password: opaque-source-value"}, runtime, events.append, frozenset(), 0)
    assert "opaque-summary-value" not in str(events)
    assert "opaque-summary-value" not in str(questions)
    assert "opaque-source-value" not in str(questions)
    async def approve(*args):
        return {"onay": True}
    _, confirmed, _, _ = await agent.resolve_goal_report(call, 0, {"source": "read source"}, SimpleNamespace(ask=approve), events.append, frozenset(), 0)
    assert "opaque-summary-value" not in confirmed
    assert secret in call["arguments"]  # approval/execution input was not rewritten
