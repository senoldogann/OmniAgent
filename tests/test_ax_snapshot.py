"""
Öğe tabanlı AX anlık görüntüsünün SAF mantığı (tools/ax_snapshot.py): süzgeç, klip budaması, sıralama, indeksleme, kırpma,
render, digest, bayatlık ve eylem-etki doğrulaması. Ekransız sentetik ağaçla çalışır; PyObjC çağrılmaz (Linux CI'da da koşar).
Gerçek Chrome ile sınama: tests/test_ax_live_chrome.py (OMNI_LIVE_GUI=1).
"""
import json
import sys
from typing import Callable, Dict, Iterator, List, Optional, Tuple

import numpy as np
import pytest

from omniagent.app import agent as main
from omniagent.app import tool_schema
from omniagent.tools import ax_snapshot as snap
from omniagent.tools.ax_snapshot import (
    Collected, EffectProbe, Element, Frame, LiveElement, NodeReader, RawNode, Snapshot, SnapshotLimits,
)
from omniagent.tools.types import ScreenGeometry

GEOMETRY: ScreenGeometry = {"point_width": 1000, "point_height": 500, "model_width": 1000, "model_height": 1000}
LIMITS: SnapshotLimits = {"element_limit": 50, "node_limit": 500, "time_budget_seconds": 10.0, "static_text_limit": 5}
WINDOW: Frame = {"x": 0.0, "y": 0.0, "w": 800.0, "h": 600.0}
Tree = Dict[str, Dict[str, object]]


@pytest.fixture(autouse=True)
def clean_label_registry() -> Iterator[None]:
    """
    Etiket kaydı bilinçli SÜREÇ GENELİ durumdur (ax_snapshot._RECENT_LABELS: 's901'…'s1032', 's950' gibi kimlikler bu
    modülün testlerinden sonra da kalırdı). Her testten önce ve sonra boşaltılır: kimlikler başka testlere sızmaz ve
    'kayıtta yok' varsayımları başka modüllerin kalıntısına bağlı olmaz.
    """
    def clear() -> None:
        with snap._LABEL_LOCK:
            snap._RECENT_LABELS.clear()

    clear()
    yield
    clear()


def frame(x: float, y: float, w: float, h: float) -> Tuple[float, float, float, float]:
    return (x, y, w, h)


class Recorder:
    """Sentetik ağaç üzerinde okuyucu: hangi düğümlerin/değerlerin okunduğunu kaydeder (gizlilik ve budama denetimi için)."""

    def __init__(self, tree: Tree) -> None:
        self.tree: Tree = tree
        self.node_reads: List[str] = []
        self.value_reads: List[str] = []

    def reader(self) -> NodeReader:
        def read_node(ref: object) -> Optional[RawNode]:
            self.node_reads.append(str(ref))
            item: Optional[Dict[str, object]] = self.tree.get(str(ref))
            if item is None:
                return None
            box = item.get("frame")
            return {
                "role": str(item.get("role", "AXGroup")), "subrole": str(item.get("subrole", "")),
                "title": str(item.get("title", "")), "description": str(item.get("description", "")),
                "placeholder": str(item.get("placeholder", "")),
                "frame": None if box is None else {"x": box[0], "y": box[1], "w": box[2], "h": box[3]},
                "enabled": bool(item.get("enabled", True)), "focused": bool(item.get("focused", False)),
                "children": list(item.get("children", [])),
            }

        def read_value(ref: object, role: str) -> str:
            self.value_reads.append(str(ref))
            return str(self.tree[str(ref)].get("value", ""))

        return NodeReader(
            read_node=read_node, read_value=read_value,
            read_actions=lambda ref: list(self.tree[str(ref)].get("actions", [])),
            read_hint=lambda ref, role: str(self.tree[str(ref)].get("hint", "")),
        )


class Clock:
    """Her okumada ilerleyen sahte saat (zaman bütçesi testleri için)."""

    def __init__(self, step: float) -> None:
        self.now: float = 0.0
        self.step: float = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def collect(tree: Tree, limits: SnapshotLimits, clock: Callable[[], float], clip: Frame) -> Tuple[Collected, Recorder]:
    recorder = Recorder(tree)
    return snap.collect_elements("root", recorder.reader(), limits, clip, clock), recorder


def elements_of(collected: Collected, limits: SnapshotLimits) -> List[Element]:
    snapshot, _refs = snap.assemble_snapshot("s1", "Uygulama", 42, "Pencere", 0.0, collected, limits, None)
    return snapshot["elements"]


def simple_tree(children: Tree, root_children: List[str]) -> Tree:
    return {"root": {"role": "AXWindow", "frame": frame(0, 0, 800, 600), "children": root_children}, **children}


# --- Gezgin: süzgeç, sıra, klip, gizlilik, sınırlar ---

