from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any, Callable, Dict, FrozenSet, Iterator, List, NamedTuple, Optional, Tuple

import pytest
from playwright.async_api import Error as PlaywrightError

from omniagent.app import agent as main
from omniagent.app.continuous import CONTINUE_PROMPT, REPLAN_GUIDANCE, WALL_CONTINUE_PROMPT, WALL_REPLAN_GUIDANCE
from omniagent.core.events import AgentEvent
from omniagent.integrations.capabilities import CapabilityService
from omniagent import tools
from omniagent.tools import Toolbox, filesystem, web
from omniagent.tools.bot_wall import (
    ACCESS_CHALLENGE_CODE, ACCESS_CHALLENGE_MARKER, AccessChallenge, BYPASS_ENABLED, bypass_note,
    classify_access_challenge, is_bypass_enabled, is_local_app_response, wall_host_key, walled_host_error,
    widget_notice,
)
from omniagent.tools.browser import (
    AGENT_PRODUCT_TOKEN, HeadlessBrowserSession, STANDARD_CHROME_USER_AGENT, browse_page_actions,
    browser_user_agent, fetch_raw_content,
)
from omniagent.tools import browser as browser_module
from omniagent.tools.filesystem import clean_html
from omniagent.tools.types import BrowserAction, ToolError


class _FakeDDGS:
    calls: list[tuple[str, str, dict[str, Any]]] = []

    def __enter__(self) -> "_FakeDDGS":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def news(self, query: str, **kwargs: Any) -> list[dict[str, str]]:
        self.calls.append(("news", query, kwargs))
        now = datetime.now(timezone.utc)
        return [
            {
                "date": now.isoformat(),
                "title": "World update",
                "body": "Current world event",
                "url": "https://example.com/news",
                "source": "Example News",
            },
            {
                "date": (now - timedelta(days=8)).isoformat(),
                "title": "Old world update",
                "body": "Stale world event",
                "url": "https://example.com/old-news",
                "source": "Old News",
            },
        ]

    def text(self, query: str, **kwargs: Any) -> list[dict[str, str]]:
        self.calls.append(("text", query, kwargs))
        return [{
            "title": "Text result",
            "body": "Fallback body",
            "href": "https://example.com/text",
        }]


def test_web_search_uses_current_ddgs_news_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeDDGS.calls.clear()
    monkeypatch.setattr(tools, "DDGS", _FakeDDGS)

    payload = json.loads(Toolbox().web_search(
        "last 3 days world news summary September 22-25 2026",
        category="auto",
        freshness_days=3,
    ))

    assert _FakeDDGS.calls == [(
        "news",
        "world news",
        {"timelimit": "w", "max_results": 12, "backend": "bing,duckduckgo,yahoo"},
    )]
    assert [item["url"] for item in payload] == ["https://example.com/news"]
    assert payload[0]["source"] == "Example News"


def test_web_search_falls_back_to_text_when_news_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    class EmptyNews(_FakeDDGS):
        def news(self, query: str, **kwargs: Any) -> list[dict[str, str]]:
            self.calls.append(("news", query, kwargs))
            return []

    EmptyNews.calls = []
    monkeypatch.setattr(tools, "DDGS", EmptyNews)

    payload = json.loads(Toolbox().web_search("dünya haberleri son 3 gün"))

    assert [call[0] for call in EmptyNews.calls] == ["news", "text"]
    assert payload[0]["url"] == "https://example.com/text"


def test_web_search_retries_transient_failures_before_succeeding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[int] = []

    class FlakyThenOk:
        def __enter__(self) -> "FlakyThenOk":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def text(self, query: str, **kwargs: Any) -> list[dict[str, str]]:
            attempts.append(1)
            if len(attempts) < 3:
                raise RuntimeError("No results found.")
            return [{"title": "OK", "body": "body", "href": "https://example.com/ok"}]

    result = json.loads(web.search_web("test query", client_factory=FlakyThenOk))

    assert len(attempts) == 3
    assert result[0]["url"] == "https://example.com/ok"


def test_web_search_gives_up_after_max_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[int] = []

    class AlwaysFails:
        def __enter__(self) -> "AlwaysFails":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def text(self, query: str, **kwargs: Any) -> list[dict[str, str]]:
            attempts.append(1)
            raise RuntimeError("No results found.")

    with pytest.raises(ToolError) as exc_info:
        web.search_web("test query", client_factory=AlwaysFails)

    assert len(attempts) == 3
    assert exc_info.value.code == "WEB_SEARCH_FAILED"


def test_fetch_raw_decodes_non_utf8_body_without_crashing(monkeypatch: pytest.MonkeyPatch) -> None:
    body = "<html><body>Schröder — München</body></html>".encode("cp1252")
    completed = subprocess.CompletedProcess(
        args=["curl"], returncode=0, stdout=body, stderr=b"",
    )
    monkeypatch.setattr("omniagent.tools.browser.subprocess.run", lambda *args, **kwargs: completed)

    result = fetch_raw_content("https://example.com")

    assert "Schröder" in result
    assert "München" in result


@pytest.mark.asyncio
async def test_tool_failures_do_not_switch_model_backend(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any,
) -> None:
    turns = [
        {
            "content": "STATE: search one",
            "tool_calls": [{"id": "1", "name": "web_search", "arguments": json.dumps({"query": "q1"})}],
            "finish_reason": "tool_calls",
            "usage": main.ZERO_USAGE,
        },
        {
            "content": "STATE: search two",
            "tool_calls": [{"id": "2", "name": "web_search", "arguments": json.dumps({"query": "q2"})}],
            "finish_reason": "tool_calls",
            "usage": main.ZERO_USAGE,
        },
        {
            "content": "Araştırma kaynağı erişilemedi.",
            "tool_calls": [],
            "finish_reason": "stop",
            "usage": main.ZERO_USAGE,
        },
    ]
    requested_backends: list[str] = []

    async def fake_model(
        clients: Any, messages: Any, schemas: Any, session_id: str, backend: str,
        emit: Any, should_stop: Any,
    ) -> tuple[dict[str, Any], str]:
        requested_backends.append(backend)
        return turns.pop(0), backend

    def fail_search(self: Toolbox, query: str, category: str = "auto", freshness_days: int | None = None) -> str:
        raise ToolError("search unavailable", "WEB_SEARCH_FAILED", True)

    monkeypatch.setattr(main, "_call_model_with_retries", fake_model)
    monkeypatch.setattr(Toolbox, "web_search", fail_search)

    events: list[Any] = []
    report = await main.run_agent_with_callback(
        "Web'de son 3 günü araştır ve rapor ver",
        events.append,
        {
            "requested_backend": "ollama-cloud",
            "should_stop": lambda: False,
            "state_file": str(tmp_path / "state.json"),
            "history": [],
        },
        {"ollama-cloud": object(), "openai": object()},
    )

    assert requested_backends == ["ollama-cloud", "ollama-cloud", "ollama-cloud"]
    assert not [event for event in events if event["kind"] == "backend_changed"]
    assert report["metrics"]["backend"] == "ollama-cloud"


# --- Bot doğrulaması ve erişim engeli (tools/bot_wall.py) ---
# Sayfa örnekleri bilinen engel sayfası kalıplarından derlenmiş SENTETİK örneklerdir; gerçek
# sitelerle doğrulanmadı. Sınıflandırıcı yalnız içeriğe bakar, ağa çıkmaz.

