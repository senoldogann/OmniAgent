"""ImsgClient: sahte `imsg rpc` alt süreciyle gerçek JSON-RPC yolu (başlatma, sayfalama, taşma, gönderim, çökme)."""
import json
import sys
from contextlib import aclosing
from pathlib import Path
from typing import Dict, List

import pytest

from omniagent.integrations.imsg import (
    DeliveryUnknown, ImsgClient, ImsgError, ImsgProcessError, ImsgRpcError, ImsgUnavailable, parse_message,
)

FAKE = Path(__file__).parent / "fixtures" / "fake_imsg.py"
USER = "+905551112233"


def raw_message(rowid: int, text: str) -> Dict[str, object]:
    return {"id": rowid, "guid": f"g{rowid}", "chat_id": 7, "sender": USER, "participants": [USER],
            "is_group": False, "is_from_me": False, "text": text, "created_at": "2026-09-29T12:00:00Z",
            "attachments": []}


def client_for(tmp_path: Path, scenario: Dict[str, object]) -> ImsgClient:
    path = tmp_path / "scenario.json"
    base: Dict[str, object] = {"log_path": str(tmp_path / "requests.jsonl"), "status": {"database": {"ready": True}}}
    path.write_text(json.dumps({**base, **scenario}), encoding="utf-8")
    return ImsgClient([sys.executable, str(FAKE), str(path)])


