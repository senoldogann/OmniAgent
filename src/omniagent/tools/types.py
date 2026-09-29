"""
OmniAgent Araç Tipleri ve Sabitleri (tools/types.py)
Tüm alt modüller tarafından paylaşılan katı tipleme yapıları, istisnalar ve limit sabitleri.
"""
from contextvars import ContextVar
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, NotRequired, Optional, Tuple, TypedDict

from omniagent.approval import ApprovalRequest

# Model ekran uzayı ve görüntü sınırları
MODEL_SCREEN_SIZE: int = 1000
SCREENSHOT_MAX_EDGE: int = 1920

# Çıktı ve bellek limitleri (her bayt token demektir)
SHELL_STDOUT_LIMIT: int = 4000
SHELL_STDERR_LIMIT: int = 1000
FILE_READ_LIMIT: int = 8000
FILE_READ_MAX_BYTES: int = 8 * 1024 * 1024
STREAM_STDOUT_MAX_BYTES: int = 64 * 1024
STREAM_STDERR_MAX_BYTES: int = 16 * 1024
STREAM_READ_CHARS: int = 4096
# Canlı komut çıktısının en geç yayılma aralığı
STREAM_EMIT_INTERVAL_SECONDS: float = 0.1
TYPED_TEXT_ECHO_LIMIT: int = 80
# Telegram Bot API sendDocument üst sınırı
DELIVERY_MAX_BYTES: int = 50 * 1024 * 1024
BACKUP_KEEP_PER_FILE: int = 5

PAGE_TEXT_LIMIT: int = 5000
PAGE_ELEMENT_LIMIT: int = 40
PAGE_LOAD_TIMEOUT_MS: int = 20000
PAGE_ACTION_TIMEOUT_MS: int = 3000

# AX Erişilebilirlik limitleri
AX_ELEMENT_LIMIT: int = 80
AX_NODE_LIMIT: int = 3000
AX_SCAN_BUDGET_SECONDS: float = 2.0
AX_MESSAGING_TIMEOUT_SECONDS: float = 1.0
AX_LABEL_SEARCH_NODES: int = 12

# Öğe tabanlı AX anlık görüntüsü (tools/ax_snapshot.py): modele giden liste ve tarama bütçesi
SNAPSHOT_ELEMENT_LIMIT: int = 150
SNAPSHOT_NODE_LIMIT: int = 6000
SNAPSHOT_TIME_BUDGET_SECONDS: float = 1.5
SNAPSHOT_STATIC_TEXT_LIMIT: int = 25
SNAPSHOT_STATIC_TEXT_MIN_CHARS: int = 3
SNAPSHOT_LABEL_LIMIT: int = 60
SNAPSHOT_VALUE_LIMIT: int = 40
SNAPSHOT_LABEL_HINT_LIMIT: int = 25
SNAPSHOT_ROW_TOLERANCE_POINTS: float = 10.0
SNAPSHOT_TEXT_AREA_MAX_CHARS: int = 2000
# Görünür kısmı bu kenardan (nokta) küçük öğe listelenmez: tıklanamayacak kadar kırpılmıştır
SNAPSHOT_MIN_VISIBLE_EDGE: float = 4.0
# Öğenin yeniden okunan çerçevesi, anlık görüntüdeki görünür çerçeveyle en az bu oranda örtüşmeli
SNAPSHOT_FRAME_OVERLAP_MIN: float = 0.5
# Eylem turu sonrası otomatik gözlemdeki AX özeti daha küçüktür (token ekonomisi): yalnız etkileşimli öğeler
OBSERVATION_ELEMENT_LIMIT: int = 60
# Host doğrulamasının (gönderim koruması) etiketini çözebildiği en son liste sayısı (bkz. ax_snapshot.remember_snapshot_labels)
SNAPSHOT_LABEL_REGISTRY_LIMIT: int = 32
# Chromium/Electron uygulamasında web erişilebilirliği açıldıktan sonra AXWebArea'nın belirmesini bekleme sınırı
AX_WEB_READY_SECONDS: float = 4.0
# Önceki yakalamada web ağacı hazır değildiyse (pencere örtülü olabilir) sonraki yakalamalar yalnız bu kadar bekler
AX_WEB_RETRY_SECONDS: float = 0.5
AX_WEB_POLL_SECONDS: float = 0.15

