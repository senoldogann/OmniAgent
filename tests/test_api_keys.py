"""Ayarlar sayfasının anahtar deposu, süreç-içi kullanım ve sır sızıntısı regresyonları."""
import os
from typing import Dict, Iterator, Optional, Set

import pytest

import api_keys
import config
import main
import tools
import ui

# Sır olduğu için asla log'a/transcripte düşmemesi gereken sınama değeri.
SECRET: str = "canli-gizli-anahtar-12345"


class FakeKeyring:
    """Keychain'i taklit eden bellek içi depo (tests/test_outlook_auth.py ile aynı desen)."""

    def __init__(self, refuse_write: Optional[Set[str]] = None, refuse_delete: bool = False) -> None:
        self.entries: Dict[tuple[str, str], str] = {}
        self.refuse_write: Set[str] = refuse_write or set()
        self.refuse_delete: bool = refuse_delete

    def get_password(self, service: str, key: str) -> Optional[str]:
        return self.entries.get((service, key))

    def set_password(self, service: str, key: str, value: str) -> None:
        if key in self.refuse_write:
            raise RuntimeError("Keychain erişimi reddedildi")
        self.entries[service, key] = value

    def delete_password(self, service: str, key: str) -> None:
        if self.refuse_delete:
            # macOS'te "silinemedi" de bir istisna gibi görünebilir; gerçek durumu doğrulama
            # okuması belirler. Bu taklit, sessizce hiçbir şey yapmayan başarısız silmeyi temsil eder.
            return
        if (service, key) not in self.entries:
            raise LookupError("kayıt yok")
        del self.entries[service, key]


class SettingsHost:
    """Gerçek pencere açmadan ayar mantığını sınamak için asgari taşıyıcı."""

    _apply_settings = ui.OmniUI._apply_settings


