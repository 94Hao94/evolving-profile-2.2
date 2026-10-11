import copy
import json

import pytest

from lib.context_associations import project_key
from lib.context_incremental import ContextIncremental
from lib.context_summary import build_session_context, read_context_index, write_context_index
from lib.memory_recovery_scenario import publish_automated_session
from lib.scenario_source import read_session_source, revision_for_messages
from test_memory_recovery_scenario import fixture, coverage
from test_context_incremental import write_runtime_settings


def accepted_with_missing_metadata(tmp_path, extra_user_text=''):
    prefix = '<external_codex_apps_open_page>{"page_id":"historical-page"}</external_codex_apps_open_page>'
    root, path, source, bundle, review = fixture(tmp_path, prefix=prefix + extra_user_text)
    row = publish_automated_session(build_session_context(source['thread_id'], 'project', [], ''),
        source, bundle, review, coverage(source, bundle), root)
    row.update(project_key=project_key('/project'), bank_id='bank-a')
    row.pop('context_metadata_exclusions', None)
    row.pop('source_record_coverage', None)
    return root, path, source, row


def test_publisher_retains_source_normalization_evidence(tmp_path):
    prefix = '<external_codex_apps_open_page>{"page_id":"new-page"}</external_codex_apps_open_page>'
    root, _, source, bundle, review = fixture(tmp_path, prefix=prefix)
    row = publish_automated_session({}, source, bundle, review, coverage(source, bundle), root)
    assert row.get('context_metadata_exclusions') == source['context_metadata_exclusions']
    assert row.get('source_record_coverage') == source['source_record_coverage']


def test_old_missing_metadata_is_completed_only_from_exact_reviewed_source(tmp_path):
    root, path, source, row = accepted_with_missing_metadata(tmp_path)
    p = ContextIncremental(tmp_path/'state', root, bank_id='bank-a')
    write_context_index(p.index, [row], [])
    p.discover(now=100)
    result = p.run_once({}, now=200, processor=lambda *_: pytest.fail('Same reviewed source reached provider'))
    assert result['status'] == 'complete'
    assert result['provider_calls'] == 0
    restored = read_context_index(p.index)['sessions'][0]
    assert restored['episodes'] == row['episodes']
    assert restored['summary'] == row['summary']
    assert restored['context_metadata_exclusions'] == source['context_metadata_exclusions']
    assert restored['summary_reuse']['metadata_completion']['new_semantic_review'] is False


@pytest.mark.parametrize('mutation', ['source_files', 'roles', 'explicit_exclusions', 'no_review', 'new_revision', 'message_hash'])
def test_metadata_completion_cannot_mask_invalid_review_binding(tmp_path, mutation):
    import lib.memory_recovery_scenario as module
    _, _, source, row = accepted_with_missing_metadata(tmp_path)
    candidate, live = copy.deepcopy(row), copy.deepcopy(source)
    if mutation == 'source_files':
        candidate['raw_source_files'] = ['/another/source.jsonl']
    elif mutation == 'roles':
        live['messages'][0]['role'] = 'assistant'
    elif mutation == 'explicit_exclusions':
        candidate['context_metadata_exclusions'] = []
    elif mutation == 'no_review':
        candidate['review_model'] = None
    elif mutation == 'new_revision':
        live['source_revision'] = 'f' * 64
    else:
        live['messages'][0]['raw_line_sha256'] = 'f' * 64
    helper = getattr(module, 'complete_same_source_metadata', None)
    assert callable(helper), 'Same-source metadata completion guard is missing'
    assert helper(candidate, live) is None


def test_exclusions_are_part_of_actual_source_revision(tmp_path):
    _, _, source, _ = accepted_with_missing_metadata(tmp_path)
    assert source['context_metadata_exclusions']
    assert revision_for_messages(source['messages'], source['context_metadata_exclusions']) == source['source_revision']
    assert revision_for_messages(source['messages'], []) != source['source_revision']


