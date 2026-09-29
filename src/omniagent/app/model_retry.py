"""
Model çağrısı hata sınıflandırması ve yeniden deneme bekleme politikası. Sınıflandırma ve karar saf
fonksiyondur (yalnız hata iletisi redakte edilirken sır deposu okunur, kullanılamayan Retry-After başlığı
loglanır); bekleme, olay yayını ve profil geçişi app/agent.py içindeki _call_model_with_retries'tadır.
"""
from __future__ import annotations

import logging
import math
import re
import ssl
import zlib
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Dict, Literal, Mapping, Optional, Tuple

from openai import APIConnectionError, APIError, APITimeoutError

from omniagent.config import redact

# access: erişim/bakiye (401/402/403 ve kota/erişim kodları); profil engellenebilir. permanent: geçersiz istek.
# retry_refused: sunucu x-should-retry: false dedi (erişim değil, profil engellenmez). certificate: TLS sertifika
# doğrulaması başarısız (bekleyerek düzelmez, profil engellenmez).
ModelErrorKind = Literal["transient", "timeout", "rate_limited", "access", "permanent", "retry_refused", "certificate"]
RetryAction = Literal["retry", "switch", "raise"]

# Bir model çağrısının yeniden denemeye ayırabileceği en uzun süre (sn); ayrıca görevin kalan duvar
# saatiyle sınırlanır (bkz. model_retry_deadline). Etkileşimli: kullanıcı ekran başında, Durdur çalışır
# (arayüz, CLI, benchmark ve agent dışı çağrılar). Uzak: kullanıcı başında değil (Telegram). Sürekli veya
# gözetimsiz görevde bütçe kalan görev süresidir; ama tek bir model çağrısı bunun için bile en çok
# UNATTENDED_MODEL_RETRY_CAP_SECONDS bekler, aşılırsa açık hata (ModelCallFailed) yükselir.
INTERACTIVE_MODEL_RETRY_SECONDS: float = 60.0
REMOTE_MODEL_RETRY_SECONDS: float = 300.0
UNATTENDED_MODEL_RETRY_CAP_SECONDS: float = 1800.0
# Üstel geri çekilme: taban * 2^n sn, en çok 30 sn; jitter beklemeyi en çok %25 kısaltır.
BACKOFF_BASE_SECONDS: float = 0.5
RATE_LIMIT_BACKOFF_BASE_SECONDS: float = 5.0
BACKOFF_CAP_SECONDS: float = 30.0
BACKOFF_MAX_EXPONENT: int = 10
JITTER_FRACTION: float = 0.25
# Sunucu bekleme süresi verdiyse tam o süre artı saat farkı payı beklenir.
RETRY_AFTER_MARGIN_SECONDS: float = 0.5
# Süre verilmeyen 429'da alternatif profil varken etkin profilin atlanacağı süre.
RATE_LIMIT_COOLDOWN_SECONDS: float = 30.0
# Bundan kısa beklemeler arayüzde durum satırı üretmez (yalnız günlük).
STATUS_MIN_WAIT_SECONDS: float = 2.0
DETAIL_LIMIT: int = 500

