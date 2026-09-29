"""Yedek sağlayıcı izninin çözümü ve saf karar yardımcıları (gerçek dosya, tablo güdümlü)."""
import json
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Tuple

import pytest

from omniagent import fallback_policy as policy
from omniagent.config import BACKENDS


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """conftest'teki 'none' sabitlemesini bu modülde kaldırır; kayıt dosyası test dizininde durur."""
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    monkeypatch.delenv(policy.FALLBACK_BACKENDS_VARIABLE, raising=False)
    monkeypatch.delenv(policy.FALLBACK_IMAGES_VARIABLE, raising=False)


@pytest.mark.parametrize("raw,expected", [
    ("", ()), ("none", ()), (" NONE ", ()),
    ("openrouter, openai", ("openai", "openrouter")), ("OpenAI", ("openai",)),
    ("ollama-cloud,openai,openrouter", ("ollama-cloud", "openai", "openrouter")),
])
def test_backend_list_grammar(raw: str, expected: Tuple[str, ...]) -> None:
    assert policy.parse_backend_list(raw) == expected


@pytest.mark.parametrize("raw", ["opencode", "gpt", "openai,bilinmeyen"])
def test_non_ladder_profile_is_rejected(raw: str) -> None:
    with pytest.raises(ValueError, match="Yedek sağlayıcı olamayan"):
        policy.parse_backend_list(raw)


def test_default_is_strict_when_nothing_is_configured() -> None:
    assert policy.load_fallback_policy() == {"backends": (), "allow_images": False}


def test_saved_file_is_the_second_source() -> None:
    policy.fallback_policy_path().write_text(
        json.dumps({"backends": ["openrouter", "openai"], "allow_images": True}), encoding="utf-8",
    )
    assert policy.load_fallback_policy() == {"backends": ("openai", "openrouter"), "allow_images": True}


def test_environment_is_authoritative_and_ignores_saved_file(monkeypatch: pytest.MonkeyPatch) -> None:
    policy.fallback_policy_path().write_text(
        json.dumps({"backends": ["openai", "openrouter"], "allow_images": True}), encoding="utf-8",
    )
    monkeypatch.setenv(policy.FALLBACK_BACKENDS_VARIABLE, "openrouter")
    # Tanımsız görüntü anahtarı kapalı sayılır; kayıttaki allow_images=True alan bazında birleştirilmez.
    assert policy.load_fallback_policy() == {"backends": ("openrouter",), "allow_images": False}
    monkeypatch.setenv(policy.FALLBACK_BACKENDS_VARIABLE, "none")
    monkeypatch.setenv(policy.FALLBACK_IMAGES_VARIABLE, "1")
    assert policy.load_fallback_policy() == {"backends": (), "allow_images": True}
    # Boş değer de tanımlıdır ve kapalı sayılır: kayıt dosyasına düşülmez.
    monkeypatch.setenv(policy.FALLBACK_BACKENDS_VARIABLE, "")
    monkeypatch.setenv(policy.FALLBACK_IMAGES_VARIABLE, "")
    assert policy.load_fallback_policy() == {"backends": (), "allow_images": False}


def test_invalid_environment_values_raise_instead_of_falling_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(policy.FALLBACK_BACKENDS_VARIABLE, "none")
    monkeypatch.setenv(policy.FALLBACK_IMAGES_VARIABLE, "belki")
    with pytest.raises(ValueError, match="OMNI_FALLBACK_IMAGES"):
        policy.load_fallback_policy()
    monkeypatch.setenv(policy.FALLBACK_BACKENDS_VARIABLE, "opencode")
    monkeypatch.delenv(policy.FALLBACK_IMAGES_VARIABLE)
    with pytest.raises(ValueError, match="OMNI_FALLBACK_BACKENDS"):
        policy.load_fallback_policy()


@pytest.mark.parametrize("text", [
    "{bozuk", "[]",
    json.dumps({"backends": [], "allow_images": False, "fazla": 1}),
    json.dumps({"backends": ["opencode"], "allow_images": False}),
    json.dumps({"backends": []}),
    json.dumps({"backends": "openai", "allow_images": False}),
])
def test_corrupt_or_unknown_saved_fields_raise_instead_of_falling_back(text: str) -> None:
    policy.fallback_policy_path().write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        policy.load_fallback_policy()


