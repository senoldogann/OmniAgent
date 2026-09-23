"""
Ajan döngüsünün arayüze ve CLI'ye AKIŞ olarak yayınladığı yapılandırılmış olaylar ve
bunları gösterirken kullanılan saf yardımcılar. Arayüz log metnini ayrıştırmaz; yalnızca
bu tipli olayları tüketir.
"""
import json
import re
from typing import Callable, Dict, Literal, Optional, TypedDict, Union

from state_manager import EpisodeMetrics

# Önizleme için akıştaki argüman metninin taranacak baş kısmı: büyük write_file içeriklerinde
# her parçada tüm metni taramak O(n²) olur; önizlenen alan hep baştadır.
PREVIEW_SCAN_LIMIT: int = 800
PREVIEW_LIMIT: int = 400


class TokenUsage(TypedDict):
    prompt_tokens: int
    cached_tokens: int
    completion_tokens: int


class RunStarted(TypedDict):
    kind: Literal["run_started"]
    goal: str
    backend: str
    model: str


class TurnStarted(TypedDict):
    kind: Literal["turn_started"]
    turn: int
    max_turns: int
    backend: str
    model: str


class TextDelta(TypedDict):
    """Modelin son kullanıcıya yazdığı metinden akan parça."""
    kind: Literal["text_delta"]
    text: str


class ReasoningDelta(TypedDict):
    """Düşünme modunda modelin akıl yürütme metninden akan parça."""
    kind: Literal["reasoning_delta"]
    text: str


class ToolCallPreview(TypedDict):
    """Model araç çağrısını yazarken güncellenen önizleme (örn. komut harf harf belirir)."""
    kind: Literal["tool_call_preview"]
    index: int
    name: str
    preview: str


class StreamReset(TypedDict):
    """Yarıda kesilen akış yeniden denenecek: o tura ait akmış içerik atılmalı."""
    kind: Literal["stream_reset"]
    reason: str


class ModelFinished(TypedDict):
    kind: Literal["model_finished"]
    turn: int
    seconds: float
    usage: TokenUsage


class ToolStarted(TypedDict):
    kind: Literal["tool_started"]
    call_id: str
    index: int
    name: str
    preview: str


class ToolOutput(TypedDict):
    """Çalışan komutun canlı çıktısından bir satır."""
    kind: Literal["tool_output"]
    call_id: str
    text: str


class ToolFinished(TypedDict):
    kind: Literal["tool_finished"]
    call_id: str
    ok: bool
    text: str
    seconds: float


class BackendChanged(TypedDict):
    kind: Literal["backend_changed"]
    backend: str
    model: str
    reason: str


class Notice(TypedDict):
    kind: Literal["notice"]
    level: Literal["info", "warning", "error"]
    text: str


class RunFinished(TypedDict):
    kind: Literal["run_finished"]
    success: bool
    outcome: str
    reason: str
    metrics: EpisodeMetrics


class IntegrationStatus(TypedDict):
    kind: Literal["integration_status"]
    stage: str
    text: str
    completed: int
    total: int


class UserInputRequired(TypedDict):
    kind: Literal["user_input_required"]
    request_id: str
    title: str
    fields: Dict[str, object]


AgentEvent = Union[
    RunStarted, TurnStarted, TextDelta, ReasoningDelta, ToolCallPreview, StreamReset,
    ModelFinished, ToolStarted, ToolOutput, ToolFinished, BackendChanged, Notice, RunFinished, IntegrationStatus, UserInputRequired,
]
# Olayları tüketen hedef; araç çıktısı işçi thread'lerinden de çağrılır (thread-safe olmalı).
EventSink = Callable[[AgentEvent], None]

# Araçların arayüzde görünen kısa adları
TOOL_LABELS: Dict[str, str] = {
    "discover_capabilities": "Bağlantı keşfi", "outlook_clean": "Posta temizliği",
    "outlook_search": "Posta arama", "outlook_apply_selection": "Posta seçimi",
    "outlook_restore": "Posta geri yükleme",
    "execute_shell": "Kabuk", "process_list": "Süreçler", "read_file": "Oku", "write_file": "Yaz",
    "web_search": "Ara", "fetch_raw": "Getir", "browse_url": "Tarayıcı", "execute_js": "Node",
    "take_screenshot": "Ekran", "cua_get_app": "Uygulama", "cua_get_ax_state": "Arayüz ağacı",
    "cua_click": "Tıkla", "smart_click": "Akıllı tıkla", "run_action_sequence": "Eylemler",
}

# Önizlemede gösterilen asıl argüman
_PREVIEW_KEYS: Dict[str, str] = {
    "discover_capabilities": "query", "outlook_restore": "operation_id",
    "execute_shell": "command", "read_file": "path", "write_file": "path", "web_search": "query",
    "fetch_raw": "url", "browse_url": "url", "execute_js": "code", "take_screenshot": "filename",
    "cua_get_app": "app_name", "cua_get_ax_state": "app_name", "cua_click": "app_name", "smart_click": "app_name",
}


def tool_label(name: str) -> str:
    """Aracın arayüz adı; tanımsız adlar (model uydurduysa) olduğu gibi gösterilir. Saf."""
    if name.startswith("mcp_"):
        return "MCP aracı"
    return TOOL_LABELS.get(name, name)


def _partial_json_string(arguments: str, key: str) -> Optional[str]:
    """
    Akış hâlindeki (yarım olabilen) JSON'da bir anahtarın şimdiye kadar gelen metin değerini
    çözer. Sonda yarım kalmış kaçış dizisi atılır. Saf fonksiyon.
    """
    match: Optional[re.Match[str]] = re.search(
        r'"' + re.escape(key) + r'"\s*:\s*"((?:[^"\\]|\\.)*)(\\?)', arguments[:PREVIEW_SCAN_LIMIT],
    )
    if match is None:
        return None
    raw: str = match.group(1)
    try:
        return json.loads(f'"{raw}"')
    except json.JSONDecodeError:
        # Yarım \uXXXX gibi dizilerde ham metin gösterilir (önizleme amaçlı)
        return raw


def preview_arguments(name: str, arguments: str) -> str:
    """Araç çağrısının insan okunur önizlemesi; model argümanı yazarken de çalışır. Saf."""
    if name == "run_action_sequence":
        return " → ".join(re.findall(r'"action"\s*:\s*"(\w+)"', arguments[:PREVIEW_SCAN_LIMIT]))
    if name == "browse_url" and re.search(r'"url"\s*:\s*null', arguments[:PREVIEW_SCAN_LIMIT]):
        return "mevcut sayfa"
    if name not in _PREVIEW_KEYS:
        return ""
    value: Optional[str] = _partial_json_string(arguments, _PREVIEW_KEYS[name])
    if value is None:
        return ""
    return value if len(value) <= PREVIEW_LIMIT else value[:PREVIEW_LIMIT] + "…"


def compact_count(value: int) -> str:
    """Token sayısını kısa gösterir (2775 → 2.8k). Saf."""
    return f"{value / 1000:.1f}k" if value >= 1000 else str(value)
