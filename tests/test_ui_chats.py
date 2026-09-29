"""Masaüstü sohbetlerinin yeniden açılma ve veri koruma sınamaları."""
from __future__ import annotations

import copy
import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from omniagent.core.conversation import make_exchange
from omniagent.ui import app as ui
from omniagent.ui import chats
from tests.test_ui_conversation import close_window


def test_chat_store_keeps_separate_records_and_private_files(tmp_path: Path) -> None:
    first = chats.new_chat("İlk hedef")
    first["history"] = [make_exchange("İlk hedef", "Birinci yanıt", [])]
    first["spans"] = [{"text": "Birinci yanıt\n", "tags": ["assistant", "md_bold"]}]
    second = chats.new_chat("İkinci hedef")
    second["spans"] = [{"text": "İkinci yanıt\n", "tags": ["assistant"]}]
    chats.save_chat(first, tmp_path)
    chats.save_chat(second, tmp_path)
    catalog = [
        {"id": second["id"], "title": second["title"], "updated_at": second["updated_at"]},
        {"id": first["id"], "title": first["title"], "updated_at": first["updated_at"]},
    ]
    chats.save_catalog(catalog, first["id"], tmp_path)

    loaded, active = chats.load_catalog(tmp_path)
    assert [item["id"] for item in loaded] == [second["id"], first["id"]]
    assert active == first["id"]
    assert chats.load_chat(first["id"], tmp_path)["history"][0]["answer"] == "Birinci yanıt"
    assert chats.load_chat(second["id"], tmp_path)["spans"][0]["text"] == "İkinci yanıt\n"
    assert (tmp_path / "index.json").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / f"{first['id']}.json").stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError):
        chats.load_chat("../index", tmp_path)


def test_transcript_dump_preserves_tags_without_live_region_ids() -> None:
    spans = chats.spans_from_dump([
        ("tagon", "goal", "1.0"), ("tagon", "r12", "1.0"),
        ("text", "› hedef\n", "1.0"), ("tagoff", "r12", "2.0"),
        ("tagoff", "goal", "2.0"), ("tagon", "assistant", "2.0"),
        ("text", "yanıt", "2.0"), ("text", "\n", "2.5"),
    ])
    assert spans == [
        {"text": "› hedef\n", "tags": ["goal"]},
        {"text": "yanıt\n", "tags": ["assistant"]},
    ]


def test_transcript_dump_drops_transient_tk_state() -> None:
    """Seçim, canlı bölge kimliği, akış imleci, takılı işaret ve dönen glif diske yazılmaz."""
    spans = chats.spans_from_dump([
        ("tagon", "goal", "1.0"), ("tagon", "sel", "1.0"), ("tagon", "r7", "1.0"),
        ("text", "› ", "1.0"), ("tagoff", "sel", "1.2"), ("text", "hedef\n", "1.2"),
        ("tagoff", "r7", "2.0"), ("tagoff", "goal", "2.0"),
        ("tagon", "cursor", "2.0"), ("text", "▌", "2.0"), ("tagoff", "cursor", "2.1"),
        ("tagon", "tool_spin", "2.1"), ("text", "✻", "2.1"), ("tagoff", "tool_spin", "2.2"),
        ("tagon", "bullet_streaming", "2.2"), ("text", "⏺ ", "2.2"), ("tagoff", "bullet_streaming", "2.4"),
        ("tagon", "assistant", "2.4"), ("text", "yanıt\n", "2.4"),
    ])
    assert spans == [
        {"text": "› hedef\n", "tags": ["goal"]},
        {"text": "yanıt\n", "tags": ["assistant"]},
    ]
    original: list[chats.TranscriptSpan] = [{"text": "a", "tags": ["sel", "x"]}, {"text": "b", "tags": ["x"]}]
    snapshot = copy.deepcopy(original)
    assert chats.sanitize_spans(original) == [{"text": "ab", "tags": ["x"]}]
    assert original == snapshot


