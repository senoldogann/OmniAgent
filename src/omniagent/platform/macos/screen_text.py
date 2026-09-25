"""
Ekran metni (OCR): görünür metni macOS Vision ile satır ve kelime kutularıyla okur, metin
hedefini bulur ve kaydırılan sayfaların metnini tekrarsız birleştirir.

Model ekranı sabit ~280 tokenlık bir ızgarada görür (gemma4 ölçümü: 64 px'lik de 3024 px'lik de
görüntü aynı token sayısını tutar); küçük yazıyı okumakta ve piksel hassas nokta vermekte
zorlanır. OCR aynı ekranın tam Retina çözünürlüğünde çalışır: gerçek sayfalardan alınan 42 metin
hedefinin 39'unda tıklama noktası DOM kutusunun içine düştü; modelin kendi nokta tahmini üretimdeki
görüntüyle 48 hedefin 20'sinde isabet etti. Tüm kutular yakalanan kapsamın (ekran veya pencere)
0-1000 normalize uzayındadır; tıklama araçlarının kullandığı uzayla aynıdır.
"""
import math
import unicodedata
from difflib import SequenceMatcher
from typing import List, Optional, Tuple, TypedDict

import Vision
from Foundation import NSMakeRange

# Model uzayı: her eksen 0-1000 (tools.MODEL_SCREEN_SIZE ile aynı değer)
NORMALIZED_SPACE: int = 1000
# Bulanık eşleşmenin kabul edildiği en düşük benzerlik (OCR 'ä'yı 'ā' okuyabiliyor)
FUZZY_MIN_RATIO: float = 0.8
# Aynı skor kademesinde sayılan fark: iki tam eşleşme birbirinden ayırt edilemez
SCORE_TIE: float = 0.02
# near verildiğinde yalnız bu skorun üstündeki adaylar arasından en yakını seçilir
NEAR_MIN_SCORE: float = 0.7
# Birleştirmede örtüşme penceresinde birebir aynı olması gereken satır oranı
OVERLAP_MIN_SHARE: float = 0.8
# Tıklanan satır aranan metinle bu benzerliğin üstündeyse "birebir" sayılır (tek harflik OCR farkı);
# 'Lead Full Stack Developer' ile 'Full Stack Developer' 0,89'da kalır ve uyarı üretir
SAME_TEXT_MIN_RATIO: float = 0.93

# Eşleştirmede yazım farkı sayılmayan karakterler: kıvrık tırnak, uzun tire, bölünmez boşluk
# ve Türkçe noktasız ı (OCR onu çoğu zaman i okur).
_CHARACTER_MAP: dict[int, str] = str.maketrans({
    "’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-", " ": " ", "ı": "i",
})
_WORD_EDGE_PUNCTUATION: str = ".,:;!?\"'()[]{}<>«»…*•·|"


class TextRecognitionError(Exception):
    """Vision metin tanıma isteği başarısız oldu (ayrıntı mesajdadır)."""


class TextBox(TypedDict):
    """0-1000 normalize uzayda kutu: sol üst köşe ve boyut."""
    left: float
    top: float
    width: float
    height: float


class TextWord(TypedDict):
    text: str
    box: TextBox


class TextLine(TypedDict):
    """Tanınan tek satır; kelime kutuları alt dizge eşleşmesinde tam konumu verir."""
    text: str
    confidence: float
    box: TextBox
    words: List[TextWord]


class TextMatch(TypedDict):
    """Aranan metnin bir satırdaki eşleşmesi: eşleşen bölümün kutusu ve güven skoru."""
    line_text: str
    box: TextBox
    score: float


def _vision_box(rect: object) -> TextBox:
    """Vision'ın sol-alt kökenli 0-1 kutusunu sol-üst kökenli 0-1000 kutuya çevirir. Saf."""
    left: float = float(rect.origin.x) * NORMALIZED_SPACE
    width: float = float(rect.size.width) * NORMALIZED_SPACE
    height: float = float(rect.size.height) * NORMALIZED_SPACE
    top: float = (1.0 - float(rect.origin.y) - float(rect.size.height)) * NORMALIZED_SPACE
    return {"left": left, "top": top, "width": width, "height": height}


def _utf16_length(text: str) -> int:
    """NSString aralıkları UTF-16 birimiyle ölçülür (emoji 2 birimdir). Saf."""
    return len(text.encode("utf-16-le")) // 2


