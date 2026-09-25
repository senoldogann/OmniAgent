"""Tek makinede UI ve Telegram görevlerinin ekran/klavye eylemlerini sıraya koyar."""
import errno
import fcntl
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from omniagent.integrations.runtime import data_root


class HostBusyError(RuntimeError):
    """Başka bir OmniAgent görevi bilgisayarı kullanıyor."""


@contextmanager
def host_task_lock(path: Optional[Path] = None) -> Iterator[None]:
    """Beklemeden süreçler arası kilit alır; görev bitince her durumda bırakır."""
    target = path or data_root() / "host-task.lock"
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(target, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                raise HostBusyError("Başka bir OmniAgent görevi çalışıyor; bitince yeniden deneyin.") from error
            raise
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)
