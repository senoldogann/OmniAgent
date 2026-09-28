"""Gönderim/yayınlama tamamlanmadan sayfadan ayrılmayı engelleyen host korumasının testleri.

Canlı kayıtta ajan X'te gönderi yayınlama düğmesine tıkladıktan sonra gönderimin
tamamlanmasını beklemeden Keşfet sekmesine geçti; ileti kutusu taslakla kaldı ve gönderi
hiç yayınlanmadı.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from omniagent.app import agent as main
from omniagent.app.types import ToolCallDraft
from omniagent.app.verification import (
    COMMIT_UNVERIFIED_MESSAGE,
    commit_action_call,
    commit_navigation_call,
    executed_call_prefix,
    no_effect_action_key,
    text_entry_call,
)
from omniagent.tools import Toolbox, ToolError

GOAL: str = "chrome sekmesinde gönderi yayınla"
Message = Dict[str, Any]
Turn = Dict[str, Any]


def _call(name: str, call_id: str = "c1", **arguments: Any) -> ToolCallDraft:
    return {"id": call_id, "name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}


def _turn(*calls: Dict[str, str]) -> Turn:
    return {"content": "", "tool_calls": list(calls), "finish_reason": "tool_calls",
            "usage": main.ZERO_USAGE}


def _tool_text(messages: List[Message]) -> str:
    return "\n".join(
        str(message.get("content", "")) for message in messages if message.get("role") == "tool"
    )


def test_commit_action_targets_send_and_publish_controls() -> None:
    """Enter'a basan gönderim ve gönder/paylaş düğmeleri gönderim sayılır; sayaçlar sayılmaz."""
    assert commit_action_call(_call("cua_submit_text", point=[10, 20], text="merhaba"))
    assert commit_action_call(_call("cua_click_text", text="Gönderi yayınla"))
    assert commit_action_call(_call("cua_click_text", text="Gönder"))
    assert commit_action_call(_call("cua_click_text", text="Send"))
    assert commit_action_call(_call("cua_click_text", text="Reply"))
    # "Yanıtlar" bir sayaçtır, gönderim değildir: sözcük sınırı tam eşleşme ister.
    assert not commit_action_call(_call("cua_click_text", text="Yanıtlar"))
    assert not commit_action_call(_call("cua_click_text", text="Keşfet"))


def test_navigation_detection_requires_leaving_the_page() -> None:
    """URL'li chrome_active_tab ve gezinme etiketleri sayfadan ayrılma sayılır; okuma sayılmaz."""
    assert commit_navigation_call(_call("chrome_active_tab", url="https://x.com/explore"))
    assert not commit_navigation_call(_call("chrome_active_tab", url=None))
    assert commit_navigation_call(_call("cua_click_text", text="Keşfet"))
    assert not commit_navigation_call(_call("cua_click_text", text="Gönder"))


def test_text_entry_detection() -> None:
    """Gönderim koruması yalnız alana metin yazan araçlarla kurulur."""
    assert text_entry_call(_call("cua_fill_field", point=[1, 2], text="a"))
    assert text_entry_call(_call("cua_submit_text", point=[1, 2], text="a"))
    assert text_entry_call(_call("cua_type_text", text="a"))
    assert not text_entry_call(_call("chrome_active_tab", url="https://x.com"))


class _GuiRun:
    """Betikli modelle gerçek ajan döngüsünü koşturur; GUI araçları sahtedir."""

    def __init__(self, script: List[Turn]) -> None:
        self.script = script
        self.inputs: List[List[Message]] = []

    async def model(
        self, clients: Any, messages: List[Message], schemas: List[Dict[str, Any]], session_id: str,
        backend: str, emit: Any, should_stop: Any,
    ) -> Tuple[Turn, str]:
        self.inputs.append(messages)
        if len(self.inputs) > len(self.script):
            return {"content": "", "tool_calls": [], "finish_reason": "stopped",
                    "usage": main.ZERO_USAGE}, backend
        return self.script[len(self.inputs) - 1], backend

    def install(self, monkeypatch: pytest.MonkeyPatch, navigation_log: List[str]) -> None:
        monkeypatch.setattr(main, "_call_model_with_retries", self.model)
        monkeypatch.setattr(Toolbox, "cua_fill_field", lambda self, point, text: "Alan dolduruldu")
        monkeypatch.setattr(Toolbox, "cua_type_text", lambda self, text: "Yazıldı")
        monkeypatch.setattr(Toolbox, "cua_click_text", lambda self, text, near=None: f"tıklandı: {text}")
        monkeypatch.setattr(
            Toolbox, "chrome_active_tab",
            lambda self, url, new_tab=False: (navigation_log.append(str(url)), f"sekme: {url}")[1],
        )

    async def run(self, tmp_path: Path, **options: Any) -> Any:
        return await main.run_agent_with_callback(
            GOAL, lambda event: None,
            {"requested_backend": None, "should_stop": lambda: False,
             "state_file": str(tmp_path / "memory.json"), "history": [], **options},
            {"ollama-cloud": object()},
        )


