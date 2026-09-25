"""Test ortamı: macOS dışında (Linux CI) pyobjc çerçevelerini sahte modülle karşılar.

Testler gerçek macOS API'lerini zaten taklit eder; burada yalnızca modül düzeyindeki
`import Quartz` gibi satırların macOS dışında toplama aşamasını düşürmesi engellenir.
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
