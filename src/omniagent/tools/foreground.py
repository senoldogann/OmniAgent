"""
OmniAgent Ön Plan Denetimi (tools/foreground.py)
Klavye olayları CGEventPost ile SİSTEMİN klavye odağındaki uygulamaya gider; hedef uygulamayı seçmek
(cua_get_app, chrome_active_tab) bunu değiştirmez. Bu modül o uygulamayı okur ve girdiden ÖNCE doğrular.

Kaynak AX sistem geneli AXFocusedApplication'dır (tek platform noktası: read_front_app). Pencere sunucusunun
z-sırası yalnız en üstteki pencerenin sahibini söyler (etkinleşmeyen bir pencere, menü çubuğu uygulaması ve sistem
izin penceresi klavye odağıyla aynı şey değildir); NSWorkspace ana run loop dönmeyen süreçlerde (CLI, launchd
köprüsü) bayat kalır ve araçlar iş parçacıklarında koşar (bkz. gui_input._app_pid). CANLI ÖLÇÜM: sistem geneli sorgu
süreçte pencere sunucusu bağlantısı yokken (CLI'de ilk pencere listesi çağrısından önce) kAXErrorCannotComplete
(-25204) döner; bağlantı kurulunca 400/400 okuma başarılıdır (boşta ~4 ms, art arda 0,13 ms). Bu yüzden okumadan
önce bağlantı kurulur. Okunamayan durumlar (girdi güvenli tarafta reddedilir, bkz. unknown_front_hint): ön plandaki uygulama
yanıtsızsa ya da okuyan sürecin kendisiyse (AX kendi sürecini okuyamaz) -25204, görünür penceresi yoksa -25212.
Karşılaştırma ve karar saf fonksiyonlardır.

Hedef seçilmişse ön plan o uygulama olmalıdır (require_front_app). Seçilmemişse (tüm ekran modu) yalnız hassas
uygulama reddedilir (require_no_sensitive_front): OmniAgent'ın kendisi ya da onu çalıştıran uygulama, terminal/IDE,
sistem ayarları/güvenlik penceresi, parola yöneticisi. Bu modda hassas olmayan bilinmeyen bir uygulamaya yazımı
engellemek mümkün değildir: doğru hedefe dair bir beyan yoktur.
"""
import functools
import logging
import os
import time
import unicodedata
from typing import Callable, FrozenSet, Optional, Tuple, TypedDict

import ApplicationServices as AX
import Quartz

from .ax_snapshot import AX_ERROR_API_DISABLED, AX_ERROR_NO_VALUE, AX_ERROR_SUCCESS, RunningApplication, running_application
from .gui_input import _bundle_name, _visible_app_owners, key_names
from .screen import BUNDLE_APP_NAMES, _raise_if_stopped, accessibility_help
from .types import FOREGROUND_POLL_SECONDS, ToolError


class FrontApp(TypedDict):
    """Sistem klavye odağındaki (ya da açıklanan) uygulama."""
    pid: int
    name: str          # yerelleştirilmiş ad ('Notlar'); NSRunningApplication yoksa pencere sahibi adı
    bundle_name: str   # paket dosya adı ('Notes')
    bundle_id: str     # 'com.apple.Notes'; NSRunningApplication yoksa boş


class HostIdentity(TypedDict):
    """Ajanı çalıştıran uygulama: kendi süreç PID'i ve onu başlatan uygulamanın paket kimliği."""
    pid: int
    bundle_id: str


class AppReadiness(TypedDict):
    """Etkinleştirilen uygulamanın ön plan durumu ve pencere sunucusunda normal (katman 0) penceresinin olup olmadığı."""
    front: FrontApp
    has_window: bool


# Kategori adları (hata mesajında görünür)
_OWN_UI_CATEGORY: str = "OmniAgent arayüzü"
_HOST_CATEGORY: str = "OmniAgent'ı çalıştıran uygulama"
_TERMINAL_CATEGORY: str = "terminal/IDE"
_SYSTEM_CATEGORY: str = "sistem ayarları/güvenlik penceresi"
_PASSWORD_CATEGORY: str = "parola yöneticisi"

