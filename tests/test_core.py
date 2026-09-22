import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
from uuid import uuid4

import pytest
from openai import AsyncOpenAI

from config import BACKENDS, DEFAULT_BACKEND
from main import _trim_old_turns, encode_image, execute_tool
from state_manager import EpisodeMetrics, load_state, make_step_record, record_episode, save_state
from tools import ScreenGeometry, ToolError, Toolbox, _is_sensitive_path, model_to_points, points_to_model


@pytest.mark.asyncio
async def test_api_connectivity() -> None:
    """İstenirse varsayılan backend'e gerçek API erişimini sınar."""
    if os.environ.get("OMNI_LIVE_API_TEST") != "1":
        pytest.skip("Canlı API testi OMNI_LIVE_API_TEST=1 ile etkinleştirilir.")
    profile = BACKENDS[DEFAULT_BACKEND]
    if not profile["api_key"]:
        pytest.skip("OpenCode API anahtarı bulunamadı.")
    async with AsyncOpenAI(api_key=profile["api_key"], base_url=profile["base_url"]) as client:
        response = await client.chat.completions.create(
            model=profile["model"],
            messages=[{"role": "user", "content": "Reply with OK"}],
            max_tokens=10,
            extra_headers={"x-opencode-session": str(uuid4())},
            extra_body=profile["extra_body"],
        )
    assert response.choices
    assert response.choices[0].message.content


def test_shell_execution() -> None:
    """Başarılı ve başarısız kabuk komutlarını ayırır."""
    toolbox: Toolbox = Toolbox()
    assert "PROFESSIONAL_TEST" in toolbox.execute_shell("printf PROFESSIONAL_TEST", False)
    with pytest.raises(ToolError) as failure:
        toolbox.execute_shell("exit 7", False)
    assert failure.value.code == "SHELL_EXIT"


def test_file_ops(tmp_path: Path) -> None:
    """Eksik üst dizinli dosya yazımını ve geri okumayı sınar."""
    toolbox: Toolbox = Toolbox()
    path: Path = tmp_path / "yeni" / "alt" / "omni_test_file.txt"
    content: str = "Türkçe içerik\r\nikinci satır"
    toolbox.write_file(str(path), content)
    assert toolbox.read_file(str(path)) == content


def test_memory_roundtrip(tmp_path: Path) -> None:
    """Epizot kaydının atomik yazılıp geri okunduğunu ve dosya izinlerini sınar."""
    path: Path = tmp_path / "memory.json"
    metrics: EpisodeMetrics = {
        "turns": 2, "tool_calls": 1, "elapsed_seconds": 3.5, "backend": "opencode",
        "prompt_tokens": 100, "cached_tokens": 80, "completion_tokens": 20,
    }
    step = make_step_record("execute_shell", '{"command": "date"}', True, "STDOUT: ok")
    state = record_episode(load_state(str(path)), "örnek hedef", [step], "tamam", True, metrics)
    save_state(str(path), state)
    loaded = load_state(str(path))
    assert loaded["episodic_memory"][-1]["goal"] == "örnek hedef"
    assert loaded["episodic_memory"][-1]["metrics"]["cached_tokens"] == 80
    assert path.stat().st_mode & 0o777 == 0o600


