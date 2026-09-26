"""
Telegram'dan bakım: kaynak kodu güncelleme (git pull), gerekirse bağımlılık eşitleme (uv sync)
ve köprünün kendi durumunu raporlama. Yeniden başlatmayı `telegram.main()` execv ile yapar.

Git ve uv kullanıcının kendi makinesinde, kendi deposunda çalışır. Kimlik bilgisi istemi
açılmaz (GIT_TERMINAL_PROMPT=0); takılan istek zaman aşımıyla düşer ve sohbete bildirilir.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
from typing import Callable, Dict, List, Optional, Sequence, TypedDict

from omniagent.tools.system import child_environment
from omniagent.tools.types import ScreenSession

Runner = Callable[..., "subprocess.CompletedProcess[str]"]
DEPENDENCY_FILES: frozenset[str] = frozenset({"pyproject.toml", "uv.lock"})
GIT_TIMEOUT_SECONDS: float = 90.0
SYNC_TIMEOUT_SECONDS: float = 600.0
COMMIT_LIST_LIMIT: int = 15
# launchd hizmetinin PATH'i kısa olduğundan uv tipik kurulum yerlerinde de aranır
_UV_CANDIDATES: tuple[str, ...] = (
    "~/.local/bin/uv", "~/.cargo/bin/uv", "/opt/homebrew/bin/uv", "/usr/local/bin/uv",
)


class UpdateResult(TypedDict):
    """git pull sonucu; `changed` yeni commit geldi mi, `message` sohbete gidecek özet."""
    ok: bool
    changed: bool
    before: str
    after: str
    commits: List[str]
    dependencies_changed: bool
    message: str


class DoctorFacts(TypedDict):
    """Köprü sürecinin kendi gözünden durum: izinler bu sürecin TCC iznidir."""
    version: str
    stale: bool
    service: str
    python: str
    screen_capture: bool
    accessibility: bool
    screen: ScreenSession
    keep_awake: bool
    models: List[str]
    voice: bool
    schedules: Optional[int]


def _environment() -> Dict[str, str]:
    return {**child_environment(), "GIT_TERMINAL_PROMPT": "0"}


def _run(runner: Runner, command: Sequence[str], root: Path, timeout: float) -> "subprocess.CompletedProcess[str]":
    return runner(
        list(command), cwd=str(root), env=_environment(), capture_output=True, text=True,
        timeout=timeout, check=False,
    )


def _tail(text: str, limit: int = 600) -> str:
    cleaned = text.strip()
    return cleaned if len(cleaned) <= limit else "…" + cleaned[-limit:]


def _failure(message: str, before: str = "") -> UpdateResult:
    return {"ok": False, "changed": False, "before": before, "after": before, "commits": [],
            "dependencies_changed": False, "message": message}


def head_commit(root: Path, runner: Runner = subprocess.run) -> Optional[str]:
    """Çalışma ağacının HEAD commit'i; git yoksa veya dizin depo değilse None."""
    try:
        result = _run(runner, ["git", "rev-parse", "HEAD"], root, 10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    commit = result.stdout.strip()
    return commit if result.returncode == 0 and commit else None


def pull_updates(root: Path, since: Optional[str] = None, runner: Runner = subprocess.run) -> UpdateResult:
    """
    Çalışma ağacını ileri sarar (--ff-only: yerel değişiklik veya ayrışma varsa dokunmaz).
    `since` çalışan kodun commit'idir: kod başka yoldan çekilmiş ama süreç yeniden
    başlatılmamışsa fark da yeni commit'ler de bu commit'e göre hesaplanır.
    """
    if not (root / ".git").exists():
        return _failure(f"Kaynak dizini git deposu değil: {root}")
    try:
        before = since or _run(runner, ["git", "rev-parse", "HEAD"], root, 10).stdout.strip()
        pulled = _run(runner, ["git", "pull", "--ff-only"], root, GIT_TIMEOUT_SECONDS)
        if pulled.returncode != 0:
            return _failure(f"git pull başarısız: {_tail(pulled.stderr or pulled.stdout)}", before)
        after = _run(runner, ["git", "rev-parse", "HEAD"], root, 10).stdout.strip()
        if before == after:
            return {"ok": True, "changed": False, "before": before, "after": after, "commits": [],
                    "dependencies_changed": False, "message": f"Kod zaten güncel ({after[:7]})."}
        commits = _run(
            runner, ["git", "log", "--oneline", "--no-decorate", f"-n{COMMIT_LIST_LIMIT}", f"{before}..{after}"],
            root, 10,
        ).stdout.splitlines()
        changed_files = _run(runner, ["git", "diff", "--name-only", before, after], root, 10).stdout.splitlines()
    except (OSError, subprocess.TimeoutExpired) as error:
        return _failure(f"git çalıştırılamadı: {type(error).__name__}")
    return {
        "ok": True, "changed": True, "before": before, "after": after, "commits": commits,
        "dependencies_changed": bool(DEPENDENCY_FILES & {name.strip() for name in changed_files}),
        "message": f"{before[:7]} → {after[:7]}: {len(commits)} yeni commit alındı.",
    }


def find_uv() -> Optional[str]:
    """uv yürütülebilirini PATH'te, yoksa tipik kurulum yerlerinde bulur."""
    found = shutil.which("uv")
    if found:
        return found
    for candidate in _UV_CANDIDATES:
        path = Path(candidate).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


def sync_dependencies(root: Path, runner: Runner = subprocess.run, uv: Optional[str] = None) -> Optional[str]:
    """Ortamı kilit dosyasına eşitler (uv sync --frozen); hata mesajı ya da başarıda None döner."""
    tool = uv or find_uv()
    if tool is None:
        return "Bağımlılıklar değişti ama uv bulunamadı; Mac'te bir kez `uv sync` çalıştırılmalı."
    try:
        result = _run(runner, [tool, "sync", "--frozen"], root, SYNC_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as error:
        return f"uv sync çalıştırılamadı: {type(error).__name__}"
    return None if result.returncode == 0 else f"uv sync başarısız: {_tail(result.stderr or result.stdout)}"


def source_version(root: Path, runner: Runner = subprocess.run) -> str:
    """Çalışan kaynağın son commit'i: 'abc1234 · 26.09.2026 09:15'."""
    try:
        result = _run(runner, ["git", "log", "-1", "--format=%h · %cd", "--date=format:%d.%m.%Y %H:%M"], root, 10)
    except (OSError, subprocess.TimeoutExpired):
        return "bilinmiyor"
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else "bilinmiyor"


def screen_line(session: ScreenSession) -> str:
    """Ekranın uzaktan kullanılabilirliği: kilitliyken yalnız ekran dışı görevler çalışır. Saf."""
    if not session["on_console"]:
        return "✗ Ekran: başka kullanıcı oturumu önde; ekran/klavye görevleri çalışmaz"
    if session["locked"]:
        return "✗ Ekran kilitli: ekran/klavye görevleri çalışmaz; kabuk, dosya ve web görevleri çalışır"
    if session["asleep"]:
        return "✓ Ekran uykuda ama kilitsiz (ekran görevi gelince uyandırılır)"
    return "✓ Ekran açık ve kilitsiz"


def doctor_lines(facts: DoctorFacts) -> List[str]:
    """Durum raporunun satırları; eksik izin için izni alacak python yolunu da söyler. Saf."""
    def mark(ok: bool) -> str:
        return "✓" if ok else "✗"

    schedules = "okunamadı" if facts["schedules"] is None else str(facts["schedules"])
    lines = [
        f"Sürüm: {facts['version']}"
        + (" (çalışan köprü daha eski kodla; yüklemek için /restart)" if facts["stale"] else ""),
        f"Hizmet: {facts['service']}",
        f"{mark(facts['screen_capture'])} Ekran kaydı izni"
        + ("" if facts["screen_capture"] else " yok: ekran görüntüsü ve OCR çalışmaz"),
        f"{mark(facts['accessibility'])} Erişilebilirlik izni"
        + ("" if facts["accessibility"] else " yok: fare/klavye olayları düşer"),
        screen_line(facts["screen"]),
        f"{mark(facts['keep_awake'])} Uyku engeli"
        + (" (prizdeyken Mac uyumaz)" if facts["keep_awake"] else " yok: Mac uyursa köprü yanıt vermez"),
        f"Hazır modeller: {', '.join(facts['models']) or 'yok'}",
        f"{mark(facts['voice'])} Sesli komut" + ("" if facts["voice"] else " kapalı: OpenAI API anahtarı yok"),
        f"Planlanmış görev: {schedules}",
    ]
    if not (facts["screen_capture"] and facts["accessibility"]):
        lines.append(
            "İzinler bu sürece verilir (Sistem Ayarları > Gizlilik ve Güvenlik): listede yoksa '+' ile "
            f"şu python eklenmeli: {facts['python']}"
        )
    return lines