# Sağlayıcı hata kodu/tipi (küçük harf). Listeler kasıtlı kısa tutuldu ve sağlayıcı belgeleriyle canlı
# doğrulanmadı; tanınmayan akış içi hata geçici sayılır (bkz. _kind_of). Yeni kod görüldüğünde yalnız
# burası genişletilir.
_ACCESS_LABELS: frozenset[str] = frozenset({
    "insufficient_quota", "billing_hard_limit_reached", "invalid_api_key", "authentication_error",
    "permission_error", "permission_denied", "payment_required", "insufficient_credits",
})
_PERMANENT_LABELS: frozenset[str] = frozenset({
    "invalid_request_error", "context_length_exceeded", "string_above_max_length", "invalid_prompt",
    "content_policy_violation", "model_not_found", "not_found_error", "request_too_large",
})
_RATE_LIMIT_LABELS: frozenset[str] = frozenset({
    "rate_limit_exceeded", "rate_limit_error", "rate_limit", "too_many_requests", "resource_exhausted",
})
_ACCESS_STATUSES: frozenset[int] = frozenset({401, 402, 403})
# HTTP durumu olarak yeniden denenebilen 4xx'ler; diğer 4xx kalıcıdır.
_RETRYABLE_CLIENT_STATUSES: frozenset[int] = frozenset({408, 409, 425, 429})
_CONTEXT_TEXT: re.Pattern[str] = re.compile(
    r"context.{0,20}(?:length|window)|maximum context|prompt is too long|too many tokens", re.IGNORECASE,
)
_RATE_LIMIT_TEXT: re.Pattern[str] = re.compile(
    r"rate.?limit|too many requests|usage limit|\b429\b", re.IGNORECASE,
)
MODEL_ERROR_KIND_LABELS: Dict[ModelErrorKind, str] = {
    "transient": "geçici hata", "timeout": "zaman aşımı", "rate_limited": "hız sınırı",
    "access": "erişim veya bakiye hatası", "permanent": "geçersiz istek",
    "retry_refused": "sunucu yeniden denemeyi reddetti", "certificate": "TLS sertifika doğrulaması başarısız",
}
# Yeniden denenmeyen ve profili engellemeyen türler: alternatif profil olsa da hata hemen yükselir (yedeğe geçilmez;
# bu yüzden hata iletisi 'yedek izni ver' önermez, bkz. app/agent.py _call_model_with_retries).
UNRETRYABLE_KINDS: frozenset[ModelErrorKind] = frozenset({"permanent", "retry_refused", "certificate"})
# İstisna zincirinde en çok kaç halka izlenir (döngü ve aşırı uzun zincire karşı).
_CAUSE_CHAIN_LIMIT: int = 16


@dataclass(frozen=True)
class ModelErrorInfo:
    """Bir model çağrısı hatasının yeniden deneme kararı için gereken özeti."""

    kind: ModelErrorKind
    status_code: Optional[int]    # HTTP durumu; akış içi hatada gövdedeki sayısal kod ("429", "502")
    code: Optional[str]           # sağlayıcı hata kodu (küçük harf)
    retry_after: Optional[float]  # sunucunun istediği bekleme (sn)
    in_stream: bool               # HTTP 200 sonrası akış içi hata mı
    cause_type: Optional[str]     # bağlantı hatasının en derin nedeninin türü (ör. SSLCertVerificationError)
    detail: str                   # redakte edilmiş, kısaltılmış hata özeti


@dataclass(frozen=True)
class RetryDecision:
    """Hata sonrası ne yapılacağı: yeniden dene, başka profile geç ya da hatayı yükselt."""

    action: RetryAction
    wait_seconds: float      # yalnız aynı profile dönüşte / plan turu sonunda beklenir
    cooldown_seconds: float  # > 0 ise etkin profil bu süre atlanır (hız sınırı)
    block_backend: bool      # True ise etkin profil görev boyu engellenir (erişim/bakiye)


class ModelCallFailed(Exception):
    """Model çağrısı, yeniden deneme bütçesi tükendiği ya da hata kalıcı olduğu için sonuçsuz kaldı."""

    def __init__(
        self, summary: str, *, backend: str, kind: ModelErrorKind, attempts: int, waited_seconds: float,
    ) -> None:
        super().__init__(summary)
        self.backend = backend
        self.kind = kind
        self.attempts = attempts
        self.waited_seconds = waited_seconds


def _finite_seconds(text: str, divisor: float) -> Optional[float]:
    """Sayısal başlık değerini saniyeye çevirir; sayı değilse, sonsuzsa veya <= 0 ise None. Saf."""
    try:
        value: float = float(text) / divisor
    except ValueError:
        return None
    return value if math.isfinite(value) and value > 0.0 else None


def _warn_unusable_retry_after(header: str, value: str) -> None:
    """Sunucunun gönderdiği ama bekleme süresi olarak kullanılamayan başlığı yapısal alanlarla uyarır."""
    logging.warning(
        "Retry-After başlığı kullanılabilir bir bekleme süresi vermiyor; yok sayıldı",
        extra={"header": header, "value": value[:64]},
    )


