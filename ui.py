import asyncio
import logging
import sys
import uuid
import webbrowser
import json
import math
import threading
from io import BytesIO
import time
import tkinter as tk
import tkinter.font as tkfont
from concurrent.futures import Future
from pathlib import Path
from queue import Empty, Queue
from typing import Dict, List, Optional, Tuple, TypedDict

import customtkinter as ctk
from openai import AsyncOpenAI
from PIL import Image

import api_keys
from config import (
    API_KEY_VARIABLES, BACKENDS, DEFAULT_BACKEND, api_key_source, apply_stored_api_keys,
    load_api_key, refresh_api_keys, set_api_key, set_backend_model,
)
from events import AgentEvent, compact_count, tool_label
from desktop_status import MenuBarTaskStatus, app_is_active, is_backgrounded, notify_finished, set_dock_badge
from host_lock import host_task_lock
from model_catalog import (
    ModelCatalogError, cached_models, list_provider_models, load_model_preferences,
    save_model_preferences, valid_model_id,
)
from main import (
    RUN_MODE_PROFILES, STATE_FILE, RunOptions, RunReport, close_model_clients,
    create_model_clients, run_agent_with_callback,
)
from state_manager import EpisodeMetrics
from markdown_render import render_markdown
from conversation import Exchange, make_exchange, trim_history
from capabilities import Capability, CapabilityService
from voice import VoiceInput, VoiceInputError

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


# --- Palet: Claude Code (turuncu vurgu, ⏺ ⎿ glifleri, yıldız spinner) + Codex (nötr koyu
# yüzeyler, mono transkript, $ komut satırları) ---
BG: str = "#141413"
SURFACE: str = "#1C1C1A"
SURFACE_RAISED: str = "#262624"
COMMAND_BG: str = "#1E1E1C"
BORDER: str = "#34332F"
TEXT: str = "#ECEAE3"
TEXT_DIM: str = "#A3A199"
TEXT_FAINT: str = "#6E6C66"
ACCENT: str = "#D97757"
ACCENT_HOVER: str = "#E48A6C"
ACCENT_DIM: str = "#7A4634"
SHINE: str = "#F6C4AE"
SUCCESS: str = "#4EBA65"
ERROR: str = "#FF6B80"
WARNING: str = "#FFC107"
INFO: str = "#B1B9F9"

MONO_FAMILY: str = "Menlo"
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


def _svg_ctk_image(svg: str, color: str, size: int = 20) -> ctk.CTkImage:
    """macOS SVG verisini CTkImage'a çevirir; ikon yüklenemezse sessiz boş yedeğe düşer."""
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
    image = image.copy()
    return ctk.CTkImage(light_image=image, dark_image=image, size=(size, size))

SPINNER_FRAMES: Tuple[str, ...] = ("·", "✢", "✳", "✶", "✻", "✽", "✻", "✶", "✳", "✢")

FRAME_MS: int = 16
IDLE_FRAME_MS: int = 100
RUNNING_IDLE_FRAME_MS: int = 50
SPINNER_INTERVAL: float = 0.11
SHIMMER_INTERVAL: float = 0.07
BLINK_INTERVAL: float = 0.45
RUNNING_REFRESH_INTERVAL: float = 0.2
MAX_EVENTS_PER_FRAME: int = 400
# Daktilo: her karede en az bu kadar karakter; birikme büyükse ~TYPEWRITER_DRAIN_FRAMES karede boşalır
TYPEWRITER_MIN_CHARS: int = 3
TYPEWRITER_DRAIN_FRAMES: int = 5
LIVE_TAIL_LINES: int = 6
SUMMARY_LINES: int = 4
COMMAND_LINES: int = 6
LINE_CLIP: int = 160
INSERT_MARK: str = "omni_insert"


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


def _clip_line(line: str) -> str:
    """Tek satırı gösterim sınırında kırpar. Saf."""
    single: str = line.rstrip("\n")
    return single if len(single) <= LINE_CLIP else single[:LINE_CLIP] + "…"


def summarize_result(name: str, text: str, head: List[str], line_count: int, ok: bool) -> List[str]:
    """Bitmiş aracın transkriptte gösterilecek kısa özet satırları (Claude Code tarzı). Saf."""
    if not ok:
        lines: List[str] = [line for line in text.strip().splitlines() if line.strip()]
        more: List[str] = [f"… +{len(lines) - 3} satır"] if len(lines) > 3 else []
        return [_clip_line(line) for line in lines[:3]] + more
    if name in ("execute_shell", "execute_js"):
        if line_count == 0:
            return ["(çıktı yok)"]
        rest: List[str] = [f"… +{line_count - SUMMARY_LINES} satır"] if line_count > SUMMARY_LINES else []
        return [_clip_line(line) for line in head[:SUMMARY_LINES]] + rest
    if name == "read_file":
        return [f"{text.count(chr(10)) + 1} satır okundu"]
    if name == "web_search":
        try:
            results: object = json.loads(text)
        except json.JSONDecodeError:
            results = []
        titles: List[str] = [str(item.get("title", "")) for item in results if isinstance(item, dict)] if isinstance(results, list) else []
        return [f"• {_clip_line(title)}" for title in titles[:SUMMARY_LINES]] or [_clip_line(text)]
    lines = [line for line in text.strip().splitlines() if line.strip()]
    tail_note: List[str] = [f"… +{len(lines) - 3} satır"] if len(lines) > 3 else []
    return [_clip_line(line) for line in lines[:3]] + tail_note


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


