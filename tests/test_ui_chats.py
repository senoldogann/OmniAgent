"""Masaüstü sohbetlerinin yeniden açılma ve veri koruma sınamaları."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from omniagent.core.conversation import make_exchange
from omniagent.ui import app as ui
from omniagent.ui import chats


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
        window._on_close()


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
        assert window._save_current_chat(force=True)

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
        window._on_close()

    reopened = ui.OmniUI()
    reopened.withdraw()
    try:
        assert reopened._active_chat_id == first_id
        assert "İlk yanıt" in reopened._text.get("1.0", "end")
        reopened._open_chat(second_id)
        assert "İkinci yanıt" in reopened._text.get("1.0", "end")
        assert reopened._history[0]["goal"] == "İkinci sohbet"
        assert reopened.context_label.cget("text") == "bağlam: 1 mesaj"
        assert reopened._rename_chat(second_id, "Yenilenen sohbet")
        reopened._chat_search.insert(0, "yenilenen")
        reopened._refresh_chat_list()
        assert len(reopened._chat_list.winfo_children()) == 1
        reopened._chat_search.delete(0, "end")
        reopened._chat_search.insert(0, "eşleşmeyen")
        reopened._refresh_chat_list()
        assert reopened._chat_list.winfo_children()[0].cget("text") == "Sonuç bulunamadı"
        assert reopened._delete_chat(second_id)
        assert reopened._active_chat_id == first_id
        reopened._restore_deleted_chat()
        assert chats.load_chat(second_id)["title"] == "Yenilenen sohbet"
    finally:
        reopened._on_close()


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

        def fail_save(_record: chats.ChatRecord) -> None:
            raise OSError("disk dolu")

        monkeypatch.setattr(ui, "save_chat", fail_save)
        window._new_chat()
        assert window._active_chat_id == chat_id
        assert "Korunacak içerik" in window._text.get("1.0", "end")
        assert "Sohbet kaydedilemedi" in window._chat_notice.cget("text")
    finally:
        monkeypatch.setattr(ui, "save_chat", original_save)
        window._on_close()
