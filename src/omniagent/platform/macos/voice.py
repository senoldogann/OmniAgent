from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Tuple


StateCallback = Callable[[str, str], None]
TextCallback = Callable[[str], None]

# PyObjC'nin bazı sürümlerinde method sonucu (başarılı, NSError) şeklinde,
# bazı sürümlerinde yalnızca NSError veya None döner.
_AudioResources = Tuple[Any, Any, Any, Any, Any, Any]


class VoiceInputError(RuntimeError):
    """Sesli giriş başlatılamadığında kullanıcıya gösterilebilir hata."""


def voice_privacy_preflight(bundle_info: Mapping[str, object]) -> None:
    """Speech/mikrofon izin API'lerine girmeden gerekli macOS privacy metadatasını doğrular."""
    for key in ("NSSpeechRecognitionUsageDescription", "NSMicrophoneUsageDescription"):
        value = bundle_info.get(key)
        if not isinstance(value, str) or not value.strip():
            raise VoiceInputError(
                f"Sesli giriş devre dışı: Info.plist içinde {key} açıklaması gerekli."
            )


def configure_recognition_policy(
    recognizer: Any, request: Any, *, require_on_device: bool,
) -> None:
    """Local-only modda ağ fallback'ine izin vermeden Speech request'ini yapılandırır."""
    if not require_on_device:
        return
    support = getattr(recognizer, "supportsOnDeviceRecognition", None)
    try:
        supported = bool(support() if callable(support) else support)
    except Exception:
        supported = False
    if not supported:
        raise VoiceInputError(
            "Bu dil için cihaz üzerinde konuşma tanıma desteklenmiyor; gizlilik için ağ fallback'i kapalı."
        )
    setter = getattr(request, "setRequiresOnDeviceRecognition_", None)
    if not callable(setter):
        raise VoiceInputError("Speech on-device zorunluluğu bu macOS/PyObjC sürümünde kullanılamıyor.")
    setter(True)


