"""Real lock contention, provider payload and native message presentation regressions."""
import asyncio
import json
import time
from contextlib import asynccontextmanager

import pytest

from omniagent.app import conversation, agent
from omniagent.app.model_runtime import stream_completion
from omniagent.config import BACKENDS
from omniagent.core.conversation_policy import derive_request_contract
from omniagent.core.evidence import new_evidence_bundle, mark_incomplete
from omniagent.platform.macos.host_lock import host_task_lock, async_host_task_lock_preempting, host_owner, HostBusyError
from tests.test_provider_requests import FakeClient


@pytest.mark.asyncio
async def test_user_waiter_acquires_only_after_owner_releases(tmp_path):
    path = tmp_path / 'host.lock'
    entered = asyncio.Event()
    async def waiter():
        async with async_host_task_lock_preempting(timeout_seconds=.5, path=path):
            entered.set()
            assert host_owner(path) == 'user'
    with host_task_lock(path):
        task = asyncio.create_task(waiter())
        await asyncio.sleep(.08)
        assert not entered.is_set() and not task.done()
        assert not path.with_name('host.lock.preempt.request').exists()
    await task
    assert entered.is_set() and host_owner(path) is None


@pytest.mark.asyncio
async def test_user_waiter_timeout_and_cancel_preserve_owner(tmp_path):
    path = tmp_path / 'host.lock'
    with host_task_lock(path):
        started = time.monotonic()
        with pytest.raises(HostBusyError):
            async with async_host_task_lock_preempting(timeout_seconds=.1, path=path):
                pytest.fail('stole lock')
        assert time.monotonic() - started >= .09
        async def waiting():
            async with async_host_task_lock_preempting(timeout_seconds=2, path=path):
                pytest.fail('cancelled waiter ran')
        task = asyncio.create_task(waiting())
        await asyncio.sleep(.06)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(.08)
        assert host_owner(path) == 'user'
        with pytest.raises(HostBusyError):
            with host_task_lock(path):
                pass
    await asyncio.sleep(.08)
    with host_task_lock(path):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize('missing', [True, False])
async def test_ollama_tool_only_assistant_content_is_text_without_mutating_history(missing):
    client = FakeClient()
    calls = [{'id': 'c', 'type': 'function', 'function': {'name': 'ping', 'arguments': '{}'}}]
    entry = {'role': 'assistant', 'tool_calls': calls}
    if not missing:
        entry['content'] = None
    messages = [entry, {'role': 'tool', 'tool_call_id': 'c', 'content': 'ok'}]
    before = json.dumps(messages)
    await stream_completion(client, BACKENDS['ollama-cloud'], messages, [], 's', lambda e: None, lambda: False)
    sent = client.chat.completions.kwargs['messages']
    assert sent[0]['content'] == '' and sent[0]['tool_calls'] == calls
    assert sent[1] == messages[1] and json.dumps(messages) == before


def test_known_empty_receipt_failure_is_one_actionable_reason():
    evidence = new_evidence_bundle(derive_request_contract('Ekranda ne görüyorsun?'))
    reason = 'macOS Ekran Kaydı izni verilmemiş.'
    mark_incomplete(evidence, reason)
    result = conversation._failure_outcome(evidence, reason, RuntimeError(reason))
    assert result.count(reason) == 1
    assert 'kaynak gözlemi alınamadı' not in result
    assert 'tamamlanamadı' in result


def test_imessage_markdown_preserves_code_links_and_names():
    from omniagent.core.message_presentation import imessage_plain_text
    code = '  print("**literal**")\n\tname = "[x](url)"\n'
    text = '# Başlık\n\n- **Dosya:** Omni_Agent\n- [Resmi sayfa](https://example.com/a?q=1&b=2)\n\n```python\n' + code + '```\n`/Users/a_b` ve **tamam**'
    actual = imessage_plain_text(text)
    assert actual == 'Başlık\n\n• Dosya: Omni_Agent\n• Resmi sayfa (https://example.com/a?q=1&b=2)\n\n' + code + '/Users/a_b ve tamam'

from tests.test_shared_conversation import coordinator, scripted, run, turn, call
from tests.test_imessage_bridge import parts
from omniagent.app.policy import screen_inspection_requested, capability_inspection_requested


@asynccontextmanager
async def task_context():
    yield


