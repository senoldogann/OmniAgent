"""Arka plandaki onaylar için odağı çalmayan, sağ üstte kalıcı macOS kartı.

Tk ana thread'inden oluşturulur. Kart yeni bildirim izni istemez, tam işlem metni
kaydırılarak okunabilir; yanıt/iptal/zaman aşımında kapatılır.
"""
from __future__ import annotations

import sys
from typing import Callable, Mapping, Optional, Tuple


def confirmation_field(fields: Mapping[str, object]) -> Optional[str]:
    inputs = [(name, spec) for name, spec in fields.items() if not name.startswith("_")]
    if len(inputs) == 1 and isinstance(inputs[0][1], dict) and inputs[0][1].get("type") == "boolean":
        return inputs[0][0]
    return None


def popup_frame(
    visible: Tuple[float, float, float, float], width: float, height: float,
) -> Tuple[float, float, float, float]:
    x, y, screen_width, screen_height = visible
    width, height = min(width, screen_width - 40), min(height, screen_height - 40)
    return x + screen_width - width - 20, y + screen_height - height - 20, width, height


class PopupDecision:
    def __init__(self, callback: Callable[[bool], None]) -> None:
        self.callback = callback
        self.closed = False

    def choose(self, allowed: bool) -> None:
        if self.closed:
            return
        self.closed = True
        self.callback(allowed)

    def close(self) -> None:
        self.closed = True


_TARGET_CLASS = None


class ConfirmationPopup:
    """CTkToplevel ile aynı destroy sözleşmesi; native panel yaşamını korur."""

    def __init__(self, title: str, detail: str, callback: Callable[[bool], None]) -> None:
        import AppKit as ak
        from Foundation import NSObject, NSAttributedString

        global _TARGET_CLASS
        if _TARGET_CLASS is None:
            class OmniInputPopupTarget(NSObject):
                def choose_(self, sender):
                    self.decision.choose(sender.tag() == 1)
            _TARGET_CLASS = OmniInputPopupTarget

        self.decision = PopupDecision(callback)
        self.target = _TARGET_CLASS.alloc().init()
        self.target.decision = self.decision
        point = ak.NSEvent.mouseLocation()
        screens = list(ak.NSScreen.screens())
        screen = next((item for item in screens if ak.NSPointInRect(point, item.frame())), ak.NSScreen.mainScreen())
        visible = screen.visibleFrame()
        x, y, width, height = popup_frame(
            (visible.origin.x, visible.origin.y, visible.size.width, visible.size.height), 460, 360,
        )
        self.panel = ak.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            ak.NSMakeRect(x, y, width, height),
            ak.NSWindowStyleMaskBorderless | ak.NSWindowStyleMaskNonactivatingPanel,
            ak.NSBackingStoreBuffered, False,
        )
        self.panel.setTitle_("OmniAgent — Yanıt gerekiyor")
        self.panel.setReleasedWhenClosed_(False)
        self.panel.setLevel_(ak.NSStatusWindowLevel)
        self.panel.setFloatingPanel_(True)
        self.panel.setHidesOnDeactivate_(False)
        self.panel.setBecomesKeyOnlyIfNeeded_(True)
        self.panel.setCollectionBehavior_(
            ak.NSWindowCollectionBehaviorCanJoinAllSpaces | ak.NSWindowCollectionBehaviorFullScreenAuxiliary,
        )
        self.panel.setOpaque_(False)
        self.panel.setBackgroundColor_(ak.NSColor.clearColor())
        self.panel.setHasShadow_(True)
        view = self.panel.contentView()
        view.setWantsLayer_(True)
        view.layer().setBackgroundColor_(ak.NSColor.colorWithCalibratedRed_green_blue_alpha_(0.10, 0.12, 0.15, 1).CGColor())
        view.layer().setCornerRadius_(14)

        heading = ak.NSTextField.labelWithString_("OmniAgent · İzin gerekiyor")
        heading.setFont_(ak.NSFont.boldSystemFontOfSize_(16))
        heading.setTextColor_(ak.NSColor.whiteColor())
        heading.setFrame_(ak.NSMakeRect(20, height - 45, width - 40, 24))
        view.addSubview_(heading)

        scroll = ak.NSScrollView.alloc().initWithFrame_(ak.NSMakeRect(20, 78, width - 40, height - 135))
        scroll.setHasVerticalScroller_(True)
        scroll.setDrawsBackground_(False)
        text = ak.NSTextView.alloc().initWithFrame_(ak.NSMakeRect(0, 0, width - 58, height - 135))
        text.setEditable_(False)
        text.setSelectable_(True)
        text.setDrawsBackground_(False)
        text.setFont_(ak.NSFont.systemFontOfSize_(13))
        text.setTextColor_(ak.NSColor.whiteColor())
        text.setVerticallyResizable_(True)
        text.setHorizontallyResizable_(False)
        text.textContainer().setWidthTracksTextView_(True)
        text.setString_(title + ("\n\n" + detail if detail else ""))
        scroll.setDocumentView_(text)
        view.addSubview_(scroll)

        self.buttons = []
        for label, tag, left in (("Reddet", 0, width - 256), ("İzin ver", 1, width - 132)):
            button = ak.NSButton.alloc().initWithFrame_(ak.NSMakeRect(left, 22, 112, 34))
            button.setTitle_(label)
            button.setBordered_(False)
            button.setWantsLayer_(True)
            color = (0.18, 0.45, 0.87, 1) if tag else (0.24, 0.27, 0.31, 1)
            button.layer().setBackgroundColor_(ak.NSColor.colorWithCalibratedRed_green_blue_alpha_(*color).CGColor())
            button.layer().setCornerRadius_(8)
            button.setAttributedTitle_(NSAttributedString.alloc().initWithString_attributes_(label, {
                ak.NSForegroundColorAttributeName: ak.NSColor.whiteColor(),
                ak.NSFontAttributeName: ak.NSFont.boldSystemFontOfSize_(13),
            }))
            button.setTag_(tag)
            button.setTarget_(self.target)
            button.setAction_("choose:")
            view.addSubview_(button)
            self.buttons.append(button)
        self.panel.orderFrontRegardless()

    def destroy(self) -> None:
        self.decision.close()
        self.panel.orderOut_(None)
        self.panel.close()


def create_confirmation_popup(
    title: str, detail: str, callback: Callable[[bool], None],
) -> Optional[ConfirmationPopup]:
    if sys.platform != "darwin":
        return None
    return ConfirmationPopup(title, detail, callback)
