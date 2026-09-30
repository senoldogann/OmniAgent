"""
Ön plan denetimi (tools/foreground.py) ve klavye araçlarındaki koruma: saf karar tabloları (mock yok), yoklama
davranışı (yalnız read_front_app taklidi), araçlardaki denetim SIRASI ve hedef beyanı, cua_get_app; macOS'ta gerçek
AX okuması. Canlı (gerçek AX, kendi test uygulaması) sınamalar tests/test_ax_live_chrome.py içindedir.
"""
import subprocess
import sys
import time
import unicodedata
import uuid
from types import SimpleNamespace
from typing import Iterator, List, Optional, Tuple, Union

import ApplicationServices as AX
import pytest

from omniagent import tools
from omniagent.tools import ToolError, Toolbox, browser, foreground, gui_input
from omniagent.tools.foreground import AppReadiness, FrontApp, HostIdentity
from omniagent.tools.types import TOOL_RUNTIME, ApprovalRefused

NOTES: FrontApp = {"pid": 10, "name": "Notlar", "bundle_name": "Notes", "bundle_id": "com.apple.Notes"}
CHROME: FrontApp = {"pid": 11, "name": "Google Chrome", "bundle_name": "Google Chrome", "bundle_id": "com.google.Chrome"}
TERMINAL: FrontApp = {"pid": 12, "name": "Terminal", "bundle_name": "Terminal", "bundle_id": "com.apple.Terminal"}
ONEPASSWORD: FrontApp = {"pid": 13, "name": "1Password", "bundle_name": "1Password", "bundle_id": "com.1password.1password"}
SETTINGS: FrontApp = {
    "pid": 14, "name": "Sistem Ayarları", "bundle_name": "System Settings", "bundle_id": "com.apple.systempreferences",
}
OMNI_UI: FrontApp = {"pid": 15, "name": "OmniAgent", "bundle_name": "OmniAgent", "bundle_id": "com.omniagent.desktop"}
LAUNCHER: FrontApp = {"pid": 16, "name": "Başlatıcı", "bundle_name": "Launcher", "bundle_id": "com.example.host"}
HOST: HostIdentity = {"pid": 99, "bundle_id": "com.example.host"}

OWN_UI = "OmniAgent arayüzü"
HOST_APP = "OmniAgent'ı çalıştıran uygulama"


# --- Saf kararlar ---

@pytest.mark.parametrize("expected, front, matches", [
    ("Notes", NOTES, True), ("notlar", NOTES, True), ("Notes.app", NOTES, True), ("  Notlar  ", NOTES, True),
    ("Google Chrome", CHROME, True), ("Chrome", CHROME, False), ("", CHROME, False), ("   ", CHROME, False),
    ("Safari", NOTES, False),
])
def test_app_matches_uses_localized_or_bundle_name(expected: str, front: FrontApp, matches: bool) -> None:
    assert foreground.app_matches(expected, front) is matches


def test_app_matches_ignores_unicode_normalization_form() -> None:
    """macOS dosya adları ayrışık (NFD) gelebilir; 'ğ' iki biçimde de aynı uygulamadır."""
    photos: FrontApp = {"pid": 20, "name": "Fotoğraflar", "bundle_name": "Photos", "bundle_id": "com.apple.Photos"}
    for form in ("NFC", "NFD"):
        assert foreground.app_matches(unicodedata.normalize(form, "Fotoğraflar"), photos)
        decomposed: FrontApp = {**photos, "name": unicodedata.normalize(form, photos["name"])}
        assert foreground.app_matches("Fotoğraflar", decomposed)


