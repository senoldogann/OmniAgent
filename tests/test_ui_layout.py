"""Faz 2 arayüz düzeni: tarihe göre gruplu kenar çubuğu ve durum noktaları, sütuna hizalı üst şerit/composer, çok satırlı giriş."""
from __future__ import annotations

import time
import tkinter as tk
import uuid
from concurrent.futures import Future
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, List, Optional

import pytest

from omniagent.ui import app as ui
from omniagent.ui import chats, composer, rendering, theme
from tests.test_ui_conversation import app, pump  # noqa: F401  (gerçek Tk pencere fixture'ı yeniden kullanılır)

NOW = datetime(2026, 9, 29, 10, 0, tzinfo=timezone(timedelta(hours=3)))


def _summary(title: str, stamp: datetime, outcome: Optional[str]) -> chats.ChatSummary:
    summary: chats.ChatSummary = {
        "id": uuid.uuid4().hex, "title": title, "updated_at": stamp.astimezone(timezone.utc).isoformat(),
    }
    if outcome is not None:
        summary["last_outcome"] = outcome
    return summary


# --- Saf dönüşümler ---

@pytest.mark.parametrize("stamp,expected", [
    ("2026-09-29T09:59:00+03:00", "Bugün"),
    ("2026-09-28T21:30:00+00:00", "Bugün"),  # yerelde (+03:00) 29 Eylül 00:30
    ("2026-09-28T20:59:00+00:00", "Dün"),  # yerelde 28 Eylül 23:59
    ("2026-09-27T12:00:00+03:00", "Önceki 7 gün"),
    ("2026-09-22T12:00:00+03:00", "Önceki 7 gün"),  # tam 7 gün önce
    ("2026-09-21T12:00:00+03:00", "Daha eski"),  # 8 gün önce
    ("2026-09-30T12:00:00+03:00", "Bugün"),  # gelecek tarih (saat sapması) Bugün sayılır
])
def test_chat_group_title_uses_local_calendar_days(stamp: str, expected: str) -> None:
    assert rendering.chat_group_title(datetime.fromisoformat(stamp), NOW) == expected


def test_group_chats_by_date_has_fixed_order_skips_empty_groups_and_keeps_input_order() -> None:
    older = _summary("eski", NOW - timedelta(days=30), None)
    today_b = _summary("bugün ikinci", NOW - timedelta(minutes=5), "failed")
    yesterday = _summary("dün", NOW - timedelta(days=1), "done")
    today_a = _summary("bugün birinci", NOW - timedelta(minutes=1), None)
    groups = rendering.group_chats_by_date([older, today_b, yesterday, today_a], NOW)
    assert [group["title"] for group in groups] == ["Bugün", "Dün", "Daha eski"]  # "Önceki 7 gün" boş: atlanır
    assert [chat["title"] for chat in groups[0]["chats"]] == ["bugün ikinci", "bugün birinci"]  # girdi sırası korunur
    assert rendering.group_chats_by_date([], NOW) == []


@pytest.mark.parametrize("outcome,running,expected", [
    ("failed", False, "failed"), ("failed", True, "running"), ("done", False, None),
    ("stopped", False, None), (None, False, None), (None, True, "running"),
])
def test_chat_dot_shows_only_what_is_actually_known(outcome: Optional[str], running: bool, expected: Optional[str]) -> None:
    assert rendering.chat_dot(outcome, running) == expected


def test_chat_row_look_clips_titles_and_swaps_the_dot_for_checkboxes_in_select_mode() -> None:
    long_title = "Çok uzun bir sohbet başlığı burada devam ediyor ve bitmiyor"
    look = rendering.chat_row_look(long_title, True, False, False, "running")
    assert look == {"label": "Çok uzun bir sohbet başlı…", "marker": "●", "tone": "running",
                    "highlighted": True, "bright": True}
    selecting = rendering.chat_row_look("Kısa", False, True, True, "failed")  # seçim modunda durum noktası gösterilmez
    assert (selecting["marker"], selecting["tone"], selecting["highlighted"]) == ("☑", "checked", True)
    assert rendering.chat_row_look("Kısa", False, False, True, None)["marker"] == "☐"
    quiet = rendering.chat_row_look("Kısa", False, False, False, None)
    assert (quiet["marker"], quiet["highlighted"], quiet["bright"]) == ("", False, False)


