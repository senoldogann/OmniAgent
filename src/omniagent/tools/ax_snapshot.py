"""
OmniAgent Öğe Tabanlı Erişilebilirlik Anlık Görüntüsü (tools/ax_snapshot.py)
Capture -> Element Indexing -> Action -> Verify iş akışının veri modeli, saf mantığı ve macOS AX
ağacının PyObjC ile okunması.

Saf kısımlar (süzgeç, sıralama, indeksleme, kırpma, render, bayatlık, doğrulama karşılaştırması) AX
API'sini çağırmaz: Linux CI'da sahte pyobjc ile import edilir ve ekransız sentetik ağaçlarla sınanır.
PyObjC yalnız `read_*` ve `capture_snapshot` işlevlerinde, çağrı anında `AX.` niteliğiyle kullanılır
(`from ... import` yok: testlerin yamaları ve başsız modun Quartz kapatması çalışsın diye).

Kanıtlanmış macOS/Chrome davranışları (Chrome 154, macOS 26): web erişilebilirliği kapalı başlar,
uygulama öğesine AXEnhancedUserInterface=True yazılınca (dönüş kodu yanıltıcı biçimde -25208 olsa da)
~2 sn içinde AXWebArea belirir; öğe eylemleri (AXPress, AXValue) bundan sonra arka planda da çalışır;
AXValue yazıldıktan hemen sonra geri okunursa eski değer döner (asenkron güncelleme).
"""
import hashlib
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Dict, FrozenSet, Iterable, List, Literal, Optional, Protocol, Set, Tuple, TypedDict, cast

import AppKit
import ApplicationServices as AX
import numpy as np

from .screen import frame_change_ratio, points_to_model
from .types import (
    AX_LABEL_SEARCH_NODES, AX_MESSAGING_TIMEOUT_SECONDS, AX_WEB_POLL_SECONDS,
    OBSERVATION_ELEMENT_LIMIT, SETTLE_CHANGED_RATIO, SETTLE_PIXEL_DELTA, SNAPSHOT_ELEMENT_LIMIT,
    SNAPSHOT_FRAME_OVERLAP_MIN, SNAPSHOT_LABEL_HINT_LIMIT, SNAPSHOT_LABEL_LIMIT, SNAPSHOT_NODE_LIMIT,
    SNAPSHOT_MIN_VISIBLE_EDGE, SNAPSHOT_ROW_TOLERANCE_POINTS, SNAPSHOT_STATIC_TEXT_LIMIT, SNAPSHOT_STATIC_TEXT_MIN_CHARS,
    SNAPSHOT_LABEL_REGISTRY_LIMIT, SNAPSHOT_TEXT_AREA_MAX_CHARS, SNAPSHOT_TIME_BUDGET_SECONDS, SNAPSHOT_VALUE_LIMIT,
    ScreenGeometry, ToolError,
)

# --- AX hata kodları (Apple AXError.h; sayısal değerler kararlıdır). Saf kararlar PyObjC'ye bağlı kalmasın diye yerel. ---
AX_ERROR_SUCCESS: int = 0
AX_ERROR_FAILURE: int = -25200
AX_ERROR_ILLEGAL_ARGUMENT: int = -25201
AX_ERROR_INVALID_ELEMENT: int = -25202
AX_ERROR_CANNOT_COMPLETE: int = -25204
AX_ERROR_ATTRIBUTE_UNSUPPORTED: int = -25205
AX_ERROR_ACTION_UNSUPPORTED: int = -25206
AX_ERROR_NOT_IMPLEMENTED: int = -25208
AX_ERROR_API_DISABLED: int = -25211
AX_ERROR_NO_VALUE: int = -25212

# --- Roller ---

# AX rolü -> modele gösterilen kısa ad. AXStaticText yalnız bağlam içindir (etkileşimli sayılmaz).
_ROLE_NAMES: Dict[str, str] = {
    "AXButton": "button", "AXLink": "link", "AXTextField": "textfield", "AXSecureTextField": "textfield",
    "AXTextArea": "textarea", "AXCheckBox": "checkbox", "AXRadioButton": "radio", "AXPopUpButton": "popup",
    "AXComboBox": "combobox", "AXMenuItem": "menuitem", "AXMenuButton": "menubutton", "AXSlider": "slider",
    "AXDisclosureTriangle": "disclosure", "AXIncrementor": "stepper", "AXSwitch": "switch", "AXTab": "tab",
    "AXRow": "row", "AXStaticText": "text",
}
STATIC_TEXT_ROLE: str = "AXStaticText"
ROW_ROLE: str = "AXRow"
WEB_AREA_ROLE: str = "AXWebArea"
INTERACTIVE_ROLES: FrozenSet[str] = frozenset(_ROLE_NAMES) - frozenset({STATIC_TEXT_ROLE})
# Basılınca bir işlemi tamamlayabilen roller (ödeme/gönderim düğmesi olabilir): host onay kapısı yalnız bunlara bakar.
# Metin alanı, onay kutusu, seçenek düğmesi, açılır menü ve kaydırıcı taahhüt etmez; adları (ör. 'Donate amount')
# gereksiz onay istemesin.
COMMIT_ROLES: FrozenSet[str] = frozenset({"AXButton", "AXLink", "AXMenuItem", "AXMenuButton", "AXRow"})
TEXT_INPUT_ROLES: FrozenSet[str] = frozenset({"AXTextField", "AXSecureTextField", "AXTextArea", "AXComboBox"})
# Değeri anlamlı olan roller: metin girdileri ile işaret/seçim durumu taşıyanlar
VALUE_ROLES: FrozenSet[str] = TEXT_INPUT_ROLES | frozenset({
    "AXCheckBox", "AXRadioButton", "AXPopUpButton", "AXSlider", "AXSwitch", "AXIncrementor", "AXTab",
})
# Alt ağacın görünür alanını daraltan roller: pencere, kaydırma kutusu, web alanı, sayfa (sheet), çekmece, açılır pano
CLIP_ROLES: FrozenSet[str] = frozenset({"AXWindow", "AXScrollArea", "AXWebArea", "AXSheet", "AXDrawer", "AXPopover"})
SECURE_SUBROLE: str = "AXSecureTextField"
TAB_SUBROLE: str = "AXTabButton"
# Adı olmayan pencere kontrol düğmeleri (trafik ışıkları): alt rolden ad verilir ki model yanlışlıkla pencereyi
# kapatmasın; bunlar 'içerik' sayılmaz (Electron gibi boş AX ağacı hâlâ boş sayılsın).
WINDOW_CONTROL_LABELS: Dict[str, str] = {
    "AXCloseButton": "pencereyi kapat", "AXMinimizeButton": "pencereyi küçült",
    "AXZoomButton": "pencereyi yakınlaştır", "AXFullScreenButton": "tam ekran",
}
# Adı olmayan bu roller hiçbir şey ifade etmez (ör. Chrome <select> seçeneklerinin adsız yankıları): listelenmez
_NAMELESS_SKIPPED_ROLES: FrozenSet[str] = frozenset({"AXMenuItem"})
# Statik metin ham adayı, sınırın bu katına kadar toplanır (etkileşimli öğeyle yinelenenler sonradan elenir)
_STATIC_OVERSAMPLE: int = 3
# Etiketi olmayan etkileşimli öğe için ipucu (ilişkili etiket/alt metin) okunan rol kümesi
_HINT_ROLES: FrozenSet[str] = INTERACTIVE_ROLES

