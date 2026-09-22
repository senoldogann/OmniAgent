import asyncio
import re
import threading
from queue import Empty, Queue
from typing import Optional, Tuple

import customtkinter as ctk

from main import RunOptions, run_agent_with_callback

BG: str = "#0f0f1a"
PANEL: str = "#171728"
LOG_BG: str = "#12121f"
BORDER: str = "#2e2e4d"
ACCENT: str = "#7c7cf0"
ACCENT_HOVER: str = "#9494f5"
TEXT_PRIMARY: str = "#eef0fb"
TEXT_MUTED: str = "#7a7a9c"
SUCCESS: str = "#4ade80"
ERROR: str = "#f87171"
WARNING: str = "#fbbf24"
INFO: str = "#60a5fa"
GOAL_COLOR: str = "#c4b5fd"

BACKEND_CHOICES: Tuple[str, ...] = ("Otomatik", "opencode", "claude", "openai")

STATUS_STYLE: dict[str, Tuple[str, str]] = {
    "idle": (SUCCESS, "Hazır"),
    "thinking": (WARNING, "Düşünüyor"),
    "running": (INFO, "Araç çalıştırılıyor"),
    "error": (ERROR, "Hata"),
    "stopped": (TEXT_MUTED, "Durduruldu"),
}

_THINKING_RE = re.compile(r"^\[(\d+)/(\d+)\] Model düşünüyor\.\.\. \(backend: (\w+)\)$")
_BACKEND_SWITCH_RE = re.compile(r"'(\w+)' backend'ine yükselti")


def _classify_line(line: str) -> str:
    """Bir log satırının hangi renk etiketiyle gösterileceğine karar verir."""
    if line.startswith("🌟 HEDEF"):
        return "goal"
    if line.startswith("Kullanılabilir modeller"):
        return "muted"
    if _THINKING_RE.match(line):
        return "thinking"
    if line.startswith("Araç çağrısı:"):
        return "tool_call"
    if "-> Başarılı" in line:
        return "success"
    if "-> Hata" in line or line.startswith("❌") or line.startswith("Kritik hata"):
        return "error"
    if "yükselti" in line or line.startswith("⚠️") or line.startswith("Uyarı:"):
        return "warning"
    if line.startswith("Tamamlandı:"):
        return "final"
    if line.startswith("Zaman bütçesi") or line.startswith("Maksimum iterasyon") or "durduruldu" in line:
        return "warning"
    return "muted"


