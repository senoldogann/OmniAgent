"""
OmniAgent Oturum Kontrol Noktası ve Durum Saklayıcı (checkpoint.py)
Yarım kalan, durdurulan veya bağlantısı kopan görevlerin durumunu atomik olarak saklar
ve 'devam et' senaryolarında doğrulanmış gerçekleri sıfırdan başlamadan yükler.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
import re
from pathlib import Path
import tempfile
from typing import Dict, List, Optional, TypedDict

from omniagent.paths import checkpoints_dir

RUNS_DIR: Path = checkpoints_dir()


class SessionCheckpoint(TypedDict):
    """Kayıt altına alınan oturum kontrol noktası veri modeli."""
    session_id: str
    goal: str
    facts: Dict[str, str]
    completed_steps: List[str]
    turn_count: int
    updated_at: str


def _utc_now_iso() -> str:
    """ISO 8601 formatında güncel UTC zaman damgası. Saf fonksiyon."""
    return datetime.now(timezone.utc).isoformat()


def save_checkpoint(
    session_id: str,
    goal: str,
    facts: Dict[str, str],
    completed_steps: List[str],
    turn_count: int,
    runs_dir: Optional[Path] = None,
) -> Path:
    """
    Oturum durumunu .omni_runs dizini altına atomik olarak yazar.
    fsync ve atomic replace ile yarım yazılma/bozulma riski sıfırlanır.
    """
    target_dir: Path = runs_dir or RUNS_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    destination: Path = target_dir / f"{session_id}.json"

    payload: SessionCheckpoint = {
        "session_id": session_id,
        "goal": goal,
        "facts": dict(facts),
        "completed_steps": list(completed_steps),
        "turn_count": turn_count,
        "updated_at": _utc_now_iso(),
    }
    content: str = json.dumps(payload, ensure_ascii=False, indent=2)

    temp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="", dir=target_dir,
            prefix=f".{session_id}.", delete=False,
        ) as target:
            temp_path = Path(target.name)
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temp_path, destination)
        return destination
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink(missing_ok=True)


def load_checkpoint(session_id: str, runs_dir: Optional[Path] = None) -> Optional[SessionCheckpoint]:
    """
    Belirtilen oturum kimliğine ait kontrol noktasını okur ve doğrular.
    Dosya yoksa veya bozulmuşsa güvenle None döner.
    """
    target_dir: Path = runs_dir or RUNS_DIR
    file_path: Path = target_dir / f"{session_id}.json"
    if not file_path.is_file():
        return None
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        if "session_id" not in data or "facts" not in data:
            return None
        return {
            "session_id": str(data["session_id"]),
            "goal": str(data.get("goal", "")),
            "facts": {str(k): str(v) for k, v in data.get("facts", {}).items()},
            "completed_steps": [str(s) for s in data.get("completed_steps", [])],
            "turn_count": int(data.get("turn_count", 0)),
            "updated_at": str(data.get("updated_at", "")),
        }
    except (OSError, ValueError, TypeError) as error:
        logging.warning("Kontrol noktası okunamadı", extra={"session_id": session_id, "error": str(error)})
        return None


def find_latest_checkpoint(runs_dir: Optional[Path] = None) -> Optional[SessionCheckpoint]:
    """
    En son güncellenen geçerli oturum kontrol noktasını bulur.
    Hiç checkpoint yoksa None döner.
    """
    target_dir: Path = runs_dir or RUNS_DIR
    if not target_dir.is_dir():
        return None
    candidates: List[Path] = sorted(
        [p for p in target_dir.glob("*.json") if not p.name.startswith(".")],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for path in candidates:
        session_id: str = path.stem
        checkpoint = load_checkpoint(session_id, runs_dir=target_dir)
        if checkpoint is not None:
            return checkpoint
    return None


def _goal_terms(goal: str) -> frozenset[str]:
    """Resume scope karşılaştırması için anlamlı hedef sözcüklerini normalize eder."""
    return frozenset(
        term for term in re.findall(r"\w+", goal.casefold(), flags=re.UNICODE)
        if len(term) >= 3
    )


def find_resume_checkpoint(
    goal_hint: Optional[str],
    runs_dir: Optional[Path] = None,
) -> Optional[SessionCheckpoint]:
    """
    Resume için güvenli checkpoint seçer.

    Aynı conversation'dan önceki hedef biliniyorsa yalnız güçlü sözcük örtüşmesi olan
    checkpoint seçilir. Scope bilgisi yoksa birden fazla yarım görev arasından tahmin
    yapılmaz; yalnız tek geçerli checkpoint varsa kullanılır.
    """
    target_dir: Path = runs_dir or RUNS_DIR
    if not target_dir.is_dir():
        return None
    checkpoints = [
        checkpoint
        for path in sorted(
            [p for p in target_dir.glob("*.json") if not p.name.startswith(".")],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if (checkpoint := load_checkpoint(path.stem, runs_dir=target_dir)) is not None
    ]
    if not goal_hint:
        return checkpoints[0] if len(checkpoints) == 1 else None
    hint_terms = _goal_terms(goal_hint)
    ranked: List[tuple[float, int, SessionCheckpoint]] = []
    for checkpoint in checkpoints:
        checkpoint_terms = _goal_terms(checkpoint["goal"])
        overlap = len(hint_terms & checkpoint_terms)
        required = min(2, len(hint_terms), len(checkpoint_terms))
        ratio = overlap / max(1, min(len(hint_terms), len(checkpoint_terms)))
        if overlap >= required and ratio >= 0.5:
            ranked.append((ratio, overlap, checkpoint))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    if not ranked or (len(ranked) > 1 and ranked[0][:2] == ranked[1][:2]):
        return None
    return ranked[0][2]


def clear_checkpoint(session_id: str, runs_dir: Optional[Path] = None) -> bool:
    """
    Başarıyla tamamlanan veya iptal edilen oturumun kontrol noktasını siler.
    """
    target_dir: Path = runs_dir or RUNS_DIR
    file_path: Path = target_dir / f"{session_id}.json"
    if file_path.exists():
        try:
            file_path.unlink()
            return True
        except OSError:
            return False
    return False


def format_checkpoint_scratchpad(checkpoint: SessionCheckpoint) -> str:
    """
    Kontrol noktasından çıkarılan doğrulanmış gerçekleri ve adımları,
    model için kompakt bir scratchpad Markdown bloğuna dönüştürür.
    Prompt caching'i bozmaz (kullanıcı mesajı sonuna eklenir).
    """
    lines: List[str] = [
        "### Önceki Oturum Kontrol Noktası (Kaldığın Yerden Devam Et):",
        f"- Önceki Hedef: {checkpoint['goal']}",
        f"- Tamamlanan Tur: {checkpoint['turn_count']}",
    ]
    if checkpoint["completed_steps"]:
        lines.append("- Daha Önce Tamamlanan Adımlar:")
        for step in checkpoint["completed_steps"][-6:]:
            lines.append(f"  * {step}")
    if checkpoint["facts"]:
        lines.append("- Doğrulanmış Gerçekler ve Veriler:")
        for key, value in sorted(checkpoint["facts"].items()):
            lines.append(f"  * {key}: {value}")
    lines.append("Lütfen bu gerçekleri tekrar sorgulamadan doğrudan sıradaki eksik adımla devam et.")
    return "\n".join(lines)