def test_load_chat_cleans_legacy_transient_tags(tmp_path: Path) -> None:
    """Diskte kalmış eski bozuk kayıt yüklenirken temizlenir; sonraki kayıt/yükleme aynı sonucu verir."""
    record = chats.new_chat("Eski sohbet")
    record["spans"] = [
        {"text": "seçili ", "tags": ["goal", "sel"]}, {"text": "metin\n", "tags": ["goal"]},
        {"text": "▌", "tags": ["cursor"]}, {"text": "⏺ ", "tags": ["bullet_streaming"]},
        {"text": "Kabuk\n", "tags": ["tool_name"]},
    ]
    chats.save_chat(record, tmp_path)  # save_chat dokunmaz: bozuk kayıt olduğu gibi diske gider
    loaded = chats.load_chat(record["id"], tmp_path)
    expected = [{"text": "seçili metin\n", "tags": ["goal"]}, {"text": "Kabuk\n", "tags": ["tool_name"]}]
    assert loaded["spans"] == expected
    chats.save_chat(loaded, tmp_path)
    assert chats.load_chat(record["id"], tmp_path)["spans"] == expected


def test_rename_delete_restore_preserves_content(tmp_path: Path) -> None:
    record = chats.new_chat("İlk ad")
    record["history"] = [make_exchange("hedef", "kalıcı yanıt", [])]
    chats.save_chat(record, tmp_path)
    catalog = [{"id": record["id"], "title": record["title"], "updated_at": record["updated_at"]}]
    chats.save_catalog(catalog, record["id"], tmp_path)

    renamed = chats.rename_chat(record["id"], "  Yeni   ad  ", catalog, record["id"], tmp_path)
    assert renamed[0]["title"] == "Yeni ad"
    assert chats.load_chat(record["id"], tmp_path)["title"] == "Yeni ad"
    with pytest.raises(ValueError):
        chats.rename_chat(record["id"], "  ", renamed, record["id"], tmp_path)

    remaining, active = chats.delete_chat(record["id"], renamed, record["id"], tmp_path)
    assert remaining == [] and active is None
    assert not (tmp_path / f"{record['id']}.json").exists()
    assert (tmp_path / "trash" / f"{record['id']}.json").exists()
    restored = chats.restore_chat(record["id"], remaining, active, tmp_path)
    assert restored[0]["title"] == "Yeni ad"
    assert chats.load_chat(record["id"], tmp_path)["history"][0]["answer"] == "kalıcı yanıt"


