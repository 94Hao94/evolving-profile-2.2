"""Historical scenario navigation must not drift to the latest turn."""
import json
import pytest


def test_source_scenario_locator_reads_old_turn_not_latest_stage(tmp_path, monkeypatch):
    import evolving_profile_controller_mcp as mcp
    sid = '00000000-0000-4000-8000-000000000041'
    root = tmp_path / 'sessions'; root.mkdir()
    rows = []
    for turn, text in [('old-stage', 'Old repair used fixed SVG coordinates'), ('new-stage', 'Later unrelated budget planning')]:
        rows += [{'type': 'turn_context', 'payload': {'turn_id': turn}},
                 {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': [{'text': text}]}}]
    (root / f'rollout-test-{sid}.jsonl').write_text('\n'.join(json.dumps(row) for row in rows) + '\n')
    index = tmp_path / 'index.json'; index.write_text(json.dumps({'schema': 'evolving-profile.context-index.v1', 'sessions': [], 'projects': []}))
    monkeypatch.setattr(mcp, 'CONTEXT_INDEX_PATH', index)
    monkeypatch.setattr(mcp, 'THREAD_SESSION_ROOT', root)
    monkeypatch.setattr(mcp.Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(mcp, 'runtime_disabled', lambda *args: None)
    reply = json.loads(mcp.read_context_summary({'scenario_type': 'session', 'scenario_id': f'session:{sid}/turn/old-stage', 'tier': 'full'})['content'][0]['text'])
    assert len(reply['items']) == 1
    assert 'fixed SVG' in reply['items'][0]['summary']
    assert 'budget planning' not in reply['items'][0]['summary']
    assert reply['items'][0]['anchor_turn_id'] == 'old-stage'
    assert reply['status'] == 'available_unreviewed'


def test_unresolved_process_locator_keeps_precise_source_turn(tmp_path, monkeypatch):
    import evolving_profile_controller_mcp as mcp
    index = tmp_path / 'index.json'; index.write_text(json.dumps({'schema': 'evolving-profile.context-index.v1', 'sessions': [], 'projects': []}))
    monkeypatch.setattr(mcp, 'CONTEXT_INDEX_PATH', index)
    result = mcp.agent_process_scenario_followup([{'process_memory_id': 'p1', 'primary_context': {
        'session_id': '00000000-0000-4000-8000-000000000041', 'turn_id': 'old-stage'}}])
    locator = result['unresolved_contexts'][0]['source_scenario_id']
    assert locator == 'session:00000000-0000-4000-8000-000000000041/turn/old-stage'
    assert result['summary_text_included'] is False


def test_unknown_turn_never_falls_back_to_latest_session_messages(tmp_path, monkeypatch):
    from lib.raw_session_evidence import read_evidence
    sid = '00000000-0000-4000-8000-000000000041'
    root = tmp_path / 'sessions'; root.mkdir()
    (root / f'rollout-test-{sid}.jsonl').write_text(json.dumps({'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': [{'text': 'unrelated current text'}]}}) + '\n')
    result = read_evidence(sid, root, turn_id='missing', cache_root=tmp_path / 'cache')
    assert result['status'] == 'source_empty'
    assert not result['source']['text']


def test_verified_project_does_not_discard_missing_session_turn_locator(tmp_path, monkeypatch):
    import evolving_profile_controller_mcp as mcp
    index = tmp_path / 'index.json'; index.write_text(json.dumps({'schema': 'evolving-profile.context-index.v1', 'sessions': [], 'projects': [{
        'context_id': 'project:p1', 'context_type': 'project', 'project_key': 'p1', 'identity_status': 'verified_project'}]}))
    monkeypatch.setattr(mcp, 'CONTEXT_INDEX_PATH', index)
    reply = mcp.agent_process_scenario_followup([{'process_memory_id': 'p1', 'primary_context': {
        'session_id': '00000000-0000-4000-8000-000000000041', 'turn_id': 'old-stage', 'project_key': 'p1'}}])
    assert reply['scenarios'][0]['source_scenario_ids'] == ['session:00000000-0000-4000-8000-000000000041/turn/old-stage']


def test_judge_gets_unread_candidate_and_raw_readback_coverage():
    from unittest.mock import patch
    from lib.jev_judge import project_tool_review
    state = {}
    with patch('lib.jev_judge.review', side_effect=lambda p, s: state.update(s) or {}):
        project_tool_review('agent_recall', {}, {'returned_count': 4, 'total_count': 144, 'next_offset': 4,
            'records': [{'process_memory_id': 'p1', 'text': 'candidate'}]}, {})
    assert state['candidate_coverage'] == {'returned_count': 4, 'eligible_count': 144, 'next_offset': 4}
    assert state['source_readback'] is False


def test_source_turn_locator_rejects_conflicting_explicit_session(monkeypatch):
    import evolving_profile_controller_mcp as mcp
    monkeypatch.setattr(mcp, 'runtime_disabled', lambda *args: None)
    with pytest.raises(ValueError, match='conflicting_session'):
        mcp.read_context_summary({'scenario_type': 'session',
            'scenario_id': 'session:00000000-0000-4000-8000-000000000041/turn/old-stage',
            'session_id': '00000000-0000-4000-8000-000000000042'})


def test_explicit_project_scope_is_not_ignored_by_raw_fallback(tmp_path, monkeypatch):
    import evolving_profile_controller_mcp as mcp
    sid = '00000000-0000-4000-8000-000000000041'
    root = tmp_path / 'sessions'; root.mkdir()
    (root / f'rollout-test-{sid}.jsonl').write_text(json.dumps({'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': [{'text': 'Some other project'}]}}) + '\n')
    index = tmp_path / 'index.json'; index.write_text(json.dumps({'schema': 'evolving-profile.context-index.v1', 'sessions': [], 'projects': []}))
    monkeypatch.setattr(mcp, 'CONTEXT_INDEX_PATH', index)
    monkeypatch.setattr(mcp, 'THREAD_SESSION_ROOT', root)
    monkeypatch.setattr(mcp.Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(mcp, 'runtime_disabled', lambda *a: None)
    r = json.loads(mcp.read_context_summary({'scenario_type': 'session', 'session_id': sid, 'project_key': 'target-project'})['content'][0]['text'])
    assert r['items'] == []
    assert r['raw_source_status'] == 'project_scope_unresolved'


def test_agent_search_reply_exposes_candidate_total_distinct_from_returned_page():
    import evolving_profile_controller_mcp as mcp
    r = json.loads(mcp._process_reply({'source': 'agent_process_memory', 'records': [{'process_memory_id': 'p1'}],
        'total_count': 80, 'returned_count': 1})['content'][0]['text'])
    assert r['candidate_count'] == 80
    assert r['returned_count'] == 1
    assert r['candidate_count_semantics'] == 'policy_eligible_total_not_visible_page'
