"""
Yanıt bekleyen istek görünmeyen pencerede kalmasın: gösterim politikası, bildirim ve Dock zıplaması.

Onay/soru isteği 15 dk içinde yanıtlanmazsa görev işlemi yapmadan sürer; pencere gizliyken (⌘⇧X) ya da
arka plandayken kullanıcı isteği göremezdi. Gizlilik için gizli pencere kendiliğinden açılmaz: yalnız
içerik göstermeyen bir bildirim ve Dock zıplaması verilir. macOS'ta gizli uygulamada YENİ bir pencere
açmak (lift olmadan bile) tüm uygulamayı görünür yaptığı için (deneyle doğrulandı) yanıt penceresi de
uygulama yeniden görünene dek açılmaz.
"""
from __future__ import annotations

import logging
import subprocess
import sys
from typing import Dict, List, Protocol

ALERT_NOTIFY: str = "notify"
ALERT_RAISE: str = "raise"
ALERT_NONE: str = "none"
# Bildirim metni sabittir: görev, onay ya da dosya içeriği bildirime sızmaz.
INPUT_NOTIFICATION_TEXT: str = "Yanıtınız bekleniyor. Pencereyi göstermek için ⌘⇧X."


class ProcessLauncher(Protocol):
    """osascript'i başlatan çağrılabilir: üretimde subprocess.Popen; testte gerçek süreç yerine kaydedici verilebilir."""

    def __call__(self, args: List[str], *, stdout: int, stderr: int, env: Dict[str, str], close_fds: bool) -> object:
        ...


def input_alert_action(hidden: bool, backgrounded: bool) -> str:
    """
    Yanıt bekleyen istek için ne yapılacağı: gizliyse bildirim (pencere açılmaz), görünür ama arka
    plandaysa dikkat kartı gösterme, ön plandaysa normal yanıt gösterimi. Saf.
    """
    if hidden:
        return ALERT_NOTIFY
    return ALERT_RAISE if backgrounded else ALERT_NONE


def notify_input_required(launch: ProcessLauncher) -> bool:
    """
    Sabit metinli macOS bildirimini osascript ile başlatır. macOS dışında ya da süreç başlatılamazsa (neden
    yapısal uyarı olarak günlüğe yazılır) False döner: bildirim ek bir uyarıdır, istek yine de ertelenmiş kalır.
    """
    if sys.platform != "darwin":
        return False
    script: str = f'display notification "{INPUT_NOTIFICATION_TEXT}" with title "OmniAgent"'
    try:
        launch(["/usr/bin/osascript", "-e", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
               env={"PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8"}, close_fds=True)
    except OSError as error:
        logging.warning("Yanıt bekleyen istek bildirimi başlatılamadı",
                        extra={"error_type": type(error).__name__, "error": str(error), "executable": "/usr/bin/osascript"})
        return False
    return True


def request_user_attention() -> None:
    """Dock simgesini uygulama etkinleşene dek zıplatır (macOS dışında etkisizdir); hata çağırana iner."""
    if sys.platform != "darwin":
        return
    import AppKit  # ağır pyobjc içe aktarması: yalnız yanıt isteğinde gerekir, açılışı yavaşlatmasın (platform/macos kalıbı)

    AppKit.NSApplication.sharedApplication().requestUserAttention_(AppKit.NSCriticalRequest)