Effect = Literal["changed", "unchanged", "unknown"]
PressStep = Literal["done", "escalate", "ambiguous", "stale", "permission"]
ApprovedStep = Literal["done", "foreground", "unverified", "stale", "permission"]


# --- Veri modeli ---

class Frame(TypedDict):
    """Küresel ekran koordinatında (nokta, sol-üst köken) dikdörtgen."""
    x: float
    y: float
    w: float
    h: float


class Element(TypedDict):
    """Anlık görüntüdeki indeksli GUI öğesi. secure: parola alanı (değeri asla okunmaz)."""
    index: int
    role: str
    subrole: str
    label: str
    value: str
    description: str
    frame: Frame
    actions: List[str]
    enabled: bool
    focused: bool
    secure: bool


class SnapshotStats(TypedDict):
    """Taramanın maliyeti ve kalitesi. web_ready: Chromium uygulamasında web ağacı geldi mi; Chromium değilse None."""
    nodes_visited: int
    pruned_subtrees: int
    seconds: float
    web_ready: Optional[bool]


class Snapshot(TypedDict):
    """Bir pencerenin etkileşimli öğe listesi; id ile bayat eylemler engellenir, digest ile aynılık anlaşılır."""
    id: str
    app: str
    pid: int
    window_title: str
    created_at: float
    elements: List[Element]
    truncated: bool
    digest: str
    stats: SnapshotStats


class SnapshotLimits(TypedDict):
    """Tarama sınırları: öğe sayısı, düğüm sayısı, zaman bütçesi ve bağlam metni kotası."""
    element_limit: int
    node_limit: int
    time_budget_seconds: float
    static_text_limit: int


SNAPSHOT_LIMITS: SnapshotLimits = {
    "element_limit": SNAPSHOT_ELEMENT_LIMIT, "node_limit": SNAPSHOT_NODE_LIMIT,
    "time_budget_seconds": SNAPSHOT_TIME_BUDGET_SECONDS, "static_text_limit": SNAPSHOT_STATIC_TEXT_LIMIT,
}
# Otomatik gözlemdeki özet: yalnız etkileşimli öğeler, küçük kota (ekran görüntüsü bağlamı zaten verir)
OBSERVATION_LIMITS: SnapshotLimits = {
    "element_limit": OBSERVATION_ELEMENT_LIMIT, "node_limit": SNAPSHOT_NODE_LIMIT,
    "time_budget_seconds": SNAPSHOT_TIME_BUDGET_SECONDS, "static_text_limit": 0,
}


class RawNode(TypedDict):
    """Tek AX düğümünün okunmuş ham öznitelikleri (saf gezgin bunlarla çalışır). children opak referanslardır."""
    role: str
    subrole: str
    title: str
    description: str
    placeholder: str
    frame: Optional[Frame]
    enabled: bool
    focused: bool
    children: List[object]


@dataclass(frozen=True)
class NodeReader:
    """
    Gezginin AX dünyasına açılan dar arayüzü. read_node: öğe kaybolduysa None. read_value yalnız
    değer taşıyan ve gizli olmayan rollerde çağrılır. read_hint yalnız etiketsiz öğede çağrılır.
    """
    read_node: Callable[[object], Optional[RawNode]]
    read_value: Callable[[object, str], str]
    read_actions: Callable[[object], List[str]]
    read_hint: Callable[[object, str], str]


@dataclass(frozen=True)
class Collected:
    """Gezginin ham çıktısı: (öğe, AX referansı) çiftleri belge sırasında, indekssiz."""
    found: Tuple[Tuple[Element, object], ...]
    nodes_visited: int
    pruned_subtrees: int
    truncated: bool
    saw_web_area: bool
    seconds: float


@dataclass(frozen=True)
class CapturedSnapshot:
    """Yakalanan anlık görüntü + AX referansları (indeks-1 sırasıyla) + pencere/uygulama öğeleri."""
    snapshot: Snapshot
    refs: Tuple[object, ...]
    window: object
    application: object


class LiveElement(TypedDict):
    """Öğenin eylem öncesi yeniden okunan canlı durumu (bayatlık kararı ve hedef durumu için)."""
    role: str
    subrole: str
    label: str
    value: str
    frame: Frame
    enabled: bool
    focused: bool
    secure: bool


class EffectProbe(TypedDict):
    """
    Eylem öncesi/sonrası hafif durum parmak izi. screen: uygulama pencere yığınının küçük gri karesi
    (ekran izni yoksa None). unreadable: okunamayan sinyallerin adları ('değişmedi' kararını 'bilinmiyor'a çevirir).
    """
    window_title: str
    window_count: int
    focus: str
    target: Optional[LiveElement]
    screen: Optional[np.ndarray]
    unreadable: Tuple[str, ...]


# --- Saf geometri ve metin yardımcıları ---

def intersect(first: Frame, second: Frame) -> Optional[Frame]:
    """İki dikdörtgenin kesişimi; ortak alan yoksa None. Saf."""
    left: float = max(first["x"], second["x"])
    top: float = max(first["y"], second["y"])
    right: float = min(first["x"] + first["w"], second["x"] + second["w"])
    bottom: float = min(first["y"] + first["h"], second["y"] + second["h"])
    if right <= left or bottom <= top:
        return None
    return {"x": left, "y": top, "w": right - left, "h": bottom - top}


def frame_area(frame: Frame) -> float:
    """Dikdörtgen alanı. Saf."""
    return max(frame["w"], 0.0) * max(frame["h"], 0.0)


def frame_center(frame: Frame) -> Tuple[float, float]:
    """Dikdörtgenin merkezi. Saf."""
    return (frame["x"] + frame["w"] / 2, frame["y"] + frame["h"] / 2)


def contains_point(frame: Frame, x: float, y: float) -> bool:
    """Nokta dikdörtgenin içinde mi (alt/sağ kenar hariç). Saf."""
    return frame["x"] <= x < frame["x"] + frame["w"] and frame["y"] <= y < frame["y"] + frame["h"]


