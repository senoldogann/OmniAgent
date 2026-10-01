"""Host-owned activity observations. Snapshots are status data, never work authority."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import stat
import tempfile
import threading
import time
import uuid
from itertools import islice
from pathlib import Path
from typing import Callable

from omniagent.core.evidence import sanitize_text

STAGES = frozenset({'preparing', 'researching', 'reading', 'acting', 'waiting-user', 'composing', 'terminal'})
LABELS = {'preparing': 'Hazırlanıyor', 'researching': 'Araştırıyor', 'reading': 'Okuyor',
          'acting': 'Çalışıyor', 'waiting-user': 'Yanıtınızı bekliyor', 'composing': 'Yanıtı hazırlıyor',
          'terminal': 'Sona erdi', 'interrupted': 'Kesildi; güncel durum kullanılamıyor'}
LIVE_STAGES = STAGES - {'terminal', 'waiting-user'}
MAX_SNAPSHOTS = 64
MAX_BYTES = 2048
STALE_SECONDS = 30.0


def stage_for_event(event) -> str | None:
    """Only structured actual events carry activity; model prose has no authority."""
    kind = event.get('kind')
    if kind in {'run_started', 'turn_started', 'tool_finished', 'model_finished'}:
        return 'preparing'
    if kind == 'run_finished':
        return 'terminal'
    if kind == 'user_input_required' or (kind == 'notice' and event.get('code') in {'AWAITING_APPROVAL', 'AWAITING_DIRECTION'}):
        return 'waiting-user'
    if kind == 'integration_status':
        stage = event.get('stage')
        return {'waiting_user': 'waiting-user', 'resumed': 'preparing'}.get(stage, stage if stage in STAGES else None)
    if kind == 'tool_started':
        name = event.get('name', '')
        if name in {'web_search', 'fetch_raw', 'browse_url'}:
            return 'researching'
        if name in {'read_file', 'list_directory', 'cua_snapshot', 'cua_get_ax_state', 'cua_read_scrollable'}:
            return 'reading'
        if name == 'ask_user':
            return 'waiting-user'
        return 'acting'
    return None


def status_question(text: str) -> bool:
    flat = ' '.join(text.casefold().strip().rstrip('?.!').split())
    return flat in {'ne yapıyorsun', 'şu an ne yapıyorsun', 'neler yapıyorsun', 'durum nedir', 'what are you doing',
                    'what are you doing now', 'status', '/status', '/durum', 'durum'}


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


async def _join_cleanup(task):
    """Repeated caller cancellation cannot abandon owned cleanup."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    task.result()
    if cancelled:
        raise asyncio.CancelledError


async def run_owned(coroutine, *, on_cancel=None):
    """Cancel an owned lifecycle once and join it despite repeated caller cancellation."""
    task = asyncio.create_task(coroutine)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        if on_cancel is not None:
            on_cancel()
        task.cancel()
        await _join_cleanup(task)
        raise


class ActivityStore:
    def __init__(self, state_file: str, *, now=time.time, alive=_alive):
        self.directory = Path(state_file).absolute().with_name(Path(state_file).name + '.activity')
        self.now, self.alive = now, alive

    def _directory(self, create=False):
        # Reject a symlink at any component, including the selected state parent.
        for path in (self.directory, *self.directory.parents):
            if path.is_symlink():
                raise ValueError('Activity path contains a symlink')
        if create:
            self.directory.mkdir(mode=0o700, exist_ok=True)
        info = self.directory.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('Activity directory is not private')

    def _validate(self, row):
        keys = {'run_id', 'subject', 'origin', 'owner_pid', 'stage', 'last_update'}
        if not isinstance(row, dict) or row.keys() != keys:
            raise ValueError('Invalid activity schema')
        if str(uuid.UUID(row['run_id'])) != row['run_id']:
            raise ValueError('Invalid activity UUID')
        if row['origin'] not in {'desktop', 'telegram', 'imessage', 'autonomous', 'scheduled'} or row['stage'] not in STAGES:
            raise ValueError('Invalid activity stage or origin')
        if type(row['owner_pid']) is not int or not 0 < row['owner_pid'] <= 2**31 - 1:
            raise ValueError('Invalid activity owner')
        if type(row['last_update']) not in (int, float) or not math.isfinite(row['last_update']):
            raise ValueError('Invalid activity clock')
        if not isinstance(row['subject'], str) or len(row['subject']) > 160 or row['subject'] != self.subject(row['subject']):
            raise ValueError('Invalid activity subject')
        return row

    @staticmethod
    def subject(text):
        return ' '.join(sanitize_text(text).split())[:160]

    def start(self, origin, subject):
        row = {'run_id': str(uuid.uuid4()), 'subject': self.subject(subject), 'origin': origin,
               'owner_pid': os.getpid(), 'stage': 'preparing', 'last_update': self.now()}
        self.save(row)
        return row

    def save(self, row):
        self._validate(row)
        self._directory(create=True)
        path = self.directory / (row['run_id'] + '.json')
        if path.is_symlink():
            raise ValueError('Activity snapshot is a symlink')
        # Never evict another live owner to admit a new optional observation.
        files = list(islice(self.directory.glob('*.json'), MAX_SNAPSHOTS + 1))
        if not path.exists() and len(files) >= MAX_SNAPSHOTS:
            removable = [row for row in self.load() if row['stage'] in {'terminal', 'interrupted'}]
            if not removable:
                raise ValueError('Activity snapshot capacity reached')
            oldest = min(removable, key=lambda row: row['last_update'])
            (self.directory / (oldest['run_id'] + '.json')).unlink()
        fd, temporary = tempfile.mkstemp(prefix='.activity-', dir=self.directory)
        try:
            with os.fdopen(fd, 'w') as handle:
                json.dump(row, handle, ensure_ascii=False)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def load(self):
        try:
            self._directory()
            paths = list(islice(self.directory.glob('*.json'), MAX_SNAPSHOTS + 1))
            if len(paths) > MAX_SNAPSHOTS:
                return []
            rows = []
            for path in paths:
                try:
                    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    with os.fdopen(fd) as handle:
                        info = os.fstat(handle.fileno())
                        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > MAX_BYTES:
                            continue
                        row = self._validate(json.loads(handle.read(MAX_BYTES + 1)))
                    if path.name != row['run_id'] + '.json':
                        continue
                    row = dict(row)
                    if row['stage'] != 'terminal' and (self.now() - row['last_update'] > STALE_SECONDS or row['last_update'] > self.now() + 5 or not self.alive(row['owner_pid'])):
                        row['stage'] = 'interrupted'
                    rows.append(row)
                except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
                    continue
            return sorted(rows, key=lambda row: row['last_update'], reverse=True)
        except (OSError, ValueError):
            return []

    def status(self, exclude=None):
        rows = [row for row in self.load() if row['run_id'] != exclude and row['stage'] != 'terminal']
        return ('\n'.join(f"{row['origin']}: {LABELS[row['stage']]} · {row['subject']}" for row in rows)
                or 'Şu anda kayıtlı etkin bir çalışma yok.')