@pytest.mark.parametrize("widget,container,expected", [
    (".a.b", ".a.b", True),  # kapsayıcının kendisi
    (".a.b.c.d", ".a.b", True),  # iç içe çocuk
    (".a.bc", ".a.b", False),  # yalnız ad öneki eşleşir, çocuk değildir
    (".a", ".a.b", False),  # üst pencere
    (".x.y", ".a.b", False),  # ilgisiz pencere
])
def test_path_within_matches_only_the_container_and_its_descendants(widget: str, container: str, expected: bool) -> None:
    assert rendering.path_within(widget, container) is expected


def test_input_deadline_and_composer_lines_tables() -> None:
    requested = datetime(2026, 9, 29, 14, 17)
    assert rendering.format_input_deadline(900.0, requested) == "Yanıt süresi 15 dk · son saat 14:32 · süre dolarsa işlem yapılmaz."
    assert rendering.format_input_deadline(30.0, requested).startswith("Yanıt süresi 30 sn · son saat 14:17")
    assert [rendering.composer_lines(count) for count in (0, 1, 2, 5, 10, 99)] == [2, 2, 2, 5, 10, 10]


# --- Kenar çubuğu (gerçek Tk) ---

def _seed_sidebar(window: ui.OmniUI) -> List[chats.ChatSummary]:
    now = datetime.now().astimezone()
    noon = now.replace(hour=12, minute=0, second=0, microsecond=0)
    index = [
        _summary("bugün başarısız", now, "failed"),
        _summary("bugün başarılı", now, "done"),
        _summary("dün", noon - timedelta(days=1), None),
        _summary("geçen hafta", noon - timedelta(days=4), "stopped"),
        _summary("çok eski başarısız", noon - timedelta(days=30), "failed"),
    ]
    window._chat_index = index
    window._refresh_chat_list()
    return index


def _grid_row(widget: tk.Misc) -> int:
    return int(widget.grid_info()["row"])


def test_sidebar_groups_chats_by_date_with_small_headers(app: ui.OmniUI) -> None:
    index = _seed_sidebar(app)
    headers = app._chat_group_labels
    assert sorted(headers, key=lambda title: _grid_row(headers[title])) == ["Bugün", "Dün", "Önceki 7 gün", "Daha eski"]
    rows = {chat["title"]: _grid_row(app._chat_rows[chat["id"]]["frame"]) for chat in index}
    assert _grid_row(headers["Bugün"]) < rows["bugün başarısız"] < rows["bugün başarılı"] < _grid_row(headers["Dün"])
    assert _grid_row(headers["Dün"]) < rows["dün"] < _grid_row(headers["Önceki 7 gün"]) < rows["geçen hafta"]
    assert rows["geçen hafta"] < _grid_row(headers["Daha eski"]) < rows["çok eski başarısız"]
    assert all(label.cget("text_color") == ui.TEXT_FAINT for label in headers.values())  # başlıklar soluktur


def test_sidebar_shows_a_red_dot_only_for_failed_and_orange_for_the_running_chat(app: ui.OmniUI) -> None:
    index = _seed_sidebar(app)
    by_title = {chat["title"]: app._chat_rows[chat["id"]]["marker"] for chat in index}
    assert [(name, by_title[name].cget("text")) for name in by_title] == [
        ("bugün başarısız", "●"), ("bugün başarılı", ""), ("dün", ""), ("geçen hafta", ""), ("çok eski başarısız", "●")]
    assert by_title["bugün başarısız"].cget("text_color") == ui.ERROR
    # Çalışan görev: yalnız etkin sohbet turuncu nokta alır; başarısız sonucu olan sohbet çalışırken 'çalışıyor' gösterir.
    app._active_chat_id = index[0]["id"]
    app._dot_bright = True
    running: Future[None] = Future()
    app._agent_future = running
    app._refresh_chat_list()
    assert by_title["bugün başarısız"].cget("text_color") == ui.ACCENT
    assert by_title["çok eski başarısız"].cget("text_color") == ui.ERROR
    running.set_result(None)
    app._agent_future = None
    app._refresh_chat_list()
    assert by_title["bugün başarısız"].cget("text_color") == ui.ERROR  # çalışma bitince son sonuç noktası döner


