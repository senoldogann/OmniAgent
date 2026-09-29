"""Model çağrısı hata sınıflandırması, bekleme politikası ve gerçek SDK akışıyla yeniden deneme."""
from __future__ import annotations

import json
import logging
import socket
import ssl
import subprocess
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
from typing import Callable, Dict, List, Optional, Tuple, TypedDict

import httpx2
import pytest
from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError, AsyncOpenAI

from omniagent.app import agent as main
from omniagent.app.model_retry import (
    ModelCallFailed, ModelErrorInfo, classify_model_error, decide_retry, interactive_model_retry_seconds,
    retry_after_seconds, retry_wait_seconds, unattended_model_retry_seconds,
)
from omniagent.core.events import AgentEvent
from omniagent.integrations.runtime import CURRENT_RUNTIME, IntegrationRuntime

REQUEST = httpx2.Request("POST", "http://127.0.0.1/v1/chat/completions")
NOW = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)
SSE_HEADERS = {"content-type": "text/event-stream"}
SSE_DONE = "data: [DONE]\n\n"
TOOLS = [{"type": "function", "function": {"name": "ping", "parameters": {}}}]


class ScriptedResponse(TypedDict):
    status: int
    headers: Dict[str, str]
    body: str


def status_error(status: int, body: Dict[str, str], headers: Dict[str, str]) -> APIStatusError:
    """SDK'nın HTTP 4xx/5xx için kurduğu hatanın aynısı (sahte sınıf yok)."""
    response = httpx2.Response(status, headers=headers, request=REQUEST)
    return APIStatusError(f"Error code: {status}", response=response, body=body)


def stream_error(body: Dict[str, str]) -> APIError:
    """SDK'nın akış içi hata olayı için fırlattığı düz APIError."""
    return APIError(body.get("message", ""), REQUEST, body=body)


