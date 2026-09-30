"""Phase D bridge acceptance: real store/turn boundaries and macOS photo conversion."""
import asyncio
import sys
import threading
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from PIL import Image

from omniagent.companion import chat, media
from omniagent.integrations import imessage
from omniagent.memory.personal import PersonalStore, utc_iso
from test_imessage_bridge import HANDLE, incoming, parts, settle


def attached(rowid, text="", sender=HANDLE, images=(), audio=(), **flags):
    return {**incoming(rowid, text, sender), **flags,
            "attachments": [{"path": str(path), "mime_type": "image/png"} for path in images]
            + [{"path": str(path), "mime_type": "audio/mp4"} for path in audio]}


class RecordingChat:
    def __init__(self):
        self.messages = []

    async def __call__(self, clients, backend, system, messages, tools, send_bubble, should_stop, session_id):
        self.messages.append(messages)
        await send_bubble("tamam, duydum")
        return {"bubbles": ["tamam, duydum"], "start_task": None}


@pytest.mark.skipif(sys.platform != "darwin", reason="real macOS sips bridge path")
@pytest.mark.asyncio
async def test_photo_only_and_audio_rows_keep_burst_indices(parts, tmp_path, monkeypatch):
    bridge, transport, store = parts
    paths = [tmp_path / f"photo{i}.png" for i in range(3)]
    for path in paths:
        Image.new("RGB", (20, 10), "green").save(path)
    audio_paths = [tmp_path / f"voice{i}.m4a" for i in range(2)]
    speech = []
    async def transcribe(path, backend, clients, model):
        speech.append(path)
        return "kızımın adı Ela" if path == audio_paths[0] else "cuma İzmir'e gidiyorum"
    scripted = RecordingChat()
    monkeypatch.setattr(media, "transcribe_audio", transcribe)
    monkeypatch.setattr(chat, "respond", scripted)
    await bridge.on_message(attached(10, images=[paths[0]]))
    await bridge.on_message(attached(11, audio=[audio_paths[0]]))
    await bridge.on_message(attached(12, "bunlara bak", images=paths[1:], audio=[audio_paths[1]]))
    await settle(bridge)
    assert speech == audio_paths and len(scripted.messages) == 1
    content = scripted.messages[0][-1]["content"]
    assert len(content) == 4  # text plus three actual photos
    assert content[0]["text"].count("🎤") == 2
    assert "kızımın adı Ela" in content[0]["text"] and "cuma İzmir'e gidiyorum" in content[0]["text"]
    rows = [row for row in store.recent_messages(10) if row["direction"] == "in"]
    assert "🎤" not in rows[0]["text"]
    assert rows[1]["text"].endswith("🎤 kızımın adı Ela")
    assert rows[2]["text"].endswith("🎤 cuma İzmir'e gidiyorum")
    assert transport.texts == ["tamam, duydum"]
    assert all(store.get_state(f"audio_pending:{row['id']}") is None for row in rows)


@pytest.mark.asyncio
async def test_only_accepted_direct_audio_is_transcribed(parts, tmp_path, monkeypatch):
    bridge, transport, store = parts
    path = tmp_path / "voice.m4a"
    calls = []
    async def transcribe(path, backend, clients, model):
        calls.append(path)
        return "kızımın adı Ela"
    scripted = RecordingChat()
    monkeypatch.setattr(media, "transcribe_audio", transcribe)
    monkeypatch.setattr(chat, "respond", scripted)
    await bridge.on_message(attached(20, sender="+905559998877", audio=[path]))
    await bridge.on_message(attached(21, audio=[path], is_group=True))
    await bridge.on_message(attached(22, sender="", audio=[path], is_from_me=True, participants=[HANDLE]))
    await settle(bridge)
    assert calls == [] and scripted.messages == [] and store.recent_messages(10) == []
    await bridge.on_message(attached(23, audio=[path]))
    await settle(bridge)
    assert calls == [path] and len(scripted.messages) == 1
    await bridge.on_message(attached(23, audio=[path]))
    await settle(bridge)
    assert calls == [path]  # watch replay does not upload again


