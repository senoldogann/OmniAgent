"""
OmniAgent Checkpoint Entegrasyon Testleri (test_checkpoint_integration.py)
main.run_agent_with_callback döngüsünde checkpoint kaydı, kurtarma ve temizliği doğrular.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List
import pytest

from omniagent.core.checkpoint import find_latest_checkpoint, load_checkpoint, save_checkpoint
from omniagent.core.events import AgentEvent
from omniagent.app import agent as main
from omniagent.tools import Toolbox


def test_resume_intent_requires_an_actual_continue_command() -> None:
    assert main.is_resume_goal("Kaldığın yerden devam et lütfen")
    assert main.is_resume_goal("devam")
    assert not main.is_resume_goal("Bu raporu nasıl devam ettireceğimi açıkla")
    assert not main.is_resume_goal("Önceki görev hakkında bilgi ver")
    assert not main.is_resume_goal("continue düğmesinin ne yaptığını anlat")


@pytest.mark.asyncio
async def test_checkpoint_saved_on_turn_and_cleared_on_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Görev sürerken her tur checkpoint kaydedilir; görev başarıyla bitince temizlenir."""
    runs_dir: Path = tmp_path / "runs"
    monkeypatch.setattr("omniagent.core.checkpoint.RUNS_DIR", runs_dir)
    monkeypatch.setattr(main, "save_checkpoint", lambda **kwargs: save_checkpoint(runs_dir=runs_dir, **kwargs))
    monkeypatch.setattr(main, "clear_checkpoint", lambda sid: (runs_dir / f"{sid}.json").unlink(missing_ok=True))

    async def fake_model(clients, messages, schemas, session_id, backend, emit, should_stop):
        return {"content": "İşlem başarıyla tamamlandı.", "tool_calls": [], "finish_reason": "stop", "usage": main.ZERO_USAGE}, backend

    monkeypatch.setattr(main, "_call_model_with_retries", fake_model)
    events: List[AgentEvent] = []

    report = await main.run_agent_with_callback(
        "Sistemi analiz et ve özetle",
        events.append,
        {
            "requested_backend": None,
            "should_stop": lambda: False,
            "state_file": str(tmp_path / "memory.json"),
            "history": [],
        },
        {"opencode": object()},
    )

    assert report["success"] is True
    # Görev başarılı bittiğinde checkpoint temizlenmiş olmalıdır
    assert find_latest_checkpoint(runs_dir=runs_dir) is None


@pytest.mark.asyncio
async def test_resume_goal_loads_latest_checkpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Kullanıcı 'devam et' dediğinde son kontrol noktasından veriler devralınır."""
    runs_dir: Path = tmp_path / "runs"
    monkeypatch.setattr("omniagent.core.checkpoint.RUNS_DIR", runs_dir)
    monkeypatch.setattr(main, "find_latest_checkpoint", lambda: find_latest_checkpoint(runs_dir=runs_dir))

    # Önce yarım kalmış bir checkpoint simüle et
    old_session_id = "test-session-prev-123"
    save_checkpoint(
        session_id=old_session_id,
        goal="Kullanıcı rehberini hazırla",
        facts={"adımlar": "1 ve 2 tamamlandı", "hedef_dosya": "rehber.txt"},
        completed_steps=["Dosya kontrol edildi", "Giriş yazıldı"],
        turn_count=2,
        runs_dir=runs_dir,
    )

    seen_messages: List[List[Dict[str, Any]]] = []

    async def fake_model(clients, messages, schemas, session_id, backend, emit, should_stop):
        seen_messages.append(list(messages))
        return {"content": "Devam edildi ve tamamlandı.", "tool_calls": [], "finish_reason": "stop", "usage": main.ZERO_USAGE}, backend

    monkeypatch.setattr(main, "_call_model_with_retries", fake_model)
    events: List[AgentEvent] = []

    report = await main.run_agent_with_callback(
        "Kaldığın yerden devam et lütfen",
        events.append,
        {
            "requested_backend": None,
            "should_stop": lambda: False,
            "state_file": str(tmp_path / "memory.json"),
            "history": [],
        },
        {"opencode": object()},
    )

    assert report["success"] is True
    assert len(seen_messages) > 0
    first_turn_user_msg = seen_messages[0][-1]
    assert first_turn_user_msg["role"] == "user"
    assert "### Önceki Oturum Kontrol Noktası" in first_turn_user_msg["content"]
    assert "hedef_dosya: rehber.txt" in first_turn_user_msg["content"]
