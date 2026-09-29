"""GUI gözlem tekrar kullanımı ve bitiş doğrulama kanıtları."""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

# Tek kaynak: app/constants.py. Burada yalnız geriye dönük uyum için yeniden dışa aktarılır;
# iki ayrı tanım zamanla sessizce ayrışıyordu (bkz. O7).
from omniagent.app.constants import FULL_DETAIL_TURNS
from omniagent.app.tool_schema import _GUI_VERIFICATION_TOOLS, _SCREEN_ACTION_TOOLS
from omniagent.app.types import ToolCallDraft, ToolResult
from omniagent.core import state as sm
from omniagent.tools.ax_snapshot import snapshot_element_label


# Yayınlama/gönderme hedefleri. Böyle bir kontrole başarıyla tıklandıktan sonra sayfadan
# ayrılmak taslağı düşürür: canlı kayıtta ajan X'te gönderiyi yayınlamadan Keşfet'e geçti ve
# ileti hiç gönderilmedi. `\byanıtla\b` "Yanıtlar" sayacına eşleşmez.
_COMMIT_TARGET_PATTERN: re.Pattern[str] = re.compile(
    r"\b(?:gönder|gönderi|yayınla|paylaş|tweetle|yanıtla|cevapla)\b"
    r"|\b(?:send|post|publish|submit|share|tweet|reply)\b",
    re.IGNORECASE,
)
# Sayfadan ayrılan gezinme etiketleri (sol menü/sekme).
_NAVIGATION_TARGET_PATTERN: re.Pattern[str] = re.compile(
    r"^\s*(?:keşfet|explore|anasayfa|home|bildirimler|notifications|mesajlar|messages|"
    r"profil|profile|daha fazla|more)\b",
    re.IGNORECASE,
)
# Metin girişi yapan araçlar: gönderim koruması yalnız yazılmış bir taslak varsa kurulur,
# böylece çıplak bir besteleyici açmak korumayı tetiklemez.
_TEXT_ENTRY_TOOLS: frozenset[str] = frozenset({
    "cua_fill_field", "cua_type_text", "cua_submit_text", "cua_set_text_element",
})
# Ekran aynı kaldığında aynı state'te yeniden yürütülmesi güvenli biçimde engellenebilen
# doğrudan GUI girdileri. cua_press_key tekrarlı gezinme için meşru olabilir;
# run_action_sequence/read_scrollable ise aynı ekrana dönse bile bilgi üretmiş olabilir.
_NO_EFFECT_GUARD_TOOLS: frozenset[str] = frozenset({
    "cua_click_point", "cua_type_text", "cua_submit_text", "cua_fill_field",
    "cua_click_text", "cua_scroll", "cua_click", "smart_click", "cua_click_element",
})

COMMIT_UNVERIFIED_MESSAGE: str = (
    "HOST: Son gönderim/tamamlama henüz doğrulanmadı; şimdi sayfadan ayrılırsan yazdığın taslak "
    "kaybolur ve ileti hiç gitmez. Önce güncel ekranı incele: ileti kutusu kapandı mı, 'gönderildi' "
    "onayı göründü mü, aranan sonuç geldi mi? Gönderim gerçekleşmediyse aynı düğmeye körlemesine "
    "yeniden basma; görünür hata/uyarıyı veya eksik alanı bulup düzelt, sonra gönderimi yeniden dene."
)

REPEATED_NO_EFFECT_ACTION_MESSAGE: str = (
    "HOST: Bu GUI eylemi aynı ekran durumunda daha önce başarıyla gönderildi ancak ekranda hiçbir "
    "etki yaratmadı. Aynı eylem bu state değişmeden yeniden yürütülmeyecek. Güncel ekrandaki "
    "hata/uyarıyı oku, formu veya hedefi değiştir ya da farklı bir yöntem kullan."
)


def _decoded_arguments(raw: object) -> Dict[str, Any]:
    """JSON argüman metnini güvenle sözlüğe çözümler. Saf."""
    try:
        value: object = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _call_arguments(call: ToolCallDraft) -> Dict[str, Any]:
    """Araç çağrısı argümanlarını güvenle çözümler. Saf."""
    return _decoded_arguments(call["arguments"])


