"""Nihai yanıttaki kod-benzeri belirteçleri hedef ve araç çıktılarıyla birebir doğrular.

Kapı hassasiyet önceliklidir: yalnız gözlenmiş TEK bir belirteçle "karışabilir karakter" farkı olan
(O-0, I/l-1, S-5, B-8, Z-2, Kiril-Latin) belirteç düzeltilir. Hesaplanmış sayı, sözcük ve yalnız
görselden okunmuş değer gözlenmiş belirteç olmadığı için dokunulmadan geçer; yalnız ölçülür.
Modül saf fonksiyonlardan oluşur: ağ, dosya ve global durum kullanmaz, model turu istemez.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

from omniagent.core.state import StepRecord
from omniagent.tools.bot_wall import ACCESS_CHALLENGE_MARKER

# Kod-benzeri belirteç: harf ve rakam birlikte, 5-64 karakter; tire ve altçizgi içerebilir. Yalnız rakam
# (sayı, tarih) ve yalnız harf (sözcük, KOD-ABCD) kapsam dışıdır: sözcük içi OCR rakamı ("Kar1yer-Merkezi")
# modelin doğru düzeltmesini ("Kariyer-Merkezi") geri almasın.
MIN_TOKEN_LENGTH: int = 5
MAX_TOKEN_LENGTH: int = 64

_WORD_RUN: re.Pattern[str] = re.compile(r"[\w-]+")
_TOKEN_EDGE: str = "-_"

# Küçük harfe indirildikten sonra Latin karşılığından görsel olarak ayırt edilemeyen Kiril harfleri
_CYRILLIC_TO_LATIN: Dict[int, str] = str.maketrans({
    "а": "a", "в": "b", "е": "e", "к": "k", "м": "m", "н": "h", "о": "o", "р": "p",
    "с": "c", "т": "t", "у": "y", "х": "x", "і": "i", "ј": "j", "ѕ": "s",
})
# Küçük harfe indirildikten sonra harf-rakam karışıklığı: O-0, I/l/ı-1, S-5, B-8, Z-2
_LETTER_TO_DIGIT: Dict[int, str] = str.maketrans({
    "o": "0", "i": "1", "l": "1", "ı": "1", "s": "5", "b": "8", "z": "2",
})


@dataclass(frozen=True)
class TokenFinding:
    """Yanıtta birebir doğrulanamayan belirteç ve karışabilir yazımlı gözlenmiş adayları."""

    token: str
    observed: Tuple[str, ...]


@dataclass(frozen=True)
class FidelityReport:
    """
    corrections: tek gözlenmiş aday (düzeltilir); ambiguous: birden çok aday (yalnız uyarı);
    unobserved: aday yok (yalnız ölçülür; hesaplanmış veya görselden okunmuş olabilir).
    """

    corrections: Tuple[TokenFinding, ...]
    ambiguous: Tuple[TokenFinding, ...]
    unobserved: Tuple[str, ...]


def confusable_key(token: str) -> str:
    """Görsel olarak karışan karakterleri tek biçime indirger: 'IL-0C1EAF' ile 'IL-OC1EAF' aynı anahtarı verir. Saf."""
    decomposed: str = unicodedata.normalize("NFKD", token)
    stripped: str = "".join(char for char in decomposed if not unicodedata.combining(char))
    return stripped.casefold().translate(_CYRILLIC_TO_LATIN).translate(_LETTER_TO_DIGIT)


def _token_core(run: str) -> str:
    """Sözcük dizisinin baştaki ve sondaki tire/altçizgilerden arınmış çekirdeği. Saf."""
    return run.strip(_TOKEN_EDGE)


def _is_code_like(core: str) -> bool:
    """5-64 karakterlik ve hem harf hem rakam içeren çekirdek mi? Saf."""
    return (
        MIN_TOKEN_LENGTH <= len(core) <= MAX_TOKEN_LENGTH
        and any(char.isdigit() for char in core)
        and any(char.isalpha() for char in core)
    )


def code_tokens(text: str) -> Tuple[str, ...]:
    """Metindeki kod-benzeri belirteçleri ilk görülme sırasıyla, tekrarsız döner. Saf."""
    found: Dict[str, None] = {}
    for run in _WORD_RUN.findall(text):
        core: str = _token_core(run)
        if _is_code_like(core):
            found[core] = None
    return tuple(found)


def code_token_set(texts: Sequence[str]) -> frozenset[str]:
    """Metinlerdeki kod-benzeri belirteçlerin kümesi. Saf."""
    return frozenset(token for text in texts for token in code_tokens(text))


def observed_step_texts(steps: Sequence[StepRecord]) -> Tuple[str, ...]:
    """
    Kod doğrulamasının araç korpusu: başarılı adımların metni ile erişim engeli hatasının metni
    (adres ve sayfa başlığı). Başarısız adımın metni genel olarak dışarıda kalır: hata iletisi
    modelin yanlış yazdığı değeri yansıtabilir ve yanlış değer 'gözlenmiş' sayılırdı. Erişim engeli
    iletisi host'un kendi ürettiği metindir; dürüstçe 'engel nedeniyle durdum' diyen yanıt, adresini
    birebir yazdığında başka bir gözlenmiş belirteçle karıştırılıp bozulmasın diye korpusa girer. Saf.
    """
    return tuple(
        step["detail"] for step in steps if step["ok"] or ACCESS_CHALLENGE_MARKER in step["detail"]
    )


def _distinct_by_case(candidates: Sequence[str]) -> Tuple[str, ...]:
    """Yalnız büyük/küçük harfle ayrışan gözlenmiş yazımları tek aday sayar; sıralamada ilk yazım kalır. Saf."""
    distinct: Dict[str, str] = {}
    for candidate in sorted(candidates):
        distinct.setdefault(candidate.casefold(), candidate)
    return tuple(distinct.values())


def check_answer_fidelity(answer: str, verified: frozenset[str]) -> FidelityReport:
    """
    Yanıttaki kod-benzeri belirteçlerden hedefte/araç çıktısında birebir (büyük-küçük harf farkı
    dışında) bulunmayanları sınıflar. Karışabilir karakter anahtarı aynı olan gözlenmiş belirteç
    aday sayılır; anahtar dizini yalnız doğrulanamayan belirteç varsa kurulur. Saf.
    """
    candidates: Tuple[str, ...] = tuple(token for token in code_tokens(answer) if token not in verified)
    if not candidates:
        return FidelityReport((), (), ())
    verified_folded: frozenset[str] = frozenset(token.casefold() for token in verified)
    unverified: Tuple[str, ...] = tuple(
        token for token in candidates if token.casefold() not in verified_folded
    )
    if not unverified:
        return FidelityReport((), (), ())
    by_key: Dict[str, List[str]] = {}
    for observed in verified:
        by_key.setdefault(confusable_key(observed), []).append(observed)
    findings: Tuple[TokenFinding, ...] = tuple(
        TokenFinding(token, _distinct_by_case(by_key.get(confusable_key(token), ()))) for token in unverified
    )
    return FidelityReport(
        corrections=tuple(finding for finding in findings if len(finding.observed) == 1),
        ambiguous=tuple(finding for finding in findings if len(finding.observed) > 1),
        unobserved=tuple(finding.token for finding in findings if not finding.observed),
    )


def apply_token_corrections(answer: str, corrections: Tuple[TokenFinding, ...]) -> str:
    """Yanıttaki belirteçleri gözlenmiş yazımlarıyla değiştirir; belirteç sınırları ve çevre metin korunur. Saf."""
    replacements: Dict[str, str] = {finding.token: finding.observed[0] for finding in corrections}

    def replace_run(match: re.Match[str]) -> str:
        run: str = match.group()
        core: str = _token_core(run)
        if core not in replacements:
            return run
        leading: int = len(run) - len(run.lstrip(_TOKEN_EDGE))
        return run[:leading] + replacements[core] + run[leading + len(core):]

    return _WORD_RUN.sub(replace_run, answer)


def correction_notice(corrections: Tuple[TokenFinding, ...]) -> str:
    """Kullanıcıya gösterilen düzeltme bildirimi (eski → yeni). Saf."""
    pairs: str = "; ".join(f"{finding.token} → {finding.observed[0]}" for finding in corrections)
    return f"Yanıttaki kod araç çıktısıyla birebir eşleşmediği için gözlenen değerle düzeltildi: {pairs}."


def unverified_notice(ambiguous: Tuple[TokenFinding, ...]) -> str:
    """Birden çok gözlenmiş adaya benzeyen, düzeltilmeyen belirteçler için uyarı. Saf."""
    details: str = "; ".join(
        f"{finding.token} (gözlenen: {' | '.join(finding.observed)})" for finding in ambiguous
    )
    return f"Yanıttaki kod araç çıktısında birebir yok ve birden çok gözlenen değere benziyor; doğrulanamadı: {details}."
