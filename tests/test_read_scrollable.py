"""
cua_read_scrollable boru hattı: gerçek görünmez Chromium ve gerçek Vision ile sıralı okumayla (paket S öncesi akış)
birebir eşdeğerlik, OCR'ın arka planda koşması, sayfa sırasıyla birleştirme, hata ve durdurma davranışı.
Yalnız yerel sayfa kullanılır (ağ yok, kullanıcı ekranına dokunulmaz).
"""
import random
import threading
import time
from typing import Dict, Iterator, List, Tuple

import pyautogui
import pytest
import Quartz

from omniagent import tools
from omniagent.app import agent as main
from omniagent.dev import headless_screen
from omniagent.platform.macos import screen_text as st
from omniagent.tools import ToolError, Toolbox

READ_POINT: List[int] = [643, 520]
MAX_PAGES: int = 15
# Üç farklı sayfa uzunluğu: 2, 3 ve 4-5 kaydırmalık okuma
PARAGRAPH_COUNTS: Tuple[int, ...] = (14, 25, 36)
WORDS: Tuple[str, ...] = (
    "ekip", "ürün", "kalite", "kod", "inceleme", "otomatik", "test", "gözlemlenebilirlik", "müşteri", "geri",
    "bildirim", "günlük", "uzaktan", "ofis", "esnek", "çalışma", "mimari", "veritabanı", "servis", "bulut",
    "güvenlik", "performans", "ölçek", "izleme", "dağıtım", "sürüm", "planlama", "hedef", "strateji", "analiz",
)
PAGE_TEMPLATE: str = """<!doctype html><html lang="tr"><head><meta charset="utf-8"><style>
body{font:15px -apple-system,sans-serif;margin:0;background:#f3f2ef;color:#1d2226}
header{background:#fff;padding:12px 24px;border-bottom:1px solid #ddd;font-weight:600}
main{display:flex;gap:16px;padding:16px 24px;height:calc(100vh - 50px);box-sizing:border-box}
#liste{width:380px;display:flex;flex-direction:column;gap:12px}
.kart{background:#fff;border:1px solid #ddd;border-radius:8px;padding:16px;min-height:118px;box-sizing:border-box}
#detay{flex:1;overflow-y:auto;background:#fff;border:1px solid #ddd;border-radius:8px;padding:24px}
#detay p{line-height:1.7}
</style></head><body><header>İş ilanları</header><main><section id="liste">__KARTLAR__</section>
<section id="detay"><h2>Uzun ilan</h2>__PARAGRAFLAR__<h3>Ücret</h3><p>Aylık maaş: <b>4800 €</b></p></section></main></body></html>"""


def _long_page(paragraph_count: int) -> str:
    """Solda sabit liste, sağda paragraph_count benzersiz paragraflı kaydırılabilir ayrıntı bölmesi (tekrarlanabilir metin)."""
    rng = random.Random(7)
    cards = "".join(f'<div class="kart"><b>İlan {index}</b><br>Şirket {index}</div>' for index in range(1, 9))
    paragraphs = "".join(
        f"<p>Bölüm {index + 1}-{rng.randint(100, 999)}: "
        + " ".join(rng.choice(WORDS) for _ in range(rng.randint(20, 34))) + ".</p>"
        for index in range(paragraph_count)
    )
    return PAGE_TEMPLATE.replace("__KARTLAR__", cards).replace("__PARAGRAFLAR__", paragraphs)


_PATCHED_TOOLS_ATTRS: Tuple[str, ...] = (
    "screen_capture_granted", "_require_screen_capture", "_require_accessibility",
    "click_model_point", "multi_click_model_point", "drag_model_points",
    "move_model_point", "type_unicode_text", "press_key_spec", "post_scroll",
)


