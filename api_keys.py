"""API anahtarlarının macOS Keychain deposu.

Arayüzdeki Ayarlar sayfası anahtarları buraya yazar; giriş noktaları açılışta
`config.apply_stored_api_keys()` ile kayıtları süreç-içi depoya alır. Anahtarlar ortam
değişkenlerine bilinçli olarak YAZILMAZ: ajanın başlattığı alt süreçler ortamı miras aldığı
için sır süreç ağacına yayılmamalıdır (bkz. AGENTS.md güvenlik rayları). Kaydedilen değerin
kendisi log'a, olay akışına veya dokümana yazılmaz; arayüzde yalnız maskeli gösterilir.
"""
import logging
import os
import sys
from typing import Any, Dict, Optional, Tuple

# Keychain kayıtlarının hizmet adı: OmniAgent.Outlook / OmniAgent.MCP ile aynı düzen.
KEYCHAIN_SERVICE: str = "OmniAgent.APIKeys"
# Ayarlar sayfasının düzenlediği değişkenler (config.API_KEY_VARIABLES ile aynı küme olmalı).
KEY_VARIABLES: Tuple[str, ...] = ("OPENAI_API_KEY", "OPENCODE_API_KEY", "OPENROUTER_API_KEY")


def _keyring() -> Any:
    """macOS Keychain arka ucunu kurar; başka platformda açık hata verir."""
    if sys.platform != "darwin":
        raise RuntimeError("API anahtarı deposu bu sürümde macOS Keychain gerektiriyor.")
    from keyring.backends.macOS import Keyring
    return Keyring()


def stored_key(variable: str) -> Optional[str]:
    """
    Kayıtlı anahtarı döner. Keychain erişimi reddedilirse anahtar yok sayılır ve durum
    loglanır: eksik anahtar profil kullanılamaz olur, uygulama çökmez.
    """
    try:
        value: Any = _keyring().get_password(KEYCHAIN_SERVICE, variable)
    except Exception as error:
        logging.warning(
            "Kayıtlı API anahtarı okunamadı",
            extra={"variable": variable, "error_type": type(error).__name__},
        )
        return None
    if not isinstance(value, str):
        return None
    return value.strip() or None


def stored_keys() -> Dict[str, str]:
    """
    Keychain'deki dolu kayıtları döner. Ortam değişkenlerine dokunmaz; çağıran
    (config.apply_stored_api_keys) bunları süreç-içi depoya alır.
    """
    values: Dict[str, str] = {}
    for variable in KEY_VARIABLES:
        value: Optional[str] = stored_key(variable)
        if value is not None:
            values[variable] = value
    return values


def _verify_absent(keyring: Any, variable: str) -> None:
    """Silmenin gerçekten olduğunu okuma ile doğrular; belirsizlikte açık hata verir."""
    try:
        remaining: Any = keyring.get_password(KEYCHAIN_SERVICE, variable)
    except Exception as error:
        raise RuntimeError(
            f"{variable} kaydının silindiği doğrulanamadı ({type(error).__name__})."
        ) from error
    if isinstance(remaining, str) and remaining.strip():
        raise RuntimeError(f"{variable} Keychain kaydı silinemedi; kayıt hâlâ duruyor.")


def store_key(variable: str, value: str) -> None:
    """
    Anahtarı Keychain'e yazar; boş değer kaydı siler ve silmeyi OKUYARAK DOĞRULAR.
    macOS'te hem "kayıt yok" hem "silinemedi" durumu aynı PasswordDeleteError ile sarıldığı
    için istisna tipi güvenilir ölçüt değildir: doğrulama yapılmazsa kullanıcının sildiğini
    sandığı sır sonraki açılışta geri gelir. Hata yutulmaz, çağırana taşınır.
    """
    cleaned: str = value.strip()
    keyring: Any = _keyring()
    if cleaned:
        keyring.set_password(KEYCHAIN_SERVICE, variable, cleaned)
        return
    try:
        keyring.delete_password(KEYCHAIN_SERVICE, variable)
    except Exception as error:
        # "Kayıt yok" da buraya düşer; gerçek durumu doğrulama okuması belirler.
        logging.warning(
            "Keychain silme çağrısı hata verdi, doğrulanıyor",
            extra={"variable": variable, "error_type": type(error).__name__},
        )
    _verify_absent(keyring, variable)


def key_environment_present(variable: str) -> bool:
    """Kullanıcı kabukta bu anahtar için ortam değişkeni de tanımlamış mı? (arayüz uyarısı)"""
    return bool(os.environ.get(variable, "").strip())
