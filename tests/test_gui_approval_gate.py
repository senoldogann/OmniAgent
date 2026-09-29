"""
GUI ödeme kapısı (host onayı): gerçek headless Chromium + gerçek Vision OCR + gerçek execute_tool + gerçek denetim
kaydı (yalnız macOS). Öğe (AX) araçlarının kapı mantığı gerçek AX gerektirdiğinden burada yalnız konnektör (CUA)
sahtesiyle sınanır; gerçek AXPress/ön plan tıklaması OMNI_LIVE_GUI=1 canlı testlerindedir (test_ax_live_chrome.py).
"""
import json
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pytest

from omniagent import approval
from omniagent.app import agent as main
from omniagent.core.state import ascii_fold
from omniagent.dev import headless_screen
from omniagent.integrations.runtime import CURRENT_RUNTIME, IntegrationRuntime
from omniagent.tools import Toolbox, ToolError
from omniagent.tools.ax_snapshot import CapturedSnapshot, Element, Frame, LiveElement, Snapshot
from omniagent.tools.bot_wall import (
    ACCESS_CHALLENGE_CODE, ACCESS_CHALLENGE_MARKER, human_verification_error, human_verification_label,
)
from omniagent.tools.gui_input import ResolvedElement

needs_vision = pytest.mark.skipif(sys.platform != "darwin", reason="Vision OCR yalnız macOS")

BUTTON_STYLE: str = "font-size:30px;padding:18px;margin:12px"
PAY_PAGE: str = (
    "<body data-benchmark-state='initial' style='font:30px sans-serif'><p>Toplam: 1.249,00 TL</p>"
    f"<button data-benchmark-target='details' style='{BUTTON_STYLE}'>Ayrıntıları göster</button>"
    f"<button data-benchmark-target='pay' style='{BUTTON_STYLE}'>Ödemeyi onayla</button></body>"
)
TRANSFER_PAGE: str = (
    "<body data-benchmark-state='initial' style='font:30px sans-serif'><p>Alıcı: Ali Veli</p>"
    "<p>IBAN: TR33 0006 1005 1978 6457 8413 26</p><p>Tutar: 500,00 TL</p>"
    f"<button data-benchmark-target='pay' style='{BUTTON_STYLE}'>Onayla</button></body>"
)
PLAIN_PAGE: str = (
    "<body data-benchmark-state='initial' style='font:30px sans-serif'><p>Çerez tercihleri</p>"
    f"<button data-benchmark-target='pay' style='{BUTTON_STYLE}'>Onayla</button></body>"
)


async def _run(call: main.ToolCallDraft, answer: Optional[Any], unattended: bool) -> main.ToolResult:
    """Aracı gerçek görev bağlamında çalıştırır (etkileşimli kanal var/yok, sürekli mod)."""
    runtime = IntegrationRuntime(lambda event: None, lambda: False, answer)
    runtime.unattended = unattended
    token = CURRENT_RUNTIME.set(runtime)
    try:
        return await main.execute_tool(call, main.Toolbox(), {}, lambda event: None, lambda: False)
    finally:
        CURRENT_RUNTIME.reset(token)


def _pay_clicks(page: headless_screen.HeadlessPage) -> int:
    return sum(1 for action in page.actions() if action["action"] == "click" and action["target"] == "pay")


def _model_point(page: headless_screen.HeadlessPage, target: str) -> List[int]:
    box = page._run(lambda p: p.locator(f"[data-benchmark-target={target}]").bounding_box())
    return [round((box["x"] + box["width"] / 2) * 1000 / headless_screen.VIEW_WIDTH),
            round((box["y"] + box["height"] / 2) * 1000 / headless_screen.VIEW_HEIGHT)]


def _audit(tmp_path: Path) -> List[Dict[str, str]]:
    return [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()]


async def _confirm(title: str, fields: Dict[str, Any]) -> Dict[str, Any]:
    return {approval.APPROVAL_FIELD: True}


def _click_text(text: str) -> main.ToolCallDraft:
    return {"id": "c1", "name": "cua_click_text", "arguments": json.dumps({"text": text, "near": None})}


