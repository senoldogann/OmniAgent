import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, TypedDict

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
            json.dump(state, target, indent=4, ensure_ascii=False)
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

def record_episode(state: StateDict, goal: str, steps: List[Dict[str, Any]], outcome: str, success: bool) -> StateDict:
    """
    Gelecekte geri çağırmak için tam bir görev dizisini kaydeder.
    """
    episode: Dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "goal": goal,
        "steps": steps,
        "outcome": outcome,
        "success": success
    }
    new_state: StateDict = state.copy()
    new_state["episodic_memory"] = state["episodic_memory"] + [episode]
    return new_state

def query_similar_episodes(state: StateDict, goal: str) -> List[Dict[str, Any]]:
    """
    Geçmişteki başarılı ve benzer hedefleri getirir.
    """
    goal_lower: str = goal.lower()
    return [
        e for e in state["episodic_memory"] 
        if e.get("success", False) and goal_lower in e.get("goal", "").lower()
    ]

# --- Öğrenilen Dersler (Evrim) ---

def learn_lesson(state: StateDict, failure_pattern: str, fix: str) -> StateDict:
    """
    Tekrarlayan bir hata için spesifik bir çözümü saklar.
    """
    lesson: Dict[str, str] = {
        "fix": fix,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
    new_state: StateDict = state.copy()
    new_state["lessons_learned"] = {**state["lessons_learned"], failure_pattern: lesson}
    return new_state

def check_for_lessons(state: StateDict, current_error: str) -> Optional[str]:
    """
    Mevcut hatanın bilinen bir hata deseniyle eşleşip eşleşmediğini kontrol eder.
    """
    error_lower: str = current_error.lower()
    for pattern, data in state["lessons_learned"].items():
        if pattern.lower() in error_lower:
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