CLOUDFLARE_INTERSTITIAL: str = (
    '<!DOCTYPE html><html lang="en-US"><head><title>Just a moment...</title>'
    '<noscript><meta http-equiv="refresh" content="35"></noscript></head><body class="no-js">'
    '<div class="main-content"><noscript><span id="challenge-error-text">'
    "Enable JavaScript and cookies to continue</span></noscript></div>"
    "<script>window._cf_chl_opt = {cZone: 'example.com'};</script></body></html>"
)
CLOUDFLARE_BLOCK: str = (
    "<!DOCTYPE html><html><head><title>Attention Required! | Cloudflare</title></head><body>"
    "<h1>Sorry, you have been blocked</h1><h2>You are unable to access example.com</h2>"
    "<p>This website is using a security service to protect itself from online attacks.</p>"
    "</body></html>"
)
AKAMAI_DENIED: str = (
    "<HTML><HEAD><TITLE>Access Denied</TITLE></HEAD><BODY><H1>Access Denied</H1>"
    "You don't have permission to access this page on this server.</BODY></HTML>"
)
GOOGLE_UNUSUAL_TRAFFIC: str = (
    "<html><head><title>https://www.google.com/search?q=x</title></head><body>"
    '<form id="captcha-form"><div class="g-recaptcha" data-sitekey="k"></div></form>'
    "<div><b>About this page</b> Our systems have detected unusual traffic from your computer "
    "network. This page checks to see if it's really you sending the requests, and not a robot."
    "</div></body></html>"
)
AMAZON_ROBOT_CHECK: str = (
    "<!DOCTYPE html><html><head><title>Robot Check</title></head><body><h4>Enter the characters "
    "you see below</h4><p>Sorry, we just need to make sure you're not a robot.</p></body></html>"
)
TURKISH_HUMAN_CHECK: str = (
    "<html><head><title>example.com</title></head><body>"
    "<h1>İnsan olduğunuzu doğrulayın</h1></body></html>"
)
DATADOME_CAPTCHA: str = (
    "<html><head><title>example.com</title></head><body>"
    '<iframe src="https://geo.captcha-delivery.com/captcha/?initialCid=x"></iframe></body></html>'
)
SHORT_VERIFYING_PAGE: str = (
    "<html><head><title>example.com</title></head><body>"
    "<p>Verifying you are human. This may take a few seconds.</p></body></html>"
)
# Başlığı ve metni sıradan, yalnız sağlayıcı izi taşıyan engel sayfası.
CLOUDFLARE_MARKER_ONLY: str = (
    "<html><head><title>example.com</title></head><body><p>Lütfen bekleyin.</p>"
    "<script>window._cf_chl_opt = {cZone: 'example.com'};</script></body></html>"
)
PERIMETERX_BLOCK: str = (
    "<html><head><title>Access to this page has been denied</title></head><body>"
    '<div id="px-captcha"></div><p>Press &amp; Hold to confirm you are a human (and not a bot).</p>'
    "</body></html>"
)
INCAPSULA_INTERRUPTION: str = (
    "<html><head><title>Pardon Our Interruption</title></head><body>"
    "<p>As you were browsing something about your browser made us think you were a bot.</p></body></html>"
)
DUCKDUCKGO_ANOMALY: str = (
    "<html><head><title>DuckDuckGo</title></head><body><div>Unfortunately, bots use DuckDuckGo too. "
    "Please complete the following challenge to confirm this search was made by a human.</div>"
    "</body></html>"
)
RECAPTCHA_CONTACT_FORM: str = (
    '<html><head><title>İletişim</title><script src="https://www.google.com/recaptcha/api.js">'
    '</script></head><body><form><label>Mesaj</label><textarea name="mesaj"></textarea>'
    '<div class="g-recaptcha" data-sitekey="k"></div><button>Gönder</button></form></body></html>'
)
HCAPTCHA_SIGNUP_FORM: str = (
    '<html><head><title>Sign up</title></head><body><form><input name="email">'
    '<div class="h-captcha" data-sitekey="k"></div><button>Sign up</button></form></body></html>'
)
# Turnstile kutusunun çalışma zamanında eklenen cf-chl-widget-… kimliği engel işareti sayılmaz.
TURNSTILE_CONTACT_FORM: str = (
    '<html><head><title>Contact</title></head><body><form><input name="email">'
    '<div class="cf-turnstile" data-sitekey="k"><div id="cf-chl-widget-abc12_response"></div></div>'
    "<button>Send</button></form></body></html>"
)

# Yanlış pozitif adayları: hiçbiri engel sayfası değildir.
# Aynı cümle uzun bir makalede geçiyor: boyut koruması onu engel saymamalı.
LONG_ARTICLE: str = (
    "<html><head><title>Bot koruması nasıl çalışır</title></head><body>"
    + "<p>Verifying you are human. Bu makale bot doğrulamasını anlatır.</p>" * 60
    + "</body></html>"
)
CAPTCHA_BLOG_POST: str = (
    "<html><head><title>CAPTCHA nedir?</title></head><body><article><h1>CAPTCHA nedir?</h1>"
    "<p>Bir captcha, kullanıcının insan olup olmadığını anlamak için kullanılan küçük bir sınavdır. "
    "Captcha türleri arasında görsel bulmaca ve onay kutusu bulunur.</p></article></body></html>"
)
LOGIN_FORM: str = (
    '<html><head><title>Giriş</title></head><body><form><label>E-posta</label><input type="email">'
    '<label>Parola</label><input type="password"><button>Giriş yap</button></form></body></html>'
)
COOKIE_BANNER_PAGE: str = (
    '<html><head><title>Haberler</title></head><body><div id="cerez">Deneyiminizi iyileştirmek için '
    "çerezler kullanıyoruz. <button>Kabul et</button><button>Reddet</button></div>"
    "<h1>Gündem</h1><p>Bugünün haberleri.</p></body></html>"
)
# Yerel benchmark formundaki gibi: sitenin kendi formundaki 'insan' onay kutusu bot kontrolü değildir.
HUMAN_CHECKBOX_FORM: str = (
    '<html lang="fi"><head><title>Ota yhteyttä</title></head><body><form>'
    '<label for="nimi">Nimi</label><input id="nimi">'
    '<div id="ihminen" role="checkbox" aria-checked="false"><span>Vahvista, että olet ihminen</span></div>'
    "<button>Lähetä viesti</button></form></body></html>"
)
BARE_VERIFY_LABEL_FORM: str = (
    '<html><head><title>Contact</title></head><body><form><input name="email">'
    '<label><input type="checkbox"> Verify you are human</label><button>Send</button></form>'
    "</body></html>"
)
CLOUDFLARE_SCRIPT_PAGE: str = (
    "<html><head><title>Merhaba</title></head><body><h1>Merhaba dünya</h1>"
    '<script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js"></script></body></html>'
)
JSON_ERROR_BODY: str = '{"error": "captcha required", "message": "Access denied"}'
# API yanıtı bir bot duvarı değildir: engel cümlesi JSON içinde geçse de yapısal yanıt sınıflandırılmaz.
JSON_WITH_WALL_PHRASE: str = '{"error": "captcha required", "message": "Verifying you are human"}'
PLAIN_FORBIDDEN_PAGE: str = (
    "<html><head><title>403 Forbidden</title></head><body><h1>Forbidden</h1></body></html>"
)
# Anında yönlenen sayfa: Playwright içeriği bu sırada "sayfa yönleniyor" hatasıyla okutmaz.
META_REFRESH_PAGE: str = (
    '<html><head><meta http-equiv="refresh" content="0;url=/form"></head>'
    "<body>Yönlendiriliyor</body></html>"
)

# Bağımsız güvenlik incelemesinde kaçan sağlayıcı kalıpları (yine sentetik örnekler).
REDDIT_BLOCK: str = (
    "<html><head><title>Blocked</title></head><body><h1>You've been blocked by network security.</h1>"
    "<p>To continue, log in to your Reddit account or use your developer token</p></body></html>"
)
WORDFENCE_LIMITED: str = (
    "<html><head><title>Your access to this site has been limited</title></head><body>"
    "<p>Your access to this site has been limited by the site owner</p></body></html>"
)
SUCURI_DENIED: str = (
    "<html><head><title>Sucuri WebSite Firewall - Access Denied</title></head><body>"
    "<h1>Access Denied - Sucuri Website Firewall</h1></body></html>"
)
CLOUDFLARE_RATE_LIMITED: str = (
    "<html><head><title>Error 1015 | Rate limited</title></head><body><h1>You are being rate limited</h1>"
    "<p>The owner of this website has banned you temporarily from accessing this website.</p></body></html>"
)
TOO_MANY_REQUESTS_PAGE: str = (
    "<html><head><title>429 Too Many Requests</title></head><body><h1>Too Many Requests</h1></body></html>"
)
YANDEX_SMARTCAPTCHA_WALL: str = (
    '<html><head><title>Are you not a robot?</title></head><body><p>Are you not a robot?</p>'
    '<div class="smart-captcha"></div></body></html>'
)
KASADA_CHALLENGE: str = "<html><head><title></title></head><body><script>KPSDK</script></body></html>"
CLOUDFLARE_BODY_ONLY: str = (
    "<html><head><title>example.com</title></head><body><div>Performing security verification</div>"
    "<p>This website uses a security service to protect against malicious bots. This page is displayed "
    "while the website verifies you are not a bot.</p></body></html>"
)
HCAPTCHA_FULL_PAGE: str = (
    "<html><head><title>Please complete the security check to access example.com</title></head><body>"
    '<div class="h-captcha" data-sitekey="x"></div>'
    "<p>Please complete the security check to access the site.</p></body></html>"
)
SMARTCAPTCHA_LOGIN_FORM: str = (
    '<html><head><title>Giriş</title></head><body><form><input name="u"><div class="smart-captcha"></div>'
    "<button>Giriş</button></form></body></html>"
)
# Yanlış pozitif adayları (bağımsız inceleme): kısa yazılar ve başlıklar engel sayfası değildir.
JUST_A_MOMENT_ESSAY: str = (
    "<html><head><title>Just a moment: notes on patience</title></head><body>"
    "<p>A short essay about waiting.</p></body></html>"
)
UNUSUAL_TRAFFIC_NEWS: str = (
    "<html><head><title>Report</title></head><body><p>The ISP reported unusual traffic from your computer "
    "network patterns last week.</p></body></html>"
)
UNUSUAL_TRAFFIC_QUOTE: str = (
    "<html><head><title>Notlar</title></head><body><p>Google bana şunu gösterdi: 'Our systems have detected "
    "unusual traffic from your computer network.' Başka bir şey yoktu.</p></body></html>"
)
NUMBERED_LIST_TITLE: str = (
    "<html><head><title>429 places to eat in Paris</title></head><body><p>A very short list.</p></body></html>"
)
# Yerel uygulamanın kendi yetki hatası: başlığı "Access denied" olan sıradan bir sayfa.
LOCAL_ACCESS_DENIED_PAGE: str = (
    "<html><head><title>Access denied</title></head><body><h1>Access denied</h1>"
    "<p>You lack the 'admin' role.</p></body></html>"
)
# HTTP 429 gövdesi hiçbir sağlayıcı kalıbı taşımaz: durum kodu tek işarettir.
PLAIN_SLOW_DOWN_PAGE: str = (
    "<html><head><title>Slow down</title></head><body><p>Please slow down.</p></body></html>"
)


