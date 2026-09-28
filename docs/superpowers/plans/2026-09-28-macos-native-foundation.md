# macOS Native Redesign — Sub-project 0 (Foundation) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a real native macOS visual layer (window vibrancy + a standard application menu bar) to the OmniAgent desktop UI, as best-effort decoration that never blocks or crashes the app.

**Architecture:** A new, isolated module `src/omniagent/ui/native_macos.py` holds two plain functions (`apply_vibrancy`, `install_native_menu_bar`), following the exact `import AppKit` (plain PyObjC) pattern the codebase already uses in `ui/app.py`'s `_style_native_titlebar` — not a new dependency, not a class, not `rubicon-objc`. `ui/app.py` calls both functions at the same points it already schedules `_style_native_titlebar`, best-effort (return value logged, never raised).

**Tech Stack:** Python, PyObjC (`AppKit`, already an installed/working dependency — see `ui/app.py:409`), pytest.

**Spec:** `docs/superpowers/specs/2026-09-28-macos-native-redesign-design.md`

## Global Constraints

- Best-effort only: any expected non-darwin/window-not-found condition returns `False`/no-op; it never raises and never blocks app startup.
- No behavior change to the agent core (`app/agent.py`, `core/*`, `events.py`) — this plan touches only `ui/`.
- Plain functions, not a class — matches the existing `_style_native_titlebar` pattern (no new "connector" abstraction for two functions).
- No default parameter values on new function signatures (per project style) — every parameter is explicit at every call site.
- All new comments/docstrings in Turkish (per project style, matching the rest of `ui/app.py`).
- Non-darwin/CI environments must stay fully green — `import AppKit` only happens inside the `if sys.platform == "darwin"` branch, never at module import time.
- `--bundle-check` fast-path (`main():2180-2186`) must stay untouched — no menu-bar/vibrancy call added there.

## Review Focus

- Running the full suite on non-macOS (or a sandboxed macOS CI without a GUI session): must not import-error or hang — `native_macos.py` must not import `AppKit` at module level.
- Vibrancy attempted before the window is mapped/visible (no matching `NSWindow` title yet): must return `False`, not raise, and must not corrupt `_style_native_titlebar`'s own window loop.
- `--bundle-check` invocation of `main()` must remain side-effect-free (no menu bar installed, no vibrancy attempted) — a reasonable person running the packaging health check expects it to stay instant.
- Existing `_style_native_titlebar` behavior (titlebar color matching `BG`) must be unaffected — vibrancy is additive, not a replacement.
- Full existing test suite (`OMNI_UI_TEST=1 pytest -q`) must stay green with zero unrelated changes.

---

### Task 1: `native_macos.apply_vibrancy` + wiring

**Files:**
- Create: `src/omniagent/ui/native_macos.py`
- Test: `tests/test_native_macos.py`
- Modify: `src/omniagent/ui/app.py:285-286` (the `<Map>` bind lambda) and `src/omniagent/ui/app.py:400` (the `after(120, ...)` fallback)

**Interfaces:**
- Produces: `apply_vibrancy(window_title: str, material: str) -> bool` — `material` is one of `"sidebar"` or `"hud"`; on macOS with a matching visible `NSWindow`, installs a real `NSVisualEffectView` behind its content view and returns `True`; on any other platform, or if no window with that exact title is currently open, returns `False` without raising.
- Consumes: nothing from other tasks.

- [ ] **Step 1: Write the failing tests**

```python
"""native_macos.py testleri: yalnız macOS'a özgü davranışı ve zararsız düşüş yollarını doğrular."""
from __future__ import annotations

import sys

import pytest

from omniagent.ui import native_macos


def test_apply_vibrancy_returns_false_off_darwin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(native_macos.sys, "platform", "linux")
    assert native_macos.apply_vibrancy("hiçbir pencere", "sidebar") is False


def test_apply_vibrancy_returns_false_when_window_title_not_found() -> None:
    if sys.platform != "darwin":
        pytest.skip("Yalnız macOS'ta anlamlı")
    missing_title = "kesinlikle var olmayan bir pencere başlığı — test-native-macos-12345"
    assert native_macos.apply_vibrancy(missing_title, "sidebar") is False
```

Add this file at `tests/test_native_macos.py`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_native_macos.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'omniagent.ui.native_macos'` (the module doesn't exist yet).

- [ ] **Step 3: Write minimal implementation**

Create `src/omniagent/ui/native_macos.py`:

```python
"""macOS native pencere efektleri (vibrancy, uygulama menüsü). Diğer platformlarda sessizce devre dışı kalır."""
from __future__ import annotations

import sys