def sse_chunk(content: str, finish_reason: Optional[str]) -> str:
    delta: Dict[str, str] = {"content": content} if content else {}
    payload = {"id": "c1", "object": "chat.completion.chunk", "created": 0, "model": "m",
               "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}
    return f"data: {json.dumps(payload)}\n\n"


def sse_error(body: Dict[str, str]) -> str:
    return f"data: {json.dumps({'error': body})}\n\n"


def serve(script: List[ScriptedResponse]) -> Tuple[ThreadingHTTPServer, List[str]]:
    """Her POST için sıradaki hazır yanıtı dönen yerel OpenAI uyumlu sunucu; istek yollarını kaydeder."""
    received: List[str] = []
    remaining: List[ScriptedResponse] = list(script)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            received.append(self.path)
            scripted: ScriptedResponse = remaining.pop(0)
            payload: bytes = scripted["body"].encode("utf-8")
            self.send_response(scripted["status"])
            for name, value in scripted["headers"].items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    return server, received


@pytest.mark.asyncio
async def test_in_stream_transient_error_is_retried_on_real_sdk_stream() -> None:
    """Akış ortasında gelen hata olayı (düz APIError) artık görevi bitirmez: stream_reset + yeniden deneme."""
    server, received = serve([
        {"status": 200, "headers": SSE_HEADERS,
         "body": sse_chunk("Mer", None) + sse_error({"message": "Provider disconnected", "type": "server_error"})},
        {"status": 200, "headers": SSE_HEADERS,
         "body": sse_chunk("Merhaba", None) + sse_chunk("", "stop") + SSE_DONE},
    ])
    client = AsyncOpenAI(api_key="test", base_url=f"http://127.0.0.1:{server.server_port}/v1", max_retries=0)
    events: List[AgentEvent] = []
    try:
        turn, used = await main._call_model_with_retries(
            {"ollama-cloud": client}, [{"role": "user", "content": "selam"}], TOOLS, "oturum",
            "ollama-cloud", events.append, lambda: False,
        )
    finally:
        await client.close()
        server.shutdown()
        server.server_close()
    assert (turn["content"], used, len(received)) == ("Merhaba", "ollama-cloud", 2)
    assert any(event["kind"] == "stream_reset" for event in events)


@pytest.mark.asyncio
async def test_in_stream_context_length_error_fails_immediately() -> None:
    """Akış içi kalıcı hata (bağlam sınırı) yeniden denenmez; ModelCallFailed ilk hatayı zincirler."""
    server, received = serve([{
        "status": 200, "headers": SSE_HEADERS,
        "body": sse_error({"message": "too long", "type": "invalid_request_error", "code": "context_length_exceeded"}),
    }])
    client = AsyncOpenAI(api_key="test", base_url=f"http://127.0.0.1:{server.server_port}/v1", max_retries=0)
    try:
        with pytest.raises(ModelCallFailed) as failure:
            await main._call_model_with_retries(
                {"ollama-cloud": client}, [{"role": "user", "content": "selam"}], TOOLS, "oturum",
                "ollama-cloud", lambda event: None, lambda: False,
            )
    finally:
        await client.close()
        server.shutdown()
        server.server_close()
    assert failure.value.kind == "permanent" and failure.value.attempts == 1 and len(received) == 1
    assert "ollama-cloud" in str(failure.value) and "context_length_exceeded" in str(failure.value)
    cause = failure.value.__cause__
    assert isinstance(cause, APIError) and not isinstance(cause, APIStatusError)


CASES = [
    pytest.param(status_error(429, {"code": "rate_limit_exceeded"}, {"retry-after": "3"}), "rate_limited", 3.0,
                 id="429 hız sınırı + Retry-After"),
    pytest.param(status_error(429, {"code": "insufficient_quota"}, {}), "access", None, id="429 bakiye bitti"),
    pytest.param(status_error(429, {}, {"retry-after-ms": "1500"}), "rate_limited", 1.5, id="429 retry-after-ms"),
    # x-should-retry: false yalnız 'yeniden deneme': erişim/bakiye sayılmaz, profil engellenmez (erişim 401/402/403 ve koddur)
    pytest.param(status_error(429, {}, {"x-should-retry": "false"}), "retry_refused", None, id="429 x-should-retry false"),
    pytest.param(status_error(500, {}, {"x-should-retry": "false"}), "retry_refused", None, id="500 x-should-retry false"),
    pytest.param(status_error(503, {}, {"x-should-retry": "false", "retry-after": "2"}), "retry_refused", 2.0,
                 id="503 x-should-retry false + Retry-After"),
    pytest.param(status_error(500, {}, {"x-should-retry": "true"}), "transient", None, id="500 x-should-retry true"),
    pytest.param(status_error(401, {}, {"x-should-retry": "false"}), "access", None, id="401 x-should-retry false"),
    pytest.param(status_error(429, {"code": "insufficient_quota"}, {"x-should-retry": "false"}), "access", None,
                 id="429 bakiye + x-should-retry false"),
    pytest.param(status_error(400, {"type": "invalid_request_error"}, {"x-should-retry": "false"}), "permanent", None,
                 id="400 x-should-retry false"),
    pytest.param(status_error(503, {}, {"retry-after": "2"}), "transient", 2.0, id="503 + Retry-After"),
    pytest.param(status_error(500, {}, {}), "transient", None, id="500"),
    pytest.param(status_error(408, {}, {}), "transient", None, id="408"),
    pytest.param(status_error(400, {"type": "invalid_request_error"}, {}), "permanent", None, id="400"),
    pytest.param(status_error(404, {}, {}), "permanent", None, id="404"),
    pytest.param(status_error(401, {}, {}), "access", None, id="401"),
    pytest.param(status_error(402, {}, {}), "access", None, id="402"),
    pytest.param(status_error(403, {}, {}), "access", None, id="403"),
    pytest.param(stream_error({"message": "x", "type": "server_error"}), "transient", None, id="akış içi server_error"),
    pytest.param(stream_error({"message": "x", "type": "overloaded_error"}), "transient", None,
                 id="akış içi overloaded_error"),
    pytest.param(stream_error({"message": "x", "code": "429"}), "rate_limited", None, id="akış içi sayısal 429"),
    pytest.param(stream_error({"message": "x", "code": "402"}), "access", None, id="akış içi sayısal 402"),
    pytest.param(stream_error({"message": "x", "code": "502"}), "transient", None, id="akış içi sayısal 502"),
    pytest.param(stream_error({"message": "x", "type": "invalid_request_error", "code": "context_length_exceeded"}),
                 "permanent", None, id="akış içi bağlam sınırı kodu"),
    pytest.param(stream_error({"message": "Rate limit reached for requests"}), "rate_limited", None,
                 id="akış içi hız sınırı metni"),
    pytest.param(stream_error({"message": "the context window is too small"}), "permanent", None,
                 id="akış içi bağlam metni"),
    pytest.param(stream_error({"message": "beklenmeyen bir şey"}), "transient", None, id="akış içi tanınmayan"),
    pytest.param(APIError("An error occurred during streaming", REQUEST, body="rate limit exceeded"),
                 "rate_limited", None, id="akış içi metin gövdesi"),
    pytest.param(APITimeoutError(request=REQUEST), "timeout", None, id="zaman aşımı"),
    pytest.param(APIConnectionError(request=REQUEST), "transient", None, id="bağlantı hatası"),
    pytest.param(ssl.SSLError(1, "[SSL: SSLV3_ALERT_BAD_RECORD_MAC]"), "transient", None, id="TLS kayıt hatası"),
]


@pytest.mark.parametrize("error,kind,retry_after", CASES)
def test_classify_model_error(error: BaseException, kind: str, retry_after: Optional[float]) -> None:
    info = classify_model_error(error, NOW)
    assert (info.kind, info.retry_after) == (kind, retry_after)


@pytest.mark.parametrize("headers,expected", [
    ({"retry-after": "2.5"}, 2.5), ({"retry-after-ms": "1500", "retry-after": "9"}, 1.5),
    ({"retry-after": "Tue, 29 Sep 2026 12:00:30 GMT"}, 30.0),
    ({"retry-after": "Tue, 29 Sep 2026 11:59:00 GMT"}, None),
    ({"retry-after": "0"}, None), ({"retry-after": "bozuk"}, None), ({"retry-after": "inf"}, None), ({}, None),
])
def test_retry_after_seconds(headers: Dict[str, str], expected: Optional[float]) -> None:
    assert retry_after_seconds(headers, NOW) == expected


def test_retry_wait_seconds_is_bounded_deterministic_and_honors_hint() -> None:
    assert 0.375 <= retry_wait_seconds("transient", None, 0, "oturum") <= 0.5
    assert retry_wait_seconds("transient", None, 3, "oturum") == retry_wait_seconds("transient", None, 3, "oturum")
    assert 3.75 <= retry_wait_seconds("rate_limited", None, 0, "oturum") <= 5.0
    assert all(22.5 <= retry_wait_seconds("transient", None, step, "oturum") <= 30.0 for step in range(6, 40))
    assert retry_wait_seconds("rate_limited", 12.0, 3, "oturum") == 12.5
    assert len({retry_wait_seconds("transient", None, 4, f"oturum-{index}") for index in range(20)}) > 1


@pytest.mark.parametrize("kind,has_alternative,action,block,cooldown", [
    ("permanent", True, "raise", False, 0.0), ("access", True, "switch", True, 0.0),
    ("access", False, "raise", False, 0.0), ("rate_limited", True, "switch", False, 30.0),
    ("rate_limited", False, "retry", False, 0.0), ("timeout", True, "switch", False, 0.0),
    ("timeout", False, "retry", False, 0.0), ("transient", True, "retry", False, 0.0),
    # Yeniden denemeyi reddeden sunucu ve sertifika hatası: alternatif olsa da beklemeden hata, profil engellenmez
    ("retry_refused", True, "raise", False, 0.0), ("retry_refused", False, "raise", False, 0.0),
    ("certificate", True, "raise", False, 0.0), ("certificate", False, "raise", False, 0.0),
])
def test_decide_retry_matrix(kind: str, has_alternative: bool, action: str, block: bool, cooldown: float) -> None:
    decision = decide_retry(ModelErrorInfo(kind, None, None, None, False, None, ""), has_alternative, 0, "oturum")
    assert (decision.action, decision.block_backend, decision.cooldown_seconds) == (action, block, cooldown)


@pytest.mark.parametrize("explicit,expected", [(None, 60.0), (300.0, 300.0)])
def test_interactive_model_retry_budget(explicit: Optional[float], expected: float) -> None:
    assert interactive_model_retry_seconds(explicit) == expected


@pytest.mark.parametrize("max_wall_clock,expected", [(600.0, 600.0), (36000.0, 1800.0), (0.5, 0.5)])
def test_unattended_model_retry_budget_is_the_remaining_run_time_capped(max_wall_clock: float, expected: float) -> None:
    """Gözetimsiz kipte açık RunOptions değeri yoktur: bütçe kalan görev süresidir (en çok 30 dk)."""
    assert unattended_model_retry_seconds(max_wall_clock) == expected


@pytest.mark.parametrize("budget_function, value", [
    (interactive_model_retry_seconds, 0.0), (interactive_model_retry_seconds, -5.0),
    (interactive_model_retry_seconds, float("nan")),
    (unattended_model_retry_seconds, 0.0), (unattended_model_retry_seconds, -5.0),
    (unattended_model_retry_seconds, float("nan")),
])
def test_non_positive_model_retry_budget_is_rejected(budget_function: Callable[[float], float], value: float) -> None:
    """Sıfır, negatif ya da NaN süre sessizce sıfır bütçe olmaz (eskiden gözetimsiz kipte 0.0 ve negatif dönüyordu)."""
    with pytest.raises(ValueError, match="pozitif"):
        budget_function(value)


@pytest.mark.asyncio
@pytest.mark.parametrize("extra,expected", [
    ({}, 60.0), ({"max_wall_clock_seconds": 10.0}, 10.0),
    ({"model_retry_seconds": 300.0}, 300.0), ({"unattended": True}, 600.0),
    ({"unattended": True, "max_wall_clock_seconds": 36000.0}, 1800.0),
])
async def test_model_retry_deadline_follows_run_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, extra: Dict[str, object], expected: float,
) -> None:
    """Agent her turda runtime.model_retry_until'i bütçe ve kalan görev süresinin küçüğü olarak atar."""
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    remaining: List[float] = []

    async def fake_model(clients, messages, schemas, session_id, backend, emit, should_stop):
        runtime = CURRENT_RUNTIME.get()
        assert runtime is not None and runtime.model_retry_until is not None
        remaining.append(runtime.model_retry_until - time.monotonic())
        return {"content": "tamam", "tool_calls": [], "finish_reason": "stop", "usage": main.ZERO_USAGE}, backend

    monkeypatch.setattr(main, "_call_model_with_retries", fake_model)
    report = await main.run_agent_with_callback(
        "Merhaba de", lambda event: None,
        {"requested_backend": "openai", "should_stop": lambda: False, "state_file": str(tmp_path / "state.json"),
         "history": [], **extra},
        {"openai": object()},
    )
    assert report["success"] and expected - 5.0 < remaining[0] <= expected