@pytest.mark.parametrize("front, category", [
    (NOTES, None), (CHROME, None), (TERMINAL, "terminal/IDE"), (ONEPASSWORD, "parola yöneticisi"),
    (SETTINGS, "sistem ayarları/güvenlik penceresi"), (OMNI_UI, OWN_UI),
    ({**NOTES, "pid": 99}, OWN_UI),      # ajanı çalıştıran sürecin kendisi (PID)
    (LAUNCHER, HOST_APP),                # ajanı başlatan uygulama (paket kimliği)
    ({**NOTES, "bundle_id": "com.apple.keychainaccess"}, "parola yöneticisi"),
    ({**NOTES, "bundle_id": "com.apple.SecurityAgent"}, "sistem ayarları/güvenlik penceresi"),
    ({**NOTES, "bundle_id": ""}, None),  # kimliği okunamayan uygulama hassas sayılmaz (yalnız koruma azalır)
])
def test_only_sensitive_apps_are_rejected_without_a_target(front: FrontApp, category: Optional[str]) -> None:
    assert foreground.sensitive_category(front, HOST) == category
    assert (foreground.sensitive_front_problem(front, HOST) is None) is (category is None)
    assert (foreground.sensitive_target_problem(front, HOST) is None) is (category is None)


def test_empty_host_bundle_id_never_matches_an_app_without_bundle_id() -> None:
    """launchd köprüsünde __CFBundleIdentifier boştur: paket kimliği de boş olan uygulama 'ev sahibi' sayılmaz."""
    anonymous: FrontApp = {**NOTES, "bundle_id": ""}
    assert foreground.sensitive_category(anonymous, {"pid": 99, "bundle_id": ""}) is None


def test_sensitive_front_messages_never_suggest_typing_into_our_own_ui() -> None:
    own = foreground.sensitive_front_problem(OMNI_UI, HOST) or ""
    assert OWN_UI in own and "cua_get_app(" not in own and "yazma" in own
    other = foreground.sensitive_front_problem(TERMINAL, HOST) or ""
    assert "cua_get_app('Terminal')" in other and "terminal/IDE" in other and "Klavye olayı gönderilmedi" in other


@pytest.mark.parametrize("code, fragment", [
    (-25212, "görünür penceresi yok"),  # kAXErrorNoValue: ön plandaki uygulamanın görünür penceresi yok
    (-25204, "kendi penceresi"),        # kAXErrorCannotComplete: yanıtsız uygulama ya da OmniAgent'ın kendi penceresi
    (-25200, "kendi penceresi"),
])
def test_unknown_front_hint_names_the_likely_cause_and_promises_no_key_was_sent(code: int, fragment: str) -> None:
    hint = foreground.unknown_front_hint(code)
    assert fragment in hint and "Klavye olayı gönderilmedi" in hint and "take_screenshot" in hint


@pytest.mark.parametrize("spec, handoff", [
    ("cmd+tab", True), ("Cmd+Shift+Tab", True), ("cmd+space", True), ("command+tab", True), ("meta+space", True),
    ("shift+cmd+tab", True), ("cmd+c", False), ("enter", False), ("a", False), ("cmd+shift+t", False),
    ("ctrl+cmd+space", False), ("", False), ("+", False),
])
def test_focus_handoff_keys(spec: str, handoff: bool) -> None:
    assert foreground.focus_handoff_key(spec) is handoff


# --- Yoklama davranışı (yalnız read_front_app taklidi; gerçek AX macOS ister) ---

def _reads(monkeypatch: pytest.MonkeyPatch, outcomes: List[Union[FrontApp, ToolError]]) -> List[int]:
    """read_front_app'i verilen sonuç sırasına bağlar; son sonuç tekrarlanır. Okuma sayısını döner."""
    reads: List[int] = []
    remaining: Iterator[Union[FrontApp, ToolError]] = iter(outcomes)
    last: List[Union[FrontApp, ToolError]] = [outcomes[-1]]

    def read() -> FrontApp:
        reads.append(1)
        outcome = next(remaining, last[0])
        if isinstance(outcome, ToolError):
            raise outcome
        return outcome

    monkeypatch.setattr(foreground, "read_front_app", read)
    monkeypatch.setattr(foreground, "FOREGROUND_POLL_SECONDS", 0.005)
    return reads


def test_require_front_app_waits_for_the_target_to_arrive(monkeypatch: pytest.MonkeyPatch) -> None:
    reads = _reads(monkeypatch, [TERMINAL, TERMINAL, CHROME])
    assert foreground.require_front_app("Google Chrome", 1.0) == CHROME
    assert len(reads) == 3


