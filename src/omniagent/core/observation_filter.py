"""
Araç gözlemi süzgeci: görev defterine (task_ledger) ve kontrol noktasına (checkpoint) yazılan araç
kaynaklı metinden hassas veriyi ve bariz talimat taklidini ayıklar.

`config.redact` yalnız BİLİNEN sır değerlerini (kayıtlı API anahtarları) maskeler ve modelin gördüğü
araç mesajına uygulanır. Bu modül BİLİNMEYEN sırları (parola, OTP, kart/IBAN, token biçimleri, GUI'de
yazılan metin yankısı, URL içi parola, komut satırı bayrakları) anahtar adından ve değer biçiminden
yakalar; yalnız defter/kontrol noktası çıkışlarında kullanılır, araç mesajının kendisini değiştirmez
(model düzenlediği dosyayı görebilmeli).
Süzgeç bir güvenlik sınırı değildir: paraphrase edilmiş talimatları yakalamaz. Asıl savunma, defterin
gözlemi 'doğrulanmamış veri' olarak çerçevelemesidir. Anahtar adı eşleşmesi Latin harfli adlarla sınırlıdır.
Saf fonksiyonlar: hiçbiri global durumu okumaz veya değiştirmez.
"""
import json
import re
import unicodedata
from typing import Dict, List, Optional, Tuple

from omniagent.core.text_norm import ascii_fold

SENSITIVE_PLACEHOLDER: str = "[gizli]"
# Hassas adlı olgunun değeri yerine konur: model değerin var olduğunu bilir ve kaynağı yeniden okuyabilir
SENSITIVE_FACT_PLACEHOLDER: str = "[gizli: değer defterde tutulmaz; gerekirse kaynaktan yeniden oku]"
# Talimat taklidi içeren özet gösterilmez; yerine bu işaret konur
DIRECTIVE_PLACEHOLDER: str = "[talimat benzeri metin atlandı]"

# Görünmez metin taşıma (sıfır genişlik, yön kontrol, etiket karakterleri, varyasyon seçiciler) temizliği
_KEPT_CONTROLS: str = "\n\t"
_VARIATION_SELECTOR_RANGES: Tuple[Tuple[int, int], ...] = ((0xFE00, 0xFE0F), (0xE0100, 0xE01EF))
# Özet ve kalıcı kayıt üretirken maskeleme yalnız metnin bu kadar ilk (ham) karakterine uygulanır (maliyet sınırı):
# gösterilen/saklanan kısım (en çok birkaç yüz karakter) bunun çok altındadır, sınırda bölünen sır parçası da pencere
# içinde kalır. Pencere ham metne göre sayılır: görünmez karakterler pencerede yer tutar (bkz. single_line_snippet).
MASK_SCAN_CHARS: int = 2000
# Tek bir gözlem değerinden işlenen en çok karakter (bkz. bound_observation): daha uzunu kötü niyetli girdiyle desen
# maliyetini şişirebilir; gerçek olgu değerleri bunun çok altındadır (defter değeri 120 karakterle sınırlanır)
OBSERVATION_MAX_CHARS: int = 20000
# HTML etiketi: '<' ve '>' içermeyen gövde. Eski '<[^>]+>' kalıbı '<' yığınında ikinci dereceden çalışıyordu
HTML_TAG_PATTERN: re.Pattern[str] = re.compile(r"<[^<>]+>")

# Latin harfe göz aldatıcı biçimde benzeyen Kiril ve Yunan harfleri (küçük ve büyük). Talimat kalıbı ve anahtar adı
# aramasından önce Latin karşılığına çevrilir: 'ignоre' (Kiril 'о') ya da 'pаssword' (Kiril 'а') süzgeci atlatamaz.
# Yalnız arama görünümünde kullanılır; saklanan metin değişmez.
_CONFUSABLE_LETTERS: Dict[str, str] = {
    "а": "a", "с": "c", "е": "e", "о": "o", "р": "p", "х": "x", "у": "y",
    "і": "i", "ј": "j", "ѕ": "s", "һ": "h", "ԁ": "d", "ԛ": "q", "ԝ": "w",
    "ӏ": "l", "ѵ": "v",
    "А": "A", "В": "B", "С": "C", "Е": "E", "Н": "H", "І": "I", "Ј": "J",
    "К": "K", "М": "M", "О": "O", "Р": "P", "Ѕ": "S", "Т": "T", "Х": "X",
    "У": "Y",
    "α": "a", "ε": "e", "ι": "i", "ο": "o", "ρ": "p", "ν": "v", "υ": "u",
    "κ": "k", "χ": "x",
    "Α": "A", "Β": "B", "Ε": "E", "Ζ": "Z", "Η": "H", "Ι": "I", "Κ": "K",
    "Μ": "M", "Ν": "N", "Ο": "O", "Ρ": "P", "Τ": "T", "Υ": "Y", "Χ": "X",
}
_CONFUSABLE_TRANSLATION: Dict[int, str] = {ord(letter): latin for letter, latin in _CONFUSABLE_LETTERS.items()}
# Ham JSON metninde ASCII olmayan harfler '\uXXXX' kaçışıyla gelir (Python'un json.dumps varsayılanı: 'şifre'
# = 'şifre'); anahtar ve talimat aramasından önce çözülür, yoksa kaçışlı ad ham metin maskesinden kaçardı
_UNICODE_ESCAPE_PATTERN: re.Pattern[str] = re.compile(r"\\u([0-9a-fA-F]{4})")

# Yalnızca küçük harften büyük harfe geçiş sözcük sınırıdır: '2FA' ve 'APIKey' tek parça kalır
_CAMEL_BOUNDARY: re.Pattern[str] = re.compile(r"(?<=[a-z])(?=[A-Z])")
_NON_ALNUM: re.Pattern[str] = re.compile(r"[^a-z0-9]+")