def test_lists_only_visible_interactive_elements_in_reading_order() -> None:
    tree = simple_tree({
        "link": {"role": "AXLink", "title": "Ayrıntılar", "frame": frame(10, 100, 80, 20), "actions": ["AXPress"]},
        "button": {"role": "AXButton", "title": "Gönder", "frame": frame(200, 104, 60, 24), "actions": ["AXPress"]},
        "field": {"role": "AXTextField", "title": "Ad", "value": "Ali", "frame": frame(10, 40, 200, 20)},
        "group": {"role": "AXGroup", "frame": frame(0, 0, 800, 600)},
        "off": {"role": "AXButton", "title": "Ekran dışı", "frame": frame(10, 900, 50, 20)},
        "zero": {"role": "AXButton", "title": "Sıfır boyut", "frame": frame(10, 200, 0, 0)},
        "disabled": {"role": "AXButton", "title": "Pasif", "enabled": False, "frame": frame(10, 300, 50, 20)},
    }, ["link", "button", "group", "field", "off", "zero", "disabled"])
    collected, _ = collect(tree, LIMITS, Clock(0.0), WINDOW)
    elements = elements_of(collected, LIMITS)
    assert [(e["index"], e["role"], e["label"]) for e in elements] == [
        (1, "AXTextField", "Ad"), (2, "AXLink", "Ayrıntılar"), (3, "AXButton", "Gönder"), (4, "AXButton", "Pasif"),
    ]
    assert elements[0]["value"] == "Ali" and elements[3]["enabled"] is False
    assert elements[1]["actions"] == ["AXPress"] and not collected.truncated


def test_offscreen_subtrees_are_pruned_and_scroll_areas_clip_their_children() -> None:
    tree = simple_tree({
        "scroll": {"role": "AXScrollArea", "frame": frame(0, 100, 400, 100), "children": ["inside", "clipped"]},
        "inside": {"role": "AXButton", "title": "İçeride", "frame": frame(10, 120, 50, 20)},
        "clipped": {"role": "AXButton", "title": "Kutunun altında", "frame": frame(10, 300, 50, 20)},
        "far": {"role": "AXGroup", "frame": frame(0, 2000, 800, 400), "children": ["hidden"]},
        "hidden": {"role": "AXButton", "title": "Çok aşağıda", "frame": frame(10, 2010, 50, 20)},
    }, ["scroll", "far"])
    collected, recorder = collect(tree, LIMITS, Clock(0.0), WINDOW)
    assert [e["label"] for e in elements_of(collected, LIMITS)] == ["İçeride"]
    assert "hidden" not in recorder.node_reads  # klip dışı alt ağaca inilmedi
    assert collected.pruned_subtrees == 2  # 'far' ve kutu altında kalan 'clipped'


def test_secure_field_value_is_never_read_and_marked() -> None:
    tree = simple_tree({
        "pw": {"role": "AXTextField", "subrole": "AXSecureTextField", "title": "Parola", "value": "topsecret",
               "frame": frame(10, 10, 200, 20)},
        "pw2": {"role": "AXSecureTextField", "title": "Onay", "value": "topsecret", "frame": frame(10, 50, 200, 20)},
        "plain": {"role": "AXTextField", "title": "Ad", "value": "Ali", "frame": frame(10, 90, 200, 20)},
    }, ["pw", "pw2", "plain"])
    collected, recorder = collect(tree, LIMITS, Clock(0.0), WINDOW)
    elements = elements_of(collected, LIMITS)
    assert recorder.value_reads == ["plain"]
    assert [(e["secure"], e["value"]) for e in elements] == [(True, ""), (True, ""), (False, "Ali")]


@pytest.mark.parametrize("limits, expected", [
    ({**LIMITS, "element_limit": 3}, 3),
    ({**LIMITS, "node_limit": 4}, None),
    ({**LIMITS, "time_budget_seconds": 0.5}, None),
])
def test_limits_report_truncation_instead_of_cutting_silently(limits: SnapshotLimits, expected: Optional[int]) -> None:
    buttons: Tree = {
        f"b{i}": {"role": "AXButton", "title": f"Düğme {i}", "frame": frame(10, 10 + i * 30, 50, 20)} for i in range(8)
    }
    tree = simple_tree(buttons, list(buttons))
    collected, _ = collect(tree, limits, Clock(0.3), WINDOW)
    assert collected.truncated is True
    if expected is not None:
        assert len(collected.found) == expected


def test_node_reachable_from_two_parents_is_listed_once() -> None:
    tree = simple_tree({
        "row": {"role": "AXGroup", "children": ["cell"]},
        "column": {"role": "AXGroup", "children": ["cell"]},
        "cell": {"role": "AXCheckBox", "title": "seç", "value": "0", "frame": frame(10, 10, 14, 14)},
    }, ["row", "column"])
    collected, _ = collect(tree, LIMITS, Clock(0.0), WINDOW)
    assert [e["label"] for e in elements_of(collected, LIMITS)] == ["seç"]