class OmniUI(ctk.CTk):
    """
    OmniAgent arayüzü: ajanın model yanıtını, araç çağrılarını, çalıştırdığı komutları ve
    komut çıktılarını AKIŞ olarak, animasyonlu gösterir. Olaylar ajan thread'lerinden
    kuyruğa gelir; tüm çizim ~60 fps'lik tek bir kare döngüsünde (Tk thread'i) yapılır.
    """

    def __init__(self) -> None:
        super().__init__()
        ctk.set_appearance_mode("dark")
        self.title("OmniAgent")
        self.geometry("780x880")
        self.minsize(560, 620)
        self.configure(fg_color=BG)
        self.bind("<Map>", lambda event: self.after_idle(self._style_native_titlebar)
                  if event.widget is self else None, add="+")
        self._ui_family: str = tkfont.nametofont("TkDefaultFont").actual("family")
        self._voice_icon: ctk.CTkImage = _svg_ctk_image(MICROPHONE_SVG, TEXT_DIM)
        self._voice_icon_active: ctk.CTkImage = _svg_ctk_image(MICROPHONE_SVG, TEXT)
        self._voice_icon_busy: ctk.CTkImage = _svg_ctk_image(MICROPHONE_SVG, TEXT_FAINT)
        self._copy_icon: ctk.CTkImage = _svg_ctk_image(COPY_SVG, TEXT_DIM)
        self._gear_icon: ctk.CTkImage = _svg_ctk_image(GEAR_SVG, TEXT_DIM)
        self._menu_status = MenuBarTaskStatus()

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        self._build_header()
        self._build_transcript()
        self._build_activity_bar()
        self._build_composer()
        self._build_footer()

        self._inbox: "Queue[UiItem]" = Queue()
        self._stop_event: threading.Event = threading.Event()
        self._agent_future: Optional["Future[RunReport]"] = None
        self._history: List[Exchange] = []
        self._active_goal: str = ""
        self._input_futures: Dict[str, asyncio.Future] = {}
        self._input_windows: Dict[str, ctk.CTkToplevel] = {}
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
        self._clients: Dict[str, AsyncOpenAI] = create_model_clients()
        # Ayarlar sayfası anahtar kaydederse istemciler yenilenir; görev sürerken beklemeye alınır.
        self._clients_stale: bool = False
        self._pending_model_choices: Dict[str, str] = {}
        self._settings_window: Optional[ctk.CTkToplevel] = None
        self._integrations = CapabilityService()

        self.bind("<Escape>", lambda event: self._request_stop())
        self.bind("<FocusIn>", self._on_focus_return, add="+")
        self.bind("<Command-k>", lambda event: self._clear_transcript())
        self.bind("<Command-comma>", lambda event: self._open_settings())
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._text.configure(state="normal")
        self._render_welcome()
        self._text.configure(state="disabled")
        self._set_activity_idle()
        self.after(FRAME_MS, self._tick)
        self.after(120, self.entry.focus_set)
        self.after(120, self._style_native_titlebar)

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

    # --- Yerleşim ---

    def _ui_font(self, size: int, weight: str) -> ctk.CTkFont:
        return ctk.CTkFont(family=self._ui_family, size=size, weight=weight)

    def _build_header(self) -> None:
        header: ctk.CTkFrame = ctk.CTkFrame(self, fg_color=BG, corner_radius=0, height=48)
        header.grid(row=0, column=0, sticky="ew", padx=18, pady=(12, 0))
        header.grid_columnconfigure(2, weight=1)
        ctk.CTkLabel(header, text="✻", text_color=ACCENT, font=ctk.CTkFont(family=MONO_FAMILY, size=20, weight="bold")).grid(
            row=0, column=0, padx=(0, 8))
        ctk.CTkLabel(header, text="OmniAgent", text_color=TEXT, font=self._ui_font(16, "bold")).grid(row=0, column=1)
        self.context_label: ctk.CTkLabel = ctk.CTkLabel(
            header, text="bağlam: 0 mesaj", text_color=TEXT_FAINT,
            font=ctk.CTkFont(family=MONO_FAMILY, size=10),
        )
        self.context_label.grid(row=1, column=1, columnspan=3, sticky="w")
        self.model_label: ctk.CTkLabel = ctk.CTkLabel(
            header, text=self._model_text(DEFAULT_BACKEND), text_color=TEXT_FAINT,
            font=ctk.CTkFont(family=MONO_FAMILY, size=11),
        )
        self.model_label.grid(row=0, column=3, padx=(0, 8))
        self.task_status_label: ctk.CTkLabel = ctk.CTkLabel(
            header, text="", text_color=ACCENT,
            font=ctk.CTkFont(family=MONO_FAMILY, size=11, weight="bold"),
        )
        self.task_status_label.grid(row=0, column=2, sticky="e", padx=(0, 12))
        self.copy_btn: ctk.CTkButton = ctk.CTkButton(
            header, text="", image=self._copy_icon, width=30, height=28, corner_radius=8,
            fg_color="transparent", hover_color=SURFACE_RAISED, border_width=1,
            border_color=BORDER, command=self._copy_transcript, cursor="hand2",
        )
        self.copy_btn.grid(row=0, column=4, padx=(0, 6))
        # Kopyala düğmesiyle birebir aynı kutu ve ikon boyutu (metin glifi daha küçük kalıyordu).
        self.settings_btn: ctk.CTkButton = ctk.CTkButton(
            header, text="", image=self._gear_icon, width=30, height=28, corner_radius=8,
            fg_color="transparent", hover_color=SURFACE_RAISED, border_width=1,
            border_color=BORDER, command=self._open_settings, cursor="hand2",
        )
        self.settings_btn.grid(row=0, column=5, padx=(0, 6))
        ctk.CTkButton(
            header, text="Temizle", width=64, height=26, corner_radius=8, fg_color="transparent",
            hover_color=SURFACE_RAISED, border_width=1, border_color=BORDER, text_color=TEXT_DIM,
            font=self._ui_font(12, "normal"), command=self._clear_transcript,
        ).grid(row=0, column=6)

    def _build_transcript(self) -> None:
        frame: ctk.CTkFrame = ctk.CTkFrame(self, fg_color=BG, corner_radius=0)
        frame.grid(row=1, column=0, sticky="nsew", padx=(6, 6), pady=(8, 0))
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(0, weight=1)
        self._mono: tkfont.Font = tkfont.Font(family=MONO_FAMILY, size=12)
        self._text: tk.Text = tk.Text(
            frame, bg=BG, fg=TEXT, font=self._mono, wrap="word", bd=0, highlightthickness=0,
            padx=16, pady=10, insertwidth=0, cursor="arrow", spacing1=1, spacing3=1,
            selectbackground=ACCENT_DIM, selectforeground=TEXT, exportselection=True,
        )
        self._text.grid(row=0, column=0, sticky="nsew")
        scrollbar: ctk.CTkScrollbar = ctk.CTkScrollbar(
            frame, command=self._text.yview, fg_color=BG, button_color=BORDER, button_hover_color=TEXT_FAINT,
        )
        scrollbar.grid(row=0, column=1, sticky="ns")
        self._text.configure(yscrollcommand=scrollbar.set, state="disabled")
        indent: int = self._mono.measure("⏺ ")
        # Kayan (wrap) çıktı satırları '⎿  ' sonrasındaki metin sütununa hizalanır
        output_margin: int = indent + self._mono.measure("⎿  ")
        mono_bold = (MONO_FAMILY, 12, "bold")
        small = (MONO_FAMILY, 11)
        tags: Dict[str, Dict[str, object]] = {
            "welcome_mark": {"foreground": ACCENT, "font": (MONO_FAMILY, 13, "bold")},
            "welcome_title": {"foreground": TEXT, "font": mono_bold},
            "welcome_dim": {"foreground": TEXT_FAINT, "font": small, "lmargin1": indent, "lmargin2": indent},
            "goal": {"foreground": TEXT, "background": SURFACE_RAISED, "lmargin1": 10, "lmargin2": 10 + indent,
                     "spacing1": 8, "spacing3": 8, "rmargin": 10},
            "goal_prompt": {"foreground": ACCENT, "background": SURFACE_RAISED, "font": mono_bold},
            "gap": {"font": (MONO_FAMILY, 6)},
            "bullet_text": {"foreground": TEXT, "spacing1": 9},
            "assistant": {"foreground": TEXT, "lmargin1": indent, "lmargin2": indent},
            "cursor": {"foreground": ACCENT, "lmargin1": indent, "lmargin2": indent},
            "reasoning_head": {"foreground": TEXT_FAINT, "font": (MONO_FAMILY, 11, "italic"), "spacing1": 9},
            "reasoning": {"foreground": TEXT_FAINT, "font": (MONO_FAMILY, 11, "italic"), "lmargin1": indent, "lmargin2": indent},
            "bullet_streaming": {"foreground": TEXT_FAINT, "spacing1": 9},
            "bullet_running": {"foreground": ACCENT, "spacing1": 9},
            "bullet_ok": {"foreground": SUCCESS, "spacing1": 9},
            "bullet_error": {"foreground": ERROR, "spacing1": 9},
            "tool_name": {"foreground": TEXT, "font": mono_bold},
            "tool_args": {"foreground": TEXT_DIM},
            "command": {"foreground": TEXT, "background": COMMAND_BG, "lmargin1": indent, "lmargin2": indent + 16},
            "command_prompt": {"foreground": ACCENT, "background": COMMAND_BG, "lmargin1": indent},
            "gutter": {"foreground": TEXT_FAINT, "lmargin1": indent, "lmargin2": output_margin},
            "gutter_hidden": {"foreground": BG, "lmargin1": indent, "lmargin2": output_margin},
            "output": {"foreground": TEXT_DIM, "font": small, "lmargin2": output_margin},
            "output_error": {"foreground": ERROR, "font": small, "lmargin2": output_margin},
            "meta": {"foreground": TEXT_FAINT, "font": small},
            "summary_ok": {"foreground": SUCCESS, "font": mono_bold},
            "summary_error": {"foreground": ERROR, "font": mono_bold},
            "summary_meta": {"foreground": TEXT_FAINT, "font": small},
            "notice_info": {"foreground": INFO, "font": small, "lmargin1": indent, "lmargin2": indent},
            "notice_warning": {"foreground": WARNING, "font": small, "lmargin1": indent, "lmargin2": indent},
            "notice_error": {"foreground": ERROR, "font": small, "lmargin1": indent, "lmargin2": indent},
        }
        tags.update({
            "md_h1": {"font": (MONO_FAMILY, 18, "bold"), "spacing1": 10},
            "md_h2": {"font": (MONO_FAMILY, 15, "bold"), "spacing1": 8},
            "md_h3": {"font": (MONO_FAMILY, 13, "bold"), "spacing1": 6},
            "md_bold": {"font": mono_bold},
            "md_italic": {"font": (MONO_FAMILY, 12, "italic")},
            "md_code": {"background": COMMAND_BG, "foreground": TEXT},
            "md_codeblock": {"background": COMMAND_BG, "spacing1": 4, "spacing3": 4},
            "md_bullet": {"lmargin2": indent + 16},
            "md_quote": {"foreground": TEXT_DIM, "lmargin1": indent + 8, "lmargin2": indent + 8},
            "md_rule": {"foreground": BORDER},
            "md_table_head": {"font": mono_bold},
            "md_link": {"foreground": INFO, "underline": True},
        })
        for name, options in tags.items():
            self._text.tag_configure(name, **options)

    def _build_activity_bar(self) -> None:
        bar: ctk.CTkFrame = ctk.CTkFrame(self, fg_color=BG, corner_radius=0, height=26)
        bar.grid(row=2, column=0, sticky="ew", padx=22, pady=(4, 2))
        bar.grid_columnconfigure(0, weight=1)
        self._activity: tk.Text = tk.Text(
            bar, height=1, bg=BG, fg=TEXT_FAINT, bd=0, highlightthickness=0, wrap="none",
            font=(MONO_FAMILY, 12), insertwidth=0, cursor="arrow", padx=0, pady=2,
        )
        self._activity.grid(row=0, column=0, sticky="ew")
        self._activity.tag_configure("glyph", foreground=ACCENT, font=(MONO_FAMILY, 13, "bold"))
        self._activity.tag_configure("verb", foreground=ACCENT)
        self._activity.tag_configure("shine", foreground=SHINE)
        self._activity.tag_configure("meta", foreground=TEXT_FAINT, font=(MONO_FAMILY, 11))
        self._activity.tag_raise("shine")
        self._activity.configure(state="disabled")

    def _build_composer(self) -> None:
        composer: ctk.CTkFrame = ctk.CTkFrame(self, fg_color=SURFACE, corner_radius=14, border_width=1, border_color=BORDER)
        composer.grid(row=3, column=0, sticky="ew", padx=16, pady=(2, 4))
        composer.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(composer, text="›", text_color=ACCENT, font=ctk.CTkFont(family=MONO_FAMILY, size=18, weight="bold")).grid(
            row=0, column=0, padx=(14, 4), pady=10)
        self.entry: ctk.CTkEntry = ctk.CTkEntry(
            composer, placeholder_text="Bir hedef yaz… (örn. masaüstündeki PDF'leri listele)", height=36,
            fg_color=SURFACE, border_width=0, text_color=TEXT, placeholder_text_color=TEXT_FAINT,
            font=self._ui_font(14, "normal"),
        )
        self.entry.grid(row=0, column=1, sticky="ew", pady=8)
        self.entry.bind("<Return>", lambda event: self._on_primary_button())
        self.mode_menu: ctk.CTkOptionMenu = ctk.CTkOptionMenu(
            composer, values=list(RUN_MODE_CHOICES), width=82, height=28, corner_radius=8,
            fg_color=SURFACE_RAISED, button_color=SURFACE_RAISED, button_hover_color=BORDER,
            dropdown_fg_color=SURFACE, dropdown_hover_color=SURFACE_RAISED, dropdown_text_color=TEXT,
            text_color=TEXT_DIM, font=ctk.CTkFont(family=MONO_FAMILY, size=11),
            dropdown_font=ctk.CTkFont(family=MONO_FAMILY, size=11),
        )
        self.mode_menu.set(RUN_MODE_CHOICES[0])
        self.mode_menu.grid(row=0, column=2, padx=(6, 4))
        self.backend_menu: ctk.CTkOptionMenu = ctk.CTkOptionMenu(
            composer, values=list(BACKEND_CHOICES), width=126, height=28, corner_radius=8,
            fg_color=SURFACE_RAISED, button_color=SURFACE_RAISED, button_hover_color=BORDER,
            dropdown_fg_color=SURFACE, dropdown_hover_color=SURFACE_RAISED, dropdown_text_color=TEXT,
            text_color=TEXT_DIM, font=ctk.CTkFont(family=MONO_FAMILY, size=11),
            dropdown_font=ctk.CTkFont(family=MONO_FAMILY, size=11),
        )
        self.backend_menu.set("Otomatik")
        self.backend_menu.grid(row=0, column=3, padx=(4, 6))
        self.voice_btn: ctk.CTkButton = ctk.CTkButton(
            composer, text="", image=self._voice_icon, width=34, height=34, corner_radius=17,
            fg_color=SURFACE_RAISED, hover_color=SURFACE_RAISED, text_color=TEXT_DIM,
            command=self._toggle_voice, cursor="hand2",
        )
        self.voice_btn.grid(row=0, column=4, padx=(0, 6))
        self.primary_btn: ctk.CTkButton = ctk.CTkButton(
            composer, text="↑", width=34, height=34, corner_radius=17, fg_color=ACCENT, hover_color=ACCENT_HOVER,
            text_color=BG, font=ctk.CTkFont(family=self._ui_family, size=17, weight="bold"),
            command=self._on_primary_button,
        )
        self.primary_btn.grid(row=0, column=5, padx=(0, 10))

    def _build_footer(self) -> None:
        footer: ctk.CTkFrame = ctk.CTkFrame(self, fg_color=BG, corner_radius=0)
        footer.grid(row=4, column=0, sticky="ew", padx=22, pady=(0, 10))
        footer.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            footer, text="enter gönder · esc durdur · ⌘K temizle", text_color=TEXT_FAINT,
            font=ctk.CTkFont(family=MONO_FAMILY, size=10), anchor="w",
        ).grid(row=0, column=0, sticky="w")
        self.stats_label: ctk.CTkLabel = ctk.CTkLabel(
            footer, text="", text_color=TEXT_FAINT, font=ctk.CTkFont(family=MONO_FAMILY, size=10),
            anchor="w", justify="left",
        )
        self.stats_label.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(2, 0))

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

        async def retire() -> None:
            """Eski bağlantı havuzunu kapatır; kapanış hatası event loop'u düşürmez."""
            try:
                await close_model_clients(replaced)
            except Exception as error:
                logging.warning("Eski model istemcileri kapatılamadı",
                                extra={"error_type": type(error).__name__})

        # Eski bağlantı havuzu yalnız burada, çalışan görev yokken kapatılır.
        asyncio.run_coroutine_threadsafe(retire(), self._loop)

    def _open_settings(self) -> None:
        """Tema ile uyumlu Ayarlar sayfasını açar; kaydedilen anahtarlar anında etkinleşir."""
        existing: Optional[ctk.CTkToplevel] = self._settings_window
        if existing is not None and existing.winfo_exists():
            existing.lift()
            existing.focus_force()
            return
        window: ctk.CTkToplevel = ctk.CTkToplevel(self)
        self._settings_window = window
        window.title("OmniAgent — Ayarlar")
        window.geometry("660x700")
        window.minsize(560, 450)
        window.configure(fg_color=BG)
        window.transient(self)
        panel: ctk.CTkScrollableFrame = ctk.CTkScrollableFrame(window, fg_color=BG)
        panel.pack(fill="both", expand=True, padx=6, pady=6)
        ctk.CTkLabel(
            panel, text="Ayarlar", text_color=TEXT, font=self._ui_font(17, "bold"), anchor="w",
        ).pack(anchor="w", padx=12, pady=(8, 0))
        ctk.CTkLabel(
            panel, text="API ANAHTARLARI", text_color=TEXT_FAINT, anchor="w",
            font=ctk.CTkFont(family=MONO_FAMILY, size=10),
        ).pack(anchor="w", padx=12, pady=(14, 2))
        ctk.CTkLabel(
            panel, text=SETTINGS_HINT, text_color=TEXT_DIM, wraplength=560, justify="left",
            font=self._ui_font(11, "normal"), anchor="w",
        ).pack(anchor="w", padx=12, pady=(0, 8))

        entries: Dict[str, ctk.CTkEntry] = {}
        chips: Dict[str, ctk.CTkLabel] = {}
        for variable in api_keys.KEY_VARIABLES:
            card: ctk.CTkFrame = ctk.CTkFrame(
                panel, fg_color=SURFACE, corner_radius=12, border_width=1, border_color=BORDER,
            )
            card.pack(fill="x", padx=12, pady=6)
            card.grid_columnconfigure(0, weight=1)
            profiles: List[str] = [name for name, var in API_KEY_VARIABLES.items() if var == variable]
            ctk.CTkLabel(
                card, text=variable, text_color=TEXT, anchor="w",
                font=ctk.CTkFont(family=MONO_FAMILY, size=12, weight="bold"),
            ).grid(row=0, column=0, sticky="w", padx=14, pady=(12, 0))
            chip: ctk.CTkLabel = ctk.CTkLabel(
                card, text="", anchor="e", font=ctk.CTkFont(family=MONO_FAMILY, size=10),
            )
            chip.grid(row=0, column=1, sticky="e", padx=14, pady=(12, 0))
            entry: ctk.CTkEntry = ctk.CTkEntry(
                card, show="•", height=32, fg_color=SURFACE_RAISED, border_width=1, border_color=BORDER,
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
                card, text=detail, text_color=TEXT_FAINT, wraplength=520, justify="left", anchor="w",
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
            panel, text="MODELLER", text_color=TEXT_FAINT, anchor="w",
            font=ctk.CTkFont(family=MONO_FAMILY, size=10),
        ).pack(anchor="w", padx=12, pady=(16, 2))
        ctk.CTkLabel(
            panel, text="Sağlayıcı modelleri arka planda yüklenir. Seçim sonraki görevde kullanılır.",
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
                card, text="", text_color=TEXT_FAINT, anchor="w",
                font=ctk.CTkFont(family=MONO_FAMILY, size=9),
            )
            note.pack(anchor="w", padx=14, pady=(0, 9))
            model_notes[name] = note

        generation = [0]
        active_model_requests: List[Future[Tuple[str, ...]]] = []

        def refresh_models(force: bool = False) -> None:
            """Ağ işini model döngüsüne verir; Tk yalnız hazır sonucu çizer."""
            generation[0] += 1
            for previous in active_model_requests:
                previous.cancel()
            active_model_requests.clear()
            current_generation = generation[0]
            pending: Dict[str, Future[Tuple[str, ...]]] = {}
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
                pending[provider] = asyncio.run_coroutine_threadsafe(
                    list_provider_models(provider, profile["base_url"], key, refresh=force),
                    self._loop,
                )
            active_model_requests.extend(pending.values())
            for provider in pending:
                for name in profiles_by_provider[provider]:
                    model_notes[name].configure(text="Modeller yükleniyor…")
            if not pending:
                return

            def poll() -> None:
                if current_generation != generation[0] or not window.winfo_exists():
                    return
                for provider, future in list(pending.items()):
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
            panel, text="", text_color=TEXT_FAINT, anchor="w", wraplength=560, justify="left",
            font=ctk.CTkFont(family=MONO_FAMILY, size=10),
        )
        status.pack(anchor="w", padx=12, pady=(6, 0))

        def close() -> None:
            generation[0] += 1
            for request in active_model_requests:
                request.cancel()
            self._settings_window = None
            window.destroy()

        def save() -> None:
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
            if failed or model_error:
                problems = []
                if failed:
                    problems.append("API anahtarı: " + ", ".join(failed))
                if model_error:
                    problems.append("Model: " + model_error)
                status.configure(text="Kaydedilemedi: " + " · ".join(problems), text_color=ERROR)
                return
            if not changed and not model_changed:
                status.configure(text="Değişiklik yok.", text_color=TEXT_FAINT)
                return
            status.configure(
                text=f"Kaydedildi · {len(model_changed)} model · hazır profiller: {ready}{suffix}",
                text_color=WARNING if self._clients_stale or shell_left else SUCCESS,
            )

        buttons: ctk.CTkFrame = ctk.CTkFrame(panel, fg_color=BG)
        buttons.pack(fill="x", padx=12, pady=(10, 14))
        ctk.CTkButton(
            buttons, text="Kaydet", width=110, height=32, corner_radius=8, fg_color=ACCENT,
            hover_color=ACCENT_HOVER, text_color=BG, font=self._ui_font(13, "bold"), command=save,
        ).pack(side="left")
        ctk.CTkButton(
            buttons, text="Kapat", width=90, height=32, corner_radius=8, fg_color="transparent",
            hover_color=SURFACE_RAISED, border_width=1, border_color=BORDER, text_color=TEXT_DIM,
            font=self._ui_font(12, "normal"), command=close,
        ).pack(side="left", padx=8)
        window.protocol("WM_DELETE_WINDOW", close)
        window.after(60, window.lift)
        self._schedule_titlebar_style(window)

    def _schedule_titlebar_style(self, window: ctk.CTkToplevel) -> None:
        """
        Başlık çubuğu stilini macOS penceresi oluştuktan sonra uygular; böylece Ayarlar
        penceresi de ana pencereyle aynı arka plan renginde görünür.
        """
        title: str = window.title()
        window.after(120, lambda: self._style_native_titlebar(title))

    # --- Transkript bölgeleri (etiket tabanlı; her bölge '\n' ile biter, asla boş kalmaz) ---

    def _insert_parts(self, index: str, region: str, parts: List[Tuple[str, Tuple[str, ...]]]) -> None:
        self._text.mark_set(INSERT_MARK, index)
        self._text.mark_gravity(INSERT_MARK, "right")
        for content, tags in parts:
            if content:
                self._text.insert(INSERT_MARK, content, tags + (region,))

    def _new_region(self, parts: List[Tuple[str, Tuple[str, ...]]]) -> str:
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
            self._replace_region(region, [("⏺ ", ("bullet_text",))] + render_markdown(self._raw_text[region]))
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
        assert self._turn is not None
        if self._turn["text_region"] is None:
            self._turn["text_region"] = self._new_streaming_region(("⏺ ", ("bullet_text",)), "assistant")
        return self._turn["text_region"]

    def _reasoning_region(self) -> str:
        assert self._turn is not None
        if self._turn["reasoning_region"] is None:
            self._turn["reasoning_region"] = self._new_streaming_region(("∴ düşünce ", ("reasoning_head",)), "reasoning")
        return self._turn["reasoning_region"]

    def _end_live_regions(self) -> None:
        """Model turu bitti: bölgeler artık akış almaz; bekleyen metin bitince imleç kalkar."""
        for region in list(self._live_regions):
            self._live_regions[region] = False
            if not self._pending_text.get(region):
                self._finish_streaming(region)

    def _tool_view(self, index: int, name: str) -> ToolView:
        assert self._turn is not None
        if index not in self._turn["tools"]:
            view: ToolView = {
                "region": self._new_region([("⏺\n", ("bullet_streaming",))]), "name": name, "preview": "",
                "status": "streaming", "call_id": "", "head": [], "tail": [], "line_count": 0,
                "result": "", "seconds": 0.0, "started_at": 0.0,
            }
            self._turn["tools"][index] = view
        return self._turn["tools"][index]

    def _mark_dirty(self, view: ToolView) -> None:
        self._dirty_tools[view["region"]] = view

    def _handle_event(self, event: AgentEvent) -> None:
        if event["kind"] == "integration_status":
            self._activity_verb = event["text"]
            self._new_region([(event["text"] + "\n", ("notice_info",))])
        elif event["kind"] == "user_input_required":
            self._show_input(event["request_id"], event["title"], event["fields"])
        elif event["kind"] == "run_started":
            self.model_label.configure(text=self._model_text(event["backend"]))
        elif event["kind"] == "turn_started":
            self._turn = {"number": event["turn"], "text_region": None, "reasoning_region": None, "tools": {}}
            self._turn_streamed_chars = 0
            self._activity_verb = "Düşünüyor"
            self.model_label.configure(text=self._model_text(event["backend"]))
        elif event["kind"] == "text_delta" and self._turn is not None:
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
            for index, view in list(self._turn["tools"].items()):
                if view["status"] == "streaming":
                    self._delete_region(view["region"])
                    del self._turn["tools"][index]
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
            self._mark_dirty(view)
            if all(v["status"] in ("ok", "error") for v in self._tools_by_call.values()):
                self._activity_verb = "Düşünüyor"
        elif event["kind"] == "backend_changed":
            self.model_label.configure(text=self._model_text(event["backend"]))
            self._new_region([(f"↻ {event['backend']} modeline geçildi ({event['reason']})\n", ("notice_info",))])
        elif event["kind"] == "notice":
            self._new_region([(f"{'⚠' if event['level'] != 'info' else 'ℹ'} {event['text']}\n", (f"notice_{event['level']}",))])
        elif event["kind"] == "run_finished":
            self._render_summary(event["success"], event["reason"], event["metrics"])
            status: str = "✓" if event["success"] else "■" if event["reason"] == "durduruldu" else "✗"
            self.stats_label.configure(text=status + " " + "\n".join(format_run_stats(event["metrics"])))

    # --- Çizim ---

    def _render_tool(self, view: ToolView) -> None:
        bullet_tag: str = {"streaming": "bullet_streaming", "running": "bullet_running",
                           "ok": "bullet_ok", "error": "bullet_error"}[view["status"]]
        parts: List[Tuple[str, Tuple[str, ...]]] = [("⏺ ", (bullet_tag,)), (tool_label(view["name"]), ("tool_name",))]
        is_command: bool = view["name"] in ("execute_shell", "execute_js")
        if not is_command and view["preview"]:
            parts.append((f"({_clip_line(view['preview'].splitlines()[0])})", ("tool_args",)))
        if view["status"] in ("ok", "error"):
            parts.append((f" · {view['seconds']:.1f}sn", ("meta",)))
        parts.append(("\n", ()))
        if is_command and view["preview"]:
            command_lines: List[str] = view["preview"].splitlines()
            shown: List[str] = command_lines[:COMMAND_LINES] + (["…"] if len(command_lines) > COMMAND_LINES else [])
            for index, command_line in enumerate(shown):
                prompt: str = ("$ " if view["name"] == "execute_shell" else "› ") if index == 0 else "  "
                parts.append((prompt, ("command_prompt",)))
                parts.append((_clip_line(command_line) + "\n", ("command",)))
        if view["status"] == "running":
            if view["tail"]:
                hidden: int = view["line_count"] - len(view["tail"])
                if hidden > 0:
                    parts.append(("⎿  ", ("gutter",)))
                    parts.append((f"… {hidden} satır daha\n", ("meta",)))
                for position, line in enumerate(view["tail"]):
                    parts.append(("⎿  ", ("gutter",) if position == 0 and hidden <= 0 else ("gutter_hidden",)))
                    parts.append((_clip_line(line) + "\n", ("output",)))
            else:
                elapsed: float = time.monotonic() - view["started_at"]
                parts.append(("⎿  ", ("gutter",)))
                parts.append((f"çalışıyor… {elapsed:.1f}sn\n", ("meta",)))
        elif view["status"] in ("ok", "error"):
            summary: List[str] = summarize_result(view["name"], view["result"], view["head"], view["line_count"], view["status"] == "ok")
            for position, line in enumerate(summary):
                parts.append(("⎿  ", ("gutter",) if position == 0 else ("gutter_hidden",)))
                parts.append((line + "\n", ("output" if view["status"] == "ok" else "output_error",)))
        self._replace_region(view["region"], parts)

    def _render_welcome(self) -> None:
        self._new_region([
            ("✻ ", ("welcome_mark",)), ("OmniAgent'a hoş geldin\n", ("welcome_title",)),
            (f"cwd: {Path.cwd()}\n", ("welcome_dim",)),
            ("Kabuk, dosya, web ve macOS arayüzü üzerinde görevleri senin yerine yürütür.\n", ("welcome_dim",)),
            ("Örnek: \"İndirilenler'deki en büyük 5 dosyayı listele\"\n", ("welcome_dim",)),
            ("\n", ("gap",)),
        ])

    def _render_goal(self, goal: str) -> None:
        self._new_region([("\n", ("gap",))])
        self._new_region([("› ", ("goal_prompt",)), (goal + "\n", ("goal",))])
        self._new_region([("\n", ("gap",))])

    def _render_summary(self, success: bool, reason: str, metrics: EpisodeMetrics) -> None:
        if success:
            head: Tuple[str, Tuple[str, ...]] = ("✓ Tamamlandı", ("summary_ok",))
        elif reason == "durduruldu":
            head = ("■ Durduruldu", ("summary_error",))
        else:
            head = (f"✗ Tamamlanamadı: {reason}", ("summary_error",))
        parts: List[Tuple[str, Tuple[str, ...]]] = [("\n", ("gap",)), (head[0] + "\n", head[1])]
        parts.extend((f"  {line}\n", ("summary_meta",)) for line in format_run_stats(metrics))
        self._new_region(parts)

    def _typewriter_step(self) -> bool:
        """Bekleyen akış metnini kare başına birkaç karakter ekleyerek gösterir (daktilo efekti)."""
        changed: bool = False
        for region in list(self._pending_text):
            pending: str = self._pending_text[region]
            if not pending:
                continue
            count: int = max(TYPEWRITER_MIN_CHARS, math.ceil(len(pending) / TYPEWRITER_DRAIN_FRAMES))
            self._append_streaming(region, pending[:count], self._region_text_tag[region])
            self._pending_text[region] = pending[count:]
            changed = True
            if not self._pending_text[region] and not self._live_regions.get(region):
                self._finish_streaming(region)
        return changed

    def _set_activity(self, glyph: str, verb: str, meta: str, shine: int) -> None:
        """Durum satırını kurar: spinner glifi, parıltılı fiil (3 karakterlik açık pencere) ve ölçümler."""
        self._activity.configure(state="normal")
        self._activity.delete("1.0", "end")
        self._activity.insert("end", glyph + " ", ("glyph",))
        for position, char in enumerate(verb + "…"):
            self._activity.insert("end", char, ("verb", "shine") if shine - 3 < position <= shine else ("verb",))
        self._activity.insert("end", "  " + meta, ("meta",))
        self._activity.configure(state="disabled")

    def _set_activity_idle(self) -> None:
        self._activity.configure(state="normal")
        self._activity.delete("1.0", "end")
        self._activity.insert("end", "✻ hazır", ("meta",))
        self._activity.configure(state="disabled")

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
        }
        label, color = labels[phase]
        self.task_status_label.configure(text=label, text_color=color)

    def _sync_menu_status(self) -> None:
        """Arka planda görev sürerken menü çubuğu göstergesini canlı tutar."""
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

    def _on_focus_return(self, _event: object = None) -> None:
        """Pencere yeniden görünür olunca tamamlanma rozetini temizler."""
        if self._badge_pending and self._agent_future is None:
            set_dock_badge(None)
            self._badge_pending = False
        self._menu_status.hide()

    def _animate(self, now: float) -> None:
        if now - self._last_menu_check >= 0.1:
            self._last_menu_check = now
            self._sync_menu_status()
        running: bool = self._agent_future is not None and not self._agent_future.done()
        if running and now - self._last_spinner >= SPINNER_INTERVAL:
            self._last_spinner = now
            self._spinner_index = (self._spinner_index + 1) % len(SPINNER_FRAMES)
            if self._task_status == "running":
                self._set_task_status("running", now - self._run_started_at)
        if running and now - self._last_shimmer >= SHIMMER_INTERVAL:
            self._last_shimmer = now
            self._shine_index = (self._shine_index + 1) % (len(self._activity_verb) + 8)
            tokens: int = self._completed_tokens + self._turn_streamed_chars // 4
            meta: str = f"({now - self._run_started_at:.0f}sn · ↓ {compact_count(tokens)} token · esc ile durdur)"
            self._set_activity(SPINNER_FRAMES[self._spinner_index], self._activity_verb, meta, self._shine_index)
        if (running or any(self._pending_text.values())) and now - self._last_blink >= BLINK_INTERVAL:
            self._last_blink = now
            self._blink_on = not self._blink_on
            self._text.tag_configure("bullet_running", foreground=ACCENT if self._blink_on else ACCENT_DIM)
            self._text.tag_configure("bullet_streaming", foreground=TEXT_DIM if self._blink_on else TEXT_FAINT)
            self._text.tag_configure("cursor", foreground=ACCENT if self._blink_on else BG)

    def _tick(self) -> None:
        """~60 fps kare döngüsü: olayları işle, daktiloyu ilerlet, kirli blokları çiz, animasyonları oynat."""
        now: float = time.monotonic()
        running: bool = self._agent_future is not None and not self._agent_future.done()
        refresh_due: bool = (
            running and now - self._last_running_refresh >= RUNNING_REFRESH_INTERVAL
            and any(view["status"] == "running" and not view["tail"] for view in self._tools_by_call.values())
        )
        if (
            self._inbox.empty() and self._voice_queue.empty()
            and not any(self._pending_text.values()) and not self._dirty_tools
            and not refresh_due
        ):
            self._animate(now)
            self.after(RUNNING_IDLE_FRAME_MS if running else IDLE_FRAME_MS, self._tick)
            return
        at_bottom: bool = self._text.yview()[1] >= 0.995
        self._text.configure(state="normal")
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
        #Queue'dan alınır. Böylece PyObjC callback'i UI'yi yarıda bırakamaz.
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
        changed: bool = self._typewriter_step() or processed > 0
        if running and now - self._last_running_refresh >= RUNNING_REFRESH_INTERVAL:
            self._last_running_refresh = now
            for view in self._tools_by_call.values():
                if view["status"] == "running" and not view["tail"]:
                    self._mark_dirty(view)
        for view in self._dirty_tools.values():
            self._render_tool(view)
            changed = True
        self._dirty_tools = {}
        self._text.configure(state="disabled")
        if changed and at_bottom:
            self._text.see("end")
        self._animate(now)
        delay: int = FRAME_MS if any(self._pending_text.values()) else (
            RUNNING_IDLE_FRAME_MS if running else IDLE_FRAME_MS
        )
        self.after(delay, self._tick)

    # --- Ajan tetikleme ---

    def _model_text(self, backend: str) -> str:
        return f"{BACKENDS[backend]['model']} · {backend}" if backend in BACKENDS else backend

    def _on_primary_button(self) -> None:
        if self._agent_future is not None and not self._agent_future.done():
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
        transcript: str = self._text.get("1.0", "end-1c")
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
            self._text.configure(state="normal")
            self._new_region([(f"🎙 {text}\n", ("notice_warning",))])
            self._text.configure(state="disabled")
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
            for window in list(self._input_windows.values()):
                window.destroy()
            self._input_windows.clear()

    def _send_goal(self) -> None:
        """Giriş alanındaki hedefi transkripte ekler ve ajanı kalıcı event loop'ta başlatır."""
        if self._agent_future is not None:
            return
        goal: str = self.entry.get().strip()
        if not goal:
            return
        if self._voice.active:
            self._voice.cancel()
            # Eski oturumun idle callback'i yeni görev composer'ına yazmasın.
            self._voice_base_text = ""
            self._voice_partial_text = ""
        if goal.casefold() in {"/tools", "/yetenekler", "/skills"}:
            self.entry.delete(0, "end")
            self._text.configure(state="normal")
            self._render_goal(goal)
            try:
                self._integrations.refresh_local()
                inventory = format_capability_inventory(
                    self._integrations.entries, show_skills=goal.casefold() == "/skills"
                )
            except Exception as error:
                inventory = f"Yetenek kataloğu okunamadı: {type(error).__name__}: {error}"
            self._new_region([(inventory + "\n", ("notice_info",))])
            self._text.configure(state="disabled")
            self._text.see("end")
            return
        self._active_goal = goal
        self.entry.delete(0, "end")
        self._text.configure(state="normal")
        self._render_goal(goal)
        self._text.configure(state="disabled")
        self._text.see("end")
        self._turn = None
        self._tools_by_call = {}
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
        self._agent_future = asyncio.run_coroutine_threadsafe(
            self._run_exclusive(goal, options), self._loop,
        )
        self._agent_future.add_done_callback(self._on_agent_future_done)

    async def _run_exclusive(self, goal: str, options: RunOptions) -> RunReport:
        """Telegram ile aynı makineyi eşzamanlı kullanma çakışmasını önler."""
        with host_task_lock():
            return await run_agent_with_callback(goal, self._post, options, self._clients)

    async def _request_input(self, title: str, fields: Dict[str, object]) -> Dict[str, object]:
        """Model çağırmadan Tk arayüzünden cevap bekler."""
        request_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self._input_futures[request_id] = future
        self._post({"kind": "user_input_required", "request_id": request_id, "title": title, "fields": fields})
        try:
            return await future
        finally:
            self._input_futures.pop(request_id, None)

    def _show_input(self, request_id: str, title: str, fields: Dict[str, object]) -> None:
        """Alanları Tk thread'inde gösterir; cevap yalnız ilgili Future'a teslim edilir."""
        if self._stop_event.is_set():
            return
        window = ctk.CTkToplevel(self)
        self._input_windows[request_id] = window
        window.title("OmniAgent — Yanıt gerekiyor")
        window.geometry("660x640")
        window.transient(self)
        panel = ctk.CTkScrollableFrame(window, fg_color=BG)
        panel.pack(fill="both", expand=True, padx=12, pady=12)
        ctk.CTkLabel(panel, text=title, wraplength=590, justify="left").pack(anchor="w", pady=8)
        if fields.get("_help"):
            ctk.CTkLabel(panel, text=str(fields["_help"]), wraplength=590, justify="left").pack(anchor="w", pady=8)
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
                widget = ctk.CTkEntry(panel, width=590)
                widget.insert(0, str(spec.get("default", "")))
                widget.pack(anchor="w", pady=(0, 6))
                widgets[name] = widget

        def submit() -> None:
            values = {name: widget.get() for name, widget in widgets.items()}
            def deliver() -> None:
                future = self._input_futures.get(request_id)
                if future is not None and not future.done():
                    future.set_result(values)
            self._loop.call_soon_threadsafe(deliver)
            self._input_windows.pop(request_id, None)
            window.destroy()

        ctk.CTkButton(panel, text="Kaydet ve devam et", command=submit).pack(pady=12)
        window.protocol("WM_DELETE_WINDOW", self._request_stop)
        window.lift()

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
        if self._clients_stale:
            # Görev sürerken kaydedilen anahtarlar biter bitmez uygulanır.
            self._rebuild_clients()
        for window in list(self._input_windows.values()):
            window.destroy()
        self._input_windows.clear()
        if error:
            self._new_region([(f"⚠ Kritik hata: {error}\n", ("notice_error",))])
        self._end_live_regions()
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

    def _clear_transcript(self) -> None:
        if self._agent_future is not None:
            return
        self._text.configure(state="normal")
        self._text.delete("1.0", "end")
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
        self._render_welcome()
        self._text.configure(state="disabled")
        self.stats_label.configure(text="")

    def _on_close(self) -> None:
        """Pencere kapanırken ses/istemci kaynaklarını kapatıp event loop'u durdurur."""
        self._voice.cancel()
        self._stop_event.set()
        set_dock_badge(None)
        self._menu_status.hide()
        async def close_connections() -> None:
            if self._agent_future is not None and not self._agent_future.done():
                try:
                    await asyncio.wait_for(asyncio.wrap_future(self._agent_future), timeout=3)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
            await self._integrations.close()
            await close_model_clients(self._clients)
        try:
            asyncio.run_coroutine_threadsafe(close_connections(), self._loop).result(timeout=8)
        except Exception as error:
            # Kapanış hiçbir durumda pencereyi kapatmayı engellemez.
            logging.warning("Kapanışta bağlantılar kapatılamadı", extra={"error_type": type(error).__name__})
        self._loop.call_soon_threadsafe(self._loop.stop)
        self.destroy()


if __name__ == "__main__":
    # Finder'dan başlatıldığında ortam değişkenleri miras alınmaz: kayıtlı anahtarları uygula.
    apply_stored_api_keys()
    app = OmniUI()
    app.mainloop()