# Hassas anahtar sözcükleri (camelCase/ayraçlardan bölünmüş, ASCII'ye indirilmiş)
_SENSITIVE_TOKENS: frozenset[str] = frozenset({
    "password", "passwords", "passwd", "passcode", "passphrase", "parola", "parolasi", "sifre", "sifresi",
    "secret", "secrets", "credential", "credentials", "apikey", "otp", "totp", "hotp", "mfa", "2fa", "pin",
    "cvv", "cvv2", "cvc", "cvc2", "iban", "ssn", "tckn", "cookie", "cookies", "authorization", "bearer",
    "jwt", "mnemonic", "seedphrase", "csrf", "xsrf", "jsessionid", "phpsessid",
})
_SENSITIVE_SUFFIXES: Tuple[str, ...] = ("password", "passwd", "secret", "apikey", "passphrase")
# Yalnız SON sözcük olduğunda hassas: 'db_pass', 'pass', 'sessionid' sır taşır; 'pass_rate' taşımaz (sondaki sözcük
# 'rate'), 'bypass' ve 'compass' tek sözcük olarak eşleşmez. 'pwd' yalnız önekliyken ('MYSQL_PWD', 'db_pwd')
# hassastır: çıplak 'PWD' kabuğun çalışma dizini değişkenidir. 'auth' bilerek yok: '--auth=kullanıcı:parola' bayrağını
# değer maskesi zaten kapatır, anahtar kuralı satırın kalanını da silerdi.
_SENSITIVE_LAST_TOKENS: frozenset[str] = frozenset({"pass", "sessionid"})
_SENSITIVE_PAIRS: frozenset[Tuple[str, str]] = frozenset({
    ("api", "key"), ("api", "anahtar"), ("api", "anahtari"), ("private", "key"), ("ozel", "anahtar"),
    ("ozel", "anahtari"), ("access", "key"), ("secret", "key"), ("signing", "key"), ("encryption", "key"),
    ("master", "key"), ("ssh", "key"), ("gizli", "anahtar"), ("gizli", "anahtari"), ("erisim", "anahtari"),
    ("card", "number"), ("card", "no"), ("card", "num"), ("card", "pan"), ("kart", "no"), ("kart", "numarasi"),
    ("kart", "numara"), ("credit", "card"), ("debit", "card"), ("kredi", "karti"), ("banka", "karti"),
    ("security", "code"), ("guvenlik", "kodu"), ("verification", "code"), ("dogrulama", "kodu"),
    ("one", "time"), ("tek", "kullanimlik"), ("sms", "code"), ("sms", "kodu"), ("backup", "code"),
    ("recovery", "code"), ("kurtarma", "kodu"), ("tc", "kimlik"), ("seed", "phrase"), ("recovery", "phrase"),
})
# 'token' sayaç anlamında kullanılıyorsa (max_tokens, token_count) hassas sayılmaz
_TOKEN_COUNTER_WORDS: frozenset[str] = frozenset({
    "count", "total", "max", "min", "limit", "usage", "used", "remaining", "prompt", "completion",
    "cached", "input", "output", "sayisi", "adet", "rate", "length", "budget",
})

