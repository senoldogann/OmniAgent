import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, TypedDict, NotRequired, Dict, Union

# Epizodik bellekte tutulacak en fazla görev sayısı (bellek dosyasının sınırsız
# büyüyüp her yüklemede yavaşlamasını önler).
MAX_EPISODES: int = 30

# Epizot kaydında adım ayrıntısının ve argümanlarının saklanacak azami uzunluğu.
STEP_DETAIL_LIMIT: int = 500
STEP_ARGS_LIMIT: int = 300


class StepRecord(TypedDict):
    """Bir araç çağrısının epizot kaydındaki sade hâli."""
    tool: str
    args: str
    ok: bool
    # Başarıda araç sonucu, hatada "hata_tipi: mesaj" (kırpılmış)
    detail: str


class EpisodeMetrics(TypedDict):
    """Görev başına ölçüm değerleri (benchmark ve teşhis için)."""
    turns: int
    tool_calls: int
    elapsed_seconds: float
    backend: str
    prompt_tokens: int
    cached_tokens: int
    completion_tokens: int
    model_seconds: NotRequired[float]
    tool_seconds: NotRequired[float]
    integrations: NotRequired[Dict[str, Union[int, float]]]


class Episode(TypedDict):
    timestamp: str
    goal: str
    steps: List[StepRecord]
    outcome: str
    success: bool
    metrics: EpisodeMetrics


class StateDict(TypedDict):
    """
    Kalıcı bellek: tamamlanan görevlerin sınırlı kaydı. Modele geri enjekte
    EDİLMEZ; otomatik ders/rota enjeksiyonu ölçümde alakasız ipuçları ve başka
    görevlerin yollarını taşıyıp hedef sapmasına yol açtığı için kaldırıldı.
    Kayıt, gerçek çalıştırmaları incelemek için tutulur.
    """
    episodic_memory: List[Episode]


def load_state(state_file: str) -> StateDict:
    """
    Dosyadan bellek durumunu yükler. Dosya yoksa boş durum döner; bozuk dosya
    sessizce sıfırlanmaz, hata fırlatılır.
    """
    path: Path = Path(state_file)
    if not path.exists():
        return {"episodic_memory": []}
    with path.open('r', encoding='utf-8') as source:
        loaded: object = json.load(source)
    if not isinstance(loaded, dict) or not isinstance(loaded.get("episodic_memory"), list):
        raise ValueError(f"Bellek dosyasında geçersiz yapı: {path} ('episodic_memory' listesi bekleniyor)")
    return {"episodic_memory": loaded["episodic_memory"]}


def save_state(state_file: str, state: StateDict) -> None:
    """Bellek durumunu atomik olarak (geçici dosya + os.replace) kaydeder."""
    path: Path = Path(state_file)
    temporary_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode='w', encoding='utf-8', dir=path.parent,
            prefix=f'.{path.name}.', delete=False,
        ) as target:
            temporary_path = Path(target.name)
            # Makine durumu dosyası: boşluksuz sıkı JSON (daha küçük dosya, daha hızlı IO)
            json.dump(state, target, ensure_ascii=False, separators=(",", ":"))
            target.flush()
            os.fsync(target.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def _clip(text: str, limit: int) -> str:
    """Metni belirtilen uzunlukta kırpıp gerektiğinde kısaltma işareti ekler."""
    return text if len(text) <= limit else text[:limit] + " …"


def make_step_record(tool: str, args: str, ok: bool, detail: str) -> StepRecord:
    """Araç çağrısını kırpılmış epizot adımına çevirir. Saf fonksiyon."""
    return {
        "tool": tool,
        "args": _clip(args, STEP_ARGS_LIMIT),
        "ok": ok,
        "detail": _clip(detail, STEP_DETAIL_LIMIT),
    }


def record_episode(
    state: StateDict,
    goal: str,
    steps: List[StepRecord],
    outcome: str,
    success: bool,
    metrics: EpisodeMetrics,
) -> StateDict:
    """
    Tamamlanan görevi ölçümleriyle birlikte kaydeder; en fazla MAX_EPISODES
    görev tutulur. Saf fonksiyon: yeni bir durum döner.
    """
    episode: Episode = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "goal": goal,
        "steps": steps,
        "outcome": outcome,
        "success": success,
        "metrics": metrics,
    }
    return {"episodic_memory": (state["episodic_memory"] + [episode])[-MAX_EPISODES:]}