# Eylem sonrası doğrulama yoklaması ve ön plan eskalasyonu
EFFECT_TIMEOUT_SECONDS: float = 0.3
EFFECT_POLL_SECONDS: float = 0.04
EFFECT_SCREEN_EDGE: int = 320
TEXT_READBACK_TIMEOUT_SECONDS: float = 0.3
# Hedefin AXFocused'u True olduktan sonra uygulama düzeyi odağın hedefe geçmesini bekleme sınırı (ilk tuşun kaybolmaması için)
FOCUS_SETTLE_SECONDS: float = 0.2
# Tümünü seç (AXSelectedTextRange) Chrome'da asenkron uygulanır: seçimin oturmasını bekleme sınırı
SELECT_ALL_TIMEOUT_SECONDS: float = 0.3
ACTIVATION_TIMEOUT_SECONDS: float = 1.0
ACTIVATION_POLL_SECONDS: float = 0.02
# Etkinleştirmenin kendi yol açtığı değişimler (başlık çubuğu, odak) tıklama öncesi taban çizgisine girsin diye bekleme sınırı
ACTIVATION_SETTLE_SECONDS: float = 0.3
HIT_TEST_ANCESTOR_DEPTH: int = 8
CLICK_EVENT_GAP_SECONDS: float = 0.03

# Unicode ve klavye
UNICODE_CHUNK_UNITS: int = 16
UNICODE_CHUNK_DELAY_SECONDS: float = 0.005
MAX_WAIT_SECONDS: float = 5.0

# Fare: çoklu tıklama ve sürükleme zamanlaması
MOUSE_MULTI_CLICK_GAP_SECONDS: float = 0.03
MOUSE_DRAG_HOLD_SECONDS: float = 0.12
MOUSE_DRAG_STEPS: int = 12
MOUSE_DRAG_STEP_SECONDS: float = 0.015
# pyautogui her genel çağrıdan (tıklama, taşıma, tuş) sonra bu kadar uyur (kütüphane varsayılanı 0,1 sn); olaylar işletim
# sisteminde sıralı işlenir ve eylemin ekrandaki etkisini durulma beklemesi karşılar. Köşe acil durdurması (FAILSAFE) açık kalır.
INPUT_PAUSE_SECONDS: float = 0.03
# Tıklamanın hemen ardından klavye girdisi gelen bileşik araçlarda (alan doldurma/gönderme) alanın odak alması için bekleme:
# PAUSE düşürülünce tıklama-yazma aralığı eski >=0,1 sn düzeyinde kalır (odakta yeniden bağlanan girdiler ilk tuşları yutmasın).
FIELD_FOCUS_WAIT_SECONDS: float = 0.08

# Ekran durulma (settle) tespiti sabitleri
SETTLE_FRAME_EDGE: int = 160
SETTLE_PIXEL_DELTA: int = 4
SETTLE_CHANGED_RATIO: float = 0.002
SETTLE_POLL_SECONDS: float = 0.03
SETTLE_REACTION_SECONDS: float = 1.0
SETTLE_QUIET_SECONDS: float = 0.45
SETTLE_MAX_SECONDS: float = 3.0
# Gecikmeli metin yükleyen sayfada OCR, ilk iskelet değişiminden hemen sonra eski metni okumamalı.
OCR_AFTER_INPUT_MIN_SECONDS: float = 0.9
# Uyuyan ekranın kullanıcı etkinliği bildirimiyle açılmasını bekleme sınırı
DISPLAY_WAKE_SECONDS: float = 2.0

# Chrome sekme betiği (AppleScript). Yükleme beklemesi duvar saatiyle sınırlıdır (AppleScript `current date` 1 sn
# çözünürlüklü: sınır tamsayı saniyedir). Süreç zaman aşımı betiğin kendi bekleme sınırının ÜSTÜNDE olmalıdır: eskiden
# betik 80×0,1 sn (en az 8 sn) beklerken süreç 3 sn'de öldürülüyordu ve betik gezinmeyi bekleme döngüsünden ÖNCE
# yaptığı için yedek yol çift sekme açıyordu.
CHROME_APP_NAME: str = "Google Chrome"
CHROME_LOAD_WAIT_SECONDS: int = 5
# Apple olayı gecikmesi + activate + pencere/sekme açma + soğuk Chrome açılışı için bekleme sınırının üstündeki pay
CHROME_SCRIPT_HEADROOM_SECONDS: float = 8.0
CHROME_SCRIPT_TIMEOUT_SECONDS: float = CHROME_LOAD_WAIT_SECONDS + CHROME_SCRIPT_HEADROOM_SECONDS

# cua_get_app: osascript etkinleştirme süreç sınırı (soğuk uygulama açılışı dahil)
CUA_ACTIVATE_TIMEOUT_SECONDS: float = 8.0