def test_running_dot_pulses_slowly_and_other_dots_stay_still(app: ui.OmniUI) -> None:
    index = _seed_sidebar(app)
    app._active_chat_id = index[1]["id"]
    running: Future[None] = Future()
    app._agent_future = running
    app._dot_bright = True
    app._refresh_chat_list()
    dot = app._chat_rows[index[1]["id"]]["marker"]
    assert dot.cget("text_color") == ui.ACCENT
    app._last_dot_pulse = 0.0
    app._pulse_running_dot(time.monotonic())
    assert dot.cget("text_color") == ui.ACCENT_MUTED
    app._pulse_running_dot(time.monotonic())  # aralık dolmadan yeniden çağrı rengi değiştirmez
    assert dot.cget("text_color") == ui.ACCENT_MUTED
    assert app._chat_rows[index[0]["id"]]["marker"].cget("text_color") == ui.ERROR
    running.set_result(None)
    app._agent_future = None


def test_sidebar_updates_rows_in_place_and_destroys_only_what_disappears(app: ui.OmniUI) -> None:
    index = _seed_sidebar(app)
    frames = {chat["id"]: app._chat_rows[chat["id"]]["frame"] for chat in index}
    app._refresh_chat_list()
    assert all(app._chat_rows[chat_id]["frame"] is frame for chat_id, frame in frames.items())  # yeniden kurulmadı
    app._chat_index = [
        {**index[0], "last_outcome": "done"} if position == 0 else chat for position, chat in enumerate(index)
    ]
    app._refresh_chat_list()
    assert app._chat_rows[index[0]["id"]]["frame"] is frames[index[0]["id"]]  # aynı widget, yalnız görünümü değişti
    assert app._chat_rows[index[0]["id"]]["marker"].cget("text") == ""
    app._chat_search.insert(0, "dün")
    app._refresh_chat_list()
    assert list(app._chat_rows) == [index[2]["id"]]
    assert list(app._chat_group_labels) == ["Dün"]
    assert not frames[index[0]["id"]].winfo_exists()
    app._chat_search.delete(0, "end")
    app._chat_search.insert(0, "yok böyle bir sohbet")
    app._refresh_chat_list()
    assert [child.cget("text") for child in app._chat_list.winfo_children()] == ["Sonuç bulunamadı"]


def test_sidebar_rebuilds_rows_for_select_mode_and_marks_checked_rows(app: ui.OmniUI) -> None:
    index = _seed_sidebar(app)
    before = app._chat_rows[index[0]["id"]]["frame"]
    app._toggle_chat_select_mode()
    assert app._chat_rows[index[0]["id"]]["frame"] is not before  # düzen değişti: satırlar yeniden kuruldu
    assert {row["marker"].cget("text") for row in app._chat_rows.values()} == {"☐"}
    assert all(row["more"] is None for row in app._chat_rows.values())  # seçim modunda '⋯' yok
    app._toggle_chat_selection(index[1]["id"])
    assert app._chat_rows[index[1]["id"]]["marker"].cget("text") == "☑"
    assert app._chat_rows[index[1]["id"]]["frame"].cget("fg_color") == ui.SURFACE_RAISED
    app._toggle_chat_select_mode()


def test_row_hover_highlights_and_reveals_the_actions_button(app: ui.OmniUI) -> None:
    index = _seed_sidebar(app)
    row = app._chat_rows[index[2]["id"]]
    assert row["frame"].cget("fg_color") == "transparent" and not row["more"].grid_info()
    app._hover_chat_row(index[2]["id"], True)
    assert row["frame"].cget("fg_color") == ui.SURFACE_HOVER and row["more"].grid_info()
    app._hover_chat_row(index[2]["id"], False)
    assert row["frame"].cget("fg_color") == "transparent" and not row["more"].grid_info()
    app._active_chat_id = index[2]["id"]
    app._refresh_chat_list()
    assert row["frame"].cget("fg_color") == ui.SURFACE_RAISED  # etkin satır belirgindir


def _fire(widget: tk.Misc, sequence: str) -> None:
    """
    CTk bileşeninin tuvaline olay üretir: CTk bağlamaları hem tuvale hem iç etikete kurar; boş metinli etiket
    (durum noktası yok) ekrana yerleşmediği için gerçek işaretçi de bileşene tuvalden girer.
    """
    canvas = next(child for child in widget.winfo_children() if isinstance(child, tk.Canvas))
    widget.update()  # yerleşim işlensin: ekrana yerleşmemiş bileşene Tk olay iletmez
    canvas.event_generate(sequence, x=3, y=3)


