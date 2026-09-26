"""Test ortamı: macOS dışında (Linux CI) pyobjc çerçevelerini sahte modülle karşılar.

Testler gerçek macOS API'lerini zaten taklit eder; burada yalnızca modül düzeyindeki
`import Quartz` gibi satırların macOS dışında toplama aşamasını düşürmesi engellenir.
Sahte çerçeveler başsız bir CI Mac'i gibi davranır: ekran kaydı ve erişilebilirlik izni yoktur.
"""

import sys
from unittest.mock import MagicMock

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
