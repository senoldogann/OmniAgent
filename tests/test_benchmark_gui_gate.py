"""
chrome_benzer'ın asıl geçme koşulu (dev/benchmark.py:run_one içindeki
`wrong_target_clicks == 0` AND kapısı) daha önce hiçbir testte uçtan uca koşmuyordu; yalnız
parçaları ayrı test ediliyordu (metin kontrolü test_benchmark.py'de, metrik sayımı
test_gui_metrics_separate_clicks_from_settle_records'ta). Bu dosya modelin YANLIŞ (decoy)
düğmeye tıklayıp yine de doğru kodu 'iddia ettiği' senaryoyu gerçek run_one() üzerinden,
gerçek headless Chromium ve gerçek DOM hit-testing ile koşturur.
"""
import asyncio
import json
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any, Dict, List, Tuple

import pytest

from omniagent import tools
from omniagent.app import agent as main
from omniagent.dev import benchmark, headless_screen

# headless_screen.install() bu modül seviyesindeki adları kalıcı olarak değiştirir
# (bkz. dev/headless_screen.py:236-244); testten sonra geri almazsak sonraki testler
# (gerçek macOS araçlarını bekleyenler dahil) sahte Toolbox ile çalışmaya devam eder.
_PATCHED_TOOLS_ATTRS: Tuple[str, ...] = (
    "screen_capture_granted", "_require_screen_capture", "_require_accessibility",
    "click_model_point", "move_model_point", "type_unicode_text", "press_key_spec", "post_scroll",
)


@pytest.fixture
def headless_page():
    saved_tools: Dict[str, object] = {name: getattr(tools, name) for name in _PATCHED_TOOLS_ATTRS}
    saved_toolbox = main.Toolbox
    try:
        page = headless_screen.HeadlessPage()
    except Exception as error:  # noqa: BLE001 - Playwright'ın kendi hata tipi burada önemli değil
        # CI/geliştirme makinesinde `playwright install` hiç çalıştırılmamış olabilir
        # (repo bunu hiçbir yerde çağırmıyor); bu test o zaman diğer ortam-bağımlı testler
        # gibi (macOS Keychain, gerçek Tk...) açık gerekçeyle atlanır, sessizce geçmez.
        pytest.skip(f"Headless Chromium başlatılamadı (playwright install gerekebilir): {error}")
    headless_screen.install(page)
    try:
        yield page
    finally:
        page.close()
        for name, value in saved_tools.items():
            setattr(tools, name, value)
        main.Toolbox = saved_toolbox


def _model_point_for(page: headless_screen.HeadlessPage, selector: str) -> List[int]:
    """CSS seçicinin merkezini araçların gördüğü ortak 0-1000 model uzayına çevirir."""
    box = page._run(lambda p: p.locator(selector).bounding_box())
    assert box is not None, f"element not found: {selector}"
    center_x = box["x"] + box["width"] / 2
    center_y = box["y"] + box["height"] / 2
    return [
        round(center_x * tools.MODEL_SCREEN_SIZE / headless_screen.VIEW_WIDTH),
        round(center_y * tools.MODEL_SCREEN_SIZE / headless_screen.VIEW_HEIGHT),
    ]


@pytest.mark.asyncio
async def test_chrome_benzer_gate_rejects_click_on_decoy_even_with_correct_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, headless_page: headless_screen.HeadlessPage,
) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), benchmark.BenchmarkHandler)
    Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_port
    try:
        run_id = "gatecheck1"
        url = f"http://127.0.0.1:{port}/benzer/{run_id}"
        headless_page.goto(url)
        decoy_point = _model_point_for(headless_page, '[data-benchmark-target="decoy"]')

        calls = 0

        async def fake_model(
            clients: Any, messages: Any, schemas: Any, session_id: str,
            backend: str, emit: Any, should_stop: Any,
        ) -> Tuple[Dict[str, Any], str]:
            nonlocal calls
            calls += 1
            if calls == 1:
                call = {"id": "nav-1", "name": "chrome_active_tab", "arguments": json.dumps({"url": url})}
            elif calls == 2:
                call = {"id": "click-1", "name": "cua_click_point",
                        "arguments": json.dumps({"point": decoy_point})}
            else:
                return ({"content": "KOD: BT-7421", "tool_calls": [], "finish_reason": "stop",
                         "usage": main.ZERO_USAGE}, backend)
            return ({"content": "", "tool_calls": [call], "finish_reason": "tool_calls",
                     "usage": main.ZERO_USAGE}, backend)

        monkeypatch.setattr(main, "_call_model_with_retries", fake_model)

        result = await benchmark.run_one(
            "chrome_benzer", tmp_path, port, None, {"ollama-cloud": object()},
            asyncio.Semaphore(1), benchmark.headless_stage(headless_page),
        )
    finally:
        server.shutdown()

    # Yalnız metin kontrolü koşsaydı model "KOD: BT-7421" yazdığı için BAŞARILI sayılırdı:
    scenario = benchmark.build_scenario("chrome_benzer", tmp_path, "sanity-check", port)
    assert scenario["check"]("KOD: BT-7421")[0] is True
    # Ama gerçek koşuda tıklama decoy düğmesine düştü: run_one'ın wrong_target_clicks==0
    # AND kapısı bunu, yalnız metin doğruyken bile, YAKALAMALI (asıl geçme koşulu).
    assert any(action["target"] == "decoy" for action in result["gui_actions"] if action["action"] == "click")
    assert result["gui_metrics"]["wrong_target_clicks"] == 1
    assert result["ok"] is False
