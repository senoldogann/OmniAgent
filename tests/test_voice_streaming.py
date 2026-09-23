"""Sesli girişin canlı buffer akışını ve temizlik sınırlarını doğrulayan testler."""
from __future__ import annotations

import sys
from typing import Any

import voice


class FakeFormat:
    def sampleRate(self) -> float:
        return 44100.0


class FakeRequest:
    def __init__(self) -> None:
        self.buffers: list[object] = []
        self.partial = False
        self.ended = False

    def setShouldReportPartialResults_(self, value: bool) -> None:
        self.partial = value

    def setRequiresOnDeviceRecognition_(self, value: bool) -> None:
        del value

    def appendAudioPCMBuffer_(self, buffer: object) -> None:
        self.buffers.append(buffer)

    def endAudio(self) -> None:
        self.ended = True


class FakeTask:
    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class FakeInputNode:
    def __init__(self) -> None:
        self.format = FakeFormat()
        self.tap: Any = None
        self.tap_removed = False

    def outputFormatForBus_(self, _bus: int) -> FakeFormat:
        return self.format

    def installTapOnBus_bufferSize_format_block_(
        self, bus: int, buffer_size: int, audio_format: FakeFormat, block: Any
    ) -> None:
        assert (bus, buffer_size) == (0, 1024)
        assert audio_format is self.format
        self.tap = block

    def removeTapOnBus_(self, _bus: int) -> None:
        self.tap_removed = True


class FakeEngine:
    def __init__(self) -> None:
        self.node = FakeInputNode()
        self.prepared = False
        self.started = False
        self.stopped = False

    def inputNode(self) -> FakeInputNode:
        return self.node

    def prepare(self) -> None:
        self.prepared = True

    def startAndReturnError_(self, _error: object) -> tuple[bool, None]:
        self.started = True
        return True, None

    def stop(self) -> None:
        self.stopped = True


class FakeRecognizer:
    def __init__(self) -> None:
        self.callback: Any = None
        self.task = FakeTask()

    def isAvailable(self) -> bool:
        return True

    def supportsOnDeviceRecognition(self) -> bool:
        return False

    def recognitionTaskWithRequest_resultHandler_(self, _request: object, callback: Any) -> FakeTask:
        self.callback = callback
        return self.task


class FakeResult:
    def __init__(self, text: str, final: bool) -> None:
        self.text = text
        self.final = final

    def bestTranscription(self) -> "FakeTranscription":
        return FakeTranscription(self.text)

    def isFinal(self) -> bool:
        return self.final


class FakeTranscription:
    def __init__(self, text: str) -> None:
        self.text = text

    def formattedString(self) -> str:
        return self.text


class FakeLocale:
    @staticmethod
    def localeWithLocaleIdentifier_(identifier: str) -> str:
        return identifier


class FakeFoundation:
    NSLocale = FakeLocale


class FakeSession:
    def __init__(self) -> None:
        self.active = False

    def setCategory_error_(self, _category: str, _error: object) -> tuple[bool, None]:
        return True, None

    def setActive_error_(self, value: bool, _error: object) -> tuple[bool, None]:
        self.active = value
        return True, None


class FakeAVAudioEngine:
    instance: "FakeEngine"

    @classmethod
    def alloc(cls) -> "FakeAVAudioEngine":
        return cls()

    def init(self) -> "FakeEngine":
        self.__class__.instance = FakeEngine()
        return self.__class__.instance


class FakeSpeechRequest:
    instance: FakeRequest

    @classmethod
    def alloc(cls) -> "FakeSpeechRequest":
        return cls()

    def init(self) -> FakeRequest:
        self.__class__.instance = FakeRequest()
        return self.__class__.instance


class FakeSpeechRecognizer:
    instance: FakeRecognizer

    @classmethod
    def alloc(cls) -> "FakeSpeechRecognizer":
        return cls()

    def initWithLocale_(self, _locale: str) -> "FakeRecognizer":
        self.__class__.instance = FakeRecognizer()
        return self.__class__.instance


class FakeSpeech:
    SFSpeechAudioBufferRecognitionRequest = FakeSpeechRequest
    SFSpeechRecognizer = FakeSpeechRecognizer


class FakeAV:
    AVAudioSessionCategoryRecord = "record"
    AVAudioEngine = FakeAVAudioEngine

    class AVAudioSession:
        session = FakeSession()

        @classmethod
        def sharedInstance(cls) -> "FakeSession":
            return cls.session


def test_live_buffer_partial_and_final(monkeypatch: Any) -> None:
    """Buffer callback'ı gerçekten canlı partial üretmeli, stop sonrası final gelmeli."""
    monkeypatch.setitem(sys.modules, "AVFoundation", FakeAV)
    monkeypatch.setitem(sys.modules, "Speech", FakeSpeech)
    monkeypatch.setitem(sys.modules, "Foundation", FakeFoundation)
    partials: list[str] = []
    finals: list[str] = []
    states: list[str] = []
    controller = voice.VoiceInput(
        finals.append,
        lambda state, _message: states.append(state),
        on_partial=partials.append,
        require_on_device=False,
    )
    controller._starting = True
    controller._generation = 1
    controller._after_speech_permission()
    assert controller.recording
    assert FakeAVAudioEngine.instance.started
    assert FakeSpeechRequest.instance.partial
    FakeAVAudioEngine.instance.node.tap(object(), 0.0)
    assert len(FakeSpeechRequest.instance.buffers) == 1
    callback = FakeSpeechRecognizer.instance.callback
    callback(FakeResult("merhaba", False), None)
    callback(FakeResult("merhaba dünya", True), None)
    assert partials == ["merhaba", "merhaba dünya"]
    assert finals == ["merhaba dünya"]
    assert states[-1] == "ready"
    assert not controller.active


def test_cancel_stops_live_engine_and_invalidates_callback(monkeypatch: Any) -> None:
    monkeypatch.setitem(sys.modules, "AVFoundation", FakeAV)
    monkeypatch.setitem(sys.modules, "Speech", FakeSpeech)
    monkeypatch.setitem(sys.modules, "Foundation", FakeFoundation)
    finals: list[str] = []
    controller = voice.VoiceInput(
        finals.append, lambda _state, _message: None, require_on_device=False,
    )
    controller._starting = True
    controller._generation = 1
    controller._after_speech_permission()
    callback = FakeSpeechRecognizer.instance.callback
    controller.cancel()
    callback(FakeResult("geç gelen sonuç", True), None)
    assert finals == []
    assert FakeSpeechRequest.instance.ended
    assert FakeAVAudioEngine.instance.stopped
    assert not controller.active