def retry_after_seconds(headers: Mapping[str, str], now: datetime) -> Optional[float]:
    """
    Sunucunun istediği bekleme (sn): önce retry-after-ms, sonra retry-after (saniye ya da HTTP tarihi).
    Başlık yoksa, bozuksa, geçmiş tarihse ya da 0 ise None: çağıran üstel geri çekilmeyi kullanır (sıcak
    döngü olmaz). Gönderilmiş ama kullanılamayan (sayı/tarih olmayan, sonsuz, <= 0) başlık uyarıyla loglanır;
    biçimi geçerli geçmiş tarih uyarı üretmez. Saat farkı için `now` açıkça verilir.
    """
    milliseconds: Optional[str] = headers.get("retry-after-ms")
    if milliseconds is not None:
        parsed: Optional[float] = _finite_seconds(milliseconds, 1000.0)
        if parsed is not None:
            return parsed
        _warn_unusable_retry_after("retry-after-ms", milliseconds)
    raw: Optional[str] = headers.get("retry-after")
    if raw is None:
        return None
    seconds: Optional[float] = _finite_seconds(raw, 1.0)
    if seconds is not None:
        return seconds
    try:
        delta: float = (parsedate_to_datetime(raw) - now).total_seconds()
    except (TypeError, ValueError, OverflowError):
        _warn_unusable_retry_after("retry-after", raw)
        return None
    return delta if delta > 0.0 else None


def _label(value: object) -> Optional[str]:
    """Sağlayıcı kod/tip alanını karşılaştırma için küçük harfe çevirir; metin değilse veya boşsa None. Saf."""
    return value.strip().casefold() if isinstance(value, str) and value.strip() else None


def _status_of(error: BaseException, code: Optional[str]) -> Optional[int]:
    """HTTP durumu; akış içi hatada (HTTP 200 sonrası) gövdedeki sayısal kod durum sayılır. Saf."""
    status: object = getattr(error, "status_code", None)
    if isinstance(status, int):
        return status
    if code is not None and code.isdigit() and 100 <= int(code) <= 599:
        return int(code)
    return None


def _error_text(error: BaseException) -> str:
    """
    Hata iletisi. Akış içi hata olayında gövde metin ise ({"error": "hız sınırı"}) SDK genel bir ileti
    üretir ve gerçek metin yalnız gövdededir; ikisi birlikte döner. Saf.
    """
    message: str = str(error).strip()
    body: object = getattr(error, "body", None)
    if isinstance(body, str) and body.strip() and body.strip() not in message:
        return f"{message} | {body.strip()}"
    return message


def _exception_chain(error: BaseException) -> Tuple[BaseException, ...]:
    """
    error ve onu doğuran istisnalar, en yakından en derine: her halkanın __cause__'u, yoksa __context__'i izlenir.
    httpcore2 sarmalayıcıları nedeni `from None` ile bastırıp yalnız __context__ bırakır (ör. TLS sertifika reddi
    ssl.SSLCertVerificationError olarak yalnız orada durur). Döngüye ve aşırı uzunluğa karşı sınırlıdır. Saf.
    """
    chain: list[BaseException] = []
    current: Optional[BaseException] = error
    while current is not None and len(chain) < _CAUSE_CHAIN_LIMIT and not any(current is seen for seen in chain):
        chain.append(current)
        current = current.__cause__ if current.__cause__ is not None else current.__context__
    return tuple(chain)


def _connection_causes(error: BaseException, chain: Tuple[BaseException, ...]) -> Tuple[Exception, ...]:
    """
    Bağlantı hatasının (APIConnectionError, APITimeoutError dahil) nedenleri, yakından derine. Yalnız Exception
    halkaları alınır (anyio zaman aşımı zincirinin sonundaki asyncio.CancelledError gürültüdür). Yanıt alınmış
    durum hatalarında bastırılmış HTTPStatusError bağlamı bir neden değildir: onlar için boş döner. Saf.
    """
    if not isinstance(error, APIConnectionError):
        return ()
    return tuple(item for item in chain[1:] if isinstance(item, Exception))