class ActivityController:
    def __init__(self, state_file, *, heartbeat_seconds=5.0, sleep=asyncio.sleep):
        self.store = ActivityStore(state_file)
        self.heartbeat_seconds = heartbeat_seconds
        self.sleep = sleep
        self.active_ids = set()

    def start(self, origin, subject):
        return ActivitySession(self, origin, subject)


class ActivitySession:
    def __init__(self, controller, origin, subject):
        self.controller = controller
        self.row = {'run_id': str(uuid.uuid4()), 'subject': controller.store.subject(subject), 'origin': origin,
                    'owner_pid': os.getpid(), 'stage': 'preparing', 'last_update': time.time()}
        self.run_id = self.row['run_id']
        self.closed = False
        self.lock = threading.RLock()
        controller.active_ids.add(self.run_id)
        self._save()
        self.heartbeat = asyncio.create_task(self._heartbeat())
        self.cleanup = None

    @property
    def stage(self):
        return self.row['stage']

    def _save(self):
        with self.lock:
            self.row['last_update'] = self.controller.store.now()
            try:
                self.controller.store.save(dict(self.row))
            except (OSError, ValueError):
                logging.warning('Activity snapshot unavailable')

    def event(self, event):
        with self.lock:
            if self.closed:
                return
            stage = stage_for_event(event)
            if stage is not None:
                self.row['stage'] = stage
                self._save()

    async def _heartbeat(self):
        while True:
            await self.controller.sleep(self.controller.heartbeat_seconds)
            self._save()

    async def _close(self):
        self.closed = True
        self.heartbeat.cancel()
        await asyncio.gather(self.heartbeat, return_exceptions=True)
        self.row['stage'] = 'terminal'
        self._save()
        self.controller.active_ids.discard(self.run_id)

    async def close(self):
        if self.cleanup is None:
            self.cleanup = asyncio.create_task(self._close())
        await _join_cleanup(self.cleanup)


class RenewalIndicator:
    """One optional renewal loop, aggregated by UUID. Cleanup joins before presentation."""
    def __init__(self, send: Callable, *, interval=4.0, sleep=asyncio.sleep):
        self.send, self.interval, self.sleep = send, interval, sleep
        self.owners = {}
        self.task = None
        self.lock = asyncio.Lock()
        self.cleanup = None
        self.closed = False

    @property
    def running(self):
        return self.task is not None and not self.task.done()

    async def update(self, run_id, stage):
        async with self.lock:
            if self.closed:
                return
            if stage == 'terminal':
                self.owners.pop(run_id, None)
            else:
                self.owners[run_id] = stage
            if any(stage in LIVE_STAGES for stage in self.owners.values()):
                if not self.running:
                    self.task = asyncio.create_task(self._renew())
            else:
                await self._stop()

    async def _renew(self):
        while True:
            try:
                await self.send()
            except Exception as error:
                logging.warning('Optional activity indicator unavailable', extra={
                    'error_type': type(error).__name__, 'status': getattr(error, 'status', None)})
            await self.sleep(self.interval)

    async def _stop(self):
        task = self.task
        if task is not None:
            task.cancel()
            if self.cleanup is None or self.cleanup.done():
                async def cleanup():
                    await asyncio.gather(task, return_exceptions=True)
                self.cleanup = asyncio.create_task(cleanup())
            await _join_cleanup(self.cleanup)
            if self.task is task:
                self.task = None

    async def close(self):
        async with self.lock:
            self.closed = True
            self.owners.clear()
            await self._stop()
