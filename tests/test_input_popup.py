"""Onay kartı: doğru ekran, güvenli varsayılan ve istek başına tek yanıt."""
from omniagent.ui.input_popup import PopupDecision, confirmation_field, popup_frame
from concurrent.futures import Future
from datetime import datetime
from types import MethodType, SimpleNamespace
import threading

from omniagent.ui import app as ui


def test_popup_uses_top_right_of_selected_screen_including_negative_origin():
    assert popup_frame((-1920, 0, 1920, 1080), 460, 360) == (-480, 700, 460, 360)
    assert popup_frame((0, 0, 1440, 875), 460, 360) == (960, 495, 460, 360)


def test_only_single_boolean_forms_can_be_answered_by_allow_deny():
    assert confirmation_field({"onay": {"type": "boolean"}, "_help": "Gönderi"}) == "onay"
    assert confirmation_field({"yanit": {"type": "string"}}) is None
    assert confirmation_field({"onay": {"type": "boolean"}, "amount": {"type": "string"}}) is None


def test_closed_or_expired_popup_cannot_deliver_late_approval():
    answers = []
    decision = PopupDecision(answers.append)
    decision.close()
    decision.choose(True)
    assert answers == []


def test_decision_is_delivered_exactly_once_and_denial_is_explicit():
    answers = []
    decision = PopupDecision(answers.append)
    decision.choose(False)
    decision.choose(True)
    assert answers == [False]


def test_ui_native_popup_delivers_only_its_own_future_and_dismisses(monkeypatch):
    callbacks = []
    closed = []

    def create(title, detail, callback):
        callbacks.append(callback)
        assert "Gerçek taslak" in detail
        return SimpleNamespace(destroy=lambda: closed.append(True))

    monkeypatch.setattr(ui, "create_confirmation_popup", create)
    first, second = Future(), Future()
    app = SimpleNamespace(
        _stop_event=threading.Event(), _input_futures={"first": first, "second": second},
        _input_windows={}, _loop=SimpleNamespace(call_soon_threadsafe=lambda callback: callback()),
    )
    app._answer_input = MethodType(ui.OmniUI._answer_input, app)
    ui.OmniUI._show_input(app, "first", "Yayınlama izni", {
        "onay": {"type": "boolean"}, "_help": "Gerçek taslak",
    }, datetime.now())
    assert "first" in app._input_windows
    callbacks[0](False)
    assert first.result() == {"onay": False}
    assert not second.done()
    assert closed == [True] and not app._input_windows
    callbacks[0](True)
    assert first.result() == {"onay": False}
