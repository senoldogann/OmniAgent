"""
Ekran kaydı izni tanısının kesinliği: uygulamanın sistem listesine kaydı, izin istemi ve
hata metninin kullanıcıya neye izin vereceğini açıkça söylemesi.
"""
import sys
from typing import List

import pytest

from omniagent import tools
from omniagent.tools import ToolError


class FakeQuartz:
    """
    Quartz ekran kaydı API'sini taklit eder. `grant_after_request=True`, kullanıcının sistem
    istemini onaylamasını temsil eder: izin istemi gösterilmeden önce izin yoktur.
    """

    def __init__(self, granted: bool = False, grant_after_request: bool = False) -> None:
        self.granted: bool = granted
        self.grant_after_request: bool = grant_after_request
        self.requests: List[bool] = []

    def CGPreflightScreenCaptureAccess(self) -> bool:
        return self.granted

    def CGRequestScreenCaptureAccess(self) -> None:
        self.requests.append(True)
        if self.grant_after_request:
            self.granted = True


@pytest.fixture
def terminal_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    """İzni süreci başlatan uygulamaya kesen macOS davranışını Terminal için kurar."""
    monkeypatch.setenv("__CFBundleIdentifier", "com.apple.Terminal")


def test_permission_request_registers_app_in_system_list_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    İzin yokken uygulama sistem listesine yalnızca CGRequestScreenCaptureAccess ile girer;
    bu çağrı süreç başına bir kez yapılır ve yoklama döngüleri istemi tekrar tetiklemez.
    """
    quartz: FakeQuartz = FakeQuartz()
    monkeypatch.setattr(tools, "Quartz", quartz)
    monkeypatch.setattr(tools, "_SCREEN_CAPTURE_REQUESTED", [False])
    assert tools.screen_capture_granted(request=True) is False
    assert tools.screen_capture_granted() is False
    assert tools.screen_capture_granted(request=True) is False
    assert len(quartz.requests) == 1


def test_permission_granted_after_prompt_is_seen(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kullanıcı istemi onaylarsa sonuç aynı çağrıda görülür; yeniden başlatma beklenmez."""
    quartz: FakeQuartz = FakeQuartz(grant_after_request=True)
    monkeypatch.setattr(tools, "Quartz", quartz)
    monkeypatch.setattr(tools, "_SCREEN_CAPTURE_REQUESTED", [False])
    assert tools.screen_capture_granted(request=True) is True
    assert len(quartz.requests) == 1


def test_granted_permission_never_prompts(monkeypatch: pytest.MonkeyPatch) -> None:
    """İzin zaten varsa hiçbir istem gösterilmez."""
    quartz: FakeQuartz = FakeQuartz(granted=True)
    monkeypatch.setattr(tools, "Quartz", quartz)
    monkeypatch.setattr(tools, "_SCREEN_CAPTURE_REQUESTED", [False])
    assert tools.screen_capture_granted(request=True) is True
    assert quartz.requests == []


def test_help_names_the_app_that_owns_the_permission(terminal_owner: None) -> None:
    """Yönerge hangi uygulamaya izin verileceğini adı ve bundle kimliğiyle söyler."""
    owner: dict[str, str] = tools.screen_capture_owner()
    assert owner["app_name"] == "Terminal"
    assert owner["bundle_id"] == "com.apple.Terminal"
    text: str = tools.screen_capture_help()
    assert "Terminal" in text
    assert "(com.apple.Terminal)" in text
    assert tools.SCREEN_SETTINGS_URL in text
    assert "Ekran ve Sistem Sesi Kaydı" in text
    assert sys.executable in text


def test_help_points_to_python_when_no_app_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Uygulama kimliği olmayan arka plan sürecinde (launchd, çıplak python) liste boş kalır;
    kullanıcının eklemesi gereken tam python yolu yönergeye yazılır.
    """
    monkeypatch.delenv("__CFBundleIdentifier", raising=False)
    assert tools.screen_capture_owner()["app_name"] == ""
    text: str = tools.screen_capture_help()
    assert "python ikilisini ekleyin" in text
    assert sys.executable in text


def test_tool_error_carries_actionable_help(monkeypatch: pytest.MonkeyPatch, terminal_owner: None) -> None:
    """Ekran kaydı gerektiren araç, izinsizken çıplak 'izin yok' yerine tam yönerge verir."""
    monkeypatch.setattr(tools, "screen_capture_granted", lambda request=False: False)
    with pytest.raises(ToolError) as error:
        tools._require_screen_capture()
    message: str = str(error.value)
    assert error.value.code == "SCREEN_CAPTURE_PERMISSION"
    assert "Terminal" in message
    assert sys.executable in message
    assert "Ekran ve Sistem Sesi Kaydı" in message


def test_report_shows_accessibility_status_and_help(monkeypatch: pytest.MonkeyPatch, terminal_owner: None) -> None:
    """Erişilebilirlik izni yoksa fare/klavye olayları sessizce düşer; tanı bunu da göstermeli."""
    from types import SimpleNamespace

    from omniagent.platform.macos import permissions
    from omniagent.tools import gui_input

    monkeypatch.setattr(tools, "screen_capture_granted", lambda request=False: True)
    monkeypatch.setattr(gui_input, "AX", SimpleNamespace(AXIsProcessTrusted=lambda: False))
    text = permissions.report()
    assert "Erişilebilirlik: İZİN YOK" in text
    assert "Privacy_Accessibility" in text
    monkeypatch.setattr(gui_input, "AX", SimpleNamespace(AXIsProcessTrusted=lambda: True))
    assert "Erişilebilirlik: izinli" in permissions.report()