@pytest.fixture(scope="module")
def headless() -> Iterator[Tuple[headless_screen.HeadlessPage, Toolbox]]:
    """
    Görünmez Chromium + HeadlessToolbox (gerçek araç mantığı). install() modül düzeyindeki adları kalıcı değiştirir:
    modül bitince geri alınır ki sonraki testler gerçek macOS araçlarını görsün (bkz. test_benchmark_gui_gate).
    """
    saved_tools: Dict[str, object] = {name: getattr(tools, name) for name in _PATCHED_TOOLS_ATTRS}
    saved_pyautogui: Dict[str, object] = {name: getattr(pyautogui, name) for name in headless_screen.REAL_INPUT_FUNCTIONS}
    saved_event_post = Quartz.CGEventPost
    saved_toolbox = main.Toolbox
    saved_route_schemas = main.route_tool_schemas
    try:
        page = headless_screen.HeadlessPage()
    except Exception as error:  # noqa: BLE001 - Playwright'ın kendi hata tipi burada önemli değil
        pytest.skip(f"Headless Chromium başlatılamadı (playwright install gerekebilir): {error}")
    headless_screen.install(page)
    try:
        yield page, main.Toolbox()
    finally:
        page.close()
        for name, value in saved_tools.items():
            setattr(tools, name, value)
        for name, value in saved_pyautogui.items():
            setattr(pyautogui, name, value)
        Quartz.CGEventPost = saved_event_post
        main.Toolbox = saved_toolbox
        main.route_tool_schemas = saved_route_schemas


def _open(page: headless_screen.HeadlessPage, paragraph_count: int) -> None:
    """Sayfayı temiz açar: panel başta, yükleme bitmiş."""
    page._run(lambda chromium_page: chromium_page.set_content(_long_page(paragraph_count)))
    time.sleep(0.3)