# OmniAgent'ın kendi masaüstü arayüzü (screen.BUNDLE_APP_NAMES içinde terminal/IDE ile aynı sözlüktedir)
_OMNIAGENT_BUNDLE_IDS: FrozenSet[str] = frozenset({"com.omniagent.desktop"})
# Hedef seçilmeden klavye girdisi almaması gereken ön plan uygulamaları (paket kimliği). Bu makinede Info.plist'ten
# doğrulananlar: com.apple.Terminal, com.microsoft.VSCode, com.apple.dt.Xcode, com.freebuff.desktop,
# com.omniagent.desktop, com.apple.systempreferences, com.apple.UserNotificationCenter, com.apple.loginwindow,
# com.apple.Passwords, com.apple.keychainaccess, com.apple.SecurityAgent. Parola yöneticisi önekleri (1Password,
# Bitwarden, Dashlane, LastPass, KeePassXC, Enpass) bu makinede kurulu değil: DOĞRULANMADI (eksik kimlik yalnız korumayı
# azaltır, yanlış engel üretmez).
_TERMINAL_OR_IDE_BUNDLE_IDS: FrozenSet[str] = frozenset(BUNDLE_APP_NAMES) - _OMNIAGENT_BUNDLE_IDS
_SYSTEM_BUNDLE_IDS: FrozenSet[str] = frozenset({
    "com.apple.systempreferences", "com.apple.SecurityAgent",
    "com.apple.loginwindow", "com.apple.UserNotificationCenter",
})
_PASSWORD_MANAGER_PREFIXES: Tuple[str, ...] = (
    "com.apple.keychainaccess", "com.apple.Passwords", "com.1password.", "com.agilebits.",
    "com.bitwarden.", "com.dashlane.", "com.lastpass.", "org.keepassxc.", "in.sinew.Enpass",
)
# Odağı BİLEREK başka uygulamaya devreden kısayollar: sonraki yazım ön plandaki yeni uygulamaya (ör. Spotlight) gider.
_FOCUS_HANDOFF_KEYS: FrozenSet[FrozenSet[str]] = frozenset({
    frozenset({"command", "tab"}), frozenset({"command", "shift", "tab"}), frozenset({"command", "space"}),
})
_FOREGROUND_HINT: str = (
    "Hedef uygulamayı öne getir (Chrome için chrome_active_tab, diğerleri için cua_get_app); üstte izin/güvenlik "
    "penceresi ya da başka bir uygulama varsa take_screenshot ile gör. Klavye olayı gönderilmedi."
)


def unknown_front_hint(error_code: int) -> str:
    """
    Ön plan okunamadığında (girdi güvenli tarafta reddedilir) olası neden ve çıkış yolu. CANLI ölçüm: sistem geneli
    sorgu anında kAXErrorCannotComplete (-25204) verir (a) ön plandaki uygulama yanıtsız/açılıyorsa ve (b) ön plandaki
    uygulama OKUYAN SÜRECİN KENDİSİYSE (AX kendi sürecini okuyamaz: OmniAgent arayüzü önde iken araçlar aynı süreçte
    koştuğundan); kAXErrorNoValue (-25212) verir ön plandaki uygulamanın görünür penceresi yoksa (küçültülmüş/kapalı). Saf.
    """
    if error_code == AX_ERROR_NO_VALUE:
        cause: str = (
            "ön plandaki uygulamanın görünür penceresi yok (kapalı ya da küçültülmüş olabilir): menü çubuğundan "
            "(cua_click_text) yeni pencere aç ya da Dock'tan geri getir"
        )
    else:
        cause = (
            "ön plandaki uygulama yanıt vermiyor ya da açılıyor olabilir; OmniAgent'ın kendi penceresi önde olabilir "
            "(AX kendi sürecini okuyamaz): hedef uygulamayı öne getir (Chrome için chrome_active_tab, diğerleri için cua_get_app)"
        )
    return f"Klavye olayı gönderilmedi. Olası neden: {cause}; take_screenshot ile bak ve birkaç saniye sonra yeniden dene."


def focus_handoff_key(spec: str) -> bool:
    """Tuş tanımı odağı bilerek başka uygulamaya (uygulama değiştirici, Spotlight) devrediyor mu. Saf."""
    return frozenset(key_names(spec)) in _FOCUS_HANDOFF_KEYS


def _normalized_name(name: str) -> str:
    """Uygulama adını karşılaştırma biçimine getirir: NFC, büyük/küçük harfsiz, '.app' sonekisiz. Saf."""
    return unicodedata.normalize("NFC", name).strip().casefold().removesuffix(".app")


def app_matches(expected: str, front: FrontApp) -> bool:
    """Beklenen ad ön plan uygulamasının yerel adı ya da paket adıyla eşleşiyor mu (gui_input._app_pid ile aynı eşdeğerlik). Saf."""
    wanted: str = _normalized_name(expected)
    return wanted != "" and wanted in (_normalized_name(front["name"]), _normalized_name(front["bundle_name"]))


