"""
İnsan onayı gerektiren araç çağrılarını sınıflandırır ve onay kararlarını denetim kaydına yazar.

Para hareketi (ödeme, transfer, alım-satım, blok zinciri gönderimi) ve kullanıcının açıkça
istemediği kalıcı hafıza değişikliği, çağrı çalışmadan önce host tarafından kullanıcıya
sorulur. Soru metnini model değil host üretir: model onay metnini yönlendiremez. Etkileşimli
kanal (arayüz/Telegram) yoksa çağrı reddedilir. Sınıflandırma en iyi çaba korumasıdır;
güvenlik sınırı değildir: bilinen finansal CLI, API adresi, RPC yöntemi ve araç adı kalıplarını
yakalar. Grafik arayüzdeki ödeme adımları sistem istemindeki `ask_user` kuralıyla korunur.
"""
import json
import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple, TypedDict

from omniagent.config import redact
from omniagent.core.state import ascii_fold

# Onay penceresinde gösterilecek çağrı özetinin üst sınırı
APPROVAL_SUMMARY_LIMIT: int = 600
# Kullanıcı yanıtı bu süre içinde gelmezse eylem onaylanmamış sayılır (görev sonsuza dek kilitlenmesin)
APPROVAL_TIMEOUT_SECONDS: float = 900.0
APPROVAL_FIELD: str = "onay"

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
    """Kullanıcıya sorulacak onay: kategori, host üretimi başlık ve çağrı özeti."""
    category: str
    title: str
    summary: str


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
            endpoint: Optional[str] = _mentions_financial_endpoint(" ".join(words))
            if endpoint is not None and _sends_data(words[1:]):
                return f"{endpoint} adresine veri gönderimi"
    method: Optional[str] = next(
        (name for name in _FINANCIAL_RPC_METHODS if name in command.casefold()), None,
    )
    return f"{method} çağrısı" if method is not None else None


def script_financial_reason(code: str) -> Optional[str]:
    """JS kodu ödeme API'sine veya blok zinciri gönderimine erişiyor mu? Saf."""
    endpoint: Optional[str] = _mentions_financial_endpoint(code)
    return f"betik {endpoint} kullanıyor" if endpoint is not None else None


def _clip(text: str, limit: int) -> str:
    """Özet metnini sınırlar. Saf."""
    return text if len(text) <= limit else text[:limit] + " …"


def call_summary(tool: str, arguments: Dict[str, object]) -> str:
    """Onay penceresi ve denetim kaydı için sırları maskelenmiş kısa çağrı özeti."""
    rendered: str = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
    return redact(_clip(f"{tool} {rendered}", APPROVAL_SUMMARY_LIMIT))


def financial_request(tool: str, arguments: Dict[str, object], reason: str) -> ApprovalRequest:
    """Para hareketi onay isteği kurar. Saf (maskeleme dışında)."""
    return {
        "category": "financial",
        "title": (
            f"FİNANSAL İŞLEM ONAYI: ajan para hareketi olabilecek bir işlem yapmak istiyor ({reason}). "
            "Tutarı, alıcıyı ve hesabı kontrol edin. Onaylamazsanız işlem yapılmaz."
        ),
        "summary": call_summary(tool, arguments),
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
    return {"category": "memory", "title": redact(_clip(title, APPROVAL_SUMMARY_LIMIT)),
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
