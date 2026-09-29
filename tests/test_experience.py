"""Deneyim belleği: doğrulanmış kurtarmadan ders çıkarma ve yalnız aynı hatada hatırlatma."""
import json
import stat
from pathlib import Path
from typing import Any, Dict, List

import pytest

from omniagent.memory import experience
from omniagent.app import agent as main
from omniagent.integrations.capabilities import CapabilityService
from omniagent.core import text_norm
from omniagent.core.events import AgentEvent
from omniagent.tools import browser

TOOL_SCRIPT: str = """#!/bin/sh
case "$*" in
  "ozet kuzey --birim=adet") echo "OZET: 42";;
  "--help") echo "kullanim: veri-araci ozet <bolge> --birim=adet";;
  *) echo "hata: E17 birim eksik" >&2; exit 2;;
esac
"""


def _install_tool(directory: Path) -> Path:
    """Hata mesajı çözümü söylemeyen yerel bir komut satırı aracı kurar."""
    directory.mkdir(parents=True)
    tool: Path = directory / "veri-araci"
    tool.write_text(TOOL_SCRIPT, encoding="utf-8")
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    return tool


def _shell_turn(call_id: str, command: str) -> Dict[str, Any]:
    return {
        "content": "STATE: veri-araci deneniyor",
        "tool_calls": [{"id": call_id, "name": "execute_shell",
                        "arguments": json.dumps({"command": command, "use_sudo": False, "timeout_seconds": None})}],
        "finish_reason": "tool_calls", "usage": main.ZERO_USAGE,
    }


def _final(text: str) -> Dict[str, Any]:
    return {"content": text, "tool_calls": [], "finish_reason": "stop", "usage": main.ZERO_USAGE}


def _last_tool_messages(messages: List[Dict[str, Any]]) -> str:
    """Son asistan turundan sonra modele giden araç sonuçları."""
    tail: List[str] = []
    for message in reversed(messages):
        if message.get("role") == "assistant":
            break
        if message.get("role") == "tool":
            tail.append(str(message["content"]))
    return "\n".join(reversed(tail))


async def _run(goal: str, script: List[Dict[str, Any]], seen: List[str], tmp_path: Path,
               events: List[AgentEvent]) -> main.RunReport:
    """Modeli betikli yanıtlarla taklit eder; araçlar (kabuk, dosya) gerçekten çalışır."""
    turns = iter(script)

    async def fake_model(clients, messages, schemas, session_id, backend, emit, should_stop):
        seen.append(_last_tool_messages(messages))
        return next(turns), backend

    service = CapabilityService(tmp_path / "entegrasyon")
    try:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(main, "_call_model_with_retries", fake_model)
            return await main.run_agent_with_callback(
                goal, events.append,
                {"requested_backend": "opencode", "should_stop": lambda: False,
                 "state_file": str(tmp_path / "memory.json"), "history": [], "integrations": service,
                 "experience_file": str(tmp_path / "deneyim.json")},
                {"opencode": object()},
            )
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_verified_recovery_is_learned_and_reminded_only_on_same_failure(tmp_path: Path) -> None:
    first_tool: Path = _install_tool(tmp_path / "kosu1")
    seen: List[str] = []
    events: List[AgentEvent] = []
    first = await _run("veri-araci ile kuzey özetini bul", [
        _shell_turn("a1", f"{first_tool} ozet kuzey"),
        _shell_turn("a2", f"{first_tool} ozet kuzey"),
        _shell_turn("a3", f"{first_tool} --help"),
        _shell_turn("a4", f"{first_tool} ozet kuzey --birim=adet"),
        _final("OZET: 42"),
    ], seen, tmp_path, events)

    assert first["success"]
    # Aynı çağrı ikinci kez çalıştırılmadan engellenir ve model farklı adıma yönlendirilir.
    assert "TEKRARLANAN HATA" not in seen[1] and "RepeatedFailedCall" in seen[2]
    lessons = experience.load_experience(str(tmp_path / "deneyim.json"))["lessons"]
    assert len(lessons) == 1
    # Keşif adımı (--help) düzeltme sayılmaz; gerçek değişiklik öğrenilir.
    assert lessons[0]["fixed_call"].endswith("ozet kuzey --birim=adet")
    assert first["metrics"]["experience_candidates"] == 1

    second_tool: Path = _install_tool(tmp_path / "kosu2")
    seen = []
    second = await _run("veri-araci ile kuzey özetini tekrar bul", [
        _shell_turn("b1", f"{second_tool} ozet kuzey"),
        _shell_turn("b2", f"{second_tool} ozet kuzey --birim=adet"),
        _final("OZET: 42"),
    ], seen, tmp_path, events)

    assert second["success"]
    assert "DENEYİM BELLEĞİ" in seen[1] and "--birim=adet" in seen[1]
    assert second["metrics"]["experience_hints"] == 1
    assert any(event["kind"] == "notice" and "Deneyim belleği" in event["text"] for event in events)
    updated = experience.load_experience(str(tmp_path / "deneyim.json"))["lessons"]
    assert (updated[0]["uses"], updated[0]["helped"]) == (1, 1)

    seen = []
    third = await _run("dosyayı oku", [
        {"content": "", "tool_calls": [{"id": "c1", "name": "read_file",
                                        "arguments": json.dumps({"path": str(tmp_path / "yok.txt")})}],
         "finish_reason": "tool_calls", "usage": main.ZERO_USAGE},
        _final("dosya yok"),
    ], seen, tmp_path, events)
    # Farklı araç/hata: ders hatırlatılmaz, başarılı yol etkilenmez.
    assert third["metrics"]["experience_hints"] == 0 and "DENEYİM BELLEĞİ" not in seen[1]


