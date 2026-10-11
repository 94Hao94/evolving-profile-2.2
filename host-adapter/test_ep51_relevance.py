import pytest
from lib.recall_relevance import apply_relevance_policy


@pytest.mark.parametrize('subject', ['Nebula', 'Orion', '青岚平台'])
def test_provenance_context_fills_omitted_subject_without_admitting_other_topics(subject):
    witness = {'status': 'source_read', 'session_id': 'session-a', 'source_revision': 'rev-a',
               'locators': [{'raw_line_sha256': 'hash-a', 'source_path': '/raw/session-a.jsonl', 'byte_offset': 12}],
               'text': f'{subject} 部署与接口验收版本演进'}
    related = {'id': 'related', 'text': '接口验收仍有缺口，部署后需要补读运行日志。', 'source_context': witness}
    unrelated = {'id': 'other', 'text': 'Potato soup tastes delicious.', 'source_context': witness}
    kept, _ = apply_relevance_policy(f'{subject} 版本演进', [related, unrelated], 'weak')
    assert [row['id'] for row in kept] == ['related']
    assert kept[0]['relevance_match_signals']['source_context_subject_support']
    assert kept[0]['relevance_level'] == 'weak'


def test_unsigned_metadata_cannot_supply_subject_or_explicit_literal():
    record = {'id': 'r', 'text': 'API endpoint deployment remains unverified.',
              'source_context': {'status': 'source_read', 'text': 'Nebula architecture'},
              'metadata': {'query': 'Nebula architecture'}}
    assert apply_relevance_policy('Nebula architecture', [record])[0] == []
    record['source_context'].update(session_id='s', source_revision='r', locators=[{'raw_line_sha256': 'h'}])
    assert apply_relevance_policy('Find exact identifier `NEBULA_888`', [record])[0] == []


def test_context_about_another_subject_cannot_borrow_shared_version_words():
    witness = {'status':'source_read','session_id':'s','source_revision':'r',
        'locators':[{'source_path':'raw','byte_offset':0,'raw_line_sha256':'hash'}],
        'text':'Orion 版本演进：接口验收与部署'}
    row = {'text':'接口验收仍有缺口，部署后需要补读运行日志。','source_context':witness}
    assert apply_relevance_policy('Nebula 版本演进', [row])[0] == []


def test_version_family_context_retains_middle_version_as_weak_background():
    witness = {'status':'source_read','session_id':'s','source_revision':'r',
        'locators':[{'source_path':'raw','byte_offset':0,'raw_line_sha256':'hash'}],
        'text':'ORION5.0 接口验收与部署；工具已经部署，接口验收仍有缺口。'}
    row = {'text':'接口验收仍有缺口，部署后需要补读运行日志。','source_context':witness}
    kept, _ = apply_relevance_policy('ORION4.0 至 ORION5.1 演进', [row])
    assert len(kept) == 1
    assert kept[0]['relevance_level'] == 'weak'


def test_actual_execution_case_ranks_ahead_of_mechanism_discussion_but_both_remain():
    records = [
        {'id': 'discussion', 'kind': 'pattern', 'text': '讨论网页配置保存故障修复经验，设计如何提炼 Skill 和验证机制。'},
        {'id': 'case', 'kind': 'episode', 'phase': 'recover', 'outcome': 'recovered',
         'failure_signature': ['保存响应错误'], 'repair_actions': ['绑定配置请求'],
         'verification_evidence': [{'verifier_kind': 'automated_test', 'status': 'passed'}],
         'text': '配置保存报错，修复请求绑定并运行测试通过。'},
    ]
    kept, _ = apply_relevance_policy('网页配置保存故障修复经验', records)
    assert [r['id'] for r in kept] == ['case', 'discussion']
    assert kept[0]['candidate_role'] == 'execution_case'
    assert kept[1]['candidate_role'] == 'mechanism_discussion'