def test_tiny_visible_slivers_and_nameless_menu_items_are_dropped() -> None:
    tree = simple_tree({
        "sliver": {"role": "AXButton", "title": "Kırpılmış", "frame": frame(10, 595, 50, 20)},  # görünür yükseklik 5>=4
        "thin": {"role": "AXButton", "title": "İnce", "frame": frame(10, 598, 50, 20)},          # görünür yükseklik 2<4
        "menu": {"role": "AXMenuItem", "frame": frame(10, 100, 50, 20)},
        "menu_named": {"role": "AXMenuItem", "title": "Kopyala", "frame": frame(10, 130, 50, 20)},
    }, ["sliver", "thin", "menu", "menu_named"])
    collected, _ = collect(tree, LIMITS, Clock(0.0), WINDOW)
    assert sorted(e["label"] for e in elements_of(collected, LIMITS)) == ["Kopyala", "Kırpılmış"]


def test_static_text_context_is_deduplicated_and_quota_limited() -> None:
    tree = simple_tree({
        "button": {"role": "AXButton", "title": "Kaydet", "frame": frame(10, 10, 100, 30), "children": ["inner"]},
        "inner": {"role": "AXStaticText", "value": "Kaydet", "frame": frame(20, 15, 50, 20)},
        **{f"t{i}": {"role": "AXStaticText", "value": f"Paragraf {i}", "frame": frame(10, 100 + i * 25, 200, 20)}
           for i in range(8)},
        "tiny": {"role": "AXStaticText", "value": "ab", "frame": frame(10, 400, 20, 20)},
    }, ["button", *[f"t{i}" for i in range(8)], "tiny"])
    collected, _ = collect(tree, LIMITS, Clock(0.0), WINDOW)
    elements = elements_of(collected, LIMITS)
    assert [e["label"] for e in elements if e["role"] == "AXStaticText"] == [f"Paragraf {i}" for i in range(5)]
    assert [e["label"] for e in elements if e["role"] == "AXButton"] == ["Kaydet"]


def test_web_table_rows_are_skipped_but_native_rows_get_a_descendant_label() -> None:
    tree = simple_tree({
        "web": {"role": "AXWebArea", "frame": frame(0, 300, 800, 300), "children": ["web_row"]},
        "web_row": {"role": "AXRow", "hint": "web satırı", "frame": frame(0, 310, 800, 20)},
        "native_row": {"role": "AXRow", "hint": "Ekim faturası", "frame": frame(0, 100, 800, 20)},
    }, ["web", "native_row"])
    collected, _ = collect(tree, LIMITS, Clock(0.0), WINDOW)
    assert [(e["role"], e["label"]) for e in elements_of(collected, LIMITS)] == [("AXRow", "Ekim faturası")]
    assert collected.saw_web_area is True


def test_window_controls_get_a_label_and_do_not_count_as_content() -> None:
    tree = simple_tree({
        "close": {"role": "AXButton", "subrole": "AXCloseButton", "frame": frame(10, 10, 16, 16)},
        "zoom": {"role": "AXButton", "subrole": "AXFullScreenButton", "frame": frame(50, 10, 16, 16)},
    }, ["close", "zoom"])
    collected, _ = collect(tree, LIMITS, Clock(0.0), WINDOW)
    elements = elements_of(collected, LIMITS)
    assert [e["label"] for e in elements] == ["pencereyi kapat", "tam ekran"]
    assert snap.snapshot_is_empty(elements) is True  # Electron benzeri boş AX hâlâ boş sayılır


# --- Render, digest, klip yardımcıları ---

def make_snapshot(elements_tree: Tree, root_children: List[str], limits: SnapshotLimits) -> Tuple[Snapshot, Tuple[object, ...]]:
    collected, _ = collect(simple_tree(elements_tree, root_children), limits, Clock(0.0), WINDOW)
    snapshot, refs = snap.assemble_snapshot("s7", "Notlar", 9, "Yeni not", 1.0, collected, limits, None)
    return snapshot, refs


def test_render_lists_indexed_lines_in_model_space_with_state_markers() -> None:
    snapshot, refs = make_snapshot({
        "pw": {"role": "AXTextField", "subrole": "AXSecureTextField", "title": "Parola", "frame": frame(100, 50, 200, 50)},
        "ok": {"role": "AXButton", "title": 'Gönder "hemen"', "enabled": False, "focused": True, "frame": frame(500, 100, 100, 25)},
        "cb": {"role": "AXCheckBox", "title": "Onay", "value": "1", "frame": frame(10, 200, 20, 20)},
    }, ["pw", "ok", "cb"], LIMITS)
    text = snap.render_snapshot(snapshot, GEOMETRY)
    lines = text.splitlines()
    assert lines[0].startswith("s7 · Notlar · pencere 'Yeni not' · 3 öğe")
    assert lines[1] == '[1] textfield "Parola" [gizli alan] @200,150 200x100'
    assert lines[2] == '[2] button "Gönder \'hemen\'" pasif odaklı @550,225 100x50'
    assert lines[3] == '[3] checkbox "Onay" değer=1 @20,420 20x40'
    assert refs == ("pw", "ok", "cb")


