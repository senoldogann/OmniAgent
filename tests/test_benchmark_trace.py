"""GUI benchmark teşhis kaydının yapılandırılmış ve gizli metinsiz kalmasını sınar."""

import json
from pathlib import Path

from omniagent.dev import benchmark
from omniagent.core.events import argument_point, argument_tag


def test_gui_trace_keeps_call_identity_without_screen_text() -> None:
    trace, emit = benchmark.gui_trace_recorder()
    emit({"kind": "turn_started", "turn": 2, "max_turns": 25,
          "backend": "ollama-cloud", "model": "test-model"})
    emit({"kind": "model_finished", "turn": 2, "seconds": 1.4,
          "usage": {"prompt_tokens": 10, "cached_tokens": 0, "completion_tokens": 2},
          "finish_reason": "tool_calls", "tool_call_count": 1, "empty_content": True})
    emit({"kind": "tool_started", "call_id": "call-1", "index": 0,
          "name": "cua_click_text", "preview": "parola: çok gizli"})
    emit({"kind": "tool_finished", "call_id": "call-1", "ok": False,
          "text": "çok gizli ekranda bulunamadı", "seconds": 0.2, "code": "TEXT_NOT_FOUND"})

    assert {key: value for key, value in trace[-1].items() if key != "target_tag"} == {
        "kind": "tool", "turn": 2, "call_id": "call-1", "tool": "cua_click_text",
        "route": "ocr", "observation": False, "ok": False, "seconds": 0.2,
        "code": "TEXT_NOT_FOUND",
    }
    assert len(trace[-1]["target_tag"]) == 16
    assert trace[1]["empty_content"] is True
    assert "çok gizli" not in json.dumps(trace, ensure_ascii=False)


def test_gui_trace_identifies_repeated_target_without_saving_preview() -> None:
    trace, emit = benchmark.gui_trace_recorder()
    for call_id in ("first", "second"):
        emit({"kind": "tool_started", "call_id": call_id, "index": 0,
              "name": "cua_click_text", "preview": "gizli hedef"})
        emit({"kind": "tool_finished", "call_id": call_id, "ok": True,
              "text": "gizli sonuç", "seconds": 0.25})

    assert trace[1]["repeat_of"] == "first"
    assert trace[0]["target_tag"] == trace[1]["target_tag"]
    assert benchmark.gui_trace_summary(trace) == {
        "turns": 0, "max_turns": None, "empty_model_answers": 0, "finish_reasons": {},
        "actions": 2, "observations": 0, "failed_calls": 0,
        "repeated_targets": 1, "route_seconds": {"ocr": 0.5},
    }
    assert "gizli" not in json.dumps(trace, ensure_ascii=False)


def test_gui_trace_attributes_empty_answer_and_loop_limit() -> None:
    """Boş yanıtın modelden, kesilmenin token sınırından geldiği toplamdan ayırt edilebilmeli."""
    trace, emit = benchmark.gui_trace_recorder()
    emit({"kind": "turn_started", "turn": 1, "max_turns": 25,
          "backend": "ollama-cloud", "model": "test-model"})
    emit({"kind": "model_finished", "turn": 1, "seconds": 2.0,
          "usage": {"prompt_tokens": 5, "cached_tokens": 0, "completion_tokens": 0},
          "finish_reason": "stop", "tool_call_count": 0, "empty_content": True})
    emit({"kind": "turn_started", "turn": 2, "max_turns": 25,
          "backend": "ollama-cloud", "model": "test-model"})
    emit({"kind": "model_finished", "turn": 2, "seconds": 1.0,
          "usage": {"prompt_tokens": 5, "cached_tokens": 0, "completion_tokens": 1},
          "finish_reason": "length", "tool_call_count": 0, "empty_content": False})

    summary = benchmark.gui_trace_summary(trace)

    assert summary["turns"] == 2
    assert summary["max_turns"] == 25
    assert summary["empty_model_answers"] == 1
    assert summary["finish_reasons"] == {"stop": 1, "length": 1}


def test_argument_tag_distinguishes_same_text_at_different_points() -> None:
    first = '{"text":"Aç","near":[765,444]}'
    second = '{"near":[765,243],"text":"Aç"}'
    equivalent = '{"near":[765,444],"text":"Aç"}'
    assert argument_tag("cua_click_text", first) == argument_tag("cua_click_text", equivalent)
    assert argument_tag("cua_click_text", first) != argument_tag("cua_click_text", second)
    assert argument_point("cua_click_text", first) == [765, 444]
    assert argument_point("write_file", '{"path":"/tmp/a","content":"secret"}') is None


def test_empty_model_answer_has_explicit_reason_category() -> None:
    assert benchmark.failure_reason_category("model boş yanıt döndü") == "empty_model_answer"
    assert benchmark.failure_reason_category("") == "completed"


def test_critical_error_reason_is_not_misread_as_verification_gap() -> None:
    """'Kritik hata: … doğrulanmadı' metni kritik hata olarak sınıflanmalı (sıra düzeltmesi)."""
    assert benchmark.failure_reason_category("Kritik hata: adım doğrulanmadı") == "critical_error"
    assert benchmark.failure_reason_category("sonuç doğrulanmadı") == "verification_gap"


def test_benchmark_seed_repeats_the_same_cases() -> None:
    first = benchmark.benchmark_run_id("chrome_ilan", 0, "cu-20260928")
    assert first == benchmark.benchmark_run_id("chrome_ilan", 0, "cu-20260928")
    assert first != benchmark.benchmark_run_id("chrome_ilan", 1, "cu-20260928")
    assert first != benchmark.benchmark_run_id("chrome_form", 0, "cu-20260928")
