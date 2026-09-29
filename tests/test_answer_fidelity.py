"""Nihai yanıt kod doğrulamasının saf parçaları: belirteç çıkarımı, karışabilir anahtar, sınıflama ve düzeltme."""
from __future__ import annotations

from typing import List, Tuple

import pytest

from omniagent.app.answer_fidelity import (
    FidelityReport, apply_token_corrections, check_answer_fidelity, code_tokens, confusable_key,
    observed_step_texts,
)
from omniagent.app.partial_report import UNKNOWN_CHALLENGE_URL, access_challenge_notes, stopped_at_access_wall
from omniagent.core.state import StepRecord, make_step_record
from omniagent.tools.bot_wall import (
    access_challenge_error, classify_access_challenge, human_verification_error, walled_host_error,
)

CYRILLIC_ES: str = "С"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("KODLAR: IL-0C1EAF,IL-26B4E6", ("IL-0C1EAF", "IL-26B4E6"), id="kod-listesi"),
        pytest.param("kod (BT-7421).", ("BT-7421",), id="parantez-ve-nokta"),
        pytest.param("2026-09-29 tarihinde 12345 kayıt", (), id="yalniz-rakam"),
        pytest.param("LONG_RESEARCH: DONE ve KOD-ABCD", (), id="yalniz-harf"),
        pytest.param("A1B2 kısa", (), id="cok-kisa"),
        pytest.param("Kar1yer-Merkezi", ("Kar1yer-Merkezi",), id="sozcuk-ici-rakam"),
        pytest.param("--IL-0C1EAF-- _IL-0C1EAF_", ("IL-0C1EAF",), id="sinir-tire-altcizgi-ve-tekrar"),
        pytest.param("x" * 64 + "1", (), id="64-karakter-ustu"),
    ],
)
def test_code_tokens_keep_only_letter_and_digit_mixed_tokens(text: str, expected: Tuple[str, ...]) -> None:
    assert code_tokens(text) == expected


@pytest.mark.parametrize(
    ("first", "second", "same"),
    [
        pytest.param("IL-0C1EAF", "IL-OC1EAF", True, id="sifir-o"),
        pytest.param("IL-0C1EAF", f"IL-0{CYRILLIC_ES}1EAF", True, id="kiril-c"),
        pytest.param("BT-7421", "BT-742l", True, id="bir-kucuk-l"),
        pytest.param("Çağrı12", "Cagri12", True, id="aksan"),
        pytest.param("IL-0C1EAF", "IL-0C1EAE", False, id="gercek-fark"),
        pytest.param("IL-0C1EAF", "IL-0C1EA", False, id="eksik-karakter"),
    ],
)
def test_confusable_key_equality(first: str, second: str, same: bool) -> None:
    assert (confusable_key(first) == confusable_key(second)) is same


def _summary(report: FidelityReport) -> Tuple[
    List[Tuple[str, Tuple[str, ...]]], List[Tuple[str, Tuple[str, ...]]], List[str],
]:
    return (
        [(finding.token, finding.observed) for finding in report.corrections],
        [(finding.token, finding.observed) for finding in report.ambiguous],
        list(report.unobserved),
    )


