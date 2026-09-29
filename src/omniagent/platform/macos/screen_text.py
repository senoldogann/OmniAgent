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
import re
import unicodedata
from difflib import SequenceMatcher
from typing import List, Optional, Tuple, TypedDict

import Quartz
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
_CODE_TOKEN: re.Pattern[str] = re.compile(r"\b[A-Za-z0-9]{2,8}-[A-Za-z0-9]{5,}\b")
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


def choose_focused_code(original: str, focused_text: str) -> str:
    """Odaklı OCR yalnız O/0 ayrımında uzlaşıyorsa kod sözcüğünü düzeltir. Saf."""
    if (
        _CODE_TOKEN.fullmatch(original) is None
        or not any(char.isdigit() for char in original)
        or not any(char in "Oo0" for char in original)
    ):
        return original
    prefix, suffix = original.split("-", 1)
    matches: List[str] = [
        token for token in _CODE_TOKEN.findall(focused_text)
        if len(token) == len(original)
        and token.split("-", 1)[0].casefold() == prefix.casefold()
        and token.split("-", 1)[1].upper().replace("O", "0")
        == suffix.upper().replace("O", "0")
    ]
    return matches[0] if len(set(matches)) == 1 else original


def _focused_code_text(image: object, box: TextBox, padding_px: int) -> Optional[str]:
    """Kod sözcüğünü çevresiyle kırpıp ayrı Vision isteğiyle yeniden okur."""
    width = int(Quartz.CGImageGetWidth(image))
    height = int(Quartz.CGImageGetHeight(image))
    left = max(0, round(box["left"] * width / NORMALIZED_SPACE) - padding_px)
    top = max(0, round(box["top"] * height / NORMALIZED_SPACE) - padding_px)
    right = min(width, round((box["left"] + box["width"]) * width / NORMALIZED_SPACE) + padding_px)
    bottom = min(height, round((box["top"] + box["height"]) * height / NORMALIZED_SPACE) + padding_px)
    if right <= left or bottom <= top:
        return None
    crop = Quartz.CGImageCreateWithImageInRect(
        image, Quartz.CGRectMake(left, top, right - left, bottom - top),
    )
    if crop is None:
        return None
    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    request.setUsesLanguageCorrection_(False)
    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(crop, None)
    succeeded, _error = handler.performRequests_error_([request], None)
    if not succeeded:
        return None
    candidates = [
        str(candidate.string())
        for observation in request.results() or []
        for candidate in observation.topCandidates_(1)
    ]
    return " ".join(candidates) if candidates else None


def _refine_code_words(image: object, text: str, words: List[TextWord]) -> Tuple[str, List[TextWord]]:
    """Tam ekran OCR'ın O/0 karışıklığını yalnız kod kırpımında teyitle düzeltir."""
    refined_text = text
    refined_words: List[TextWord] = []
    for word in words:
        original = word["text"]
        suspicious = (
            _CODE_TOKEN.fullmatch(original) is not None
            and any(char.isdigit() for char in original)
            and any(char in "Oo0" for char in original)
        )
        if suspicious:
            first = choose_focused_code(
                original, _focused_code_text(image, word["box"], 20) or "",
            )
            second = choose_focused_code(
                original, _focused_code_text(image, word["box"], 30) or "",
            )
            chosen = first if first == second else original
        else:
            chosen = original
        if chosen != original:
            refined_text = refined_text.replace(original, chosen, 1)
        refined_words.append({**word, "text": chosen})
    return refined_text, refined_words


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
        refined_text, words = _refine_code_words(image, text, _word_boxes(candidate, text))
        lines.append({
            "text": refined_text,
            "confidence": float(candidate.confidence()),
            "box": _vision_box(observation.boundingBox()),
            "words": words,
        })
    return reading_order(lines)


def region_around(center: Tuple[int, int], radius_x: int, radius_y: int) -> TextBox:
    """Merkez etrafında 0-1000 uzayına sıkıştırılmış dikdörtgen bölge. Saf."""
    left: int = max(0, center[0] - radius_x)
    top: int = max(0, center[1] - radius_y)
    right: int = min(NORMALIZED_SPACE, center[0] + radius_x)
    bottom: int = min(NORMALIZED_SPACE, center[1] + radius_y)
    return {"left": float(left), "top": float(top), "width": float(right - left), "height": float(bottom - top)}


def _remap_box(box: TextBox, origin: TextBox) -> TextBox:
    """Kırpım uzayındaki (0-1000) kutuyu, kırpımın tam görüntüdeki konumuna göre tam uzaya taşır. Saf."""
    scale_x: float = origin["width"] / NORMALIZED_SPACE
    scale_y: float = origin["height"] / NORMALIZED_SPACE
    return {
        "left": origin["left"] + box["left"] * scale_x, "top": origin["top"] + box["top"] * scale_y,
        "width": box["width"] * scale_x, "height": box["height"] * scale_y,
    }


