import json
import recall


def test_ingress_records_exact_native_context_without_credentials(tmp_path):
    path = tmp_path/'ingress.jsonl'
    recall.record_prompt_ingress({'promptIngressAuditPath':str(path)}, 'Review project', 's', '/project',
        turn_id='t',hook_invocation_id='c',transcript_path='/raw/s.jsonl',model='actual-model',model_provider='configured-provider')
    row = json.loads(path.read_text())
    assert row['cwd'] == '/project'
    assert row['transcript_path'] == '/raw/s.jsonl'
    assert row['model'] == 'actual-model'
    assert row['model_provider'] == 'configured-provider'