def test_none_is_valid_only_in_the_environment_and_each_error_says_how_to_disable_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    'none' yalnız ortam değişkeninde geçerlidir; kayıt dosyasında yedek istemeyen boş liste yazar. Eskiden ortak ileti kayıt
    dosyası için de 'ya da none' diyordu (["none"] yazan kullanıcı yine hata alırdı). Kayıt hatası dosya yolunu da söyler.
    """
    path: Path = policy.fallback_policy_path()
    path.write_text(json.dumps({"backends": ["none"], "allow_images": False}), encoding="utf-8")
    with pytest.raises(ValueError) as saved:
        policy.load_fallback_policy()
    assert "ya da 'none'" not in str(saved.value) and "yazın: \"backends\": []" in str(saved.value)
    assert str(path) in str(saved.value)
    monkeypatch.setenv(policy.FALLBACK_BACKENDS_VARIABLE, "opencode")
    with pytest.raises(ValueError) as from_environment:
        policy.load_fallback_policy()
    assert "'none' yazın" in str(from_environment.value) and "boş liste" not in str(from_environment.value)
    # Kayıt dosyasında boş liste gerçekten yedeksizdir
    monkeypatch.delenv(policy.FALLBACK_BACKENDS_VARIABLE)
    path.write_text(json.dumps({"backends": [], "allow_images": False}), encoding="utf-8")
    assert policy.load_fallback_policy() == {"backends": (), "allow_images": False}


@pytest.mark.parametrize("backends,allow_images,images,expected", [
    (frozenset(), True, 0, frozenset()),
    (frozenset({"openai"}), False, 0, frozenset({"openai"})),
    (frozenset({"openai"}), False, 2, frozenset()),
    (frozenset({"openai"}), True, 2, frozenset({"openai"})),
])
def test_images_need_separate_permission(
    backends: frozenset[str], allow_images: bool, images: int, expected: frozenset[str],
) -> None:
    assert policy.permitted_fallbacks(backends, allow_images, images) == expected


def test_count_image_parts_counts_only_image_url_parts() -> None:
    messages: List[Mapping[str, object]] = [
        {"role": "user", "content": "düz metin"},
        {"role": "user", "content": [{"type": "text", "text": "x"}, {"type": "image_url", "image_url": {"url": "d"}}]},
        {"role": "assistant", "tool_calls": []},
    ]
    assert policy.count_image_parts(messages) == 1


def test_permanent_fallback_needs_image_permission_too() -> None:
    allowed = frozenset({"openai"})
    assert policy.fallback_recipient_problem("ollama-cloud", "ollama-cloud", allowed, False, 3) is None
    assert policy.fallback_recipient_problem("openai", "ollama-cloud", allowed, False, 0) is None
    assert "OMNI_FALLBACK_IMAGES" in str(policy.fallback_recipient_problem("openai", "ollama-cloud", allowed, False, 3))
    assert policy.fallback_recipient_problem("openai", "ollama-cloud", allowed, True, 3) is None
    assert "izin listesinde yok" in str(policy.fallback_recipient_problem("openrouter", "ollama-cloud", allowed, True, 0))


def test_declined_hint_names_only_ready_but_declined_profiles() -> None:
    ready = frozenset({"ollama-cloud", "openai", "openrouter"})
    only_selected = frozenset({"ollama-cloud"})
    assert policy.fallback_declined_hint("ollama-cloud", only_selected, frozenset(), False, 0) == ""
    general = policy.fallback_declined_hint("ollama-cloud", ready, frozenset(), False, 0)
    assert "openai, openrouter" in general and "OMNI_FALLBACK_BACKENDS" in general
    images = policy.fallback_declined_hint("ollama-cloud", ready, frozenset({"openai"}), False, 2)
    assert "OMNI_FALLBACK_IMAGES" in images and "2 ekran görüntüsü" in images
    # İzinli ve görüntü sorunu olmayan profil atlanmış sayılmaz.
    assert policy.fallback_declined_hint("ollama-cloud", ready, frozenset({"openai", "openrouter"}), False, 0) == ""


READY = frozenset({"ollama-cloud", "openai", "openrouter"})


@pytest.mark.parametrize("selected,available,backends,allow_images,attached,expected", [
    # Varsayılan (Otomatik) profil hazır değil: izin yoksa taşıma yok.
    ("ollama-cloud", frozenset({"openai"}), frozenset(), False, 0, None),
    ("ollama-cloud", frozenset({"openai"}), frozenset({"openai"}), False, 0, "openai"),
    # Görsel ek görüntü izni ister.
    ("ollama-cloud", frozenset({"openai"}), frozenset({"openai"}), False, 2, None),
    ("ollama-cloud", frozenset({"openai"}), frozenset({"openai"}), True, 2, "openai"),
    # İlk HAZIR profil değil, ilk hazır VE izinli profil seçilir (merdiven sırasıyla).
    ("ollama-cloud", frozenset({"openai", "openrouter"}), frozenset({"openrouter"}), False, 0, "openrouter"),
    ("ollama-cloud", frozenset({"openai", "openrouter"}), frozenset({"openai", "openrouter"}), False, 0, "openai"),
    # opencode* hiçbir zaman yedek olamaz; seçilen profilin kendisi aday değildir.
    ("ollama-cloud", frozenset({"opencode"}), frozenset({"openai"}), True, 0, None),
    ("openai", READY, frozenset({"openai", "ollama-cloud"}), False, 0, "ollama-cloud"),
    ("openai", frozenset({"openai"}), frozenset({"openai"}), False, 0, None),
])
def test_startup_replacement_is_first_ready_and_permitted_ladder_profile(
    selected: str, available: frozenset[str], backends: frozenset[str], allow_images: bool, attached: int,
    expected: Optional[str],
) -> None:
    assert policy.startup_replacement(selected, available, backends, allow_images, attached) == expected


@pytest.mark.parametrize("available,backends,allow_images,attached,phrases", [
    # Hazır yedek adayı var ama izin yok.
    (frozenset({"openai"}), frozenset(), False, 0, ("yedek sağlayıcı izni yok", "(openai)", "OMNI_FALLBACK_BACKENDS")),
    (frozenset({"openai", "openrouter"}), frozenset(), False, 0, ("(openai, openrouter)",)),
    # Görsel ek: profil izinli ama görüntü izni kapalı.
    (frozenset({"openai"}), frozenset({"openai"}), False, 2, ("2 görsel ek", "OMNI_FALLBACK_IMAGES=1")),
    # Hazır yedek adayı yok (yalnız opencode ya da hiçbiri): açık seçim öğütlenir.
    (frozenset({"opencode"}), frozenset({"openai"}), True, 0, ("yedek olabilecek hazır profil yok", "/model")),
    (frozenset(), frozenset(), False, 0, ("yedek olabilecek hazır profil yok",)),
])
def test_startup_substitution_problem_is_actionable(
    available: frozenset[str], backends: frozenset[str], allow_images: bool, attached: int,
    phrases: Tuple[str, ...],
) -> None:
    message = policy.startup_substitution_problem("ollama-cloud", available, backends, allow_images, attached)
    assert "'ollama-cloud' profili hazır değil" in message
    assert all(phrase in message for phrase in phrases)


@pytest.mark.parametrize("backends,allowed", [
    (frozenset(), False), (frozenset({"openrouter"}), False), (frozenset({"openai"}), True),
    (frozenset({"ollama-cloud", "openai"}), True),
])
def test_voice_transcription_needs_openai_in_the_allow_list(backends: frozenset[str], allowed: bool) -> None:
    problem = policy.voice_transcription_problem(backends)
    assert (problem is None) is allowed
    if problem is not None:
        assert "OMNI_FALLBACK_BACKENDS=openai" in problem and "OpenAI" in problem


def test_every_grant_message_names_the_record_file_for_processes_without_the_shell_environment() -> None:
    """
    Finder/launchd ile açılan uygulama ve Telegram köprüsü (launchd) kabuk ortamını miras almaz: izin verme iletileri
    yalnız OMNI_FALLBACK_* değişkenini değil kayıt dosyasının yolunu ve biçimini de söyler.
    """
    path: str = str(policy.fallback_policy_path())
    openai_only, ready = frozenset({"openai"}), frozenset({"ollama-cloud", "openai"})
    messages: Dict[str, Optional[str]] = {
        "ses": policy.voice_transcription_problem(frozenset()),
        "başlangıç-izin": policy.startup_substitution_problem("ollama-cloud", openai_only, frozenset(), False, 0),
        "başlangıç-görüntü": policy.startup_substitution_problem("ollama-cloud", openai_only, openai_only, False, 2),
        "kalıcı-yedek-görüntü": policy.fallback_recipient_problem("openai", "ollama-cloud", openai_only, False, 3),
        "atlanan-izin": policy.fallback_declined_hint("ollama-cloud", ready, frozenset(), False, 0),
        "atlanan-görüntü": policy.fallback_declined_hint("ollama-cloud", ready, openai_only, False, 2),
    }
    for name, message in messages.items():
        assert message is not None and path in message and '"allow_images"' in message, name
    assert '"backends": ["openai"]' in str(messages["ses"])


def test_processor_labels_cover_every_profile() -> None:
    assert set(policy.PROCESSOR_LABELS) == set(BACKENDS)


def test_event_and_audit_record_carry_target_reason_and_image_count_but_no_content() -> None:
    event = policy.provider_fallback_event("ollama-cloud", "openai", "hız sınırı (HTTP 429)", 2)
    assert (event["to_backend"], event["to_model"], event["image_count"]) == (
        "openai", BACKENDS["openai"]["model"], 2,
    )
    record: Dict[str, str] = dict(policy.provider_fallback_audit(event, "2026-09-29T12:00:00+00:00"))
    assert record["category"] == "provider_fallback" and record["tool"] == "model:openai"
    assert "hız sınırı" in record["summary"] and "görüntü: 2" in record["summary"]