@pytest.mark.asyncio
async def test_navigation_is_blocked_until_the_send_is_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Yazılan taslak gönderildikten sonra ilk gezinme engellenir; sayfa gerçekten değişmez."""
    navigation_log: List[str] = []
    session = _GuiRun([
        _turn(_call("cua_fill_field", "f1", point=[400, 280], text="trend gönderisi")),
        _turn(_call("cua_click_text", "k1", text="Gönderi yayınla")),
        _turn(_call("chrome_active_tab", "n1", url="https://x.com/explore")),
    ])
    session.install(monkeypatch, navigation_log)
    report = await session.run(tmp_path)

    assert navigation_log == []
    assert "Son gönderim/tamamlama henüz doğrulanmadı" in _tool_text(session.inputs[3])
    assert report["reason"] == "durduruldu"


@pytest.mark.asyncio
async def test_navigation_without_a_pending_send_still_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Taslak yokken gezinme serbesttir: koruma yalnız gönderilmemiş yazı varken kurulur."""
    navigation_log: List[str] = []
    session = _GuiRun([
        _turn(_call("cua_click_text", "k1", text="Gönderi yayınla")),
        _turn(_call("chrome_active_tab", "n1", url="https://x.com/explore")),
    ])
    session.install(monkeypatch, navigation_log)
    await session.run(tmp_path)

    assert navigation_log == ["https://x.com/explore"]
    assert COMMIT_UNVERIFIED_MESSAGE not in _tool_text(session.inputs[2])


def _install_observation_digests(
    monkeypatch: pytest.MonkeyPatch, digests: List[str],
) -> None:
    """GUI tur sonu gözlemlerini gerçek ekran kullanmadan deterministik digest'lerle döndürür."""
    remaining = iter(digests)

    async def observe(
        call_id: str, index: int, preview: str, toolbox: Toolbox,
        cache: Dict[str, Any], emit: Any, should_stop: Any,
    ) -> Tuple[Message, Dict[str, Any], str]:
        digest = next(remaining)
        step = main.sm.make_step_record("take_screenshot", "{}", True, f"gözlem:{digest}")
        return {"role": "user", "content": f"gözlem:{digest}"}, step, digest

    monkeypatch.setattr(main, "_observe_after_actions", observe)


@pytest.mark.asyncio
async def test_same_no_effect_gui_action_is_blocked_before_second_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disabled submit gibi ok=True fakat etkisiz click aynı state'te ikinci kez yürütülmez."""
    navigation_log: List[str] = []
    click_log: List[str] = []
    session = _GuiRun([
        _turn(_call("cua_fill_field", "f1", point=[400, 280], text="fazla uzun gönderi")),
        _turn(_call("cua_click_text", "k1", text="Gönderi yayınla")),
        _turn(_call("cua_click_text", "k2", text="Gönderi yayınla")),
    ])
    session.install(monkeypatch, navigation_log)
    monkeypatch.setattr(
        Toolbox, "cua_click_text",
        lambda self, text, near=None: (click_log.append(text), f"tıklandı: {text}")[1],
    )
    _install_observation_digests(monkeypatch, ["compose-state", "compose-state"])

    report = await session.run(tmp_path)

    assert click_log == ["Gönderi yayınla"]
    blocked = _tool_text(session.inputs[3])
    assert "RepeatedNoEffectAction" in blocked or "aynı ekran durumunda" in blocked
    assert report["reason"] == "durduruldu"


