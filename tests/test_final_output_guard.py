"""Kanıtsız final iddiası kullanıcıya akmadan host kararıyla değiştirilir."""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any, Iterator, List, Tuple

import pytest

from omniagent.app import agent as main
from omniagent.core.checkpoint import clear_checkpoint, save_checkpoint
from omniagent.core.events import AgentEvent
from omniagent.core.state import load_state
from omniagent.integrations.capabilities import CapabilityService


ScriptTurn = Tuple[str, List[dict[str, str]], List[AgentEvent]]


async def _run_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, goal: str,
    script: List[ScriptTurn],
) -> Tuple[main.RunReport, List[AgentEvent]]:
    """Modelin gerçekten akıttığı parçaları ve host olaylarını ayrı ayrı gözler."""
    events: List[AgentEvent] = []
    index = 0

    async def fake_model(
        clients: Any, messages: Any, schemas: Any, session_id: str,
        backend: str, emit: Any, should_stop: Any,
    ) -> Tuple[main.ModelTurn, str]:
        nonlocal index
        content, calls, deltas = script[min(index, len(script) - 1)]
        index += 1
        for delta in deltas:
            emit(delta)
        return {
            "content": content, "tool_calls": calls,
            "finish_reason": "tool_calls" if calls else "stop", "usage": main.ZERO_USAGE,
        }, backend

    monkeypatch.setattr(main, "_call_model_with_retries", fake_model)
    service = CapabilityService(tmp_path)
    try:
        report = await main.run_agent_with_callback(
            goal, events.append,
            {"requested_backend": None, "should_stop": lambda: False,
             "state_file": str(tmp_path / "memory.json"), "history": [],
             "integrations": service, "max_iterations": 5},
            {"ollama-cloud": object()},
        )
    finally:
        await service.close()
    return report, events


def _visible_text(events: List[AgentEvent]) -> str:
    """Tüm kullanıcı yüzeylerinin ortak `text_delta` akışı."""
    return "".join(event["text"] for event in events if event["kind"] == "text_delta")


def _final_text_precedes_model_finished(events: List[AgentEvent], expected: str) -> None:
    text_index = next(
        index for index, event in enumerate(events)
        if event["kind"] == "text_delta" and event["text"] == expected
    )
    assert events[text_index + 1]["kind"] == "model_finished"


@pytest.mark.asyncio
async def test_rejected_action_claim_never_reaches_user_surfaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    false_claim = "Dosya silindi."
    turn: ScriptTurn = (
        false_claim, [], [
            {"kind": "reasoning_delta", "text": false_claim},
            {"kind": "text_delta", "text": false_claim},
        ],
    )

    report, events = await _run_script(
        tmp_path, monkeypatch, "Masaüstündeki gereksiz dosyayı sil", [turn, turn],
    )

    assert not report["success"]
    assert false_claim not in _visible_text(events)
    assert not any(
        event["kind"] == "reasoning_delta" and false_claim in event["text"]
        for event in events
    )
    assert false_claim not in report["outcome"]
    assert report["outcome"].startswith("Doğrulanmadı:")
    assert false_claim not in report["exchange"]["answer"]
    assert [event["kind"] for event in events].count("model_finished") == 1
    _final_text_precedes_model_finished(events, report["outcome"])
    finished = next(event for event in events if event["kind"] == "run_finished")
    assert finished["outcome"] == report["outcome"]


@pytest.mark.asyncio
async def test_failed_history_omits_unverified_model_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("gerçek içerik", encoding="utf-8")
    read_call = {"id": "read-1", "name": "read_file",
                 "arguments": json.dumps({"path": str(target)})}
    false_claim = "Dosya silindi."
    claim_turn: ScriptTurn = (
        false_claim, [], [{"kind": "text_delta", "text": false_claim}],
    )
    script: List[ScriptTurn] = [
        (f"STATE:\nFACTS: {false_claim}", [read_call],
         [{"kind": "text_delta", "text": f"STATE:\nFACTS: {false_claim}"}]),
        claim_turn, claim_turn,
    ]

    report, events = await _run_script(tmp_path, monkeypatch, "Bu dosyayı sil", script)

    assert not report["success"]
    assert false_claim not in _visible_text(events)
    assert false_claim not in report["exchange"]["answer"]
    assert "Doğrulanmadı" in report["exchange"]["answer"]


