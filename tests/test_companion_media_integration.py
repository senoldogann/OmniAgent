"""Actual image parts, restart attachment metadata and transcript evidence boundaries."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from PIL import Image

from omniagent.app.model_retry import ModelCallFailed
from omniagent.companion import chat
from omniagent.memory.personal import PersonalStore, utc_iso
from omniagent.memory.learning import passes_first_gate


def test_attachment_cursor_and_transcript_survive_restart(tmp_path):
    path = tmp_path / "companion.db"
    now = utc_iso(datetime.now(timezone.utc))
    store = PersonalStore(path)
    attachments = [{"path": "/tmp/voice.m4a", "mime_type": "audio/mp4"}]
    mid = store.record_incoming(7, "g7", "[dosya: voice.m4a]", now, attachments)
    assert mid is not None
    assert store.append_transcript(mid, "cuma İzmir'e gidiyorum")
    assert not store.append_transcript(mid, "ikinci döküm")
    assert not store.append_transcript(mid, "password: private-secret")
    store.close()
    reopened = PersonalStore(path)
    try:
        assert reopened.cursor() == 7
        assert reopened.incoming_media(mid) == attachments
        assert reopened.message(mid)["text"].endswith("🎤 cuma İzmir'e gidiyorum")
        assert reopened.message(mid)["created_at"] == now
        assert reopened.record_incoming(7, "g7", "duplicate", now, []) is None
        assert reopened.recall("İzmir", 8)[0]["direction"] == "in"
    finally:
        reopened.close()


def test_transcription_secret_never_enters_archive(tmp_path):
    store = PersonalStore(tmp_path / "companion.db")
    try:
        mid = store.record_incoming(1, "g1", "[dosya: voice.m4a]", utc_iso(datetime.now(timezone.utc)))
        assert not store.append_transcript(mid, "benim password: super-secret-value")
        assert "super-secret" not in store.message(mid)["text"]
        assert store.get_state(f"transcribed:{mid}") is None
    finally:
        store.close()


def test_image_turn_contains_actual_jpeg(tmp_path):
    path = tmp_path / "photo.jpg"
    Image.new("RGB", (20, 10), "green").save(path)
    turn = chat.image_turn("ne görüyorsun?", [path])
    assert turn["content"][0] == {"type": "text", "text": "ne görüyorsun?"}
    assert turn["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,/9j/")


def test_removing_attachment_markers_cannot_invent_a_contiguous_user_quote(tmp_path):
    store = PersonalStore(tmp_path / "companion.db")
    try:
        mid = store.record_incoming(1, "g1", "kızımın adı\n[fotoğraf: başka.png]\nEla, okula başladı",
                                    utc_iso(datetime.now(timezone.utc)))
        source = store.pending_evidence(0, 5)[0]
        candidate = {"statement": "Kullanıcının kızının adı Ela", "quote": "kızımın adı Ela", "message_id": mid,
                     "category": "kisi", "supersedes": None, "follow_up_at": None}
        assert not passes_first_gate(candidate, {mid: source}, set())
    finally:
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [False, True])
async def test_image_fallback_requires_image_permission_and_audit(tmp_path, monkeypatch, allowed):
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OMNI_FALLBACK_BACKENDS", "openai")
    monkeypatch.setenv("OMNI_FALLBACK_IMAGES", "1" if allowed else "0")
    calls = []
    async def model(clients, messages, tools, session, backend, emit, stopped):
        calls.append(backend)
        if backend == "opencode":
            raise ModelCallFailed("image input is not supported", backend=backend, kind="permanent",
                                  attempts=1, waited_seconds=0)
        assert (tmp_path / "audit.jsonl").exists()
        return {"tool_calls": [], "text": "yeşil", "finish_reason": "stop"}, backend
    monkeypatch.setattr(chat, "call_model_with_retries", model)
    messages = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,x"}}]}]
    if allowed:
        result = await chat._chat_completion({"opencode": object(), "openai": object()}, messages, [], "session",
                                             "opencode", lambda event: None, lambda: False)
        assert result[1] == "openai"
        assert json.loads((tmp_path / "audit.jsonl").read_text())["decision"] == "policy_allowed"
        assert calls == ["opencode", "openai"]
    else:
        with pytest.raises(ModelCallFailed):
            await chat._chat_completion({"opencode": object(), "openai": object()}, messages, [], "session",
                                       "opencode", lambda event: None, lambda: False)
        assert calls == ["opencode"]
        assert not (tmp_path / "audit.jsonl").exists()
