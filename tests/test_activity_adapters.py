"""Authenticated adapters through real coordinator/provider and local tool boundaries."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from omniagent.app import agent, conversation
from omniagent.companion import chat, delegate
from omniagent.core.activity import ActivityController, ActivityStore, LABELS
from omniagent.integrations import telegram, imessage
from omniagent.integrations.imsg import DeliveryUnknown
from tests.test_shared_conversation import call, route, turn, verified
from tests.test_telegram_bridge import FakeAPI
from tests.test_imessage_bridge import parts, incoming, HANDLE, settle


@pytest.fixture
def script(monkeypatch):
    turns, observed = [], []
    async def stream(client, profile, messages, tools, session, emit, stop):
        observed.append((messages, tools))
        item = turns.pop(0)
        if callable(item):
            item = await item(emit, stop)
        emit({'kind': 'text_delta', 'text': item['content']})
        return item
    monkeypatch.setattr(agent, '_stream_completion', stream)
    monkeypatch.setattr(agent, 'load_fallback_policy', lambda: {'backends': [], 'allow_images': False})
    return turns, observed


@pytest.mark.asyncio
@pytest.mark.parametrize('verbose', [False, True])
async def test_telegram_real_ingress_local_read_private_verifier_and_final_join(tmp_path, monkeypatch, script, verbose):
    turns, observed = script
    monkeypatch.setenv('OMNI_DATA_DIR', str(tmp_path))
    state = str(tmp_path / 'chosen.json')
    monkeypatch.setattr(telegram, 'STATE_FILE', state)
    monkeypatch.setattr(telegram, 'create_model_clients', lambda: {'ollama-cloud': object()})
    target = tmp_path / 'real.txt'; target.write_text('EXACT_LOCAL_NAME')
    turns.extend([route('investigate'), turn(calls=[call('read_file', path=str(target))]),
                  turn('EXACT_LOCAL_NAME')])
    api = FakeAPI(); indicator_calls = []
    bridge = telegram.TelegramBridge(api, {'chat_id': 123, 'user_id': 456})
    bridge.backend = 'ollama-cloud'; bridge.verbose = verbose
    async def typing(chat_id):
        indicator_calls.append(chat_id)
    api.send_chat_action = typing
    async def verify(emit, stop):
        rows = ActivityStore(state).load()
        assert rows[0]['stage'] == 'composing'
        assert 'EXACT_LOCAL_NAME' not in '\n'.join(api.sent + api.edited + api.html_sent + api.html_edited)
        return verified()
    turns.append(verify)
    original_send = api.send
    async def send(chat_id, text):
        if 'EXACT_LOCAL_NAME' in text:
            assert not bridge.activity_indicator.running
        return await original_send(chat_id, text)
    api.send = send
    await bridge.handle({'message': {'chat': {'id': 123, 'type': 'private'}, 'from': {'id': 456},
                                    'text': f'Read the file {target}'}})
    await bridge.active
    assert indicator_calls and not bridge.activity_indicator.running
    assert len(observed) == 4
    assert all(row['stage'] == 'terminal' for row in ActivityStore(state).load())
    assert 'EXACT_LOCAL_NAME' in '\n'.join(api.sent + api.edited + api.html_sent + api.html_edited)
    await bridge.integrations.close()


@pytest.mark.asyncio
async def test_imessage_actual_ingress_single_post_commit_ack_and_local_read(parts, tmp_path, monkeypatch, script):
    bridge, transport, store = parts
    state = str(tmp_path / 'chosen.json')
    monkeypatch.setattr(imessage, 'STATE_FILE', state)
    monkeypatch.setattr(delegate, 'STATE_FILE', state)
    monkeypatch.setattr(imessage, 'decide_conversation', conversation.decide_conversation)
    class Client:
        async def close(self):
            pass
    monkeypatch.setattr(delegate, 'create_model_clients', lambda: {'openai': Client()})
    bridge.chat_clients = {'openai': object()}
    target = tmp_path / 'real.txt'; target.write_text('IMESSAGE_LOCAL_IDENTIFIER')
    turns, observed = script
    turns.extend([route('investigate'), turn(calls=[call('read_file', path=str(target))]),
                  turn('IMESSAGE_LOCAL_IDENTIFIER'), verified()])
    original = transport.send_text
    async def send(handle, text):
        if text == chat.TASK_ACK:
            assert bridge.task is not None and bridge.task_activity is not None
        return await original(handle, text)
    transport.send_text = send
    from omniagent.platform.macos.host_lock import host_task_lock
    with host_task_lock():
        await bridge.on_message(incoming(30, f'Read the file {target}', HANDLE))
        await settle(bridge)
    assert transport.texts[0] == chat.TASK_ACK and transport.texts.count(chat.TASK_ACK) == 1
    assert 'IMESSAGE_LOCAL_IDENTIFIER' in '\n'.join(transport.texts[1:])
    assert len(observed) == 4 and not turns
    rows = ActivityStore(state).load()
    assert len(rows) == 1 and rows[0]['stage'] == 'terminal'
    await bridge.close()


@pytest.mark.asyncio
async def test_authenticated_cross_channel_status_reads_other_run_excludes_query(parts, tmp_path, monkeypatch):
    bridge, transport, _ = parts
    state = str(tmp_path / 'selected.json')
    monkeypatch.setattr(imessage, 'STATE_FILE', state)
    desktop = ActivityController(state).start('desktop', 'OpenAI Anthropic models')
    desktop.event({'kind': 'tool_started', 'name': 'web_search'})
    await bridge.on_message(incoming(42, 'ne yapıyorsun?', HANDLE))
    await settle(bridge)
    assert transport.texts == ['desktop: Araştırıyor · OpenAI Anthropic models']
    assert not bridge.task
    await desktop.close(); await bridge.close()


@pytest.mark.asyncio
async def test_unsupported_capability_cached_reconnect_and_no_native_call(parts):
    bridge, transport, _ = parts
    probes, native = [], []
    transport.generation = 1
    async def capabilities():
        probes.append(transport.generation)
        return {'typing_indicators': False, 'v2_ready': False, 'methods': ['typing']}
    async def typing(handle):
        native.append(handle)
    transport.activity_capabilities = capabilities
    transport.send_typing = typing
    assert not await bridge._typing_available()
    assert not await bridge._typing_available()
    await bridge._send_typing()
    assert probes == [1] and not native
    transport.generation = 2
    assert not await bridge._typing_available()
    assert probes == [1, 2]
    await bridge.close()


@pytest.mark.asyncio
async def test_ack_unknown_does_not_cancel_committed_job_or_replay(parts, monkeypatch):
    bridge, transport, _ = parts
    completed = asyncio.Event()
    async def run(goal, options, progress):
        await options["on_execution_ready"]()
        completed.set()
        return delegate.failure_outcome(goal, 'observed failure', '2026-09-30T12:00:00+00:00')
    monkeypatch.setattr(delegate, 'run_task', run)
    attempts = []
    async def send(handle, text):
        attempts.append(text)
        if text == chat.TASK_ACK:
            raise DeliveryUnknown('unknown')
        return {'ok': True, 'rowid': None, 'guid': None}
    transport.send_text = send
    assert await bridge._start_task('committed task', [])
    await completed.wait(); await settle(bridge)
    assert attempts.count(chat.TASK_ACK) == 1
    await bridge.close()


@pytest.mark.asyncio
async def test_desktop_actual_runner_emits_classifier_activity_and_one_verified_final(tmp_path, monkeypatch, script):
    from omniagent.ui import app as ui
    turns, _ = script
    turns.extend([route('chat'), turn('Merhaba. Nasılsın?')])
    monkeypatch.setenv('OMNI_DATA_DIR', str(tmp_path))
    events = []
    host = SimpleNamespace(_post=events.append, _clients={'ollama-cloud': object()})
    report = await ui.OmniUI._run_exclusive(host, 'Merhaba', {
        'state_file': str(tmp_path / 'desktop.json'), 'requested_backend': 'ollama-cloud',
        'history': [], 'should_stop': lambda: False})
    assert events[0]['kind'] == 'integration_status' and events[0]['stage'] == 'preparing'
    assert [e['text'] for e in events if e['kind'] == 'text_delta'] == [report['outcome']]
    assert all(row['stage'] == 'terminal' for row in ActivityStore(host._evidence_state_file).load())
    # The real desktop event ingress reuses its animated bar and common labels.
    view = SimpleNamespace(_activity_verb='', _handle_event_data=lambda event: None)
    ui.OmniUI._handle_event(view, {'kind': 'tool_started', 'name': 'read_file'})
    assert view._activity_verb == LABELS['reading']


@pytest.mark.asyncio
async def test_actual_imessage_free_chat_classifier_and_companion_close_one_owner(parts, tmp_path, monkeypatch, script):
    bridge, transport, _ = parts
    state = str(tmp_path / 'chat.json')
    monkeypatch.setattr(imessage, 'STATE_FILE', state)
    monkeypatch.setattr(imessage, 'decide_conversation', conversation.decide_conversation)
    bridge.chat_clients = {'openai': object()}
    turns, observed = script
    turns.extend([route('chat'), turn('Merhaba.\nNasılsın?')])
    await bridge.on_message(incoming(51, 'Merhaba', HANDLE))
    await settle(bridge)
    assert len(observed) == 2 and not turns
    assert transport.texts == ['Merhaba.\nNasılsın?']
    rows = ActivityStore(state).load()
    assert len(rows) == 1 and rows[0]['stage'] == 'terminal'
    assert chat.TASK_ACK not in transport.texts
    await bridge.close()


@pytest.mark.asyncio
async def test_adapters_unauthorized_and_groups_create_no_activity(parts, tmp_path, monkeypatch):
    bridge, transport, _ = parts
    state = str(tmp_path / 'auth.json')
    monkeypatch.setattr(imessage, 'STATE_FILE', state)
    monkeypatch.setattr(telegram, 'STATE_FILE', state)
    api = FakeAPI()
    tg = telegram.TelegramBridge(api, {'chat_id': 123, 'user_id': 456})
    async def forbidden(*args):
        raise AssertionError('unauthorized indicator/model')
    api.send_chat_action = forbidden
    monkeypatch.setattr(tg, '_refresh_clients', forbidden)
    await tg.handle({'message': {'chat': {'id': 123, 'type': 'group'}, 'from': {'id': 456}, 'text': 'hello'}})
    await tg.handle({'message': {'chat': {'id': 123, 'type': 'private'}, 'from': {'id': 999}, 'text': 'hello'}})
    await bridge.on_message(incoming(60, 'hello', '+905559998877'))
    await bridge.on_message({**incoming(61, 'hello', HANDLE), 'is_group': True})
    await settle(bridge)
    assert ActivityStore(state).load() == []
    assert not api.sent and not transport.texts
    await bridge.close()


@pytest.mark.asyncio
async def test_telegram_actual_approval_pause_resume_and_cancel_final_join(tmp_path, monkeypatch, script):
    from omniagent.integrations.runtime import CURRENT_RUNTIME
    monkeypatch.setenv('OMNI_DATA_DIR', str(tmp_path))
    monkeypatch.setattr(telegram, 'STATE_FILE', str(tmp_path / 'approval.json'))
    monkeypatch.setattr(telegram, 'create_model_clients', lambda: {'ollama-cloud': object()})
    turns, _ = script
    api = FakeAPI(); bridge = telegram.TelegramBridge(api, {'chat_id': 123, 'user_id': 456})
    bridge.backend = 'ollama-cloud'
    typing_entered, typing_cancelled = asyncio.Event(), asyncio.Event()
    async def typing(chat_id):
        typing_entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            typing_cancelled.set()
    api.send_chat_action = typing
    approved = asyncio.Event()
    async def classifier(emit, stop):
        await typing_entered.wait()
        result = await CURRENT_RUNTIME.get().ask('Actual approval', {'approve': {'type': 'boolean'}}, None)
        assert result == {'approve': True}
        approved.set()
        return route('chat')
    composing = asyncio.Event()
    async def model(emit, stop):
        composing.set()
        await asyncio.Event().wait()
    turns.extend([classifier, model])
    await bridge.handle({'message': {'chat': {'id': 123, 'type': 'private'}, 'from': {'id': 456}, 'text': 'Hello'}})
    worker = bridge.active
    await asyncio.wait_for(typing_cancelled.wait(), 1)
    assert not bridge.activity_indicator.running
    bridge.pending_answer.set_result({'approve': True})
    await asyncio.wait_for(approved.wait(), 1)
    await asyncio.wait_for(composing.wait(), 1)
    await bridge.handle({'message': {'chat': {'id': 123, 'type': 'private'}, 'from': {'id': 456}, 'text': '/stop'}})
    worker.cancel()
    await asyncio.sleep(0)
    worker.cancel()
    await asyncio.wait_for(asyncio.gather(worker, return_exceptions=True), 2)
    assert not bridge.activity_indicator.running
    assert all(row['stage'] == 'terminal' for row in ActivityStore(telegram.STATE_FILE).load())
    await bridge.integrations.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['extended', 'continuous'])
async def test_status_question_preserves_explicit_force_task_mode(tmp_path, monkeypatch, mode):
    async def forbidden_model(*args):
        raise AssertionError('forced mode must not classify')
    monkeypatch.setattr(agent, '_stream_completion', forbidden_model)
    options = {'state_file': str(tmp_path / 'mode.json'), 'requested_backend': 'ollama-cloud',
               'should_stop': lambda: False, 'history': [], 'run_mode': mode}
    decision = await conversation.decide_conversation('what are you doing?', lambda event: None, options, {'ollama-cloud': object()})
    assert decision.contract['route'] == 'task' and decision.run_mode == mode


@pytest.mark.asyncio
@pytest.mark.parametrize('channel', ['desktop', 'telegram', 'imessage'])
@pytest.mark.parametrize('example', ['current-models', 'explicit-search', 'directory', 'names-followup', 'failed-research'])
async def test_common_examples_through_actual_adapters(parts, tmp_path, monkeypatch, script, channel, example):
    from omniagent.ui import app as ui
    from omniagent.core.conversation import make_exchange
    bridge, transport, _ = parts
    state = str(tmp_path / 'common.json')
    monkeypatch.setattr(imessage, 'STATE_FILE', state)
    monkeypatch.setattr(delegate, 'STATE_FILE', state)
    monkeypatch.setattr(telegram, 'STATE_FILE', state)
    monkeypatch.setattr(imessage, 'decide_conversation', conversation.decide_conversation)
    class Client:
        async def close(self):
            pass
    clients = {'openai': Client(), 'ollama-cloud': Client()}
    monkeypatch.setattr(delegate, 'create_model_clients', lambda: clients)
    monkeypatch.setattr(telegram, 'create_model_clients', lambda: clients)
    bridge.chat_clients = clients
    turns, requests = script
    history = []
    if example == 'directory':
        directory = tmp_path / 'Requested'; directory.mkdir()
        (directory / 'Middle Name').mkdir(); (directory / 'notes.txt').write_text('sample')
        goal = f'List directory names in {directory}'
        answer = 'Klasör: Middle Name. Dosya: notes.txt.'
        turns.extend([route('investigate', ['directory_names']),
                      turn(calls=[call('list_directory', path=str(directory))]), turn(answer), verified()])
        expected = ['Middle Name', 'notes.txt']
    else:
        goal = ('Search the web for OpenAI and Anthropic model names' if example == 'explicit-search'
                else 'Have OpenAI and Anthropic released new models?')
        if example == 'names-followup':
            previous = 'Have OpenAI and Anthropic released new models?'
            history = [make_exchange(previous, 'Araştırma eksik.', [])]
            # Archive through the authenticated ingress, without triggering a model turn.
            bridge.archive_backlog(incoming(100, previous, HANDLE))
            goal = 'No, give names'
        answer = 'OpenAI: GPT Example. Anthropic: Claude Example. https://example.org/releases\nKaynaklar arama özeti olduğu için eksik; tam sayfalar incelenmedi.'
        def search(self, **kwargs):
            if example == 'failed-research':
                raise OSError('network unavailable')
            return 'OpenAI introduced GPT Example. Anthropic introduced Claude Example. https://example.org/releases'
        monkeypatch.setattr(agent.Toolbox, 'web_search', search)
        turns.extend([route('investigate', ['model_names', 'source_urls']),
                      turn(calls=[call('web_search', query='OpenAI Anthropic latest models')]), turn(answer)])
        if example != 'failed-research':
            turns.append(turn(json.dumps({'ok': True, 'facts': [
                {'field': 'model_names', 'value': 'GPT Example', 'source': 0, 'quote': 'GPT Example'},
                {'field': 'model_names', 'value': 'Claude Example', 'source': 0, 'quote': 'Claude Example'}],
                'missing_fields': [], 'unsupported_claims': []})))
        expected = ['GPT Example', 'Claude Example']
    if channel == 'desktop':
        events = []
        host = SimpleNamespace(_post=events.append, _clients=clients)
        report = await ui.OmniUI._run_exclusive(host, goal, {'state_file': state, 'requested_backend': 'ollama-cloud',
                    'history': history, 'should_stop': lambda: False})
        output = report['outcome']
        subject = report['evidence']['contract']['subject']
        assert len([e for e in events if e['kind'] == 'text_delta']) == 1
    elif channel == 'telegram':
        api = FakeAPI()
        tg = telegram.TelegramBridge(api, {'chat_id': 123, 'user_id': 456})
        tg.backend = 'ollama-cloud'; tg.history = history
        async def typing(chat_id):
            pass
        api.send_chat_action = typing
        await tg.handle({'message': {'chat': {'id': 123, 'type': 'private'}, 'from': {'id': 456}, 'text': goal}})
        await tg.active
        output = '\n'.join(api.sent + api.edited + api.html_sent + api.html_edited)
        subject = tg.history[-1]['goal']
        assert not tg.activity_indicator.running
        await tg.integrations.close()
    else:
        bridge.history = history
        await bridge.on_message(incoming(101, goal, HANDLE))
        await settle(bridge)
        output = '\n'.join(transport.texts)
        subject = bridge.history[-1]['goal']
        assert transport.texts.count(chat.TASK_ACK) == 1
    assert not turns
    if example == 'failed-research':
        assert 'GPT Example' not in output and 'Claude Example' not in output
        assert 'başarısız' in output.casefold() or 'tamamlanamadı' in output.casefold()
    else:
        assert all(value in output for value in expected)
    if example == 'names-followup':
        # Actual classifier history preserves the providers even when adapters archive a new goal.
        assert 'OpenAI' in json.dumps(requests[0][0]) and 'Anthropic' in json.dumps(requests[0][0])
    assert all(row['stage'] == 'terminal' for row in ActivityStore(state).load())
    await bridge.close()


@pytest.mark.asyncio
async def test_installed_imessage_capability_probe_is_readonly_cached_and_invalidated(monkeypatch):
    commands = []
    class Process:
        returncode = 0
        async def communicate(self):
            return b'{"typing_indicators":false,"v2_ready":false,"rpc_methods":["typing"]}', b''
    async def subprocess(*command, **kwargs):
        commands.append(command)
        return Process()
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', subprocess)
    session = imessage.ImsgSession(['/installed/imsg', 'rpc'])
    assert await session.activity_capabilities() == {'typing_indicators': False, 'v2_ready': False}
    await session.activity_capabilities()
    assert commands == [('/installed/imsg', 'status', '--json')]
    session._activity_cache = None  # Reconnect invalidates exactly this cache.
    await session.activity_capabilities()
    session.command = ['/replacement/imsg', 'rpc']
    await session.activity_capabilities()
    assert commands[-1] == ('/replacement/imsg', 'status', '--json') and len(commands) == 3


@pytest.mark.asyncio
async def test_optional_snapshot_failure_does_not_fail_natural_chat(tmp_path, monkeypatch, script):
    state = str(tmp_path / 'snapshot.json')
    directory = ActivityStore(state).directory
    directory.symlink_to(tmp_path / 'outside')
    turns, _ = script
    turns.extend([route('chat'), turn('Merhaba.')])
    report = await conversation.run_conversation_with_callback('Merhaba', lambda event: None,
        {'state_file': state, 'requested_backend': 'ollama-cloud', 'history': [], 'should_stop': lambda: False},
        {'ollama-cloud': object()})
    assert report['success'] and report['outcome'] == 'Merhaba.'
    assert ActivityStore(state).load() == []


@pytest.mark.asyncio
@pytest.mark.parametrize('text', ['/status', '/durum', 'ne yapıyorsun?'])
async def test_telegram_status_ingress_reads_live_other_channel_without_indicator(tmp_path, monkeypatch, text):
    monkeypatch.setenv('OMNI_DATA_DIR', str(tmp_path))
    state = str(tmp_path / 'status.json')
    monkeypatch.setattr(telegram, 'STATE_FILE', state)
    desktop = ActivityController(state).start('desktop', 'real directory inspection')
    desktop.event({'kind': 'tool_started', 'name': 'list_directory'})
    api = FakeAPI(); bridge = telegram.TelegramBridge(api, {'chat_id': 123, 'user_id': 456})
    async def forbidden(*args):
        raise AssertionError('status inspection must not start a model or typing')
    api.send_chat_action = forbidden
    monkeypatch.setattr(bridge, '_refresh_clients', forbidden)
    await bridge.handle({'message': {'chat': {'id': 123, 'type': 'private'}, 'from': {'id': 456}, 'text': text}})
    assert 'desktop: Okuyor · real directory inspection' in api.sent[0]
    assert bridge.active is None
    assert len(ActivityStore(state).load()) == 1
    await desktop.close()


@pytest.mark.asyncio
async def test_imessage_status_observes_real_concurrent_telegram_read_and_verifier(parts, tmp_path, monkeypatch, script):
    bridge, transport, _ = parts
    state = str(tmp_path / 'concurrent.json')
    monkeypatch.setattr(imessage, 'STATE_FILE', state)
    monkeypatch.setattr(telegram, 'STATE_FILE', state)
    monkeypatch.setattr(telegram, 'create_model_clients', lambda: {'ollama-cloud': object()})
    target = tmp_path / 'observed.txt'; target.write_text('OBSERVED_NAME')
    verification, release = asyncio.Event(), asyncio.Event()
    turns, _ = script
    async def verifier(emit, stop):
        verification.set()
        await release.wait()
        return verified()
    turns.extend([route('investigate'), turn(calls=[call('read_file', path=str(target))]),
                  turn('OBSERVED_NAME'), verifier])
    api = FakeAPI(); tg = telegram.TelegramBridge(api, {'chat_id': 123, 'user_id': 456})
    tg.backend = 'ollama-cloud'
    async def typing(chat_id):
        pass
    api.send_chat_action = typing
    await tg.handle({'message': {'chat': {'id': 123, 'type': 'private'}, 'from': {'id': 456},
                                 'text': f'Read the file {target}'}})
    await asyncio.wait_for(verification.wait(), 2)
    await bridge.on_message(incoming(77, 'ne yapıyorsun?', HANDLE))
    await settle(bridge)
    assert 'telegram: Yanıtı hazırlıyor · Read the file' in transport.texts[0]
    assert 'OBSERVED_NAME' not in transport.texts[0]
    release.set()
    await tg.active
    assert not turns and all(row['stage'] == 'terminal' for row in ActivityStore(state).load())
    await tg.integrations.close(); await bridge.close()


@pytest.mark.asyncio
async def test_imessage_rejected_real_host_lock_sends_no_work_ack_or_start_record(parts, tmp_path, monkeypatch, script):
    from omniagent.platform.macos.host_lock import host_task_lock
    bridge, transport, store = parts
    from omniagent.platform.macos.host_lock import async_host_task_lock_preempting
    monkeypatch.setattr(delegate, 'async_host_task_lock_preempting', lambda: async_host_task_lock_preempting(timeout_seconds=.1))
    state = str(tmp_path / 'busy-real.json')
    monkeypatch.setattr(imessage, 'STATE_FILE', state)
    monkeypatch.setattr(delegate, 'STATE_FILE', state)
    monkeypatch.setattr(imessage, 'decide_conversation', conversation.decide_conversation)
    class Client:
        async def close(self):
            pass
    clients = {'openai': Client()}
    bridge.chat_clients = clients
    monkeypatch.setattr(delegate, 'create_model_clients', lambda: clients)
    turns, requests = script
    turns.append(route('task'))
    with host_task_lock():
        await bridge.on_message(incoming(90, 'Open the Calculator app', HANDLE))
        await settle(bridge)
    assert len(requests) == 1 and not turns
    assert chat.TASK_ACK not in transport.texts
    outgoing = [row['id'] for row in store.recent_messages(20) if row['direction'] == 'out']
    assert store.task_starts(outgoing) == {}
    assert 'tamamlanamadı' in transport.texts[-1].casefold()
    await bridge.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('concurrent', [False, True])
async def test_imessage_whole_close_joins_classifier_and_activity_on_repeated_cancel(parts, tmp_path, monkeypatch, script, concurrent):
    bridge, transport, _ = parts
    monkeypatch.setattr(imessage, 'STATE_FILE', str(tmp_path / 'shutdown.json'))
    monkeypatch.setattr(imessage, 'decide_conversation', conversation.decide_conversation)
    bridge.chat_clients = {'openai': object()}
    classifier_entered, typing_entered, cleaning, release = (asyncio.Event() for _ in range(4))
    async def capability():
        return {'typing_indicators': True, 'v2_ready': True}
    async def typing(handle):
        typing_entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
    transport.activity_capabilities = capability; transport.send_typing = typing
    async def classifier(emit, stop):
        classifier_entered.set()
        await asyncio.Event().wait()
    turns, _ = script; turns.append(classifier)
    await bridge.on_message(incoming(91, 'Merhaba', HANDLE))
    await asyncio.wait_for(classifier_entered.wait(), 1)
    await asyncio.wait_for(typing_entered.wait(), 1)
    activity = bridge.chat_activity
    notification_cleaning, release_notification = asyncio.Event(), asyncio.Event()
    async def notification():
        while not bridge.closing:
            await asyncio.sleep(0)
        notification_cleaning.set()
        await release_notification.wait()
    pending_notice = asyncio.create_task(notification())
    bridge.notification_tasks.add(pending_notice)
    pending_notice.add_done_callback(bridge.notification_tasks.discard)
    first = asyncio.create_task(bridge.close())
    await cleaning.wait()
    prompt_stop = bridge.stop_event.is_set()
    first.cancel(); await asyncio.sleep(0); first.cancel()
    cleanup = bridge._close_task
    second = asyncio.create_task(bridge.close()) if concurrent else None
    await asyncio.sleep(0)
    assert not first.done() and (second is None or not second.done())
    release.set()
    await notification_cleaning.wait()
    await asyncio.sleep(0)
    assert not first.done() and (second is None or not second.done())
    release_notification.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    if second is not None:
        await asyncio.wait_for(second, 2)
    await bridge.close()
    assert bridge._close_task is cleanup
    assert prompt_stop and bridge.stop_event.is_set() and bridge.closing
    assert activity.closed and activity.heartbeat.done()
    assert (bridge.burst_timer is None or bridge.burst_timer.done()) and not bridge.chat_lock.locked()
    assert not bridge.activity_indicator.running
    assert pending_notice.done() and not bridge.task_runs and not bridge.notification_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize('unknown', [False, True])
async def test_task_readiness_owns_host_lock_and_awaits_ack_before_engine(parts, tmp_path, monkeypatch, script, unknown):
    from omniagent.platform.macos.host_lock import host_task_lock, HostBusyError
    from tests.test_imessage_bridge import report_for
    bridge, transport, store = parts
    state = str(tmp_path / 'ready.json')
    monkeypatch.setattr(imessage, 'STATE_FILE', state)
    monkeypatch.setattr(delegate, 'STATE_FILE', state)
    monkeypatch.setattr(imessage, 'decide_conversation', conversation.decide_conversation)
    class Client:
        async def close(self):
            pass
    clients = {'openai': Client()}; bridge.chat_clients = clients
    monkeypatch.setattr(delegate, 'create_model_clients', lambda: clients)
    turns, _ = script; turns.append(route('task'))
    entered, release = asyncio.Event(), asyncio.Event()
    order, attempts = [], []
    original = transport.send_text
    async def send(handle, text):
        attempts.append(text)
        if text == chat.TASK_ACK:
            with pytest.raises(HostBusyError):
                with host_task_lock():
                    pass
            entered.set()
            await release.wait()
            order.append('ack')
            if unknown:
                raise DeliveryUnknown('optional acknowledgment unknown')
        return await original(handle, text)
    transport.send_text = send
    async def engine(goal, emit, options, clients):
        order.append('engine')
        with pytest.raises(HostBusyError):
            with host_task_lock():
                pass
        return report_for(goal, 'Engine observed failure', False)
    monkeypatch.setattr(conversation, 'run_agent_with_callback', engine)
    await bridge.on_message(incoming(92, 'Open the Calculator app', HANDLE))
    await asyncio.wait_for(entered.wait(), 2)
    assert order == [] and bridge.task is not None
    ack_ids = [row['id'] for row in store.recent_messages(20) if row['text'] == chat.TASK_ACK]
    assert store.task_starts(ack_ids) == {}
    release.set()
    await settle(bridge)
    assert order == ['ack', 'engine'] and attempts.count(chat.TASK_ACK) == 1
    assert store.task_starts(ack_ids) == {ack_ids[0]: 'Open the Calculator app'}
    with host_task_lock():
        pass
    await bridge.close()
