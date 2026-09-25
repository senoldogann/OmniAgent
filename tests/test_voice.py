"""Sesli giriş yardımcısının izin açmadan doğrulanabilen yaşam döngüsü testleri."""
import builtins
from pathlib import Path

import pytest

from omniagent.platform.macos import voice


def test_voice_is_idle_without_native_permission(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[tuple[str, str]] = []
    controller = voice.VoiceInput(lambda text: None, lambda state, message: events.append((state, message)))
    assert not controller.active
    assert not controller.recording
    controller.cancel()
    assert events[-1][0] == "idle"


def test_voice_rejects_non_macos_before_import(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(voice.sys, "platform", "linux")
    controller = voice.VoiceInput(lambda text: None, lambda state, message: None)
    with pytest.raises(voice.VoiceInputError, match="macOS"):
        controller.start()


def test_voice_reports_missing_speech_dependency(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(voice.sys, "platform", "darwin")
    gerçek_import = builtins.__import__

    def speech_imports(name: str, *args: object, **kwargs: object) -> object:
        if name == "Speech":
            raise ImportError("Speech testte yok")
        return gerçek_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", speech_imports)
    controller = voice.VoiceInput(lambda text: None, lambda state, message: None)
    with pytest.raises(voice.VoiceInputError, match="bağımlılıklar"):
        controller.start()
    assert not controller.active


def test_voice_cancel_removes_temporary_audio(tmp_path: Path) -> None:
    audio = tmp_path / "recording.m4a"
    audio.write_bytes(b"test")
    controller = voice.VoiceInput(lambda text: None, lambda state, message: None)
    controller._path = audio
    controller.cancel()
    assert not audio.exists()
    assert not controller.active


def test_voice_preflight_rejects_missing_usage_descriptions() -> None:
    with pytest.raises(voice.VoiceInputError, match="NSSpeechRecognitionUsageDescription"):
        voice.voice_privacy_preflight({})

    with pytest.raises(voice.VoiceInputError, match="NSMicrophoneUsageDescription"):
        voice.voice_privacy_preflight({
            "NSSpeechRecognitionUsageDescription": "Sesli komutu yazıya çevirmek için kullanılır.",
        })


def test_voice_preflight_accepts_complete_usage_descriptions() -> None:
    voice.voice_privacy_preflight({
        "NSSpeechRecognitionUsageDescription": "Sesli komutu yazıya çevirmek için kullanılır.",
        "NSMicrophoneUsageDescription": "Sesli komutu kaydetmek için mikrofon kullanılır.",
    })


def test_voice_default_duration_stays_below_speech_one_minute_limit() -> None:
    controller = voice.VoiceInput(lambda text: None, lambda state, message: None)
    assert controller.max_seconds <= 55


class _FakeRecognizer:
    def __init__(self, supported: bool) -> None:
        self._supported = supported

    def supportsOnDeviceRecognition(self) -> bool:
        return self._supported


class _FakeRequest:
    def __init__(self) -> None:
        self.required = False

    def setRequiresOnDeviceRecognition_(self, value: bool) -> None:
        self.required = value


def test_local_only_voice_requires_on_device_support() -> None:
    request = _FakeRequest()
    with pytest.raises(voice.VoiceInputError, match="cihaz üzerinde"):
        voice.configure_recognition_policy(_FakeRecognizer(False), request, require_on_device=True)
    assert request.required is False


def test_local_only_voice_sets_request_to_on_device() -> None:
    request = _FakeRequest()
    voice.configure_recognition_policy(_FakeRecognizer(True), request, require_on_device=True)
    assert request.required is True