def test_failed_task_does_not_learn_and_unhelpful_lesson_is_pruned() -> None:
    def shell(command: str) -> str:
        return json.dumps({"command": command, "use_sudo": False, "timeout_seconds": None})

    failure: str = "ToolError: Kabuk komutu başarısız: çıkış=2, stdout=, stderr=hata: E17"
    tracker = experience.new_tracker()
    tracker, _ = experience.observe_result(experience.empty_state(), tracker, "execute_shell",
                                           shell("/a/veri-araci ozet"), False, failure, 1)
    tracker, _ = experience.observe_result(experience.empty_state(), tracker, "execute_shell",
                                           shell("/a/veri-araci ozet --birim=adet"), True, "ok", 2)
    assert experience.finish_task(experience.empty_state(), tracker, False, "t0") == experience.empty_state()
    learned = experience.finish_task(experience.empty_state(), tracker, True, "t0")
    assert len(learned["lessons"]) == 1

    state = learned
    for attempt in range(experience.PRUNE_MIN_USES):
        task = experience.new_tracker()
        task, observed = experience.observe_result(state, task, "execute_shell",
                                                   shell("/b/veri-araci ozet"), False, failure, 1)
        assert observed["lesson_id"] is not None
        task, _ = experience.observe_result(state, task, "execute_shell",
                                            shell("/b/veri-araci ozet --birim=adet"), False, failure, 2)
        state = experience.finish_task(state, task, False, f"t{attempt + 1}")
    # Üç hatırlatmada da işe yaramayan ders silinir.
    assert state["lessons"] == []


def test_lesson_file_masks_secrets(tmp_path: Path) -> None:
    command: str = "curl -H 'Authorization: Bearer abcdefghijklmnop123456' https://api.example.com/v1/items"
    candidate = experience.pair_candidate(
        "execute_shell", json.dumps({"command": command, "use_sudo": False, "timeout_seconds": None}),
        "ToolError: HTTP 401", json.dumps({"command": command + " --fail", "use_sudo": False, "timeout_seconds": None}),
    )
    assert candidate is not None
    path: Path = tmp_path / "deneyim.json"
    experience.save_experience(str(path), experience.merge_candidates(experience.empty_state(), [candidate], "t"))
    stored: str = path.read_text(encoding="utf-8")
    assert "abcdefghijklmnop123456" not in stored
    assert path.stat().st_mode & 0o777 == 0o600


