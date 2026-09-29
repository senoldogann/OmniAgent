"""Çift tıklama ve sürükle-bırak: eylem yönlendirmesi ve macOS'a giden Quartz olay dizisi."""
from types import SimpleNamespace
from typing import Any, List, Tuple

import pytest

from omniagent import tools
from omniagent.tools import ToolError, Toolbox, screen
from omniagent.app.verification import gui_verification_needed, needs_action_observation
from omniagent.core import state as sm

GEOMETRY: tools.ScreenGeometry = {"point_width": 2000, "point_height": 1000, "model_width": 1000, "model_height": 1000}


def _toolbox(monkeypatch: pytest.MonkeyPatch) -> Toolbox:
    monkeypatch.setattr(tools, "_require_accessibility", lambda: None)
    monkeypatch.setattr(tools, "screen_capture_granted", lambda request=False: False)
    monkeypatch.setattr(tools, "current_geometry", lambda: GEOMETRY)
    return Toolbox()


def test_click_routes_single_double_and_drag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tek tıklama eski yolu korur; clicks=2 çoklu tıklamaya, drag sürüklemeye gider."""
    calls: List[Tuple[Any, ...]] = []
    monkeypatch.setattr(tools, "click_model_point", lambda x, y, button, geometry: calls.append(("click", x, y, button)) or "tek")
    monkeypatch.setattr(tools, "multi_click_model_point",
                        lambda x, y, button, clicks, geometry: calls.append(("multi", x, y, button, clicks)) or "çift")
    monkeypatch.setattr(tools, "drag_model_points",
                        lambda start, end, button, geometry: calls.append(("drag", start, end, button)) or "sürüklendi")
    box = _toolbox(monkeypatch)
    result = box.run_action_sequence([
        {"action": "click", "point": [10, 20]},
        {"action": "click", "point": [30, 40], "clicks": 2},
        {"action": "drag", "point": [50, 60], "to": [700, 800]},
    ])
    assert calls == [
        ("click", 10, 20, "left"),
        ("multi", 30, 40, "left", 2),
        ("drag", (50, 60), (700, 800), "left"),
    ]
    assert "sürüklendi" in result


def test_sequence_reads_each_detail_after_its_own_text_click(monkeypatch: pytest.MonkeyPatch) -> None:
    """Liste öğeleri tek araç turunda sırayla açılıp okunabilir."""
    box = _toolbox(monkeypatch)
    calls: List[Tuple[Any, ...]] = []
    monkeypatch.setattr(box, "cua_click_text", lambda text, near: calls.append(("click", text, near)) or text)
    monkeypatch.setattr(box, "cua_read_scrollable",
                        lambda point, max_pages: calls.append(("read", point, max_pages)) or "İlan kodu: K-1")

    result = box.run_action_sequence([
        {"action": "click_text", "text": "İlk ilan", "near": [80, 200]},
        {"action": "read_scrollable", "point": [600, 500], "max_pages": 15},
        {"action": "click_text", "text": "İkinci ilan", "near": [80, 400]},
        {"action": "read_scrollable", "point": [600, 500], "max_pages": 15},
    ])

    assert calls == [
        ("click", "İlk ilan", [80, 200]), ("read", [600, 500], 15),
        ("click", "İkinci ilan", [80, 400]), ("read", [600, 500], 15),
    ]
    assert result.count("İlan kodu: K-1") == 2


@pytest.mark.parametrize("step", [
    {"action": "click", "point": [1, 2], "clicks": 5},
    {"action": "drag", "point": [1, 2]},
    {"action": "drag", "point": [1, 2], "to": "3,4"},
])
def test_bad_mouse_steps_fail_explicitly(monkeypatch: pytest.MonkeyPatch, step: dict) -> None:
    """Geçersiz tıklama sayısı veya eksik/bozuk hedef nokta sessizce çalışmaz."""
    monkeypatch.setattr(tools, "multi_click_model_point", lambda *args: pytest.fail("çalışmamalı"))
    monkeypatch.setattr(tools, "drag_model_points", lambda *args: pytest.fail("çalışmamalı"))
    with pytest.raises(ToolError) as error:
        _toolbox(monkeypatch).run_action_sequence([step])
    assert error.value.code in ("INVALID_ACTION_PARAMS", "INVALID_POINT")


def _fake_quartz(monkeypatch: pytest.MonkeyPatch) -> List[Tuple[str, Any, Any, int]]:
    """Quartz olaylarını (tür, konum, düğme, tıklama durumu) olarak kaydeder."""
    posted: List[Tuple[str, Any, Any, int]] = []

    def create(source: object, kind: str, point: Tuple[float, float], button: str) -> dict:
        return {"kind": kind, "point": point, "button": button, "state": 0}

    def set_field(event: dict, field: str, value: int) -> None:
        assert field == "clickState"
        event["state"] = value

    fake = SimpleNamespace(
        kCGEventLeftMouseDown="down", kCGEventLeftMouseUp="up", kCGEventLeftMouseDragged="dragged",
        kCGMouseButtonLeft="L", kCGEventRightMouseDown="rdown", kCGEventRightMouseUp="rup",
        kCGEventRightMouseDragged="rdragged", kCGMouseButtonRight="R",
        kCGEventOtherMouseDown="odown", kCGEventOtherMouseUp="oup", kCGEventOtherMouseDragged="odragged",
        kCGMouseButtonCenter="C", kCGMouseEventClickState="clickState", kCGHIDEventTap="tap",
        CGEventCreateMouseEvent=create, CGEventSetIntegerValueField=set_field,
        CGEventPost=lambda tap, event: posted.append((event["kind"], event["point"], event["button"], event["state"])),
    )
    monkeypatch.setattr(screen, "Quartz", fake)
    monkeypatch.setattr(screen.pyautogui, "moveTo", lambda x, y: None)
    monkeypatch.setattr(screen.time, "sleep", lambda seconds: None)
    return posted


def test_double_click_sets_increasing_click_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """macOS çift tıklamayı yalnız tıklama durumu 2 olan ikinci basma/bırakmadan anlar."""
    posted = _fake_quartz(monkeypatch)
    text = screen.multi_click_model_point(100, 200, "left", 2, GEOMETRY)
    assert [(kind, state) for kind, _, _, state in posted] == [("down", 1), ("up", 1), ("down", 2), ("up", 2)]
    assert all(point == (200, 200) and button == "L" for _, point, button, _ in posted)
    assert text.startswith("Çift tıklandı")


def test_drag_holds_moves_through_intermediate_points_and_releases(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sürükleme: başlangıçta bas, ara sürükleme olaylarıyla git, hedefte bırak."""
    posted = _fake_quartz(monkeypatch)
    screen.drag_model_points((100, 100), (600, 900), "left", GEOMETRY)
    kinds = [kind for kind, _, _, _ in posted]
    assert kinds[0] == "down" and kinds[-1] == "up"
    assert kinds[1:-1] == ["dragged"] * screen.MOUSE_DRAG_STEPS
    assert posted[0][1] == (200, 100)
    assert posted[-2][1] == (1200, 900)
    assert posted[-1][1] == (1200, 900)


