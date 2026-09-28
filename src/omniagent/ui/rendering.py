"""
OmniAgent arayüzünün saf sunum yardımcıları: transkript satır özetleri ve görev istatistikleri.

Tk/customtkinter'a bağlı değildir; bu yüzden gerçek arayüzden (ui/app.py) ayrı tutulur ve
başsız testlerde de doğrudan kullanılabilir.
"""
from __future__ import annotations

import json
from typing import Dict, List, Optional, TypedDict

from omniagent.app.types import RunReport
from omniagent.core.events import AgentEvent
from omniagent.core.state import EpisodeMetrics
from omniagent.integrations.capabilities import Capability

# Gösterim sınırları: en dar pencereye sığan satır sayısı ve mono karakter uzunluğu.
LIVE_TAIL_LINES: int = 6
SUMMARY_LINES: int = 4
COMMAND_LINES: int = 6
LINE_CLIP: int = 160
INSERT_MARK: str = "omni_insert"
# Sütunlu Markdown tablosu en dar pencerede de (560 px) satır kaydırmadan bu kadar mono
# karaktere sığar; daha geniş tablo etiket/değer satırlarına dönüşür.
TABLE_MAX_COLUMNS: int = 64


class ToolView(TypedDict):
    """Transkriptteki bir araç bloğunun durumu."""
    region: str
    name: str
    preview: str
    status: str
    call_id: str
    head: List[str]
    tail: List[str]
    line_count: int
    result: str
    seconds: float
    started_at: float


class TurnView(TypedDict):
    number: int
    text_region: Optional[str]
    reasoning_region: Optional[str]
    tools: Dict[int, ToolView]


class UiItem(TypedDict):
    """Ajan thread'lerinden arayüz thread'ine giden kuyruk öğesi."""
    event: Optional[AgentEvent]
    done: bool
    error: str
    report: Optional[RunReport]


def format_capability_inventory(entries: List[Capability], show_skills: bool = False) -> str:
    """Yerel kataloğu model veya ağ çağrısı olmadan kısa metne dönüştürür."""
    executable = sorted((entry for entry in entries if entry.get("kind") != "skill"),
                        key=lambda entry: entry.get("id", ""))
    skills = sorted((entry for entry in entries if entry.get("kind") == "skill"),
                    key=lambda entry: entry.get("id", ""))
    if show_skills:
        lines = [f"Kurulu skill sayısı: {len(skills)}"]
        lines.extend("• " + str(entry.get("id", "")).removeprefix("skill:") for entry in skills)
        return "\n".join(lines)
    lines = ["Kayıtlı entegrasyonlar:"]
    for entry in executable:
        name = entry.get("id", "")
        kind = entry.get("kind", "")
        status = entry.get("connection", "unknown")
        lines.append(f"• {name} ({kind}) · {status}")
    lines.append(f"Kurulu skill sayısı: {len(skills)} · adlar için /skills")
    return "\n".join(lines)


def clip_line(line: str) -> str:
    """Tek satırı gösterim sınırında kırpar. Saf."""
    single: str = line.rstrip("\n")
    return single if len(single) <= LINE_CLIP else single[:LINE_CLIP] + "…"


def summarize_result(name: str, text: str, head: List[str], line_count: int, ok: bool) -> List[str]:
    """Bitmiş aracın transkriptte gösterilecek kısa özet satırları (Claude Code tarzı). Saf."""
    if not ok:
        lines: List[str] = [line for line in text.strip().splitlines() if line.strip()]
        more: List[str] = [f"… +{len(lines) - 3} satır"] if len(lines) > 3 else []
        return [clip_line(line) for line in lines[:3]] + more
    if name in ("execute_shell", "execute_js"):
        if line_count == 0:
            return ["(çıktı yok)"]
        rest: List[str] = [f"… +{line_count - SUMMARY_LINES} satır"] if line_count > SUMMARY_LINES else []
        return [clip_line(line) for line in head[:SUMMARY_LINES]] + rest
    if name == "read_file":
        return [f"{text.count(chr(10)) + 1} satır okundu"]
    if name == "web_search":
        try:
            results: object = json.loads(text)
        except json.JSONDecodeError:
            results = []
        titles: List[str] = [str(item.get("title", "")) for item in results if isinstance(item, dict)] if isinstance(results, list) else []
        return [f"• {clip_line(title)}" for title in titles[:SUMMARY_LINES]] or [clip_line(text)]
    lines = [line for line in text.strip().splitlines() if line.strip()]
    tail_note: List[str] = [f"… +{len(lines) - 3} satır"] if len(lines) > 3 else []
    return [clip_line(line) for line in lines[:3]] + tail_note


def format_run_stats(metrics: EpisodeMetrics) -> List[str]:
    """Görev bitişinde tam token sayılarını ve süre dağılımını okunur satırlara çevirir."""
    prompt: int = metrics["prompt_tokens"]
    cached: int = min(prompt, metrics["cached_tokens"])
    completion: int = metrics["completion_tokens"]
    def fmt(value: int) -> str:
        return f"{value:,}".replace(",", ".")
    first: str = (
        f"{metrics['elapsed_seconds']:.1f} sn · {metrics['turns']} tur · "
        f"{metrics['tool_calls']} araç · {metrics['backend']}"
    )
    if "model_seconds" in metrics and "tool_seconds" in metrics:
        first += f" · model {metrics['model_seconds']:.1f} sn · araç {metrics['tool_seconds']:.1f} sn"
    second: str = (
        f"Giriş {fmt(prompt)} (önbellek {fmt(cached)}, yeni {fmt(prompt - cached)})"
        f" · çıkış {fmt(completion)} · toplam {fmt(prompt + completion)} token"
    )
    lines: List[str] = [first, second]
    integration = metrics.get("integrations", {})
    if integration:
        pieces: List[str] = []
        for key, label, unit in (
            ("discovery_seconds", "keşif", " sn"),
            ("install_seconds", "kurulum", " sn"),
            ("network_seconds", "ağ", " sn"),
            ("wait_seconds", "bekleme", " sn"),
            ("user_wait_seconds", "kullanıcı", " sn"),
            ("network_requests", "ağ isteği", ""),
            ("operations_ok", "işlem başarılı", ""),
            ("operations_failed", "işlem hatalı", ""),
        ):
            value = integration.get(key, 0)
            if value:
                pieces.append(f"{label} {value}{unit}")
        if pieces:
            lines.append("Entegrasyon: " + " · ".join(pieces))
    return lines
