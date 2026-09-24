"""Ayarlar sayfasının gerçek Tk bileşenlerindeki açılış ve kaydetme davranışı."""
import os
from typing import Dict, Iterator, List, Optional

import customtkinter as ctk
import pytest
from openai import AsyncOpenAI

import api_keys
import config
import ui


class FakeKeyring:
    """Keychain'i taklit eden bellek içi depo."""

    def __init__(self, refuse_write: Optional[set[str]] = None) -> None:
        self.entries: Dict[tuple[str, str], str] = {}
        self.refuse_write: set[str] = refuse_write or set()

    def get_password(self, service: str, key: str) -> Optional[str]:
        return self.entries.get((service, key))

    def set_password(self, service: str, key: str, value: str) -> None:
        if key in self.refuse_write:
            raise RuntimeError("Keychain erişimi reddedildi")
        self.entries[service, key] = value

    def delete_password(self, service: str, key: str) -> None:
        self.entries.pop((service, key), None)


def _descendants(widget: object) -> Iterator[object]:
    """Oluşturulma sırasını koruyarak alt bileşenleri dolaşır."""
    for child in widget.winfo_children():
        yield child
        yield from _descendants(child)


def _of_kind(root: object, kind: type) -> List[object]:
    return [widget for widget in _descendants(root) if isinstance(widget, kind)]


def _save_buttons(window: object) -> List[object]:
    return [button for button in _of_kind(window, ctk.CTkButton) if button.cget("text") == "Kaydet"]


def _fill(field: object, value: str) -> None:
    field.delete(0, "end")
    field.insert(0, value)


@pytest.fixture(autouse=True)
def clean_keys(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Kabuk değişkeni, süreç-içi depo ve profil anahtarları testler arasında sızmasın."""
    original: Dict[str, Optional[str]] = {
        name: profile["api_key"] for name, profile in config.BACKENDS.items()
    }
    for variable in api_keys.KEY_VARIABLES:
        monkeypatch.delenv(variable, raising=False)
        config.set_api_key(variable, None)
    yield
    for variable in api_keys.KEY_VARIABLES:
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


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> Iterator[ui.OmniUI]:
    if os.environ.get("OMNI_UI_TEST") != "1":
        pytest.skip("Gerçek Tk testi OMNI_UI_TEST=1 ile etkinleştirilir.")
    monkeypatch.setattr(ui, "create_model_clients", lambda: {})
    window: ui.OmniUI = ui.OmniUI()
    window.withdraw()
    yield window
    window._on_close()


def test_header_has_settings_entry(app: ui.OmniUI) -> None:
    """Dişli düğmesi kopyala düğmesiyle aynı kutuyu ve SVG ikon boyutunu kullanır."""
    assert isinstance(app.settings_btn.cget("image"), ctk.CTkImage)
    assert app.settings_btn.winfo_reqwidth() == app.copy_btn.winfo_reqwidth()
    assert app.settings_btn.winfo_reqheight() == app.copy_btn.winfo_reqheight()


def test_settings_window_schedules_titlebar_styling(
    app: ui.OmniUI, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ayarlar penceresi de başlık çubuğu stilini kendi başlığıyla zamanlar."""
    app._open_settings()
    window = app._settings_window
    assert window is not None
    styles: List[Optional[str]] = []
    monkeypatch.setattr(app, "_style_native_titlebar", lambda title=None: styles.append(title))
    pending: List[object] = []
    monkeypatch.setattr(window, "after", lambda ms, callback=None, *rest: pending.append((ms, callback)))
    app._schedule_titlebar_style(window)
    assert [ms for ms, _ in pending] == [120]
    pending[0][1]()
    assert styles == [window.title()]
    window.destroy()


def test_settings_page_saves_key_without_touching_environment(
    app: ui.OmniUI, keychain: FakeKeyring,
) -> None:
    """
    Ayarlar sayfası anahtarı Keychain'e ve süreç-içi depoya yazar, ORTAMA yazmaz: ajanın
    başlattığı alt süreçler sırrı miras almaz. Arka plan rengi de temayla aynıdır.
    """
    app._open_settings()
    window = app._settings_window
    assert window is not None and window.winfo_exists()
    assert window.title() == "OmniAgent — Ayarlar"
    assert window.cget("fg_color") == ui.BG
    entries: List[object] = _of_kind(window, ctk.CTkEntry)
    assert len(entries) == len(api_keys.KEY_VARIABLES)
    assert len(_save_buttons(window)) == 1

    variable: str = api_keys.KEY_VARIABLES[0]
    _fill(entries[0], "tk-test-anahtari")
    _save_buttons(window)[0].invoke()
    assert api_keys.stored_key(variable) == "tk-test-anahtari"
    assert config.load_api_key(variable) == "tk-test-anahtari"
    assert config.BACKENDS["openai"]["api_key"] == "tk-test-anahtari"
    assert variable not in os.environ

    # Alanı boşaltıp kaydetmek kaydı siler ve profili kullanılamaz yapar.
    _fill(entries[0], "")
    _save_buttons(window)[0].invoke()
    assert api_keys.stored_key(variable) is None
    assert config.load_api_key(variable) is None
    assert config.BACKENDS["openai"]["api_key"] is None
    window.destroy()


def test_partial_save_still_rebuilds_clients(
    app: ui.OmniUI, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bir alan kaydedilemese bile başarılı anahtar bekletilmez: istemciler yeniden kurulur."""
    import keyring.backends.macOS
    store: FakeKeyring = FakeKeyring(refuse_write={api_keys.KEY_VARIABLES[1]})
    monkeypatch.setattr(keyring.backends.macOS, "Keyring", lambda: store)
    rebuilt: List[bool] = []

    def counting_clients() -> Dict[str, AsyncOpenAI]:
        rebuilt.append(True)
        return {}

    monkeypatch.setattr(ui, "create_model_clients", counting_clients)
    app._open_settings()
    window = app._settings_window
    assert window is not None
    entries: List[object] = _of_kind(window, ctk.CTkEntry)
    _fill(entries[0], "kaydedilen")
    _fill(entries[1], "reddedilen")
    _save_buttons(window)[0].invoke()
    assert rebuilt == [True]
    assert api_keys.stored_key(api_keys.KEY_VARIABLES[0]) == "kaydedilen"
    assert config.load_api_key(api_keys.KEY_VARIABLES[1]) is None
    window.destroy()


def test_settings_page_reveals_keys_on_request(app: ui.OmniUI, keychain: FakeKeyring) -> None:
    """Anahtarlar varsayılan olarak maskeli; 'Anahtarları göster' kutusu maskeyi kaldırır."""
    app._open_settings()
    window = app._settings_window
    assert window is not None
    entries: List[object] = _of_kind(window, ctk.CTkEntry)
    checks: List[object] = _of_kind(window, ctk.CTkCheckBox)
    assert entries and checks
    assert all(entry.cget("show") == "•" for entry in entries)
    checks[0].toggle()
    assert all(entry.cget("show") == "" for entry in entries)
    window.destroy()
