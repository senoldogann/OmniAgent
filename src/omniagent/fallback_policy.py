"""
Sağlayıcı yedeğe geçiş izni (KVKK/GDPR: amaçla sınırlılık, veri minimizasyonu, şeffaflık).

Model isteği (sistem istemi, kullanıcı hafızası, görev geçmişi, araç çıktıları, AX/OCR metni ve
ekran görüntüleri) yalnızca SEÇİLİ (birincil) sağlayıcıya gider. Seçili sağlayıcı yanıt vermezse
istek başka bir sağlayıcıya ancak burada AÇIKÇA izin verilmişse yönlendirilir; aynı sağlayıcıda
yeniden deneme her zaman serbesttir (bkz. app/model_retry.py). Varsayılan: hiçbir yedek yok.

İzin kaynağı (ilk bulunan TAMAMEN belirleyicidir, alanlar birleştirilmez):
1. Ortam: OMNI_FALLBACK_BACKENDS ('none' ya da virgüllü QUALITY_LADDER profil adları) ve
   OMNI_FALLBACK_IMAGES ('1' ya da '0'). İkisinden biri tanımlıysa (boş değer de tanımlıdır ve
   'kapalı' sayılır) kayıt dosyası hiç okunmaz; tanımsız olan kapalı sayılır. Eski (izinsiz)
   davranışın karşılığı:
   OMNI_FALLBACK_BACKENDS=ollama-cloud,openai,openrouter OMNI_FALLBACK_IMAGES=1
2. Kayıt: data_root()/provider_fallback.json = {"backends": [...], "allow_images": bool}
   (elle yazılır; model_preferences.json'dan bilerek ayrıdır). Bozuk, bilinmeyen alanlı ya da
   merdiven dışı profil içeren kayıt sessizce yok sayılmaz: ValueError yükselir ve görev başlamaz.
   Finder/launchd ile açılan uygulama kabuk ortamını miras almaz; orada kalıcı kaynak bu dosyadır.
3. Varsayılan: strict_policy().

Görüntülü istek (ekran görüntüsü, kullanıcı eki) ayrıca allow_images ister: görüntü kullanıcının
yazmadığı üçüncü kişi verisi taşıyabilir ve maskelenemez. Görüntüsüz bir yedek isteği de AX/OCR
metni, hafıza ve araç çıktısı taşır; allow_images yalnız ekran piksellerini sınırlar (tam koruma
değildir). Görüntüsüz istekle kalıcı yedeğe geçilmiş görevde (birincil erişim hatasıyla
karantinada) yedek sağlayıcı da aynı görüntü kuralına bağlıdır. Yedek adayları yalnız
QUALITY_LADDER profilleridir (opencode* otomatik yedek olmaz).

Otomatik seçim de izin listesine tabidir. Seçilen profil, açık seçimdir (arayüz menüsü, /model,
--backend, OMNI_BACKEND) ya da 'Otomatik' = varsayılan profildir (config.DEFAULT_BACKEND). Seçilen
profil hazır değilse (anahtar yok, Ollama kapalı) görev başka sağlayıcıya YALNIZ izinle taşınır:
merdivendeki ilk hazır ve izinli profil seçilir, geçiş istekten önce olay + denetim kaydı üretir; izin
ya da (görsel ek için) görüntü izni yoksa görev başlamaz ve hata izni nasıl vereceğini söyler.
Ollama'sız kullanıcı için tek seferlik yol OMNI_FALLBACK_BACKENDS=openai (ya da kayıt dosyası); ya da
hazır profil açıkça seçilir: hazır bir profilin açık seçimi izin gerektirmez, çünkü veri zaten seçilen
sağlayıcıya gider.

Bu izin ajanın kendi araçlarına (fetch_raw, kabuk) karşı güvenlik sınırı değildir; host'un model
isteği yönlendirmesini sınırlar. Kayıt dosyasını ajanın araçları değiştirebilir; sertleştirilmiş
kurulumda ortam değişkeni kullanılmalıdır. Yerel ses girişi (platform/macos/voice.py) ağ fallback'i
kullanmaz. Telegram sesli mesajının yazıya çevrilmesi (integrations/transcription.py) ise sesi
OpenAI'a gönderir: bunun için openai'ın da bu izin listesinde olması gerekir (voice_transcription_problem),
aksi hâlde hiçbir şey gönderilmez. Bu düzenek hukuki uyum beyanı değil, şeffaflık + kullanıcı
kontrolü + kayıt tutma önlemidir.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple, TypedDict

from omniagent.approval import AuditRecord
from omniagent.config import BACKENDS, QUALITY_LADDER
from omniagent.core.events import ProviderFallback
from omniagent.integrations.runtime import data_root

FALLBACK_BACKENDS_VARIABLE: str = "OMNI_FALLBACK_BACKENDS"
FALLBACK_IMAGES_VARIABLE: str = "OMNI_FALLBACK_IMAGES"
# Telegram sesli mesajını yazıya çeviren (sesi alan) sağlayıcı: integrations/transcription.py yalnız bunu kullanır.
TRANSCRIPTION_BACKEND: str = "openai"

# Her profilin verisini gerçekte işleyen taraf (kullanıcıya gösterilen ad). Anahtarlar BACKENDS ile aynı olmalı.
PROCESSOR_LABELS: Dict[str, str] = {
    "ollama-cloud": "Ollama Cloud (yerel Ollama üzerinden bulut modeli)",
    "openai": "OpenAI",
    "opencode": "Opencode Go",
    "opencode-think": "Opencode Go",
    "openrouter": "OpenRouter ve yönlendirdiği model sağlayıcısı",
}


class FallbackPolicy(TypedDict):
    backends: Tuple[str, ...]
    allow_images: bool


class FallbackNotPermitted(RuntimeError):
    """İstek yedek sağlayıcıya gidemiyor (izin yok); mesaj izni nasıl vereceğini söyler."""


class FallbackAuditFailed(RuntimeError):
    """Yedek sağlayıcıya geçiş denetim kaydına yazılamadı; kayıtsız aktarım yapılmadığı için istek gönderilmedi."""


def strict_policy() -> FallbackPolicy:
    """Varsayılan: hiçbir yedek sağlayıcıya izin yok."""
    return {"backends": (), "allow_images": False}


def _ladder_subset(names: Sequence[str]) -> Tuple[str, ...]:
    """
    Profil adlarını QUALITY_LADDER sırasına dizer; merdiven dışı ad açık hata verir. İleti izin kaynağına özgü
    'yedek istemiyorum' yolunu söylemez (ortamda 'none', kayıt dosyasında boş liste): onu çağıran ekler. Saf.
    """
    unknown: List[str] = [name for name in names if name not in QUALITY_LADDER]
    if unknown:
        raise ValueError(f"Yedek sağlayıcı olamayan profil: {unknown}; geçerli: {list(QUALITY_LADDER)}")
    return tuple(name for name in QUALITY_LADDER if name in names)


def parse_backend_list(raw: str) -> Tuple[str, ...]:
    """'' ve 'none' → (); 'openrouter, openai' → ('openai', 'openrouter'). Saf."""
    text: str = raw.strip().casefold()
    if text in ("", "none"):
        return ()
    return _ladder_subset([part.strip() for part in text.split(",") if part.strip()])


def parse_switch(variable: str, raw: str) -> bool:
    """'1' → True; '0' → False; başka değer açık hata. Saf."""
    token: str = raw.strip()
    if token == "1":
        return True
    if token == "0":
        return False
    raise ValueError(f"{variable}: '1' ya da '0' bekleniyor; alınan: {raw!r}")


def validate_fallback_policy(raw: Mapping[str, object]) -> FallbackPolicy:
    """Kayıt nesnesini doğrular; bilinmeyen alan, yanlış tür ve merdiven dışı profil açık hata verir. Saf."""
    extra: List[str] = sorted(set(raw) - {"backends", "allow_images"})
    if extra:
        raise ValueError(f"Yedek sağlayıcı kaydında bilinmeyen alan: {extra}")
    backends: object = raw.get("backends")
    allow_images: object = raw.get("allow_images")
    if not isinstance(backends, list) or not all(isinstance(name, str) for name in backends):
        raise ValueError(f"'backends' profil adı listesi olmalı; alınan: {backends!r}")
    if not isinstance(allow_images, bool):
        raise ValueError(f"'allow_images' true/false olmalı; alınan: {allow_images!r}")
    try:
        allowed: Tuple[str, ...] = _ladder_subset(backends)
    except ValueError as error:
        # Kayıt dosyasında 'none' YOKTUR (yalnız ortam değişkeninde): yedek istemeyen boş liste yazar
        raise ValueError(f"{error}; yedek istemiyorsanız 'backends' için boş liste yazın: \"backends\": []") from error
    return {"backends": allowed, "allow_images": allow_images}


def fallback_policy_path() -> Path:
    """Elle yazılan yedek sağlayıcı izni (model_preferences.json'dan ayrı)."""
    return data_root() / "provider_fallback.json"


def load_saved_fallback_policy() -> FallbackPolicy:
    """Yalnız kayıt dosyası: yoksa strict_policy(); bozuksa ValueError. Saf değildir: dosyayı okur."""
    path: Path = fallback_policy_path()
    if not path.is_file():
        return strict_policy()
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as error:
        raise ValueError(
            f"Yedek sağlayıcı kaydı okunamadı ({path}): {error}. Dosyayı düzeltin ya da silin."
        ) from error
    if not isinstance(raw, dict):
        raise ValueError(f"Yedek sağlayıcı kaydı JSON nesnesi olmalı: {path}")
    try:
        return validate_fallback_policy(raw)
    except ValueError as error:
        raise ValueError(f"Yedek sağlayıcı kaydı geçersiz ({path}): {error}") from error


def _environment_policy() -> Optional[FallbackPolicy]:
    """Ortam değişkenlerinden biri tanımlıysa tüm izin odur; ikisi de yoksa None. Saf değildir: ortamı okur."""
    backends_raw: Optional[str] = os.environ.get(FALLBACK_BACKENDS_VARIABLE)
    images_raw: Optional[str] = os.environ.get(FALLBACK_IMAGES_VARIABLE)
    if backends_raw is None and images_raw is None:
        return None
    try:
        backends: Tuple[str, ...] = parse_backend_list(backends_raw or "")
    except ValueError as error:
        raise ValueError(f"{FALLBACK_BACKENDS_VARIABLE}: {error}; yedek istemiyorsanız 'none' yazın") from error
    allow_images: bool = (
        parse_switch(FALLBACK_IMAGES_VARIABLE, images_raw) if images_raw is not None and images_raw.strip() else False
    )
    return {"backends": backends, "allow_images": allow_images}


def load_fallback_policy() -> FallbackPolicy:
    """Geçerli izin: ortam > kayıt > kapalı. Geçersiz değer sessizce yok sayılmaz. Saf değildir: ortamı ve dosyayı okur."""
    from_environment: Optional[FallbackPolicy] = _environment_policy()
    return from_environment if from_environment is not None else load_saved_fallback_policy()


def count_image_parts(messages: Sequence[Mapping[str, object]]) -> int:
    """Modele gidecek mesajlardaki image_url parçası sayısı (ekran görüntüsü + kullanıcı eki). Saf."""
    total: int = 0
    for entry in messages:
        content: object = entry.get("content")
        if isinstance(content, list):
            total += sum(1 for part in content if isinstance(part, dict) and part.get("type") == "image_url")
    return total


def permitted_fallbacks(backends: frozenset[str], allow_images: bool, image_count: int) -> frozenset[str]:
    """Bu isteğe verilebilecek yedek profiller; görüntülü istek ayrıca allow_images ister. Saf."""
    return frozenset() if image_count > 0 and not allow_images else backends


def _permission_paths(environment_setting: str, record_content: str) -> str:
    """
    İzni vermenin iki yolu: ortam değişkeni ve kayıt dosyası. Finder/launchd ile açılan uygulama ve Telegram köprüsü
    (launchd) kabuk ortamını miras almaz: orada yalnız kayıt dosyası çalışır, bu yüzden iletiler dosya yolunu ve
    biçimini de söyler. İleti kuran işlevlerin tek ortak parçasıdır. Saf değildir: veri kökü yolunu çözer.
    """
    return (
        f"{environment_setting}; Finder/launchd ve Telegram köprüsü ortamı miras almadığından orada "
        f"{fallback_policy_path()} dosyasına {record_content}"
    )


_BACKENDS_RECORD_EXAMPLE: str = '{"backends": ["<profil>", ...], "allow_images": false}'
_IMAGES_RECORD_CONTENT: str = '"allow_images": true'


def fallback_recipient_problem(
    backend: str, primary: str, backends: frozenset[str], allow_images: bool, image_count: int,
) -> Optional[str]:
    """
    İsteğin başlayacağı profil birincil değilse (birincil erişim hatasıyla karantinaya alınıp kalıcı
    yedeğe geçilmiştir) ve bu istek o profile gidemiyorsa açıklayıcı hata metni; sorun yoksa None. Saf değildir:
    izin verme yolu iletisi veri kökü yolunu çözer.
    """
    if backend == primary or backend in permitted_fallbacks(backends, allow_images, image_count):
        return None
    if backend in backends:
        return (
            f"Birincil sağlayıcı '{primary}' bu görevde kullanılamıyor ve istek yedek '{backend}' üzerinde "
            f"sürdürülemiyor: {image_count} ekran görüntüsü içeriyor, görüntülü yedek izni kapalı "
            f"({_permission_paths(f'{FALLBACK_IMAGES_VARIABLE}=1', _IMAGES_RECORD_CONTENT)})."
        )
    return f"'{backend}' birincil sağlayıcı ('{primary}') değil ve yedek izin listesinde yok."


def startup_replacement(
    selected: str, available: frozenset[str], backends: frozenset[str], allow_images: bool, image_count: int,
) -> Optional[str]:
    """
    Görev başında seçilen profil (açık seçim ya da 'Otomatik' = varsayılan profil) hazır değilken görevin
    taşınacağı profil: merdiven sırasındaki ilk hazır VE izinli profil (görsel ek varsa görüntü izni de
    gerekir); yoksa None. Saf.
    """
    permitted: frozenset[str] = permitted_fallbacks(backends, allow_images, image_count)
    return next(
        (name for name in QUALITY_LADDER if name != selected and name in available and name in permitted), None,
    )


def startup_substitution_problem(
    selected: str, available: frozenset[str], backends: frozenset[str], allow_images: bool, image_count: int,
) -> str:
    """
    Seçilen profil hazır değil ve görevin taşınabileceği izinli hazır profil yok (startup_replacement None
    döndü): eyleme dönük hata metni. Hazır yedek adayı yoksa profili açıkça seçmeyi, adaylar izin ya da
    görüntü izni yüzünden elenmişse izin vermeyi (ortam değişkeni ya da kayıt dosyası) söyler. Saf değildir:
    veri kökü yolunu çözer.
    """
    head: str = f"'{selected}' profili hazır değil (API anahtarı yok ya da model kurulu değil)"
    ready: Tuple[str, ...] = tuple(name for name in QUALITY_LADDER if name != selected and name in available)
    if not ready:
        return (
            f"{head} ve yedek olabilecek hazır profil yok. Anahtarı girin ya da hazır bir profili açıkça seçin "
            "(arayüzde model menüsü, Telegram'da /model)."
        )
    if image_count > 0 and not allow_images and backends.intersection(ready):
        how: str = (
            f"görev {image_count} görsel ek içeriyor; görüntülü yedek izni kapalı "
            f"({_permission_paths(f'{FALLBACK_IMAGES_VARIABLE}=1', _IMAGES_RECORD_CONTENT)})"
        )
    else:
        how = (
            "yedek sağlayıcı izni yok "
            f"({_permission_paths(f'{FALLBACK_BACKENDS_VARIABLE}=<profil,...>', _BACKENDS_RECORD_EXAMPLE)})"
        )
    return (
        f"{head}; hazır yedek sağlayıcılara ({', '.join(ready)}) geçilmedi: {how}. "
        "Anahtarı girin, hazır bir profili açıkça seçin ya da izin verin."
    )


def voice_transcription_problem(backends: frozenset[str]) -> Optional[str]:
    """
    Telegram sesli mesajını yazıya çevirmek sesi TRANSCRIPTION_BACKEND'e (OpenAI) gönderir: bu sağlayıcı
    yedek izin listesinde değilse açıklayıcı hata metni (ortam değişkeni ve kayıt dosyası yolu); izinliyse None.
    Telegram köprüsü launchd altında koşar ve kabuk ortamını miras almaz: dosya yolu bu yüzden söylenir. Saf değildir:
    veri kökü yolunu çözer.
    """
    if TRANSCRIPTION_BACKEND in backends:
        return None
    record: str = f'{{"backends": ["{TRANSCRIPTION_BACKEND}"], "allow_images": false}}'
    return (
        f"Sesli mesajı yazıya çevirmek sesi OpenAI'a gönderir; OpenAI yedek sağlayıcı izin listesinde değil "
        f"(izin verin: {_permission_paths(f'{FALLBACK_BACKENDS_VARIABLE}={TRANSCRIPTION_BACKEND}', record)})."
    )


def fallback_declined_hint(
    backend: str, available: frozenset[str], backends: frozenset[str], allow_images: bool, image_count: int,
) -> str:
    """
    Seçili sağlayıcı tükendiğinde, HAZIR olduğu hâlde izin verilmediği için atlanan yedekleri eyleme
    dönük bildirir (izin verme yolları: ortam değişkeni ve kayıt dosyası); atlanan yoksa boş metin. Saf değildir:
    veri kökü yolunu çözer.
    """
    permitted: frozenset[str] = permitted_fallbacks(backends, allow_images, image_count)
    declined: Tuple[str, ...] = tuple(
        name for name in QUALITY_LADDER if name != backend and name in available and name not in permitted
    )
    if not declined:
        return ""
    if image_count > 0 and not allow_images and backends.intersection(declined):
        how: str = (
            f"istek {image_count} ekran görüntüsü içeriyor; görüntülü yedek izni kapalı "
            f"({_permission_paths(f'{FALLBACK_IMAGES_VARIABLE}=1', _IMAGES_RECORD_CONTENT)})"
        )
    else:
        how = (
            "yedek sağlayıcı izni yok "
            f"({_permission_paths(f'{FALLBACK_BACKENDS_VARIABLE}=<profil,...>', _BACKENDS_RECORD_EXAMPLE)})"
        )
    return f"Hazır yedek sağlayıcılara ({', '.join(declined)}) geçilmedi: {how}."


def provider_fallback_event(from_backend: str, to_backend: str, reason: str, image_count: int) -> ProviderFallback:
    """Yedek geçiş olayını kurar; bilinmeyen profil KeyError verir (sessiz varsayılan yok). Saf."""
    return {
        "kind": "provider_fallback", "from_backend": from_backend, "to_backend": to_backend,
        "to_model": BACKENDS[to_backend]["model"], "processor": PROCESSOR_LABELS[to_backend],
        "reason": reason, "image_count": image_count,
    }


def provider_fallback_audit(event: ProviderFallback, timestamp: str) -> AuditRecord:
    """audit.jsonl satırı: içerik değil yalnız hedef, gerekçe ve görüntü sayısı. Saf."""
    return {
        "timestamp": timestamp, "category": "provider_fallback", "tool": f"model:{event['to_backend']}",
        "summary": (
            f"{event['from_backend']} → {event['to_backend']} ({event['to_model']}); "
            f"neden: {event['reason']}; görüntü: {event['image_count']}"
        ),
        "decision": "policy_allowed",
    }