@pytest.mark.parametrize(
    ("verified", "answer", "corrections", "ambiguous", "unobserved", "corrected"),
    [
        pytest.param(
            {"IL-0C1EAF", "IL-26B4E6"}, "KODLAR: IL-OC1EAF,IL-26B4E6",
            [("IL-OC1EAF", ("IL-0C1EAF",))], [], [], "KODLAR: IL-0C1EAF,IL-26B4E6", id="tek-aday-duzelir",
        ),
        pytest.param(
            {"IL-0C1EAF", "IL-OC1EAF"}, "IL-0CIEAF",
            [], [("IL-0CIEAF", ("IL-0C1EAF", "IL-OC1EAF"))], [], "IL-0CIEAF", id="iki-aday-duzeltilmez",
        ),
        pytest.param({"IL-0C1EAF"}, "IL-9F3A21", [], [], ["IL-9F3A21"], "IL-9F3A21", id="aday-yok-dokunulmaz"),
        pytest.param({"IL-0C1EAF"}, "il-0c1eaf", [], [], [], "il-0c1eaf", id="yalniz-harf-boyu-farki"),
        pytest.param(set(), "IL-0C1EAF", [], [], ["IL-0C1EAF"], "IL-0C1EAF", id="bos-korpus"),
        pytest.param({"IL-0C1EAF"}, "TOPLAM: 31 ve 2026-09-29", [], [], [], "TOPLAM: 31 ve 2026-09-29", id="sayilar"),
        pytest.param(
            {"Kar1yer-Merkezi"}, "Kariyer-Merkezi açık", [], [], [], "Kariyer-Merkezi açık",
            id="sozcuk-duzeltmesi-geri-alinmaz",
        ),
        pytest.param(
            {"IL-0C1EAF", "il-0c1eaf"}, "KOD: IL-OC1EAF",
            [("IL-OC1EAF", ("IL-0C1EAF",))], [], [], "KOD: IL-0C1EAF", id="harf-boyu-farkli-gozlem-tek-aday",
        ),
        pytest.param(
            {"IL-0C1EAF"}, "kod:(IL-OC1EAF)! ve _IL-OC1EAF_",
            [("IL-OC1EAF", ("IL-0C1EAF",))], [], [], "kod:(IL-0C1EAF)! ve _IL-0C1EAF_", id="sinirlar-korunur",
        ),
    ],
)
def test_fidelity_classification_and_correction(
    verified: set[str], answer: str, corrections: List[Tuple[str, Tuple[str, ...]]],
    ambiguous: List[Tuple[str, Tuple[str, ...]]], unobserved: List[str], corrected: str,
) -> None:
    report = check_answer_fidelity(answer, frozenset(verified))
    assert _summary(report) == (corrections, ambiguous, unobserved)
    fixed = apply_token_corrections(answer, report.corrections)
    assert fixed == corrected
    # Düzeltilmiş yanıt yeniden denetlenince düzeltme kalmaz
    assert not check_answer_fidelity(fixed, frozenset(verified)).corrections


def test_observed_step_texts_skip_failed_steps_except_the_access_wall_message() -> None:
    """Başarısız adımın metni gözlem sayılmaz; host'un erişim engeli iletisi (adres, başlık) sayılır."""
    page = "<html><head><title>Just a moment...</title></head><body></body></html>"
    challenge = classify_access_challenge(page, "Just a moment...")
    assert challenge is not None
    wall_text = f"ToolError: {access_challenge_error('https://ornek.test/ilan/IL-0C1EAF', challenge, page)}"
    steps = [
        make_step_record("read_file", "{}", True, "kod IL-0C1EAF"),
        make_step_record("write_file", "{}", False, "ToolError: yazılan kod IL-OC1EAF geçersiz"),
        make_step_record("fetch_raw", "{}", False, wall_text),
    ]
    assert observed_step_texts(steps) == ("kod IL-0C1EAF", wall_text)


WALL_PAGE: str = "<html><head><title>Just a moment...</title></head><body></body></html>"


def _wall_step(url: str) -> StepRecord:
    """Gerçek access_challenge_error iletisini taşıyan başarısız adım (araç hatası 'ToolError: ...' biçimindedir)."""
    challenge = classify_access_challenge(WALL_PAGE, "Just a moment...")
    assert challenge is not None
    return make_step_record("fetch_raw", "{}", False, f"ToolError: {access_challenge_error(url, challenge, WALL_PAGE)}")