@pytest.mark.asyncio
async def test_textual_tool_call_is_not_delivered_as_completed_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pseudo_call = 'call:write_file {"path":"/tmp/olmayan.txt","content":"bitti"}'
    turn: ScriptTurn = (pseudo_call, [], [{"kind": "text_delta", "text": pseudo_call}])

    report, events = await _run_script(
        tmp_path, monkeypatch, "Projede hesaplama kodunu değiştir", [turn, turn],
    )

    assert not report["success"]
    assert pseudo_call not in _visible_text(events)
    assert pseudo_call not in report["outcome"]
    assert "gerçek araç çağrısı" in report["reason"] or "yalnız metin" in report["reason"]


@pytest.mark.asyncio
async def test_rejected_source_claim_is_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    false_claim = "Kod değişikliğini yaptım."
    turn: ScriptTurn = (false_claim, [], [{"kind": "text_delta", "text": false_claim}])

    report, events = await _run_script(
        tmp_path, monkeypatch, "Projede hesaplama kodunu değiştir", [turn, turn],
    )

    assert not report["success"]
    assert false_claim not in _visible_text(events)
    assert false_claim not in report["outcome"]
    assert "write_file" in report["reason"] or "dosya" in report["reason"]


@pytest.mark.asyncio
async def test_unmet_wait_status_cannot_publish_ready_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    false_claim = "Status ready; görev tamamlandı."
    turn: ScriptTurn = (false_claim, [], [{"kind": "text_delta", "text": false_claim}])

    report, events = await _run_script(
        tmp_path, monkeypatch,
        "status 'ready' olana kadar aynı adresi kontrol et", [turn],
    )

    assert not report["success"]
    assert "beklenen status" in report["reason"]
    assert false_claim not in _visible_text(events)
    assert report["outcome"].startswith("Doğrulanmadı:")


@pytest.mark.asyncio
async def test_guarded_tool_turn_text_is_hidden_but_valid_final_is_delivered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "not.txt"
    call = {"id": "write-1", "name": "write_file",
            "arguments": json.dumps({"path": str(target), "content": "Merhaba\n"})}
    claim = "Dosyayı oluşturdum."
    script: List[ScriptTurn] = [
        ("STATE: Dosyayı yazdım", [call],
         [{"kind": "text_delta", "text": "STATE: Dosyayı yazdım"}]),
        (claim, [], [{"kind": "text_delta", "text": claim}]),
    ]

    report, events = await _run_script(
        tmp_path, monkeypatch, f"{target} dosyasını oluştur", script,
    )

    assert report["success"]
    assert target.read_text(encoding="utf-8") == "Merhaba\n"
    assert _visible_text(events) == claim
    _final_text_precedes_model_finished(events, claim)


@pytest.mark.asyncio
async def test_provider_reset_discards_previously_buffered_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "not.txt"
    call = {"id": "write-1", "name": "write_file",
            "arguments": json.dumps({"path": str(target), "content": "Merhaba\n"})}
    claim = "Dosyayı oluşturdum."
    script: List[ScriptTurn] = [
        ("STATE: yazılıyor", [call], [{"kind": "text_delta", "text": "STATE: yazılıyor"}]),
        (claim, [], [
            {"kind": "text_delta", "text": "ESKİ AKIŞ"},
            {"kind": "stream_reset", "reason": "sağlayıcı yeniden denendi"},
            {"kind": "text_delta", "text": claim},
        ]),
    ]

    report, events = await _run_script(
        tmp_path, monkeypatch, f"{target} dosyasını oluştur", script,
    )

    assert report["success"]
    assert "ESKİ AKIŞ" not in _visible_text(events)
    assert _visible_text(events) == claim
    assert any(event["kind"] == "stream_reset" for event in events)


