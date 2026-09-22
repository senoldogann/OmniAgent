import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
from uuid import uuid4

import pytest
from openai import AsyncOpenAI

from config import config
from main import encode_image, execute_tool
from state_manager import (
    absorb_episode_lessons,
    check_for_lessons,
    clear_active_goal,
    distill_lesson_pairs,
    load_state,
    normalize_error_pattern,
    recall_similar_success,
    record_episode,
    save_state,
    set_active_goal,
)
from tools import ToolError, Toolbox, _is_sensitive_path


@pytest.mark.asyncio
async def test_api_connectivity() -> None:
    """İstenirse gerçek API erişimini sınar."""
    if os.environ.get("OMNI_LIVE_API_TEST") != "1":
        pytest.skip("Canlı API testi OMNI_LIVE_API_TEST=1 ile etkinleştirilir.")
    api_key: str | None = config["API_KEY"]
    if not api_key:
        pytest.skip("OpenCode API anahtarı bulunamadı.")
    async with AsyncOpenAI(api_key=api_key, base_url=config["BASE_URL"]) as client:
        response = await client.chat.completions.create(
            model=config["MODEL"],
            messages=[{"role": "user", "content": "Reply with OK"}],
            max_tokens=10,
            extra_headers={"x-opencode-session": str(uuid4())},
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
    """Dosya yazımını ve geri okumayı sınar."""
    toolbox: Toolbox = Toolbox()
    path: Path = tmp_path / "omni_test_file.txt"
    content: str = "Türkçe içerik"
    toolbox.write_file(str(path), content)
    assert toolbox.read_file(str(path)) == content


def test_memory_roundtrip(tmp_path: Path) -> None:
    """Belleğin atomik kaydını ve hedef temizliğini sınar."""
    path: Path = tmp_path / "memory.json"
    state = set_active_goal(load_state(str(path)), "örnek hedef")
    save_state(str(path), state)
    assert load_state(str(path))["active_goals"] == ["örnek hedef"]
    assert path.stat().st_mode & 0o777 == 0o600
    save_state(str(path), clear_active_goal(state))
    assert load_state(str(path))["active_goals"] == []


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
    """Araç çağrısı hatasını başarıdan ayırır."""
    call = SimpleNamespace(
        id="call-1",
        function=SimpleNamespace(name="execute_shell", arguments='{"command":"exit 7","use_sudo":false}'),
    )
    result = await execute_tool(call, Toolbox(), {})
    assert result["tool_call_id"] == "call-1"
    assert result["ok"] is False
    assert result["error_type"] == "ToolError"


def test_vision_capture(tmp_path: Path) -> None:
    """Gerçek ekran görüntüsünün kaydedildiğini sınar."""
    toolbox: Toolbox = Toolbox()
    path: Path = tmp_path / "vision.png"
    toolbox.take_screenshot(str(path))
    assert path.exists()
    assert len(encode_image(str(path))) > 0


def test_normalize_error_pattern_strips_volatile_parts() -> None:
    """Hata desenindeki yol/sayı/uuid gibi kararsız parçaların atıldığını sınar."""
    a = normalize_error_pattern("SHELL_EXIT: /Users/dogan/a.py satır 4096 bulunamadı")
    b = normalize_error_pattern("SHELL_EXIT: /Users/ali/b.py satır 8192 bulunamadı")
    assert a == b


def test_distill_lesson_pairs_from_recovery() -> None:
    """Başarısız olup sonra toparlanan araçtan ders damıtıldığını sınar."""
    steps = [
        {"tool": "execute_shell", "result": {"ok": False, "error_type": "ToolError", "error": "kabuk komutu başarısız: exit=2"}},
        {"tool": "web_search", "result": {"ok": True, "result": "sonuçlar"}},
        {"tool": "execute_shell", "result": {"ok": True, "result": "STDOUT: tamam"}},
    ]
    pairs = distill_lesson_pairs(steps)
    assert len(pairs) == 1
    pattern, fix = pairs[0]
    assert "execute_shell" in fix
    assert "kabuk" in pattern


def test_distill_lesson_pairs_without_recovery_is_empty() -> None:
    """Hiç toparlanmayan hataların ders üretmediğini sınar (çözüm kanıtı yok)."""
    steps = [{"tool": "find_and_click", "result": {"ok": False, "error_type": "ToolError", "error": "hedef yok"}}]
    assert distill_lesson_pairs(steps) == []


def test_absorb_episode_lessons_persists_and_recalls() -> None:
    """Damıtılan dersin belleğe yazıldığını ve hata anında geri çağrıldığını sınar."""
    state = load_state(str(Path("/tmp/omni_lesson_test.json")))
    steps = [
        {"tool": "web_search", "result": {"ok": False, "error_type": "ToolError", "error": "paket import edilemedi"}},
        {"tool": "web_search", "result": {"ok": True, "result": "5 sonuç"}},
    ]
    state = absorb_episode_lessons(state, steps, success=True)
    assert state["lessons_learned"]
    hint = check_for_lessons(state, "ToolError: paket import edilemedi")
    assert hint is not None and "web_search" in hint


def test_recall_similar_success_uses_token_overlap() -> None:
    """Benzer hedefli başarılı epizotlardan rota özeti döndüğünü sınar."""
    state = load_state(str(Path("/tmp/omni_route_test.json")))
    state = record_episode(
        state,
        "hava durumu raporu hazırla ve kaydet",
        [{"tool": "web_search", "result": {"ok": True, "result": "sonuç"}}],
        "Tamamlandı",
        True,
    )
    recalled = recall_similar_success(state, "hava durumu raporu hazırla", limit=3)
    assert recalled is not None
    assert "web_search" in recalled


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


def test_sensitive_path_macos_firmlink_false_positive() -> None:
    """macOS'ta /System/Volumes/Data altına çözülen kullanıcı yollarının bloklanmadığını sınar."""
    assert not _is_sensitive_path(Path("/tmp/omni_yazilabilir.txt"))
    assert not _is_sensitive_path(Path("/home/kullanici/notlar.txt"))
    assert _is_sensitive_path(Path("/System/Library/CoreServices/test.txt"))
    assert _is_sensitive_path(Path("/etc/hosts"))
    assert _is_sensitive_path(Path.home() / ".ssh" / "id_rsa")
