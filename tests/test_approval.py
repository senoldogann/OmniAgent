"""Para hareketi ve istenmemiş hafıza değişikliği için host onay kapısı, ask_user ve denetim kaydı."""
import asyncio
import base64
import json
import os
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from omniagent import approval
from omniagent.app import agent as main
from omniagent.core import observation_filter
from omniagent.integrations import telegram
from omniagent.integrations.runtime import CURRENT_RUNTIME, IntegrationRuntime, data_root
from omniagent.paths import workspace_dir
from omniagent.tools import ToolError, Toolbox, shell_command_words
from omniagent.tools.system import approval_gate_blocking
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
    # Hedef adresi değişken/komut ikamesinden geliyor: literal eşleşme yok ama veri gönderiliyor.
    ("curl -X POST $PAYMENT_URL -d amount=500", True),
    ("curl \"$(cat url.txt)\" -d amount=500", True),
    ("curl $SOME_URL", False),  # değişken var ama veri gönderimi yok, tetiklenmemeli
    (
        "bash -c \"$(echo "
        + base64.b64encode(b"curl -X POST https://api.stripe.com/v1/charges -d amount=100").decode()
        + " | base64 -d)\"",
        True,
    ),
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


CTA_POSITIVE: List[str] = [
    "Pay", "Pay now", "Pay $49.99", "Pay with Apple Pay", "Pay securely", "Buy now", "Buy it now", "Buy with Apple Pay",
    "Place order", "Place your order", "Complete purchase", "Complete your purchase", "Confirm payment",
    "Confirm & Pay", "Confirm order", "Donate", "Donate $25", "Withdraw funds", "Send money", "Transfer money",
    "Make a payment", "Check out", "Secure checkout", "Checkout", "Transfer", "Deposit funds", "Place buy order",
    "Öde", "ÖDE", "Şimdi öde", "Öde 249,90 TL", "Ödemeyi tamamla", "ÖDEMEYİ TAMAMLA", "Ödemeyi onayla",
    "Ödeme yap", "Satın al", "SATIN AL", "Hemen satın al", "Hemen al", "Siparişi onayla", "Siparişi tamamla",
    "Sipariş ver", "Bağış yap", "Bağış Yap 100 TL", "Havale", "Havale yap", "Para gönder", "Para çek",
    ">> Pay now", "Place ord3r", "0demeyi onayla", "Odemeyi tamamia",
    "Maksa", "Maksa nyt", "Osta nyt", "Lahjoita", "Vahvista tilaus", "Vahvista maksu", "Tee tilaus", "Suorita maksu",
    "Viimeistele ostos", "Lähetä rahaa", "Siirrä rahaa", "Nosta rahaa", "Tee lahjoitus", "Osta",
]
CTA_NEGATIVE: List[str] = [
    "Payments", "Payment methods", "Add payment method", "Pay attention", "Pay & Benefits", "Purchase history",
    "Purchase order", "Buy again", "Order status", "Cancel order", "Proceed to checkout", "Go to checkout",
    "Checkout with PayPal", "Add to cart", "Confirm", "Send", "Send message", "Submit", "Subscribe", "Upgrade",
    "Transfer history", "Transfers", "Deposit", "Donations", "Payment received", "Thank you for your purchase",
    "Ödeme", "Ödeme yöntemleri", "Ödemeyi iptal et", "Satın alma geçmişi", "Siparişlerim", "Siparişi iptal et",
    "Sepete ekle", "Sepeti onayla", "Onayla", "Gönder", "Tamam", "Bağışlar", "Havale / EFT", "Lähetä viesti",
    "Ayrıntıları göster", "Detayı aç", "$49.99", "",
    "Maksutavat", "Ostoskori", "Ostohistoria", "Osta uudelleen", "Maksuhistoria", "Tilaukset",
]


@pytest.mark.parametrize("label", CTA_POSITIVE)
def test_payment_button_labels_need_approval(label: str) -> None:
    assert approval.financial_cta_reason(label) is not None


@pytest.mark.parametrize("label", CTA_NEGATIVE)
def test_navigation_and_generic_labels_do_not_need_approval(label: str) -> None:
    assert approval.financial_cta_reason(label) is None


def test_cta_lead_words_stay_within_financial_name_tokens() -> None:
    """Ortak (EN/TR) lead fiilleri araç adı jetonlarının alt kümesidir; Fince lead sözcükleri yalnız etikete özgüdür."""
    assert approval._CTA_LEAD_WORDS_COMMON <= approval._FINANCIAL_NAME_TOKENS
    assert approval._CTA_LEAD_WORDS == approval._CTA_LEAD_WORDS_COMMON | approval._CTA_LEAD_WORDS_FI


