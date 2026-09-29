"""Model erişim hatasında aynı profilin her turda yeniden denenmesini önler; bekleme ve durdurma davranışı."""
import json
import threading
import time
from pathlib import Path
from typing import Any, Optional

import httpx2
import pytest
from openai import APIConnectionError, APITimeoutError
from PIL import Image

from omniagent.app import agent as main
from omniagent.app.model_retry import ModelCallFailed
from omniagent.fallback_policy import FallbackAuditFailed, FallbackNotPermitted, count_image_parts
from omniagent.integrations.runtime import CURRENT_RUNTIME, IntegrationRuntime
from tests.test_model_retry import status_error


@pytest.mark.asyncio
async def test_access_failure_is_quarantined_for_rest_of_task(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    attempts: list[str] = []

    async def fake_stream(
        client: Any, profile: Any, messages: Any, schemas: Any, session_id: str,
        emit: Any, should_stop: Any,
    ) -> Any:
        attempts.append(profile["provider"])
        if profile["provider"] == "ollama-cloud":
            raise status_error(402, {"message": "kota doldu"}, {})
        return {
            "content": "tamam", "tool_calls": [], "finish_reason": "stop",
            "usage": main.ZERO_USAGE,
        }

    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(main, "_stream_completion", fake_stream)
    runtime = IntegrationRuntime(lambda event: None, lambda: False)
    runtime.fallback_backends = frozenset({"openai"})
    token = CURRENT_RUNTIME.set(runtime)
    try:
        clients = {"ollama-cloud": object(), "openai": None}
        first, used = await main._call_model_with_retries(
            clients, [], [], "oturum", "ollama-cloud", lambda event: None, lambda: False,
        )
        assert first["content"] == "tamam" and used == "openai"
        assert runtime.blocked_backends == {"ollama-cloud"}
        second, used = await main._call_model_with_retries(
            clients, [], [], "oturum", "openai", lambda event: None, lambda: False,
        )
        assert second["content"] == "tamam" and used == "openai"
        assert attempts == ["ollama-cloud", "openai", "openai"]
    finally:
        CURRENT_RUNTIME.reset(token)


@pytest.mark.asyncio
async def test_runner_persists_access_fallback_without_restarting_original(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    target = tmp_path / "data.txt"
    target.write_text("gözlem", encoding="utf-8")
    requested: list[str] = []

    async def fake_model(
        clients: Any, messages: Any, schemas: Any, session_id: str,
        backend: str, emit: Any, should_stop: Any,
    ) -> Any:
        requested.append(backend)
        if len(requested) == 1:
            runtime = CURRENT_RUNTIME.get()
            assert runtime is not None
            runtime.blocked_backends.add("ollama-cloud")
            return {
                "content": "STATE: dosya okunacak",
                "tool_calls": [{"id": "one", "name": "read_file",
                                "arguments": json.dumps({"path": str(target)})}],
                "finish_reason": "tool_calls", "usage": main.ZERO_USAGE,
            }, "openai"
        return {
            "content": "gözlem okundu", "tool_calls": [], "finish_reason": "stop",
            "usage": main.ZERO_USAGE,
        }, "openai"

    monkeypatch.setattr(main, "_call_model_with_retries", fake_model)
    events: list[Any] = []
    report = await main.run_agent_with_callback(
        "Dosyayı oku", events.append,
        {"requested_backend": "ollama-cloud", "should_stop": lambda: False,
         "state_file": str(tmp_path / "state.json"), "history": []},
        {"ollama-cloud": object(), "openai": None},
    )
    assert report["success"]
    assert requested == ["ollama-cloud", "openai"]
    assert report["metrics"]["backend"] == "openai"
    changed = [event for event in events if event["kind"] == "backend_changed"]
    assert changed and "yeniden denenmeyecek" in changed[0]["reason"]


@pytest.mark.asyncio
async def test_retry_after_uses_other_provider_without_sleep(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """429'da alternatif varken beklenmez; profil Retry-After kadar serinler ve sonraki çağrıda atlanır."""
    attempts: list[str] = []

    async def fake_stream(
        client: Any, profile: Any, messages: Any, schemas: Any, session_id: str,
        emit: Any, should_stop: Any,
    ) -> Any:
        attempts.append(profile["provider"])
        if profile["provider"] == "ollama-cloud":
            raise status_error(429, {"message": "bekle"}, {"retry-after": "120"})
        return {"content": "tamam", "tool_calls": [], "finish_reason": "stop",
                "usage": main.ZERO_USAGE}

    async def fail_sleep(seconds: float) -> None:
        raise AssertionError(f"Gereksiz bekleme: {seconds}")

    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(main, "_stream_completion", fake_stream)
    monkeypatch.setattr(main.asyncio, "sleep", fail_sleep)
    runtime = IntegrationRuntime(lambda event: None, lambda: False)
    runtime.fallback_backends = frozenset({"openai"})
    token = CURRENT_RUNTIME.set(runtime)
    try:
        clients = {"ollama-cloud": object(), "openai": None}
        events: list[Any] = []
        turn, used = await main._call_model_with_retries(
            clients, [], [], "oturum", "ollama-cloud", events.append, lambda: False,
        )
        assert turn["content"] == "tamam" and used == "openai"
        assert attempts == ["ollama-cloud", "openai"]
        # Hız sınırı erişim hatası değildir: görev boyu engel yok, Retry-After kadar serinleme var.
        assert runtime.blocked_backends == set()
        assert 119.0 < runtime.backend_cooldowns["ollama-cloud"] - time.monotonic() <= 121.0
        second, used = await main._call_model_with_retries(
            clients, [], [], "oturum", "ollama-cloud", events.append, lambda: False,
        )
        assert second["content"] == "tamam" and used == "openai"
        assert attempts == ["ollama-cloud", "openai", "openai"]
        # İkinci çağrı doğrudan yedekle başlar (birincil serinlemede); ama aynı (hedef, görüntü seviyesi) çifti
        # görev boyunca bir kez duyurulur ve kaydedilir.
        switches = [event for event in events if event["kind"] == "provider_fallback"]
        assert len(switches) == 1 and switches[0]["from_backend"] == "ollama-cloud"
        assert "hız sınırı (HTTP 429)" in switches[0]["reason"]
        assert len((tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()) == 1
    finally:
        CURRENT_RUNTIME.reset(token)


@pytest.mark.asyncio
async def test_two_blocked_providers_reach_third_ladder_step(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Erişim hatası alan iki sağlayıcı karantinaya alınır, merdivenin üçüncü basamağı işi bitirir."""
    attempts: list[str] = []

    async def fake_stream(
        client: Any, profile: Any, messages: Any, schemas: Any, session_id: str,
        emit: Any, should_stop: Any,
    ) -> Any:
        attempts.append(profile["provider"])
        if profile["provider"] in ("ollama-cloud", "openai"):
            raise status_error(402, {"message": "kota doldu"}, {})
        return {"content": "üçüncü profil çalıştı", "tool_calls": [],
                "finish_reason": "stop", "usage": main.ZERO_USAGE}

    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(main, "_stream_completion", fake_stream)
    runtime = IntegrationRuntime(lambda event: None, lambda: False)
    runtime.fallback_backends = frozenset({"openai", "openrouter"})
    token = CURRENT_RUNTIME.set(runtime)
    try:
        turn, used = await main._call_model_with_retries(
            {"ollama-cloud": object(), "openai": object(), "openrouter": object()}, [], [],
            "oturum", "ollama-cloud", lambda event: None, lambda: False,
        )
        assert turn["content"] == "üçüncü profil çalıştı"
        assert used == "openrouter"
        assert attempts == ["ollama-cloud", "openai", "openrouter"]
        assert runtime.blocked_backends == {"ollama-cloud", "openai"}
    finally:
        CURRENT_RUNTIME.reset(token)


@pytest.mark.asyncio
async def test_stop_during_backoff_returns_stopped_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bekleme sırasında Durdur istisna değil 'stopped' turu döndürür ve beklemeyi hemen keser."""
    stop = threading.Event()

    async def fake_stream(
        client: Any, profile: Any, messages: Any, schemas: Any, session_id: str,
        emit: Any, should_stop: Any,
    ) -> Any:
        raise status_error(429, {"message": "bekle"}, {"retry-after": "30"})

    monkeypatch.setattr(main, "_stream_completion", fake_stream)
    runtime = IntegrationRuntime(lambda event: None, stop.is_set)
    token = CURRENT_RUNTIME.set(runtime)
    timer = threading.Timer(0.2, stop.set)
    timer.start()
    started = time.monotonic()
    try:
        turn, used = await main._call_model_with_retries(
            {"opencode": object()}, [], [], "oturum", "opencode", lambda event: None, stop.is_set,
        )
    finally:
        timer.cancel()
        CURRENT_RUNTIME.reset(token)
    assert turn["finish_reason"] == "stopped" and used == "opencode"
    assert time.monotonic() - started < 5 and runtime.metrics["wait_seconds"] > 0


@pytest.mark.asyncio
async def test_retry_budget_exhaustion_raises_model_call_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kesinti bütçeyi aşınca ModelCallFailed yükselir: son hata zincirde, bağlam iletide."""

    async def fake_stream(
        client: Any, profile: Any, messages: Any, schemas: Any, session_id: str,
        emit: Any, should_stop: Any,
    ) -> Any:
        raise status_error(503, {"message": "kapalı"}, {})

    monkeypatch.setattr(main, "_stream_completion", fake_stream)
    runtime = IntegrationRuntime(lambda event: None, lambda: False)
    runtime.model_retry_until = time.monotonic() + 1.0
    token = CURRENT_RUNTIME.set(runtime)
    try:
        with pytest.raises(ModelCallFailed) as failure:
            await main._call_model_with_retries(
                {"opencode": object()}, [], [], "oturum", "opencode", lambda event: None, lambda: False,
            )
    finally:
        CURRENT_RUNTIME.reset(token)
    assert failure.value.kind == "transient" and failure.value.attempts == 2
    assert 0.0 < failure.value.waited_seconds < 1.0 and "durum 503" in str(failure.value)
    assert getattr(failure.value.__cause__, "status_code", None) == 503


@pytest.mark.asyncio
async def test_retry_after_beyond_budget_fails_without_sleeping(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sunucu bütçeden uzun bekleme isterse hiç beklenmeden açık hata verilir."""

    async def fake_stream(
        client: Any, profile: Any, messages: Any, schemas: Any, session_id: str,
        emit: Any, should_stop: Any,
    ) -> Any:
        raise status_error(429, {"message": "bekle"}, {"retry-after": "120"})

    async def fail_sleep(seconds: float) -> None:
        raise AssertionError(f"Gereksiz bekleme: {seconds}")

    monkeypatch.setattr(main, "_stream_completion", fake_stream)
    monkeypatch.setattr(main.asyncio, "sleep", fail_sleep)
    with pytest.raises(ModelCallFailed) as failure:
        await main._call_model_with_retries(
            {"opencode": object()}, [], [], "oturum", "opencode", lambda event: None, lambda: False,
        )
    assert (failure.value.kind, failure.value.attempts, failure.value.waited_seconds) == ("rate_limited", 1, 0.0)
    assert "2 dk sonra yeniden denenmesini istedi" in str(failure.value)


@pytest.mark.asyncio
async def test_timeout_retries_same_provider_when_no_alternative(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tek sağlayıcıda zaman aşımı görevi bitirmez: aynı profil bir kez daha denenir."""
    attempts: list[str] = []

    async def flaky(
        client: Any, profile: Any, messages: Any, schemas: Any, session_id: str,
        emit: Any, should_stop: Any,
    ) -> Any:
        attempts.append(profile["provider"])
        if len(attempts) == 1:
            raise APITimeoutError(request=httpx2.Request("POST", "http://127.0.0.1/v1/chat/completions"))
        return {"content": "tamam", "tool_calls": [], "finish_reason": "stop", "usage": main.ZERO_USAGE}

    monkeypatch.setattr(main, "_stream_completion", flaky)
    turn, used = await main._call_model_with_retries(
        {"opencode": object()}, [], [], "oturum", "opencode", lambda event: None, lambda: False,
    )
    assert turn["content"] == "tamam" and used == "opencode" and attempts == ["opencode", "opencode"]


@pytest.mark.asyncio
async def test_client_error_is_not_retried_or_switched(monkeypatch: pytest.MonkeyPatch) -> None:
    """400 gibi kalıcı istemci hatası ne yeniden denenir ne de alternatif sağlayıcıya taşınır."""
    attempts: list[str] = []

    async def fake_stream(
        client: Any, profile: Any, messages: Any, schemas: Any, session_id: str,
        emit: Any, should_stop: Any,
    ) -> Any:
        attempts.append(profile["provider"])
        raise status_error(400, {"message": "geçersiz istek", "type": "invalid_request_error"}, {})

    monkeypatch.setattr(main, "_stream_completion", fake_stream)
    runtime = IntegrationRuntime(lambda event: None, lambda: False)
    runtime.fallback_backends = frozenset({"openai"})
    token = CURRENT_RUNTIME.set(runtime)
    try:
        with pytest.raises(ModelCallFailed) as failure:
            await main._call_model_with_retries(
                {"ollama-cloud": object(), "openai": object()}, [], [], "oturum", "ollama-cloud",
                lambda event: None, lambda: False,
            )
    finally:
        CURRENT_RUNTIME.reset(token)
    assert failure.value.kind == "permanent" and attempts == ["ollama-cloud"]


def text_turn() -> dict[str, Any]:
    return {"content": "tamam", "tool_calls": [], "finish_reason": "stop", "usage": main.ZERO_USAGE}


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [
    pytest.param(status_error(429, {"message": "bekle"}, {"retry-after": "120"}), id="429"),
    pytest.param(status_error(402, {"message": "kota doldu"}, {}), id="402"),
    pytest.param(APIConnectionError(request=httpx2.Request("POST", "http://127.0.0.1/x")), id="bağlantı hatası"),
])
async def test_default_runtime_never_leaves_selected_provider(
    monkeypatch: pytest.MonkeyPatch, error: Exception,
) -> None:
    """Yedek izni verilmemiş (varsayılan) görevde hiçbir hata sağlayıcı geçişi doğurmaz; hata açık yükselir."""
    attempts: list[str] = []

    async def fake_stream(
        client: Any, profile: Any, messages: Any, schemas: Any, session_id: str,
        emit: Any, should_stop: Any,
    ) -> Any:
        attempts.append(profile["provider"])
        raise error

    monkeypatch.setattr(main, "_stream_completion", fake_stream)
    runtime = IntegrationRuntime(lambda event: None, lambda: False)
    # Bağlantı hatasında ilk geri çekilme bu bütçeyi aşar: test beklemeden biter.
    runtime.model_retry_until = time.monotonic() + 0.3
    token = CURRENT_RUNTIME.set(runtime)
    try:
        with pytest.raises(ModelCallFailed) as failure:
            await main._call_model_with_retries(
                {"ollama-cloud": object(), "openai": object()}, [], [], "oturum", "ollama-cloud",
                lambda event: None, lambda: False,
            )
    finally:
        CURRENT_RUNTIME.reset(token)
    assert set(attempts) == {"ollama-cloud"} and runtime.blocked_backends == set()
    assert "OMNI_FALLBACK_BACKENDS" in str(failure.value)


@pytest.mark.asyncio
async def test_switch_is_announced_and_audited_before_second_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Sağlayıcı değişimi, veri ikinci sağlayıcıya GİTMEDEN önce olay ve denetim kaydı olarak görünür."""
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    events: list[Any] = []
    state_at_second_request: list[tuple[int, int]] = []

    async def fake_stream(
        client: Any, profile: Any, messages: Any, schemas: Any, session_id: str,
        emit: Any, should_stop: Any,
    ) -> Any:
        if profile["provider"] == "ollama-cloud":
            raise status_error(402, {"message": "kota doldu"}, {})
        audit = tmp_path / "audit.jsonl"
        state_at_second_request.append((
            len([event for event in events if event["kind"] == "provider_fallback"]),
            len(audit.read_text(encoding="utf-8").splitlines()) if audit.exists() else 0,
        ))
        return text_turn()

    monkeypatch.setattr(main, "_stream_completion", fake_stream)
    runtime = IntegrationRuntime(lambda event: None, lambda: False)
    runtime.fallback_backends = frozenset({"openai"})
    token = CURRENT_RUNTIME.set(runtime)
    try:
        await main._call_model_with_retries(
            {"ollama-cloud": object(), "openai": object()}, [], [], "oturum", "ollama-cloud",
            events.append, lambda: False,
        )
    finally:
        CURRENT_RUNTIME.reset(token)
    assert state_at_second_request == [(1, 1)]
    switch = next(event for event in events if event["kind"] == "provider_fallback")
    assert (switch["from_backend"], switch["to_backend"], switch["image_count"]) == ("ollama-cloud", "openai", 0)
    assert "HTTP 402" in switch["reason"]


@pytest.mark.asyncio
async def test_audit_failure_blocks_switch_and_request(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Denetim kaydı yazılamazsa (fail-closed) ikinci sağlayıcıya istek gönderilmez."""
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    (tmp_path / "audit.jsonl").mkdir()
    attempts: list[str] = []

    async def fake_stream(
        client: Any, profile: Any, messages: Any, schemas: Any, session_id: str,
        emit: Any, should_stop: Any,
    ) -> Any:
        attempts.append(profile["provider"])
        raise status_error(402, {"message": "kota doldu"}, {})

    monkeypatch.setattr(main, "_stream_completion", fake_stream)
    runtime = IntegrationRuntime(lambda event: None, lambda: False)
    runtime.fallback_backends = frozenset({"openai"})
    token = CURRENT_RUNTIME.set(runtime)
    try:
        with pytest.raises(FallbackAuditFailed, match="istek gönderilmedi"):
            await main._call_model_with_retries(
                {"ollama-cloud": object(), "openai": object()}, [], [], "oturum", "ollama-cloud",
                lambda event: None, lambda: False,
            )
    finally:
        CURRENT_RUNTIME.reset(token)
    assert attempts == ["ollama-cloud"]


@pytest.mark.asyncio
async def test_permanent_switch_does_not_leak_images_without_permission(monkeypatch: pytest.MonkeyPatch) -> None:
    """Birincil karantinadayken kalıcı yedeğe geçilmiş görevde sonradan gelen ekran görüntüsü izinsiz gitmez."""
    called: list[str] = []

    async def fake_stream(
        client: Any, profile: Any, messages: Any, schemas: Any, session_id: str,
        emit: Any, should_stop: Any,
    ) -> Any:
        called.append(profile["provider"])
        return text_turn()

    monkeypatch.setattr(main, "_stream_completion", fake_stream)
    runtime = IntegrationRuntime(lambda event: None, lambda: False)
    runtime.primary_backend = "ollama-cloud"
    runtime.fallback_backends = frozenset({"openai"})
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "ekrana bak"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,x"}},
    ]}]
    token = CURRENT_RUNTIME.set(runtime)
    try:
        with pytest.raises(FallbackNotPermitted, match="görüntülü yedek izni kapalı"):
            await main._call_model_with_retries(
                {"openai": object()}, messages, [], "oturum", "openai", lambda event: None, lambda: False,
            )
        runtime.fallback_images = True
        turn, used = await main._call_model_with_retries(
            {"openai": object()}, messages, [], "oturum", "openai", lambda event: None, lambda: False,
        )
    finally:
        CURRENT_RUNTIME.reset(token)
    assert used == "openai" and turn["content"] == "tamam" and called == ["openai"]


@pytest.mark.asyncio
async def test_run_reports_actionable_error_when_ready_fallback_is_not_permitted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Seçili sağlayıcı tükenince hazır ama izinsiz yedek, izni nasıl vereceğini söyleyen hatayla bildirilir."""
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    attempts: list[str] = []

    async def fake_stream(
        client: Any, profile: Any, messages: Any, schemas: Any, session_id: str,
        emit: Any, should_stop: Any,
    ) -> Any:
        attempts.append(profile["provider"])
        raise status_error(402, {"message": "kota doldu"}, {})

    monkeypatch.setattr(main, "_stream_completion", fake_stream)
    report = await main.run_agent_with_callback(
        "Merhaba de", lambda event: None,
        {"requested_backend": "ollama-cloud", "should_stop": lambda: False,
         "state_file": str(tmp_path / "state.json"), "history": []},
        {"ollama-cloud": object(), "openai": object()},
    )
    assert not report["success"] and report["reason"].startswith("model çağrısı başarısız")
    assert "OMNI_FALLBACK_BACKENDS" in report["outcome"] and attempts == ["ollama-cloud"]


@pytest.mark.asyncio
async def test_explicit_unavailable_backend_fails_without_permission(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Açıkça seçilen profil hazır değilse görev sessizce başka sağlayıcıya taşınmaz."""
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    events: list[Any] = []
    report = await main.run_agent_with_callback(
        "Merhaba de", events.append,
        {"requested_backend": "openai", "should_stop": lambda: False,
         "state_file": str(tmp_path / "state.json"), "history": []},
        {"ollama-cloud": object()},
    )
    assert not report["success"] and "hazır yedek sağlayıcılara (ollama-cloud) geçilmedi" in report["outcome"]
    assert not [event for event in events if event["kind"] in ("provider_fallback", "run_started")]
    assert not (tmp_path / "audit.jsonl").exists()


@pytest.mark.asyncio
async def test_explicit_unavailable_backend_is_replaced_only_with_permission_and_audited(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OMNI_FALLBACK_BACKENDS", "ollama-cloud")
    requested: list[str] = []
    events: list[Any] = []

    async def fake_model(
        clients: Any, messages: Any, schemas: Any, session_id: str, backend: str, emit: Any, should_stop: Any,
    ) -> Any:
        requested.append(backend)
        return text_turn(), backend

    monkeypatch.setattr(main, "_call_model_with_retries", fake_model)
    report = await main.run_agent_with_callback(
        "Merhaba de", events.append,
        {"requested_backend": "openai", "should_stop": lambda: False,
         "state_file": str(tmp_path / "state.json"), "history": []},
        {"ollama-cloud": object()},
    )
    assert report["success"] and requested == ["ollama-cloud"]
    assert events[0]["kind"] == "provider_fallback" and events[0]["reason"] == "seçili profil hazır değil"
    assert (events[0]["from_backend"], events[0]["to_backend"]) == ("openai", "ollama-cloud")
    assert [line for line in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
            if "provider_fallback" in line] != []


@pytest.mark.asyncio
async def test_startup_substitution_refuses_image_attachments_without_image_permission(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OMNI_FALLBACK_BACKENDS", "ollama-cloud")
    report = await main.run_agent_with_callback(
        "Bu görseli incele", lambda event: None,
        {"requested_backend": "openai", "should_stop": lambda: False,
         "state_file": str(tmp_path / "state.json"), "history": [], "images": [str(tmp_path / "ek.png")]},
        {"ollama-cloud": object()},
    )
    assert not report["success"] and "görsel ek" in report["outcome"]


@pytest.mark.asyncio
async def test_call_starting_with_fallback_because_primary_is_cooling_is_announced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Birincil serinlemedeyken çağrı doğrudan yedekle başlar; hata olmasa da veri yedeğe gidiyor: duyurulur ve kaydedilir."""
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    attempts: list[str] = []

    async def fake_stream(
        client: Any, profile: Any, messages: Any, schemas: Any, session_id: str,
        emit: Any, should_stop: Any,
    ) -> Any:
        attempts.append(profile["provider"])
        return text_turn()

    monkeypatch.setattr(main, "_stream_completion", fake_stream)
    runtime = IntegrationRuntime(lambda event: None, lambda: False)
    runtime.primary_backend = "ollama-cloud"
    runtime.fallback_backends = frozenset({"openai"})
    runtime.backend_cooldowns["ollama-cloud"] = time.monotonic() + 100.0
    events: list[Any] = []
    token = CURRENT_RUNTIME.set(runtime)
    try:
        await main._call_model_with_retries(
            {"ollama-cloud": object(), "openai": object()}, [], [], "oturum", "ollama-cloud",
            events.append, lambda: False,
        )
    finally:
        CURRENT_RUNTIME.reset(token)
    assert attempts == ["openai"]
    switch = next(event for event in events if event["kind"] == "provider_fallback")
    assert switch["from_backend"] == "ollama-cloud" and "kullanılamıyor (engelli, hız sınırında ya da hazır değil)" in switch["reason"]
    assert len((tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()) == 1


def photo(tmp_path: Path) -> str:
    """Telegram fotoğrafı gibi gerçek küçük bir görsel ek."""
    path = tmp_path / "foto.png"
    Image.new("RGB", (32, 32), (200, 10, 10)).save(path)
    return str(path)


async def run_auto(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clients: dict[str, Any], images: list[str],
) -> tuple[Any, list[Any], list[tuple[str, int]]]:
    """'Otomatik' seçimle (requested_backend=None) görevi çalıştırır; model çağrılarını (profil, görüntü sayısı) döner."""
    calls: list[tuple[str, int]] = []
    events: list[Any] = []

    async def fake_model(
        clients_: Any, messages: Any, schemas: Any, session_id: str, backend: str, emit: Any, should_stop: Any,
    ) -> Any:
        calls.append((backend, count_image_parts(messages)))
        return text_turn(), backend

    monkeypatch.setattr(main, "_call_model_with_retries", fake_model)
    report = await main.run_agent_with_callback(
        "bu fotoğrafa bak", events.append,
        {"requested_backend": None, "should_stop": lambda: False, "state_file": str(tmp_path / "state.json"),
         "history": [], "images": images},
        clients,
    )
    return report, events, calls


@pytest.mark.asyncio
async def test_auto_selection_is_the_default_profile_and_is_not_silently_replaced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Otomatik = varsayılan profil (ollama-cloud). Hazır değilse yalnız openai hazır diye veri OpenAI'a gitmez."""
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    report, events, calls = await run_auto(monkeypatch, tmp_path, {"openai": object()}, [])
    assert not report["success"] and "yedek sağlayıcı izni yok" in report["outcome"]
    assert "'ollama-cloud' profili hazır değil" in report["outcome"] and calls == []
    assert not [event for event in events if event["kind"] in ("provider_fallback", "run_started")]
    assert not (tmp_path / "audit.jsonl").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("backends,images,success,phrase", [
    ("none", None, False, "yedek sağlayıcı izni yok"),
    ("openai", None, False, "1 görsel ek"),
    ("openai", "0", False, "OMNI_FALLBACK_IMAGES=1"),
    ("openai", "1", True, ""),
])
async def test_auto_with_image_attachment_never_reaches_unpermitted_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, backends: str, images: Optional[str], success: bool, phrase: str,
) -> None:
    """Ollama kapalıyken yalnız openai hazırsa Otomatik + görsel ek: izin ve görüntü izni olmadan görüntü gitmez."""
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OMNI_FALLBACK_BACKENDS", backends)
    if images is None:
        monkeypatch.delenv("OMNI_FALLBACK_IMAGES", raising=False)
    else:
        monkeypatch.setenv("OMNI_FALLBACK_IMAGES", images)
    report, events, calls = await run_auto(monkeypatch, tmp_path, {"openai": object()}, [photo(tmp_path)])
    audit = tmp_path / "audit.jsonl"
    lines = audit.read_text(encoding="utf-8").splitlines() if audit.exists() else []
    switches = [event for event in events if event["kind"] == "provider_fallback"]
    assert report["success"] is success
    if success:
        assert calls == [("openai", 1)]
        assert len(switches) == 1 and switches[0]["image_count"] == 1 and switches[0]["from_backend"] == "ollama-cloud"
        assert len(lines) == 1 and "görüntü: 1" in lines[0]
    else:
        assert phrase in report["outcome"] and calls == [] and switches == [] and lines == []


@pytest.mark.asyncio
async def test_auto_uses_first_ready_and_permitted_ladder_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Merdivendeki ilk HAZIR profil izinsizse (openai) izinli sonraki hazır profil (openrouter) seçilir."""
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OMNI_FALLBACK_BACKENDS", "openrouter")
    report, events, calls = await run_auto(monkeypatch, tmp_path, {"openai": object(), "openrouter": object()}, [])
    assert report["success"] and calls == [("openrouter", 0)]
    assert [event["to_backend"] for event in events if event["kind"] == "provider_fallback"] == ["openrouter"]


@pytest.mark.asyncio
async def test_startup_substitution_is_recorded_once_for_the_whole_task(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Başlangıçta duyurulan ikame, ilk gerçek model çağrısında (gerçek _call_model_with_retries) yeniden yazılmaz."""
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OMNI_FALLBACK_BACKENDS", "openai")
    seen: list[str] = []

    async def fake_stream(
        client: Any, profile: Any, messages: Any, schemas: Any, session_id: str,
        emit: Any, should_stop: Any,
    ) -> Any:
        seen.append(profile["provider"])
        return text_turn()

    monkeypatch.setattr(main, "_stream_completion", fake_stream)
    events: list[Any] = []
    report = await main.run_agent_with_callback(
        "Merhaba de", events.append,
        {"requested_backend": None, "should_stop": lambda: False, "state_file": str(tmp_path / "state.json"),
         "history": []},
        {"openai": object()},
    )
    assert report["success"] and seen == ["openai"]
    assert len([event for event in events if event["kind"] == "provider_fallback"]) == 1
    assert len((tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()) == 1
