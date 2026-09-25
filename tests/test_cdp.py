"""
CDP Hibrit Köprüsü Testleri (tests/test_cdp.py)
Chrome remote debugging bağlantısı, port yoklaması ve graceful fallback davranışı doğrulanır.
"""
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import threading
from typing import Dict, List, Optional
import pytest

from omniagent.tools.cdp import (
    DEFAULT_CDP_PORT,
    ChromeCDPSession,
    is_cdp_available,
    list_cdp_tabs,
)


class MockCDPHandler(BaseHTTPRequestHandler):
    """Test için sahte Chrome DevTools JSON endpoint sunucusu."""
    def do_GET(self) -> None:
        if self.path == "/json/version":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            payload = {
                "Browser": "Chrome/120.0.0.0",
                "Protocol-Version": "1.3",
                "webSocketDebuggerUrl": "ws://127.0.0.1:9999/devtools/browser/abc",
            }
            self.wfile.write(json.dumps(payload).encode("utf-8"))
        elif self.path == "/json/list":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            payload = [
                {
                    "id": "tab-1",
                    "title": "İş İlanları",
                    "url": "https://example.com/ilanlar",
                    "type": "page",
                },
                {
                    "id": "tab-2",
                    "title": "Uzantı",
                    "url": "chrome-extension://xyz",
                    "type": "background_page",
                },
            ]
            self.wfile.write(json.dumps(payload).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def mock_cdp_server():
    """Rastgele boş portta çalışan mock CDP sunucusu."""
    server = HTTPServer(("127.0.0.1", 0), MockCDPHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield port
    server.shutdown()


def test_is_cdp_available_when_closed() -> None:
    """Kapalı bir port sorgulandığında anında ve hatasız False döner."""
    assert is_cdp_available(port=65432, timeout=0.05) is False


def test_list_cdp_tabs_when_closed() -> None:
    """Kapalı bir port sorgulandığında boş liste döner."""
    assert list_cdp_tabs(port=65432, timeout=0.05) == []


def test_is_cdp_available_when_mock_server_running(mock_cdp_server: int) -> None:
    """Mock CDP sunucusu açıkken port tanınır."""
    assert is_cdp_available(port=mock_cdp_server, timeout=1.0) is True


def test_list_cdp_tabs_filters_only_pages(mock_cdp_server: int) -> None:
    """Yalnızca type='page' olan sekmeler ayıklanır."""
    tabs = list_cdp_tabs(port=mock_cdp_server, timeout=1.0)
    assert len(tabs) == 1
    assert tabs[0]["id"] == "tab-1"
    assert tabs[0]["title"] == "İş İlanları"
    assert tabs[0]["url"] == "https://example.com/ilanlar"


@pytest.mark.asyncio
async def test_cdp_session_fallback_when_closed() -> None:
    """Port kapalıyken oturum hiçbir hata üretmeden güvenle False/None döner."""
    session = ChromeCDPSession(port=65432)
    connected = await session.connect()
    assert connected is False
    assert await session.is_connected() is False
    assert await session.find_tab() is None
    assert await session.click_text("Gönder") is False
    assert await session.fill_field("#q", "Deneme") is False
    await session.disconnect()