# Ön plan (foreground) denetimi (tools/foreground.py): klavye girdisinden önce ve uygulama etkinleştirmeden sonra yoklama
FOREGROUND_POLL_SECONDS: float = 0.05
INPUT_FOREGROUND_WAIT_SECONDS: float = 1.5
APP_ACTIVATION_WAIT_SECONDS: float = 5.0

# Kaydırma (scroll) sabitleri
SCROLL_CHUNK_POINTS: float = 60.0
SCROLL_CHUNK_DELAY_SECONDS: float = 0.008
SCROLL_MAX_EVENTS: int = 20
SCROLL_SETTLE_QUIET_SECONDS: float = 0.25
SCROLL_SETTLE_MAX_SECONDS: float = 1.5
SCROLL_DIFF_EDGE: int = 480
SCROLL_PIXEL_DELTA: int = 12
SCROLL_MOVED_RATIO: float = 0.01

# Baştan sona okuma (read_scrollable) sabitleri
READ_MAX_PAGES: int = 15
READ_TEXT_LIMIT: int = 12000
READ_FIRST_STEP_SHARE: float = 0.25
READ_STEP_SHARE: float = 0.8
READ_GAP_MARKER: str = "[… olası atlama …]"
READ_EDGE_UNITS: float = 6.0
READ_TOP_ATTEMPTS: int = 4
# Arka plan OCR işçisi sayısı. Vision istekleri eşzamanlı güvenlidir (çıktı sıralı çalıştırmayla birebir aynı) ama ölçümde
# ikinci işçi çıkarım hızını artırmadı (istekler sıraya girer); işçi OCR'ı kaydırma/durulma ile üst üste bindirmeye yeter.
READ_OCR_WORKERS: int = 1
# Boşluk kararı (kaydırma adımını yarılama) OCR sonucu beklenmeden en çok bu kadar sayfa gecikmeli verilir; bu sayede
# OCR sonraki kaydırma/durulma sürerken koşar. 0: her sayfanın sonucu beklenir (eski sıralı karar sırası).
READ_OCR_LAG_PAGES: int = 1
# Toplanan sayfa sayısı bu değere ulaşınca (tepe + probe + ilk tam adım) karar OCR sonucu beklenerek verilir: kaydırma miktarı
# bir uygulamada sayfa yüksekliğini aşıyorsa boşluk ve adım yarılama ilk tam adımda eski sıralı okumadaki gibi hemen görülür.
READ_SYNC_DECISION_PAGES: int = 3
TEXT_CANDIDATE_LIMIT: int = 8
# Yaklaşık hedef noktasından uzak OCR eşleşmesi tıklanmaz; önce yerel kırpma yeniden okunur.
TEXT_NEAR_MAX_DISTANCE: int = 180
TEXT_FOCUS_RADIUS: int = 190
# cua_click_point / run_action_sequence click / cua_submit_text: tıklama noktasının çevresindeki etiketi okuyan ROI
# yarıçapı ve noktayı "etikete ait" sayan tolerans (0-1000 model uzayı birimi; canlı ölçümle ayarlanır)
POINT_LABEL_RADIUS_X: int = 200
POINT_LABEL_RADIUS_Y: int = 80
POINT_LABEL_TOLERANCE_X: int = 80
POINT_LABEL_TOLERANCE_Y: int = 30
# Etiketsiz öğenin çerçevesi kadar okunan ROI'ye eklenen pay ve en küçük yarıçap (0-1000 birimi)
ELEMENT_LABEL_MARGIN: int = 8
ELEMENT_LABEL_MIN_RADIUS: int = 20

# Süreç ve kabuk sabitleri
# Komut serbestliği: ajan uzun kurulum/derleme/indirmeleri tek çağrıda bitirebilsin. Kullanıcı
# kararı gereği kabuk komutları engellenmez; yalnız süre üst sınırı vardır (1 saat).
SHELL_TIMEOUT_SECONDS: float = 60.0
SHELL_MAX_TIMEOUT_SECONDS: int = 3600
TIMEOUT_OUTPUT_TAIL: int = 1500
JS_TIMEOUT_SECONDS: float = 20.0
FETCH_ERROR_BODY_LIMIT: int = 800
HISTORY_RESULT_LIMIT: int = 8
PROCESS_POLL_SECONDS: float = 0.05

# macOS Güvenlik ve Gizlilik Tercih URL'i
SCREEN_SETTINGS_URL: str = "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"
ACCESSIBILITY_SETTINGS_URL: str = "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"


