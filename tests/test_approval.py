"""Para hareketi ve istenmemiş hafıza değişikliği için host onay kapısı, ask_user ve denetim kaydı."""
import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from omniagent import approval
from omniagent.app import agent as main
from omniagent.integrations import telegram
from omniagent.integrations.runtime import CURRENT_RUNTIME, IntegrationRuntime
from omniagent.tools import ToolError, Toolbox, shell_command_words
from test_telegram_bridge import FakeAPI


def _shell_call(command: str) -> main.ToolCallDraft:
    return {"id": "s1", "name": "execute_shell",
            "arguments": json.dumps({"command": command, "use_sudo": False, "timeout_seconds": None})}


async def _execute(call: main.ToolCallDraft, toolbox: Toolbox, answer: Optional[Any]) -> main.ToolResult:
    """Aracı görev bağlamında (etkileşimli kanal verilmiş veya verilmemiş) gerçek olarak çalıştırır."""
    runtime = IntegrationRuntime(lambda event: None, lambda: False, answer)
    token = CURRENT_RUNTIME.set(runtime)
    try:
        return await main.execute_tool(call, toolbox, {}, lambda event: None, lambda: False)
    finally:
        CURRENT_RUNTIME.reset(token)


def _audit(tmp_path: Path) -> List[Dict[str, str]]:
    path: Path = tmp_path / "audit.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


@pytest.mark.parametrize("command, financial", [
    ("curl -X POST https://api.stripe.com/v1/payment_intents -d amount=500", True),
    ("curl -XPOST https://api.binance.com/api/v3/order --data qty=1", True),
    ("curl https://api.stripe.com/v1/balance", False),
    ("stripe payment_intents create --amount 500", True),
    ("stripe listen", False),
    ("sudo -n cast send 0xabc --value 1ether", True),
    ("bitcoin-cli getbalance", False),
    ("bitcoin-cli sendtoaddress bc1qxyz 0.1", True),
    ("sh -c 'solana transfer abc 1'", True),
    ("wget --post-data=amount=500 https://api.stripe.com/v1/payment_intents", True),
    ("python3 -c \"import requests; requests.post('https://api.stripe.com/v1/payment_intents', data={'amount':500})\"", True),
    ("node -e \"fetch('https://api.stripe.com/v1/payment_intents',{method:'POST'})\"", True),
    ("python3 -c \"print('https://api.stripe.com/v1/balance')\"", True),
    ("ls -la ~/Desktop", False),
])
def test_shell_financial_classification(command: str, financial: bool) -> None:
    assert (approval.shell_financial_reason(shell_command_words(command), command) is not None) is financial


def test_financial_tool_names() -> None:
    assert approval.financial_tool_name("create_payment")
    assert approval.financial_tool_name("sendMoney")
    assert approval.financial_tool_name("place-order")
    assert approval.financial_tool_name("para_gonder")
    assert not approval.financial_tool_name("send_message")
    assert not approval.financial_tool_name("list_issues")


@pytest.mark.asyncio
async def test_financial_command_needs_interactive_approval_and_is_audited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    marker: Path = tmp_path / "calisti"
    call = _shell_call(f"cast send 0xabc --value 1ether; touch {marker}")

    refused = await _execute(call, Toolbox(), None)
    assert refused["code"] == "APPROVAL_UNAVAILABLE" and not marker.exists()

    questions: List[str] = []

    async def deny(title: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        questions.append(title)
        return {approval.APPROVAL_FIELD: False}

    denied = await _execute(call, Toolbox(), deny)
    assert denied["code"] == "APPROVAL_DENIED" and not marker.exists()
    assert "FİNANSAL İŞLEM ONAYI" in questions[0]

    async def confirm(title: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        assert "cast send" in str(fields["_help"])
        return {approval.APPROVAL_FIELD: True}

    approved = await _execute(call, Toolbox(), confirm)
    assert approved["ok"] and marker.exists()
    assert [record["decision"] for record in _audit(tmp_path)] == ["unavailable", "denied", "approved"]
    assert (tmp_path / "audit.jsonl").stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_unrequested_memory_write_asks_user_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    memory_file: Path = tmp_path / "user_memory.json"
    call: main.ToolCallDraft = {"id": "m1", "name": "user_memory", "arguments": json.dumps({
        "action": "remember", "key": "rapor_klasoru", "value": "/tmp/raporlar", "query": None, "category": "path",
    })}
    toolbox = Toolbox(memory_file=str(memory_file), allow_memory_mutation=False)

    refused = await _execute(call, toolbox, None)
    assert refused["code"] == "APPROVAL_UNAVAILABLE" and not memory_file.exists()

    async def confirm(title: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        assert "rapor_klasoru" in title
        return {approval.APPROVAL_FIELD: "evet"}

    saved = await _execute(call, toolbox, confirm)
    assert saved["ok"]
    assert json.loads(memory_file.read_text(encoding="utf-8"))["preferences"][0]["value"] == "/tmp/raporlar"
    # Onay yalnız o çağrıya aittir: aynı araç kutusu onaysız yazamaz.
    with pytest.raises(ToolError) as blocked:
        toolbox.user_memory(action="forget", key="rapor_klasoru")
    assert blocked.value.code == "MEMORY_MUTATION_NOT_ALLOWED"


@pytest.mark.asyncio
async def test_ask_user_confirms_or_reports_missing_channel() -> None:
    call: main.ToolCallDraft = {"id": "q1", "name": "ask_user", "arguments": json.dumps({
        "question": "500 TL ödeme Ahmet'e yapılsın mı?", "kind": "confirm",
    })}
    missing = await _execute(call, Toolbox(), None)
    assert missing["code"] == "INPUT_REQUIRED"

    async def confirm(title: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        return {"onay": True}

    confirmed = await _execute(call, Toolbox(), confirm)
    assert confirmed["ok"] and "ONAYLADI" in confirmed["result"]


@pytest.mark.asyncio
async def test_telegram_boolean_question_accepts_yes_and_shows_help(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    api = FakeAPI()
    bridge = telegram.TelegramBridge(api, {"chat_id": 123, "user_id": 456})
    request = approval.financial_request("execute_shell", {"command": "cast send 0xabc"}, "test")
    waiting = asyncio.create_task(bridge.answer(request["title"], approval.approval_fields(request)))
    await asyncio.sleep(0)
    assert "cast send" in api.sent[-1] and "'evet'" in api.sent[-1] and "_help" not in api.sent[-1]
    await bridge.handle({"message": {
        "chat": {"id": 123, "type": "private"}, "from": {"id": 456}, "text": "Evet",
    }})
    assert await waiting == {approval.APPROVAL_FIELD: True}