def frames_agree(stored: Frame, live: Frame, minimum: float) -> bool:
    """
    Öğe hâlâ listede görüldüğü yerde mi: canlı çerçevenin saklı (görünür) çerçeveyle örtüşen alanı,
    saklı alanın en az `minimum` oranı olmalı. Kısmen görünen öğe ve küçük kaymalar geçer; başka yere
    sıçrayan (yeniden sıralanan/kaydırılan) öğe geçmez. Saf.
    """
    overlap: Optional[Frame] = intersect(stored, live)
    stored_area: float = frame_area(stored)
    return overlap is not None and stored_area > 0 and frame_area(overlap) / stored_area >= minimum


def normalize_text(text: str) -> str:
    """Boşlukları tek aralığa indirir ve kırpar. Saf."""
    return " ".join(text.split())


def clip_label(text: str, limit: int) -> str:
    """Metni normalleştirip limitte '…' ile keser; token ekonomisi için. Saf."""
    normalized: str = normalize_text(text)
    return normalized if len(normalized) <= limit else normalized[:max(limit - 1, 0)] + "…"


def role_name(role: str, subrole: str) -> str:
    """Modele gösterilen kısa rol adı (sekme düğmesi alt rolü 'tab' olur). Saf."""
    if subrole == TAB_SUBROLE:
        return "tab"
    return _ROLE_NAMES.get(role, role.removeprefix("AX").lower())


def is_secure(role: str, subrole: str) -> bool:
    """Parola alanı mı: değeri hiçbir koşulda okunmaz/gösterilmez. Saf."""
    return role == "AXSecureTextField" or subrole == SECURE_SUBROLE


def element_gate_labels(element: Element) -> List[str]:
    """Öğenin kullanıcıya görünen ad adayları (ad, açıklama, değer): host onay kapısı bunların her birine bakar. Saf."""
    return [text for text in (element["label"], element["description"], element["value"]) if text]


def label_from_raw(raw: RawNode) -> str:
    """Öğenin adı: başlık, açıklama, yer tutucu sırasıyla ilk dolu olan (kırpılmış). Saf."""
    for candidate in (raw["title"], raw["description"], raw["placeholder"]):
        text: str = clip_label(candidate, SNAPSHOT_LABEL_LIMIT)
        if text:
            return text
    return WINDOW_CONTROL_LABELS.get(raw["subrole"], "")


def hint_label(raw: RawNode, node: object, reader: NodeReader) -> str:
    """Etiketsiz öğenin adı için ilişkili etiket/alt metin ipucu (kırpılmış). Saf (okuyucu üzerinden)."""
    return clip_label(reader.read_hint(node, raw["role"]), SNAPSHOT_LABEL_LIMIT)


def resolve_label(raw: RawNode, node: object, reader: NodeReader) -> str:
    """Ham özniteliklerden ad; boşsa ve rol ipucu okunan roldeyse ilişkili etiket/alt metin ipucundan. Saf (okuyucu üzerinden)."""
    label: str = label_from_raw(raw)
    if label or raw["role"] not in _HINT_ROLES:
        return label
    return hint_label(raw, node, reader)


# --- Gezgin (saf; okuyucu enjekte edilir) ---

def _is_visible_frame(frame: Optional[Frame]) -> bool:
    return frame is not None and frame["w"] > 0 and frame["h"] > 0


def collect_elements(
    root: object, reader: NodeReader, limits: SnapshotLimits, root_clip: Frame, clock: Callable[[], float],
) -> Collected:
    """
    Pencere ağacını belge sırasıyla (derinlik öncelikli) gezer; yalnız görünür etkileşimli öğeleri (ve
    kotalı bağlam metnini) toplar. Görünürlük: atalardan gelen klip dikdörtgeniyle kesişim; çerçevesi
    klibin tamamen dışında kalan alt ağaçlara İNİLMEZ (uzun sayfada maliyet düşer). Sınırlar (düğüm, zaman,
    öğe sayısı) aşılırsa `truncated` bildirilir; sessiz kırpma yoktur. Parola alanının değeri okunmaz.
    Web alanı içindeki tablo satırları (AXRow) listelenmez: içerdikleri öğeler zaten ayrı çıkar.
    """
    started: float = clock()
    stack: List[Tuple[object, Frame, bool]] = [(root, root_clip, False)]
    # Aynı düğüm birden çok yoldan görünebilir (Chrome tablosu satırlardan ve sütunlardan aynı hücreleri verir): bir kez gezilir
    seen: Set[object] = {root}
    found: List[Tuple[Element, object]] = []
    interactive: int = 0
    statics: int = 0
    visited: int = 0
    descended: int = 0
    pruned: int = 0
    hints: int = 0
    saw_web_area: bool = False
    truncated: bool = False
    static_raw_limit: int = limits["static_text_limit"] * _STATIC_OVERSAMPLE
    while stack:
        # Düğüm sınırı yalnız içine inilen düğümleri sayar: klip dışı budanan düğümler ucuzdur (tek okuma, alt ağaç yok)
        # ve sınırı doldurup görünür alanı eksiksiz bulmuş taramayı yanlışlıkla 'kısaltıldı' göstermemelidir.
        if descended >= limits["node_limit"] or clock() - started > limits["time_budget_seconds"]:
            truncated = True
            break
        if interactive >= limits["element_limit"]:
            truncated = True
            break
        node, clip, in_web = stack.pop()
        visited += 1
        raw: Optional[RawNode] = reader.read_node(node)
        if raw is None:
            continue  # Tarama sırasında kaybolan öğe (dinamik arayüz)
        frame: Optional[Frame] = raw["frame"]
        visible: Optional[Frame] = None
        node_clip: Frame = clip
        if _is_visible_frame(frame):
            visible = intersect(clip, frame) if frame is not None else None
            if visible is None:
                pruned += 1
                continue
            if raw["role"] in CLIP_ROLES:
                node_clip = visible
        descended += 1
        role: str = raw["role"]
        below_web: bool = in_web or role == WEB_AREA_ROLE
        saw_web_area = saw_web_area or role == WEB_AREA_ROLE
        fresh: List[object] = [child for child in raw["children"] if child not in seen]
        seen.update(fresh)
        stack.extend((child, node_clip, below_web) for child in reversed(fresh))
        if visible is None or min(visible["w"], visible["h"]) < SNAPSHOT_MIN_VISIBLE_EDGE:
            continue
        secure: bool = is_secure(role, raw["subrole"])
        if role in INTERACTIVE_ROLES:
            if role == ROW_ROLE and in_web:
                continue
            allow_hint: bool = hints < SNAPSHOT_LABEL_HINT_LIMIT
            label: str = label_from_raw(raw)
            if not label and allow_hint and role in _HINT_ROLES:
                hints += 1
                label = hint_label(raw, node, reader)
            if not label and role in _NAMELESS_SKIPPED_ROLES:
                continue
            value: str = ""
            if not secure and role in VALUE_ROLES:
                value = clip_label(reader.read_value(node, role), SNAPSHOT_VALUE_LIMIT)
            description: str = clip_label(raw["description"], SNAPSHOT_LABEL_LIMIT)
            found.append(({
                "index": 0, "role": role, "subrole": raw["subrole"], "label": label, "value": value,
                "description": description if description != label else "", "frame": visible,
                "actions": reader.read_actions(node), "enabled": raw["enabled"], "focused": raw["focused"],
                "secure": secure,
            }, node))
            interactive += 1
        elif role == STATIC_TEXT_ROLE and statics < static_raw_limit:
            text: str = clip_label(reader.read_value(node, role), SNAPSHOT_LABEL_LIMIT)
            if len(text) >= SNAPSHOT_STATIC_TEXT_MIN_CHARS:
                statics += 1
                found.append(({
                    "index": 0, "role": role, "subrole": raw["subrole"], "label": text, "value": "",
                    "description": "", "frame": visible, "actions": [], "enabled": True,
                    "focused": False, "secure": False,
                }, node))
    return Collected(tuple(found), visited, pruned, truncated, saw_web_area, clock() - started)