_TOKEN_VALUE_PATTERNS: Tuple[re.Pattern[str], ...] = (
    # Öneki bilinen sağlayıcı anahtarları (önek + '-'/'_' + en az 12 karakter): OpenAI, GitHub, Slack, GitLab, HF
    re.compile(r"(?i)\b(?:sk|pk|rk|ghp|gho|ghs|ghu|ghr|github_pat|xox[baprs]|xapp|glpat|hf)[-_][A-Za-z0-9_-]{12,}"),
    # Önek genel bir sözcük olabilen anahtarlar sıkı biçimle aranır: 'npm_config_cache' ya da 'gsk-rapor-2024' eşleşmez
    re.compile(r"\bnpm_[A-Za-z0-9]{36}\b"),
    re.compile(r"\bgsk_[A-Za-z0-9]{20,}"),
    re.compile(r"\bpypi-AgE[A-Za-z0-9_-]{40,}"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"),
    re.compile(r"\bya29\.[A-Za-z0-9_-]{20,}"),
    # Başındaki '-' de sözcük dışıdır: 'eyJ-eyJ-...' yığınında her 'eyJ' ayrı başlangıç sayılıp süre ikinci dereceden büyürdü
    re.compile(r"(?<![\w-])eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    # Yetki başlığı değeri yalnız jeton biçimindeyse maskelenir: 'basic understanding' ya da 'Bearer authentication'
    # gibi düz sözcük çiftleri (yalnız harflerden oluşan uzun sözcük) jeton değildir. Jetonda rakam, sembol ya da
    # küçük harften büyük harfe geçiş bulunur (base64/hex/JWT).
    re.compile(r"(?i)\bbearer\s+(?-i:(?=[A-Za-z0-9._~+/-]*(?:[0-9._~+/-]|[a-z][A-Z])))[A-Za-z0-9._~+/-]{12,}=*"),
    re.compile(r"(?i)\bbasic\s+(?-i:(?=[A-Za-z0-9+/]*(?:[0-9+/]|[a-z][A-Z])))[A-Za-z0-9+/]{12,}={0,2}"),
    # Sırrı URL yolunda taşıyan biçimler: Telegram bot jetonu ('bot<kimlik>:AA...') ve Slack gelen webhook adresi
    re.compile(r"(?<![\w-])(?:bot)?\d{8,10}:AA[A-Za-z0-9_-]{30,}"),
    re.compile(r"hooks\.slack\.com/services/[A-Za-z0-9]+/[A-Za-z0-9]+/[A-Za-z0-9]{20,}"),
    # PEM/PGP özel anahtarı: başlıkla birlikte gövdesi de maskelenir (END satırı yoksa metnin sonuna kadar)
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY(?: BLOCK)?-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY(?: BLOCK)?-----|\Z)"
    ),
)
# 'ad: değer' atamaları; ad en çok 3 sözcük olabilir ('API key: ...'). Değer aynı satırda görünür ya da satır boştur
# (etiket ve değer ayrı satırlarda: 'Password:' + sonraki satır); ikinci durumu _mask_assignments çözer
_ASSIGNMENT_PATTERN: re.Pattern[str] = re.compile(
    r"(?P<name>(?:[^\s=:\"']{1,30}[ \t]+){0,2}[^\s=:\"']{1,30})[\"']?[ \t]*[=:][ \t]*(?=\S|\r|\n|\Z)"
)
# YAML blok skaler göstergesi ('password: |', 'key: >-'): değer sonraki girintili satırlardadır
_BLOCK_SCALAR_INDICATOR: re.Pattern[str] = re.compile(r"[|>][+-]?[1-9]?[+-]?")
# GUI yazma araçlarının yankısı (tools/facade.py cua_type_text/cua_submit_text/cua_fill_field ve
# tools/gui_input.py _run_action_step): '... (N karakter): <yankı>'. Şablon sınırı verir: yankı, yazılan metnin
# İLK min(N, 80) karakteridir (tools/types.py TYPED_TEXT_ECHO_LIMIT). Daha uzun metinde ardından
# '\n…[kısaltıldı, toplam N karakter]' gelir; cua_submit_text ayrıca '; Enter'a basıldı.' ekler. Yazılan metin satır
# sonu içerebildiği için sınır satır sonu değil karakter sayısıdır. tools paketi çekirdeğe bağlanmasın diye sabit
# burada tekrarlanır; testler ikisinin eşitliğini ve gerçek şablon çıktısını sabitler.
TYPED_ECHO_LIMIT: int = 80
_TYPED_ECHO_HEADER: re.Pattern[str] = re.compile(r"\((\d{1,9}) karakter\): ")
_TYPED_ENTER_SUFFIX: str = "; Enter'a basıldı."
_TYPED_KEY_PATTERN: re.Pattern[str] = re.compile(r"(?m)(Tuş yazıldı: ).+$")
# 'şema://kullanıcı:parola@sunucu': yalnız parola maskelenir, kullanıcı adı ve sunucu kalır. Parolasız
# 'kullanıcı@sunucu' ve 'http://sunucu:8080/yol' eşleşmez: parolayı '@' izlemeli; '/', '?' ve '#' yetki bölümünü
# (userinfo@host) sonlandırır.
_URL_CREDENTIAL_PATTERN: re.Pattern[str] = re.compile(r"(?<=://)([^/\s:@?#]*:)[^/\s?#]+(?=@)")
# Kabuk sözcüğü: tırnaklı parçalar (çift tırnakta '\"' kaçışı dahil, en çok 200 karakter) ile tırnaksız karakterler
# bitişikse tek değerdir ('"abc"def' tek sözcüktür, sırrın tamamı maskelenir); kapanmayan tırnak sıradan karakter sayılır.
_SHELL_WORD: str = r"(?:\"(?:[^\"\\\n]|\\.){0,200}\"|'[^'\n]{0,200}'|\S)+"
# Komut satırı sırları: açık adlı uzun bayraklar '--bayrak değer' ve '--bayrak=değer' (tırnaklı değer bütün alınır).
# Bayrak adı tam eşleşir ('--password-stdin', '--token-count' eşleşmez). Boşluklu biçimde '-' ile başlayan sonraki
# sözcük değer sayılmaz ('--auth --verbose'); '=' biçimi bundan etkilenmez. '-p' ve '-P' BİLEREK kapsam dışıdır:
# ssh/scp/nc'de port, mkdir/cp'de parent/preserve, mysql'de bitişik parola anlamına gelir; tek harften hangisinin
# kastedildiği bilinemez, maskelemek port ve yol bilgisini silerdi.
_SECRET_FLAG_PATTERN: re.Pattern[str] = re.compile(
    r"(?i)(?<![\w-])(?P<flag>--(?:password|passwd|token|api[-_]?key|secret|auth))(?P<sep>=|[ \t]+(?!-))" + _SHELL_WORD
)
# curl: '-u kullanıcı:parola' ve '--user kullanıcı:parola' yalnız curl komutunun içinde aranır; 'docker run -u
# 1000:1000' ya da '--user root:root' uid:gid'dir, parola değildir. Komut satır sonu, '|', ';', '&' ya da '`' ile
# biter; '\'+satır sonu sürdürür. Bitişik yazım ('-uadmin:parola') kapsam dışıdır. 'Authorization: Bearer ...'
# başlığı ayrı kalıp istemez: hassas adlı 'ad: değer' kuralı (_mask_assignments) onu zaten satır sonuna kadar maskeler.
_CURL_COMMAND_PATTERN: re.Pattern[str] = re.compile(r"\bcurl\b(?:\\\r?\n|[^\n|;&`])*")
_CURL_USER_PATTERN: re.Pattern[str] = re.compile(
    r"(?<![\w-])(?P<flag>-u|--user)(?P<sep>=|[ \t]+)(?P<value>" + _SHELL_WORD + ")"
)
# Kart numarası biçimleri: 4-4-4-4 ya da 4-6-5/4-6-4 (Amex/Diners) gruplu, ya da bitişik 13-19 hane. Komşu rakam
# gruplarını (son kullanma tarihi, sipariş no) kendine katmaz; katsaydı Luhn sağlaması tutmaz ve numara sızardı.
_PAN_PATTERN: re.Pattern[str] = re.compile(
    r"(?<![\d-])(?:\d{4}(?:[ -]\d{4}){3}|\d{4}[ -]\d{6}[ -]\d{4,5}|\d{13,19})(?![\d-])"
)
# Bitişik hane dizisi yalnız bilinen bir kart ağı önekiyle başlıyorsa kart sayılır (gruplu yazımda önek aranmaz):
# Visa 4; Mastercard 51-55 ve 2221-2720; Maestro 50/56-58/63/67; Amex 34/37; JCB 35; Diners 30/36/38/39;
# Discover 60/64/65 (6011, 644-649); UnionPay 62; Mir 22; Troy 9792.
# approval.py'nin kart giriş kapısı (_CARD_RANGES) BİLEREK daha dar ve ayrıdır: kapı yanlış pozitifte kullanıcıya
# gereksiz onay sorar, bu süzgeç ise yanlış pozitifte yalnız bir sayıyı maskeler; kaçan gerçek kart ikisinde de sızıntıdır
# ama maskelemenin bedeli çok daha düşüktür. Bu yüzden ön ek ve uzunluk burada geniş tutulur: kapının kabul ettiği her
# ön ek/uzunluk süzgeçte de vardır (tersi geçerli değildir); gruplama biçimleri ise iki yerde ayrı ele alınır.
# Yalnız Luhn (luhn_valid) ortaktır.
_PAN_ISSUER_PATTERN: re.Pattern[str] = re.compile(r"4|5[0-8]|2[2-7]|3[04-9]|6[0-57]|9792")
# Bitişik ya da 4'lü gruplu IBAN. Sondaki 1-3 karakterlik grup ayrı yakalanır: komşu büyük harfli kısa sözcük
# ('TL', 'OCR') ona katılmış olabilir; o durumda çekirdek tek başına denenir.
_IBAN_PATTERN: re.Pattern[str] = re.compile(
    r"\b(?P<core>[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7})(?P<tail> ?[A-Z0-9]{1,3})?\b"
)
# T.C. kimlik numarası yalnız hassas bir etiketle ('TCKN', 'T.C. Kimlik No', 'Kimlik No', 'Kimlik') ve sağlaması tutan
# 11 hane olarak maskelenir: etiketsiz 11 haneli sayı (telefon, sipariş no) kimlik sayılmaz. 'ad: değer' biçimi
# (etiketin ':' ile ayrıldığı hassas adlı atama) zaten _mask_assignments'ta; bu kalıp ayraçsız yazımı kapatır.
_TCKN_PATTERN: re.Pattern[str] = re.compile(
    r"(?i)\b(?P<label>tckn|t\.?[ \t]?c\.?(?:[ \t]+kimlik)?(?:[ \t]+(?:no|numaras[ıi]))?"
    r"|kimlik(?:[ \t]+(?:no|numaras[ıi]))?)(?P<gap>[ \t:=.\-]{0,4})(?P<number>[1-9]\d{10})(?!\d)"
)

