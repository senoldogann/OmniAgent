import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, TypedDict

# Epizodik bellekte tutulacak en fazla görev sayısı (bellek dosyasının sınırsız
# büyüyüp her yüklemede yavaşlamasını önler).
MAX_EPISODES: int = 30

# Öğrenilen derslerde tutulacak en fazla desen sayısı (aynı nedenle sınırsız büyümesin).
MAX_LESSONS: int = 200

# Epizot kaydında adım sonucunun saklanacak azami uzunluğu.
STEP_RESULT_LIMIT: int = 500

# Hata deseni normalizasyonunda kararsız (volatile) parçaları atan kalıplar.
_VOLATILE_PATTERN = re.compile(
    r"("
    r"(/[^\s:,'\"]+)+"          # dosya yolları
    r"|\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"  # uuid
    r"|\b\d{4,}\b"              # uzun sayılar (pid, port, timestamp)
    r"|\b0x[0-9a-f]+\b"         # hex adresler
    r")",
    re.IGNORECASE,
)

# Bilişsel Bellek Durumu Veri Tipi Tanımı
class StateDict(TypedDict):
    semantic_memory: Dict[str, Any]       # Sistem gerçekleri, sabitler, tercihler
    episodic_memory: List[Dict[str, Any]] # Tamamlanan görevlerin geçmişi
    lessons_learned: Dict[str, Dict[str, str]] # Hata -> Çözüm eşleşmeleri
    active_goals: List[str]               # Mevcut hedefler
    global_context: Dict[str, Any]        # Oturumlar arası kalıcılık

def load_state(state_file: str) -> StateDict:
    """
    Dosyadan bilişsel bellek durumunu yükler. Dosya yoksa varsayılan durumu oluşturur.
    """
    path: Path = Path(state_file)
    if path.exists():
        with path.open('r', encoding='utf-8') as source:
            loaded: object = json.load(source)
        if not isinstance(loaded, dict):
            raise ValueError(f"Bellek dosyası nesne içermiyor: {path}")
        expected: Dict[str, type] = {
            "semantic_memory": dict,
            "episodic_memory": list,
            "lessons_learned": dict,
            "active_goals": list,
            "global_context": dict,
        }
        for key, value_type in expected.items():
            if not isinstance(loaded.get(key), value_type):
                raise ValueError(f"Bellek dosyasında geçersiz alan: {path}, {key}")
        return loaded  # type: ignore[return-value]
    
    # Varsayılan bilişsel mimariyi başlat
    return {
        "semantic_memory": {},
        "episodic_memory": [],
        "lessons_learned": {},
        "active_goals": [],
        "global_context": {}
    }

def save_state(state_file: str, state: StateDict) -> None:
    """
    Bilişsel bellek durumunu belirtilen dosyaya kaydeder.
    """
    path: Path = Path(state_file)
    temporary_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode='w', encoding='utf-8', dir=path.parent,
            prefix=f'.{path.name}.', delete=False,
        ) as target:
            temporary_path = Path(target.name)
            # Makine durumu dosyası: boşluk/indent yok, sıkı ayraçlar. Her görevde
            # yüklenip yazıldığı için 3-4x daha küçük dosya = daha hızlı IO.
            json.dump(state, target, ensure_ascii=False, separators=(",", ":"))
            target.flush()
            os.fsync(target.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()

# --- Semantik Bellek (Bilgi) ---

def store_truth(state: StateDict, key: str, value: Any) -> StateDict:
    """
    Sistem veya ortam hakkında doğrulanmış bir gerçeği saklar.
    Saf fonksiyon: Yeni bir durum sözlüğü döner.
    """
    new_state: StateDict = state.copy()
    new_state["semantic_memory"] = {**state["semantic_memory"], key: value}
    return new_state

def get_truth(state: StateDict, key: str) -> Optional[Any]:
    """
    Belirli bir anahtara karşılık gelen bilgiyi getirir.
    """
    return state["semantic_memory"].get(key)

# --- Epizodik Bellek (Deneyim) ---

def _clip(text: str, limit: int) -> str:
    """Metni belirtilen uzunlukta kırpıp gerektiğinde kısaltma işareti ekler."""
    return text if len(text) <= limit else text[:limit] + " …"

def _compact_step(step: Dict[str, Any]) -> Dict[str, Any]:
    """
    Adım kaydını epizodik bellek için sadeleştirir: uzun araç sonuçlarını kırpıp
    hata alanlarını korur. Saf fonksiyon.
    """
    result: Any = step.get("result")
    compact: Dict[str, Any] = {"tool": step.get("tool", "?")}
    if isinstance(result, dict):
        compact["ok"] = bool(result.get("ok"))
        if result.get("ok"):
            compact["result"] = _clip(str(result.get("result", "")), STEP_RESULT_LIMIT)
        else:
            compact["error_type"] = str(result.get("error_type", "Unknown"))
            compact["error"] = _clip(str(result.get("error", "")), STEP_RESULT_LIMIT)
    else:
        compact["ok"] = False
        compact["error_type"] = "Malformed"
        compact["error"] = _clip(str(result), STEP_RESULT_LIMIT)
    return compact

def record_episode(state: StateDict, goal: str, steps: List[Dict[str, Any]], outcome: str, success: bool) -> StateDict:
    """
    Gelecekte geri çağırmak için tam bir görev dizisini kaydeder.
    Epizot sayısı MAX_EPISODES ile sınırlıdır; adım sonuçları kısaltılır.
    """
    episode: Dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "goal": goal,
        "steps": [_compact_step(s) for s in steps],
        "outcome": outcome,
        "success": success
    }
    history: List[Dict[str, Any]] = (state["episodic_memory"] + [episode])[-MAX_EPISODES:]
    new_state: StateDict = state.copy()
    new_state["episodic_memory"] = history
    return new_state