def test_delete_catalog_failure_restores_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    record = chats.new_chat("Korunan")
    chats.save_chat(record, tmp_path)
    catalog = [{"id": record["id"], "title": record["title"], "updated_at": record["updated_at"]}]
    chats.save_catalog(catalog, record["id"], tmp_path)

    def fail_catalog(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("disk dolu")

    monkeypatch.setattr(chats, "save_catalog", fail_catalog)
    with pytest.raises(OSError, match="disk dolu"):
        chats.delete_chat(record["id"], catalog, record["id"], tmp_path)
    assert chats.load_chat(record["id"], tmp_path)["title"] == "Korunan"
    assert not (tmp_path / "trash" / f"{record['id']}.json").exists()


def test_bulk_delete_is_atomic_and_restorable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = chats.new_chat("Birinci")
    second = chats.new_chat("İkinci")
    for record in (first, second):
        chats.save_chat(record, tmp_path)
    catalog = [
        {"id": record["id"], "title": record["title"], "updated_at": record["updated_at"]}
        for record in (first, second)
    ]
    chats.save_catalog(catalog, first["id"], tmp_path)
    original_save = chats.save_catalog

    def fail_catalog(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("disk dolu")

    monkeypatch.setattr(chats, "save_catalog", fail_catalog)
    with pytest.raises(OSError, match="disk dolu"):
        chats.delete_chats([first["id"], second["id"]], catalog, first["id"], tmp_path)
    assert all((tmp_path / f"{record['id']}.json").exists() for record in (first, second))
    monkeypatch.setattr(chats, "save_catalog", original_save)
    remaining, active = chats.delete_chats([first["id"], second["id"]], catalog, first["id"], tmp_path)
    assert remaining == [] and active is None
    restored = chats.restore_chats([first["id"], second["id"]], remaining, active, tmp_path)
    assert [item["id"] for item in restored] == [first["id"], second["id"]]


def test_last_outcome_is_additive_and_legacy_catalogs_still_load(tmp_path: Path) -> None:
    """Son görev sonucu dizine ve kayda EK alan olarak yazılır; alanı olmayan eski kayıtlar açılır, bozuk değer yok sayılır."""
    failed = chats.new_chat("Başarısız görev")
    failed["last_outcome"] = "failed"
    quiet = chats.new_chat("Sonuçsuz görev")
    for record in (failed, quiet):
        chats.save_chat(record, tmp_path)
    chats.save_catalog([chats.summary_of(failed), chats.summary_of(quiet)], failed["id"], tmp_path)
    loaded, _active = chats.load_catalog(tmp_path)
    assert [item.get("last_outcome") for item in loaded] == ["failed", None]
    assert chats.load_chat(failed["id"], tmp_path)["last_outcome"] == "failed"
    assert "last_outcome" not in chats.load_chat(quiet["id"], tmp_path)
    # Eski biçim (alan yok) ve tanınmayan değer: açılır, alan taşınmaz.
    raw = json.loads((tmp_path / "index.json").read_text(encoding="utf-8"))
    raw["chats"][0].pop("last_outcome")
    raw["chats"][1]["last_outcome"] = "bilinmeyen"
    (tmp_path / "index.json").write_text(json.dumps(raw), encoding="utf-8")
    legacy, _active = chats.load_catalog(tmp_path)
    assert all("last_outcome" not in item for item in legacy)
    # with_outcome yeni satır döndürür, girdiyi değiştirmez ve bilinmeyen sonucu reddeder.
    summary = chats.summary_of(quiet)
    assert chats.with_outcome(summary, "done")["last_outcome"] == "done" and "last_outcome" not in summary
    assert "last_outcome" not in chats.with_outcome({**summary, "last_outcome": "done"}, None)
    with pytest.raises(ValueError, match="Bilinmeyen görev sonucu"):
        chats.with_outcome(summary, "belki")


def test_deleted_and_restored_chat_keeps_its_last_outcome(tmp_path: Path) -> None:
    record = chats.new_chat("Geri alınacak")
    record["last_outcome"] = "failed"
    chats.save_chat(record, tmp_path)
    catalog = [chats.summary_of(record)]
    chats.save_catalog(catalog, record["id"], tmp_path)
    renamed = chats.rename_chat(record["id"], "Yeni ad", catalog, record["id"], tmp_path)
    assert renamed[0]["last_outcome"] == "failed"  # yeniden adlandırma sonucu korur
    remaining, active = chats.delete_chat(record["id"], renamed, record["id"], tmp_path)
    restored = chats.restore_chat(record["id"], remaining, active, tmp_path)
    assert restored[0]["last_outcome"] == "failed"


def test_catalog_with_an_unreadable_timestamp_is_refused_explicitly(tmp_path: Path) -> None:
    """Tarihe göre gruplama zaman damgasına dayanır: bozuk kayıt sessizce gizlenmez, açık hata verir."""
    record = chats.new_chat("Bozuk zaman")
    chats.save_chat(record, tmp_path)
    (tmp_path / "index.json").write_text(json.dumps({
        "version": 1, "active_id": None,
        "chats": [{"id": record["id"], "title": record["title"], "updated_at": "dün akşam"}],
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="zaman damgası okunamadı"):
        chats.load_catalog(tmp_path)


def test_unhashable_last_outcome_values_are_ignored_and_never_crash_the_load(tmp_path: Path) -> None:
    """Elle bozulmuş dizin ya da kayıtta last_outcome liste/sözlük olsa da açılış çökmez; değer taşınmaz."""
    record = chats.new_chat("Bozuk sonuç")
    chats.save_chat(record, tmp_path)
    chats.save_catalog([chats.summary_of(record)], record["id"], tmp_path)
    raw_index = json.loads((tmp_path / "index.json").read_text(encoding="utf-8"))
    raw_index["chats"][0]["last_outcome"] = ["failed"]
    (tmp_path / "index.json").write_text(json.dumps(raw_index), encoding="utf-8")
    loaded, _active = chats.load_catalog(tmp_path)
    assert "last_outcome" not in loaded[0]
    stored = json.loads((tmp_path / f"{record['id']}.json").read_text(encoding="utf-8"))
    stored["last_outcome"] = {"failed": True}
    (tmp_path / f"{record['id']}.json").write_text(json.dumps(stored), encoding="utf-8")
    assert "last_outcome" not in chats.load_chat(record["id"], tmp_path)


@pytest.mark.parametrize("stamp", ["", "dün akşam"])
def test_restoring_a_chat_with_an_unreadable_timestamp_is_refused_and_stays_in_the_trash(
    tmp_path: Path, stamp: str,
) -> None:
    """Geri alma yolu da zaman damgasını doğrular: bozuk kayıt kataloğa girmez, çöpte kalır, açık hata verilir."""
    record = chats.new_chat("Bozuk geri alma")
    record["updated_at"] = stamp
    chats.save_chat(record, tmp_path)
    (tmp_path / "trash").mkdir()
    trashed = tmp_path / "trash" / f"{record['id']}.json"
    (tmp_path / f"{record['id']}.json").rename(trashed)
    with pytest.raises(ValueError, match="zaman damgası okunamadı"):
        chats.restore_chat(record["id"], [], None, tmp_path)
    assert trashed.exists() and not (tmp_path / f"{record['id']}.json").exists()
    with pytest.raises(ValueError, match="zaman damgası okunamadı"):
        chats.restore_chats([record["id"]], [], None, tmp_path)
    assert trashed.exists() and not (tmp_path / f"{record['id']}.json").exists()


def _isolate_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Sohbet kayıtları tmp_path'e gider; model istemcisi ve global kısayol sahtedir (gerçek Keychain/kısayol yok)."""
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(ui, "create_model_clients", lambda: {})

    class FakeHotkey:
        def __init__(self, callback: object) -> None:
            self.callback = callback

        def close(self) -> None:
            return

    monkeypatch.setattr(ui, "GlobalVisibilityHotkey", FakeHotkey)


def _open_window_after_first_paint() -> ui.OmniUI:
    """Kayıtlı sohbetleri okuyan gerçek pencere; kenar çubuğu ve son sohbet ilk boyamadan sonra kurulur, o ana dek döner."""
    window = ui.OmniUI()
    window.withdraw()
    deadline = time.monotonic() + 3
    while not window._first_paint_done and time.monotonic() < deadline:
        window.update()
        time.sleep(0.01)
    assert window._first_paint_done
    return window


def _seed_two_chats(first_title: str, second_title: str, active: str) -> tuple[chats.ChatRecord, chats.ChatRecord]:
    first, second = chats.new_chat(first_title), chats.new_chat(second_title)
    for record in (first, second):
        chats.save_chat(record)
    active_id = {"first": first["id"], "second": second["id"], "none": None}[active]
    chats.save_catalog([chats.summary_of(first), chats.summary_of(second)], active_id)
    return first, second


@pytest.mark.skipif(os.environ.get("OMNI_UI_TEST") != "1", reason="Gerçek Tk testi OMNI_UI_TEST=1 ile etkinleştirilir.")
def test_broken_last_chat_at_startup_keeps_the_sidebar_and_reports_the_problem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Son açık sohbetin dosyası bozuksa açılışta kenar çubuğu boş kalmaz; sorun bildirimde görünür."""
    _isolate_data(tmp_path, monkeypatch)
    broken, healthy = _seed_two_chats("Bozuk sohbet", "Sağlam sohbet", "first")
    (chats.chats_dir() / f"{broken['id']}.json").write_text("{bozuk json", encoding="utf-8")
    window = _open_window_after_first_paint()
    try:
        assert set(window._chat_rows) == {healthy["id"], broken["id"]}  # liste boş kalmadı
        assert "Sohbet açılamadı" in window._chat_notice.cget("text")
        assert window._chat_record is None
    finally:
        close_window(window)


@pytest.mark.skipif(os.environ.get("OMNI_UI_TEST") != "1", reason="Gerçek Tk testi OMNI_UI_TEST=1 ile etkinleştirilir.")
def test_catalog_without_an_active_chat_still_fills_the_sidebar_after_first_paint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Etkin sohbet yoksa _open_chat hiç çağrılmaz: liste yine de ilk boyamadan sonra kurulur."""
    _isolate_data(tmp_path, monkeypatch)
    first, second = _seed_two_chats("Birinci", "İkinci", "none")
    window = _open_window_after_first_paint()
    try:
        assert set(window._chat_rows) == {first["id"], second["id"]} and window._chat_record is None
    finally:
        close_window(window)


@pytest.mark.skipif(os.environ.get("OMNI_UI_TEST") != "1", reason="Gerçek Tk testi OMNI_UI_TEST=1 ile etkinleştirilir.")
def test_deleting_the_active_chat_refreshes_the_sidebar_even_when_the_next_chat_cannot_be_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Silme sonrası sıradaki sohbet açılamasa da silinen satır kenar çubuğunda kalmaz."""
    _isolate_data(tmp_path, monkeypatch)
    doomed, broken = _seed_two_chats("Silinecek etkin sohbet", "Açılamayan sonraki sohbet", "first")
    (chats.chats_dir() / f"{broken['id']}.json").write_text("{bozuk json", encoding="utf-8")
    window = _open_window_after_first_paint()
    try:
        assert window._active_chat_id == doomed["id"] and set(window._chat_rows) == {doomed["id"], broken["id"]}
        assert window._delete_chat(doomed["id"])
        assert set(window._chat_rows) == {broken["id"]}  # silinen satır listede kalmadı
        assert "Sohbet açılamadı" in window._chat_notice.cget("text")
    finally:
        close_window(window)


@pytest.mark.skipif(os.environ.get("OMNI_UI_TEST") != "1", reason="Gerçek Tk testi OMNI_UI_TEST=1 ile etkinleştirilir.")
def test_sidebar_bulk_selection_delete_and_undo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(ui, "create_model_clients", lambda: {})
    first = chats.new_chat("Birinci")
    second = chats.new_chat("İkinci")
    for record in (first, second):
        chats.save_chat(record)
    catalog = [
        {"id": record["id"], "title": record["title"], "updated_at": record["updated_at"]}
        for record in (first, second)
    ]
    chats.save_catalog(catalog, first["id"])
    window = ui.OmniUI()
    window.withdraw()
    try:
        window._toggle_chat_select_mode()
        window._toggle_chat_selection(first["id"])
        window._toggle_chat_selection(second["id"])
        assert window._delete_selected_chats()
        assert window._chat_index == []
        assert window._empty_state.winfo_manager() == "place"
        assert window._last_deleted_chat_ids == [first["id"], second["id"]]
        window._restore_deleted_chat()
        assert [item["id"] for item in window._chat_index] == [first["id"], second["id"]]
    finally:
        close_window(window)


@pytest.mark.skipif(os.environ.get("OMNI_UI_TEST") != "1", reason="Gerçek Tk testi OMNI_UI_TEST=1 ile etkinleştirilir.")
def test_sidebar_switch_new_chat_and_restart_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(ui, "create_model_clients", lambda: {})

    class FakeHotkey:
        def __init__(self, callback: Any) -> None:
            self.callback = callback

        def close(self) -> None:
            return

    monkeypatch.setattr(ui, "GlobalVisibilityHotkey", FakeHotkey)
    window = ui.OmniUI()
    window.withdraw()
    try:
        assert window._ensure_chat("İlk sohbet")
        first_id = window._active_chat_id
        assert first_id is not None
        window._text.configure(state="normal")
        window._render_goal("İlk sohbet")
        window._new_region([("İlk yanıt\n", ("assistant",))])
        window._text.configure(state="disabled")
        window._history = [make_exchange("İlk sohbet", "İlk yanıt", [])]
        assert window._save_current_chat()

        window._new_chat()
        assert window._active_chat_id is None
        assert len(window._chat_index) == 1
        assert window._history == []
        assert window._ensure_chat("İkinci sohbet")
        second_id = window._active_chat_id
        assert second_id != first_id
        window._text.configure(state="normal")
        window._render_goal("İkinci sohbet")
        window._new_region([("İkinci yanıt\n", ("assistant",))])
        window._text.configure(state="disabled")
        window._history = [make_exchange("İkinci sohbet", "İkinci yanıt", [])]
        window._open_chat(first_id)
        assert "İlk yanıt" in window._text.get("1.0", "end")
        assert "İkinci yanıt" not in window._text.get("1.0", "end")
        assert window._history[0]["goal"] == "İlk sohbet"
        assert len(window._chat_index) == 2
    finally:
        close_window(window)

    reopened = ui.OmniUI()
    reopened.withdraw()
    try:
        # Açılıştaki sohbet ilk boyamadan sonra yüklenir (after_idle + FIRST_PAINT_DELAY_MS); yüklenene dek olay döngüsü döner.
        assert reopened._chat_record is None
        deadline = time.monotonic() + 3
        while reopened._chat_record is None and time.monotonic() < deadline:
            reopened.update()
            time.sleep(0.01)
        assert reopened._active_chat_id == first_id
        assert "İlk yanıt" in reopened._text.get("1.0", "end")
        reopened._open_chat(second_id)
        assert "İkinci yanıt" in reopened._text.get("1.0", "end")
        assert reopened._history[0]["goal"] == "İkinci sohbet"
        assert reopened.context_label.cget("text") == "bağlam: 1 mesaj"
        assert reopened._rename_chat(second_id, "Yenilenen sohbet")
        reopened._chat_search.insert(0, "yenilenen")
        reopened._refresh_chat_list()
        assert list(reopened._chat_rows) == [second_id]  # yalnız eşleşen satır kalır
        reopened._chat_search.delete(0, "end")
        reopened._chat_search.insert(0, "eşleşmeyen")
        reopened._refresh_chat_list()
        assert not reopened._chat_rows
        assert [child.cget("text") for child in reopened._chat_list.winfo_children()] == ["Sonuç bulunamadı"]
        assert reopened._delete_chat(second_id)
        assert reopened._active_chat_id == first_id
        reopened._restore_deleted_chat()
        assert chats.load_chat(second_id)["title"] == "Yenilenen sohbet"
    finally:
        close_window(reopened)


@pytest.mark.skipif(os.environ.get("OMNI_UI_TEST") != "1", reason="Gerçek Tk testi OMNI_UI_TEST=1 ile etkinleştirilir.")
def test_save_failure_keeps_current_chat_visible(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(ui, "create_model_clients", lambda: {})
    window = ui.OmniUI()
    window.withdraw()
    original_save = ui.save_chat
    try:
        assert window._ensure_chat("Korunacak sohbet")
        chat_id = window._active_chat_id
        window._text.configure(state="normal")
        window._new_region([("Korunacak içerik\n", ("assistant",))])
        window._text.configure(state="disabled")

        def fail_save(_record: chats.ChatRecord, _root: Path) -> None:
            raise OSError("disk dolu")

        monkeypatch.setattr(ui, "save_chat", fail_save)
        window._new_chat()
        assert window._active_chat_id == chat_id
        assert "Korunacak içerik" in window._text.get("1.0", "end")
        assert "Sohbet kaydedilemedi" in window._chat_notice.cget("text")
    finally:
        monkeypatch.setattr(ui, "save_chat", original_save)
        close_window(window)
