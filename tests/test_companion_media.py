"""Real macOS photo/audio conversion, bounded inputs and scripted transcription client."""
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

from omniagent.companion.media import MAX_IMAGE_BYTES, prepare_images

MACOS = pytest.mark.skipif(sys.platform != "darwin", reason="macOS built-in media tools")


@MACOS
@pytest.mark.parametrize("suffix", [".png", ".heic"])
def test_real_sips_photo_conversion_and_cleanup(tmp_path: Path, suffix: str) -> None:
    original = tmp_path / "source.png"
    Image.new("RGB", (2400, 1800), "navy").save(original)
    source = original
    if suffix == ".heic":
        source = tmp_path / "source.heic"
        subprocess.run(["/usr/bin/sips", "-s", "format", "heic", str(original), "--out", str(source)],
                       check=True, capture_output=True)
    original_bytes = source.read_bytes()
    with prepare_images([source]) as prepared:
        assert not prepared.failures
        assert len(prepared.image_paths) == 1
        converted = prepared.image_paths[0]
        with Image.open(converted) as photo:
            assert photo.format == "JPEG" and photo.size == (1600, 1200)
        assert converted.stat().st_size <= MAX_IMAGE_BYTES
    assert not converted.exists()
    assert not converted.parent.exists()
    assert source.read_bytes() == original_bytes


@MACOS
def test_four_candidate_limit_and_cleanup_on_caller_failure(tmp_path: Path) -> None:
    source = tmp_path / "source.png"
    Image.new("RGB", (32, 16)).save(source)
    with pytest.raises(RuntimeError):
        with prepare_images([source] * 5) as prepared:
            assert len(prepared.image_paths) == 4
            assert len(prepared.failures) == 1
            directory = prepared.image_paths[0].parent
            raise RuntimeError("caller failed")
    assert not directory.exists()


@MACOS
def test_invalid_oversize_and_missing_photos_are_content_free_errors(tmp_path: Path) -> None:
    invalid = tmp_path / "private-image-name.heic"
    invalid.write_bytes(b"private image contents")
    oversize = tmp_path / "large.jpg"
    with oversize.open("wb") as output:
        output.truncate(MAX_IMAGE_BYTES + 1)
    with prepare_images([invalid, oversize, tmp_path / "absent"]) as prepared:
        assert prepared.image_paths == [] and len(prepared.failures) == 3
        assert all("private" not in failure for failure in prepared.failures)


class ScriptedTranscriptions:
    def __init__(self, text="  kızımın adı\n Ela ", error=None):
        self.text, self.error, self.calls = text, error, []

    async def create(self, *, model, file):
        from types import SimpleNamespace
        name, handle = file
        self.calls.append((model, name, handle.read(), Path(handle.name)))
        if self.error:
            raise self.error
        return SimpleNamespace(text=self.text)


def fake_client(transcriptions):
    from types import SimpleNamespace
    return SimpleNamespace(audio=SimpleNamespace(transcriptions=transcriptions))