@pytest.mark.parametrize(
    ("page", "expected"),
    [
        pytest.param(CLOUDFLARE_INTERSTITIAL, {"kind": "wall", "signal": "cloudflare-interstitial"}, id="cloudflare-ara-sayfa"),
        pytest.param(CLOUDFLARE_BLOCK, {"kind": "wall", "signal": "cloudflare-block"}, id="cloudflare-engel"),
        pytest.param(AKAMAI_DENIED, {"kind": "wall", "signal": "access-denied"}, id="akamai-access-denied"),
        pytest.param(GOOGLE_UNUSUAL_TRAFFIC, {"kind": "wall", "signal": "unusual-traffic"}, id="google-olagan-disi-trafik"),
        pytest.param(AMAZON_ROBOT_CHECK, {"kind": "wall", "signal": "robot-check"}, id="amazon-robot-check"),
        pytest.param(TURKISH_HUMAN_CHECK, {"kind": "wall", "signal": "tr-human-verification"}, id="turkce-insan-dogrulama"),
        pytest.param(DATADOME_CAPTCHA, {"kind": "wall", "signal": "datadome"}, id="datadome-iframe"),
        pytest.param(SHORT_VERIFYING_PAGE, {"kind": "wall", "signal": "human-verification"}, id="kisa-verifying-sayfasi"),
        pytest.param(CLOUDFLARE_MARKER_ONLY, {"kind": "wall", "signal": "cloudflare-challenge"}, id="cloudflare-yalniz-saglayici-izi"),
        pytest.param(PERIMETERX_BLOCK, {"kind": "wall", "signal": "perimeterx"}, id="perimeterx-basili-tut"),
        pytest.param(INCAPSULA_INTERRUPTION, {"kind": "wall", "signal": "incapsula-interruption"}, id="incapsula-kesinti"),
        pytest.param(DUCKDUCKGO_ANOMALY, {"kind": "wall", "signal": "search-anomaly"}, id="duckduckgo-anomali"),
        pytest.param(REDDIT_BLOCK, {"kind": "wall", "signal": "network-block"}, id="reddit-ag-guvenligi"),
        pytest.param(WORDFENCE_LIMITED, {"kind": "wall", "signal": "wordfence"}, id="wordfence-sinirlandi"),
        pytest.param(SUCURI_DENIED, {"kind": "wall", "signal": "sucuri"}, id="sucuri-guvenlik-duvari"),
        pytest.param(CLOUDFLARE_RATE_LIMITED, {"kind": "wall", "signal": "rate-limited"}, id="cloudflare-1015-hiz-siniri"),
        pytest.param(TOO_MANY_REQUESTS_PAGE, {"kind": "wall", "signal": "http-429"}, id="duz-429-sayfasi"),
        pytest.param(YANDEX_SMARTCAPTCHA_WALL, {"kind": "wall", "signal": "robot-check"}, id="yandex-smartcaptcha-sayfasi"),
        pytest.param(KASADA_CHALLENGE, {"kind": "wall", "signal": "kasada"}, id="kasada-kpsdk"),
        pytest.param(CLOUDFLARE_BODY_ONLY, {"kind": "wall", "signal": "cloudflare-verification"}, id="cloudflare-guvenlik-dogrulamasi"),
        pytest.param(HCAPTCHA_FULL_PAGE, {"kind": "wall", "signal": "security-check"}, id="tam-sayfa-hcaptcha"),
        pytest.param(LOCAL_ACCESS_DENIED_PAGE, {"kind": "wall", "signal": "access-denied"}, id="access-denied-sayfa-metinsel"),
        pytest.param(RECAPTCHA_CONTACT_FORM, {"kind": "widget", "signal": "recaptcha"}, id="gomulu-recaptcha"),
        pytest.param(HCAPTCHA_SIGNUP_FORM, {"kind": "widget", "signal": "hcaptcha"}, id="gomulu-hcaptcha"),
        pytest.param(TURNSTILE_CONTACT_FORM, {"kind": "widget", "signal": "turnstile"}, id="gomulu-turnstile"),
        pytest.param(SMARTCAPTCHA_LOGIN_FORM, {"kind": "widget", "signal": "smartcaptcha"}, id="gomulu-smartcaptcha"),
        pytest.param(JUST_A_MOMENT_ESSAY, None, id="just-a-moment-baslikli-yazi"),
        pytest.param(UNUSUAL_TRAFFIC_NEWS, None, id="haber-unusual-traffic-cumlesi"),
        pytest.param(UNUSUAL_TRAFFIC_QUOTE, None, id="google-cumlesinin-alintisi"),
        pytest.param(NUMBERED_LIST_TITLE, None, id="429-ile-baslayan-liste-basligi"),
        pytest.param(LONG_ARTICLE, None, id="uzun-makale-ayni-cumle"),
        pytest.param(CAPTCHA_BLOG_POST, None, id="captcha-blog-yazisi"),
        pytest.param(LOGIN_FORM, None, id="giris-formu"),
        pytest.param(COOKIE_BANNER_PAGE, None, id="cerez-bandi"),
        pytest.param(HUMAN_CHECKBOX_FORM, None, id="yerel-insan-onay-kutusu"),
        pytest.param(BARE_VERIFY_LABEL_FORM, None, id="ciplak-verify-etiketi"),
        pytest.param(CLOUDFLARE_SCRIPT_PAGE, None, id="cloudflare-betik-yolu"),
        pytest.param(JSON_ERROR_BODY, None, id="json-govde"),
        pytest.param("", None, id="bos-sayfa"),
    ],
)
def test_access_challenge_classifier(page: str, expected: Optional[AccessChallenge]) -> None:
    """Engel sayfaları 'wall', gömülü CAPTCHA 'widget' olur; sıradan sayfalar None kalır."""
    assert classify_access_challenge(page, clean_html(page)) == expected


class _Route(NamedTuple):
    """Yerel sınama sitesinin bir yol için verdiği yanıt."""

    status: int
    content_type: str
    body: str


class _LocalSite(NamedTuple):
    """Çalışan yerel sınama sitesi: taban adres ve gelen isteklerin yolları."""

    base_url: str
    requested_paths: List[str]