@needs_vision
@pytest.mark.asyncio
async def test_click_text_resolved_to_payment_button_needs_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, headless_page: headless_screen.HeadlessPage,
    manual_approval: None,
) -> None:
    """Model 'Onayla' ister, ekrandaki gerçek düğme 'Ödemeyi onayla': host çözümlenen etikete göre sorar."""
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    headless_page._run(lambda page: page.set_content(PAY_PAGE))
    call = _click_text("Onayla")

    refused = await _run(call, None, False)
    assert refused["code"] == "APPROVAL_UNAVAILABLE" and refused["recoverable"] is False and _pay_clicks(headless_page) == 0

    async def deny(title: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        assert "odemeyi onayla" in ascii_fold(str(fields["_help"])) and "1.249,00" in str(fields["_help"])
        return {approval.APPROVAL_FIELD: False}

    assert (await _run(call, deny, False))["code"] == "APPROVAL_DENIED" and _pay_clicks(headless_page) == 0
    assert (await _run(call, _confirm, True))["code"] == "APPROVAL_UNAVAILABLE"  # sürekli mod: kanal yok
    approved = await _run(call, _confirm, False)
    assert approved["ok"] and _pay_clicks(headless_page) == 1
    assert [record["decision"] for record in _audit(tmp_path)] == ["unavailable", "denied", "unavailable", "approved"]
    assert "odemeyi onayla" in ascii_fold(_audit(tmp_path)[-1]["summary"])


@needs_vision
@pytest.mark.asyncio
async def test_point_and_sequence_clicks_are_gated_but_ordinary_buttons_are_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, headless_page: headless_screen.HeadlessPage,
) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    headless_page._run(lambda page: page.set_content(PAY_PAGE))
    details, pay = _model_point(headless_page, "details"), _model_point(headless_page, "pay")
    ordinary: main.ToolCallDraft = {"id": "p1", "name": "cua_click_point", "arguments": json.dumps({"point": details})}
    assert (await _run(ordinary, None, False))["ok"]  # sıradan düğme: onay istenmez
    payment: main.ToolCallDraft = {"id": "p2", "name": "cua_click_point", "arguments": json.dumps({"point": pay})}
    assert (await _run(payment, None, False))["code"] == "APPROVAL_UNAVAILABLE"
    sequence: main.ToolCallDraft = {"id": "s1", "name": "run_action_sequence", "arguments": json.dumps({
        "steps": [{"action": "click", "point": details}, {"action": "click", "point": pay}]})}
    result = await _run(sequence, None, False)
    assert result["code"] == "APPROVAL_UNAVAILABLE" and result["completed_steps"] == 1
    assert _pay_clicks(headless_page) == 0
    assert (await _run(payment, _confirm, False))["ok"] and _pay_clicks(headless_page) == 1
    # Çift tıklama tek onayla kapsanır ama sağ tuş kapı dışıdır (ödeme onayı sol tıklamadır)
    right: main.ToolCallDraft = {"id": "s2", "name": "run_action_sequence", "arguments": json.dumps({
        "steps": [{"action": "click", "point": pay, "button": "right"}]})}
    assert (await _run(right, None, False))["ok"]