def test_render_adds_hints_for_truncated_empty_and_web_not_ready_lists() -> None:
    truncated_limits: SnapshotLimits = {**LIMITS, "element_limit": 1}
    snapshot, _ = make_snapshot({
        "a": {"role": "AXButton", "title": "A", "frame": frame(10, 10, 50, 20)},
        "b": {"role": "AXButton", "title": "B", "frame": frame(10, 50, 50, 20)},
    }, ["a", "b"], truncated_limits)
    assert snap.TRUNCATED_HINT in snap.render_snapshot(snapshot, GEOMETRY)
    empty, _ = make_snapshot({"x": {"role": "AXButton", "frame": frame(10, 10, 50, 20)}}, ["x"], LIMITS)
    assert snap.EMPTY_SNAPSHOT_HINT in snap.render_snapshot(empty, GEOMETRY)
    not_ready = {**empty, "stats": {**empty["stats"], "web_ready": False}}
    rendered = snap.render_snapshot(not_ready, GEOMETRY)
    assert snap.WEB_NOT_READY_HINT in rendered and snap.EMPTY_SNAPSHOT_HINT not in rendered


def test_digest_is_stable_across_ids_and_sensitive_to_content() -> None:
    tree: Tree = {"a": {"role": "AXButton", "title": "A", "frame": frame(12, 12, 48, 20)}}
    first, _ = make_snapshot(tree, ["a"], LIMITS)
    second, _ = make_snapshot(tree, ["a"], LIMITS)
    assert first["digest"] == second["digest"]
    jitter = {"a": {**tree["a"], "frame": frame(12.9, 12.9, 48, 20)}}
    assert make_snapshot(jitter, ["a"], LIMITS)[0]["digest"] == first["digest"]  # alt piksel titreşimi digest'i bozmaz
    changed = {"a": {**tree["a"], "title": "B"}}
    assert make_snapshot(changed, ["a"], LIMITS)[0]["digest"] != first["digest"]


@pytest.mark.parametrize("text, limit, expected", [
    ("  çok   boşluk  ", 20, "çok boşluk"),
    ("abcdefghij", 5, "abcd…"),
    ("kısa", 10, "kısa"),
])
def test_clip_label_normalizes_and_cuts(text: str, limit: int, expected: str) -> None:
    assert snap.clip_label(text, limit) == expected


# --- Bayatlık ---

def live(role: str, label: str, box: Tuple[float, float, float, float]) -> LiveElement:
    return {"role": role, "subrole": "", "label": label, "value": "", "frame": {"x": box[0], "y": box[1], "w": box[2], "h": box[3]},
            "enabled": True, "focused": False, "secure": False}


STORED: Element = {
    "index": 1, "role": "AXButton", "subrole": "", "label": "Sil", "value": "", "description": "",
    "frame": {"x": 100.0, "y": 100.0, "w": 80.0, "h": 20.0}, "actions": ["AXPress"], "enabled": True,
    "focused": False, "secure": False,
}


@pytest.mark.parametrize("current, stale", [
    (live("AXButton", "Sil", (100, 100, 80, 20)), False),
    (live("AXButton", "sil", (104, 103, 80, 20)), False),       # küçük kayma ve büyük/küçük harf
    (live("AXButton", "Sil", (100, 90, 80, 30)), False),        # boyut değişimi ama örtüşme yüksek
    (live("AXLink", "Sil", (100, 100, 80, 20)), True),          # rol değişti
    (live("AXButton", "Kaydet", (100, 100, 80, 20)), True),     # ad değişti
    (live("AXButton", "Sil", (100, 400, 80, 20)), True),        # başka yere sıçradı
])
def test_stale_reason_detects_role_label_and_position_changes(current: LiveElement, stale: bool) -> None:
    assert (snap.stale_reason(STORED, current) is not None) is stale


def test_stale_check_skips_label_when_none_was_recorded() -> None:
    unnamed: Element = {**STORED, "label": ""}
    assert snap.stale_reason(unnamed, live("AXButton", "sonradan gelen ipucu", (100, 100, 80, 20))) is None


# --- Eylem etkisi doğrulaması ---

def probe(**changes: object) -> EffectProbe:
    base: EffectProbe = {
        "window_title": "Sayfa", "window_count": 1, "focus": "AXButton|Tamam|1:1:1:1",
        "target": live("AXButton", "Gönder", (10, 10, 50, 20)), "screen": None, "unreadable": (),
    }
    return {**base, **changes}  # type: ignore[typeddict-item]


UNCHANGED_SCREEN = np.zeros((10, 10), dtype=np.uint8)
CHANGED_SCREEN = np.full((10, 10), 200, dtype=np.uint8)