def _goal_tokens(goal: str) -> set:
    """Hedef metninden anlamlı (3+ karakterli) küçük harfli eşleme anahtarları çıkarır."""
    return {t for t in re.split(r"\W+", goal.lower()) if len(t) >= 3}

def query_similar_episodes(state: StateDict, goal: str) -> List[Dict[str, Any]]:
    """
    Geçmişteki başarılı ve benzer hedefleri getirir.
    Benzerlik: hedef kelimelerinin en az yarısı geçmiş hedefte geçiyorsa eş sayılır.
    """
    tokens: set = _goal_tokens(goal)
    if not tokens:
        return []
    matches: List[Dict[str, Any]] = []
    for episode in state["episodic_memory"]:
        if not episode.get("success", False):
            continue
        past_tokens: set = _goal_tokens(str(episode.get("goal", "")))
        overlap: int = len(tokens & past_tokens)
        if overlap and overlap * 2 >= len(tokens):
            matches.append(episode)
    return matches

def summarize_episode_path(episode: Dict[str, Any]) -> str:
    """
    Bir epizodun araç yolunu (hangi araçlarla başarıya ulaşıldığını) tek satırlık
    özete çevirir. Benzer görevlerde rota önerisi olarak kullanılır.
    """
    tools: List[str] = [str(s.get("tool", "?")) for s in episode.get("steps", [])]
    if not tools:
        return str(episode.get("outcome", ""))[:200]
    return f"Yol: {' -> '.join(tools[:12])} | Sonuç: {str(episode.get('outcome', ''))[:160]}"

def recall_similar_success(state: StateDict, goal: str, limit: int) -> Optional[str]:
    """
    Benzer başarılı epizotlardan en fazla `limit` adet rota özeti döner.
    Görev başında modele 'bilinen ders' olarak enjekte edilir.
    """
    similar: List[Dict[str, Any]] = query_similar_episodes(state, goal)
    if not similar:
        return None
    summaries: List[str] = [summarize_episode_path(e) for e in similar[-limit:]]
    return "\n".join(f"- {s}" for s in summaries)

# --- Öğrenilen Dersler (Evrim) ---

def normalize_error_pattern(error_text: str) -> str:
    """
    Hata metnindeki kararsız parçaları (yol, uuid, uzun sayı, hex adres) atıp
    kararlı bir desen döner. Böylece aynı kök neden farklı yollarla tekrar
    ederse ders yine eşleşir. Saf fonksiyon.
    """
    cleaned: str = _VOLATILE_PATTERN.sub(" ", error_text.lower())
    collapsed: str = " ".join(cleaned.split())
    return collapsed[:200] if collapsed else error_text.lower()[:200]

