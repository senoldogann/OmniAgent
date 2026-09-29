"""Çekirdek modüllerin ve araçların paylaştığı hafif metin yardımcıları (ağır içe aktarma yok).

`ascii_fold` state.py'de yaşarken observation_filter'ın state'i, state'in de observation_filter'ı içe aktarması
gerekince döngü oluştu; ortak metin işlevleri bu modüle taşındı. state.ascii_fold adı geriye uyumluluk için korunur.
`curl_http_statuses` ise tools/browser.py (HTTP durumunu okur) ve memory/experience.py (erişim denetimi hatasını
ders dışı bırakır) arasında aynı 'returned error:' çapasını tek yerde tutar; iki modül birbirini içe aktarmaz.
"""
import re
import unicodedata
from typing import List

# curl --fail-with-body HTTP hatasında "curl: (22) The requested URL returned error: 429" yazar.
_CURL_HTTP_STATUS: re.Pattern[str] = re.compile(r"returned error: (\d{3})\b")


def ascii_fold(text: str) -> str:
    """Türkçe harfleri ASCII karşılığına indirir ve küçük harfe çevirir (ı→i, ş→s…). Saf."""
    replaced: str = text.replace("ı", "i").replace("İ", "i")
    decomposed: str = unicodedata.normalize("NFKD", replaced)
    return "".join(character for character in decomposed if not unicodedata.combining(character)).casefold()


def curl_http_statuses(text: str) -> List[int]:
    """curl hata çıktısındaki HTTP durum kodları (görünme sırasıyla; yoksa boş liste). Saf."""
    return [int(status) for status in _CURL_HTTP_STATUS.findall(text)]