def _kind_of(
    error: BaseException, status: Optional[int], labels: set[str], should_retry: Optional[str], message: str,
    certificate_failure: bool,
) -> ModelErrorKind:
    """Hatayı yeniden deneme sınıfına indirger; öncelik sırası classify_model_error açıklamasındadır. Saf."""
    if isinstance(error, APITimeoutError):
        return "timeout"
    if isinstance(error, (APIConnectionError, ssl.SSLError)):
        # Sertifika doğrulaması (bilinmeyen CA, süresi dolmuş, ana bilgisayar adı uyuşmuyor) beklemekle düzelmez
        return "certificate" if certificate_failure else "transient"
    if labels & _ACCESS_LABELS or status in _ACCESS_STATUSES:
        return "access"
    if labels & _PERMANENT_LABELS or (
        status is not None and status < 500 and status not in _RETRYABLE_CLIENT_STATUSES
    ):
        return "permanent"
    # SDK'nın kendi kuralı: sunucu yeniden denemeyi açıkça yasakladıysa (x-should-retry: false) beklenmez. Bu
    # erişim/bakiye değildir (o yukarıda 401/402/403 ve kodlarla ayrılır): profil engellenmez, etiket dürüst kalır.
    if should_retry == "false" and status is not None:
        return "retry_refused"
    if status == 429 or labels & _RATE_LIMIT_LABELS:
        return "rate_limited"
    if status is not None:
        return "transient"
    if _RATE_LIMIT_TEXT.search(message):
        return "rate_limited"
    if _CONTEXT_TEXT.search(message):
        return "permanent"
    # Tanınmayan akış içi hata: kalıcı doğrulama hataları normalde HTTP 4xx ile akıştan ÖNCE gelir; sınırlı
    # yeniden deneme + günlük uyarısı, sonra son hata yükselir.
    return "transient"


def _detail(
    error: BaseException, status: Optional[int], code: Optional[str], message: str, causes: Tuple[Exception, ...],
) -> str:
    """
    Teşhis için redakte edilmiş, kısaltılmış hata özeti: tür, durum, kod, istek kimliği, ileti ve bağlantı
    hatasının neden zinciri ("neden: ConnectError → SSLCertVerificationError: ileti", ileti en derin nedenin).
    Saf değil: sır deposunu okur.
    """
    parts: list[str] = [type(error).__name__]
    if status is not None:
        parts.append(f"durum {status}")
    if code is not None and not code.isdigit():
        parts.append(code)
    request_id: object = getattr(error, "request_id", None)
    if isinstance(request_id, str):
        parts.append(f"istek={request_id}")
    summary: str = f"{' '.join(parts)}: {message or '(ileti yok)'}"
    if causes:
        names: str = " → ".join(type(item).__name__ for item in causes)
        root_text: str = str(causes[-1]).strip()
        summary += f" | neden: {names}: {root_text}" if root_text else f" | neden: {names}"
    return redact(summary)[:DETAIL_LIMIT]


def classify_model_error(error: BaseException, now: datetime) -> ModelErrorInfo:
    """
    APIStatusError, APIConnectionError/APITimeoutError, akış içi düz APIError ve ssl.SSLError'ı yeniden
    deneme sınıfına çevirir. Öncelik: zaman aşımı > bağlantı (zincirinde ssl.SSLCertVerificationError varsa
    yeniden denenmeyen 'certificate', yoksa geçici) > erişim (kod/tip veya 401/402/403; 429 +
    insufficient_quota dahil) > kalıcı (kod/tip veya yeniden denenemeyen 4xx) > sunucu yeniden denemeyi
    reddetti (x-should-retry: false) > hız sınırı > geçici. Saat `now` ile verilir; ayrıca ileti redakte
    edilirken sır deposu okunur.
    """
    code: Optional[str] = _label(getattr(error, "code", None))
    error_type: Optional[str] = _label(getattr(error, "type", None))
    labels: set[str] = {label for label in (code, error_type) if label is not None}
    status: Optional[int] = _status_of(error, code)
    headers: Optional[Mapping[str, str]] = getattr(getattr(error, "response", None), "headers", None)
    retry_after: Optional[float] = retry_after_seconds(headers, now) if headers is not None else None
    should_retry: Optional[str] = headers.get("x-should-retry") if headers is not None else None
    in_stream: bool = (
        isinstance(error, APIError) and not isinstance(error, APIConnectionError)
        and getattr(error, "status_code", None) is None
    )
    message: str = _error_text(error)
    chain: Tuple[BaseException, ...] = (
        _exception_chain(error) if isinstance(error, (APIConnectionError, ssl.SSLError)) else (error,)
    )
    causes: Tuple[Exception, ...] = _connection_causes(error, chain)
    certificate_failure: bool = any(isinstance(item, ssl.SSLCertVerificationError) for item in chain)
    return ModelErrorInfo(
        kind=_kind_of(error, status, labels, should_retry, message, certificate_failure), status_code=status,
        code=code, retry_after=retry_after, in_stream=in_stream,
        cause_type=type(causes[-1]).__name__ if causes else None,
        detail=_detail(error, status, code, message, causes),
    )