def sensitive_category(front: FrontApp, host: HostIdentity) -> Optional[str]:
    """Hedef seçilmeden yazılmaması gereken uygulamanın türü; değilse None. Saf."""
    bundle_id: str = front["bundle_id"]
    if front["pid"] == host["pid"] or bundle_id in _OMNIAGENT_BUNDLE_IDS:
        return _OWN_UI_CATEGORY
    if host["bundle_id"] != "" and bundle_id == host["bundle_id"]:
        return _HOST_CATEGORY
    if bundle_id in _TERMINAL_OR_IDE_BUNDLE_IDS:
        return _TERMINAL_CATEGORY
    if bundle_id in _SYSTEM_BUNDLE_IDS:
        return _SYSTEM_CATEGORY
    if bundle_id.startswith(_PASSWORD_MANAGER_PREFIXES):
        return _PASSWORD_CATEGORY
    return None


def _label(front: FrontApp) -> str:
    """Uygulamayı hata mesajı için tanımlar. Saf."""
    return f"{front['name']!r} (paket={front['bundle_id'] or '?'}, pid={front['pid']})"


def front_app_problem(expected: str, front: FrontApp) -> Optional[str]:
    """Ön plan beklenen uygulama değilse açıklama; uygunsa None. Saf."""
    if app_matches(expected, front):
        return None
    return f"Ön planda {_label(front)} var; beklenen uygulama {expected!r}. {_FOREGROUND_HINT}"


def sensitive_front_problem(front: FrontApp, host: HostIdentity) -> Optional[str]:
    """Hedef seçilmemişken ön plan hassas bir uygulamaysa açıklama; değilse None. Kendi arayüzümüze bilerek yazma önerilmez. Saf."""
    category: Optional[str] = sensitive_category(front, host)
    if category is None:
        return None
    opening: str = (
        f"Ön planda {_label(front)} ({category}) var ve klavye girdisi için hedef uygulama seçilmedi; yazı oraya giderdi."
    )
    if category == _OWN_UI_CATEGORY:
        return f"{opening} OmniAgent'ın kendi arayüzüne yazma. {_FOREGROUND_HINT}"
    return (
        f"{opening} Bilerek o uygulamaya yazacaksan önce cua_get_app({front['name']!r}) çağır. {_FOREGROUND_HINT}"
    )


def sensitive_target_problem(target: FrontApp, host: HostIdentity) -> Optional[str]:
    """Metin yazılacak hedef uygulama hassassa açıklama; değilse None (hedef ön planda olmasa da yazı oraya gider). Saf."""
    category: Optional[str] = sensitive_category(target, host)
    if category is None:
        return None
    return (
        f"Hedef uygulama {_label(target)} hassas bir uygulama ({category}); cua_set_text_element bu uygulamaya yazmaz. "
        "Metin girişi gerçekten gerekiyorsa kullanıcıya bildir."
    )


def host_identity() -> HostIdentity:
    """Ajanı çalıştıran uygulamanın kimliği (screen.screen_capture_owner ile aynı ortam değişkeni)."""
    return {"pid": os.getpid(), "bundle_id": os.environ.get("__CFBundleIdentifier", "").strip()}


def describe_pid(pid: int) -> FrontApp:
    """PID'yi ad, paket adı ve paket kimliğiyle tanımlar; NSRunningApplication kaydı yoksa ad pencere sunucusundan okunur."""
    running: Optional[RunningApplication] = running_application(pid)
    localized: str = str(running.localizedName() or "") if running is not None else ""
    bundle_id: str = str(running.bundleIdentifier() or "") if running is not None else ""
    name: str = localized or next((owner for owner, owner_pid in _visible_app_owners() if owner_pid == pid), "")
    return {"pid": pid, "name": name, "bundle_name": _bundle_name(pid), "bundle_id": bundle_id}


@functools.cache
def _connect_window_server() -> None:
    """
    Sistem geneli AX sorgusu için süreçte pencere sunucusu bağlantısı kurar (ilk pencere listesi çağrısı kurar; süreç
    boyu kalıcıdır, iş parçacığından bağımsızdır). Bağlantı yokken AXFocusedApplication kAXErrorCannotComplete döner.
    """
    Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID)


def read_front_app() -> FrontApp:
    """
    Sistem klavye odağındaki uygulamayı AX sistem geneli sorgusuyla okur. AX izni yoksa AX_PERMISSION (kalıcı);
    okunamazsa FOREGROUND_UNKNOWN (geçici: uygulama geçişi sırasında olabilir). NSWorkspace kullanılmaz.
    """
    if not AX.AXIsProcessTrusted():
        raise ToolError(accessibility_help(), "AX_PERMISSION", False)
    _connect_window_server()
    error, application = AX.AXUIElementCopyAttributeValue(AX.AXUIElementCreateSystemWide(), "AXFocusedApplication", None)
    if error == AX_ERROR_API_DISABLED:
        raise ToolError(accessibility_help(), "AX_PERMISSION", False)
    if error != AX_ERROR_SUCCESS or application is None:
        raise ToolError(
            f"Ön plandaki uygulama okunamadı (AXFocusedApplication hata kodu={error}). {unknown_front_hint(error)}",
            "FOREGROUND_UNKNOWN", True,
        )
    pid_error, pid = AX.AXUIElementGetPid(application, None)
    if pid_error != AX_ERROR_SUCCESS:
        raise ToolError(
            f"Ön plandaki uygulamanın süreç kimliği okunamadı (AXUIElementGetPid hata kodu={pid_error}). "
            f"{unknown_front_hint(pid_error)}",
            "FOREGROUND_UNKNOWN", True,
        )
    return describe_pid(int(pid))