def test_unknown_mouse_button_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_quartz(monkeypatch)
    with pytest.raises(ToolError) as error:
        screen.multi_click_model_point(1, 1, "thumb", 2, GEOMETRY)
    assert error.value.code == "INVALID_BUTTON"


def test_partial_sequence_marks_executed_steps_for_observation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Son adım hata verse de önceki tıklama gerçek eylemdir ve gözlemlenir."""
    box = _toolbox(monkeypatch)
    monkeypatch.setattr(tools, "click_model_point", lambda x, y, button, geometry: "tıklandı")
    steps = [{"action": "click", "point": [20, 30]}, {"action": "bilinmeyen"}]

    with pytest.raises(ToolError) as caught:
        box.run_action_sequence(steps)

    assert caught.value.completed_steps == 1
    call = {"id": "partial", "name": "run_action_sequence", "arguments": '{"steps":[]}'}
    result = {"tool_call_id": "partial", "ok": False, "completed_steps": 1, "error": str(caught.value)}
    assert needs_action_observation([call], [result])
    record = sm.make_step_record("run_action_sequence", "{}", False, str(caught.value), partial_steps=1)
    assert gui_verification_needed([record])


def _keyboard_sequence_events(monkeypatch: pytest.MonkeyPatch) -> List[str]:
    """Tıklama, klavye ve ön plan denetimi olaylarını tek listeye SIRAYLA yazan yamalar."""
    events: List[str] = []
    monkeypatch.setattr(tools, "click_model_point", lambda x, y, button, geometry: events.append("click") or "tıklandı")
    monkeypatch.setattr(tools, "type_unicode_text", lambda text: events.append(f"type:{text}"))
    monkeypatch.setattr(tools, "press_key_spec", lambda key: events.append(f"key:{key}") or "basıldı")
    monkeypatch.setattr(tools, "require_front_app", lambda expected, wait_seconds: events.append(f"front:{expected}"))
    monkeypatch.setattr(tools, "require_no_sensitive_front", lambda wait_seconds: events.append("sensitive"))
    return events


def test_sequence_guards_each_keyboard_step_right_before_it_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Tıklama uygulamayı öne getirir ama etkinleşme ms sürer: her type/press'ten hemen önce ön plan doğrulanır. cmd+space
    (Spotlight) hassas ön plandan çıkış yoludur: denetlenmez ve sonrasında hedef beklentisi bırakılır.
    """
    events = _keyboard_sequence_events(monkeypatch)
    box = _toolbox(monkeypatch)
    box._input_app = "Notes"
    box.run_action_sequence([
        {"action": "click", "point": [10, 20]}, {"action": "type", "text": "a"}, {"action": "press", "key": "enter"},
        {"action": "press", "key": "cmd+space"}, {"action": "type", "text": "b"},
    ])
    assert events == ["click", "front:Notes", "type:a", "front:Notes", "key:enter", "key:cmd+space", "sensitive", "type:b"]
    assert box._input_app is None