def _jitter_unit(session_id: str, step: int) -> float:
    """(session_id, adım) çiftinden deterministik [0, 1] değeri: RNG/global durum yok, testte sabit. Saf."""
    return zlib.crc32(f"{session_id}:{step}".encode("utf-8")) / 0xFFFFFFFF


def retry_wait_seconds(kind: ModelErrorKind, retry_after: Optional[float], waits: int, session_id: str) -> float:
    """
    Sonraki denemeden önceki bekleme (sn). Sunucu süre verdiyse tam o süre + RETRY_AFTER_MARGIN_SECONDS.
    Yoksa üstel takvim (geçici: 0,5 * 2^n, hız sınırı: 5 * 2^n; tavan 30 sn) ve %0-25 deterministik
    kısaltma (jitter: eşzamanlı görevler farklı session_id ile ayrışır). Saf.
    """
    if retry_after is not None:
        return retry_after + RETRY_AFTER_MARGIN_SECONDS
    base: float = RATE_LIMIT_BACKOFF_BASE_SECONDS if kind == "rate_limited" else BACKOFF_BASE_SECONDS
    scheduled: float = min(base * 2 ** min(waits, BACKOFF_MAX_EXPONENT), BACKOFF_CAP_SECONDS)
    return scheduled * (1.0 - JITTER_FRACTION * _jitter_unit(session_id, waits))


def decide_retry(info: ModelErrorInfo, has_alternative: bool, waits: int, session_id: str) -> RetryDecision:
    """
    Karar matrisi. has_alternative: bu turun planında etkin profilden farklı bir profil daha var.
    permanent, retry_refused, certificate -> raise (profil engellenmez); access -> alternatif varsa
    engelle+geç, yoksa raise; rate_limited -> alternatif varsa serinlemeye al+geç, yoksa bekleyerek
    yeniden dene; timeout -> alternatif varsa geç, yoksa yeniden dene; transient -> yeniden dene. Saf.
    """
    if info.kind in UNRETRYABLE_KINDS:
        return RetryDecision("raise", 0.0, 0.0, False)
    if info.kind == "access":
        if has_alternative:
            return RetryDecision("switch", 0.0, 0.0, True)
        return RetryDecision("raise", 0.0, 0.0, False)
    if has_alternative and info.kind == "rate_limited":
        cooldown: float = (
            info.retry_after + RETRY_AFTER_MARGIN_SECONDS if info.retry_after is not None
            else RATE_LIMIT_COOLDOWN_SECONDS
        )
        return RetryDecision("switch", 0.0, cooldown, False)
    if has_alternative and info.kind == "timeout":
        return RetryDecision("switch", 0.0, 0.0, False)
    return RetryDecision("retry", retry_wait_seconds(info.kind, info.retry_after, waits, session_id), 0.0, False)


def cooling_backends(cooldowns: Mapping[str, float], now: float) -> frozenset[str]:
    """Serinleme süresi dolmamış profiller (time.monotonic saatiyle). Saf."""
    return frozenset(name for name, until in cooldowns.items() if until > now)