_HTML: str = "text/html; charset=utf-8"
_SITE_ROUTES: Dict[str, _Route] = {
    # Ara sayfa HTTP 403 ile gelir (curl hata yolu). Bağlantı tıklanırsa /tiklandi istenir: tıklama
    # gezinmeyi bitirene dek beklendiği için sunucu kaydı yarışsız kanıttır.
    "/dogrulama": _Route(403, _HTML, CLOUDFLARE_INTERSTITIAL.replace(
        "</body>", '<a id="dogrula" href="/tiklandi">Doğrula</a></body>',
    )),
    # Aynı engel sayfası iki adreste: deneyim belleğinin hata imzası (host + son yol parçası) aynı çıkar.
    "/bir/dogrulama": _Route(403, _HTML, CLOUDFLARE_INTERSTITIAL),
    "/iki/dogrulama": _Route(403, _HTML, CLOUDFLARE_INTERSTITIAL),
    # Yumuşak engel: HTTP 200 ile gelen 'Robot Check' (curl başarı yolu).
    "/yumusak-engel": _Route(200, _HTML, AMAZON_ROBOT_CHECK),
    "/haber": _Route(200, _HTML, LONG_ARTICLE),
    "/api": _Route(403, "application/json", JSON_WITH_WALL_PHRASE),
    "/api-ok": _Route(200, "application/json", JSON_WITH_WALL_PHRASE),
    "/yasak": _Route(403, _HTML, PLAIN_FORBIDDEN_PAGE),
    "/form": _Route(200, _HTML, HUMAN_CHECKBOX_FORM),
    "/bilesen": _Route(200, _HTML, RECAPTCHA_CONTACT_FORM),
    "/yonlendir": _Route(200, _HTML, META_REFRESH_PAGE),
    # Yerel uygulamanın 'Access denied' sayfası (loopback adreste engel sayılmaz) ve 403'lü hâli.
    "/yerel-yasak": _Route(200, _HTML, LOCAL_ACCESS_DENIED_PAGE),
    "/yerel-yasak-403": _Route(403, _HTML, LOCAL_ACCESS_DENIED_PAGE),
    # HTTP 429: HTML gövde durum koduyla engel sayılır, JSON gövde sayılmaz.
    "/hiz-siniri": _Route(429, _HTML, PLAIN_SLOW_DOWN_PAGE),
    "/hiz-siniri-json": _Route(429, "application/json", '{"error": "rate limit exceeded"}'),
    # Sunucu hatası: HTTP durumu sunucunun kararıdır, yeniden denenmez. /gecici-* yollarında sunucu ilk bağlantıları
    # yanıt yazmadan kapatır (curl çıkış 52); sonra bu içerik gelir.
    "/hata-503": _Route(503, "text/plain; charset=utf-8", "geçici sunucu hatası"),
    "/gecici-bir": _Route(200, _HTML, "<html><body><p>Geçici kopmadan sonra gelen içerik</p></body></html>"),
    "/gecici-hep": _Route(200, _HTML, "<html><body><p>Buna hiç ulaşılamaz</p></body></html>"),
}
# Yolun ilk N bağlantısı yanıt yazılmadan kapatılır (geçici AĞ hatası benzetimi; gerçek TCP)
_SITE_DROPPED_CONNECTIONS: Dict[str, int] = {"/gecici-bir": 1, "/gecici-hep": 1000}


