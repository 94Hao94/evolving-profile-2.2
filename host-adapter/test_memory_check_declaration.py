"""Exercise declarations through the real JSON-RPC dispatch, without network."""
import contextlib
import io
import json
import runpy
import socket
from pathlib import Path

import pytest


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HINDSIGHT_TURN_CHECK_ROOT', str(tmp_path / 'turn-checks'))
    monkeypatch.setenv('EVOLVING_PROFILE_ROUTE_RECEIPT_ROOT', str(tmp_path / 'receipts'))
    def deny_network(*_args, **_kwargs):
        raise AssertionError('Declaration must not perform retrieval or model calls')
    monkeypatch.setattr(socket.socket, 'connect', deny_network)
    return tmp_path


def ingress(home, check='modern-check', session='s1', turn='t1', at='2026-10-01T01:00:00Z', prompt='可以，开工'):
    target = home / '.evolving-profile/audit/prompt-ingress.jsonl'
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open('a') as stream:
        stream.write(json.dumps({'at':at, 'hook_invocation_id':check, 'session_id':session,
            'turn_id':turn, 'prompt_preview':prompt}) + '\n')


def invoke(monkeypatch, args, meta=None):
    request = {'jsonrpc':'2.0','id':7,'method':'tools/call',
        'params':{'name':'memory_check','arguments':args,'_meta':meta or {}}}
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps(request) + '\n'))
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        runpy.run_path(str(Path(__file__).with_name('evolving_profile_controller_mcp.py')), run_name='__main__')
    return json.loads(output.getvalue())


def declaration_args(check='modern-check'):
    return {'check_id':check,'full_prompt':'继续已批准的发布门收尾',
        'need':'not_needed','reason':'已有当前证据，不需要历史检索'}


def test_modern_hook_declaration_succeeds_without_legacy_registration(isolated_home, monkeypatch):
    ingress(isolated_home)
    response = invoke(monkeypatch, declaration_args())
    assert 'error' not in response
    body = json.loads(response['result']['content'][0]['text'])
    assert body['mode'] == 'memory_check_declaration'
    assert body['check_id'] == 'modern-check'
    assert body['original_prompt'] == '可以，开工'
    assert body['semantic_alignment'] == 'not_verified'
    assert body['execution_verified'] is False
    assert body['observability_binding']['state'] == 'prompt_bound'
    assert body['caller_identity_state'] == 'caller_identity_unverified'
    assert body['original_prompt_coverage'] == 'hook_ingress_preview'
    assert body['original_prompt_complete'] is False


def test_caller_from_another_turn_is_rejected(isolated_home, monkeypatch):
    ingress(isolated_home)
    response = invoke(monkeypatch, declaration_args(), {'session_id':'s1','turn_id':'t2'})
    assert response['error']['message'] == 'memory_check caller identity does not match Hook binding'


def test_matching_host_caller_is_verified(isolated_home, monkeypatch):
    ingress(isolated_home)
    response = invoke(monkeypatch, declaration_args(), {'session_id':'s1','turn_id':'t1'})
    body = json.loads(response['result']['content'][0]['text'])
    assert body['caller_identity_state'] == 'caller_identity_verified'


def test_registered_legacy_id_still_declares(isolated_home, monkeypatch):
    from memory_turn_check import register
    register({'invocation_id':'legacy-check','session_id':'s1','turn_id':'t1','raw_prompt':'legacy original'})
    response = invoke(monkeypatch, declaration_args('legacy-check'))
    assert 'error' not in response
    body = json.loads(response['result']['content'][0]['text'])
    assert body['mode'] == 'memory_check_declaration'
    assert body['original_prompt'] == 'legacy original'


@pytest.mark.parametrize('change,expected', [
    ({'check_id':'fabricated'}, 'check_id was not registered by a Hook; do not invent one'),
    ({'check_id':None}, 'invalid check_id'),
    ({'need':''}, 'invalid need'),
    ({'need':'bogus /secret/path api_key=private'}, 'invalid need'),
    ({'reason':''}, 'full_prompt and reason are required'),
    ({'reason':'  '}, 'full_prompt and reason are required'),
    ({'full_prompt':''}, 'full_prompt and reason are required'),
])
def test_invalid_declarations_report_the_real_error(isolated_home, monkeypatch, change, expected):
    ingress(isolated_home)
    response = invoke(monkeypatch, {**declaration_args(), **change})
    assert response['error']['message'] == expected
    assert '/secret/path' not in json.dumps(response)
    assert 'api_key=private' not in json.dumps(response)


@pytest.mark.parametrize('conflict,expected', [
    ({'check':'new-check','session':'s1','turn':'t2','at':'2026-10-01T02:00:00Z'}, 'stale_prompt_binding'),
    ({'check':'modern-check','session':'s1','turn':'t2','at':'2026-10-01T02:00:00Z'}, 'ambiguous_prompt_binding'),
    ({'check':'modern-check','session':'s2','turn':'t2','at':'2026-10-01T02:00:00Z'}, 'ambiguous_prompt_binding'),
])
def test_stale_and_conflicting_modern_identities_never_fall_back(isolated_home, monkeypatch, conflict, expected):
    from memory_turn_check import register
    register({'invocation_id':'modern-check','session_id':'s1','turn_id':'t1','raw_prompt':'legacy original'})
    ingress(isolated_home)
    ingress(isolated_home, **conflict)
    response = invoke(monkeypatch, declaration_args())
    assert response['error']['message'] == 'memory_check identity is not current: ' + expected


def test_older_legacy_registration_is_rejected(isolated_home, monkeypatch):
    from memory_turn_check import register
    register({'invocation_id':'legacy-check','session_id':'s1','turn_id':'t1','raw_prompt':'old'})
    register({'invocation_id':'new-check','session_id':'s1','turn_id':'t2','raw_prompt':'new'})
    response = invoke(monkeypatch, declaration_args('legacy-check'))
    assert response['error']['message'] == 'memory_check identity is not current: stale_legacy_binding'


@pytest.mark.parametrize('modern_at,legacy_at,allowed', [
    ('2026-10-01T01:00:00Z','2026-10-02T01:00:00Z',True),
    ('2026-10-02T01:00:00Z','2026-10-01T01:00:00Z',False),
    ('2026-10-01T01:00:00Z',None,False),
])
def test_legacy_registration_checks_modern_chronology(isolated_home, monkeypatch, modern_at, legacy_at, allowed):
    from memory_turn_check import register
    ingress(isolated_home, check='modern-other-turn', turn='t0', at=modern_at)
    register({'invocation_id':'legacy-check','session_id':'s1','turn_id':'t1','raw_prompt':'current legacy','at':legacy_at})
    response = invoke(monkeypatch, declaration_args('legacy-check'), {'session_id':'s1','turn_id':'t1'})
    if allowed:
        assert 'error' not in response
        body = json.loads(response['result']['content'][0]['text'])
        assert body['original_prompt'] == 'current legacy'
        assert body['caller_identity_state'] == 'caller_identity_verified'
    else:
        assert response['error']['message'] == 'memory_check identity is not current: legacy_turn_unverified'


@pytest.mark.parametrize('identity', [{'session':'','turn':'t1'}, {'session':'s1','turn':None}])
def test_incomplete_modern_hook_identity_is_rejected(isolated_home, monkeypatch, identity):
    ingress(isolated_home, **identity)
    response = invoke(monkeypatch, declaration_args())
    assert response['error']['message'] == 'memory_check Hook identity incomplete'