def test_explicit_metadata_disagreement_is_not_overwritten_by_reuse(tmp_path):
    root, _, source, row = accepted_with_missing_metadata(tmp_path)
    row['context_metadata_exclusions'] = []
    p = ContextIncremental(tmp_path/'state', root, bank_id='bank-a')
    write_context_index(p.index, [row], [])
    p.discover(now=100)
    result = p.run_once({}, now=200, processor=lambda *_: (_ for _ in ()).throw(ValueError('scenario_state_invalid')))
    assert result.get('summary_reused') is not True
    assert read_context_index(p.index)['sessions'][0]['status'] != 'episode_directory_ready'


def test_recovery_plan_reuses_completed_evidence_without_replenishing_attempts(tmp_path):
    from lib.context_incremental import reassess_source_jobs, _input_revision
    root, path, source, row = accepted_with_missing_metadata(tmp_path)
    p = ContextIncremental(tmp_path/'state', root, bank_id='bank-a')
    write_runtime_settings(p.root.parent)
    write_context_index(p.index, [row], [])
    p.discover(now=100)
    current = read_context_index(p.index)['sessions'][0]
    current.update(status='raw_available_summary_failed', error_code='scenario_accepted_summary_unavailable')
    write_context_index(p.index, [current], [])
    with p.db() as db:
        db.execute("UPDATE jobs SET status='failed',attempts=3,error_code='scenario_accepted_summary_unavailable',input_revision=?",
                   (_input_revision(source, '/project', 'bank-a'),))
    plan = reassess_source_jobs(p.root.parent, root, session_ids=[source['thread_id']])
    assert plan['changes'][0]['action'] == 'reuse_reviewed_summary'
    assert plan['changes'][0]['metadata_completion']['new_semantic_review'] is False
    applied = reassess_source_jobs(p.root.parent, root, session_ids=[source['thread_id']],
        apply=True, expected_plan_sha256=plan['plan_sha256'])
    assert applied['applied'] == 1
    with p.db() as db:
        job = dict(db.execute('SELECT status,attempts,error_code FROM jobs').fetchone())
    assert job == {'status':'complete', 'attempts':3, 'error_code':None}
    restored = read_context_index(p.index)['sessions'][0]
    assert restored['episodes'] == row['episodes']
    # A normal transport append and discovery cannot regress the recovery.
    with path.open('a') as stream:
        stream.write(json.dumps({'type':'event_msg','payload':{'type':'token_count'}}) + '\n')
    p.discover(now=300)
    assert p.run_once({}, now=500, processor=lambda *_: pytest.fail('Repeated source review'))['status'] == 'complete'
    with p.db() as db:
        assert db.execute('SELECT attempts FROM jobs').fetchone()[0] == 3


def test_same_reviewed_hash_does_not_override_current_write_prohibition(tmp_path):
    from lib.memory_recovery_scenario import complete_same_source_metadata
    root, _, source, row = accepted_with_missing_metadata(tmp_path, extra_user_text='不要写入记忆。')
    assert complete_same_source_metadata(row, source) is None
    p = ContextIncremental(tmp_path/'state', root, bank_id='bank-a')
    write_context_index(p.index, [row], [])
    p.discover(now=100)
    result = p.run_once({}, now=200, processor=lambda *_: pytest.fail('Protected source reached provider'))
    assert result['status'] == 'protected'
    assert result.get('summary_reused') is not True


def test_bank_mismatch_prevents_summary_reuse(tmp_path):
    root, _, _, row = accepted_with_missing_metadata(tmp_path)
    p = ContextIncremental(tmp_path/'state', root, bank_id='bank-b')
    write_context_index(p.index, [row], [])
    p.discover(now=100)
    result = p.run_once({}, now=200, processor=lambda *_: (_ for _ in ()).throw(ValueError('scenario_state_invalid')))
    assert result.get('summary_reused') is not True
    assert result['status'] == 'retrying'


def test_same_revision_with_changed_raw_byte_role_is_not_metadata_completion(tmp_path):
    from lib.memory_recovery_scenario import complete_same_source_metadata
    _, path, source, row = accepted_with_missing_metadata(tmp_path)
    lines = path.read_text().splitlines()
    raw = json.loads(lines[1])
    raw['payload']['role'] = 'assistant'
    lines[1] = json.dumps(raw)
    path.write_text('\n'.join(lines)+'\n')
    assert complete_same_source_metadata(row, source) is None
