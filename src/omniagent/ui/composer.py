"""
Composer giriş alanı: çok satırlı, yer tutuculu, içerik kadar büyüyen metin kutusu.

Uygulama kodu alanı Entry gibi kullanır (get, delete(0, "end"), insert(0, metin), configure(state=...),
focus_set); ses girişi ve testler bu küçük API'ye dayanır. Return gönderir, Shift+Return yeni satır ekler.
Yükseklik görünen satır sayısına göre COMPOSER_MIN_LINES..COMPOSER_MAX_LINES arasında büyür ve küçülür.
"""
from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from typing import Callable, Optional, Union

from omniagent.ui.rendering import composer_lines
from omniagent.ui.theme import ACCENT_DIM, COMPOSER_MIN_LINES, SURFACE, TEXT, TEXT_FAINT

# Entry uyumlu dizinler: 0 başlangıç, "end" son.
EntryIndex = Union[int, str]
_ENTRY_INDEXES: dict[EntryIndex, str] = {0: "1.0", "end": "end-1c"}


def text_index(index: EntryIndex) -> str:
    """Entry biçimli dizini (0 ya da "end") Text dizinine çevirir; başka dizin açıkça reddedilir. Saf."""
    if index not in _ENTRY_INDEXES:
        raise ValueError(f"Composer yalnız 0 ve 'end' dizinlerini destekler: {index!r}")
    return _ENTRY_INDEXES[index]


class ComposerInput:
    """
    tk.Text tabanlı çok satırlı giriş alanı. `widget` yerleşim için dışarıya açıktır; içerik
    okuma/yazma ve durum değişimi Entry benzeri yöntemlerle yapılır. Boşken yer tutucu görünür.
    """

    def __init__(self, master: tk.Misc, font: tkfont.Font, placeholder: str,
                 on_submit: Callable[[], None], on_focus_change: Callable[[bool], None]) -> None:
        self._on_submit: Callable[[], None] = on_submit
        self._on_focus_change: Callable[[bool], None] = on_focus_change
        self._idle_id: Optional[str] = None
        self.widget: tk.Text = tk.Text(
            master, height=COMPOSER_MIN_LINES, width=1, wrap="word", font=font, bg=SURFACE, fg=TEXT,
            bd=0, highlightthickness=0, padx=0, pady=0, spacing1=0, spacing2=0, spacing3=0,
            insertbackground=TEXT, insertwidth=1, selectbackground=ACCENT_DIM, selectforeground=TEXT,
            undo=True, autoseparators=True, maxundo=200,
        )
        self._placeholder: tk.Label = tk.Label(
            self.widget, text=placeholder, bg=SURFACE, fg=TEXT_FAINT, font=font, bd=0,
            highlightthickness=0, padx=0, pady=0, anchor="w", cursor="xterm",
        )
        self._placeholder.place(x=0, y=0)
        self._placeholder.bind("<Button-1>", lambda _event: self._focus_start())
        self.widget.bind("<Return>", self._submit)
        self.widget.bind("<KP_Enter>", self._submit)
        self.widget.bind("<Shift-Return>", self._newline)
        self.widget.bind("<Tab>", self._next_focus)
        self.widget.bind("<FocusIn>", lambda _event: self._on_focus_change(True), add="+")
        self.widget.bind("<FocusOut>", lambda _event: self._on_focus_change(False), add="+")
        self.widget.bind("<<Modified>>", self._modified)
        self.widget.bind("<Configure>", lambda _event: self._schedule_sync(), add="+")
        self.widget.bind("<Destroy>", self._cancel_sync, add="+")

    # --- Entry benzeri API ---

    def get(self) -> str:
        """Tüm içeriği döndürür (Text'in sondaki zorunlu satır sonu olmadan)."""
        return self.widget.get("1.0", "end-1c")

    def delete(self, start: EntryIndex, end: EntryIndex) -> None:
        """Yalnız tüm içeriği silmeyi destekler: delete(0, "end")."""
        self.widget.delete(text_index(start), text_index(end))
        self._after_programmatic_change()

    def insert(self, index: EntryIndex, value: str) -> None:
        """Metni başa (0) ya da sona ("end") ekler."""
        self.widget.insert(text_index(index), value)
        self._after_programmatic_change()

    def configure(self, state: str) -> None:
        """Alanı düzenlenebilir ('normal') ya da kilitli ('disabled') yapar; ses girişi sırasında kilitlenir."""
        self.widget.configure(state=state)

    def focus_set(self) -> None:
        self.widget.focus_set()

    def visible_lines(self) -> int:
        """Alanın şu an gösterdiği satır sayısı."""
        return int(self.widget.cget("height"))

    # --- Yer tutucu ve yükseklik ---

    def _after_programmatic_change(self) -> None:
        """Kod ile yazılan içerik geri alma yığınına girmez; yer tutucu ve yükseklik hemen güncellenir."""
        self.widget.edit_reset()
        self._sync()

    def _schedule_sync(self) -> None:
        """Yerleşim hesabı boşta çalışır: art arda değişiklikler tek hesapta birleşir."""
        if self._idle_id is None:
            self._idle_id = self.widget.after_idle(self._run_scheduled_sync)

    def _run_scheduled_sync(self) -> None:
        self._idle_id = None
        self._sync()

    def _cancel_sync(self, _event: "tk.Event[tk.Misc]") -> None:
        if self._idle_id is not None:
            self.widget.after_cancel(self._idle_id)
            self._idle_id = None

    def _sync(self) -> None:
        """Yer tutucuyu içerik boşken gösterir; yüksekliği görünen (sarılmış) satır sayısına uydurur."""
        if self.get():
            self._placeholder.place_forget()
        else:
            self._placeholder.place(x=0, y=0)
        # `count -displaylines` iki dizin arasındaki satır SINIRLARINI sayar: n görünen satır için n - 1 döner.
        crossings: object = self.widget.tk.call(str(self.widget), "count", "-update", "-displaylines", "1.0", "end-1c")
        wanted: int = composer_lines((int(crossings) if crossings else 0) + 1)
        if wanted != self.visible_lines():
            self.widget.configure(height=wanted)

    # --- Olay işleyicileri ---

    def _modified(self, _event: "tk.Event[tk.Misc]") -> None:
        """Değişiklik bayrağı her düzenlemede yeniden kurulur; bayrak sıfırlanırken gelen ikinci olay yok sayılır."""
        if self.widget.edit_modified():
            self.widget.edit_modified(False)
            self._schedule_sync()

    def _submit(self, _event: "tk.Event[tk.Misc]") -> str:
        self._on_submit()
        return "break"

    def _newline(self, _event: "tk.Event[tk.Misc]") -> str:
        self.widget.insert("insert", "\n")
        self._schedule_sync()
        return "break"

    def _next_focus(self, _event: "tk.Event[tk.Misc]") -> str:
        """Tab boşluk eklemez; odağı bir sonraki denetime taşır."""
        self.widget.tk_focusNext().focus_set()
        return "break"

    def _focus_start(self) -> None:
        self.widget.focus_set()
        self.widget.mark_set("insert", "1.0")
