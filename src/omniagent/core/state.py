import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, TypedDict, NotRequired, Dict, Union

from omniagent.core.observation_filter import MASK_SCAN_CHARS, mask_sensitive_text, mask_typed_arguments
# ascii_fold artık core/text_norm.py'de (observation_filter'la döngüyü kırmak için); ad, onu state'ten alan modüller
# için burada korunur (search_stems de kullanır).
from omniagent.core.text_norm import ascii_fold

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

# Epizot dosyasına yazılırken adım ayrıntısının ve argümanlarının azami uzunluğu. Bellek içi adım
# kayıtları KIRPILMAZ: host kapıları (unmet_wait_status, has_action_evidence, gui_evidence_summary,
# kod-benzeri belirteç doğrulaması) argümanı ve ayrıntıyı tam metin olarak ayrıştırır; kırpılmış JSON
# çözülemez ve kırpma işareti kuyrukta olduğundan ayrıntıda görünmez. Dosyaya yazılan metin ise önce hassas
# kalıplardan geçirilir (bkz. clip_step_for_storage): kalıcı kayıt bellekteki tam metnin sızdırma yolu olmamalı.
STEP_DETAIL_LIMIT: int = 500
STEP_ARGS_LIMIT: int = 300
# Görev başında ipucu olarak enjekte edilecek benzer geçmiş görev özetleri (bkz. relevant_episodes):
# en az EPISODIC_HINT_MIN_SCORE sözcük kökü örtüşen, en çok EPISODIC_HINT_LIMIT görev.
EPISODIC_HINT_LIMIT: int = 2
EPISODIC_HINT_MIN_SCORE: int = 2


class StepRecord(TypedDict):
    """Bir araç çağrısının adım kaydı: bellekte tam metin, epizot dosyasında kırpılmış (clip_step_for_storage)."""
    tool: str
    args: str
    ok: bool
    # Başarıda araç sonucu, hatada "hata_tipi: mesaj"
    detail: str
    partial_steps: NotRequired[int]


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
    # Nihai yanıt doğrulaması: düzeltilen, belirsiz (düzeltilmeyen) ve gözlemsiz kod-benzeri belirteç sayıları
    answer_tokens_corrected: NotRequired[int]
    answer_tokens_unverified: NotRequired[int]
    answer_tokens_unobserved: NotRequired[int]


class Episode(TypedDict):
    timestamp: str
    goal: str
    steps: List[StepRecord]
    outcome: str
    success: bool
    metrics: EpisodeMetrics


class StateDict(TypedDict):
    """
    Kalıcı bellek: tamamlanan görevlerin sınırlı kaydı. Modele geri enjekte EDİLMEZ — tek
    istisna: hedefle YÜKSEK sözcük kökü örtüşen az sayıda özet görev başında ipucu olarak
    verilir (bkz. relevant_episodes/episodic_hint_text). Tüm kaydın otomatik ders/rota
    enjeksiyonu ölçümde alakasız ipuçları ve başka görevlerin yollarını taşıyıp hedef
    sapmasına yol açtığı için kaldırılmıştı. Kayıt, gerçek çalıştırmaları incelemek ve
    user_memory(action=history) araması için tutulur.
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


def make_step_record(
    tool: str, args: str, ok: bool, detail: str, partial_steps: int = 0,
) -> StepRecord:
    """Araç çağrısını adım kaydına çevirir; metni kırpmaz (kırpma yalnız saklamada: clip_step_for_storage). Saf fonksiyon."""
    record: StepRecord = {"tool": tool, "args": args, "ok": ok, "detail": detail}
    if partial_steps > 0:
        record["partial_steps"] = partial_steps
    return record


def _masked_clip(text: str, limit: int) -> str:
    """
    Kalıcı kayıt için metni ÖNCE hassas kalıplardan geçirir (yalnız ilk MASK_SCAN_CHARS karakter: maliyet sınırı,
    saklanan kısmın çok üstünde), SONRA kırpar: sınırda bölünen sır parçası maskeden kaçmaz. Saf.
    """
    return _clip(mask_sensitive_text(text[:MASK_SCAN_CHARS]), limit)


def clip_step_for_storage(step: StepRecord) -> StepRecord:
    """
    Adımı epizot dosyasına yazmadan önce maskeler ve kırpar; girdiyi değiştirmez. Argümanda alana yazılan metin
    (parola olabilir) ve komut satırı sırları (--password değer), ayrıntıda araç çıktısındaki sır kalıpları ve
    yazma yankısı ([gizli]) dosyaya girmez; bellekteki adım tam kalır. Saf fonksiyon.
    """
    clipped: StepRecord = {
        **step,
        "args": _masked_clip(mask_typed_arguments(step["tool"], step["args"]), STEP_ARGS_LIMIT),
        "detail": _masked_clip(step["detail"], STEP_DETAIL_LIMIT),
    }
    return clipped


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
    görev tutulur, adımlar dosyaya kırpılarak yazılır. Saf fonksiyon: yeni bir durum döner.
    """
    episode: Episode = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "goal": goal,
        "steps": [clip_step_for_storage(step) for step in steps[-MAX_STEPS_PER_EPISODE:]],
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


def relevant_episodes(state: StateDict, query: str, limit: int, min_score: int) -> List[EpisodeSummary]:
    """
    Hedefle en az `min_score` sözcük kökü örtüşen geçmiş görevlerin özetleri (skor sırası, eşit skorda
    yeni görev önce). Bilinçli sınır: geçmiş görevlerin TÜMÜNÜN enjeksiyonu ölçümde alakasız ipuçları
    taşıyıp hedef sapmasına yol açtığı için kaldırılmıştı (bkz. StateDict); bu işlev yalnız YÜKSEK
    örtüşen az sayıda özeti görev başında ipucu olarak verir. Saf fonksiyon.
    """
    wanted: frozenset[str] = search_stems(query)
    if not wanted:
        return []
    scored: List[tuple[int, str, EpisodeSummary]] = []
    for episode in state["episodic_memory"]:
        score: int = len(wanted & search_stems(f"{episode['goal']} {episode['outcome']}"))
        if score < min_score:
            continue
        summary: EpisodeSummary = {
            "timestamp": episode["timestamp"],
            "goal": _clip(episode["goal"], HISTORY_GOAL_LIMIT),
            "success": episode["success"],
            "outcome": _clip(episode["outcome"], HISTORY_OUTCOME_LIMIT),
        }
        scored.append((score, episode["timestamp"], summary))
    ordered = sorted(scored, key=lambda item: (item[0], item[1]), reverse=True)
    return [summary for _, _, summary in ordered[:limit]]


def episodic_hint_text(episodes: List[EpisodeSummary]) -> str:
    """
    Modele verilecek geçmiş görev ipucu metni. İpucu olarak çerçevelenir: model önceki sonucu
    doğrulamadan tekrar etmemeli; önceki yaklaşımı ve engeli yalnız bağlam olarak kullanmalı,
    hedefin kendisi için yeni araç kanıtı üretmeli. Saf fonksiyon.
    """
    if not episodes:
        return ""
    lines: List[str] = [
        "HOST — BENZER GEÇMİŞ GÖREVLER (yalnız bağlam: önceki sonucu doğrulamadan tekrarlama; "
        "bu hedef için yeni araç kanıtı üret):",
    ]
    for episode in episodes:
        status: str = "başarılı" if episode["success"] else "başarısız"
        lines.append(f"- [{episode['timestamp'][:10]}] {episode['goal']} → {status}: {episode['outcome']}")
    return "\n".join(lines)