def next_different_backend(plan: Tuple[str, ...], attempt: int) -> int:
    """attempt'ten sonraki, plan[attempt]'ten farklı ilk profilin indeksi; yoksa len(plan). Saf."""
    return next((index for index in range(attempt + 1, len(plan)) if plan[index] != plan[attempt]), len(plan))


def model_retry_deadline(now: float, retry_seconds: float, wall_clock_deadline: float) -> float:
    """Bu turdaki model çağrısının yeniden denemeyi bırakacağı an: bütçe ile görev duvar saatinin küçüğü. Saf."""
    return min(now + retry_seconds, wall_clock_deadline)


def unattended_model_retry_seconds(max_wall_clock: float) -> float:
    """
    Sürekli/gözetimsiz görevde çağrı başına yeniden deneme bütçesi: kalan görev süresi, en çok
    UNATTENDED_MODEL_RETRY_CAP_SECONDS (RunOptions["model_retry_seconds"] açık değeri bu kipte yok sayılır).
    Görev süresi pozitif olmalı: 0, negatif ya da NaN süre sessizce sıfır bütçe olmaz, ValueError verir. Saf.
    """
    if not max_wall_clock > 0.0:
        raise ValueError(f"Görev süresi pozitif olmalı; alınan: {max_wall_clock!r}")
    return min(max_wall_clock, UNATTENDED_MODEL_RETRY_CAP_SECONDS)


def interactive_model_retry_seconds(explicit: Optional[float]) -> float:
    """
    Etkileşimli görevde çağrı başına yeniden deneme bütçesi: açık değer (RunOptions["model_retry_seconds"]) ya da
    INTERACTIVE_MODEL_RETRY_SECONDS. Pozitif olmayan (ya da NaN) açık değer ValueError verir. Saf.
    """
    if explicit is None:
        return INTERACTIVE_MODEL_RETRY_SECONDS
    if not explicit > 0.0:
        raise ValueError(f"Model yeniden deneme bütçesi pozitif olmalı; alınan: {explicit!r}")
    return explicit


def _wait_text(seconds: float) -> str:
    """Bekleme süresini kısa Türkçe metne çevirir: '45 sn' ya da '3 dk'. Saf."""
    return f"{seconds / 60:.0f} dk" if seconds >= 120.0 else f"{seconds:.0f} sn"


def failure_label(info: ModelErrorInfo) -> str:
    """Kullanıcıya gösterilen kısa hata türü: 'hız sınırı (HTTP 429)'. Hata gövdesi taşımaz. Saf."""
    label: str = MODEL_ERROR_KIND_LABELS[info.kind]
    return f"{label} (HTTP {info.status_code})" if info.status_code is not None else label


def retry_status_text(backend: str, info: ModelErrorInfo, wait_seconds: float) -> str:
    """Arayüz/Telegram/CLI durum satırı (integration_status olayı). Saf."""
    return (
        f"Model yanıt vermedi ({backend}: {failure_label(info)}); "
        f"{_wait_text(wait_seconds)} sonra yeniden denenecek"
    )


def model_call_failure(
    backend: str, model: str, info: ModelErrorInfo, attempts: int, waited_seconds: float, fallback_hint: str,
) -> ModelCallFailed:
    """
    Son hatayı ve teşhis bağlamını taşıyan ModelCallFailed kurar (çağıran `raise ... from son_hata` yapar).
    fallback_hint: izin verilmediği için atlanan hazır yedekleri anlatan cümle (yoksa boş metin).
    """
    retry_hint: str = (
        f" Sunucu {_wait_text(info.retry_after)} sonra yeniden denenmesini istedi."
        if info.retry_after is not None else ""
    )
    declined_hint: str = f" {fallback_hint}" if fallback_hint else ""
    summary: str = (
        f"{backend} ({model}): {MODEL_ERROR_KIND_LABELS[info.kind]}; {attempts} deneme, "
        f"{waited_seconds:.0f} sn beklendi.{retry_hint}{declined_hint} Son hata: {info.detail}"
    )
    return ModelCallFailed(
        summary, backend=backend, kind=info.kind, attempts=attempts, waited_seconds=waited_seconds,
    )