@pytest.mark.parametrize("after, expected, signal", [
    (probe(), "unchanged", None),
    (probe(window_title="Gönderildi"), "changed", "pencere başlığı"),
    (probe(window_count=2), "changed", "pencere sayısı"),
    (probe(focus="AXTextField|Ad|2:2:2:2"), "changed", "odak değişti"),
    (probe(target=None), "changed", "öğe kayboldu"),
    (probe(target={**live("AXButton", "Gönder", (10, 10, 50, 20)), "value": "1"}), "changed", "öğe değeri"),
    (probe(target={**live("AXButton", "Gönder", (10, 10, 50, 20)), "enabled": False}), "changed", "etkinlik"),
    (probe(target={**live("AXButton", "Gönder", (10, 10, 50, 20)), "focused": True}), "changed", "odak durumu"),
    (probe(target=live("AXButton", "Gönder", (10, 500, 50, 20))), "changed", "yer değiştirdi"),
    (probe(unreadable=("odak",)), "unknown", None),
])
def test_verify_effect_decisions(after: EffectProbe, expected: str, signal: Optional[str]) -> None:
    before = probe()
    assert snap.verify_effect(before, after) == expected
    signals = snap.changed_signals(before, after)
    assert (signal is None) == (not signals)
    if signal is not None:
        assert any(signal in item for item in signals)


def test_verify_effect_screen_signal_and_unreadable_changes_still_count() -> None:
    before = probe(screen=UNCHANGED_SCREEN)
    assert snap.verify_effect(before, probe(screen=UNCHANGED_SCREEN.copy())) == "unchanged"
    assert snap.verify_effect(before, probe(screen=CHANGED_SCREEN)) == "changed"
    assert snap.verify_effect(before, probe(screen=None)) == "unchanged"  # kare yoksa ekran sinyali yok
    assert snap.verify_effect(before, probe(window_title="x", unreadable=("odak",))) == "changed"


@pytest.mark.parametrize("code, effect, step", [
    (snap.AX_ERROR_SUCCESS, "changed", "done"),
    (snap.AX_ERROR_SUCCESS, "unchanged", "escalate"),
    (snap.AX_ERROR_SUCCESS, "unknown", "ambiguous"),
    (snap.AX_ERROR_CANNOT_COMPLETE, "changed", "done"),
    (snap.AX_ERROR_CANNOT_COMPLETE, "unchanged", "escalate"),
    (snap.AX_ERROR_ACTION_UNSUPPORTED, None, "escalate"),
    (snap.AX_ERROR_ATTRIBUTE_UNSUPPORTED, None, "escalate"),
    (snap.AX_ERROR_NOT_IMPLEMENTED, None, "escalate"),
    (snap.AX_ERROR_FAILURE, None, "escalate"),
    (snap.AX_ERROR_INVALID_ELEMENT, None, "stale"),
    (snap.AX_ERROR_API_DISABLED, None, "permission"),
])
def test_press_outcome_never_escalates_on_unknown_effect(code: int, effect: Optional[str], step: str) -> None:
    assert snap.press_outcome(code, effect) == step  # type: ignore[arg-type]


def poll(sequence: List[EffectProbe], timeout: float, before: EffectProbe) -> Tuple[Tuple[str, Tuple[str, ...]], List[float], int]:
    clock = Clock(0.0)
    slept: List[float] = []
    reads: List[int] = []
    remaining = list(sequence)

    def read_probe() -> EffectProbe:
        reads.append(1)
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock.now += seconds

    result = snap.poll_effect(read_probe, before, timeout, 0.1, lambda: clock.now, sleep, lambda: None)
    return result, slept, len(reads)


def test_poll_effect_returns_on_first_change_and_bounds_the_wait() -> None:
    before = probe()
    (effect, signals), slept, reads = poll([probe(), probe(), probe(window_title="Yeni")], 1.0, before)
    assert effect == "changed" and "pencere başlığı değişti" in signals and reads == 3 and len(slept) == 2
    (effect, signals), slept, reads = poll([probe()], 0.3, before)
    assert effect == "unchanged" and signals == () and sum(slept) <= 0.31 and reads >= 3
    (effect, _), _, _ = poll([probe(unreadable=("odak",)), probe()], 0.3, before)
    assert effect == "unknown"  # bir okuma başarısızsa sonuç 'değişmedi' sayılmaz


def test_poll_effect_decision_is_verify_effects_and_unreadable_signals_are_never_forgotten() -> None:
    """
    Süre dolunca karar verify_effect'ten gelir (satır içi kopya kalktı). Okunamayan sinyal hangi okumada görülürse görülsün
    unutulmaz: sonraki temiz okuma ve olay öncesi parmak izindeki 'okunamadı' da 'değişmedi'yi 'bilinmiyor'a çevirir.
    """
    clean, unreadable = probe(), probe(unreadable=("odak",))
    (effect, _), _, _ = poll([clean, unreadable, clean], 0.3, clean)
    assert effect == "unknown"
    (effect, _), _, _ = poll([clean], 0.3, unreadable)  # olay ÖNCESİ okunamadı
    assert effect == "unknown"
    (effect, signals), _, _ = poll([clean], 0.3, clean)
    assert (effect, signals) == ("unchanged", ())
    (effect, signals), _, _ = poll([unreadable, probe(window_title="Yeni", unreadable=("odak",))], 0.3, clean)
    assert effect == "changed" and signals == ("pencere başlığı değişti",)  # fark görülürse okunamayan sinyal kararı değiştirmez