class ToolError(Exception):
    """
    Ajanın kendi kendini iyileştirmesi için yapılandırılmış hata sınıfı.

    `recoverable` her raise yerinde zorunlu olarak verilir: "bu hatadan sonra aynı yol/yöntem yeniden
    denenebilir mi". TODO(recoverable): alan bugün src/ içinde hiç OKUNMUYOR; yalnız ToolResult yüküne ve
    tool_finished olayına taşınır, modele giden araç mesajına girmez (bkz. app/tool_execution._tool_result_to_message:
    yalnız "HATA <error_type>: <error>" kurar). Sıkı moddaki "bu engeli aşmaya çalışma" kuralı bu yüzden
    `recoverable`a değil `code`a ve metin işaretine dayanır (bot_wall.ACCESS_CHALLENGE_MARKER; app/answer_fidelity,
    app/partial_report). Karar bekliyor: ya alan bir kurala bağlanmalı (ör. kurtarılamaz hatada yeniden deneme /
    alternatif yol önerisini bastırmak) ve metin işareti bağımlılığı kalkmalı, ya da ToolResult yükünden
    çıkarılmalı. Parametrenin kendisini silmek ucuz DEĞİLDİR: her raise yerinde mecburen veriliyor.
    """
    def __init__(self, message: str, code: str, recoverable: bool, completed_steps: int = 0) -> None:
        super().__init__(message)
        self.code: str = code
        self.recoverable: bool = recoverable
        self.completed_steps: int = completed_steps


class ApprovalRefused(ToolError):
    """
    Host onay kapısı hedefi tıklatmadı: ret, zaman aşımı, onay kanalı yok, onay sonrası hedefin değişmesi ya da
    durdurma. Yedek yollara (şablon tıklama vb.) düşen ToolError yakalayıcıları bunu yeniden yükseltmelidir:
    reddedilen ödeme adımı başka bir katmandan dolanılmasın.
    """


class ToolRuntime(TypedDict):
    """
    Çağrı başına araç bağlamı: canlı çıktı hedefi, kullanıcı durdurma denetimi, host'un bu çağrı için
    kullanıcıdan açık onay alıp almadığı ve host onay kapısı. Onay kapısını execute_tool kurar: araç
    hedefi ÇÖZÜMLEDİKTEN SONRA, eylemden ÖNCE çağırır; onay yoksa/reddedilirse ToolError yükselir.
    request_approval_blocking eşzamanlı (asyncio.to_thread) araçlar, request_approval olay döngüsündeki
    (async) araçlar içindir; blocking sürüm olay döngüsü iş parçacığından ÇAĞRILMAZ (açık hata verir).
    Bağlam yoksa (birim test, betik) kapı yoktur.
    """
    emit_output: Callable[[str], None]
    should_stop: Callable[[], bool]
    approved: NotRequired[bool]
    request_approval: NotRequired[Callable[[ApprovalRequest], Awaitable[None]]]
    request_approval_blocking: NotRequired[Callable[[ApprovalRequest], None]]


TOOL_RUNTIME: ContextVar[Optional[ToolRuntime]] = ContextVar("TOOL_RUNTIME", default=None)


class ScreenGeometry(TypedDict):
    """Retina ekran pikseli ve normalize model koordinatı arasındaki dönüşüm geometrisi."""
    point_width: int
    point_height: int
    model_width: int
    model_height: int
    origin_x: NotRequired[int]
    origin_y: NotRequired[int]


class ScreenSession(TypedDict):
    """Konsol oturumunun kilit durumu ve ana ekranın uyku durumu (izin gerektirmez)."""
    locked: bool
    on_console: bool
    asleep: bool


class ActionStep(TypedDict, total=False):
    """run_action_sequence içindeki tek bir eylem adımı."""
    action: str
    point: List[int]
    to: List[int]
    button: str
    clicks: int
    text: str
    near: Optional[List[int]]
    max_pages: int
    key: str
    seconds: float


class AXElement(TypedDict):
    """macOS Accessibility ağacından ayıklanan etkileşimli GUI öğesi."""
    index: int
    role: str
    title: str
    description: str
    value: str
    center: List[int]
    enabled: bool
    actions: List[str]


class BrowserAction(TypedDict, total=False):
    """Playwright tarayıcı eylem adımı."""
    action: str
    selector: str
    text: str
    timeout_ms: int


class PendingInput(TypedDict):
    """Kullanıcıdan arayüz yoluyla metin bekleme durumu."""
    prompt: str
    submitted: bool
    value: str


def clip_text(text: str, limit: int) -> str:
    """Metni belirtilen uzunlukta kırpıp kısaltma bilgisini ekler. Saf fonksiyon."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[kısaltıldı, toplam {len(text)} karakter]"