def test_corrupt_memory_is_preserved(tmp_path: Path) -> None:
    """Bozuk bellek dosyasının sessizce sıfırlanmasını önler."""
    path: Path = tmp_path / "memory.json"
    path.write_text("{bozuk", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        load_state(str(path))
    assert path.read_text(encoding="utf-8") == "{bozuk"


class EchoHandler(BaseHTTPRequestHandler):
    """Yerel HTTP çağrısında istek yolunu geri döndürür."""

    def do_GET(self) -> None:
        body: bytes = self.path.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def test_fetch_raw_keeps_url_literal() -> None:
    """URL içindeki kabuk karakterlerinin metin olarak taşındığını sınar."""
    server: HTTPServer = HTTPServer(("127.0.0.1", 0), EchoHandler)
    thread: Thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url: str = f"http://127.0.0.1:{server.server_port}/search?q=one;two"
        assert "/search?q=one;two" in Toolbox().fetch_raw(url)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.asyncio
async def test_tool_error_is_explicit() -> None:
    """Araç çağrısı hatasını başarıdan ayırır; şemada olmayan adlar (özel yöntemler) reddedilir."""
    call = SimpleNamespace(
        id="call-1",
        function=SimpleNamespace(name="execute_shell", arguments='{"command":"exit 7","use_sudo":false}'),
    )
    result = await execute_tool(call, Toolbox(), {})
    assert result["tool_call_id"] == "call-1"
    assert result["ok"] is False
    assert result["error_type"] == "ToolError"
    private = SimpleNamespace(id="call-2", function=SimpleNamespace(name="_read_full", arguments='{"path":"/etc/hosts"}'))
    assert (await execute_tool(private, Toolbox(), {}))["error_type"] == "UnknownTool"


def test_vision_capture(tmp_path: Path) -> None:
    """Gerçek ekran görüntüsünün ortak koordinat uzayında kaydedildiğini sınar."""
    toolbox: Toolbox = Toolbox()
    path: Path = tmp_path / "vision.png"
    toolbox.take_screenshot(str(path))
    assert path.exists()
    assert len(encode_image(str(path))) > 0


@pytest.mark.asyncio
async def test_readonly_tool_calls_are_cached(tmp_path: Path) -> None:
    """Salt okunur araçların önbelleklendiğini, yan etkili çağrı sonrası önbelleğin temizlendiğini sınar."""
    cache: dict = {}
    toolbox: Toolbox = Toolbox()
    target: Path = tmp_path / "omni_cache_read.txt"
    target.write_text("icerik", encoding="utf-8")
    make_call = lambda name, args: SimpleNamespace(
        id="c", function=SimpleNamespace(name=name, arguments=json.dumps(args)),
    )
    first = await execute_tool(make_call("read_file", {"path": str(target)}), toolbox, cache)
    second = await execute_tool(make_call("read_file", {"path": str(target)}), toolbox, cache)
    assert first["ok"] and second["ok"]
    assert first["result"] == second["result"]
    assert len(cache) == 1
    await execute_tool(
        make_call("write_file", {"path": str(tmp_path / "omni_cache_probe.txt"), "content": "x"}), toolbox, cache,
    )
    assert cache == {}


def test_trim_never_cuts_latest_turn() -> None:
    """Paralel 5'li okumanın sonuçları model görmeden kırpılmaz; yalnızca eski turlar budanır."""
    def turn(prefix: str) -> list:
        calls = [{"id": f"{prefix}{i}", "type": "function", "function": {"name": "read_file", "arguments": "{}"}} for i in range(5)]
        return [{"role": "assistant", "tool_calls": calls}] + [
            {"role": "tool", "tool_call_id": f"{prefix}{i}", "content": "x" * 1500} for i in range(5)
        ]
    messages = [{"role": "system", "content": "S"}, {"role": "user", "content": "hedef"}] + turn("a") + turn("b") + turn("c")
    trimmed = _trim_old_turns(messages)
    tool_lengths = [len(m["content"]) for m in trimmed if m["role"] == "tool"]
    assert all(length < 1500 for length in tool_lengths[:5])
    assert all(length == 1500 for length in tool_lengths[5:])
    assert trimmed[1]["content"] == "hedef"


def test_model_space_roundtrip() -> None:
    """Retina nokta uzayı ile modelin gördüğü görüntü uzayı arasındaki dönüşümü sınar."""
    geometry: ScreenGeometry = {"point_width": 1710, "point_height": 1112, "model_width": 1280, "model_height": 832}
    assert model_to_points(1279, 831, geometry) == (1709, 1111)
    assert points_to_model(855, 556, geometry) == (640, 416)


def test_sensitive_path_macos_firmlink_false_positive() -> None:
    """macOS'ta /System/Volumes/Data altına çözülen kullanıcı yollarının bloklanmadığını sınar."""
    assert not _is_sensitive_path(Path("/tmp/omni_yazilabilir.txt"))
    assert not _is_sensitive_path(Path("/home/kullanici/notlar.txt"))
    assert _is_sensitive_path(Path("/System/Library/CoreServices/test.txt"))
    assert _is_sensitive_path(Path("/etc/hosts"))
    assert _is_sensitive_path(Path.home() / ".ssh" / "id_rsa")
