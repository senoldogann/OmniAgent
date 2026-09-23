import json
import os
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from typing import List
from uuid import uuid4

import pytest
from openai import AsyncOpenAI

from config import BACKENDS, DEFAULT_BACKEND
import main
import tools
from PIL import Image
from events import AgentEvent, preview_arguments
from main import ToolCallDraft, _trim_old_turns, encode_image, execute_tool, merge_tool_call_delta
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
    """Araç hatasını başarıdan ayırır, komut çıktısını canlı yayınlar, özel yöntemleri reddeder."""
    events: List[AgentEvent] = []
    call: ToolCallDraft = {"id": "call-1", "name": "execute_shell", "arguments": '{"command":"echo canli; exit 7","use_sudo":false}'}
    result = await execute_tool(call, Toolbox(), {}, events.append, lambda: False)
    assert result["tool_call_id"] == "call-1"
    assert result["ok"] is False
    assert result["error_type"] == "ToolError"
    assert [e for e in events if e["kind"] == "tool_output"] == [{"kind": "tool_output", "call_id": "call-1", "text": "canli\n"}]
    private: ToolCallDraft = {"id": "call-2", "name": "_read_full", "arguments": '{"path":"/etc/hosts"}'}
    assert (await execute_tool(private, Toolbox(), {}, events.append, lambda: False))["error_type"] == "UnknownTool"


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
    def make_call(name: str, args: dict) -> ToolCallDraft:
        return {"id": "c", "name": name, "arguments": json.dumps(args)}
    first = await execute_tool(make_call("read_file", {"path": str(target)}), toolbox, cache, lambda event: None, lambda: False)
    second = await execute_tool(make_call("read_file", {"path": str(target)}), toolbox, cache, lambda event: None, lambda: False)
    assert first["ok"] and second["ok"]
    assert first["result"] == second["result"]
    assert len(cache) == 1
    await execute_tool(
        make_call("write_file", {"path": str(tmp_path / "omni_cache_probe.txt"), "content": "x"}), toolbox, cache,
        lambda event: None, lambda: False,
    )
    assert cache == {}


def test_streaming_tool_call_assembly_and_preview() -> None:
    """Akış parçalarından araç çağrısının birleştirildiğini ve yarım JSON'dan komut önizlemesi çıktığını sınar."""
    drafts: List[ToolCallDraft] = []
    for call_id, name, chunk in [("c1", "execute_shell", ""), ("", None, '{"command": "ls /'), ("", None, 'tmp | head'), ("", None, ' -3", "use')]:
        drafts = merge_tool_call_delta(drafts, 0, call_id, name, chunk)
    assert drafts == [{"id": "c1", "name": "execute_shell", "arguments": '{"command": "ls /tmp | head -3", "use'}]
    assert preview_arguments("execute_shell", drafts[0]["arguments"]) == "ls /tmp | head -3"
    assert preview_arguments("execute_shell", '{"command": "echo \\"a') == 'echo "a'
    assert preview_arguments("write_file", '{"path": "/tmp/ç.txt", "content": "uzun') == "/tmp/ç.txt"


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


def test_camera_tool_only_appears_for_photo_capture_goal() -> None:
    ordinary = {entry["function"]["name"] for entry in main.build_tool_schemas()}
    camera = {entry["function"]["name"] for entry in main.build_tool_schemas(
        "Kamerayı açıp fotoğrafımı çek ve masaüstüne kaydet")}
    assert "capture_photo" not in ordinary
    assert "capture_photo" in camera
    assert len(camera) == len(ordinary) + 1
    assert not main.camera_photo_goal("Photo Booth fotoğraflarını listele")
    assert not main.camera_photo_goal("Fotoğrafımı çek ve /tmp/ozel.jpg dosyasına kaydet")
    assert not main.camera_photo_goal("Photo Booth ile fotoğraf çek ve masaüstüne kaydet")
    assert main.build_system_prompt(date.today(), "Bir dosya oku") == main.build_system_prompt(date.today())
    assert "### CAMERA PHOTO" in main.build_system_prompt(date.today(), "Fotoğrafımı çek ve desktop’a kaydet")