# Talimat taklidi: yüksek kesinlikli, düşük yakalamalı dar liste (genel emir kipi tespiti yapılmaz)
_DIRECTIVE_EXACT_KEYS: frozenset[str] = frozenset({
    "system", "assistant", "developer", "human", "prompt", "system_prompt",
})
_DIRECTIVE_KEY_TOKENS: frozenset[str] = frozenset({
    "instruction", "instructions", "talimat", "talimatlar", "jailbreak", "override",
})
_DIRECTIVE_LEADING_TOKENS: frozenset[str] = frozenset({"ignore", "disregard", "forget"})
_DIRECTIVE_LEADING_PAIRS: frozenset[Tuple[str, str]] = frozenset({("note", "to"), ("message", "to")})
# Emir kipiyle geçersiz kılma ya da gizleme ('önceki talimatları yok say', 'kullanıcıya söyleme'). Gözlemde de,
# modelin kendi notunda da (bkz. task_ledger._clean_model_note) talimat taklidi sayılır.
_DIRECTIVE_OVERRIDE_PATTERNS: Tuple[re.Pattern[str], ...] = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    r"\b(?:ignore|disregard|forget|override|bypass)\b[^.\n]{0,40}"
    r"\b(?:previous|prior|above|earlier|preceding|all|any|your)\b"
    r"[^.\n]{0,30}\b(?:instruction|prompt|rule|guideline|direction|command)s?\b",
    r"\bdo\s+not\s+(?:tell|inform|mention|reveal|show|alert)\b[^.\n]{0,30}\b(?:user|human|owner)\b",
    r"\b(?:önceki|onceki|yukarıdaki|yukaridaki)\s+(?:tüm\s+|tum\s+)?(?:talimat|komut|yönerge|yonerge|kural)\w*"
    r"\s+(?:yok\s+say|unut|dikkate\s+alma|görmezden\s+gel|gormezden\s+gel)",
    r"\bkullanıcıya\s+(?:söyleme|soyleme|bildirme|gösterme|gosterme)\b",
))
# Konu ifadeleri ('system prompt', 'yeni talimat'): gözlemde (olgu, makbuz) talimat sayılır; ama modelin notunda ve
# kullanıcı talimatını özetleyen cümlelerde doğal geçer (bu depodaki sistem istemi görevleri gibi), notta elenmez.
_DIRECTIVE_TOPIC_PATTERNS: Tuple[re.Pattern[str], ...] = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    r"\bsystem\s+prompt\b",
    r"\b(?:new|updated|revised)\s+(?:system\s+)?instructions?\b",
    r"\b(?:yeni|güncel|guncel)\s+talimat",
))
_DIRECTIVE_VALUE_PATTERNS: Tuple[re.Pattern[str], ...] = _DIRECTIVE_OVERRIDE_PATTERNS + _DIRECTIVE_TOPIC_PATTERNS


