"""
CANLI sınama (opt-in): OMNI_LIVE_GUI=1 yokken atlanır. AYRI, geçici profilli bir Chrome örneği açar; hiçbir çağrı ad ile
uygulama aramaz (yalnız pid), böylece kullanıcının gerçek Chrome'una dokunulmaz. Akış: anlık görüntü -> öğe bulma ->
yazma -> tıklama merdiveni (arka plan AXPress, ön plan tıklaması) -> bayat/örtülü koruması -> ödeme/sipariş düğmesi
için host onay kapısı (onaysız/ret/zaman aşımı/kanal yok/onay sonrası hedef değişimi: tıklama YOK; onaylı: TEK tetikleme).

Ödeme kapısı testlerinde tıklamanın gerçekten olup olmadığının kanıtı sayfanın yerel bir HTTP sunucusuna attığı
isteklerdir (DOM/AX'ten bağımsız: etkisi görünmeyen düğme de sayılır).

Not: başka süreçler (ör. eşzamanlı UI testleri) odak çalabilir; ön plan gerektiren testler Chrome öne alınamazsa ATLANIR.
"""
import asyncio
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any, Dict, Iterator, List, Optional, Tuple
from urllib.parse import urlsplit

import ApplicationServices as AX
import json
import pytest

from omniagent import approval, tools
from omniagent.app import agent as main
from omniagent.integrations.runtime import CURRENT_RUNTIME, IntegrationRuntime
from omniagent.tools import Toolbox, foreground, gui_input
from omniagent.tools.ax_snapshot import SNAPSHOT_LIMITS, Element, Snapshot, ax_read, ax_text, frame_center
from omniagent.tools.gui_input import CUA, ResolvedElement
from omniagent.tools.types import ToolError

LIVE: bool = os.environ.get("OMNI_LIVE_GUI") == "1" and sys.platform == "darwin"
pytestmark = pytest.mark.skipif(not LIVE, reason="Canlı GUI testi: OMNI_LIVE_GUI=1 ve macOS gerekir")

CHROME_APP: Path = Path("/Applications/Google Chrome.app")
PAGE: str = """<!doctype html><html><head><meta charset="utf-8"><title>canli-baslangic</title></head>
<body style="font-family:sans-serif">
<h1>Canlı sınama sayfası</h1>
<p id="say">sayaç: 0</p>
<button id="b1" style="width:160px;height:40px"
  onclick="window.n=(window.n||0)+1;document.title='basildi:'+window.n;document.getElementById('say').textContent='sayaç: '+window.n">Bana bas</button>
<button id="rename" onclick="document.getElementById('b1').textContent='Yeni ad'">Adı değiştir</button>
<input id="ad" aria-label="Ad alanı" oninput="document.title='yazildi:'+this.value"><br><br>
<input id="pw" type="password" aria-label="Parola alanı" value="gizli123"><br><br>
<label><input id="cb" type="checkbox" onchange="document.title='isaret:'+this.checked"> Onay kutusu</label><br>
<button id="off" disabled>Pasif düğme</button>
<script>function hit(name) { fetch("http://127.0.0.1:__PORT__/hit?b=" + name, {mode: "no-cors"}); }</script>
<hr><p>Toplam: 249,90 TL</p><p id="durum">durum: bekliyor</p>
<button id="ayrinti" onclick="hit('ayrinti');document.getElementById('durum').textContent='durum: ayrıntı'">Ayrıntıları göster</button>
<button id="pay" onclick="hit('pay');document.getElementById('durum').textContent='durum: pay'">Pay now</button>
<button id="pay2" onclick="hit('pay2');document.getElementById('durum').textContent='durum: pay2'">Buy now</button>
<button id="swap" onclick="document.getElementById('pay2').textContent='Cancel order'">Etiketi değiştir</button>
<button id="sessiz" onclick="hit('sessiz')">Complete purchase</button>
<button id="ocr" style="font-size:28px;padding:10px" onclick="hit('ocr');document.getElementById('durum').textContent='durum: ocr'"><span aria-hidden="true">Ödemeyi onayla</span></button>
<div id="robot" role="checkbox" aria-checked="false" tabindex="0" onclick="hit('robot')">I'm not a robot</div>
</body></html>"""

# Arka plan sınaması için: sağ kenarda küçük, KENDİNİ ETKİNLEŞTİREN pencere (Chrome penceresiyle kesişmez, Chrome arka planda kalır)
CORNER_WINDOW_SCRIPT: str = """
import AppKit
app = AppKit.NSApplication.sharedApplication()
app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)
size = AppKit.NSScreen.mainScreen().frame().size
window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
    AppKit.NSMakeRect(size.width - 260, 200, 240, 120), AppKit.NSWindowStyleMaskTitled, AppKit.NSBackingStoreBuffered, False)
window.makeKeyAndOrderFront_(None)
app.activateIgnoringOtherApps_(True)
app.run()
"""

# Örtülü hedef sınaması için: verilen noktanın üstünde ÜSTTE KALAN (yüzen) küçük pencere açan yardımcı süreç
FLOATING_WINDOW_SCRIPT: str = """
import sys, AppKit
app = AppKit.NSApplication.sharedApplication()
app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)
x, y = float(sys.argv[1]), float(sys.argv[2])
height = AppKit.NSScreen.mainScreen().frame().size.height
window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
    AppKit.NSMakeRect(x - 100, height - y - 30, 200, 60), AppKit.NSWindowStyleMaskTitled, AppKit.NSBackingStoreBuffered, False)
window.setLevel_(AppKit.NSFloatingWindowLevel)
window.makeKeyAndOrderFront_(None)
app.run()
"""


