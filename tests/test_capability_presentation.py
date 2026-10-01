"""Successful local audits must not depend on a probabilistic prose verifier."""
import json

import pytest

from omniagent.app.conversation_grounding import ground_answer
from omniagent.app.policy import capability_inspection_requested
from omniagent.core.conversation_policy import derive_request_contract
from omniagent.core.evidence import EvidenceStore, new_evidence_bundle
from tests.test_shared_conversation import coordinator, scripted


def audit(tmp_path):
    store = EvidenceStore(tmp_path / 'evidence')
    bundle = new_evidence_bundle(derive_request_contract(
        'Bilgisayarımda ne gibi yetkilerin var?', route='investigate'))
    receipt = {'permission_report': 'Ekran kaydı: İZİN YOK\nErişilebilirlik: izinli\nTam Disk Erişimi: izinli',
               'registered_tools': ['inspect_host_capabilities', 'web_search', 'read_file'],
               'scope': 'İzin denetimi bu çalışan sürece aittir.',
               'limitations': ['Tarayıcı oturumu ve API anahtarları doğrulanmadı.']}
    store.capture(bundle, 'inspect_host_capabilities', {'ok': True, 'result': json.dumps(receipt, ensure_ascii=False)})
    return store, bundle


@pytest.mark.asyncio
async def test_complete_local_audit_is_readable_authoritative_without_model(tmp_path):
    store, bundle = audit(tmp_path)
    async def model(messages):
        pytest.fail('Actual host permission receipt does not need a model verifier')
    answer, ok = await ground_answer('Her şeye erişiyorum; ekran iznim var.', bundle, store, model)
    assert ok
    assert 'Ekran kaydı: İZİN YOK' in answer and 'Erişilebilirlik: izinli' in answer
    assert 'Tam Disk Erişimi: izinli' in answer and 'web_search' in answer
    assert 'bu çalışan sürece' in answer and 'API anahtarları doğrulanmadı' in answer
    assert 'Her şeye erişiyorum' not in answer
    assert 'permission_report' not in answer and '\\n' not in answer


@pytest.mark.asyncio
async def test_exact_full_disk_question_runs_actual_audit_and_answers_its_scope(tmp_path, coordinator, scripted, monkeypatch):
    from omniagent.platform.macos import permissions
    from tests.test_shared_conversation import turn, call, run
    goal = 'Bilgisayarda tam disk erişimi açık mı'
    assert capability_inspection_requested(goal)
    assert not capability_inspection_requested('Tam disk erişimi nedir, nasıl çalışır?')
    monkeypatch.setattr(permissions, 'report', lambda: 'Ekran kaydı: İZİN YOK\nTam Disk Erişimi: izinli')
    scripts, requests = scripted
    scripts.extend([turn(calls=[call('inspect_host_capabilities')]), turn('Tahmin: izin yok.')])
    report, _ = await run(coordinator, tmp_path, goal)
    assert report['success'], report['outcome']
    assert report['metrics']['tool_calls'] == 1
    assert report['outcome'].startswith('Tam Disk Erişimi: izinli')
    assert 'bu çalışan sürece' in report['outcome']
    assert 'Tahmin' not in report['outcome'] and len(requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['failed', 'incomplete', 'extra_source', 'extra_fields', 'wrong_subject', 'malformed'])
async def test_local_audit_does_not_certify_other_or_incomplete_evidence(tmp_path, boundary):
    store, bundle = audit(tmp_path)
    if boundary == 'failed':
        bundle['observations'][0]['ok'] = False
    elif boundary == 'incomplete':
        bundle['complete'] = False
        bundle['observations'][0]['complete'] = False
    elif boundary == 'extra_source':
        store.capture(bundle, 'fetch_raw', {'ok': True, 'result': 'Unrelated external claim'})
    elif boundary == 'extra_fields':
        bundle['contract']['required_fields'] = ['release_dates']
    elif boundary == 'wrong_subject':
        bundle['contract']['subject'] = 'En yeni modelleri araştır'
    else:
        bundle['observations'][0]['text'] = '{broken'
    calls = []
    async def model(messages):
        calls.append(messages)
        return {'content': '{"ok":false,"facts":[],"missing_fields":[],"unsupported_claims":["unsupported"]}'}
    _, ok = await ground_answer('Unsupported claim', bundle, None, model)
    assert not ok