def test_row_bindings_track_the_pointer_over_every_part_of_the_row(app: ui.OmniUI, monkeypatch: pytest.MonkeyPatch) -> None:
    index = _seed_sidebar(app)
    row = app._chat_rows[index[2]["id"]]
    monkeypatch.setattr(app, "winfo_containing", lambda x, y: None)  # işaretçi satırın dışında
    for part in (row["marker"], row["button"], row["more"]):
        _fire(part, "<Enter>")
        assert row["hover"] and row["more"].winfo_manager() == "grid"
        _fire(part, "<Leave>")
        assert not row["hover"] and row["more"].winfo_manager() == ""


def test_leaving_towards_another_part_of_the_same_row_keeps_the_highlight(
    app: ui.OmniUI, monkeypatch: pytest.MonkeyPatch,
) -> None:
    index = _seed_sidebar(app)
    row = app._chat_rows[index[2]["id"]]
    monkeypatch.setattr(app, "winfo_containing", lambda x, y: row["button"])  # işaretçi aynı satırın başlığına geçti
    _fire(row["marker"], "<Enter>")
    _fire(row["marker"], "<Leave>")
    assert row["hover"] and row["more"].winfo_manager() == "grid"
    monkeypatch.setattr(app, "winfo_containing", lambda x, y: app.entry.widget)  # başka pencereye geçince çıkılır
    _fire(row["marker"], "<Leave>")
    assert not row["hover"]


def test_clicking_any_part_of_a_row_opens_the_chat_or_toggles_its_selection(
    app: ui.OmniUI, monkeypatch: pytest.MonkeyPatch,
) -> None:
    index = _seed_sidebar(app)
    chat_id = index[2]["id"]
    opened: List[str] = []
    monkeypatch.setattr(app, "_open_chat", lambda target: opened.append(target))
    row = app._chat_rows[chat_id]
    _fire(row["marker"], "<Button-1>")  # sol sütuna tıklamak da sohbeti açar
    row["button"].invoke()
    assert opened == [chat_id, chat_id]
    app._toggle_chat_select_mode()
    _fire(app._chat_rows[chat_id]["marker"], "<Button-1>")
    assert chat_id in app._selected_chat_ids and app._chat_rows[chat_id]["marker"].cget("text") == "☑"
    assert opened == [chat_id, chat_id]  # seçim modunda sohbet açılmaz
    app._toggle_chat_select_mode()


def test_unknown_row_tone_fails_loudly_instead_of_falling_back_to_a_colour(app: ui.OmniUI) -> None:
    assert app._marker_color("failed") == ui.ERROR and app._marker_color("none") == ui.TEXT_FAINT
    with pytest.raises(KeyError):
        app._marker_color("bilinmeyen")


# --- Üst şerit ve composer hizası ---

@pytest.mark.parametrize("width", [860, 1500])
def test_header_status_line_and_composer_share_the_transcript_column(app: ui.OmniUI, width: int) -> None:
    app.geometry(f"{width}x820")
    pump(app, 0.5)
    left, span = app._text.winfo_rootx(), app._text.winfo_width()
    assert span == app._column_width
    for widget in (app._header, app._activity_bar, app._composer_card):
        assert (widget.winfo_rootx(), widget.winfo_width()) == (left, span)
    assert app.chat_title_label.winfo_rootx() == left  # başlık sütunun sol kenarında
    assert app.settings_btn.winfo_rootx() + app.settings_btn.winfo_width() == left + span  # sağ düğme sütunun sağ kenarında
    assert app.mode_hint.winfo_rootx() == left + ui.COMPOSER_PAD_X  # ipucu giriş metniyle hizalı
    assert app.entry.widget.winfo_rootx() - left == ui.COMPOSER_PAD_X  # giriş metni kartın iç boşluğunda


def test_wide_window_caps_the_column_and_narrow_window_keeps_the_minimum_side(app: ui.OmniUI) -> None:
    app.geometry("1600x820")
    pump(app, 0.4)
    assert app._column_width == ui.COLUMN_MAX_WIDTH and app._column_side > ui.COLUMN_MIN_SIDE
    app.geometry("820x820")
    pump(app, 0.4)
    assert app._column_side == ui.COLUMN_MIN_SIDE and app._composer_card.winfo_width() == app._column_width


# --- Composer ---

