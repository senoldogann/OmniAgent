"""
OmniAgent Araç Tipleri ve Sabitleri (tools/types.py)
Tüm alt modüller tarafından paylaşılan katı tipleme yapıları, istisnalar ve limit sabitleri.
"""
from contextvars import ContextVar
from pathlib import Path
from typing import Callable, Dict, List, NotRequired, Optional, Tuple, TypedDict

# Model ekran uzayı ve görüntü sınırları
MODEL_SCREEN_SIZE: int = 1000
SCREENSHOT_MAX_EDGE: int = 1920

# Çıktı ve bellek limitleri (her bayt token demektir)
SHELL_STDOUT_LIMIT: int = 4000
SHELL_STDERR_LIMIT: int = 1000
FILE_READ_LIMIT: int = 8000
FILE_READ_MAX_BYTES: int = 8 * 1024 * 1024
STREAM_STDOUT_MAX_BYTES: int = 64 * 1024
STREAM_STDERR_MAX_BYTES: int = 16 * 1024
STREAM_READ_CHARS: int = 4096
# Canlı komut çıktısının en geç yayılma aralığı
STREAM_EMIT_INTERVAL_SECONDS: float = 0.1
TYPED_TEXT_ECHO_LIMIT: int = 80
# Telegram Bot API sendDocument üst sınırı
DELIVERY_MAX_BYTES: int = 50 * 1024 * 1024
BACKUP_KEEP_PER_FILE: int = 5

PAGE_TEXT_LIMIT: int = 5000
PAGE_ELEMENT_LIMIT: int = 40
PAGE_LOAD_TIMEOUT_MS: int = 20000
PAGE_ACTION_TIMEOUT_MS: int = 3000

# AX Erişilebilirlik limitleri
AX_ELEMENT_LIMIT: int = 80
AX_NODE_LIMIT: int = 3000
AX_SCAN_BUDGET_SECONDS: float = 2.0
AX_MESSAGING_TIMEOUT_SECONDS: float = 1.0
AX_LABEL_SEARCH_NODES: int = 12

# Unicode ve klavye
UNICODE_CHUNK_UNITS: int = 16
UNICODE_CHUNK_DELAY_SECONDS: float = 0.005
MAX_WAIT_SECONDS: float = 5.0

# Fare: çoklu tıklama ve sürükleme zamanlaması
MOUSE_MULTI_CLICK_GAP_SECONDS: float = 0.03
MOUSE_DRAG_HOLD_SECONDS: float = 0.12
MOUSE_DRAG_STEPS: int = 12
MOUSE_DRAG_STEP_SECONDS: float = 0.015

# Ekran durulma (settle) tespiti sabitleri
SETTLE_FRAME_EDGE: int = 160
SETTLE_PIXEL_DELTA: int = 4
SETTLE_CHANGED_RATIO: float = 0.002
SETTLE_POLL_SECONDS: float = 0.03
SETTLE_REACTION_SECONDS: float = 1.0
SETTLE_QUIET_SECONDS: float = 0.45
SETTLE_MAX_SECONDS: float = 3.0

# Chrome sekme ve AppleScript zaman aşımları
CHROME_LOAD_CHECKS: int = 80
CHROME_SCRIPT_TIMEOUT_SECONDS: float = 3.0

# Kaydırma (scroll) sabitleri
SCROLL_CHUNK_POINTS: float = 60.0
SCROLL_CHUNK_DELAY_SECONDS: float = 0.008
SCROLL_MAX_EVENTS: int = 20
SCROLL_SETTLE_QUIET_SECONDS: float = 0.25
SCROLL_SETTLE_MAX_SECONDS: float = 1.5
SCROLL_DIFF_EDGE: int = 480
SCROLL_PIXEL_DELTA: int = 12
SCROLL_MOVED_RATIO: float = 0.01

# Baştan sona okuma (read_scrollable) sabitleri
READ_MAX_PAGES: int = 15
READ_TEXT_LIMIT: int = 12000
READ_FIRST_STEP_SHARE: float = 0.25
READ_STEP_SHARE: float = 0.8
READ_GAP_MARKER: str = "[… olası atlama …]"
READ_EDGE_UNITS: float = 6.0
READ_TOP_ATTEMPTS: int = 4
TEXT_CANDIDATE_LIMIT: int = 8

# Süreç ve kabuk sabitleri
SHELL_TIMEOUT_SECONDS: float = 60.0
SHELL_MAX_TIMEOUT_SECONDS: int = 900
TIMEOUT_OUTPUT_TAIL: int = 1500
JS_TIMEOUT_SECONDS: float = 20.0
FETCH_ERROR_BODY_LIMIT: int = 800
HISTORY_RESULT_LIMIT: int = 8
PROCESS_POLL_SECONDS: float = 0.05

# macOS Güvenlik ve Gizlilik Tercih URL'i
SCREEN_SETTINGS_URL: str = "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"
ACCESSIBILITY_SETTINGS_URL: str = "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"


class ToolError(Exception):
    """Ajanın kendi kendini iyileştirmesi için yapılandırılmış hata sınıfı."""
    def __init__(self, message: str, code: str, recoverable: bool) -> None:
        super().__init__(message)
        self.code: str = code
        self.recoverable: bool = recoverable


class ToolRuntime(TypedDict):
    """
    Çağrı başına araç bağlamı: canlı çıktı hedefi, kullanıcı durdurma denetimi ve host'un bu
    çağrı için kullanıcıdan açık onay alıp almadığı.
    """
    emit_output: Callable[[str], None]
    should_stop: Callable[[], bool]
    approved: NotRequired[bool]


TOOL_RUNTIME: ContextVar[Optional[ToolRuntime]] = ContextVar("TOOL_RUNTIME", default=None)


class ScreenGeometry(TypedDict):
    """Retina ekran pikseli ve normalize model koordinatı arasındaki dönüşüm geometrisi."""
    point_width: int
    point_height: int
    model_width: int
    model_height: int
    origin_x: NotRequired[int]
    origin_y: NotRequired[int]


class ActionStep(TypedDict, total=False):
    """run_action_sequence içindeki tek bir eylem adımı."""
    action: str
    point: List[int]
    to: List[int]
    button: str
    clicks: int
    text: str
    key: str
    seconds: float


class AXElement(TypedDict):
    """macOS Accessibility ağacından ayıklanan etkileşimli GUI öğesi."""
    index: int
    role: str
    title: str
    description: str
    value: str
    center: List[int]
    enabled: bool
    actions: List[str]


class BrowserAction(TypedDict, total=False):
    """Playwright tarayıcı eylem adımı."""
    action: str
    selector: str
    text: str
    timeout_ms: int


class PendingInput(TypedDict):
    """Kullanıcıdan arayüz yoluyla metin bekleme durumu."""
    prompt: str
    submitted: bool
    value: str


def clip_text(text: str, limit: int) -> str:
    """Metni belirtilen uzunlukta kırpıp kısaltma bilgisini ekler. Saf fonksiyon."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[kısaltıldı, toplam {len(text)} karakter]"