@pytest.mark.asyncio
@pytest.mark.parametrize("goal", ["Ekranda ne görüyorsun?", "Ekranda ne göruyorsun?", "bilgisayar ekranimda ne var"])
async def test_explicit_screen_query_recovers_to_real_image_and_visual_verifier(coordinator, scripted, tmp_path, monkeypatch, goal):
    from PIL import Image
    from omniagent.tools.facade import Toolbox
    from omniagent.core.evidence import EvidenceStore
    picture = tmp_path / 'screen.png'
    Image.new('RGB', (80, 60), 'red').save(picture)
    captures = []
    def capture(self, filename, **kwargs):
        captures.append(filename)
        return 'Ekran görüntüsü kaydedildi: ' + filename
    monkeypatch.setattr(Toolbox, 'take_screenshot', capture)
    scripts, requests = scripted
    scripts.extend([turn('Tahmin: Chrome açık.'), turn(calls=[call('take_screenshot', filename=str(picture))]),
                    turn('Ekranda kırmızı bir alan görüyorum.'),
                    turn(json.dumps({'ok': True, 'visible_claims': ['kırmızı bir alan'], 'missing_fields': [], 'unsupported_claims': []}))])
    report, events = await run(coordinator, tmp_path, goal, {'task_context': task_context})
    assert report['success'], report['outcome']
    assert report['outcome'] == 'Ekranda kırmızı bir alan görüyorum.'
    assert captures == [str(picture)] and len(requests) == 4
    assert 'image_url' in json.dumps(requests[-1][0]) and requests[-1][1] == []
    assert 'Tahmin: Chrome' not in json.dumps(events)
    stored = EvidenceStore(str(tmp_path / 'state.json')).load(report['evidence']['run_id'])
    assert 'base64' not in json.dumps(stored) and 'image_url' not in json.dumps(stored)
    assert stored['observations'][0]['completeness'] == 'image_receipt'


@pytest.mark.asyncio
async def test_screen_capture_known_failure_does_not_claim_observation(coordinator, scripted, tmp_path, monkeypatch):
    from omniagent.tools.facade import Toolbox
    from omniagent.tools.types import ToolError
    def capture(self, filename, **kwargs):
        raise ToolError('macOS Ekran Kaydı izni verilmemiş.', 'SCREEN_CAPTURE_DENIED', False)
    monkeypatch.setattr(Toolbox, 'take_screenshot', capture)
    scripts, requests = scripted
    scripts.extend([turn(calls=[call('take_screenshot', filename=str(tmp_path / 'screen.png'))]),
                    turn('Her şey çalışıyor, Chrome görüyorum.')])
    report, events = await run(coordinator, tmp_path, 'Ekranda ne var?', {'task_context': task_context})
    assert not report['success'] and 'Ekran Kaydı izni' in report['outcome']
    assert 'Chrome görüyorum' not in json.dumps(events) and len(requests) == 2
    assert report['metrics']['tool_calls'] == 1


@pytest.mark.asyncio
async def test_screen_verifier_rejects_invented_content_and_keeps_images_private(coordinator, scripted, tmp_path, monkeypatch):
    from PIL import Image
    from omniagent.tools.facade import Toolbox
    picture = tmp_path / 'screen.png'
    Image.new('RGB', (10, 10), 'red').save(picture)
    monkeypatch.setattr(Toolbox, 'take_screenshot', lambda self, filename, **kwargs: 'Alındı: ' + filename)
    scripts, _ = scripted
    scripts.extend([turn(calls=[call('take_screenshot', filename=str(picture))]), turn('Gizli banka hesabın açık.'),
                    turn(json.dumps({'ok': False, 'visible_claims': [], 'missing_fields': [], 'unsupported_claims': ['gizli banka']})),
                    turn('Gizli banka hesabın açık.'), turn('{"ok":false}')])
    report, events = await run(coordinator, tmp_path, 'Ekranda ne görüyorsun?', {'task_context': task_context})
    assert not report['success'] and 'güvenilir biçimde' in report['outcome']
    assert 'Gizli banka' not in json.dumps(events) and 'base64' not in json.dumps(report)


def test_inspection_guards_do_not_turn_explanations_into_host_inspection():
    assert screen_inspection_requested('Ekranda ne görüyorsun?')
    assert not screen_inspection_requested('Ekran çözünürlüğü nedir?')
    assert capability_inspection_requested('Bilgisayardaki mevcut yetkileri ve erişilebilir araçları belirlemek için sistem analizini gerçekleştir.')
    assert not capability_inspection_requested('Neler yapabiliyorsun?')