def strip_invisible_characters(text: str) -> str:
    """Sıfır genişlikli, yön kontrol, etiket ve varyasyon seçici karakterleri atar; \\n ve \\t kalır. Saf."""
    return "".join(
        char for char in text
        if char in _KEPT_CONTROLS
        or (
            unicodedata.category(char) not in ("Cc", "Cf")
            and not any(low <= ord(char) <= high for low, high in _VARIATION_SELECTOR_RANGES)
        )
    )


def _decode_unicode_escape(match: re.Match[str]) -> str:
    """'\\uXXXX' kaçışını gösterdiği karaktere çevirir. Saf."""
    return chr(int(match.group(1), 16))


def key_tokens(key: str) -> List[str]:
    """
    Anahtarı '\\uXXXX' kaçışlarından, görünmezlerden ve Latin'e benzeşen Kiril/Yunan harflerden arındırıp
    camelCase/ayraç sınırlarından küçük harfli ASCII sözcüklere böler ('pаssword' Kiril 'а' ile de, ham JSON'daki
    '\\u015fifre' de 'sifre' olur). Saf.
    """
    decoded: str = _UNICODE_ESCAPE_PATTERN.sub(_decode_unicode_escape, key)
    normalized: str = strip_invisible_characters(decoded).translate(_CONFUSABLE_TRANSLATION)
    spaced: str = _CAMEL_BOUNDARY.sub("_", normalized)
    return [token for token in _NON_ALNUM.split(ascii_fold(spaced)) if token]


def is_sensitive_key(raw_key: str) -> bool:
    """Anahtar adı parola/sır/OTP/kart/IBAN türü bir değere mi işaret ediyor? Ham (normalize edilmemiş) ad verilir. Saf."""
    tokens: List[str] = key_tokens(raw_key)
    if any(token in _SENSITIVE_TOKENS or token.endswith(_SENSITIVE_SUFFIXES) for token in tokens):
        return True
    if tokens and (tokens[-1] in _SENSITIVE_LAST_TOKENS or (tokens[-1] == "pwd" and len(tokens) > 1)):
        return True
    if any(token.endswith("token") for token in tokens) and not (set(tokens) & _TOKEN_COUNTER_WORDS):
        return True
    return any(pair in _SENSITIVE_PAIRS for pair in zip(tokens, tokens[1:]))


def _line_indent(text: str, position: int) -> int:
    """`position`ın bulunduğu satırın girinti genişliği (baştaki boşluk/sekme sayısı). Saf."""
    head: str = text[text.rfind("\n", 0, position) + 1:position]
    return len(head) - len(head.lstrip(" \t"))


def _indented_value_end(text: str, line_end: int, key_indent: int, block_scalar: bool) -> int:
    """
    Değeri sonraki satırlarda olan atamanın değer bölgesinin biten konumu; değer yoksa -1. İlk dolu satır değerdir.
    Etiket ve değer ayrı satırlardaysa ('Password:' + 'hunter2') değer TEK satırdır ve etiketle aynı sütunda
    başlamalıdır: 'if token:' ya da 'class TokenFinding:' gibi kod başlıklarının girintili gövdesi (ya da başka
    girintideki satır) değer sayılıp maskelenmez. YAML blok skalerinde ('password: |') gövde anahtardan daha
    girintili satırlardır (aradaki boş satırlar dahil). Saf.
    """
    end: int = -1
    position: int = line_end
    while position < len(text):
        start: int = position + 1
        stop: int = text.find("\n", start)
        stop = len(text) if stop < 0 else stop
        line: str = text[start:stop]
        if line.strip():
            indent: int = len(line) - len(line.lstrip(" \t"))
            if end < 0 and (indent <= key_indent if block_scalar else indent != key_indent):
                break
            if end >= 0 and indent <= key_indent:
                break
            end = stop
            if not block_scalar:
                break
        position = stop
    return end


def _assignment_end(text: str, line_end: int, key_indent: int, inline_value: str) -> int:
    """
    Hassas atamanın maskelenecek bölgesinin biten konumu. Değer aynı satırdaysa satır sonu; değer yoksa
    ('Password:' + sonraki dolu satır) ya da YAML blok skaler göstergesiyse ('password: |') sonraki satırlardaki
    değerin sonu. Maskelenecek değer hiç yoksa -1. Saf.
    """
    block_scalar: bool = _BLOCK_SCALAR_INDICATOR.fullmatch(inline_value) is not None
    if inline_value and not block_scalar:
        return line_end
    body_end: int = _indented_value_end(text, line_end, key_indent, block_scalar)
    if body_end >= 0:
        return body_end
    return line_end if block_scalar else -1


def _mask_assignments(text: str) -> str:
    """
    Hassas adlı 'ad: değer' atamalarını değerin sonuna kadar maskeler: değer aynı satırdaysa satır sonuna, etiket
    ve değer ayrı satırlardaysa ('Password:' + sonraki dolu satır) ya da değer YAML blok skaleriyse ('password: |')
    sonraki satırlardaki değerin sonuna kadar. Saf.
    """
    pieces: List[str] = []
    cursor: int = 0
    for match in _ASSIGNMENT_PATTERN.finditer(text):
        if match.start() < cursor or not is_sensitive_key(match.group("name")):
            continue
        line_end: int = text.find("\n", match.end())
        line_end = len(text) if line_end < 0 else line_end
        end: int = _assignment_end(
            text, line_end, _line_indent(text, match.start()), text[match.end():line_end].strip(),
        )
        if end < 0:
            continue
        pieces.append(text[cursor:match.start()])
        pieces.append(SENSITIVE_PLACEHOLDER)
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces)


