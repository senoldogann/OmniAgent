"""
Kontrol Noktası ve Durum Saklayıcı Testleri (tests/test_checkpoint.py)
Atomik yazma, bozuk veri dayanıklılığı, en son checkpoint arama ve scratchpad biçimlendirmesi.
"""
from pathlib import Path
import pytest

from omniagent.core.checkpoint import (
    SessionCheckpoint,
    clear_checkpoint,
    find_latest_checkpoint,
    find_resume_checkpoint,
    format_checkpoint_scratchpad,
    load_checkpoint,
    save_checkpoint,
)


def test_checkpoint_save_and_load_roundtrip(tmp_path: Path) -> None:
    """Oturum kontrol noktası atomik olarak yazılır ve tam doğrulukla geri okunur."""
    saved_path = save_checkpoint(
        session_id="run-12345",
        goal="İş ilanlarını tara ve en yüksek maaşı bul",
        facts={"en_yuksek": "IL-12F528 6300 €", "toplam_ilan": "10"},
        completed_steps=["https://example.com/jobs açıldı", "10 ilan incelendi"],
        turn_count=6,
        runs_dir=tmp_path,
    )
    assert saved_path.is_file()
    assert saved_path.name == "run-12345.json"

    loaded = load_checkpoint("run-12345", runs_dir=tmp_path)
    assert loaded is not None
    assert loaded["session_id"] == "run-12345"
    assert loaded["goal"] == "İş ilanlarını tara ve en yüksek maaşı bul"
    assert loaded["facts"]["en_yuksek"] == "IL-12F528 6300 €"
    assert len(loaded["completed_steps"]) == 2
    assert loaded["turn_count"] == 6


def test_find_latest_checkpoint(tmp_path: Path) -> None:
    """Birden fazla oturum arasından en son güncellenen bulunur."""
    save_checkpoint("run-old", "Eski görev", {}, [], 2, runs_dir=tmp_path)
    save_checkpoint("run-new", "Yeni görev", {"status": "ok"}, [], 4, runs_dir=tmp_path)

    latest = find_latest_checkpoint(runs_dir=tmp_path)
    assert latest is not None
    assert latest["session_id"] == "run-new"
    assert latest["goal"] == "Yeni görev"


def test_resume_checkpoint_uses_goal_scope_and_refuses_ambiguous_global_latest(tmp_path: Path) -> None:
    save_checkpoint("run-mail", "Outlook gelen kutusunu temizle", {}, [], 2, runs_dir=tmp_path)
    save_checkpoint("run-code", "OmniAgent paketleme hatasını düzelt", {}, [], 4, runs_dir=tmp_path)

    assert find_resume_checkpoint(None, runs_dir=tmp_path) is None
    selected = find_resume_checkpoint("OmniAgent paketleme hatasını düzelt", runs_dir=tmp_path)
    assert selected is not None
    assert selected["session_id"] == "run-code"


def test_corrupt_checkpoint_returns_none(tmp_path: Path) -> None:
    """Bozuk JSON içeren kontrol noktası dosyası hatasız None döner."""
    corrupt_file = tmp_path / "run-broken.json"
    corrupt_file.write_text("{bu bir gecersiz json", encoding="utf-8")

    assert load_checkpoint("run-broken", runs_dir=tmp_path) is None


def test_clear_checkpoint(tmp_path: Path) -> None:
    """Tamamlanan checkpoint temizlenir."""
    save_checkpoint("run-done", "Bitmiş iş", {}, [], 1, runs_dir=tmp_path)
    assert (tmp_path / "run-done.json").exists()

    success = clear_checkpoint("run-done", runs_dir=tmp_path)
    assert success is True
    assert not (tmp_path / "run-done.json").exists()


def test_format_checkpoint_scratchpad() -> None:
    """Model scratchpad metni doğrulanmış gerçekleri ve adımları içerir."""
    checkpoint: SessionCheckpoint = {
        "session_id": "sess-99",
        "goal": "Dosya analizi",
        "facts": {"aktif_kullanici": "dogan", "port": "8080"},
        "completed_steps": ["port tarandı", "log dosyası okundu"],
        "turn_count": 3,
        "updated_at": "2026-09-25T12:00:00Z",
    }
    scratchpad = format_checkpoint_scratchpad(checkpoint)
    assert "Dosya analizi" in scratchpad
    assert "aktif_kullanici: dogan" in scratchpad
    assert "port: 8080" in scratchpad
    assert "log dosyası okundu" in scratchpad