@pytest.mark.asyncio
@pytest.mark.parametrize("error,reason_prefix", [
    (RuntimeError("beklenmeyen"), "kritik hata"),
    (ModelCallFailed("özet", backend="openai", kind="rate_limited", attempts=3, waited_seconds=12.0),
     "model çağrısı başarısız (hız sınırı)"),
])
async def test_run_failure_is_logged_with_traceback(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path,
    error: Exception, reason_prefix: str,
) -> None:
    """Dış except iki dala ayrılır ve ikisi de traceback ile loglar; sağlayıcı kesintisi 'kritik hata' sayılmaz."""
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))

    async def failing_model(clients, messages, schemas, session_id, backend, emit, should_stop):
        raise error

    monkeypatch.setattr(main, "_call_model_with_retries", failing_model)
    with caplog.at_level(logging.ERROR):
        report = await main.run_agent_with_callback(
            "Merhaba de", lambda event: None,
            {"requested_backend": "openai", "should_stop": lambda: False,
             "state_file": str(tmp_path / "state.json"), "history": []},
            {"openai": object()},
        )
    assert not report["success"] and report["reason"].startswith(reason_prefix)
    record = next(item for item in caplog.records if item.levelno == logging.ERROR)
    assert record.exc_info is not None and record.session_id


@pytest.mark.asyncio
async def test_x_should_retry_false_raises_honestly_without_blocking_the_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """
    x-should-retry: false erişim/bakiye hatası değildir: yeniden denenmez ama profil görev boyu engellenmez,
    özet 'sunucu yeniden denemeyi reddetti' der (eskiden alternatif varken profili engelleyip yedeğe geçiyordu).
    """
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    refused, refused_paths = serve([{
        "status": 500, "headers": {"x-should-retry": "false", "content-type": "application/json"},
        "body": json.dumps({"error": {"message": "server error", "type": "server_error"}}),
    }])
    spare, spare_paths = serve([{
        "status": 200, "headers": SSE_HEADERS,
        "body": sse_chunk("Merhaba", None) + sse_chunk("", "stop") + SSE_DONE,
    }])
    clients = {
        "ollama-cloud": AsyncOpenAI(api_key="test", base_url=f"http://127.0.0.1:{refused.server_port}/v1", max_retries=0),
        "openai": AsyncOpenAI(api_key="test", base_url=f"http://127.0.0.1:{spare.server_port}/v1", max_retries=0),
    }
    runtime = IntegrationRuntime(lambda event: None, lambda: False)
    runtime.primary_backend = "ollama-cloud"
    runtime.fallback_backends = frozenset({"openai"})
    token = CURRENT_RUNTIME.set(runtime)
    try:
        with caplog.at_level(logging.WARNING):
            with pytest.raises(ModelCallFailed) as failure:
                await main._call_model_with_retries(
                    clients, [{"role": "user", "content": "selam"}], TOOLS, "oturum", "ollama-cloud",
                    lambda event: None, lambda: False,
                )
    finally:
        CURRENT_RUNTIME.reset(token)
        for client in clients.values():
            await client.close()
        for server in (refused, spare):
            server.shutdown()
            server.server_close()
    assert failure.value.kind == "retry_refused" and failure.value.attempts == 1
    assert "sunucu yeniden denemeyi reddetti" in str(failure.value) and "erişim" not in str(failure.value)
    assert runtime.blocked_backends == set()
    # Hata sunucunun kararıdır: alternatif profil denenmez (yeniden deneme değil, açık hata)
    assert len(refused_paths) == 1 and spare_paths == []