def _sequential_reference_read(box: Toolbox, point: List[int], max_pages: int) -> str:
    """
    Paket S öncesi cua_read_scrollable akışının birebir kopyası (eşdeğerlik oracle'ı): her sayfa tam kare OCR'ı ana iş
    parçacığında, sonucu beklenerek ve boşluk kararı hemen verilerek okunur.
    """
    x, y = tools.parse_point(point)
    geometry = box._input_geometry()
    box._wait_pending_input()
    tools.move_model_point(x, y, geometry)
    box._scroll_to_start(geometry)
    first_lines, _first_geometry = box._screen_text()
    before = box._scope_gray()
    first_step_points = tools.READ_FIRST_STEP_SHARE * geometry["point_height"]
    tools.post_scroll(0.0, first_step_points)
    after = box._settled_gray(before)
    anchor = (
        min(after.shape[1] - 1, x * after.shape[1] // tools.MODEL_SCREEN_SIZE),
        min(after.shape[0] - 1, y * after.shape[0] // tools.MODEL_SCREEN_SIZE),
    )
    bounds = tools.changed_region(before, after, anchor, tools.SCROLL_MOVED_RATIO)
    assert bounds is not None, "referans okuma bölgeyi bulamadı: sayfa kaydırılamıyor"
    height, width = after.shape[:2]
    region: st.TextBox = {
        "left": bounds[0] * tools.MODEL_SCREEN_SIZE / width, "top": bounds[1] * tools.MODEL_SCREEN_SIZE / height,
        "width": (bounds[2] - bounds[0]) * tools.MODEL_SCREEN_SIZE / width,
        "height": (bounds[3] - bounds[1]) * tools.MODEL_SCREEN_SIZE / height,
    }
    text_lines, cut_tail = st.split_bottom_cut(st.lines_within(first_lines, region), region, tools.READ_EDGE_UNITS)
    probe_lines, _probe_geometry = box._screen_text()
    probe_page, cut_tail = st.split_bottom_cut(
        st.without_top_cut(st.lines_within(probe_lines, region), region, tools.READ_EDGE_UNITS),
        region, tools.READ_EDGE_UNITS,
    )
    text_lines, _probe_overlap = st.merge_page_lines(text_lines, probe_page)
    step_points = tools.READ_STEP_SHARE * region["height"] * geometry["point_height"] / tools.MODEL_SCREEN_SIZE
    scrolled_points = first_step_points
    scrolls = 1
    gaps = 0
    reached_end = False
    while scrolls < max_pages:
        before = box._scope_gray()
        tools.post_scroll(0.0, step_points)
        after = box._settled_gray(before)
        scrolled_points += step_points
        if tools.changed_region(before, after, anchor, tools.SCROLL_MOVED_RATIO) is None:
            reached_end = True
            break
        top_px = max(0, round(region["top"] * height / tools.MODEL_SCREEN_SIZE))
        bottom_px = min(height, round((region["top"] + region["height"]) * height / tools.MODEL_SCREEN_SIZE))
        left_px = max(0, round(region["left"] * width / tools.MODEL_SCREEN_SIZE))
        right_px = min(width, round((region["left"] + region["width"]) * width / tools.MODEL_SCREEN_SIZE))
        if (
            bottom_px > top_px and right_px > left_px
            and tools.frame_change_ratio(
                before[top_px:bottom_px, left_px:right_px], after[top_px:bottom_px, left_px:right_px],
                tools.SCROLL_PIXEL_DELTA,
            ) < tools.SCROLL_MOVED_RATIO
        ):
            reached_end = True
            break
        scrolls += 1
        page_lines, _page_geometry = box._screen_text()
        page, cut_tail = st.split_bottom_cut(
            st.without_top_cut(st.lines_within(page_lines, region), region, tools.READ_EDGE_UNITS),
            region, tools.READ_EDGE_UNITS,
        )
        merged, overlap = st.merge_page_lines(text_lines, page)
        if overlap == 0 and text_lines and page:
            gaps += 1
            merged = text_lines + [tools.READ_GAP_MARKER] + page
            step_points /= 2
        text_lines = merged
    if cut_tail:
        text_lines, _tail_overlap = st.merge_page_lines(text_lines, cut_tail)
    restore_before = box._scope_gray()
    tools.post_scroll(0.0, -(scrolled_points + geometry["point_height"]))
    box._settled_gray(restore_before)
    end_note = (
        "sona ulaşıldı (son kaydırmada içerik kaymadı)" if reached_end
        else f"SONA ULAŞILMADI: {max_pages} sayfa sınırı doldu, devamı var"
    )
    center_x, center_y = st.box_center(region)
    gap_note = (
        f" {gaps} yerde örtüşme bulunamadı ({tools.READ_GAP_MARKER}): arada satır atlanmış olabilir." if gaps else ""
    )
    header = (
        f"Okunan bölge merkezi ({center_x},{center_y}), {scrolls} kaydırma, "
        f"{len(text_lines)} satır; {end_note}.{gap_note} Panel yeniden başına döndürüldü.\n"
    )
    return tools._clip(header + "\n".join(text_lines), tools.READ_TEXT_LIMIT)


_REFERENCES: Dict[int, str] = {}


def _reference(page: headless_screen.HeadlessPage, box: Toolbox, paragraph_count: int) -> str:
    """Sıralı referans okuması (aynı sayfa için bir kez hesaplanır)."""
    if paragraph_count not in _REFERENCES:
        _open(page, paragraph_count)
        _REFERENCES[paragraph_count] = _sequential_reference_read(box, READ_POINT, MAX_PAGES)
    return _REFERENCES[paragraph_count]


def _read(page: headless_screen.HeadlessPage, box: Toolbox, paragraph_count: int) -> str:
    _open(page, paragraph_count)
    return box.cua_read_scrollable(READ_POINT, MAX_PAGES)


def _ocr_thread_names() -> List[str]:
    return [thread.name for thread in threading.enumerate() if thread.name.startswith("omni-read-ocr")]


@pytest.mark.parametrize("paragraph_count", PARAGRAPH_COUNTS)
def test_pipelined_read_is_byte_identical_to_sequential_reference(
    headless: Tuple[headless_screen.HeadlessPage, Toolbox], paragraph_count: int,
) -> None:
    """Boşluksuz sayfada boru hattı (arka plan OCR, karar gecikmesi, kare yeniden kullanımı) eski sıralı okumayla BİREBİR aynı çıktıyı verir."""
    page, box = headless
    reference = _reference(page, box, paragraph_count)
    result = _read(page, box, paragraph_count)
    assert result == reference
    # Boş bir eşdeğerlik olmasın: tüm paragraflar okunmuş, sona ulaşılmış, yalancı boşluk yok
    assert "sona ulaşıldı" in result and tools.READ_GAP_MARKER not in result
    for index in range(1, paragraph_count + 1):
        assert f"Bölüm {index}-" in result
    assert box._pending_input is None  # araç ekranı durulmuş bırakır: sonraki OCR/gözlem boşuna beklemez
    assert _ocr_thread_names() == []


def test_systematic_overscroll_reads_like_the_sequential_reference(
    headless: Tuple[headless_screen.HeadlessPage, Toolbox], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Uygulama kaydırmayı sayfa yüksekliğini aşacak kadar büyütürse ilk tam adımda boşluk çıkar: karar o adımda BEKLENİR, bu yüzden
    eski sıralı okumadaki gibi adım hemen yarılanır (tek boşluk işareti) ve çıktı birebir aynıdır.
    """
    page, box = headless
    original = tools.post_scroll

    def overshoot(delta_x: float, delta_y: float) -> None:
        original(delta_x, delta_y * 1.7 if delta_y > 0 else delta_y)

    monkeypatch.setattr(tools, "post_scroll", overshoot)
    _open(page, 25)
    reference = _sequential_reference_read(box, READ_POINT, MAX_PAGES)
    result = _read(page, box, 25)
    assert tools.READ_GAP_MARKER in reference  # senaryo gerçekten boşluk üretiyor
    assert result == reference


def test_ocr_runs_in_pool_threads_while_the_main_thread_keeps_scrolling(
    headless: Tuple[headless_screen.HeadlessPage, Toolbox], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OCR iş parçacığı havuzunda koşar ve ana iş parçacığı sonucu beklemeden kaydırmaya devam eder (sonuç yine referansla aynı)."""
    page, box = headless
    reference = _reference(page, box, 14)
    real_recognize = st.recognize_text
    events: List[Tuple[str, str, float]] = []
    lock = threading.Lock()

    def slow_recognize(image: object) -> List[st.TextLine]:
        with lock:
            events.append(("ocr-start", threading.current_thread().name, time.monotonic()))
        time.sleep(0.5)  # kaydırma/durulma süresinden uzun: örtüşme pencere olarak görünür olsun
        result = real_recognize(image)
        with lock:
            events.append(("ocr-end", threading.current_thread().name, time.monotonic()))
        return result

    def recording_scroll(delta_x: float, delta_y: float) -> None:
        with lock:
            events.append(("scroll", threading.current_thread().name, time.monotonic()))
        original_scroll(delta_x, delta_y)

    original_scroll = tools.post_scroll
    monkeypatch.setattr(st, "recognize_text", slow_recognize)
    monkeypatch.setattr(tools, "post_scroll", recording_scroll)
    result = _read(page, box, 14)

    assert result == reference
    ocr_threads = {name for kind, name, _at in events if kind.startswith("ocr")}
    assert ocr_threads and all(name.startswith("omni-read-ocr") for name in ocr_threads)
    first_start = next(at for kind, _name, at in events if kind == "ocr-start")
    first_end = next(at for kind, _name, at in events if kind == "ocr-end")
    assert any(kind == "scroll" and first_start < at < first_end for kind, _name, at in events)
    assert _ocr_thread_names() == []


def test_pages_merge_in_page_order_even_when_ocr_jobs_finish_out_of_order(
    headless: Tuple[headless_screen.HeadlessPage, Toolbox], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """İki işçi ve ilk sayfaları YAVAŞ tanıyan OCR ile sayfalar sırasız biter; birleştirme sayfa sırasıyladır: sonuç sıralı referansla aynıdır."""
    page, box = headless
    reference = _reference(page, box, 14)
    real_recognize = st.recognize_text
    calls: List[int] = []
    lock = threading.Lock()
    finish_order: List[int] = []

    def slow_first_pages(image: object) -> List[st.TextLine]:
        with lock:
            index = len(calls)
            calls.append(index)
        time.sleep(2.0 if index == 0 else 0.0)  # tepe sayfa, sonradan başlayan sayfalardan sonra biter
        result = real_recognize(image)
        with lock:
            finish_order.append(index)
        return result

    monkeypatch.setattr(tools, "READ_OCR_WORKERS", 2)
    monkeypatch.setattr(st, "recognize_text", slow_first_pages)
    result = _read(page, box, 14)

    assert finish_order != sorted(finish_order)  # senaryo gerçekten sırasız bitişi üretti
    assert result == reference


def test_stop_request_cancels_pending_ocr_jobs_and_leaves_no_worker_threads(
    headless: Tuple[headless_screen.HeadlessPage, Toolbox], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Durdurma isteğinde çalışan OCR bitirilir, bekleyenler iptal edilir (hiç başlamaz) ve iş parçacığı sızmaz."""
    page, box = headless
    real_recognize = st.recognize_text
    started: List[int] = []
    finished: List[int] = []

    def slow_ocr(image: object) -> List[st.TextLine]:
        started.append(len(started))
        time.sleep(3.0)  # ilk sayfa, durdurma istendiğinde hâlâ tanınıyor olsun (yüklü makinede de)
        result = real_recognize(image)
        finished.append(len(finished))
        return result

    stop = {"requested": False}
    captures: List[int] = []
    original_scope_image = type(box)._scope_image  # başsız kutu bu yöntemi kendi sınıfında geçersiz kılar

    def scope_image_then_stop(self: Toolbox, image_option: int) -> Tuple[object, tools.ScreenGeometry]:
        if image_option == Quartz.kCGWindowImageDefault:  # OCR karesi: ikincisi (probe) sıraya girince durdurulur
            captures.append(len(captures))
            stop["requested"] = len(captures) >= 2
        return original_scope_image(self, image_option)

    monkeypatch.setattr(st, "recognize_text", slow_ocr)
    monkeypatch.setattr(type(box), "_scope_image", scope_image_then_stop)
    token = tools.TOOL_RUNTIME.set({"emit_output": lambda text: None, "should_stop": lambda: stop["requested"]})
    try:
        _open(page, 14)
        with pytest.raises(ToolError) as stopped:
            box.cua_read_scrollable(READ_POINT, MAX_PAGES)
    finally:
        tools.TOOL_RUNTIME.reset(token)

    assert stopped.value.code == "STOPPED"
    assert len(captures) == 2
    assert len(started) == 1 and len(finished) == 1  # tepe sayfa bitti; sırada bekleyen probe sayfası hiç başlamadı
    assert _ocr_thread_names() == []


def test_first_ocr_failure_is_raised_and_remaining_jobs_are_cancelled(
    headless: Tuple[headless_screen.HeadlessPage, Toolbox], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OCR hatası sessizce yutulmaz: ilk hata (kod ve bağlamıyla) çağırana yükselir, kalan işler iptal edilir, iş parçacığı sızmaz."""
    page, box = headless
    real_recognize = st.recognize_text
    calls: List[int] = []

    def failing_third_page(image: object) -> List[st.TextLine]:
        calls.append(len(calls))
        if len(calls) == 3:
            raise st.TextRecognitionError("Vision metin tanıma başarısız: sınama")
        return real_recognize(image)

    monkeypatch.setattr(st, "recognize_text", failing_third_page)
    _open(page, 14)
    with pytest.raises(ToolError) as failed:
        box.cua_read_scrollable(READ_POINT, MAX_PAGES)

    assert failed.value.code == "OCR_FAILED" and failed.value.recoverable
    assert "sınama" in str(failed.value)
    calls_at_return = len(calls)
    time.sleep(0.5)
    assert len(calls) == calls_at_return  # tool döndükten sonra hiçbir OCR işi çalışmıyor
    assert _ocr_thread_names() == []
