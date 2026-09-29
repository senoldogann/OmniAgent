"""GUI benchmark teşhis kaydının yapılandırılmış ve gizli metinsiz kalmasını sınar."""

import json
from pathlib import Path

from omniagent.app.model_retry import MODEL_ERROR_KIND_LABELS
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


def test_provider_failure_category_separates_infrastructure_from_code_errors() -> None:
    """
    Sağlayıcı/altyapı kesintisi yalnız ajan döngüsünün 'model çağrısı başarısız (<tür>)' nedeniyle sınıflanır
    (ModelCallFailed tüm APIError/ssl.SSLError'ı sarar). 'Kritik hata' metni ise ne içerirse içersin kod hatasıdır:
    içindeki 'rate_limit', 'quota', '429' gibi sözcükler sağlayıcı sınıfı sayılmaz.
    """
    table: list[tuple[str, str]] = [
        # Ajan döngüsünün sağlayıcı kesintisi nedeni (app/agent.py ModelCallFailed): tür etiketi sınıfı belirler
        ("model çağrısı başarısız (hız sınırı)", "provider_rate_limited"),
        ("model çağrısı başarısız (erişim veya bakiye hatası)", "provider_auth"),
        ("model çağrısı başarısız (zaman aşımı)", "provider_timeout"),
        ("model çağrısı başarısız (geçici hata)", "provider_unavailable"),
        ("model çağrısı başarısız (sunucu yeniden denemeyi reddetti)", "provider_unavailable"),
        ("model çağrısı başarısız (TLS sertifika doğrulaması başarısız)", "provider_unavailable"),
        # Eksik anahtar/model bir sağlayıcı kesintisi değil, yapılandırma eksiğidir
        ("Kritik hata: Kullanılabilir model yok: Ollama Cloud modeli veya bir API anahtarı gerekli.",
         "config_missing"),
        # Kod hataları: metindeki sağlayıcı benzeri iğneler yanıltmaz (eskiden yanlış pozitifti)
        ("Kritik hata: 'rate_limit'", "critical_error"),
        ("Kritik hata: 'quota'", "critical_error"),
        ("Kritik hata: Error code: 429 - {'error': 'usage limit'}", "critical_error"),
        ("Kritik hata: Error code: 503 - upstream", "critical_error"),
        ("Kritik hata: Error code: 401 - unauthorized", "critical_error"),
        ("Kritik hata: Connection error.", "critical_error"),
        ("Kritik hata: Request timed out.", "critical_error"),
        ("Kritik hata: Too Many Requests", "critical_error"),
        ("Kritik hata: Error code: 404 - model not found", "critical_error"),
        ("Kritik hata: Error code: 400 - {'error': 'bad request'}", "critical_error"),
        ("Kritik hata: Error code: 422 - rate limit alanı geçersiz", "critical_error"),
        ("zaman bütçesi (600sn) aşıldı", "time_limit"),
        # İzin/yapılandırma sonuçları sağlayıcı kesintisi ya da kod hatası değildir (eskiden other_failure / critical_error)
        ("yedek sağlayıcı izni yok", "config_missing"),
        ("Kritik hata: 'ollama-cloud' profili hazır değil (API anahtarı yok ya da model kurulu değil); hazır yedek "
         "sağlayıcılara (openai) geçilmedi: yedek sağlayıcı izni yok.", "config_missing"),
        # Kod hatası olarak kalanlar: yalnız TAM kalıplar sınıflanır
        ("Kritik hata: FallbackAuditFailed: yedek geçiş kaydı yazılamadı", "critical_error"),
        ("Kritik hata: 'x' profili bulunamadı", "critical_error"),
        ("yedek sağlayıcı izni yok ama başka bir şey", "other_failure"),
    ]
    for reason, expected in table:
        assert benchmark.failure_reason_category(reason) == expected, reason
    # Yalnız 'model çağrısı başarısız (<tür>)' sağlayıcı sınıfı üretir; kalıcı geçersiz istek kod gerilemesidir
    assert benchmark.provider_failure_category("Connection error.") is None
    assert benchmark.provider_failure_category("Kritik hata: Connection error.") is None
    assert benchmark.provider_failure_category("model çağrısı başarısız (geçersiz istek)") is None


def test_startup_failure_reason_from_the_real_policy_message_is_config_missing() -> None:
    """
    Başlangıç hatasının nedeni app/agent.py startup_failure'daki gibi 'Kritik hata: <gerçek ileti>' kurulur: ileti kalıbı
    değişirse (fallback_policy.startup_substitution_problem) sınıflama sessizce critical_error'a düşmesin.
    """
    from omniagent import fallback_policy

    for available, backends, images in ((frozenset({"openai"}), frozenset(), 0), (frozenset({"openai"}), frozenset({"openai"}), 2)):
        message = fallback_policy.startup_substitution_problem("ollama-cloud", available, backends, False, images)
        assert benchmark.failure_reason_category(f"Kritik hata: {message}") == "config_missing"
    # bg_compare'ın kullandığı sağlayıcı sınıfı bunlardan etkilenmez
    assert benchmark.provider_failure_category("yedek sağlayıcı izni yok") is None
    assert benchmark.provider_failure_category("Kritik hata: 'ollama-cloud' profili hazır değil") is None


