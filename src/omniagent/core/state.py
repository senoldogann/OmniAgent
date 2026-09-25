import json
import os
import re
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, TypedDict, NotRequired, Dict, Union

# Epizodik bellekte tutulacak en fazla görev sayısı (bellek dosyasının sınırsız
# büyüyüp her yüklemede yavaşlamasını önler). Kayıt, kullanıcının "geçen gün ne yapmıştık"
# sorusuna user_memory(action=history) ile yanıt verebilmek için yüz görev tutar.
MAX_EPISODES: int = 100
# Uzun otonom görevlerin kaydı dosyayı şişirmesin: epizot başına son adımlar tutulur.
MAX_STEPS_PER_EPISODE: int = 60
# Geçmiş aramasında gösterilen hedef/sonuç özetinin uzunluğu
HISTORY_GOAL_LIMIT: int = 240
HISTORY_OUTCOME_LIMIT: int = 400
# Türkçe ekler aramayı bozmasın: sözcüklerin ilk bu kadar harfi karşılaştırılır (rapor/raporları)
SEARCH_STEM_LENGTH: int = 5

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
    fast_loop_transitions: NotRequired[int]
    fast_loop_replans: NotRequired[int]
    fast_loop_delivery_entries: NotRequired[int]
    semantic_progress_events: NotRequired[int]
    fast_loop_stagnation_events: NotRequired[int]
    observations: NotRequired[int]
    observations_reused: NotRequired[int]
    duplicate_navigation: NotRequired[int]
    uncached_prompt_tokens: NotRequired[int]
    integrations: NotRequired[Dict[str, Union[int, float]]]
    # Deneyim belleği: hatırlatılan ders sayısı ve görevde doğrulanan yeni ders adayı sayısı
    experience_hints: NotRequired[int]
    experience_candidates: NotRequired[int]


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
    path.parent.mkdir(parents=True, exist_ok=True)
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
        "steps": steps[-MAX_STEPS_PER_EPISODE:],
        "outcome": outcome,
        "success": success,
        "metrics": metrics,
    }
    return {"episodic_memory": (state["episodic_memory"] + [episode])[-MAX_EPISODES:]}


class EpisodeSummary(TypedDict):
    """Geçmiş görev aramasında modele dönen kısa özet."""
    timestamp: str
    goal: str
    success: bool
    outcome: str


def ascii_fold(text: str) -> str:
    """Türkçe harfleri ASCII karşılığına indirir ve küçük harfe çevirir (ı→i, ş→s…). Saf."""
    replaced: str = text.replace("ı", "i").replace("İ", "i")
    decomposed: str = unicodedata.normalize("NFKD", replaced)
    return "".join(character for character in decomposed if not unicodedata.combining(character)).casefold()


def search_stems(text: str) -> frozenset[str]:
    """Arama için sözcük kökleri: ASCII'ye indirgenmiş, en az 3 harfli sözcüklerin ilk harfleri. Saf."""
    return frozenset(
        word[:SEARCH_STEM_LENGTH] for word in re.split(r"[^a-z0-9]+", ascii_fold(text)) if len(word) >= 3
    )


def search_episodes(state: StateDict, query: str, limit: int) -> List[EpisodeSummary]:
    """
    Önceki görevleri sorgu köklerinin hedef + sonuç metninde geçme sayısına göre sıralar; eşit
    skorda yeni görev önce gelir. Boş sorgu en yeni görevleri döner. Saf fonksiyon.
    """
    wanted: frozenset[str] = search_stems(query)
    scored: List[tuple[int, str, EpisodeSummary]] = []
    for episode in state["episodic_memory"]:
        score: int = len(wanted & search_stems(f"{episode['goal']} {episode['outcome']}")) if wanted else 0
        if wanted and score == 0:
            continue
        summary: EpisodeSummary = {
            "timestamp": episode["timestamp"],
            "goal": _clip(episode["goal"], HISTORY_GOAL_LIMIT),
            "success": episode["success"],
            "outcome": _clip(episode["outcome"], HISTORY_OUTCOME_LIMIT),
        }
        scored.append((score, episode["timestamp"], summary))
    ordered: List[tuple[int, str, EpisodeSummary]] = sorted(scored, key=lambda item: (item[0], item[1]), reverse=True)
    return [summary for _, _, summary in ordered[:limit]]
