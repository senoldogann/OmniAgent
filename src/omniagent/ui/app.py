import sys

# The windowed bundle is also the narrowly scoped search worker executable.
# Dispatch before GUI imports, logging or credential startup in that process.
if __name__ == "__main__" and sys.argv[1:] == ["--internal-readonly-worker"]:
    from omniagent.tools.readonly_worker import main as readonly_worker_main
    readonly_worker_main()
    raise SystemExit(0)

import asyncio
import gc
import logging
import os
import subprocess
import sys
import uuid
import webbrowser
import json
import threading
import weakref
from contextlib import contextmanager
from datetime import datetime, timezone
from io import BytesIO, TextIOWrapper
from logging.handlers import RotatingFileHandler
import time
import traceback
import tkinter as tk
import tkinter.font as tkfont
from tkinter import messagebox, simpledialog
from concurrent.futures import Future, ThreadPoolExecutor, wait
from pathlib import Path
from queue import Empty, Queue
from types import TracebackType
from typing import Callable, Dict, Iterator, List, Optional, Set, Tuple, TypedDict

import customtkinter as ctk
from openai import AsyncOpenAI
from PIL import Image, ImageOps, ImageTk

from omniagent.platform.macos import api_keys
from omniagent.config import (
    API_KEY_VARIABLES, BACKENDS, DEFAULT_BACKEND, api_key_source, apply_stored_api_keys,
    load_api_key, redact, refresh_api_keys, set_api_key, set_backend_model,
)
from omniagent.integrations.runtime import INPUT_TIMEOUT_FIELD
from omniagent.paths import data_root
from omniagent.core.events import AgentEvent, compact_count, provider_fallback_text, tool_label
from omniagent.core.log_format import StructuredFormatter
from omniagent.ui import native_macos
from omniagent.platform.macos.desktop_status import MenuBarTaskStatus, app_is_active, is_backgrounded, notify_finished, set_dock_badge
from omniagent.platform.macos.host_lock import async_host_task_lock_preempting
from omniagent.memory.channels import record_failed_task, record_report, record_user_message, recording_answer
from omniagent.memory.personal import utc_now_iso
from omniagent.platform.macos.visibility import (
    GlobalVisibilityHotkey, VisibilityHotkeyError, application_is_hidden, set_application_hidden,
)
from omniagent.model_catalog import (
    ModelCatalogError, cached_models, cached_remote_targets, list_provider_models,
    load_model_preferences, save_model_preferences, valid_model_id,
)
from omniagent.app.agent import (
    RUN_MODE_PROFILES, STATE_FILE, RunOptions, RunReport, close_model_clients,
    create_model_clients,
)
from omniagent.core.activity import LABELS, stage_for_event
from omniagent.app.conversation import run_conversation_with_callback as run_agent_with_callback
from omniagent.app.continuous import (
    ContinuousLimits, continuous_limits_path, load_continuous_limits, parse_continuous_limits,
    save_continuous_limits,
)
from omniagent.core.state import EpisodeMetrics
from omniagent.ui.markdown import render_markdown
from omniagent.ui.appearance import (
    AppearanceSettings, appearance_path, default_appearance, load_appearance,
    save_appearance, validate_appearance,
)
from omniagent.core.conversation import Exchange, make_exchange, trim_history
from omniagent.core.evidence import EvidenceStore
from omniagent.ui.attention import (
    ALERT_NOTIFY, ALERT_RAISE, input_alert_action, notify_input_required, request_user_attention,
)
from omniagent.ui.chats import (
    OUTCOME_DONE, OUTCOME_FAILED, OUTCOME_STOPPED, ChatRecord, ChatSummary, TranscriptSpan, chats_dir,
    delete_chat, delete_chats, load_catalog, load_chat, new_chat, rename_chat, restore_chats,
    save_catalog, save_chat, spans_from_dump, summary_of, with_outcome,
)
from omniagent.ui.composer import ComposerInput
from omniagent.ui.input_popup import ConfirmationPopup, confirmation_field, create_confirmation_popup
from omniagent.integrations.capabilities import CapabilityService
from omniagent.platform.macos.voice import VoiceInput, VoiceInputError
# Saf sunum yardımcıları app.py'den ayrıldı (D4); adlar geriye dönük uyum için burada da
# görünür kalır (ör. testler `ui.app.format_run_stats` kullanır).
from omniagent.ui.rendering import (
    DOT_FAILED, DOT_RUNNING, INSERT_MARK, LIVE_TAIL_LINES, SUMMARY_LINES, TABLE_MAX_COLUMNS,
    TONE_CHECKED, TONE_NONE, TONE_UNCHECKED, ChatRowLook, ToolGroup, ToolView, TurnView, UiItem, activity_meta,
    chat_dot, chat_row_look, fold_tags, format_capability_inventory, format_input_deadline,
    format_run_stats, group_chats_by_date, group_head_parts, parse_fold_tag, path_within, plain_transcript,
    remap_fold_tags, sent_time_text, summary_parts, tool_parts,
)
# Renk, tipografi ve sütun ölçüleri ui/theme.py'de tek yerdedir; adlar geriye dönük uyum için
# burada da görünür kalır (ör. testler `ui.SURFACE`, `ui.SUCCESS` kullanır).
from omniagent.ui.theme import (
    ACCENT, ACCENT_DIM, ACCENT_HOVER, ACCENT_MUTED, BASE_FONT_SIZE, BG, BORDER, BORDER_SOFT,
    BUBBLE_COLLAPSE_LINES, BUBBLE_COLLAPSED_LINES, BUBBLE_MAX_RATIO, BUBBLE_MIN_TEXT_WIDTH,
    BUBBLE_PAD_X, BUBBLE_PAD_Y, BUBBLE_RADIUS, CHAT_MARKER_WIDTH, COLUMN_MAX_WIDTH, COLUMN_MIN_SIDE,
    COMPOSER_FOCUS_BORDER, COMPOSER_PAD_X, COMPOSER_RADIUS, ERROR, HEADER_TITLE_MAX_CHARS, MONO_FAMILY,
    READING_SIZE, SIDEBAR_WIDTH, SUCCESS, SURFACE, SURFACE_HOVER, SURFACE_RAISED, TEXT, TEXT_DIM,
    TEXT_FAINT, WARNING, column_side, column_width, transcript_tag_styles,
)

SETTINGS_HINT: str = (
    "Anahtarlar macOS Keychain'de saklanır ve yalnızca bu makinede kalır. Kaydettiğiniz anda "
    "ilgili model profili kullanılabilir olur; alanı boş bırakıp Kaydet demek kaydı siler. "
    "Kayıtlı anahtar, kabukta tanımlı aynı değişkenden önceliklidir ve ajanın çalıştırdığı "
    "komutlara ortam değişkeni olarak geçmez."
)
BACKEND_CHOICES: Tuple[str, ...] = ("Otomatik", "ollama-cloud", "openai", "opencode", "opencode-think", "openrouter")
RUN_MODE_CHOICES: Tuple[str, ...] = tuple(
    profile["label"] for profile in RUN_MODE_PROFILES.values()
)
RUN_MODE_KEYS: Dict[str, str] = {
    profile["label"]: key for key, profile in RUN_MODE_PROFILES.items()
}
VOICE_MAX_SECONDS: int = 55
# Kenar çubuğu satırı sol sütun işaretinin renkleri (çalışan noktanınki yanıp sönmeye göre _marker_color'da).
MARKER_COLORS: Dict[str, str] = {
    DOT_FAILED: ERROR, TONE_CHECKED: ACCENT, TONE_UNCHECKED: TEXT_FAINT, TONE_NONE: TEXT_FAINT,
}
# Model çağırmadan yetenek envanterini gösteren komutlar (görev başlatmaz).
INVENTORY_COMMANDS: frozenset[str] = frozenset({"/tools", "/yetenekler", "/skills"})

MICROPHONE_SVG: str = """
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none">
  <rect x="9" y="3" width="6" height="11" rx="3" stroke="ICON_COLOR" stroke-width="1.8"/>
  <path d="M5.5 11.5a6.5 6.5 0 0 0 13 0M12 18v3M8.5 21h7" stroke="ICON_COLOR" stroke-width="1.8" stroke-linecap="round"/>
</svg>
"""
COPY_SVG: str = """
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none">
  <rect x="8" y="8" width="11" height="12" rx="2" stroke="ICON_COLOR" stroke-width="1.8"/>
  <path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h2" stroke="ICON_COLOR" stroke-width="1.8" stroke-linecap="round"/>
</svg>
"""
# Diğer ikonlarla aynı boyut ve çizgi ağırlığında dişli: boyut farkı görsel tutarsızlık yaratıyordu.
GEAR_SVG: str = """
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none">
  <circle cx="12" cy="12" r="3.2" stroke="ICON_COLOR" stroke-width="1.8"/>
  <path d="M12 3.2v2.4M12 18.4v2.4M3.2 12h2.4M18.4 12h2.4M5.9 5.9l1.7 1.7M16.4 16.4l1.7 1.7M18.1 5.9l-1.7 1.7M7.6 16.4l-1.7 1.7" stroke="ICON_COLOR" stroke-width="1.8" stroke-linecap="round"/>
</svg>
"""
EMPTY_SVG: str = """
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 96 96" fill="none">
  <path d="M48 9l7.8 28.2L84 45l-28.2 7.8L48 81l-7.8-28.2L12 45l28.2-7.8L48 9z"
        fill="ICON_COLOR"/>
  <circle cx="72" cy="20" r="4" fill="ICON_COLOR" opacity=".55"/>
  <circle cx="22" cy="72" r="3" fill="ICON_COLOR" opacity=".45"/>
</svg>
"""


def _svg_pil_image(svg: str, color: str, size: int) -> Image.Image:
    """SVG'yi verilen renkte ve boyutta RGBA görüntüye çevirir; çizim yüklenemezse saydam kalır."""
    rendered: str = svg.replace("ICON_COLOR", color)
    try:
        import AppKit
        encoded: bytes = rendered.encode("utf-8")
        data = AppKit.NSData.dataWithBytes_length_(encoded, len(encoded))
        native = AppKit.NSImage.alloc().initWithData_(data)
        tiff = native.TIFFRepresentation()
        image: Image.Image = Image.open(BytesIO(tiff)).convert("RGBA")
    except Exception:
        image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    image.thumbnail((size, size), getattr(Image, "Resampling", Image).LANCZOS)
    return image.copy()


def _svg_ctk_image(svg: str, color: str, size: int = 20) -> ctk.CTkImage:
    """macOS SVG verisini CTkImage'a çevirir; ikon yüklenemezse sessiz boş yedeğe düşer."""
    image: Image.Image = _svg_pil_image(svg, color, size)
    return ctk.CTkImage(light_image=image, dark_image=image, size=(size, size))


def _svg_tk_image(svg: str, color: str, size: int) -> tk.PhotoImage:
    """Aynı SVG'yi düz Tk bileşenleri için PhotoImage olarak üretir (CTkImage tk.Label ile çalışmaz)."""
    return ImageTk.PhotoImage(_svg_pil_image(svg, color, size))

SPINNER_FRAMES: Tuple[str, ...] = ("·", "✢", "✳", "✶", "✻", "✽", "✻", "✶", "✳", "✢")

FRAME_MS: int = 16
IDLE_FRAME_MS: int = 100
RUNNING_IDLE_FRAME_MS: int = 50
SPINNER_INTERVAL: float = 0.11
SHIMMER_INTERVAL: float = 0.07
BLINK_INTERVAL: float = 0.45
RUNNING_REFRESH_INTERVAL: float = 0.2
MAX_EVENTS_PER_FRAME: int = 400
# Model akışını yapay gecikme eklemeden göster; çok büyük parçalar Tk karelerini kilitlemesin.
STREAM_CHARS_PER_FRAME: int = 4096
# Kare istisna verirse döngü bu gecikmeyle yeniden kurulur; kalıcı hata saniyede yüzlerce kayıt üretmez.
ERROR_FRAME_MS: int = 250
CHAT_AUTOSAVE_INTERVAL: float = 1.5
# Anlık görüntü (Text.dump) Tk süresinin en çok ~%5'ini yesin: otomatik kayıt aralığı
# max(CHAT_AUTOSAVE_INTERVAL, CHAT_SNAPSHOT_DUTY x ölçülen görüntü süresi) olur.
CHAT_SNAPSHOT_DUTY: float = 20.0
# Sohbet değiştirme/silme/yeniden adlandırma gibi eylemlerde Tk'nin diske yazılmasını bekleyeceği en uzun süre.
CHAT_FLUSH_TIMEOUT_SECONDS: float = 10.0
# Arka plan thread'lerinden Tk'ye kare başına taşınan en çok iş sayısı.
TK_CALLS_PER_FRAME: int = 50
# Açılış: ilk boyama turundan sonra bu kadar bekleyip ağır işler başlar; Keychain yavaşsa ipucu
# gösterilir (STARTUP_HINT_DELAY_MS) ve görev gönderme kapısı bir süre sonra açılır (KEYCHAIN_WAIT_LIMIT_MS).
FIRST_PAINT_DELAY_MS: int = 30
STARTUP_HINT_DELAY_MS: int = 1500
KEYCHAIN_WAIT_LIMIT_MS: int = 45000
# Sohbet ara kutusuna yazarken liste bu gecikmeden sonra bir kez güncellenir.
CHAT_LIST_DEBOUNCE_MS: int = 150
# Çalışan sohbetin durum noktası bu aralıkla yavaşça yanıp söner.
DOT_PULSE_INTERVAL: float = 0.9
# Kapanış: Tk'nin kapatma işinin bitmesini en çok bu kadar beklediği kısa süre (pencere zaten gizlidir),
# sohbetin son yazımı ve çalışan ajan için bekleme süreleri, tüm kapatma üst sınırı ve Tk'nin bitişi yokladığı aralık.
# Üst sınır iki beklemenin toplamından (2 + 3 sn) ve bağlantıları kapatma payından büyük olmalıdır.
CLOSE_GRACE_SECONDS: float = 0.3
CLOSE_FLUSH_WAIT_SECONDS: float = 2.0
CLOSE_AGENT_WAIT_SECONDS: float = 3.0
CLOSE_TIMEOUT_SECONDS: float = 8.0
CLOSE_POLL_MS: int = 25
KEYCHAIN_HINT: str = (
    "Anahtarlar Keychain'den okunuyor; macOS izin penceresi çıktıysa yanıtlayın, sonra tekrar deneyin."
)
KEYCHAIN_LATE_HINT: str = (
    "Keychain yanıt vermedi; anahtarlar yanıt gelince yüklenecek (şimdilik yalnız yerel/ortam profilleri)."
)
# Tk'de bir etiketin `elide` ayarını kaldırır (bölüm açık); False verilseydi iç içe etiketlerde
# üstteki gizlemeyi ezerdi.
ELIDE_UNSET: str = ""
# Asistan metin bölgesinin başındaki küçük boşluk satırı: kabarcıksız düz metni öncekinden ayırır.
ASSISTANT_HEAD: Tuple[str, Tuple[str, ...]] = ("\n", ("gap",))

# Günlük dosyası: paketli uygulamada stderr görünmez; uyarı ve hatalar veri klasöründeki dönen dosyaya yazılır.
LOG_FILE_NAME: str = "ui.log"
LOG_MAX_BYTES: int = 512 * 1024
LOG_BACKUP_COUNT: int = 2


class PrivateRotatingFileHandler(RotatingFileHandler):
    """Dönen günlük dosyası; her yeni dosya (dönüş sonrasında açılan dahil) yalnız kullanıcıya açık (0600) olur."""

    def _open(self) -> TextIOWrapper:
        stream: TextIOWrapper = super()._open()
        os.chmod(self.baseFilename, 0o600)
        return stream


def configure_ui_logging(log_path: Path) -> logging.Handler:
    """
    Kök logger'a dönen günlük dosyası işleyicisi ekler (WARNING ve üstü, yapısal alanlarla) ve
    işleyiciyi döndürür. Veri klasörü zaten 0700'dür; dosyalar 0600 açılır.
    """
    handler: PrivateRotatingFileHandler = PrivateRotatingFileHandler(
        log_path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8",
    )
    handler.setFormatter(StructuredFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root: logging.Logger = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.WARNING)
    return handler


class BubbleView(TypedDict):
    """Kullanıcı mesajının transkripte gömülü kabarcığı, katlama durumu ve alt satırı (saat + kopyala)."""
    frame: ctk.CTkFrame
    clip: tk.Frame
    label: tk.Label
    toggle: tk.Label
    foot: tk.Frame
    time: tk.Label
    copy: tk.Label
    expanded: bool


class ChatRow(TypedDict):
    """Kenar çubuğundaki bir sohbet satırının widget'ları, son uygulanan görünümü ve üzerine gelme durumu."""
    frame: ctk.CTkFrame
    marker: ctk.CTkLabel
    button: ctk.CTkButton
    more: Optional[ctk.CTkButton]
    look: ChatRowLook
    hover: bool


class DeferredInput(TypedDict):
    """Uygulama gizliyken gelen, pencere açılması ertelenen yanıt isteği ve istendiği an."""
    title: str
    fields: Dict[str, object]
    requested_at: datetime


def _run_startup(future: "Future[Dict[str, AsyncOpenAI]]") -> None:
    """
    Arka plan thread'i: Keychain anahtarlarını uygular ve model istemcilerini kurar. Sonuç ya da istisna
    Future ile Tk thread'ine taşınır ve orada yeniden yükselir (sessiz yutulmaz).
    """
    try:
        apply_stored_api_keys()
        clients: Dict[str, AsyncOpenAI] = create_model_clients()
    except Exception as error:
        future.set_exception(error)
        return
    future.set_result(clients)