# --- Anlık görüntü kurma (saf) ---

def drop_duplicate_static_text(items: List[Tuple[Element, object]]) -> List[Tuple[Element, object]]:
    """
    Bir etkileşimli öğenin içinde kalan ve aynı metni taşıyan statik metni eler (düğme/bağlantı iç metni
    listede iki kez çıkmasın). Saf.
    """
    interactive: List[Element] = [element for element, _ref in items if element["role"] != STATIC_TEXT_ROLE]
    kept: List[Tuple[Element, object]] = []
    for element, ref in items:
        if element["role"] == STATIC_TEXT_ROLE:
            x, y = frame_center(element["frame"])
            if any(
                owner["label"].casefold() == element["label"].casefold() and contains_point(owner["frame"], x, y)
                for owner in interactive
            ):
                continue
        kept.append((element, ref))
    return kept


def cap_static_text(items: List[Tuple[Element, object]], limit: int) -> List[Tuple[Element, object]]:
    """Etkileşimli öğelerin hepsini, statik metinlerin yalnız ilk `limit` tanesini tutar (belge sırası). Saf."""
    kept: List[Tuple[Element, object]] = []
    statics: int = 0
    for element, ref in items:
        if element["role"] == STATIC_TEXT_ROLE:
            if statics >= limit:
                continue
            statics += 1
        kept.append((element, ref))
    return kept


def reading_order(items: List[Tuple[Element, object]], tolerance: float) -> List[Tuple[Element, object]]:
    """
    Okuma sırası: yukarıdan aşağı satırlar (merkez y farkı `tolerance` içindekiler aynı satır), satır içinde
    soldan sağa. Aynı satırdaki farklı yükseklikli öğeler böylece karışmaz. Saf.
    """
    by_top: List[Tuple[Element, object]] = sorted(
        items, key=lambda item: (frame_center(item[0]["frame"])[1], item[0]["frame"]["x"]),
    )
    ordered: List[Tuple[Element, object]] = []
    row: List[Tuple[Element, object]] = []
    row_y: float = 0.0
    for item in by_top:
        center_y: float = frame_center(item[0]["frame"])[1]
        if row and center_y - row_y > tolerance:
            ordered.extend(sorted(row, key=lambda entry: entry[0]["frame"]["x"]))
            row = []
        if not row:
            row_y = center_y
        row.append(item)
    ordered.extend(sorted(row, key=lambda entry: entry[0]["frame"]["x"]))
    return ordered


def quantized_frame(frame: Frame) -> str:
    """Alt piksel titreşimi digest/imzayı bozmasın diye çerçeveyi 4 pt'lik kovalara yuvarlar. Saf."""
    return f"{round(frame['x'] / 4)}:{round(frame['y'] / 4)}:{round(frame['w'] / 4)}:{round(frame['h'] / 4)}"


def snapshot_digest(window_title: str, elements: List[Element]) -> str:
    """
    Liste içeriğinin kısa özeti: pencere başlığı + her öğenin rol/etiket/değer/durum/yuvarlanmış çerçevesi.
    Kimlik ve zaman girmez: aynı arayüz iki kez alınınca aynı digest çıkar. Saf.
    """
    lines: List[str] = [window_title] + [
        f"{element['role']}|{element['label']}|{element['value']}|{int(element['enabled'])}|"
        f"{int(element['focused'])}|{quantized_frame(element['frame'])}"
        for element in elements
    ]
    return hashlib.sha1("\n".join(lines).encode("utf-8")).hexdigest()[:12]


def assemble_snapshot(
    snapshot_id: str, app: str, pid: int, window_title: str, created_at: float,
    collected: Collected, limits: SnapshotLimits, web_ready: Optional[bool],
) -> Tuple[Snapshot, Tuple[object, ...]]:
    """Ham gezgin çıktısını sıralı, indeksli Snapshot'a ve indeks-1 sırasıyla AX referanslarına çevirir. Saf."""
    items: List[Tuple[Element, object]] = drop_duplicate_static_text(list(collected.found))
    items = cap_static_text(items, limits["static_text_limit"])
    items = reading_order(items, SNAPSHOT_ROW_TOLERANCE_POINTS)
    elements: List[Element] = [
        {**element, "index": position} for position, (element, _ref) in enumerate(items, start=1)
    ]
    snapshot: Snapshot = {
        "id": snapshot_id, "app": app, "pid": pid, "window_title": clip_label(window_title, SNAPSHOT_LABEL_LIMIT),
        "created_at": created_at, "elements": elements, "truncated": collected.truncated,
        "digest": snapshot_digest(window_title, elements),
        "stats": {
            "nodes_visited": collected.nodes_visited, "pruned_subtrees": collected.pruned_subtrees,
            "seconds": round(collected.seconds, 3), "web_ready": web_ready,
        },
    }
    return snapshot, tuple(ref for _element, ref in items)


