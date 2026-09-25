"""
Telegram sesli mesajlarını yazıya çevirir (OpenAI Speech-to-Text, kullanıcının OpenAI anahtarıyla).

macOS konuşma tanıması launchd altında çalışan Python sürecinde güvenilir izin alamıyor
(Info.plist kullanım açıklaması ve TCC onayı gerekir); OpenAI uç noktası Telegram'ın OGG/Opus
sesini dönüştürmeden kabul eder. Ses OpenAI'a gönderilir; anahtar yoksa hiçbir şey gönderilmez.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Optional, Tuple

from httpx import Timeout
from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI, BadRequestError, NotFoundError

from omniagent.config import API_KEY_VARIABLES, BACKENDS, load_api_key

# Belgelerin önerdiği model; hesapta yoksa whisper-1 denenir. OMNI_TRANSCRIBE_MODEL ile değiştirilebilir.
DEFAULT_TRANSCRIBE_MODEL: str = "gpt-transcribe"
FALLBACK_TRANSCRIBE_MODEL: str = "whisper-1"
# Uç noktanın dosya sınırı (Telegram botları zaten en çok 20 MB indirir)
MAX_AUDIO_BYTES: int = 25 * 1024 * 1024
TRANSCRIBE_TIMEOUT: Timeout = Timeout(60.0, connect=5.0)
# Telegram sesli mesaj dosyası .oga uzantılıdır; uç nokta biçimi uzantıdan anlar
_SUFFIX_ALIASES = {".oga": ".ogg", ".opus": ".ogg"}

ClientFactory = Callable[[str, str], AsyncOpenAI]


class TranscriptionUnavailable(Exception):
    """Yazıya çevirme yolu yapılandırılmamış; mesaj kullanıcıya nasıl etkinleştireceğini söyler."""


class TranscriptionFailed(Exception):
    """Ses gönderildi ama yazıya çevrilemedi (ağ, yetki, boş konuşma)."""


def transcribe_models() -> Tuple[str, ...]:
    """Denenecek modeller: ayarlı/önerilen model, sonra whisper-1 (tekrarsız)."""
    preferred = os.environ.get("OMNI_TRANSCRIBE_MODEL", "").strip() or DEFAULT_TRANSCRIBE_MODEL
    return tuple(dict.fromkeys((preferred, FALLBACK_TRANSCRIBE_MODEL)))


def upload_name(path: Path) -> str:
    """Uç noktanın tanıdığı uzantıyla dosya adı: 'voice.oga' → 'voice.ogg'. Saf."""
    suffix = path.suffix.lower()
    return path.with_suffix(_SUFFIX_ALIASES[suffix]).name if suffix in _SUFFIX_ALIASES else path.name


def _default_client(api_key: str, base_url: str) -> AsyncOpenAI:
    return AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=TRANSCRIBE_TIMEOUT, max_retries=0)


async def transcribe_audio(path: Path, client_factory: Optional[ClientFactory] = None) -> str:
    """
    Ses dosyasını yazıya çevirir. Önerilen model hesapta yoksa (404/400) whisper-1 denenir;
    yetki, hız sınırı ve ağ hataları yeniden denemeden bildirilir.
    """
    api_key = load_api_key(API_KEY_VARIABLES["openai"])
    if not api_key:
        raise TranscriptionUnavailable(
            "Sesli mesajı yazıya çevirmek için OpenAI API anahtarı gerekli (arayüzde ⚙ Ayarlar)."
        )
    data = path.read_bytes()
    if len(data) > MAX_AUDIO_BYTES:
        raise TranscriptionFailed(f"Ses dosyası {len(data) / 1_048_576:.1f} MB; sınır {MAX_AUDIO_BYTES // 1_048_576} MB.")
    client = (client_factory or _default_client)(api_key, BACKENDS["openai"]["base_url"])
    models = transcribe_models()
    try:
        for index, model in enumerate(models):
            try:
                result = await client.audio.transcriptions.create(model=model, file=(upload_name(path), data))
            except (NotFoundError, BadRequestError) as error:
                if index + 1 < len(models):
                    continue
                raise TranscriptionFailed(f"OpenAI yazıya çevirme isteğini reddetti (HTTP {error.status_code}).") from None
            except APIStatusError as error:
                raise TranscriptionFailed(f"OpenAI yazıya çevirme hatası (HTTP {error.status_code}).") from None
            except (APIConnectionError, APITimeoutError) as error:
                raise TranscriptionFailed(f"OpenAI'a bağlanılamadı ({type(error).__name__}).") from None
            text = " ".join(str(getattr(result, "text", "") or "").split())
            if not text:
                raise TranscriptionFailed("Sesli mesajda anlaşılır konuşma bulunamadı.")
            return text
    finally:
        await client.close()
    raise TranscriptionFailed("Yazıya çevirme modeli bulunamadı.")
