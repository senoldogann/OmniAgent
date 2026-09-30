"""
İnsan onayı gerektiren araç çağrılarını sınıflandırır ve onay kararlarını denetim kaydına yazar.

Para hareketi (ödeme, transfer, alım-satım, blok zinciri gönderimi, ödeme kartı numarası girişi), geri
alınamaz dış iletişim (entegrasyon araçlarıyla mesaj gönderme/yayınlama) ve kullanıcının açıkça istemediği
kalıcı hafıza değişikliği, eylem gerçekleşmeden önce host tarafından kullanıcıya sorulur. Soru metnini
model değil host üretir: model onay metnini yönlendiremez. Etkileşimli kanal (arayüz/Telegram) yoksa eylem
reddedilir. İki otomatik onay yolu vardır ve ikisi de aşağıdaki ayarlarla kapatılabilir: (1) should_auto_approve
— AUTO_APPROVE_HOSTS'taki adresler ve AUTO_APPROVE_LIMIT altındaki tutarlar sorulmadan geçer (varsayılan
olarak ikisi de kapalı); (2) AUTO_APPROVE_IN_CONTINUOUS_MODE — sürekli (gözetimsiz) modda onay sorulmaz.

Kapsam ve sınırlar (en iyi çaba; güvenlik sınırı değildir):
- Kabuk/JS/MCP: bilinen finansal CLI, API adresi, RPC yöntemi ve araç adı kalıpları çağrıdan ÖNCE,
  yalnız argümanlara bakılarak sınıflandırılır.
- Yazılan metin: Luhn ve ağ ön eki (IIN) tutan 13-19 haneli değeri (ödeme kartı numarası) yazan
  GUI/tarayıcı araçları çağrıdan önce sorulur; numara özette ve denetim kaydında maskelenir.
- Grafik arayüz ve tarayıcı: tıklama ANINDA çözümlenen gerçek hedef etiketi (OCR satırı, erişilebilirlik
  adı, DOM metni) kısa ve fiil odaklı bir TR/EN/FI tabloyla denetlenir (financial_cta_reason); genel
  son-onay etiketi ('Onayla', 'Gönder') yalnız aynı ekranda tutar ve alıcı/IBAN bilgisi de varsa
  (financial_context_reason). Araç hedefi çözer, tıklamadan önce ToolRuntime üzerinden host onayı ister;
  onay beklerken hedef değiştiyse tıklamaz. İnsan/bot doğrulaması ifadesi taşıyan hedef ('I'm not a robot')
  onaya hiç düşmez: kullanıcı onayıyla da geçilmeyen sert reddir (bot_wall.human_verification_label).
- Denetlenmeyen yollar: Enter/boşluk ile gönderim, ikon-only düğme, bağlamsız genel etiketli düğme, OCR
  okuma hatası ve kabuktan (osascript vb.) tıklama. Sistem istemindeki `ask_user` kuralı bu boşluklar
  için ikinci katmandır. Amaç kararlı bir saldırganı durdurmak değil, kazara ya da yönlendirilmiş para
  hareketini yakalamaktır.
"""
import base64
import json
import os
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, List, NotRequired, Optional, Tuple, TypedDict

from omniagent.config import redact
from omniagent.core.observation_filter import luhn_valid, typed_texts
from omniagent.core.text_norm import ascii_fold

# Onay penceresinde gösterilecek çağrı özetinin üst sınırı
APPROVAL_SUMMARY_LIMIT: int = 600
# Kullanıcı yanıtı bu süre içinde gelmezse eylem onaylanmamış sayılır (görev sonsuza dek kilitlenmesin)
APPROVAL_TIMEOUT_SECONDS: float = 900.0
APPROVAL_FIELD: str = "onay"
# Host onay kapısının araç sonuçlarındaki ret kodları: model yürütülmeyen bir çağrının hatasını "çözmeye" çalışmasın,
# deneyim belleği de bunlardan ders çıkarmasın (host politikasıdır, araç hatası değil)
APPROVAL_UNAVAILABLE_CODE: str = "APPROVAL_UNAVAILABLE"
APPROVAL_DENIED_CODE: str = "APPROVAL_DENIED"
APPROVAL_TIMEOUT_CODE: str = "APPROVAL_TIMEOUT"
TARGET_CHANGED_CODE: str = "TARGET_CHANGED_AFTER_APPROVAL"
APPROVAL_REFUSAL_CODES: frozenset[str] = frozenset({
    APPROVAL_UNAVAILABLE_CODE, APPROVAL_DENIED_CODE, APPROVAL_TIMEOUT_CODE, TARGET_CHANGED_CODE,
})
# Reddedilen bir tıklamanın hata metnine eklenir: sürekli modda "farklı bir yöntem seç" kurtarma mesajı modeli reddedilen
# düğmeyi başka araçla dolanmaya itmesin
NO_CIRCUMVENTION_NOTE: str = (
    "Aynı düğmeyi başka araçla ya da yolla (nokta, metin, klavye, kabuk) tıklamaya çalışma; durumu kullanıcıya bildir."
)