@pytest.mark.asyncio
async def test_audio_only_failure_sends_one_fixed_reply_and_never_calls_model(parts, tmp_path, monkeypatch):
    bridge, transport, store = parts
    async def failure(*args):
        raise media.MediaFailed("audio_transcription_empty")
    scripted = RecordingChat()
    monkeypatch.setattr(media, "transcribe_audio", failure)
    monkeypatch.setattr(chat, "respond", scripted)
    await bridge.on_message(attached(30, audio=[tmp_path / "first.caf", tmp_path / "second.m4a"]))
    await bridge.on_message(attached(31, audio=[tmp_path / "third.amr"]))
    await settle(bridge)
    assert transport.texts == [media.AUDIO_FAILURE_TEXT] and scripted.messages == []
    for row in store.recent_messages(10):
        if row["direction"] == "in":
            assert store.get_state(f"audio_pending:{row['id']}") is None
            assert "🎤" not in row["text"]


@pytest.mark.asyncio
async def test_mixed_audio_failure_waits_for_remaining_speech_before_reply(parts, tmp_path, monkeypatch):
    bridge, transport, store = parts
    bad, good = tmp_path / "first.caf", tmp_path / "second.m4a"
    entered, release = asyncio.Event(), asyncio.Event()
    async def transcribe(path, *args):
        if path == bad:
            raise media.MediaFailed("audio_transcription_empty")
        entered.set()
        await release.wait()
        return "kızımın adı Ela"
    scripted = RecordingChat()
    monkeypatch.setattr(media, "transcribe_audio", transcribe)
    monkeypatch.setattr(chat, "respond", scripted)
    await bridge.on_message(attached(32, audio=[bad]))
    await bridge.on_message(attached(33, audio=[good]))
    await asyncio.wait_for(entered.wait(), 5)
    try:
        assert transport.texts == []
        assert len(store.unanswered_burst(datetime.now(timezone.utc), 60)) == 2
    finally:
        release.set()
    await settle(bridge)
    assert len(scripted.messages) == 1
    assert "🎤 kızımın adı Ela" in scripted.messages[0][-1]["content"]
    assert transport.texts == ["tamam, duydum", media.AUDIO_FAILURE_TEXT]


@pytest.mark.asyncio
async def test_restart_reuses_successful_transcript_before_response(parts, tmp_path, monkeypatch):
    bridge, transport, store = parts
    message = attached(40, audio=[tmp_path / "voice.m4a"])
    bridge.archive_backlog(message)
    mid = store.recent_messages(1)[0]["id"]
    assert store.append_transcript(mid, "cuma İzmir'e gidiyorum")
    store.finish_transcription(mid)
    scripted = RecordingChat()
    async def no_upload(*args):
        raise AssertionError("restart re-uploaded archived speech")
    monkeypatch.setattr(media, "transcribe_audio", no_upload)
    monkeypatch.setattr(chat, "respond", scripted)
    # New bridge instance shares only persisted state, as on service restart.
    restarted = imessage.ImessageBridge(transport, bridge.settings, store, {}, "# Deniz", "restarted")
    await restarted.answer_unanswered()
    assert len(scripted.messages) == 1
    assert scripted.messages[0][-1]["content"].count("🎤 cuma İzmir'e gidiyorum") == 1
    assert transport.texts == ["tamam, duydum"]