@pytest.mark.asyncio
async def test_honest_failure_is_delivered_and_information_still_streams(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = "Yapamadım: erişim izni yok."
    failed: ScriptTurn = (failure, [], [{"kind": "text_delta", "text": failure}])
    report, events = await _run_script(
        tmp_path, monkeypatch, "Bu dosyayı sil", [failed],
    )
    assert not report["success"]
    assert report["outcome"] == failure
    assert _visible_text(events) == failure
    _final_text_precedes_model_finished(events, failure)

    answer = "4"
    simple: ScriptTurn = (answer, [], [{"kind": "text_delta", "text": answer}])
    report, events = await _run_script(
        tmp_path, monkeypatch, "İki artı iki kaçtır?", [simple],
    )
    assert report["success"]
    assert _visible_text(events) == answer
    _final_text_precedes_model_finished(events, answer)


# --- Nihai yanıt kod doğrulaması ve kısmi rapor (app/answer_fidelity.py, app/partial_report.py) ---
# Araçlar gerçektir (execute_shell printf, yerel HTTP siteye fetch_raw); yalnız model betiklidir.

GUARDED_CODE_GOAL: str = "execute_shell ile printf çalıştır; çıktıdaki ilan kodunu tek satır 'KOD: <kod>' yaz."
UNGUARDED_CODE_GOAL: str = "printf çıktısındaki ilan kodunu oku ve tek satır 'KOD: <kod>' yaz."
WALL_PAGE: str = (
    "<html><head><title>Just a moment...</title></head>"
    "<body><script>window._cf_chl_opt = {};</script></body></html>"
)


@pytest.fixture
def isolated_checkpoints(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Kontrol noktaları kullanıcı veri dizinine değil test dizinine yazılır."""
    runs_dir: Path = tmp_path / "runs"
    monkeypatch.setattr(main, "save_checkpoint", lambda **kwargs: save_checkpoint(runs_dir=runs_dir, **kwargs))
    monkeypatch.setattr(main, "clear_checkpoint", lambda session_id: clear_checkpoint(session_id, runs_dir=runs_dir))


@pytest.fixture
def wall_site() -> Iterator[str]:
    """Her yolda 403 ile Cloudflare tarzı ara sayfa dönen gerçek yerel HTTP sitesi; 'adres:port' değerini verir."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            payload: bytes = WALL_PAGE.encode("utf-8")
            self.send_response(403)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            return

    server: ThreadingHTTPServer = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def _shell_call(command: str) -> dict[str, str]:
    return {"id": "shell-1", "name": "execute_shell",
            "arguments": json.dumps({"command": command, "use_sudo": False, "timeout_seconds": None})}


def _fetch_call(url: str, call_id: str) -> dict[str, str]:
    return {"id": call_id, "name": "fetch_raw", "arguments": json.dumps({"url": url})}


def _notices(events: List[AgentEvent]) -> List[str]:
    return [event["text"] for event in events if event["kind"] == "notice"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("goal", "guarded"),
    [pytest.param(GUARDED_CODE_GOAL, True, id="kapili-gorev"), pytest.param(UNGUARDED_CODE_GOAL, False, id="kapisiz-gorev")],
)
async def test_transcription_error_is_corrected_from_tool_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_checkpoints: None, goal: str, guarded: bool,
) -> None:
    """Modelin 'IL-0C1EAF' yerine yazdığı 'IL-OC1EAF' (harf O) araç çıktısındaki değerle düzeltilir ve bildirilir."""
    script: List[ScriptTurn] = [
        ("STATE: kod okundu", [_shell_call("printf 'ilan kodu: IL-0C1EAF\\n'")],
         [{"kind": "text_delta", "text": "STATE: kod okundu"}]),
        ("KOD: IL-OC1EAF", [], [{"kind": "text_delta", "text": "KOD: IL-OC1EAF"}]),
    ]

    report, events = await _run_script(tmp_path, monkeypatch, goal, script)

    assert report["success"]
    assert report["outcome"] == "KOD: IL-0C1EAF"
    assert any("IL-OC1EAF → IL-0C1EAF" in notice for notice in _notices(events))
    assert report["metrics"]["answer_tokens_corrected"] == 1
    assert report["exchange"]["answer"] == "KOD: IL-0C1EAF"
    if guarded:
        # Kapılı görevde yanlış metin hiçbir yüzeye akmaz; düzeltilmiş metin model_finished'dan önce gelir.
        assert _visible_text(events) == "KOD: IL-0C1EAF"
        _final_text_precedes_model_finished(events, "KOD: IL-0C1EAF")


@pytest.mark.asyncio
async def test_unobserved_token_passes_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_checkpoints: None,
) -> None:
    """Araç çıktısında karşılığı olmayan (hesaplanmış ya da görselden okunmuş) kod düzeltilmez; yalnız ölçülür."""
    script: List[ScriptTurn] = [
        ("STATE: kod okundu", [_shell_call("printf 'ilan kodu: IL-0C1EAF\\n'")], []),
        ("KOD: IL-9F3A21", [], [{"kind": "text_delta", "text": "KOD: IL-9F3A21"}]),
    ]

    report, events = await _run_script(tmp_path, monkeypatch, GUARDED_CODE_GOAL, script)

    assert report["success"]
    assert report["outcome"] == "KOD: IL-9F3A21"
    assert not [notice for notice in _notices(events) if "düzeltildi" in notice or "doğrulanamadı" in notice]
    assert report["metrics"]["answer_tokens_unobserved"] == 1
    assert report["metrics"]["answer_tokens_corrected"] == 0
    assert report["metrics"]["answer_tokens_unverified"] == 0


