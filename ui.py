import asyncio
import threading
from queue import Empty, Queue
from typing import Optional

import customtkinter as ctk

from main import run_agent_with_callback

class OmniUI(ctk.CTk):
    """
    OmniAgent için modern, 'Glass' görünümlü kullanıcı arayüzü.
    """
    def __init__(self) -> None:
        super().__init__()

        # Pencere Ayarları
        self.title("OmniAgent v1.0")
        self.geometry("450x600")
        self.attributes("-topmost", True)
        self.attributes("-alpha", 0.95) # Yarı saydam cam görünümü
        
        # Tema ve Stil
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        # Yerleşim (Layout)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # 1. Akış Log Alanı (Cam Panel)
        self.log_area: ctk.CTkTextbox = ctk.CTkTextbox(
            self, 
            fg_color="#1a1a2e", 
            text_color="#e0e0e0", 
            font=("SF Pro Display", 13),
            corner_radius=15,
            border_width=2,
            border_color="#3a3a5a"
        )
        self.log_area.grid(row=0, column=0, padx=20, pady=(20, 10), sticky="nsew")
        self.log_area.configure(state="disabled")

        # 2. Giriş Alanı
        self.input_frame: ctk.CTkFrame = ctk.CTkFrame(self, fg_color="transparent")
        self.input_frame.grid(row=1, column=0, padx=20, pady=(0, 20), sticky="ew")
        self.input_frame.grid_columnconfigure(0, weight=1)

        self.entry: ctk.CTkEntry = ctk.CTkEntry(
            self.input_frame, 
            placeholder_text="Hedefinizi girin...",
            height=45,
            corner_radius=20,
            border_width=1,
            border_color="#5a5a8a",
            fg_color="#252545",
            text_color="white"
        )
        self.entry.grid(row=0, column=0, padx=(0, 10), sticky="ew")
        self.entry.bind("<Return>", lambda e: self.send_goal())

        self.send_btn: ctk.CTkButton = ctk.CTkButton(
            self.input_frame, 
            text="🚀", 
            width=45, 
            height=45, 
            corner_radius=20,
            command=self.send_goal,
            fg_color="#4a4ae2",
            hover_color="#6a6ae8"
        )
        self.send_btn.grid(row=0, column=1)

        # Ajan Entegrasyonu
        self.queue: Queue[tuple[str, str | None]] = Queue()
        self.agent_thread: Optional[threading.Thread] = None
        self.after(50, self._drain_queue)

    def log(self, message: str) -> None:
        """
        Kullanıcı arayüzüne thread-safe şekilde log yazar.
        """
        self.queue.put(("log", message))

    def _append_log(self, message: str) -> None:
        """Ana iş parçacığında log alanını günceller."""
        self.log_area.configure(state="normal")
        self.log_area.insert("end", message + "\n")
        self.log_area.see("end")
        self.log_area.configure(state="disabled")

    def _drain_queue(self) -> None:
        """Arka plan olaylarını ana iş parçacığında işler."""
        while True:
            try:
                event, message = self.queue.get_nowait()
            except Empty:
                break
            if event == "log" and message is not None:
                self._append_log(message)
            elif event == "done":
                self.agent_thread = None
                self.entry.configure(state="normal")
                self.send_btn.configure(text="🚀", state="normal")
        self.after(50, self._drain_queue)

    def send_goal(self) -> None:
        """
        Giriş alanındaki hedefi alır ve ajanı başlatır.
        """
        if self.agent_thread is not None and self.agent_thread.is_alive():
            self._append_log("⚠️ Bir ajan zaten çalışıyor. Lütfen bekleyin.")
            return
        goal: str = self.entry.get().strip()
        if not goal:
            return
        self._append_log(f"\n🌟 HEDEF: {goal}\n" + "-" * 30)
        self.entry.delete(0, "end")

        self.entry.configure(state="disabled")
        self.send_btn.configure(text="⏳", state="disabled")
        self.agent_thread = threading.Thread(
            target=self.start_agent_loop, 
            args=(goal,), 
            daemon=True
        )
        self.agent_thread.start()

    def start_agent_loop(self, goal: str) -> None:
        """
        Async ajanı bir thread içinde çalıştıran wrapper.
        """
        try:
            asyncio.run(self.run_agent_with_logging(goal))
        except Exception as e:
            self.log(f"❌ Kritik Hata: {e}")
        finally:
            self.queue.put(("done", None))

    async def run_agent_with_logging(self, goal: str) -> None:
        """
        Ajanı çalıştırır ve çıktıları UI log alanına yönlendirir.
        """
        await run_agent_with_callback(goal, callback=self.log)

if __name__ == "__main__":
    app = OmniUI()
    app.mainloop()