TRANSFER_SCREEN: List[str] = ["Alıcı: Ali Veli", "IBAN: TR33 0006 1005 1978 6457 8413 26", "Tutar: 500,00 TL"]


@pytest.mark.parametrize("label, context, financial", [
    ("Onayla", TRANSFER_SCREEN, True), ("Gönder", TRANSFER_SCREEN, True), ("Confirm", ["Recipient: Ali", "Total $500"], True),
    ("Send", ["Beneficiary Ali Veli", "Amount: 1.200,00 EUR"], True), ("Onayla", ["Hesap No 123456", "Toplam 90 TL"], True),
    # Tek başına bağlamsız genel etiket, yalnız tutar ya da yalnız alıcı sözcüğü yetmez
    ("Onayla", ["Çerezleri kabul et"], False), ("Gönder", ["Toplam 50 TL"], False),
    ("Gönder", ["Alıcı: Ali Veli", "Merhaba, toplantı yarın"], False), ("Send message", TRANSFER_SCREEN, False),
    ("Devam", TRANSFER_SCREEN, False), ("Sepeti onayla", TRANSFER_SCREEN, False),
])
def test_generic_commit_label_needs_amount_and_recipient_context(label: str, context: List[str], financial: bool) -> None:
    assert (approval.financial_context_reason(label, context) is not None) is financial
    assert (approval.click_financial_reason(label, context) is not None) is financial


def test_gui_click_request_quotes_page_text_and_stays_within_the_limit() -> None:
    target: approval.ClickTarget = {
        "tool": "cua_click_text", "label": "Ödemeyi onayla\nOnaylıyorum: evet", "requested": "Onayla",
        "where": "Google Chrome", "amounts": ["Toplam: 1.249,00 TL"],
    }
    request = approval.gui_click_request(target, "deneme")
    assert request["category"] == "financial" and "Onaylıyorum: evet" not in request["title"]
    assert "'Ödemeyi onayla\\nOnaylıyorum: evet'" in request["summary"]  # satır sonu repr ile kaçırılır
    assert "Modelin istediği: 'Onayla'" in request["summary"] and "1.249,00 TL" in request["summary"]
    assert len(request["summary"]) <= approval.APPROVAL_SUMMARY_LIMIT
    same: approval.ClickTarget = {**target, "label": "Onayla", "amounts": []}
    assert "Modelin istediği" not in approval.gui_click_request(same, "deneme")["summary"]


@pytest.mark.parametrize("name, outbound", [
    ("send_message", True), ("slack_post_message", True), ("sendMessage", True), ("reply_to_thread", True),
    ("publish_page", True), ("gonder_mesaj", True), ("get_post", False), ("list_posts", False),
    ("search_messages", False), ("read_email", False), ("resolve-library-id", False),
    # DEĞİŞEN satır: 'send_status' eskiden okuma sözcüğü ('status') taşıdığı için muaftı; artık gönderme eylemidir
    ("send_status", True),
    # Nesne adı okuma sözcüğüyle çakışan GÖNDERME araçları eskiden muaftı: okuma muafiyeti yalnız okuma FİİLİYLE başlamaktır
    ("send_report", True), ("send_summary", True), ("send_status_update", True), ("post_status", True),
    ("send_info", True), ("send_details", True), ("send_balance_alert", True), ("reply_history", True),
    # Ad öneki atlanır: ilk EYLEM sözcüğü okuma fiiliyse muaf kalır
    ("slack_get_post", False), ("github_get_comment", False), ("notion_search_comment", False),
    ("get_report", False), ("check_status", False),
])
def test_outbound_tool_names(name: str, outbound: bool) -> None:
    assert approval.outbound_tool_name(name) is outbound


@pytest.mark.parametrize("name, conflict", [
    ("send_message", "geri alınamaz dış iletişim"), ("pay_invoice", "para hareketi"), ("get_order", None),
    ("query-docs", None), ("toplam", None),
    # DEĞİŞEN satır: 'transfer_status' eskiden okuma sözcüğü taşıdığı için çakışma sayılmazdı; artık belirsiz ad onay ister
    # (kabul edilen yanlış pozitif: gerçekten salt okunur ise kataloğa yazmak yerine araç onaydan geçer)
    ("transfer_status", "para hareketi"),
    # Nesne adı okuma sözcüğü olan EYLEM araçları katalogda salt okunur işaretlenemez
    ("withdraw_balance", "para hareketi"), ("transfer_balance", "para hareketi"), ("pay_invoice_summary", "para hareketi"),
    ("send_payment_details", "para hareketi"), ("send_report", "geri alınamaz dış iletişim"),
    ("post_status", "geri alınamaz dış iletişim"), ("send_status_update", "geri alınamaz dış iletişim"),
    # Okuma fiiliyle başlayan (ad öneki dahil) araçlar salt okunur işaretlenebilir
    ("stripe_list_payment_intents", None), ("get_transfer_history", None), ("search_payments", None),
    ("check_balance", None), ("show_invoice", None), ("describe_order", None), ("slack_get_post", None),
])
def test_readonly_conflict(name: str, conflict: Optional[str]) -> None:
    assert approval.readonly_conflict(name) == conflict