@pytest.fixture
def site() -> Iterator[_LocalSite]:
    """Gerçek HTTP yanıtı veren yerel site; /ua isteğin User-Agent başlığını gövdede döndürür."""
    requested: List[str] = []
    drops: Dict[str, int] = dict(_SITE_DROPPED_CONNECTIONS)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requested.append(self.path)
            route_path: str = self.path.split("?", 1)[0]
            if drops.get(route_path, 0) > 0:
                drops[route_path] -= 1
                self.close_connection = True
                self.request.shutdown(socket.SHUT_RDWR)  # yanıt yazmadan kes: curl 'Empty reply from server' (çıkış 52)
                return
            if route_path == "/ua":
                route: _Route = _Route(200, "text/plain; charset=utf-8", str(self.headers["User-Agent"]))
            elif route_path in _SITE_ROUTES:
                route = _SITE_ROUTES[route_path]
            else:
                route = _Route(404, "text/plain; charset=utf-8", "yok")
            payload: bytes = route.body.encode("utf-8")
            self.send_response(route.status)
            self.send_header("Content-Type", route.content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            return

    server: ThreadingHTTPServer = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield _LocalSite(f"http://127.0.0.1:{server.server_port}", requested)
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize(
    ("path", "code", "recoverable", "message_part"),
    [
        pytest.param("/dogrulama", ACCESS_CHALLENGE_CODE, False, "aşmaya çalışma", id="403-ara-sayfa-hata-yolu"),
        pytest.param("/yumusak-engel", ACCESS_CHALLENGE_CODE, False, "aşmaya çalışma", id="200-robot-check-basari-yolu"),
        pytest.param("/api", "FETCH_FAILED", True, "captcha required", id="403-json-siniflandirilmaz"),
        pytest.param("/yasak", "FETCH_FAILED", True, "Forbidden", id="403-siradan-hata-sayfasi"),
        pytest.param("/hiz-siniri", ACCESS_CHALLENGE_CODE, False, "belirteç=http-429", id="429-html-govde-ayri-sinyal"),
        pytest.param("/hiz-siniri-json", "FETCH_FAILED", True, "rate limit exceeded", id="429-json-siniflandirilmaz"),
        pytest.param("/yerel-yasak-403", "FETCH_FAILED", True, "Access denied", id="yerel-403-access-denied-engel-degil"),
    ],
)
def test_fetch_raw_stops_at_access_wall_and_keeps_ordinary_errors(
    site: _LocalSite, path: str, code: str, recoverable: bool, message_part: str, strict_wall_mode: None,
) -> None:
    """Sıkı mod: engel sayfası kurtarılamaz BOT_WALL_DETECTED olur; JSON ve sıradan HTTP hatası FETCH_FAILED kalır."""
    with pytest.raises(ToolError) as failure:
        fetch_raw_content(site.base_url + path)

    assert failure.value.code == code
    assert failure.value.recoverable is recoverable
    assert message_part in str(failure.value)


@pytest.mark.parametrize(
    ("path", "expected_text"),
    [
        pytest.param("/haber", "Bu makale bot doğrulamasını anlatır.", id="uzun-makale"),
        pytest.param("/form", "Vahvista, että olet ihminen", id="yerel-insan-onay-kutusu"),
        pytest.param("/bilesen", "Gönder", id="gomulu-captcha-bileseni"),
        pytest.param("/api-ok", "Verifying you are human", id="200-json-siniflandirilmaz"),
    ],
)
def test_fetch_raw_returns_pages_that_only_mention_verification(
    site: _LocalSite, path: str, expected_text: str,
) -> None:
    """Doğrulamadan söz eden sayfa, gömülü bileşen ve JSON yanıt engel sayılmaz; içerik okunur."""
    assert expected_text in fetch_raw_content(site.base_url + path)


@pytest.fixture
def quick_fetch_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Yeniden deneme bekleme süresi (1 sn) testte kısaltılır; davranış aynıdır. raising=False: sabit olmayan (yeniden denemesiz)
    eski sürümde de testler DAVRANIŞ (tek deneme) yüzünden kırılsın, ayarlama hatasıyla değil.
    """
    monkeypatch.setattr(browser_module, "FETCH_RETRY_WAIT_SECONDS", 0.05, raising=False)


def test_fetch_raw_retries_transient_network_errors_and_succeeds(
    site: _LocalSite, quick_fetch_retries: None, caplog: pytest.LogCaptureFixture,
) -> None:
    """
    İlk bağlantı yanıtsız kapanırsa (curl çıkış 52) yeniden denenir ve içerik gelir; curl'ün kendi --retry'ı kaldırıldığı için
    geçici ağ hatası eskiden tek denemeyle biterdi. Her yeniden deneme yapısal uyarı loglar.
    """
    with caplog.at_level(logging.WARNING):
        text: str = fetch_raw_content(f"{site.base_url}/gecici-bir")

    assert "Geçici kopmadan sonra gelen içerik" in text
    assert site.requested_paths == ["/gecici-bir", "/gecici-bir"]
    retries = [record for record in caplog.records if record.getMessage().startswith("Geçici ağ hatası")]
    assert [(record.exit_code, record.attempt, record.max_retries) for record in retries] == [(52, 1, 2)]


def test_fetch_raw_gives_up_after_two_retries_and_raises_the_last_error(
    site: _LocalSite, quick_fetch_retries: None,
) -> None:
    with pytest.raises(ToolError) as failure:
        fetch_raw_content(f"{site.base_url}/gecici-hep")

    assert failure.value.code == "FETCH_FAILED" and failure.value.recoverable is True and "çıkış=52" in str(failure.value)
    assert site.requested_paths == ["/gecici-hep"] * 3  # ilk deneme + en çok iki yeniden deneme


def test_fetch_raw_retries_a_refused_connection_with_structured_warnings(
    quick_fetch_retries: None, caplog: pytest.LogCaptureFixture,
) -> None:
    """Bağlantı reddi (curl çıkış 7) de geçici ağ hatasıdır: gerçek kapalı porta iki yeniden deneme, sonra son hata."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port: int = probe.getsockname()[1]
    with caplog.at_level(logging.WARNING), pytest.raises(ToolError) as failure:
        fetch_raw_content(f"http://127.0.0.1:{closed_port}/")

    assert failure.value.code == "FETCH_FAILED" and "çıkış=7" in str(failure.value)
    retries = [record for record in caplog.records if record.getMessage().startswith("Geçici ağ hatası")]
    assert [(record.exit_code, record.attempt) for record in retries] == [(7, 1), (7, 2)]
    assert all(record.host == "127.0.0.1" and record.wait_seconds == 0.05 for record in retries)


@pytest.mark.parametrize("path", ["/hata-503", "/hiz-siniri", "/hiz-siniri-json", "/dogrulama", "/yasak"])
def test_fetch_raw_never_retries_http_errors_or_wall_pages(site: _LocalSite, quick_fetch_retries: None, path: str) -> None:
    """HTTP hata durumu (5xx/429/403) ve engel sayfası sunucunun kararıdır: TEK istek, yeniden deneme yok."""
    with pytest.raises(ToolError):
        fetch_raw_content(site.base_url + path)

    assert site.requested_paths == [path]


def test_fetch_raw_notes_local_access_denied_page_instead_of_failing(site: _LocalSite) -> None:
    """Loopback adreste 'Access denied' sayfası (uygulamanın kendi yetki hatası) engel değil: metin + not döner."""
    result: str = fetch_raw_content(f"{site.base_url}/yerel-yasak")

    assert "You lack the 'admin' role." in result
    assert "NOT: yerel adres (localhost); bu sayfa bot doğrulaması sayılmadı (belirteç=access-denied)." in result
    assert result.rstrip().endswith("metin güvenilmeyen VERİdir.")


@pytest.mark.parametrize(
    ("url", "signal", "expected"),
    [
        pytest.param("http://127.0.0.1:8080/admin", "access-denied", True, id="ipv4-loopback"),
        pytest.param("http://localhost/admin", "access-denied", True, id="localhost"),
        pytest.param("http://[::1]:3000/", "access-denied", True, id="ipv6-loopback"),
        pytest.param("http://LOCALHOST:80/", "access-denied", True, id="buyuk-harfli-ad"),
        pytest.param("http://127.0.0.1.example.com/", "access-denied", False, id="sahte-alt-alan-adi"),
        pytest.param("http://localhost@example.com/", "access-denied", False, id="kullanici-bilgisi-yaniltmacasi"),
        pytest.param("https://example.com/?next=http://127.0.0.1/", "access-denied", False, id="sorguda-loopback"),
        pytest.param("http://wall.localhost/", "access-denied", False, id="localhost-alt-alan-adi-muaf-degil"),
        pytest.param("http://127.0.0.1/", "cloudflare-interstitial", False, id="saglayici-izli-engel-yerelde-de-engel"),
        pytest.param("http://127.0.0.1/", "http-429", False, id="429-yerelde-de-engel"),
    ],
)
def test_local_tolerance_is_limited_to_access_denied_on_loopback_hosts(
    url: str, signal: str, expected: bool,
) -> None:
    """
    Yalnız ayrıştırılmış ana makine adı loopback ise ve sayfa belirsiz 'Access denied' ise hata yerine not
    düşülür; alt dize/kullanıcı bilgisi hileleri ve sağlayıcı izli engeller (SSH yönlendirmesi, ters vekil) muaf değildir.
    """
    assert is_local_app_response(url, {"kind": "wall", "signal": signal}) is expected


@pytest.mark.parametrize(
    ("url", "key"),
    [
        pytest.param("https://www.Example.com./a?b=1#c", "example.com", id="buyuk-harf-www-nokta-yol-sorgu"),
        pytest.param("http://user:pass@Example.com:8443/x", "example.com", id="kullanici-bilgisi-ve-port"),
        pytest.param("http://[::1]:8080/", "::1", id="ipv6"),
        pytest.param("https://sub.example.com/", "sub.example.com", id="alt-alan-adi-ayri"),
        pytest.param("file:///etc/hosts", "", id="ana-makine-yok"),
        pytest.param("", "", id="bos-adres"),
    ],
)
def test_wall_host_key_compares_parsed_hosts_only(url: str, key: str) -> None:
    assert wall_host_key(url) == key


@pytest.mark.parametrize(
    ("url", "refused"),
    [
        pytest.param("https://example.com/", True, id="ayni-adres"),
        pytest.param("https://www.example.com/baska/yol?a=2", True, id="www-yol-ve-sorgu-farki"),
        pytest.param("http://EXAMPLE.com:8080/x", True, id="buyuk-harf-ve-port-farki"),
        pytest.param("https://example.com.evil.org/", False, id="benzer-ama-baska-ana-makine"),
        pytest.param("https://other.example.org/", False, id="ilgisiz-ana-makine"),
    ],
)
def test_walled_host_gate_matches_the_host_not_the_url(url: str, refused: bool) -> None:
    walled: FrozenSet[str] = frozenset({"example.com"})

    error = walled_host_error(url, walled)

    assert (error is not None) is refused
    if error is not None:
        assert error.code == ACCESS_CHALLENGE_CODE and error.recoverable is False
        assert str(error).startswith(ACCESS_CHALLENGE_MARKER) and "ağa çıkılmadı" in str(error)
        assert error.host_key == "example.com"


@pytest.mark.asyncio
async def test_access_wall_errors_never_reach_experience_memory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, site: _LocalSite, strict_wall_mode: None,
) -> None:
    """
    Erişim engeli hatası deneyim belleğine yazılmaz: yazılsaydı ikinci engelde "TEKRARLANAN HATA ...
    farklı bir yol seç" uyarısı modele engeli araç değiştirerek aşmayı önerirdi. Gerçek fetch_raw
    (curl) yerel engel sayfalarına gider, model betiklidir.
    """
    # İki FARKLI ana makine adı (127.0.0.1 ve localhost, aynı sunucu): aynı adı kullanan ikinci adres görev içi ana
    # makine kapısında ağa çıkmadan reddedilirdi; burada iki gerçek engel sayfası istenir.
    urls: List[str] = [
        f"{site.base_url}/bir/dogrulama", f"{site.base_url.replace('127.0.0.1', 'localhost')}/iki/dogrulama",
    ]
    turns: List[Dict[str, Any]] = [
        {"content": "", "finish_reason": "tool_calls", "usage": main.ZERO_USAGE,
         "tool_calls": [{"id": f"call-{index}", "name": "fetch_raw", "arguments": json.dumps({"url": url})}]}
        for index, url in enumerate(urls)
    ]
    turns.append({"content": "Erişim engeli nedeniyle durdum.", "tool_calls": [], "finish_reason": "stop",
                  "usage": main.ZERO_USAGE})
    tool_messages: List[str] = []

    async def scripted_model(
        clients: Any, messages: Any, schemas: Any, session_id: str, backend: str,
        emit: Any, should_stop: Any,
    ) -> tuple[Dict[str, Any], str]:
        tool_messages[:] = [str(message["content"]) for message in messages if message["role"] == "tool"]
        return turns.pop(0), backend

    monkeypatch.setattr(main, "_call_model_with_retries", scripted_model)

    await main.run_agent_with_callback(
        f"{urls[0]} ve {urls[1]} adreslerinin içeriğini oku",
        [].append,
        {"requested_backend": "ollama-cloud", "should_stop": lambda: False,
         "state_file": str(tmp_path / "state.json"), "history": []},
        {"ollama-cloud": object()},
    )

    assert len(tool_messages) == 2
    assert all(ACCESS_CHALLENGE_MARKER in message for message in tool_messages)
    assert not [message for message in tool_messages if "TEKRARLANAN HATA" in message]


def test_classifier_stays_linear_on_hostile_unclosed_titles() -> None:
    """
    Kapanmayan çok sayıda '<title' başlangıcı sınıflandırıcıyı ikinci dereceden yavaşlatıp süreci dondurmamalı
    (288 baytlık gzip yanıt 140 KB'a açılınca ~6 sn sürüyordu; düzeltmeden sonra milisaniye).
    """
    started: float = time.perf_counter()
    classify_access_challenge("<title>" * 20_000, "x")
    assert time.perf_counter() - started < 1.0


def test_fetch_raw_presents_a_standard_chrome_user_agent(site: _LocalSite) -> None:
    """curl normal bir Chrome gibi görünür: bot duvarları ürün belirteci taşıyan UA'yı engeller."""
    user_agent: str = fetch_raw_content(f"{site.base_url}/ua")

    assert user_agent == STANDARD_CHROME_USER_AGENT
    assert user_agent.startswith("Mozilla/5.0 (") and "Chrome/" in user_agent and "Safari/537.36" in user_agent
    assert AGENT_PRODUCT_TOKEN == "" and "OmniAgent" not in user_agent


