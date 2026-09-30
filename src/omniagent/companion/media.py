"""Bounded macOS attachment conversion and explicitly configured voice transcription.

Only temporary JPEG/M4A files are created. Conversion errors exclude source paths, tool output and user content.
The caller owns model clients and image context lifetime; no provider or model fallback is selected here.
"""
from __future__ import annotations

import asyncio
import subprocess
from contextlib import AbstractContextManager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import List, Mapping, Optional, Sequence, Union

from openai import APIError, AsyncOpenAI
from PIL import Image, UnidentifiedImageError

MAX_IMAGE_BYTES: int = 5 * 1024 * 1024
MAX_IMAGE_FILES: int = 4
MAX_IMAGE_EDGE: int = 1600
MAX_AUDIO_BYTES: int = 25 * 1024 * 1024
CONVERSION_SECONDS: float = 30.0
TRANSCRIPTION_SECONDS: float = 60.0
AUDIO_SUFFIXES = frozenset((".caf", ".m4a", ".amr"))
AUDIO_FAILURE_TEXT = "sesini açamadım, yazar mısın?"


class MediaUnavailable(Exception):
    """The explicit transcription profile/model is disabled or unavailable."""


class MediaFailed(Exception):
    """Attachment reading, conversion or transcription failed, without user content."""


def _checked_file(path: Path, maximum: int) -> None:
    try:
        if not path.is_file():
            raise MediaFailed("media_not_a_file")
        size = path.stat().st_size
    except OSError:
        raise MediaFailed("media_unreadable") from None
    if size == 0 or size > maximum:
        raise MediaFailed("media_size_limit")


def _convert(command: List[str]) -> None:
    try:
        result = subprocess.run(command, capture_output=True, timeout=CONVERSION_SECONDS, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise MediaFailed("media_conversion_unavailable") from None
    if result.returncode != 0:
        raise MediaFailed("media_conversion_failed")


class PreparedImages(AbstractContextManager):
    """JPEG paths and sanitized per-input failures; close/context exit removes the temporary directory."""

    def __init__(self, paths: Sequence[Union[str, Path]]) -> None:
        self.image_paths: List[Path] = []
        self.failures: List[str] = []
        self._directory = TemporaryDirectory(prefix="omniagent-photos-")
        try:
            for index, raw in enumerate(paths[:MAX_IMAGE_FILES]):
                output = Path(self._directory.name) / f"photo-{index}.jpg"
                try:
                    try:
                        source = Path(raw).expanduser().resolve()
                    except (OSError, RuntimeError):
                        raise MediaFailed("media_unreadable") from None
                    _checked_file(source, MAX_IMAGE_BYTES)
                    _convert(["/usr/bin/sips", "-s", "format", "jpeg", "-s", "formatOptions", "80",
                              str(source), "--out", str(output)])
                    try:
                        with Image.open(output) as photo:
                            edge = max(photo.size)
                    except (OSError, UnidentifiedImageError, Image.DecompressionBombError):
                        raise MediaFailed("image_conversion_invalid") from None
                    # sips -Z enlarges small photos too; apply it only when reduction is needed.
                    if edge > MAX_IMAGE_EDGE:
                        _convert(["/usr/bin/sips", "-Z", str(MAX_IMAGE_EDGE), str(output)])
                    _checked_file(output, MAX_IMAGE_BYTES)
                except MediaFailed as error:
                    self.failures.append(str(error))
                else:
                    self.image_paths.append(output)
            if len(paths) > MAX_IMAGE_FILES:
                self.failures.append("image_count_limit")
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        self._directory.cleanup()

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


def prepare_images(paths: Sequence[Union[str, Path]]) -> PreparedImages:
    """Prepare at most four input candidates, each at most 5 MB; use as a context manager for turn cleanup."""
    return PreparedImages(paths)


async def transcribe_audio(path: Path, backend: Optional[str], clients: Mapping[str, AsyncOpenAI],
                           model: Optional[str] = None) -> str:
    """Convert CAF/AMR to AAC M4A and use only the selected existing client and explicit transcription model.

    No HTTP request happens for disabled/incomplete config, absent clients or invalid/oversize files. The bridge
    archives the returned transcript as the user's own words and logs only these sanitized error codes.
    """
    if backend is None:
        raise MediaUnavailable("transcription_disabled")
    if backend not in clients or not isinstance(model, str) or not model.strip():
        raise MediaUnavailable("transcription_profile_unavailable")
    try:
        source = Path(path).expanduser().resolve()
    except (OSError, RuntimeError):
        raise MediaFailed("media_unreadable") from None
    if source.suffix.lower() not in AUDIO_SUFFIXES:
        raise MediaFailed("audio_format_unsupported")
    _checked_file(source, MAX_AUDIO_BYTES)
    with TemporaryDirectory(prefix="omniagent-voice-") as directory:
        upload = source
        if source.suffix.lower() != ".m4a":
            upload = Path(directory) / "voice.m4a"
            # Shield the conversion thread before deleting its output directory on cancellation.
            conversion = asyncio.create_task(asyncio.to_thread(
                _convert, ["/usr/bin/afconvert", "-f", "m4af", "-d", "aac", str(source), str(upload)],
            ))
            try:
                await asyncio.shield(conversion)
            except asyncio.CancelledError:
                try:
                    await conversion
                except MediaFailed:
                    pass
                raise
        _checked_file(upload, MAX_AUDIO_BYTES)
        try:
            # A generic upload name avoids sharing local attachment names with the provider.
            with upload.open("rb") as audio:
                result = await asyncio.wait_for(
                    clients[backend].audio.transcriptions.create(model=model.strip(), file=("voice.m4a", audio)),
                    timeout=TRANSCRIPTION_SECONDS,
                )
        except APIError as error:
            raise MediaFailed(f"audio_transcription_{type(error).__name__}") from None
        except (OSError, TimeoutError):
            raise MediaFailed("audio_transcription_unavailable") from None
        text = " ".join(str(getattr(result, "text", "") or "").split())
        if not text:
            raise MediaFailed("audio_transcription_empty")
        return text