@pytest.mark.asyncio
async def test_budget_end_reports_verified_findings_instead_of_empty_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_checkpoints: None,
) -> None:
    """
    Sınır ya da ilerleme yokluğuyla biten görev boş çıktı bırakmaz: araç çıktılarından alınan bulgu ve araç
    metninde birebir görülen kod rapora girer; modelin uydurduğu kod ve düzyazısı girmez. Geçmiş kaydı değişmez.
    """
    turn: ScriptTurn = (
        "STATE:\nFACTS: kod IL-FINAL-99, tahmin IL-UYDURMA-1",
        [_shell_call("printf 'ilan kodu: IL-FINAL-99\\n'")], [],
    )

    report, events = await _run_script(tmp_path, monkeypatch, GUARDED_CODE_GOAL, [turn])

    assert not report["success"]
    assert report["outcome"].startswith("Görev tamamlanamadı")
    assert "IL-FINAL-99" in report["outcome"]
    assert "IL-UYDURMA-1" not in report["outcome"] and "tahmin" not in report["outcome"]
    finished = next(event for event in events if event["kind"] == "run_finished")
    assert finished["outcome"] == report["outcome"]
    assert report["outcome"] in _notices(events)
    assert load_state(str(tmp_path / "memory.json"))["episodic_memory"][-1]["outcome"] == report["outcome"]
    assert report["exchange"]["answer"].startswith("[Görev tamamlanamadı")
    assert "Bulgular araç çıktılarından otomatik alındı" not in report["exchange"]["answer"]


@pytest.mark.asyncio
async def test_access_wall_report_names_the_wall_and_address_without_retry_advice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_checkpoints: None, wall_site: str,
    strict_wall_mode: None,
) -> None:
    """
    Gerçek fetch_raw yerel engel sayfasına çarpar, model aynı çağrıyı tekrarlayıp ilerleyemez. Rapor engeli ve
    adresi açıkça yazar (URL içi parola maskeli), 'başarısız tamamlama' dili ve engeli aşma önerisi taşımaz.
    """
    url: str = f"http://kullanici:gizlisifre123@{wall_site}/ilan/dogrulama"

    report, events = await _run_script(
        tmp_path, monkeypatch, f"{url} adresindeki ilan kodunu oku", [("", [_fetch_call(url, "wall-1")], [])],
    )

    assert not report["success"]
    assert report["outcome"].startswith("Erişim engeli:")
    assert f"{wall_site}/ilan/dogrulama" in report["outcome"]
    assert "belirteç=cloudflare-interstitial" in report["outcome"]
    assert "gizlisifre123" not in report["outcome"]
    assert "Görev tamamlanamadı" not in report["outcome"]
    assert not [word for word in ("başka araç", "proxy", "yeniden dene") if word in report["outcome"]]
    assert report["outcome"] in _notices(events)


@pytest.mark.asyncio
async def test_honest_access_wall_answer_is_not_rewritten_by_the_code_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_checkpoints: None, wall_site: str,
    strict_wall_mode: None,
) -> None:
    """
    Dürüstçe 'engel nedeniyle durdum' diyen yanıt, engelin adresini birebir yazdığında başka bir gözlenmiş kodla
    karıştırılıp bozulmaz: adres yalnız engel hatasında görülür (başarısız adım) ve o metin doğrulama korpusundadır.
    """
    wall_url: str = f"http://{wall_site}/ilan/IL-0C1EAF/dogrulama"
    honest: str = (
        f"Bot doğrulaması nedeniyle durdum: {wall_url} içerik yerine doğrulama sayfası döndürdü; "
        "engeli aşmaya çalışmadım."
    )
    script: List[ScriptTurn] = [
        ("", [_shell_call("printf 'onceki ilan: IL-OC1EAF\\n'")], []),
        ("", [_fetch_call(wall_url, "wall-1")], []),
        (honest, [], [{"kind": "text_delta", "text": honest}]),
    ]

    report, events = await _run_script(
        tmp_path, monkeypatch, "Yerel ilan sayfalarını oku ve durumu bildir", script,
    )

    assert report["success"]
    assert report["outcome"] == honest
    assert report["metrics"]["answer_tokens_corrected"] == 0
    assert not [notice for notice in _notices(events) if "düzeltildi" in notice]


