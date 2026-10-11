import json
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from lib.context_incremental import ContextIncremental
from lib.context_summary import read_context_index
from test_context_incremental import rollout, write_runtime_settings


def assistant_source(path):
    lines = path.read_text().splitlines()
    message = json.loads(lines[-1])
    message['payload'].update(role='assistant', phase='final_answer')
    lines[-1] = json.dumps(message)
    path.write_text('\n'.join(lines) + '\n')


def legacy_job(tmp_path, *, code='scenario_source_no_user_messages', attempts=1, assistant=True):
    sid = str(uuid.uuid4())
    root = tmp_path / 'sessions'
    path = rollout(root, sid)
    if assistant:
        assistant_source(path)
    p = ContextIncremental(tmp_path / 'state', root)
    write_runtime_settings(p.root.parent)
    p.discover(now=100)
    with p.db() as db:
        db.execute("UPDATE jobs SET status='failed',attempts=?,error_code=?", (attempts, code))
    return p, sid, path


def inspect(p):
    with p.db() as db:
        return dict(db.execute('SELECT * FROM jobs').fetchone())


def reassess(p, **kwargs):
    import lib.context_incremental as module
    fn = getattr(module, 'reassess_source_jobs', None)
    assert callable(fn), 'Read-only input reassessment entry is missing'
    return fn(p.root.parent, p.session_root, **kwargs)


def test_assistant_only_input_is_not_applicable_and_never_complete(tmp_path):
    p, sid, _ = legacy_job(tmp_path)
    with p.db() as db:
        db.execute("UPDATE jobs SET status='pending',attempts=0,error_code=NULL")
    result = p.run_once({}, now=200, processor=lambda *_: pytest.fail('Unqualified input reached provider'))
    assert result['status'] == 'source_not_applicable'
    assert inspect(p)['status'] == 'source_not_applicable'
    assert read_context_index(p.index)['sessions'][0]['summary'] == {}
    assert p.progress()['not_applicable'] == 1
    assert p.progress()['failed'] == 0
    assert p.progress()['succeeded'] == 0
    assert p.claim(now=1000) is None


def test_missing_source_waits_without_publishing_or_automatic_retry(tmp_path):
    p, _, path = legacy_job(tmp_path)
    path.unlink()
    with p.db() as db:
        db.execute("UPDATE jobs SET status='pending',attempts=0,error_code=NULL")
    result = p.run_once({}, now=200, processor=lambda *_: pytest.fail('Missing input reached provider'))
    assert result['status'] == 'waiting_source'
    assert result['error_code'] == 'scenario_source_missing'
    assert p.progress()['waiting_source'] == 1
    assert p.claim(now=1000) is None


def test_dry_run_reclassifies_legacy_reason_without_any_state_writes(tmp_path):
    p, sid, _ = legacy_job(tmp_path, code='scenario_source_incomplete')
    before = {path.name: path.read_bytes() for path in p.root.iterdir() if path.is_file()}
    plan = reassess(p)
    assert plan['dry_run'] is True
    assert plan['model_calls'] == 0
    assert plan['changes'][0]['session_id'] == sid
    assert plan['changes'][0]['to_status'] == 'source_not_applicable'
    assert plan['changes'][0]['prior_error_code'] == 'scenario_source_incomplete'
    assert plan['changes'][0]['qualification']['reason'] == 'scenario_source_no_user_messages'
    assert before == {path.name: path.read_bytes() for path in p.root.iterdir() if path.is_file()}


def test_controlled_apply_retains_attempts_error_and_prior_revision_evidence(tmp_path):
    p, sid, _ = legacy_job(tmp_path, code='scenario_source_incomplete')
    plan = reassess(p, session_ids=[sid])
    applied = reassess(p, session_ids=[sid], apply=True, expected_plan_sha256=plan['plan_sha256'])
    assert applied['applied'] == 1
    row = inspect(p)
    assert row['status'] == 'source_not_applicable'
    assert row['attempts'] == 1
    assert row['error_code'] == 'scenario_source_incomplete'
    with p.db() as db:
        evidence = json.loads(db.execute('SELECT evidence FROM job_evidence WHERE session_id=?', (sid,)).fetchone()[0])
    assert evidence['prior']['status'] == 'failed'
    assert evidence['prior']['attempts'] == 1
    assert evidence['prior']['input_revision'] is None
    assert evidence['prior']['error_code'] == 'scenario_source_incomplete'
    assert reassess(p, session_ids=[sid])['changes'] == []