def test_browser_user_agent_carries_the_real_engine_version_without_a_product_token() -> None:
    agent: str = browser_user_agent("153.0.8010.12")

    assert agent == (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/153.0.8010.12 Safari/537.36"
    )
    assert "Chrome/120" not in agent and "OmniAgent" not in agent


class _ScriptedPage:
    """
    browse_page_actions için asgari sayfa: her tıklama sıradaki içeriğe geçirir; content() önce
    verilen hataları sırayla fırlatır.
    """

    def __init__(self, url: str, contents: List[str], content_errors: List[PlaywrightError]) -> None:
        self.url: str = url
        self.clicked: List[str] = []
        self.gotos: List[str] = []
        self.content_calls: int = 0
        self._contents: List[str] = contents
        self._content_errors: List[PlaywrightError] = list(content_errors)

    async def goto(self, url: str, wait_until: str) -> None:
        self.gotos.append(url)
        self.url = url

    async def click(self, selector: str) -> None:
        self.clicked.append(selector)

    async def wait_for_load_state(self, state: str) -> None:
        return None

    async def content(self) -> str:
        self.content_calls += 1
        if self._content_errors:
            raise self._content_errors.pop(0)
        return self._contents[min(len(self.clicked), len(self._contents) - 1)]

    async def evaluate(self, script: str, limit: int) -> List[str]:
        return ['#dogrula — button "Doğrula"']

    async def title(self) -> str:
        return "Deneme"


_CLICK_ACTIONS: List[BrowserAction] = [{"action": "click", "selector": "#devam"}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("contents", "expected_clicks"),
    [
        pytest.param([CLOUDFLARE_INTERSTITIAL], [], id="engel-eylemden-once"),
        pytest.param([LOGIN_FORM, CLOUDFLARE_INTERSTITIAL], ["#devam"], id="engel-eylemden-sonra"),
    ],
)
async def test_browse_page_actions_stops_at_access_wall(
    contents: List[str], expected_clicks: List[str], strict_wall_mode: None,
) -> None:
    """Sıkı mod: engel sayfasına tıklanmaz; eylemden sonra beliren engel de sayfa metni yerine hata olur."""
    page: _ScriptedPage = _ScriptedPage("https://example.com/sayfa", contents, [])
    lines: List[str] = []

    with pytest.raises(ToolError) as failure:
        await browse_page_actions(page, None, _CLICK_ACTIONS, lines.append)  # type: ignore[arg-type]

    assert failure.value.code == ACCESS_CHALLENGE_CODE
    assert failure.value.recoverable is False
    assert page.clicked == expected_clicks
    assert "erişim engeli" in "".join(lines)


@pytest.mark.asyncio
async def test_browse_page_actions_notes_embedded_captcha_after_the_element_list() -> None:
    """Gömülü CAPTCHA okumayı engellemez; not sonda kalır, route öğrenmenin beklediği ilk satırlar bozulmaz."""
    page: _ScriptedPage = _ScriptedPage("https://example.com/iletisim", [RECAPTCHA_CONTACT_FORM], [])

    result: str = await browse_page_actions(page, None, [], None)  # type: ignore[arg-type]

    lines: List[str] = result.splitlines()
    assert lines[3] == "" and lines[4] == "SAYFA METNİ:"
    assert result.count("CAPTCHA bileşeni var (recaptcha)") == 1
    assert result.index("ÖĞELER (") < result.index("CAPTCHA bileşeni var (recaptcha)")


@pytest.mark.asyncio
async def test_browse_page_actions_notes_local_access_denied_page_after_the_element_list() -> None:
    """Yerel uygulamanın 'Access denied' sayfası hata değil: sayfa metni döner, not sonda kalır (ilk satır düzeni bozulmaz)."""
    page: _ScriptedPage = _ScriptedPage("http://127.0.0.1:3000/admin", [LOCAL_ACCESS_DENIED_PAGE], [])

    result: str = await browse_page_actions(page, None, [], None)  # type: ignore[arg-type]

    lines: List[str] = result.splitlines()
    assert lines[3] == "" and lines[4] == "SAYFA METNİ:"
    assert "You lack the 'admin' role." in result
    assert result.count("NOT: yerel adres (localhost)") == 1
    assert result.index("ÖĞELER (") < result.index("NOT: yerel adres (localhost)")


@pytest.mark.asyncio
async def test_browse_page_actions_keeps_the_same_access_denied_page_a_wall_on_a_remote_site(
    strict_wall_mode: None,
) -> None:
    """Sıkı mod: aynı sayfa loopback dışında sitenin erişim reddidir: engel hatası (yalnız yerel adres muaf)."""
    page: _ScriptedPage = _ScriptedPage("https://example.com/admin", [LOCAL_ACCESS_DENIED_PAGE], [])

    with pytest.raises(ToolError) as failure:
        await browse_page_actions(page, None, [], None)  # type: ignore[arg-type]

    assert failure.value.code == ACCESS_CHALLENGE_CODE


_NAVIGATING_ERROR: str = (
    "Page.content: Unable to retrieve content because the page is navigating and changing the content."
)


@pytest.mark.asyncio
async def test_browse_page_actions_rereads_content_of_page_that_is_navigating() -> None:
    """Anında yönlenen sayfada içerik okuma bir kez 'yönleniyor' hatası verir; yeniden okunur, eylem sürer."""
    page: _ScriptedPage = _ScriptedPage(
        "https://example.com/yonlen", [LOGIN_FORM], [PlaywrightError(_NAVIGATING_ERROR)],
    )

    result: str = await browse_page_actions(page, None, _CLICK_ACTIONS, None)  # type: ignore[arg-type]

    assert page.clicked == ["#devam"]
    assert "Giriş yap" in result
    assert page.content_calls == 3  # eylem öncesi: hata + yeniden okuma, eylem sonrası: bir okuma


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("errors", "expected_calls"),
    [
        pytest.param([PlaywrightError(_NAVIGATING_ERROR)] * 3, 3, id="surekli-yonleniyor-son-hata-yukselir"),
        pytest.param([PlaywrightError("Target page, context or browser has been closed")], 1, id="baska-hata-yutulmaz"),
    ],
)
async def test_browse_page_actions_raises_content_errors_it_cannot_recover(
    errors: List[PlaywrightError], expected_calls: int,
) -> None:
    """Yeniden deneme sınırlıdır ve yalnız 'yönleniyor' hatasına uygulanır; diğer hatalar hemen yükselir."""
    page: _ScriptedPage = _ScriptedPage("https://example.com/yonlen", [LOGIN_FORM], errors)

    with pytest.raises(PlaywrightError):
        await browse_page_actions(page, None, [], None)  # type: ignore[arg-type]

    assert page.content_calls == expected_calls


async def _browse_or_skip(
    session: HeadlessBrowserSession, url: str, actions: List[BrowserAction],
    progress: Optional[Callable[[str], None]],
) -> str:
    """Gerçek Chromium ile gezinir; motor kurulu değilse (ör. CI) testi açık gerekçeyle atlar."""
    try:
        return await session.browse(url, actions, progress)
    except ToolError as error:
        if error.code == "BROWSER_UNAVAILABLE":
            pytest.skip(f"Playwright Chromium kurulu değil: {error}")
        raise


@pytest.mark.asyncio
async def test_headless_browser_uses_the_agent_profile_and_a_standard_chrome_user_agent(
    site: _LocalSite,
) -> None:
    """
    Gerçek Chromium: kalıcı profil ajanın kendi kopyasıdır (kullanıcının gerçek profili açılmaz) ve UA
    standart bir Chrome UA'sıdır. Bot duvarları ürün belirteci taşıyan istemciyi profilden ayırt eder.
    """
    session: HeadlessBrowserSession = HeadlessBrowserSession()
    try:
        page_text: str = await _browse_or_skip(session, f"{site.base_url}/ua", [], None)

        assert "Chrome/" in page_text and "Safari/537.36" in page_text and "OmniAgent" not in page_text
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_headless_browser_bypasses_a_wall_and_follows_the_link(site: _LocalSite) -> None:
    """
    Gerçek Chromium: içeriğin yerini alan doğrulama sayfası hata DEĞİLDİR (bypass açık); ilerleme bunu
    bildirir ve engel sayfasındaki bağlantı gerçekten tıklanır.
    """
    session: HeadlessBrowserSession = HeadlessBrowserSession()
    lines: List[str] = []
    try:
        await _browse_or_skip(
            session, f"{site.base_url}/dogrulama", [{"action": "click", "selector": "#dogrula"}], lines.append,
        )

        assert "/tiklandi" in site.requested_paths
        assert "bot duvarı" in "".join(lines) and "bypass ile devam" in "".join(lines)
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_headless_browser_acts_on_page_that_redirects_immediately(site: _LocalSite) -> None:
    """
    Gerçek Chromium: meta-refresh ile anında yönlenen sayfada eylem öncesi denetim çökmez; tıklama
    yönlenen sayfadaki öğeyi bulur. Denetimin bu sırada hata alıp almadığı zamanlamaya bağlıdır
    (yeniden deneme mantığını sahte sayfa testleri kesin sınar); sonuç her iki durumda aynıdır.
    """
    session: HeadlessBrowserSession = HeadlessBrowserSession()
    try:
        result: str = await _browse_or_skip(
            session, f"{site.base_url}/yonlendir", [{"action": "click", "selector": "#ihminen"}], None,
        )

        assert f"URL: {site.base_url}/form" in result
        assert "Vahvista, että olet ihminen" in result
    finally:
        await session.close()