def luhn_valid(digits: str) -> bool:
    """
    Rakam dizisi Luhn sağlama toplamını tutuyor mu (ödeme kartı numaralarının ortak sağlaması)? Projedeki TEK
    uygulamadır: approval.py'nin kart giriş kapısı da bunu kullanır (kart AĞ tabloları ise bilerek ayrıdır). Saf.
    """
    total: int = 0
    for index, char in enumerate(reversed(digits)):
        value: int = int(char)
        if index % 2 == 1:
            value = value * 2 - 9 if value > 4 else value * 2
        total += value
    return total % 10 == 0


def _mask_card(match: re.Match[str]) -> str:
    """13-19 haneli, Luhn geçerli ve (gruplu ya da bilinen kart öneki taşıyan) sayıyı maskeler. Saf."""
    digits: str = re.sub(r"\D", "", match.group(0))
    grouped: bool = re.search(r"[ -]", match.group(0)) is not None
    if 13 <= len(digits) <= 19 and luhn_valid(digits) and (grouped or _PAN_ISSUER_PATTERN.match(digits)):
        return SENSITIVE_PLACEHOLDER
    return match.group(0)


def _iban_valid(compact: str) -> bool:
    """IBAN uzunluğu (15-34) ve mod-97 sağlaması. Saf."""
    if not 15 <= len(compact) <= 34:
        return False
    rearranged: str = compact[4:] + compact[:4]
    return int("".join(str(int(char, 36)) for char in rearranged)) % 97 == 1


def _mask_iban(match: re.Match[str]) -> str:
    """Mod-97 sağlaması geçen IBAN'ı maskeler; komşu kısa sözcük katıldıysa çekirdeği tek başına dener. Saf."""
    core: str = match.group("core")
    tail: str = match.group("tail") if match.group("tail") is not None else ""
    if _iban_valid((core + tail).replace(" ", "")):
        return SENSITIVE_PLACEHOLDER
    if tail.startswith(" ") and _iban_valid(core.replace(" ", "")):
        return SENSITIVE_PLACEHOLDER + tail
    return match.group(0)


def _tckn_valid(digits: str) -> bool:
    """T.C. kimlik numarası sağlaması: ilk hane 0 değil, 10. hane ve 11. hane algoritmaya uyar. Saf."""
    values: List[int] = [int(char) for char in digits]
    if len(values) != 11 or values[0] == 0:
        return False
    odd_sum: int = sum(values[0:9:2])
    even_sum: int = sum(values[1:8:2])
    return (odd_sum * 7 - even_sum) % 10 == values[9] and sum(values[:10]) % 10 == values[10]


def _mask_tckn(match: re.Match[str]) -> str:
    """Hassas etiketin yanındaki, sağlaması tutan 11 haneli T.C. kimlik numarasını maskeler; etiket kalır. Saf."""
    if not _tckn_valid(match.group("number")):
        return match.group(0)
    return f"{match.group('label')}{match.group('gap')}{SENSITIVE_PLACEHOLDER}"


def _typed_echo_end(text: str, start: int, typed_length: int) -> int:
    """
    Yankının biten konumu (dışlayıcı): şablonun verdiği sınır (yazılan uzunluk, en çok TYPED_ECHO_LIMIT) ile aynı
    satırdaki sınır (satır sonu ya da Enter eki) içinden ileride olanı. İkincisi, sayısı tutmayan yankıyı (görünmez
    karakteri atılmış ya da araç dışı metin) en az satır sonuna kadar örter. Saf.
    """
    line_end: int = text.find("\n", start)
    line_end = len(text) if line_end < 0 else line_end
    enter_start: int = text.find(_TYPED_ENTER_SUFFIX, start, line_end)
    same_line_end: int = line_end if enter_start < 0 else enter_start
    return min(len(text), max(same_line_end, start + min(typed_length, TYPED_ECHO_LIMIT)))


def _mask_typed_echo(text: str) -> str:
    """'(N karakter): <yankı>' yankılarını, çok satırlı olsalar da, bütünüyle maskeler; başlık ve sonrası kalır. Saf."""
    pieces: List[str] = []
    cursor: int = 0
    for match in _TYPED_ECHO_HEADER.finditer(text):
        start: int = match.end()
        # Önceden maskelenmiş yankı (yeniden süzme) ya da maskelenen bölgenin içindeki başlık atlanır
        if start < cursor or text.startswith(SENSITIVE_PLACEHOLDER, start):
            continue
        end: int = _typed_echo_end(text, start, int(match.group(1)))
        if end <= start:
            continue
        pieces.append(text[cursor:start])
        pieces.append(SENSITIVE_PLACEHOLDER)
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces)


def _mask_curl_user(match: re.Match[str]) -> str:
    """'-u kullanıcı:parola' değerinde yalnız parolayı maskeler (kullanıcı adı ve tırnaklar kalır); ':' yoksa dokunmaz. Saf."""
    value: str = match.group("value")
    quote: str = value[0] if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0] else ""
    credential: str = value[1:-1] if quote else value
    user, separator, password = credential.partition(":")
    if not separator or not password:
        return match.group(0)
    return f"{match.group('flag')}{match.group('sep')}{quote}{user}:{SENSITIVE_PLACEHOLDER}{quote}"


def _mask_curl_command(match: re.Match[str]) -> str:
    """Tek bir curl komutunun içindeki '-u/--user' kimlik bilgisini maskeler. Saf."""
    return _CURL_USER_PATTERN.sub(_mask_curl_user, match.group(0))


