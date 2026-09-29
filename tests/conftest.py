"""Test ortamı: macOS dışında (Linux CI) pyobjc çerçevelerini sahte modülle karşılar.

Testler gerçek macOS API'lerini zaten taklit eder; burada yalnızca modül düzeyindeki
`import Quartz` gibi satırların macOS dışında toplama aşamasını düşürmesi engellenir.
Sahte çerçeveler başsız bir CI Mac'i gibi davranır: ekran kaydı ve erişilebilirlik izni yoktur.
"""

import os
import shutil
import sys
import tempfile
from typing import TYPE_CHECKING, Dict, Iterator, Tuple
from unittest.mock import MagicMock

import pytest

if TYPE_CHECKING:
    from omniagent.dev.headless_screen import HeadlessPage

_MACOS_MODULES = (
    "AppKit",
    "ApplicationServices",
    "AVFoundation",
    "Cocoa",
    "CoreFoundation",
    "Foundation",
    "HIServices",
    "objc",
    "Quartz",
    "Speech",
    "Vision",
)

if sys.platform != "darwin":
    for _name in _MACOS_MODULES:
        sys.modules.setdefault(_name, MagicMock(name=_name))
    # MagicMock çağrısı doğru-değerli döner; izin sorguları "izin var" sanılmasın.
    sys.modules["Quartz"].CGPreflightScreenCaptureAccess.return_value = False
    sys.modules["Quartz"].CGRequestScreenCaptureAccess.return_value = False
    sys.modules["ApplicationServices"].AXIsProcessTrusted.return_value = False
    # Oturum açık ve konsolda, ekran uyanık: kilit denetimi sahte değerle "kilitli" sanılmasın.
    sys.modules["Quartz"].CGSessionCopyCurrentDictionary.return_value = {"kCGSSessionOnConsoleKey": True}
    sys.modules["Quartz"].CGDisplayIsAsleep.return_value = False

# Kalıcı kullanıcı verisi (kontrol noktaları, hafıza, denetim kaydı) testlerde geçici dizine yönlendirilir:
# ajan döngüsü testleri aksi halde gerçek ~/Library/Application Support/OmniAgent altına yüzlerce kontrol
# noktası yazar ve gerçek "devam et" isteklerinde yanlış eşleşmeye yol açabilir. Ortam değişkeni elle
# verilmişse dokunulmaz. Toplama sırasında içe aktarılan modüller de yalıtılmış kökü görsün diye kurulum
# modül düzeyindedir.
_OWNED_DATA_ROOT: str | None = None
if not os.environ.get("OMNI_DATA_DIR", "").strip():
    _OWNED_DATA_ROOT = tempfile.mkdtemp(prefix="omni-test-data-")
    os.environ["OMNI_DATA_DIR"] = _OWNED_DATA_ROOT


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Oturum için oluşturulan geçici veri kökünü siler; elle verilen dizine dokunmaz."""
    if _OWNED_DATA_ROOT is not None:
        shutil.rmtree(_OWNED_DATA_ROOT, ignore_errors=True)


@pytest.fixture(autouse=True)
def strict_fallback_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Yedek sağlayıcı izni testlerde kapalı sabitlenir: ortam tanımlı olduğu için kayıt dosyası hiç okunmaz,
    geliştiricinin gerçek veri dizini testlere karışmaz. Yedek davranışını sınayan testler kendi izinlerini verir.
    """
    monkeypatch.setenv("OMNI_FALLBACK_BACKENDS", "none")
    monkeypatch.delenv("OMNI_FALLBACK_IMAGES", raising=False)


# headless_screen.install() modül seviyesindeki adları kalıcı olarak değiştirir (bkz. dev/headless_screen.py install ve
# block_real_input); testten sonra geri almazsak sonraki testler (gerçek macOS araçlarını bekleyenler dahil) sahte Toolbox ve
# kapatılmış gerçek girdi katmanıyla çalışmaya devam eder.
_PATCHED_TOOLS_ATTRS: Tuple[str, ...] = (
    "screen_capture_granted", "_require_screen_capture", "_require_accessibility",
    "click_model_point", "multi_click_model_point", "drag_model_points",
    "move_model_point", "type_unicode_text", "press_key_spec", "post_scroll",
)


