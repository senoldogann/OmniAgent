"""Actual event activity, private snapshots and optional indicator lifecycle."""
import asyncio
import json
import os

import pytest

from omniagent.core.activity import ActivityController, ActivityStore, RenewalIndicator, stage_for_event


def test_event_mapping_has_no_prose_authority():
    assert stage_for_event({'kind': 'text_delta', 'text': 'I am searching'}) is None
    assert stage_for_event({'kind': 'tool_started', 'name': 'web_search'}) == 'researching'
    assert stage_for_event({'kind': 'tool_started', 'name': 'list_directory'}) == 'reading'
    assert stage_for_event({'kind': 'integration_status', 'stage': 'waiting_user'}) == 'waiting-user'
    assert stage_for_event({'kind': 'integration_status', 'stage': 'resumed'}) == 'preparing'
    assert stage_for_event({'kind': 'run_finished'}) == 'terminal'


def test_private_bounded_stale_dead_and_symlink_snapshots(tmp_path):
    clock = [100.0]
    store = ActivityStore(str(tmp_path / 'selected.json'), now=lambda: clock[0], alive=lambda pid: pid == os.getpid())
    run = store.start('desktop', 'secret token=abcdefghi\n' + 'x' * 1000)
    loaded = store.load()
    assert len(loaded) == 1 and len(loaded[0]['subject']) <= 160
    assert 'abcdefghi' not in loaded[0]['subject']
    assert store.directory.stat().st_mode & 0o777 == 0o700
    path = store.directory / (run['run_id'] + '.json')
    assert path.stat().st_mode & 0o777 == 0o600
    clock[0] = 131
    assert store.load()[0]['stage'] == 'interrupted'
    clock[0] = 100
    data = json.loads(path.read_text()); data['owner_pid'] = 999999
    path.write_text(json.dumps(data))
    assert store.load()[0]['stage'] == 'interrupted'
    path.unlink(); path.symlink_to(tmp_path / 'outside')
    assert store.load() == []


@pytest.mark.asyncio
async def test_ownership_overlap_and_heartbeat_cleanup(tmp_path):
    tick, sleeping = asyncio.Queue(), asyncio.Queue()
    async def clock(delay):
        sleeping.put_nowait(delay)
        await tick.get()
    controller = ActivityController(str(tmp_path / 's.json'), sleep=clock)
    first = controller.start('telegram', 'first')
    second = controller.start('desktop', 'second')
    first.event({'kind': 'tool_started', 'name': 'read_file'})
    assert await sleeping.get() == 5.0
    assert await sleeping.get() == 5.0
    tick.put_nowait(None)
    assert await sleeping.get() == 5.0
    await first.close()
    assert controller.active_ids == {second.run_id}
    assert [row['stage'] for row in controller.store.load() if row['run_id'] == second.run_id] == ['preparing']
    await first.close()
    await second.close()
    assert not controller.active_ids
    assert all(row['stage'] == 'terminal' for row in controller.store.load())


@pytest.mark.asyncio
async def test_renewal_ttl_pause_resume_optional_error_and_join(caplog):
    sent, tick, delays = asyncio.Queue(), asyncio.Queue(), []
    count = 0
    async def send():
        nonlocal count
        count += 1
        sent.put_nowait(count)
        if count == 1:
            raise RuntimeError('private credentials')
    async def clock(delay):
        delays.append(delay)
        await tick.get()
    indicator = RenewalIndicator(send, sleep=clock)
    await indicator.update('a', 'preparing')
    assert await sent.get() == 1
    tick.put_nowait(None)
    assert await sent.get() == 2
    assert delays == [4.0, 4.0]  # Telegram typing lifetime is five seconds.
    await indicator.update('a', 'waiting-user')
    assert not indicator.running and sent.empty()
    await indicator.update('b', 'researching')
    assert await sent.get() == 3
    await indicator.update('a', 'terminal')
    assert indicator.running
    await indicator.update('b', 'terminal')
    assert not indicator.running
    await indicator.close(); await indicator.close()
    assert sent.empty()
    assert 'private credentials' not in caplog.text and 'RuntimeError' in str(caplog.records[0].error_type)