@pytest.fixture(autouse=True)
def clean_keys(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """
    Her testi temiz bir anahtar durumunda çalıştırır ve sonrasında toplar: kabuk değişkenleri,
    süreç-içi depo ve profil anahtarları testler arasında sızmamalı.
    """
    original: Dict[str, Optional[str]] = {
        name: profile["api_key"] for name, profile in config.BACKENDS.items()
    }
    for variable in config.API_KEY_VARIABLES.values():
        monkeypatch.delenv(variable, raising=False)
        config.set_api_key(variable, None)
    yield
    for variable in config.API_KEY_VARIABLES.values():
        config.set_api_key(variable, None)
    for name, value in original.items():
        config.BACKENDS[name]["api_key"] = value


@pytest.fixture
def keychain(monkeypatch: pytest.MonkeyPatch) -> FakeKeyring:
    """Gerçek Keychain yerine bellek içi depo kurar."""
    import keyring.backends.macOS
    store: FakeKeyring = FakeKeyring()
    monkeypatch.setattr(keyring.backends.macOS, "Keyring", lambda: store)
    return store


def _install(monkeypatch: pytest.MonkeyPatch, store: FakeKeyring) -> FakeKeyring:
    """Verilen taklit anahtarlığı api_keys'in kullanacağı arka uç yapar."""
    import keyring.backends.macOS
    monkeypatch.setattr(keyring.backends.macOS, "Keyring", lambda: store)
    return store


def test_key_variables_match_profile_mapping() -> None:
    """Ayarlar sayfasının düzenlediği değişken kümesi profil eşlemesinden kopmasın."""
    assert set(api_keys.KEY_VARIABLES) == set(config.API_KEY_VARIABLES.values())


def test_store_and_clear_roundtrip(keychain: FakeKeyring) -> None:
    """Kaydedilen anahtar kırpılır; boş değer kaydı siler; kayıt yokken silmek hata vermez."""
    api_keys.store_key("OPENAI_API_KEY", "  gizli-anahtar  ")
    assert api_keys.stored_key("OPENAI_API_KEY") == "gizli-anahtar"
    api_keys.store_key("OPENAI_API_KEY", "   ")
    assert api_keys.stored_key("OPENAI_API_KEY") is None
    api_keys.store_key("OPENAI_API_KEY", "")


def test_store_key_verifies_that_delete_actually_happened(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Silmeyi reddeden Keychain sessizce yutulmaz: kayıt hâlâ duruyorsa açık hata verilir.
    Doğrulama olmadan kullanıcı anahtarı sildiğini sanır, sır sonraki açılışta geri gelir.
    """
    store: FakeKeyring = _install(monkeypatch, FakeKeyring(refuse_delete=True))
    store.entries[api_keys.KEYCHAIN_SERVICE, "OPENAI_API_KEY"] = "eski-anahtar"
    with pytest.raises(RuntimeError, match="silinemedi"):
        api_keys.store_key("OPENAI_API_KEY", "")
    assert api_keys.stored_key("OPENAI_API_KEY") == "eski-anahtar"


def test_stored_keys_returns_only_filled_records(keychain: FakeKeyring) -> None:
    """Hidrasyon yalnız dolu kayıtları görür; boş kayıt profil kurmaya yetmez."""
    api_keys.store_key("OPENAI_API_KEY", "kayitli-openai")
    api_keys.store_key("OPENROUTER_API_KEY", "   ")
    assert api_keys.stored_keys() == {"OPENAI_API_KEY": "kayitli-openai"}


def test_apply_stored_api_keys_uses_runtime_store_not_environment(
    keychain: FakeKeyring, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Açılış hidrasyonu kayıtlı anahtarı süreç-içi depoya alır; kabuk değişkenine YAZMAZ.
    Kayıtlı anahtar bilinçli olarak kabuk değişkenini geçersiz kılar (arayüzden girilen
    anahtar sessizce yok sayılmasın).
    """
    api_keys.store_key("OPENAI_API_KEY", "kayitli")
    monkeypatch.setenv("OPENAI_API_KEY", "kabuktan")
    ready: tuple[str, ...] = config.apply_stored_api_keys()
    assert config.load_api_key("OPENAI_API_KEY") == "kayitli"
    assert config.BACKENDS["openai"]["api_key"] == "kayitli"
    assert config.api_key_source("OPENAI_API_KEY") == "ayarlar"
    assert "openai" in ready
    # Kabuk değişkeni yerinde kalır ama sürece sır olarak yayılmaz.
    assert os.environ["OPENAI_API_KEY"] == "kabuktan"
    assert "OPENAI_API_KEY" not in tools.child_environment()


def test_environment_only_key_still_works_without_keychain_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kayıt yoksa kabuk değişkeni yedek olarak kullanılır (geriye dönük uyum)."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "kabuktan")
    assert config.load_api_key("OPENROUTER_API_KEY") == "kabuktan"
    assert config.api_key_source("OPENROUTER_API_KEY") == "ortam"


def test_settings_apply_writes_keychain_and_runtime_store(
    keychain: FakeKeyring,
) -> None:
    """Kaydet: Keychain + süreç-içi depo + profil anahtarı güncellenir, ortam değişmez."""
    host: SettingsHost = SettingsHost()
    changed, failed = host._apply_settings({"OPENAI_API_KEY": " ayarlardan ", "OPENROUTER_API_KEY": ""})
    assert changed == ["OPENAI_API_KEY"] and failed == []
    assert api_keys.stored_key("OPENAI_API_KEY") == "ayarlardan"
    assert config.load_api_key("OPENAI_API_KEY") == "ayarlardan"
    assert config.BACKENDS["openai"]["api_key"] == "ayarlardan"
    assert "OPENAI_API_KEY" not in os.environ
    # Aynı değeri tekrar kaydetmek değişiklik üretmez.
    assert host._apply_settings({"OPENAI_API_KEY": "ayarlardan"}) == ([], [])
    # Boş alan kaydı siler ve profili kullanılamaz yapar.
    changed, failed = host._apply_settings({"OPENAI_API_KEY": ""})
    assert changed == ["OPENAI_API_KEY"] and failed == []
    assert api_keys.stored_key("OPENAI_API_KEY") is None
    assert config.BACKENDS["openai"]["api_key"] is None


def test_settings_apply_reports_keychain_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keychain yazımı reddedilirse sessizce yutulmaz; kullanıcıya başarısız değişken döner."""
    _install(monkeypatch, FakeKeyring(refuse_write={"OPENCODE_API_KEY"}))
    host: SettingsHost = SettingsHost()
    changed, failed = host._apply_settings({"OPENCODE_API_KEY": "yazilamaz"})
    assert changed == [] and failed == ["OPENCODE_API_KEY"]
    assert config.BACKENDS["opencode"]["api_key"] is None
    assert config.load_api_key("OPENCODE_API_KEY") is None


def test_settings_apply_keeps_successful_key_on_partial_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kısmi başarıda kaydedilen anahtar etkinleşir; yalnız başarısız değişken bildirilir."""
    _install(monkeypatch, FakeKeyring(refuse_write={"OPENCODE_API_KEY"}))
    host: SettingsHost = SettingsHost()
    changed, failed = host._apply_settings(
        {"OPENAI_API_KEY": "kaydedilen", "OPENCODE_API_KEY": "reddedilen"}
    )
    assert changed == ["OPENAI_API_KEY"] and failed == ["OPENCODE_API_KEY"]
    assert config.BACKENDS["openai"]["api_key"] == "kaydedilen"
    assert config.BACKENDS["opencode"]["api_key"] is None


def test_shell_children_do_not_inherit_api_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Ajanın çalıştırdığı alt süreç anahtarı görmemeli: 'printenv' ile okunan sır ne modele
    ne transcripte düşer. Kontrol değişkeni görünür kaldığı için sonda anlamlıdır.
    """
    config.set_api_key("OPENAI_API_KEY", SECRET)
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)
    monkeypatch.setenv("OMNI_LEAK_CONTROL", "gorunur-deger")
    assert "OPENAI_API_KEY" not in tools.child_environment()
    _code, stdout, stderr = tools.run_streaming_process(
        "printenv OPENAI_API_KEY; printenv OMNI_LEAK_CONTROL", True, 20,
    )
    assert "gorunur-deger" in stdout + stderr
    assert SECRET not in stdout + stderr


def test_result_text_masks_secret_values() -> None:
    """Araç sonucu modele/transcripte gitmeden önce sır maskelenir (başarı ve hata metni)."""
    config.set_api_key("OPENAI_API_KEY", SECRET)
    result: main.ToolResult = {"ok": True, "result": f"STDOUT: {SECRET}", "seconds": 0.1}
    masked: str = main.result_text(result)
    assert SECRET not in masked
    assert config.SECRET_PLACEHOLDER in masked
    failure: main.ToolResult = {"ok": False, "error_type": "TOOL_ERROR", "error": f"kapı reddetti: {SECRET}"}
    assert SECRET not in main.result_text(failure)


def test_short_values_are_not_masked() -> None:
    """Çok kısa sırlar (ör. '1') metni bozmasın: yalnız eşiği geçen değerler maskelenir."""
    config.set_api_key("OPENAI_API_KEY", "1")
    assert config.redact("sürüm 1 hazır") == "sürüm 1 hazır"