@pytest.mark.asyncio
async def test_imessage_report_formats_once_before_chunks_and_echoes_exact_display(parts):
    from tests.test_imessage_bridge import report_for, echo
    bridge, transport, store = parts
    text = '**Başlık**\n\n```python\n' + '    print("**literal**")\n' * 170 + '```\n[Kaynak](https://example.org)'
    outcome = {'goal': 'kod', 'report': report_for('kod', text, True), 'started_at': '', 'finished_at': '', 'tokens': 0}
    await bridge._present_report(outcome)
    display = ''.join(transport.texts)
    assert '```' not in display and '**Başlık**' not in display
    assert display.count('    print("**literal**")') == 170
    assert 'Kaynak (https://example.org)' in display
    for index, sent in enumerate(transport.texts):
        await bridge.on_message(echo(900 + index, sent))
    rows = [row for row in store.recent_messages(20) if row['direction'] == 'out']
    assert all(row['delivery'] == 'sent' for row in rows)


def test_plain_message_keeps_literal_url_delimiters_and_unclosed_code():
    from omniagent.core.message_presentation import imessage_plain_text
    assert imessage_plain_text('Dosya: __init__.py') == 'Dosya: __init__.py'
    assert imessage_plain_text('Bağlantı: **https://example.com/path**') == 'Bağlantı: https://example.com/path'
    assert imessage_plain_text('**[Sayfa](https://example.com/path)**') == 'Sayfa (https://example.com/path)'
    assert imessage_plain_text('**Kaynak:** https://example.org/**exact**/a_b?q=1') == 'Kaynak: https://example.org/**exact**/a_b?q=1'
    assert imessage_plain_text('```python\n    **literal**') == '```python\n    **literal**'


@pytest.mark.asyncio
async def test_local_capability_audit_uses_permission_receipt_without_blocking_busy_host(coordinator, scripted, tmp_path, monkeypatch):
    from omniagent.platform.macos import permissions
    from omniagent.platform.macos.host_lock import host_task_lock
    monkeypatch.setattr(permissions, 'report', lambda: 'Ekran kaydı: izinli\nErişilebilirlik: İZİN YOK')
    scripts, requests = scripted
    scripts.extend([turn('Her şeye erişiyorum.'),
                    turn(calls=[call('inspect_host_capabilities')]),
                    turn('Kurulu araç kataloğunu inceledim; işletim sistemi izinlerini ayrıca kontrol etmek gerekiyor.')])
    with host_task_lock():
        report, events = await run(coordinator, tmp_path,
            'Bilgisayarımda neler yapabilirsin ne gibi yetkilerin var?',
            {'task_context': task_context})
    assert report['success'], report['outcome']
    assert report['metrics']['tool_calls'] == 1 and len(requests) == 3
    assert report['evidence']['observations'][0]['tool'] == 'inspect_host_capabilities'
    assert 'Her şeye erişiyorum' not in json.dumps(events)


@pytest.mark.asyncio
async def test_cancellation_during_actual_image_check_never_publishes_private_draft(coordinator, scripted, tmp_path, monkeypatch):
    from PIL import Image
    from omniagent.tools.facade import Toolbox
    picture = tmp_path / 'screen.png'
    Image.new('RGB', (10, 10), 'red').save(picture)
    monkeypatch.setattr(Toolbox, 'take_screenshot', lambda self, filename, **kwargs: 'Alındı: ' + filename)
    stopped = {'value': False}
    async def checking(emit, stop):
        stopped['value'] = True
        return turn(json.dumps({'ok': True, 'visible_claims': ['özel taslak'], 'missing_fields': [], 'unsupported_claims': []}))
    scripts, _ = scripted
    scripts.extend([turn(calls=[call('take_screenshot', filename=str(picture))]), turn('özel taslak'), checking])
    report, events = await run(coordinator, tmp_path, 'Ekranda ne var?', {
        'task_context': task_context, 'should_stop': lambda: stopped['value']})
    assert not report['success'] and 'özel taslak' not in json.dumps(events)
    assert report['evidence']['observations'][0]['tool'] == 'take_screenshot'
    assert 'image_url' not in json.dumps(report)


