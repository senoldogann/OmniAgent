"""Oturum içindeki sohbeti sınırlı, yan etkisiz veri dönüşümleriyle taşır."""
import json
from typing import Any, Dict, List, TypedDict

from state_manager import StepRecord

MAX_EXCHANGES: int = 8
ANSWER_LIMIT: int = 1200
TOOL_LIMIT: int = 5
TOOL_TEXT_LIMIT: int = 240


class Exchange(TypedDict):
    goal: str
    answer: str
    tools: List[str]


def _clip(text: str, limit: int) -> str:
    """Üç nokta dahil belirtilen uzunluğu aşmaz."""
    if limit <= 0:
        return ""
    return text if len(text) <= limit else text[:limit - 1] + "…"


def tool_digest(steps: List[StepRecord]) -> List[str]:
    """Son farklı çağrıları özetler; dosya içeriklerini veya büyük sonuçları taşımaz."""
    summaries: List[str] = []
    for step in steps:
        try:
            arguments = json.loads(step["args"])
        except (ValueError, TypeError):
            arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}
        values = [str(arguments[key]) for key in ("path", "command", "url", "query", "app_name")
                  if key in arguments and arguments[key] is not None]
        label = step["tool"] + (" " + " · ".join(values) if values else "")
        if not step["ok"]:
            label = "başarısız: " + label
        label = _clip(label.replace("\n", " "), TOOL_TEXT_LIMIT)
        if label in summaries:
            summaries.remove(label)
        summaries.append(label)
    return summaries[-TOOL_LIMIT:]


def make_exchange(goal: str, answer: str, steps: List[StepRecord]) -> Exchange:
    """Tamamlanan, başarısız veya durdurulan bir görevi sohbet kaydına dönüştürür."""
    return {"goal": goal, "answer": _clip(answer, ANSWER_LIMIT), "tools": tool_digest(steps)}


def trim_history(history: List[Exchange], max_exchanges: int = MAX_EXCHANGES,
                 answer_limit: int = ANSWER_LIMIT) -> List[Exchange]:
    """Girdiyi değiştirmeden bağımsız, sınırlandırılmış kayıtlar döndürür."""
    if max_exchanges <= 0:
        return []
    return [{"goal": entry["goal"], "answer": _clip(entry["answer"], answer_limit),
             "tools": [_clip(tool.replace("\n", " "), TOOL_TEXT_LIMIT) for tool in entry["tools"][-TOOL_LIMIT:]]}
            for entry in history[-max_exchanges:]]


def to_messages(history: List[Exchange]) -> List[Dict[str, Any]]:
    """Sistem mesajına dokunmadan kullanıcı/asistan sırasını kurar."""
    messages: List[Dict[str, Any]] = []
    for exchange in trim_history(history):
        suffix = ("\n[önceki görevde kullanılan:\n" + "\n".join(exchange["tools"]) + "]"
                  if exchange["tools"] else "")
        messages.extend([
            {"role": "user", "content": exchange["goal"]},
            {"role": "assistant", "content": exchange["answer"] + suffix},
        ])
    return messages