def test_composer_sits_in_one_rounded_card_with_menus_voice_and_send_and_a_room_for_attachments(app: ui.OmniUI) -> None:
    assert app._composer_card.cget("corner_radius") == ui.COMPOSER_RADIUS
    inside = {str(widget.master) for widget in (app.mode_menu, app.backend_menu, app.voice_btn, app.primary_btn)}
    assert len(inside) == 1  # hepsi aynı alt satırda
    assert str(app._composer_attach_slot.master) in inside  # ileride ek düğmesi için ayrılmış boş yer
    assert app.entry.widget.master is app._composer_card
    strip = app._composer_attachments  # ileride ek önizlemeleri için girişin üstünde ayrılmış boş şerit
    assert strip.master is app._composer_card and _grid_row(strip) < _grid_row(app.entry.widget)
    pump(app, 0.2)
    assert strip.winfo_height() <= 1  # boşken yer kaplamaz
    assert strip.winfo_y() >= int(app._composer_card.cget("border_width"))  # kartın üst kenarlık çizgisini örtmez


def test_placeholder_never_covers_the_caret(app: ui.OmniUI) -> None:
    """
    Yer tutucu opak bir etikettir ve imleç metin alanının en solunda (x=0) çizilir: yer tutucu tam
    x=0'a konursa imleci örter ve boş composer'a tıklayan kullanıcı "imleç yanmıyor" görür. Bu yüzden
    yer tutucu imlecin sağından başlar.
    """
    pump(app, 0.3)
    caret = app.entry.widget.bbox("insert")
    placeholder_x = int(app.entry._placeholder.place_info()["x"])
    assert caret is not None and caret[0] < placeholder_x
    assert placeholder_x == composer.PLACEHOLDER_CARET_GAP > int(app.entry.widget.cget("insertwidth")) - 1


def test_composer_grows_with_the_content_and_shows_the_placeholder_only_when_empty(app: ui.OmniUI) -> None:
    pump(app, 0.3)
    assert app.entry.visible_lines() == theme.COMPOSER_MIN_LINES and app.entry._placeholder.place_info()
    app.entry.insert(0, "Şu görevi yap:\n1) dosyaları listele\n2) büyükleri bul\n3) özetle")
    pump(app, 0.3)
    assert app.entry.visible_lines() == 4 and not app.entry._placeholder.place_info()  # dört satır: sonuncusu kesilmez
    app.entry.delete(0, "end")
    app.entry.insert(0, "\n".join(f"satır {number}" for number in range(1, 30)))
    pump(app, 0.3)
    assert app.entry.visible_lines() == theme.COMPOSER_MAX_LINES  # üst sınırdan sonra kaydırılır
    app.entry.delete(0, "end")
    pump(app, 0.3)
    assert app.entry.visible_lines() == theme.COMPOSER_MIN_LINES and app.entry._placeholder.place_info()


def test_composer_wraps_long_lines_and_regrows_when_the_window_narrows(app: ui.OmniUI) -> None:
    app.geometry("1500x820")
    pump(app, 0.4)
    app.entry.insert(0, "uzun cümle " * 40)
    pump(app, 0.3)
    wide_lines = app.entry.visible_lines()
    app.geometry("820x820")
    pump(app, 0.5)
    assert app.entry.visible_lines() > wide_lines  # dar pencerede aynı metin daha çok satıra sarılır


def test_enter_sends_the_goal_and_shift_enter_adds_a_line(app: ui.OmniUI, monkeypatch: pytest.MonkeyPatch) -> None:
    sent: List[str] = []

    async def fake_run(goal: str, options: object) -> None:
        sent.append(goal)

    monkeypatch.setattr(app, "_run_exclusive", fake_run)
    monkeypatch.setattr(ui, "set_dock_badge", lambda badge: None)
    for sequence in ("<Return>", "<KP_Enter>", "<Shift-Return>", "<Tab>"):
        assert app.entry.widget.bind(sequence)  # bağlamalar gerçekten kurulu
    app.entry.insert(0, "birinci satır")
    app.entry.widget.mark_set("insert", "end-1c")  # imleç satır sonunda
    assert app.entry._newline(None) == "break"  # type: ignore[arg-type]
    app.entry.insert("end", "ikinci satır")
    assert app.entry.get() == "birinci satır\nikinci satır"
    assert app._agent_future is None  # Shift+Enter göndermez
    assert app.entry._submit(None) == "break"  # type: ignore[arg-type]
    assert app._agent_future is not None
    app._agent_future.result(timeout=3)
    assert sent == ["birinci satır\nikinci satır"]  # çok satırlı hedef olduğu gibi gider
    assert app.entry.get() == ""