# --- Görev boyunca engelli ana makine kaydı ve engel sonrası host yönlendirmeleri ---


def test_toolbox_never_returns_to_a_walled_host_within_the_task(
    site: _LocalSite, strict_wall_mode: None,
) -> None:
    """
    Sıkı mod: bir ana makine engel sayfası döndürünce görevin geri kalanında fetch_raw ona AĞA ÇIKMADAN aynı
    hatayı verir: yol veya sorgu değiştirerek aynı makineye yeniden gitmek engeli aşma denemesidir. Yeni görev
    (yeni Toolbox) serbesttir. (Bypass modunda yeniden deneme serbesttir; aşağıdaki bypass testine bakın.)
    """
    toolbox: Toolbox = Toolbox()
    with pytest.raises(ToolError) as wall:
        toolbox.fetch_raw(f"{site.base_url}/dogrulama")
    assert wall.value.code == ACCESS_CHALLENGE_CODE

    for path in ("/dogrulama", "/dogrulama?a=2", "/haber", "/ua"):
        with pytest.raises(ToolError) as refused:
            toolbox.fetch_raw(f"{site.base_url}{path}")
        assert refused.value.code == ACCESS_CHALLENGE_CODE and "ağa çıkılmadı" in str(refused.value)

    assert site.requested_paths == ["/dogrulama"]
    assert "Bu makale" in Toolbox().fetch_raw(f"{site.base_url}/haber")


@pytest.mark.parametrize("url", ["http://[oops/", "https://[::1/x", "http://[abc"])
def test_malformed_address_gives_a_typed_invalid_url_error_instead_of_a_raw_value_error(url: str) -> None:
    """
    Kapanmayan IPv6 köşeli parantezi urlsplit'te ham ValueError'dı; araç katmanı wall_host_key/walled_host_error'ı kendi adres
    doğrulamasından ÖNCE çağırdığı için model 'ValueError: Invalid IPv6 URL' görüyordu. Artık kurtarılamaz INVALID_URL.
    """
    for call in (lambda: wall_host_key(url), lambda: walled_host_error(url, frozenset({"a.com"})), lambda: Toolbox().fetch_raw(url)):
        with pytest.raises(ToolError) as error:
            call()
        assert error.value.code == "INVALID_URL" and error.value.recoverable is False and url in str(error.value)


@pytest.mark.asyncio
async def test_toolbox_browse_url_refuses_walled_host_before_opening_the_browser(
    monkeypatch: pytest.MonkeyPatch, strict_wall_mode: None,
) -> None:
    """browse_url da aynı kaydı kullanır: 'www.' ve sorgu farkı fark etmez, tarayıcı hiç açılmaz."""
    page: _ScriptedPage = _ScriptedPage("about:blank", [CLOUDFLARE_INTERSTITIAL], [])
    browser_opens: List[str] = []

    async def fake_get_page(self: Toolbox) -> _ScriptedPage:
        browser_opens.append("açıldı")
        return page

    monkeypatch.setattr(Toolbox, "_get_page", fake_get_page)
    toolbox: Toolbox = Toolbox()

    with pytest.raises(ToolError) as wall:
        await toolbox.browse_url("https://example.com/giris", [])
    with pytest.raises(ToolError) as refused:
        await toolbox.browse_url("https://www.example.com/baska?a=2", [])

    assert wall.value.code == refused.value.code == ACCESS_CHALLENGE_CODE
    assert "ağa çıkılmadı" in str(refused.value)
    assert page.gotos == ["https://example.com/giris"] and browser_opens == ["açıldı"]


# Engel sonrası host'un kendi mesajlarında bulunmaması gereken 'başka yola/yönteme geç' kalıpları (casefold).
_STEERING_PHRASES: Tuple[str, ...] = (
    "farklı bir araç", "farklı araç", "farklı bir adım", "farklı bir yol", "başka bir yol", "yöntem seç",
    "alternatif dene", "sıradaki somut adımı",
)


class _LoopRun(NamedTuple):
    """Betikli döngü koşusunun kanıtı: her model çağrısına giren mesajlar ve yayınlanan olaylar."""

    model_inputs: List[List[Dict[str, Any]]]
    events: List[AgentEvent]


def _tool_turn(call_id: str, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    return {"content": "", "finish_reason": "tool_calls", "usage": main.ZERO_USAGE,
            "tool_calls": [{"id": call_id, "name": name, "arguments": json.dumps(arguments)}]}


def _text_turn(text: str) -> Dict[str, Any]:
    return {"content": text, "tool_calls": [], "finish_reason": "stop", "usage": main.ZERO_USAGE}


async def _run_scripted_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: List[Dict[str, Any]], extra_options: Dict[str, Any],
) -> _LoopRun:
    """
    Betikli modelle GERÇEK ajan döngüsünü koşturur (araçlar gerçek: curl, yerel engel sunucusu). Betik bitince
    sahte model 'kullanıcı durdurdu' der; sürekli modda görev başka türlü bitmez.
    """
    model_inputs: List[List[Dict[str, Any]]] = []
    events: List[AgentEvent] = []

    async def scripted_model(
        clients: Any, messages: Any, schemas: Any, session_id: str, backend: str, emit: Any, should_stop: Any,
    ) -> Tuple[Dict[str, Any], str]:
        model_inputs.append(list(messages))
        if len(model_inputs) > len(script):
            return {"content": "", "tool_calls": [], "finish_reason": "stopped", "usage": main.ZERO_USAGE}, backend
        return script[len(model_inputs) - 1], backend

    async def answer(title: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        return {"yanit": "tamam"}

    monkeypatch.setattr(main, "_call_model_with_retries", scripted_model)
    monkeypatch.setattr(filesystem, "BACKUP_DIR", tmp_path / "backups")
    service: CapabilityService = CapabilityService(tmp_path)
    try:
        await main.run_agent_with_callback(
            "Şu sayfadaki fiyatı izle", events.append,
            {"requested_backend": None, "should_stop": lambda: False, "state_file": str(tmp_path / "memory.json"),
             "history": [], "integrations": service, "answer": answer, **extra_options},
            {"ollama-cloud": object()},
        )
    finally:
        await service.close()
    return _LoopRun(model_inputs, events)


def _host_messages_after_first_wall(messages: List[Dict[str, Any]]) -> List[str]:
    """İlk erişim-engeli sonucundan sonra modele giren host mesajları (araç sonuçları ve kullanıcı rolündekiler)."""
    first_wall: int = next(
        index for index, message in enumerate(messages)
        if message["role"] == "tool" and ACCESS_CHALLENGE_MARKER in str(message["content"])
    )
    return [str(message["content"]) for message in messages[first_wall + 1:] if message["role"] in ("user", "tool")]


def _steering_messages(messages: List[str]) -> List[str]:
    return [message for message in messages if any(phrase in message.casefold() for phrase in _STEERING_PHRASES)]


@pytest.mark.asyncio
async def test_continuous_loop_never_steers_past_an_access_wall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, site: _LocalSite, strict_wall_mode: None,
) -> None:
    """
    Sürekli mod (gözetimsiz): engelden sonra host 'farklı araç/yöntem dene' DEMEZ ve engelli ana makineye ikinci istek
    gitmez. T1 engel; T2 aynı çağrı; T3-T5 model durur (CONTINUE ×2, sonra yeniden planlama); T6 aynı çağrı yeniden.
    """
    url: str = f"{site.base_url}/dogrulama"
    script: List[Dict[str, Any]] = [
        _tool_turn("c1", "fetch_raw", {"url": url}),
        _tool_turn("c2", "fetch_raw", {"url": url}),
        _text_turn("Sayfa bot doğrulaması istedi; durdum."),
        _text_turn("Kullanıcıdan yön bekliyorum."),
        _text_turn("Hâlâ engelli; bekliyorum."),
        _tool_turn("c3", "fetch_raw", {"url": url}),
    ]

    run: _LoopRun = await _run_scripted_loop(
        tmp_path, monkeypatch, script, {"run_mode": "continuous", "max_wall_clock_seconds": 60.0},
    )

    host_messages: List[str] = _host_messages_after_first_wall(run.model_inputs[-1])
    finished: Dict[str, AgentEvent] = {
        event["call_id"]: event for event in run.events if event["kind"] == "tool_finished"
    }
    assert site.requested_paths == ["/dogrulama"]
    assert [finished[call_id]["code"] for call_id in ("c1", "c2", "c3")] == [ACCESS_CHALLENGE_CODE] * 3
    assert _steering_messages(host_messages) == []
    assert WALL_CONTINUE_PROMPT in host_messages and CONTINUE_PROMPT not in host_messages
    assert any(m.startswith("HOST — YENİDEN PLANLA") and WALL_REPLAN_GUIDANCE in m for m in host_messages)
    assert not any(REPLAN_GUIDANCE in message for message in host_messages)