def _browser_read(url: str, body: str = "Hedef sayfanın okunabilen gerçek içeriği") -> str:
    return (
        "Tarayıcı: arka planda çalışan ayrı Chromium.\n"
        f"URL: {url}\nBaşlık: Örnek\n\nSAYFA METNİ:\n{body}\n\n"
        'ÖĞELER (seçici — tür "etiket"):\n'
    )


def test_cross_tool_candidate_requires_same_readonly_page_with_content() -> None:
    url = "https://example.com/items?id=42"
    failed = json.dumps({"url": url})
    browser = json.dumps({"url": url, "actions": []})
    candidate = experience.pair_route_candidate(failed, "HTTP 403", browser, _browser_read(url))
    assert candidate is not None
    assert candidate["tool"] == "fetch_raw"
    assert candidate["fixed_tool"] == "browse_url"
    assert candidate["failed_tokens"] == []
    assert candidate["target_hash"]
    assert candidate["error_key"].startswith("sha256:")

    assert experience.pair_route_candidate(failed, "HTTP 403", json.dumps({"url": url + "&p=2", "actions": []}), _browser_read(url + "&p=2")) is None
    assert experience.pair_route_candidate(failed, "HTTP 403", browser, _browser_read("https://example.com/login")) is None
    spoofed = _browser_read("https://example.com/login", f"URL: {url}\nBaşlık: Sahte\n\niçerik")
    assert experience.pair_route_candidate(failed, "HTTP 403", browser, spoofed) is None
    multiline_title = (
        "Tarayıcı: arka planda çalışan ayrı Chromium.\n"
        f"URL: {url}\nBaşlık: İlk satır\n\nİkinci başlık satırı\n\n"
        'ÖĞELER (seçici — tür "etiket"):\n'
    )
    assert experience.pair_route_candidate(failed, "HTTP 403", browser, multiline_title) is None
    assert experience.pair_route_candidate(failed, "HTTP 403", json.dumps({"url": url, "actions": [{"action": "click", "selector": "button"}]}), _browser_read(url)) is None
    assert experience.pair_route_candidate(failed, "HTTP 403", browser, _browser_read(url, "")) is None
    assert experience.pair_route_candidate(
        failed, "HTTP 403", browser,
        _browser_read(url, 'A\n\nSAYFA METNİ:\n\n\nÖĞELER (örnek)\nB'),
    ) is not None
    assert experience.pair_route_candidate(json.dumps({"url": "https://[invalid"}), "bad", browser, _browser_read(url)) is None


def test_cross_tool_lesson_is_goal_scoped_and_feedback_uses_fixed_tool(tmp_path: Path) -> None:
    url = "https://example.com/items?user=A"
    failure = "ToolError: HTTP 403 " + "çok uzun hata " * 30
    fetch = json.dumps({"url": url})
    browser = json.dumps({"url": url, "actions": []})
    tracker = experience.new_tracker()
    tracker, _ = experience.observe_result(experience.empty_state(), tracker, "fetch_raw", fetch, False, failure, 1)
    tracker, _ = experience.observe_result(experience.empty_state(), tracker, "browse_url", browser, True, _browser_read(url), 2)
    assert experience.finish_task(experience.empty_state(), tracker, False, "t0")["lessons"] == []
    learned = experience.finish_task(experience.empty_state(), tracker, True, "t0")
    assert len(learned["lessons"]) == 1
    assert learned["lessons"][0]["fixed_tool"] == "browse_url"

    different = experience.new_tracker()
    different, unrelated = experience.observe_result(learned, different, "fetch_raw", json.dumps({"url": "https://example.com/items?user=B"}), False, failure, 1)
    assert unrelated["lesson_id"] is None

    same = experience.new_tracker()
    same, observed = experience.observe_result(learned, same, "fetch_raw", fetch, False, failure, 1)
    assert observed["lesson_id"] is not None
    assert "browse_url" in "\n".join(observed["notes"])
    same, _ = experience.observe_result(learned, same, "browse_url", browser, True, _browser_read(url), 2)
    updated = experience.finish_task(learned, same, True, "t1")
    assert (updated["lessons"][0]["uses"], updated["lessons"][0]["helped"]) == (1, 1)

    wrong = experience.new_tracker()
    wrong, _ = experience.observe_result(learned, wrong, "fetch_raw", fetch, False, failure, 1)
    wrong, _ = experience.observe_result(
        learned, wrong, "browse_url", json.dumps({"url": "https://example.com/items?user=B", "actions": []}),
        True, _browser_read("https://example.com/items?user=B"), 2,
    )
    wrong, _ = experience.observe_result(learned, wrong, "browse_url", browser, True, _browser_read(url), 3)
    unhelpful = experience.finish_task(learned, wrong, False, "t2")
    assert (unhelpful["lessons"][0]["uses"], unhelpful["lessons"][0]["helped"]) == (1, 0)

    mutating = experience.new_tracker()
    mutating, _ = experience.observe_result(learned, mutating, "fetch_raw", fetch, False, failure, 1)
    mutating, _ = experience.observe_result(
        learned, mutating, "browse_url",
        json.dumps({"url": url, "actions": [{"action": "click", "selector": "button"}]}),
        True, _browser_read(url), 2,
    )
    assert experience.finish_task(learned, mutating, False, "t3")["lessons"][0]["helped"] == 0

    path = tmp_path / "deneyim.json"
    experience.save_experience(str(path), updated)
    persisted = path.read_text(encoding="utf-8")
    assert "user=A" not in persisted
    assert "çok uzun hata" not in persisted