@pytest.mark.asyncio
async def test_partial_report_failure_is_reported_and_does_not_skip_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_checkpoints: None,
) -> None:
    """Rapor derlenemezse hata temizlik uyarısı olarak bildirilir; bellek kaydı ve görev bitişi atlanmaz, çıktı boş kalır."""

    def broken_report(*arguments: object) -> str:
        raise TypeError("sahte rapor hatası")

    monkeypatch.setattr(main, "format_partial_report", broken_report)
    turn: ScriptTurn = ("STATE: x", [_shell_call("printf 'ilan kodu: IL-FINAL-99\\n'")], [])

    report, events = await _run_script(tmp_path, monkeypatch, GUARDED_CODE_GOAL, [turn])

    assert not report["success"] and report["outcome"] == ""
    assert any(notice.startswith("Kısmi rapor: TypeError") for notice in _notices(events))
    assert any(event["kind"] == "run_finished" for event in events)
    assert len(load_state(str(tmp_path / "memory.json"))["episodic_memory"]) == 1


@pytest.mark.asyncio
async def test_guarded_honest_access_wall_answer_needs_no_retry_and_is_not_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_checkpoints: None, wall_site: str,
    strict_wall_mode: None,
) -> None:
    """
    Eylem fiilli ('aç') hedefte bile engele çarpıp dürüstçe duran yanıt için kanıt kapısı yeniden deneme turu
    dayatmaz ve yanıtı 'Doğrulanmadı' ile değiştirmez: engeli aşmaya yönlendirme olmaz.
    """
    url: str = f"http://{wall_site}/ilan/dogrulama"
    honest: str = f"Bot doğrulaması nedeniyle durdum: {url} içerik yerine doğrulama sayfası döndürdü; engeli aşmaya çalışmadım."
    script: List[ScriptTurn] = [
        ("", [_fetch_call(url, "wall-1")], []),
        (honest, [], [{"kind": "text_delta", "text": honest}]),
    ]

    report, events = await _run_script(tmp_path, monkeypatch, f"{url} adresini aç ve sayfadaki başlığı bildir", script)

    assert report["success"]
    assert report["outcome"] == honest
    assert report["metrics"]["turns"] == 2
    assert not [notice for notice in _notices(events) if "gerçek işlem denemesi" in notice]
    _final_text_precedes_model_finished(events, honest)


@pytest.mark.asyncio
async def test_false_completion_claim_after_access_wall_is_still_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_checkpoints: None, wall_site: str,
) -> None:
    """Engele çarpıp sonra engeli anmadan 'yaptım' diyen yanıt muafiyetten yararlanmaz: normal kanıt kapısı reddeder."""
    url: str = f"http://{wall_site}/ilan/dogrulama"
    claim: str = "Dosya silindi."
    script: List[ScriptTurn] = [
        ("", [_fetch_call(url, "wall-1")], []),
        (claim, [], [{"kind": "text_delta", "text": claim}]),
    ]

    report, events = await _run_script(tmp_path, monkeypatch, f"{url} adresini aç ve sayfadaki başlığı bildir", script)

    assert not report["success"]
    assert report["outcome"].startswith("Doğrulanmadı:")
    assert claim not in _visible_text(events)


@pytest.mark.asyncio
async def test_access_wall_turn_gets_no_host_advice_to_switch_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_checkpoints: None, wall_site: str,
    strict_wall_mode: None,
) -> None:
    """Erişim engeli turunun ardından host 'farklı araç/yöntem seç' demez; modele yalnız aracın kendi iletisi gider."""
    url: str = f"http://{wall_site}/ilan/dogrulama"
    seen_user_messages: List[str] = []
    turns: List[ScriptTurn] = [("", [_fetch_call(url, "wall-1")], []), ("Bot doğrulaması nedeniyle durdum.", [], [])]

    async def scripted_model(
        clients: Any, messages: Any, schemas: Any, session_id: str, backend: str, emit: Any, should_stop: Any,
    ) -> Tuple[main.ModelTurn, str]:
        seen_user_messages[:] = [str(message["content"]) for message in messages if message["role"] == "user"]
        content, calls, _deltas = turns.pop(0)
        return {"content": content, "tool_calls": calls, "finish_reason": "tool_calls" if calls else "stop",
                "usage": main.ZERO_USAGE}, backend

    monkeypatch.setattr(main, "_call_model_with_retries", scripted_model)
    service = CapabilityService(tmp_path)
    try:
        await main.run_agent_with_callback(
            f"{url} adresindeki başlığı oku", lambda event: None,
            {"requested_backend": None, "should_stop": lambda: False, "state_file": str(tmp_path / "memory.json"),
             "history": [], "integrations": service, "max_iterations": 5},
            {"ollama-cloud": object()},
        )
    finally:
        await service.close()

    assert not [message for message in seen_user_messages if "ARAÇ HATASI" in message or "farklı bir araç" in message]