def test_summary_keeps_recipient_first_and_command_tail() -> None:
    mail = approval.call_summary("mcp_send", {"body": "x" * 2000, "to": "ali@example.com"})
    assert "ali@example.com" in mail[:80] and len(mail) <= approval.APPROVAL_SUMMARY_LIMIT
    command = "cast send 0xabc " + "y" * 900 + " --rpc-url https://hedef.example"
    shell = approval.call_summary("execute_shell", {"command": command})
    assert "cast send" in shell and "hedef.example" in shell and len(shell) <= approval.APPROVAL_SUMMARY_LIMIT


def test_dynamic_outbound_tool_needs_approval_unless_readonly() -> None:
    async def execute(**arguments: object) -> object:
        return None

    entry = {"schema": {}, "execute": execute, "readonly": False, "capability": "demo", "label": "send_message"}
    request = main.approval_request_for_call("mcp_demo", {"to": "ali@example.com", "body": "merhaba"}, entry, False)
    assert request is not None and request["category"] == "communication"
    assert main.approval_request_for_call("mcp_demo", {}, {**entry, "readonly": True}, False) is None
    assert main.approval_request_for_call("mcp_demo", {}, {**entry, "label": "get_message"}, False) is None
    finance = main.approval_request_for_call("mcp_demo", {}, {**entry, "label": "pay_invoice", "financial": True}, False)
    assert finance is not None and finance["category"] == "financial"
    # Nesne adı okuma sözcüğü olan gönderme araçları da onay ister (eskiden 'report'/'status' adı onayı atlatıyordu)
    for label in ("send_report", "post_status", "send_status_update"):
        report = main.approval_request_for_call("mcp_demo", {"to": "ali@example.com"}, {**entry, "label": label}, False)
        assert report is not None and report["category"] == "communication", label
    assert main.approval_request_for_call("mcp_demo", {}, {**entry, "label": "get_report"}, False) is None


KNOWN_CARDS: List[str] = [
    "4242424242424242", "4111111111111111", "5555555555554444", "2223003122003222", "378282246310005",
    "6011111111111117", "30569309025904", "3566002020360505", "6200000000000005",
]


def _luhn_completed(prefix: str, length: int) -> str:
    """Önek + dolgu rakamları + Luhn sağlama hanesi (test için geçerli numara üretir)."""
    body = prefix + "0" * (length - len(prefix) - 1)
    for check in "0123456789":
        if approval.luhn_valid(body + check):
            return body + check
    raise AssertionError("Luhn hanesi bulunamadı")


@pytest.mark.parametrize("number", KNOWN_CARDS + [_luhn_completed("9792", 16)])
def test_card_numbers_are_recognised_and_masked(number: str) -> None:
    grouped = " ".join(number[i:i + 4] for i in range(0, len(number), 4))
    for text in (number, grouped, "-".join(number[i:i + 4] for i in range(0, len(number), 4)), f"kart no: {grouped} lütfen"):
        assert approval.card_number_in_text(text) == f"•••• {number[-4:]}"
    assert approval.card_number_in_text(f"{grouped} 123") == f"•••• {number[-4:]}"  # CVC ile aynı metinde


@pytest.mark.parametrize("text", [
    "4242424242424241",                       # Luhn tutmaz
    "1234567890123456", "0000000000000000",   # ağ ön eki yok
    "TR33 0006 1005 1978 6457 8413 26",       # IBAN
    "+90 532 123 45 67", "20260929123456", "1759142400000", "12345", "Merhaba dünya", "",
])
def test_non_card_digit_strings_are_not_recognised(text: str) -> None:
    assert approval.card_number_in_text(text) is None


def test_typed_card_number_needs_approval_and_is_never_logged_in_full() -> None:
    number = "4242 4242 4242 4242"
    calls: List[tuple] = [
        ("cua_type_text", {"text": number}), ("cua_fill_field", {"point": [1, 2], "text": number}),
        ("cua_submit_text", {"point": [1, 2], "text": number}),
        ("cua_set_text_element", {"snapshot": "s1", "index": 3, "text": number}),
        ("run_action_sequence", {"steps": [{"action": "click", "point": [1, 2]}, {"action": "type", "text": number}]}),
        ("run_action_sequence", {"steps": json.dumps([{"action": "type", "text": number}])}),
        ("browse_url", {"url": None, "actions": [{"action": "fill", "selector": "#kart", "value": number}]}),
    ]
    for name, arguments in calls:
        request = main.approval_request_for_call(name, arguments, None, False)
        assert request is not None and request["category"] == "financial", name
        assert "4242 4242" not in request["summary"] and "4242424242424242" not in request["summary"], name
        assert request["summary"].endswith("•••• 4242"), name
    for name, arguments in [
        ("cua_type_text", {"text": "merhaba"}), ("cua_type_text", {"text": "4242424242424241"}),
        ("run_action_sequence", {"steps": [{"action": "press", "key": number}]}),  # yazma değil tuş adı
        ("browse_url", {"url": None, "actions": [{"action": "click", "selector": number, "value": None}]}),
        ("read_file", {"path": number}),
    ]:
        assert main.approval_request_for_call(name, arguments, None, False) is None, name