def mask_sensitive_text(text: str) -> str:
    """
    Metindeki bilinmeyen sır biçimlerini [gizli] ile değiştirir: GUI yazma yankısı (çok satırlı dahil),
    sağlayıcı jetonları, JWT, PEM/PGP anahtarı, URL yolundaki Telegram jetonu ve Slack webhook, URL içi parola,
    komut satırı bayrakları (--password değer, curl -u), hassas adlı atamalar (değer sonraki satırdaysa ya da
    YAML blok skaleriyse orada da), Luhn geçerli kart numarası, mod-97 geçerli IBAN, etiketli T.C. kimlik numarası.
    Komut satırında '-p'/'-P' BİLEREK kapsam dışıdır: ssh/scp/nc'de port, mkdir/cp'de parent/preserve, mysql'de
    bitişik parola demektir; tek harften kastedilen anlaşılamaz ve maskelemek port ile yol bilgisini silerdi
    (bkz. _SECRET_FLAG_PATTERN). Saf.
    """
    masked: str = _mask_typed_echo(text)
    masked = _TYPED_KEY_PATTERN.sub(r"\1" + SENSITIVE_PLACEHOLDER, masked)
    for pattern in _TOKEN_VALUE_PATTERNS:
        masked = pattern.sub(SENSITIVE_PLACEHOLDER, masked)
    masked = _URL_CREDENTIAL_PATTERN.sub(r"\1" + SENSITIVE_PLACEHOLDER, masked)
    masked = _SECRET_FLAG_PATTERN.sub(r"\g<flag>\g<sep>" + SENSITIVE_PLACEHOLDER, masked)
    masked = _CURL_COMMAND_PATTERN.sub(_mask_curl_command, masked)
    # Değer biçimleri (kart, IBAN, T.C. kimlik) atamalardan ÖNCE: 'pin36140000000002:9' gibi sözcüğe bitişik sayı
    # maskelendikten sonra ad ('pin[gizli]') hassas görünür; sıra tersse ikinci geçiş yeni maske üretirdi
    masked = _PAN_PATTERN.sub(_mask_card, masked)
    masked = _IBAN_PATTERN.sub(_mask_iban, masked)
    masked = _TCKN_PATTERN.sub(_mask_tckn, masked)
    return _mask_assignments(masked)


# --- Alana yazılan metin (GUI/tarayıcı yazma araçlarının argümanları) ---
# Aracın tek 'text' argümanı ile alana yazdığı araçlar; dizi/tarayıcı araçlarının yazdığı metinler adımlarından ayrıca
# çıkarılır. Hem approval.py'nin kart girişi kapısı hem kalıcı epizot kaydı (core/state.py) aynı kapsamı kullanır.
_SINGLE_TEXT_TOOLS: frozenset[str] = frozenset({"cua_type_text", "cua_fill_field", "cua_submit_text", "cua_set_text_element"})
TYPED_TEXT_TOOLS: frozenset[str] = _SINGLE_TEXT_TOOLS | frozenset({"run_action_sequence", "browse_url"})
_STEP_LIST_KEYS: Tuple[str, ...] = ("steps", "actions")