def audio_fixture(tmp_path, suffix):
    import wave
    wav = tmp_path / "voice.wav"
    with wave.open(str(wav), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x00\x00" * 16000)
    target = tmp_path / f"private-voice{suffix}"
    subprocess.run(["/usr/bin/afconvert", "-f", "caff" if suffix == ".caf" else "m4af", "-d",
                    "LEI16" if suffix == ".caf" else "aac", str(wav), str(target)],
                   check=True, capture_output=True)
    return target


@MACOS
@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", [".caf", ".m4a"])
async def test_real_afconvert_transcribes_selected_model_and_cleans(tmp_path, suffix):
    from omniagent.companion.media import transcribe_audio
    path = audio_fixture(tmp_path, suffix)
    endpoint = ScriptedTranscriptions()
    clients = {"chosen": fake_client(endpoint)}
    assert await transcribe_audio(path, "chosen", clients, "explicit-audio-model") == "kızımın adı Ela"
    assert len(endpoint.calls) == 1
    model, name, data, upload = endpoint.calls[0]
    assert model == "explicit-audio-model" and name == "voice.m4a" and b"ftyp" in data[:32]
    if suffix == ".caf":
        assert upload != path and not upload.exists() and not upload.parent.exists()
    assert path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend,model", [(None, None), ("missing", "whisper-1"), ("chosen", None), ("chosen", " ")])
async def test_disabled_incomplete_profile_never_opens_or_uploads(tmp_path, backend, model):
    from omniagent.companion.media import MediaUnavailable, transcribe_audio
    endpoint = ScriptedTranscriptions()
    with pytest.raises(MediaUnavailable):
        await transcribe_audio(tmp_path / "absent.caf", backend, {"chosen": fake_client(endpoint)}, model)
    assert endpoint.calls == []


@MACOS
@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", [".caf", ".amr"])
async def test_corrupt_audio_fails_before_network(tmp_path, suffix):
    from omniagent.companion.media import MediaFailed, transcribe_audio
    path = tmp_path / f"secret-name{suffix}"
    path.write_bytes(b"secret contents")
    endpoint = ScriptedTranscriptions()
    with pytest.raises(MediaFailed, match="media_conversion_failed") as error:
        await transcribe_audio(path, "chosen", {"chosen": fake_client(endpoint)}, "whisper-1")
    assert "secret" not in str(error.value) and endpoint.calls == []


@pytest.mark.asyncio
async def test_audio_size_limit_and_no_fallback(tmp_path):
    import httpx
    from openai import BadRequestError
    from omniagent.companion.media import MAX_AUDIO_BYTES, MediaFailed, transcribe_audio
    path = tmp_path / "voice.m4a"
    with path.open("wb") as audio:
        audio.truncate(MAX_AUDIO_BYTES + 1)
    endpoint = ScriptedTranscriptions()
    with pytest.raises(MediaFailed, match="media_size_limit"):
        await transcribe_audio(path, "chosen", {"chosen": fake_client(endpoint)}, "whisper-1")
    assert endpoint.calls == []
    path.write_bytes(b"fake m4a")
    request = httpx.Request("POST", "https://example.test/audio/transcriptions")
    endpoint.error = BadRequestError("SECRET user data", response=httpx.Response(400, request=request), body=None)
    other = ScriptedTranscriptions()
    with pytest.raises(MediaFailed, match="BadRequestError") as error:
        await transcribe_audio(path, "chosen", {"chosen": fake_client(endpoint), "other": fake_client(other)}, "bad-model")
    assert "SECRET" not in str(error.value) and len(endpoint.calls) == 1 and other.calls == []


@MACOS
@pytest.mark.asyncio
async def test_empty_transcript_and_cancellation_cleanup(tmp_path):
    import asyncio
    from omniagent.companion.media import MediaFailed, transcribe_audio
    path = audio_fixture(tmp_path, ".caf")
    endpoint = ScriptedTranscriptions(" ")
    with pytest.raises(MediaFailed, match="audio_transcription_empty"):
        await transcribe_audio(path, "chosen", {"chosen": fake_client(endpoint)}, "whisper-1")
    assert not endpoint.calls[0][3].exists()

    entered = asyncio.Event()
    uploads = []
    class WaitingEndpoint:
        async def create(self, *, model, file):
            uploads.append(Path(file[1].name))
            entered.set()
            await asyncio.Future()
    task = asyncio.create_task(transcribe_audio(path, "chosen", {"chosen": fake_client(WaitingEndpoint())}, "whisper-1"))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert uploads and not uploads[0].parent.exists()


@MACOS
def test_small_image_is_not_upscaled(tmp_path):
    source = tmp_path / "small.png"
    Image.new("RGB", (40, 20)).save(source)
    with prepare_images([source]) as prepared:
        with Image.open(prepared.image_paths[0]) as image:
            assert image.size == (40, 20)


def test_conversion_output_limit_and_timeout_are_content_free(tmp_path, monkeypatch):
    from omniagent.companion import media
    source = tmp_path / "input.jpg"
    source.write_bytes(b"small input")
    def oversized(command):
        target = Path(command[-1])
        Image.new("RGB", (10, 10)).save(target, format="JPEG")
        with target.open("ab") as output:
            output.truncate(MAX_IMAGE_BYTES + 1)
    monkeypatch.setattr(media, "_convert", oversized)
    with prepare_images([source]) as prepared:
        assert not prepared.image_paths and prepared.failures == ["media_size_limit"]
    def timed_out(*args, **kwargs):
        raise subprocess.TimeoutExpired("private-image-name", 30, output=b"private-content")
    monkeypatch.undo()
    monkeypatch.setattr(media.subprocess, "run", timed_out)
    with prepare_images([source]) as prepared:
        assert prepared.failures == ["media_conversion_unavailable"]


@pytest.mark.asyncio
async def test_existing_openai_sdk_posts_exact_endpoint_model_and_generic_name(tmp_path):
    import httpx
    from openai import AsyncOpenAI
    from omniagent.companion.media import transcribe_audio
    path = tmp_path / "PRIVATE-USER-NAME.m4a"
    path.write_bytes(b"audio bytes")
    requests = []
    async def handle(request):
        body = await request.aread()
        requests.append((str(request.url), body))
        return httpx.Response(200, json={"text": "kızımın adı Ela"})
    client = AsyncOpenAI(api_key="test", base_url="https://example.test/v1", max_retries=0,
                         http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    try:
        assert await transcribe_audio(path, "chosen", {"chosen": client}, "explicit-model") == "kızımın adı Ela"
        assert len(requests) == 1 and requests[0][0] == "https://example.test/v1/audio/transcriptions"
        assert b"explicit-model" in requests[0][1] and b'filename="voice.m4a"' in requests[0][1]
        assert b"PRIVATE-USER-NAME" not in requests[0][1]
        assert not client.is_closed()
    finally:
        await client.close()