def distill_lesson_pairs(steps: List[Dict[str, Any]]) -> List[Tuple[str, str]]:
    """
    Epizot adımlarından (hata_deseni -> çözüm) çiftleri damıtır.

    Kanıt kuralı: bir araç başarısız olup DAHA SONRA aynı araç tam 1 başarısızlık
    sonrası başarılı olduysa ders üretilir. Aynı araçtan bekleyen birden fazla hata
    varsa kredi belirsizdir (hangi hatanın çözüldüğü bilinemez) ve yanlış ders
    üretmemek için atlanır. Hiç toparlanmayan hatalar da ders üretmez.
    Saf fonksiyon.
    """
    pending_failures: Dict[str, List[str]] = {}
    pairs: List[Tuple[str, str]] = []
    seen_patterns: set = set()
    for step in steps:
        tool: str = str(step.get("tool", "?"))
        result: Any = step.get("result")
        ok: bool = isinstance(result, dict) and bool(result.get("ok"))
        if not ok:
            error_text: str = ""
            if isinstance(result, dict):
                error_text = f"{result.get('error_type', '')}: {result.get('error', '')}"
            else:
                error_text = str(result)
            pending_failures.setdefault(tool, []).append(normalize_error_pattern(error_text))
            continue
        failures: List[str] = pending_failures.pop(tool, [])
        if len(failures) != 1:
            continue
        success_note: str = _clip(str(result.get("result", "")), 160) if isinstance(result, dict) else ""
        fix: str = f"'{tool}' aracı başarısız olduktan sonra şu yaklaşımla çalıştı: {success_note}"
        pattern: str = failures[0]
        if pattern and pattern not in seen_patterns:
            seen_patterns.add(pattern)
            pairs.append((pattern, fix))
    return pairs

def learn_lesson(state: StateDict, failure_pattern: str, fix: str) -> StateDict:
    """
    Tekrarlayan bir hata için spesifik bir çözümü saklar. Desen yazarken
    normalize edilir; arama sırasında tekrar tekrar normalize etmeye gerek kalmaz.
    MAX_LESSONS aşıldığında en eski desenler düşürülür.
    """
    key: str = normalize_error_pattern(failure_pattern)
    lesson: Dict[str, str] = {
        "fix": fix,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
    lessons: Dict[str, Dict[str, str]] = {**state["lessons_learned"], key: lesson}
    if len(lessons) > MAX_LESSONS:
        for old_key in list(lessons)[:len(lessons) - MAX_LESSONS]:
            del lessons[old_key]
    new_state: StateDict = state.copy()
    new_state["lessons_learned"] = lessons
    return new_state

def absorb_episode_lessons(state: StateDict, steps: List[Dict[str, Any]], success: bool) -> StateDict:
    """
    Tamamlanan epizottan dersleri damıtıp belleğe yazar. Her görev sonunda
    otomatik çağrılır — ajan hatalarından sistematik olarak öğrenir.
    Toparlanan (fail->success) desenler 'fix' olarak; görev başarısız bittiyse
    ve toparlanma yoksa en azından desen tanınsın diye 'fix' olarak uyarı kaydı
    tutulur ki sonraki görevde model aynı tuzağa tekrar düşmesin.
    """
    new_state: StateDict = state
    pairs: List[Tuple[str, str]] = distill_lesson_pairs(steps)
    for pattern, fix in pairs:
        new_state = learn_lesson(new_state, pattern, fix)
    if not success and not pairs:
        for step in steps:
            result: Any = step.get("result")
            if isinstance(result, dict) and not result.get("ok"):
                pattern: str = normalize_error_pattern(
                    f"{result.get('error_type', '')}: {result.get('error', '')}"
                )
                if pattern and pattern not in new_state["lessons_learned"]:
                    new_state = learn_lesson(
                        new_state,
                        pattern,
                        f"Bu hata daha önce çözülemedi ({step.get('tool')}). "
                        "Aynı yaklaşımı tekrar denemek yerine farklı bir araç/strateji seç.",
                    )
    return new_state

def check_for_lessons(state: StateDict, current_error: str) -> Optional[str]:
    """
    Mevcut hatanın bilinen bir hata deseniyle eşleşip eşleşmediğini kontrol edir.
    Desenler yazarken normalize edildiği için arama salt alt-dize kontrolüdür;
    ham desenle de denenir (eski/elle girilmiş kayıtlarla geriye dönük uyumluluk).
    """
    normalized: str = normalize_error_pattern(current_error)
    lowered: str = current_error.lower()
    for pattern, data in state["lessons_learned"].items():
        if pattern and pattern in normalized:
            return data.get("fix")
        if pattern.lower() in lowered:
            return data.get("fix")
    return None

# --- Hedef Yönetimi ---

def set_active_goal(state: StateDict, goal: str) -> StateDict:
    """
    Aktif hedefi belirler.
    """
    new_state: StateDict = state.copy()
    new_state["active_goals"] = [goal]
    return new_state

def get_active_goal(state: StateDict) -> Optional[str]:
    """
    Mevcut aktif hedefi getirir.
    """
    goals: List[str] = state["active_goals"]
    return goals[0] if goals else None


def clear_active_goal(state: StateDict) -> StateDict:
    """Aktif hedefi yeni durum içinde temizler."""
    return {**state, "active_goals": []}