def apply_vibrancy(window_title: str, material: str) -> bool:
    """
    Başlığı `window_title` olan görünür pencerenin arkasına native NSVisualEffectView yerleştirir.
    Saf değil: Cocoa'ya bağlanır. macOS dışında veya eşleşen pencere yoksa False döner.
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
        effect_view = AppKit.NSVisualEffectView.alloc().initWithFrame_(content_view.bounds())
        effect_view.setMaterial_(materials[material])
        effect_view.setBlendingMode_(AppKit.NSVisualEffectBlendingModeBehindWindow)
        effect_view.setState_(AppKit.NSVisualEffectStateActive)
        effect_view.setAutoresizingMask_(
            AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable
        )
        content_view.addSubview_positioned_relativeTo_(effect_view, AppKit.NSWindowBelow, None)
        return True
    return False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_native_macos.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Wire into `ui/app.py`**

Modify `src/omniagent/ui/app.py`:

At line 285-286, change:
```python
        self.bind("<Map>", lambda event: self.after_idle(self._style_native_titlebar)
                  if event.widget is self else None, add="+")
```
to:
```python
        self.bind("<Map>", lambda event: self.after_idle(self._style_native_window)
                  if event.widget is self else None, add="+")
```

At line 400, change:
```python
        self.after(120, self._style_native_titlebar)
```
to:
```python
        self.after(120, self._style_native_window)
```

Immediately after the existing `_style_native_titlebar` method (after its closing line, currently line 416), add a new method:

```python
    def _style_native_window(self, title: Optional[str] = None) -> None:
        """Başlık çubuğu rengini ve vibrancy'yi birlikte uygular; ikisi de en-iyi-çaba."""
        self._style_native_titlebar(title)
        applied = native_macos.apply_vibrancy(title or self.title(), "sidebar")
        if not applied and sys.platform == "darwin":
            logging.info("Vibrancy uygulanamadı (pencere henüz görünür olmayabilir).")
```

Add the import near the top of `ui/app.py`, alongside the other `omniagent` imports (near line 25):
```python
from omniagent.ui import native_macos
```

- [ ] **Step 6: Run the full test suite**

Run: `OMNI_UI_TEST=1 .venv/bin/python -m pytest -q`
Expected: all green, same pass/skip count as before this task plus the 2 new tests (no regressions — `_style_native_titlebar` itself is untouched, only its call sites are renamed to go through the new wrapper). Before this step, run `grep -n "_style_native_titlebar" src/omniagent/ui/app.py` to find every caller (e.g. the Ayarlar/Settings window may call it directly with its own title) and route each one through `_style_native_window` the same way, so every native window gets vibrancy too.

- [ ] **Step 7: Manually verify in the real app**

Launch the app while the screen is idle. Confirm: the main window's titlebar still matches the content background (unchanged), and the window now shows a visibly blurred/vibrant background rather than a flat opaque one. If vibrancy is not visible, check the logged "Vibrancy uygulanamadı" message and re-check the window-title match logic before proceeding — do not mark this task complete on a red/failed visual check.

- [ ] **Step 8: Commit**

```bash
git add src/omniagent/ui/native_macos.py tests/test_native_macos.py src/omniagent/ui/app.py
git commit -m "feat(ui): add native window vibrancy (macOS)"
```

### Task 2: `native_macos.install_native_menu_bar` + wiring

**Files:**
- Modify: `src/omniagent/ui/native_macos.py` (add a second function to the file Task 1 created)
- Modify: `tests/test_native_macos.py` (add tests to the file Task 1 created)
- Modify: `src/omniagent/ui/app.py:main()` (lines 2178-2199)

**Interfaces:**
- Consumes: nothing from Task 1 (independent function in the same module).
- Produces: `install_native_menu_bar(app_name: str) -> bool` — on macOS, installs a standard two-item application menu (About, Quit) as the app's main menu and returns `True`; on any other platform returns `False` without raising. (Preferences/⌘, is deliberately NOT included in this task — wiring a custom menu action to the existing `OmniUI._open_settings` method needs a PyObjC-callable target object, which is a separate, not-yet-verified piece of work; tracked as a follow-up, not silently skipped.)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_native_macos.py`:

```python
def test_install_native_menu_bar_returns_false_off_darwin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(native_macos.sys, "platform", "linux")
    assert native_macos.install_native_menu_bar("OmniAgent Test") is False


def test_install_native_menu_bar_succeeds_on_darwin() -> None:
    if sys.platform != "darwin":
        pytest.skip("Yalnız macOS'ta anlamlı")
    assert native_macos.install_native_menu_bar("OmniAgent Test") is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_native_macos.py -v`
Expected: FAIL — `AttributeError: module 'omniagent.ui.native_macos' has no attribute 'install_native_menu_bar'`

- [ ] **Step 3: Write minimal implementation**

Append to `src/omniagent/ui/native_macos.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_native_macos.py -v`
Expected: PASS (4 passed total in this file)

- [ ] **Step 5: Wire into `main()`**

Modify `src/omniagent/ui/app.py`, in `main()` (current lines 2178-2199): after the `--bundle-check` early-return block and after `app = OmniUI()` (current line 2191), add:

```python
    app = OmniUI()
    if not native_macos.install_native_menu_bar("OmniAgent"):
        logging.info("Native menü çubuğu kurulamadı (macOS dışında beklenen).")
```

(The `--bundle-check` branch returns before this point, so it is unaffected — confirm this by re-reading the diff before committing.)

- [ ] **Step 6: Run the full test suite**

Run: `OMNI_UI_TEST=1 .venv/bin/python -m pytest -q`
Expected: all green, same count as Task 1's end plus the 2 new tests.

- [ ] **Step 7: Manually verify in the real app**

Launch the app while the screen is idle. Confirm: the macOS menu bar now shows "OmniAgent" as the active application menu with "OmniAgent Hakkında" and "OmniAgent'dan Çık" (previously it likely showed a generic/blank Python menu). Click "Hakkında" and confirm the standard About panel appears without error. Confirm ⌘Q still quits the app.

- [ ] **Step 8: Commit**

```bash
git add src/omniagent/ui/native_macos.py tests/test_native_macos.py src/omniagent/ui/app.py
git commit -m "feat(ui): add a native macOS application menu"
```