# Araç adlarında para hareketi bildiren fiiller (ASCII'ye indirgenmiş, tam sözcük eşleşmesi).
# Yalnız eylem fiilleri: "payments" listeleyen salt okunur araçlar ayrıca readonly işaretlenir.
_FINANCIAL_NAME_TOKENS: frozenset[str] = frozenset({
    "pay", "payment", "payments", "payout", "payouts", "transfer", "transfers", "withdraw",
    "withdrawal", "deposit", "purchase", "buy", "sell", "checkout", "charge", "refund",
    "trade", "swap", "wire", "remit", "remittance", "donate", "donation", "order", "bid",
    "invoice", "odeme", "ode", "havale", "eft", "satin", "sat",
})
# "send" tek başına e-posta/mesaj da olabilir; yalnız para birimiyle birlikte finansaldır
_SEND_TOKENS: frozenset[str] = frozenset({"send", "gonder"})
_MONEY_TOKENS: frozenset[str] = frozenset({
    "money", "funds", "fund", "cash", "crypto", "coin", "coins", "btc", "eth", "usdc", "usdt",
    "sol", "para", "tl", "eur", "usd",
})
# Ödeme/borsa API'leri: bu adreslere veri gönderen komut veya betik para hareketi sayılır
FINANCIAL_API_HOSTS: Tuple[str, ...] = (
    "api.stripe.com", "api.paypal.com", "api-m.paypal.com", "api.coinbase.com",
    "api.exchange.coinbase.com", "api.binance.com", "api.kraken.com", "api.wise.com",
    "api.transferwise.com", "b2b.revolut.com", "api.iyzipay.com", "api.bybit.com",
    "www.okx.com", "api.gemini.com", "api.bitfinex.com",
)
# Blok zinciri gönderim yöntemleri (JSON-RPC ve bitcoin-cli)
_FINANCIAL_RPC_METHODS: Tuple[str, ...] = (
    "eth_sendrawtransaction", "eth_sendtransaction", "sendtoaddress", "sendmany",
    "sendrawtransaction", "walletcreatefundedpsbt",
)
# CLI adı → para hareketi yapan alt komutlar (boş küme: her çağrı finansal)
_FINANCIAL_CLI_COMMANDS: Dict[str, frozenset[str]] = {
    "cast": frozenset({"send", "publish"}),
    "bitcoin-cli": frozenset(_FINANCIAL_RPC_METHODS),
    "solana": frozenset({"transfer", "pay"}),
    "spl-token": frozenset({"transfer"}),
    "stripe": frozenset({"create", "confirm", "capture", "pay", "refund", "post"}),
}
_OPAQUE_SCRIPT_PROGRAMS: frozenset[str] = frozenset({
    "python", "python3", "node", "deno", "bun", "ruby", "perl", "php",
})
_DATA_FLAGS: frozenset[str] = frozenset({"-d", "--json", "-F", "--form", "POST", "PUT", "PATCH", "DELETE"})
_WRITE_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_AFFIRMATIVE_ANSWERS: frozenset[str] = frozenset({
    "evet", "e", "yes", "y", "onay", "onayla", "onayliyorum", "tamam", "ok", "okay", "true", "1",
})


class ApprovalRequest(TypedDict):
    """
    Kullanıcıya sorulacak onay: kategori, host üretimi başlık ve çağrı özeti. url/amount yalnız otomatik onay
    kararını (should_auto_approve) besler; verilmezse güvenli-host/tutar yolu kapalıdır ve çağrı normal onay
    akışına düşer. Ödeme kartı numarası girişi gibi istekler bu alanları BİLEREK taşımaz.
    """
    category: str
    title: str
    summary: str
    url: NotRequired[str]
    amount: NotRequired[float]


class AuditRecord(TypedDict):
    """Onay kararının kalıcı denetim kaydı (sırlar maskelenmiş)."""
    timestamp: str
    category: str
    tool: str
    summary: str
    decision: str


def name_tokens(name: str) -> List[str]:
    """Araç adını camelCase/snake_case/kebab sınırlarından sözcüklere böler. Saf."""
    spaced: str = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    return [token for token in re.split(r"[^a-z0-9]+", ascii_fold(spaced)) if token]


def financial_tool_name(name: str) -> bool:
    """Araç adı para hareketi yapan bir eylem mi? (create_payment, sendMoney, satin_al…) Saf."""
    tokens: frozenset[str] = frozenset(name_tokens(name))
    if tokens & _FINANCIAL_NAME_TOKENS:
        return True
    return bool(tokens & _SEND_TOKENS) and bool(tokens & _MONEY_TOKENS)


def _mentions_financial_endpoint(text: str) -> Optional[str]:
    """Metinde ödeme API adresi veya blok zinciri gönderim yöntemi varsa onu döner. Saf."""
    folded: str = text.casefold()
    for host in FINANCIAL_API_HOSTS:
        if host in folded:
            return host
    for method in _FINANCIAL_RPC_METHODS:
        if method in folded:
            return method
    return None


def _has_shell_expansion(words: List[str]) -> bool:
    """
    Argümanlardan biri değişken/komut ikamesi mi (ör. $PAYMENT_URL, `cmd`). Böyle bir argümana
    veri gönderiliyorsa gerçek hedef bu metinden doğrulanamaz; literal eşleşme arayan
    _mentions_financial_endpoint bunu asla yakalayamaz. Saf.
    """
    return any("$" in word or "`" in word for word in words)


# echo/printf ile üretilip base64 -d/--decode'a borulanan gövdeyi yakalar: opaque yorumlayıcıya
# ("bash -c "$(echo <b64> | base64 -d)"" veya "echo <b64> | base64 -d | bash") gizlenmiş finansal
# içerik böyle çözülüp yeniden taranır.
_BASE64_DECODE_PIPE: re.Pattern[str] = re.compile(
    r"(?:echo|printf)\s+(?:-[a-zA-Z]+\s+)?['\"]?([A-Za-z0-9+/=]{8,})['\"]?\s*\|\s*base64\s+(?:-d|--decode)\b",
)


def _decoded_financial_reason(command: str) -> Optional[str]:
    """Komuttaki base64-çöz borularının içeriğini çözüp finansal içerik arar. Saf."""
    for match in _BASE64_DECODE_PIPE.finditer(command):
        try:
            padded = match.group(1) + "=" * (-len(match.group(1)) % 4)
            decoded = base64.b64decode(padded).decode("utf-8", errors="ignore")
        except (ValueError, base64.binascii.Error):
            continue
        endpoint = _mentions_financial_endpoint(decoded)
        if endpoint is not None:
            return f"base64 ile çözülen içerikte {endpoint} bulundu"
    return None


def _sends_data(words: List[str]) -> bool:
    """HTTP istemci sözcükleri veri gönderen/yazan bir istek mi? (-d, --data*, -X POST, -XPOST…) Saf."""
    for word in words:
        if (word in _DATA_FLAGS or word.startswith("--data") or word.startswith("--form")
                or word.startswith("--post-data") or word.startswith("--post-file") or word.startswith("--body")):
            return True
        if word.startswith("-X") and word[2:].upper() in _WRITE_METHODS:
            return True
        if word.startswith("--request=") and word.split("=", 1)[1].upper() in _WRITE_METHODS:
            return True
    return False


