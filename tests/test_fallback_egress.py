"""İzin listesinin telin öbür ucunda doğrulanması: gerçek openai SDK'sı, gerçek HTTP, yerel sahte sağlayıcılar."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List

import pytest
from openai import AsyncOpenAI
from PIL import Image

from omniagent.app import agent as main
from omniagent.app.model_retry import ModelCallFailed
from omniagent.core.events import AgentEvent
from omniagent.integrations.runtime import CURRENT_RUNTIME, IntegrationRuntime

IMAGE_MARKER = "GORUNTU-ISARETCISI-123"
CONTENT_CHUNK = json.dumps({
    "id": "x", "object": "chat.completion.chunk", "created": 0, "model": "m",
    "choices": [{"index": 0, "delta": {"role": "assistant", "content": "tamam"}, "finish_reason": None}],
})
FINISH_CHUNK = json.dumps({
    "id": "x", "object": "chat.completion.chunk", "created": 0, "model": "m",
    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
})


class FakeProvider:
    """OpenAI uyumlu yerel uç nokta: gelen gövdeleri kaydeder; status=200 ise akış, değilse hata döner."""

    def __init__(self, status: int) -> None:
        self.bodies: List[str] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                owner.bodies.append(self.rfile.read(int(self.headers["Content-Length"])).decode("utf-8"))
                if status == 200:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    for chunk in (CONTENT_CHUNK, FINISH_CHUNK, "[DONE]"):
                        self.wfile.write(f"data: {chunk}\n\n".encode())
                    return
                payload = json.dumps({"error": {"message": "kota", "type": "quota"}}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def request_messages(with_image: bool) -> List[Dict[str, object]]:
    if not with_image:
        return [{"role": "user", "content": "merhaba"}]
    return [{"role": "user", "content": [
        {"type": "text", "text": "ekrana bak"},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{IMAGE_MARKER}"}},
    ]}]


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed,allow_images,with_image,second_requests,marker_sent", [
    (frozenset(), False, False, 0, False),               # varsayılan: ikinci sağlayıcıya HİÇ istek gitmez
    (frozenset({"openai"}), False, False, 1, False),     # izinli, görüntüsüz
    (frozenset({"openai"}), False, True, 0, False),      # izinli ama görüntülü istek ayrıca görüntü izni ister
    (frozenset({"openai"}), True, True, 1, True),        # görüntü izni açık: gövdede görüntü var
])
async def test_request_reaches_second_provider_only_with_explicit_permission(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, allowed: frozenset[str], allow_images: bool,
    with_image: bool, second_requests: int, marker_sent: bool,
) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    primary, secondary = FakeProvider(402), FakeProvider(200)
    clients = {
        "ollama-cloud": AsyncOpenAI(base_url=primary.url, api_key="x", max_retries=0),
        "openai": AsyncOpenAI(base_url=secondary.url, api_key="x", max_retries=0),
    }
    runtime = IntegrationRuntime(lambda event: None, lambda: False)
    runtime.primary_backend = "ollama-cloud"
    runtime.fallback_backends = allowed
    runtime.fallback_images = allow_images
    events: List[AgentEvent] = []
    token = CURRENT_RUNTIME.set(runtime)
    try:
        call = main._call_model_with_retries(
            clients, request_messages(with_image), [], "oturum", "ollama-cloud", events.append, lambda: False,
        )
        if second_requests:
            turn, used = await call
            assert turn["content"] == "tamam" and used == "openai"
        else:
            with pytest.raises(ModelCallFailed) as failure:
                await call
            assert failure.value.kind == "access" and "OMNI_FALLBACK" in str(failure.value)
    finally:
        CURRENT_RUNTIME.reset(token)
        for client in clients.values():
            await client.close()
        primary.close()
        secondary.close()
    assert len(primary.bodies) == 1
    assert len(secondary.bodies) == second_requests
    assert any(IMAGE_MARKER in body for body in secondary.bodies) is marker_sent
    switches = [event for event in events if event["kind"] == "provider_fallback"]
    assert len(switches) == second_requests
    audit = tmp_path / "audit.jsonl"
    lines = audit.read_text(encoding="utf-8").splitlines() if audit.exists() else []
    assert len([line for line in lines if "provider_fallback" in line]) == second_requests
    if switches:
        assert switches[0]["to_backend"] == "openai" and switches[0]["image_count"] == int(with_image)


@pytest.mark.asyncio
async def test_persistent_fallback_records_every_new_image_level(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """
    Birincil 402 ile karantinaya alınıp kalıcı yedeğe geçilmiş görevde sonraki ekran görüntülü istekler de
    denetim kaydına düşer: her yeni (hedef, görüntü seviyesi) için bir satır, aynı seviyede tekrar yok.
    """
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    primary, secondary = FakeProvider(402), FakeProvider(200)
    clients = {
        "ollama-cloud": AsyncOpenAI(base_url=primary.url, api_key="x", max_retries=0),
        "openai": AsyncOpenAI(base_url=secondary.url, api_key="x", max_retries=0),
    }
    runtime = IntegrationRuntime(lambda event: None, lambda: False)
    runtime.primary_backend = "ollama-cloud"
    runtime.fallback_backends = frozenset({"openai"})
    runtime.fallback_images = True
    events: List[AgentEvent] = []
    token = CURRENT_RUNTIME.set(runtime)
    try:
        # 1. çağrı: birincil 402 -> openai (görüntüsüz); openai kalıcı yedek olur.
        _, used = await main._call_model_with_retries(
            clients, request_messages(False), [], "oturum", "ollama-cloud", events.append, lambda: False,
        )
        assert used == "openai" and runtime.blocked_backends == {"ollama-cloud"}
        # Sonraki turlar ajanın yaptığı gibi kalıcı yedekle (backend="openai") ve ekran görüntüsüyle gider.
        for _ in range(3):
            await main._call_model_with_retries(
                clients, request_messages(True), [], "oturum", "openai", events.append, lambda: False,
            )
    finally:
        CURRENT_RUNTIME.reset(token)
        for client in clients.values():
            await client.close()
        primary.close()
        secondary.close()
    assert len(primary.bodies) == 1 and len(secondary.bodies) == 4
    assert sum(IMAGE_MARKER in body for body in secondary.bodies) == 3
    switches = [event for event in events if event["kind"] == "provider_fallback"]
    assert [(event["from_backend"], event["to_backend"], event["image_count"]) for event in switches] == [
        ("ollama-cloud", "openai", 0), ("ollama-cloud", "openai", 1),
    ]
    lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and "görüntü: 0" in lines[0] and "görüntü: 1" in lines[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("backends,images,request_count", [
    ("none", None, 0),        # varsayılan: Otomatik + görsel ek, yalnız openai hazır -> OpenAI'a HİÇ istek gitmez
    ("openai", None, 0),      # profil izinli ama görüntü izni yok -> yine gitmez
    ("openai", "1", 1),       # ikisi de açık -> tam bir istek, gövdede görüntü var
])
async def test_auto_start_with_only_openai_ready_sends_the_photo_only_with_permission(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, backends: str, images: str | None, request_count: int,
) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OMNI_FALLBACK_BACKENDS", backends)
    if images is None:
        monkeypatch.delenv("OMNI_FALLBACK_IMAGES", raising=False)
    else:
        monkeypatch.setenv("OMNI_FALLBACK_IMAGES", images)
    photo = tmp_path / "foto.png"
    Image.new("RGB", (32, 32), (200, 10, 10)).save(photo)
    provider = FakeProvider(200)
    clients = {"openai": AsyncOpenAI(base_url=provider.url, api_key="x", max_retries=0)}
    events: List[AgentEvent] = []
    try:
        report = await main.run_agent_with_callback(
            "bu fotoğrafa bak", events.append,
            {"requested_backend": None, "should_stop": lambda: False, "state_file": str(tmp_path / "state.json"),
             "history": [], "images": [str(photo)]},
            clients,
        )
    finally:
        await clients["openai"].close()
        provider.close()
    assert len(provider.bodies) == request_count
    audit = tmp_path / "audit.jsonl"
    lines = audit.read_text(encoding="utf-8").splitlines() if audit.exists() else []
    switches = [event for event in events if event["kind"] == "provider_fallback"]
    if request_count:
        assert report["success"] and "image_url" in provider.bodies[0]
        assert len(switches) == 1 and len(lines) == 1 and "görüntü: 1" in lines[0]
    else:
        assert not report["success"] and "izin" in report["outcome"] and not switches and not lines