@pytest.mark.asyncio
async def test_typed_card_number_is_refused_without_channel_and_audited_masked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    typed: List[str] = []
    monkeypatch.setattr(Toolbox, "cua_type_text", lambda self, text: typed.append(text) or "yazıldı")
    call: main.ToolCallDraft = {"id": "k1", "name": "cua_type_text", "arguments": json.dumps({"text": "4242 4242 4242 4242"})}

    refused = await _execute(call, Toolbox(), None)
    assert refused["code"] == "APPROVAL_UNAVAILABLE" and typed == []

    async def confirm(title: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        return {approval.APPROVAL_FIELD: True}

    approved = await _execute(call, Toolbox(), confirm)
    assert approved["ok"] and typed == ["4242 4242 4242 4242"]
    audit_text = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert "4242 4242" not in audit_text and "4242424242424242" not in audit_text and "•••• 4242" in audit_text


def _probe_call() -> main.ToolCallDraft:
    return {"id": "g1", "name": "probe_tool", "arguments": "{}"}


def _probe_runtime(answer: Optional[Any], unattended: bool, stop: List[bool]) -> IntegrationRuntime:
    runtime = IntegrationRuntime(lambda event: None, lambda: bool(stop), answer)
    runtime.unattended = unattended
    return runtime


def _gate_entry(execute: Any) -> Dict[str, Any]:
    return {"schema": {"function": {"parameters": {"type": "object"}}}, "execute": execute,
            "readonly": False, "capability": "test"}


async def _execute_dynamic(runtime: IntegrationRuntime, entry: Dict[str, Any]) -> main.ToolResult:
    runtime.published["probe_tool"] = entry
    token = CURRENT_RUNTIME.set(runtime)
    try:
        return await main.execute_tool(_probe_call(), Toolbox(), {}, lambda event: None, runtime.should_stop)
    finally:
        CURRENT_RUNTIME.reset(token)


@pytest.mark.asyncio
async def test_blocking_gate_bridges_worker_thread_to_the_host_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, manual_approval: None,
) -> None:
    """Gerçek execute_tool: to_thread aracı, olay döngüsündeki require_approval'ı bloklayan köprüyle sorar."""
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    ran: List[str] = []

    def probe() -> str:
        gate = approval_gate_blocking()
        assert gate is not None
        gate(approval.financial_request("probe_tool", {"tutar": "500 TL"}, "deneme"))
        ran.append("çalıştı")
        return "tıklandı"

    async def confirm(title: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        assert "500 TL" in str(fields["_help"])
        return {approval.APPROVAL_FIELD: True}

    async def deny(title: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        return {approval.APPROVAL_FIELD: False}

    stop: List[bool] = []
    refused = await _execute_dynamic(_probe_runtime(None, False, stop), _gate_entry(probe))
    assert refused["code"] == "APPROVAL_UNAVAILABLE" and refused["recoverable"] is False and ran == []
    assert (await _execute_dynamic(_probe_runtime(confirm, True, stop), _gate_entry(probe)))["code"] == "APPROVAL_UNAVAILABLE"
    assert (await _execute_dynamic(_probe_runtime(deny, False, stop), _gate_entry(probe)))["code"] == "APPROVAL_DENIED"
    approved = await _execute_dynamic(_probe_runtime(confirm, False, stop), _gate_entry(probe))
    assert approved["ok"] and ran == ["çalıştı"]
    assert [record["decision"] for record in _audit(tmp_path)] == ["unavailable", "unavailable", "denied", "approved"]


def test_should_auto_approve_uses_the_host_whitelist_and_the_amount_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Otomatik onayın iki kapısı: güvenli host listesi (tutar sınırsız) ve tutar limiti. İkisi de varsayılan
    olarak KAPALI olduğu için hiçbir çağrı kendiliğinden onaysız geçmez.
    """
    assert approval.AUTO_APPROVE_LIMIT == 0.0 and approval.AUTO_APPROVE_HOSTS == frozenset()
    assert not approval.should_auto_approve("https://banka.example/ode", 1.0)

    monkeypatch.setattr(approval, "AUTO_APPROVE_HOSTS", frozenset({"guvenli.example"}))
    monkeypatch.setattr(approval, "AUTO_APPROVE_LIMIT", 100.0)

    assert approval.should_auto_approve("https://www.guvenli.example/odeme", 5_000.0)  # liste: tutar bakılmaz
    assert approval.should_auto_approve("https://baska.example/x", 99.9)             # limit altı
    assert not approval.should_auto_approve("https://baska.example/x", 100.01)       # limit üstü
    assert not approval.should_auto_approve("https://baska.example/x", None)         # tutar yok


def test_financial_request_carries_amount_and_url_but_card_entry_never_does() -> None:
    """Otomatik onay kararı isteğin taşıdığı tutar/adresle verilir; kart numarası girişi bu alanları taşımaz."""
    request: approval.ApprovalRequest = approval.financial_request(
        "transfer_money", {"amount": 250, "url": "https://banka.example/ode"}, "deneme",
    )

    assert request["amount"] == 250.0 and request["url"] == "https://banka.example/ode"
    card: approval.ApprovalRequest = approval.card_entry_request("type_text", "•••• 4242")
    assert "amount" not in card and "url" not in card


@pytest.mark.asyncio
async def test_continuous_mode_auto_approves_and_audits_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Sürekli mod (gözetimsiz): onay sorulmaz, çağrı yürür ve denetim kaydına 'auto_approved' yazılır —
    karar izlenebilir kalır. (Kapatmak için approval.AUTO_APPROVE_IN_CONTINUOUS_MODE = False.)
    """
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    ran: List[str] = []

    def probe() -> str:
        gate = approval_gate_blocking()
        assert gate is not None
        gate(approval.financial_request("probe_tool", {"tutar": "500 TL"}, "deneme"))
        ran.append("çalıştı")
        return "tıklandı"

    stop: List[bool] = []
    result = await _execute_dynamic(_probe_runtime(None, True, stop), _gate_entry(probe))

    assert result["ok"] and ran == ["çalıştı"]
    assert [record["decision"] for record in _audit(tmp_path)] == ["auto_approved"]


@pytest.mark.asyncio
async def test_blocking_gate_times_out_and_stops_while_waiting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(approval, "APPROVAL_TIMEOUT_SECONDS", 0.3)

    def probe() -> str:
        approval_gate_blocking()(approval.financial_request("probe_tool", {}, "deneme"))
        return "çalıştı"

    async def never_answers(title: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        await asyncio.sleep(30)
        return {approval.APPROVAL_FIELD: True}

    stop: List[bool] = []
    timed_out = await _execute_dynamic(_probe_runtime(never_answers, False, stop), _gate_entry(probe))
    assert timed_out["code"] == "APPROVAL_TIMEOUT"

    monkeypatch.setattr(approval, "APPROVAL_TIMEOUT_SECONDS", 30.0)
    runtime = _probe_runtime(never_answers, False, stop)
    asyncio.get_running_loop().call_later(0.4, stop.append, True)
    stopped = await _execute_dynamic(runtime, _gate_entry(probe))
    assert stopped["code"] == "STOPPED" and stopped["recoverable"] is False


@pytest.mark.asyncio
async def test_blocking_gate_refuses_to_deadlock_when_called_on_the_event_loop_thread() -> None:
    async def probe() -> str:
        approval_gate_blocking()(approval.financial_request("probe_tool", {}, "deneme"))
        return "çalıştı"

    result = await _execute_dynamic(_probe_runtime(None, False, []), _gate_entry(probe))
    assert result["error_type"] == "RuntimeError" and "olay döngüsü" in result["error"]


BROWSE_PAGE: str = (
    "<p id='durum'>başlangıç</p><p>Toplam: 249,90 TL</p>"
    "<button id='ayrinti' onclick=\"durum.textContent='ayrıntı'\">Show details</button>"
    "<button id='siparis' onclick=\"durum.textContent='verildi'\">Place order</button>"
    "<button id='ikon' aria-label='Buy now' onclick=\"durum.textContent='ikon-verildi'\">🛒</button>"
    "<button id='onay' onclick=\"durum.textContent='onaylandı'\">Onayla</button>"
    "<p>Alıcı: Ali Veli IBAN TR33 0006 1005 1978 6457 8413 26</p>"
)


@pytest.mark.asyncio
async def test_browse_url_click_on_order_button_needs_approval(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Gerçek Playwright: sıradan düğme onaysız çalışır; ödeme etiketli (metin, aria-label, bağlamlı genel) düğme host onayı ister."""
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    url: str = "data:text/html;charset=utf-8," + urllib.parse.quote(BROWSE_PAGE)
    toolbox = Toolbox()

    def click(selector: str) -> main.ToolCallDraft:
        arguments = {"url": url, "actions": [{"action": "click", "selector": selector, "value": None}]}
        return {"id": "b1", "name": "browse_url", "arguments": json.dumps(arguments)}

    async def confirm(title: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        return {approval.APPROVAL_FIELD: True}

    async def deny(title: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        return {approval.APPROVAL_FIELD: False}

    try:
        ordinary = await _execute(click("#ayrinti"), toolbox, None)
        if str(ordinary.get("code", "")).startswith("BROWSER_"):
            pytest.skip("Playwright Chromium kurulu değil")
        assert ordinary["ok"] and "ayrıntı" in ordinary["result"]
        for selector, done in (("#siparis", "verildi"), ("#ikon", "ikon-verildi"), ("#onay", "onaylandı")):
            refused = await _execute(click(selector), toolbox, None)
            assert refused["code"] == "APPROVAL_UNAVAILABLE" and done not in refused.get("result", ""), selector
            assert (await _execute(click(selector), toolbox, deny))["code"] == "APPROVAL_DENIED", selector
            approved = await _execute(click(selector), toolbox, confirm)
            assert approved["ok"] and done in approved["result"], selector
        decisions = [record["decision"] for record in _audit(tmp_path)]
        assert decisions == ["unavailable", "denied", "approved"] * 3
        assert "Place order" in _audit(tmp_path)[0]["summary"] and "249,90 TL" in _audit(tmp_path)[0]["summary"]
    finally:
        await toolbox.close_browser()


def test_agent_file_and_shell_tools_need_approval_for_protected_data_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Ajanın dosya/kabuk araçları veri kökündeki güvenlik ilkesi dosyalarını (yedek sağlayıcı izni, denetim kaydı,
    kalıcı hafıza, zamanlanmış görevler) onaysız yazamaz: yazsaydı yedek sağlayıcı ve görüntü izni ajanın kendisi
    tarafından açılır, denetim izi silinir ve hafıza onayı atlanırdı. Salt okuma ve sıradan dosyalar sorulmaz.
    """
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    root = data_root()
    workspace_dir().mkdir(parents=True, exist_ok=True)
    link = workspace_dir() / "kisayol.json"
    link.symlink_to(root / "provider_fallback.json")
    protected = [
        ("write_file", {"path": str(root / "provider_fallback.json"), "content": "{}"}),
        ("write_file", {"path": "../provider_fallback.json", "content": "{}"}),
        ("write_file", {"path": str(root / "Provider_Fallback.JSON"), "content": "{}"}),
        ("edit_file", {"path": str(root / "audit.jsonl"), "old_text": "a", "new_text": "b"}),
        ("write_file", {"path": str(link), "content": "{}"}),
        ("execute_shell", {"command": f"echo '{{}}' > {root}/provider_fallback.json"}),
        ("execute_shell", {"command": f"sed -i '' s/a/b/ {root}/user_memory.json"}),
    ]
    for name, arguments in protected:
        request = main.approval_request_for_call(name, arguments, None, False)
        assert request is not None and request["category"] == "configuration", (name, arguments)
    ordinary = [
        ("write_file", {"path": "notlar.md", "content": "x"}),
        ("write_file", {"path": str(root / "workspace" / "provider_fallback.json"), "content": "x"}),
        ("execute_shell", {"command": f"cat {root}/provider_fallback.json"}),
        ("execute_shell", {"command": "ls -la"}),
    ]
    for name, arguments in ordinary:
        assert main.approval_request_for_call(name, arguments, None, False) is None, (name, arguments)


def _config_write_asked(name: str, arguments: Dict[str, Any]) -> bool:
    request = main.approval_request_for_call(name, arguments, None, False)
    return request is not None and request["category"] == "configuration"


def test_new_memory_files_are_protected_and_ordinary_files_are_not(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Deneyim dersleri ve epizot geçmişi de ajanın kendi araçlarıyla yazılamayan dosyalardandır."""
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    root = data_root()
    for name in ("experience_memory.json", "cognitive_memory.json"):
        assert name in approval.PROTECTED_DATA_FILES
        assert _config_write_asked("write_file", {"path": str(root / name), "content": "{}"}), name
        assert _config_write_asked("execute_shell", {"command": f"rm {root}/{name}"}), name
    assert not _config_write_asked("write_file", {"path": str(root / "notlar.json"), "content": "{}"})


def test_hard_link_to_a_protected_file_needs_approval(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Sert bağ aynı dosyaya başka ad verir: yalnız ad ve üst dizin karşılaştırması (eski kapı) onu kaçırırdı. Kimliğe (aygıt +
    inode) bakılır; korumalı olmayan dosyanın sert bağı ise sorulmaz.
    """
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    root = data_root()
    workspace_dir().mkdir(parents=True, exist_ok=True)
    (root / "audit.jsonl").touch()
    link = workspace_dir() / "yedek.txt"
    os.link(root / "audit.jsonl", link)
    ordinary = workspace_dir() / "baska.json"
    ordinary.write_text("{}", encoding="utf-8")
    ordinary_link = workspace_dir() / "baska-ad.json"
    os.link(ordinary, ordinary_link)
    assert _config_write_asked("write_file", {"path": str(link), "content": "{}"})
    assert _config_write_asked("write_file", {"path": "yedek.txt", "content": "{}"})  # göreli yol iş alanına çözülür
    assert _config_write_asked("edit_file", {"path": str(link), "old_text": "a", "new_text": "b"})
    assert not _config_write_asked("write_file", {"path": str(ordinary_link), "content": "{}"})
    assert not _config_write_asked("write_file", {"path": "notlar.md", "content": "x"})


def test_directory_spelled_with_other_case_reaches_the_same_protected_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    APFS büyük/küçük harfe duyarsızdır: '/…/OMNIAGENT/audit.jsonl' aynı dosyadır. Eski kapı üst dizini harfiyen karşılaştırıp
    kaçırırdı. Dosya henüz yokken de (provider_fallback.json ilk yazılışta yoktur) üst dizin kimliğiyle yakalanır.
    """
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    root = data_root()
    shouted = root.parent / root.name.swapcase()
    if not shouted.exists():
        pytest.skip("dosya sistemi büyük/küçük harfe duyarlı: farklı harfli dizin adı başka bir dizindir")
    (root / "audit.jsonl").touch()
    workspace_dir().mkdir(parents=True, exist_ok=True)
    for name in ("provider_fallback.json", "audit.jsonl", "PROVIDER_FALLBACK.JSON"):
        assert _config_write_asked("write_file", {"path": str(shouted / name), "content": "{}"}), name
    assert _config_write_asked("execute_shell", {"command": f"tee {shouted}/audit.jsonl"})
    # Aynı adlı dosya iş alanı alt dizininde: veri kökünün kendisi değil, korumasız
    assert not _config_write_asked("write_file", {"path": str(shouted / "workspace" / "provider_fallback.json"), "content": "x"})


def test_firmlink_prefix_reaches_the_same_protected_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """macOS'ta '/System/Volumes/Data' öneki aynı dizine ikinci yol verir (firmlink); kimlikle yakalanır."""
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    root = data_root()
    via_firmlink = Path("/System/Volumes/Data" + str(root.resolve()))
    if not via_firmlink.is_dir():
        pytest.skip("bu dosya sisteminde /System/Volumes/Data firmlink'i yok (macOS Data birimi değil)")
    (root / "audit.jsonl").touch()
    for name in ("provider_fallback.json", "audit.jsonl"):
        assert _config_write_asked("write_file", {"path": str(via_firmlink / name), "content": "{}"}), name
    assert not _config_write_asked("write_file", {"path": str(via_firmlink / "workspace" / "audit.jsonl"), "content": "x"})


@pytest.mark.parametrize("template, protected", [
    # Veri kökü dışındaki aynı adlı PROJE dosyaları sorulmaz ('add' içindeki 'dd' yazma işareti sayılmaz)
    ("git add catalog.json", None), ("python build.py > catalog.json", None), ("ls 2>&1 | tee telegram.json", None),
    ("jq . catalog.json > cikti.json", None), ("cp x.json ~/proje/schedules.json", None), ("echo x > ~/proj/telegram.json", None),
    # Veri kökünden okuma sorulmaz
    ("cat {root}/audit.jsonl", None), ("grep -c a {root}/user_memory.json", None),
    # Veri köküne yazan komutlar sorulur: mutlak yol, Application Support (kaçışlı boşluk dahil), OMNI_DATA_DIR, '..' ve harf farkı
    ("echo '{{}}' > {root}/provider_fallback.json", "provider_fallback.json"),
    ("tee -a {root}/audit.jsonl < x", "audit.jsonl"),
    ('printf x >> "$HOME/Library/Application Support/OmniAgent/audit.jsonl"', "audit.jsonl"),
    ("cd ~/Library/Application\\ Support/OmniAgent && rm experience_memory.json", "experience_memory.json"),
    ("echo x > $OMNI_DATA_DIR/cognitive_memory.json", "cognitive_memory.json"),
    ("mv x.json ../user_memory.json", "user_memory.json"),
    ("python3 -c \"open('{root}/telegram.json','w')\"", "telegram.json"),
    ("sed -i '' s/a/b/ {root}/catalog.json", "catalog.json"),
    ("tee {upper}/AUDIT.JSONL", "audit.jsonl"),
])
def test_shell_write_gate_needs_the_data_root_and_word_bounded_markers(
    template: str, protected: Optional[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Kabuk sezgisi yalnız dosya adı + yazma işaretiyle değil, komutun veri kökünü göstermesiyle de tetiklenir: eskiden
    'git add catalog.json', 'python build.py > catalog.json' ve 'ls 2>&1 | tee telegram.json' gibi proje komutları yanlışlıkla
    onay isterdi ('dd ' işaretçisi 'add ' içine uyuyordu).
    """
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    root = data_root()
    command = template.format(root=root, upper=str(root).upper())
    request = main.approval_request_for_call("execute_shell", {"command": command}, None, False)
    assert (request is not None) is (protected is not None), command
    if request is not None:
        assert request["category"] == "configuration" and protected is not None and protected in request["summary"]


AMOUNT_ADVERSARIAL_RUNS: Dict[str, str] = {
    "rakam": "1" * 20000, "rakam-virgul": "1," * 10000, "rakam-nokta": "1." * 10000, "rakam-bosluk": "1 " * 10000,
    "yalniz-virgul": "," * 20000, "harf-rakam": "a1" * 10000,
}


@pytest.mark.parametrize("run", sorted(AMOUNT_ADVERSARIAL_RUNS))
def test_amount_scan_is_linear_in_digit_runs(run: str) -> None:
    """Eski 'sayı öbeği + simge' kolu her rakamdan yeniden başlayıp '1' * 20000 girdisinde 6,4 sn sürüyordu."""
    text: str = AMOUNT_ADVERSARIAL_RUNS[run]
    started = time.perf_counter()
    assert approval._AMOUNT_PATTERN.search(text) is None
    assert approval.amount_lines([text], approval.AMOUNT_LINES_LIMIT) == []
    assert time.perf_counter() - started < 0.1


@pytest.mark.parametrize("text", ["1" * 20000 + " TL", "1," * 10000 + "5 TL", "1." * 10000 + "5 EUR", "x" * 20000 + " 5 TL"])
def test_amount_after_a_very_long_number_run_is_still_found_quickly(text: str) -> None:
    started = time.perf_counter()
    assert approval._AMOUNT_PATTERN.search(text) is not None
    assert time.perf_counter() - started < 0.1


@pytest.mark.parametrize("line, has_amount", [
    ("Toplam: 1.249,00 TL", True), ("Total $49.99", True), ("₺249,90", True), ("49,90 €", True), ("Amount due: USD 500", True),
    ("TL 12,5", True), ("Pay 1,299.00 EUR", True), ("toplam 3 try", True), ("1,5€", True), ("3 £", True),
    (".50 USD", True), (",5 TL", True), ("12tl", True), ("a1 TL", True),  # baştaki ayraçlı ve bitişik yazımlar eskisi gibi tutar
    ("IBAN TR33 0006 1005 1978 6457 8413 26", False), ("Merhaba dünya", False), ("", False), ("12 tlx", False),
    ("abc 12", False), ("1 2 3", False),
])
def test_amount_lines_recognise_common_price_formats(line: str, has_amount: bool) -> None:
    """Regex düzeltmesi var/yok kararını değiştirmez (400 bin rastgele metinde eski kalıpla karşılaştırıldı)."""
    assert bool(approval.amount_lines([line], 3)) is has_amount


def test_amount_lines_are_searched_in_the_shown_clipped_line() -> None:
    """Gösterilen satır (boşlukları sadeleşmiş, 80 karakterle kırpık) her zaman tutarı içerir; 80. karakterden sonraki tutar sayılmaz."""
    assert approval.amount_lines(["Toplam:   1.249,00 \t TL"], 1) == ["Toplam: 1.249,00 TL"]
    assert approval.amount_lines(["Toplam: 5 TL", "a " * 60 + "5 TL"], 3) == ["Toplam: 5 TL"]


def test_card_gate_and_filter_share_one_luhn_and_the_filter_is_the_broader_net() -> None:
    """Luhn tek yerdedir (observation_filter.luhn_valid); ağ tabloları bilerek ayrıdır: süzgeç kapının kabul ettiğini de maskeler."""
    assert approval.luhn_valid is observation_filter.luhn_valid
    for number in KNOWN_CARDS + [_luhn_completed("9792", 16)]:
        assert approval.card_number_in_text(number) is not None, number
        assert observation_filter.mask_sensitive_text(number) == observation_filter.SENSITIVE_PLACEHOLDER, number
    # Süzgeç daha geniştir: Luhn geçerli '56' önekli 16 hane kapıda kart ağı değil (Maestro 50xx/58xx tablosunda yok) ama maskelenir
    broad = _luhn_completed("5600", 16)
    assert approval.card_number_in_text(broad) is None
    assert observation_filter.mask_sensitive_text(broad) == observation_filter.SENSITIVE_PLACEHOLDER