def test_require_front_app_reports_both_apps_after_the_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    _reads(monkeypatch, [TERMINAL])
    with pytest.raises(ToolError) as mismatch:
        foreground.require_front_app("Google Chrome", 0.05)
    assert mismatch.value.code == "FOREGROUND_MISMATCH" and mismatch.value.recoverable
    assert "Terminal" in str(mismatch.value) and "Google Chrome" in str(mismatch.value)
    assert "Klavye olayı gönderilmedi" in str(mismatch.value)


def test_transient_read_failures_are_retried_and_the_last_error_is_raised_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unknown = ToolError("okunamadı", "FOREGROUND_UNKNOWN", True)
    _reads(monkeypatch, [unknown, unknown, CHROME])
    assert foreground.require_front_app("Google Chrome", 1.0) == CHROME  # geçici hata süre dolmadan toparlandı
    _reads(monkeypatch, [unknown])
    with pytest.raises(ToolError) as failure:
        foreground.require_front_app("Google Chrome", 0.05)
    assert failure.value is unknown


def test_permanent_read_failure_is_raised_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    permission = ToolError("izin yok", "AX_PERMISSION", False)
    reads = _reads(monkeypatch, [permission])
    with pytest.raises(ToolError) as failure:
        foreground.require_front_app("Google Chrome", 5.0)
    assert failure.value is permission and len(reads) == 1