@pytest.fixture
def headless_page() -> Iterator["HeadlessPage"]:
    """
    Gerçek başsız Chromium sayfası ve headless_screen.install() yamaları (test_benchmark_gui_gate ve test_gui_approval_gate
    paylaşır); bitince yamalar geri alınır. autouse DEĞİLDİR: yalnız isteyen test Chromium'u başlatır. Ağır modüller
    (pyautogui, agent, headless_screen) burada, fikstür gövdesinde içe aktarılır: conftest'in oturum başındaki ortam
    yalıtımı bozulmasın ve bu modüller yalnız fikstürü kullanan testlerin maliyeti olsun.
    """
    import pyautogui
    import Quartz

    from omniagent import tools
    from omniagent.app import agent as main
    from omniagent.dev import headless_screen

    saved_tools: Dict[str, object] = {name: getattr(tools, name) for name in _PATCHED_TOOLS_ATTRS}
    saved_pyautogui: Dict[str, object] = {
        name: getattr(pyautogui, name) for name in headless_screen.REAL_INPUT_FUNCTIONS
    }
    saved_event_post = Quartz.CGEventPost
    saved_toolbox = main.Toolbox
    saved_route_schemas = main.route_tool_schemas
    try:
        page = headless_screen.HeadlessPage()
    except Exception as error:  # noqa: BLE001 - Playwright'ın kendi hata tipi burada önemli değil
        # CI/geliştirme makinesinde `playwright install` hiç çalıştırılmamış olabilir
        # (repo bunu hiçbir yerde çağırmıyor); bu test o zaman diğer ortam-bağımlı testler
        # gibi (macOS Keychain, gerçek Tk...) açık gerekçeyle atlanır, sessizce geçmez.
        pytest.skip(f"Headless Chromium başlatılamadı (playwright install gerekebilir): {error}")
    headless_screen.install(page)
    try:
        yield page
    finally:
        page.close()
        for name, value in saved_tools.items():
            setattr(tools, name, value)
        for name, value in saved_pyautogui.items():
            setattr(pyautogui, name, value)
        Quartz.CGEventPost = saved_event_post
        main.Toolbox = saved_toolbox
        # hide_ax_tools() route_tool_schemas'ı süreç boyunca sarar; geri alınmazsa sonraki testler AX araçsız kalır
        main.route_tool_schemas = saved_route_schemas


@pytest.fixture(autouse=True)
def isolated_bridge_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Telegram köprüsü model istemcilerini her görev başında yeniden kurar (Ollama hazırlığı yoklanır); testler
    geliştirici makinesindeki Ollama'ya bağlı kalmasın. Gerçek fabrikayı sınayan testler kendisi geri koyar.
    """
    telegram = sys.modules.get("omniagent.integrations.telegram")
    if telegram is not None:
        monkeypatch.setattr(telegram, "create_model_clients", lambda: {})


@pytest.fixture(autouse=True)
def no_chrome_profile_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Testler geliştiricinin GERÇEK Chrome profilini kopyalamaz: otomatik tohumlama kapatılır, böylece çerez ve
    giriş verisi test kopyasına taşınmaz. Tohumlamayı sınayan test kendi geçici kaynak dizinini verir.
    """
    monkeypatch.setenv("OMNI_SEED_CHROME_PROFILE", "0")


@pytest.fixture
def strict_wall_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Varsayılan mod artık BYPASS'tır: bot doğrulaması/engel sayfası hata değil, içerik olarak okunur ve modele
    uygulanacak strateji ve seçiciler bildirilir (bkz. tools/bot_wall.bypass_note). Bu fikstür eski SIKI
    sözleşmeyi sınayan testler için bayrağı kapatır: duvar kurtarılamaz BOT_WALL_DETECTED ToolError'ı olur ve
    engelli ana makine görev boyunca yeniden denenmez. Bypass davranışını sınayan testler bu fikstürü ALMAZ.
    """
    from omniagent.tools import bot_wall

    monkeypatch.setattr(bot_wall, "BYPASS_ENABLED", False)


@pytest.fixture
def manual_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Host onay kapısı testlerde elle sorar: sürekli modda otomatik onay (AUTO_APPROVE_IN_CONTINUOUS_MODE)
    kapatılır, böylece eski sözleşme (onay yoksa çağrı çalışmaz) sınanabilir. Otomatik onay yolunu sınayan
    testler bu fikstürü almaz; güvenli-host/tutar yolu için bkz. approval.should_auto_approve.
    """
    from omniagent import approval

    monkeypatch.setattr(approval, "AUTO_APPROVE_IN_CONTINUOUS_MODE", False)