def app_has_window(pid: int) -> bool:
    """Süreç pencere sunucusunda normal (katman 0) pencere sahibi mi (gui_input._app_pid ile aynı kaynak)."""
    return any(owner_pid == pid for _name, owner_pid in _visible_app_owners())


def _poll_front(accept: Callable[[FrontApp], bool], wait_seconds: float) -> FrontApp:
    """
    accept sağlanana ya da süre dolana kadar ön plan uygulamasını yoklar. Süre dolarsa son gözlemi döner (kabul
    edilmemiş olabilir: karar çağırana aittir). Geçici okuma hatası (FOREGROUND_UNKNOWN) süre dolana kadar yeniden
    denenir, dolunca son hata yükseltilir; AX izni gibi kalıcı hatalar hemen yükselir.
    """
    deadline: float = time.monotonic() + wait_seconds
    warned: bool = False
    while True:
        try:
            front: FrontApp = read_front_app()
        except ToolError as error:
            if error.code != "FOREGROUND_UNKNOWN" or time.monotonic() >= deadline:
                raise
            if not warned:
                logging.warning(
                    "Ön plan uygulaması okunamadı; yeniden denenecek",
                    extra={"error_code": error.code, "detail": str(error)},
                )
                warned = True
        else:
            if accept(front) or time.monotonic() >= deadline:
                return front
        _raise_if_stopped()
        time.sleep(FOREGROUND_POLL_SECONDS)


def require_front_app(expected: str, wait_seconds: float) -> FrontApp:
    """Ön planın `expected` uygulaması olmasını en çok wait_seconds bekler; olmazsa FOREGROUND_MISMATCH."""
    front: FrontApp = _poll_front(lambda candidate: app_matches(expected, candidate), wait_seconds)
    problem: Optional[str] = front_app_problem(expected, front)
    if problem is not None:
        raise ToolError(problem, "FOREGROUND_MISMATCH", True)
    return front


def require_no_sensitive_front(wait_seconds: float) -> FrontApp:
    """Hedef seçilmemişken ön plan hassas bir uygulama olmasın; olursa en çok wait_seconds beklenir, sonra FOREGROUND_MISMATCH."""
    host: HostIdentity = host_identity()
    front: FrontApp = _poll_front(lambda candidate: sensitive_front_problem(candidate, host) is None, wait_seconds)
    problem: Optional[str] = sensitive_front_problem(front, host)
    if problem is not None:
        raise ToolError(problem, "FOREGROUND_MISMATCH", True)
    return front


def require_target_not_sensitive(pid: int) -> FrontApp:
    """Metin yazılacak hedef süreç hassas bir uygulama olmasın (hedefin ön planda olması gerekmez); olursa SENSITIVE_TARGET."""
    target: FrontApp = describe_pid(pid)
    problem: Optional[str] = sensitive_target_problem(target, host_identity())
    if problem is not None:
        raise ToolError(problem, "SENSITIVE_TARGET", False)
    return target


def wait_app_ready(app_name: str, wait_seconds: float) -> AppReadiness:
    """
    Etkinleştirilen uygulamanın ön plana gelmesini ve penceresinin açılmasını en çok wait_seconds bekler.
    Ön plana gelmezse FOREGROUND_MISMATCH; ön plandaysa ama pencere açılmadıysa hata değil, has_window=False döner.
    CANLI ölçüm: ön plandaki uygulamanın görünür penceresi yoksa AXFocusedApplication okunamaz (NoValue): süre
    dolarsa son okuma hatası (FOREGROUND_UNKNOWN, pencere ipucuyla) yükselir; has_window=False yalnız pencere sunucusu
    ile AX'in anlık çeliştiği nadir durumdadır.
    """
    front: FrontApp = _poll_front(
        lambda candidate: app_matches(app_name, candidate) and app_has_window(candidate["pid"]), wait_seconds,
    )
    problem: Optional[str] = front_app_problem(app_name, front)
    if problem is not None:
        raise ToolError(problem, "FOREGROUND_MISMATCH", True)
    return {"front": front, "has_window": app_has_window(front["pid"])}
