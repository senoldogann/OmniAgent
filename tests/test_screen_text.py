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


def test_read_scrollable_returns_whole_pane_once_and_stops_at_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """Okuma başa döner, panel sonuna kadar okur, örtüşmeyi atar ve sabit paneli karıştırmaz."""
    document = [f"Satır {index}: açıklama" for index in range(40)] + ["Palkka: 5200 €/kk"]
    pane = _VirtualPane(document)
    pane.offset = 17  # kullanıcı paneli yarıya kadar kaydırmış
    geometry: tools.ScreenGeometry = {
        "point_width": 1200, "point_height": 900, "model_width": 1000, "model_height": 1000,
    }
    monkeypatch.setattr(tools, "_require_accessibility", lambda: None)
    monkeypatch.setattr(tools, "screen_capture_granted", lambda request=False: False)
    monkeypatch.setattr(tools, "move_model_point", lambda x, y, used: "taşındı")
    monkeypatch.setattr(tools, "post_scroll", lambda dx, dy: pane.scroll(dy))
    monkeypatch.setattr(Toolbox, "_input_geometry", lambda self: geometry)
    monkeypatch.setattr(Toolbox, "_scope_gray", lambda self: pane.gray())
    monkeypatch.setattr(Toolbox, "_settled_gray", lambda self, reference: pane.gray())
    monkeypatch.setattr(Toolbox, "_screen_text", lambda self: (pane.text(), geometry))

    result = Toolbox().cua_read_scrollable([750, 500], 15)
    assert "sona ulaşıldı" in result
    body = result.split("\n", 1)[1].splitlines()
    assert body == document
    assert "Sabit menü" not in result
    # Sayfa sınırı dolarsa sonuç devamı olduğunu açıkça söyler
    pane.offset = 0
    limited = Toolbox().cua_read_scrollable([750, 500], 1)
    assert "SONA ULAŞILMADI" in limited
