import importlib.util
from pathlib import Path

spec=importlib.util.spec_from_file_location('binding_status',Path(__file__).with_name('evolving_profile_status_server.py'))
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

def test_matching_hook_cannot_override_explicit_turn_or_nested_identity_conflict():
    prompt={'hook_invocation_id':'h','session_id':'s','turn_id':'t'}
    for event in [{'check_id':'h','session_id':'s','turn_id':'other'},
                  {'check_id':'h','hook_invocation_id':'wrong','session_id':'s','turn_id':'t'}]:
        assert module._bound_to_prompt(event,prompt,[]) is False
    assert module._bound_to_prompt({'check_id':'h','session_id':'s','turn_id':'t'},prompt,[]) is True
