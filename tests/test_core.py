import itertools
import json
import os
import ssl
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from typing import List
from uuid import uuid4

import pytest
from openai import AsyncOpenAI

from capabilities import CapabilityService
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


def test_fetch_raw_rejects_non_http_urls() -> None:
    """fetch_raw yerel dosya veya şema enjeksiyonuyla sistem okuyamaz."""
    with pytest.raises(ToolError) as failure:
        Toolbox().fetch_raw("file:///etc/hosts")
    assert failure.value.code == "INVALID_URL"
    with pytest.raises(ToolError) as failure:
        Toolbox().fetch_raw("not-a-url")
    assert failure.value.code == "INVALID_URL"


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
    assert preview_arguments("cua_click_point", '{"point": [196, 17') == ""
    assert preview_arguments("cua_click_point", '{"point": [196, 175]}') == "196, 175"


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


def test_trim_keeps_text_from_expired_screenshot() -> None:
    """Eski görsel atılırken aynı multimodal mesajdaki dayanıklı STATE metni kaybolmaz."""
    image_message = {
        "role": "user",
        "content": [
            {"type": "text", "text": "STATE: angular/angular stars=100000 status=VALID"},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,abc"}},
        ],
    }
    messages = [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "hedef"},
        {"role": "assistant", "content": "ilk tur"},
        image_message,
        {"role": "assistant", "content": "ikinci tur"},
        {"role": "assistant", "content": "üçüncü tur"},
    ]
    trimmed = _trim_old_turns(messages)
    assert isinstance(trimmed[3]["content"], str)
    assert "angular/angular stars=100000" in trimmed[3]["content"]
    assert "eski ekran görüntüsü" in trimmed[3]["content"]
    assert "data:image" not in trimmed[3]["content"]