class VoiceInput:
    """Tek mikrofon oturumunu canlı Speech buffer akışıyla yönetir."""

    def __init__(
        self,
        on_text: TextCallback,
        on_state: StateCallback,
        locale: str = "tr-TR",
        max_seconds: int = 55,
        on_partial: Optional[TextCallback] = None,
        require_on_device: bool = True,
    ) -> None:
        self._on_text = on_text
        self._on_state = on_state
        self._on_partial = on_partial
        self._locale = locale
        self._max_seconds = max(5, int(max_seconds))
        self._require_on_device = bool(require_on_device)
        self._lock = threading.RLock()
        self._audio_session: Any = None
        self._engine: Any = None
        self._input_node: Any = None
        self._request: Any = None
        self._recognizer: Any = None
        self._task: Any = None
        # Eski çağırma noktalarının test/temizlik uyumluluğu için tutulur; yeni akış
        # canlı olduğu için geçici ses dosyası oluşturmaz.
        self._path: Optional[Path] = None
        self._timer: Optional[threading.Timer] = None
        self._recording = False
        self._starting = False
        self._transcribing = False
        self._generation = 0
        self._last_partial = ""

    @property
    def active(self) -> bool:
        """Mikrofon veya Speech oturumu açık mı?"""
        with self._lock:
            return self._recording or self._starting or self._transcribing

    @property
    def recording(self) -> bool:
        with self._lock:
            return self._recording

    @property
    def max_seconds(self) -> int:
        return self._max_seconds

    def start(self) -> None:
        """İzinleri isteyip canlı mikrofon buffer akışını başlatır."""
        if sys.platform != "darwin":
            raise VoiceInputError("Sesli giriş yalnızca macOS'ta kullanılabilir.")
        with self._lock:
            if self.active:
                return
            self._starting = True
            self._transcribing = False
            self._last_partial = ""
            self._generation += 1
            generation = self._generation
        self._notify("requesting", "Mikrofon ve konuşma tanıma izni isteniyor")
        try:
            import Speech  # type: ignore[import-not-found]
        except ImportError as error:
            with self._lock:
                self._starting = False
            raise VoiceInputError(
                "Sesli giriş bağımlılıkları kurulu değil; pyobjc AVFoundation/Speech paketlerini kontrol et."
            ) from error
        try:
            from Foundation import NSBundle  # type: ignore[import-not-found]
            voice_privacy_preflight(dict(NSBundle.mainBundle().infoDictionary() or {}))
            self._ensure_speech_permission(Speech, self._after_speech_permission, generation)
        except ImportError as error:
            with self._lock:
                self._starting = False
            message = "Sesli giriş bağımlılıkları kurulu değil; pyobjc AVFoundation/Speech paketlerini kontrol et."
            self._notify("error", message)
            raise VoiceInputError(message) from error
        except VoiceInputError as error:
            with self._lock:
                self._starting = False
            self._notify("error", str(error))
            raise

    def stop(self) -> None:
        """Canlı buffer akışını durdurur; Speech'in sonucunu beklemeye devam eder."""
        with self._lock:
            if not self._recording:
                if self._starting:
                    self._generation += 1
                    self._starting = False
                return
            self._recording = False
            self._transcribing = True
            self._cancel_timer_locked()
            generation = self._generation
            resources = self._detach_capture_locked()
        self._release_capture(*resources, end_request=True, cancel_task=False, deactivate=True)
        timer = threading.Timer(
            10.0, lambda: self._finish_error("Ses tanıma zaman aşımına uğradı", generation)
        )
        timer.daemon = True
        with self._lock:
            if generation == self._generation and self._transcribing:
                self._timer = timer
        timer.start()
        self._notify("transcribing", "Ses yazıya çevriliyor")

    def cancel(self) -> None:
        """Canlı kaydı ve tanımayı iptal eder; tüm kaynakları serbest bırakır."""
        with self._lock:
            self._generation += 1
            self._starting = False
            self._recording = False
            self._transcribing = False
            self._last_partial = ""
            self._cancel_timer_locked()
            resources = self._resources_locked()
            path = self._path
            self._path = None
        self._release_capture(*resources, end_request=True, cancel_task=True, deactivate=True)
        if path is not None:
            path.unlink(missing_ok=True)
        self._notify("idle", "")

    # İzinler ---------------------------------------------------------------

    def _ensure_speech_permission(
        self, speech: Any, done: Callable[[], None], generation: int
    ) -> None:
        status = int(speech.SFSpeechRecognizer.authorizationStatus())
        if status == speech.SFSpeechRecognizerAuthorizationStatusAuthorized:
            done()
            return
        if status in (
            speech.SFSpeechRecognizerAuthorizationStatusDenied,
            speech.SFSpeechRecognizerAuthorizationStatusRestricted,
        ):
            raise VoiceInputError("Konuşma tanıma izni verilmedi.")
        speech.SFSpeechRecognizer.requestAuthorization_(
            lambda value: self._speech_permission_callback(value, done, generation)
        )

    def _speech_permission_callback(
        self, value: int, done: Callable[[], None], generation: int
    ) -> None:
        with self._lock:
            if generation != self._generation or not self._starting:
                return
        if int(value) != 3:
            self._permission_failed(generation, "Konuşma tanıma izni verilmedi.")
            return
        try:
            import AVFoundation as AV  # type: ignore[import-not-found]
            self._ensure_microphone_permission(AV, done, generation)
        except Exception as error:
            self._permission_failed(generation, f"Mikrofon izni hatası: {error}")

    def _ensure_microphone_permission(
        self, av: Any, done: Callable[[], None], generation: int
    ) -> None:
        session = av.AVAudioSession.sharedInstance()
        status = session.recordPermission()
        if status == av.AVAudioSessionRecordPermissionGranted:
            done()
            return
        if status == av.AVAudioSessionRecordPermissionDenied:
            raise VoiceInputError("Mikrofon izni verilmedi.")
        session.requestRecordPermission_(
            lambda granted: done() if granted else self._permission_failed(
                generation, "Mikrofon izni verilmedi."
            )
        )

    def _permission_failed(self, generation: int, message: str) -> None:
        with self._lock:
            if generation != self._generation:
                return
            self._starting = False
        self._notify("error", message)

    # Canlı kayıt ve Speech akışı -------------------------------------------

    def _after_speech_permission(self) -> None:
        """AVAudioEngine input buffer'larını Speech isteğine anında aktarır."""
        try:
            import AVFoundation as AV  # type: ignore[import-not-found]
            import Speech  # type: ignore[import-not-found]
            from Foundation import NSLocale  # type: ignore[import-not-found]
        except ImportError:
            with self._lock:
                if not self._starting:
                    return
                self._starting = False
            self._notify("error", "Sesli giriş bağımlılıkları kurulu değil.")
            return

        with self._lock:
            if not self._starting:
                return
            generation = self._generation
            session = AV.AVAudioSession.sharedInstance()
            self._audio_session = session
        try:
            result = session.setCategory_error_(AV.AVAudioSessionCategoryRecord, None)
            self._raise_result_error(result, "Mikrofon kategorisi ayarlanamadı")
            result = session.setActive_error_(True, None)
            self._raise_result_error(result, "Mikrofon etkinleştirilemedi")

            recognizer = Speech.SFSpeechRecognizer.alloc().initWithLocale_(
                NSLocale.localeWithLocaleIdentifier_(self._locale)
            )
            if recognizer is None or not recognizer.isAvailable():
                raise VoiceInputError("Türkçe Speech tanıyıcı şu anda kullanılamıyor.")

            request = Speech.SFSpeechAudioBufferRecognitionRequest.alloc().init()
            request.setShouldReportPartialResults_(True)
            configure_recognition_policy(
                recognizer, request, require_on_device=self._require_on_device,
            )

            engine = AV.AVAudioEngine.alloc().init()
            with self._lock:
                stale = generation != self._generation or not self._starting
                if not stale:
                    # Kaynaklar kurulum tamamlanmadan önce de kaydedilir; prepare
                    # veya tap hatasında da _finish_error bunları temizleyebilsin.
                    self._recognizer = recognizer
                    self._request = request
                    self._engine = engine
                    self._transcribing = True
            if stale:
                self._release_capture(
                    engine, None, request, recognizer, None, session,
                    end_request=True, cancel_task=False, deactivate=True,
                )
                return

            input_node = engine.inputNode()
            with self._lock:
                stale = generation != self._generation or not self._starting
                if not stale:
                    self._input_node = input_node
            if stale:
                self._release_capture(
                    engine, input_node, request, recognizer, None, session,
                    end_request=True, cancel_task=False, deactivate=True,
                )
                return
            audio_format = input_node.outputFormatForBus_(0)
            if audio_format is None or self._audio_sample_rate(audio_format) <= 0:
                raise VoiceInputError("Mikrofon ses girişi bulunamadı.")

            def append_buffer(buffer: Any, _when: Any) -> None:
                with self._lock:
                    valid = generation == self._generation and self._request is request
                    active_request = self._request if valid else None
                if active_request is not None and buffer is not None:
                    try:
                        active_request.appendAudioPCMBuffer_(buffer)
                    except Exception:
                        # Buffer callback'leri tanıma iptalinden sonra da gecikmeli
                        # gelebilir; ses oturumunu bu callback bozmasın.
                        pass

            input_node.installTapOnBus_bufferSize_format_block_(
                0, 1024, audio_format, append_buffer
            )
            engine.prepare()
            with self._lock:
                stale = generation != self._generation or not self._starting
            if stale:
                self._release_capture(
                    engine, input_node, request, recognizer, None, session,
                    end_request=True, cancel_task=False, deactivate=True,
                )
                return

            task = recognizer.recognitionTaskWithRequest_resultHandler_(
                request,
                lambda result, error: self._recognition_finished(
                    result, error, generation
                ),
            )
            with self._lock:
                stale = generation != self._generation or not self._transcribing
                if not stale:
                    self._task = task
            if stale:
                try:
                    task.cancel()
                except Exception:
                    pass
                return

            result = engine.startAndReturnError_(None)
            self._raise_result_error(result, "Mikrofon akışı başlatılamadı")
            with self._lock:
                if generation != self._generation or not self._transcribing:
                    return
                self._starting = False
                self._recording = True
                self._timer = threading.Timer(self._max_seconds, self._recording_timeout)
                self._timer.daemon = True
                self._timer.start()
            self._notify("recording", "Dinleniyor; konuşurken metin canlı yazılır")
        except Exception as error:
            if isinstance(error, VoiceInputError):
                message = str(error)
            else:
                message = f"Kayıt başlatılamadı: {error}"
            self._finish_error(message, generation)

    @staticmethod
    def _audio_sample_rate(audio_format: Any) -> float:
        """PyObjC'nin selector/property biçimlerinin ikisini de destekler."""
        selector = getattr(audio_format, "sampleRate", None)
        if selector is None:
            return 0.0
        try:
            return float(selector())
        except Exception:
            try:
                return float(selector)
            except Exception:
                return 0.0

    @staticmethod
    def _raise_result_error(result: Any, message: str) -> None:
        if isinstance(result, tuple) and len(result) > 1 and result[1] is not None:
            raise VoiceInputError(f"{message}: {result[1]}")

    def _recording_timeout(self) -> None:
        if self.recording:
            self._notify("recording_timeout", "En uzun kayıt süresi doldu")
            self.stop()

    def _detach_capture_locked(self) -> _AudioResources:
        """Capture dururken task/recognizer'ı final callback'i için saklar."""
        resources: _AudioResources = (
            self._engine,
            self._input_node,
            self._request,
            self._recognizer,
            self._task,
            self._audio_session,
        )
        self._engine = None
        self._input_node = None
        self._request = None
        self._audio_session = None
        return resources

    def _resources_locked(self) -> _AudioResources:
        resources: _AudioResources = (
            self._engine,
            self._input_node,
            self._request,
            self._recognizer,
            self._task,
            self._audio_session,
        )
        self._engine = None
        self._input_node = None
        self._request = None
        self._recognizer = None
        self._task = None
        self._audio_session = None
        return resources

    @staticmethod
    def _release_capture(
        engine: Any,
        input_node: Any,
        request: Any,
        _recognizer: Any,
        task: Any,
        session: Any,
        *,
        end_request: bool,
        cancel_task: bool,
        deactivate: bool,
    ) -> None:
        """PyObjC nesnelerini sırayla, tek bir hata türü dışarı taşmadan serbest bırakır."""
        if request is not None and end_request:
            try:
                request.endAudio()
            except Exception:
                pass
        if input_node is not None:
            try:
                input_node.removeTapOnBus_(0)
            except Exception:
                pass
        if engine is not None:
            try:
                engine.stop()
            except Exception:
                pass
        if task is not None and cancel_task:
            try:
                task.cancel()
            except Exception:
                pass
        if session is not None and deactivate:
            try:
                session.setActive_error_(False, None)
            except Exception:
                pass

    # Tanıma ---------------------------------------------------------------

    def _emit_partial(self, text: str) -> None:
        """Ara sonucu UI kuyruğuna aktarır; callback hatası tanımayı kesmez."""
        if self._on_partial is None:
            return
        try:
            self._on_partial(text)
        except Exception:
            pass

    def _recognition_finished(self, result: Any, error: Any, generation: int) -> None:
        with self._lock:
            if generation != self._generation or not self._transcribing:
                return
        if error is not None:
            self._finish_error(f"Ses tanıma başarısız: {error}", generation)
            return
        text = ""
        is_final = False
        if result is not None:
            transcription = result.bestTranscription()
            if transcription is not None:
                text = str(transcription.formattedString()).strip()
            is_final = bool(result.isFinal())
            if text != self._last_partial:
                self._last_partial = text
                self._emit_partial(text)
        if is_final:
            if text:
                try:
                    self._on_text(text)
                except Exception as callback_error:
                    self._finish_error(f"Sesli metin aktarılamadı: {callback_error}", generation)
                    return
            self._finish_success(generation)

    def _finish_success(self, generation: int) -> None:
        with self._lock:
            if generation != self._generation or not self._transcribing:
                return
            self._starting = False
            self._recording = False
            self._transcribing = False
            self._last_partial = ""
            self._cancel_timer_locked()
            resources = self._resources_locked()
            path = self._path
            self._path = None
        self._release_capture(*resources, end_request=True, cancel_task=True, deactivate=True)
        if path is not None:
            path.unlink(missing_ok=True)
        self._notify("ready", "Ses yazıya çevrildi")

    def _finish_error(self, message: str, generation: int) -> None:
        with self._lock:
            if generation != self._generation:
                return
            self._starting = False
            self._recording = False
            self._transcribing = False
            self._last_partial = ""
            self._cancel_timer_locked()
            resources = self._resources_locked()
            path = self._path
            self._path = None
        self._release_capture(*resources, end_request=True, cancel_task=True, deactivate=True)
        if path is not None:
            path.unlink(missing_ok=True)
        self._notify("error", message)

    def _cancel_timer_locked(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _notify(self, state: str, message: str) -> None:
        try:
            self._on_state(state, message)
        except Exception:
            # UI callback'i ses motorunu durdurmamalı.
            pass