def no_effect_action_key(call: ToolCallDraft) -> Optional[str]:
    """Aynı görsel state'te tekrar yürütülmemesi gereken GUI girdisinin kararlı anahtarı."""
    if call["name"] not in _NO_EFFECT_GUARD_TOOLS:
        return None
    arguments = json.dumps(
        _call_arguments(call), ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return f"{call['name']}:{arguments}"


def _sequence_steps(call: ToolCallDraft) -> List[Dict[str, Any]]:
    """Eylem dizisindeki nesne adımlarını güvenle ayıklar. Saf."""
    if call["name"] != "run_action_sequence":
        return []
    raw = _call_arguments(call).get("steps")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    return [step for step in raw if isinstance(step, dict)] if isinstance(raw, list) else []


def executed_call_prefix(call: ToolCallDraft, result: ToolResult) -> Optional[ToolCallDraft]:
    """Başarılı çağrıyı veya hatadan önce tamamlanan dizi önekini döndürür. Saf."""
    if result.get("ok"):
        return call
    count = int(result.get("completed_steps") or 0)
    if call["name"] != "run_action_sequence" or count <= 0:
        return None
    arguments = _call_arguments(call)
    raw_steps: object = arguments.get("steps")
    if isinstance(raw_steps, str):
        try:
            raw_steps = json.loads(raw_steps)
        except json.JSONDecodeError:
            return None
    if not isinstance(raw_steps, list):
        return None
    return {
        **call,
        "arguments": json.dumps(
            {**arguments, "steps": raw_steps[:count]}, ensure_ascii=False,
        ),
    }


def text_entry_call(call: ToolCallDraft) -> bool:
    """Çağrı bir alana metin yazıyor mu (gönderim korumasını kurar). Saf."""
    if call["name"] in _TEXT_ENTRY_TOOLS:
        return True
    return any(
        step.get("action") == "type" and bool(str(step.get("text") or ""))
        for step in _sequence_steps(call)
    )


def _element_click_label(call: ToolCallDraft) -> str:
    """
    cua_click_element çağrısının hedef öğe etiketi: çağrıda yalnız (liste kimliği, indeks) vardır, etiketi
    host'un anlık görüntü kaydı verir (ax_snapshot.snapshot_element_label). Başka araç ya da kayıtta olmayan liste
    için boş metin. Kayıt salt okunur bir süreç geneli tablodur; bu işlev onu değiştirmez.
    """
    if call["name"] != "cua_click_element":
        return ""
    arguments: Dict[str, Any] = _call_arguments(call)
    return snapshot_element_label(str(arguments.get("snapshot", "")), arguments.get("index"))


def commit_action_call(call: ToolCallDraft) -> bool:
    """
    Çağrı bir gönderme/yayınlama mı: Enter'a basan tek alanlı gönderim ya da metni gönderim
    sözcüğü içeren bir hedefe tıklama (cua_click_element için hedefin etiketi anlık görüntü kaydından
    okunur). Çalıştırmadan önce bakılır. Saf.
    """
    if call["name"] == "cua_submit_text":
        return True
    if call["name"] == "cua_click_element":
        return bool(_COMMIT_TARGET_PATTERN.search(_element_click_label(call)))
    if call["name"] == "run_action_sequence":
        return any(
            step.get("action") == "click_text"
            and bool(_COMMIT_TARGET_PATTERN.search(str(step.get("text") or "")))
            for step in _sequence_steps(call)
        )
    if call["name"] != "cua_click_text":
        return False
    return bool(_COMMIT_TARGET_PATTERN.search(str(_call_arguments(call).get("text", ""))))


def commit_navigation_call(call: ToolCallDraft) -> bool:
    """
    Gönderim beklerken sayfadan ayrılan çağrı mı: URL'li chrome_active_tab veya gezinme
    etiketine tıklama. URL'siz chrome_active_tab yalnız okur, engellenmez. Saf.
    """
    if call["name"] == "chrome_active_tab":
        return bool(str(_call_arguments(call).get("url") or "").strip())
    if call["name"] == "cua_click_text":
        return bool(_NAVIGATION_TARGET_PATTERN.search(str(_call_arguments(call).get("text", ""))))
    if call["name"] == "cua_click_element":
        return bool(_NAVIGATION_TARGET_PATTERN.search(_element_click_label(call)))
    return any(
        step.get("action") == "click_text"
        and bool(_NAVIGATION_TARGET_PATTERN.search(str(step.get("text") or "")))
        for step in _sequence_steps(call)
    )


def commit_then_navigation_in_sequence(call: ToolCallDraft) -> bool:
    """Tek dizide gönderimden sonra sayfadan ayrılma adımı var mı? Saf."""
    submitted = False
    for step in _sequence_steps(call):
        if step.get("action") != "click_text":
            continue
        label = str(step.get("text") or "")
        if submitted and _NAVIGATION_TARGET_PATTERN.search(label):
            return True
        submitted = submitted or bool(_COMMIT_TARGET_PATTERN.search(label))
    return False


def should_reuse_observation(
    digest: Optional[str],
    last_digest: Optional[str],
    *,
    current_turn: int,
    last_injected_turn: Optional[int],
) -> bool:
    """Aynı görsel full-detail context penceresindeyse image payload'ını yeniden gönderme."""
    if not digest or digest != last_digest or last_injected_turn is None:
        return False
    age = current_turn - last_injected_turn
    return 0 < age < FULL_DETAIL_TURNS


def needs_action_observation(
    calls: List[ToolCallDraft],
    results: List[ToolResult],
) -> bool:
    """Son başarılı ekran eyleminden sonra başarılı screenshot yoksa gözlem ister."""
    pairs: List[Tuple[ToolCallDraft, ToolResult]] = list(zip(calls, results, strict=True))
    acted = [
        index
        for index, (call, result) in enumerate(pairs)
        if call["name"] in _SCREEN_ACTION_TOOLS
        and (result.get("ok") or int(result.get("completed_steps") or 0) > 0)
    ]
    if not acted:
        return False
    return not any(
        call["name"] == "take_screenshot" and result.get("ok")
        for call, result in pairs[acted[-1] + 1:]
    )


def unchanged_screen_note(
    digest: Optional[str],
    previous_digest: Optional[str],
) -> Optional[str]:
    """Eylem sonrası ekran piksel olarak değişmediyse modele açık host uyarısı üretir."""
    if digest is None or digest != previous_digest:
        return None
    return (
        "HOST: Son eylemden sonra ekran HİÇ DEĞİŞMEDİ (önceki gözlemle piksel piksel aynı): eylem "
        "hedefi ıskaladı ya da etkisiz kaldı (kaydırmada: içerik sonu). Aynı eylemi tekrarlama; "
        "metinli hedefte cua_click_text kullan, değilse farklı bir hedef seç."
    )


def with_observation_note(
    observation: Dict[str, Any],
    note: Optional[str],
) -> Dict[str, Any]:
    """Host notunu görüntü parçasını bozmadan gözlem mesajının sonuna ekler."""
    if note is None:
        return observation
    content: Any = observation.get("content")
    if isinstance(content, list):
        return {**observation, "content": content + [{"type": "text", "text": note}]}
    return {**observation, "content": f"{content}\n{note}"}


def gui_verification_needed(steps: List[sm.StepRecord]) -> bool:
    """Görevde başarılı gerçek ekran eylemi varsa final görsel doğrulaması ister."""
    return any(
        step["tool"] in _GUI_VERIFICATION_TOOLS
        and (step["ok"] or step.get("partial_steps", 0) > 0)
        for step in steps
    )


GUI_VERIFICATION_PROMPT: str = (
    "HOST — BİTİRMEDEN ÖNCE DOĞRULA: Güncel ekran ekte. Hedefi zorunlu maddelerine ayır ve her maddeyi "
    "bir kanıtla eşleştir. Eylem: istenen her alan dolu, seçim yapılmış, onay kutusu işaretli mi; "
    "gönder/kaydet sonrası onay mesajı ya da yeni durum ekranda görünüyor mu? Kapsam: hedef 'tüm, "
    "hepsi, her, en yüksek/düşük' gibi bir tarama istiyorsa listenin/sayfanın sonuna gerçekten "
    "ulaşıldı mı (cua_read_scrollable 'sona ulaşıldı', kısaltma uyarısı olmadan)? Tek bir "
    "cua_scroll 'KAYMADI' yanlış panel anlamına da gelebilir. Aşağıdaki kanıt "
    "özeti ve STATE bir maddeyi zaten kanıtlıyorsa onu yeniden tarama; yalnız kanıtı eksik maddeyi "
    "araçlarla tamamla. Hepsi kanıtlıysa final yanıtı yeniden yaz; doğrulayamadığın maddeyi açıkça "
    "'doğrulanmadı' diye belirt."
)


def _step_arguments(step: sm.StepRecord) -> Dict[str, Any]:
    """Kırpılmış olabilecek step argümanlarını güvenle çözümler."""
    return _decoded_arguments(step["args"])


def requested_chrome_navigation_gap(goal: str, steps: List[sm.StepRecord]) -> Optional[str]:
    """Açıkça istenen yeni sekme ve LinkedIn akışı için araç kanıtını denetler."""
    lowered = goal.casefold()
    chrome_goal = bool(re.search(r"\b(?:chrome|google chrome)\b", lowered))
    if not chrome_goal:
        return None
    wants_new_tab = bool(re.search(r"\b(?:yeni\s+(?:bir\s+)?(?:chrome\s+)?(?:tab|sekme)|new\s+tab)\b", lowered))
    wants_feed = "linkedin" in lowered and bool(re.search(r"(?:anasayfa\s+akış|\bfeed\b)", lowered))
    navigations = [
        _step_arguments(step)
        for step in steps
        if step["ok"] and step["tool"] == "chrome_active_tab"
    ]
    if wants_new_tab and not any(args.get("new_tab") is True for args in navigations):
        return "yeni Chrome sekmesi istendi fakat yeni sekme açıldığına dair araç kanıtı yok"
    if wants_feed and not any(
        (urlsplit(str(args.get("url") or "")).hostname or "").casefold() in {"linkedin.com", "www.linkedin.com"}
        and urlsplit(str(args.get("url") or "")).path.rstrip("/") == "/feed"
        and (not wants_new_tab or args.get("new_tab") is True)
        for args in navigations
    ):
        return "LinkedIn ana akışı istendi fakat yeni sekmede /feed/ adresine gidildiğine dair araç kanıtı yok" if wants_new_tab else "LinkedIn ana akışı istendi fakat /feed/ adresine gidildiğine dair araç kanıtı yok"
    return None


def gui_evidence_summary(steps: List[sm.StepRecord]) -> str:
    """Host step kayıtlarından final verification için kısa kanıt özeti çıkarır."""
    clicked: List[str] = []
    inexact = 0
    reads = 0
    reads_to_end = 0
    scrolls = 0
    scroll_ends = 0
    filled: List[str] = []
    point_clicks = 0
    for step in steps:
        if not step["ok"]:
            continue
        arguments = _step_arguments(step)
        if step["tool"] == "cua_click_text":
            clicked.append(str(arguments.get("text", "")))
            inexact += int("DİKKAT" in step["detail"])
        elif step["tool"] == "cua_read_scrollable":
            reads += 1
            reads_to_end += int(
                "sona ulaşıldı" in step["detail"] and "kısaltıldı" not in step["detail"]
            )
        elif step["tool"] == "cua_scroll":
            scrolls += 1
            scroll_ends += int("KAYMADI" in step["detail"])
        elif step["tool"] in ("cua_fill_field", "cua_submit_text"):
            filled.append(str(arguments.get("text", ""))[:40])
        elif step["tool"] == "cua_click_point":
            point_clicks += 1

    parts: List[str] = []
    if clicked:
        unique = list(dict.fromkeys(clicked))
        parts.append(
            f"metne tıklama {len(clicked)} ({len(unique)} farklı: {', '.join(unique)[:600]})"
            + (f", {inexact} tanesi birebir eşleşmedi" if inexact else "")
        )
    if reads:
        parts.append(f"baştan sona okuma {reads} ({reads_to_end} tanesi sona ulaştı)")
    if scrolls:
        parts.append(f"kaydırma {scrolls} ({scroll_ends} tanesi KAYMADI; tek başına son kanıtı değil)")
    if filled:
        parts.append(f"doldurulan alanlar: {', '.join(filled)}")
    if point_clicks:
        parts.append(f"noktaya tıklama {point_clicks}")
    return "; ".join(parts) or "ekran eylemi kaydı yok"


def verification_message(
    observation: Dict[str, Any],
    evidence: str,
) -> Dict[str, Any]:
    """Final doğrulama promptunu host kanıtı ve varsa güncel görüntüyle birleştirir."""
    content: Any = observation.get("content")
    images: List[Dict[str, Any]] = (
        [
            part
            for part in content
            if isinstance(part, dict) and part.get("type") == "image_url"
        ]
        if isinstance(content, list)
        else []
    )
    failure = "" if images else f"\n({content})"
    text = f"{GUI_VERIFICATION_PROMPT}\nHOST KANIT ÖZETİ: {evidence}{failure}"
    return {
        "role": "user",
        "content": [{"type": "text", "text": text}] + images,
    }
