"""GUI gözlem tekrar kullanımı ve bitiş doğrulama kanıtları."""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from omniagent.app.tool_schema import _GUI_VERIFICATION_TOOLS, _SCREEN_ACTION_TOOLS
from omniagent.app.types import ToolCallDraft, ToolResult
from omniagent.core import state as sm


FULL_DETAIL_TURNS: int = 2


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
        if call["name"] in _SCREEN_ACTION_TOOLS and result.get("ok")
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
        step["ok"] and step["tool"] in _GUI_VERIFICATION_TOOLS
        for step in steps
    )


GUI_VERIFICATION_PROMPT: str = (
    "HOST — BİTİRMEDEN ÖNCE DOĞRULA: Güncel ekran ekte. Hedefi zorunlu maddelerine ayır ve her maddeyi "
    "bir kanıtla eşleştir. Eylem: istenen her alan dolu, seçim yapılmış, onay kutusu işaretli mi; "
    "gönder/kaydet sonrası onay mesajı ya da yeni durum ekranda görünüyor mu? Kapsam: hedef 'tüm, "
    "hepsi, her, en yüksek/düşük' gibi bir tarama istiyorsa listenin/sayfanın sonuna gerçekten "
    "ulaşıldı mı (cua_scroll 'KAYMADI' ya da cua_read_scrollable 'sona ulaşıldı')? Aşağıdaki kanıt "
    "özeti ve STATE bir maddeyi zaten kanıtlıyorsa onu yeniden tarama; yalnız kanıtı eksik maddeyi "
    "araçlarla tamamla. Hepsi kanıtlıysa final yanıtı yeniden yaz; doğrulayamadığın maddeyi açıkça "
    "'doğrulanmadı' diye belirt."
)


def _step_arguments(step: sm.StepRecord) -> Dict[str, Any]:
    """Kırpılmış olabilecek step argümanlarını güvenle çözümler."""
    try:
        value: object = json.loads(step["args"] or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


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
            reads_to_end += int("sona ulaşıldı" in step["detail"])
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
        parts.append(f"kaydırma {scrolls} ({scroll_ends} tanesi KAYMADI)")
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