def _word_boxes(candidate: object, text: str) -> List[TextWord]:
    """Satırdaki her kelimenin kutusunu Vision'dan alır; kutusu okunamayan kelime atlanır."""
    words: List[TextWord] = []
    offset: int = 0
    for index, part in enumerate(text.split(" ")):
        if index > 0:
            offset += 1
        if part:
            observation, _error = candidate.boundingBoxForRange_error_(
                NSMakeRange(offset, _utf16_length(part)), None,
            )
            if observation is not None:
                words.append({"text": part, "box": _vision_box(observation.boundingBox())})
        offset += _utf16_length(part)
    return words


def _vertical_center(line: TextLine) -> float:
    return line["box"]["top"] + line["box"]["height"] / 2


def reading_order(lines: List[TextLine]) -> List[TextLine]:
    """
    Satırları okuma sırasına dizer: üstten alta, aynı hizadakileri soldan sağa. İki satırın dikey
    merkezleri kısa olanın yarım yüksekliğinden yakınsa aynı hizadadır. Satır başına bant bölmesi
    farklı yükseklikteki satırların sırasını karıştırıyordu (ilan kartında şirket başlıktan önce). Saf.
    """
    rows: List[List[TextLine]] = []
    for line in sorted(lines, key=_vertical_center):
        if rows:
            anchor: TextLine = rows[-1][0]
            tolerance: float = min(anchor["box"]["height"], line["box"]["height"]) / 2
            if abs(_vertical_center(line) - _vertical_center(anchor)) <= tolerance:
                rows[-1].append(line)
                continue
        rows.append([line])
    return [line for row in rows for line in sorted(row, key=lambda item: item["box"]["left"])]


def recognize_text(image: object) -> List[TextLine]:
    """
    CGImage üzerindeki metni doğru (accurate) kipte tanır ve okuma sırasıyla döner. Dil düzeltmesi
    kapalıdır: kod, URL ve özel adlar sözlüğe göre "düzeltilmesin". Dil otomatik algılanır; Vision'ın
    desteklemediği Fince gibi Latin alfabeli diller de aksanlarıyla okunur.
    """
    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    request.setUsesLanguageCorrection_(False)
    request.setAutomaticallyDetectsLanguage_(True)
    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(image, None)
    succeeded, error = handler.performRequests_error_([request], None)
    if not succeeded:
        raise TextRecognitionError(f"Vision metin tanıma başarısız: {error}")
    lines: List[TextLine] = []
    for observation in request.results() or []:
        candidates = observation.topCandidates_(1)
        if not candidates:
            continue
        candidate = candidates[0]
        text: str = str(candidate.string())
        if not text.strip():
            continue
        lines.append({
            "text": text,
            "confidence": float(candidate.confidence()),
            "box": _vision_box(observation.boundingBox()),
            "words": _word_boxes(candidate, text),
        })
    return reading_order(lines)


def normalize_text(text: str) -> str:
    """Eşleştirme anahtarı: küçük harf, aksansız, tek boşluklu. 'Lähetä' ile 'lahetä' eşleşir. Saf."""
    decomposed: str = unicodedata.normalize("NFKD", text.casefold().translate(_CHARACTER_MAP))
    return " ".join("".join(ch for ch in decomposed if not unicodedata.combining(ch)).split())


def _word_key(text: str) -> str:
    """Kelimeyi kenar noktalaması olmadan karşılaştırma anahtarına çevirir. Saf."""
    return normalize_text(text).strip(_WORD_EDGE_PUNCTUATION)


def union_box(boxes: List[TextBox]) -> TextBox:
    """Kutuların birleşimini döner; liste boş olmamalıdır. Saf."""
    left: float = min(box["left"] for box in boxes)
    top: float = min(box["top"] for box in boxes)
    right: float = max(box["left"] + box["width"] for box in boxes)
    bottom: float = max(box["top"] + box["height"] for box in boxes)
    return {"left": left, "top": top, "width": right - left, "height": bottom - top}


def box_center(box: TextBox) -> Tuple[int, int]:
    """Kutunun merkezini tamsayı model noktası olarak döner. Saf."""
    return (round(box["left"] + box["width"] / 2), round(box["top"] + box["height"] / 2))


def _word_span_box(line: TextLine, wanted_words: List[str]) -> Optional[TextBox]:
    """Aranan kelimeler satırda ardışık geçiyorsa o kelimelerin birleşik kutusunu döner. Saf."""
    keys: List[str] = [_word_key(word["text"]) for word in line["words"]]
    size: int = len(wanted_words)
    for start in range(len(keys) - size + 1):
        if keys[start:start + size] == wanted_words:
            return union_box([word["box"] for word in line["words"][start:start + size]])
    return None