class OmniUI(ctk.CTk):
    """
    OmniAgent arayüzü: ajanın model yanıtını, araç çağrılarını, çalıştırdığı komutları ve
    komut çıktılarını AKIŞ olarak, animasyonlu gösterir. Olaylar ajan thread'lerinden
    kuyruğa gelir; tüm çizim ~60 fps'lik tek bir kare döngüsünde (Tk thread'i) yapılır.

    Transkript modeli: metin alanı yalnız içerik sütunu genişliğindedir (theme.column_side/
    column_width; pencere değişince _apply_column_layout günceller). İçerik `rN` bölgeleridir;
    asistan metni kabarcıksız düz metindir. Kullanıcı mesajı gizli (elide) metin + gömülü yuvarlak
    kabarcık penceresidir. Araç grubu, satırı ve ayrıntısı yalnız etiketlerle katlanır (rendering.
    fold_tags: durum etiketlerin `elide` ayarındadır, kayıttan yüklenince kapalı gelir). Sondaysa
    (_stick_to_end) yeni içerik görünümü sona kaydırır.
    """

    def __init__(self) -> None:
        super().__init__()
        # Kapanış başladıysa kare döngüsü yeniden kurulmaz; aynı hata tekrar ederse günlük seyrelir.
        self._closing: bool = False
        self._callback_error_counts: Dict[str, int] = {}
        ctk.set_appearance_mode("dark")
        self.title("OmniAgent")
        self.geometry("1120x820")
        self.minsize(800, 600)
        self.configure(fg_color=BG)
        self.bind("<Map>", lambda event: self.after_idle(self._style_native_window)
                  if event.widget is self else None, add="+")
        self._system_ui_family: str = (
            "Helvetica Neue" if sys.platform == "darwin"
            else tkfont.nametofont("TkDefaultFont").actual("family")
        )
        try:
            self._appearance: AppearanceSettings = load_appearance()
        except (OSError, ValueError, json.JSONDecodeError) as error:
            logging.warning("Görünüm ayarları okunamadı: %s", type(error).__name__)
            self._appearance = default_appearance()
        family: str = self._appearance["font_family"]
        # Kurulu yazı tipi listesi soğukken ~70 ms sürer: yalnız özel bir yazı tipi seçiliyse sorulur.
        self._ui_family: str = family if family and family in tkfont.families() else self._system_ui_family
        self._ui_fonts: Dict[int, Tuple[weakref.ReferenceType[ctk.CTkFont], int]] = {}
        self.attributes("-alpha", self._window_alpha())
        self._voice_icon: ctk.CTkImage = _svg_ctk_image(MICROPHONE_SVG, TEXT_DIM)
        self._voice_icon_active: ctk.CTkImage = _svg_ctk_image(MICROPHONE_SVG, TEXT)
        self._voice_icon_busy: ctk.CTkImage = _svg_ctk_image(MICROPHONE_SVG, TEXT_FAINT)
        self._copy_icon: ctk.CTkImage = _svg_ctk_image(COPY_SVG, TEXT_DIM)
        # Kullanıcı mesajının altındaki simge düz tk.Label kullanır: CTkImage değil PhotoImage ister.
        self._copy_photo: tk.PhotoImage = _svg_tk_image(COPY_SVG, TEXT_FAINT, 13)
        self._copy_photo_done: tk.PhotoImage = _svg_tk_image(COPY_SVG, SUCCESS, 13)
        self._gear_icon: ctk.CTkImage = _svg_ctk_image(GEAR_SVG, TEXT_DIM)
        self._empty_icon: ctk.CTkImage = _svg_ctk_image(EMPTY_SVG, ACCENT, 80)
        self._menu_status = MenuBarTaskStatus()

        self._chat_index: List[ChatSummary] = []
        self._active_chat_id: Optional[str] = None
        self._last_deleted_chat_ids: List[str] = []
        self._chat_select_mode: bool = False
        self._selected_chat_ids: set[str] = set()
        self._chat_record: Optional[ChatRecord] = None
        self._chat_last_save: float = 0.0
        self._chat_store_problem: str = ""
        self._chat_catalog_writable: bool = True
        try:
            self._chat_index, self._active_chat_id = load_catalog()
        except (OSError, ValueError, json.JSONDecodeError) as error:
            self._chat_store_problem = f"Sohbet listesi okunamadı: {error}"
            self._chat_catalog_writable = False
            logging.warning(self._chat_store_problem)
        # Sohbet kaydı Tk thread'inde diske yazılmaz: anlık görüntü Tk'de alınır, yazma tek thread'li
        # sıralı yazıcıdadır (son durum kazanır); sonuçlar _call_in_tk ile Tk'ye döner.
        self._chat_executor: ThreadPoolExecutor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="chat-writer")
        self._chat_write: Optional["Future[None]"] = None
        self._chat_snapshot_seconds: float = 0.0
        self._tk_calls: "Queue[Callable[[], None]]" = Queue()
        # Kenar çubuğu: görünen satırlar yerinde güncellenir, her değişiklikte yeniden kurulmaz.
        self._chat_rows: Dict[str, ChatRow] = {}
        self._chat_group_labels: Dict[str, ctk.CTkLabel] = {}
        self._chat_rows_layout: Optional[Tuple[bool, bool]] = None
        self._chat_empty_label: Optional[ctk.CTkLabel] = None
        self._chat_list_refresh_id: Optional[str] = None
        self._last_dot_pulse: float = 0.0
        self._dot_bright: bool = True
        # Açılış: Keychain okuması ve istemci kurulumu ilk boyamadan sonra arka planda yapılır.
        self._first_paint_done: bool = False
        self._startup_pending: bool = False
        self._startup_future: Optional["Future[Dict[str, AsyncOpenAI]]"] = None
        # Kapanışta Tk en çok CLOSE_GRACE_SECONDS bekler; işi biten yoksa bloklanmadan bitişi yoklar.
        self._destroyed: bool = False
        self._close_future: Optional["Future[None]"] = None
        self._close_deadline: float = 0.0
        # İçerik sütunu ölçüleri pencere boyutuna göre _apply_column_layout'ta güncellenir.
        self._column_side: int = COLUMN_MIN_SIDE
        self._column_width: int = COLUMN_MAX_WIDTH
        # Sütuna hizalı ana öğeler (üst şerit, durum satırı, composer, ipucu) ve sol iç girintileri.
        self._column_widgets: List[Tuple[tk.Misc, int]] = []
        self._bubbles: List[BubbleView] = []
        # Kullanıcı sondayken yeni içerik transkripti sona kaydırır; yukarı çıkınca durur.
        self._stick_to_end: bool = True
        self._activity_bar: Optional[ctk.CTkFrame] = None
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._main = ctk.CTkFrame(self, fg_color=BG, corner_radius=0)
        self._main.grid(row=0, column=1, sticky="nsew")
        self._main.grid_columnconfigure(0, weight=1)
        self._main.grid_rowconfigure(1, weight=1)
        self._build_sidebar()
        # Transkript önce kurulur: başlık, durum satırı ve composer sütun kenarlarını kaydırma
        # çubuğunun gerçek genişliğine göre hizalar (_column_padding).
        self._build_transcript()
        self._build_header()
        self._build_activity_bar()
        self._build_composer()
        self._build_footer()

        self._inbox: "Queue[UiItem]" = Queue()
        self._visibility_requests: "Queue[bool]" = Queue()
        self._visibility_hidden: bool = False
        self._visibility_hotkey: Optional[GlobalVisibilityHotkey] = None
        self._stop_event: threading.Event = threading.Event()
        self._agent_future: Optional["Future[RunReport]"] = None
        self._active_run_mode: str = "normal"
        self._control_messages: "Queue[str]" = Queue()
        self._history: List[Exchange] = []
        self._active_goal: str = ""
        self._input_futures: Dict[str, asyncio.Future] = {}
        self._input_windows: Dict[str, ctk.CTkToplevel | ConfirmationPopup] = {}
        self._deferred_inputs: Dict[str, DeferredInput] = {}
        self._voice_queue: "Queue[Tuple[str, str]]" = Queue()
        self._voice_base_text: str = ""
        self._voice_partial_text: str = ""
        self._voice = VoiceInput(
            self._queue_voice_text,
            self._queue_voice_state,
            max_seconds=VOICE_MAX_SECONDS,
            on_partial=self._queue_voice_partial,
        )
        self._region_seq: int = 0
        # Akan bölgeler: bekleyen metin, metin etiketi, imleç var mı, model hâlâ yazıyor mu
        self._pending_text: Dict[str, str] = {}
        self._raw_text: Dict[str, str] = {}
        self._region_text_tag: Dict[str, str] = {}
        self._streaming_regions: Dict[str, bool] = {}
        self._live_regions: Dict[str, bool] = {}
        self._turn: Optional[TurnView] = None
        self._tools_by_call: Dict[str, ToolView] = {}
        self._dirty_tools: Dict[str, ToolView] = {}
        self._artifact_widgets: List[ctk.CTkFrame] = []
        self._artifact_images: List[ctk.CTkImage] = []
        # Araç grupları ve katlanabilir bölümler: durum yalnız etiketlerin elide ayarında tutulur;
        # açık bölümler ve kullanıcının elle değiştirdikleri burada izlenir.
        self._tool_group: Optional[ToolGroup] = None
        self._tool_groups: Dict[int, ToolGroup] = {}
        self._fold_seq: int = 0
        self._open_folds: Set[Tuple[str, int]] = set()
        self._manual_folds: Set[Tuple[str, int]] = set()
        self._prepared_tags: Set[str] = set()
        self._chat_dirty: bool = False
        self._run_started_at: float = 0.0
        self._task_status: str = "idle"
        self._badge_pending: bool = False
        self._turn_streamed_chars: int = 0
        self._completed_tokens: int = 0
        self._activity_verb: str = ""
        self._spinner_index: int = 0
        self._shine_index: int = 0
        self._blink_on: bool = True
        self._last_spinner: float = 0.0
        self._last_shimmer: float = 0.0
        self._last_blink: float = 0.0
        self._last_running_refresh: float = 0.0
        self._last_menu_check: float = 0.0

        # Görevler tek bir kalıcı event loop'ta çalışır ve model istemcilerini paylaşır:
        # her görev sıcak HTTP bağlantılarıyla başlar (TLS el sıkışması tekrarlanmaz).
        self._loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, daemon=True).start()
        # İstemciler Keychain anahtarlarına bağlıdır: boş başlar, begin_background_startup() ilk boyamadan
        # sonra arka planda doldurur (Keychain izin istemi Tk'yi bloklamasın).
        self._clients: Dict[str, AsyncOpenAI] = {}
        # Ayarlar sayfası anahtar kaydederse istemciler yenilenir; görev sürerken beklemeye alınır.
        self._clients_stale: bool = False
        self._pending_model_choices: Dict[str, str] = {}
        self._settings_window: Optional[ctk.CTkToplevel] = None
        self._integrations = CapabilityService()

        self.bind("<Escape>", lambda event: self._request_stop())
        self.bind("<FocusIn>", self._on_focus_return, add="+")
        # Bırakma anında çalışır: CustomTkinter'ın "all" <Button-1> bağlaması odağı tıklanan bileşene
        # taşıdıktan sonra klavye yüzeyi composer'a döner (basış anında yapılsa ezilirdi).
        self.bind("<ButtonRelease-1>", self._keep_composer_focus, add="+")
        self.bind("<Command-k>", lambda event: self._clear_transcript())
        self.bind("<Command-f>", lambda event: self._focus_chat_search())
        self.bind("<Command-comma>", lambda event: self._open_settings())
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        if sys.platform == "darwin":
            # ⌘Q, Dock > Çık ve oturum kapatma: tanımlı değilse Tk varsayılanı Tcl `exit` çağırır (tkinter bunu
            # SystemExit'e çevirir), mainloop'tan çıkılır ve _on_close (kayıt, bağlantı kapatma) hiç çalışmaz.
            # Kanca tk_mac(n)'de belgelidir; idlelib de aynı kalıbı kullanır.
            self.createcommand("::tk::mac::Quit", self._on_close)
        self._text.configure(state="normal")
        self._render_welcome()
        self._text.configure(state="disabled")
        # Kenar çubuğu listesi ve son açık sohbet ilk boyamadan sonra yüklenir: büyük bir sohbeti
        # senkron yüklemek __init__'i bloke edip boş/çizilmemiş pencere bırakıyordu.
        self._after_first_paint(self._post_paint_init)
        self._set_activity_idle()
        if sys.platform == "darwin":
            try:
                self._visibility_hotkey = GlobalVisibilityHotkey(
                    lambda: self._visibility_requests.put(True)
                )
            except (VisibilityHotkeyError, OSError) as error:
                self._post({"kind": "notice", "level": "warning",
                            "text": f"Genel ⌘⇧X kullanılamıyor: {error}"})
        self.after(FRAME_MS, self._tick)
        self.after(120, self.entry.focus_set)
        self.after(120, self._style_native_window)

    def report_callback_exception(self, exc: type[BaseException], val: BaseException,
                                  tb: Optional[TracebackType]) -> None:
        """
        Tk geri çağrısındaki (after/bind/command) istisnayı günlüğe yazar ve kullanıcıya bildirir;
        döngüyü sürdürmek _tick'in işidir. Aynı hata art arda gelirse kayıt 1, 2, 4, 8… aralıkla
        yazılır; transkript bildirimi yalnız ilkinde eklenir. Sırlar günlüğe maskelenerek geçer.
        """
        message: str = redact(str(val))[:300]
        key: str = f"{exc.__name__}:{message[:80]}"
        count: int = self._callback_error_counts.get(key, 0) + 1
        self._callback_error_counts[key] = count
        if count & (count - 1) == 0:
            # exc_info verilmez: ham istisna metni maskelenmemiş sır taşıyabilir; izleme metni maskelenir.
            logging.error(
                "Arayüz geri çağrısında beklenmeyen hata",
                extra={"error_type": exc.__name__, "error_message": message, "occurrences": count,
                       "traceback": redact("".join(traceback.format_exception(exc, val, tb))).rstrip()},
            )
        if count == 1:
            self._post({"kind": "notice", "level": "error",
                        "text": f"Arayüz hatası ({exc.__name__}): {message[:200]}. Ayrıntı günlükte."})

    @contextmanager
    def _writable_transcript(self) -> Iterator[None]:
        """Transkripti yazılabilir yapar; istisna olsa bile salt okunura döner (kullanıcı yazamasın)."""
        self._text.configure(state="normal")
        try:
            yield
        finally:
            self._text.configure(state="disabled")

    def _call_in_tk(self, action: Callable[[], None]) -> None:
        """
        Herhangi bir thread'den çağrılır (Tk'ye doğrudan dokunmaz); eylem sonraki karede Tk thread'inde
        çalışır. Transkripte yazacak eylem doğrudan yazmaz (Text salt okunurdur): _post(notice) kullanır.
        """
        self._tk_calls.put(action)

    def _drain_tk_calls(self) -> None:
        """Arka plan thread'lerinden gelen eylemleri kare başına en çok TK_CALLS_PER_FRAME kadar çalıştırır."""
        for _ in range(TK_CALLS_PER_FRAME):
            try:
                action: Callable[[], None] = self._tk_calls.get_nowait()
            except Empty:
                return
            action()

    def _style_native_titlebar(self, title: Optional[str] = None) -> None:
        """
        macOS başlık çubuğunu içerikle aynı arka plan rengine getirir. Başlık verilmezse ana
        pencereyi hedefler; Ayarlar sayfası da aynı renkle çağırır.
        """
        if sys.platform != "darwin":
            return
        import AppKit
        target: str = title or self.title()
        color = AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(
            int(BG[1:3], 16) / 255, int(BG[3:5], 16) / 255, int(BG[5:7], 16) / 255, 1.0)
        for window in AppKit.NSApplication.sharedApplication().windows():
            if str(window.title()) == target:
                window.setTitlebarAppearsTransparent_(True)
                window.setBackgroundColor_(color)

    def _style_native_window(self, title: Optional[str] = None) -> None:
        """Tk içeriğini örten native efekt eklemeden başlık çubuğunu renklendirir."""
        self._style_native_titlebar(title)

    # --- Yerleşim ---

    def _ui_font(self, size: int, weight: str) -> ctk.CTkFont:
        font = ctk.CTkFont(
            family=self._ui_family, size=max(9, size + self._appearance["font_size"] - 13), weight=weight,
        )
        font_id = id(font)
        self._ui_fonts[font_id] = (
            weakref.ref(font, lambda _ref, key=font_id: self._ui_fonts.pop(key, None)), size,
        )
        return font

    def _window_alpha(self) -> float:
        return self._appearance["opacity"] if self._appearance["transparent_window"] else 1.0

    def _font_delta(self) -> int:
        """Görünüm ayarındaki yazı boyutunun tabana (BASE_FONT_SIZE) farkı; tüm yazı boyutları bu kadar kayar."""
        return self._appearance["font_size"] - BASE_FONT_SIZE

    def _refresh_transcript_fonts(self) -> None:
        """Transkript etiketlerini (renk, yazı tipi, boşluk) geçerli yazı ayarlarıyla yeniden yapılandırır."""
        delta: int = self._font_delta()
        self._body.configure(family=self._ui_family, size=READING_SIZE + delta)
        for name, style in transcript_tag_styles(self._ui_family, delta).items():
            self._text.tag_configure(name, **style)
        for name in ("md_h3", "md_h2", "md_h1"):
            self._text.tag_raise(name)
        if hasattr(self, "_activity"):
            self._configure_activity_tags(delta)

    def _apply_appearance(self, values: Dict[str, object]) -> bool:
        """Doğrulanmış görünüm tercihlerini kaydeder ve açık pencerelere uygular."""
        settings = validate_appearance(values)
        if settings["font_family"] and settings["font_family"] not in tkfont.families():
            raise ValueError("Seçilen yazı tipi bu Mac'te kurulu değil.")
        if settings == self._appearance:
            return False
        save_appearance(settings, appearance_path())
        self._appearance = settings
        self._ui_family = settings["font_family"] or self._system_ui_family
        delta = self._font_delta()
        for font_ref, base_size in list(self._ui_fonts.values()):
            font = font_ref()
            if font is not None:
                font.configure(family=self._ui_family, size=max(9, base_size + delta))
        self._refresh_transcript_fonts()
        self._relayout_bubbles()
        self.attributes("-alpha", self._window_alpha())
        if self._settings_window is not None and self._settings_window.winfo_exists():
            self._settings_window.attributes("-alpha", self._window_alpha())
        self._refresh_chat_list()
        return True

    def _build_sidebar(self) -> None:
        sidebar = ctk.CTkFrame(self, width=SIDEBAR_WIDTH, fg_color=SURFACE, corner_radius=0)
        self._sidebar = sidebar
        sidebar.grid(row=0, column=0, sticky="nsew")
        sidebar.grid_propagate(False)
        sidebar.grid_columnconfigure(0, weight=1)
        sidebar.grid_rowconfigure(4, weight=1)
        brand = ctk.CTkFrame(sidebar, fg_color="transparent")
        brand.grid(row=0, column=0, sticky="ew", padx=18, pady=(24, 25))
        ctk.CTkLabel(brand, text="✻", text_color=ACCENT,
                     font=ctk.CTkFont(family=MONO_FAMILY, size=22, weight="bold")).pack(side="left")
        ctk.CTkLabel(brand, text="OmniAgent", text_color=TEXT,
                     font=self._ui_font(16, "bold")).pack(side="left", padx=(10, 0))
        self.new_chat_btn = ctk.CTkButton(
            sidebar, text="＋   Yeni sohbet", height=40, fg_color=ACCENT, hover_color=ACCENT_HOVER,
            text_color=BG, corner_radius=10, font=self._ui_font(13, "bold"), anchor="w", command=self._new_chat,
        )
        self.new_chat_btn.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 14))
        self._chat_search = ctk.CTkEntry(
            sidebar, placeholder_text="⌕   Sohbet ara", height=35, fg_color=BG,
            border_color=BORDER, corner_radius=9, text_color=TEXT, font=self._ui_font(12, "normal"),
        )
        self._chat_search.grid(row=2, column=0, sticky="ew", padx=14, pady=(0, 13))
        self._chat_search.bind("<KeyRelease>", lambda _event: self._schedule_chat_list_refresh())
        selection_bar = ctk.CTkFrame(sidebar, fg_color="transparent")
        selection_bar.grid(row=3, column=0, sticky="ew", padx=14, pady=(0, 5))
        selection_bar.grid_columnconfigure(0, weight=1)
        self._select_all_btn = ctk.CTkButton(
            selection_bar, text="Görünenler", width=84, height=26, fg_color="transparent",
            hover_color=SURFACE_RAISED, text_color=TEXT_DIM, font=self._ui_font(10, "normal"),
            command=self._select_visible_chats,
        )
        self._select_all_btn.grid(row=0, column=0, sticky="w")
        self._select_all_btn.grid_remove()
        self._delete_selected_btn = ctk.CTkButton(
            selection_bar, text="Sil (0)", width=65, height=26, fg_color="transparent",
            hover_color=ACCENT_DIM, text_color=ACCENT, font=self._ui_font(10, "bold"),
            command=self._confirm_delete_selected_chats,
        )
        self._delete_selected_btn.grid(row=0, column=1, padx=(0, 4))
        self._delete_selected_btn.grid_remove()
        self._select_chats_btn = ctk.CTkButton(
            selection_bar, text="Seç", width=48, height=26, fg_color="transparent",
            hover_color=SURFACE_RAISED, text_color=TEXT_DIM, font=self._ui_font(10, "normal"),
            command=self._toggle_chat_select_mode,
        )
        self._select_chats_btn.grid(row=0, column=2)
        # İnce ve sönük kaydırma çubuğu: varsayılan açık gri çubuk kenar çubuğunda baskın duruyordu.
        self._chat_list = ctk.CTkScrollableFrame(
            sidebar, fg_color=SURFACE, corner_radius=0, scrollbar_fg_color=SURFACE,
            scrollbar_button_color=BORDER, scrollbar_button_hover_color=TEXT_FAINT,
        )
        self._chat_list.grid(row=4, column=0, sticky="nsew", padx=(7, 9))
        self._chat_list.grid_columnconfigure(0, weight=1)
        self._undo_chat_btn = ctk.CTkButton(
            sidebar, text="↶  Silmeyi geri al", height=30, fg_color="transparent",
            hover_color=SURFACE_RAISED, text_color=TEXT_DIM,
            font=self._ui_font(11, "normal"), command=self._restore_deleted_chat,
        )
        self._undo_chat_btn.grid(row=5, column=0, sticky="ew", padx=14, pady=(4, 0))
        self._undo_chat_btn.grid_remove()
        ctk.CTkFrame(sidebar, height=1, fg_color=BORDER, corner_radius=0).grid(
            row=6, column=0, sticky="ew", padx=14, pady=(10, 10))
        self.sidebar_settings_btn = ctk.CTkButton(
            sidebar, text="⚙   Ayarlar", height=36, anchor="w", fg_color="transparent",
            hover_color=SURFACE_RAISED, text_color=TEXT_DIM, corner_radius=9,
            font=self._ui_font(12, "normal"), command=self._open_settings,
        )
        self.sidebar_settings_btn.grid(row=7, column=0, sticky="ew", padx=14)
        self._chat_notice = ctk.CTkLabel(sidebar, text=self._chat_store_problem, text_color=WARNING,
                                         font=self._ui_font(10, "normal"), wraplength=210)
        self._chat_notice.grid(row=8, column=0, sticky="ew", padx=14, pady=(2, 12))
        ctk.CTkFrame(sidebar, width=1, fg_color=BORDER, corner_radius=0).grid(
            row=0, column=1, rowspan=9, sticky="ns")

    def _schedule_chat_list_refresh(self) -> None:
        """Ara kutusuna yazarken liste her tuşta değil, yazma durunca bir kez güncellenir."""
        if self._chat_list_refresh_id is not None:
            self.after_cancel(self._chat_list_refresh_id)
        self._chat_list_refresh_id = self.after(CHAT_LIST_DEBOUNCE_MS, self._run_scheduled_chat_list_refresh)

    def _run_scheduled_chat_list_refresh(self) -> None:
        self._chat_list_refresh_id = None
        self._refresh_chat_list()

    def _agent_running(self) -> bool:
        return self._agent_future is not None and not self._agent_future.done()

    def _refresh_chat_list(self) -> None:
        """
        Kenar çubuğunu tarihe göre gruplayarak (Bugün, Dün, Önceki 7 gün, Daha eski) YERİNDE günceller:
        değişmeyen satırlara dokunulmaz, yalnız görünümü değişenler yapılandırılır, görünmeyenler yok
        edilir. Seçim modu ya da kompakt liste değişince (nadir) tüm satırlar yeniden kurulur.
        """
        query: str = self._chat_search.get().strip().casefold()
        visible: List[ChatSummary] = [chat for chat in self._chat_index if query in chat["title"].casefold()]
        layout: Tuple[bool, bool] = (self._chat_select_mode, self._appearance["compact_sidebar"])
        if layout != self._chat_rows_layout:
            self._destroy_chat_rows(list(self._chat_rows))
            self._chat_rows_layout = layout
        groups = group_chats_by_date(visible, datetime.now().astimezone())
        wanted: Set[str] = {chat["id"] for group in groups for chat in group["chats"]}
        self._destroy_chat_rows([chat_id for chat_id in self._chat_rows if chat_id not in wanted])
        titles: Set[str] = {group["title"] for group in groups}
        for title in [name for name in self._chat_group_labels if name not in titles]:
            self._chat_group_labels.pop(title).destroy()
        if visible:
            self._hide_empty_label()
        else:
            self._show_empty_label(query)
        running_id: Optional[str] = self._active_chat_id if self._agent_running() else None
        grid_row: int = 0
        for group in groups:
            self._chat_group_label(group["title"]).grid(
                row=grid_row, column=0, sticky="ew", padx=(14, 0), pady=(12 if grid_row else 4, 3))
            grid_row += 1
            for chat in group["chats"]:
                look: ChatRowLook = chat_row_look(
                    chat["title"], chat["id"] == self._active_chat_id, chat["id"] in self._selected_chat_ids,
                    self._chat_select_mode, chat_dot(chat.get("last_outcome"), chat["id"] == running_id),
                )
                row: Optional[ChatRow] = self._chat_rows.get(chat["id"])
                if row is None:
                    row = self._create_chat_row(chat["id"], look)
                    self._chat_rows[chat["id"]] = row
                elif row["look"] != look:
                    self._apply_chat_row_look(row, look)
                row["frame"].grid(row=grid_row, column=0, sticky="ew", padx=3, pady=1 if layout[1] else 2)
                grid_row += 1
        self._delete_selected_btn.configure(
            text=f"Sil ({len(self._selected_chat_ids)})",
            state="normal" if self._selected_chat_ids else "disabled",
        )
        self._sync_chat_title()

    def _sync_chat_title(self) -> None:
        """Üst şeritteki sohbet başlığını etkin sohbete uydurur; uzun başlık '…' ile kısalır."""
        active: Optional[ChatSummary] = next(
            (chat for chat in self._chat_index if chat["id"] == self._active_chat_id), None)
        title: str = active["title"] if active else "Yeni sohbet"
        if len(title) > HEADER_TITLE_MAX_CHARS:
            title = title[:HEADER_TITLE_MAX_CHARS - 1].rstrip() + "…"
        self.chat_title_label.configure(text=title)

    def _destroy_chat_rows(self, chat_ids: List[str]) -> None:
        for chat_id in chat_ids:
            self._chat_rows.pop(chat_id)["frame"].destroy()

    def _chat_group_label(self, title: str) -> ctk.CTkLabel:
        """Tarih grubunun küçük, soluk başlığı; aynı başlık yeniden kullanılır."""
        label: Optional[ctk.CTkLabel] = self._chat_group_labels.get(title)
        if label is None:
            label = ctk.CTkLabel(
                self._chat_list, text=title, anchor="w", text_color=TEXT_FAINT, font=self._ui_font(11, "bold"),
            )
            self._chat_group_labels[title] = label
        return label

    def _show_empty_label(self, query: str) -> None:
        text: str = "Sonuç bulunamadı" if query else "Henüz sohbet yok"
        if self._chat_empty_label is None:
            self._chat_empty_label = ctk.CTkLabel(
                self._chat_list, text=text, text_color=TEXT_FAINT, font=self._ui_font(11, "normal"),
            )
            self._chat_empty_label.grid(row=0, column=0, sticky="ew", pady=14)
        else:
            self._chat_empty_label.configure(text=text)

    def _hide_empty_label(self) -> None:
        if self._chat_empty_label is not None:
            self._chat_empty_label.destroy()
            self._chat_empty_label = None

    def _marker_color(self, tone: str) -> str:
        """Satır sol sütun işaretinin rengi; çalışan noktanın rengi yavaş yanıp sönmeye göre değişir."""
        if tone == DOT_RUNNING:
            return ACCENT if self._dot_bright else ACCENT_MUTED
        return MARKER_COLORS[tone]  # bilinmeyen ton KeyError ile açıkça patlar

    def _create_chat_row(self, chat_id: str, look: ChatRowLook) -> ChatRow:
        """
        Sohbet satırını kurar: sol sütunda durum noktası (seçim modunda ☐/☑), başlık düğmesi ve yalnız
        üzerine gelince görünen '⋯' menü düğmesi. Satırın her yerine tıklamak sohbeti açar.
        """
        compact: bool = self._appearance["compact_sidebar"]
        select_mode: bool = self._chat_select_mode
        activate: Callable[[], None] = (
            (lambda: self._toggle_chat_selection(chat_id)) if select_mode else (lambda: self._open_chat(chat_id))
        )
        frame = ctk.CTkFrame(self._chat_list, fg_color="transparent", corner_radius=9)
        frame.grid_columnconfigure(1, weight=1)
        marker = ctk.CTkLabel(
            frame, text=look["marker"], width=CHAT_MARKER_WIDTH, text_color=self._marker_color(look["tone"]),
            font=self._ui_font(13 if select_mode else 10, "normal"),
        )
        marker.grid(row=0, column=0, padx=(6, 0))
        button = ctk.CTkButton(
            frame, text=look["label"], height=30 if compact else 38, anchor="w", fg_color="transparent",
            hover_color=SURFACE_RAISED if look["highlighted"] else SURFACE_HOVER,
            text_color=TEXT if look["bright"] else TEXT_DIM, font=self._ui_font(12, "normal"), command=activate,
        )
        button.grid(row=0, column=1, sticky="ew")
        more: Optional[ctk.CTkButton] = None
        if not select_mode:
            more = ctk.CTkButton(
                frame, text="⋯", width=29, height=30, fg_color="transparent", hover_color=BORDER,
                text_color=TEXT_DIM, font=self._ui_font(17, "normal"),
                command=lambda: self._show_chat_menu(chat_id, frame),
            )
            more.grid(row=0, column=2, padx=(0, 3))
            more.grid_remove()
            button.bind("<Button-2>", lambda event: self._show_chat_menu(chat_id, event.widget), add="+")
        row: ChatRow = {"frame": frame, "marker": marker, "button": button, "more": more, "look": look,
                        "hover": False}
        for widget in (frame, marker, button, more):
            if widget is not None:
                widget.bind("<Enter>", lambda _event: self._hover_chat_row(chat_id, True), add="+")
                widget.bind("<Leave>", lambda _event: self._hover_chat_row(chat_id, False), add="+")
        for widget in (frame, marker):
            widget.bind("<Button-1>", lambda _event: activate(), add="+")
        self._paint_chat_row(row)
        return row

    def _apply_chat_row_look(self, row: ChatRow, look: ChatRowLook) -> None:
        """Yalnız görünümü değişen satırı yapılandırır (başlık, işaret, vurgu)."""
        row["look"] = look
        row["marker"].configure(text=look["marker"], text_color=self._marker_color(look["tone"]))
        row["button"].configure(
            text=look["label"], text_color=TEXT if look["bright"] else TEXT_DIM,
            hover_color=SURFACE_RAISED if look["highlighted"] else SURFACE_HOVER,
        )
        self._paint_chat_row(row)

    def _paint_chat_row(self, row: ChatRow) -> None:
        """Satır zemini: seçili/etkin satır belirgin, üzerine gelinen satır hafif vurgulu, diğerleri saydam."""
        if row["look"]["highlighted"]:
            background: str = SURFACE_RAISED
        else:
            background = SURFACE_HOVER if row["hover"] else "transparent"
        row["frame"].configure(fg_color=background)

    def _hover_chat_row(self, chat_id: str, inside: bool) -> None:
        """Fare satıra girince zemini vurgular ve '⋯' düğmesini gösterir; satırın kendi çocuklarına geçiş çıkış sayılmaz."""
        row: Optional[ChatRow] = self._chat_rows.get(chat_id)
        if row is None:
            return
        if not inside:
            try:
                under: Optional[tk.Misc] = self.winfo_containing(*self.winfo_pointerxy())
            except KeyError:
                under = None  # Python'un tanımadığı iç Tk penceresi (ör. açılır menü): satırın dışındadır
            if under is not None and path_within(str(under), str(row["frame"])):
                return
        row["hover"] = inside
        self._paint_chat_row(row)
        if row["more"] is not None:
            if inside:
                row["more"].grid()
            else:
                row["more"].grid_remove()

    def _pulse_running_dot(self, now: float) -> None:
        """Çalışan sohbetin durum noktası yavaşça sönüp yanar; yalnız o noktanın rengi değişir."""
        if now - self._last_dot_pulse < DOT_PULSE_INTERVAL:
            return
        self._last_dot_pulse = now
        self._dot_bright = not self._dot_bright
        for row in self._chat_rows.values():
            if row["look"]["tone"] == DOT_RUNNING:
                row["marker"].configure(text_color=self._marker_color(DOT_RUNNING))

    def _toggle_chat_select_mode(self) -> None:
        if self._agent_future is not None:
            return
        self._chat_select_mode = not self._chat_select_mode
        self._selected_chat_ids.clear()
        self._select_chats_btn.configure(text="Bitti" if self._chat_select_mode else "Seç")
        if self._chat_select_mode:
            self._select_all_btn.grid()
            self._delete_selected_btn.grid()
        else:
            self._select_all_btn.grid_remove()
            self._delete_selected_btn.grid_remove()
        self._refresh_chat_list()

    def _toggle_chat_selection(self, chat_id: str) -> None:
        if chat_id in self._selected_chat_ids:
            self._selected_chat_ids.remove(chat_id)
        else:
            self._selected_chat_ids.add(chat_id)
        self._refresh_chat_list()

    def _select_visible_chats(self) -> None:
        query = self._chat_search.get().strip().casefold()
        self._selected_chat_ids.update(
            chat["id"] for chat in self._chat_index if query in chat["title"].casefold()
        )
        self._refresh_chat_list()

    def _confirm_delete_selected_chats(self) -> None:
        count = len(self._selected_chat_ids)
        if count and messagebox.askyesno(
            "Sohbetleri sil", f"Seçilen {count} sohbet silinsin mi?\n\nBu işlem geri alınabilir.", parent=self,
        ):
            self._delete_selected_chats()

    def _delete_selected_chats(self) -> bool:
        if self._agent_future is not None or not self._chat_catalog_writable or not self._selected_chat_ids:
            return False
        if not self._save_current_chat():
            return False
        selected = [chat["id"] for chat in self._chat_index if chat["id"] in self._selected_chat_ids]
        was_active = self._active_chat_id in self._selected_chat_ids
        try:
            remaining, next_active = delete_chats(selected, self._chat_index, self._active_chat_id)
        except (OSError, ValueError) as error:
            self._chat_notice.configure(text=f"Sohbetler silinemedi: {error}")
            return False
        self._chat_index = remaining
        self._last_deleted_chat_ids = selected
        self._undo_chat_btn.configure(text=f"↶  {len(selected)} sohbeti geri al")
        self._undo_chat_btn.grid()
        self._chat_notice.configure(text="")
        self._toggle_chat_select_mode()
        if was_active:
            self._active_chat_id = None
            self._chat_record = None
            if next_active is not None:
                self._open_chat(next_active, save_current=False)
            else:
                self._reset_chat_view(show_welcome=True)
                self._refresh_chat_list()
        return True

    def _focus_chat_search(self) -> None:
        self._chat_search.focus_set()
        self._chat_search.select_range(0, "end")

    def _show_chat_menu(self, chat_id: str, anchor: tk.Misc) -> None:
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Yeniden adlandır", command=lambda: self._prompt_rename_chat(chat_id))
        menu.add_command(label="Sil…", command=lambda: self._confirm_delete_chat(chat_id))
        try:
            menu.tk_popup(anchor.winfo_rootx() + anchor.winfo_width() - 4,
                          anchor.winfo_rooty() + anchor.winfo_height())
        finally:
            menu.grab_release()

    def _prompt_rename_chat(self, chat_id: str) -> None:
        if self._agent_future is not None:
            return
        chat = next((item for item in self._chat_index if item["id"] == chat_id), None)
        if chat is None:
            return
        title = simpledialog.askstring("Sohbeti yeniden adlandır", "Yeni sohbet adı:",
                                       initialvalue=chat["title"], parent=self)
        if title is not None:
            self._rename_chat(chat_id, title)

    def _rename_chat(self, chat_id: str, title: str) -> bool:
        if self._agent_future is not None or not self._chat_catalog_writable:
            return False
        if chat_id == self._active_chat_id and not self._save_current_chat():
            return False
        try:
            updated = rename_chat(chat_id, title, self._chat_index, self._active_chat_id)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            self._chat_notice.configure(text=f"Sohbet adı değiştirilemedi: {error}")
            return False
        self._chat_index = updated
        if self._chat_record is not None and self._chat_record["id"] == chat_id:
            self._chat_record["title"] = next(item["title"] for item in updated if item["id"] == chat_id)
        self._chat_notice.configure(text="")
        self._refresh_chat_list()
        return True

    def _confirm_delete_chat(self, chat_id: str) -> None:
        if self._agent_future is not None:
            return
        chat = next((item for item in self._chat_index if item["id"] == chat_id), None)
        if chat is not None and messagebox.askyesno(
            "Sohbeti sil", f"“{chat['title']}” sohbeti silinsin mi?\n\nSon silinen sohbet geri alınabilir.",
            parent=self,
        ):
            self._delete_chat(chat_id)

    def _delete_chat(self, chat_id: str) -> bool:
        if self._agent_future is not None or not self._chat_catalog_writable:
            return False
        if not self._save_current_chat():
            return False
        was_active: bool = chat_id == self._active_chat_id
        try:
            remaining, next_active = delete_chat(chat_id, self._chat_index, self._active_chat_id)
        except (OSError, ValueError) as error:
            self._chat_notice.configure(text=f"Sohbet silinemedi: {error}")
            return False
        self._chat_index = remaining
        self._last_deleted_chat_ids = [chat_id]
        self._undo_chat_btn.configure(text="↶  Sohbeti geri al")
        self._undo_chat_btn.grid()
        self._chat_notice.configure(text="")
        if was_active:
            self._active_chat_id = None
            self._chat_record = None
            if next_active is not None:
                self._open_chat(next_active, save_current=False)
            else:
                self._reset_chat_view(show_welcome=True)
                self._refresh_chat_list()
        else:
            self._refresh_chat_list()
        return True

    def _restore_deleted_chat(self) -> None:
        if not self._last_deleted_chat_ids or self._agent_future is not None:
            return
        try:
            updated = restore_chats(self._last_deleted_chat_ids, self._chat_index, self._active_chat_id)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            self._chat_notice.configure(text=f"Sohbet geri alınamadı: {error}")
            return
        self._chat_index = updated
        self._last_deleted_chat_ids = []
        self._undo_chat_btn.grid_remove()
        self._chat_notice.configure(text="")
        self._refresh_chat_list()

    def _column_padding(self, inset: int) -> Tuple[int, int]:
        """
        Ana sütundaki bir öğenin (sol, sağ) boşluğu: transkript metin sütununun iki kenarıyla hizalanır
        (sağda kaydırma çubuğunun genişliği de düşülür); `inset`, sol kenardan ek içeri girintidir.
        """
        return self._column_side + inset, self._column_side + self._scrollbar.winfo_reqwidth()

    def _grid_in_column(self, widget: tk.Misc, row: int, inset: int, pady: Tuple[int, int]) -> None:
        """
        Ana pencere sütununun `row` satırına yerleştirir: kenarları transkript metin sütunuyla hizalıdır
        ve pencere yeniden boyutlanınca _align_column_widgets aynı ölçüyle günceller.
        """
        self._column_widgets.append((widget, inset))
        widget.grid(row=row, column=0, sticky="ew", padx=self._column_padding(inset), pady=pady)

    def _align_column_widgets(self) -> None:
        """Üst şerit, durum satırı, composer kartı ve ipucu satırının kenarlarını transkript sütununa hizalar."""
        for widget, inset in self._column_widgets:
            widget.grid_configure(padx=self._column_padding(inset))

    def _build_header(self) -> None:
        """
        Üst şerit: sohbet başlığı ve altında ince bağlam satırı (mesaj sayısı · model), sağda görev durumu,
        kopyala ve ayarlar düğmeleri. Sol ve sağ kenarı transkript sütunuyla hizalıdır (_grid_in_column).
        """
        header: ctk.CTkFrame = ctk.CTkFrame(self._main, fg_color=BG, corner_radius=0)
        self._header: ctk.CTkFrame = header
        self._grid_in_column(header, 0, 0, (20, 0))
        header.grid_columnconfigure(0, weight=1)
        self.chat_title_label = ctk.CTkLabel(
            header, text="Yeni sohbet", text_color=TEXT, font=self._ui_font(19, "bold"), anchor="w",
        )
        self.chat_title_label.grid(row=0, column=0, sticky="w")
        meta: ctk.CTkFrame = ctk.CTkFrame(header, fg_color="transparent")
        meta.grid(row=1, column=0, sticky="w", pady=(0, 12))
        self.context_label: ctk.CTkLabel = ctk.CTkLabel(
            meta, text="bağlam: 0 mesaj", text_color=TEXT_FAINT, anchor="w",
            font=self._ui_font(11, "normal"),
        )
        self.context_label.pack(side="left")
        ctk.CTkLabel(
            meta, text="·", text_color=TEXT_FAINT, font=self._ui_font(11, "normal"),
        ).pack(side="left", padx=7)
        self.model_label: ctk.CTkLabel = ctk.CTkLabel(
            meta, text=self._model_text(DEFAULT_BACKEND), text_color=TEXT_FAINT,
            font=ctk.CTkFont(family=MONO_FAMILY, size=11),
        )
        self.model_label.pack(side="left")
        self.task_status_label: ctk.CTkLabel = ctk.CTkLabel(
            header, text="", text_color=ACCENT,
            font=ctk.CTkFont(family=MONO_FAMILY, size=11, weight="bold"),
        )
        self.task_status_label.grid(row=0, column=1, rowspan=2, sticky="e", padx=(0, 10))
        self.copy_btn: ctk.CTkButton = ctk.CTkButton(
            header, text="", image=self._copy_icon, width=32, height=32, corner_radius=8,
            fg_color="transparent", hover_color=SURFACE_RAISED, border_width=0,
            command=self._copy_transcript, cursor="hand2",
        )
        self.copy_btn.grid(row=0, column=2, rowspan=2, padx=(0, 4))
        # Kopyala düğmesiyle birebir aynı kutu ve ikon boyutu (metin glifi daha küçük kalıyordu).
        self.settings_btn: ctk.CTkButton = ctk.CTkButton(
            header, text="", image=self._gear_icon, width=32, height=32, corner_radius=8,
            fg_color="transparent", hover_color=SURFACE_RAISED, border_width=0,
            command=self._open_settings, cursor="hand2",
        )
        self.settings_btn.grid(row=0, column=3, rowspan=2)
        ctk.CTkFrame(self._main, height=1, fg_color=BORDER_SOFT, corner_radius=0).grid(
            row=0, column=0, sticky="sew")

    def _build_transcript(self) -> None:
        frame: ctk.CTkFrame = ctk.CTkFrame(self._main, fg_color=BG, corner_radius=0)
        frame.grid(row=1, column=0, sticky="nsew", pady=(12, 0))
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(0, weight=1)
        self._transcript_frame: ctk.CTkFrame = frame
        self._body: tkfont.Font = tkfont.Font(family=self._ui_family, size=READING_SIZE)
        # Metin alanı yalnız içerik sütunu genişliğindedir (iki yanında eşit boşluk): Tk etiket
        # arka planları satırın tamamını boyadığı için kod/komut blokları ancak böyle sütuna sığar.
        self._text: tk.Text = tk.Text(
            frame, bg=BG, fg=TEXT, font=self._body, wrap="word", bd=0, highlightthickness=0,
            padx=0, pady=18, insertwidth=0, cursor="arrow", spacing1=0, spacing3=0,
            selectbackground=ACCENT_DIM, selectforeground=TEXT, exportselection=True,
        )
        self._text.grid(row=0, column=0, sticky="nsew", padx=self._column_side)
        scrollbar: ctk.CTkScrollbar = ctk.CTkScrollbar(
            frame, command=self._on_scrollbar, fg_color=BG, button_color=BORDER, button_hover_color=TEXT_FAINT,
        )
        scrollbar.grid(row=0, column=1, sticky="ns")
        self._scrollbar: ctk.CTkScrollbar = scrollbar
        self._text.configure(yscrollcommand=scrollbar.set, state="disabled")
        self._refresh_transcript_fonts()
        frame.bind("<Configure>", lambda _event: self._apply_column_layout())
        frame.bind("<MouseWheel>", self._scroll_transcript)
        # Kullanıcının kaydırması (tekerlek, tuşlar) sondan ayrılıp ayrılmadığını izletir.
        self._text.bind("<MouseWheel>", lambda _event: self._after_user_scroll(), add="+")
        self._text.bind("<Key>", lambda _event: self._after_user_scroll(), add="+")
        self._empty_state = ctk.CTkFrame(frame, fg_color="transparent", corner_radius=0)
        ctk.CTkLabel(self._empty_state, text="", image=self._empty_icon).pack(pady=(0, 20))
        ctk.CTkLabel(
            self._empty_state, text="Ne üzerinde çalışalım?", text_color=TEXT,
            font=self._ui_font(23, "bold"),
        ).pack()
        ctk.CTkLabel(
            self._empty_state, text="Bir görev yazın. İlerlemeyi burada canlı izleyin.",
            text_color=TEXT_FAINT, font=self._ui_font(12, "normal"),
        ).pack(pady=(7, 0))
        self._empty_state.place(relx=0.5, rely=0.45, anchor="center")

    def _build_activity_bar(self) -> None:
        bar: ctk.CTkFrame = ctk.CTkFrame(self._main, fg_color=BG, corner_radius=0, height=26)
        self._grid_in_column(bar, 2, 0, (6, 4))
        bar.grid_columnconfigure(0, weight=1)
        self._activity_bar = bar
        self._activity: tk.Text = tk.Text(
            bar, height=1, bg=BG, fg=TEXT_DIM, bd=0, highlightthickness=0, wrap="none",
            font=(self._ui_family, 13), insertwidth=0, cursor="arrow", padx=0, pady=2,
        )
        self._activity.grid(row=0, column=0, sticky="ew")
        self._configure_activity_tags(self._font_delta())
        self._activity.configure(state="disabled")

    def _configure_activity_tags(self, delta: int) -> None:
        """Akış durum satırının yazı tiplerini ve renklerini yapılandırır (glif vurgu rengi, metin soluk)."""
        self._activity.configure(font=(self._ui_family, 13 + delta))
        self._activity.tag_configure("glyph", foreground=ACCENT, font=(MONO_FAMILY, 13 + delta, "bold"))
        self._activity.tag_configure("meta", foreground=TEXT_DIM)
        self._activity.tag_configure("verb", foreground=TEXT_DIM)
        self._activity.tag_configure("shine", foreground=TEXT)
        self._activity.tag_raise("shine")

    # --- İçerik sütunu ve kullanıcı kabarcıkları ---

    def _apply_column_layout(self) -> None:
        """
        İçerik sütununu pencere genişliğine göre yeniden hesaplar: sütun en çok COLUMN_MAX_WIDTH
        olur, iki yanında eşit boşluk kalır (dar pencerede COLUMN_MIN_SIDE). Metin alanı, üst şerit,
        durum satırı, composer ve gömülü kabarcıklar yeni ölçüye uyar; ölçü değişmediyse hiçbir
        şey yapılmaz.
        """
        available: int = self._transcript_frame.winfo_width() - self._scrollbar.winfo_reqwidth()
        side: int = column_side(available)
        width: int = column_width(available)
        if side == self._column_side and width == self._column_width:
            return
        self._column_side = side
        self._column_width = width
        self._text.grid_configure(padx=side)
        self._align_column_widgets()
        self._relayout_bubbles()
        if self._stick_to_end:
            # Satırlar yeni genişlikte yeniden sarılır; sondaysa görünüm sonda kalır.
            self.after_idle(self._scroll_to_end)

    def _scroll_transcript(self, event: "tk.Event[tk.Misc]") -> str:
        """Kenar boşluğu ve gömülü pencereler üzerindeki tekerlek hareketini transkripte yönlendirir."""
        self._text.yview_scroll(-int(event.delta), "units")
        self._after_user_scroll()
        return "break"

    def _on_scrollbar(self, *arguments: str | float) -> None:
        """Kaydırma çubuğu hareketini metne iletir ve kullanıcının sondan ayrılıp ayrılmadığını izler."""
        self._text.yview(*arguments)
        self._after_user_scroll()

    def _after_user_scroll(self) -> None:
        """Kullanıcı kaydırdı: sınıf bağlamaları bittikten sonra 'sondayım' bilgisini yeniler."""
        self.after_idle(self._sync_stick_to_end)

    def _sync_stick_to_end(self) -> None:
        self._stick_to_end = self._text.yview()[1] >= 0.995

    def _scroll_to_end(self) -> None:
        """
        Transkripti sona kaydırır. Eklenen satırların yerleşimi (özellikle sarılan uzun satırlar)
        boşta çalışan işlerde hesaplanır; hemen kaydırmak sonu kaçırır, bu yüzden önce bekleyen
        boşta işleri tamamlanır. Tüm belgenin satır ölçümü (sync) beklenmez: büyük sohbette saniyeler alır.
        """
        self._text.update_idletasks()
        self._text.see("end")

    def _create_bubble(self, goal: str, sent_at: Optional[str] = None) -> BubbleView:
        """
        Kullanıcı mesajı için yuvarlak köşeli kabarcık penceresini kurar (altında gönderim saati ve
        kopyalama simgesi); henüz yerleştirmez. sent_at None ise saat satırı boş kalır.
        """
        frame: ctk.CTkFrame = ctk.CTkFrame(
            self._text, fg_color=SURFACE_RAISED, bg_color=BG, corner_radius=BUBBLE_RADIUS,
        )
        clip: tk.Frame = tk.Frame(frame, bg=SURFACE_RAISED, bd=0, highlightthickness=0)
        label: tk.Label = tk.Label(
            clip, text=goal, bg=SURFACE_RAISED, fg=TEXT, font=self._body, justify="left",
            anchor="nw", bd=0, highlightthickness=0, padx=0, pady=0,
        )
        label.place(x=0, y=0)
        toggle: tk.Label = tk.Label(
            frame, text="Daha fazla", bg=SURFACE_RAISED, fg=TEXT_DIM, font=(self._ui_family, 12),
            cursor="hand2", bd=0, highlightthickness=0, padx=0, pady=0,
        )
        # Alt satır: gönderim saati ve yalnız bu mesajı kopyalayan simge. Simge düz tk.Label'dir:
        # CTkButton yuvarlak arka planı kabarcığın rengine oturmuyor (master tk.Frame).
        foot: tk.Frame = tk.Frame(frame, bg=SURFACE_RAISED, bd=0, highlightthickness=0)
        time_label: tk.Label = tk.Label(
            foot, text=sent_time_text(sent_at) if sent_at else "", bg=SURFACE_RAISED, fg=TEXT_FAINT,
            font=(self._ui_family, 11), bd=0, highlightthickness=0, padx=0, pady=0,
        )
        copy_btn: tk.Label = tk.Label(
            foot, image=self._copy_photo, bg=SURFACE_RAISED, bd=0, highlightthickness=0,
            padx=0, pady=0, cursor="hand2",
        )
        time_label.pack(side="left")
        copy_btn.pack(side="left", padx=(3, 0))
        clip.grid(row=0, column=0, padx=BUBBLE_PAD_X, pady=BUBBLE_PAD_Y)
        toggle.grid(row=1, column=0, sticky="w", padx=BUBBLE_PAD_X, pady=(0, BUBBLE_PAD_Y - 3))
        foot.grid(row=2, column=0, sticky="e", padx=BUBBLE_PAD_X, pady=(0, BUBBLE_PAD_Y - 3))
        bubble: BubbleView = {"frame": frame, "clip": clip, "label": label, "toggle": toggle,
                              "foot": foot, "time": time_label, "copy": copy_btn, "expanded": False}
        toggle.bind("<Button-1>", lambda _event, view=bubble: self._toggle_bubble(view))
        toggle.bind("<Enter>", lambda _event, link=toggle: link.configure(fg=TEXT))
        toggle.bind("<Leave>", lambda _event, link=toggle: link.configure(fg=TEXT_DIM))
        copy_btn.bind("<Button-1>", lambda _event, message=goal, icon=copy_btn: self._copy_message(message, icon))
        for widget in (frame, clip, label, toggle, foot, time_label, copy_btn):
            widget.bind("<MouseWheel>", self._scroll_transcript)
        return bubble

    def _layout_bubble(self, bubble: BubbleView) -> None:
        """Kabarcığın satır kırma genişliğini ve (uzunsa) katlanmış yüksekliğini geçerli sütuna göre ayarlar."""
        text_width: int = max(
            BUBBLE_MIN_TEXT_WIDTH, int(self._column_width * BUBBLE_MAX_RATIO) - 2 * BUBBLE_PAD_X,
        )
        label: tk.Label = bubble["label"]
        label.configure(wraplength=text_width)
        line_height: int = max(1, self._body.metrics("linespace"))
        full_height: int = label.winfo_reqheight()
        collapsible: bool = round(full_height / line_height) > BUBBLE_COLLAPSE_LINES
        height: int = full_height if bubble["expanded"] or not collapsible else (
            BUBBLE_COLLAPSED_LINES * line_height)
        bubble["clip"].configure(width=label.winfo_reqwidth(), height=height)
        if collapsible:
            bubble["toggle"].configure(text="Daha az" if bubble["expanded"] else "Daha fazla",
                                       font=(self._ui_family, 12 + self._font_delta()))
            bubble["toggle"].grid()
        else:
            bubble["toggle"].grid_remove()

    def _relayout_bubbles(self) -> None:
        """Sütun genişliği ya da yazı ayarı değişince tüm kabarcıkları yeniden ölçer."""
        for bubble in self._bubbles:
            self._layout_bubble(bubble)

    def _toggle_bubble(self, bubble: BubbleView) -> None:
        """Uzun kullanıcı mesajını 'Daha fazla / Daha az' ile açar ya da katlar."""
        bubble["expanded"] = not bubble["expanded"]
        self._layout_bubble(bubble)

    def _embed_bubble(self, index: str, goal: str, sent_at: Optional[str] = None) -> None:
        """Kullanıcı mesajının kabarcığını verilen konuma gömer; satır sağa yaslanır."""
        bubble: BubbleView = self._create_bubble(goal, sent_at)
        self._layout_bubble(bubble)
        self._text.window_create(index, window=bubble["frame"], padx=0, pady=1)
        self._text.tag_add("user_line", index)
        self._bubbles.append(bubble)

    def _restore_user_bubbles(self) -> None:
        """Kayıttan yüklenen sohbette gizli kullanıcı metinlerinin başına kabarcık penceresini yerleştirir."""
        ranges: Tuple[object, ...] = self._text.tag_ranges("user_msg")
        starts: List[str] = [str(item) for item in ranges[0::2]]
        ends: List[str] = [str(item) for item in ranges[1::2]]
        sends: List[str] = list(self._chat_record.get("sends", [])) if self._chat_record is not None else []
        # Kabarcıklar kayıtta gönderim sırasıyla durur; saat listesi aynı sırayı paylaşır (eski
        # kayıtlarda saat yoktur: o kabarcıklar yalnız kopyalama simgesini gösterir).
        messages: List[Tuple[str, str, Optional[str]]] = [
            (start, self._text.get(start, end), sends[index] if index < len(sends) else None)
            for index, (start, end) in enumerate(zip(starts, ends))
        ]
        for start, goal, sent_at in reversed(messages):
            self._embed_bubble(start, goal, sent_at)

    def _composer_menu(self, parent: ctk.CTkFrame, values: Tuple[str, ...], width: int,
                       command: Optional[Callable[[str], None]]) -> ctk.CTkOptionMenu:
        """Composer alt satırındaki küçük, sade açılır menü (mod ve sağlayıcı seçimi)."""
        return ctk.CTkOptionMenu(
            parent, values=list(values), width=width, height=28, corner_radius=8,
            fg_color=SURFACE_RAISED, button_color=SURFACE_RAISED, button_hover_color=BORDER,
            dropdown_fg_color=SURFACE, dropdown_hover_color=SURFACE_RAISED, dropdown_text_color=TEXT,
            text_color=TEXT_DIM, font=self._ui_font(12, "normal"), dropdown_font=self._ui_font(12, "normal"),
            command=command,
        )

    def _build_composer(self) -> None:
        """
        Composer: transkript sütunu genişliğinde tek yuvarlak kart. Üstte çok satırlı giriş (Enter gönderir,
        Shift+Enter yeni satır; içerik kadar büyür), altta mod ve sağlayıcı menüleri, sağda mikrofon ile
        gönder/durdur düğmesi. Kartın üstündeki boş `_composer_attachments` şeridi (ek önizlemeleri) ve alt
        satırın sol başındaki boş `_composer_attach_slot` (ek düğmesi) ileride eklenecek ek (attachment)
        için ayrılmıştır; boşken yer kaplamazlar. Ek altyapısı ayrı bir karardır.
        """
        card: ctk.CTkFrame = ctk.CTkFrame(
            self._main, fg_color=SURFACE, corner_radius=COMPOSER_RADIUS, border_width=1, border_color=BORDER,
        )
        self._composer_card: ctk.CTkFrame = card
        self._grid_in_column(card, 3, 0, (2, 0))
        card.grid_columnconfigure(0, weight=1)
        self._composer_attachments: ctk.CTkFrame = ctk.CTkFrame(card, height=0, fg_color="transparent")
        # pady=(1, 0): alt widget'lar kartın kenarlığının üstüne çizilir; boş şerit bile 1 px üst çizgiyi örterdi.
        self._composer_attachments.grid(row=0, column=0, sticky="ew", padx=COMPOSER_PAD_X, pady=(1, 0))
        self.entry: ComposerInput = ComposerInput(
            card, self._ui_font(14, "normal"), "OmniAgent'a bir görev ver…",
            self._on_primary_button, self._on_composer_focus,
        )
        self.entry.widget.grid(row=1, column=0, sticky="ew", padx=COMPOSER_PAD_X, pady=(16, 10))
        # Kartın dolgusuna/boş alanına tıklamak burada bağlanmaz: CustomTkinter `<Button-1>`'i "all"
        # düzeyinde bağlayıp odağı tıklanan bileşene taşır ve bu bağlama ezilirdi. Tıklama bırakılınca
        # odağı geri alan _keep_composer_focus (__init__) bu işi yapar.
        toolbar: ctk.CTkFrame = ctk.CTkFrame(card, fg_color="transparent")
        toolbar.grid(row=2, column=0, sticky="ew", padx=COMPOSER_PAD_X - 8, pady=(0, 10))
        toolbar.grid_columnconfigure(3, weight=1)
        self._composer_attach_slot: ctk.CTkFrame = ctk.CTkFrame(toolbar, width=0, height=0, fg_color="transparent")
        self._composer_attach_slot.grid(row=0, column=0)
        self.mode_menu: ctk.CTkOptionMenu = self._composer_menu(
            toolbar, RUN_MODE_CHOICES, 92, self._update_mode_hint)
        self.mode_menu.set(RUN_MODE_CHOICES[0])
        self.mode_menu.grid(row=0, column=1, padx=(0, 6))
        self.backend_menu: ctk.CTkOptionMenu = self._composer_menu(toolbar, BACKEND_CHOICES, 132, None)
        self.backend_menu.set("Otomatik")
        self.backend_menu.grid(row=0, column=2)
        self.voice_btn: ctk.CTkButton = ctk.CTkButton(
            toolbar, text="", image=self._voice_icon, width=34, height=34, corner_radius=10,
            fg_color=SURFACE_RAISED, hover_color=SURFACE_RAISED, text_color=TEXT_DIM,
            command=self._toggle_voice, cursor="hand2",
        )
        self.voice_btn.grid(row=0, column=4, padx=(0, 8))
        self.primary_btn: ctk.CTkButton = ctk.CTkButton(
            toolbar, text="↑", width=34, height=34, corner_radius=10, fg_color=ACCENT, hover_color=ACCENT_HOVER,
            text_color=BG, font=ctk.CTkFont(family=self._ui_family, size=17, weight="bold"),
            command=self._on_primary_button,
        )
        self.primary_btn.grid(row=0, column=5)
        # Seçilen modun sınırı (ve açılıştaki Keychain ipucu) kartın altında, giriş metniyle hizalı durur.
        self.mode_hint: ctk.CTkLabel = ctk.CTkLabel(
            self._main, text="", anchor="w", text_color=TEXT_FAINT, font=self._ui_font(11, "normal"),
        )
        self._grid_in_column(self.mode_hint, 4, COMPOSER_PAD_X, (6, 12))
        self._update_mode_hint(self.mode_menu.get())

    def _on_composer_focus(self, focused: bool) -> None:
        """Composer'a odaklanınca kart kenarlığı belirginleşir."""
        self._composer_card.configure(border_color=COMPOSER_FOCUS_BORDER if focused else BORDER)

    def _keep_composer_focus(self, event: "tk.Event[tk.Misc]") -> None:
        """
        Ana pencerede klavye yüzeyi composer'dır. Kartın dolgusuna (yuvarlak tuval), üst şeride, akış
        satırına ya da sohbete tıklamak CustomTkinter'ın "all" bağlaması yüzünden odağı tıklanan bileşene
        bırakıyordu: kullanıcı hemen yazmaya başladığında tuşlar hiçbir yere gitmiyordu (komut düşüyordu).
        Bırakma anında odak geri alınır; kendi yazı alanları (giriş, sohbet arama), açılır menüler ve
        transkriptte metin seçimi dokunulmadan bırakılır.
        """
        target: str = str(event.widget)
        owners: Tuple[tk.Misc, ...] = (self.entry.widget, self._chat_search, self.mode_menu, self.backend_menu)
        if any(path_within(target, str(owner)) for owner in owners):
            return
        if path_within(target, str(self._text)) and self._text.tag_ranges("sel"):
            return  # kullanıcı transkriptten metin seçti: ⌘C odağın bulunduğu bileşenden kopyalar
        self.entry.focus_set()

    def _update_mode_hint(self, label: str) -> None:
        """Seçilen modun çalışma sınırını composer'ın altında görünür kılar."""
        mode: str = RUN_MODE_KEYS.get(label, "normal")
        if mode == "continuous":
            hint: str = "Sürekli · kanıtla tamamlar · /btw ile yön ver"
        else:
            profile = RUN_MODE_PROFILES[mode]
            minutes = int(profile["max_wall_clock_seconds"] / 60)
            hint = f"{profile['max_iterations']} tur · {minutes} dk sınırı"
        self.mode_hint.configure(text=hint, text_color=TEXT_FAINT)

    def _build_footer(self) -> None:
        self.stats_label: ctk.CTkLabel = ctk.CTkLabel(
            self._main, text="", text_color=TEXT_FAINT, font=ctk.CTkFont(family=MONO_FAMILY, size=10),
            anchor="w", justify="left",
        )

    # --- Ayarlar sayfası (API anahtarları, Keychain ile senkron) ---

    def _apply_settings(self, values: Dict[str, str]) -> Tuple[List[str], List[str]]:
        """
        Ayarlar sayfasındaki değerleri Keychain'e ve süreç-içi depoya uygular; (kaydedilen,
        başarısız) değişken listelerini döner. Anahtarlar os.environ'a YAZILMAZ: ajanın
        başlattığı alt süreçler sırrı miras almaz. Değişmeyen alan yazılmaz, boş değer kaydı siler.
        """
        changed: List[str] = []
        failed: List[str] = []
        for variable in api_keys.KEY_VARIABLES:
            if variable not in values:
                continue
            value: str = values[variable].strip()
            persisted: str = api_keys.stored_key(variable) or ""
            # Yalnız hem Keychain'de hem süreç-içi depoda aynı değer duruyorsa yazma atlanır;
            # kabukta tanımlı bir anahtar birebir yazılsa bile kalıcı kayda geçirilir.
            if value == persisted and (not value or api_key_source(variable) == "ayarlar"):
                continue
            try:
                api_keys.store_key(variable, value)
            except Exception as error:
                # Anahtarın kendisi hiçbir zaman log'a yazılmaz; yalnız hata türü kaydedilir.
                logging.warning(
                    "API anahtarı Keychain'e yazılamadı veya silinemedi",
                    extra={"variable": variable, "error_type": type(error).__name__},
                )
                failed.append(variable)
                continue
            set_api_key(variable, value)
            changed.append(variable)
        if changed:
            refresh_api_keys()
        return changed, failed

    def _apply_model_settings(self, values: Dict[str, str]) -> List[str]:
        """Model seçimlerini kalıcı kaydeder; çalışan görevin modelini değiştirmez."""
        changed: Dict[str, str] = {}
        for name, value in values.items():
            model = value.strip()
            if name not in BACKENDS or not valid_model_id(model):
                raise ValueError(f"{name}: geçerli bir model kimliği girin.")
            current = self._pending_model_choices.get(name, BACKENDS[name]["model"])
            if model != current:
                changed[name] = model
        if changed:
            selections = load_model_preferences()
            selections.update(changed)
            save_model_preferences(selections)
            self._pending_model_choices.update(changed)
        return list(changed)

    def _apply_continuous_limits(self, hours_text: str, tokens_text: str) -> bool:
        """Sürekli mod sınırlarını doğrular, değiştiyse kaydeder; sonraki sürekli görevde geçerlidir."""
        limits: ContinuousLimits = parse_continuous_limits(hours_text, tokens_text)
        path: Path = continuous_limits_path()
        try:
            unchanged: bool = load_continuous_limits(path) == limits
        except (OSError, ValueError):
            # Bozuk kayıt, kullanıcının girdiği geçerli değerle bilerek üzerine yazılır.
            unchanged = False
        if unchanged:
            return False
        save_continuous_limits(path, limits)
        return True

    # --- Açılış: Keychain ve model istemcileri ilk boyamadan sonra arka planda ---

    def _after_first_paint(self, action: Callable[[], None]) -> None:
        """
        Eylemi ilk boyama turu bittikten sonra çalıştırır: boşta işleri (yerleşim, çizim) bittikten sonra
        bir kısa bekleme turu geçer. update() bilerek kullanılmaz: mainloop öncesinde kare, after ve
        <Map> geri çağrılarını yarım kurulmuş nesnede yeniden çalıştırırdı.
        """
        self.after_idle(lambda: self.after(FIRST_PAINT_DELAY_MS, action))

    def _post_paint_init(self) -> None:
        """İlk kare çizildikten sonra: sohbet listesini kurar ve son açık sohbeti yükler."""
        self._first_paint_done = True
        # Liste koşulsuz kurulur: son açık sohbetin dosyası bozuksa _open_chat erken döner ve liste boş kalırdı.
        self._refresh_chat_list()
        if self._active_chat_id is not None and self._chat_record is None:
            self._open_chat(self._active_chat_id, save_current=False)

    def begin_background_startup(self) -> None:
        """
        main() çağırır: Keychain okuması ve istemci kurulumu ilk boyamadan sonra arka planda başlar. Bitene
        dek görev göndermek ve Ayarlar açmak bekletilir (bayat Keychain değeri yarışı olmasın).
        """
        self._startup_pending = True
        self._after_first_paint(self._start_startup_worker)

    def _start_startup_worker(self) -> None:
        future: "Future[Dict[str, AsyncOpenAI]]" = Future()
        self._startup_future = future
        future.add_done_callback(lambda done: self._call_in_tk(lambda: self._apply_startup(done)))
        threading.Thread(target=_run_startup, args=(future,), name="omni-startup", daemon=True).start()
        self.after(STARTUP_HINT_DELAY_MS, self._hint_if_startup_slow)
        self.after(KEYCHAIN_WAIT_LIMIT_MS, self._release_startup_gate)

    def _keychain_busy(self) -> bool:
        return self._startup_future is not None and not self._startup_future.done()

    def _show_startup_hint(self) -> None:
        self.mode_hint.configure(text=KEYCHAIN_HINT, text_color=WARNING)

    def _hint_if_startup_slow(self) -> None:
        """Keychain 1,5 sn içinde yanıt vermediyse (macOS izin penceresi olabilir) ipucu ve durum gösterilir."""
        if self._keychain_busy():
            self._set_task_status("keys")
            self._show_startup_hint()

    def _release_startup_gate(self) -> None:
        """Keychain uzun süre yanıt vermezse görev gönderme açılır; anahtarlar yanıt gelince yüklenir."""
        if not self._startup_pending:
            return
        self._startup_pending = False
        if self._task_status == "keys":
            self._set_task_status("idle")
        self.mode_hint.configure(text=KEYCHAIN_LATE_HINT, text_color=WARNING)

    def _apply_startup(self, done: "Future[Dict[str, AsyncOpenAI]]") -> None:
        """Tk thread'i: açılış işinin sonucunu uygular; işçinin istisnası burada yeniden yükselir."""
        self._startup_pending = False
        if self._task_status == "keys":
            self._set_task_status("idle")
        self._update_mode_hint(self.mode_menu.get())
        clients: Dict[str, AsyncOpenAI] = done.result()
        if self._agent_running():
            self._clients_stale = True  # görev bitince _rebuild_clients yeni anahtarlarla kurar
            self._retire_clients(clients)  # kullanılmayacak istemcilerin bağlantı havuzu kapatılır
            return
        replaced: Dict[str, AsyncOpenAI] = self._clients
        self._clients = clients
        self.model_label.configure(text=self._model_text(DEFAULT_BACKEND))
        self._retire_clients(replaced)

    def _retire_clients(self, replaced: Dict[str, AsyncOpenAI]) -> None:
        """Eski bağlantı havuzunu event loop'ta kapatır; kapanış hatası döngüyü düşürmez."""
        if not replaced:
            return

        async def retire() -> None:
            try:
                await close_model_clients(replaced)
            except Exception as error:
                logging.warning("Eski model istemcileri kapatılamadı",
                                extra={"error_type": type(error).__name__})

        # Eski bağlantı havuzu yalnız çalışan görev yokken kapatılır.
        asyncio.run_coroutine_threadsafe(retire(), self._loop)

    def _rebuild_clients(self) -> None:
        """Model istemcilerini yeni anahtarlarla kurar; görev sürüyorsa görev bitince uygular."""
        if self._agent_future is not None and not self._agent_future.done():
            self._clients_stale = True
            return
        replaced: Dict[str, AsyncOpenAI] = self._clients
        for name, model in self._pending_model_choices.items():
            set_backend_model(name, model)
        self._pending_model_choices.clear()
        self._clients = create_model_clients()
        self.model_label.configure(text=self._model_text(DEFAULT_BACKEND))
        self._clients_stale = False
        self._retire_clients(replaced)

    def _open_settings(self) -> None:
        """Tema ile uyumlu Ayarlar sayfasını açar; kaydedilen anahtarlar anında etkinleşir."""
        existing: Optional[ctk.CTkToplevel] = self._settings_window
        if existing is not None and existing.winfo_exists():
            existing.lift()
            existing.focus_force()
            return
        if self._startup_pending or self._keychain_busy():
            # Açılıştaki Keychain okuması sürerken Ayarlar açılırsa bayat anahtar değerleri gösterilirdi
            # (kapı 45 sn sonra açılsa da okuma bitene dek kapalı kalır).
            self._show_startup_hint()
            return
        if self.state() == "iconic":
            # Simge durumundaki ana pencerenin geçici (transient) Ayarlar penceresi Tk'de olay döngüsünü
            # kilitler (Configure fırtınası; deneyle doğrulandı): önce ana pencere geri getirilir.
            set_application_hidden(False, self)
        window: ctk.CTkToplevel = ctk.CTkToplevel(self)
        self._settings_window = window
        window.title("OmniAgent — Ayarlar")
        window.geometry("720x760")
        window.minsize(600, 520)
        window.configure(fg_color=BG)
        window.attributes("-alpha", self._window_alpha())
        window.transient(self)
        top = ctk.CTkFrame(window, fg_color=BG, corner_radius=0)
        top.pack(fill="x", padx=30, pady=(26, 18))
        ctk.CTkLabel(
            top, text="Ayarlar", text_color=TEXT, font=self._ui_font(25, "bold"), anchor="w",
        ).pack(anchor="w")
        ctk.CTkLabel(
            top, text="Modelleri, anahtarları ve çalışma sınırlarını yönet.", text_color=TEXT_FAINT,
            font=self._ui_font(12, "normal"), anchor="w",
        ).pack(anchor="w", pady=(1, 0))
        ctk.CTkFrame(window, height=1, fg_color=BORDER, corner_radius=0).pack(fill="x")
        panel: ctk.CTkScrollableFrame = ctk.CTkScrollableFrame(window, fg_color=BG)
        panel.pack(fill="both", expand=True, padx=18, pady=(12, 0))
        ctk.CTkLabel(
            panel, text="Görünüm", text_color=TEXT, anchor="w", font=self._ui_font(16, "bold"),
        ).pack(anchor="w", padx=12, pady=(8, 8))
        appearance_card = ctk.CTkFrame(
            panel, fg_color=SURFACE, corner_radius=12, border_width=1, border_color=BORDER,
        )
        appearance_card.pack(fill="x", padx=12, pady=(0, 8))
        appearance_card.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(
            appearance_card, text="Yazı tipi", text_color=TEXT, font=self._ui_font(12, "bold"),
        ).grid(row=0, column=0, sticky="w", padx=14, pady=(15, 8))
        installed_fonts = set(tkfont.families())
        font_choices = list(dict.fromkeys(
            name for name in (self._system_ui_family, "Helvetica Neue", "Avenir Next", "Arial", "Menlo")
            if name in installed_fonts
        ))
        font_menu = ctk.CTkOptionMenu(
            appearance_card, values=font_choices, width=180, height=32, corner_radius=8,
            fg_color=SURFACE_RAISED, button_color=SURFACE_RAISED, button_hover_color=BORDER,
            dropdown_fg_color=SURFACE, dropdown_hover_color=SURFACE_RAISED,
            text_color=TEXT, dropdown_text_color=TEXT, font=self._ui_font(12, "normal"),
        )
        font_menu.set(self._ui_family)
        font_menu.grid(row=0, column=1, sticky="e", padx=14, pady=(15, 8))
        ctk.CTkLabel(
            appearance_card, text="Yazı boyutu", text_color=TEXT, font=self._ui_font(12, "bold"),
        ).grid(row=1, column=0, sticky="w", padx=14, pady=8)
        size_value = ctk.CTkLabel(
            appearance_card, text=f"{self._appearance['font_size']} pt", text_color=TEXT_DIM,
            font=self._ui_font(11, "normal"), width=42,
        )
        size_value.grid(row=1, column=2, padx=(0, 14))
        size_slider = ctk.CTkSlider(
            appearance_card, from_=11, to=18, number_of_steps=7,
            fg_color=BORDER, progress_color=ACCENT, button_color=ACCENT,
            button_hover_color=ACCENT_HOVER,
            command=lambda value: size_value.configure(text=f"{round(value)} pt"),
        )
        size_slider.set(self._appearance["font_size"])
        size_slider.grid(row=1, column=1, sticky="ew", padx=14, pady=8)
        transparency_var = tk.BooleanVar(value=self._appearance["transparent_window"])
        opacity_value = ctk.CTkLabel(
            appearance_card, text=f"%{round(self._appearance['opacity'] * 100)}", text_color=TEXT_DIM,
            font=self._ui_font(11, "normal"), width=42,
        )
        opacity_slider = ctk.CTkSlider(
            appearance_card, from_=0.65, to=1.0, number_of_steps=35,
            fg_color=BORDER, progress_color=ACCENT, button_color=ACCENT,
            button_hover_color=ACCENT_HOVER,
            command=lambda value: opacity_value.configure(text=f"%{round(value * 100)}"),
        )
        opacity_slider.set(self._appearance["opacity"])

        def toggle_opacity() -> None:
            opacity_slider.configure(state="normal" if transparency_var.get() else "disabled")

        ctk.CTkSwitch(
            appearance_card, text="Pencere saydamlığı", variable=transparency_var,
            command=toggle_opacity, fg_color=BORDER, progress_color=ACCENT,
            button_color=TEXT, text_color=TEXT, font=self._ui_font(12, "bold"),
        ).grid(row=2, column=0, columnspan=3, sticky="w", padx=14, pady=(14, 3))
        ctk.CTkLabel(
            appearance_card, text="Opaklık", text_color=TEXT_DIM, font=self._ui_font(12, "normal"),
        ).grid(row=3, column=0, sticky="w", padx=14, pady=8)
        opacity_slider.grid(row=3, column=1, sticky="ew", padx=14, pady=8)
        opacity_value.grid(row=3, column=2, padx=(0, 14))
        toggle_opacity()
        compact_var = tk.BooleanVar(value=self._appearance["compact_sidebar"])
        ctk.CTkSwitch(
            appearance_card, text="Kompakt sohbet listesi", variable=compact_var,
            fg_color=BORDER, progress_color=ACCENT, button_color=TEXT,
            text_color=TEXT, font=self._ui_font(12, "normal"),
        ).grid(row=4, column=0, columnspan=3, sticky="w", padx=14, pady=(10, 4))
        ctk.CTkLabel(
            appearance_card, text="Saydamlık pencerenin tamamına uygulanır.",
            text_color=TEXT_FAINT, font=self._ui_font(10, "normal"),
        ).grid(row=5, column=0, columnspan=3, sticky="w", padx=14, pady=(0, 14))
        ctk.CTkLabel(
            panel, text="API Anahtarları", text_color=TEXT, anchor="w",
            font=self._ui_font(16, "bold"),
        ).pack(anchor="w", padx=12, pady=(18, 3))
        ctk.CTkLabel(
            panel, text=SETTINGS_HINT, text_color=TEXT_DIM, wraplength=500, justify="left",
            font=self._ui_font(11, "normal"), anchor="w",
        ).pack(anchor="w", padx=12, pady=(0, 12))

        entries: Dict[str, ctk.CTkEntry] = {}
        chips: Dict[str, ctk.CTkLabel] = {}
        for variable in api_keys.KEY_VARIABLES:
            card: ctk.CTkFrame = ctk.CTkFrame(
                panel, fg_color=SURFACE, corner_radius=12, border_width=1, border_color=BORDER,
            )
            card.pack(fill="x", padx=12, pady=5)
            card.grid_columnconfigure(0, weight=1)
            profiles: List[str] = [name for name, var in API_KEY_VARIABLES.items() if var == variable]
            ctk.CTkLabel(
                card, text=variable, text_color=TEXT, anchor="w",
                font=self._ui_font(12, "bold"),
            ).grid(row=0, column=0, sticky="w", padx=14, pady=(12, 0))
            chip: ctk.CTkLabel = ctk.CTkLabel(
                card, text="", anchor="e", font=ctk.CTkFont(family=MONO_FAMILY, size=10),
            )
            chip.grid(row=0, column=1, sticky="e", padx=14, pady=(12, 0))
            entry: ctk.CTkEntry = ctk.CTkEntry(
                card, show="•", height=36, fg_color=BG, border_width=1, border_color=BORDER,
                text_color=TEXT, placeholder_text="anahtar gir", placeholder_text_color=TEXT_FAINT,
                font=ctk.CTkFont(family=MONO_FAMILY, size=12),
            )
            entry.grid(row=1, column=0, columnspan=2, sticky="ew", padx=14, pady=(8, 4))
            entry.insert(0, load_api_key(variable) or "")
            entries[variable] = entry
            chips[variable] = chip
            detail: str = " · ".join(
                f"{name} → {BACKENDS[name]['model']} ({BACKENDS[name]['base_url']})" for name in profiles
            )
            ctk.CTkLabel(
                card, text=detail, text_color=TEXT_FAINT, wraplength=500, justify="left", anchor="w",
                font=ctk.CTkFont(family=MONO_FAMILY, size=9),
            ).grid(row=2, column=0, columnspan=2, sticky="w", padx=14, pady=(0, 12))

        def refresh_chips() -> None:
            """Rozet kaynağı da söyler: Ayarlar kaydı kabuk değişkenini geçersiz kılar."""
            for variable, chip in chips.items():
                source: str = api_key_source(variable)
                label: str = {"ayarlar": "ayarlardan", "ortam": "ortam değişkeni"}.get(source, "yok")
                if source == "ayarlar" and api_keys.key_environment_present(variable):
                    label = "ayarlardan (kabukta da var)"
                chip.configure(text=label, text_color=SUCCESS if source != "yok" else TEXT_FAINT)

        refresh_chips()

        ctk.CTkLabel(
            panel, text="Modeller", text_color=TEXT, anchor="w",
            font=self._ui_font(16, "bold"),
        ).pack(anchor="w", padx=12, pady=(24, 3))
        ctk.CTkLabel(
            panel, text=(
                "Sağlayıcı modelleri arka planda yüklenir. Ollama listesi yerel sunucudan "
                "(127.0.0.1:11434) gelir; eksik bulut modelini `ollama pull <ad>` ile kurun. "
                "Listede görünmeyen bir model adını kutuya elle yazabilirsiniz. "
                "Seçim sonraki görevde kullanılır."
            ),
            text_color=TEXT_DIM, wraplength=560, justify="left", anchor="w",
            font=self._ui_font(11, "normal"),
        ).pack(anchor="w", padx=12, pady=(0, 8))
        model_selectors: Dict[str, ctk.CTkComboBox] = {}
        model_notes: Dict[str, ctk.CTkLabel] = {}
        for name, profile in BACKENDS.items():
            card = ctk.CTkFrame(
                panel, fg_color=SURFACE, corner_radius=12, border_width=1, border_color=BORDER,
            )
            card.pack(fill="x", padx=12, pady=5)
            ctk.CTkLabel(
                card, text=name, text_color=TEXT, anchor="w",
                font=ctk.CTkFont(family=MONO_FAMILY, size=11, weight="bold"),
            ).pack(anchor="w", padx=14, pady=(10, 2))
            variable = API_KEY_VARIABLES.get(name)
            key = entries[variable].get().strip() if variable else None
            choices = list(dict.fromkeys((profile["model"],) + cached_models(profile["provider"], key)))
            selector = ctk.CTkComboBox(
                card, values=choices, height=30, fg_color=SURFACE_RAISED,
                border_color=BORDER, button_color=SURFACE_RAISED,
                button_hover_color=BORDER, text_color=TEXT, dropdown_fg_color=SURFACE,
                dropdown_text_color=TEXT, font=ctk.CTkFont(family=MONO_FAMILY, size=11),
            )
            selector.pack(fill="x", padx=14, pady=(2, 4))
            selector.set(profile["model"])
            model_selectors[name] = selector
            note = ctk.CTkLabel(
                card, text="", text_color=TEXT_FAINT, anchor="w", wraplength=500, justify="left",
                font=ctk.CTkFont(family=MONO_FAMILY, size=9),
            )
            note.pack(anchor="w", padx=14, pady=(0, 9))
            model_notes[name] = note

        ctk.CTkLabel(
            panel, text="Sürekli Mod", text_color=TEXT, anchor="w",
            font=self._ui_font(16, "bold"),
        ).pack(anchor="w", padx=12, pady=(24, 3))
        ctk.CTkLabel(
            panel, text=(
                "Sürekli görev sen durdurana, sınır dolana veya hedef kanıtla doğrulanana kadar çalışır; "
                "bilgisayar başında olmasan da bağımsız işleri sürdürür. Çalışırken /btw <mesaj> ile yön verirsin. "
                "Tamamlanma onayı beklemez; eksik izin veya bilgi varsa bağımsız adımları sürdürür."
            ),
            text_color=TEXT_DIM, wraplength=560, justify="left", anchor="w",
            font=self._ui_font(11, "normal"),
        ).pack(anchor="w", padx=12, pady=(0, 8))
        limits_card: ctk.CTkFrame = ctk.CTkFrame(
            panel, fg_color=SURFACE, corner_radius=12, border_width=1, border_color=BORDER,
        )
        limits_card.pack(fill="x", padx=12, pady=5)
        limits_problem: str = ""
        try:
            stored_limits: Optional[ContinuousLimits] = load_continuous_limits(continuous_limits_path())
        except (OSError, ValueError) as error:
            stored_limits = None
            limits_problem = f"Kayıtlı sınır okunamadı; geçerli değer girip kaydedin: {error}"
        limit_fields: Dict[str, ctk.CTkEntry] = {}
        for key, label in (("max_hours", "En uzun süre (saat)"),
                           ("max_total_tokens", "Token sınırı (giriş + çıkış)")):
            ctk.CTkLabel(
                limits_card, text=label, text_color=TEXT, anchor="w", font=self._ui_font(12, "bold"),
            ).pack(anchor="w", padx=14, pady=(10, 2))
            field = ctk.CTkEntry(
                limits_card, height=30, fg_color=SURFACE_RAISED, border_color=BORDER, text_color=TEXT,
                font=ctk.CTkFont(family=MONO_FAMILY, size=11),
            )
            field.pack(fill="x", padx=14, pady=(0, 4))
            if stored_limits is not None:
                field.insert(0, f"{stored_limits[key]:g}" if key == "max_hours" else str(stored_limits[key]))
            limit_fields[key] = field
        ctk.CTkLabel(
            limits_card, text=limits_problem, text_color=WARNING, anchor="w", wraplength=540,
            justify="left", font=ctk.CTkFont(family=MONO_FAMILY, size=9),
        ).pack(anchor="w", padx=14, pady=(0, 9))

        generation = [0]
        active_model_requests: List[Future[Tuple[str, ...]]] = []

        def refresh_models(force: bool = False) -> None:
            """Ağ işini model döngüsüne verir; Tk yalnız hazır sonucu çizer."""
            generation[0] += 1
            for previous in active_model_requests:
                previous.cancel()
            active_model_requests.clear()
            current_generation = generation[0]
            pending: Dict[str, Tuple[Future[Tuple[str, ...]], Optional[str]]] = {}
            profiles_by_provider: Dict[str, List[str]] = {}
            for name, profile in BACKENDS.items():
                provider = profile["provider"]
                profiles_by_provider.setdefault(provider, []).append(name)
                if provider in pending:
                    continue
                variable = API_KEY_VARIABLES.get(name)
                key = entries[variable].get().strip() if variable else None
                if provider in ("openai", "openrouter") and not key:
                    for profile_name in profiles_by_provider[provider]:
                        model_notes[profile_name].configure(text="Liste için API anahtarı girin.")
                    continue
                pending[provider] = (
                    asyncio.run_coroutine_threadsafe(
                        list_provider_models(provider, profile["base_url"], key, refresh=force),
                        self._loop,
                    ),
                    key,
                )
            active_model_requests.extend(request for request, _ in pending.values())
            for provider in pending:
                for name in profiles_by_provider[provider]:
                    model_notes[name].configure(text="Modeller yükleniyor…")
            if not pending:
                return

            def poll() -> None:
                if current_generation != generation[0] or not window.winfo_exists():
                    return
                for provider, (future, provider_key) in list(pending.items()):
                    if not future.done():
                        continue
                    del pending[provider]
                    try:
                        models = future.result()
                    except ModelCatalogError as error:
                        message = str(error)
                    except Exception:
                        message = "Model listesi alınamadı."
                    else:
                        message = f"{len(models)} model · seçim için listeyi açın"
                        # Bulut kaydının yerel adı ile ollama.com adı farklı olabilir:
                        # `gemma4:cloud` → `gemma4:31b` eşlemesi kullanıcıya görünür olsun.
                        targets = cached_remote_targets(provider, provider_key)
                        if targets:
                            pairs: str = " · ".join(
                                f"{name} → {remote}" for name, remote in sorted(targets.items())[:3]
                            )
                            message += f" · ollama.com: {pairs}"
                        for name in profiles_by_provider[provider]:
                            selector = model_selectors[name]
                            selected = selector.get()
                            selector.configure(values=list(dict.fromkeys((selected,) + models)))
                            selector.set(selected)
                    for name in profiles_by_provider[provider]:
                        model_notes[name].configure(text=message)
                if pending:
                    window.after(120, poll)

            window.after(120, poll)

        ctk.CTkButton(
            panel, text="Modelleri yenile", width=130, height=28, corner_radius=8,
            fg_color=SURFACE_RAISED, hover_color=BORDER, text_color=TEXT_DIM,
            font=self._ui_font(11, "normal"), command=lambda: refresh_models(True),
        ).pack(anchor="w", padx=12, pady=(6, 4))
        refresh_models()
        reveal: tk.BooleanVar = tk.BooleanVar(value=False)

        def toggle_visibility() -> None:
            show: str = "" if reveal.get() else "•"
            for field in entries.values():
                field.configure(show=show)

        ctk.CTkCheckBox(
            panel, text="Anahtarları göster", variable=reveal, command=toggle_visibility,
            fg_color=ACCENT, hover_color=ACCENT_HOVER, border_color=BORDER, checkmark_color=BG,
            text_color=TEXT_DIM, font=ctk.CTkFont(family=MONO_FAMILY, size=11),
        ).pack(anchor="w", padx=12, pady=(6, 2))
        status: ctk.CTkLabel = ctk.CTkLabel(
            window, text="", text_color=TEXT_FAINT, anchor="w", wraplength=540, justify="left",
            font=ctk.CTkFont(family=MONO_FAMILY, size=10),
        )
        status.pack(fill="x", padx=30, pady=(4, 8))

        def close() -> None:
            generation[0] += 1
            for request in active_model_requests:
                request.cancel()
            self._settings_window = None
            window.destroy()

        def save() -> None:
            try:
                appearance_changed = self._apply_appearance({
                    "font_family": "" if font_menu.get() == self._system_ui_family else font_menu.get(),
                    "font_size": round(size_slider.get()),
                    "transparent_window": transparency_var.get(),
                    "opacity": opacity_slider.get(),
                    "compact_sidebar": compact_var.get(),
                })
                appearance_error = ""
            except (OSError, ValueError, tk.TclError) as error:
                appearance_changed = False
                appearance_error = str(error)
            changed, failed = self._apply_settings(
                {variable: field.get() for variable, field in entries.items()}
            )
            try:
                model_changed = self._apply_model_settings(
                    {name: selector.get() for name, selector in model_selectors.items()}
                )
                model_error = ""
            except (OSError, ValueError) as error:
                model_changed = []
                model_error = str(error)
            try:
                limits_changed: bool = self._apply_continuous_limits(
                    limit_fields["max_hours"].get(), limit_fields["max_total_tokens"].get(),
                )
                limits_error = ""
            except (OSError, ValueError) as error:
                limits_changed = False
                limits_error = str(error)
            for variable, field in entries.items():
                field.delete(0, "end")
                field.insert(0, load_api_key(variable) or "")
            refresh_chips()
            if changed:
                refresh_models()
            # Kısmi başarıda da istemciler yenilenir: kaydedilen anahtar beklemesin.
            if changed or model_changed:
                self._rebuild_clients()
            ready: str = ", ".join(sorted(self._clients)) or "yok"
            suffix: str = " (çalışan görev bitince uygulanacak)" if self._clients_stale else ""
            # Kabukta kalmaya devam eden değişkenler "silindi" yanılgısını önler.
            shell_left: List[str] = [
                variable for variable in changed if api_key_source(variable) == "ortam"
            ]
            if shell_left:
                suffix += f" · kabuk değişkeni hâlâ tanımlı: {', '.join(shell_left)}"
            if changed:
                self._text.configure(state="normal")
                self._new_region([(f"⚙ Ayarlar: {len(changed)} anahtar kaydedildi · hazır profiller: {ready}{suffix}\n",
                                   ("notice_info",))])
                self._text.see("end")
                self._text.configure(state="disabled")
            if failed or model_error or limits_error or appearance_error:
                problems = []
                if appearance_error:
                    problems.append("Görünüm: " + appearance_error)
                if failed:
                    problems.append("API anahtarı: " + ", ".join(failed))
                if model_error:
                    problems.append("Model: " + model_error)
                if limits_error:
                    problems.append("Sürekli mod: " + limits_error)
                status.configure(text="Kaydedilemedi: " + " · ".join(problems), text_color=ERROR)
                return
            if not changed and not model_changed and not limits_changed and not appearance_changed:
                status.configure(text="Değişiklik yok.", text_color=TEXT_FAINT)
                return
            limits_note: str = " · sürekli mod sınırları" if limits_changed else ""
            appearance_note: str = " · görünüm" if appearance_changed else ""
            status.configure(
                text=f"Kaydedildi · {len(model_changed)} model{limits_note}{appearance_note} · hazır profiller: {ready}{suffix}",
                text_color=WARNING if self._clients_stale or shell_left else SUCCESS,
            )

        ctk.CTkFrame(window, height=1, fg_color=BORDER, corner_radius=0).pack(fill="x")
        buttons: ctk.CTkFrame = ctk.CTkFrame(window, fg_color=BG, corner_radius=0)
        buttons.pack(fill="x", padx=30, pady=16)
        ctk.CTkButton(
            buttons, text="Kaydet", width=110, height=32, corner_radius=8, fg_color=ACCENT,
            hover_color=ACCENT_HOVER, text_color=BG, font=self._ui_font(13, "bold"), command=save,
        ).pack(side="right")
        ctk.CTkButton(
            buttons, text="Kapat", width=90, height=32, corner_radius=8, fg_color="transparent",
            hover_color=SURFACE_RAISED, border_width=1, border_color=BORDER, text_color=TEXT_DIM,
            font=self._ui_font(12, "normal"), command=close,
        ).pack(side="right", padx=(0, 8))
        window.protocol("WM_DELETE_WINDOW", close)
        window.after(60, window.lift)
        self._schedule_titlebar_style(window)

    def _schedule_titlebar_style(self, window: ctk.CTkToplevel) -> None:
        """
        Başlık çubuğu stilini macOS penceresi oluştuktan sonra uygular; böylece Ayarlar
        penceresi de ana pencereyle aynı arka plan renginde görünür.
        """
        title: str = window.title()
        window.after(120, lambda: self._style_native_window(title))

    # --- Transkript bölgeleri (etiket tabanlı; her bölge '\n' ile biter, asla boş kalmaz) ---

    def _prepare_tag(self, tag: str) -> None:
        """
        Etiketin tıklama davranışını ve başlangıç durumunu ilk kullanımda kurar: bağlantılar
        tarayıcıda açılır, katlama başlıkları aç/kapa olur, kayıttan yüklenen katlama bölümleri
        (bu oturumda açılmamış) kapalı gelir.
        """
        if tag in self._prepared_tags:
            return
        if tag.startswith("md_href:"):
            url: str = tag.removeprefix("md_href:")
            self._text.tag_bind(tag, "<Button-1>", lambda _event, target=url: webbrowser.open(target))
            self._text.tag_bind(tag, "<Enter>", lambda _event: self._text.configure(cursor="hand2"))
            self._text.tag_bind(tag, "<Leave>", lambda _event: self._text.configure(cursor="arrow"))
            self._prepared_tags.add(tag)
            return
        fold: Optional[Tuple[str, str, int]] = parse_fold_tag(tag)
        if fold is None:
            return
        kind, part, ident = fold
        self._prepared_tags.add(tag)
        if part == "h":
            self._text.tag_bind(tag, "<Button-1>", lambda _event, k=kind, i=ident: self._toggle_fold(k, i))
            self._text.tag_bind(tag, "<Enter>", lambda _event, k=kind, i=ident: self._hover_fold(k, i, True))
            self._text.tag_bind(tag, "<Leave>", lambda _event, k=kind, i=ident: self._hover_fold(k, i, False))
        elif part in ("b", "o"):
            self._text.tag_configure(tag, elide=True)

    def _new_fold(self, kind: str, open_now: bool) -> int:
        """Yeni bir katlanabilir bölümün kimliğini ayırır ve etiketlerini istenen durumda kurar."""
        self._fold_seq += 1
        ident: int = self._fold_seq
        tags = fold_tags(kind, ident)
        self._prepare_tag(tags["click"])
        self._prepared_tags.update((tags["body"], tags["open"], tags["closed"]))
        self._set_fold(kind, ident, open_now)
        return ident

    def _set_fold(self, kind: str, ident: int, open_now: bool) -> None:
        """
        Bölümü açar ya da kapatır. Gövde ve 'açık' işareti kapalıyken, 'kapalı' işareti açıkken
        gizlenir (elide). Açık durum `elide` ayarının kaldırılmasıyla ("") ifade edilir, False ile
        değil: iç içe etiketlerde False üst gizlemeyi ezerdi.
        """
        tags = fold_tags(kind, ident)
        self._text.tag_configure(tags["body"], elide=ELIDE_UNSET if open_now else True)
        self._text.tag_configure(tags["open"], elide=ELIDE_UNSET if open_now else True)
        self._text.tag_configure(tags["closed"], elide=True if open_now else ELIDE_UNSET)
        if open_now:
            self._open_folds.add((kind, ident))
        else:
            self._open_folds.discard((kind, ident))
        self._paint_fold(kind, ident, False)

    def _toggle_fold(self, kind: str, ident: int) -> None:
        """Kullanıcının tıkladığı bölümü açar/kapatır; seçim otomatik katlamaya karşı korunur."""
        self._manual_folds.add((kind, ident))
        self._set_fold(kind, ident, (kind, ident) not in self._open_folds)

    def _paint_fold(self, kind: str, ident: int, hover: bool) -> None:
        """Araç satırının zeminini günceller: üstünde ya da açıkken vurgulu, aksi halde zemin ayarı kaldırılır."""
        if kind == "t":
            highlighted: bool = hover or (kind, ident) in self._open_folds
            self._text.tag_configure(fold_tags(kind, ident)["click"], background=SURFACE if highlighted else "")

    def _hover_fold(self, kind: str, ident: int, inside: bool) -> None:
        """Fare katlama başlığına girince/çıkınca imleci ve satır zeminini günceller."""
        self._text.configure(cursor="hand2" if inside else "arrow")
        self._paint_fold(kind, ident, inside)

    def _insert_parts(self, index: str, region: str, parts: List[Tuple[str, Tuple[str, ...]]]) -> None:
        self._text.mark_set(INSERT_MARK, index)
        self._text.mark_gravity(INSERT_MARK, "right")
        for content, tags in parts:
            if content:
                for tag in tags:
                    self._prepare_tag(tag)
                self._text.insert(INSERT_MARK, content, tags + (region,))

    def _new_region(self, parts: List[Tuple[str, Tuple[str, ...]]]) -> str:
        self._empty_state.place_forget()
        self._region_seq += 1
        region: str = f"r{self._region_seq}"
        self._insert_parts("end-1c", region, parts)
        return region

    def _region_bounds(self, region: str) -> Optional[Tuple[str, str]]:
        ranges: Tuple[object, ...] = self._text.tag_ranges(region)
        return (str(ranges[0]), str(ranges[-1])) if ranges else None

    def _replace_region(self, region: str, parts: List[Tuple[str, Tuple[str, ...]]]) -> None:
        bounds: Optional[Tuple[str, str]] = self._region_bounds(region)
        if bounds is None:
            return
        self._text.delete(bounds[0], bounds[1])
        self._insert_parts(bounds[0], region, parts)

    def _delete_region(self, region: str) -> None:
        bounds: Optional[Tuple[str, str]] = self._region_bounds(region)
        if bounds is not None:
            self._text.delete(bounds[0], bounds[1])
        self._raw_text.pop(region, None)
        self._pending_text.pop(region, None)
        self._region_text_tag.pop(region, None)
        self._streaming_regions.pop(region, None)
        self._live_regions.pop(region, None)
        self._dirty_tools.pop(region, None)

    def _append_streaming(self, region: str, text: str, tag: str) -> None:
        """Akan bölgeye, sondaki '▌\\n' imlecinden önce metin ekler."""
        bounds: Optional[Tuple[str, str]] = self._region_bounds(region)
        if bounds is None:
            return
        self._text.insert(f"{bounds[1]} - 2 chars", text, (tag, region))

    def _finish_streaming(self, region: str) -> None:
        """Akışı biten bölgenin imlecini kaldırır."""
        bounds: Optional[Tuple[str, str]] = self._region_bounds(region)
        if bounds is not None and self._streaming_regions.get(region):
            self._text.delete(f"{bounds[1]} - 2 chars", f"{bounds[1]} - 1 chars")
        if self._streaming_regions.get(region) and region in self._raw_text:
            self._replace_region(region, [ASSISTANT_HEAD] + render_markdown(self._raw_text[region], max_columns=TABLE_MAX_COLUMNS))
            if self._stick_to_end:
                self._text.see("end")
        self._streaming_regions[region] = False

    # --- Olay → durum ---

    def _post(self, event: AgentEvent) -> None:
        """Ajan thread'lerinden çağrılır (thread-safe kuyruk)."""
        self._inbox.put({"event": event, "done": False, "error": "", "report": None})

    def _new_streaming_region(self, head: Tuple[str, Tuple[str, ...]], text_tag: str) -> str:
        """Sonunda yanıp sönen '▌' imleci olan, daktilo ile dolacak bir bölge açar."""
        region: str = self._new_region([head, ("▌", ("cursor",)), ("\n", ())])
        self._region_text_tag[region] = text_tag
        self._streaming_regions[region] = True
        self._live_regions[region] = True
        return region

    def _text_region(self) -> str:
        if self._turn is None:
            raise RuntimeError("Metin bölgesi için etkin model turu yok.")
        if self._turn["text_region"] is None:
            # Asistan konuşmaya devam ediyor: önceki araç grubu biter ve başlığa katlanır.
            self._close_tool_group()
            self._turn["text_region"] = self._new_streaming_region(ASSISTANT_HEAD, "assistant")
        return self._turn["text_region"]

    def _reasoning_region(self) -> str:
        if self._turn is None:
            raise RuntimeError("Düşünce bölgesi için etkin model turu yok.")
        if self._turn["reasoning_region"] is None:
            self._close_tool_group()
            self._turn["reasoning_region"] = self._new_streaming_region(("∴ düşünce ", ("reasoning_head",)), "reasoning")
        return self._turn["reasoning_region"]

    def _end_live_regions(self) -> None:
        """Model turu bitti: bölgeler artık akış almaz; bekleyen metin bitince imleç kalkar."""
        for region in list(self._live_regions):
            self._live_regions[region] = False
            if not self._pending_text.get(region):
                self._finish_streaming(region)

    # --- Araç grupları ---

    def _current_tool_group(self) -> ToolGroup:
        """Art arda araç çağrılarının toplandığı açık grubu döndürür; yoksa başlığıyla birlikte açar."""
        if self._tool_group is None:
            ident: int = self._new_fold("g", True)
            region: str = self._new_region(group_head_parts(ident, 0, True))
            self._tool_group = {"id": ident, "region": region, "views": []}
            self._tool_groups[ident] = self._tool_group
        return self._tool_group

    def _refresh_group_head(self, group: ToolGroup) -> None:
        """Grup başlığındaki araç sayısını ve 'çalışıyor/çalıştırıldı' durumunu günceller."""
        running: bool = any(view["status"] in ("streaming", "running") for view in group["views"])
        self._replace_region(group["region"], group_head_parts(group["id"], len(group["views"]), running))

    def _close_tool_group(self) -> None:
        """
        Grup bitti (asistan devam etti ya da görev sona erdi): kullanıcı elle açıp kapatmadıysa
        başlığa katlanır. Hâlâ akan ya da çalışan araç varsa grup açık ve güncel kalır.
        """
        group: Optional[ToolGroup] = self._tool_group
        if group is None or any(view["status"] in ("streaming", "running") for view in group["views"]):
            return
        self._tool_group = None
        if ("g", group["id"]) not in self._manual_folds:
            self._set_fold("g", group["id"], False)

    def _drop_tool_view(self, view: ToolView) -> None:
        """Hiç başlamamış (akışı yarıda kesilmiş) araç satırını ve gerekirse boşalan grubu siler."""
        self._delete_region(view["region"])
        group: Optional[ToolGroup] = self._tool_groups.get(view["group_id"])
        if group is None:
            return
        group["views"].remove(view)
        if group["views"]:
            self._refresh_group_head(group)
            return
        self._delete_region(group["region"])
        del self._tool_groups[group["id"]]
        if self._tool_group is group:
            self._tool_group = None

    def _drop_unstarted_tool_views(self) -> None:
        """Önizlemesi görünüp hiç başlamamış araç satırlarını kaldırır; takılı dönen glif kalmaz."""
        if self._turn is None:
            return
        for index, view in list(self._turn["tools"].items()):
            if view["status"] == "streaming":
                self._drop_tool_view(view)
                del self._turn["tools"][index]

    def _settle_running_tool_views(self) -> None:
        """Görev bitti: sonuç olayı gelmemiş çalışan araçlar hata olarak kapanır (dönen glif takılı kalmaz)."""
        for view in self._tools_by_call.values():
            if view["status"] == "running":
                view.update({"status": "error", "result": "Görev sona erdi; araç sonucu alınamadı.",
                             "seconds": max(0.0, time.monotonic() - view["started_at"])})
                if ("t", view["fold_id"]) not in self._manual_folds:
                    self._set_fold("t", view["fold_id"], False)
                self._render_tool(view)
                self._refresh_group_head(self._tool_groups[view["group_id"]])

    def _tool_view(self, index: int, name: str) -> ToolView:
        if self._turn is None:
            raise RuntimeError("Araç görünümü için etkin model turu yok.")
        if index not in self._turn["tools"]:
            group: ToolGroup = self._current_tool_group()
            view: ToolView = {
                "region": "", "name": name, "preview": "", "status": "streaming", "call_id": "",
                "head": [], "tail": [], "line_count": 0, "result": "", "seconds": 0.0, "started_at": 0.0,
                "group_id": group["id"], "fold_id": self._new_fold("t", False),
                "more_id": self._new_fold("m", False),
            }
            view["region"] = self._new_region(
                tool_parts(view, time.monotonic(), SPINNER_FRAMES[self._spinner_index]))
            group["views"].append(view)
            self._refresh_group_head(group)
            self._turn["tools"][index] = view
        return self._turn["tools"][index]

    def _mark_dirty(self, view: ToolView) -> None:
        self._dirty_tools[view["region"]] = view

    def _handle_event(self, event: AgentEvent) -> None:
        previous = self._activity_verb
        try:
            self._handle_event_data(event)
        finally:
            stage = stage_for_event(event)
            self._activity_verb = LABELS[stage] if stage is not None else previous

    def _handle_event_data(self, event: AgentEvent) -> None:
        if event["kind"] == "integration_status":
            self._activity_verb = LABELS.get(stage_for_event(event), event["text"])
            if event["stage"] in LABELS:
                return
            self._new_region([(event["text"] + "\n", ("notice_info",))])
        elif event["kind"] == "user_input_required":
            self._on_input_required(event["request_id"], event["title"], event["fields"])
        elif event["kind"] == "run_started":
            self.model_label.configure(text=self._model_text(event["backend"]))
        elif event["kind"] == "turn_started":
            self._drop_unstarted_tool_views()
            self._turn = {"number": event["turn"], "text_region": None, "reasoning_region": None, "tools": {}}
            self._turn_streamed_chars = 0
            self._activity_verb = "Düşünüyor"
            self.model_label.configure(text=self._model_text(event["backend"]))
        elif event["kind"] == "text_delta":
            # Shared chat/investigation publishes one verified final without the
            # legacy agent's turn_started lifecycle event.
            if self._turn is None:
                self._turn = {"number": 0, "text_region": None, "reasoning_region": None, "tools": {}}
            region: str = self._text_region()
            self._raw_text[region] = self._raw_text.get(region, "") + event["text"]
            self._pending_text[region] = self._pending_text.get(region, "") + event["text"]
            self._turn_streamed_chars += len(event["text"])
            self._activity_verb = "Yazıyor"
        elif event["kind"] == "reasoning_delta" and self._turn is not None:
            region = self._reasoning_region()
            self._pending_text[region] = self._pending_text.get(region, "") + event["text"]
            self._turn_streamed_chars += len(event["text"])
            self._activity_verb = "Akıl yürütüyor"
        elif event["kind"] == "tool_call_preview" and self._turn is not None:
            view: ToolView = self._tool_view(event["index"], event["name"])
            view["name"] = event["name"] or view["name"]
            view["preview"] = event["preview"]
            self._activity_verb = "Komut yazıyor" if view["name"] == "execute_shell" else f"{tool_label(view['name'])} hazırlıyor"
            self._mark_dirty(view)
        elif event["kind"] == "stream_reset" and self._turn is not None:
            for region_name in (self._turn["text_region"], self._turn["reasoning_region"]):
                if region_name is not None:
                    self._delete_region(region_name)
            self._drop_unstarted_tool_views()
            self._turn["text_region"] = None
            self._turn["reasoning_region"] = None
            self._new_region([(f"↻ akış yeniden deneniyor: {event['reason']}\n", ("notice_warning",))])
        elif event["kind"] == "model_finished":
            self._completed_tokens += event["usage"]["completion_tokens"]
            self._turn_streamed_chars = 0
            usage = event["usage"]
            self.stats_label.configure(text=(
                f"son tur {event['seconds']:.1f}sn · ↑{compact_count(usage['prompt_tokens'])} "
                f"(önbellek {compact_count(usage['cached_tokens'])}) · ↓{usage['completion_tokens']}"
            ))
            self._end_live_regions()
            self._activity_verb = "Düşünüyor"
        elif event["kind"] == "tool_started":
            if self._turn is None:
                self._turn = {"number": 0, "text_region": None, "reasoning_region": None, "tools": {}}
            view = self._tool_view(event["index"], event["name"])
            view.update({"status": "running", "call_id": event["call_id"], "name": event["name"],
                         "preview": event["preview"] or view["preview"], "started_at": time.monotonic()})
            self._tools_by_call[event["call_id"]] = view
            # Çalışan araç canlı çıktısıyla açık görünür; bitince kendiliğinden kapanır.
            self._set_fold("t", view["fold_id"], True)
            self._activity_verb = "Komut çalıştırıyor" if event["name"] == "execute_shell" else f"{tool_label(event['name'])} çalışıyor"
            self._mark_dirty(view)
        elif event["kind"] == "tool_output" and event["call_id"] in self._tools_by_call:
            view = self._tools_by_call[event["call_id"]]
            for line in event["text"].splitlines(keepends=True):
                if len(view["head"]) < SUMMARY_LINES:
                    view["head"].append(line)
                view["tail"] = (view["tail"] + [line])[-LIVE_TAIL_LINES:]
                view["line_count"] += 1
            self._mark_dirty(view)
        elif event["kind"] == "tool_finished" and event["call_id"] in self._tools_by_call:
            view = self._tools_by_call[event["call_id"]]
            view.update({"status": "ok" if event["ok"] else "error", "result": event["text"], "seconds": event["seconds"]})
            if ("t", view["fold_id"]) not in self._manual_folds:
                self._set_fold("t", view["fold_id"], False)
            self._refresh_group_head(self._tool_groups[view["group_id"]])
            self._mark_dirty(view)
            if all(v["status"] in ("ok", "error") for v in self._tools_by_call.values()):
                self._activity_verb = "Düşünüyor"
        elif event["kind"] == "artifact_ready":
            self._render_artifact(event["path"], event["title"], event["media_type"])
        elif event["kind"] == "backend_changed":
            self.model_label.configure(text=self._model_text(event["backend"]))
            self._new_region([(f"↻ {event['backend']} modeline geçildi ({event['reason']})\n", ("notice_info",))])
        elif event["kind"] == "provider_fallback":
            # İstek içeriği (varsa ekran görüntüleriyle) izinli yedek sağlayıcıya gidiyor: kalıcı uyarı.
            self.model_label.configure(text=self._model_text(event["to_backend"]))
            self._new_region([(f"⚠ {provider_fallback_text(event)}\n", ("notice_warning",))])
        elif event["kind"] == "notice":
            self._new_region([(f"{'⚠' if event['level'] != 'info' else 'ℹ'} {event['text']}\n", (f"notice_{event['level']}",))])
        elif event["kind"] == "run_finished":
            self._close_tool_group()
            self._render_summary(event["success"], event["reason"], event["metrics"])
            status: str = "✓" if event["success"] else "■" if event["reason"] == "durduruldu" else "✗"
            metrics = event["metrics"]
            self.stats_label.configure(text=(
                f"{status} {metrics['elapsed_seconds']:.1f} sn · "
                f"{metrics['turns']} tur · {metrics['tool_calls']} araç"
            ))

    # --- Çizim ---

    def _render_artifact(self, path_text: str, title: str, media_type: str) -> None:
        """Doğrulanmış yerel çıktıyı sohbetin içinde açılabilir kart olarak göster."""
        path = Path(path_text)
        # Dosya tek kez okunur: denetim ile boyut okuması arasında silinirse _tick döngüsü ölmez.
        try:
            size: int = path.stat().st_size
        except OSError as error:
            self._new_region([(f"⚠ {title}: dosya artık okunamıyor ({type(error).__name__}): {path}\n",
                               ("notice_warning",))])
            return
        viewport = self._text.winfo_width()
        card_width = max(320, min(620, (viewport if viewport > 100 else 700) - 40))
        card = ctk.CTkFrame(
            self._text, width=card_width, fg_color=SURFACE, border_width=1,
            border_color=BORDER, corner_radius=12,
        )
        card.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            card, text=title.upper(), text_color=ACCENT, anchor="w",
            font=self._ui_font(11, "bold"),
        ).grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 2))
        ctk.CTkLabel(
            card, text=path.name, text_color=TEXT, anchor="w",
            font=self._ui_font(13, "bold"), wraplength=card_width - 28,
        ).grid(row=1, column=0, sticky="ew", padx=14)
        row = 2
        if media_type == "image" and size <= 40 * 1024 * 1024:
            try:
                with Image.open(path) as source:
                    preview = ImageOps.exif_transpose(source)
                    preview.thumbnail((card_width - 28, 320), Image.Resampling.LANCZOS)
                    preview = preview.copy()
                picture = ctk.CTkImage(light_image=preview, dark_image=preview, size=preview.size)
                self._artifact_images.append(picture)
                ctk.CTkLabel(card, text="", image=picture).grid(
                    row=row, column=0, padx=14, pady=(10, 4), sticky="w",
                )
                row += 1
            except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
                # PIL, bozuk PNG'de (PngImagePlugin) OSError değil SyntaxError yükseltir.
                ctk.CTkLabel(
                    card, text="Görsel önizlemesi açılamadı", text_color=WARNING,
                    font=self._ui_font(11, "normal"), anchor="w",
                ).grid(row=row, column=0, sticky="ew", padx=14, pady=(8, 0))
                row += 1
        size_label =f"{size / (1024 * 1024):.1f} MB" if size >= 1024 * 1024 else f"{max(1, size // 1024)} KB"
        bottom = ctk.CTkFrame(card, fg_color="transparent")
        bottom.grid(row=row, column=0, sticky="ew", padx=14, pady=(8, 12))
        bottom.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            bottom, text=size_label, text_color=TEXT_FAINT,
            font=ctk.CTkFont(family=MONO_FAMILY, size=10),
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkButton(
            bottom, text="Yolu kopyala", width=88, height=28, fg_color="transparent",
            hover_color=SURFACE_RAISED, text_color=TEXT_DIM, corner_radius=7,
            font=self._ui_font(11, "normal"), cursor="hand2",
            command=lambda: (self.clipboard_clear(), self.clipboard_append(str(path))),
        ).grid(row=0, column=1, sticky="e", padx=(0, 6))
        ctk.CTkButton(
            bottom, text="Aç ↗", width=72, height=28, fg_color=SURFACE_RAISED,
            hover_color=BORDER, text_color=TEXT, corner_radius=7,
            font=self._ui_font(11, "bold"), cursor="hand2",
            command=lambda: webbrowser.open(path.as_uri()),
        ).grid(row=0, column=2, sticky="e")
        self._artifact_widgets.append(card)
        self._text.insert("end-1c", f"\n▣ {title} · {path.name}\n", ("sum_meta",))
        self._text.window_create("end-1c", window=card, padx=0, pady=4)
        self._text.insert("end-1c", "\n", ("gap",))

    def _render_tool(self, view: ToolView) -> None:
        """Araç bloğunu (satır + açılır ayrıntı) yeniden çizer; katlama durumu etiketlerde durduğu için korunur."""
        self._replace_region(
            view["region"], tool_parts(view, time.monotonic(), SPINNER_FRAMES[self._spinner_index]))

    def _render_welcome(self) -> None:
        self._empty_state.place(relx=0.5, rely=0.45, anchor="center")

    def _note_sent_time(self) -> str:
        """
        Kullanıcı mesajının gönderim zamanını (ISO 8601) sohbet kaydına ekler ve aynı değeri döner;
        kabarcığın altındaki saat bundan gelir ve yeniden açılışta kayıttan okunur. Kayıt yoksa
        (sohbet listesi yazılamıyorsa) mesaj yine saatini gösterir, kalıcı olmaz.
        """
        stamp: str = datetime.now(timezone.utc).isoformat()
        if self._chat_record is not None:
            self._chat_record.setdefault("sends", []).append(stamp)
        return stamp

    def _render_goal(self, goal: str) -> None:
        """
        Kullanıcı mesajını sağa yaslı yuvarlak kabarcık olarak gösterir. Metnin kendisi gizli
        (elide) kalır: kopyalama ve kalıcı kayıt ondan yapılır, kabarcık penceresi yüklemede
        bu metinden yeniden kurulur.
        """
        self._new_region([("\n", ("gap_lg",))])
        region: str = self._new_region([(goal, ("user_msg",)), ("\n", ("user_line",))])
        bounds: Optional[Tuple[str, str]] = self._region_bounds(region)
        if bounds is None:
            raise RuntimeError("Kullanıcı mesajı bölgesi oluşturulamadı.")
        self._embed_bubble(bounds[0], goal, self._note_sent_time())
        self._new_region([("\n", ("gap",))])
        # Gömülü kabarcığın yüksekliği yerleşimden sonra belli olur; sona kaydırma bu yüzden ertelenir.
        self.after_idle(self._scroll_to_end)

    def _render_summary(self, success: bool, reason: str, metrics: EpisodeMetrics) -> None:
        """Görev bitişini tek satırlık sessiz özet olarak ekler; ayrıntı satırları tıklanınca açılır."""
        self._new_region(summary_parts(self._new_fold("s", False), success, reason, metrics))

    def _typewriter_step(self) -> bool:
        """Bekleyen model metnini geciktirmeden, Tk'yi kilitlemeyecek parçalarda gösterir."""
        changed: bool = False
        for region in list(self._pending_text):
            pending: str = self._pending_text[region]
            if not pending:
                continue
            count: int = min(len(pending), STREAM_CHARS_PER_FRAME)
            # Bekleyen metin ekleme ÖNCESİ ilerletilir: ekleme patlarsa aynı parça sonsuza dek
            # tekrar edilmez (asistan metni _finish_streaming'de _raw_text'ten yeniden çizilir).
            self._pending_text[region] = pending[count:]
            self._append_streaming(region, pending[:count], self._region_text_tag[region])
            changed = True
            if not self._pending_text[region] and not self._live_regions.get(region):
                self._finish_streaming(region)
        return changed

    def _set_activity(self, glyph: str, verb: str, meta: str, shine: int) -> None:
        """
        Akış durum satırını kurar: dönen glif (vurgu rengi), ölçümler ve parıltılı fiil (3 karakterlik
        açık pencere), örn. '✻  6dk 10sn · ~420 token · Düşünüyor…'.
        """
        self._activity.configure(state="normal")
        self._activity.delete("1.0", "end")
        self._activity.insert("end", glyph + "  ", ("glyph",))
        self._activity.insert("end", meta + " · ", ("meta",))
        for position, char in enumerate(verb + "…"):
            self._activity.insert("end", char, ("verb", "shine") if shine - 3 < position <= shine else ("verb",))
        self._activity.configure(state="disabled")

    def _set_activity_idle(self) -> None:
        """Görev yokken durum satırı boşalır (görev bitince satır kalkar; özet transkripte iner)."""
        self._activity.configure(state="normal")
        self._activity.delete("1.0", "end")
        self._activity.configure(state="disabled")

    def _spin_tool_glyphs(self) -> None:
        """Çalışan araç satırlarındaki dönen glifi yerinde günceller (satırı yeniden çizmeden, kaydı kirletmeden)."""
        ranges: Tuple[object, ...] = self._text.tag_ranges("tool_spin")
        if not ranges:
            return
        glyph: str = SPINNER_FRAMES[self._spinner_index]
        with self._writable_transcript():
            for start in [str(item) for item in ranges[0::2]]:
                tags: Tuple[str, ...] = tuple(tag for tag in self._text.tag_names(start) if tag != "sel")
                self._text.delete(start, f"{start} + 1 chars")
                self._text.insert(start, glyph, tags)

    def _set_task_status(self, phase: str, seconds: float = 0.0) -> None:
        """Üst sağdaki kısa görev durumunu günceller."""
        self._task_status = phase
        elapsed = max(0, int(seconds))
        labels = {
            "idle": ("", TEXT_FAINT),
            "running": (f"{SPINNER_FRAMES[self._spinner_index]} {elapsed} sn", ACCENT),
            "stopping": ("■ Durduruluyor", WARNING),
            "done": (f"✓ {elapsed} sn", SUCCESS),
            "failed": (f"✕ {elapsed} sn", ERROR),
            "stopped": ("■ Durduruldu", TEXT_FAINT),
            "keys": ("⏳ Anahtarlar okunuyor", WARNING),
        }
        label, color = labels[phase]
        self.task_status_label.configure(text=label, text_color=color)

    def _sync_menu_status(self) -> None:
        """Arka planda görev sürerken menü çubuğu göstergesini canlı tutar."""
        if self._visibility_hidden:
            self._menu_status.hide()
            return
        background = is_backgrounded(
            self.state(), self.focus_displayof() is not None, app_is_active())
        if not background:
            self._menu_status.hide()
        elif self._task_status in ("running", "stopping"):
            self._menu_status.show(f"✻ {SPINNER_FRAMES[self._spinner_index]}",
                                   "OmniAgent görev üzerinde çalışıyor")
        elif self._badge_pending:
            symbol = "✓" if self._task_status == "done" else "!"
            self._menu_status.show(f"✻ {symbol}", "OmniAgent görevi tamamlandı")
        else:
            self._menu_status.hide()

    def _toggle_visibility(self) -> None:
        """
        Genel kısayol isteğini yalnız Tk thread'inde, işletim sisteminin GERÇEK durumuna göre uygular:
        Dock/⌘Tab ile açılan ya da simge durumuna küçültülen pencere için bayrak eskimiş olsa da ilk
        kısayol boşa gitmez.
        """
        target: bool = not (application_is_hidden(self) or self.state() == "iconic")
        try:
            set_application_hidden(target, self)
        except Exception as error:
            self._post({"kind": "notice", "level": "warning",
                        "text": f"OmniAgent görünürlüğü değiştirilemedi: {error}"})
            return
        self._visibility_hidden = target
        self._sync_menu_status()

    def _on_focus_return(self, _event: object = None) -> None:
        """Pencere yeniden görünür olunca tamamlanma rozetini temizler."""
        if self._badge_pending and self._agent_future is None:
            set_dock_badge(None)
            self._badge_pending = False
        self._menu_status.hide()

    def _animate(self, now: float) -> None:
        if now - self._last_menu_check >= 0.1:
            self._last_menu_check = now
            # Bayrak yalnız menü göstergesi içindir ve eskiyebilir (Dock ile açma, ⌘H): gerçeği okur.
            self._visibility_hidden = application_is_hidden(self)
            self._sync_menu_status()
            if self._deferred_inputs and not self._visibility_hidden and not self._closing:
                self._show_deferred_inputs()
        running: bool = self._agent_future is not None and not self._agent_future.done()
        if running:
            self._pulse_running_dot(now)
        if running and now - self._last_spinner >= SPINNER_INTERVAL:
            self._last_spinner = now
            self._spinner_index = (self._spinner_index + 1) % len(SPINNER_FRAMES)
            if self._task_status == "running":
                self._set_task_status("running", now - self._run_started_at)
            self._spin_tool_glyphs()
        if running and now - self._last_shimmer >= SHIMMER_INTERVAL:
            self._last_shimmer = now
            self._shine_index = (self._shine_index + 1) % (len(self._activity_verb) + 8)
            # Token sayısı: biten turların gerçek kullanımı + akan metnin karakter tahmini (~ ile belirtilir).
            tokens: int = self._completed_tokens + self._turn_streamed_chars // 4
            meta: str = activity_meta(now - self._run_started_at, tokens, self._turn_streamed_chars > 0)
            self._set_activity(SPINNER_FRAMES[self._spinner_index], self._activity_verb, meta, self._shine_index)
        if (running or any(self._pending_text.values())) and now - self._last_blink >= BLINK_INTERVAL:
            self._last_blink = now
            self._blink_on = not self._blink_on
            self._text.tag_configure("cursor", foreground=ACCENT if self._blink_on else BG)

    def _tick(self) -> None:
        """
        ~60 fps kare döngüsü sarmalayıcısı: kare istisna verse de sonraki kare kurulur (aksi halde
        arayüz kalıcı donar). İstisna yutulmaz: finally sonrası Tk'ye yükselir ve
        report_callback_exception günlüğe yazar. Kapanırken yeniden kurulmaz.
        """
        next_delay: int = ERROR_FRAME_MS
        try:
            next_delay = self._run_frame()
        finally:
            if not self._closing:
                self.after(next_delay, self._tick)

    def _run_frame(self) -> int:
        """Tek kare: olayları işle, daktiloyu ilerlet, kirli blokları çiz, animasyonları oynat; sonraki gecikmeyi (ms) döndürür."""
        while True:
            try:
                self._visibility_requests.get_nowait()
            except Empty:
                break
            self._toggle_visibility()
        self._drain_tk_calls()
        now: float = time.monotonic()
        running: bool = self._agent_future is not None and not self._agent_future.done()
        refresh_due: bool = (
            running and now - self._last_running_refresh >= RUNNING_REFRESH_INTERVAL
            and any(view["status"] == "running" for view in self._tools_by_call.values())
        )
        idle: bool = (
            self._inbox.empty() and self._voice_queue.empty()
            and not any(self._pending_text.values()) and not self._dirty_tools and not refresh_due
        )
        if not idle:
            self._process_frame(now, running)
        self._animate(now)
        self._autosave_chat(now)
        return FRAME_MS if any(self._pending_text.values()) else (
            RUNNING_IDLE_FRAME_MS if running else IDLE_FRAME_MS
        )

    def _process_frame(self, now: float, running: bool) -> None:
        """Kuyruktaki olayları işler, daktiloyu ilerletir, kirli araç bloklarını çizer."""
        stick: bool = self._stick_to_end
        changed: bool = False
        refreshed: bool = False
        with self._writable_transcript():
            processed: int = 0
            while processed < MAX_EVENTS_PER_FRAME:
                try:
                    item: UiItem = self._inbox.get_nowait()
                except Empty:
                    break
                processed += 1
                if item["event"] is not None:
                    self._handle_event(item["event"])
                if item["done"]:
                    self._on_run_done(item["error"], item["report"])
            # Ses callback'leri Tk thread'inde doğrudan çalışmaz; aynı kare döngüsünde
            # Queue'dan alınır. Böylece PyObjC callback'i UI'yi yarıda bırakamaz.
            for _ in range(20):
                try:
                    voice_kind, voice_value = self._voice_queue.get_nowait()
                except Empty:
                    break
                if voice_kind == "text":
                    self._insert_voice_text(voice_value)
                elif voice_kind == "partial":
                    self._insert_voice_partial(voice_value)
                else:
                    self._set_voice_state(voice_kind, voice_value)
                processed += 1
            changed = self._typewriter_step() or processed > 0
            if running and now - self._last_running_refresh >= RUNNING_REFRESH_INTERVAL:
                # Çalışan araç satırlarının süresi canlı kalır; bu yenileme kaydı kirletmez.
                self._last_running_refresh = now
                for view in self._tools_by_call.values():
                    if view["status"] == "running" and view["region"] not in self._dirty_tools:
                        self._render_tool(view)
                        refreshed = True
            while self._dirty_tools:
                _region, view = self._dirty_tools.popitem()  # önce çıkar: zehirli görünüm tekrar denenmez
                self._render_tool(view)
                changed = True
        # Bölge silinip yeniden eklenince Tk görünümü kayabilir: yenileme de sona dönüşü gerektirir.
        if (changed or refreshed) and stick:
            self._scroll_to_end()
        if changed and self._chat_record is not None:
            self._chat_dirty = True

    # --- Ajan tetikleme ---

    def _model_text(self, backend: str) -> str:
        return f"{BACKENDS[backend]['model']} · {backend}" if backend in BACKENDS else backend

    def _on_primary_button(self) -> None:
        if self._agent_future is not None and not self._agent_future.done():
            command = self.entry.get().strip()
            if self._active_run_mode == "continuous" and (
                command.startswith("/btw ") or command == "/approve"
            ):
                self._control_messages.put(command)
                self.entry.delete(0, "end")
                self._text.configure(state="normal")
                self._new_region([(f"Siz: {command}\n", ("notice_info",))])
                self._text.configure(state="disabled")
                self._text.see("end")
                return
            self._request_stop()
            return
        self._send_goal()

    def _queue_voice_text(self, text: str) -> None:
        """Kesin ses sonucunu UI kuyruğuna aktarır."""
        self._voice_queue.put(("text", text))

    def _queue_voice_partial(self, text: str) -> None:
        """Ara Speech sonucunu UI kuyruğuna aktarır."""
        self._voice_queue.put(("partial", text))

    def _queue_voice_state(self, state: str, message: str) -> None:
        """Ses motoru durumunu Tk thread'ine aktarır."""
        self._voice_queue.put((state, message))

    def _copy_transcript(self) -> None:
        """Transkriptte görünen sohbeti tek dokunuşla panoya kopyalar."""
        transcript: str = plain_transcript(self._text.get("1.0", "end-1c"))
        if not transcript.strip():
            return
        self.clipboard_clear()
        self.clipboard_append(transcript)
        self.update_idletasks()
        self.copy_btn.configure(
            fg_color=SUCCESS, hover_color=SUCCESS, border_color=SUCCESS,
        )
        self.after(900, self._restore_copy_button)

    def _restore_copy_button(self) -> None:
        """Kopyalama geri bildirimini eski SVG buton görünümüne döndürür."""
        try:
            self.copy_btn.configure(
                fg_color="transparent", hover_color=SURFACE_RAISED, border_color=BORDER,
            )
        except tk.TclError:
            return

    def _copy_message(self, text: str, icon: tk.Label) -> None:
        """Tek bir kullanıcı mesajını panoya kopyalar; simge kısa süre yeşil onaya döner."""
        self.clipboard_clear()
        self.clipboard_append(text)
        self.update_idletasks()
        try:
            icon.configure(image=self._copy_photo_done)
        except tk.TclError:
            return
        self.after(900, lambda: self._restore_message_copy(icon))

    def _restore_message_copy(self, icon: tk.Label) -> None:
        """Mesaj kopyalama onayını geri alır; kabarcık bu arada kapatıldıysa sessizce döner."""
        try:
            icon.configure(image=self._copy_photo)
        except tk.TclError:
            return

    def _replace_entry_text(self, text: str, disabled: bool = False) -> None:
        """Entry'yi programatik olarak günceller; ses sırasında kullanıcı yazımını kilitler."""
        self.entry.configure(state="normal")
        self.entry.delete(0, "end")
        self.entry.insert(0, text)
        if disabled:
            self.entry.configure(state="disabled")

    def _restore_voice_draft(self) -> None:
        """Hatalı/iptal edilen oturumda ara metni eski composer içeriğine geri alır."""
        self._replace_entry_text(self._voice_base_text)
        self._voice_partial_text = ""

    def _toggle_voice(self) -> None:
        """Mikrofonu aç/kapat; tanıma sonucu otomatik gönderilmez, önce düzenlenebilir."""
        if self._agent_future is not None and not self._agent_future.done():
            return
        try:
            if self._voice.active:
                if self._voice.recording:
                    self._voice.stop()
                return
            self._voice_base_text = self.entry.get()
            self._voice_partial_text = ""
            self._voice.start()
        except VoiceInputError as error:
            self._set_voice_state("error", str(error))

    def _set_voice_state(self, state: str, message: str) -> None:
        """Sesli giriş durumunu composer düğmesine ve transkripte yansıtır."""
        if state == "recording":
            self.voice_btn.configure(image=self._voice_icon_active, fg_color=ERROR, hover_color=ERROR,
                                     text_color=TEXT, state="normal")
            self.entry.configure(state="disabled")
        elif state in ("requesting", "transcribing"):
            self.voice_btn.configure(image=self._voice_icon_busy, state="disabled")
            self.entry.configure(state="disabled")
        else:
            self.voice_btn.configure(image=self._voice_icon, fg_color=SURFACE_RAISED, hover_color=SURFACE_RAISED,
                                     text_color=TEXT_DIM, state="normal")
            self.entry.configure(state="normal")
        if state in ("error", "recording_timeout", "idle"):
            self._restore_voice_draft()
        if state in ("error", "recording_timeout"):
            text: str = message or "Sesli giriş tamamlanamadı."
            # Kare içinde Text'i açıp kapamak aynı karedeki sonraki eklemeleri düşürürdü;
            # bildirim olay kuyruğundan gelir ve sonraki karede eklenir.
            self._post({"kind": "notice", "level": "warning", "text": f"🎙 {text}"})
            self.entry.focus_set()

    def _insert_voice_partial(self, text: str) -> None:
        """Speech'in kümülatif ara sonucunu composer'da canlı olarak gösterir."""
        transcript: str = text.strip()
        self._voice_partial_text = transcript
        if not transcript:
            self._replace_entry_text(self._voice_base_text, disabled=True)
            return
        separator: str = " " if self._voice_base_text and not self._voice_base_text.endswith((" ", "\n")) else ""
        self._replace_entry_text(self._voice_base_text + separator + transcript, disabled=True)

    def _insert_voice_text(self, text: str) -> None:
        """Kesin tanımayı composer'a yazar ve oturum başındaki metni korur."""
        transcript: str = text.strip()
        if transcript:
            separator: str = " " if self._voice_base_text and not self._voice_base_text.endswith((" ", "\n")) else ""
            rendered: str = self._voice_base_text + separator + transcript
        else:
            rendered = self._voice_base_text
        self._replace_entry_text(rendered)
        self._voice_base_text = rendered
        self._voice_partial_text = ""
        self.entry.focus_set()

    def _request_stop(self) -> None:
        if self._agent_future is not None and not self._agent_future.done():
            self._stop_event.set()
            self._activity_verb = "Durduruluyor"
            self._set_task_status("stopping")
            self._close_input_windows()

    def _send_goal(self) -> None:
        """Giriş alanındaki hedefi transkripte ekler ve ajanı kalıcı event loop'ta başlatır."""
        if self._agent_future is not None:
            return
        if self._startup_pending:
            # Keychain okuması sürerken başlayan görev anahtarsız istemcilerle çalışırdı.
            self._show_startup_hint()
            return
        goal: str = self.entry.get().strip()
        if not goal:
            return
        inventory_command: bool = goal.casefold() in INVENTORY_COMMANDS
        if not self._ensure_chat(goal):
            return
        if not inventory_command and self._set_chat_outcome(None):
            self._save_catalog()  # yeni görev başlıyor: önceki görevin sonucu geçersizdir
        if self._voice.active:
            self._voice.cancel()
            # Eski oturumun idle callback'i yeni görev composer'ına yazmasın.
            self._voice_base_text = ""
            self._voice_partial_text = ""
        if inventory_command:
            self.entry.delete(0, "end")
            with self._writable_transcript():
                self._render_goal(goal)
                try:
                    self._integrations.refresh_local()
                    inventory = format_capability_inventory(
                        self._integrations.entries, show_skills=goal.casefold() == "/skills"
                    )
                except Exception as error:
                    inventory = f"Yetenek kataloğu okunamadı: {type(error).__name__}: {error}"
                self._new_region([(inventory + "\n", ("notice_info",))])
            self._stick_to_end = True
            self._scroll_to_end()
            self._request_chat_save()
            return
        self._active_goal = goal
        self.entry.delete(0, "end")
        with self._writable_transcript():
            self._render_goal(goal)
        self._stick_to_end = True
        self._scroll_to_end()
        self._turn = None
        self._tools_by_call = {}
        self._tool_group = None
        self._completed_tokens = 0
        self._turn_streamed_chars = 0
        self._run_started_at = time.monotonic()
        self._activity_verb = "Düşünüyor"
        self._set_task_status("running")
        self._badge_pending = False
        set_dock_badge("•")
        self.backend_menu.configure(state="disabled")
        self.mode_menu.configure(state="disabled")
        self.voice_btn.configure(state="disabled")
        self.primary_btn.configure(text="■", fg_color=SURFACE_RAISED, hover_color=BORDER, text_color=TEXT)
        selected: str = self.backend_menu.get()
        selected_mode: str = RUN_MODE_KEYS.get(self.mode_menu.get(), "normal")
        if DEFAULT_BACKEND not in self._clients:
            # Varsayılan profil (yerel Ollama) uygulama açılırken hazır değildi: görev başında yeniden yoklanır,
            # Ollama sonradan açıldıysa uygulamayı yeniden başlatmak gerekmez (Telegram köprüsü de görev başında yeniler).
            self._rebuild_clients()
        self._active_run_mode = selected_mode
        self._control_messages = Queue()
        self._stop_event = threading.Event()
        options: RunOptions = {
            "requested_backend": None if selected == "Otomatik" else selected,
            "should_stop": self._stop_event.is_set,
            "state_file": STATE_FILE,
            "history": trim_history(self._history),
            "integrations": self._integrations,
            "answer": self._request_input,
            "run_mode": selected_mode,
        }
        # Yazılı soru yanıtları kanıtlı hafızaya kaydedilir (kullanıcının kendi sözleri; onaylar kaydedilmez).
        options["answer"] = recording_answer("desktop", self._request_input)
        if selected_mode == "continuous":
            options["pop_control_messages"] = self._drain_control_messages
        self._agent_future = asyncio.run_coroutine_threadsafe(
            self._run_exclusive(goal, options), self._loop,
        )
        self._agent_future.add_done_callback(self._on_agent_future_done)
        # Ajan başladıktan SONRA: anlık görüntü ve satır durumu ilk modele gidiş-dönüşle çakışır;
        # diske yazma arka plandadır, Tk beklemez.
        self._refresh_chat_list()
        self._request_chat_save()

    def _drain_control_messages(self) -> List[str]:
        """Çalışan ajan için yeni kullanıcı komutlarını sırayla ve tek seferde alır."""
        commands: List[str] = []
        while True:
            try:
                commands.append(self._control_messages.get_nowait())
            except Empty:
                return commands

    async def _run_exclusive(self, goal: str, options: RunOptions) -> RunReport:
        """Ortak coordinator'ı çalıştırır; yalnız effectful task route host lock alır."""
        started_at = utc_now_iso()
        # Kanıtlı hafıza: kullanıcının hedef metni. Bağlantı iş parçacığında açılıp kapanır; hata görevi durdurmaz.
        await asyncio.to_thread(record_user_message, "desktop", goal, utc_now_iso())
        self._evidence_state_file = options.get("state_file", STATE_FILE)
        options = {**options, "task_context": async_host_task_lock_preempting}
        try:
            report = await run_agent_with_callback(goal, self._post, options, self._clients)
        except (Exception, asyncio.CancelledError) as error:
            await asyncio.to_thread(record_failed_task, "desktop", goal, type(error).__name__, started_at, 0)
            raise
        await asyncio.to_thread(record_report, "desktop", report)
        return report

    async def _request_input(self, title: str, fields: Dict[str, object]) -> Dict[str, object]:
        """
        Model çağırmadan Tk arayüzünden cevap bekler. İstek yanıt, zaman aşımı ya da durdurmayla biterse
        (event loop thread'i) penceresini kapatma işi Tk thread'ine bırakılır: Tk'ye burada dokunulmaz.
        """
        request_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self._input_futures[request_id] = future
        self._post({"kind": "user_input_required", "request_id": request_id, "title": title, "fields": fields})
        try:
            return await future
        finally:
            self._input_futures.pop(request_id, None)
            self._call_in_tk(lambda: self._dismiss_input(request_id))

    def _close_input_windows(self) -> None:
        """Görev durdu ya da bitti: açık yanıt pencereleri kapanır, ertelenmiş istekler düşer."""
        for window in list(self._input_windows.values()):
            window.destroy()
        self._input_windows.clear()
        self._deferred_inputs.clear()

    def _dismiss_input(self, request_id: str) -> None:
        """
        Tk thread'i: yanıt beklenmeden biten (süre doldu, görev durdu) isteğin penceresini kapatır; henüz
        açılmamış (ertelenmiş) istek de düşer. Kullanıcı istemsiz kalan isteği transkriptte görür.
        """
        window = self._input_windows.pop(request_id, None)
        deferred: Optional[DeferredInput] = self._deferred_inputs.pop(request_id, None)
        if window is None and deferred is None:
            return  # kullanıcı zaten yanıtladı ya da _request_stop/_on_run_done kapattı
        if window is not None:
            window.destroy()
        self._post({"kind": "notice", "level": "warning",
                    "text": "Yanıt penceresi kapandı: süre doldu veya görev durduruldu; işlem yapılmadı."})

    def _on_input_required(self, request_id: str, title: str, fields: Dict[str, object]) -> None:
        """
        Yanıt bekleyen istek görünmeyen yerde kalmasın (onay zaman aşımı 15 dk). Uygulama gizliyse (⌘⇧X)
        içerik göstermeyen bildirim ve Dock zıplaması verilir; macOS'ta gizli uygulamada yeni pencere açmak
        tüm uygulamayı görünür yaptığı için pencere uygulama yeniden görünene dek ertelenir. Görünür ama
        arka plandaysa sağ üstte yanıt kartı açılır; ana uygulama öne alınmaz.
        """
        action: str = input_alert_action(
            application_is_hidden(self),
            is_backgrounded(self.state(), self.focus_displayof() is not None, app_is_active()),
        )
        requested_at: datetime = datetime.now()
        if action == ALERT_NOTIFY:
            self._deferred_inputs[request_id] = {"title": title, "fields": fields, "requested_at": requested_at}
            notify_input_required(subprocess.Popen)
            request_user_attention()
            return
        self._show_input(request_id, title, fields, requested_at)

    def _show_deferred_inputs(self) -> None:
        """
        Uygulama yeniden görünür olunca ertelenmiş yanıt pencereleri açılır (süre isteğin anından sayılır);
        ana pencere Dock'tan açılıp simge durumunda kalmış olabilir, bu yüzden önce öne alınır.
        """
        set_application_hidden(False, self)
        for request_id, deferred in list(self._deferred_inputs.items()):
            del self._deferred_inputs[request_id]
            self._show_input(request_id, deferred["title"], deferred["fields"], deferred["requested_at"])

    def _show_input(self, request_id: str, title: str, fields: Dict[str, object], requested_at: datetime) -> None:
        """
        Alanları Tk thread'inde gösterir; cevap yalnız ilgili Future'a teslim edilir. Görev durdurulmuşsa ya da
        istek artık yaşamıyorsa (süresi dolmuş) pencere açılmaz.
        """
        if self._stop_event.is_set() or request_id not in self._input_futures:
            return
        field = confirmation_field(fields)
        if field is not None:
            detail = str(fields.get("_help", ""))
            timeout = fields.get(INPUT_TIMEOUT_FIELD)
            if isinstance(timeout, (int, float)):
                detail += "\n\n" + format_input_deadline(float(timeout), requested_at)
            try:
                popup = create_confirmation_popup(
                    title, detail, lambda allowed: self._answer_input(request_id, {field: allowed}),
                )
            except Exception:
                logging.exception("Native onay kartı açılamadı; Tk penceresi kullanılacak")
                popup = None
            if popup is not None:
                self._input_windows[request_id] = popup
                return
        window = ctk.CTkToplevel(self)
        self._input_windows[request_id] = window
        window.title("OmniAgent — Yanıt gerekiyor")
        width, height = 480, 460
        window.geometry(f"{width}x{height}+{max(0, self.winfo_screenwidth() - width - 20)}+40")
        window.attributes("-topmost", True)
        panel = ctk.CTkScrollableFrame(window, fg_color=BG)
        panel.pack(fill="both", expand=True, padx=12, pady=12)
        ctk.CTkLabel(panel, text=title, wraplength=420, justify="left").pack(anchor="w", pady=8)
        if fields.get("_help"):
            ctk.CTkLabel(panel, text=str(fields["_help"]), wraplength=420, justify="left").pack(anchor="w", pady=8)
        timeout: object = fields.get(INPUT_TIMEOUT_FIELD)
        if isinstance(timeout, (int, float)):
            ctk.CTkLabel(
                panel, text=format_input_deadline(float(timeout), requested_at), text_color=WARNING,
                wraplength=420, justify="left",
            ).pack(anchor="w", pady=(0, 8))
        if fields.get("_url"):
            url = str(fields["_url"])
            ctk.CTkButton(panel, text="Microsoft uygulama kaydını aç",
                          command=lambda: webbrowser.open(url)).pack(anchor="w", pady=6)
        widgets = {}
        for name, spec in fields.items():
            if name.startswith("_") or not isinstance(spec, dict):
                continue
            if spec.get("type") == "boolean":
                variable = tk.BooleanVar(value=bool(spec.get("default", False)))
                widget = ctk.CTkCheckBox(panel, text=spec.get("label", name), variable=variable)
                widget.pack(anchor="w", pady=8)
                widgets[name] = variable
            else:
                ctk.CTkLabel(panel, text=spec.get("label", name)).pack(anchor="w")
                widget = ctk.CTkEntry(panel, width=420)
                widget.insert(0, str(spec.get("default", "")))
                widget.pack(anchor="w", pady=(0, 6))
                widgets[name] = widget

        def submit() -> None:
            values = {name: widget.get() for name, widget in widgets.items()}
            self._answer_input(request_id, values)

        if field is not None:
            ctk.CTkButton(panel, text="İzin ver", command=lambda: self._answer_input(request_id, {field: True})).pack(pady=6)
            ctk.CTkButton(panel, text="Reddet", command=lambda: self._answer_input(request_id, {field: False})).pack(pady=6)
            window.protocol("WM_DELETE_WINDOW", lambda: self._answer_input(request_id, {field: False}))
        else:
            ctk.CTkButton(panel, text="Yanıtla ve devam et", command=submit).pack(pady=12)
            window.protocol("WM_DELETE_WINDOW", self._request_stop)
        window.lift()

    def _answer_input(self, request_id: str, values: Dict[str, object]) -> None:
        """Tk/native kart yanıtını yalnız yaşayan isteğe ve doğru event loop'a teslim et."""
        def deliver() -> None:
            future = self._input_futures.get(request_id)
            if future is not None and not future.done():
                future.set_result(values)
        self._loop.call_soon_threadsafe(deliver)
        window = self._input_windows.pop(request_id, None)
        if window is not None:
            window.destroy()

    def _on_agent_future_done(self, future: "Future[RunReport]") -> None:
        """Görev bitince (event loop thread'inde) sonucu kuyruğa bırakır."""
        try:
            report = future.result()
        except Exception as error:
            self._inbox.put({"event": None, "done": True,
                             "error": f"{type(error).__name__}: {error}", "report": None})
        else:
            self._inbox.put({"event": None, "done": True, "error": "", "report": report})

    def _on_run_done(self, error: str, report: Optional[RunReport]) -> None:
        background = is_backgrounded(
            self.state(), self.focus_displayof() is not None, app_is_active())
        elapsed = time.monotonic() - self._run_started_at
        stopped = self._stop_event.is_set() or bool(report and report.get("reason") == "durduruldu")
        success = not error and bool(report and report["success"])
        exchange = report["exchange"] if report is not None else make_exchange(
            self._active_goal, f"Kritik hata: {error}", [])
        self._history = trim_history(self._history + [exchange])
        self.context_label.configure(text=f"bağlam: {len(self._history)} mesaj")
        self._agent_future = None
        self._active_run_mode = "normal"
        self._close_input_windows()
        if error:
            self._new_region([(f"⚠ Kritik hata: {error}\n", ("notice_error",))])
        # Görev bitti: takılı/dönen araç satırları kapanır, son araç grubu başlığa katlanır.
        self._drop_unstarted_tool_views()
        self._settle_running_tool_views()
        self._close_tool_group()
        self._end_live_regions()
        # The coordinator's final can still be buffered in the typewriter. Render
        # the complete authoritative region before taking the delivery snapshot.
        final_presented = False
        region = self._turn.get("text_region") if self._turn else None
        if (report is not None and not error and region and self._region_bounds(region)
                and self._raw_text.get(region, "").strip() == str(report.get("outcome", "")).strip()):
            self._pending_text[region] = ""
            self._finish_streaming(region)
            final_presented = True
        self.backend_menu.configure(state="normal")
        self.mode_menu.configure(state="normal")
        self.voice_btn.configure(
            text="", image=self._voice_icon, fg_color=SURFACE_RAISED,
            hover_color=SURFACE_RAISED, text_color=TEXT_DIM, state="normal",
        )
        self.primary_btn.configure(text="↑", fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color=BG)
        self._set_activity_idle()
        self._set_task_status("stopped" if stopped else "done" if success else "failed", elapsed)
        if background and not stopped:
            set_dock_badge("✓" if success else "!")
            self._badge_pending = True
            notify_finished(success, False, elapsed)
        else:
            set_dock_badge(None)
            self._badge_pending = False
            if not background:
                self.entry.focus_set()
        self._sync_menu_status()
        # Görevin sonucu dizine ve kayda yazılır (kenar çubuğu durum noktası); sohbet diske arka planda gider.
        self._set_chat_outcome(OUTCOME_STOPPED if stopped else OUTCOME_DONE if success else OUTCOME_FAILED)
        self._save_catalog()
        self._refresh_chat_list()
        self._request_chat_save(report if final_presented else None)
        if self._clients_stale:
            # Görev sürerken kaydedilen anahtarlar biter bitmez uygulanır. Patlayabilecek işlem
            # en sona alındı: arayüz durumu yukarıda zaten geri yüklendi, hata arayüzü kilitlemez.
            self._rebuild_clients()

    def _save_catalog(self) -> bool:
        if not self._chat_catalog_writable:
            return False
        try:
            save_catalog(self._chat_index, self._active_chat_id)
        except OSError as error:
            self._chat_store_problem = f"Sohbet listesi kaydedilemedi: {error}"
            self._chat_notice.configure(text=self._chat_store_problem)
            logging.warning(self._chat_store_problem)
            return False
        return True

    def _ensure_chat(self, goal: str) -> bool:
        if not self._chat_catalog_writable:
            return False
        if self._chat_record is None:
            fresh: ChatRecord = new_chat(goal)
            try:
                save_chat(fresh)
            except OSError as error:
                self._chat_store_problem = f"Sohbet oluşturulamadı: {error}"
                self._chat_notice.configure(text=self._chat_store_problem)
                return False
            self._chat_record = fresh
            self._active_chat_id = self._chat_record["id"]
        now: str = datetime.now(timezone.utc).isoformat()
        entry: ChatSummary = {**summary_of(self._chat_record), "updated_at": now}
        self._chat_index = [entry, *[chat for chat in self._chat_index if chat["id"] != self._chat_record["id"]]]
        if not self._save_catalog():
            return False
        self._refresh_chat_list()
        return True

    def _set_chat_outcome(self, outcome: Optional[str]) -> bool:
        """
        Etkin sohbetin son görev sonucunu bellekteki dizine ve kayda yazar (None siler); diske yazmaz. Sonuç
        kenar çubuğundaki durum noktasını (son görev başarısızsa kırmızı) belirler; sonuç değiştiyse True döner.
        """
        if self._active_chat_id is None:
            return False
        previous: Optional[str] = next(
            (chat.get("last_outcome") for chat in self._chat_index if chat["id"] == self._active_chat_id), None)
        self._chat_index = [
            with_outcome(chat, outcome) if chat["id"] == self._active_chat_id else chat
            for chat in self._chat_index
        ]
        if self._chat_record is not None:
            if outcome is None:
                self._chat_record.pop("last_outcome", None)
            else:
                self._chat_record["last_outcome"] = outcome
        return previous != outcome

    # --- Sohbet kaydı: anlık görüntü Tk'de, yazma arka plandaki sıralı yazıcıda ---

    def _snapshot_chat(self) -> Optional[ChatRecord]:
        """Tk thread'i: etkin sohbetin DEĞİŞMEZ anlık görüntüsünü çıkarır; Text.dump yalnız burada çalışır."""
        if self._chat_record is None:
            return None
        started: float = time.perf_counter()
        snapshot: ChatRecord = {
            **self._chat_record,
            "history": list(self._history),
            "spans": spans_from_dump(self._text.dump("1.0", "end-1c", text=True, tag=True)),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self._chat_snapshot_seconds = time.perf_counter() - started
        return snapshot

    def _submit_chat_write(self, snapshot: ChatRecord) -> "Future[None]":
        """
        Anlık görüntüyü tek thread'li yazıcıya sıralı verir (son durum kazanır). Kök dizin şimdi sabitlenir:
        yazıcı thread'i sonradan değişen veri dizinini görmez.
        """
        self._chat_last_save = time.monotonic()
        self._chat_dirty = False
        future: "Future[None]" = self._chat_executor.submit(save_chat, snapshot, chats_dir())
        self._chat_write = future
        return future

    def _request_chat_save(self, delivered_report: Optional[RunReport] = None) -> None:
        """Sohbeti arka plan yazıcısına verir; diske yazma BEKLENMEZ, sonuç sonraki karede uygulanır."""
        snapshot: Optional[ChatRecord] = self._snapshot_chat()
        if snapshot is None:
            return
        future = self._submit_chat_write(snapshot)
        self._watch_chat_write(future)
        evidence = delivered_report.get("evidence") if delivered_report else None
        if isinstance(evidence, dict) and isinstance(evidence.get("run_id"), str):
            run_id = evidence["run_id"]
            state_file = getattr(self, "_evidence_state_file", STATE_FILE)
            def confirm(done: "Future[None]") -> None:
                if done.cancelled() or done.exception() is not None:
                    return
                try:
                    EvidenceStore(state_file).mark_delivered(run_id)
                except (OSError, ValueError) as error:
                    logging.warning("Masaüstü evidence teslimi doğrulanamadı",
                                    extra={"error_type": type(error).__name__})
            future.add_done_callback(confirm)

    def _watch_chat_write(self, future: "Future[None]") -> None:
        """Yazma bitince sonucu (hata bildirimi ya da bildirimin temizlenmesi) Tk thread'ine taşır."""
        future.add_done_callback(lambda done: self._call_in_tk(lambda: self._apply_chat_write_result(done)))

    def _autosave_chat(self, now: float) -> None:
        """
        Değişen sohbeti arka plana verir: aralık en az CHAT_AUTOSAVE_INTERVAL, anlık görüntü pahalıysa daha
        seyrektir (Text.dump Tk süresinin ~%5'ini aşmasın); önceki yazma sürerken beklenir, son değişiklik yine yazılır.
        """
        if not self._chat_dirty or self._chat_record is None:
            return
        interval: float = max(CHAT_AUTOSAVE_INTERVAL, CHAT_SNAPSHOT_DUTY * self._chat_snapshot_seconds)
        if now - self._chat_last_save < interval:
            return
        if self._chat_write is not None and not self._chat_write.done():
            return
        self._request_chat_save()

    def _apply_chat_write_result(self, done: "Future[None]") -> None:
        """Tk thread'i: EN SON yazmanın sonucunu uygular; eski yazmaların sonucu yok sayılır."""
        if done is not self._chat_write or done.cancelled():
            return
        error: Optional[BaseException] = done.exception()
        if error is None:
            self._clear_chat_store_problem()
            return
        self._chat_dirty = True  # sonraki karede yeniden denenir
        if not isinstance(error, (OSError, ValueError)):
            raise error  # beklenmeyen tür: report_callback_exception'a gider
        self._report_chat_store_problem(error)

    def _save_current_chat(self) -> bool:
        """
        Sohbeti yazıcıya verip bitmesini bekler (sohbet değiştirme, silme, yeniden adlandırma gibi disk durumuna
        güvenen eylemlerden önce). JSON ve fsync yazıcı thread'indedir; Tk en çok CHAT_FLUSH_TIMEOUT_SECONDS
        bekler. Başarısızlıkta uyarı gösterir ve False döner.
        """
        snapshot: Optional[ChatRecord] = self._snapshot_chat()
        if snapshot is None:
            return True
        future: "Future[None]" = self._submit_chat_write(snapshot)
        try:
            future.result(timeout=CHAT_FLUSH_TIMEOUT_SECONDS)
        except TimeoutError:
            # Yazma sonradan biterse sonucu yine uygulanır.
            self._watch_chat_write(future)
            self._chat_dirty = True
            self._report_chat_store_problem(TimeoutError(f"yazma {CHAT_FLUSH_TIMEOUT_SECONDS:g} sn içinde bitmedi"))
            return False
        except (OSError, ValueError) as error:
            self._chat_dirty = True
            self._report_chat_store_problem(error)
            return False
        self._clear_chat_store_problem()
        return True

    def _report_chat_store_problem(self, error: BaseException) -> None:
        self._chat_store_problem = f"Sohbet kaydedilemedi: {error}"
        self._chat_notice.configure(text=self._chat_store_problem)
        logging.warning("Sohbet kaydedilemedi", extra={"error_type": type(error).__name__})

    def _clear_chat_store_problem(self) -> None:
        if self._chat_store_problem.startswith("Sohbet kaydedilemedi"):
            self._chat_store_problem = ""
            self._chat_notice.configure(text="")

    def _insert_spans(self, spans: List[TranscriptSpan]) -> None:
        """
        Kayıtlı parçaları TEK Tk çağrısında ekler (Text.insert 'metin etiketler metin etiketler…' biçimi):
        parça başına Python↔Tcl geçişi ve değişiklik bildirimi olmaz. Bağlantı ve katlama etiketleri önce kurulur.
        """
        if not spans:
            return
        for tag in {tag for span in spans for tag in span["tags"]}:
            self._prepare_tag(tag)
        arguments: List[object] = []
        for span in spans:
            arguments.extend((span["text"], tuple(span["tags"])))
        self._text.insert("end-1c", *arguments)

    def _open_chat(self, chat_id: str, save_current: bool = True) -> None:
        if self._agent_future is not None:
            return
        if save_current and chat_id == self._active_chat_id:
            return
        if save_current:
            if not self._save_current_chat():
                return
        try:
            record: ChatRecord = load_chat(chat_id)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            self._chat_store_problem = f"Sohbet açılamadı: {error}"
            self._chat_notice.configure(text=self._chat_store_problem)
            self._refresh_chat_list()  # silme akışlarında silinen satır listede kalmasın
            return
        previous_active: Optional[str] = self._active_chat_id
        self._reset_chat_view(show_welcome=False)
        self._chat_record = record
        self._active_chat_id = chat_id
        self._history = list(record["history"])
        legacy_welcome = bool(record["spans"]) and not record["history"] and all(
            set(span["tags"]) <= {"welcome_mark", "welcome_title", "welcome_dim", "welcome_example", "gap"}
            for span in record["spans"]
        )
        with self._writable_transcript():
            if record["spans"] and not legacy_welcome:
                self._empty_state.place_forget()
                # Kayıtlı katlama etiketlerinin kimlikleri bu oturumdakilerle çakışmasın diye yenilenir.
                spans, next_id = remap_fold_tags(record["spans"], self._fold_seq + 1)
                self._fold_seq = next_id - 1
                self._insert_spans(spans)
                self._restore_user_bubbles()
            else:
                self._render_welcome()
        self._chat_dirty = False  # yüklenen içerik diskteki kayıtla aynıdır
        self._stick_to_end = True
        self._text.see("end")
        # Uzun sohbetin satır yükseklikleri yerleşimden sonra ölçülür; sona dönüş bu yüzden ertelenir.
        self.after_idle(self._scroll_to_end)
        self.context_label.configure(text=f"bağlam: {len(self._history)} mesaj")
        if previous_active != chat_id:
            self._save_catalog()  # etkin sohbet değişti; açılışta (aynı sohbet) gereksiz dizin yazımı olmaz
        self._refresh_chat_list()

    def _new_chat(self) -> None:
        if self._agent_future is not None:
            return
        if not self._save_current_chat():
            return
        self._active_chat_id = None
        self._chat_record = None
        self._reset_chat_view(show_welcome=True)
        self._save_catalog()
        self._refresh_chat_list()
        self.entry.delete(0, "end")
        self.entry.focus_set()

    def _clear_transcript(self) -> None:
        """Eski temizleme çağrılarını geçmişi koruyan yeni sohbet akışına yönlendirir."""
        self._new_chat()

    def _reset_chat_view(self, show_welcome: bool) -> None:
        self._text.configure(state="normal")
        self._text.delete("1.0", "end")
        self._empty_state.place_forget()
        for card in self._artifact_widgets:
            card.destroy()
        self._artifact_widgets = []
        self._artifact_images = []
        for bubble in self._bubbles:
            bubble["frame"].destroy()
        self._bubbles = []
        # Bağlantı ve katlama etiketleri (kimlikler oturumda artar) silinir: etiket tablosu şişmesin.
        for tag in self._prepared_tags:
            self._text.tag_delete(tag)
        self._prepared_tags = set()
        self._open_folds = set()
        self._manual_folds = set()
        self._tool_group = None
        self._tool_groups = {}
        self._stick_to_end = True
        self._pending_text = {}
        self._raw_text = {}
        self._history = []
        self._turn = None
        self._tools_by_call = {}
        self._dirty_tools = {}
        self.context_label.configure(text="bağlam: 0 mesaj")
        self._region_text_tag = {}
        self._streaming_regions = {}
        self._live_regions = {}
        self._active_goal = ""
        if show_welcome:
            self._render_welcome()
        self._text.configure(state="disabled")
        self.stats_label.configure(text="")
        if self._task_status in ("done", "failed", "stopped"):
            # Önceki sohbetin son görev sonucu (✓/✕ süre) başka bir sohbete taşınmaz; sonuç satırdaki noktada durur.
            self._set_task_status("idle")

    def _on_close(self) -> None:
        """
        Kırmızı düğme, ⌘Q, Dock > Çık ve oturum kapatmada çağrılır. Pencere önce gizlenir, sohbet son kez
        yazıcıya verilir ve bağlantıları kapatma işi event loop thread'ine bırakılır; Tk thread'i en çok
        CLOSE_GRACE_SECONDS bekler, işi bitmemişse bloklanmadan bitişi yoklar (_poll_close; eskiden 3 sn ajan +
        8 sn bağlantı beklemesi Tk'yi kilitliyordu). Her koşulda pencere yok edilir: createcommand ile
        kayıtlı çağrıda istisna mainloop'u sonlandırırdı, bu yüzden başlangıç adımı patlarsa hemen
        _finish_close çalışır.
        """
        if self._destroyed or self._close_future is not None:
            return  # kapanış zaten başladı (⌘Q + kırmızı düğme art arda)
        self._closing = True  # kare döngüsü artık yeniden kurulmaz
        self.withdraw()
        started: bool = False
        try:
            self._begin_close()
            started = True
        finally:
            if not started:
                self._finish_close()

    def _begin_close(self) -> None:
        """Tk thread'inde hızlı adımlar: son anlık görüntü, kısayol/ses/rozet kapatma; ağır iş event loop'a verilir."""
        snapshot: Optional[ChatRecord] = self._snapshot_chat()
        final_write: Optional["Future[None]"] = None if snapshot is None else self._submit_chat_write(snapshot)
        if self._visibility_hotkey is not None:
            self._visibility_hotkey.close()
        self._voice.cancel()
        self._stop_event.set()
        set_dock_badge(None)
        self._menu_status.hide()
        # Tk yazı tipi nesnelerinin sonlandırıcısı (Font.__del__) Tcl çağırır; çöp toplama event loop
        # thread'inde çalışırsa o çağrı Tk thread'ini bekletir. Bekleyen çöp bu yüzden şimdi, Tk thread'inde toplanır.
        gc.collect()
        self._close_deadline = time.monotonic() + CLOSE_TIMEOUT_SECONDS
        self._close_future = asyncio.run_coroutine_threadsafe(self._shutdown(final_write), self._loop)
        # Kapatma çoğunlukla milisaniyeler sürer: kısa bir bekleme pencereyi hemen yok eder (gizli pencerede
        # görünmez). Uzun sürerse (çalışan ajan, yavaş bağlantı) Tk bloklanmaz, _poll_close bitişi yoklar.
        wait([self._close_future], timeout=CLOSE_GRACE_SECONDS)
        self._poll_close()

    async def _shutdown(self, final_write: Optional["Future[None]"]) -> None:
        """
        Event loop thread'i: sohbetin son yazımını ve çalışan ajanı kısa süre bekler, sonra tümleşik araçları ve
        model bağlantılarını kapatır. Önceki adım patlasa da bağlantılar kapatılır; hata _poll_close'ta günlüğe düşer.
        """
        try:
            if final_write is not None:
                await self._await_final_chat_write(final_write)
            await self._stop_running_agent()
        finally:
            try:
                await self._integrations.close()
            finally:
                await close_model_clients(self._clients)

    async def _await_final_chat_write(self, final_write: "Future[None]") -> None:
        """
        Kapanışta sohbetin son yazımını en çok CLOSE_FLUSH_WAIT_SECONDS bekler. Yazma İPTAL EDİLMEZ: süre dolarsa
        arka planda sürer (yazıcı thread'i yorumlayıcı çıkışında beklenir), yalnız uyarı yazılır.
        """
        wrapped: "asyncio.Future[None]" = asyncio.wrap_future(final_write)
        await asyncio.wait({wrapped}, timeout=CLOSE_FLUSH_WAIT_SECONDS)
        if not wrapped.done():
            logging.warning("Kapanışta sohbetin son kaydı süre içinde bitmedi; yazma arka planda sürüyor",
                            extra={"timeout_seconds": CLOSE_FLUSH_WAIT_SECONDS})
        elif wrapped.cancelled():
            logging.warning("Kapanışta sohbetin son kaydı iptal edilmiş")
        elif wrapped.exception() is not None:
            logging.warning("Kapanışta sohbet kaydedilemedi", extra={"error_type": type(wrapped.exception()).__name__})

    async def _stop_running_agent(self) -> None:
        """Durdurulması istenen ajanı en çok CLOSE_AGENT_WAIT_SECONDS bekler; durmadıysa iptal eder."""
        agent: Optional["Future[RunReport]"] = self._agent_future
        if agent is None or agent.done():
            return
        wrapped: "asyncio.Future[RunReport]" = asyncio.wrap_future(agent)
        await asyncio.wait({wrapped}, timeout=CLOSE_AGENT_WAIT_SECONDS)
        if wrapped.done():
            if not wrapped.cancelled():
                wrapped.exception()  # hata _on_agent_future_done'da işlenir; burada yalnız "okundu" diye işaretlenir
            return
        logging.warning("Kapanışta çalışan görev süre içinde durmadı; iptal ediliyor",
                        extra={"timeout_seconds": CLOSE_AGENT_WAIT_SECONDS})
        agent.cancel()

    def _poll_close(self) -> None:
        """Tk thread'i: kapatma işi bitince ya da süre dolunca pencereyi yok eder; beklerken Tk'yi bloklamaz."""
        future: Optional["Future[None]"] = self._close_future
        if future is None or not future.done():
            if time.monotonic() < self._close_deadline:
                self.after(CLOSE_POLL_MS, self._poll_close)
                return
            logging.warning("Kapanışta bağlantılar zamanında kapatılamadı",
                            extra={"timeout_seconds": CLOSE_TIMEOUT_SECONDS})
        elif not future.cancelled() and future.exception() is not None:
            logging.warning("Kapanışta bağlantılar kapatılamadı",
                            extra={"error_type": type(future.exception()).__name__})
        self._finish_close()

    def _finish_close(self) -> None:
        """Yazıcıyı ve event loop'u durdurup pencereyi yok eder; ikinci çağrı zararsızdır."""
        if self._destroyed:
            return
        self._destroyed = True
        try:
            # Bekleyen son sohbet yazımı İPTAL EDİLMEZ: işçi thread'i işini bitirir (yorumlayıcı çıkışında beklenir).
            self._chat_executor.shutdown(wait=False, cancel_futures=False)
            self._loop.call_soon_threadsafe(self._loop.stop)
        finally:
            self.destroy()


def main() -> None:
    """OmniAgent masaüstü arayüzünü başlatır."""
    if sys.argv[1:] == ["--internal-readonly-worker"]:
        from omniagent.tools.readonly_worker import main as readonly_worker_main
        readonly_worker_main()
        return
    if sys.argv[1:] == ["--bundle-check"]:
        # Paket açılışını Keychain izni istemeden doğrula.
        probe = tk.Tk()
        probe.withdraw()
        probe.update_idletasks()
        probe.destroy()
        return

    if getattr(sys, "frozen", False) and Path.cwd() == Path("/"):
        # Finder'ın / çalışma dizini araçların çıktı yazmasını engeller.
        os.chdir(Path.home())
    configure_ui_logging(data_root() / LOG_FILE_NAME)  # paketli uygulamada stderr görünmez
    app = OmniUI()
    if not native_macos.install_native_menu_bar("OmniAgent"):
        logging.info("Native menü çubuğu kurulamadı (macOS dışında beklenen).")
    # Yeni .app kimliği için Keychain izin istemi çıkabilir: okuma ilk boyamadan sonra arka planda yapılır.
    app.begin_background_startup()
    app.mainloop()


if __name__ == "__main__":
    main()