def test_sensitive_front_wait_ends_as_soon_as_a_safe_app_arrives(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(foreground, "host_identity", lambda: HOST)
    _reads(monkeypatch, [TERMINAL, TERMINAL, NOTES])
    assert foreground.require_no_sensitive_front(1.0) == NOTES
    _reads(monkeypatch, [ONEPASSWORD])
    with pytest.raises(ToolError) as blocked:
        foreground.require_no_sensitive_front(0.05)
    assert blocked.value.code == "FOREGROUND_MISMATCH" and "parola yöneticisi" in str(blocked.value)


def test_user_stop_interrupts_the_foreground_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    _reads(monkeypatch, [TERMINAL])
    token = TOOL_RUNTIME.set({"emit_output": lambda line: None, "should_stop": lambda: True})
    try:
        with pytest.raises(ToolError) as stopped:
            foreground.require_front_app("Google Chrome", 30.0)
    finally:
        TOOL_RUNTIME.reset(token)
    assert stopped.value.code == "STOPPED"


def test_wait_app_ready_distinguishes_missing_window_from_wrong_app(monkeypatch: pytest.MonkeyPatch) -> None:
    _reads(monkeypatch, [NOTES])
    monkeypatch.setattr(foreground, "app_has_window", lambda pid: True)
    ready: AppReadiness = foreground.wait_app_ready("Notes", 0.5)
    assert ready == {"front": NOTES, "has_window": True}
    monkeypatch.setattr(foreground, "app_has_window", lambda pid: False)
    windowless: AppReadiness = foreground.wait_app_ready("Notlar", 0.05)  # ön planda ama penceresiz: hata değil
    assert windowless["has_window"] is False and windowless["front"] == NOTES
    with pytest.raises(ToolError) as wrong:
        foreground.wait_app_ready("Safari", 0.05)
    assert wrong.value.code == "FOREGROUND_MISMATCH" and "Safari" in str(wrong.value)


# --- Klavye araçlarında denetim SIRASI ve hedef beyanı ---

def _keyboard_toolbox(monkeypatch: pytest.MonkeyPatch) -> Tuple[Toolbox, List[tuple]]:
    """Klavye araçlarını taklit eden Toolbox; olaylar ve ön plan denetimleri tek listeye SIRAYLA yazılır."""
    events: List[tuple] = []
    monkeypatch.setattr(tools, "_require_accessibility", lambda: None)
    monkeypatch.setattr(tools, "screen_capture_granted", lambda request=False: False)
    monkeypatch.setattr(tools, "type_unicode_text", lambda value: events.append(("type", value)))
    monkeypatch.setattr(tools, "press_key_spec", lambda key: events.append(("key", key)) or "basıldı")
    monkeypatch.setattr(tools, "require_front_app", lambda expected, wait_seconds: events.append(("front", expected, wait_seconds)))
    monkeypatch.setattr(tools, "require_no_sensitive_front", lambda wait_seconds: events.append(("sensitive", wait_seconds)))
    return Toolbox(), events


def test_declared_target_requires_that_app_in_front_and_undeclared_only_rejects_sensitive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    box, events = _keyboard_toolbox(monkeypatch)
    wait = tools.INPUT_FOREGROUND_WAIT_SECONDS
    box.cua_type_text("a")
    box._input_app = "Notes"
    box.cua_type_text("b")
    box.cua_press_key("enter")
    assert events == [("sensitive", wait), ("type", "a"), ("front", "Notes", wait), ("type", "b"), ("front", "Notes", wait), ("key", "enter")]


def test_focus_handoff_keys_skip_the_guard_and_release_the_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """cmd+tab/cmd+space hassas ön plandan çıkış yoludur: engellenmez; sonrasında hedef beklentisi bırakılır."""
    box, events = _keyboard_toolbox(monkeypatch)
    box._input_app = "Notes"
    box.cua_press_key("cmd+c")
    assert box._input_app == "Notes"
    box.cua_press_key("cmd+space")
    assert box._input_app is None
    box.cua_type_text("Notlar")
    box.cua_press_key("Cmd+Shift+Tab")
    wait = tools.INPUT_FOREGROUND_WAIT_SECONDS
    assert events == [
        ("front", "Notes", wait), ("key", "cmd+c"), ("key", "cmd+space"),
        ("sensitive", wait), ("type", "Notlar"), ("key", "Cmd+Shift+Tab"),
    ]


def test_approval_refusal_comes_before_the_click_and_the_foreground_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sıra: insan doğrulaması/onay kapısı → (tıklama) → ön plan koruması → yazım. Reddedilen tıklamada koruma bile koşmaz."""
    box, events = _keyboard_toolbox(monkeypatch)
    monkeypatch.setattr(tools, "current_geometry", lambda: {"model_width": 1000, "model_height": 1000})
    monkeypatch.setattr(tools, "click_model_point", lambda x, y, button, geometry: events.append(("click",)) or "tıklandı")
    refusal = ApprovalRefused("Onay reddedildi", "APPROVAL_DENIED", True)

    def refuse(tool: str, point: Tuple[int, int], geometry: object) -> None:
        raise refusal

    monkeypatch.setattr(box, "_confirm_point_click", refuse)
    with pytest.raises(ToolError) as error:
        box.cua_submit_text([10, 20], "gönder")
    assert error.value is refusal and events == []


def test_failed_guard_sends_no_keyboard_event(monkeypatch: pytest.MonkeyPatch) -> None:
    box, events = _keyboard_toolbox(monkeypatch)
    mismatch = ToolError("ön planda Terminal var", "FOREGROUND_MISMATCH", True)

    def refuse(wait_seconds: float) -> None:
        raise mismatch

    monkeypatch.setattr(tools, "require_no_sensitive_front", refuse)
    for call in (lambda: box.cua_type_text("gizli"), lambda: box.cua_press_key("enter")):
        with pytest.raises(ToolError) as error:
            call()
        assert error.value is mismatch
    assert events == []


# --- cua_get_app ---

def _get_app_toolbox(monkeypatch: pytest.MonkeyPatch, readiness: Union[AppReadiness, ToolError]) -> Tuple[Toolbox, List[List[str]]]:
    """osascript ve wait_app_ready taklidiyle Toolbox; osascript argv'leri döner."""
    runs: List[List[str]] = []

    def fake_run(args, **kwargs):
        runs.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    def ready(app_name: str, wait_seconds: float) -> AppReadiness:
        if isinstance(readiness, ToolError):
            raise readiness
        return readiness

    monkeypatch.setattr(tools.subprocess, "run", fake_run)
    monkeypatch.setattr(tools, "screen_capture_granted", lambda request=False: False)
    monkeypatch.setattr(tools, "_require_accessibility", lambda: None)
    monkeypatch.setattr(tools, "wait_app_ready", ready)
    # Gerçek pencere listesi ve AX testi kirletmesin: ad çözümleme boş listeyle, pencere geri açma yapılmadan.
    monkeypatch.setattr(gui_input, "_dock_app_owners", lambda: [])
    monkeypatch.setattr(tools, "restore_minimized_window", lambda pid: False)
    return Toolbox(), runs


def test_get_app_passes_the_name_as_argv_and_declares_the_target(monkeypatch: pytest.MonkeyPatch) -> None:
    box, runs = _get_app_toolbox(monkeypatch, {"front": NOTES, "has_window": True})
    message = box.cua_get_app('Fotoğraflar "x" \\ \n')
    assert runs[0][:3] == ["osascript", "-e", tools.gui_input._ACTIVATE_APPLESCRIPT]
    assert runs[0][3] == 'Fotoğraflar "x" \\ \n' and "Fotoğraflar" not in runs[0][2]  # ad betik metnine GİRMEZ
    assert "aktif edildi" in message and "penceresi yok" not in message
    assert box._input_app == 'Fotoğraflar "x" \\ \n'


def test_get_app_reports_a_missing_window_without_failing(monkeypatch: pytest.MonkeyPatch) -> None:
    box, _runs = _get_app_toolbox(monkeypatch, {"front": NOTES, "has_window": False})
    assert "Görünür penceresi yok" in box.cua_get_app("Finder") and box._input_app == "Finder"


def test_get_app_does_not_declare_a_target_that_never_reached_the_front(monkeypatch: pytest.MonkeyPatch) -> None:
    mismatch = ToolError("Ön planda Terminal var; beklenen uygulama 'Notes'", "FOREGROUND_MISMATCH", True)
    box, _runs = _get_app_toolbox(monkeypatch, mismatch)
    with pytest.raises(ToolError) as error:
        box.cua_get_app("Notes")
    assert error.value is mismatch and box._input_app is None


def test_get_app_timeout_is_a_typed_error_and_permission_is_checked_before_activation(monkeypatch: pytest.MonkeyPatch) -> None:
    box, _runs = _get_app_toolbox(monkeypatch, {"front": NOTES, "has_window": True})

    def hang(args, **kwargs):
        raise subprocess.TimeoutExpired(args, timeout=kwargs["timeout"])

    monkeypatch.setattr(tools.subprocess, "run", hang)
    with pytest.raises(ToolError) as timeout:
        box.cua_get_app("Notes")
    assert timeout.value.code == "APP_ACTIVATE_TIMEOUT" and timeout.value.recoverable and box._input_app is None
    monkeypatch.setattr(tools.subprocess, "run", lambda *args, **kwargs: pytest.fail("izin yokken etkinleştirilmemeli"))
    denied = ToolError("izin yok", "AX_PERMISSION", False)

    def no_permission() -> None:
        raise denied

    monkeypatch.setattr(tools, "_require_accessibility", no_permission)
    with pytest.raises(ToolError) as error:
        box.cua_get_app("Notes")
    assert error.value is denied


T3: FrontApp = {"pid": 22, "name": "T3 Code (Nightly)", "bundle_name": "T3 Code (Nightly)",
                "bundle_id": "com.t3tools.t3code"}
RUNNING: List[Tuple[str, int]] = [("Finder", 11), ("T3 Code (Nightly)", 22)]


def test_running_app_is_matched_by_exact_name_or_unique_prefix() -> None:
    """Canlı hata (29 Eylül): kullanıcı 'T3 kod' dedi, uygulamanın gerçek adı 'T3 Code (Nightly)'."""
    assert gui_input.match_running_app("t3 code", RUNNING) == ("T3 Code (Nightly)", 22)
    assert gui_input.match_running_app("FINDER", RUNNING) == ("Finder", 11)
    assert gui_input.match_running_app("Cursor", RUNNING) is None
    assert gui_input.match_running_app("S", [("Safari", 1), ("Signal", 2)]) is None


def test_background_services_are_neither_matched_nor_listed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Canlı hata (29 Eylül, düzeltme sonrası): 'Cursor' ön eki arka plan servisi CursorUIViewService'e eşlendi."""
    owners = [("CursorUIViewService", 5), ("Finder", 11), ("T3 Code (Nightly)", 22)]
    policies = {5: 2, 11: 0, 22: 0}  # 0 = NSApplicationActivationPolicyRegular (Dock'ta görünür)
    monkeypatch.setattr(gui_input, "_visible_app_owners", lambda: owners)
    monkeypatch.setattr(gui_input, "running_application",
                        lambda pid: SimpleNamespace(activationPolicy=lambda: policies[pid]))
    assert gui_input.resolve_running_app("Cursor") == ("Cursor", None)
    assert gui_input.resolve_running_app("t3 code") == ("T3 Code (Nightly)", 22)
    assert "CursorUIViewService" not in gui_input._running_apps_text()


def test_get_app_restores_the_minimized_window_before_waiting_for_the_front(monkeypatch: pytest.MonkeyPatch) -> None:
    """Canlı hata (29 Eylül, düzeltme sonrası): tek penceresi küçültülmüş uygulamada ön plan okunamıyor
    (AXFocusedApplication NoValue); bekleme geri açmadan önce yapılınca araç hata veriyordu."""
    box, runs = _get_app_toolbox(monkeypatch, {"front": T3, "has_window": True})
    monkeypatch.setattr(gui_input, "_dock_app_owners", lambda: RUNNING)
    calls: List[str] = []

    def restore(pid: int) -> bool:
        calls.append(f"restore:{pid}")
        return True

    def ready(app_name: str, wait_seconds: float) -> AppReadiness:
        calls.append(f"ready:{app_name}")
        return {"front": T3, "has_window": True}

    monkeypatch.setattr(tools, "restore_minimized_window", restore)
    monkeypatch.setattr(tools, "wait_app_ready", ready)
    message = box.cua_get_app("T3 Code")
    assert runs[0][3] == "T3 Code (Nightly)" and box._input_app == "T3 Code (Nightly)"
    assert calls == ["restore:22", "ready:T3 Code (Nightly)"]
    assert "Küçültülmüş penceresi geri açıldı" in message


def test_failed_activation_lists_running_apps_instead_of_pointing_to_a_screenshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Canlı hata (29 Eylül): uydurma 'Cursor' adı zaman aşımına düştü; hata 'ekran görüntüsüne bak' deyince model
    masaüstü görüntüsünden 'uygulama açık değil' sonucuna vardı."""
    box, _runs = _get_app_toolbox(monkeypatch, {"front": NOTES, "has_window": True})
    monkeypatch.setattr(gui_input, "_dock_app_owners", lambda: RUNNING)

    def hang(args, **kwargs):
        raise subprocess.TimeoutExpired(args, timeout=kwargs["timeout"])

    monkeypatch.setattr(tools.subprocess, "run", hang)
    with pytest.raises(ToolError) as timeout:
        box.cua_get_app("Cursor")
    assert timeout.value.code == "APP_ACTIVATE_TIMEOUT" and timeout.value.recoverable
    assert "T3 Code (Nightly)" in str(timeout.value) and "take_screenshot ile bak" not in str(timeout.value)


def test_minimized_window_is_restored_only_when_every_window_is_minimized(monkeypatch: pytest.MonkeyPatch) -> None:
    minimized = {"w1": True, "w2": True}
    writes: List[Tuple[object, str, object]] = []

    def attribute(element: object, name: str) -> object:
        if name == "AXWindows":
            return list(minimized)
        return minimized[str(element)] if name == "AXMinimized" else None

    def write(element: object, name: str, value: object) -> int:
        writes.append((element, name, value))
        return 0

    monkeypatch.setattr(gui_input, "_ax_attribute", attribute)
    monkeypatch.setattr(gui_input.AX, "AXUIElementCreateApplication", lambda pid: f"app-{pid}")
    monkeypatch.setattr(gui_input.AX, "AXUIElementSetAttributeValue", write)
    assert gui_input.restore_minimized_window(22) is True and writes == [("w1", "AXMinimized", False)]
    minimized["w2"] = False
    writes.clear()
    assert gui_input.restore_minimized_window(22) is False and writes == []


@pytest.mark.skipif(sys.platform != "darwin", reason="osascript yalnız macOS'ta")
def test_real_hung_osascript_becomes_a_typed_timeout_and_the_process_is_killed(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Gerçek osascript takılması (Apple olayı gönderilmez: betik yalnız bekler): iki yol da tipli süre aşımı verir, süreç ölür.
    Yetim süreç denetimi BU testin betiğine özgü benzersiz işaretçiyle yapılır: eski 'pgrep -f "delay 45"' aynı komut satırını
    taşıyan her süreçle (eşzamanlı başka bir koşunun osascript'i dahil) eşleşip testi yanlış kırabilirdi.
    """
    marker = f"omni-takili-betik-{uuid.uuid4().hex}"
    hang = f"on run argv\n-- {marker}\ndelay 45\nend run"
    monkeypatch.setattr(gui_input, "_ACTIVATE_APPLESCRIPT", hang)
    monkeypatch.setattr(gui_input, "CUA_ACTIVATE_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(browser, "_CHROME_TAB_APPLESCRIPT", hang)
    monkeypatch.setattr(browser, "CHROME_SCRIPT_TIMEOUT_SECONDS", 1.0)
    started = time.monotonic()
    with pytest.raises(ToolError) as activate:
        gui_input.CUA().get_app("Deneme")
    with pytest.raises(ToolError) as chrome:
        browser.run_chrome_active_tab("https://example.com/yavas", None, False)
    assert activate.value.code == "APP_ACTIVATE_TIMEOUT" and activate.value.recoverable
    assert chrome.value.code == "CHROME_SCRIPT_TIMEOUT" and chrome.value.recoverable
    assert time.monotonic() - started < 10
    leftovers = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True).stdout.split()
    assert leftovers == []  # zaman aşımında subprocess.run osascript'i öldürür: yetim süreç kalmaz


@pytest.mark.skipif(sys.platform != "darwin", reason="pgrep davranışı macOS için doğrulandı")
def test_orphan_check_marker_finds_a_live_process_and_only_that_one() -> None:
    """Kontrol: benzersiz işaretçili pgrep canlı süreci bulur, başka işaretçiyi bulmaz (yetim denetimi boş geçmesin)."""
    marker = f"omni-kontrol-{uuid.uuid4().hex}"
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", marker])
    try:
        found = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True).stdout.split()
        assert found == [str(process.pid)]
        assert subprocess.run(["pgrep", "-f", f"{marker}-baska"], capture_output=True, text=True).stdout.split() == []
    finally:
        process.kill()
        process.wait()