def test_access_challenge_notes_read_the_address_only_from_a_scheme_url() -> None:
    """
    Adres yalnız şemalı adrestir: eski kalıp 'MARKER: ' sonrasındaki ilk boşluksuz sözcüğü adres sayıyordu, adres taşımayan
    human_verification_error iletisinde ('...: ekrandaki ...') AccessChallengeNote(url='ekrandaki') çıkıyordu.
    """
    wall = _wall_step("https://ornek.test/ilan?a=1")
    credentials = _wall_step("https://kullanici:gizlisifre123@ornek.test/x")
    not_robot_error = human_verification_error("I'm not a robot")
    button = make_step_record("cua_click_text", "{}", False, f"ToolError: {not_robot_error}")
    repeated = make_step_record("fetch_raw", "{}", False, wall["detail"])
    notes = access_challenge_notes([wall, credentials, button, repeated])
    assert [note.url for note in notes] == ["https://ornek.test/ilan?a=1", "https://kullanici:[gizli]@ornek.test/x", UNKNOWN_CHALLENGE_URL]
    assert [note.signal for note in notes] == ["cloudflare-interstitial", "cloudflare-interstitial", ""]
    assert all("ekrandaki" != note.url and "gizlisifre123" not in note.url for note in notes)
    previous = make_step_record("fetch_raw", "{}", False, f"ToolError: {walled_host_error('https://ornek.test/baska', frozenset({'ornek.test'}))}")
    assert [(note.url, note.signal) for note in access_challenge_notes([previous])] == [("https://ornek.test/baska", "önceki-engel")]


HONEST_WALL_ANSWERS: List[str] = [
    "Bot doğrulaması nedeniyle durdum: sayfa doğrulama sayfası döndürdü; engeli aşmaya çalışmadım.",
    "Siteye erişim engellendi (Cloudflare); engeli aşmaya çalışmadım.",
    "Erişim engeli var, işlemi tamamlayamadım.",
    "Site insan doğrulaması istiyor; doğrulamayı kullanıcı kendi Chrome'unda tamamlamalı.",
    "Sayfa doğrulama veya engelleme sayfası döndürdü, bu yüzden durdum.",
    "İçerik bir CAPTCHA arkasında.",
    "I was blocked by a bot check on the page, so I stopped without trying to bypass it.",
    "The site returned a Cloudflare challenge page; I did not try to bypass it.",
    "Access requires human verification, stopping here.",
    "Site erişimi engelledi, işlemi yarım bıraktım.",
]
COMPLETION_CLAIMS: List[str] = [
    "Dosya kaydedildi, doğrulama tamamlandı.", "Kayıt engelsiz tamamlandı.", "Tamamlandı: ödeme yapıldı.",
    "Bu bir engelli erişim raporu.", "Dosya engellenmeden kaydedildi.", "Doğrulama başarılı, kayıt oluşturuldu.",
    "Görev bitti; engel yok.", "All done, the record was saved.", "Kayıt işlemini engellemedi, dosya yazıldı.",
    "Engeller kalktı, tüm dosyalar kaydedildi.", "Unblocked the queue and saved the file.",
]


@pytest.mark.parametrize("answer", HONEST_WALL_ANSWERS)
def test_honest_wall_answers_count_as_stopping_at_the_wall(answer: str) -> None:
    assert stopped_at_access_wall(answer, [_wall_step("https://ornek.test/ilan")])


@pytest.mark.parametrize("answer", COMPLETION_CLAIMS)
def test_completion_claims_do_not_count_as_stopping_at_the_wall(answer: str) -> None:
    """
    'dogrulama' ve 'engel' alt dizgisi olarak eşleşmez: engel sonrası tamamlanmış gibi konuşan yanıt kanıt kapılarına takılır
    (eskiden 'Dosya kaydedildi, doğrulama tamamlandı.' dürüst durma sayılıp kapıları atlatıyordu).
    """
    assert not stopped_at_access_wall(answer, [_wall_step("https://ornek.test/ilan")])


def test_stopping_at_the_wall_needs_a_wall_error_and_no_later_success() -> None:
    honest = HONEST_WALL_ANSWERS[0]
    assert not stopped_at_access_wall(honest, [make_step_record("read_file", "{}", True, "tamam")])  # engel hatası yok
    recovered = [_wall_step("https://ornek.test/ilan"), make_step_record("read_file", "{}", True, "tamam")]
    assert not stopped_at_access_wall(honest, recovered)  # engelden sonra başarılı adım var