def test_every_model_error_kind_has_an_explicit_benchmark_category() -> None:
    """Yeni bir hata türü eklenirse benchmark eşlemesi de güncellenmeli: 'geçersiz istek' dışındaki her tür sağlayıcı sınıfına iner."""
    for kind, label in MODEL_ERROR_KIND_LABELS.items():
        category = benchmark.provider_failure_category(f"model çağrısı başarısız ({label})")
        assert (category is None) == (kind == "permanent"), (kind, label, category)


def test_provenance_recorder_captures_backend_switch_without_content() -> None:
    """Sağlayıcı/model izi ve yedeğe geçişler kaydedilir; hedef, yanıt ve araç metni sızmaz."""
    provenance, emit = benchmark.provenance_recorder()
    # run_started hiç gelmezse (başlangıç hatası) iz boş ve sıfırdır
    assert provenance == {
        "started_backend": "", "started_model": "", "backend_changes": [], "stream_resets": 0,
        "model_retry_waits": 0, "provider_fallbacks": [],
    }

    emit({"kind": "run_started", "goal": "gizli hedef", "backend": "ollama-cloud", "model": "gemma4:cloud"})
    emit({"kind": "turn_started", "turn": 1, "max_turns": 25, "backend": "ollama-cloud", "model": "gemma4:cloud"})
    emit({"kind": "text_delta", "text": "gizli yanıt"})
    emit({"kind": "tool_started", "call_id": "call-1", "index": 0, "name": "cua_click_text",
          "preview": "gizli önizleme"})
    emit({"kind": "turn_started", "turn": 2, "max_turns": 25, "backend": "ollama-cloud", "model": "gemma4:cloud"})
    emit({"kind": "stream_reset", "reason": "APIConnectionError (ollama-cloud), yeniden deneniyor"})
    emit({"kind": "backend_changed", "backend": "openai", "model": "o3",
          "reason": "geçici hata; yalnız bu tur için fallback, sonraki tur ollama-cloud yeniden denenecek"})
    emit({"kind": "turn_started", "turn": 3, "max_turns": 25, "backend": "ollama-cloud", "model": "gemma4:cloud"})
    emit({"kind": "backend_changed", "backend": "openai", "model": "o3",
          "reason": "erişim/bakiye/hız sınırı; ollama-cloud bu görevde yeniden denenmeyecek"})
    # Yalnız model_retry aşamalı durum olayı sayılır; başka aşamalar (keşif, kurulum) ve metinleri yok sayılır
    emit({"kind": "integration_status", "stage": "model_retry", "text": "gizli durum metni",
          "completed": 1, "total": 0})
    emit({"kind": "integration_status", "stage": "discovery", "text": "gizli keşif", "completed": 1, "total": 3})
    emit({"kind": "provider_fallback", "from_backend": "ollama-cloud", "to_backend": "openai", "to_model": "o3",
          "processor": "gizli işleyici", "reason": "hız sınırı", "image_count": 1})

    assert provenance == {
        "started_backend": "ollama-cloud", "started_model": "gemma4:cloud",
        "backend_changes": [
            {"turn": 2, "backend": "openai", "model": "o3",
             "reason": "geçici hata; yalnız bu tur için fallback, sonraki tur ollama-cloud yeniden denenecek"},
            {"turn": 3, "backend": "openai", "model": "o3",
             "reason": "erişim/bakiye/hız sınırı; ollama-cloud bu görevde yeniden denenmeyecek"},
        ],
        "stream_resets": 1,
        "model_retry_waits": 1,
        "provider_fallbacks": [{
            "turn": 3, "from_backend": "ollama-cloud", "to_backend": "openai", "to_model": "o3",
            "reason": "hız sınırı",
        }],
    }
    assert "gizli" not in json.dumps(provenance, ensure_ascii=False)


def test_benchmark_seed_repeats_the_same_cases() -> None:
    first = benchmark.benchmark_run_id("chrome_ilan", 0, "cu-20260928")
    assert first == benchmark.benchmark_run_id("chrome_ilan", 0, "cu-20260928")
    assert first != benchmark.benchmark_run_id("chrome_ilan", 1, "cu-20260928")
    assert first != benchmark.benchmark_run_id("chrome_form", 0, "cu-20260928")