def _substring_box(line: TextLine, wanted: str) -> Optional[TextBox]:
    """
    Aranan metin bir kelimenin parçasıysa ('tietosuojaseloste' ⊂ 'tietosuojaselosteen') onu
    içeren kelimelerin kutusunu döner. Kelime anahtarları tek boşlukla birleştirilerek karakter
    konumu kelime sırasına eşlenir. Saf.
    """
    keys: List[str] = [normalize_text(word["text"]) for word in line["words"]]
    joined: str = " ".join(keys)
    start: int = joined.find(wanted)
    if start < 0:
        return None
    end: int = start + len(wanted)
    covered: List[TextBox] = []
    position: int = 0
    for key, word in zip(keys, line["words"], strict=True):
        word_end: int = position + len(key)
        if word_end > start and position < end:
            covered.append(word["box"])
        position = word_end + 1
    return union_box(covered) if covered else None


def _display_core(normalized: str) -> Tuple[str, bool]:
    """
    Satırın simge/madde işareti önekini ('@', '•', '>_') ve sondaki kırpma üç noktasını atar;
    (çekirdek metin, satır kırpılmış mı) döner. GitHub bağlantıyı '@https://…/donati...' diye
    kısaltıyordu. Saf.
    """
    truncated: bool = normalized.endswith("...")
    core: str = normalized[:-3] if truncated else normalized
    start: int = 0
    while start < len(core) and not core[start].isalnum():
        start += 1
    return core[start:].strip(), truncated


def same_text(first: str, second: str) -> bool:
    """
    İki metin simge öneki, kenar noktalaması ('*', ':'), aksan ve OCR'ın tek harflik okuma farkı
    ('tietosuoiaselosteen') dışında aynı mı? Fazladan kelime ('Lead Full Stack Developer' ile
    'Full Stack Developer') aynı sayılmaz. Saf.
    """
    edges: str = _WORD_EDGE_PUNCTUATION + " "
    left: str = _display_core(normalize_text(first))[0].strip(edges)
    right: str = _display_core(normalize_text(second))[0].strip(edges)
    return left == right or SequenceMatcher(None, left, right).ratio() >= SAME_TEXT_MIN_RATIO