def test_cross_tool_lesson_persists_no_url_components(tmp_path: Path) -> None:
    url = "https://alice:secret-userinfo@example.com/reset/secret-path?session=secret-query"
    candidate = experience.pair_route_candidate(
        json.dumps({"url": url}), "ToolError: fetch failed for " + url,
        json.dumps({"url": url, "actions": []}), _browser_read(url),
    )
    assert candidate is not None
    path = tmp_path / "deneyim.json"
    experience.save_experience(str(path), experience.merge_candidates(experience.empty_state(), [candidate], "t"))
    persisted = path.read_text(encoding="utf-8")
    for secret in ("secret-userinfo", "secret-path", "secret-query", "alice", "example.com"):
        assert secret not in persisted


def test_legacy_lesson_id_and_counters_survive_merge(tmp_path: Path) -> None:
    failed = json.dumps({"command": "veri-araci ozet kuzey"})
    fixed = json.dumps({"command": "veri-araci ozet kuzey --birim=adet"})
    candidate = experience.pair_candidate("execute_shell", failed, "E17", fixed)
    assert candidate is not None
    original = experience.merge_candidates(experience.empty_state(), [candidate], "t0")
    legacy = {key: value for key, value in original["lessons"][0].items() if key not in ("fixed_tool", "target_hash")}
    legacy["uses"] = 2
    legacy["helped"] = 1
    path = tmp_path / "eski-deneyim.json"
    path.write_text(json.dumps({"lessons": [legacy]}), encoding="utf-8")
    loaded = experience.load_experience(str(path))["lessons"][0]
    merged = experience.merge_candidates({"lessons": [loaded]}, [candidate], "t1")
    assert len(merged["lessons"]) == 1
    assert merged["lessons"][0]["id"] == original["lessons"][0]["id"]
    assert (merged["lessons"][0]["uses"], merged["lessons"][0]["helped"]) == (2, 1)


def test_same_tool_and_cross_tool_lessons_do_not_overwrite_each_other() -> None:
    url = "https://example.com/items?id=42"
    failure = "HTTP 403"
    same = experience.pair_candidate(
        "fetch_raw", json.dumps({"url": url}), failure,
        json.dumps({"url": url + "&format=json"}),
    )
    route = experience.pair_route_candidate(
        json.dumps({"url": url}), failure,
        json.dumps({"url": url, "actions": []}), _browser_read(url),
    )
    assert same is not None and route is not None
    state = experience.merge_candidates(experience.empty_state(), [same, route], "t0")
    assert len(state["lessons"]) == 2
    assert len({item["id"] for item in state["lessons"]}) == 2
    assert {item["fixed_tool"] for item in state["lessons"]} == {"fetch_raw", "browse_url"}