def test_ax_list_normalises_ax_array_values_and_missing_values() -> None:
    """pyobjc yalnız 'object' döndürür (NSArray/tuple/None): tek yardımcı liste yapar; yok ya da boş değer boş liste."""
    assert snap.ax_list(None) == [] and snap.ax_list(()) == [] and snap.ax_list([]) == []
    assert snap.ax_list(("a", "b")) == ["a", "b"] and snap.ax_list(["x"]) == ["x"]


def test_label_hint_is_read_only_for_unnamed_elements_of_hint_roles() -> None:
    """resolve_label: adı olan öğe ipucu okumaz; ipucu yalnız etiketsiz ve ipucu rolündeki öğede okunur (artık sabit bayrak yok)."""
    hints: List[str] = []
    reader = NodeReader(
        read_node=lambda ref: None, read_value=lambda ref, role: "", read_actions=lambda ref: [],
        read_hint=lambda ref, role: hints.append(str(ref)) or "Kime",
    )

    def raw(role: str, title: str) -> RawNode:
        return {"role": role, "subrole": "", "title": title, "description": "", "placeholder": "", "frame": None,
                "enabled": True, "focused": False, "children": []}

    assert snap.resolve_label(raw("AXTextField", "Ad"), "adli", reader) == "Ad"
    assert snap.resolve_label(raw("AXGroup", ""), "grup", reader) == ""
    assert hints == []
    assert snap.resolve_label(raw("AXTextField", ""), "adsiz", reader) == "Kime" and hints == ["adsiz"]
    assert snap.hint_label(raw("AXButton", ""), "dugme", reader) == "Kime" and hints == ["adsiz", "dugme"]


def test_poll_effect_honors_stop_requests() -> None:
    def stop() -> None:
        raise RuntimeError("durduruldu")

    with pytest.raises(RuntimeError):
        snap.poll_effect(lambda: probe(), probe(), 1.0, 0.1, lambda: 0.0, lambda seconds: None, stop)


@pytest.mark.parametrize("names, expected", [
    (["Google Chrome Framework.framework", "Helpers"], True),
    (["Electron Framework.framework", "Squirrel.framework"], True),
    (["Microsoft Edge Framework.framework"], True),
    (["Sparkle.framework", "Python.framework"], False),
    ([], False),
])
def test_chromium_bundles_are_recognized_by_their_framework_name(names: List[str], expected: bool) -> None:
    assert snap.chromium_framework_present(names) is expected


@pytest.mark.skipif(sys.platform != "darwin", reason="Gerçek pyobjc yalnız macOS'ta")
def test_local_ax_error_constants_match_the_real_module() -> None:
    from omniagent.tools import gui_input
    ax = gui_input.AX
    assert (snap.AX_ERROR_SUCCESS, snap.AX_ERROR_FAILURE, snap.AX_ERROR_ILLEGAL_ARGUMENT, snap.AX_ERROR_INVALID_ELEMENT,
            snap.AX_ERROR_CANNOT_COMPLETE, snap.AX_ERROR_ATTRIBUTE_UNSUPPORTED, snap.AX_ERROR_ACTION_UNSUPPORTED,
            snap.AX_ERROR_NOT_IMPLEMENTED, snap.AX_ERROR_API_DISABLED, snap.AX_ERROR_NO_VALUE) == (
        ax.kAXErrorSuccess, ax.kAXErrorFailure, ax.kAXErrorIllegalArgument, ax.kAXErrorInvalidUIElement,
        ax.kAXErrorCannotComplete, ax.kAXErrorAttributeUnsupported, ax.kAXErrorActionUnsupported,
        ax.kAXErrorNotImplemented, ax.kAXErrorAPIDisabled, ax.kAXErrorNoValue,
    )


# --- Eylem seçimi ve tıklama noktası (gui_input saf yardımcıları) ---

@pytest.mark.parametrize("role, actions, expected", [
    ("AXTextField", ["AXPress"], "AXFocused"),
    ("AXTextArea", [], "AXFocused"),
    ("AXButton", ["AXShowMenu", "AXPress"], "AXPress"),
    ("AXRow", ["AXShowMenu"], None),
])
def test_background_action_is_focus_for_inputs_press_when_supported_else_none(
    role: str, actions: List[str], expected: Optional[str],
) -> None:
    from omniagent.tools import gui_input
    assert gui_input._ax_action_name({**STORED, "role": role, "actions": actions}) == expected