# --- Host doğrulaması için etiket kaydı (bilinçli, sınırlı süreç geneli durum) ---
# Model bir öğeyi (liste kimliği, indeks) ile ister; gönderim koruması (app/verification.py) çağrının hedef etiketini
# görmek zorundadır ve o saf işlevler Toolbox'a erişemez. Bu yüzden son yakalamaların etiketleri süreç genelinde,
# kilitli ve SINIRLI (en son SNAPSHOT_LABEL_REGISTRY_LIMIT liste) tutulur; kimlikler görev başına 's1'den başladığı için
# aynı kimliğin yeni yakalaması eskisinin yerini alır. Yalnız OKUMA amaçlıdır: eylem kararı bu kayda dayanmaz (eylemler
# canlı çözümlenir), bu yüzden bayat bir kayıt en çok bir koruma sezgisini yanıltır.
_LABEL_LOCK: threading.Lock = threading.Lock()
_RECENT_LABELS: "OrderedDict[str, Tuple[str, ...]]" = OrderedDict()


def normalize_snapshot_id(raw: str) -> str:
    """Model rakamı 's' öneksiz verirse ('3') kabul edilir: 's3'. Saf."""
    wanted: str = str(raw).strip()
    return f"s{wanted}" if wanted.isdigit() else wanted


def remember_snapshot_labels(snapshot: Snapshot) -> None:
    """Yakalanan listenin öğe etiketlerini (ad, yoksa açıklama, yoksa değer) kayda alır; en eski liste sınır aşılınca düşer."""
    labels: Tuple[str, ...] = tuple(
        element["label"] or element["description"] or element["value"] for element in snapshot["elements"]
    )
    with _LABEL_LOCK:
        _RECENT_LABELS[snapshot["id"]] = labels
        _RECENT_LABELS.move_to_end(snapshot["id"])
        while len(_RECENT_LABELS) > SNAPSHOT_LABEL_REGISTRY_LIMIT:
            _RECENT_LABELS.popitem(last=False)


def snapshot_element_label(snapshot_id: str, index: object) -> str:
    """Kayıtlı listedeki öğenin etiketi; liste/indeks kayıtta yoksa ya da geçersizse boş metin (bilinmiyor). Saf okuma."""
    if isinstance(index, bool) or not isinstance(index, int):
        return ""
    with _LABEL_LOCK:
        labels: Optional[Tuple[str, ...]] = _RECENT_LABELS.get(normalize_snapshot_id(snapshot_id))
    return labels[index - 1] if labels is not None and 1 <= index <= len(labels) else ""


# --- Modele çıktı (saf) ---

# Otomatik gözleme eklenen AX özetinin ilk sözcükleri: ajan döngüsü eski özetleri bu işaretle tanıyıp bağlamdan düşürür
# (her tur ~1 bin token biriktirmesin); düşürülen özetin yerine AX_SUMMARY_TRIMMED konur.
AX_SUMMARY_MARKER: str = "AX özeti ("
AX_SUMMARY_TRIMMED: str = "[eski AX özeti bağlamdan çıkarıldı]"
EMPTY_SNAPSHOT_HINT: str = (
    "Bu pencerenin erişilebilirlik ağacı içerik vermiyor (Electron/canvas/Tk olabilir); aradığın öğe bu listede "
    "çıkmaz. Görünür metne cua_click_text ile tıkla; metni olmayan ikona take_screenshot görüntüsündeki noktayla tıkla."
)
WEB_NOT_READY_HINT: str = (
    "Web içeriği erişilebilirlik ağacında henüz yok (pencere arka planda/örtülü olabilir): uygulamayı öne getirip "
    "(chrome_active_tab veya cua_get_app) cua_snapshot'ı yeniden çağır."
)
TRUNCATED_HINT: str = (
    "…liste öğe/süre sınırıyla kısaltıldı; aranan öğe yoksa sayfayı kaydırıp cua_snapshot'ı yenile ya da take_screenshot kullan."
)


def snapshot_is_empty(elements: List[Element]) -> bool:
    """Listede adı ya da değeri olan hiç öğe yok mu (yalnız etiketsiz pencere düğmeleri = boş AX). Saf."""
    return not any(
        (element["label"] or element["value"]) and element["subrole"] not in WINDOW_CONTROL_LABELS
        for element in elements
    )


def render_element(element: Element, geometry: ScreenGeometry) -> str:
    """
    Tek öğe satırı: '[N] rol "etiket" değer=… @merkezx,merkezy genişlikxyükseklik'. Konum ve boyut ekran
    görüntüsü/tıklama uzayındadır (0-1000). Parola alanında değer yerine '[gizli alan]'. Saf.
    """
    center_x, center_y = frame_center(element["frame"])
    x, y = points_to_model(center_x, center_y, geometry)
    width: int = max(1, round(element["frame"]["w"] * geometry["model_width"] / geometry["point_width"]))
    height: int = max(1, round(element["frame"]["h"] * geometry["model_height"] / geometry["point_height"]))
    parts: List[str] = [f"[{element['index']}] {role_name(element['role'], element['subrole'])}"]
    if element["label"]:
        parts.append('"' + element["label"].replace('"', "'") + '"')
    if element["secure"]:
        parts.append("[gizli alan]")
    elif element["value"]:
        parts.append("değer=" + element["value"].replace('"', "'"))
    if not element["enabled"]:
        parts.append("pasif")
    if element["focused"]:
        parts.append("odaklı")
    parts.append(f"@{x},{y} {width}x{height}")
    return " ".join(parts)


def render_snapshot(snapshot: Snapshot, geometry: ScreenGeometry) -> str:
    """
    Modele giden metin: başlık satırı (kimlik, uygulama, pencere, öğe sayısı) + öğe satırları + gerekirse
    kırpma/boş AX/web hazır değil ipuçları. Token ekonomisi için etiket ve değerler kırpılmıştır. Saf.
    """
    lines: List[str] = [
        f"{snapshot['id']} · {snapshot['app']} · pencere {snapshot['window_title']!r} · {len(snapshot['elements'])} öğe "
        f"(@merkez genişlikxyükseklik, ekran görüntüsü/tıklama uzayı {geometry['model_width']}×{geometry['model_height']})"
    ]
    lines.extend(render_element(element, geometry) for element in snapshot["elements"])
    if snapshot["truncated"]:
        lines.append(TRUNCATED_HINT)
    if snapshot["stats"]["web_ready"] is False:
        lines.append(WEB_NOT_READY_HINT)
    elif snapshot_is_empty(snapshot["elements"]):
        lines.append(EMPTY_SNAPSHOT_HINT)
    return "\n".join(lines)


# --- Bayatlık ve doğrulama kararları (saf) ---

