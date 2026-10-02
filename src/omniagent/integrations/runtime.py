"""Entegrasyonlar için iptal, kullanıcı etkileşimi ve süre ölçümü."""
import asyncio
import json
import os
import tempfile
import time
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, Optional, Tuple, TypeVar, TypedDict

if TYPE_CHECKING:
    from omniagent.app.types import AutonomyGuards

from omniagent.approval import APPROVAL_TIMEOUT_SECONDS
from omniagent.core.events import EventSink
from omniagent.paths import data_root

T = TypeVar("T")
AnswerSink = Callable[[str, Dict[str, Any]], Awaitable[Dict[str, Any]]]
# ask() süre sınırı verilmişse alanlara bu üst veri (saniye) eklenir: arayüz son saati gösterir ve
# süre dolunca pencereyi kapatır. '_' ile başlayan alanlar yanıt alanı değildir (Telegram da yok sayar).
INPUT_TIMEOUT_FIELD: str = "_timeout_seconds"
# Dosyayı kullanıcının kanalına (Telegram sohbeti) teslim eder: (yol, başlık)
DeliverSink = Callable[[Path, str], Awaitable[None]]


class IntegrationMetrics(TypedDict):
    discovery_seconds: float
    install_seconds: float
    network_seconds: float
    wait_seconds: float
    user_wait_seconds: float
    network_requests: int
    operations_ok: int
    operations_failed: int


class InteractionRequired(Exception):
    """Kullanıcı etkileşimi olmayan çalıştırmalarda eksik önkoşulu belirtir."""


class IntegrationStopped(Exception):
    """Esc sonrasında yeni dış eylem başlatılmasını engeller."""


class DeliveryFailed(Exception):
    """Kullanıcı kanalına dosya teslimi başarısız oldu (ağ, boyut, kanal hatası)."""