def test_sequence_stops_at_a_failed_foreground_check_and_reports_completed_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    events = _keyboard_sequence_events(monkeypatch)
    mismatch = ToolError("Ön planda Terminal var", "FOREGROUND_MISMATCH", True)

    def refuse(wait_seconds: float) -> None:
        raise mismatch

    monkeypatch.setattr(tools, "require_no_sensitive_front", refuse)
    with pytest.raises(ToolError) as caught:
        _toolbox(monkeypatch).run_action_sequence([
            {"action": "click", "point": [10, 20]}, {"action": "type", "text": "gizli"}, {"action": "press", "key": "enter"},
        ])
    assert caught.value.code == "FOREGROUND_MISMATCH" and caught.value.recoverable
    assert caught.value.completed_steps == 1 and "Tamamlanan adımlar" in str(caught.value)
    assert events == ["click"]  # denetim başarısız: ne yazı ne tuş gitti, sonraki adım çalışmadı


def test_sequence_preserves_long_scrollable_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """Birden çok ilanı tek çağrıda okurken orta bölgedeki değerler kaybolmamalı."""
    box = _toolbox(monkeypatch)
    middle = "ORTADAKİ KRİTİK DEĞER: 6742"
    observation = "başlangıç\n" + "a" * 1500 + middle + "b" * 1500 + "\nson"
    monkeypatch.setattr(box, "cua_read_scrollable", lambda point, max_pages: observation)

    result = box.run_action_sequence([
        {"action": "read_scrollable", "point": [600, 500], "max_pages": 15},
    ])

    assert middle in result
    assert observation in result


def test_input_pause_is_reduced_but_failsafe_stays_enabled() -> None:
    """pyautogui'nin her çağrı sonrası beklemesi düşürülür; köşe acil durdurması (FAILSAFE) kullanıcı için açık kalır."""
    assert 0 < screen.pyautogui.PAUSE < 0.1  # kütüphane varsayılanı 0,1 sn
    assert screen.pyautogui.PAUSE == screen.INPUT_PAUSE_SECONDS
    assert screen.pyautogui.FAILSAFE is True


def test_fill_and_submit_wait_for_field_focus_between_click_and_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Alan doldurma/gönderme tıklamadan sonra alanın odak alması için beklenir: PAUSE düşürülünce eski >=0,1 sn tıklama-yazma
    aralığı bu bekleme ile korunur; ilk klavye olayından (cmd+a) önce, ön plan doğrulamasından bağımsız gelir.
    """
    events = _keyboard_sequence_events(monkeypatch)
    monkeypatch.setattr(tools.time, "sleep", lambda seconds: events.append(f"sleep:{seconds}"))
    box = _toolbox(monkeypatch)
    box._input_app = "Notes"
    box.cua_fill_field([10, 20], "ad")
    waits = [event for event in events if event.startswith("sleep:")]
    assert len(waits) == 1 and float(waits[0].removeprefix("sleep:")) == tools.FIELD_FOCUS_WAIT_SECONDS
    assert events == ["click", waits[0], "front:Notes", "key:cmd+a", "type:ad"]
    events.clear()
    box.cua_submit_text([10, 20], "ad")
    assert events == ["click", waits[0], "front:Notes", "key:cmd+a", "type:ad", "key:enter"]