def self_signed_certificate(directory: Path) -> Tuple[Path, Path]:
    """İstemcinin güven deposunda olmayan geçici öz-imzalı sertifika ve anahtar (openssl komutu macOS'ta hazır gelir)."""
    certificate, key = directory / "cert.pem", directory / "key.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(certificate),
         "-days", "1", "-subj", "/CN=127.0.0.1"],
        check=True, capture_output=True,
    )
    return certificate, key


def serve_tls(certificate: Path, key: Path) -> ThreadingHTTPServer:
    """TLS'li yerel sunucu; el sıkışma istemci sertifikayı reddedince sunucu tarafında sessizce biter."""
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.send_response(200)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.mark.asyncio
async def test_certificate_failure_is_not_retried_and_names_the_root_cause(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """
    Sertifika doğrulama hatası kalıcıdır: bütçe boyunca beklenmez, ilk denemede ModelCallFailed yükselir; kök neden
    (ssl.SSLCertVerificationError, SDK zincirinde yalnız __context__ üzerinden erişilir) iletide ve uyarı logunda görünür.
    """
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    certificate, key = self_signed_certificate(tmp_path)
    server = serve_tls(certificate, key)
    client = AsyncOpenAI(api_key="test", base_url=f"https://127.0.0.1:{server.server_port}/v1", max_retries=0)
    runtime = IntegrationRuntime(lambda event: None, lambda: False)
    started = time.monotonic()
    runtime.model_retry_until = started + 3.0
    token = CURRENT_RUNTIME.set(runtime)
    try:
        with caplog.at_level(logging.WARNING):
            with pytest.raises(ModelCallFailed) as failure:
                await main._call_model_with_retries(
                    {"openai": client}, [{"role": "user", "content": "selam"}], TOOLS, "oturum", "openai",
                    lambda event: None, lambda: False,
                )
    finally:
        CURRENT_RUNTIME.reset(token)
        await client.close()
        server.shutdown()
        server.server_close()
    assert failure.value.kind == "certificate" and failure.value.attempts == 1
    assert time.monotonic() - started < 2.5, "sertifika hatası yeniden deneme bütçesi boyunca beklenmemeli"
    assert "TLS sertifika doğrulaması başarısız" in str(failure.value)
    assert "SSLCertVerificationError" in str(failure.value)
    record = next(item for item in caplog.records if item.getMessage() == "Model çağrısı başarısız")
    assert record.kind == "certificate" and record.cause_type == "SSLCertVerificationError"
    assert runtime.blocked_backends == set()


@pytest.mark.asyncio
async def test_connection_error_detail_names_the_root_cause_and_stays_retryable() -> None:
    """Bağlantı reddi gibi APIConnectionError'un kök nedeni özet iletide ve cause_type'ta görünür; geçici sayılır."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    client = AsyncOpenAI(api_key="test", base_url=f"http://127.0.0.1:{port}/v1", max_retries=0)
    try:
        with pytest.raises(APIConnectionError) as failure:
            await client.chat.completions.create(
                model="m", messages=[{"role": "user", "content": "selam"}], stream=True,
            )
    finally:
        await client.close()
    info = classify_model_error(failure.value, NOW)
    assert info.kind == "transient" and info.cause_type == "ConnectionRefusedError"
    assert "ConnectionRefusedError" in info.detail
    # Durum hatası (yanıt alındı) bağlantı nedeni taşımaz: SDK'nın bastırılmış HTTPStatusError bağlamı iletiye girmez
    status = classify_model_error(status_error(500, {}, {}), NOW)
    assert status.cause_type is None and "neden" not in status.detail


@pytest.mark.parametrize("headers,logged,expected", [
    ({"retry-after": "bozuk"}, "retry-after", None), ({"retry-after": "inf"}, "retry-after", None),
    ({"retry-after-ms": "abc"}, "retry-after-ms", None),
    ({"retry-after-ms": "abc", "retry-after": "9"}, "retry-after-ms", 9.0),
    # Biçimi geçerli başlıklar (geçmiş tarih dahil) uyarı üretmez
    ({"retry-after": "Tue, 29 Sep 2026 11:59:00 GMT"}, None, None),
    ({"retry-after": "Tue, 29 Sep 2026 12:00:30 GMT"}, None, 30.0), ({"retry-after": "2.5"}, None, 2.5),
])
def test_unusable_retry_after_is_logged_but_behaviour_is_unchanged(
    caplog: pytest.LogCaptureFixture, headers: Dict[str, str], logged: Optional[str], expected: Optional[float],
) -> None:
    with caplog.at_level(logging.WARNING):
        assert retry_after_seconds(headers, NOW) == expected
    records = [item for item in caplog.records if item.getMessage().startswith("Retry-After başlığı kullanılabilir")]
    if logged is None:
        assert records == []
    else:
        assert len(records) == 1 and records[0].header == logged and records[0].value == headers[logged]


@pytest.mark.asyncio
async def test_stop_is_observed_while_waiting_for_response_headers() -> None:
    """
    Sunucu yanıt başlığını geç gönderirse Durdur ilk yanıtı beklemez (eskiden istemci zaman aşımı olan 30 sn'ye kadar
    etkisizdi): istek iptal edilir ve mevcut 'stopped' turu sözleşmesiyle dönülür.
    """
    release = Event()

    class SlowHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            release.wait(timeout=5.0)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), SlowHandler)
    Thread(target=server.serve_forever, daemon=True).start()
    client = AsyncOpenAI(api_key="test", base_url=f"http://127.0.0.1:{server.server_port}/v1", max_retries=0, timeout=30.0)
    started = time.monotonic()
    try:
        turn, used = await main._call_model_with_retries(
            {"openai": client}, [{"role": "user", "content": "selam"}], TOOLS, "oturum", "openai",
            lambda event: None, lambda: time.monotonic() - started >= 0.3,
        )
        elapsed = time.monotonic() - started
    finally:
        release.set()
        await client.close()
        server.shutdown()
        server.server_close()
    assert (turn["finish_reason"], turn["content"], turn["tool_calls"], used) == ("stopped", "", [], "openai")
    assert elapsed < 1.5, f"durdurma isteği ilk yanıtı bekledi: {elapsed:.1f} sn"


@pytest.mark.asyncio
@pytest.mark.parametrize("first_chunk, expected_content", [
    pytest.param(None, "", id="baslik-hemen-ilk-parca-gelmiyor"),
    pytest.param("Mer", "Mer", id="akis-ortada-duruyor"),
])
async def test_stop_is_observed_while_the_stream_is_stalled_after_the_headers(
    first_chunk: Optional[str], expected_content: str,
) -> None:
    """
    Başlık hemen gelip ilk parça gecikirse ya da akış ortada duraksarsa Durdur bir sonraki parçayı beklemez (eskiden parça
    gelene ya da istemci zaman aşımına dek etkisizdi; parça denetimi yalnız parça geldikten sonra yapılıyordu): akış
    iptal edilir, o ana dek gelen içerik ve 'stopped' sözleşmesiyle dönülür. Gerçek SDK akışı, gerçek yerel sunucu.
    """
    release = Event()

    class StalledStreamHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            if first_chunk is not None:
                self.wfile.write(sse_chunk(first_chunk, None).encode("utf-8"))
                self.wfile.flush()
            release.wait(timeout=5.0)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), StalledStreamHandler)
    Thread(target=server.serve_forever, daemon=True).start()
    client = AsyncOpenAI(api_key="test", base_url=f"http://127.0.0.1:{server.server_port}/v1", max_retries=0, timeout=30.0)
    events: List[AgentEvent] = []
    started = time.monotonic()
    try:
        turn, used = await main._call_model_with_retries(
            {"openai": client}, [{"role": "user", "content": "selam"}], TOOLS, "oturum", "openai",
            events.append, lambda: time.monotonic() - started >= 0.3,
        )
        elapsed = time.monotonic() - started
    finally:
        release.set()
        await client.close()
        server.shutdown()
        server.server_close()
    assert (turn["finish_reason"], turn["content"], turn["tool_calls"], used) == ("stopped", expected_content, [], "openai")
    assert [event["text"] for event in events if event["kind"] == "text_delta"] == ([first_chunk] if first_chunk else [])
    assert elapsed < 1.5, f"durdurma isteği bir sonraki akış parçasını bekledi: {elapsed:.1f} sn"


@pytest.mark.asyncio
async def test_unexpected_startup_error_is_logged_with_traceback(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path,
) -> None:
    """
    Görev başlangıcındaki beklenmeyen istisna (burada: hafıza dosyası yerine dizin) yalnız 'Kritik hata' bildirimine
    dönüşüp kaybolmaz: traceback ve yapısal alanlarla loglanır.
    """
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    (tmp_path / "hafiza_dizini").mkdir()
    with caplog.at_level(logging.ERROR):
        report = await main.run_agent_with_callback(
            "Merhaba de", lambda event: None,
            {"requested_backend": "openai", "should_stop": lambda: False, "state_file": str(tmp_path / "state.json"),
             "history": [], "memory_file": str(tmp_path / "hafiza_dizini")},
            {"openai": object()},
        )
    assert not report["success"] and report["reason"].startswith("Kritik hata")
    record = next(
        item for item in caplog.records if item.getMessage() == "Görev başlangıcı beklenmeyen hatayla başarısız"
    )
    assert record.exc_info is not None and record.error_type == "IsADirectoryError" and record.backend == "openai"


@pytest.mark.asyncio
async def test_refused_retry_message_does_not_suggest_enabling_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Sunucu yeniden denemeyi reddettiyse hata alternatif profile geçmez: iletide 'yedek izni ver' önerilmez."""
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    refused, _ = serve([{
        "status": 500, "headers": {"x-should-retry": "false", "content-type": "application/json"},
        "body": json.dumps({"error": {"message": "server error", "type": "server_error"}}),
    }])
    clients = {
        "ollama-cloud": AsyncOpenAI(api_key="test", base_url=f"http://127.0.0.1:{refused.server_port}/v1", max_retries=0),
        "openai": AsyncOpenAI(api_key="test", base_url="http://127.0.0.1:9/v1", max_retries=0),
    }
    runtime = IntegrationRuntime(lambda event: None, lambda: False)
    runtime.primary_backend = "ollama-cloud"
    token = CURRENT_RUNTIME.set(runtime)
    try:
        with pytest.raises(ModelCallFailed) as failure:
            await main._call_model_with_retries(
                clients, [{"role": "user", "content": "selam"}], TOOLS, "oturum", "ollama-cloud",
                lambda event: None, lambda: False,
            )
    finally:
        CURRENT_RUNTIME.reset(token)
        for client in clients.values():
            await client.close()
        refused.shutdown()
        refused.server_close()
    assert failure.value.kind == "retry_refused"
    assert "yedek sağlayıcı izni" not in str(failure.value)