# --- Öğe araçları: hedef beyanı ve hassas hedef reddi ---

class FakeElementCUA:
    """cua_click_element / cua_set_text_element için AX konnektörünün sahtesi (yalnız Toolbox akışı sınanır)."""

    def __init__(self, pid: int, app: str) -> None:
        self.pid: int = pid
        self.app: str = app
        self.actions: List[str] = []
        self.failure: Optional[ToolError] = None

    def snapshot_owner(self, snapshot: str) -> Tuple[int, str]:
        return self.pid, self.app

    def prepare_target(self, snapshot: str, index: int) -> SimpleNamespace:
        return SimpleNamespace(captured=SimpleNamespace(snapshot={"app": self.app, "pid": self.pid}))

    def click_resolved_element(self, target: SimpleNamespace) -> str:
        if self.failure is not None:
            raise self.failure
        self.actions.append("tıklandı")
        return "tıklandı"

    def set_snapshot_text(self, snapshot: str, index: int, text: str) -> str:
        if self.failure is not None:
            raise self.failure
        self.actions.append("yazıldı")
        return "yazıldı"


def _element_toolbox(monkeypatch: pytest.MonkeyPatch, cua: FakeElementCUA, front: FrontApp) -> Toolbox:
    monkeypatch.setattr(tools, "screen_capture_granted", lambda request=False: False)
    monkeypatch.setattr(foreground, "describe_pid", lambda pid: front)
    monkeypatch.setattr(foreground, "host_identity", lambda: HOST)
    box = Toolbox()
    box.cua = cua  # type: ignore[assignment]
    return box