def _command_sends_data(command: str) -> bool:
    """Yorumlayıcı/wrapper içindeki HTTP mutasyon niyetini conservative biçimde tanır."""
    folded = command.casefold()
    return bool(re.search(
        r"(?:\.(?:post|put|patch|delete)\s*\(|"
        r"\bmethod\s*[:=]\s*['\"]?(?:post|put|patch|delete)\b|"
        r"--(?:post-data|post-file|data|form|body)(?:=|\s)|"
        r"-x\s*(?:post|put|patch|delete)\b|\b(?:post|put|patch|delete)\s+https?://)",
        folded,
    ))


def shell_financial_reason(words_by_segment: List[List[str]], command: str) -> Optional[str]:
    """
    Kabuk komutunun para hareketi yapıp yapmadığını söyler: bilinen finansal CLI alt komutu,
    blok zinciri gönderim yöntemi ya da ödeme API adresine veri gönderen istek. Parçalama
    (sudo/env sarmalayıcıları atılmış sözcükler) tools.py'deki kabuk ayrıştırıcısından gelir. Saf.
    """
    endpoint_in_command: Optional[str] = _mentions_financial_endpoint(command)
    if endpoint_in_command is not None and _command_sends_data(command):
        return f"{endpoint_in_command} adresine veri gönderimi"

    decoded_reason: Optional[str] = _decoded_financial_reason(command)
    if decoded_reason is not None:
        return decoded_reason

    for words in words_by_segment:
        if not words:
            continue
        program: str = Path(words[0]).name.casefold()
        if program in _OPAQUE_SCRIPT_PROGRAMS and endpoint_in_command is not None:
            return (
                f"{endpoint_in_command} kullanan opaque {program} betiği; finansal ağ erişimi onay gerektirir"
            )
        subcommands: Optional[frozenset[str]] = _FINANCIAL_CLI_COMMANDS.get(program)
        if subcommands is not None:
            arguments: frozenset[str] = frozenset(word.casefold() for word in words[1:])
            if arguments & subcommands:
                return f"{program} para hareketi komutu"
        if program in ("curl", "wget", "http", "https", "xh"):
            sends_data: bool = _sends_data(words[1:])
            endpoint: Optional[str] = _mentions_financial_endpoint(" ".join(words))
            if endpoint is not None and sends_data:
                return f"{endpoint} adresine veri gönderimi"
            if sends_data and _has_shell_expansion(words[1:]):
                return f"{program} değişken/komut ikamesiyle doğrulanamayan bir adrese veri gönderiyor"
    method: Optional[str] = next(
        (name for name in _FINANCIAL_RPC_METHODS if name in command.casefold()), None,
    )
    return f"{method} çağrısı" if method is not None else None


def script_financial_reason(code: str) -> Optional[str]:
    """JS kodu ödeme API'sine veya blok zinciri gönderimine erişiyor mu? Saf."""
    endpoint: Optional[str] = _mentions_financial_endpoint(code)
    return f"betik {endpoint} kullanıyor" if endpoint is not None else None


# --- Ekranda okunan düğme etiketi (GUI/tarayıcı) ---
CTA_MAX_TOKENS: int = 6
# Bundan uzun etiket düğme etiketi olamaz (6 jetonluk en uzun etiket bile bunun altındadır): sınıflandırılmaz, tarama maliyeti sınırlı
CTA_MAX_CHARS: int = 160
CTA_LEAD_MAX_TOKENS: int = 4
CTA_FUZZY_MIN_RATIO: float = 0.88
CTA_FUZZY_MIN_CHARS: int = 8
AMOUNT_LINES_LIMIT: int = 3
AMOUNT_LINE_CHARS: int = 80
# Etiket temizliğinde atılan jetonlar: bağlaç/belirteç ve para birimi süsü ("Pay $49.99 now" -> "pay")
_CTA_FILLER_TOKENS: frozenset[str] = frozenset("the your my a an it this and ve now simdi nyt securely secure".split())
_CTA_CURRENCY_TOKENS: frozenset[str] = frozenset(
    "tl try usd eur gbp chf jpy cad aud btc eth usdc usdt lira dolar euro dollar dollars euros".split()
)
# Etiketin İLK sözcüğü bunlardan biriyse (kısa etiket) ödeme/alım düğmesi sayılır. _FINANCIAL_NAME_TOKENS'ın
# fiil altkümesidir (Fince maksa/osta/lahjoita hariç; tests/test_approval.py tutarlılığı denetler);
# payment/order/transfer gibi isimler gezinmedir.
_CTA_LEAD_WORDS_COMMON: frozenset[str] = frozenset("pay buy purchase donate withdraw ode".split())
_CTA_LEAD_WORDS_FI: frozenset[str] = frozenset("maksa osta lahjoita".split())
_CTA_LEAD_WORDS: frozenset[str] = _CTA_LEAD_WORDS_COMMON | _CTA_LEAD_WORDS_FI
# Lead fiilinden sonra gelirse etiketin gezinme/isim olduğunu gösteren sözcükler: "Purchase history", "Pay stubs"
_CTA_NAV_TOKENS: frozenset[str] = frozenset(
    "history methods method settings options preferences info information details policy terms help faq "
    "guide attention stub stubs benefits again limit limits rules order orders request requests report "
    "reports receipt receipts summary status list schedule "
    "uudelleen historia asetukset tiedot ehdot ohje".split()
)
# Etiketin TAMAMI bu olduğunda eşleşenler (tek başına belirsiz sözcükler; "Proceed to checkout" eşleşmez)
_CTA_WHOLE_LABELS: frozenset[Tuple[str, ...]] = frozenset(
    tuple(label.split()) for label in ("checkout", "check out", "transfer", "havale")
)
# Kısa etikette ardışık geçen ödeme/sipariş onayı kalıpları (ASCII'ye indirgenmiş, | ayraçlı). Yeni dil = yeni tablo.
_CTA_PHRASES_EN: str = (
    "place order|place bid|place trade|place buy order|place sell order|complete purchase|complete order|"
    "complete payment|complete booking|confirm payment|confirm order|confirm purchase|confirm booking|"
    "confirm pay|confirm transfer|confirm deposit|confirm swap|confirm trade|submit payment|submit order|"
    "make payment|make donation|make transfer|send money|send payment|send funds|transfer money|"
    "transfer funds|deposit funds|wire money|authorize payment|approve payment|approve transfer"
)
_CTA_PHRASES_TR: str = (
    "hemen al|hemen ode|satin al|satin alimi tamamla|satin alimi onayla|odemeyi tamamla|odemeyi onayla|"
    "odemeyi yap|odeme yap|odeme yapin|odemeyi gerceklestir|siparisi onayla|siparisi tamamla|siparisi ver|"
    "siparis ver|bagis yap|bagis yapin|havale yap|havale gonder|eft yap|eft gonder|para gonder|"
    "para transferi|para cek|parayi gonder|transferi onayla|transferi tamamla"
)
_CTA_PHRASES_FI: str = (
    "vahvista tilaus|vahvista maksu|vahvista ostos|tee tilaus|suorita maksu|viimeistele ostos|"
    "viimeistele tilaus|laheta rahaa|siirra rahaa|nosta rahaa|tee lahjoitus"
)
_CTA_PHRASES: Tuple[Tuple[str, ...], ...] = tuple(
    tuple(phrase.split()) for phrase in "|".join((_CTA_PHRASES_EN, _CTA_PHRASES_TR, _CTA_PHRASES_FI)).split("|")
)
# Para tutarı: simge+sayı ("$49.99", "₺249,90") ya da sayı+simge/kod ("49,90 €", "1.249,00 TL"). İkinci kolun
# başlangıcı yalnız sayı öbeğinin (rakam/nokta/virgül dizisi) başlangıcıdır ((?<![0-9.,])): eski '[0-9][0-9.,]*' her
# rakamdan yeniden başlayıp '1' * 20000 girdisinde ikinci dereceden sürüyordu (6,4 sn). Öbek '.' ya da ',' ile başlayabilir
# ('.50 USD'), bu yüzden başındaki ayraçlar '[.,]*' ile yutulur: var/yok kararı eskisiyle aynıdır (eşleşen metin değil).
_AMOUNT_PATTERN: re.Pattern[str] = re.compile(
    "[$€£₺] ?[0-9][0-9.,]*"
    "|(?<![0-9.,])[.,]*[0-9][0-9.,]* ?(?:[$€£₺]|(?:tl|try|usd|eur|gbp)(?![a-z]))"
    "|(?<![a-z])(?:tl|try|usd|eur|gbp) ?[0-9][0-9.,]*",
    re.IGNORECASE,
)
_TOTAL_WORDS: Tuple[str, ...] = ("toplam", "total", "tutar", "amount", "odenecek", "due", "to pay")


