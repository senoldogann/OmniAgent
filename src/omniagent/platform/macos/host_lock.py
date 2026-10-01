"""Cross process host ownership, with cooperative priority for user work."""
from __future__ import annotations

import asyncio
import errno
import fcntl
import json
import os
import time
import threading
import uuid
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import AsyncIterator, Iterator, Optional

from omniagent.paths import data_root

POLL_SECONDS = 0.05


class HostBusyError(RuntimeError):
    """Another OmniAgent task owns the computer."""


def _target(path: Optional[Path]) -> Path:
    return path or data_root() / "host-task.lock"


def _request_path(target: Path) -> Path:
    return target.with_name("host-preempt.request" if target.name == "host-task.lock" else target.name + ".preempt.request")


def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def host_owner(path: Optional[Path] = None) -> Optional[str]:
    """Owner hint for inspection; flock is still the authority for acquisition."""
    owner = _read(_target(path)).get("origin")
    return owner if owner in ("user", "autonomous") else None


def _write_request(target: Path, owner: dict) -> None:
    # Target the particular owner, so a request cannot stop a subsequent autonomous run.
    request = _request_path(target)
    descriptor = os.open(request, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump({"since": time.time(), "owner": owner.get("token")}, output)
        output.flush()
        os.fsync(output.fileno())


def preemption_requested(since: float, path: Optional[Path] = None) -> bool:
    target = _target(path)
    request = _read(_request_path(target))
    owner = _read(target)
    requested = request.get("since")
    return (isinstance(requested, (int, float)) and requested >= since
            and owner.get("origin") == "autonomous" and bool(owner.get("token"))
            and request.get("owner") == owner.get("token"))


def _acquire(target: Path, origin: str, timeout_seconds: float = 0,
             cancelled: Optional[threading.Event] = None) -> int:
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(target, os.O_CREAT | os.O_RDWR, 0o600)
    os.fchmod(descriptor, 0o600)
    deadline = time.monotonic() + max(0, timeout_seconds)
    try:
        while True:
            if cancelled is not None and cancelled.is_set():
                raise HostBusyError("Kilit bekleyişi iptal edildi.")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as error:
                if error.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                    raise
                owner = _read(target)
                if origin != "user" or timeout_seconds <= 0:
                    raise HostBusyError("Başka bir OmniAgent görevi çalışıyor; bitince yeniden deneyin.") from error
                if time.monotonic() >= deadline:
                    raise HostBusyError("Bilgisayar başka bir OmniAgent görevine ayrılmış; kısa bekleme süresi doldu. Çalışan görevin bulunduğu kanaldan durumunu kontrol et; varsa onay sorusunu yanıtla veya o görevi durdur.") from error
                if owner.get("origin") == "autonomous":
                    _write_request(target, owner)
                time.sleep(POLL_SECONDS)
        if cancelled is not None and cancelled.is_set():
            raise HostBusyError("Kilit bekleyişi iptal edildi.")
        os.ftruncate(descriptor, 0)
        os.write(descriptor, json.dumps({"origin": origin, "pid": os.getpid(), "token": uuid.uuid4().hex,
                                        "started_at": time.time()}).encode())
        os.fsync(descriptor)
        if origin == "user":
            _request_path(target).unlink(missing_ok=True)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _release(descriptor: int) -> None:
    try:
        # Clear owner while still holding the lock, avoiding stale user/autonomous metadata.
        os.ftruncate(descriptor, 0)
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


@contextmanager
def host_task_lock(path: Optional[Path] = None) -> Iterator[None]:
    """Existing immediate, nonpreempting user lock (also used for service singleton locks)."""
    descriptor = _acquire(_target(path), "user")
    try:
        yield
    finally:
        _release(descriptor)


@contextmanager
def preemptible_host_task_lock(path: Optional[Path] = None) -> Iterator[None]:
    descriptor = _acquire(_target(path), "autonomous")
    try:
        yield
    finally:
        _release(descriptor)


@contextmanager
def host_task_lock_preempting(timeout_seconds: float = 15.0, path: Optional[Path] = None) -> Iterator[None]:
    descriptor = _acquire(_target(path), "user", timeout_seconds)
    try:
        yield
    finally:
        _release(descriptor)


@asynccontextmanager
async def async_host_task_lock_preempting(timeout_seconds: float = 15.0,
                                         path: Optional[Path] = None) -> AsyncIterator[None]:
    """Wait off the event loop: same process autonomous tasks must be able to notice preemption."""
    cancelled = threading.Event()
    acquiring = asyncio.create_task(asyncio.to_thread(_acquire, _target(path), "user", timeout_seconds, cancelled))
    try:
        descriptor = await asyncio.shield(acquiring)
    except BaseException:
        cancelled.set()
        # The worker cannot be cancelled. Release eventual ownership even if its caller goes away.
        def cleanup(task: asyncio.Task[int]) -> None:
            if not task.cancelled() and task.exception() is None:
                _release(task.result())
        acquiring.add_done_callback(cleanup)
        raise
    try:
        yield
    finally:
        _release(descriptor)
