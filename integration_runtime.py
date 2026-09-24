"""Entegrasyonlar için iptal, kullanıcı etkileşimi ve süre ölçümü."""
import asyncio
import json
import os
import tempfile
import time
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional, TypeVar, TypedDict

from events import EventSink

T = TypeVar("T")
AnswerSink = Callable[[str, Dict[str, Any]], Awaitable[Dict[str, Any]]]


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


def data_root() -> Path:
    return Path(os.environ.get("OMNI_DATA_DIR", str(Path.home() / "Library/Application Support/OmniAgent")))


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


class IntegrationRuntime:
    """Dış araçların görev kapsamındaki iptal ve UI bağlantısı."""
    def __init__(self, emit: EventSink, should_stop: Callable[[], bool],
                 answer: Optional[AnswerSink] = None) -> None:
        self.emit = emit
        self.should_stop = should_stop
        self.answer = answer
        self.metrics: IntegrationMetrics = {
            "discovery_seconds": 0.0, "install_seconds": 0.0, "network_seconds": 0.0,
            "wait_seconds": 0.0, "user_wait_seconds": 0.0, "network_requests": 0,
            "operations_ok": 0, "operations_failed": 0,
        }
        self.discovery_remaining = 8.0
        self.install_attempts: set[str] = set()
        self.blocked_backends: set[str] = set()
        self.published: Dict[str, Any] = {}
        self.selected: Dict[str, Any] = {}
        self.allowed_tools: Optional[frozenset[str]] = None

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

    async def ask(self, title: str, fields: Dict[str, Any], timeout: Optional[float]) -> Dict[str, Any]:
        """
        Kullanıcıdan arayüz/Telegram üzerinden yanıt bekler; bekleme süresi görev bütçesinden
        düşülür. timeout verilirse süre dolunca TimeoutError yükselir (onay/soru görevi
        sonsuza dek kilitlemesin); None kurulum akışlarında sınırsız bekler.
        """
        if self.answer is None:
            raise InteractionRequired(title)
        start = time.monotonic()
        self.status("waiting_user", title)
        try:
            return await self.wait(self.answer(title, fields), timeout=timeout)
        finally:
            self.metrics["user_wait_seconds"] += time.monotonic() - start
            self.status("resumed", "Göreve devam ediliyor")


CURRENT_RUNTIME: ContextVar[Optional[IntegrationRuntime]] = ContextVar("integration_runtime", default=None)
CURRENT_SERVICE: ContextVar[Optional[Any]] = ContextVar("integration_service", default=None)
