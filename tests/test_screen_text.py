"""Ekran metni (OCR): gerçek Vision tanıması, metin hedefi seçimi ve kaydırılan sayfa birleştirme."""
from pathlib import Path
from typing import List

import pytest
from PIL import Image, ImageDraw, ImageFont

from omniagent.platform.macos import screen_text as st
from omniagent import tools
from omniagent.tools import Toolbox, ToolError

FONT_PATH: Path = Path("/System/Library/Fonts/Supplemental/Arial.ttf")


def _line(text: str, left: float, top: float, width: float, height: float) -> st.TextLine:
    """Kelime kutuları satır genişliğine harf sayısıyla orantılı dağıtılmış test satırı."""
    words: List[st.TextWord] = []
    cursor: float = left
    unit: float = width / max(len(text), 1)
    for word in text.split(" "):
        words.append({"text": word, "box": {"left": cursor, "top": top, "width": unit * len(word), "height": height}})
        cursor += unit * (len(word) + 1)
    return {"text": text, "confidence": 1.0, "box": {"left": left, "top": top, "width": width, "height": height},
            "words": words}


@pytest.mark.skipif(not FONT_PATH.exists(), reason="macOS Arial yazı tipi yok")
def test_real_vision_ocr_boxes_are_top_left_normalized(tmp_path: Path) -> None:
    """Gerçek Vision çağrısı: kutular sol-üst kökenli 0-1000 uzayında ve aksanlı metin eşleşir."""
    image = Image.new("RGB", (1600, 1000), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(str(FONT_PATH), 44)
    draw.text((1200, 820), "Lähetä viesti", fill="black", font=font)
    draw.text((100, 120), "Olen lukenut tietosuojaselosteen", fill="black", font=font)
    path = tmp_path / "ocr.png"
    image.save(path)

    lines = st.recognize_text(tools.Quartz.CGImageSourceCreateImageAtIndex(
        tools.Quartz.CGImageSourceCreateWithURL(tools.AppKit.NSURL.fileURLWithPath_(str(path)), None), 0, None,
    ))
    send, _ = st.select_text_match(st.find_text_matches(lines, "lähetä viesti"), None)
    assert send is not None
    x, y = st.box_center(send["box"])
    # Metin (1200..~1470, 820..~870) pikselde; 0-1000 uzayında x≈835, y≈845
    assert 760 <= x <= 920 and 815 <= y <= 875
    # Kelimenin parçası da o kelimenin kutusuna düşer (üstteki satır, solda)
    part, _ = st.select_text_match(st.find_text_matches(lines, "tietosuojaseloste"), None)
    assert part is not None
    part_x, part_y = st.box_center(part["box"])
    assert part_x > 300 and 110 <= part_y <= 190


def test_text_match_prefers_exact_and_reports_ties() -> None:
    lines = [
        _line("Duck.ai", 400, 50, 40, 12),
        _line("Senior Full Stack Developer", 180, 300, 200, 16),
        _line("Senior Fullstack Developer, Defence", 180, 420, 240, 16),
        _line("Duck.ai", 620, 515, 40, 12),
        _line("@https://www.python.org/psf/donati...", 700, 880, 230, 12),
    ]
    chosen, tied = st.select_text_match(st.find_text_matches(lines, "senior full stack developer"), None)
    assert chosen is not None and chosen["line_text"] == "Senior Full Stack Developer" and not tied
    # İki tam eşleşme: near yoksa tıklanmaz, near varsa en yakın aday seçilir
    chosen, tied = st.select_text_match(st.find_text_matches(lines, "Duck.ai"), None)
    assert chosen is None and len(tied) == 2
    chosen, _ = st.select_text_match(st.find_text_matches(lines, "Duck.ai"), (630, 500))
    assert chosen is not None and st.box_center(chosen["box"]) == (640, 521)
    # Kırpılmış bağlantı metni tam URL ile bulunur
    chosen, _ = st.select_text_match(
        st.find_text_matches(lines, "https://www.python.org/psf/donations/python-dev/"), None,
    )
    assert chosen is not None and chosen["line_text"].startswith("@https")
    assert st.find_text_matches(lines, "Tietosuojaseloste") == []


def test_scrolled_pages_merge_without_repeating_overlap() -> None:
    first = ["Software developer", "Twoday · Oulu", "Tietoa työstä", "Palkka: kilpailukykyinen"]
    second = ["Tietoa tyosta", "Palkka: kilpailukykyinen", "Vaatimukset", "Python, React"]
    merged, overlap = st.merge_page_lines(first, second)
    assert overlap == 2
    assert merged == first + ["Vaatimukset", "Python, React"]
    unrelated, none = st.merge_page_lines(first, ["Yhteystiedot"])
    assert none == 0 and unrelated[-1] == "Yhteystiedot"
    # Yalnız sayısı farklı satır örtüşme sayılmaz: yeni maaş satırı yutulmaz
    salaries, digits_overlap = st.merge_page_lines(["Palkka: 5200 €/kk"], ["Palkka: 5300 €/kk"])
    assert digits_overlap == 0 and salaries == ["Palkka: 5200 €/kk", "Palkka: 5300 €/kk"]


def test_click_text_clicks_ocr_center_and_refuses_ambiguous_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """Metne tıklama OCR kutusunun merkezini yakalanan kapsamın geometrisiyle tıklar."""
    geometry: tools.ScreenGeometry = {
        "point_width": 1200, "point_height": 900, "model_width": 1000, "model_height": 1000,
        "origin_x": 20, "origin_y": 40,
    }
    lines = [_line("Lähetä viesti", 850, 540, 100, 30), _line("Etusivu", 100, 20, 60, 12),
             _line("Etusivu", 300, 760, 60, 12)]
    clicks: List[tuple] = []
    monkeypatch.setattr(tools, "_require_accessibility", lambda: None)
    monkeypatch.setattr(tools, "screen_capture_granted", lambda request=False: False)
    monkeypatch.setattr(Toolbox, "_screen_text", lambda self: (lines, geometry))
    monkeypatch.setattr(tools, "click_model_point",
                        lambda x, y, button, used: clicks.append((x, y, used)) or f"({x}, {y}) tıklandı.")
    toolbox = Toolbox()
    assert "Lähetä viesti" in toolbox.cua_click_text("Lahetä viesti", None)
    assert clicks == [(900, 555, geometry)]
    with pytest.raises(ToolError) as ambiguous:
        toolbox.cua_click_text("Etusivu", None)
    assert ambiguous.value.code == "TEXT_AMBIGUOUS" and len(clicks) == 1
    toolbox.cua_click_text("Etusivu", [320, 740])
    assert clicks[-1][:2] == (330, 766)
    with pytest.raises(ToolError) as missing:
        toolbox.cua_click_text("Gönder", None)
    assert missing.value.code == "TEXT_NOT_FOUND"


def test_click_text_rechecks_far_ocr_match_before_click(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tam ekran OCR ikinci düğmeyi kaçırınca uzak ilk düğme yanlışlıkla tıklanmaz."""
    geometry: tools.ScreenGeometry = {
        "point_width": 1400, "point_height": 860, "model_width": 1000, "model_height": 1000,
    }
    first = _line("Aç", 760, 230, 28, 26)
    target = {"line_text": "Aç", "score": 1.0,
              "box": {"left": 760, "top": 436, "width": 28, "height": 26}}
    clicks: List[tuple] = []
    monkeypatch.setattr(tools, "_require_accessibility", lambda: None)
    monkeypatch.setattr(tools, "screen_capture_granted", lambda request=False: False)
    monkeypatch.setattr(Toolbox, "_screen_text", lambda self: ([first], geometry))
    monkeypatch.setattr(tools, "click_model_point",
                        lambda x, y, button, used: clicks.append((x, y)) or "tıklandı")
    box = Toolbox()
    monkeypatch.setattr(box, "_focused_text_match", lambda text, near, used: target)

    box.cua_click_text("Aç", [765, 444])

    assert clicks == [(774, 449)]
    monkeypatch.setattr(box, "_focused_text_match", lambda text, near, used: None)
    with pytest.raises(ToolError) as wrong:
        box.cua_click_text("Aç", [765, 444])
    assert wrong.value.code == "TEXT_TARGET_MISMATCH"
    assert clicks == [(774, 449)]


def test_screen_text_waits_for_delayed_content_after_input(monkeypatch: pytest.MonkeyPatch) -> None:
    """Eylemden hemen sonra OCR eski içeriği okumasın; en az bekleme uygulanır."""
    geometry: tools.ScreenGeometry = {
        "point_width": 1400, "point_height": 860, "model_width": 1000, "model_height": 1000,
    }
    waited: List[tuple] = []
    box = Toolbox()
    box._pending_input = {"baseline": "old-frame", "at": 3.0}
    monkeypatch.setattr(tools, "wait_for_screen_settle",
                        lambda baseline, at, frame, minimum: waited.append((baseline, at, minimum)))
    monkeypatch.setattr(box, "_scope_image", lambda flags: ("new-image", geometry))
    monkeypatch.setattr(tools.st, "recognize_text", lambda image: [_line("Yeni içerik", 10, 20, 100, 20)])

    lines, used_geometry = box._screen_text()

    assert lines[0]["text"] == "Yeni içerik"
    assert used_geometry == geometry
    assert waited == [("old-frame", 3.0, tools.OCR_AFTER_INPUT_MIN_SECONDS)]
    assert box._pending_input is None


class _VirtualPane:
    """Sağ yarısı kaydırılabilir, sol yarısı sabit sanal ekran: gri kare ve OCR satırları üretir."""
    # Panel ~880 punto yüksekliğinde 12 satır gösterir: 1 punto kaydırma 1 punto içerik kaydırır
    LINE_POINTS: int = 75
    VISIBLE: int = 12

    def __init__(self, document: List[str]) -> None:
        self.document = document
        self.offset = 0  # görünür ilk satırın indeksi

    def scroll(self, delta_points: float) -> None:
        lines = int(delta_points / self.LINE_POINTS)
        self.offset = max(0, min(len(self.document) - self.VISIBLE, self.offset + lines))

    def gray(self) -> "tools.np.ndarray":
        frame = tools.np.zeros((300, 480), dtype=tools.np.uint8)
        frame[:, :240] = 90  # sabit sol panel
        for row in range(self.VISIBLE):
            index = self.offset + row
            frame[row * 25:(row * 25) + 18, 260:470] = (index * 37) % 200 + 40
        return frame

    def text(self) -> List[st.TextLine]:
        visible = [_line("Sabit menü", 50, 100, 200, 20)]
        for row in range(self.VISIBLE):
            visible.append(_line(self.document[self.offset + row], 560, row * 80 + 10, 400, 30))
        return visible

    def snapshot(self) -> "_Snapshot":
        """Yakalandığı andaki satırların değişmez kopyası: OCR arka planda, panel kaydırılırken koşar."""
        return _Snapshot(self.text())


class _Snapshot:
    """Sanal ekranın anlık görüntüsü (CGImage yerine): tanıma o andaki satırları döner."""

    def __init__(self, lines: List[st.TextLine]) -> None:
        self.lines = lines


_READ_GEOMETRY: tools.ScreenGeometry = {
    "point_width": 1200, "point_height": 900, "model_width": 1000, "model_height": 1000,
}


def _install_virtual_pane(monkeypatch: pytest.MonkeyPatch, pane: _VirtualPane) -> None:
    """Okuma aracının ekran/kaydırma/OCR dikişlerini sanal panele bağlar (eski ve yeni okuma akışı aynı dikişlerden geçer)."""
    monkeypatch.setattr(tools, "_require_accessibility", lambda: None)
    monkeypatch.setattr(tools, "screen_capture_granted", lambda request=False: True)
    monkeypatch.setattr(tools, "move_model_point", lambda x, y, used: "taşındı")
    monkeypatch.setattr(tools, "post_scroll", lambda dx, dy: pane.scroll(dy))
    monkeypatch.setattr(tools.st, "recognize_text", lambda image: image.lines)
    monkeypatch.setattr(Toolbox, "_input_geometry", lambda self: _READ_GEOMETRY)
    monkeypatch.setattr(Toolbox, "_settle_frame", lambda self: pane.gray())
    monkeypatch.setattr(Toolbox, "_scope_gray", lambda self: pane.gray())
    monkeypatch.setattr(Toolbox, "_settled_gray", lambda self, reference: pane.gray())
    monkeypatch.setattr(Toolbox, "_scope_image", lambda self, option: (pane.snapshot(), _READ_GEOMETRY))


@pytest.mark.parametrize("lag_pages", [0, 1])
def test_read_scrollable_returns_whole_pane_once_and_stops_at_end(
    monkeypatch: pytest.MonkeyPatch, lag_pages: int,
) -> None:
    """
    Okuma başa döner, panel sonuna kadar okur, örtüşmeyi atar ve sabit paneli karıştırmaz; karar gecikmesi (0: her sayfa
    beklenir, 1: bir sayfa gecikir) boşluksuz panelde sonucu değiştirmez.
    """
    document = [f"Satır {index}: açıklama" for index in range(40)] + ["Palkka: 5200 €/kk"]
    pane = _VirtualPane(document)
    pane.offset = 17  # kullanıcı paneli yarıya kadar kaydırmış
    monkeypatch.setattr(tools, "READ_OCR_LAG_PAGES", lag_pages)
    _install_virtual_pane(monkeypatch, pane)

    result = Toolbox().cua_read_scrollable([750, 500], 15)
    assert "sona ulaşıldı" in result
    body = result.split("\n", 1)[1].splitlines()
    assert body == document
    assert "Sabit menü" not in result
    # Sayfa sınırı dolarsa sonuç devamı olduğunu açıkça söyler
    pane.offset = 0
    limited = Toolbox().cua_read_scrollable([750, 500], 1)
    assert "SONA ULAŞILMADI" in limited


def test_read_scrollable_leaves_screen_settled_without_pending_input_record(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Araç kendi sonunda paneli başa döndürüp ekranın durulmasını bekler: bekleyen girdi kaydı bırakmaz, böylece sonraki
    OCR/gözlem 0,9-1,0 sn boşuna beklemez. Başka ekran araçlarının kaydı değişmez (tıklama girdisi hâlâ kayıt bırakır).
    """
    pane = _VirtualPane([f"Satır {index}" for index in range(30)])
    _install_virtual_pane(monkeypatch, pane)
    monkeypatch.setattr(tools, "click_model_point", lambda x, y, button, used: "tıklandı")
    box = Toolbox()

    box.cua_read_scrollable([750, 500], 15)
    assert box._pending_input is None
    box.cua_click_point([750, 500])
    assert box._pending_input is not None


def test_changed_region_reports_transient_screen_resize() -> None:
    with pytest.raises(tools.ToolError) as error:
        tools.changed_region(
            tools.np.zeros((24, 480), dtype=tools.np.uint8),
            tools.np.zeros((277, 480), dtype=tools.np.uint8),
            (240, 12), 0.01,
        )
    assert error.value.code == "SCREEN_GEOMETRY_CHANGED"


def test_read_scrollable_waits_for_click_before_scrolling(monkeypatch: pytest.MonkeyPatch) -> None:
    """Detay tıklaması yüklenmeden panel başa kaydırılmamalı."""
    box = Toolbox()
    events: list[str] = []
    geometry: tools.ScreenGeometry = {
        "point_width": 1000, "point_height": 800, "model_width": 1000, "model_height": 1000,
    }
    box._pending_input = {"baseline": tools.np.zeros((10, 10), dtype=tools.np.uint8), "at": 0.0}
    monkeypatch.setattr(tools, "_require_accessibility", lambda: None)
    monkeypatch.setattr(tools, "screen_capture_granted", lambda request=False: False)
    monkeypatch.setattr(tools, "move_model_point", lambda x, y, used: "taşındı")
    monkeypatch.setattr(tools, "wait_for_screen_settle", lambda *args: events.append("bekle") or 0.0)
    monkeypatch.setattr(box, "_input_geometry", lambda: geometry)
    monkeypatch.setattr(
        box, "_scroll_to_start",
        lambda used: events.append("başa kaydır") or tools.np.zeros((20, 20), dtype=tools.np.uint8),
    )
    monkeypatch.setattr(box, "_scope_image", lambda option: (object(), geometry))
    monkeypatch.setattr(tools.st, "recognize_text", lambda image: [])
    monkeypatch.setattr(box, "_scope_gray", lambda: tools.np.zeros((20, 20), dtype=tools.np.uint8))
    monkeypatch.setattr(box, "_settled_gray", lambda before: before)
    monkeypatch.setattr(tools, "post_scroll", lambda dx, dy: None)

    box.cua_read_scrollable([500, 500], 15)

    assert events[:2] == ["bekle", "başa kaydır"]


def test_changed_region_ignores_animation_outside_scroll_anchor() -> None:
    """Başka yerdeki animasyon, imlecin altındaki panel kaymış sayılmamalı."""
    before = tools.np.zeros((120, 160), dtype=tools.np.uint8)
    after = before.copy()
    after[5:45, 5:45] = 255

    assert tools.changed_region(before, after, (140, 95), 0.01) is None


def test_focused_code_candidate_corrects_only_ambiguous_o_zero() -> None:
    """Odaklı OCR yalnız O/0 ayrımını düzeltir; ilgisiz kodu uydurmaz."""
    assert st.choose_focused_code("IL-5EDBOB", ": IL-5EDB0B") == "IL-5EDB0B"
    assert st.choose_focused_code("IL-5EDBOB", "IL-5EDB9B") == "IL-5EDBOB"
    assert st.choose_focused_code("IL-5EDBOB", "Başka metin") == "IL-5EDBOB"
    assert st.choose_focused_code("AB-CODE5", "AB-C0DE5") == "AB-C0DE5"


def test_region_and_point_helpers_are_pure() -> None:
    assert st.region_around((30, 990), 200, 80) == {"left": 0.0, "top": 910.0, "width": 230.0, "height": 90.0}
    lines = [_line("Ödemeyi onayla", 400, 500, 200, 30), _line("İptal", 100, 500, 60, 30)]
    line = st.line_at_point(lines, (500, 515), 80, 30)
    assert line is not None and line["text"] == "Ödemeyi onayla"
    near = st.line_at_point(lines, (130, 515), 80, 30)
    assert near is not None and near["text"] == "İptal"  # komşu düğmenin etiketi yazılmaz
    assert st.line_at_point(lines, (500, 700), 80, 30) is None
    # İki satır da toleransın içindeyse listedeki ilk değil NOKTAYA EN YAKIN olan seçilir
    close = [_line("Ödemeyi onayla", 400, 500, 200, 30), _line("İptal", 290, 500, 30, 30)]
    nearest = st.line_at_point(close, (330, 515), 80, 30)
    assert nearest is not None and nearest["text"] == "İptal"


@pytest.mark.skipif(not FONT_PATH.exists(), reason="macOS Arial yazı tipi yok")
def test_recognize_region_maps_boxes_to_full_image_space(tmp_path: Path) -> None:
    """Gerçek Vision: bölge OCR'ı kutuları kırpımın değil TAM görüntünün 0-1000 uzayında verir (üst-sol köken)."""
    image = Image.new("RGB", (1600, 1000), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(str(FONT_PATH), 44)
    draw.text((1200, 820), "Ödemeyi onayla", fill="black", font=font)
    draw.text((100, 120), "Ayrıntıları göster", fill="black", font=font)
    path = tmp_path / "region.png"
    image.save(path)
    cg_image = tools.Quartz.CGImageSourceCreateImageAtIndex(
        tools.Quartz.CGImageSourceCreateWithURL(tools.AppKit.NSURL.fileURLWithPath_(str(path)), None), 0, None,
    )
    lines = st.recognize_region(cg_image, st.region_around((840, 845), 200, 80))
    line = st.line_at_point(lines, (840, 845), 80, 30)
    assert line is not None and st.same_text(line["text"], "Ödemeyi onayla")
    x, y = st.box_center(line["box"])
    assert 760 <= x <= 940 and 815 <= y <= 875
    assert all(760 <= st.box_center(word["box"])[0] <= 940 for word in line["words"])  # kelime kutuları da tam uzayda
    assert not any("Ayrıntı" in item["text"] for item in lines)  # bölge dışındaki satır okunmaz
    with pytest.raises(st.TextRecognitionError):
        st.recognize_region(cg_image, {"left": 500.0, "top": 500.0, "width": 0.0, "height": 40.0})


_PANE_REGION: st.TextBox = {"left": 500.0, "top": 50.0, "width": 480.0, "height": 900.0}


def _page(*texts: str) -> List[st.TextLine]:
    """Kaydırma bölgesinin içinde, kenarlardan uzak, üstten alta dizili satırlar."""
    return [_line(text, 600, 150 + index * 60, 300, 30) for index, text in enumerate(texts)]


def test_page_fold_merges_overlap_and_flags_gap_without_touching_input() -> None:
    """Sayfa katlama: örtüşme atılır; örtüşme yoksa işaret konur, sayaç artar, adım yarılanır; girdi değişmez."""
    first = st.first_pages_progress(_page("a", "b", "c"), _page("b", "c", "d"), _PANE_REGION, tools.READ_EDGE_UNITS, 400.0)
    assert first == {"text_lines": ["a", "b", "c", "d"], "cut_tail": [], "gaps": 0, "step_points": 400.0}
    joined = st.absorb_page(first, _page("c", "d", "e"), _PANE_REGION, tools.READ_EDGE_UNITS, "[boşluk]")
    assert joined["text_lines"] == ["a", "b", "c", "d", "e"] and joined["gaps"] == 0 and joined["step_points"] == 400.0
    gapped = st.absorb_page(joined, _page("x", "y"), _PANE_REGION, tools.READ_EDGE_UNITS, "[boşluk]")
    assert gapped["text_lines"] == ["a", "b", "c", "d", "e", "[boşluk]", "x", "y"]
    assert gapped["gaps"] == 1 and gapped["step_points"] == 200.0
    assert joined["text_lines"] == ["a", "b", "c", "d", "e"] and joined["gaps"] == 0  # girdi değişmedi
    # Boş sayfa boşluk sayılmaz (örtüşme aranacak satır yok); önceki metin korunur
    empty = st.absorb_page(gapped, [], _PANE_REGION, tools.READ_EDGE_UNITS, "[boşluk]")
    assert empty["text_lines"] == gapped["text_lines"] and empty["gaps"] == 1