def stale_reason(element: Element, live: LiveElement) -> Optional[str]:
    """
    Anlık görüntüdeki öğe ile yeniden okunan canlı durumu karşılaştırır: rol, ad (saklı ad varsa) ve konum
    uyuşmuyorsa nedenini döner (STALE_ELEMENT hata metni için); taze ise None. Saf.
    """
    if live["role"] != element["role"]:
        return f"rolü değişti ({element['role']} -> {live['role']})"
    if element["label"] and live["label"].casefold() != element["label"].casefold():
        return f"adı değişti ({element['label']!r} -> {live['label']!r})"
    if not frames_agree(element["frame"], live["frame"], SNAPSHOT_FRAME_OVERLAP_MIN):
        return "listede görüldüğü yerden ayrıldı (sayfa kaydı ya da yeniden düzenleme)"
    return None


def press_outcome(error_code: int, effect: Optional[Effect]) -> PressStep:
    """
    AX eyleminin (AXPress/AXFocused) sonucu için sıradaki adım. Dönüş kodu güvenilmezdir: 0 gelse de eylem
    etkisiz olabilir, bu yüzden etki doğrulanır. CannotComplete (uygulama meşgul/modal) eylem yine de
    işlemiş olabilir: doğrulamaya bağlanır. Etki 'bilinmiyor' ise ESKALASYON YOK (çift tetikleme riski).
    Desteklenmeyen eylem yoklamasız eskale edilir. Saf.
    """
    if error_code == AX_ERROR_INVALID_ELEMENT:
        return "stale"
    if error_code == AX_ERROR_API_DISABLED:
        return "permission"
    if error_code not in (AX_ERROR_SUCCESS, AX_ERROR_CANNOT_COMPLETE):
        return "escalate"
    if effect == "changed":
        return "done"
    if effect == "unchanged":
        return "escalate"
    return "ambiguous"


def approved_press_outcome(error_code: int, effect: Optional[Effect]) -> ApprovedStep:
    """
    Host onayından geçmiş ödeme/sipariş düğmesinde AXPress sonucunun sıradaki adımı: TEK tetikleme kuralı. Sıradan
    press_outcome'dan farkı: etki 'etkisiz' ya da 'bilinmiyor' görünse de ikinci tetikleme (ön plan tıklaması)
    YAPILMAZ ('unverified'): etki yalnız gecikmiş olabilir ve ikinci tıklama çift ödeme/sipariş olurdu. Ön plana
    yalnız AX eylemi HİÇ uygulanamadıysa (eylem desteklenmiyor: hiçbir şey tetiklenmedi) geçilir. Saf.
    """
    if error_code == AX_ERROR_INVALID_ELEMENT:
        return "stale"
    if error_code == AX_ERROR_API_DISABLED:
        return "permission"
    if error_code == AX_ERROR_ACTION_UNSUPPORTED:
        return "foreground"
    if error_code in (AX_ERROR_SUCCESS, AX_ERROR_CANNOT_COMPLETE) and effect == "changed":
        return "done"
    return "unverified"


def _screen_changed(before: Optional[np.ndarray], after: Optional[np.ndarray]) -> Optional[bool]:
    """İki gri karenin belirgin farkı: kare yoksa None (bilinmiyor). Saf."""
    if before is None or after is None:
        return None
    return frame_change_ratio(before, after, SETTLE_PIXEL_DELTA) > SETTLE_CHANGED_RATIO


def changed_signals(before: EffectProbe, after: EffectProbe) -> Tuple[str, ...]:
    """
    Eylem öncesi/sonrası parmak izlerindeki fark sinyalleri (insan okunur, sonuç metnine girer):
    pencere başlığı, pencere sayısı, odak, hedefin durumu (yok oldu / değer / etkin / odak / rol / çerçeve) ve ekran.
    Saf.
    """
    signals: List[str] = []
    if before["window_title"] != after["window_title"]:
        signals.append("pencere başlığı değişti")
    if before["window_count"] != after["window_count"]:
        signals.append("pencere sayısı değişti")
    if before["focus"] != after["focus"]:
        signals.append("odak değişti")
    target_before, target_after = before["target"], after["target"]
    if target_before is not None and target_after is None:
        signals.append("öğe kayboldu")
    elif target_before is not None and target_after is not None:
        if target_before["value"] != target_after["value"]:
            signals.append("öğe değeri değişti")
        if target_before["enabled"] != target_after["enabled"]:
            signals.append("öğenin etkinlik durumu değişti")
        if target_before["focused"] != target_after["focused"]:
            signals.append("öğe odak durumu değişti")
        if target_before["label"].casefold() != target_after["label"].casefold():
            signals.append("öğe adı değişti")
        if not frames_agree(target_before["frame"], target_after["frame"], SNAPSHOT_FRAME_OVERLAP_MIN):
            signals.append("öğe yer değiştirdi")
    if _screen_changed(before["screen"], after["screen"]):
        signals.append("ekran değişti")
    return tuple(signals)


def verify_effect(before: EffectProbe, after: EffectProbe) -> Effect:
    """
    Eylem etkisi kararı: herhangi bir sinyal varsa 'changed'; sinyal yok ve her şey okunabildiyse
    'unchanged'; sinyal yok ama okunamayan probe varsa 'unknown' (etki gözden kaçmış olabilir, körlemesine
    eskalasyon çift tetikleme yapar). Saf.
    """
    if changed_signals(before, after):
        return "changed"
    return "unknown" if before["unreadable"] or after["unreadable"] else "unchanged"


def poll_effect(
    read_probe: Callable[[], EffectProbe], before: EffectProbe, timeout_seconds: float, interval_seconds: float,
    clock: Callable[[], float], sleep: Callable[[float], None], check_stop: Callable[[], None],
) -> Tuple[Effect, Tuple[str, ...]]:
    """
    Sleep yerine durum yoklaması: sınırlı süre içinde parmak izini okur, ilk fark görülünce hemen döner.
    Süre dolarsa son karar verify_effect'ten gelir; hiçbir okumada görülen okunamayan sinyal unutulmaz (son okuma
    okunabilir olsa da 'unknown'). Döner: (karar, sinyaller). Saf (zaman ve okuma enjekte edilir; kullanıcı
    durdurması check_stop ile yükselir).
    """
    deadline: float = clock() + timeout_seconds
    unreadable_seen: Tuple[str, ...] = ()
    while True:
        check_stop()
        after: EffectProbe = read_probe()
        signals: Tuple[str, ...] = changed_signals(before, after)
        if signals:
            return "changed", signals
        unreadable_seen = tuple(dict.fromkeys((*unreadable_seen, *after["unreadable"])))
        if clock() >= deadline:
            return verify_effect(before, {**after, "unreadable": unreadable_seen}), ()
        sleep(interval_seconds)


def chromium_framework_present(framework_names: List[str]) -> bool:
    """
    Uygulama paketi Chromium tabanlı mı (Chrome, Edge, Brave, Electron, CEF…): Frameworks içinde
    '<ad> Framework.framework' bulunur. Bu uygulamalarda web erişilebilirliği açılmadıkça sayfa içeriği ağaçta yoktur. Saf.
    """
    return any(name.endswith(" Framework.framework") for name in framework_names)


