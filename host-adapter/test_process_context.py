import json
import uuid
from lib.process_context import resolve_source_metadata


def test_exact_turn_metadata_never_uses_latest_model_from_another_turn(tmp_path):
    sid = str(uuid.uuid4()); path = tmp_path / f'rollout-day-{sid}.jsonl'
    rows = [{'type': 'turn_context', 'payload': {'turn_id': 'a', 'model': 'model-a', 'cwd': '/a'}},
            {'type': 'turn_context', 'payload': {'turn_id': 'b', 'model': 'model-b', 'cwd': '/b'}}]
    path.write_text(''.join(json.dumps(row)+'\n' for row in rows))
    binding = {'state': 'prompt_bound', 'session_id': sid, 'turn_id': 'a'}
    result = resolve_source_metadata(binding, tmp_path)
    assert result['model'] == 'model-a'
    assert result['cwd'] == '/a'
    assert result['raw_context_locator']['byte_offset'] == 0
    assert resolve_source_metadata({**binding, 'state': 'stale_prompt_binding'}, tmp_path) == {}
    assert resolve_source_metadata({**binding, 'turn_id': 'missing'}, tmp_path) == {}