class ClickTarget(TypedDict):
    """Tıklanmak üzere çözümlenmiş hedef: onay isteğinde kullanıcıya gösterilen ekran gerçeği."""
    tool: str                 # tıklamayı yapan araç (cua_click_text, cua_click_point, browse_url...)
    label: str                # çözümlenen gerçek etiket (OCR satırı, erişilebilirlik adı, DOM metni)
    requested: Optional[str]  # modelin istediği metin/seçici; nokta ve öğe tıklamasında None
    where: str                # uygulama/pencere ya da sayfa adresi
    amounts: List[str]        # ekrandaki tutar satırları (en çok AMOUNT_LINES_LIMIT)


def _cta_tokens(label: str) -> List[str]:
    """Etiketi ASCII'ye indirip sözcüklere böler; rakam, para birimi ve bağlaç jetonlarını atar. Saf."""
    tokens: List[str] = [token for token in re.split(r"[^a-z0-9]+", ascii_fold(label)) if token]
    return [
        token for token in tokens
        if not token.isdigit() and token not in _CTA_CURRENCY_TOKENS and token not in _CTA_FILLER_TOKENS
    ]


def _contains_run(tokens: List[str], phrase: Tuple[str, ...]) -> bool:
    """Kalıp jetonlar içinde ardışık geçiyor mu? Saf."""
    size: int = len(phrase)
    return any(tuple(tokens[start:start + size]) == phrase for start in range(len(tokens) - size + 1))


def financial_cta_reason(label: str) -> Optional[str]:
    """
    Ekranda okunan/çözümlenen düğme etiketi para hareketini onaylayan bir eylem mi? Kısa etiket (en çok
    CTA_MAX_TOKENS jeton) + TAM sözcük kuralı: (1) tek başına belirsiz sözcükler etiketin tamamıysa,
    (2) ilk sözcük ödeme/alım fiiliyse ve gezinme sözcüğü yoksa, (3) ardışık onay kalıbı varsa, (4) tek
    harflik OCR hatası için kalıba >= CTA_FUZZY_MIN_RATIO benzerse. Genel etiketler ("Onayla", "Gönder",
    "Confirm") ve gezinme ("Payments", "Ödeme yöntemleri") bilerek eşleşmez. Saf.
    """
    if len(label) > CTA_MAX_CHARS:
        return None
    tokens: List[str] = _cta_tokens(label)
    if not tokens or len(tokens) > CTA_MAX_TOKENS:
        return None
    reason: str = "ekranda ödeme/sipariş onayı gibi görünen düğme"
    if tuple(tokens) in _CTA_WHOLE_LABELS:
        return reason
    if (tokens[0] in _CTA_LEAD_WORDS and len(tokens) <= CTA_LEAD_MAX_TOKENS
            and not frozenset(tokens[1:]) & _CTA_NAV_TOKENS):
        return reason
    if any(_contains_run(tokens, phrase) for phrase in _CTA_PHRASES):
        return reason
    joined: str = " ".join(tokens)
    for phrase in _CTA_PHRASES:
        text: str = " ".join(phrase)
        if len(text) >= CTA_FUZZY_MIN_CHARS and SequenceMatcher(None, joined, text).ratio() >= CTA_FUZZY_MIN_RATIO:
            return reason
    return None