def requests_sent(tmp_path: Path) -> List[Dict[str, object]]:
    lines = (tmp_path / "requests.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


@pytest.mark.asyncio
async def test_start_rejects_unreadable_database(tmp_path: Path) -> None:
    client = client_for(tmp_path, {"status": {"database": {"ready": False, "error": "authorization denied"}}})
    try:
        with pytest.raises(ImsgUnavailable, match="Full Disk Access"):
            await client.start()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_catch_up_pages_until_done(tmp_path: Path) -> None:
    client = client_for(tmp_path, {"after_pages": [
        {"messages": [raw_message(11, "a")], "next_rowid": 11, "has_more": True},
        {"messages": [raw_message(12, "b")], "next_rowid": 20, "has_more": False},
    ]})
    await client.start()
    try:
        messages, cursor = await client.catch_up(10)
    finally:
        await client.close()
    assert [message["text"] for message in messages] == ["a", "b"] and cursor == 20
    pages = [request for request in requests_sent(tmp_path) if request["method"] == "messages.after"]
    assert [request["params"]["since_rowid"] for request in pages] == [10, 11]


@pytest.mark.asyncio
async def test_subscribe_recovers_from_overflow(tmp_path: Path) -> None:
    client = client_for(tmp_path, {
        "subscribe_batches": [
            [{"method": "message", "params": {"message": raw_message(31, "ilk")}},
             {"method": "watch.overflow", "params": {"resume_after_rowid": 31, "reason": "buffer_limit_exceeded",
                                                        "terminal": True}}],
            [{"method": "message", "params": {"message": raw_message(33, "canlı")}}],
        ],
        "after_pages": [{"messages": [raw_message(32, "kaçan")], "next_rowid": 32, "has_more": False}],
    })
    await client.start()
    received: List[str] = []
    try:
        async with aclosing(client.subscribe(30)) as stream:
            async for message in stream:
                received.append(message["text"])
                if len(received) == 3:
                    break
    finally:
        await client.close()
    assert received == ["ilk", "kaçan", "canlı"]
    calls = [(request["method"], request["params"].get("since_rowid")) for request in requests_sent(tmp_path)]
    assert calls == [("initialize", None), ("watch.subscribe", 30), ("messages.after", 31), ("watch.subscribe", 32)]


@pytest.mark.asyncio
async def test_send_errors_are_never_retried(tmp_path: Path) -> None:
    client = client_for(tmp_path, {"send_errors": [None, -32001, -32004]})
    await client.start()
    try:
        result = await client.send_text(USER, "selam")
        assert result["ok"] and result["guid"] == "sent-2"
        with pytest.raises(DeliveryUnknown):
            await client.send_text(USER, "ikinci")
        with pytest.raises(DeliveryUnknown):
            await client.send_text(USER, "üçüncü")
        with pytest.raises(ImsgProcessError):
            await client.send_text(USER, "dördüncü")
    finally:
        await client.close()
    sends = [request["params"] for request in requests_sent(tmp_path) if request["method"] == "send"]
    assert [params["text"] for params in sends] == ["selam", "ikinci", "üçüncü"]
    assert all(params["service"] == "imessage" and params["allow_sms_fallback"] is False for params in sends)


def test_rpc_error_names_parameters_but_never_their_values() -> None:
    """Hata metni stderr günlüğüne ve zincirli izlere düşer: alıcı, mesaj metni ve dosya yolu yazılmamalı."""
    error = ImsgRpcError("send", -32001, "teslim bilinmiyor",
                         {"to": "+905551112233", "text": "gizli içerik", "file": "/Users/dogan/gizli.pdf"})
    shown = str(error)
    assert "gizli içerik" not in shown and "+905551112233" not in shown and "gizli.pdf" not in shown
    assert all(name in shown for name in ("text", "to", "file")) and "teslim bilinmiyor" in shown
    assert error.method == "send" and error.code == -32001


@pytest.mark.asyncio
async def test_send_failures_never_carry_message_content(tmp_path: Path) -> None:
    """Gerçek istemci yolunda da (DeliveryUnknown kopyası ve zincirli neden dahil) içerik ve alıcı sızmaz."""
    client = client_for(tmp_path, {"send_errors": [-32001, -32602]})
    await client.start()
    try:
        with pytest.raises(DeliveryUnknown) as unknown:
            await client.send_text(USER, "gizli içerik bir")
        with pytest.raises(ImsgRpcError) as rejected:
            await client.send_text(USER, "gizli içerik iki")
    finally:
        await client.close()
    for failure in (unknown.value, unknown.value.__cause__, rejected.value):
        assert "gizli içerik" not in str(failure) and USER not in str(failure)
    assert rejected.value.code == -32602 and rejected.value.method == "send"


@pytest.mark.asyncio
async def test_process_exit_fails_pending_and_later_requests(tmp_path: Path) -> None:
    client = client_for(tmp_path, {"exit_after_requests": 1})
    await client.start()
    try:
        with pytest.raises(ImsgProcessError, match="çıkış kodu 3"):
            await client.send_text(USER, "selam")
        with pytest.raises(ImsgProcessError):
            await client.catch_up(0)
    finally:
        await client.close()


def test_parse_message_keeps_disk_attachments_only() -> None:
    message = parse_message({
        "id": 5, "guid": "g5", "chat_id": 1, "is_from_me": False, "created_at": "2026-09-29T12:00:00Z",
        "attachments": [
            {"original_path": "/a.heic", "converted_path": "/a.jpg", "mime_type": "image/heic",
             "converted_mime_type": "image/jpeg"},
            {"original_path": "/b.png", "mime_type": "image/png", "missing": True},
        ],
    })
    assert message["sender"] == "" and message["text"] == "" and message["is_group"] is False
    assert message["attachments"] == [{"path": "/a.jpg", "mime_type": "image/jpeg"}]


@pytest.mark.asyncio
async def test_subscribe_terminal_unknown_notification(tmp_path: Path) -> None:
    client = client_for(tmp_path, {
        "subscribe_batches": [[{"method": "watch.error", "params": {"terminal": True, "reason": "watcher failed"}}]],
    })
    await client.start()
    try:
        with pytest.raises(ImsgError, match="watch.error"):
            async with aclosing(client.subscribe(0)) as stream:
                async for _message in stream:
                    pass
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_subscribe_non_terminal_unknown_then_message(tmp_path: Path) -> None:
    client = client_for(tmp_path, {
        "subscribe_batches": [
            [{"method": "watch.progress", "params": {}},
             {"method": "message", "params": {"message": raw_message(5, "sonra")}}],
        ],
    })
    await client.start()
    received: List[str] = []
    try:
        async with aclosing(client.subscribe(0)) as stream:
            async for message in stream:
                received.append(message["text"])
                if len(received) == 1:
                    break
    finally:
        await client.close()
    assert received == ["sonra"]