@pytest.mark.parametrize("live_box, window_box, expected", [
    ((100, 100, 80, 20), (0, 0, 800, 600), (140.0, 110.0)),      # tamamen görünür: merkez
    ((100, 590, 80, 40), (0, 0, 800, 600), (140.0, 595.0)),      # pencere altından taşıyor: görünür kısmın merkezi
    ((100, 900, 80, 20), (0, 0, 800, 600), None),                # pencere dışında: tıklanamaz
    ((100, 100, 80, 20), None, (140.0, 110.0)),                  # pencere çerçevesi okunamadı: canlı çerçeve
])
def test_click_point_is_the_center_of_the_visible_part(
    live_box: Tuple[float, float, float, float], window_box: Optional[Tuple[float, float, float, float]],
    expected: Optional[Tuple[float, float]],
) -> None:
    from omniagent.tools import gui_input
    live_frame: Frame = {"x": live_box[0], "y": live_box[1], "w": live_box[2], "h": live_box[3]}
    window_frame: Optional[Frame] = (
        None if window_box is None else {"x": window_box[0], "y": window_box[1], "w": window_box[2], "h": window_box[3]}
    )
    assert gui_input._click_point(live_frame, window_frame) == expected


# --- Şema, sınıflandırma ve görünmez mod ---

def test_new_tools_are_routed_and_classified_correctly() -> None:
    general = {entry["function"]["name"] for entry in tool_schema.route_tool_schemas("genel", False, False)}
    chrome = {entry["function"]["name"] for entry in tool_schema.route_tool_schemas("açık Chrome oturumunu kullan", False, True)}
    new_tools = {"cua_snapshot", "cua_click_element", "cua_set_text_element"}
    assert new_tools <= general and new_tools <= chrome and new_tools <= tool_schema.TOOL_NAMES
    # eski numaralı liste araçları Chrome yolunda kapalı kalır (yenileriyle çakışır, bayat koruması yok)
    assert not chrome & {"cua_get_ax_state", "cua_click", "smart_click", "cua_get_app"}
    assert {"cua_click_element", "cua_set_text_element"} <= tool_schema._SIDE_EFFECT_TOOLS
    assert {"cua_click_element", "cua_set_text_element"} <= tool_schema._SCREEN_ACTION_TOOLS
    # cua_snapshot salt okurdur: eylem kanıtı (has_action_evidence) veya ekran eylemi sayılmamalı
    assert "cua_snapshot" not in tool_schema._SIDE_EFFECT_TOOLS | tool_schema._SCREEN_ACTION_TOOLS
    assert tool_schema.AX_TOOL_NAMES <= tool_schema.TOOL_NAMES


def test_headless_mode_hides_and_refuses_every_ax_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    from omniagent.dev import headless_screen

    monkeypatch.setattr(main, "route_tool_schemas", main.route_tool_schemas)  # test sonunda özgün işlev geri gelir
    headless_screen.hide_ax_tools()
    for chrome_session in (False, True):
        names = {entry["function"]["name"] for entry in main.route_tool_schemas("x", False, chrome_session, False, False)}
        assert not names & tool_schema.AX_TOOL_NAMES
        assert "take_screenshot" in names
    error = headless_screen.ax_unavailable("cua_snapshot")
    assert error.code == "AX_UNAVAILABLE" and error.recoverable is False
    assert "görünmez modda kapalı" in str(error)


# --- Host onay kapısı desteği: kapı rolleri, aday etiketler, tek tetikleme, etiket kaydı ---

@pytest.mark.parametrize("code, effect, step", [
    (snap.AX_ERROR_SUCCESS, "changed", "done"),
    (snap.AX_ERROR_CANNOT_COMPLETE, "changed", "done"),
    # Etki 'etkisiz'/'bilinmiyor' görünse de onaylı ödeme adımı ikinci kez tetiklenmez (çift ödeme riski)
    (snap.AX_ERROR_SUCCESS, "unchanged", "unverified"),
    (snap.AX_ERROR_SUCCESS, "unknown", "unverified"),
    (snap.AX_ERROR_CANNOT_COMPLETE, "unchanged", "unverified"),
    (snap.AX_ERROR_FAILURE, None, "unverified"),
    (snap.AX_ERROR_NOT_IMPLEMENTED, None, "unverified"),
    (snap.AX_ERROR_ATTRIBUTE_UNSUPPORTED, None, "unverified"),
    # Ön plana yalnız hiçbir şey tetiklenmediği kesinse geçilir: eylem desteklenmiyor
    (snap.AX_ERROR_ACTION_UNSUPPORTED, None, "foreground"),
    (snap.AX_ERROR_INVALID_ELEMENT, None, "stale"),
    (snap.AX_ERROR_API_DISABLED, None, "permission"),
])
def test_approved_press_outcome_fires_at_most_once(code: int, effect: Optional[str], step: str) -> None:
    assert snap.approved_press_outcome(code, effect) == step  # type: ignore[arg-type]


def test_only_press_capable_roles_are_gated_and_all_name_candidates_are_used() -> None:
    assert snap.COMMIT_ROLES == {"AXButton", "AXLink", "AXMenuItem", "AXMenuButton", "AXRow"}
    assert not snap.COMMIT_ROLES & (snap.TEXT_INPUT_ROLES | {"AXCheckBox", "AXRadioButton", "AXPopUpButton", "AXSlider"})
    element: Element = {**STORED, "label": "Buy", "description": "Complete purchase", "value": ""}
    assert snap.element_gate_labels(element) == ["Buy", "Complete purchase"]
    assert snap.element_gate_labels({**element, "label": "", "description": "", "value": ""}) == []


