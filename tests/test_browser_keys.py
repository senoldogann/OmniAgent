"""Gerçek görevde görülen küçük harf Enter hatasının regresyon testi."""
import pytest

from tools import Toolbox, normalize_browser_key


def test_browser_key_aliases_keep_playwright_chords() -> None:
    assert normalize_browser_key("enter") == "Enter"
    assert normalize_browser_key("cmd+a") == "Meta+a"
    assert normalize_browser_key("Ctrl+Shift+Enter") == "Control+Shift+Enter"
    assert normalize_browser_key("esc") == "Escape"


@pytest.mark.asyncio
async def test_browse_url_normalizes_press_before_browser_call(monkeypatch: pytest.MonkeyPatch) -> None:
    pressed: list[tuple[str, str]] = []

    class FakePage:
        url = "https://example.invalid"

        async def press(self, selector: str, key: str) -> None:
            pressed.append((selector, key))

        async def wait_for_load_state(self, state: str) -> None:
            assert state == "domcontentloaded"

        async def content(self) -> str:
            return "<html><title>Deneme</title><body>Hazır</body></html>"

        async def evaluate(self, script: str, limit: int) -> list[str]:
            return []

        async def title(self) -> str:
            return "Deneme"

    async def fake_page(self: Toolbox) -> FakePage:
        return FakePage()

    monkeypatch.setattr(Toolbox, "_get_page", fake_page)
    result = await Toolbox().browse_url(None, [{"action": "press", "selector": "#q", "value": "enter"}])
    assert pressed == [("#q", "Enter")]
    assert "arka planda çalışan ayrı Chromium" in result