def test_composer_entry_api_keeps_voice_and_state_behaviour(app: ui.OmniUI) -> None:
    app._voice_base_text = ""
    app._replace_entry_text("sesli metin", disabled=True)
    assert app.entry.get() == "sesli metin" and str(app.entry.widget.cget("state")) == "disabled"
    app.entry.configure(state="normal")
    app.entry.insert("end", " ve devamı")
    assert app.entry.get() == "sesli metin ve devamı"
    with pytest.raises(ValueError, match="yalnız 0 ve 'end'"):
        app.entry.insert(3, "x")
    app.entry.delete(0, "end")


def test_composer_border_follows_focus(app: ui.OmniUI) -> None:
    assert app._composer_card.cget("border_color") == ui.BORDER
    app._on_composer_focus(True)
    assert app._composer_card.cget("border_color") == ui.COMPOSER_FOCUS_BORDER
    app._on_composer_focus(False)
    assert app._composer_card.cget("border_color") == ui.BORDER


def _press(widget: tk.Misc) -> SimpleNamespace:
    """Tk olayı yerine geçen tutucu: odak kararı yalnız `event.widget` alanını okur."""
    return SimpleNamespace(widget=widget)


def _containers(ctk_widget: Any) -> List[Any]:
    """CTk bileşenini ve iç tuvalini birlikte döner: gerçek tıklama tuvale düşer."""
    canvas: Any = getattr(ctk_widget, "_canvas", None)
    return [ctk_widget, canvas] if canvas is not None else [ctk_widget]


def test_clicking_anywhere_but_typing_surfaces_returns_focus_to_the_composer(
    app: ui.OmniUI, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    CustomTkinter `<Button-1>`'i "all" düzeyinde bağlayıp odağı tıklanan bileşene verir: kartın dolgu
    alanına (yuvarlak tuval), üst şeride, akış satırına ya da sohbete tıklayıp yazmaya başlayınca tuşlar
    hiçbir yere gitmiyordu. Bırakma anında odak composer'a döner.
    """
    assert app.bind("<ButtonRelease-1>")  # bırakma bağlaması kurulu
    assert str(app._composer_card) not in app._composer_card._canvas.bindtags()  # kart bağlaması tuvale ulaşmaz
    focused: List[str] = []
    monkeypatch.setattr(app.entry, "focus_set", lambda: focused.append("composer"))
    app._text.tag_remove("sel", "1.0", "end")
    targets: List[Any] = [*_containers(app._composer_card),
                          app._header, app._activity, app._text, app.voice_btn, app.primary_btn]
    for widget in targets:
        app._keep_composer_focus(_press(widget))
    assert focused == ["composer"] * len(targets)


def test_focus_stays_where_the_user_is_typing_or_selecting(
    app: ui.OmniUI, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Girişler, sohbet arama, mod/model menüleri ve transkriptteki metin seçimi kendi odağını korur."""
    focused: List[str] = []
    monkeypatch.setattr(app.entry, "focus_set", lambda: focused.append("composer"))
    app._text.tag_remove("sel", "1.0", "end")
    app._text.insert("end", "seçilecek metin")
    for owner in (app.entry.widget, app._chat_search, app.mode_menu, app.backend_menu):
        for widget in _containers(owner):
            app._keep_composer_focus(_press(widget))
    assert focused == []
    app._text.tag_add("sel", "1.0", "1.4")
    app._keep_composer_focus(_press(app._text))
    assert focused == []  # seçim varken ⌘C odağın bulunduğu bileşenden kopyalar
    app._text.tag_remove("sel", "1.0", "end")
    app._keep_composer_focus(_press(app._text))
    assert focused == ["composer"]


def test_switching_chats_clears_the_previous_task_result_from_the_header(app: ui.OmniUI) -> None:
    """Önceki sohbetin son görev sonucu (✓/✕ süre) yeni ya da açılan sohbetin başlığına taşınmaz."""
    index = _seed_sidebar(app)
    app._set_task_status("failed", 42.0)
    assert app.task_status_label.cget("text") == "✕ 42 sn"
    app._new_chat()
    assert app.task_status_label.cget("text") == "" and app._task_status == "idle"
    app._set_task_status("done", 5.0)
    app._chat_record = None
    record = chats.new_chat("Açılacak sohbet")
    chats.save_chat(record)
    app._chat_index = [chats.summary_of(record), *index]
    app._open_chat(record["id"], save_current=False)
    assert app.task_status_label.cget("text") == ""
    app._set_task_status("running")  # çalışan görev durumu sohbet yenilemeleriyle silinmez
    app._refresh_chat_list()
    assert app._task_status == "running"
    app._set_task_status("idle")