# --- PyObjC okuma (AX API'sini çağıran ince katman) ---

class BundleURL(Protocol):
    """NSURL'nin kullanılan yüzü (pyobjc tür bilgisi vermez)."""

    def path(self) -> str: ...


class RunningApplication(Protocol):
    """NSRunningApplication'ın kullanılan yüzü (pyobjc tür bilgisi vermez; küçük sözleşme tek yerde durur)."""

    def bundleURL(self) -> Optional[BundleURL]: ...

    def localizedName(self) -> Optional[str]: ...

    def bundleIdentifier(self) -> Optional[str]: ...


def running_application(pid: int) -> Optional[RunningApplication]:
    """PID'nin NSRunningApplication kaydı; süreç kayıtlı değilse None (tools/foreground.py de bunu kullanır)."""
    return cast(Optional[RunningApplication], AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid))


def ax_list(value: object) -> List[object]:
    """AX dizi değeri (NSArray/tuple) liste olarak; değer yoksa ya da boşsa boş liste. pyobjc yalnız 'object' verir, daraltma burada."""
    return list(cast(Iterable[object], value)) if value else []


_NODE_ATTRIBUTES: List[str] = [
    "AXRole", "AXSubrole", "AXTitle", "AXDescription", "AXPlaceholderValue",
    "AXPosition", "AXSize", "AXEnabled", "AXFocused", "AXChildren",
]


def ax_missing_to_none(value: object) -> object:
    """CopyMultipleAttributeValues eksik öznitelikleri AXError tipli AXValue olarak döner; onları None yapar."""
    if isinstance(value, AX.AXValueRef) and AX.AXValueGetType(value) == AX.kAXValueAXErrorType:
        return None
    return value


def ax_frame(position_value: object, size_value: object) -> Optional[Frame]:
    """AXPosition ve AXSize değerlerinden çerçeve; biri okunamazsa None."""
    if not isinstance(position_value, AX.AXValueRef) or not isinstance(size_value, AX.AXValueRef):
        return None
    position_ok, point = AX.AXValueGetValue(position_value, AX.kAXValueCGPointType, None)
    size_ok, size = AX.AXValueGetValue(size_value, AX.kAXValueCGSizeType, None)
    if not (position_ok and size_ok):
        return None
    return {"x": float(point.x), "y": float(point.y), "w": float(size.width), "h": float(size.height)}


def ax_text(value: object) -> str:
    """AX metin değeri; metin olmayanlar boş."""
    return normalize_text(value) if isinstance(value, str) else ""


def ax_value_text(value: object) -> str:
    """AXValue'yu metne çevirir: işaret kutuları 0/1, sayılar sade, metin normalleştirilmiş; diğerleri boş."""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(int(value)) if float(value).is_integer() else f"{value:g}"
    return ax_text(value)


def ax_read(node: object, attribute: str) -> object:
    """Tek AX özniteliği; öğede yoksa None. Yanıtsız uygulama açık hata verir."""
    error, value = AX.AXUIElementCopyAttributeValue(node, attribute, None)
    if error == AX.kAXErrorSuccess:
        return value
    if error == AX.kAXErrorCannotComplete:
        raise ToolError("Uygulama erişilebilirlik sorgusuna yanıt vermiyor (meşgul olabilir).", "AX_TIMEOUT", True)
    return None


def read_ax_node(node: object) -> Optional[RawNode]:
    """
    Düğümün ham özniteliklerini TEK toplu çağrıyla okur (AXValue bilerek yok: parola alanı değeri hiçbir
    koşulda okunmasın diye değer ayrı, rol bilindikten sonra okunur). Öğe kaybolduysa None.
    """
    error, values = AX.AXUIElementCopyMultipleAttributeValues(node, _NODE_ATTRIBUTES, 0, None)
    if error == AX.kAXErrorInvalidUIElement:
        return None
    if error == AX.kAXErrorCannotComplete:
        raise ToolError("Uygulama erişilebilirlik sorgusuna yanıt vermiyor (meşgul olabilir).", "AX_TIMEOUT", True)
    if error != AX.kAXErrorSuccess:
        raise ToolError(f"AX öznitelikleri okunamadı: hata kodu={error}", "AX_READ_FAILED", True)
    attributes: Dict[str, object] = {
        name: ax_missing_to_none(value) for name, value in zip(_NODE_ATTRIBUTES, values, strict=True)
    }
    children: object = attributes["AXChildren"]
    return {
        "role": ax_text(attributes["AXRole"]), "subrole": ax_text(attributes["AXSubrole"]),
        "title": ax_text(attributes["AXTitle"]), "description": ax_text(attributes["AXDescription"]),
        "placeholder": ax_text(attributes["AXPlaceholderValue"]),
        "frame": ax_frame(attributes["AXPosition"], attributes["AXSize"]),
        "enabled": attributes["AXEnabled"] is not False, "focused": attributes["AXFocused"] is True,
        "children": ax_list(children),
    }


def read_ax_value(node: object, role: str) -> str:
    """
    AXValue metni. Yalnız gizli olmayan öğede çağrılır. Büyük metin alanı (belge gövdesi) tamamen okunmaz:
    önce karakter sayısına bakılır, eşiği aşarsa '[N karakter]' döner.
    """
    if role == "AXTextArea":
        count: object = ax_read(node, "AXNumberOfCharacters")
        if isinstance(count, int) and not isinstance(count, bool) and count > SNAPSHOT_TEXT_AREA_MAX_CHARS:
            return f"[{count} karakter]"
    return ax_value_text(ax_read(node, "AXValue"))


def read_ax_actions(node: object) -> List[str]:
    """Öğenin desteklediği AX eylem adları (yalnız etkileşimli öğede çağrılır)."""
    error, names = AX.AXUIElementCopyActionNames(node, None)
    if error == AX.kAXErrorCannotComplete:
        raise ToolError("Uygulama erişilebilirlik sorgusuna yanıt vermiyor (meşgul olabilir).", "AX_TIMEOUT", True)
    return [str(name) for name in names] if error == AX.kAXErrorSuccess and names else []


def _descendant_text(node: object) -> str:
    """Etiketsiz öğenin (bağlantı, satır, düğme) ilk alt metnini sınırlı genişlikte arar."""
    queue: List[object] = [node]
    visited: int = 0
    while queue and visited < AX_LABEL_SEARCH_NODES:
        current: object = queue.pop(0)
        visited += 1
        children: object = ax_read(current, "AXChildren")
        for child in ax_list(children):
            if ax_read(child, "AXRole") == STATIC_TEXT_ROLE:
                text: str = ax_text(ax_read(child, "AXValue"))
                if text:
                    return text
            queue.append(child)
    return ""