def find_text_matches(lines: List[TextLine], query: str) -> List[TextMatch]:
    """
    Aranan metnin görünür satırlardaki eşleşmelerini skorla sıralı döner: tam satır (1.0),
    ardışık kelimeler (0.95), kelime parçası (0.9), kırpılmış satırın başı (0.88), bulanık satır
    (≤0.85) ve aranan uzun metnin içinde geçen satır (≤0.8; satıra bölünmüş başlık). Kutu mümkünse
    yalnız eşleşen kelimeleri kapsar. Saf.
    """
    wanted: str = normalize_text(query)
    if not wanted:
        return []
    wanted_words: List[str] = [_word_key(word) for word in wanted.split(" ")]
    matches: List[TextMatch] = []
    for line in lines:
        normalized: str = normalize_text(line["text"])
        core, truncated = _display_core(normalized)
        if wanted in (normalized, core, normalized.strip(_WORD_EDGE_PUNCTUATION)):
            matches.append({"line_text": line["text"], "box": line["box"], "score": 1.0})
            continue
        span: Optional[TextBox] = _word_span_box(line, wanted_words)
        if span is not None:
            matches.append({"line_text": line["text"], "box": span, "score": 0.95})
            continue
        part: Optional[TextBox] = _substring_box(line, wanted)
        if part is not None:
            matches.append({"line_text": line["text"], "box": part, "score": 0.9})
            continue
        if truncated and len(core) >= 8 and wanted.startswith(core):
            matches.append({"line_text": line["text"], "box": line["box"], "score": 0.88})
            continue
        ratio: float = SequenceMatcher(None, wanted, core).ratio()
        if ratio >= FUZZY_MIN_RATIO:
            matches.append({"line_text": line["text"], "box": line["box"], "score": 0.85 * ratio})
            continue
        if len(core) >= max(4, len(wanted) // 2) and core in wanted:
            matches.append({
                "line_text": line["text"], "box": line["box"], "score": 0.8 * len(core) / len(wanted),
            })
    return sorted(matches, key=lambda match: -match["score"])


def select_text_match(
    matches: List[TextMatch], near: Optional[Tuple[int, int]],
) -> Tuple[Optional[TextMatch], List[TextMatch]]:
    """
    Tıklanacak eşleşmeyi seçer. near verildiyse güçlü adaylardan ona en yakını; verilmediyse
    tek en iyi aday. En iyi kademede birden çok aday varsa seçim yapılmaz ve adaylar döner
    (yanlış satıra tıklamak yerine model near ile ayırt eder). Saf.
    """
    if not matches:
        return None, []
    if near is not None:
        strong: List[TextMatch] = [match for match in matches if match["score"] >= NEAR_MIN_SCORE] or matches
        return min(strong, key=lambda match: math.dist(box_center(match["box"]), near)), []
    best: float = matches[0]["score"]
    tier: List[TextMatch] = [match for match in matches if match["score"] >= best - SCORE_TIE]
    if len(tier) == 1:
        return tier[0], []
    return None, tier


def similar_texts(lines: List[TextLine], query: str, limit: int) -> List[str]:
    """Bulunamayan metne en çok benzeyen görünür satırlar: yazım ya da OCR farkını modele gösterir. Saf."""
    wanted: str = normalize_text(query)
    scored: List[Tuple[float, str]] = sorted(
        ((SequenceMatcher(None, wanted, normalize_text(line["text"])).ratio(), line["text"]) for line in lines),
        key=lambda item: -item[0],
    )
    return [text for ratio, text in scored[:limit] if ratio >= 0.5]


def lines_within(lines: List[TextLine], region: TextBox) -> List[TextLine]:
    """Merkezi bölgenin içinde kalan satırları döner (kaydırılan panelin metni). Saf."""
    right: float = region["left"] + region["width"]
    bottom: float = region["top"] + region["height"]
    selected: List[TextLine] = []
    for line in lines:
        x, y = box_center(line["box"])
        if region["left"] <= x <= right and region["top"] <= y <= bottom:
            selected.append(line)
    return selected


def without_top_cut(lines: List[TextLine], region: TextBox, edge: float) -> List[TextLine]:
    """
    Bölgenin üst kenarına değen satırları atar: kaydırmadan sonra üstte yarım görünen satır bir
    önceki sayfada tam okunmuştu; kesik okunuşu örtüşme eşleşmesini bozuyordu. Saf.
    """
    return [line for line in lines if line["box"]["top"] > region["top"] + edge]


def split_bottom_cut(lines: List[TextLine], region: TextBox, edge: float) -> Tuple[List[str], List[str]]:
    """
    Satır metinlerini (tam görünen, alt kenara değen) diye ayırır. Alttaki kesik satır bir sonraki
    sayfada tam görünür; yalnız son sayfada okunanlara eklenir. Saf.
    """
    bottom: float = region["top"] + region["height"]
    body: List[str] = []
    cut: List[str] = []
    for line in lines:
        target: List[str] = cut if line["box"]["top"] + line["box"]["height"] >= bottom - edge else body
        target.append(line["text"])
    return body, cut


def merge_page_lines(accumulated: List[str], page: List[str]) -> Tuple[List[str], int]:
    """
    Kaydırmadan sonra okunan sayfayı öncekine ekler: önceki metnin sonuyla yeni sayfanın başı
    arasındaki en uzun örtüşme (satırların en az %80'i birebir aynı) atılır. (birleşik satırlar,
    atılan örtüşme satırı sayısı) döner; örtüşme yoksa sayfa olduğu gibi eklenir. Satırlar yalnız
    birebir (normalize) eşitse aynıdır: aynı satır aynı çözünürlükte aynı okunur. Bulanık benzerlik
    'Palkka: 5200 €' ile 'Palkka: 5300 €'yu ve aynı cümleyle başlayan paragrafları aynı sayıp
    kaydırılan sayfanın yeni satırlarını (ilanın maaş satırı dahil) yutuyordu. Saf.
    """
    previous_keys: List[str] = [normalize_text(line) for line in accumulated]
    page_keys: List[str] = [normalize_text(line) for line in page]
    for size in range(min(len(previous_keys), len(page_keys)), 0, -1):
        tail: List[str] = previous_keys[-size:]
        head: List[str] = page_keys[:size]
        equal: int = sum(1 for first, second in zip(tail, head, strict=True) if first == second)
        if equal >= max(1, math.ceil(size * OVERLAP_MIN_SHARE)):
            return accumulated + page[size:], size
    return accumulated + page, 0