def amount_lines(texts: List[str], limit: int) -> List[str]:
    """
    Ekran satırlarından tutar içerenleri (toplam/tutar sözcüklüler önce) en çok limit kadar döner. Tutar, boşlukları
    sadeleştirilip AMOUNT_LINE_CHARS'a kırpılmış satırda aranır: gösterilen satır her zaman tutarı içerir ve arama
    maliyeti satırın uzunluğundan bağımsızdır (browser.confirm_click_target olay döngüsünde çalışır). Saf.
    """
    shown: List[str] = [" ".join(text.split())[:AMOUNT_LINE_CHARS] for text in texts]
    matching: List[str] = [line for line in shown if _AMOUNT_PATTERN.search(line)]
    totals: List[str] = [text for text in matching if any(word in ascii_fold(text) for word in _TOTAL_WORDS)]
    others: List[str] = [text for text in matching if text not in totals]
    return list(dict.fromkeys(totals + others))[:limit]


# Tek başına belirsiz son-onay etiketleri: yalnız aynı ekranda hem tutar hem alıcı/IBAN/transfer sözcüğü varsa
# para hareketi sayılır (banka transferinin son adımı çoğunlukla 'Onayla'/'Gönder'dir). 'Devam'/'Continue'/'Tamam'
# bilerek yok: her ödeme öncesi sayfada bulunurlar ve gereksiz onay yağdırırlardı.
_CTA_GENERIC_LABELS: frozenset[Tuple[str, ...]] = frozenset(
    tuple(label.split()) for label in ("onayla", "gonder", "confirm", "send", "submit", "approve", "onayliyorum", "i confirm")
)
_TRANSFER_CONTEXT_WORDS: frozenset[str] = frozenset(
    "iban swift alici recipient beneficiary payee receiver havale eft transfer transferi".split()
)
_TRANSFER_CONTEXT_PHRASES: Tuple[str, ...] = ("hesap no", "hesap numarasi", "account number")


def is_generic_commit_label(label: str) -> bool:
    """Etiket tek başına belirsiz bir son-onay düğmesi mi ('Onayla', 'Gönder', 'Confirm', 'Send')? Saf."""
    return len(label) <= CTA_MAX_CHARS and tuple(_cta_tokens(label)) in _CTA_GENERIC_LABELS


def financial_context_reason(label: str, context_texts: List[str]) -> Optional[str]:
    """
    Genel son-onay etiketi ('Onayla', 'Gönder', 'Confirm', 'Send') aynı ekranda hem tutar hem alıcı/IBAN/transfer
    sözcüğüyle birlikteyse para hareketi sayar (financial_cta_reason'ın kapsamadığı en tehlikeli boşluk: banka
    transferinin son adımı). Bağlam ekranın/sayfanın görünür satırlarıdır. Bilinen yanlış pozitif: içinde hem tutar
    hem IBAN geçen bir e-postada 'Gönder'. Saf.
    """
    if not is_generic_commit_label(label) or not amount_lines(context_texts, 1):
        return None
    folded: str = " ".join(ascii_fold(text) for text in context_texts)
    words: frozenset[str] = frozenset(re.split(r"[^a-z0-9]+", folded))
    if words & _TRANSFER_CONTEXT_WORDS or any(phrase in folded for phrase in _TRANSFER_CONTEXT_PHRASES):
        return "aynı ekranda tutar ve alıcı/transfer bilgisi var; para transferinin son adımı olabilir"
    return None


def click_financial_reason(label: str, context_texts: List[str]) -> Optional[str]:
    """Tıklanacak etiket para hareketini onaylıyor mu: düğme etiketi tablosu ya da genel etiket + tutar/alıcı bağlamı. Saf."""
    return financial_cta_reason(label) or financial_context_reason(label, context_texts)


# --- Ödeme kartı numarası (PAN) girişi ---
CARD_MIN_DIGITS: int = 13
CARD_MAX_DIGITS: int = 19
# Bir sayı öbeğinde (grup ayraçlı) pencere denenen en çok grup: uzun kimlik/IBAN dizileri sınırsız taranmasın
_CARD_RUN_GROUPS_LIMIT: int = 12
_DIGIT_RUN: re.Pattern[str] = re.compile(r"\d+(?:[ -]\d+)*")


@dataclass(frozen=True)
class CardRange:
    """Ödeme ağının ön ek aralığı (IIN): ilk `prefix_digits` hane [low, high] içindeyse ve uzunluk izinliyse olası kart."""
    low: int
    high: int
    prefix_digits: int
    lengths: Tuple[int, ...]


_LENGTHS_16_TO_19: Tuple[int, ...] = (16, 17, 18, 19)
_LENGTHS_14_TO_19: Tuple[int, ...] = (14, 15, 16, 17, 18, 19)
_LENGTHS_13_TO_19: Tuple[int, ...] = (13, 14, 15, 16, 17, 18, 19)
# Kapı (yanlış pozitifte kullanıcıya onay sorar) BİLEREK dardır: ağ başına kesin IIN aralığı ve uzunluk. Süzgeç
# (core/observation_filter._PAN_ISSUER_PATTERN; yanlış pozitifte yalnız maskeler) daha geniş ön ek kabul eder; iki tablo
# birleştirilmedi. Luhn ise ortaktır: observation_filter.luhn_valid.
_CARD_RANGES: Tuple[CardRange, ...] = (
    CardRange(4, 4, 1, (13, 16, 19)),                                     # Visa
    CardRange(51, 55, 2, (16,)), CardRange(2221, 2720, 4, (16,)),         # Mastercard
    CardRange(34, 34, 2, (15,)), CardRange(37, 37, 2, (15,)),             # American Express
    CardRange(6011, 6011, 4, _LENGTHS_16_TO_19), CardRange(644, 649, 3, _LENGTHS_16_TO_19),
    CardRange(65, 65, 2, _LENGTHS_16_TO_19),                              # Discover
    CardRange(62, 62, 2, _LENGTHS_16_TO_19),                              # UnionPay
    CardRange(300, 305, 3, _LENGTHS_14_TO_19), CardRange(36, 36, 2, _LENGTHS_14_TO_19),
    CardRange(38, 39, 2, _LENGTHS_14_TO_19),                              # Diners Club
    CardRange(3528, 3589, 4, _LENGTHS_16_TO_19),                          # JCB
    CardRange(5018, 5018, 4, _LENGTHS_13_TO_19), CardRange(5020, 5020, 4, _LENGTHS_13_TO_19),
    CardRange(5038, 5038, 4, _LENGTHS_13_TO_19), CardRange(5893, 5893, 4, _LENGTHS_13_TO_19),
    CardRange(6304, 6304, 4, _LENGTHS_13_TO_19), CardRange(6759, 6759, 4, _LENGTHS_13_TO_19),
    CardRange(6761, 6763, 4, _LENGTHS_13_TO_19),                          # Maestro
    CardRange(9792, 9792, 4, (16,)),                                      # Troy (Türkiye)
    CardRange(2200, 2204, 4, _LENGTHS_16_TO_19),                          # Mir
)