def test_element_tools_declare_the_snapshot_app_and_never_require_it_in_front(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ön plan GEREKMEZ (fg_click uygulamayı kendi öne alır); yalnız başarıda hedef beyan edilir."""
    monkeypatch.setattr(tools, "require_front_app", lambda expected, wait_seconds: pytest.fail("ön plan beklenmemeli"))
    cua = FakeElementCUA(10, "Notlar")
    box = _element_toolbox(monkeypatch, cua, NOTES)
    assert box.cua_click_element("s1", 1) == "tıklandı" and box._input_app == "Notlar"
    box._input_app = None
    assert box.cua_set_text_element("s1", 2, "merhaba") == "yazıldı" and box._input_app == "Notlar"
    box._input_app = "Google Chrome"
    cua.failure = ToolError("etkisiz", "ACTION_INEFFECTIVE", True)
    for call in (lambda: box.cua_click_element("s1", 1), lambda: box.cua_set_text_element("s1", 2, "x")):
        with pytest.raises(ToolError):
            call()
    assert box._input_app == "Google Chrome"  # başarısız eylem hedefi değiştirmez


@pytest.mark.parametrize("front", [TERMINAL, ONEPASSWORD, SETTINGS, OMNI_UI, LAUNCHER], ids=lambda app: app["name"])
def test_set_text_element_refuses_sensitive_targets_before_any_write(monkeypatch: pytest.MonkeyPatch, front: FrontApp) -> None:
    cua = FakeElementCUA(front["pid"], front["name"])
    box = _element_toolbox(monkeypatch, cua, front)
    with pytest.raises(ToolError) as refused:
        box.cua_set_text_element("s1", 1, "gizli metin")
    assert refused.value.code == "SENSITIVE_TARGET" and not refused.value.recoverable
    assert cua.actions == [] and box._input_app is None
    assert "gizli metin" not in str(refused.value)
    # Fare eylemi kapsam dışı: aynı hedefe tıklama engellenmez (yalnız klavye/yazma araçları korunur)
    assert box.cua_click_element("s1", 1) == "tıklandı"


# --- Gerçek macOS modülü ---

@pytest.mark.skipif(sys.platform != "darwin", reason="Gerçek AX yalnız macOS'ta")
def test_ax_symbols_used_by_the_foreground_reader_resolve_from_the_real_module() -> None:
    """Yeni AX çağrılarının modül çözümlemesi gerçek pyobjc'de sınanır (sahte modülde her ad 'vardır')."""
    for name in ("AXUIElementCreateSystemWide", "AXUIElementGetPid", "AXUIElementCopyAttributeValue", "AXIsProcessTrusted"):
        assert callable(getattr(AX, name)), name
    assert AX.kAXFocusedApplicationAttribute == "AXFocusedApplication"
    assert AX.kAXErrorSuccess == 0 and AX.kAXErrorNoValue == -25212 and AX.kAXErrorCannotComplete == -25204


@pytest.mark.skipif(sys.platform != "darwin", reason="Gerçek AX yalnız macOS'ta")
def test_real_front_app_is_readable_when_accessibility_is_granted() -> None:
    """Gerçek çağrı: pencere sunucusu bağlantısı kurulduktan sonra klavye odağındaki uygulama okunur (izin yoksa atlanır)."""
    if not AX.AXIsProcessTrusted():
        pytest.skip("Erişilebilirlik izni yok")
    try:
        front = foreground._poll_front(lambda candidate: True, 3.0)
    except ToolError as error:
        if error.code != "FOREGROUND_UNKNOWN":
            raise
        pytest.skip(f"Ön plandaki uygulama AX'e yanıt vermiyor (ör. eşzamanlı UI testi): {error}")
    assert front["pid"] > 0 and front["name"] != "" and front["bundle_id"] != ""
