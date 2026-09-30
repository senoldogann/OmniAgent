"""iMessage kurulumu: launchd kaydı, eşleştirme kodu kuralı, servis içinde eşleştirme (gerçek istemci + sahte imsg),
Tam Disk Erişimi durumu."""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List

import pytest

from omniagent.integrations import imessage_setup
from omniagent.integrations.imessage_settings import (
    DraftSettings, ImessageConfigError, load_settings, save_pairing, save_settings,
)
from omniagent.integrations.imsg import ImsgUnavailable, IncomingMessage
from omniagent.memory.personal import PersonalStore, utc_iso
from omniagent.platform.macos.permissions import messages_database_status

FAKE = Path(__file__).parent / "fixtures" / "fake_imsg.py"
USER = "+905551112233"


def draft() -> DraftSettings:
    return {"persona_name": "Deniz", "chat_backend": "openai", "memory_backend": "opencode",
            "quiet_hours": {"start": "23:30", "end": "09:00"}, "burst_quiet_seconds": 2.0,
            "gui_idle_seconds": 180, "heartbeat_minutes": {"base": 30, "jitter": 10, "min": 20, "max": 240}}


def code_message(rowid: int, text: str, sender: str, created_at: datetime) -> Dict[str, object]:
    return {"id": rowid, "guid": f"g{rowid}", "chat_id": 3, "sender": sender, "participants": [sender],
            "is_group": False, "is_from_me": False, "text": text, "created_at": utc_iso(created_at),
            "attachments": []}


def write_scenario(tmp_path: Path, scenario: Dict[str, object]) -> List[str]:
    path = tmp_path / "scenario.json"
    base: Dict[str, object] = {"log_path": str(tmp_path / "requests.jsonl"), "status": {"database": {"ready": True}}}
    path.write_text(json.dumps({**base, **scenario}), encoding="utf-8")
    return [sys.executable, str(FAKE), str(path)]


def test_launchd_record_runs_bridge_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    record = imessage_setup.build_launchd_record()
    assert record["Label"] == "com.omniagent.imessage" and record["KeepAlive"] is True
    assert record["ProgramArguments"] == [sys.executable, "-m", "omniagent.integrations.imessage", "run"]
    assert record["StandardErrorPath"] == str(tmp_path / "imessage-stderr.log")


def test_pairing_handle_accepts_only_live_code_in_direct_chat() -> None:
    now = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
    request = imessage_setup.new_pairing(draft(), "042917", now + timedelta(seconds=180))
    message: IncomingMessage = {"rowid": 1, "guid": "g1", "chat_id": 3, "sender": "+90 555 111 22 33",
                                "participants": [USER], "is_from_me": False, "is_group": False, "text": " 042917 ",
                                "created_at": "2026-09-29T12:00:00Z", "attachments": []}
    assert imessage_setup.pairing_handle(message, request, now) == USER
    assert imessage_setup.pairing_handle({**message, "text": "042918"}, request, now) is None
    assert imessage_setup.pairing_handle({**message, "is_group": True}, request, now) is None
    assert imessage_setup.pairing_handle(message, request, now + timedelta(seconds=181)) is None
    # Kod gösterilmeden önce yazılmış eski mesaj (abonelik tüm geçmişi yeniden oynatır) yeni kodla eşleşse de kabul edilmez.
    assert imessage_setup.pairing_handle({**message, "created_at": "2026-09-29T11:59:59Z"}, request, now) is None
    assert imessage_setup.pairing_handle({**message, "created_at": "2026-09-29T12:00:01+00:00"}, request, now) == USER


@pytest.mark.asyncio
async def test_service_pairs_code_sender_and_greets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    now = datetime.now(timezone.utc)
    stranger = "+90 555 999 88 77"
    command = write_scenario(tmp_path, {"subscribe_batches": [[
        # Yeni kodun metniyle eşleşen ama günler önce yazılmış eski mesaj: göndericisi eşleştirilmemeli.
        {"method": "message", "params": {"message": code_message(39, "042917", stranger, now - timedelta(days=2))}},
        {"method": "message", "params": {"message": code_message(40, "999999", USER, now + timedelta(seconds=4))}},
        {"method": "message", "params": {"message": code_message(41, "042917", USER, now + timedelta(seconds=5))}},
    ]]})
    monkeypatch.setattr(imessage_setup, "imsg_command", lambda: command)
    save_pairing(tmp_path / "imessage-pairing.json",
                 imessage_setup.new_pairing(draft(), "042917", now + timedelta(seconds=180)))
    store = PersonalStore(tmp_path / "companion.db")
    try:
        settings = await imessage_setup.pair_from_service(store)
        assert settings["handle"] == USER
        assert load_settings(tmp_path / "imessage.json", ("openai", "opencode"))["handle"] == USER
        assert not (tmp_path / "imessage-pairing.json").exists()
        assert store.get_state(imessage_setup.PAIRING_STATUS_KEY) == "paired" and store.cursor() == 41
    finally:
        store.close()
    requests = [json.loads(line) for line in (tmp_path / "requests.jsonl").read_text(encoding="utf-8").splitlines()]
    sends = [request["params"] for request in requests if request["method"] == "send"]
    assert len(sends) == 1 and sends[0]["to"] == USER and sends[0]["text"].startswith("eşleştik")


@pytest.mark.asyncio
async def test_service_reports_unreadable_database_to_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    command = write_scenario(tmp_path, {"status": {"database": {"ready": False, "error": "authorization denied"}}})
    monkeypatch.setattr(imessage_setup, "imsg_command", lambda: command)
    save_pairing(tmp_path / "imessage-pairing.json",
                 imessage_setup.new_pairing(draft(), "042917", datetime.now(timezone.utc) + timedelta(seconds=180)))
    store = PersonalStore(tmp_path / "companion.db")
    try:
        with pytest.raises(ImsgUnavailable):
            await imessage_setup.pair_from_service(store)
        assert (store.get_state(imessage_setup.PAIRING_STATUS_KEY) or "").startswith("error:")
    finally:
        store.close()


def test_install_service_refuses_to_loop_without_pairing_or_pairing_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Eşleşme de eşleştirme isteği de yokken KeepAlive servis kurulmaz (sonsuz yeniden başlatma döngüsü)."""
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    installed: List[str] = []
    monkeypatch.setattr(imessage_setup.launch_agent, "install", lambda *arguments: installed.append("kuruldu"))
    with pytest.raises(ImessageConfigError, match="omniagent-imessage setup"):
        imessage_setup.install_service()
    assert installed == []
    save_pairing(tmp_path / "imessage-pairing.json",
                 imessage_setup.new_pairing(draft(), "042917", datetime.now(timezone.utc) + timedelta(seconds=180)))
    imessage_setup.install_service()
    assert installed == ["kuruldu"]
    (tmp_path / "imessage-pairing.json").unlink()
    save_settings(tmp_path / "imessage.json", imessage_setup.paired_settings(draft(), USER))
    imessage_setup.install_service()
    assert installed == ["kuruldu", "kuruldu"]


def test_messages_database_status(tmp_path: Path) -> None:
    database = tmp_path / "chat.db"
    assert "yok" in messages_database_status(database)
    database.write_bytes(b"SQLite format 3")
    assert messages_database_status(database) == "izinli"
    database.chmod(0)
    try:
        assert "İZİN YOK" in messages_database_status(database)
    finally:
        database.chmod(0o600)