def _plausible_card_number(digits: str) -> bool:
    """Uzunluk, Luhn ve bilinen bir ağ ön eki (IIN) birlikte tutuyor mu? Saf."""
    if not CARD_MIN_DIGITS <= len(digits) <= CARD_MAX_DIGITS or not luhn_valid(digits):
        return False
    return any(
        len(digits) in card_range.lengths
        and card_range.low <= int(digits[:card_range.prefix_digits]) <= card_range.high
        for card_range in _CARD_RANGES
    )


def card_number_in_text(text: str) -> Optional[str]:
    """
    Metinde ödeme kartı numarası (13-19 hane, Luhn + ağ ön eki tutan; boşluk/tire gruplu olabilir) varsa
    MASKELİ halini ('•••• 4242') döner, yoksa None. Sayı öbeğinin ardışık grup pencereleri denenir:
    'kart 4242 4242 4242 4242 123' içinde de bulunur; IBAN/telefon/sipariş numarası ön ek ve Luhn'da elenir. Saf.
    """
    for run in _DIGIT_RUN.finditer(text):
        groups: List[str] = re.split(r"[ -]", run.group())[:_CARD_RUN_GROUPS_LIMIT]
        for start in range(len(groups)):
            digits: str = ""
            for group in groups[start:]:
                digits += group
                if len(digits) > CARD_MAX_DIGITS:
                    break
                if _plausible_card_number(digits):
                    return f"•••• {digits[-4:]}"
    return None


def typed_card_number(tool: str, arguments: Dict[str, object]) -> Optional[str]:
    """Çağrının yazacağı metinlerde ödeme kartı numarası varsa maskeli halini döner (bkz. card_number_in_text). Saf."""
    return next(
        (masked for masked in (card_number_in_text(text) for text in typed_texts(tool, arguments)) if masked is not None),
        None,
    )


# --- Geri alınamaz dış iletişim (entegrasyon/MCP araçları): dış iletişim fiili var, önde okuma fiili yok ---
_OUTBOUND_VERB_TOKENS: frozenset[str] = frozenset(
    "send post publish reply forward share tweet broadcast comment "
    "gonder yayinla yanitla cevapla paylas duyur yorum".split()
)
# Salt okuma FİİLLERİ. Nesne adları (status, balance, details, info, summary, report, history) BİLEREK yok: adda geçmeleri
# eylemi okunur göstermez ('send_report', 'post_status', 'withdraw_balance', 'pay_invoice_summary' eskiden muaf sayılıyordu).
_READ_VERB_TOKENS: frozenset[str] = frozenset(
    "get list search read fetch find query count describe show view check lookup resolve "
    "listele oku ara getir sorgula goster".split()
)
# Adın eylemini belirleyen sözcükler: okuma, dış iletişim ('send'/'gonder' dahil) ve para hareketi fiilleri
_ACTION_TOKENS: frozenset[str] = _READ_VERB_TOKENS | _OUTBOUND_VERB_TOKENS | _FINANCIAL_NAME_TOKENS
# Onay özetinde kırpma sonrası bile görünmesi gereken alanlar (alıcı/tutar/adres önce)
_SUMMARY_PRIORITY_KEYS: Tuple[str, ...] = (
    "to", "recipient", "recipients", "receiver", "channel", "chat_id", "thread", "subject", "title",
    "amount", "currency", "account", "iban", "address", "url", "command", "code", "selector", "text", "label",
)


def _leads_with_read_verb(tokens: List[str]) -> bool:
    """
    Adın İLK eylem sözcüğü salt okuma fiili mi ('get_order', 'list_posts', 'slack_get_post')? Eylem sözcüğü, okuma/dış
    iletişim/para hareketi jetonlarından soldan ilk geçendir: ad öneki ('slack') atlanır, nesne adları ('report',
    'balance') fiil sayılmaz; 'send_report' ve 'transfer_status' okuma fiiliyle başlamaz. Saf.
    """
    first_action: Optional[str] = next((token for token in tokens if token in _ACTION_TOKENS), None)
    return first_action is not None and first_action in _READ_VERB_TOKENS


def outbound_tool_name(name: str) -> bool:
    """
    Araç adı geri alınamaz dış iletişim eylemi mi? (send_message, slack_post_message, send_report; get_post değil)
    Okuma muafiyeti yalnız adın okuma FİİLİYLE başlaması ile verilir (bkz. _leads_with_read_verb). Saf.
    """
    tokens: List[str] = name_tokens(name)
    return bool(frozenset(tokens) & _OUTBOUND_VERB_TOKENS) and not _leads_with_read_verb(tokens)


def readonly_conflict(name: str) -> Optional[str]:
    """
    Katalog araca salt okunur demiş olsa da adı para hareketi/dış iletişim bildiriyorsa nedenini döner. Okuma
    muafiyeti yalnız adın okuma fiiliyle başlamasıdır (get_order, list_*, search_*; bkz. _leads_with_read_verb):
    'transfer_status', 'withdraw_balance' ya da 'send_report' çakışmadır. Kayıt anında açık hata için. Saf.
    """
    if _leads_with_read_verb(name_tokens(name)):
        return None
    if financial_tool_name(name):
        return "para hareketi"
    if outbound_tool_name(name):
        return "geri alınamaz dış iletişim"
    return None