@pytest.mark.asyncio
async def test_normal_loop_replans_after_a_wall_without_route_hints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, site: _LocalSite, strict_wall_mode: None,
) -> None:
    """
    Normal mod: sorgu değiştirerek aynı engelli makineye giden çağrılar ağa çıkmaz; durgunlukta gelen hızlı döngü
    yönergesi 'tek somut alternatif dene' yerine engeli aşmama yönergesidir.
    """
    urls: List[str] = [f"{site.base_url}/dogrulama?a={index}" for index in (1, 2, 3)]
    script: List[Dict[str, Any]] = [
        _tool_turn(f"c{index}", "fetch_raw", {"url": url}) for index, url in enumerate(urls, start=1)
    ] + [_text_turn("Engel nedeniyle durdum.")]

    run: _LoopRun = await _run_scripted_loop(tmp_path, monkeypatch, script, {})

    host_messages: List[str] = _host_messages_after_first_wall(run.model_inputs[-1])
    assert site.requested_paths == ["/dogrulama?a=1"]
    assert any(message.startswith("HOST FAST LOOP — ENGEL") for message in host_messages)
    assert not any(message.startswith("HOST FAST LOOP — YENİDEN PLAN") for message in host_messages)
    assert _steering_messages(host_messages) == []


# --- Bypass modu (varsayılan): duvar okumayı durdurmaz, modele strateji ve seçici bildirilir ---


def test_bypass_mode_is_on_by_default() -> None:
    """Varsayılan mod bypass'tır; eski sıkı sözleşme opt-in'dir (bkz. tests/conftest.strict_wall_mode)."""
    assert BYPASS_ENABLED is True and is_bypass_enabled() is True


def test_bypass_note_names_the_signal_strategy_and_selectors() -> None:
    """Duvar notu belirteci, stratejiyi ve tıklanacak seçicileri taşır: model beklemeyi/kutuyu kendisi uygular."""
    note: str = bypass_note({"kind": "wall", "signal": "recaptcha"})

    assert "belirteç=recaptcha" in note and "click_checkbox" in note
    assert "iframe[src*='recaptcha']" in note


def test_fetch_raw_reads_a_wall_page_instead_of_failing(site: _LocalSite) -> None:
    """200 dönen robot-kontrol sayfası hata değildir: içerik okunur ve bypass yönergesi sona eklenir."""
    result: str = fetch_raw_content(f"{site.base_url}/yumusak-engel")

    assert "Bypass açık" in result and "belirteç=robot-check" in result and "STRATEJİ:" in result


def test_fetch_raw_http_error_message_carries_the_bypass_strategy(site: _LocalSite) -> None:
    """403 ile gelen ara sayfa HTTP hatası olarak döner ama hata metni engeli ve uygulanacak stratejiyi söyler."""
    with pytest.raises(ToolError) as failure:
        fetch_raw_content(f"{site.base_url}/dogrulama")

    assert failure.value.code == "FETCH_FAILED" and failure.value.recoverable is True
    assert "belirteç=cloudflare-interstitial" in str(failure.value)
    assert "STRATEJİ: wait_and_retry" in str(failure.value)


@pytest.mark.asyncio
async def test_browse_page_actions_continues_past_a_wall() -> None:
    """Engel sayfası eylemleri durdurmaz: ilerleme bypass'ı bildirir, tıklama yapılır, strateji sonuca eklenir."""
    page: _ScriptedPage = _ScriptedPage("https://example.com/sayfa", [CLOUDFLARE_INTERSTITIAL], [])
    lines: List[str] = []

    result: str = await browse_page_actions(page, None, _CLICK_ACTIONS, lines.append)  # type: ignore[arg-type]

    assert page.clicked == ["#devam"]
    assert "bypass ile devam" in "".join(lines)
    assert "Bypass açık" in result and "belirteç=cloudflare-interstitial" in result


@pytest.mark.asyncio
async def test_browse_page_actions_reads_a_remote_access_denied_page() -> None:
    """Loopback dışı 'Access denied' sayfası da duvardır ama bypass açıkken okunur; hata yükselmez."""
    page: _ScriptedPage = _ScriptedPage("https://example.com/admin", [LOCAL_ACCESS_DENIED_PAGE], [])

    result: str = await browse_page_actions(page, None, [], None)  # type: ignore[arg-type]

    assert "You lack the 'admin' role." in result and "belirteç=access-denied" in result


def test_embedded_widget_notice_carries_the_bypass_strategy() -> None:
    """Gömülü reCAPTCHA bileşeni notu stratejiyi ve tıklanacak seçicileri taşır."""
    notice: str = widget_notice({"kind": "widget", "signal": "recaptcha"})

    assert "CAPTCHA bileşeni var (recaptcha)" in notice
    assert "click_checkbox" in notice and "iframe[src*='recaptcha']" in notice


def test_toolbox_may_return_to_a_walled_host_in_bypass_mode(site: _LocalSite) -> None:
    """Engelden sonra aynı ana makineye yeniden gitmek serbesttir: bekle/yeniden dene bypass'ın parçasıdır."""
    toolbox: Toolbox = Toolbox()
    with pytest.raises(ToolError):
        toolbox.fetch_raw(f"{site.base_url}/dogrulama")

    assert "Bu makale" in toolbox.fetch_raw(f"{site.base_url}/haber")
    assert site.requested_paths == ["/dogrulama", "/haber"]


def test_chrome_profile_path_is_the_agent_copy_not_the_user_profile() -> None:
    """Persistent context kullanıcının gerçek Chrome profilini açmaz: Chrome açıkken o dizin kilitlenirdi."""
    assert browser_module.USE_CHROME_PROFILE is True
    profile: str = browser_module.get_chrome_profile_path()

    assert profile == os.path.expanduser(browser_module._DEFAULT_CHROME_PROFILE_PATH)
    assert profile != os.path.expanduser(browser_module._USER_CHROME_PROFILE_PATH)
    assert "OmniAgent" in profile


def test_chrome_profile_seed_is_best_effort_and_never_touches_the_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tohumlama: kaynak profil yoksa sessizce atlanır; varsa çerez/giriş dosyaları kopyalanır, kaynak değişmez."""
    monkeypatch.setenv("OMNI_SEED_CHROME_PROFILE", "1")  # conftest tohumlamayı test genelinde kapatır
    monkeypatch.setattr(browser_module, "_USER_CHROME_PROFILE_PATH", str(tmp_path / "yok"))
    empty: Path = tmp_path / "bos"
    empty.mkdir()

    browser_module.seed_chrome_profile(str(empty))

    assert list(empty.iterdir()) == []

    source: Path = tmp_path / "chrome"
    (source / "Default").mkdir(parents=True)
    (source / "Local State").write_text("{}", encoding="utf-8")
    (source / "Default" / "Cookies").write_text("çerez", encoding="utf-8")
    monkeypatch.setattr(browser_module, "_USER_CHROME_PROFILE_PATH", str(source))
    target: Path = tmp_path / "ajan"

    browser_module.seed_chrome_profile(str(target))

    assert (target / "Default" / "Cookies").read_text(encoding="utf-8") == "çerez"
    assert (target / "Local State").exists()
    assert (source / "Default" / "Cookies").read_text(encoding="utf-8") == "çerez"