# Yerel (AppKit) test uygulaması: NSButton, onay kutusu, NSTextField, NSSecureTextField ve AXPress DESTEKLEMEYEN özel düğme.
# Sonuçlar pencere başlığına yazılır: basildi:N / kutu:0|1 / alan:<metin> / ozel:N.
NATIVE_APP_SCRIPT: str = """
import AppKit, objc

class ClickView(AppKit.NSView):
    def initWithFrame_(self, frame):
        self = objc.super(ClickView, self).initWithFrame_(frame)
        self.count = 0
        return self
    def isAccessibilityElement(self):
        return True
    def accessibilityRole(self):
        return "AXButton"
    def accessibilityLabel(self):
        return "Özel düğme"
    def acceptsFirstMouse_(self, event):
        return True
    def drawRect_(self, rect):
        AppKit.NSColor.systemBlueColor().setFill()
        AppKit.NSRectFill(self.bounds())
    def mouseDown_(self, event):
        self.count += 1
        self.window().setTitle_("ozel:%d" % self.count)

class Controller(AppKit.NSObject):
    def pressed_(self, sender):
        self.n = getattr(self, "n", 0) + 1
        sender.window().setTitle_("basildi:%d" % self.n)
    def toggled_(self, sender):
        sender.window().setTitle_("kutu:%d" % sender.state())
    def controlTextDidChange_(self, notification):
        field = notification.object()
        field.window().setTitle_("alan:" + str(field.stringValue()))

app = AppKit.NSApplication.sharedApplication()
app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)
height = AppKit.NSScreen.mainScreen().frame().size.height
window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
    AppKit.NSMakeRect(60, height - 560 - 300, 420, 300), AppKit.NSWindowStyleMaskTitled, AppKit.NSBackingStoreBuffered, False)
window.setTitle_("yerel-baslangic")
controller = Controller.alloc().init()
content = window.contentView()
button = AppKit.NSButton.alloc().initWithFrame_(AppKit.NSMakeRect(20, 250, 120, 30))
button.setTitle_("Tamam düğmesi"); button.setTarget_(controller); button.setAction_("pressed:")
box = AppKit.NSButton.alloc().initWithFrame_(AppKit.NSMakeRect(20, 210, 160, 24))
box.setButtonType_(AppKit.NSButtonTypeSwitch); box.setTitle_("Yerel onay kutusu"); box.setTarget_(controller); box.setAction_("toggled:")
field = AppKit.NSTextField.alloc().initWithFrame_(AppKit.NSMakeRect(20, 170, 240, 24))
field.setDelegate_(controller); field.cell().setAccessibilityLabel_("Yerel alan")
secure = AppKit.NSSecureTextField.alloc().initWithFrame_(AppKit.NSMakeRect(20, 130, 240, 24))
secure.cell().setAccessibilityLabel_("Yerel parola")
custom = ClickView.alloc().initWithFrame_(AppKit.NSMakeRect(20, 60, 140, 40))
for view in (button, box, field, secure, custom):
    content.addSubview_(view)
window.makeKeyAndOrderFront_(None)
app.activateIgnoringOtherApps_(True)
app.run()
"""


# AXPress DESTEKLEMEYEN iki özel düğme: biri erişilebilirlik adı 'Pay now', öbürü adsız ama ekranda 'Ödemeyi onayla' yazıyor.
# Tıklama sayısı pencere başlığına yazılır (odeme:N).
PAY_APP_SCRIPT: str = """
import AppKit, objc

count = [0]

class PayView(AppKit.NSView):
    def isAccessibilityElement(self):
        return True
    def accessibilityRole(self):
        return "AXButton"
    def acceptsFirstMouse_(self, event):
        return True
    def drawRect_(self, rect):
        AppKit.NSColor.whiteColor().setFill()
        AppKit.NSRectFill(self.bounds())
        attributes = {AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_(24),
                      AppKit.NSForegroundColorAttributeName: AppKit.NSColor.blackColor()}
        AppKit.NSString.stringWithString_(self.caption()).drawAtPoint_withAttributes_((10, 10), attributes)
    def mouseDown_(self, event):
        count[0] += 1
        self.window().setTitle_("odeme:%d" % count[0])

class LabeledPay(PayView):
    def accessibilityLabel(self):
        return "Pay now"
    def caption(self):
        return "Pay now"

class SilentPay(PayView):
    def accessibilityLabel(self):
        return ""
    def caption(self):
        return "Ödemeyi onayla"

app = AppKit.NSApplication.sharedApplication()
app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)
height = AppKit.NSScreen.mainScreen().frame().size.height
window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
    AppKit.NSMakeRect(60, height - 560 - 300, 420, 300), AppKit.NSWindowStyleMaskTitled, AppKit.NSBackingStoreBuffered, False)
window.setTitle_("odeme-baslangic")
content = window.contentView()
content.addSubview_(LabeledPay.alloc().initWithFrame_(AppKit.NSMakeRect(20, 200, 200, 44)))
content.addSubview_(SilentPay.alloc().initWithFrame_(AppKit.NSMakeRect(20, 120, 260, 44)))
window.makeKeyAndOrderFront_(None)
app.activateIgnoringOtherApps_(True)
app.run()
"""


class LiveChrome:
    """Geçici profilli Chrome örneği: pid, uygulama öğesi ve temizlik bilgisi."""

    def __init__(self, pid: int, workdir: Path) -> None:
        self.pid: int = pid
        self.workdir: Path = workdir