class OmniUI(ctk.CTk):
    """
    OmniAgent için 'Glass' görünümlü, durum ve backend göstergeli kullanıcı arayüzü.
    """
    def __init__(self) -> None:
        super().__init__()

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.title("OmniAgent")
        self.geometry("640x820")
        self.minsize(520, 600)
        self.configure(fg_color=BG)
        self.attributes("-alpha", 0.97)
        # Açılışta öne gelsin (script/terminalden başlatılınca odak almayabiliyor),
        # ama sonrasında diğer pencerelerin önünde kilitli kalmasın.
        self.lift()
        self.attributes("-topmost", True)
        self.after(400, lambda: self.attributes("-topmost", False))
        self.focus_force()

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self._build_header()
        self._build_log_area()
        self._build_control_bar()

        self.queue: Queue = Queue()
        self.agent_thread: Optional[threading.Thread] = None
        self._stop_event: threading.Event = threading.Event()
        self.after(50, self._drain_queue)

    # --- Yerleşim ---

    def _build_header(self) -> None:
        self.header: ctk.CTkFrame = ctk.CTkFrame(
            self, fg_color=PANEL, corner_radius=16, border_width=1, border_color=BORDER,
        )
        self.header.grid(row=0, column=0, padx=16, pady=(16, 8), sticky="ew")
        self.header.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            self.header, text="⚡ OmniAgent",
            font=ctk.CTkFont(family="SF Pro Display", size=20, weight="bold"),
            text_color=TEXT_PRIMARY,
        ).grid(row=0, column=0, padx=(16, 8), pady=(12, 0), sticky="w")

        self.status_dot: ctk.CTkLabel = ctk.CTkLabel(
            self.header, text="●", font=ctk.CTkFont(size=15), text_color=SUCCESS, width=16,
        )
        self.status_dot.grid(row=0, column=2, padx=(0, 4), pady=(12, 0), sticky="e")
        self.status_label: ctk.CTkLabel = ctk.CTkLabel(
            self.header, text="Hazır", font=ctk.CTkFont(family="SF Pro Display", size=13),
            text_color=TEXT_MUTED,
        )
        self.status_label.grid(row=0, column=3, padx=(0, 12), pady=(12, 0), sticky="e")

        self.backend_badge: ctk.CTkLabel = ctk.CTkLabel(
            self.header, text="opencode", font=ctk.CTkFont(family="SF Mono", size=11, weight="bold"),
            text_color=ACCENT, fg_color=BG, corner_radius=8,
        )
        self.backend_badge.grid(row=1, column=0, padx=(16, 8), pady=(6, 12), sticky="w")

        self.clear_btn: ctk.CTkButton = ctk.CTkButton(
            self.header, text="Temizle", width=70, height=22, corner_radius=10,
            fg_color="transparent", hover_color=BG, border_width=1, border_color=BORDER,
            text_color=TEXT_MUTED, font=ctk.CTkFont(size=11), command=self._clear_log,
        )
        self.clear_btn.grid(row=1, column=3, padx=(0, 16), pady=(6, 12), sticky="e")

    def _build_log_area(self) -> None:
        self.log_area: ctk.CTkTextbox = ctk.CTkTextbox(
            self, fg_color=LOG_BG, text_color=TEXT_PRIMARY,
            font=ctk.CTkFont(family="SF Mono", size=12),
            corner_radius=16, border_width=1, border_color=BORDER, wrap="word",
        )
        self.log_area.grid(row=1, column=0, padx=16, pady=8, sticky="nsew")
        self.log_area.configure(state="disabled")
        for tag, color in (
            ("goal", GOAL_COLOR), ("thinking", TEXT_MUTED), ("tool_call", INFO),
            ("success", SUCCESS), ("error", ERROR), ("warning", WARNING),
            ("final", TEXT_PRIMARY), ("muted", TEXT_MUTED),
        ):
            self.log_area.tag_config(tag, foreground=color)

    def _build_control_bar(self) -> None:
        self.control_frame: ctk.CTkFrame = ctk.CTkFrame(self, fg_color="transparent")
        self.control_frame.grid(row=2, column=0, padx=16, pady=(8, 16), sticky="ew")
        self.control_frame.grid_columnconfigure(1, weight=1)

        self.backend_menu: ctk.CTkOptionMenu = ctk.CTkOptionMenu(
            self.control_frame, values=list(BACKEND_CHOICES), width=112,
            fg_color=PANEL, button_color=ACCENT, button_hover_color=ACCENT_HOVER,
            dropdown_fg_color=PANEL, text_color=TEXT_PRIMARY, font=ctk.CTkFont(size=12),
        )
        self.backend_menu.set("Otomatik")
        self.backend_menu.grid(row=0, column=0, padx=(0, 8), sticky="w")

        self.entry: ctk.CTkEntry = ctk.CTkEntry(
            self.control_frame, placeholder_text="Hedefinizi girin...", height=45, corner_radius=20,
            border_width=1, border_color=BORDER, fg_color=PANEL, text_color=TEXT_PRIMARY,
            font=ctk.CTkFont(size=13),
        )
        self.entry.grid(row=0, column=1, padx=(0, 8), sticky="ew")
        self.entry.bind("<Return>", lambda event: self._on_primary_button())

        self.primary_btn: ctk.CTkButton = ctk.CTkButton(
            self.control_frame, text="🚀", width=45, height=45, corner_radius=20,
            command=self._on_primary_button, fg_color=ACCENT, hover_color=ACCENT_HOVER,
            font=ctk.CTkFont(size=16),
        )
        self.primary_btn.grid(row=0, column=2)

    # --- Log / durum ---

    def log(self, message: str) -> None:
        """Kullanıcı arayüzüne thread-safe şekilde log yazar."""
        self.queue.put(("log", message))

    def _append_log(self, message: str, tag: str) -> None:
        self.log_area.configure(state="normal")
        self.log_area.insert("end", message + "\n", (tag,))
        self.log_area.see("end")
        self.log_area.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log_area.configure(state="normal")
        self.log_area.delete("1.0", "end")
        self.log_area.configure(state="disabled")

    def _set_status(self, key: str) -> None:
        color, text = STATUS_STYLE[key]
        self.status_dot.configure(text_color=color)
        self.status_label.configure(text=text)

    def _update_status_from_line(self, line: str) -> None:
        thinking_match = _THINKING_RE.match(line)
        if thinking_match:
            self._set_status("thinking")
            self.backend_badge.configure(text=thinking_match.group(3))
            return
        if line.startswith("Araç çağrısı:"):
            self._set_status("running")
            return
        if line.startswith("Tamamlandı:") or line.startswith("Zaman bütçesi") or line.startswith("Maksimum iterasyon"):
            self._set_status("idle")
            return
        if line.startswith("Kritik hata") or line.startswith("❌"):
            self._set_status("error")
            return
        if "durduruldu" in line:
            self._set_status("stopped")
            return
        backend_match = _BACKEND_SWITCH_RE.search(line)
        if backend_match:
            self.backend_badge.configure(text=backend_match.group(1))

    def _drain_queue(self) -> None:
        """Arka plan olaylarını ana iş parçacığında işler."""
        while True:
            try:
                event, payload = self.queue.get_nowait()
            except Empty:
                break
            if event == "log" and payload is not None:
                self._update_status_from_line(payload)
                self._append_log(payload, _classify_line(payload))
            elif event == "done":
                self.agent_thread = None
                self.entry.configure(state="normal")
                self.backend_menu.configure(state="normal")
                self.primary_btn.configure(text="🚀", state="normal")
        self.after(50, self._drain_queue)

    # --- Ajan tetikleme ---

    def _on_primary_button(self) -> None:
        if self.agent_thread is not None and self.agent_thread.is_alive():
            self._stop_event.set()
            self.primary_btn.configure(state="disabled")
            self._append_log("⏹ Durdurma istendi, mevcut adım bitince duracak...", "warning")
            return
        self._send_goal()

    def _send_goal(self) -> None:
        """Giriş alanındaki hedefi alır ve ajanı başlatır."""
        goal: str = self.entry.get().strip()
        if not goal:
            return
        self._append_log(f"\n🌟 HEDEF: {goal}\n{'─' * 40}", "goal")
        self.entry.delete(0, "end")
        self.entry.configure(state="disabled")
        self.backend_menu.configure(state="disabled")
        self.primary_btn.configure(text="⏹")
        self._set_status("thinking")

        selected: str = self.backend_menu.get()
        requested_backend: Optional[str] = None if selected == "Otomatik" else selected
        self._stop_event = threading.Event()

        self.agent_thread = threading.Thread(
            target=self._run_agent_thread, args=(goal, requested_backend), daemon=True,
        )
        self.agent_thread.start()

    def _run_agent_thread(self, goal: str, requested_backend: Optional[str]) -> None:
        """Async ajanı bir thread içinde çalıştıran wrapper."""
        try:
            asyncio.run(self._run_agent_async(goal, requested_backend))
        except Exception as error:
            self.log(f"❌ Kritik Hata: {error}")
        finally:
            self.queue.put(("done", None))

    async def _run_agent_async(self, goal: str, requested_backend: Optional[str]) -> None:
        options: RunOptions = {"requested_backend": requested_backend, "should_stop": self._stop_event.is_set}
        await run_agent_with_callback(goal, callback=self.log, options=options)


if __name__ == "__main__":
    app = OmniUI()
    app.mainloop()