def read_ax_hint(node: object, role: str) -> str:
    """
    Etiketsiz öğenin adı için ipucu: metin girdisinde ilişkili etiket (AXTitleUIElement), tanımlayıcı, yardım
    metni; diğerlerinde ilk alt metin. Mail'in Kime/Konu/Gövde alanları böyle ayırt edilir.
    """
    if role in TEXT_INPUT_ROLES:
        linked: object = ax_read(node, "AXTitleUIElement")
        if isinstance(linked, AX.AXUIElementRef):
            for attribute in ("AXValue", "AXTitle", "AXDescription"):
                text: str = ax_text(ax_read(linked, attribute))
                if text:
                    return text
        for attribute in ("AXIdentifier", "AXHelp"):
            text = ax_text(ax_read(node, attribute))
            if text:
                return text
        return ""
    return _descendant_text(node)


AX_NODE_READER: NodeReader = NodeReader(
    read_node=read_ax_node, read_value=read_ax_value, read_actions=read_ax_actions, read_hint=read_ax_hint,
)


def read_live_element(node: object, reader: NodeReader) -> Optional[LiveElement]:
    """
    Öğenin canlı durumunu (rol, ad, değer, çerçeve, etkin, odak) okur; öğe artık yoksa None. Ad, anlık
    görüntüdekiyle aynı kuralla (ipucu dahil) kurulur ki karşılaştırma tutarlı olsun. Parola alanının
    değeri okunmaz. Çerçeve okunamazsa sıfır çerçeve döner (bayatlık kararı bunu 'yerinden ayrıldı' sayar).
    """
    raw: Optional[RawNode] = reader.read_node(node)
    if raw is None:
        return None
    secure: bool = is_secure(raw["role"], raw["subrole"])
    value: str = ""
    if not secure and raw["role"] in VALUE_ROLES:
        value = clip_label(reader.read_value(node, raw["role"]), SNAPSHOT_VALUE_LIMIT)
    # Statik metnin adı kendi metnidir (AXValue); anlık görüntüde de öyle kurulur, karşılaştırma tutarlı kalsın
    label: str = (
        clip_label(reader.read_value(node, raw["role"]), SNAPSHOT_LABEL_LIMIT) if raw["role"] == STATIC_TEXT_ROLE
        else resolve_label(raw, node, reader)
    )
    frame: Frame = raw["frame"] if raw["frame"] is not None else {"x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0}
    return {
        "role": raw["role"], "subrole": raw["subrole"], "label": label,
        "value": value, "frame": frame, "enabled": raw["enabled"], "focused": raw["focused"], "secure": secure,
    }


def bundle_framework_names(pid: int) -> List[str]:
    """Uygulama paketinin Contents/Frameworks içindeki girdi adları; paket/dizin yoksa boş."""
    running: Optional[RunningApplication] = running_application(pid)
    bundle: Optional[BundleURL] = running.bundleURL() if running is not None else None
    if bundle is None:
        return []
    frameworks: str = os.path.join(str(bundle.path()), "Contents", "Frameworks")
    try:
        return sorted(os.listdir(frameworks))
    except (FileNotFoundError, NotADirectoryError):
        return []


def enable_web_accessibility(application: object) -> None:
    """
    Chromium/Electron uygulamasında web erişilebilirliğini açar. Chromium bu iki uygulama düzeyi özniteliği
    kendi erişilebilirlik temsilcisinde işler; AX API'si ise -25205/-25208 gibi yanıltıcı kodlar döner (ölçüldü:
    kodlara rağmen ağaç ~2 sn içinde gelir), bu yüzden dönüş kodları burada anlamsızdır. Uygulama yeniden
    başlayana kadar kalıcıdır; idempotenttir.
    """
    AX.AXUIElementSetAttributeValue(application, "AXEnhancedUserInterface", True)
    AX.AXUIElementSetAttributeValue(application, "AXManualAccessibility", True)


def pick_window(application: object, app_name: str) -> object:
    """Uygulamanın odaktaki (yoksa ana, yoksa ilk) penceresi; yoksa açık hata."""
    for attribute in ("AXFocusedWindow", "AXMainWindow"):
        window: object = ax_read(application, attribute)
        if window is not None:
            return window
    windows: List[object] = ax_list(ax_read(application, "AXWindows"))
    if windows:
        return windows[0]
    raise ToolError(
        f"{app_name} uygulamasının bu masaüstünde erişilebilir penceresi yok (başka bir alanda veya küçültülmüş "
        "olabilir); önce öne getir (chrome_active_tab veya cua_get_app).",
        "AX_NO_WINDOW", True,
    )


def capture_snapshot(
    pid: int, app_name: str, snapshot_id: str, limits: SnapshotLimits, web_wait_seconds: float,
) -> CapturedSnapshot:
    """
    pid'in penceresinin anlık görüntüsünü yakalar (yalnız okur, hiçbir şey tıklamaz). Chromium tabanlı
    uygulamada web erişilebilirliği açılır ve AXWebArea belirene kadar en çok web_wait_seconds (durum yoklamasıyla)
    beklenir: ilk yakalamada ~2 sn sürer. Pencere örtülü/arka plandaysa ağaç gelmeyebilir; bu durum
    `stats.web_ready=False` ile bildirilir. AX sorgusu takılan uygulamaya karşı mesajlaşma zaman aşımı kurulur.
    """
    application: object = AX.AXUIElementCreateApplication(pid)
    AX.AXUIElementSetMessagingTimeout(application, AX_MESSAGING_TIMEOUT_SECONDS)
    chromium: bool = chromium_framework_present(bundle_framework_names(pid))
    if chromium:
        enable_web_accessibility(application)
    started: float = time.monotonic()
    while True:
        window: object = pick_window(application, app_name)
        window_frame: Optional[Frame] = ax_frame(ax_read(window, "AXPosition"), ax_read(window, "AXSize"))
        if window_frame is None:
            raise ToolError(f"Pencere sınırları okunamadı: {app_name}", "WINDOW_BOUNDS_FAILED", True)
        collected: Collected = collect_elements(window, AX_NODE_READER, limits, window_frame, time.monotonic)
        if not chromium or collected.saw_web_area or time.monotonic() - started >= web_wait_seconds:
            break
        time.sleep(AX_WEB_POLL_SECONDS)
    snapshot, refs = assemble_snapshot(
        snapshot_id, app_name, pid, ax_text(ax_read(window, "AXTitle")), time.time(), collected, limits,
        collected.saw_web_area if chromium else None,
    )
    return CapturedSnapshot(snapshot, refs, window, application)