class HitServer:
    """Sayfanın düğmelerinin gerçekten tıklandığını sayan yerel HTTP sunucusu (DOM/AX'ten bağımsız kanıt)."""

    def __init__(self) -> None:
        self.hits: List[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                outer.hits.append(urlsplit(self.path).query.removeprefix("b="))
                self.send_response(204)
                self.end_headers()

            def log_message(self, *arguments: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port: int = self.server.server_address[1]
        Thread(target=self.server.serve_forever, daemon=True).start()

    def settled(self, seconds: float) -> List[str]:
        """İstekler asenkron gelir: kısa süre bekleyip o ana kadarki tıklamaları döner."""
        time.sleep(seconds)
        return list(self.hits)


def _launch_chrome(port: int) -> LiveChrome:
    workdir = Path(tempfile.mkdtemp(prefix="omni-ax-live-"))
    (workdir / "page.html").write_text(PAGE.replace("__PORT__", str(port)), encoding="utf-8")
    profile = workdir / "profile"
    subprocess.run(
        ["open", "-g", "-n", "-a", "Google Chrome", "--args", f"--user-data-dir={profile}", "--no-first-run",
         "--no-default-browser-check", f"file://{workdir / 'page.html'}"],
        check=True,
    )
    deadline = time.time() + 30
    pid: Optional[int] = None
    while time.time() < deadline and pid is None:
        for candidate in subprocess.run(["pgrep", "-f", str(profile)], capture_output=True, text=True).stdout.split():
            command = subprocess.run(["ps", "-o", "command=", "-p", candidate], capture_output=True, text=True).stdout
            if "--type=" not in command and f"user-data-dir={profile}" in command:
                pid = int(candidate)
        time.sleep(0.3)
    if pid is None:
        shutil.rmtree(workdir, ignore_errors=True)
        pytest.fail("Geçici Chrome süreci bulunamadı")
    return LiveChrome(pid, workdir)


def _wait_for_window(instance: LiveChrome) -> None:
    application = AX.AXUIElementCreateApplication(instance.pid)
    deadline = time.time() + 30
    while time.time() < deadline and not _has_windows(application):
        time.sleep(0.3)


def _has_windows(application: object) -> bool:
    """Açılış sırasında Chrome erişilebilirlik sorgusuna henüz yanıt vermeyebilir (AX_TIMEOUT): pencere gelene kadar yoklanır."""
    try:
        return bool(ax_read(application, "AXWindows"))
    except ToolError as error:
        if error.code != "AX_TIMEOUT":
            raise
        return False


@pytest.fixture(scope="module")
def hit_server() -> Iterator[HitServer]:
    server = HitServer()
    yield server
    server.server.shutdown()


@pytest.fixture(scope="module")
def chrome(hit_server: HitServer) -> Iterator[LiveChrome]:
    if not CHROME_APP.exists():
        pytest.skip("Google Chrome kurulu değil")
    if not AX.AXIsProcessTrusted():
        pytest.skip("Erişilebilirlik izni yok")
    front_before: Optional[Tuple[str, int]] = gui_input._front_app_owner()
    instance = _launch_chrome(hit_server.port)
    try:
        _wait_for_window(instance)
        # Web erişilebilirliği örtülü/arka plan pencerede kurulmaz: ilk yakalama için kısa süre öne alınır
        require_foreground(instance)
        warmup = CUA().capture(instance.pid, "Google Chrome", SNAPSHOT_LIMITS)
        if warmup["stats"]["web_ready"] is not True:
            pytest.skip("Chrome web erişilebilirliği hazır olmadı (pencere görünmüyor olabilir)")
        yield instance
    finally:
        # Kurulum yarıda kesilse (atlama/hata) bile geçici Chrome ve profil temizlenir
        subprocess.run(["kill", str(instance.pid)])
        time.sleep(1.0)
        shutil.rmtree(instance.workdir, ignore_errors=True)
        if front_before is not None:
            gui_input._activate_pid(front_before[1])


@pytest.fixture()
def session(chrome: LiveChrome) -> Tuple[CUA, Snapshot]:
    cua = CUA()
    return cua, cua.capture(chrome.pid, "Google Chrome", SNAPSHOT_LIMITS)


def index_of(snapshot: Snapshot, role: str, needle: str) -> int:
    for element in snapshot["elements"]:
        if element["role"] == role and needle in element["label"].lower():
            return element["index"]
    raise LookupError(f"{role} {needle!r} anlık görüntüde yok: {[e['label'] for e in snapshot['elements']]}")


def window_title(cua: CUA, chrome: LiveChrome) -> str:
    return ax_text(ax_read(cua._stored[chrome.pid].window, "AXTitle"))


def wait_for_title(cua: CUA, chrome: LiveChrome, needle: str) -> bool:
    """Başlık güncellemesi asenkron gelir: kısa süre yoklanır."""
    deadline = time.time() + 2.0
    while time.time() < deadline:
        if needle in window_title(cua, chrome):
            return True
        time.sleep(0.05)
    return False


def press_count(chrome: LiveChrome) -> int:
    """Sayfanın 'sayaç: N' metnini AYRI bir CUA ile okur (asıl testin liste kimliklerini bozmaz)."""
    snapshot = CUA().capture(chrome.pid, "Google Chrome", SNAPSHOT_LIMITS)
    for element in snapshot["elements"]:
        if element["role"] == "AXStaticText" and element["label"].startswith("sayaç:"):
            return int(element["label"].split(":")[1])
    raise LookupError("sayaç metni anlık görüntüde yok")


@pytest.fixture()
def background_app(chrome: LiveChrome) -> Iterator[subprocess.Popen]:
    """Başka bir uygulamayı öne alır: Chrome arka planda (görünür) kalır."""
    helper = subprocess.Popen([sys.executable, "-c", CORNER_WINDOW_SCRIPT])
    deadline = time.time() + 15
    while time.time() < deadline:
        owner = gui_input._front_app_owner()
        if owner is not None and owner[1] == helper.pid:
            break
        time.sleep(0.2)
    else:
        helper.terminate()
        pytest.skip("Yardımcı ön plan uygulaması öne gelemedi")
    yield helper
    helper.terminate()
    helper.wait()


def require_foreground(chrome: LiveChrome) -> None:
    deadline = time.time() + 30
    while time.time() < deadline:
        if gui_input._activate_pid(chrome.pid):
            return
        time.sleep(1.0)
    pytest.skip("Chrome ön plana alınamadı (başka bir süreç odağı tutuyor)")


def test_snapshot_lists_elements_and_never_reads_the_password_value(session: Tuple[CUA, Snapshot]) -> None:
    _cua, snapshot = session
    labels = {(e["role"], e["label"]) for e in snapshot["elements"]}
    assert {("AXButton", "Bana bas"), ("AXTextField", "Ad alanı"), ("AXCheckBox", "Onay kutusu")} <= labels
    assert snapshot["stats"]["web_ready"] is True and not snapshot["truncated"]
    password: Element = snapshot["elements"][index_of(snapshot, "AXTextField", "parola") - 1]
    assert password["secure"] is True and password["value"] == ""
    disabled: Element = snapshot["elements"][index_of(snapshot, "AXButton", "pasif") - 1]
    assert disabled["enabled"] is False
    assert all("gizli123" not in str(element) for element in snapshot["elements"])


def test_press_works_in_the_background_and_reports_the_layer(
    chrome: LiveChrome, session: Tuple[CUA, Snapshot], background_app: subprocess.Popen,
) -> None:
    cua, snapshot = session
    front_before = gui_input._front_app_owner()
    assert front_before is not None and front_before[1] == background_app.pid
    count = press_count(chrome)
    message = cua.click_snapshot_element(snapshot["id"], index_of(snapshot, "AXButton", "bana bas"))
    assert "katman: ax_press" in message and "etki doğrulandı" in message
    assert press_count(chrome) == count + 1
    assert gui_input._front_app_owner() == front_before  # arka plan eylemi odak çalmadı
    assert "öğe değeri değişti" in cua.click_snapshot_element(snapshot["id"], index_of(snapshot, "AXCheckBox", "onay"))


def test_set_text_verifies_by_readback_and_never_echoes_secret_fields(chrome: LiveChrome, session: Tuple[CUA, Snapshot]) -> None:
    cua, snapshot = session
    message = cua.set_snapshot_text(snapshot["id"], index_of(snapshot, "AXTextField", "ad alan"), "Değer ğüşİ")
    assert "katman: ax_value" in message and "geri okunup doğrulandı" in message
    assert wait_for_title(cua, chrome, "yazildi:Değer ğüşİ")
    secret = "cok-gizli-metin-987"
    secure_message = cua.set_snapshot_text(snapshot["id"], index_of(snapshot, "AXTextField", "parola"), secret)
    assert "doğrulanamaz" in secure_message and secret not in secure_message


def test_foreground_click_restores_cursor_and_verifies_the_effect(chrome: LiveChrome, session: Tuple[CUA, Snapshot]) -> None:
    cua, snapshot = session
    require_foreground(chrome)
    cursor_before = gui_input._capture_input_state().cursor
    count = press_count(chrome)
    target: ResolvedElement = cua.resolve_element(snapshot["id"], index_of(snapshot, "AXButton", "bana bas"))
    message = cua._foreground_click(target, "[test]", "AXPress atlandı", time.monotonic())
    assert "katman: fg_click" in message and "etki doğrulandı" in message
    assert gui_input._capture_input_state().cursor == pytest.approx(cursor_before, abs=1.5)
    assert press_count(chrome) == count + 1


def test_foreground_typing_replaces_text_only_after_focus_is_confirmed(chrome: LiveChrome, session: Tuple[CUA, Snapshot]) -> None:
    cua, snapshot = session
    require_foreground(chrome)
    field = index_of(snapshot, "AXTextField", "ad alan")
    cua.set_snapshot_text(snapshot["id"], field, "eski değer")
    target = cua.resolve_element(snapshot["id"], field)
    outcome, note = cua._foreground_type(target, "[test]", "yeni ğüşİ değer", False)
    assert outcome == "verified" and note
    assert wait_for_title(cua, chrome, "yazildi:yeni ğüşİ değer")


def test_click_is_refused_when_another_window_covers_the_target(chrome: LiveChrome, session: Tuple[CUA, Snapshot]) -> None:
    cua, snapshot = session
    require_foreground(chrome)
    target = cua.resolve_element(snapshot["id"], index_of(snapshot, "AXButton", "bana bas"))
    x, y = frame_center(target.live["frame"])
    helper = subprocess.Popen([sys.executable, "-c", FLOATING_WINDOW_SCRIPT, str(round(x)), str(round(y))])
    try:
        deadline = time.time() + 10
        while time.time() < deadline and gui_input._hit_matches(target.ref, x, y):
            time.sleep(0.2)
        if gui_input._hit_matches(target.ref, x, y):
            pytest.skip("Yüzen örtü penceresi oluşturulamadı")
        count = press_count(chrome)
        with pytest.raises(ToolError) as blocked:
            cua._foreground_click(target, "[test]", "AXPress atlandı", time.monotonic())
        assert blocked.value.code == "ELEMENT_OBSCURED"
        assert press_count(chrome) == count  # yanlış öğeye/hedefe tıklanmadı
    finally:
        helper.terminate()
        helper.wait()


class GateSession:
    """Gerçek Toolbox + gerçek CUA ile geçici Chrome'a bağlı ödeme kapısı oturumu; tıklama kanıtı yerel sunucudur."""

    def __init__(self, chrome: LiveChrome, hits: HitServer) -> None:
        self.chrome: LiveChrome = chrome
        self.hits: HitServer = hits
        self.toolbox: Toolbox = Toolbox()
        self.snapshot: Snapshot = self.toolbox.cua.capture(chrome.pid, "Google Chrome", SNAPSHOT_LIMITS)
        self.questions: List[str] = []

    def index(self, label: str) -> int:
        return index_of(self.snapshot, "AXButton", label.lower())

    def click_count(self, name: str) -> int:
        return self.hits.settled(0.5).count(name)

    async def click(self, index: int, answer: Optional[Any], unattended: bool) -> main.ToolResult:
        runtime = IntegrationRuntime(lambda event: None, lambda: False, answer)
        runtime.unattended = unattended
        token = CURRENT_RUNTIME.set(runtime)
        try:
            call: main.ToolCallDraft = {"id": "e1", "name": "cua_click_element", "arguments": json.dumps(
                {"snapshot": self.snapshot["id"], "index": index})}
            return await main.execute_tool(call, self.toolbox, {}, lambda event: None, lambda: False)
        finally:
            CURRENT_RUNTIME.reset(token)

    async def confirm(self, title: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        self.questions.append(str(fields["_help"]))
        return {approval.APPROVAL_FIELD: True}

    async def deny(self, title: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        return {approval.APPROVAL_FIELD: False}


@pytest.fixture()
def gate(chrome: LiveChrome, hit_server: HitServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> GateSession:
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))  # denetim kaydı geçici dizine yazılır
    return GateSession(chrome, hit_server)


def audit_decisions(tmp_path: Path) -> List[str]:
    path = tmp_path / "audit.jsonl"
    return [json.loads(line)["decision"] for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


@pytest.mark.asyncio
async def test_live_payment_button_is_never_clicked_without_an_approval(
    gate: GateSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Onaysız/ret/zaman aşımı/kanal yok/sürekli mod: 'Pay now' düğmesi hiç tetiklenmez; sıradan düğme onaysız çalışır."""
    ordinary = await gate.click(gate.index("Ayrıntıları göster"), None, False)
    assert ordinary["ok"] and gate.click_count("ayrinti") == 1 and not gate.questions
    pay = gate.index("Pay now")

    async def never_answers(title: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        await asyncio.sleep(30)
        return {approval.APPROVAL_FIELD: True}

    assert (await gate.click(pay, None, False))["code"] == "APPROVAL_UNAVAILABLE"           # kanal yok
    assert (await gate.click(pay, gate.confirm, True))["code"] == "APPROVAL_UNAVAILABLE"    # gözetimsiz (sürekli) mod
    assert (await gate.click(pay, gate.deny, False))["code"] == "APPROVAL_DENIED"           # kullanıcı reddetti
    monkeypatch.setattr(approval, "APPROVAL_TIMEOUT_SECONDS", 0.3)
    assert (await gate.click(pay, never_answers, False))["code"] == "APPROVAL_TIMEOUT"      # yanıt gelmedi
    assert gate.click_count("pay") == 0 and gate.hits.hits == ["ayrinti"]
    assert audit_decisions(tmp_path) == ["unavailable", "unavailable", "denied", "timeout"]


@pytest.mark.asyncio
async def test_live_approved_payment_click_fires_exactly_once(gate: GateSession, tmp_path: Path) -> None:
    result = await gate.click(gate.index("Pay now"), gate.confirm, False)
    assert result["ok"] and "katman: ax_press" in result["result"] and "tek kez tetiklendi" in result["result"]
    assert gate.click_count("pay") == 1
    assert "'Pay now'" in gate.questions[0] and "Toplam: 249,90 TL" in gate.questions[0]
    assert audit_decisions(tmp_path) == ["approved"]


@pytest.mark.asyncio
async def test_live_unverifiable_approved_click_is_never_fired_twice(gate: GateSession) -> None:
    """Etkisi doğrulanamayan onaylı tıklamada ikinci tetikleme (ön plan tıklaması) yok: her onay tam 1 istek."""
    button = gate.index("Complete purchase")
    outcomes: List[str] = []
    for attempt in range(1, 4):  # 1. tıklamada odak değişir (etki görülür); sonrakilerde görülebilir hiçbir şey değişmez
        result = await gate.click(button, gate.confirm, False)
        assert result["ok"], result
        outcomes.append(result["result"])
        assert gate.click_count("sessiz") == attempt, (attempt, outcomes)
    assert "etki doğrulanamadı; sonraki gözlemi kontrol et" in outcomes[-1]
    assert "fg_click" not in " ".join(outcomes)


@pytest.mark.asyncio
async def test_live_target_renamed_during_approval_is_not_clicked(gate: GateSession) -> None:
    """Onay beklenirken düğme adı değişirse (gerçek sayfa değişimi) tıklama yapılmaz."""
    buy = gate.index("Buy now")
    swap = gate.index("Etiketi değiştir")

    async def swap_then_confirm(title: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        other = CUA()  # ayrı depo: asıl oturumun liste kimliklerini bozmaz
        snapshot = other.capture(gate.chrome.pid, "Google Chrome", SNAPSHOT_LIMITS)
        other.click_snapshot_element(snapshot["id"], index_of(snapshot, "AXButton", "etiketi değiştir"))
        return {approval.APPROVAL_FIELD: True}

    assert swap != buy
    result = await gate.click(buy, swap_then_confirm, False)
    assert result["code"] in ("STALE_ELEMENT", "TARGET_CHANGED_AFTER_APPROVAL"), result
    assert gate.click_count("pay2") == 0


@pytest.mark.asyncio
async def test_live_unlabeled_button_is_completed_by_roi_ocr(gate: GateSession, tmp_path: Path) -> None:
    """Erişilebilirlik adı olmayan ama ekranda 'Ödemeyi onayla' yazan düğme ROI OCR ile tanınır ve onay ister."""
    unlabeled = [element["index"] for element in gate.snapshot["elements"]
                 if element["role"] == "AXButton" and not element["label"] and not element["description"]]
    assert len(unlabeled) == 1
    assert (await gate.click(unlabeled[0], None, False))["code"] == "APPROVAL_UNAVAILABLE"
    assert gate.click_count("ocr") == 0
    result = await gate.click(unlabeled[0], gate.confirm, False)
    assert result["ok"] and gate.click_count("ocr") == 1
    assert "Ödemeyi onayla" in gate.questions[0]
    assert audit_decisions(tmp_path) == ["unavailable", "approved"]


@pytest.mark.asyncio
async def test_live_human_check_checkbox_is_refused_even_when_the_user_would_approve(gate: GateSession, tmp_path: Path) -> None:
    """'I'm not a robot' onay kutusu (gerçek AX öğesi): kullanıcı onaylayacak olsa bile sorulmaz ve tıklanmaz."""
    checkbox = index_of(gate.snapshot, "AXCheckBox", "not a robot")
    for unattended in (False, True):
        result = await gate.click(checkbox, gate.confirm, unattended)
        assert result["code"] == "BOT_WALL_DETECTED" and result["recoverable"] is False, result
        assert "BOT DOĞRULAMASI/ERİŞİM ENGELİ" in result["error"]
    assert gate.click_count("robot") == 0 and gate.questions == [] and audit_decisions(tmp_path) == []


def test_stale_list_and_renamed_element_are_refused(chrome: LiveChrome, session: Tuple[CUA, Snapshot]) -> None:
    """Sayfayı kalıcı değiştirir (düğme adı): bu yüzden dosyadaki SON test."""
    cua, snapshot = session
    old_id = snapshot["id"]
    button = index_of(snapshot, "AXButton", "bana bas")
    cua.click_snapshot_element(old_id, index_of(snapshot, "AXButton", "adı değiştir"))
    with pytest.raises(ToolError) as renamed:
        cua.click_snapshot_element(old_id, button)
    assert renamed.value.code == "STALE_ELEMENT" and "adı değişti" in str(renamed.value)
    fresh = cua.capture(chrome.pid, "Google Chrome", SNAPSHOT_LIMITS)
    with pytest.raises(ToolError) as old:
        cua.click_snapshot_element(old_id, button)
    assert old.value.code == "STALE_ELEMENT" and fresh["id"] in str(old.value)
    with pytest.raises(ToolError) as bad_index:
        cua.click_snapshot_element(fresh["id"], 999)
    assert bad_index.value.code == "INVALID_ELEMENT"
    with pytest.raises(ToolError) as disabled:
        cua.click_snapshot_element(fresh["id"], index_of(fresh, "AXButton", "pasif"))
    assert disabled.value.code == "ELEMENT_DISABLED"


class NativeApp:
    """Kendi yerel test uygulamamız: süreç ve uygulama düzeyi başlık okuyucusu."""

    def __init__(self, process: subprocess.Popen) -> None:
        self.process: subprocess.Popen = process
        self.pid: int = process.pid


@pytest.fixture()
def native() -> Iterator[NativeApp]:
    if not AX.AXIsProcessTrusted():
        pytest.skip("Erişilebilirlik izni yok")
    process = subprocess.Popen([sys.executable, "-c", NATIVE_APP_SCRIPT])
    deadline = time.time() + 20
    while time.time() < deadline:
        owner = gui_input._front_app_owner()
        if owner is not None and owner[1] == process.pid:
            break
        time.sleep(0.2)
    else:
        process.terminate()
        pytest.skip("Yerel test uygulaması öne gelemedi")
    time.sleep(0.5)
    yield NativeApp(process)
    process.terminate()
    process.wait()


def native_title(cua: CUA, app: NativeApp) -> str:
    return ax_text(ax_read(cua._stored[app.pid].window, "AXTitle"))


def test_native_controls_work_in_the_background_and_secure_fields_are_marked(
    native: NativeApp, background_app: subprocess.Popen,
) -> None:
    cua = CUA()
    snapshot = cua.capture(native.pid, "yerel", SNAPSHOT_LIMITS)
    assert snapshot["stats"]["web_ready"] is None  # Chromium değil: web beklemesi yok
    front_before = gui_input._front_app_owner()
    assert "katman: ax_press" in cua.click_snapshot_element(snapshot["id"], index_of(snapshot, "AXButton", "tamam"))
    assert native_title(cua, native) == "basildi:1"
    assert "öğe değeri değişti" in cua.click_snapshot_element(snapshot["id"], index_of(snapshot, "AXCheckBox", "yerel onay"))
    text = cua.set_snapshot_text(snapshot["id"], index_of(snapshot, "AXTextField", "yerel alan"), "yerel ğüşİ")
    assert "katman: ax_value" in text and native_title(cua, native) == "alan:yerel ğüşİ"
    secure = next(element for element in snapshot["elements"] if element["secure"])
    assert secure["label"] == "Yerel parola" and secure["value"] == ""
    assert gui_input._front_app_owner() == front_before  # hiçbiri odak çalmadı


def test_element_without_press_action_escalates_to_a_verified_foreground_click(
    native: NativeApp, background_app: subprocess.Popen,
) -> None:
    cua = CUA()
    snapshot = cua.capture(native.pid, "yerel", SNAPSHOT_LIMITS)
    custom = index_of(snapshot, "AXButton", "özel")
    assert "AXPress" not in snapshot["elements"][custom - 1]["actions"]
    message = cua.click_snapshot_element(snapshot["id"], custom)
    assert "AXPress eylemini desteklemiyor" in message and "katman: fg_click" in message
    assert native_title(cua, native) == "ozel:1"
    assert "önceki ön plan uygulaması" in message  # arka plandaki uygulama geri yüklendi
    owner = gui_input._front_app_owner()
    assert owner is not None and owner[1] == background_app.pid



@pytest.fixture()
def pay_native() -> Iterator[NativeApp]:
    if not AX.AXIsProcessTrusted():
        pytest.skip("Erişilebilirlik izni yok")
    process = subprocess.Popen([sys.executable, "-c", PAY_APP_SCRIPT])
    deadline = time.time() + 20
    while time.time() < deadline:
        owner = gui_input._front_app_owner()
        if owner is not None and owner[1] == process.pid:
            break
        time.sleep(0.2)
    else:
        process.terminate()
        pytest.skip("Ödeme test uygulaması öne gelemedi")
    time.sleep(0.5)
    yield NativeApp(process)
    process.terminate()
    process.wait()


@pytest.mark.asyncio
async def test_live_native_payment_button_without_press_action_gets_one_foreground_click(
    pay_native: NativeApp, background_app: subprocess.Popen, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AXPress'i olmayan özel düğme (adlı ve adsız+OCR): onaysız tıklanmaz; onayla TEK ön plan tıklaması, önceki uygulama geri gelir."""
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    toolbox = Toolbox()
    snapshot = toolbox.cua.capture(pay_native.pid, "yerel", SNAPSHOT_LIMITS)
    labeled = index_of(snapshot, "AXButton", "pay now")
    unlabeled = next(element["index"] for element in snapshot["elements"]
                     if element["role"] == "AXButton" and not element["label"] and element["subrole"] == "")
    assert "AXPress" not in snapshot["elements"][labeled - 1]["actions"]
    questions: List[str] = []

    async def confirm(title: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        questions.append(str(fields["_help"]))
        return {approval.APPROVAL_FIELD: True}

    async def click(index: int, answer: Optional[Any]) -> main.ToolResult:
        runtime = IntegrationRuntime(lambda event: None, lambda: False, answer)
        token = CURRENT_RUNTIME.set(runtime)
        try:
            call: main.ToolCallDraft = {"id": "n1", "name": "cua_click_element", "arguments": json.dumps(
                {"snapshot": snapshot["id"], "index": index})}
            return await main.execute_tool(call, toolbox, {}, lambda event: None, lambda: False)
        finally:
            CURRENT_RUNTIME.reset(token)

    def title() -> str:
        return ax_text(ax_read(toolbox.cua._stored[pay_native.pid].window, "AXTitle"))

    for index in (labeled, unlabeled):
        assert (await click(index, None))["code"] == "APPROVAL_UNAVAILABLE"
    assert title() == "odeme-baslangic"  # onaysız hiçbir düğme tıklanmadı
    first = await click(labeled, confirm)
    assert first["ok"] and "katman: fg_click" in first["result"] and "tek kez tetiklendi" in first["result"], first
    assert title() == "odeme:1"
    second = await click(unlabeled, confirm)  # adsız düğme: etiket ROI OCR ile okunur
    assert second["ok"] and title() == "odeme:2", second
    assert "Pay now" in questions[0] and "Ödemeyi onayla" in questions[1]
    owner = gui_input._front_app_owner()
    assert owner is not None and owner[1] == background_app.pid  # önceki ön plan uygulaması geri yüklendi


# --- Ön plan koruması (klavye girdisi): gerçek AX klavye odağı ve kendi test uygulamalarımız ---
# Gerçek tuş olayı GÖNDERİLMEZ: type/press işlevleri kayda yamalıdır; sınanan şey korumanın gerçek AX okumasıyla verdiği karardır.

# Ana iş parçacığı uykudaki (yanıtsız) test uygulaması: sistem geneli AX sorgusu ön planı okuyamaz
HUNG_APP_SCRIPT: str = """
import AppKit, time
app = AppKit.NSApplication.sharedApplication()
app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)
window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
    AppKit.NSMakeRect(80, 300, 300, 160), AppKit.NSWindowStyleMaskTitled, AppKit.NSBackingStoreBuffered, False)
window.setTitle_("yanitsiz-uygulama")
window.makeKeyAndOrderFront_(None)
app.activateIgnoringOtherApps_(True)
for _ in range(20):
    AppKit.NSRunLoop.currentRunLoop().runUntilDate_(AppKit.NSDate.dateWithTimeIntervalSinceNow_(0.05))
time.sleep(60)
"""


def wait_ax_front(pid: int, seconds: float) -> bool:
    """AX klavye odağı pid'e geçene kadar yoklar; geçici okuma hataları (yanıtsız ön plan) süre dolana kadar yok sayılır."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            if foreground.read_front_app()["pid"] == pid:
                return True
        except ToolError as error:
            if error.code != "FOREGROUND_UNKNOWN":
                raise
        time.sleep(0.05)
    return False


def launch_services_front_pid() -> Optional[int]:
    """AX'ten bağımsız ön plan bilgisi (lsappinfo): yanıtsız uygulamanın gerçekten önde olduğunu doğrulamak için."""
    asn = subprocess.run(["lsappinfo", "front"], capture_output=True, text=True).stdout.strip()
    info = subprocess.run(["lsappinfo", "info", "-only", "pid", asn], capture_output=True, text=True).stdout
    found = re.search(r'"pid"=(\d+)', info)
    return int(found.group(1)) if found else None


def test_live_front_app_reader_follows_the_real_keyboard_focus(native: NativeApp) -> None:
    if not wait_ax_front(native.pid, 15):
        pytest.skip("Yerel test uygulaması klavye odağına geçemedi (başka bir süreç odağı tutuyor)")
    front = foreground.read_front_app()
    assert front["pid"] == native.pid and front["name"] != "" and front["bundle_id"] != ""
    helper = subprocess.Popen([sys.executable, "-c", CORNER_WINDOW_SCRIPT])
    try:
        if not wait_ax_front(helper.pid, 15):
            pytest.skip("İkinci test uygulaması klavye odağına geçemedi")
        assert foreground.read_front_app()["pid"] == helper.pid  # odak değişimi ilk okumada yansır
        assert gui_input._activate_pid(native.pid) and foreground.read_front_app()["pid"] == native.pid
        started = time.perf_counter()
        for _ in range(50):
            foreground.read_front_app()
        assert (time.perf_counter() - started) / 50 < 0.05  # ölçülen ~0,15 ms; klavye eylemi başına bütçe 50 ms'nin altında
    finally:
        helper.terminate()
        helper.wait()


def test_live_keyboard_guard_decides_from_the_real_front_app(native: NativeApp, monkeypatch: pytest.MonkeyPatch) -> None:
    if not wait_ax_front(native.pid, 15):
        pytest.skip("Yerel test uygulaması klavye odağına geçemedi (başka bir süreç odağı tutuyor)")
    events: List[Tuple[str, str]] = []
    monkeypatch.setattr(tools, "type_unicode_text", lambda value: events.append(("type", value)))
    monkeypatch.setattr(tools, "press_key_spec", lambda key: events.append(("key", key)) or "basıldı")
    box = Toolbox()
    box.cua_type_text("a")  # hedef seçilmemiş ve ön plan hassas değil: geçer
    assert events == [("type", "a")]
    # Test uygulamasını "ajanı çalıştıran uygulama" ilan eden yapılandırma: hedef seçilmemişken hassas ön plan reddedilir
    with monkeypatch.context() as declared_host:
        declared_host.setattr(foreground, "host_identity", lambda: {"pid": native.pid, "bundle_id": ""})
        started = time.perf_counter()
        with pytest.raises(ToolError) as sensitive:
            box.cua_type_text("gizli")
        assert sensitive.value.code == "FOREGROUND_MISMATCH" and "OmniAgent arayüzü" in str(sensitive.value)
        assert time.perf_counter() - started < 3.0  # en çok 1,5 sn yoklama
        with pytest.raises(ToolError) as in_sequence:
            box.run_action_sequence([{"action": "type", "text": "gizli"}])
        assert in_sequence.value.code == "FOREGROUND_MISMATCH" and in_sequence.value.completed_steps == 0
        assert events == [("type", "a")]  # reddedilen yazımlar hiçbir olay göndermedi
        # cmd+tab hassas ön plandan çıkış yoludur: engellenmez (tuş olayı kayda yamalı, gerçek uygulama değiştirici tetiklenmez)
        box.cua_press_key("cmd+tab")
        assert events == [("type", "a"), ("key", "cmd+tab")]
    # Hedef seçilmiş: ön plan o uygulama olmalı (yerel ad 'Python')
    events.clear()
    box = Toolbox()
    box._input_app = "Notes"
    with pytest.raises(ToolError) as wrong:
        box.cua_press_key("enter")
    assert wrong.value.code == "FOREGROUND_MISMATCH" and "Python" in str(wrong.value) and "Notes" in str(wrong.value)
    assert events == []
    box._input_app = "Python"
    box.cua_press_key("enter")
    assert events == [("key", "enter")]


def test_live_unresponsive_front_app_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    if not AX.AXIsProcessTrusted():
        pytest.skip("Erişilebilirlik izni yok")
    events: List[Tuple[str, str]] = []
    monkeypatch.setattr(tools, "type_unicode_text", lambda value: events.append(("type", value)))
    monkeypatch.setattr(tools, "press_key_spec", lambda key: events.append(("key", key)) or "basıldı")
    hung = subprocess.Popen([sys.executable, "-c", HUNG_APP_SCRIPT])
    try:
        deadline = time.time() + 15
        while time.time() < deadline and launch_services_front_pid() != hung.pid:
            time.sleep(0.2)
        time.sleep(1.5)  # uygulama koşu döngüsünü bitirip uykuya geçsin
        if launch_services_front_pid() != hung.pid:
            pytest.skip("Yanıtsız test uygulaması öne gelemedi (başka bir süreç odağı tutuyor)")
        started = time.perf_counter()
        with pytest.raises(ToolError) as unreadable:
            Toolbox().cua_type_text("gizli")
        assert unreadable.value.code == "FOREGROUND_UNKNOWN" and unreadable.value.recoverable
        assert "Klavye olayı gönderilmedi" in str(unreadable.value) and events == []
        assert time.perf_counter() - started < 3.0
    finally:
        hung.terminate()
        hung.wait()
