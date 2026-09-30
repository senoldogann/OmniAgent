"""Model metnini iMessage balonlarına çeviren saf yardımcılar."""
from __future__ import annotations

import re
from typing import List, Tuple

MAX_BUBBLES: int = 4
MAX_BUBBLE_CHARS: int = 600
_MARKDOWN_PREFIX: re.Pattern[str] = re.compile(r"^(?:[-*•]\s+|#{1,6}\s+|\d+[.)]\s+|>\s*)")


def final_message(text: str) -> str:
    """Normal chat/report finalini lossless tutar; yalnız dış boşluğu ve CRLF farkını normalize eder."""
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def clean_line(line: str) -> str:
    """Proaktif kısa balondan markdown işaretlerini atar ve mevcut heartbeat sınırında keser. Saf."""
    text: str = _MARKDOWN_PREFIX.sub("", line.strip())
    text = text.replace("**", "").replace("__", "").replace("`", "")
    return text.strip()[:MAX_BUBBLE_CHARS]


def split_complete_lines(buffer: str, delta: str) -> Tuple[List[str], str]:
    """Akış parçasını tampona ekler; tamamlanmış satırları ve kalan yarım satırı döndürür. Saf."""
    parts: List[str] = (buffer + delta).split("\n")
    return parts[:-1], parts[-1]


def bubbles_from_lines(lines: List[str]) -> List[str]:
    """Boş olmayan temiz satırlar balondur; MAX_BUBBLES'ı aşanlar son balonda birleşir. Saf."""
    kept: List[str] = [cleaned for cleaned in (clean_line(line) for line in lines) if cleaned]
    if len(kept) <= MAX_BUBBLES:
        return kept
    return kept[:MAX_BUBBLES - 1] + [" ".join(kept[MAX_BUBBLES - 1:])[:MAX_BUBBLE_CHARS]]


def split_bubbles(text: str) -> List[str]:
    """Tam metni balonlara böler. Saf."""
    return bubbles_from_lines(text.split("\n"))


def bubble_delay(text: str) -> float:
    """Balonlar arası insan benzeri bekleme: min(1.5, 0.4 + 0.02 × karakter) sn. Saf."""
    return min(1.5, 0.4 + 0.02 * len(text))