def recognize_region(image: object, region: TextBox) -> List[TextLine]:
    """
    Görüntünün 0-1000 model uzayındaki bölgesini kırpıp tanır. Dönen satır ve kelime kutuları kırpımın değil
    TAM görüntünün uzayındadır: find_text_matches/select_text_match/box_center doğrudan kullanılır. Yalnız bölge
    kadar alan okunur (tam ekran OCR'dan orantılı ucuz). Bölge boşsa ya da kırpım alınamazsa TextRecognitionError.
    """
    width: int = int(Quartz.CGImageGetWidth(image))
    height: int = int(Quartz.CGImageGetHeight(image))
    left: int = max(0, round(region["left"] * width / NORMALIZED_SPACE))
    top: int = max(0, round(region["top"] * height / NORMALIZED_SPACE))
    right: int = min(width, round((region["left"] + region["width"]) * width / NORMALIZED_SPACE))
    bottom: int = min(height, round((region["top"] + region["height"]) * height / NORMALIZED_SPACE))
    if right <= left or bottom <= top:
        raise TextRecognitionError(f"OCR bölgesi boş: {region}")
    crop = Quartz.CGImageCreateWithImageInRect(image, Quartz.CGRectMake(left, top, right - left, bottom - top))
    if crop is None:
        raise TextRecognitionError(f"OCR bölgesi görüntüden kırpılamadı: {region}")
    origin: TextBox = {
        "left": left * NORMALIZED_SPACE / width, "top": top * NORMALIZED_SPACE / height,
        "width": (right - left) * NORMALIZED_SPACE / width, "height": (bottom - top) * NORMALIZED_SPACE / height,
    }
    return [
        {**line, "box": _remap_box(line["box"], origin),
         "words": [{"text": word["text"], "box": _remap_box(word["box"], origin)} for word in line["words"]]}
        for line in recognize_text(crop)
    ]


def line_at_point(
    lines: List[TextLine], point: Tuple[int, int], tolerance_x: float, tolerance_y: float,
) -> Optional[TextLine]:
    """
    Noktayı (tolerans payıyla) kapsayan satırlardan noktaya en yakın olanı döner; yoksa None. Yalnız en yakın
    satır seçilir: komşu düğmenin etiketi tıklanan düğmeye yazılmasın. Saf.
    """
    def gap(line: TextLine) -> Tuple[float, float]:
        box: TextBox = line["box"]
        return (
            max(box["left"] - point[0], 0.0, point[0] - (box["left"] + box["width"])),
            max(box["top"] - point[1], 0.0, point[1] - (box["top"] + box["height"])),
        )

    gaps: List[Tuple[Tuple[float, float], TextLine]] = [(gap(line), line) for line in lines]
    near: List[Tuple[float, TextLine]] = [
        (math.hypot(dx, dy), line) for (dx, dy), line in gaps if dx <= tolerance_x and dy <= tolerance_y
    ]
    return min(near, key=lambda item: item[0])[1] if near else None


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


class ReadProgress(TypedDict):
    """Kaydırılan panelin o ana dek birleşmiş metni ve bir sonraki kaydırma adımı için gereken durum."""
    text_lines: List[str]
    cut_tail: List[str]
    gaps: int
    step_points: float


def first_pages_progress(
    top_lines: List[TextLine], probe_lines: List[TextLine], region: TextBox, edge: float, step_points: float,
) -> ReadProgress:
    """
    İlk iki okumayı (tepe sayfa ve ilk küçük kaydırmadan sonraki sayfa) birleştirir: bölgeyi bulmak için yapılan ilk
    kaydırmanın metni de katılır, böylece ilk tam kaydırma adımı arada satır atlamaz. Saf.
    """
    top_body, _top_cut = split_bottom_cut(lines_within(top_lines, region), region, edge)
    probe_body, cut_tail = split_bottom_cut(
        without_top_cut(lines_within(probe_lines, region), region, edge), region, edge,
    )
    merged, _overlap = merge_page_lines(top_body, probe_body)
    return {"text_lines": merged, "cut_tail": cut_tail, "gaps": 0, "step_points": step_points}


def absorb_page(
    progress: ReadProgress, page_lines: List[TextLine], region: TextBox, edge: float, gap_marker: str,
) -> ReadProgress:
    """
    Sonraki kaydırma sayfasını birikmiş metne ekler. Örtüşme bulunamazsa araya boşluk işareti konur ve kaydırma adımı
    yarılanır (arada satır atlanmış olabilir). Girdiyi değiştirmez. Saf.
    """
    page, cut_tail = split_bottom_cut(
        without_top_cut(lines_within(page_lines, region), region, edge), region, edge,
    )
    merged, overlap = merge_page_lines(progress["text_lines"], page)
    if overlap == 0 and progress["text_lines"] and page:
        return {
            "text_lines": progress["text_lines"] + [gap_marker] + page, "cut_tail": cut_tail,
            "gaps": progress["gaps"] + 1, "step_points": progress["step_points"] / 2,
        }
    return {
        "text_lines": merged, "cut_tail": cut_tail, "gaps": progress["gaps"], "step_points": progress["step_points"],
    }