@pytest.mark.asyncio
async def test_repeated_cancellation_joins_indicator_send_before_return():
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def send():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
    indicator = RenewalIndicator(send)
    await indicator.update('owned', 'composing')
    await entered.wait()
    shutdown = asyncio.create_task(indicator.close())
    await cleaning.wait()
    shutdown.cancel(); await asyncio.sleep(0); shutdown.cancel(); await asyncio.sleep(0)
    assert not shutdown.done()
    second = asyncio.create_task(indicator.close())
    assert not second.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await shutdown
    await second
    assert not indicator.running


def test_full_snapshot_store_keeps_live_owners(tmp_path, monkeypatch):
    from omniagent.core import activity
    monkeypatch.setattr(activity, 'MAX_SNAPSHOTS', 2)
    store = ActivityStore(str(tmp_path / 's.json'))
    a, b = store.start('desktop', 'a'), store.start('telegram', 'b')
    with pytest.raises(ValueError, match='capacity'):
        store.start('imessage', 'c')
    assert {row['run_id'] for row in store.load()} == {a['run_id'], b['run_id']}
    a['stage'] = 'terminal'; store.save(a)
    c = store.start('imessage', 'c')
    assert {row['run_id'] for row in store.load()} == {b['run_id'], c['run_id']}


@pytest.mark.asyncio
async def test_owned_adapter_cleanup_finishes_all_steps_after_repeated_cancel(tmp_path):
    from omniagent.core.activity import run_owned
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    session = ActivityController(str(tmp_path / 'cleanup.json')).start('telegram', 'actual job')
    async def send():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
    indicator = RenewalIndicator(send)
    async def lifecycle():
        await indicator.update(session.run_id, 'preparing')
        try:
            await asyncio.Event().wait()
        finally:
            await indicator.update(session.run_id, 'terminal')
            await session.close()
    task = asyncio.create_task(run_owned(lifecycle()))
    await entered.wait(); task.cancel(); await cleaning.wait()
    task.cancel(); await asyncio.sleep(0)
    assert not task.done() and not session.closed
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert session.closed and session.heartbeat.done() and not indicator.running
    await indicator.close()
    await indicator.update('replacement-after-shutdown', 'preparing')
    assert not indicator.running


def test_strict_snapshot_schema_private_bounds_restart_and_selected_state(tmp_path):
    store = ActivityStore(str(tmp_path / 'selected.json'), alive=lambda pid: False)
    row = store.start('desktop', 'real work')
    path = store.directory / (row['run_id'] + '.json')
    assert store.load()[0]['stage'] == 'interrupted'
    assert ActivityStore(str(tmp_path / 'other.json')).load() == []
    os.chmod(path, 0o644); assert store.load() == []
    os.chmod(path, 0o600)
    for field, invalid in [('owner_pid', 2**512), ('stage', 'invented'), ('subject', 'x' * 161),
                           ('last_update', float('nan')), ('origin', 'external')]:
        path.write_text(json.dumps({**row, field: invalid}))
        assert store.load() == []
    path.write_text(json.dumps({**row, 'queue': ['future effect']})); assert store.load() == []
    path.write_text('[' * 1500); assert store.load() == []
    path.write_text('x' * 2049); assert store.load() == []
    path.write_text(json.dumps(row)); renamed = path.with_name('wrong-uuid.json'); path.rename(renamed)
    assert store.load() == []
    renamed.unlink(); path.write_text(json.dumps({**row, 'last_update': store.now() + 60}))
    os.chmod(path, 0o600)
    assert store.load()[0]['stage'] == 'interrupted'
    os.chmod(store.directory, 0o755); assert store.load() == []


