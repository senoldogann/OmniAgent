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
    assert "incomplete" in render_evidence(loaded).lower()
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
    assert "continu" in render_evidence(bundle).lower()
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
