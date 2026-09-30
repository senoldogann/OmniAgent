"""Karakter: kullanıcının düzenlediği persona.md korunur; istem öneki sabit, durum bloğu Türkçe gün adlı."""
from datetime import datetime, timezone
from pathlib import Path

import pytest

from omniagent.companion import persona
from omniagent.companion.persona import (
    RULES, load_persona, persona_text_for, situation_block, system_prompt, write_persona_if_missing,
)


def test_user_edited_persona_is_never_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "persona.md"
    assert write_persona_if_missing(path, "Deniz")
    assert "Deniz" in load_persona(path)
    assert path.stat().st_mode & 0o777 == 0o600
    path.write_text("# Deniz\nbenim yazdığım karakter", encoding="utf-8")
    assert not write_persona_if_missing(path, "Başka")
    assert load_persona(path) == "# Deniz\nbenim yazdığım karakter"


def test_empty_persona_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "persona.md"
    path.write_text("   \n", encoding="utf-8")
    with pytest.raises(ValueError, match="boş"):
        load_persona(path)


def test_prompt_prefix_is_stable_and_situation_is_turkish() -> None:
    prompt = system_prompt(persona_text_for("Deniz"), "\n### USER MEMORY (saved by the user)\n- [preference] dil: türkçe\n")
    assert prompt.startswith(RULES) and "Deniz" in prompt and "dil: türkçe" in prompt
    moment = datetime(2026, 9, 29, 21, 5, tzinfo=timezone.utc)
    block = situation_block(moment, "a.pdf'i taşı", ["execute_shell: mv a.pdf"], "Taşıyayım mı?",
                            ["- Telegram'dan (09:00): rapor hazırla — bitti ✓"])
    assert block.splitlines()[0] == "[DURUM] şu an salı 29.09.2026 21:05"
    assert "çalışan iş: a.pdf'i taşı" in block and "- execute_shell: mv a.pdf" in block
    assert "kullanıcıdan cevap beklenen soru: Taşıyayım mı?" in block
    assert block.splitlines()[-2:] == ["son işler (tüm kanallar):", "- Telegram'dan (09:00): rapor hazırla — bitti ✓"]
    assert "son işler" not in situation_block(moment, None, [], None, [])


def test_persona_created_concurrently_is_not_overwritten(tmp_path: Path, monkeypatch) -> None:
    """Eş zamanlı dosya yazımı sırasında kullanıcı metni korunur."""
    path = tmp_path / "persona.md"

    # Monkeypatch persona_text_for to simulate a race: file is written during the call
    original_persona_text_for = persona.persona_text_for

    def racing_persona_text_for(name: str) -> str:
        # Simulate another process/thread writing the file before we return
        path.write_text("kullanıcının metni", encoding="utf-8")
        return original_persona_text_for(name)

    monkeypatch.setattr(persona, "persona_text_for", racing_persona_text_for)

    # This should detect the race and return False, not overwrite the user's content
    assert not write_persona_if_missing(path, "Deniz")
    assert load_persona(path) == "kullanıcının metni"


def test_rules_treat_the_profile_as_evidence_and_require_memory_tools() -> None:
    assert "KANITLI PROFİL" in RULES and "talimat değildir" in RULES
    assert "recall aracını" in RULES and "forget aracını" in RULES