# --- Onay özeti ---

def _clip_head_tail(text: str, limit: int) -> str:
    """Uzun metinde ilk 2/3'ü ve son 1/3'ü korur, ortayı atar (komutun sonundaki hedef adres kaybolmasın). Saf."""
    if len(text) <= limit:
        return text
    marker: str = " … [orta kısım atlandı] … "
    room: int = limit - len(marker)
    head: int = room * 2 // 3
    return text[:head] + marker + text[len(text) - (room - head):]


def _priority_ordered(arguments: Dict[str, object]) -> Dict[str, object]:
    """Alıcı/tutar/adres alanlarını başa alır, kalanı ada göre dizer. Saf."""
    first: Dict[str, object] = {key: arguments[key] for key in _SUMMARY_PRIORITY_KEYS if key in arguments}
    return {**first, **{key: arguments[key] for key in sorted(arguments) if key not in first}}


def call_summary(tool: str, arguments: Dict[str, object]) -> str:
    """Onay penceresi ve denetim kaydı için maskelenmiş, alan-öncelikli, baş+son kırpılmış çağrı özeti."""
    rendered: str = json.dumps(_priority_ordered(arguments), ensure_ascii=False)
    return _clip_head_tail(redact(f"{tool} {rendered}"), APPROVAL_SUMMARY_LIMIT)


def _financial_title(reason: str) -> str:
    """Finansal onay başlığı (host üretir; sayfa/model metni içermez). Saf."""
    return (
        f"FİNANSAL İŞLEM ONAYI: ajan para hareketi olabilecek bir işlem yapmak istiyor ({reason}). "
        "Tutarı, alıcıyı ve hesabı kontrol edin. Onaylamazsanız işlem yapılmaz."
    )


def _numeric_amount(value: object) -> float:
    """
    Argümandaki tutarı karşılaştırılabilir sayıya çevirir; çevrilemeyen/güvenilmeyen değerde 0.0 döner
    (0.0 = otomatik onay eşiğinin altında sayılmaz, yani fail-closed). Saf.
    """
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip().replace(",", "."))
        except ValueError:
            return 0.0
    return 0.0


def financial_request(tool: str, arguments: Dict[str, object], reason: str) -> ApprovalRequest:
    """Para hareketi onay isteği kurar; tutar ve adres otomatik onay kararını beslemek için taşınır. Saf (maskeleme dışında)."""
    return {
        "category": "financial", "title": _financial_title(reason), "summary": call_summary(tool, arguments),
        "url": str(arguments.get("url") or ""), "amount": _numeric_amount(arguments.get("amount")),
    }


def card_entry_request(tool: str, masked_number: str) -> ApprovalRequest:
    """Ödeme kartı numarası yazma onayı: özet ve denetim kaydı numaranın yalnız son dört hanesini taşır. Saf."""
    return {
        "category": "financial",
        "title": _financial_title("ödeme kartı numarası yazılıyor"),
        "summary": f"{tool} ödeme kartı numarasını alana yazacak: {masked_number}",
    }


def gui_click_request(target: ClickTarget, reason: str) -> ApprovalRequest:
    """
    Ekranda tıklanacak ödeme/sipariş düğmesi için onay isteği: çözümlenen gerçek etiket önce gelir. Etiket, istek,
    yer ve tutarlar sayfa/model kaynaklıdır: yalnız özette, repr ile (satır sonu/biçim taklidi kaçırılır) gösterilir;
    başlık sabittir. Saf (maskeleme dışında).
    """
    lines: List[str] = [f"Tıklanacak düğme (ekranda okunan): {target['label']!r}"]
    requested: Optional[str] = target["requested"]
    if requested is not None and ascii_fold(requested) != ascii_fold(target["label"]):
        lines.append(f"Modelin istediği: {requested!r}")
    lines.append(f"Yer: {target['where']!r} · araç: {target['tool']}")
    if target["amounts"]:
        lines.append("Ekrandaki tutar: " + " | ".join(repr(amount) for amount in target["amounts"]))
    where: str = target["where"]
    return {
        "category": "financial",
        "title": _financial_title(reason),
        "summary": _clip_head_tail(redact("\n".join(lines)), APPROVAL_SUMMARY_LIMIT),
        # Güvenli host listesi yalnız gerçek adresler için uygulanır; GUI'de 'where' uygulama adıdır.
        "url": where if where.startswith(("http://", "https://")) else "",
    }


def outbound_request(tool: str, arguments: Dict[str, object], reason: str) -> ApprovalRequest:
    """Geri alınamaz dış iletişim (mesaj/e-posta gönderme, yayınlama, yanıtlama) onay isteği. Saf (maskeleme dışında)."""
    return {
        "category": "communication",
        "title": (
            f"DIŞ İLETİŞİM ONAYI: ajan geri alınamaz bir dış iletişim yapmak istiyor ({reason}). "
            "Alıcıyı ve içeriği kontrol edin. Onaylamazsanız gönderilmez."
        ),
        "summary": call_summary(tool, arguments),
    }


def communication_click_label(label: str) -> bool:
    """Kısa yayın/gönderim CTA'ları; besteleyici açma ve gezinme etiketleri hariç."""
    normalized = " ".join(ascii_fold(label).strip().split()).rstrip(".!…")
    return normalized in {
        "gonder", "gonderi yayinla", "yayinla", "paylas", "yanitla", "cevapla", "tweetle",
        "send", "send message", "send reply", "post", "post all", "publish", "publish post",
        "reply", "tweet", "share", "share now",
    }


def gui_communication_request(tool: str, label: str, where: str, draft: str) -> ApprovalRequest:
    request = outbound_request(tool, {"hedef": label, "uygulama": where}, "yayınlama/gönderme")
    request["summary"] = redact(
        f"Uygulama/sayfa: {where}\nDüğme: {label}\n"
        + (f"Son yazılan taslak (ekrandan kontrol edin):\n{draft}" if draft else
           "Taslak metni araç kaydında yok. İçeriği ve alıcıyı ekranda kontrol edin."),
    )
    return request