def test_budget_pressure_and_duplicate_chrome_visit_are_explicit() -> None:
    """Soft budget ve tekrar ziyaret guard'ları zorunlu işi kesmeden modele görünür not üretir."""
    usage = {"prompt_tokens": 70_000, "cached_tokens": 5_000, "completion_tokens": 1_000}
    note = main.budget_pressure_message(usage, main.SOFT_TOOL_CALL_BUDGET)
    assert note is not None and "opsiyonel keşfi" in note and "STATE" in note

    call: ToolCallDraft = {
        "id": "c1", "name": "chrome_active_tab",
        "arguments": '{"url":"https://github.com/angular/angular"}',
    }
    result = {"tool_call_id": "c1", "ok": True, "result": "ok"}
    first, first_note = main.update_chrome_visits({}, call, result)
    second, second_note = main.update_chrome_visits(first, call, result)
    assert first == {"https://github.com/angular/angular": 1}
    assert first_note is None
    assert second["https://github.com/angular/angular"] == 2
    assert second_note is not None and "yeniden doğrulama" in second_note


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
    assert {"cua_click_point", "cua_type_text", "cua_press_key", "cua_submit_text"} <= names
    assert not names.intersection({
        "browse_url", "discover_capabilities", "fetch_raw", "execute_shell", "execute_js",
        "run_action_sequence", "smart_click", "cua_get_ax_state", "cua_click", "cua_get_app",
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
    """Chrome denetimi yeni profil açmaz; URL, köken ve yükleme sınırını ayrı argüman olarak iletir."""
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return tools.subprocess.CompletedProcess(
            args, 0, "https://outlook.live.com/mail/0/deleteditems\nPoistetut\ntrue\n", "")

    monkeypatch.setattr(tools.subprocess, "run", fake_run)
    toolbox = Toolbox()
    result = toolbox.chrome_active_tab("https://outlook.live.com/mail/0/deleteditems")
    assert "Görünür Chrome" in result and "Poistetut" in result and "hâlâ yükleniyor" in result
    assert calls[0][0] == "osascript"
    assert calls[0][-3:] == [
        "https://outlook.live.com/mail/0/deleteditems", "https://outlook.live.com/", str(tools.CHROME_LOAD_CHECKS),
    ]
    assert "tab id tabId of targetWindow" in calls[0][2]
    assert toolbox.browser is None
    with pytest.raises(ToolError) as error:
        toolbox.chrome_active_tab("javascript:alert(1)")
    assert error.value.code == "INVALID_URL"


def test_chrome_active_tab_live_matching_tab_in_back_window() -> None:
    """
    Gerçek Chrome'da: aynı kökenli sekme arkadaki penceredeyse o sekme öne gelip gezinir,
    öndeki pencerenin sekmesine dokunulmaz. Test pencereleri sonunda kapatılır.
    """
    if os.environ.get("OMNI_CHROME_TEST") != "1":
        pytest.skip("Canlı Chrome testi OMNI_CHROME_TEST=1 ile etkinleştirilir (ekranda pencere açar).")
    server: HTTPServer = HTTPServer(("127.0.0.1", 0), EchoHandler)
    Thread(target=server.serve_forever, daemon=True).start()
    target_origin = f"http://127.0.0.1:{server.server_port}/"
    other_origin = f"http://localhost:{server.server_port}/"

    def chrome(script: str, *args: str) -> str:
        return tools.subprocess.run(["osascript", "-e", script, *args], capture_output=True, text=True,
                                    check=True, timeout=20).stdout.strip()

    try:
        chrome('on run argv\ntell application "Google Chrome"\nset backWindow to make new window\n'
               'set URL of active tab of backWindow to (item 1 of argv)\nset frontWindow to make new window\n'
               'set URL of active tab of frontWindow to (item 2 of argv)\nend tell\nend run',
               target_origin + "arka", other_origin + "on")
        result = Toolbox().chrome_active_tab(target_origin + "hedef")
        assert f"Görünür Chrome sekmesi: {target_origin}hedef" in result
        front = chrome('tell application "Google Chrome" to get URL of every tab of front window')
        assert f"{target_origin}hedef" in front and other_origin not in front
    finally:
        chrome('on run argv\nset targetOrigin to item 1 of argv\nset otherOrigin to item 2 of argv\n'
               'tell application "Google Chrome"\nset windowIds to id of every window\n'
               'repeat with windowId in windowIds\nclose (every tab of window id windowId whose URL starts with '
               'targetOrigin or URL starts with otherOrigin)\nend repeat\nend tell\nend run',
               target_origin, other_origin)
        server.shutdown()
        server.server_close()


def test_screen_settle_waits_for_late_reaction_then_quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Durulma sabit uyku değildir: gecikmeli tepki beklenir, tepki durulunca bırakılır; tepki
    yoksa tepki süresinde, sürekli animasyonda üst sınırda biter.
    """
    monkeypatch.setattr(tools, "SETTLE_POLL_SECONDS", 0.005)
    monkeypatch.setattr(tools, "SETTLE_REACTION_SECONDS", 0.3)
    monkeypatch.setattr(tools, "SETTLE_QUIET_SECONDS", 0.1)
    monkeypatch.setattr(tools, "SETTLE_MAX_SECONDS", 0.6)
    baseline = tools.np.zeros((10, 16), dtype=tools.np.uint8)
    clock = {"now": 0.0}

    monkeypatch.setattr(tools.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        tools.time, "sleep",
        lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
    )

    def settle(frame_at) -> float:
        clock["now"] = 0.0
        started = clock["now"]
        monkeypatch.setattr(tools, "settle_frame", lambda: frame_at(clock["now"] - started))
        return tools.wait_for_screen_settle(baseline, started)

    changed = baseline + 255
    # 0,15 sn sonra gelen sonuç görülür ve 0,1 sn sakin kalınca bırakılır
    late = settle(lambda elapsed: changed if elapsed >= 0.15 else baseline)
    # Hiç tepki yoksa tepki süresi dolunca bırakılır
    none = settle(lambda elapsed: baseline)
    # Her karede değişen ekran üst sınırda bırakılır
    frames = itertools.cycle([baseline, changed])
    endless = settle(lambda elapsed: next(frames))
    # Kare başına eşiğin altında değişen yavaş iskelet parıltısı birikerek hareket sayılır
    shimmer = settle(lambda elapsed: baseline + min(255, int(elapsed * 100)))
    assert 0.24 <= late <= 0.26
    assert 0.30 <= none <= 0.31
    assert 0.60 <= endless <= 0.61
    assert 0.60 <= shimmer <= 0.61
    # Gerçek kare boyutunda imleç kadar değişim yok sayılır, içerik bloğu tepki sayılır
    screen = tools.np.zeros((104, 160), dtype=tools.np.uint8)
    caret, block = screen.copy(), screen.copy()
    caret[50, 80:82] = 255
    block[20:40, 30:70] = 255
    assert tools.frame_change_ratio(screen, caret, tools.SETTLE_PIXEL_DELTA) <= tools.SETTLE_CHANGED_RATIO
    assert tools.frame_change_ratio(screen, block, tools.SETTLE_PIXEL_DELTA) > tools.SETTLE_CHANGED_RATIO


@pytest.mark.asyncio
async def test_tls_record_error_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """Akış okumasından sarılmadan yükselen ssl.SSLError görevi bitirmez, yeniden denenir."""
    attempts: List[str] = []

    async def flaky(client, profile, messages, schemas, session_id, emit, should_stop):
        attempts.append(profile["model"])
        if len(attempts) == 1:
            raise ssl.SSLError(1, "[SSL: SSLV3_ALERT_BAD_RECORD_MAC] ssl/tls alert bad record mac")
        return {"content": "TAMAM", "tool_calls": [], "finish_reason": "stop", "usage": main.ZERO_USAGE}

    monkeypatch.setattr(main, "_stream_completion", flaky)
    turn, backend = await main._call_model_with_retries(
        {"opencode": object()}, [], [], "oturum", "opencode", lambda event: None, lambda: False)
    assert turn["content"] == "TAMAM" and backend == "opencode" and len(attempts) == 2


@pytest.mark.asyncio
async def test_transient_api_fallback_does_not_become_sticky(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Bir turdaki Claude fallback'i sonraki turu düşük max_tokens backend'ine kilitlemez."""
    target = tmp_path / "probe.txt"
    target.write_text("ok", encoding="utf-8")
    requested_backends: List[str] = []

    async def fake_model(clients, messages, schemas, session_id, backend, emit, should_stop):
        requested_backends.append(backend)
        if len(requested_backends) == 1:
            return {
                "content": "STATE: probe okunacak",
                "tool_calls": [{
                    "id": "read-1", "name": "read_file",
                    "arguments": json.dumps({"path": str(target)}),
                }],
                "finish_reason": "tool_calls", "usage": main.ZERO_USAGE,
            }, "claude"
        return {
            "content": "tamam", "tool_calls": [], "finish_reason": "stop", "usage": main.ZERO_USAGE,
        }, backend

    monkeypatch.setattr(main, "_call_model_with_retries", fake_model)
    service = CapabilityService(tmp_path)
    try:
        report = await main.run_agent_with_callback(
            "Dosyayı oku ve sonucu söyle", lambda event: None,
            {"requested_backend": None, "should_stop": lambda: False,
             "state_file": str(tmp_path / "memory.json"), "history": [], "integrations": service},
            {"opencode": object(), "claude": object()},
        )
    finally:
        await service.close()
    assert report["success"]
    assert requested_backends == ["opencode", "opencode"]


@pytest.mark.asyncio
async def test_truncated_final_answer_gets_one_recovery_turn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Araçsız final max_tokens'ta kesilirse görev hemen başarısız sayılmaz; bir kısa tamamlama turu yapılır."""
    calls: List[int] = []

    async def fake_model(clients, messages, schemas, session_id, backend, emit, should_stop):
        calls.append(len(messages))
        if len(calls) == 1:
            return {
                "content": "yarım cevap", "tool_calls": [], "finish_reason": "length",
                "usage": main.ZERO_USAGE,
            }, backend
        assert "max_tokens" in str(messages[-1]["content"])
        return {
            "content": "tam cevap", "tool_calls": [], "finish_reason": "stop",
            "usage": main.ZERO_USAGE,
        }, backend

    monkeypatch.setattr(main, "_call_model_with_retries", fake_model)
    service = CapabilityService(tmp_path)
    try:
        report = await main.run_agent_with_callback(
            "Kısa bir sonuç üret", lambda event: None,
            {"requested_backend": None, "should_stop": lambda: False,
             "state_file": str(tmp_path / "memory.json"), "history": [], "integrations": service},
            {"opencode": object()},
        )
    finally:
        await service.close()
    assert report["success"] and report["outcome"] == "tam cevap"
    assert report["metrics"]["turns"] == 2


@pytest.mark.asyncio
async def test_chrome_action_turns_end_with_automatic_observation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Açık Chrome yolunda eylem turu otomatik gözlemle biter; model ayrı ekran turu harcamaz."""
    shots: List[str] = []

    def fake_screenshot(self: Toolbox, filename: str) -> str:
        shots.append(filename)
        Image.new("RGB", (8, 8), "white").save(filename)
        return "kaydedildi"

    monkeypatch.setattr(Toolbox, "chrome_active_tab", lambda self, url: f"Görünür Chrome sekmesi: {url}")
    monkeypatch.setattr(Toolbox, "cua_click_point", lambda self, point: f"{point} tıklandı")
    monkeypatch.setattr(Toolbox, "take_screenshot", fake_screenshot)
    own_shot = str(tmp_path / "model.png")
    plan = [
        [("chrome_active_tab", {"url": "https://ornek.test/"})],
        [("cua_click_point", {"point": [1, 2]}), ("take_screenshot", {"filename": own_shot}),
         ("cua_click_point", {"point": [3, 4]})],
        [("cua_click_point", {"point": [5, 6]}), ("take_screenshot", {"filename": own_shot})],
    ]
    seen: List[list] = []

    async def fake_model(clients, messages, schemas, session_id, backend, emit, should_stop):
        seen.append(messages)
        if len(seen) > 1:
            # Önceki turun son gözlemi görüntü olarak modele gelmiş olmalı
            assert messages[-1]["role"] == "user" and messages[-1]["content"][1]["type"] == "image_url"
        if len(seen) > len(plan):
            return {"content": "TAMAM", "tool_calls": [], "finish_reason": "stop", "usage": main.ZERO_USAGE}, backend
        calls = [{"id": f"c{len(seen)}-{i}", "name": name, "arguments": json.dumps(arguments)}
                 for i, (name, arguments) in enumerate(plan[len(seen) - 1])]
        return {"content": "", "tool_calls": calls, "finish_reason": "tool_calls", "usage": main.ZERO_USAGE}, backend

    monkeypatch.setattr(main, "_call_model_with_retries", fake_model)
    events: List[AgentEvent] = []
    service = CapabilityService(tmp_path)
    try:
        report = await main.run_agent_with_callback(
            "Açık Chrome oturumunu kullanarak örnek sayfayı aç", events.append,
            {"requested_backend": None, "should_stop": lambda: False,
             "state_file": str(tmp_path / "memory.json"), "history": [], "integrations": service},
            {"opencode": object()})
    finally:
        await service.close()
    automatic = [event for event in events
                 if event["kind"] == "tool_started" and event["preview"] == main.AUTO_OBSERVATION_PREVIEW]
    assert report["success"] and report["metrics"]["turns"] == 4
    # 1. tur (gezinme) ve 2. tur (son tıklamadan sonra görüntü yok) otomatik gözlenir; 3. tur zaten gözlendi
    assert len(automatic) == 2
    assert len(shots) == 4
    assert not any(Path(path).exists() for path in shots if path != own_shot)

def test_action_sequence_accepts_json_list_and_reports_bad_step(monkeypatch: pytest.MonkeyPatch) -> None:
    """Çift kodlanan eylem listesi çalışır; serbest metin açık şema hatası verir."""
    monkeypatch.setattr(tools, "_require_accessibility", lambda: None)
    monkeypatch.setattr(tools, "current_geometry", lambda: {})
    monkeypatch.setattr(tools, "_run_action_step", lambda step, geometry: step["action"])
    toolbox = Toolbox()
    assert "click" in toolbox.run_action_sequence('[{"action":"click","point":[100,200]}]')
    with pytest.raises(ToolError) as error:
        toolbox.run_action_sequence(["click (100,200)"])
    assert error.value.code == "INVALID_ACTION_PARAMS"
    assert "nesne olmalı" in str(error.value)

def test_flat_chrome_actions_use_shared_coordinates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Düz Chrome araçları ortak koordinat dönüşümünü ve klavye katmanını kullanır."""
    calls = []
    monkeypatch.setattr(tools, "_require_accessibility", lambda: None)
    monkeypatch.setattr(tools, "current_geometry", lambda: {"model_width": 1280})
    monkeypatch.setattr(tools, "click_model_point", lambda x, y, button, geometry: calls.append((x, y, button)) or "tıklandı")
    monkeypatch.setattr(tools, "type_unicode_text", lambda value: calls.append(("type", value)))
    monkeypatch.setattr(tools, "press_key_spec", lambda key: calls.append(("key", key)) or "basıldı")
    toolbox = Toolbox()
    assert toolbox.cua_click_point([123, 456]) == "tıklandı"
    assert "Yazıldı" in toolbox.cua_type_text("Türkçe")
    assert toolbox.cua_press_key("enter") == "basıldı"
    assert calls == [(123, 456, "left"), ("type", "Türkçe"), ("key", "enter")]
    calls.clear()
    assert "Enter" in toolbox.cua_submit_text([10, 20], "senior developer")
    assert calls == [(10, 20, "left"), ("key", "cmd+a"), ("type", "senior developer"), ("key", "enter")]
    # Modelin ayrı x/y alanlarında ürettiği bozuk biçim açık hata verir, tıklamaz
    for bad in ([196, 175, 1], [[196, 175]], "196,175", [True, 3]):
        with pytest.raises(ToolError) as error:
            toolbox.cua_click_point(bad)
        assert error.value.code == "INVALID_POINT"
    assert len(calls) == 4


def test_task_ledger_extracts_state_block_and_is_bounded() -> None:
    content = "Kısa not.\nSTATE:\nFACTS: repo=ok\nREMAINING: report\n" + ("x" * 9000)
    ledger = main.extract_task_ledger("", content)
    assert ledger.startswith("STATE:")
    assert "repo=ok" in ledger
    assert len(ledger) <= main.TASK_LEDGER_LIMIT
    assert main.extract_task_ledger(ledger, "STATE içermeyen metin") == ledger


@pytest.mark.asyncio
async def test_successful_but_useless_turns_enter_conserve_then_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = tmp_path / "probe.txt"
    probe.write_text("ok", encoding="utf-8")
    calls = 0
    seen_messages: List[str] = []

    async def fake_model(clients, messages, schemas, session_id, backend, emit, should_stop):
        nonlocal calls
        calls += 1
        seen_messages.append("\n".join(
            str(message.get("content", "")) for message in messages if isinstance(message.get("content"), str)
        ))
        if calls <= 7:
            return {
                "content": "STATE:\nFACTS: probe=ok\nREMAINING: final cevap",
                "tool_calls": [{
                    "id": f"read-{calls}", "name": "read_file",
                    "arguments": json.dumps({"path": str(probe)}),
                }],
                "finish_reason": "tool_calls", "usage": main.ZERO_USAGE,
            }, backend
        return {
            "content": "tamam", "tool_calls": [], "finish_reason": "stop", "usage": main.ZERO_USAGE,
        }, backend

    monkeypatch.setattr(main, "_call_model_with_retries", fake_model)
    events: List[AgentEvent] = []
    service = CapabilityService(tmp_path)
    try:
        report = await main.run_agent_with_callback(
            "Probe dosyasını kullan ve sonunda tamam yaz", events.append,
            {"requested_backend": None, "should_stop": lambda: False,
             "state_file": str(tmp_path / "memory.json"), "history": [], "integrations": service},
            {"opencode": object()},
        )
    finally:
        await service.close()

    assert report["success"]
    assert report["metrics"]["fast_loop_replans"] == 1
    assert report["metrics"]["fast_loop_delivery_entries"] == 1
    assert report["metrics"]["fast_loop_transitions"] >= 2
    assert any("YENİDEN PLAN" in message for message in seen_messages)
    assert any("TESLİM MODU" in message for message in seen_messages)


@pytest.mark.asyncio
async def test_semantic_state_change_resets_stagnation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = tmp_path / "probe.txt"
    probe.write_text("ok", encoding="utf-8")
    states = [
        "STATE:\nFACTS: step=1\nREMAINING: next",
        "STATE:\nFACTS: step=1\nREMAINING: next",
        "STATE:\nFACTS: step=2\nREMAINING: final",
        "STATE:\nFACTS: step=2\nREMAINING: final",
    ]
    calls = 0

    async def fake_model(clients, messages, schemas, session_id, backend, emit, should_stop):
        nonlocal calls
        if calls < len(states):
            state_text = states[calls]
            calls += 1
            return {
                "content": state_text,
                "tool_calls": [{
                    "id": f"read-{calls}", "name": "read_file",
                    "arguments": json.dumps({"path": str(probe)}),
                }],
                "finish_reason": "tool_calls", "usage": main.ZERO_USAGE,
            }, backend
        return {
            "content": "tamam", "tool_calls": [], "finish_reason": "stop", "usage": main.ZERO_USAGE,
        }, backend

    monkeypatch.setattr(main, "_call_model_with_retries", fake_model)
    service = CapabilityService(tmp_path)
    try:
        report = await main.run_agent_with_callback(
            "Probe dosyasını iki aşamada kontrol et", lambda event: None,
            {"requested_backend": None, "should_stop": lambda: False,
             "state_file": str(tmp_path / "memory.json"), "history": [], "integrations": service},
            {"opencode": object()},
        )
    finally:
        await service.close()

    assert report["success"]
    assert report["metrics"]["fast_loop_replans"] == 0
    assert report["metrics"]["fast_loop_delivery_entries"] == 0
    assert report["metrics"]["semantic_progress_events"] >= 2