def _snapshot_with(snapshot_id: str, labels: List[str]) -> Snapshot:
    elements: List[Element] = [
        {**STORED, "index": index, "label": label, "description": "", "value": ""} for index, label in enumerate(labels, start=1)
    ]
    return {"id": snapshot_id, "app": "x", "pid": 1, "window_title": "t", "created_at": 0.0, "elements": elements,
            "truncated": False, "digest": "d", "stats": {"nodes_visited": 1, "pruned_subtrees": 0, "seconds": 0.0, "web_ready": None}}


def test_label_registry_resolves_ids_indexes_and_stays_bounded() -> None:
    snap.remember_snapshot_labels(_snapshot_with("s901", ["Gönder", "İptal", ""]))
    assert snap.snapshot_element_label("s901", 1) == "Gönder" and snap.snapshot_element_label("901", 2) == "İptal"
    for unknown in (("s902", 1), ("s901", 0), ("s901", 4), ("s901", "1"), ("s901", True), ("s901", None)):
        assert snap.snapshot_element_label(*unknown) == "", unknown  # type: ignore[arg-type]
    snap.remember_snapshot_labels(_snapshot_with("s901", ["Yayınla"]))  # aynı kimlik yeniden yakalanınca ezilir
    assert snap.snapshot_element_label("s901", 1) == "Yayınla" and snap.snapshot_element_label("s901", 2) == ""
    for number in range(1000, 1000 + snap.SNAPSHOT_LABEL_REGISTRY_LIMIT + 1):
        snap.remember_snapshot_labels(_snapshot_with(f"s{number}", ["a"]))
    assert snap.snapshot_element_label("s901", 1) == ""  # sınır aşılınca en eski liste düşer
    assert snap.snapshot_element_label(f"s{1000 + snap.SNAPSHOT_LABEL_REGISTRY_LIMIT}", 1) == "a"


def test_commit_guard_sees_the_label_of_a_clicked_element() -> None:
    """cua_click_element çağrısında yalnız (liste kimliği, indeks) vardır: gönderim/gezinme koruması etiketi kayıttan okur."""
    from omniagent.app import verification

    snap.remember_snapshot_labels(_snapshot_with("s950", ["Bana bas", "Gönder", "Post", "Keşfet", "Home"]))

    def click(index: object) -> main.ToolCallDraft:
        return {"id": "e", "name": "cua_click_element", "arguments": json.dumps({"snapshot": "s950", "index": index})}

    assert [verification.commit_action_call(click(index)) for index in (1, 2, 3, 4, 5)] == [False, True, True, False, False]
    assert [verification.commit_navigation_call(click(index)) for index in (1, 2, 3, 4, 5)] == [False, False, False, True, True]
    assert not verification.commit_action_call(click(9)) and not verification.commit_navigation_call(click(9))
    unknown_list: main.ToolCallDraft = {"id": "e", "name": "cua_click_element",
                                        "arguments": json.dumps({"snapshot": "s999999", "index": 1})}
    assert not verification.commit_action_call(unknown_list)
    assert "cua_set_text_element" in verification._TEXT_ENTRY_TOOLS and "cua_click_element" in verification._NO_EFFECT_GUARD_TOOLS
    assert verification.text_entry_call({"id": "t", "name": "cua_set_text_element", "arguments": "{}"})


def test_element_ocr_region_is_window_relative_and_text_inside_it_is_joined() -> None:
    from omniagent.platform.macos import screen_text as st
    from omniagent.tools import gui_input

    window: Frame = {"x": 100.0, "y": 50.0, "w": 800.0, "h": 400.0}
    button: Frame = {"x": 500.0, "y": 250.0, "w": 96.0, "h": 40.0}
    # Pencere içinde merkez (448, 220) nokta -> 0-1000 uzayında (560, 550); yarıçap (48*1,25+8, 20*2,5+8) = (68, 58)
    region = gui_input.element_region(button, window, 8, 20)
    assert region == st.region_around((560, 550), 68, 58) == {"left": 492.0, "top": 492.0, "width": 136.0, "height": 116.0}
    tiny = gui_input.element_region({"x": 300.0, "y": 100.0, "w": 4.0, "h": 4.0}, window, 0, 20)
    assert tiny["width"] >= 40 and tiny["height"] >= 40  # küçük simge için en küçük yarıçap
    lines = [
        {"text": "Ödemeyi", "confidence": 1.0, "box": {"left": 520.0, "top": 540.0, "width": 40.0, "height": 20.0}, "words": []},
        {"text": "onayla", "confidence": 1.0, "box": {"left": 570.0, "top": 540.0, "width": 40.0, "height": 20.0}, "words": []},
        {"text": "uzak metin", "confidence": 1.0, "box": {"left": 50.0, "top": 50.0, "width": 40.0, "height": 20.0}, "words": []},
    ]
    assert gui_input.text_in_region(lines, region) == "Ödemeyi onayla"
    assert gui_input.text_in_region(lines[2:], region) is None