# Veri kökündeki güvenlik ilkesini, denetim kaydını, kalıcı hafızayı (kullanıcı tercihleri, deneyim dersleri, epizot
# geçmişi) ve zamanlanmış görevleri belirleyen dosyalar: ajanın kendi dosya/kabuk araçlarıyla yazması yedek sağlayıcı
# iznini, hafıza onayını ve denetim izini atlatırdı. Adlar küçük harfle karşılaştırılır (macOS dosya sistemi büyük/küçük
# harfe duyarsızdır); hedefin veri kökünde olup olmadığı yazıma değil kimliğe bakılarak denetlenir (tool_execution).
PROTECTED_DATA_FILES: frozenset[str] = frozenset({
    "provider_fallback.json", "audit.jsonl", "telegram.json", "continuous_limits.json",
    "user_memory.json", "schedules.json", "catalog.json", "experience_memory.json", "cognitive_memory.json",
    # iMessage kanalı: eşleşme, eşleştirme isteği, karakter, görev geçmişi ve yol arkadaşı deposu
    "imessage.json", "imessage-pairing.json", "persona.md", "imessage-history.json",
    "companion.db", "companion.db-wal", "companion.db-shm",
})


def config_write_request(tool: str, file_name: str) -> ApprovalRequest:
    """Ajan araçlarıyla güvenlik/ilke yapılandırma dosyasına yazma onay isteği. Saf."""
    return {
        "category": "configuration",
        "title": (
            f"YAPILANDIRMA DEĞİŞİKLİĞİ ONAYI: ajan '{file_name}' dosyasını değiştirmek istiyor. Bu dosya güvenlik "
            "ilkesini, denetim kaydını, kalıcı hafızayı veya zamanlanmış görevleri belirler. Ne yazıldığını "
            "bilmiyorsanız onaylamayın."
        ),
        "summary": f"{tool} -> {file_name}",
    }


def memory_request(arguments: Dict[str, object]) -> ApprovalRequest:
    """Kullanıcının açıkça istemediği kalıcı hafıza değişikliği için onay isteği. Saf (maskeleme dışında)."""
    action: str = str(arguments.get("action", "")).strip().casefold()
    key: str = str(arguments.get("key") or "")
    if action == "forget":
        title: str = f"Kalıcı hafızadan silinsin mi? Anahtar: {key}"
    else:
        category: str = str(arguments.get("category") or "preference")
        title = f"Kalıcı hafızaya kaydedilsin mi? [{category}] {key}: {arguments.get('value') or ''}"
    return {"category": "memory", "title": _clip_head_tail(redact(title), APPROVAL_SUMMARY_LIMIT),
            "summary": call_summary("user_memory", arguments)}


def approval_fields(request: ApprovalRequest) -> Dict[str, object]:
    """Arayüz/Telegram soru alanları: tek onay kutusu ve çağrı özeti. Saf."""
    return {
        APPROVAL_FIELD: {"type": "boolean", "label": "Onaylıyorum", "default": False},
        "_help": "İşlem: " + request["summary"],
    }


def approval_granted(value: object) -> bool:
    """Onay kutusu (bool) veya Telegram metni ('evet', 'onaylıyorum'…) olumlu mu? Saf."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return ascii_fold(value.strip()).rstrip(".!") in _AFFIRMATIVE_ANSWERS
    return False


def audit_record(tool: str, request: ApprovalRequest, decision: str, timestamp: str) -> AuditRecord:
    """Denetim kaydı satırını kurar. Saf."""
    return {"timestamp": timestamp, "category": request["category"], "tool": tool,
            "summary": request["summary"], "decision": decision}


def append_audit(path: Path, record: AuditRecord) -> None:
    """
    Kaydı yalnız eklenen JSONL denetim dosyasına 0600 izinle yazar ve diske zorlar. Denetim
    kaydı yazılamazsa hata yükselir: onaylı finansal işlem kayıtsız yürütülmemelidir.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor: int = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(descriptor, (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


# === BYPASS ONAY AYARLARI: Ajan serbest, sürekli modda otomatik onay ===

# Sürekli modda (sürekli çalışan görevler) otomatik onay verme limiti.
# Bu tutarın altındaki işlemler onaysız geçer.
AUTO_APPROVE_LIMIT: float = 0.0  # 0.0 = tüm tutarlar onay ister; >0 ise bu kadar altı onaysız geçer

# Güvenli site whitelist: bu listedeki alan adlarındaki her türlü etkileşim (ödemeler dahil) onaysız geçer.
# Bot korumaları atlanır ve ödeme/satın alma işlemleri de dahil her şey serbesttir.
AUTO_APPROVE_HOSTS: frozenset[str] = frozenset()  # boş = whitelist yok; dolu = bu hostlar serbest

# Sürekli modda otomatik onay verilsin mi?
AUTO_APPROVE_IN_CONTINUOUS_MODE: bool = False


def should_auto_approve(url: str, amount: Optional[float] = None) -> bool:
    """
    Verilen adres ve tutar için güvenli-host/tutar yoluyla otomatik onay uygulanmalı mı? Saf.

    Kurallar:
    - Adresin ana makinesi AUTO_APPROVE_HOSTS'ta ise → onayla (her tutar).
    - Tutar AUTO_APPROVE_LIMIT'ten küçük/eşitse ve limit > 0 ise → onayla.
    Aksi hâlde (ve iki ayar da varsayılan olarak boş/0 olduğu için) False döner ve çağrı normal onay akışına
    düşer. Sürekli mod kuralı burada DEĞİL, app/tool_execution.require_approval içinde uygulanır.
    """
    from urllib.parse import urlsplit
    host: str = urlsplit(url).hostname.rstrip(".").removeprefix("www.") if url else ""
    if host and host in AUTO_APPROVE_HOSTS:
        return True
    if amount is not None and AUTO_APPROVE_LIMIT > 0 and amount <= AUTO_APPROVE_LIMIT:
        return True
    return False