@pytest.mark.parametrize('kind', ['no_scope', 'wrong_digest', 'source_changed', 'job_changed'])
def test_reassessment_apply_rejects_unreviewed_or_stale_plan(tmp_path, kind):
    p, sid, path = legacy_job(tmp_path)
    plan = reassess(p, session_ids=[sid])
    args = dict(session_ids=[sid], apply=True, expected_plan_sha256=plan['plan_sha256'])
    if kind == 'no_scope':
        args['session_ids'] = None
    elif kind == 'wrong_digest':
        args['expected_plan_sha256'] = '0' * 64
    elif kind == 'source_changed':
        with path.open('a') as stream:
            stream.write(json.dumps({'type':'event_msg','payload':{'type':'token_count'}}) + '\n')
    else:
        with p.db() as db:
            db.execute('UPDATE jobs SET attempts=2')
    with pytest.raises(ValueError, match='reassessment_'):
        reassess(p, **args)
    assert inspect(p)['status'] == 'failed'


def test_coverage_exhausted_protected_and_complete_are_not_requeued(tmp_path):
    p, sid, _ = legacy_job(tmp_path, code='automated_source_coverage_incomplete', attempts=3, assistant=False)
    plan = reassess(p, session_ids=[sid])
    assert plan['changes'] == []
    assert plan['observations'][0]['failure_class'] == 'extraction_failed'
    assert plan['observations'][0]['remaining_attempts'] == 0
    for status in ('protected', 'complete', 'running'):
        with p.db() as db:
            db.execute('UPDATE jobs SET status=?', (status,))
        plan = reassess(p, session_ids=[sid])
        assert plan['changes'] == []
        assert inspect(p)['attempts'] == 3


def test_changed_source_reevaluates_not_applicable_and_retains_old_budget(tmp_path):
    p, sid, path = legacy_job(tmp_path)
    plan = reassess(p, session_ids=[sid])
    reassess(p, session_ids=[sid], apply=True, expected_plan_sha256=plan['plan_sha256'])
    # Restore a real user-led semantic source. The accepted entry must be
    # derived again; the prior attempt remains in immutable evidence.
    rollout(path.parent, sid, text='Review the new user request')
    p.discover(now=300)
    result = p.run_once({}, now=500, processor=lambda *_: (_ for _ in ()).throw(ValueError('scenario_state_invalid')))
    assert result['status'] == 'retrying'
    with p.db() as db:
        evidence = [json.loads(r[0]) for r in db.execute('SELECT evidence FROM job_evidence')]
    assert any(e['prior']['status'] == 'source_not_applicable' and e['prior']['attempts'] == 1 for e in evidence)


