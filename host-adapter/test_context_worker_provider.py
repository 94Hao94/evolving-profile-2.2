import importlib.util
import json
from pathlib import Path
import pytest


def worker():
    spec = importlib.util.spec_from_file_location('context_worker',Path(__file__).with_name('context-incremental-worker.py'))
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def test_worker_uses_saved_ep_primary_override_and_profile_field_fallback(tmp_path):
    env = tmp_path/'api.env'; settings = tmp_path/'settings.json'
    env.write_text('EVOLVING_PROFILE_API_LLM_MODEL=profile-model\nEVOLVING_PROFILE_API_LLM_BASE_URL=https://existing-provider/v1\nEVOLVING_PROFILE_API_LLM_API_KEY=fixture-secret\n')
    settings.write_text(json.dumps({'providers':{'primary':{'model':'saved-active-model'}}}))
    config = worker().load_provider(env,settings)
    assert config['EVOLVING_PROFILE_API_LLM_MODEL'] == 'saved-active-model'
    assert config['EVOLVING_PROFILE_API_LLM_BASE_URL'] == 'https://existing-provider/v1'
    assert config['EVOLVING_PROFILE_API_LLM_API_KEY'] == 'fixture-secret'


def test_runtime_primary_can_be_complete_without_profile_and_missing_config_fails_closed(tmp_path):
    settings = tmp_path/'settings.json'
    settings.write_text(json.dumps({'providers':{'primary':{'model':'ep-provider-model',
        'base_url':'https://configured/v1','api_key':'fixture-key'}}}))
    assert worker().load_provider(tmp_path/'absent.env',settings)['EVOLVING_PROFILE_API_LLM_MODEL'] == 'ep-provider-model'
    with pytest.raises(ValueError,match='ep_provider_configuration_unavailable'):
        worker().load_provider(tmp_path/'absent.env',tmp_path/'absent.json')