@needs_vision
@pytest.mark.asyncio
async def test_screen_change_during_approval_cancels_the_click(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, headless_page: headless_screen.HeadlessPage,
) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    headless_page._run(lambda page: page.set_content(PAY_PAGE))

    async def swap_then_confirm(title: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        headless_page._run(lambda page: page.evaluate(
            'document.querySelector("[data-benchmark-target=pay]").textContent = "Ayrıntıları kapat"'))
        return {approval.APPROVAL_FIELD: True}

    result = await _run(_click_text("Ödemeyi onayla"), swap_then_confirm, False)
    assert result["code"] == "TARGET_CHANGED_AFTER_APPROVAL" and result["recoverable"] is True
    assert _pay_clicks(headless_page) == 0
    point_call: main.ToolCallDraft = {"id": "p3", "name": "cua_click_point",
                                      "arguments": json.dumps({"point": _model_point(headless_page, "pay")})}
    headless_page._run(lambda page: page.evaluate(
        'document.querySelector("[data-benchmark-target=pay]").textContent = "Ödemeyi onayla"'))

    async def move_then_confirm(title: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        headless_page._run(lambda page: page.evaluate(
            'document.querySelector("[data-benchmark-target=pay]").style.marginTop = "300px"'))
        return {approval.APPROVAL_FIELD: True}

    moved = await _run(point_call, move_then_confirm, False)
    assert moved["code"] == "TARGET_CHANGED_AFTER_APPROVAL" and _pay_clicks(headless_page) == 0


@needs_vision
@pytest.mark.asyncio
async def test_generic_label_is_gated_only_with_amount_and_recipient_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, headless_page: headless_screen.HeadlessPage,
) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    headless_page._run(lambda page: page.set_content(TRANSFER_PAGE))
    assert (await _run(_click_text("Onayla"), None, False))["code"] == "APPROVAL_UNAVAILABLE"
    assert _pay_clicks(headless_page) == 0
    assert (await _run(_click_text("Onayla"), _confirm, False))["ok"] and _pay_clicks(headless_page) == 1
    assert "500,00" in _audit(tmp_path)[-1]["summary"]
    headless_page._run(lambda page: page.set_content(PLAIN_PAGE))
    assert (await _run(_click_text("Onayla"), None, False))["ok"]  # bağlamsız 'Onayla' sıradandır


# --- Öğe (AX) araçları: kapı mantığı konnektör sahtesiyle ---

def _frame() -> Frame:
    return {"x": 10.0, "y": 10.0, "w": 100.0, "h": 30.0}


def _described(index: int, role: str, label: str, description: str) -> Element:
    return {"index": index, "role": role, "subrole": "", "label": label, "value": "", "description": description,
            "frame": _frame(), "actions": ["AXPress"], "enabled": True, "focused": False, "secure": False}


def _element(index: int, role: str, label: str) -> Element:
    return _described(index, role, label, "")


class FakeCUA:
    """AX konnektörünün sahtesi: yalnız kapı akışını sınar (çözümleme, onay sonrası yeniden çözümleme, tek tetikleme)."""

    def __init__(self, elements: List[Element]) -> None:
        self.elements: List[Element] = elements
        self.clicks: List[str] = []
        self.stale: bool = False
        self.ocr_text: Optional[str] = None
        self.ocr_error: Optional[ToolError] = None
        self.legacy_labels: List[str] = []

    def _target(self, index: int) -> ResolvedElement:
        if self.stale:
            raise ToolError("öğe bayat", "STALE_ELEMENT", True)
        element = self.elements[index - 1]
        snapshot: Snapshot = {
            "id": "s1", "app": "DenemeUygulaması", "pid": 1, "window_title": "t", "created_at": 0.0,
            "elements": self.elements, "truncated": False, "digest": "d",
            "stats": {"nodes_visited": 1, "pruned_subtrees": 0, "seconds": 0.0, "web_ready": None},
        }
        live: LiveElement = {"role": element["role"], "subrole": "", "label": element["label"], "value": "",
                             "frame": _frame(), "enabled": True, "focused": False, "secure": False}
        return ResolvedElement(CapturedSnapshot(snapshot, (object(),), object(), object()), element, object(), live)

    def prepare_target(self, snapshot: str, index: int) -> ResolvedElement:
        return self._target(index)

    def click_resolved_element(self, target: ResolvedElement) -> str:
        self.clicks.append("sıradan")
        return "tıklandı"

    def click_snapshot_element(self, snapshot: str, index: int) -> str:
        self.clicks.append("sıradan")
        return "tıklandı"

    def click_approved_element(self, target: ResolvedElement) -> str:
        self.clicks.append("onaylı-tek-tetikleme")
        return "onaylı tıklandı"

    def element_visible_text(self, target: ResolvedElement) -> Optional[str]:
        if self.ocr_error is not None:
            raise self.ocr_error
        return self.ocr_text

    def element_labels(self, app_name: str, element_id: int) -> List[str]:
        return list(self.legacy_labels)

    def click_element(self, app_name: str, element_id: int) -> str:
        self.clicks.append("eski-liste")
        return "eski tıklandı"


def _element_call(index: int) -> main.ToolCallDraft:
    return {"id": "e1", "name": "cua_click_element", "arguments": json.dumps({"snapshot": "s1", "index": index})}


async def _run_with(toolbox: Toolbox, call: main.ToolCallDraft, answer: Optional[Any], unattended: bool) -> main.ToolResult:
    runtime = IntegrationRuntime(lambda event: None, lambda: False, answer)
    runtime.unattended = unattended
    token = CURRENT_RUNTIME.set(runtime)
    try:
        return await main.execute_tool(call, toolbox, {}, lambda event: None, lambda: False)
    finally:
        CURRENT_RUNTIME.reset(token)


def _toolbox(cua: FakeCUA) -> Toolbox:
    toolbox = Toolbox()
    toolbox.cua = cua  # type: ignore[assignment]
    return toolbox


@pytest.mark.asyncio
async def test_element_click_needs_approval_and_fires_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, manual_approval: None,
) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    cua = FakeCUA([_element(1, "AXButton", "Ayrıntıları göster"), _element(2, "AXButton", "Pay now"),
                   _element(3, "AXLink", "Payments"), _element(4, "AXTextField", "Donate amount"),
                   _described(5, "AXButton", "Vazgeç", "Complete purchase")])
    toolbox = _toolbox(cua)
    questions: List[str] = []

    async def deny(title: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        questions.append(str(fields["_help"]))
        return {approval.APPROVAL_FIELD: False}

    for ordinary in (1, 3, 4):  # sıradan düğme, gezinme bağlantısı, metin alanı: onay istenmez
        assert (await _run_with(toolbox, _element_call(ordinary), None, False))["ok"]
    assert cua.clicks == ["sıradan"] * 3
    for financial in (2, 5):  # ad ya da açıklama adayı ödeme düğmesi
        assert (await _run_with(toolbox, _element_call(financial), None, False))["code"] == "APPROVAL_UNAVAILABLE"
        assert (await _run_with(toolbox, _element_call(financial), _confirm, True))["code"] == "APPROVAL_UNAVAILABLE"
        assert (await _run_with(toolbox, _element_call(financial), deny, False))["code"] == "APPROVAL_DENIED"
    assert cua.clicks == ["sıradan"] * 3 and "Pay now" in questions[0] and "DenemeUygulaması" in questions[0]
    approved = await _run_with(toolbox, _element_call(2), _confirm, False)
    assert approved["ok"] and cua.clicks[3:] == ["onaylı-tek-tetikleme"]
    assert [record["decision"] for record in _audit(tmp_path)].count("approved") == 1


@pytest.mark.asyncio
async def test_element_renamed_or_gone_during_approval_is_not_clicked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    cua = FakeCUA([_element(1, "AXButton", "Pay now")])
    toolbox = _toolbox(cua)

    async def rename_then_confirm(title: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        cua.elements = [_element(1, "AXButton", "Cancel order")]
        return {approval.APPROVAL_FIELD: True}

    renamed = await _run_with(toolbox, _element_call(1), rename_then_confirm, False)
    assert renamed["code"] == "TARGET_CHANGED_AFTER_APPROVAL" and cua.clicks == []

    cua.elements = [_element(1, "AXButton", "Pay now")]

    async def vanish_then_confirm(title: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        cua.stale = True
        return {approval.APPROVAL_FIELD: True}

    vanished = await _run_with(toolbox, _element_call(1), vanish_then_confirm, False)
    assert vanished["code"] == "STALE_ELEMENT" and cua.clicks == []


@pytest.mark.asyncio
async def test_unlabeled_element_is_completed_by_roi_ocr_and_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    cua = FakeCUA([_element(1, "AXButton", "")])
    toolbox = _toolbox(cua)
    cua.ocr_text = "Ödemeyi onayla"
    assert (await _run_with(toolbox, _element_call(1), None, False))["code"] == "APPROVAL_UNAVAILABLE"
    assert cua.clicks == []

    async def relabel_then_confirm(title: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        cua.ocr_text = "İptal"
        return {approval.APPROVAL_FIELD: True}

    assert (await _run_with(toolbox, _element_call(1), relabel_then_confirm, False))["code"] == "TARGET_CHANGED_AFTER_APPROVAL"
    cua.ocr_text = "Ödemeyi onayla"
    assert (await _run_with(toolbox, _element_call(1), _confirm, False))["ok"] and cua.clicks == ["onaylı-tek-tetikleme"]

    cua.clicks.clear()
    cua.ocr_text = None  # simge: metin yok, kapı yok
    assert (await _run_with(toolbox, _element_call(1), None, False))["ok"] and cua.clicks == ["sıradan"]
    cua.clicks.clear()
    cua.ocr_error = ToolError("ekran kaydı izni yok", "SCREEN_CAPTURE_PERMISSION", False)  # okunamayan etiket: sessizce geçme
    failed = await _run_with(toolbox, _element_call(1), _confirm, False)
    assert failed["code"] == "SCREEN_CAPTURE_PERMISSION" and cua.clicks == []


@pytest.mark.asyncio
async def test_generic_element_label_uses_static_text_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    statics = [_element(2, "AXStaticText", "Alıcı: Ali Veli"), _element(3, "AXStaticText", "Tutar: 500,00 TL")]
    cua = FakeCUA([_element(1, "AXButton", "Gönder"), *statics])
    toolbox = _toolbox(cua)
    assert (await _run_with(toolbox, _element_call(1), None, False))["code"] == "APPROVAL_UNAVAILABLE"
    cua.elements = [_element(1, "AXButton", "Gönder"), _element(2, "AXStaticText", "Merhaba")]
    assert (await _run_with(toolbox, _element_call(1), None, False))["ok"]


@pytest.mark.asyncio
async def test_legacy_list_clicks_are_gated_and_refusal_does_not_fall_back_to_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    cua = FakeCUA([])
    toolbox = _toolbox(cua)
    template_clicks: List[str] = []
    monkeypatch.setattr(Toolbox, "_click_template", lambda self, app, path, confidence: template_clicks.append(path) or "şablon")
    cua.legacy_labels = ["Buy now"]
    click: main.ToolCallDraft = {"id": "l1", "name": "cua_click",
                                 "arguments": json.dumps({"app_name": "Uygulama", "element_id": 2})}
    smart: main.ToolCallDraft = {"id": "l2", "name": "smart_click", "arguments": json.dumps(
        {"app_name": "Uygulama", "element_id": 2, "template_path": "/tmp/yok.png", "confidence": 0.8})}
    assert (await _run_with(toolbox, click, None, False))["code"] == "APPROVAL_UNAVAILABLE"
    assert (await _run_with(toolbox, smart, None, False))["code"] == "APPROVAL_UNAVAILABLE"  # şablon yoluyla dolanılamaz
    assert cua.clicks == [] and template_clicks == []
    assert (await _run_with(toolbox, click, _confirm, False))["ok"] and cua.clicks == ["eski-liste"]
    cua.clicks.clear()
    cua.legacy_labels = ["Tamam"]
    assert (await _run_with(toolbox, smart, None, False))["ok"] and cua.clicks == ["eski-liste"]
    # Onay beklerken ad değişirse eski liste tıklaması da yapılmaz
    cua.clicks.clear()
    cua.legacy_labels = ["Buy now"]

    async def rename_then_confirm(title: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        cua.legacy_labels = ["Cancel"]
        return {approval.APPROVAL_FIELD: True}

    assert (await _run_with(toolbox, click, rename_then_confirm, False))["code"] == "TARGET_CHANGED_AFTER_APPROVAL"
    assert cua.clicks == []


def test_gate_is_inert_without_a_host_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """Birim test/betik bağlamında (TOOL_RUNTIME yok) kapı ve ROI OCR maliyeti yoktur: araç doğrudan çalışır."""
    cua = FakeCUA([_element(1, "AXButton", "Pay now")])
    toolbox = _toolbox(cua)
    assert toolbox.cua_click_element("s1", 1) == "tıklandı" and cua.clicks == ["sıradan"]
    clicks: List[tuple] = []
    monkeypatch.setattr("omniagent.tools.click_model_point", lambda x, y, button, geometry: clicks.append((x, y)) or "ok")
    monkeypatch.setattr("omniagent.tools.screen_capture_granted", lambda request=False: False)
    monkeypatch.setattr("omniagent.tools._require_accessibility", lambda: None)
    monkeypatch.setattr(Toolbox, "_input_geometry", lambda self: {
        "point_width": 1000, "point_height": 1000, "model_width": 1000, "model_height": 1000})
    assert toolbox.cua_click_point([100, 200]) == "ok" and clicks == [(100, 200)]


def _order_page(variant: str) -> str:
    """Aynı düğmeyi ('Place order', #bir) taşıyan iki farklı sayfa: hata metni aynı, çağrı bağımsız değişkenleri farklı."""
    return f"<!-- {variant} --><p>Toplam: 249,90 TL</p><button id='bir'>Place order</button>"


@pytest.mark.asyncio
async def test_approval_refusals_never_reach_experience_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Onay kapısının retleri host politikasıdır: deneyim belleğine yazılırsa ikinci retten sonra "TEKRARLANAN HATA ... farklı
    bir yol seç" uyarısı modele reddedilen ödeme tıklamasını başka yoldan dolanmayı önerirdi. Gerçek browse_url (Playwright)
    ve gerçek onay kapısı; model betiklidir, kanal yoktur.
    """
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    urls: List[str] = ["data:text/html;charset=utf-8," + urllib.parse.quote(_order_page(name)) for name in ("a", "b")]
    turns: List[Dict[str, Any]] = [
        {"content": "", "finish_reason": "tool_calls", "usage": main.ZERO_USAGE, "tool_calls": [{
            "id": f"call-{index}", "name": "browse_url", "arguments": json.dumps({
                "url": url, "actions": [{"action": "click", "selector": "#bir", "value": None}]})}]}
        for index, url in enumerate(urls)
    ]
    final: Dict[str, Any] = {"content": "Onay gerektiğini bildiriyorum.", "tool_calls": [], "finish_reason": "stop",
                             "usage": main.ZERO_USAGE}
    tool_messages: List[str] = []

    async def scripted_model(
        clients: Any, messages: Any, schemas: Any, session_id: str, backend: str, emit: Any, should_stop: Any,
    ) -> tuple[Dict[str, Any], str]:
        tool_messages[:] = [str(message["content"]) for message in messages if message["role"] == "tool"]
        return (turns.pop(0) if turns else final), backend  # sonuç kapıları ek tur isteyebilir: yine son yanıt

    monkeypatch.setattr(main, "_call_model_with_retries", scripted_model)
    toolbox = Toolbox()
    monkeypatch.setattr(main, "Toolbox", lambda *arguments, **keywords: toolbox)
    try:
        await main.run_agent_with_callback(
            "Sipariş sayfalarında Place order düğmesine tıkla",
            [].append,
            {"requested_backend": "ollama-cloud", "should_stop": lambda: False,
             "state_file": str(tmp_path / "state.json"), "history": []},
            {"ollama-cloud": object()},
        )
    finally:
        await toolbox.close_browser()

    if any("BROWSER_UNAVAILABLE" in message for message in tool_messages):
        pytest.skip("Playwright Chromium kurulu değil")
    assert len(tool_messages) == 2 and all("kullanıcı onayı gerektiriyor" in message for message in tool_messages)
    assert not [message for message in tool_messages if "TEKRARLANAN HATA" in message or "DENEYİM BELLEĞİ" in message]


# --- İnsan/bot doğrulaması etiketleri: onay yolu olmayan sert red ---

HUMAN_CHECK_POSITIVE: List[str] = [
    "I'm not a robot", "I am not a robot", "Im not a robot", "Verify you are human", "Verify you're human",
    "Confirm you are human", "Robot olmadığınızı doğrulayın", "İnsan olduğunuzu doğrulayın", "Ben robot değilim",
    "Ich bin kein Roboter", "Je ne suis pas un robot", "En ole robotti", "No soy un robot", "  I’m NOT a Robot ✓ ",
]
HUMAN_CHECK_NEGATIVE: List[str] = [
    "Doğrula", "Verify email", "Verify", "Onayla", "Confirm", "Human resources", "Robot vacuum", "Not now",
    "Vahvista, että olet ihminen",  # repo benchmark'ının yerel form kutusu: sistem istemi yerel sayfada sıradan alan sayar
    "Please verify you are human by completing the action below, then press continue to proceed to the site",
    "To continue you must confirm that I'm not a robot before entering the members area of this website today",
    "", "Şifremi doğrula",
]


@pytest.mark.parametrize("label", HUMAN_CHECK_POSITIVE)
def test_human_verification_labels_are_recognised(label: str) -> None:
    assert human_verification_label(label)


@pytest.mark.parametrize("label", HUMAN_CHECK_NEGATIVE)
def test_ordinary_verify_buttons_and_long_paragraphs_are_not_human_checks(label: str) -> None:
    assert not human_verification_label(label)


def test_human_verification_error_reuses_the_access_challenge_code_and_message_format() -> None:
    error = human_verification_error("I'm not a robot")
    assert error.code == ACCESS_CHALLENGE_CODE == "BOT_WALL_DETECTED" and error.recoverable is False
    assert str(error).startswith(ACCESS_CHALLENGE_MARKER) and "onay yoluyla da geçilemez" in str(error)


# Kapı taramasının kaynakları AD ile sabitlenir: sayfalar ve onları betikle dolduran veri sabitleri (ilan/şirket/maaş/paragraf/aday).
# Ad değişir ya da tür değişirse tarama sessizce daralmak yerine açık hatayla kırılır (eski 'len(texts) > 100' eşiği tarama
# boş kalsa bile başka sabitlerin metniyle geçebilirdi).
BENCHMARK_PAGE_NAMES: Tuple[str, ...] = ("FORM_PAGE", "JOB_PAGE", "SALARY_PAGE", "SIMILAR_BUTTON_PAGE")
BENCHMARK_DATA_CONSTANTS: Tuple[str, ...] = ("JOB_LISTINGS", "SALARY_LISTINGS", "SALARY_PARAGRAPHS", "RESEARCH_CANDIDATES")


def test_no_benchmark_page_label_triggers_any_click_gate() -> None:
    """
    Repo benchmark sayfalarındaki (FORM/JOB/SALARY/SIMILAR) tüm görünür metin ve etiketler kapıyı TETİKLEMEZ. Tarama her kaynaktan
    metin bulduğunu kanıtlar (kaynak başına boş değil); ayrıca benchmark'taki DİĞER büyük harfli liste/demet sabitleri de taranır.
    """
    from bs4 import BeautifulSoup
    from omniagent.dev import benchmark

    texts: set = set()
    per_source: Dict[str, int] = {}
    for name in BENCHMARK_PAGE_NAMES:
        found: set = set()
        soup = BeautifulSoup(getattr(benchmark, name).replace("__RUN__", "x").replace("__ILANLAR__", "[]"), "html.parser")
        for tag in soup(["script", "style"]):
            tag.decompose()
        found.update(" ".join(line.split()) for line in soup.get_text("\n").splitlines() if line.strip())
        for element in soup.find_all(True):
            for attribute in ("aria-label", "value", "title", "alt", "placeholder"):
                value = element.get(attribute)
                if isinstance(value, str) and value.strip():
                    found.add(" ".join(value.split()))
        per_source[name] = len(found)
        texts.update(found)
    # Betikle doldurulan içerik: ilan/şirket/maaş/paragraf/aday veri sabitleri (yalnız dize yaprakları)
    def leaves(item: Any) -> Iterator[str]:
        if isinstance(item, str):
            yield " ".join(item.split())
        elif isinstance(item, dict):
            for value in item.values():
                yield from leaves(value)
        elif isinstance(item, (list, tuple)):
            for value in item:
                yield from leaves(value)

    data_constants: List[str] = sorted(
        attribute for attribute in dir(benchmark)
        if attribute.isupper() and isinstance(getattr(benchmark, attribute), (list, tuple))
    )
    assert set(BENCHMARK_DATA_CONSTANTS) <= set(data_constants), sorted(set(BENCHMARK_DATA_CONSTANTS) - set(data_constants))
    for attribute in data_constants:
        found = {text for text in leaves(getattr(benchmark, attribute)) if text}
        per_source[attribute] = len(found)
        texts.update(found)
    empty_sources = sorted(name for name, count in per_source.items() if count == 0)
    assert not empty_sources, f"tarama şu kaynaklardan metin bulamadı: {empty_sources}"
    everything: List[str] = sorted(texts)
    assert [t for t in texts if approval.financial_cta_reason(t) is not None] == []
    assert [t for t in texts if approval.click_financial_reason(t, everything) is not None] == []
    assert [t for t in texts if human_verification_label(t)] == []


class _HitServer:
    """Sayfadaki 'tıklandı' isteklerini sayan yerel sunucu (headless Chromium'un gerçek ağı)."""

    def __init__(self, page: str) -> None:
        self.hits: List[str] = []
        outer, body = self, page.encode("utf-8")

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path.startswith("/tiklandi"):
                    outer.hits.append(self.path)
                    self.send_response(204)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *arguments: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url: str = f"http://127.0.0.1:{self.server.server_address[1]}/"
        Thread(target=self.server.serve_forever, daemon=True).start()


ROBOT_PAGE: str = (
    "<body data-benchmark-state='initial' style='font:30px sans-serif'>"
    f"<button data-benchmark-target='robot' style='{BUTTON_STYLE}' onclick=\"fetch('/tiklandi?robot')\">I'm not a robot</button>"
    f"<button data-benchmark-target='email' style='{BUTTON_STYLE}' onclick=\"fetch('/tiklandi?email')\">Verify email</button></body>"
)


@needs_vision
@pytest.mark.asyncio
async def test_human_check_click_is_refused_without_reaching_the_page_even_if_the_user_would_approve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, headless_page: headless_screen.HeadlessPage,
    strict_wall_mode: None,
) -> None:
    """Sıkı mod, gerçek headless Chromium: 'I'm not a robot' tıklanmaz (sunucu /tiklandi almaz), onay sorulmaz."""
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    site = _HitServer(ROBOT_PAGE)
    try:
        headless_page.goto(site.url)
        robot, email = _model_point(headless_page, "robot"), _model_point(headless_page, "email")
        questions: List[str] = []

        async def would_approve(title: str, fields: Dict[str, Any]) -> Dict[str, Any]:
            questions.append(title)
            return {approval.APPROVAL_FIELD: True}

        def call(name: str, arguments: Dict[str, Any]) -> main.ToolCallDraft:
            return {"id": "h1", "name": name, "arguments": json.dumps(arguments)}

        refused = [
            await _run(call("cua_click_text", {"text": "I'm not a robot", "near": None}), would_approve, False),
            await _run(call("cua_click_point", {"point": robot}), would_approve, False),
            await _run(call("cua_submit_text", {"point": robot, "text": "x"}), would_approve, False),
            await _run(call("run_action_sequence", {"steps": [{"action": "click", "point": robot}]}), would_approve, False),
            await _run(call("run_action_sequence", {"steps": [{"action": "click_text", "text": "I'm not a robot"}]}),
                       would_approve, False),
            await _run(call("cua_click_text", {"text": "I'm not a robot", "near": None}), None, True),  # sürekli mod
        ]
        for result in refused:
            assert not result["ok"] and result["code"] == ACCESS_CHALLENGE_CODE and result["recoverable"] is False, result
            assert ACCESS_CHALLENGE_MARKER in result["error"]
        assert questions == [] and not (tmp_path / "audit.jsonl").exists()  # onay yoluna hiç düşmedi
        assert site.hits == []
        ordinary = await _run(call("cua_click_point", {"point": email}), would_approve, False)  # 'Verify email' sıradandır
        assert ordinary["ok"]
        headless_page._run(lambda page: page.wait_for_timeout(300))
        assert site.hits == ["/tiklandi?email"]
    finally:
        site.server.shutdown()


@pytest.mark.asyncio
async def test_human_check_elements_are_refused_in_every_role_and_never_fall_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, strict_wall_mode: None,
) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    cua = FakeCUA([_element(1, "AXCheckBox", "I'm not a robot"), _element(2, "AXButton", "Verify email"),
                   _described(3, "AXButton", "", "Verify you are human"), _element(4, "AXButton", "")])
    toolbox = _toolbox(cua)
    cua.ocr_text = "Ben robot değilim"
    questions: List[str] = []

    async def would_approve(title: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        questions.append(title)
        return {approval.APPROVAL_FIELD: True}

    for index in (1, 3, 4):  # onay kutusu, açıklaması doğrulama olan düğme, adsız düğmenin OCR metni
        result = await _run_with(toolbox, _element_call(index), would_approve, False)
        assert result["code"] == ACCESS_CHALLENGE_CODE and result["recoverable"] is False, (index, result)
    assert cua.clicks == [] and questions == []
    assert (await _run_with(toolbox, _element_call(2), would_approve, False))["ok"] and cua.clicks == ["sıradan"]
    # Eski numaralı liste yolu: şablon tıklamasına düşülmez
    template_clicks: List[str] = []
    monkeypatch.setattr(Toolbox, "_click_template", lambda self, app, path, confidence: template_clicks.append(path) or "şablon")
    cua.legacy_labels = ["Verify you're human"]
    smart: main.ToolCallDraft = {"id": "l2", "name": "smart_click", "arguments": json.dumps(
        {"app_name": "Uygulama", "element_id": 2, "template_path": "/tmp/yok.png", "confidence": 0.8})}
    assert (await _run_with(toolbox, smart, would_approve, False))["code"] == ACCESS_CHALLENGE_CODE
    assert template_clicks == [] and questions == []


@pytest.mark.asyncio
async def test_browse_url_clicks_a_human_check_label_in_bypass_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Bypass açıkken insan/bot doğrulaması etiketi tıklamayı engellemez: host onay kapısı yalnız ödeme/sipariş
    onayı düğmelerini sorar (sıkı moddaki sert ret davranışı human_verification_error testlerinde sınanır).
    """
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    page = "<p id='durum'>başlangıç</p><button id='r' onclick=\"durum.textContent='tıklandı'\">I'm not a robot</button>"
    url = "data:text/html;charset=utf-8," + urllib.parse.quote(page)
    toolbox = Toolbox()

    def browse(actions: List[Dict[str, Any]]) -> main.ToolCallDraft:
        return {"id": "b1", "name": "browse_url", "arguments": json.dumps({"url": url, "actions": actions})}

    async def would_approve(title: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        return {approval.APPROVAL_FIELD: True}

    try:
        clicked = await _run_with(toolbox, browse([{"action": "click", "selector": "#r", "value": None}]), would_approve, False)
        if str(clicked.get("code", "")).startswith("BROWSER_"):
            pytest.skip("Playwright Chromium kurulu değil")
        assert clicked["ok"] and clicked.get("code") != ACCESS_CHALLENGE_CODE
        # Doğrulama sayfası okunmaya devam eder ve uygulanacak strateji sonuca eklenir.
        assert "Bypass açık" in clicked["result"] and "I'm not a robot" in clicked["result"]
    finally:
        await toolbox.close_browser()


@pytest.mark.asyncio
async def test_human_check_element_is_refused_in_strict_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, strict_wall_mode: None,
) -> None:
    """
    Sıkı modda insan/bot doğrulaması etiketli öğe onay yoluna düşmeden reddedilir (kullanıcı onayıyla da geçilmez);
    hata kodu bot duvarının koduyla aynıdır. Bu kapı varsayılan (bypass) modda kapalıdır.
    """
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    cua = FakeCUA([_element(1, "AXCheckBox", "I'm not a robot")])

    refused = await _run_with(_toolbox(cua), _element_call(1), None, False)

    assert refused["code"] == ACCESS_CHALLENGE_CODE and refused["recoverable"] is False
    assert cua.clicks == []


@pytest.mark.asyncio
async def test_human_check_element_is_clicked_in_bypass_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Bypass açıkken (varsayılan) doğrulama kutusu engellenmez ve onay sorulmaz: öğe sıradan bir hedef gibi tıklanır,
    çünkü bypass'ın kendisi bu kutuyu geçmektir. GUI yolu tarayıcı yoluyla aynı politikayı izler.
    """
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    cua = FakeCUA([_element(1, "AXCheckBox", "I'm not a robot")])

    result = await _run_with(_toolbox(cua), _element_call(1), None, False)

    assert result["ok"] and result.get("code") != ACCESS_CHALLENGE_CODE
    assert cua.clicks == ["sıradan"]