@pytest.mark.asyncio
async def test_restart_completes_pending_audio_after_committed_transcript(parts, tmp_path, monkeypatch):
    bridge, transport, store = parts
    bridge.archive_backlog(attached(41, audio=[tmp_path / "voice.m4a"]))
    mid = store.recent_messages(1)[0]["id"]
    pending_at = store.get_state(f"audio_pending:{mid}")
    assert pending_at is not None
    assert store.append_transcript(mid, "cuma İzmir'e gidiyorum")
    assert store.get_state(f"audio_pending:{mid}") is None  # successful append completes atomically
    # Recover an older persisted crash state where transcript and completion were separate transactions.
    store.set_state(f"audio_pending:{mid}", pending_at)
    assert store.get_state(f"audio_pending:{mid}") is not None
    scripted = RecordingChat()
    async def no_upload(*args):
        raise AssertionError("cached transcript should not be uploaded again")
    monkeypatch.setattr(media, "transcribe_audio", no_upload)
    monkeypatch.setattr(chat, "respond", scripted)
    restarted = imessage.ImessageBridge(transport, bridge.settings, store, {}, "# Deniz", "restarted")
    await restarted.answer_unanswered()
    assert len(scripted.messages) == 1
    assert store.get_state(f"audio_pending:{mid}") is None
    assert "cuma İzmir'e gidiyorum" in store.pending_evidence(0, 100)[0]["text"]


def test_pending_audio_blocks_memory_batch_until_transcription_finishes(tmp_path):
    now = utc_iso(datetime.now(timezone.utc))
    with closing(PersonalStore(tmp_path / "companion.db")) as store:
        first = store.record_channel_message("telegram", "kızımın adı Ela", now)
        voice = store.record_incoming(50, "g50", "[dosya: voice.m4a]", now,
                                      [{"path": "/tmp/voice.m4a", "mime_type": "audio/mp4"}])
        last = store.record_channel_message("desktop", "cuma İzmir'e gidiyorum", now)
        batch = store.pending_evidence(0, 100)
        assert [row["id"] for row in batch] == [first]
        assert store.commit_learning(0, first, [], now) == []
        assert store.pending_evidence(first, 100) == []
        assert store.append_transcript(voice, "yarın anneme gidiyorum")
        store.finish_transcription(voice)
        batch = store.pending_evidence(first, 100)
        assert [row["id"] for row in batch] == [voice, last]
        assert batch[0]["text"] == "🎤 yarın anneme gidiyorum"
        assert store.commit_learning(first, last, [], now) == []
        assert store.memory_cursor() == last


def test_host_attachment_filenames_never_become_profile_evidence(tmp_path):
    now = utc_iso(datetime.now(timezone.utc))
    with closing(PersonalStore(tmp_path / "companion.db")) as store:
        store.record_incoming(60, "g60", "[fotoğraf: kızımın adı Ela.png]\n[dosya: cuma İzmir'e gidiyorum.pdf]", now)
        store.record_incoming(61, "g61", "annemin adı Ayşe\n[fotoğraf: kızımın adı Ela.png]", now)
        batch = store.pending_evidence(0, 100)
        assert batch[0]["text"] == "" and batch[1]["text"] == "annemin adı Ayşe"
        assert all("Ela" not in item["text"] and "İzmir" not in item["text"] for item in batch)


def test_stale_pending_audio_does_not_block_memory_forever(tmp_path):
    old = utc_iso(datetime.now(timezone.utc) - timedelta(hours=2))
    with closing(PersonalStore(tmp_path / "companion.db")) as store:
        voice = store.record_incoming(70, "g70", "[dosya: voice.m4a]", old,
                                     [{"path": "/tmp/voice.m4a", "mime_type": "audio/mp4"}])
        assert [row["id"] for row in store.pending_evidence(0, 100)] == [voice]


@pytest.mark.asyncio
async def test_cancelled_threaded_photo_conversion_waits_and_cleans(tmp_path, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    objects = []
    original = media.prepare_images
    def blocked(paths):
        prepared = original([])
        objects.append(prepared)
        entered.set()
        assert release.wait(5)
        return prepared
    monkeypatch.setattr(media, "prepare_images", blocked)
    task = asyncio.create_task(imessage.prepare_turn_images([]))
    assert await asyncio.to_thread(entered.wait, 5)
    directory = Path(objects[0]._directory.name)
    assert directory.exists()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()  # cancellation must join the conversion worker
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert not directory.exists()
