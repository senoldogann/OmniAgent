"""Telegram'dan bakım: git ile güncelleme, bağımlılık eşitleme, durum raporu ve yeniden başlatma."""
import asyncio
import re
import subprocess
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from omniagent.core import schedule
from omniagent.integrations import maintenance, telegram


def git(cwd: Path, *arguments: str) -> str:
    return subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def commit(repo: Path, name: str, content: str, message: str) -> str:
    (repo / name).write_text(content)
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", message)
    git(repo, "push", "-q", "origin", "HEAD:main")
    return git(repo, "rev-parse", "HEAD")


def subjects(commits: List[str]) -> List[str]:
    return [line.split(" ", 1)[1] for line in commits]


@pytest.fixture
def repos(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Tuple[Path, Path]:
    """Yerel 'origin', oraya iten yazar kopyası ve köprünün güncelleyeceği çalışma kopyası."""
    config = tmp_path / "gitconfig"
    config.write_text("[user]\n\tname = Test\n\temail = test@example.com\n[commit]\n\tgpgsign = false\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    origin, author, work = tmp_path / "origin.git", tmp_path / "author", tmp_path / "work"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    git(tmp_path, "init", "-q", "-b", "main", str(author))
    git(author, "remote", "add", "origin", str(origin))
    commit(author, "README.md", "ilk\n", "ilk sürüm")
    git(tmp_path, "clone", "-q", str(origin), str(work))
    return author, work


def test_pull_reports_new_commits_and_dependency_changes(repos: Tuple[Path, Path]) -> None:
    author, work = repos
    current = maintenance.pull_updates(work)
    assert current["ok"] and not current["changed"] and current["message"].startswith("Kod zaten güncel")

    pushed = commit(author, "README.md", "iki\n", "belgeyi güncelle")
    result = maintenance.pull_updates(work)
    assert result["ok"] and result["changed"] and not result["dependencies_changed"]
    assert result["after"] == pushed == git(work, "rev-parse", "HEAD")
    assert subjects(result["commits"]) == ["belgeyi güncelle"] and "1 yeni commit" in result["message"]

    commit(author, "uv.lock", "kilit\n", "bağımlılık ekle")
    assert maintenance.pull_updates(work)["dependencies_changed"]
    assert re.fullmatch(r"[0-9a-f]{7,} · \d{2}\.\d{2}\.\d{4} \d{2}:\d{2}", maintenance.source_version(work))


def test_code_pulled_elsewhere_counts_as_new_for_the_running_bridge(repos: Tuple[Path, Path]) -> None:
    author, work = repos
    running = maintenance.head_commit(work)
    commit(author, "README.md", "iki\n", "başka yoldan çekilen")
    git(work, "pull", "-q", "--ff-only")  # ör. başka bir ajan kodu çekti ama köprü yeniden başlamadı
    assert not maintenance.pull_updates(work)["changed"]
    result = maintenance.pull_updates(work, since=running)
    assert result["changed"] and result["before"] == running
    assert subjects(result["commits"]) == ["başka yoldan çekilen"]


def test_diverged_tree_and_plain_directory_are_reported_untouched(repos: Tuple[Path, Path], tmp_path: Path) -> None:
    author, work = repos
    commit(author, "README.md", "uzak\n", "uzak değişiklik")
    (work / "yerel.txt").write_text("yerel\n")
    git(work, "add", "yerel.txt")
    git(work, "commit", "-q", "-m", "yerel değişiklik")
    local = git(work, "rev-parse", "HEAD")
    result = maintenance.pull_updates(work)
    assert not result["ok"] and not result["changed"] and result["message"].startswith("git pull başarısız")
    assert git(work, "rev-parse", "HEAD") == local

    plain = tmp_path / "plain"
    plain.mkdir()
    assert "git deposu değil" in maintenance.pull_updates(plain)["message"]
    assert maintenance.head_commit(plain) is None and maintenance.source_version(plain) == "bilinmiyor"


def test_dependency_sync_runs_frozen_uv_without_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-gizli")
    calls: List[Dict[str, Any]] = []
    outcomes = iter([(0, ""), (2, "error: kilit dosyası güncel değil")])

    def runner(command: List[str], **options: Any) -> "subprocess.CompletedProcess[str]":
        calls.append({"command": command, **options})
        code, stderr = next(outcomes)
        return subprocess.CompletedProcess(command, code, "", stderr)

    assert maintenance.sync_dependencies(tmp_path, runner, uv="/opt/uv") is None
    assert calls[0]["command"] == ["/opt/uv", "sync", "--frozen"] and calls[0]["cwd"] == str(tmp_path)
    assert "OPENAI_API_KEY" not in calls[0]["env"] and calls[0]["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert "kilit dosyası güncel değil" in str(maintenance.sync_dependencies(tmp_path, runner, uv="/opt/uv"))
    monkeypatch.setattr(maintenance, "find_uv", lambda: None)
    assert "uv bulunamadı" in str(maintenance.sync_dependencies(tmp_path, runner))


def test_doctor_lines_name_missing_permissions_and_the_python_to_allow() -> None:
    facts: maintenance.DoctorFacts = {
        "version": "abc1234 · 26.09.2026 09:15", "stale": True, "service": "launchd hizmeti",
        "python": "/Users/me/OmniAgent/.venv/bin/python", "screen_capture": True, "accessibility": False,
        "models": ["ollama-cloud", "openai"], "voice": False, "schedules": 2,
    }
    lines = maintenance.doctor_lines(facts)
    assert lines[0] == "Sürüm: abc1234 · 26.09.2026 09:15 (çalışan köprü daha eski kodla; yüklemek için /restart)"
    assert "✓ Ekran kaydı izni" in lines and "✗ Erişilebilirlik izni yok: fare/klavye olayları düşer" in lines
    assert "Hazır modeller: ollama-cloud, openai" in lines and "✗ Sesli komut kapalı: OpenAI API anahtarı yok" in lines
    assert lines[-1].endswith("/Users/me/OmniAgent/.venv/bin/python")
    granted = maintenance.doctor_lines({**facts, "stale": False, "accessibility": True, "schedules": None})
    assert granted[0] == "Sürüm: abc1234 · 26.09.2026 09:15" and "Planlanmış görev: okunamadı" in granted
    assert not any(".venv/bin/python" in line for line in granted)


# --- Telegram komutları ---

class MaintenanceAPI:
    def __init__(self, updates: Optional[List[Dict[str, Any]]] = None) -> None:
        self.sent: List[str] = []
        self.updates = list(updates or [])
        self.closed = False

    async def send(self, chat_id: int, text: str) -> int:
        assert chat_id == 123
        self.sent.append(text)
        return len(self.sent)

    async def call(self, method: str, payload: Dict[str, Any]) -> Any:
        if self.updates:
            batch, self.updates = self.updates, []
            return batch
        raise telegram.TelegramError("getUpdates: HTTP 401", 401)

    async def close(self) -> None:
        self.closed = True


def message(text: str) -> Dict[str, Any]:
    return {"message": {"chat": {"id": 123, "type": "private"}, "from": {"id": 456}, "text": text}}


def new_bridge(api: MaintenanceAPI, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> telegram.TelegramBridge:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    return telegram.TelegramBridge(api, {"chat_id": 123, "user_id": 456})  # type: ignore[arg-type]


def update_result(**changes: Any) -> maintenance.UpdateResult:
    result: Dict[str, Any] = {
        "ok": True, "changed": True, "before": "a" * 40, "after": "b" * 40,
        "commits": ["bbbbbbb Telegram'dan bakım"], "dependencies_changed": False,
        "message": "aaaaaaa → bbbbbbb: 1 yeni commit alındı.",
    }
    return {**result, **changes}  # type: ignore[return-value]


@pytest.mark.asyncio
async def test_update_pulls_relative_to_running_code_and_requests_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    api = MaintenanceAPI()
    bridge = new_bridge(api, tmp_path, monkeypatch)
    bridge.loaded_commit = "a" * 40
    seen: List[Optional[str]] = []

    def pull(root: Path, since: Optional[str] = None) -> maintenance.UpdateResult:
        seen.append(since)
        assert bridge.maintenance  # bu sırada zamanlayıcı görev başlatmaz
        return update_result()

    monkeypatch.setattr(telegram, "pull_updates", pull)
    monkeypatch.setattr(telegram, "sync_dependencies", lambda root: pytest.fail("bağımlılık değişmedi"))
    with pytest.raises(telegram.RestartRequested):
        await bridge.handle(message("/update"))
    assert seen == ["a" * 40] and not bridge.maintenance
    assert "bbbbbbb Telegram'dan bakım" in api.sent[-1] and api.sent[-1].endswith("Yeniden başlatılıyor…")


@pytest.mark.asyncio
async def test_update_without_new_code_or_with_failed_pull_keeps_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    api = MaintenanceAPI()
    bridge = new_bridge(api, tmp_path, monkeypatch)
    results = iter([
        update_result(changed=False, commits=[], message="Kod zaten güncel (bbbbbbb)."),
        update_result(ok=False, changed=False, message="git pull başarısız: Not possible to fast-forward"),
    ])
    monkeypatch.setattr(telegram, "pull_updates", lambda root, since=None: next(results))
    await bridge.handle(message("/update"))
    assert api.sent[-1] == "Kod zaten güncel (bbbbbbb)."
    await bridge.handle(message("/update"))
    assert api.sent[-1].startswith("git pull başarısız") and not bridge.maintenance


@pytest.mark.asyncio
async def test_update_syncs_changed_dependencies_before_restarting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    api = MaintenanceAPI()
    bridge = new_bridge(api, tmp_path, monkeypatch)
    monkeypatch.setattr(telegram, "pull_updates", lambda root, since=None: update_result(dependencies_changed=True))
    outcomes = iter(["uv sync başarısız: ağ yok", None])
    monkeypatch.setattr(telegram, "sync_dependencies", lambda root: next(outcomes))
    await bridge.handle(message("/update"))
    assert "uv sync başarısız: ağ yok" in api.sent[-1] and "eski kodla" in api.sent[-1]
    with pytest.raises(telegram.RestartRequested):
        await bridge.handle(message("/update"))
    assert "Bağımlılıklar eşitlendi." in api.sent[-1]


@pytest.mark.asyncio
async def test_restart_waits_for_the_running_task(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    api = MaintenanceAPI()
    bridge = new_bridge(api, tmp_path, monkeypatch)
    monkeypatch.setattr(telegram, "pull_updates", lambda root, since=None: pytest.fail("görev sürerken güncellenmez"))
    bridge.active = asyncio.create_task(asyncio.sleep(10))
    for command in ("/update", "/restart"):
        await bridge.handle(message(command))
        assert api.sent[-1].startswith("Bir görev çalışıyor")
    bridge.active.cancel()
    await asyncio.gather(bridge.active, return_exceptions=True)
    bridge.active = None
    with pytest.raises(telegram.RestartRequested):
        await bridge.handle(message("/restart"))
    assert api.sent[-1] == "Yeniden başlatılıyor…"


@pytest.mark.asyncio
async def test_scheduler_does_not_start_a_task_while_updating(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Güncelleme sürerken zamanı gelen plan başlarsa yeniden başlatma onu keserdi."""
    api = MaintenanceAPI()
    bridge = new_bridge(api, tmp_path, monkeypatch)
    now = datetime.now().astimezone()
    due = now - timedelta(minutes=1)
    spec = schedule.build_spec("daily", due.strftime("%H:%M"), None, None, None)
    schedule.save_schedules(tmp_path / "schedules.json", [{
        "id": "abc123", "goal": "Gündemi özetle", "spec": spec, "created_at": due.isoformat(),
        "next_run": due.isoformat(), "last_run": None, "runs": 0,
    }])
    started, release = threading.Event(), threading.Event()

    def slow_pull(root: Path, since: Optional[str] = None) -> maintenance.UpdateResult:
        started.set()
        release.wait(5)
        return update_result()

    monkeypatch.setattr(telegram, "pull_updates", slow_pull)
    command = asyncio.create_task(bridge.handle(message("/update")))
    assert await asyncio.to_thread(started.wait, 5)
    await bridge.scheduler_tick(now)
    assert bridge.active is None
    release.set()
    with pytest.raises(telegram.RestartRequested):
        await command
    # Plan tüketilmedi: yeni süreçteki zamanlayıcı onu çalıştırır
    assert schedule.due_schedules(schedule.load_schedules(tmp_path / "schedules.json"), now)


@pytest.mark.asyncio
async def test_doctor_reports_version_permissions_and_capabilities_even_during_a_task(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    api = MaintenanceAPI()
    bridge = new_bridge(api, tmp_path, monkeypatch)
    bridge.clients = {"openai": object(), "ollama-cloud": object()}  # type: ignore[dict-item]
    bridge.loaded_commit = "a" * 40
    monkeypatch.setattr(telegram, "head_commit", lambda root: "b" * 40)
    monkeypatch.setattr(telegram, "source_version", lambda root: "bbbbbbb · 26.09.2026 12:00")
    monkeypatch.setattr(telegram, "screen_capture_granted", lambda: False)
    monkeypatch.setattr(telegram, "accessibility_granted", lambda: True)
    monkeypatch.setattr(telegram, "load_api_key", lambda variable: None)
    monkeypatch.setenv("XPC_SERVICE_NAME", telegram.SERVICE_LABEL)
    bridge.active = asyncio.create_task(asyncio.sleep(10))
    await bridge.handle(message("/doctor"))
    bridge.active.cancel()
    await asyncio.gather(bridge.active, return_exceptions=True)
    report = api.sent[-1].splitlines()
    assert report[0] == "Sürüm: bbbbbbb · 26.09.2026 12:00 (çalışan köprü daha eski kodla; yüklemek için /restart)"
    assert "Hizmet: launchd hizmeti" in report and "✗ Ekran kaydı izni yok: ekran görüntüsü ve OCR çalışmaz" in report
    assert "✓ Erişilebilirlik izni" in report and "Hazır modeller: ollama-cloud, openai" in report
    assert "Planlanmış görev: 0" in report and report[-1].endswith(sys.executable)


@pytest.mark.asyncio
async def test_run_announces_restart_and_cleans_up_before_restarting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(telegram, "create_model_clients", lambda: {})
    monkeypatch.setattr(telegram, "head_commit", lambda root: "a" * 40)
    monkeypatch.setattr(telegram, "source_version", lambda root: "aaaaaaa · 26.09.2026 12:00")
    api = MaintenanceAPI([{"update_id": 41, **message("/restart")}])
    bridge = new_bridge(api, tmp_path, monkeypatch)
    with pytest.raises(telegram.RestartRequested):
        await bridge.run(announce=True)
    assert api.sent == ["✓ Köprü yeniden başladı: aaaaaaa · 26.09.2026 12:00", "Yeniden başlatılıyor…"]
    assert api.closed and bridge.loaded_commit == "a" * 40
    # Yeni süreç /restart komutunu yeniden işlemez
    assert telegram.read_json(telegram.offset_path(), {}) == {"offset": 42}


def test_main_replaces_the_process_with_the_announcing_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    announced: List[bool] = []
    executed: List[Tuple[str, List[str]]] = []

    async def bridge(announce: bool = False) -> None:
        announced.append(announce)
        raise telegram.RestartRequested()

    monkeypatch.setattr(telegram, "apply_stored_api_keys", lambda: ())
    monkeypatch.setattr(telegram, "run_bridge", bridge)
    monkeypatch.setattr(telegram.os, "execv", lambda path, arguments: executed.append((path, arguments)))
    monkeypatch.setattr(sys, "argv", ["omniagent-telegram", "run"])
    telegram.main()
    assert announced == [False]
    assert executed == [(sys.executable, [sys.executable, "-m", "omniagent.integrations.telegram", "run", "--announce"])]
    monkeypatch.setattr(sys, "argv", ["omniagent-telegram", "run", "--announce"])
    telegram.main()
    assert announced == [False, True]
    assert telegram.build_launchd_record()["ProgramArguments"] == telegram.bridge_command()