@pytest.mark.asyncio
async def test_no_effect_guard_is_cleared_after_visual_state_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Form düzeltildikten sonra aynı submit anahtarı yeniden gerçek GUI'ye gönderilebilir."""
    navigation_log: List[str] = []
    click_log: List[str] = []
    session = _GuiRun([
        _turn(_call("cua_fill_field", "f1", point=[400, 280], text="fazla uzun gönderi")),
        _turn(_call("cua_click_text", "k1", text="Gönderi yayınla")),
        _turn(_call("cua_fill_field", "f2", point=[400, 280], text="kısa gönderi")),
        _turn(_call("cua_click_text", "k2", text="Gönderi yayınla")),
    ])
    session.install(monkeypatch, navigation_log)
    monkeypatch.setattr(
        Toolbox, "cua_click_text",
        lambda self, text, near=None: (click_log.append(text), f"tıklandı: {text}")[1],
    )
    _install_observation_digests(
        monkeypatch, ["compose-state", "compose-state", "edited-state", "posted-state"],
    )

    report = await session.run(tmp_path)

    assert click_log == ["Gönderi yayınla", "Gönderi yayınla"]
    assert "RepeatedNoEffectAction" not in _tool_text(session.inputs[4])
    assert report["reason"] == "durduruldu"


def test_no_effect_action_key_normalizes_equivalent_arguments() -> None:
    """Aynı hedef farklı anahtar sırasıyla yazılsa da tek kararlı anahtar üretir; nokta ayrımı korunur."""
    first = _call("cua_click_text", "a", text="Gönderi yayınla", near=[765, 444])
    equivalent = {
        "id": "b", "name": "cua_click_text",
        "arguments": '{"near":[765,444],"text":"Gönderi yayınla"}',
    }
    elsewhere = _call("cua_click_text", "c", text="Gönderi yayınla", near=[765, 243])

    assert no_effect_action_key(first) == no_effect_action_key(equivalent)
    assert no_effect_action_key(first) != no_effect_action_key(elsewhere)
    assert no_effect_action_key(first).startswith("cua_click_text:")


def test_no_effect_action_key_is_none_outside_the_guard() -> None:
    """Tekrarı meşru olan tuş/okuma ve çok adımlı araçlar guard dışında kalır."""
    assert no_effect_action_key(_call("cua_press_key", key="Return")) is None
    assert no_effect_action_key(_call("run_action_sequence", actions=[{"action": "click"}])) is None
    assert no_effect_action_key(_call("take_screenshot")) is None
    assert no_effect_action_key(_call("cua_read_visible_text")) is None


def test_no_effect_action_key_covers_the_safe_direct_inputs() -> None:
    """Güvenli doğrudan GUI girdileri guard kapsamında olmalı."""
    calls = [
        _call("cua_click_point", "p", point=[10, 20]),
        _call("cua_type_text", "t", text="merhaba"),
        _call("cua_submit_text", "s", point=[1, 2], text="a"),
        _call("cua_fill_field", "f", point=[1, 2], text="a"),
        _call("cua_click_text", "ct", text="Gönder"),
        _call("cua_scroll", "sc", direction="down"),
        _call("cua_click", "cl", app_name="Safari"),
        _call("smart_click", "sm", app_name="Safari"),
    ]
    assert all(no_effect_action_key(call) is not None for call in calls)


def test_sequence_commit_and_navigation_are_detected() -> None:
    """Sıralı GUI adımları taslak, gönderim ve sayfadan ayrılma korumasına katılır."""
    assert text_entry_call(_call("run_action_sequence", steps=[
        {"action": "click", "point": [200, 300]},
        {"action": "type", "text": "Merhaba"},
    ]))
    assert commit_action_call(_call("run_action_sequence", steps=[
        {"action": "click_text", "text": "Gönderi yayınla"},
    ]))
    assert commit_navigation_call(_call("run_action_sequence", steps=[
        {"action": "click_text", "text": "Keşfet"},
    ]))


@pytest.mark.asyncio
async def test_navigation_after_send_in_same_tool_batch_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Model tek turda doldur/gönder/gez çağırsa da taslak koruması çalışmalı."""
    navigation_log: List[str] = []
    session = _GuiRun([
        _turn(
            _call("cua_fill_field", "f1", point=[400, 280], text="trend gönderisi"),
            _call("cua_click_text", "k1", text="Gönderi yayınla"),
            _call("chrome_active_tab", "n1", url="https://x.com/explore"),
        ),
    ])
    session.install(monkeypatch, navigation_log)

    await session.run(tmp_path)

    assert navigation_log == []


@pytest.mark.asyncio
async def test_unrelated_click_does_not_verify_pending_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gönderimden sonraki rastgele tıklama, gezinme kilidini kaldırmamalı."""
    navigation_log: List[str] = []
    session = _GuiRun([
        _turn(_call("cua_fill_field", "f1", point=[400, 280], text="trend gönderisi")),
        _turn(_call("cua_click_text", "k1", text="Gönderi yayınla")),
        _turn(_call("cua_click_text", "k2", text="Yardım")),
        _turn(_call("chrome_active_tab", "n1", url="https://x.com/explore")),
    ])
    session.install(monkeypatch, navigation_log)

    await session.run(tmp_path)

    assert navigation_log == []