def test_cli_reassessment_default_is_read_only_and_needs_no_provider(tmp_path):
    p, sid, _ = legacy_job(tmp_path)
    result = subprocess.run([sys.executable, str(Path(__file__).with_name('context-incremental-worker.py')),
        '--state-root', str(p.root.parent), '--session-root', str(p.session_root),
        '--reassess-source', '--session-id', sid], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['dry_run'] is True
    assert inspect(p)['status'] == 'failed'


def test_classification_apply_revalidates_source_at_index_commit(tmp_path, monkeypatch):
    import lib.context_incremental as module
    p, sid, path = legacy_job(tmp_path)
    plan = reassess(p, session_ids=[sid])
    update = module.update_context_index
    def race(index_path, updater):
        rollout(path.parent, sid, text='A real user request arrived concurrently')
        return update(index_path, updater)
    monkeypatch.setattr(module, 'update_context_index', race)
    with pytest.raises(ValueError, match='reassessment_source_changed'):
        reassess(p, session_ids=[sid], apply=True, expected_plan_sha256=plan['plan_sha256'])
    assert inspect(p)['status'] == 'failed'
    assert read_context_index(p.index)['sessions'][0]['status'] == 'raw_available_summary_pending'


@pytest.mark.parametrize('settings', [
    {'modules': {'scenario_summary': {'record': False}}},
    {'routing': {'ep_enabled': False}},
])
def test_disabled_recording_prevents_reassessment_apply(tmp_path, settings):
    p, sid, _ = legacy_job(tmp_path)
    plan = reassess(p, session_ids=[sid])
    config = p.root.parent / 'config/runtime-settings.json'
    write_runtime_settings(p.root.parent, settings)
    with pytest.raises(ValueError, match='reassessment_recording_disabled'):
        reassess(p, session_ids=[sid], apply=True, expected_plan_sha256=plan['plan_sha256'])
    assert inspect(p)['status'] == 'failed'


def test_duplicate_and_unknown_ids_do_not_expand_reassessment_scope(tmp_path):
    p, sid, _ = legacy_job(tmp_path)
    unknown = str(uuid.uuid4())
    plan = reassess(p, session_ids=[sid, sid, unknown])
    assert len(plan['observations']) == 1
    assert len(plan['changes']) == 1
    assert plan['session_ids'] == sorted([sid, unknown])


def test_not_applicable_progress_never_claims_summary_success(tmp_path):
    p, sid, _ = legacy_job(tmp_path)
    plan = reassess(p, session_ids=[sid])
    reassess(p, session_ids=[sid], apply=True, expected_plan_sha256=plan['plan_sha256'])
    snapshot = p.progress()
    assert snapshot['succeeded'] == 0
    assert snapshot['status'] == 'partial_not_applicable'


def test_unknown_source_shape_is_waiting_not_paid_failure():
    from lib.context_incremental import qualify_summary_input
    result = qualify_summary_input({'status':'unknown', 'messages':[], 'source_revision':None})
    assert result['status'] == 'waiting_source'
    assert result['provider_calls'] == 0


def test_legacy_schema_dry_run_and_classification_preserve_unknown_input_budget(tmp_path):
    p, sid, _ = legacy_job(tmp_path)
    with p.db() as db:
        for name in ('input_revision', 'prior_status', 'source_check_pending', 'input_qualification'):
            db.execute('ALTER TABLE jobs DROP COLUMN ' + name)
    plan = reassess(p, session_ids=[sid])
    assert plan['changes'][0]['to_status'] == 'source_not_applicable'
    reassess(p, session_ids=[sid], apply=True, expected_plan_sha256=plan['plan_sha256'])
    assert inspect(p)['attempts'] == 1
    assert inspect(p)['error_code'] == 'scenario_source_no_user_messages'


def test_new_eligible_source_replaces_old_qualification_after_success(tmp_path):
    p, sid, path = legacy_job(tmp_path)
    plan = reassess(p, session_ids=[sid])
    reassess(p, session_ids=[sid], apply=True, expected_plan_sha256=plan['plan_sha256'])
    rollout(path.parent, sid, text='Review an actual new user request')
    p.discover(now=300)
    def accepted(source, row, *_):
        return {**row, 'status':'model_reviewed', 'summary':{'compact':'Fixture reviewed summary'},
            'source_revision':source['source_revision']}
    assert p.run_once({}, now=500, processor=accepted)['status'] == 'complete'
    row = read_context_index(p.index)['sessions'][0]
    assert row['input_qualification']['status'] == 'eligible'
    assert json.loads(inspect(p)['input_qualification'])['status'] == 'eligible'


def test_current_waiting_state_overrides_retained_historical_no_user_error(tmp_path):
    p, sid, path = legacy_job(tmp_path)
    path.unlink()
    plan = reassess(p, session_ids=[sid])
    reassess(p, session_ids=[sid], apply=True, expected_plan_sha256=plan['plan_sha256'])
    assert inspect(p)['status'] == 'waiting_source'
    assert inspect(p)['error_code'] == 'scenario_source_no_user_messages'
    snapshot = p.progress()
    assert snapshot['failure_classes'].get('waiting_source') == 1
    assert snapshot['failure_classes'].get('not_applicable', 0) == 0


@pytest.mark.parametrize('mutation', ['record_disabled', 'ep_disabled', 'generation_changed', 'missing', 'invalid_json'])
def test_reviewer_permission_race_at_commit_rolls_back_without_publication(tmp_path, monkeypatch, mutation):
    import lib.context_incremental as module
    p, sid, _ = legacy_job(tmp_path)
    plan = reassess(p, session_ids=[sid])
    before_index = p.index.read_bytes()
    update = module.update_context_index
    def race(index_path, updater):
        config = p.root.parent / 'config/runtime-settings.json'
        if mutation == 'missing':
            config.unlink()
        elif mutation == 'invalid_json':
            config.write_text('{broken settings')
        else:
            override = {'record_disabled': {'modules':{'scenario_summary':{'record':False}}},
                'ep_disabled': {'routing':{'ep_enabled':False}}, 'generation_changed': {'generation':2}}[mutation]
            write_runtime_settings(p.root.parent, override)
        return update(index_path, updater)
    monkeypatch.setattr(module, 'update_context_index', race)
    with pytest.raises(ValueError, match='reassessment_'):
        reassess(p, session_ids=[sid], apply=True, expected_plan_sha256=plan['plan_sha256'])
    assert inspect(p)['status'] == 'failed'
    assert p.index.read_bytes() == before_index
    with p.db() as db:
        assert db.execute('SELECT count(*) FROM job_evidence').fetchone()[0] == 0


@pytest.mark.parametrize('value', [None, '{invalid-json', [],
    {'routing':None, 'modules':{'scenario_summary':[]}},
    {'routing':{'ep_enabled':True}, 'modules':{'scenario_summary':{'record':'true'}}},
    {'routing':{'ep_enabled':True}, 'modules':{}},
])
def test_missing_or_malformed_permission_cannot_authorize_apply(tmp_path, value):
    p, sid, _ = legacy_job(tmp_path)
    plan = reassess(p, session_ids=[sid])
    config = p.root.parent / 'config/runtime-settings.json'
    if value is None:
        config.unlink()
    elif isinstance(value, str):
        config.write_text(value)
    else:
        config.write_text(json.dumps(value))
    with pytest.raises(ValueError, match='reassessment_'):
        reassess(p, session_ids=[sid], apply=True, expected_plan_sha256=plan['plan_sha256'])
    assert inspect(p)['status'] == 'failed'


def test_permission_is_freshly_read_after_acquiring_context_lock(tmp_path, monkeypatch):
    from contextlib import contextmanager
    import lib.context_incremental as module
    p, sid, _ = legacy_job(tmp_path)
    plan = reassess(p, session_ids=[sid])
    lock = module.context_index_lock
    @contextmanager
    def race(path):
        with lock(path):
            write_runtime_settings(p.root.parent, {'modules':{'scenario_summary':{'record':False}}})
            yield
    monkeypatch.setattr(module, 'context_index_lock', race)
    with pytest.raises(ValueError, match='reassessment_'):
        reassess(p, session_ids=[sid], apply=True, expected_plan_sha256=plan['plan_sha256'])
    assert inspect(p)['status'] == 'failed'


def test_runtime_generation_is_part_of_reviewed_plan_not_old_source_signature(tmp_path):
    p, sid, _ = legacy_job(tmp_path)
    first = reassess(p, session_ids=[sid])
    write_runtime_settings(p.root.parent, {'generation':2})
    second = reassess(p, session_ids=[sid])
    assert first['observations'][0]['input_revision'] == second['observations'][0]['input_revision']
    assert first['plan_sha256'] != second['plan_sha256']


def test_latest_protected_job_cannot_be_overridden_by_old_plan_or_signature(tmp_path):
    p, sid, _ = legacy_job(tmp_path)
    plan = reassess(p, session_ids=[sid])
    with p.db() as db:
        db.execute("UPDATE jobs SET status='protected',error_code='scenario_knowledge_write_prohibited'")
    with pytest.raises(ValueError, match='reassessment_stale_plan'):
        reassess(p, session_ids=[sid], apply=True, expected_plan_sha256=plan['plan_sha256'])
    assert inspect(p)['status'] == 'protected'
    assert inspect(p)['attempts'] == 1
    assert reassess(p, session_ids=[sid])['changes'] == []


def test_latest_source_write_prohibition_is_checked_again_at_commit(tmp_path, monkeypatch):
    import lib.context_incremental as module
    p, sid, path = legacy_job(tmp_path)
    plan = reassess(p, session_ids=[sid])
    before_index = p.index.read_bytes()
    update = module.update_context_index
    def race(index_path, updater):
        rollout(path.parent, sid, text='不要写入记忆。')
        return update(index_path, updater)
    monkeypatch.setattr(module, 'update_context_index', race)
    with pytest.raises(ValueError, match='reassessment_source_changed'):
        reassess(p, session_ids=[sid], apply=True, expected_plan_sha256=plan['plan_sha256'])
    assert inspect(p)['status'] == 'failed'
    assert inspect(p)['attempts'] == 1
    assert p.index.read_bytes() == before_index


def test_same_configured_generation_but_changed_settings_bytes_invalidates_plan(tmp_path):
    p, sid, _ = legacy_job(tmp_path)
    plan = reassess(p, session_ids=[sid])
    write_runtime_settings(p.root.parent, {'updated_at':'2026-10-09T01:00:00Z'})
    assert reassess(p, session_ids=[sid])['runtime_permission']['configured_generation'] == 1
    with pytest.raises(ValueError, match='reassessment_stale_plan'):
        reassess(p, session_ids=[sid], apply=True, expected_plan_sha256=plan['plan_sha256'])
    assert inspect(p)['status'] == 'failed'


def test_empty_change_plan_still_rejects_permission_generation_changed_in_lock(tmp_path, monkeypatch):
    import lib.context_incremental as module
    p, sid, _ = legacy_job(tmp_path)
    with p.db() as db:
        db.execute("UPDATE jobs SET status='protected'")
    plan = reassess(p, session_ids=[sid])
    assert plan['changes'] == []
    build_plan = module._reassessment_plan
    def race(*args):
        result = build_plan(*args)
        write_runtime_settings(p.root.parent, {'generation':2})
        return result
    monkeypatch.setattr(module, '_reassessment_plan', race)
    with pytest.raises(ValueError, match='reassessment_runtime_permission_changed'):
        reassess(p, session_ids=[sid], apply=True, expected_plan_sha256=plan['plan_sha256'])
    assert inspect(p)['status'] == 'protected'