def read_json(path: Path, default: Any) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def save_json(path: Path, value: Any) -> None:
    """Hesap yapılandırmasını ve günlükleri atomik, yalnız kullanıcıya açık saklar."""
    path.parent.mkdir(parents=True, exist_ok=True)
    name: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as target:
            name = target.name
            json.dump(value, target, ensure_ascii=False, indent=2)
            target.flush()
            os.fsync(target.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def boolean_field(spec: object) -> bool:
    """Soru alanı onay kutusu mu? Telegram ve iMessage bu alanı evet/hayır metniyle yanıtlatır. Saf."""
    return isinstance(spec, dict) and spec.get("type") == "boolean"


class IntegrationRuntime:
    """Dış araçların görev kapsamındaki iptal ve UI bağlantısı."""
    def __init__(self, emit: EventSink, should_stop: Callable[[], bool],
                 answer: Optional[AnswerSink] = None, deliver: Optional[DeliverSink] = None) -> None:
        self.emit = emit
        self.should_stop = should_stop
        self.answer = answer
        self.deliver = deliver
        self.metrics: IntegrationMetrics = {
            "discovery_seconds": 0.0, "install_seconds": 0.0, "network_seconds": 0.0,
            "wait_seconds": 0.0, "user_wait_seconds": 0.0, "network_requests": 0,
            "operations_ok": 0, "operations_failed": 0,
        }
        self.discovery_remaining = 8.0
        self.install_attempts: set[str] = set()
        # Erişim/bakiye hatası (401/402/403, insufficient_quota) alan profiller görev boyu engellenir.
        self.blocked_backends: set[str] = set()
        # Hız sınırına (429) takılan profil -> serinleme bitişi (time.monotonic); süre dolunca profil yeniden denenir.
        self.backend_cooldowns: Dict[str, float] = {}
        # Bu turdaki model çağrısının yeniden denemeden vazgeçeceği an (time.monotonic). Agent her turda atar;
        # None ise (agent dışı çağrı: CLI, test) etkileşimli bütçe kullanılır.
        self.model_retry_until: Optional[float] = None
        # Yedek sağlayıcı izni (bkz. omniagent.fallback_policy): görev başında run_agent_with_callback atar.
        # Varsayılanlar KAPALI'dır; doğrudan kurulan runtime hiçbir yedeğe izin vermez.
        self.primary_backend: Optional[str] = None
        self.fallback_backends: frozenset[str] = frozenset()
        self.fallback_images: bool = False
        # Bu görevde ProviderFallback ile bildirilip denetim kaydına yazılan (hedef profil, görüntülü mü) çiftleri:
        # aynı çift görev boyunca bir kez duyurulur, yeni görüntü seviyesi (metinden ekran görüntüsüne) yeni kayıt üretir.
        self.announced_fallbacks: set[Tuple[str, bool]] = set()
        self.published: Dict[str, Any] = {}
        self.selected: Dict[str, Any] = {}
        self.allowed_tools: Optional[frozenset[str]] = None
        # Authenticated quick-read scope owns synchronous workers until stopped.
        self.cancellable_reads = False
        # Etkileşimli modun bekleme sınırı; nonblocking mod kullanıcı girdisini bağımlılığa dönüştürür.
        self.user_input_timeout: Optional[float] = APPROVAL_TIMEOUT_SECONDS
        self.unattended: bool = False
        self.nonblocking: bool = False
        self.autonomy: Optional["AutonomyGuards"] = None
        self.deferred_questions: set[str] = set()
        self.gui_draft_text: str = ""
        self.denied_confirmation_questions: set[str] = set()
        self.denied_approval_requests: set[tuple[str, str]] = set()

    def check(self) -> None:
        if self.should_stop():
            raise IntegrationStopped("Kullanıcı tarafından durduruldu.")

    def status(self, stage: str, text: str, completed: int = 0, total: int = 0) -> None:
        self.emit({"kind": "integration_status", "stage": stage, "text": text,
                   "completed": completed, "total": total})

    async def wait(self, operation: Awaitable[T], timeout: Optional[float] = None) -> T:
        """Tamamlanmayı bekler; 50 ms iptal denetimi işin bitişine gecikme eklemez."""
        task = asyncio.ensure_future(operation)
        start = time.monotonic()
        try:
            while not task.done():
                self.check()
                remaining = None if timeout is None else timeout - (time.monotonic() - start)
                if remaining is not None and remaining <= 0:
                    raise TimeoutError("İşlem zaman sınırına ulaştı.")
                await asyncio.wait({task}, timeout=min(0.05, remaining) if remaining is not None else 0.05)
            self.check()
            return task.result()
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def delay(self, seconds: float) -> None:
        start = time.monotonic()
        try:
            await self.wait(asyncio.sleep(max(0.0, seconds)))
        finally:
            self.metrics["wait_seconds"] += time.monotonic() - start

    async def ask(
        self, title: str, fields: Dict[str, Any], timeout: Optional[float], *, allow_unattended: bool = False,
    ) -> Dict[str, Any]:
        """
        Kullanıcıdan arayüz/Telegram üzerinden yanıt bekler; bekleme süresi görev bütçesinden
        düşülür. timeout verilirse süre dolunca TimeoutError yükselir (onay/soru görevi
        sonsuza dek kilitlemesin); None kurulum akışlarında sınırsız bekler.
        """
        if self.nonblocking:
            raise InteractionRequired(title)
        if self.answer is None or (self.unattended and not allow_unattended):
            raise InteractionRequired(title)
        if self.autonomy is not None and self.autonomy["quiet_now"]():
            if title not in self.autonomy["deferred_approvals"]:
                self.autonomy["deferred_approvals"].append(title)
            raise TimeoutError("Sessiz saatlerde otonom işin sorusu sabah raporuna bırakıldı.")
        start = time.monotonic()
        self.status("waiting_user", title)
        shown: Dict[str, Any] = fields if timeout is None else {**fields, INPUT_TIMEOUT_FIELD: timeout}
        try:
            return await self.wait(self.answer(title, shown), timeout=timeout)
        finally:
            self.metrics["user_wait_seconds"] += time.monotonic() - start
            self.status("resumed", "Göreve devam ediliyor")


CURRENT_RUNTIME: ContextVar[Optional[IntegrationRuntime]] = ContextVar("integration_runtime", default=None)
CURRENT_SERVICE: ContextVar[Optional[Any]] = ContextVar("integration_service", default=None)