def test_snapshot_admission_serializes_two_independent_stores_at_capacity(tmp_path, monkeypatch):
    import concurrent.futures
    import threading
    from omniagent.core import activity
    state = str(tmp_path / 'concurrent.json')
    seed = ActivityStore(state)
    for _ in range(activity.MAX_SNAPSHOTS - 1):
        seed.start('desktop', 'still live')
    first_at_publication, release_first = threading.Event(), threading.Event()
    real_mkstemp = activity.tempfile.mkstemp
    def held_publication(*args, **kwargs):
        if threading.current_thread().name.startswith('first'):
            first_at_publication.set()
            assert release_first.wait(2)
        return real_mkstemp(*args, **kwargs)
    monkeypatch.setattr(activity.tempfile, 'mkstemp', held_publication)
    def admit():
        try:
            ActivityStore(state).start('telegram', 'new live')
            return 'admitted'
        except (OSError, ValueError):
            return 'unavailable'
    with concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix='first') as a:
        with concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix='second') as b:
            first = a.submit(admit)
            assert first_at_publication.wait(2)
            second = b.submit(admit)
            second.result(timeout=2)
            release_first.set()
            first.result(timeout=2)
    assert len(list(seed.directory.glob('*.json'))) <= activity.MAX_SNAPSHOTS
    assert len(seed.load()) == activity.MAX_SNAPSHOTS
    # A terminal owner makes room again without evicting any live owner.
    terminal = seed.load()[0]; terminal['stage'] = 'terminal'; seed.save(terminal)
    seed.start('imessage', 'after capacity')
    assert len(seed.load()) == activity.MAX_SNAPSHOTS


def _process_snapshot_admit(state, ready, release, results):
    ready.put(True)
    release.wait(5)
    try:
        ActivityStore(state).start('telegram', 'process observation')
        results.put('admitted')
    except (OSError, ValueError):
        results.put('unavailable')


def test_snapshot_capacity_serializes_actual_process_writers(tmp_path):
    import multiprocessing
    from omniagent.core import activity
    state = str(tmp_path / 'processes.json')
    store = ActivityStore(state)
    for _ in range(activity.MAX_SNAPSHOTS - 1):
        store.start('desktop', 'live parent observation')
    context = multiprocessing.get_context('spawn')
    ready, results, release = context.Queue(), context.Queue(), context.Event()
    writers = [context.Process(target=_process_snapshot_admit, args=(state, ready, release, results)) for _ in range(2)]
    try:
        for writer in writers:
            writer.start()
        ready.get(timeout=5); ready.get(timeout=5)
        release.set()
        outcomes = [results.get(timeout=5), results.get(timeout=5)]
        for writer in writers:
            writer.join(5)
            assert writer.exitcode == 0
        assert sorted(outcomes) == ['admitted', 'unavailable']
        assert len(store.load()) == activity.MAX_SNAPSHOTS
    finally:
        for writer in writers:
            if writer.is_alive():
                writer.terminate()
                writer.join(5)
        ready.close(); results.close()


def test_snapshot_overflow_is_unavailable_and_recovers_only_from_terminal_rows(tmp_path, monkeypatch):
    from omniagent.core import activity
    store = ActivityStore(str(tmp_path / 'recovery.json'))
    monkeypatch.setattr(activity, 'MAX_SNAPSHOTS', 3)
    rows = [store.start('desktop', f'owner {index}') for index in range(3)]
    monkeypatch.setattr(activity, 'MAX_SNAPSHOTS', 2)
    assert store.load() == []
    assert 'kullanılamıyor' in store.status()
    with pytest.raises(ValueError):
        store.start('imessage', 'no free capacity')
    for row in rows[:2]:
        row['stage'] = 'terminal'; store.save(row)
    new = store.start('imessage', 'new observation')
    assert {row['run_id'] for row in store.load()} == {rows[2]['run_id'], new['run_id']}
