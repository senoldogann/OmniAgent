"""macOS native pencere efektleri (vibrancy, uygulama menüsü). Diğer platformlarda sessizce devre dışı kalır."""
from __future__ import annotations

import sys


_VIBRANCY_IDENTIFIER: str = "OmniAgentVibrancyEffectView"


def apply_vibrancy(window_title: str, material: str) -> bool:
    """
    Başlığı `window_title` olan görünür pencerenin arkasına native NSVisualEffectView yerleştirir.
    Saf değil: Cocoa'ya bağlanır. macOS dışında veya eşleşen pencere yoksa False döner.
    Idempotent: pencerede zaten bir efekt view'ı varsa ikincisini eklemez (tekrarlanan <Map>
    olaylarında — küçültme/⌘X gizle-göster — view'ların sonsuz birikmesini önler).
    """
    if sys.platform != "darwin":
        return False
    import AppKit

    materials: dict[str, int] = {
        "sidebar": AppKit.NSVisualEffectMaterialSidebar,
        "hud": AppKit.NSVisualEffectMaterialHUDWindow,
    }
    for window in AppKit.NSApplication.sharedApplication().windows():
        if str(window.title()) != window_title:
            continue
        content_view = window.contentView()
        if any(str(existing.identifier()) == _VIBRANCY_IDENTIFIER
               for existing in content_view.subviews()):
            return True
        effect_view = AppKit.NSVisualEffectView.alloc().initWithFrame_(content_view.bounds())
        effect_view.setIdentifier_(_VIBRANCY_IDENTIFIER)
        effect_view.setMaterial_(materials[material])
        effect_view.setBlendingMode_(AppKit.NSVisualEffectBlendingModeBehindWindow)
        effect_view.setState_(AppKit.NSVisualEffectStateActive)
        effect_view.setAutoresizingMask_(
            AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable
        )
        content_view.addSubview_positioned_relativeTo_(effect_view, AppKit.NSWindowBelow, None)
        return True
    return False


def install_native_menu_bar(app_name: str) -> bool:
    """Standart Uygulama menüsünü (Hakkında/Çık) kurar. Saf değil: Cocoa'ya bağlanır."""
    if sys.platform != "darwin":
        return False
    import AppKit

    app = AppKit.NSApplication.sharedApplication()
    main_menu = AppKit.NSMenu.alloc().init()
    app_menu_item = AppKit.NSMenuItem.alloc().init()
    main_menu.addItem_(app_menu_item)

    app_menu = AppKit.NSMenu.alloc().init()
    app_menu.addItem_(AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        f"{app_name} Hakkında", "orderFrontStandardAboutPanel:", "",
    ))
    app_menu.addItem_(AppKit.NSMenuItem.separatorItem())
    app_menu.addItem_(AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        f"{app_name}'dan Çık", "terminate:", "q",
    ))
    app_menu_item.setSubmenu_(app_menu)
    app.setMainMenu_(main_menu)
    return True