def test_capture_photo_validates_image_and_never_overwrites(tmp_path: Path, monkeypatch) -> None:
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    monkeypatch.setattr(tools.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(tools.shutil, "which", lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else None)
    commands = []

    def fake_process(command, shell, timeout):
        commands.append(command)
        Image.new("RGB", (3, 2), "red").save(command[-1])
        return 0, "", ""

    monkeypatch.setattr(tools, "run_streaming_process", fake_process)
    first = Toolbox().capture_photo()
    second = Toolbox().capture_photo()
    files = list(desktop.glob("fotograf-*.jpg"))
    assert len(files) == 2 and files[0] != files[1]
    assert all(path.stat().st_size > 0 for path in files)
    assert all("doğrulandı" in result for result in (first, second))
    assert all(command[command.index("-i") + 1] == "default:none" for command in commands)
    assert not list(desktop.glob(".omni_camera_*"))


def test_capture_photo_rejects_failed_or_invalid_capture(tmp_path: Path, monkeypatch) -> None:
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    monkeypatch.setattr(tools.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(tools.shutil, "which", lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else None)

    def failed_process(command, shell, timeout):
        Path(command[-1]).write_bytes(b"broken")
        return 1, "", "kamera meşgul"

    monkeypatch.setattr(tools, "run_streaming_process", failed_process)
    with pytest.raises(ToolError) as error:
        Toolbox().capture_photo()
    assert error.value.code == "CAMERA_CAPTURE_FAILED"
    assert not list(desktop.glob("fotograf-*.jpg"))
    assert not list(desktop.glob(".omni_camera_*"))
    monkeypatch.setattr(tools, "run_streaming_process",
                        lambda command, shell, timeout: (0, "", ""))
    with pytest.raises(ToolError) as error:
        Toolbox().capture_photo()
    assert error.value.code == "CAMERA_EMPTY"
    assert not list(desktop.glob("fotograf-*.jpg"))

def test_explicit_chrome_session_excludes_hidden_browser_and_discovery() -> None:
    """Açık Chrome isteği yalnız görünür oturum yolunu açar."""
    goal = "Açık Google Chrome oturumunu kullanarak Outlook çöp kutusuna git."
    names = {entry["function"]["name"] for entry in main.build_tool_schemas(goal)}
    assert main.active_chrome_session_goal(goal)
    assert "chrome_active_tab" in names
    assert "take_screenshot" in names
    assert "run_action_sequence" in names
    assert not names.intersection({
        "browse_url", "discover_capabilities", "fetch_raw", "execute_shell", "execute_js",
    })
    assert "USER'S OPEN CHROME SESSION" in main.build_system_prompt(date.today(), goal)
    ordinary = {entry["function"]["name"] for entry in main.build_tool_schemas("Outlook hesabımı incele")}
    assert "browse_url" in ordinary and "discover_capabilities" in ordinary
    assert not main.active_chrome_session_goal("Chrome kullanma")


@pytest.mark.asyncio
async def test_chrome_route_rejects_hidden_browser_at_execution() -> None:
    """Şemadan gizlenen araç, model adını uydursa bile çalışmaz."""
    from integration_runtime import CURRENT_RUNTIME, IntegrationRuntime

    runtime = IntegrationRuntime(lambda event: None, lambda: False)
    runtime.allowed_tools = frozenset({"chrome_active_tab", "take_screenshot"})
    token = CURRENT_RUNTIME.set(runtime)
    try:
        call: ToolCallDraft = {
            "id": "hidden", "name": "browse_url",
            "arguments": '{"url":"https://outlook.live.com","actions":[]}',
        }
        result = await execute_tool(call, Toolbox(), {}, lambda event: None, lambda: False)
        assert result["error_type"] == "ToolUnavailable"
    finally:
        CURRENT_RUNTIME.reset(token)


def test_chrome_active_tab_reuses_front_tab(monkeypatch: pytest.MonkeyPatch) -> None:
    """Chrome denetimi yeni profil açmaz; URL'yi ayrı argüman olarak iletir."""
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return tools.subprocess.CompletedProcess(args, 0, "https://outlook.live.com/mail/0/deleteditems\\nPoistetut\\n", "")

    monkeypatch.setattr(tools.subprocess, "run", fake_run)
    toolbox = Toolbox()
    result = toolbox.chrome_active_tab("https://outlook.live.com/mail/0/deleteditems")
    assert "Görünür Chrome" in result and "Poistetut" in result
    assert calls[0][0] == "osascript"
    assert calls[0][-1] == "https://outlook.live.com/mail/0/deleteditems"
    assert toolbox.browser is None
    with pytest.raises(ToolError) as error:
        toolbox.chrome_active_tab("javascript:alert(1)")
    assert error.value.code == "INVALID_URL"
