"""Sınır veya ilerleme yokluğuyla biten görevin dürüst, LLM'siz kısmi raporu (saf fonksiyonlar).

Rapor yalnız üç kaynaktan derlenir: host defterindeki (core/task_ledger; süzgeçten geçmiş) olgular,
modelin notunda geçip araç metninde birebir görülen kodlar ve araç hatasından okunan erişim engeli
bilgisi. Ham araç çıktısı ve modelin serbest metni yazılmaz. Metin kullanıcıya gitmeden hassas
kalıplardan (mask_sensitive_text) geçirilir. Erişim engeli varsa rapor engeli ve adresini açıkça
yazar; engeli aşmak için yeniden deneme, başka araç ya da başka yöntem önermez.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from omniagent.app.answer_fidelity import code_tokens
from omniagent.core.observation_filter import SENSITIVE_FACT_PLACEHOLDER, mask_sensitive_text
from omniagent.core.state import StepRecord, ascii_fold
from omniagent.core.task_ledger import TaskFact, TaskLedger
from omniagent.tools.bot_wall import ACCESS_CHALLENGE_MARKER

# Raporda gösterilecek en çok olgu ve erişim engeli adresi; kalanı sayı olarak belirtilir
REPORT_MAX_FACTS: int = 10
REPORT_MAX_CHALLENGES: int = 3
# Kabuk sarmalayıcı satırları ("STDERR:", "Çıkış Kodu:") bulgu değildir; "STDOUT:" ilk çıktı satırını taşır
_WRAPPER_FACT_KEYS: frozenset[str] = frozenset({"stderr", "cikis_kodu"})
UNKNOWN_CHALLENGE_URL: str = "adres belirlenemedi"

# Erişim engeli hata iletisi (tools/bot_wall.access_challenge_error): "<ÖNEK>: <adres> içerik yerine ...
# (belirteç=<etiket>, başlık=...)". Adres yalnız ŞEMALI adrestir (http://...): ilk boşluksuz sözcüğü adres saymak,
# adres taşımayan iletide ('<ÖNEK>: ekrandaki ... bir insan/bot doğrulaması', bot_wall.human_verification_error) 'ekrandaki'
# sözcüğünü adres yapıyordu; şemalı adres yoksa UNKNOWN_CHALLENGE_URL kullanılır. Testler bu kalıbı gerçek iletiyle sabitler.
_CHALLENGE_URL: re.Pattern[str] = re.compile(re.escape(ACCESS_CHALLENGE_MARKER) + r":\s*(?P<url>\w+://\S*)?")
_CHALLENGE_SIGNAL: re.Pattern[str] = re.compile(r"belirteç=(?P<signal>[^,\s)]+)")
# Dürüst durma yanıtının erişim engelini andığını gösteren ifadeler (ascii_fold sonrası metinde aranır). SÖZCÜK SINIRLI ve
# engele özgüdür: 'dogrulama' ve 'engel' alt dizgisi olarak eşleşmez ('Dosya kaydedildi, doğrulama tamamlandı.', 'Kayıt
# engelsiz tamamlandı.' engel sonrası dürüst durma sayılıp kanıt kapılarını atlatıyordu). 'engelleme sayfa', aracın kendi
# iletisindeki 'doğrulama veya engelleme sayfası' ifadesidir (model çoğu kez onu yineler); 'engelledi/engellendi' engelleme
# fiilleridir ('engeller'/'engelli' isim ve sıfatları eşleşmez); olumsuzlar ('engelsiz', 'engellenmeden', 'engellemedi')
# dışarıda kalır. 'verification' ve 'challenge' sözcük sınırlı tek başına eşleşir: İngilizce 'Verification passed' gibi bir
# tamamlama cümlesi bilinen yanlış pozitiftir (engel hatası VE sonrasında başarılı adım yokken; bkz. stopped_at_access_wall).
_WALL_ACKNOWLEDGEMENT: re.Pattern[str] = re.compile(
    r"\b(?:bot|robot|captcha|cloudflare|blocked|challenge|verification)\b"
    r"|\binsan dogrulama|\berisim engel(?!siz)|\bengelle(?:di\b|n(?!meden|meksizin))|\bengelleme sayfa"
)


@dataclass(frozen=True)
class AccessChallengeNote:
    """Bir araç hatasının bildirdiği erişim engeli: engelin adresi ve sınıflandırıcı etiketi (yoksa boş)."""

    url: str
    signal: str


def access_challenge_notes(steps: Sequence[StepRecord]) -> Tuple[AccessChallengeNote, ...]:
    """Başarısız adımların erişim engeli iletilerinden adres ve etiketi okur; aynı adres bir kez döner. Saf."""
    notes: Dict[str, AccessChallengeNote] = {}
    for step in steps:
        if step["ok"]:
            continue
        located: Optional[re.Match[str]] = _CHALLENGE_URL.search(step["detail"])
        if located is None:
            continue
        url: str = located.group("url") or UNKNOWN_CHALLENGE_URL
        labelled: Optional[re.Match[str]] = _CHALLENGE_SIGNAL.search(step["detail"], located.end())
        notes.setdefault(url, AccessChallengeNote(url, labelled.group("signal") if labelled else ""))
    return tuple(notes.values())


def stopped_at_access_wall(answer: str, steps: Sequence[StepRecord]) -> bool:
    """
    Koşu bir erişim engelinde bitti ve yanıt bunu açıkça belirtiyor mu? Son engel hatasından sonra başarılı
    adım yoksa ve yanıt engeli anıyorsa bu dürüst durmadır: kanıt kapıları ona yeniden deneme turu
    dayatmaz ve yanıtı değiştirmez. Engeli anmayan yanıt (ör. "Dosya silindi.") normal kapılardan geçer. Saf.
    """
    walls: List[int] = [
        index for index, step in enumerate(steps) if not step["ok"] and ACCESS_CHALLENGE_MARKER in step["detail"]
    ]
    return (
        bool(walls)
        and not any(step["ok"] for step in steps[walls[-1] + 1:])
        and _WALL_ACKNOWLEDGEMENT.search(ascii_fold(answer)) is not None
    )


def report_facts(ledger: TaskLedger) -> Tuple[TaskFact, ...]:
    """
    Bulgu sayılabilecek host olgularını en yeni turdan eskiye sıralar. Kabuk sarmalayıcı satırları ve
    yalnız 'değer defterde tutulmaz' yer tutucusu taşıyan hassas adlı olgular dışarıda kalır. Saf.
    """
    facts: List[TaskFact] = [
        fact for key, fact in ledger["facts"].items()
        if key not in _WRAPPER_FACT_KEYS and fact["value"] != SENSITIVE_FACT_PLACEHOLDER
    ]
    return tuple(sorted(facts, key=lambda fact: (-fact["turn"], fact["key"])))


def verified_note_codes(model_state: str, observed: frozenset[str]) -> Tuple[str, ...]:
    """Modelin notunda geçen kod-benzeri değerlerden yalnız araç çıktısında birebir görülenleri döner. Saf."""
    return tuple(token for token in code_tokens(model_state) if token in observed)


def _headline_lines(reason: str, challenges: Tuple[AccessChallengeNote, ...]) -> List[str]:
    """Rapor başlığı; erişim engelinde 'tamamlanamadı' yerine engel ve adresi açıkça yazılır. Saf."""
    if not challenges:
        return [f"Görev tamamlanamadı ({reason})." if reason else "Görev tamamlanamadı."]
    lines: List[str] = [
        f"Erişim engeli: {note.url} içerik yerine doğrulama veya engelleme sayfası döndürdü"
        + (f" (belirteç={note.signal})." if note.signal else ".")
        for note in challenges[:REPORT_MAX_CHALLENGES]
    ]
    if len(challenges) > REPORT_MAX_CHALLENGES:
        lines.append(f"… {len(challenges) - REPORT_MAX_CHALLENGES} adres daha erişim engeli döndürdü.")
    # Host modelin engel sonrası ne yaptığını bilmez; yalnız ajanın politikasını söyler
    lines.append("Ajan erişim engellerini aşmaya çalışmaz.")
    if reason:
        lines.append(f"Çalışma sona erdi ({reason}).")
    return lines


def _finding_lines(facts: Tuple[TaskFact, ...], note_codes: Tuple[str, ...]) -> List[str]:
    """Olgu ve doğrulanmış kod satırları; hiçbiri yoksa bunu açıkça söyler. Saf."""
    lines: List[str] = []
    if facts:
        lines.append("Araç çıktılarından alınan bulgular:")
        lines.extend(
            f"- {fact['key']}: {fact['value']} (tur {fact['turn']}, {fact['source']})"
            for fact in facts[:REPORT_MAX_FACTS]
        )
        if len(facts) > REPORT_MAX_FACTS:
            lines.append(f"- … {len(facts) - REPORT_MAX_FACTS} bulgu daha")
    if note_codes:
        lines.append(
            "Model notunda geçen ve araç çıktısında birebir görülen değerler: " + ", ".join(note_codes)
        )
    if not facts and not note_codes:
        lines.append("Araç çıktılarından alınmış bulgu yok.")
    return lines


def format_partial_report(
    reason: str, facts: Tuple[TaskFact, ...], note_codes: Tuple[str, ...],
    challenges: Tuple[AccessChallengeNote, ...],
) -> str:
    """
    Tamamlanamayan görevin kullanıcıya gösterilecek raporu: sonlanma nedeni, araç çıktılarından alınan
    bulgular, araç çıktısıyla birebir doğrulanan kodlar ve varsa erişim engeli. Modelin serbest metni
    girmez; metnin tamamı hassas kalıplardan geçirilir. Saf.
    """
    lines: List[str] = [
        *_headline_lines(reason, challenges),
        *_finding_lines(facts, note_codes),
        "Bulgular araç çıktılarından otomatik alındı; eksik ve doğrulanmamış olabilir, istenen işin sonucu değildir.",
    ]
    return mask_sensitive_text("\n".join(lines))