@pytest.mark.parametrize("command, secret", [
    ("mysql --password hunter2Zx9 -h db.example.com", "hunter2Zx9"),
    ("curl -u admin:s3cr3tPass https://api.example.com/v1/items", "s3cr3tPass"),
    ("deploy --token=ghp_A1b2C3d4E5f6G7h8i9J0 --env prod", "ghp_A1b2C3d4E5f6G7h8i9J0"),
    ("psql postgres://kullanici:gizliSifre99@db.example.com/uygulama", "gizliSifre99"),
    ("export DB_PASSWORD=Xk29qLm0pQ && ./calistir", "Xk29qLm0pQ"),
])
def test_command_line_secrets_reach_neither_the_tokens_nor_the_lesson_file(command: str, secret: str, tmp_path: Path) -> None:
    """
    Ders dosyasına sır yazılmaz: scrub_secrets projedeki ortak süzgeci (mask_sensitive_text) kullanır. Eskiden ayrı, daha zayıf
    üçüncü bir maskeleyici vardı ve 'mysql --password hunter2Zx9' belirteç olarak ders dosyasına sızıyordu.
    """
    failed = json.dumps({"command": command, "use_sudo": False, "timeout_seconds": None})
    fixed = json.dumps({"command": command + " --fail", "use_sudo": False, "timeout_seconds": None})
    assert secret.casefold() not in " ".join(experience.argument_tokens(failed))
    assert secret not in experience.display_call(failed) and secret not in experience.scrub_secrets(command)
    candidate = experience.pair_candidate("execute_shell", failed, f"ToolError: {command} başarısız", fixed)
    assert candidate is not None
    path: Path = tmp_path / "deneyim.json"
    experience.save_experience(str(path), experience.merge_candidates(experience.empty_state(), [candidate], "t"))
    assert secret not in path.read_text(encoding="utf-8") and secret.casefold() not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("status, skipped", [
    (401, True), (403, True), (429, True), (451, True), (404, False), (500, False), (503, False),
])
def test_only_access_control_statuses_are_excluded_from_learning(status: int, skipped: bool) -> None:
    """Durum kodu tools/browser.py ile ORTAK çapadan ('returned error: <kod>') okunur; erişim denetimi olmayan durumlar öğrenilir."""
    url = "https://example.com/items"
    failure = f"HTTP başarısız: url={url}, çıkış=22, stderr=curl: (22) The requested URL returned error: {status}"
    tracker, observed = experience.observe_result(
        experience.empty_state(), experience.new_tracker(), "fetch_raw", json.dumps({"url": url}), False, failure, 1,
    )
    assert observed == {"notes": [], "lesson_id": None}
    assert (tracker["open_failures"].get("fetch_raw", []) == []) is skipped
    assert browser._curl_http_status(failure) == status
    assert text_norm.curl_http_statuses(f"{failure} sonra returned error: 4031 ve returned error: 500") == [status, 500]


def test_access_denied_status_never_teaches_a_route_around_it() -> None:
    """
    403/429 gibi erişim denetimi hatası, aynı adreste sonradan tarayıcı okuması başarılı olsa da "başka araç dene"
    dersine dönüşmez: sınıflandırılamayan engel sayfası da engeli aşmayı kalıcı belleğe öğretmemeli.
    """
    url = "https://example.com/items"
    failure = f"HTTP başarısız: url={url}, çıkış=22, stderr=curl: (22) The requested URL returned error: 403"
    tracker = experience.new_tracker()
    tracker, observed = experience.observe_result(
        experience.empty_state(), tracker, "fetch_raw", json.dumps({"url": url}), False, failure, 1,
    )
    assert observed == {"notes": [], "lesson_id": None}
    tracker, _ = experience.observe_result(
        experience.empty_state(), tracker, "browse_url", json.dumps({"url": url, "actions": []}), True,
        _browser_read(url), 2,
    )
    assert experience.finish_task(experience.empty_state(), tracker, True, "t0")["lessons"] == []
