import asyncio
from types import SimpleNamespace

import pytest

from omniagent import approval
from omniagent.app.tool_execution import _blocking_approver, require_approval
from omniagent.app.progress import failed_tool_recovery_message
from omniagent.integrations.runtime import CURRENT_RUNTIME, IntegrationRuntime
from omniagent.tools import Toolbox, ToolError
from omniagent.tools.types import TOOL_RUNTIME
from omniagent.tools import facade


@pytest.mark.asyncio
async def test_gui_publication_asks_host_with_actual_draft_and_denial_stops_it(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    questions = []

    async def answer(title, fields):
        questions.append((title, fields))
        return {"onay": False}

    runtime = IntegrationRuntime(lambda event: None, lambda: False, answer)
    runtime.gui_draft_text = "Rust projemde ölçtüğüm sonuçlar."
    token = CURRENT_RUNTIME.set(runtime)
    tool_token = TOOL_RUNTIME.set({
        "emit_output": lambda text: None, "should_stop": lambda: False,
        "request_approval_blocking": _blocking_approver("cua_click_point", asyncio.get_running_loop(), lambda: False),
    })
    try:
        box = Toolbox()
        with pytest.raises(ToolError) as refusal:
            await asyncio.to_thread(box._guard_click, "cua_click_point", "Gönderi yayınla", None, "Google Chrome", [])
        assert refusal.value.code == "APPROVAL_DENIED"
        assert len(questions) == 1
        assert runtime.gui_draft_text in questions[0][1]["_help"]
    finally:
        TOOL_RUNTIME.reset(tool_token)
        CURRENT_RUNTIME.reset(token)


def test_disabled_control_recovery_does_not_suggest_alternate_click_route():
    message = failed_tool_recovery_message(
        [{"id": "c", "name": "cua_click_element", "arguments": "{}"}],
        [{"tool_call_id": "c", "ok": False, "code": "ELEMENT_DISABLED", "error": "pasif"}],
    )
    assert "karakter sayacını" in message
    assert "tıklamayı deneme" in message
    assert "farklı bir araç/yöntem seç" not in message


def test_ordinary_controls_and_composer_opening_are_not_publication():
    for label in ("Kaydet", "Profil", "Gönderi oluştur", "Reply settings", "Rust post processing", "Gönderiler"):
        assert approval.communication_click_label(label) is False
    for label in ("Gönderi yayınla", "Yanıtla", "Post", "Publish", "Send message", "Paylaş"):
        assert approval.communication_click_label(label) is True


def test_coordinate_field_fill_reports_truncation_instead_of_success(monkeypatch):
    monkeypatch.setattr(facade, "screen_capture_granted", lambda: False)
    monkeypatch.setattr(facade, "_require_accessibility", lambda: None)
    monkeypatch.setattr(facade, "click_model_point", lambda *args: "Tıklandı.")
    monkeypatch.setattr(facade, "press_key_spec", lambda *args: None)
    monkeypatch.setattr(facade, "type_unicode_text", lambda *args: None)
    box = Toolbox()
    monkeypatch.setattr(box, "_input_geometry", lambda: {"point_width": 1000, "point_height": 1000})
    monkeypatch.setattr(box, "_require_input_target", lambda: None)
    monkeypatch.setattr(facade.gui_input, "focused_text_value", lambda app: "a" * 160)
    with pytest.raises(ToolError) as failure:
        box.cua_fill_field([500, 500], "a" * 208)
    assert failure.value.code == "TEXT_VALUE_MISMATCH"
    assert "160" in str(failure.value) and "208" in str(failure.value)


@pytest.mark.asyncio
async def test_denied_draft_cannot_be_sent_through_another_tool(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    questions = []

    async def answer(title, fields):
        questions.append(title)
        return {"onay": False}

    runtime = IntegrationRuntime(lambda event: None, lambda: False, answer)
    runtime.gui_draft_text = "Gönderilmesini istemediğim tam taslak."
    token = CURRENT_RUNTIME.set(runtime)
    try:
        for tool in ("cua_click_text", "cua_click_point"):
            with pytest.raises(ToolError) as failure:
                await require_approval(tool, approval.gui_communication_request(
                    tool, "Yayınla", "Google Chrome", runtime.gui_draft_text,
                ))
            assert failure.value.code == "APPROVAL_DENIED"
        assert len(questions) == 1
    finally:
        CURRENT_RUNTIME.reset(token)


@pytest.mark.asyncio
async def test_approval_needs_actual_target_card_not_a_free_text_draft_match(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    questions = []

    async def answer(title, fields):
        questions.append(title)
        return {"onay": False}

    runtime = IntegrationRuntime(lambda event: None, lambda: False, answer)
    runtime.gui_draft_text = "Tam ve değişmemiş yayın taslağı."
    token = CURRENT_RUNTIME.set(runtime)
    try:
        request = approval.gui_communication_request("cua_click_text", "Yayınla", "Google Chrome", runtime.gui_draft_text)
        with pytest.raises(ToolError) as failure:
            await require_approval("cua_click_text", request)
        assert failure.value.code == "APPROVAL_DENIED" and len(questions) == 1
    finally:
        CURRENT_RUNTIME.reset(token)


def test_disabled_point_guard_rereads_native_target_until_form_is_fixed(monkeypatch):
    box = Toolbox()
    live = {"enabled": False, "frame": {"x": 100, "y": 100, "w": 100, "h": 50}}
    native_ref = object()
    box._disabled_controls.append(SimpleNamespace(ref=native_ref, live=live))
    monkeypatch.setattr(facade, "points_to_model", lambda x, y, geometry: (int(x), int(y)))
    reads = []

    def read(ref, reader):
        assert ref is native_ref
        reads.append(ref)
        return live

    monkeypatch.setattr(facade.gui_input, "read_live_element", read)
    with pytest.raises(ToolError) as failure:
        box._refuse_disabled_point((150, 125), {})
    assert failure.value.code == "ELEMENT_DISABLED"
    box._refuse_disabled_point((300, 300), {})  # unrelated controls still usable
    live["enabled"] = True
    box._refuse_disabled_point((150, 125), {})
    assert len(reads) == 3 and box._disabled_controls == []