def _steps_list(raw: object) -> List[object]:
    """Adım listesi: liste olduğu gibi, JSON metni çözülerek; başka biçim boş liste. Saf."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    return list(raw) if isinstance(raw, list) else []


def typed_texts(tool: str, arguments: Dict[str, object]) -> List[str]:
    """Aracın bir alana yazacağı metinler (yalnız argümanlardan); yazmayan araç ya da bozuk biçim için boş liste. Saf."""
    if tool in _SINGLE_TEXT_TOOLS:
        text: object = arguments.get("text")
        return [text] if isinstance(text, str) else []
    if tool == "run_action_sequence":
        return [
            step["text"] for step in _steps_list(arguments.get("steps"))
            if isinstance(step, dict) and step.get("action") == "type" and isinstance(step.get("text"), str)
        ]
    if tool == "browse_url":
        return [
            step["value"] for step in _steps_list(arguments.get("actions"))
            if isinstance(step, dict) and step.get("action") == "fill" and isinstance(step.get("value"), str)
        ]
    return []


def _replace_typed(value: object, typed: frozenset[str]) -> object:
    """İç içe sözlük/liste yapısında, yazılan metinlerden birine eşit her metin yaprağını SENSITIVE_PLACEHOLDER yapar. Saf."""
    if isinstance(value, str):
        return SENSITIVE_PLACEHOLDER if value in typed else value
    if isinstance(value, dict):
        return {key: _replace_typed(item, typed) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_typed(item, typed) for item in value]
    return value


def mask_typed_arguments(tool: str, arguments: str) -> str:
    """
    Alana yazılan metni taşıyan araç argümanında (typed_texts) o metinleri SENSITIVE_PLACEHOLDER ile değiştirir:
    yazılan metin parola olabilir ve mask_sensitive_text ona bakarak sır olduğunu anlayamaz ('{"text": "hunter2"}').
    Metin yazmayan araç ya da JSON olmayan argüman aynen döner (kalanını çağıran mask_sensitive_text'ten geçirir);
    dizi olarak metin gelen adım listeleri ('steps'/'actions') çözülüp yapıya işlenir. Aşırı derin iç içe değer
    işlenemez: argümanın tamamı SENSITIVE_PLACEHOLDER olur (sessizce sızdırılmaz). Saf.
    """
    if tool not in TYPED_TEXT_TOOLS:
        return arguments  # büyük write_file/edit_file argümanları boşuna çözülmez
    try:
        parsed: object = json.loads(arguments)
        if not isinstance(parsed, dict):
            return arguments
        typed: frozenset[str] = frozenset(text for text in typed_texts(tool, parsed) if text)
        if not typed:
            return arguments
        expanded: Dict[str, object] = {
            key: (_steps_list(value) or value) if key in _STEP_LIST_KEYS else value for key, value in parsed.items()
        }
        return json.dumps(_replace_typed(expanded, typed), ensure_ascii=False)
    except ValueError:  # JSONDecodeError ve aşırı uzun tamsayı metni (ValueError) dahil: argüman JSON değil
        return arguments
    except RecursionError:
        return SENSITIVE_PLACEHOLDER


def _is_directive_key(tokens: List[str]) -> bool:
    """Anahtar bir rol/talimat etiketi mi ('system', 'ignore_*', 'note_to_*', '*instructions')? Saf."""
    return (
        "_".join(tokens) in _DIRECTIVE_EXACT_KEYS
        or bool(_DIRECTIVE_KEY_TOKENS & set(tokens))
        or (bool(tokens) and tokens[0] in _DIRECTIVE_LEADING_TOKENS)
        or (len(tokens) >= 2 and (tokens[0], tokens[1]) in _DIRECTIVE_LEADING_PAIRS)
    )


def directive_view(text: str) -> str:
    """
    Talimat kalıbı aramasının baktığı görünüm: '\\uXXXX' kaçışları çözülür, görünmez karakterler atılır, NFKC ile
    tam genişlik/matematiksel/daire içi harfler ASCII'ye iner, HTML etiketleri boşluksuz silinir
    ('ig<b></b>nore' -> 'ignore') ve Latin harfe benzeyen Kiril/Yunan harfler Latin'e çevrilir. Yalnız arama
    içindir; saklanan metin değişmez. Saf.
    """
    decoded: str = _UNICODE_ESCAPE_PATTERN.sub(_decode_unicode_escape, text)
    composed: str = unicodedata.normalize("NFKC", strip_invisible_characters(decoded))
    return HTML_TAG_PATTERN.sub("", composed).translate(_CONFUSABLE_TRANSLATION)


def _matches_directive(patterns: Tuple[re.Pattern[str], ...], value: str) -> bool:
    """
    Kalıplardan biri hem özgün metinde hem directive_view görünümünde aranır: Unicode/HTML bölme ('ｉｇｎｏｒｅ',
    Kiril 'о', 'ig<b></b>nore') süzgeci atlatamaz, özgün metinde eşleşen hiçbir şey de kaybolmaz. Saf.
    """
    view: str = directive_view(value)
    return any(pattern.search(value) is not None or pattern.search(view) is not None for pattern in patterns)


def has_directive_phrase(value: str) -> bool:
    """Metin 'önceki talimatları yok say' türü açık bir talimat taklidi (emir kipi ya da konu ifadesi) içeriyor mu? Saf."""
    return _matches_directive(_DIRECTIVE_VALUE_PATTERNS, value)


def has_override_phrase(value: str) -> bool:
    """
    Metin yalnız emir kipiyle geçersiz kılma/gizleme kalıbı ('ignore previous instructions', 'önceki talimatları yok
    say', 'do not tell the user') içeriyor mu? 'system prompt' ya da 'yeni talimat' gibi konu ifadeleri sayılmaz: modelin
    kendi notu için kullanılır (bkz. _DIRECTIVE_TOPIC_PATTERNS). Saf.
    """
    return _matches_directive(_DIRECTIVE_OVERRIDE_PATTERNS, value)


def bound_observation(text: str) -> str:
    """
    OBSERVATION_MAX_CHARS'ı aşan metni son boşluk sınırında keser ve sonuna SENSITIVE_PLACEHOLDER koyar: atılan
    kuyrukta sır olabilir, sınırda bölünen yarım sözcük ise sır kalıntısı olabilir; ikisi de metinde kalmaz.
    Sınırın altındaki metin aynen döner. Saf.
    """
    if len(text) <= OBSERVATION_MAX_CHARS:
        return text
    head: str = text[:OBSERVATION_MAX_CHARS]
    if not text[OBSERVATION_MAX_CHARS].isspace():
        head = head[:max(head.rfind(" "), head.rfind("\n"), head.rfind("\t")) + 1]
    return f"{head.rstrip()} {SENSITIVE_PLACEHOLDER}".lstrip()


def is_directive_observation(raw_key: str, value: str) -> bool:
    """Gözlem, modele yönelik talimat taklidi mi (rol etiketi anahtarı ya da açık talimat kalıbı)? Saf."""
    return _is_directive_key(key_tokens(raw_key)) or has_directive_phrase(value)


def sanitize_observation(raw_key: str, value: str) -> Optional[str]:
    """
    Gözlemin deftere/kontrol noktasına yazılacak değerini döner. Hassas adlı anahtarda değer yerine
    SENSITIVE_FACT_PLACEHOLDER; hassas görünen değerde ya da talimat taklidinde None (olgu düşer). Değer
    OBSERVATION_MAX_CHARS'ı aşıyorsa önce bound_observation ile sınırlanır (işlem maliyeti girdiyle şişmez). Saf.
    """
    bounded: str = bound_observation(value)
    if is_sensitive_key(raw_key):
        return SENSITIVE_FACT_PLACEHOLDER
    if mask_sensitive_text(bounded) != bounded or is_directive_observation(raw_key, bounded):
        return None
    return bounded


def single_line_snippet(text: str, limit: int) -> str:
    """
    Metni görünmez karakterlerden arındırır, hassas kalıpları maskeler, tek satıra indirir ve kısaltır;
    görünen özet açık bir talimat taklidi içeriyorsa özet yerine işaret döner. Maliyet metnin uzunluğundan değil
    MASK_SCAN_CHARS penceresinden gelir: önce ham metin pencereye kesilir, sonra görünmezler atılır (görünmez
    karakterle şişirilmiş metinde pencerede daha az görünür karakter kalabilir; özet yalnız kısalır). Saf.
    """
    masked: str = mask_sensitive_text(strip_invisible_characters(text[:MASK_SCAN_CHARS]))
    snippet: str = " ".join(masked.split())[:limit]
    return DIRECTIVE_PLACEHOLDER if has_directive_phrase(snippet) else snippet