def test_failure_retains_actual_completed_tool_receipt(tmp_path):
    from omniagent.core.evidence import EvidenceStore
    evidence = new_evidence_bundle(derive_request_contract('Bir dosya yaz sonra kontrol et'))
    store = EvidenceStore(str(tmp_path / 'state.json'))
    store.save(evidence, create_only=True)
    store.capture(evidence, 'write_file', {'ok': True, 'result': 'notes.txt yazıldı; 12 karakter.'}, arguments='{"path":"notes.txt"}')
    reason = 'Son kontrol aracı kullanılamadı.'
    mark_incomplete(evidence, reason)
    display = conversation._failure_outcome(evidence, reason, RuntimeError(reason), store)
    assert 'notes.txt yazıldı; 12 karakter.' in display and display.count(reason) == 1


@pytest.mark.asyncio
async def test_native_code_only_reply_preserves_first_line_indent_and_last_newline(parts, monkeypatch):
    from omniagent.companion import chat
    from omniagent.core.message_presentation import imessage_plain_text
    sent = []
    async def model(*args, **kwargs):
        return turn('```python\n    return "**literal**"\n```'), 'openai'
    async def send(text):
        sent.append(text)
    monkeypatch.setattr(chat, '_chat_completion', model)
    await chat.respond({}, 'openai', '', [], [], send, lambda: False, 's', presentation=imessage_plain_text)
    assert sent == ['    return "**literal**"\n']


@pytest.mark.asyncio
async def test_realistic_screen_image_larger_than_text_budget_remains_valid_visual_input(coordinator, scripted, tmp_path, monkeypatch):
    from PIL import Image
    from omniagent.tools.facade import Toolbox
    picture = tmp_path / 'screen.png'
    Image.effect_noise((1200, 800), 90).convert('RGB').save(picture)
    monkeypatch.setattr(Toolbox, 'take_screenshot', lambda self, filename, **kwargs: 'Alındı: ' + filename)
    scripts, requests = scripted
    scripts.extend([turn(calls=[call('take_screenshot', filename=str(picture))]), turn('Ekranda gri bir alan var.'),
                    turn(json.dumps({'ok': True, 'visible_claims': ['gri bir alan'], 'missing_fields': [], 'unsupported_claims': []}))])
    report, events = await run(coordinator, tmp_path, 'Ekranda ne görüyorsun?', {'task_context': task_context})
    assert report['success'], report['outcome']
    assert len(json.dumps(requests[-1][0])) > 80000 and 'image_url' in json.dumps(requests[-1][0])
    assert 'base64' not in json.dumps(report) and 'base64' not in json.dumps(events)


@pytest.mark.asyncio
async def test_screen_verification_budget_failure_cannot_promote_private_engine_success(coordinator, scripted, tmp_path, monkeypatch):
    from PIL import Image
    from omniagent.tools.facade import Toolbox
    picture = tmp_path / 'screen.png'
    Image.new('RGB', (10, 10), 'red').save(picture)
    monkeypatch.setattr(Toolbox, 'take_screenshot', lambda self, filename, **kwargs: 'Alındı: ' + filename)
    scripts, requests = scripted
    scripts.extend([turn(calls=[call('take_screenshot', filename=str(picture))]), turn('private_unverified ' * 5000)])
    report, events = await run(coordinator, tmp_path, 'Ekranda ne görüyorsun?', {'task_context': task_context})
    assert not report['success'] and '80.000' in report['reason']
    assert len(requests) == 2 and 'private_unverified' not in json.dumps(events)


@pytest.mark.asyncio
async def test_new_unreadable_screen_image_cannot_reuse_an_earlier_visual_snapshot(coordinator, scripted, tmp_path, monkeypatch):
    from PIL import Image
    from omniagent.tools.facade import Toolbox
    first, latest = tmp_path / 'first.png', tmp_path / 'latest.png'
    Image.new('RGB', (10, 10), 'red').save(first)
    latest.write_bytes(b'not an image')
    monkeypatch.setattr(Toolbox, 'take_screenshot', lambda self, filename, **kwargs: 'Alındı: ' + filename)
    scripts, requests = scripted
    one, two = call('take_screenshot', filename=str(first)), call('take_screenshot', filename=str(latest))
    two['id'] = 'latest-screen'
    scripts.extend([turn(calls=[one, two]), turn('Son ekranda kırmızı alan var.')])
    report, events = await run(coordinator, tmp_path, 'Ekranda ne görüyorsun?', {'task_context': task_context})
    assert not report['success'] and len(requests) == 2
    assert 'Son ekranda kırmızı alan var.' not in json.dumps(events)
