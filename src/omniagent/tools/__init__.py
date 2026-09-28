"""OmniAgent araç paketinin geriye dönük uyumlu dış API katmanı."""
from __future__ import annotations

import sys
import types as _py_types
from typing import List

from . import browser, filesystem, gui_input, screen, system, types as tool_types
from . import facade as facade


# Facade içindeki mevcut public ve testlerde kullanılan private sembolleri paket
# seviyesinde aynı adlarla tut. Böylece src-layout refactor'ı monkeypatch/import
# semantiğini değiştirmez.
_EXPORTED_NAMES: List[str] = [name for name in dir(facade) if not name.startswith("__")]
for _name in _EXPORTED_NAMES:
    globals()[_name] = getattr(facade, _name)

# Paketin açık yüzeyi: facade'dan kopyalanan tüm adlar + alt modül adları. Star-import yalnız
# bunları getirir; testlerin patch'lediği iç semboller modülde erişilebilir kalır.
__all__: List[str] = sorted({
    *_EXPORTED_NAMES,
    "browser", "filesystem", "gui_input", "screen", "system", "facade", "tool_types",
})


class _ToolsModule(_py_types.ModuleType):
    """Paket seviyesindeki monkeypatch'leri gerçek uygulama modüllerine yansıtır."""

    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)
        if hasattr(facade, name):
            setattr(facade, name, value)
        for submod in (browser, filesystem, gui_input, screen, system, tool_types):
            if hasattr(submod, name):
                setattr(submod, name, value)


sys.modules[__name__].__class__ = _ToolsModule