@pytest.mark.asyncio
async def test_send_then_navigation_inside_sequence_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tek araç içinde gönderimden sonra gezinme de yürütülmemeli."""
    executed: List[str] = []
    session = _GuiRun([
        _turn(_call("run_action_sequence", "seq1", steps=[
            {"action": "type", "text": "Merhaba"},
            {"action": "click_text", "text": "Gönderi yayınla"},
            {"action": "click_text", "text": "Keşfet"},
        ])),
    ])
    session.install(monkeypatch, [])
    monkeypatch.setattr(
        Toolbox, "run_action_sequence",
        lambda self, steps: (executed.append("sequence"), "tamamlandı")[1],
    )

    await session.run(tmp_path)

    assert executed == []


@pytest.mark.asyncio
async def test_goal_report_in_send_turn_waits_for_delivery_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Aynı turdaki gönderim, sürekli modun hedef onayını ekran kanıtına kadar bekletir."""
    questions: List[str] = []
    session = _GuiRun([
        _turn(_call("cua_fill_field", "f1", point=[400, 280], text="trend gönderisi")),
        _turn(
            _call("cua_click_text", "k1", text="Gönderi yayınla"),
            _call("report_goal_met", "g1", summary="Gönderi yayınlandı.", evidence_call_ids=["f1", "k1"]),
        ),
    ])
    session.install(monkeypatch, [])

    async def answer(title: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        questions.append(title)
        return {"yanit": "evet"}

    report = await session.run(tmp_path, run_mode="continuous", answer=answer)

    assert questions == []
    assert report["reason"] == "durduruldu"
    assert "Son gönder/paylaş eyleminin tamamlandığı henüz doğrulanmadı" in _tool_text(session.inputs[2])


@pytest.mark.asyncio
async def test_partial_sequence_send_blocks_goal_report_and_navigation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gönder adımı çalışıp sonraki adım hata verirse erken bitiş ve gezinme engellenir."""
    questions: List[str] = []
    navigation_log: List[str] = []
    session = _GuiRun([
        _turn(_call("cua_fill_field", "f1", point=[400, 280], text="trend gönderisi")),
        _turn(
            _call("run_action_sequence", "s1", steps=[
                {"action": "click_text", "text": "Gönderi yayınla"},
                {"action": "click_text", "text": "Yardım"},
            ]),
            _call("report_goal_met", "g1", summary="Gönderi yayınlandı.", evidence_call_ids=["f1"]),
        ),
        _turn(_call("chrome_active_tab", "n1", url="https://x.com/explore")),
    ])
    session.install(monkeypatch, navigation_log)

    def partial_sequence(self: Toolbox, steps: List[Dict[str, Any]]) -> str:
        raise ToolError("İkinci tıklama başarısız", "TEXT_NOT_FOUND", True, completed_steps=1)

    async def answer(title: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        questions.append(title)
        return {"yanit": "evet"}

    monkeypatch.setattr(Toolbox, "run_action_sequence", partial_sequence)
    report = await session.run(tmp_path, run_mode="continuous", answer=answer)

    assert questions == []
    assert navigation_log == []
    assert report["reason"] == "durduruldu"
    assert "Son gönder/paylaş eyleminin tamamlandığı henüz doğrulanmadı" in _tool_text(session.inputs[2])


def test_partial_sequence_guard_uses_only_executed_steps() -> None:
    """Başarısız dizinin henüz yürümemiş gönderimi korumayı tetiklemez."""
    sequence = _call("run_action_sequence", steps=[
        {"action": "click_text", "text": "Yardım"},
        {"action": "click_text", "text": "Gönderi yayınla"},
    ])
    prefix = executed_call_prefix(sequence, {
        "tool_call_id": "c1", "ok": False, "completed_steps": 1,
    })
    assert prefix is not None
    assert not commit_action_call(prefix)
