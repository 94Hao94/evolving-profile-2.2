import json
import uuid
from urllib.parse import parse_qs, urlsplit

from evidence_workspace import search, read_page


def test_version_history_discovers_omitted_subject_through_actual_source_relation(tmp_path):
    seed, background, unrelated, denied, foreign, withdrawn = [str(uuid.uuid4()) for _ in range(6)]
    records = {
        seed: {'id': seed, 'state': 'valid', 'text': 'Nebula 5.0 release branch passed validation.'},
        background: {'id': background, 'state': 'valid', 'type': 'world', 'text': '接口验收仍有缺口，部署后需要补读运行日志。', 'document_id': 'd', 'chunk_id': 'c'},
        unrelated: {'id': unrelated, 'state': 'valid', 'type': 'world', 'text': 'Potato soup tastes delicious.', 'document_id': 'd', 'chunk_id': 'c'},
        denied: {'id': denied, 'state': 'valid', 'type': 'world', 'text': 'Nebula 5.0 deployment', 'scope_status': 'denied', 'document_id': 'd', 'chunk_id': 'c'},
        foreign: {'id': foreign, 'state': 'valid', 'type': 'world', 'text': 'Nebula 5.0 deployment', 'document_id': 'other', 'chunk_id': 'other'},
        withdrawn: {'id': withdrawn, 'state': 'invalidated', 'type': 'world', 'text': 'Nebula 5.0 deployment', 'document_id': 'd', 'chunk_id': 'c'},
    }
    def api(path, body=None, timeout=8):
        if path.endswith('/recall'): return {'results': [records[seed]]}
        if path.endswith('/sources/search'):
            assert body['match'] == 'any'
            assert 'Nebula5.0' in body['terms']
            return {'items': [{'document_id': 'd', 'chunk_id': 'c', 'text': 'Nebula 5.0 接口验收部署仍有缺口。'}], 'next_cursor': None, 'scanned_chunks': 1}
        if '/memories/list?' in path:
            assert parse_qs(urlsplit(path).query)['document_id'] == ['d']
            return {'items': list(records.values())[1:], 'total': 5}
        if path.endswith('/chunks/c'):
            return {'bank_id': 'bank', 'document_id': 'd', 'chunk_id': 'c', 'chunk_text': 'Nebula 版本演进：接口验收与部署仍有缺口，补读运行日志。'}
        return records[path.rsplit('/', 1)[-1]]
    result = search('bank', 'Nebula 版本演进', api, tmp_path, page_size=1, types=['world', 'experience'], relevance_policy='weak')
    state = json.loads((tmp_path / (result['research_id'] + '.json')).read_text())
    assert background in state['raw_memory_ids']
    assert background in state['memory_ids']
    assert not {unrelated, denied, foreign, withdrawn} & set(state['memory_ids'])
    assert foreign not in state['raw_memory_ids']
    assert state['history_discovery']['status'] == 'bounded_source_discovery'
    assert state['history_discovery']['main_query'] == 'Nebula 版本演进'
    assert result['tool_call_count'] == 3
    assert result['next_offset'] == 1
    records[background]['permission_status'] = 'denied'
    denied_page = read_page('bank', result['research_id'], state['memory_ids'].index(background), api, tmp_path, page_size=1, relevance_policy='weak')
    assert denied_page['memories'] == []
    records[background].pop('permission_status')
    records[background]['state'] = 'invalidated'
    page = read_page('bank', result['research_id'], state['memory_ids'].index(background), api, tmp_path, page_size=1, relevance_policy='weak')
    assert page['memories'] == []
    assert page['unavailable'][0]['status'] == 'withdrawn'


def test_bilingual_history_has_same_discovery_but_exact_identifier_does_not(tmp_path):
    from lib.history_discovery import observed_version_terms
    rows = [{'text': '星河 v3.2 deployed. Nebula5.0 release; Nebula 4.0 previous release.'}]
    assert observed_version_terms('星河 版本演进', rows) == [['星河v3.2', '星河 v3.2']]
    assert observed_version_terms('Nebula version evolution', rows) == [['Nebula5.0', 'Nebula 5.0'], ['Nebula4.0', 'Nebula 4.0']]
    assert observed_version_terms('Garden version evolution', rows) == []
    assert observed_version_terms('Find exact identifier `Nebula5.0`', rows) == []
    assert observed_version_terms('Nebula pagination bug', rows) == []
    assert observed_version_terms('Compare prior releases of Nebula', rows) == [['Nebula5.0', 'Nebula 5.0'], ['Nebula4.0', 'Nebula 4.0']]
    assert observed_version_terms('星河 先前发布对比', rows) == [['星河v3.2', '星河 v3.2']]
    assert observed_version_terms('Nebula version evolution', [{'text': '/projects/Nebula5.0/source.py Garden5.0 released'}]) == []
    assert observed_version_terms('Compare Nebula4.0 and Nebula5.0', rows) == [['Nebula5.0', 'Nebula 5.0'], ['Nebula4.0', 'Nebula 4.0']]
    assert observed_version_terms('Nebula4.0到Nebula5.0的版本演进', rows + [{'text':'Nebula6.0 released'}]) == [['Nebula5.0', 'Nebula 5.0'], ['Nebula4.0', 'Nebula 4.0']]
    for boundary in [{'permission_status':'denied'}, {'scope_status':'mismatch'}, {'hard_scope_match':False}]:
        assert observed_version_terms('Nebula version evolution', [{'text':'Nebula6.0 released', **boundary}]) == []
    assert observed_version_terms('Nebula previous releases excluding Nebula5.0', rows) == []
    assert observed_version_terms('Nebula版本演进，不要包含Nebula5.0', rows) == []
    assert observed_version_terms('Nebula version evolution', [{'text':'Nebula6.0 released','scope_verification':{'status':'denied'}}]) == []


def test_source_discovery_is_bounded_and_reports_unread_candidates():
    from lib.history_discovery import discover_history_sources
    calls = []
    def api(path, body=None, timeout=8):
        calls.append(path)
        if path.endswith('/sources/search'):
            return {'items': [{'document_id': 'd', 'chunk_id': 'c', 'text': 'Nebula 5.0'}], 'next_cursor': str(int(body.get('cursor') or 0) + 20), 'scanned_chunks': 20}
        return {'items': [], 'total': 0}
    rows, audit = discover_history_sources('bank', 'Nebula version evolution', [{'text': 'Nebula5.0 released'}], api, types=['world'])
    assert rows == []
    assert len(calls) == 4
    assert audit['coverage'] == 'partial'
    assert audit['source_receipts'][-1]['next_cursor'] == '60'
    assert audit['api_call_count'] == 4
    assert audit['continuation_hints'][0]['tool'] == 'find_sources'
    assert audit['continuation_hints'][0]['arguments'] == {'terms': ['Nebula5.0', 'Nebula 5.0'], 'match': 'any', 'role': 'any', 'limit': 20, 'cursor': '60'}


def test_failed_source_search_is_explicit_partial_not_successful_empty():
    from lib.history_discovery import discover_history_sources
    def api(*args, **kwargs): raise PermissionError('Bank read denied')
    rows, audit = discover_history_sources('bank', 'Nebula version evolution', [{'text': 'Nebula5.0 released'}], api)
    assert rows == []
    assert audit['coverage'] == 'partial'
    assert audit['source_receipts'][0]['status'] == 'failed'
    assert audit['source_receipts'][0]['error_type'] == 'PermissionError'


def test_source_documents_share_candidate_budget_without_one_document_monopoly():
    from lib.history_discovery import discover_history_sources
    ids = [str(uuid.uuid4()) for _ in range(4)]
    def api(path, body=None, timeout=8):
        if path.endswith('/sources/search'):
            return {'items': [{'document_id': d, 'chunk_id': d+'c', 'text': 'Nebula5.0'} for d in ['a','b']], 'next_cursor': None}
        did = parse_qs(urlsplit(path).query)['document_id'][0]
        selected = ids[:2] if did == 'a' else ids[2:]
        return {'items': [{'id': mid, 'document_id': did, 'chunk_id': did+'c', 'state':'valid', 'type':'world', 'text':'Nebula5.0 implemented'} for mid in selected], 'total':2}
    rows, _ = discover_history_sources('bank','Nebula version evolution',[{'text':'Nebula5.0 released'}],api)
    assert [row['id'] for row in rows] == [ids[0],ids[2],ids[1],ids[3]]


def test_new_source_candidates_label_soft_temporal_window_without_deleting_background():
    from lib.history_discovery import discover_history_sources
    ids = [str(uuid.uuid4()) for _ in range(3)]
    def api(path, body=None, timeout=8):
        if path.endswith('/sources/search'):
            return {'items':[{'document_id':'d','chunk_id':'c','text':'Nebula5.0'}],'next_cursor':None}
        return {'items':[
            {'id':mid,'document_id':'d','chunk_id':'c','state':'valid','text':'Nebula5.0 released',**date}
            for mid,date in zip(ids,[{'occurred_start':'2026-10-03T00:00:00Z'}, {'occurred_start':'2026-09-20T00:00:00Z'}, {}])], 'total':3}
    rows,audit = discover_history_sources('bank','Nebula version evolution',[{'text':'Nebula5.0 released'}],api,
        temporal_window={'start':'2026-10-01T00:00:00Z','end':'2026-10-09T00:00:00Z'})
    assert [row['id'] for row in rows] == ids
    assert [row['retrieval_temporal_relation'] for row in rows] == ['inside','outside','unknown']
    assert audit['outside_temporal_window_count'] == 1
    assert audit['unknown_temporal_count'] == 1
    assert audit['temporal_window']['start'] == '2026-10-01T00:00:00Z'


def test_live_permission_is_checked_without_relevance_policy(tmp_path):
    mid = str(uuid.uuid4())
    row = {'id':mid,'state':'valid','text':'Nebula deployment'}
    def api(path, body=None, timeout=8):
        return {'results':[row]} if path.endswith('/recall') else row
    first = search('bank','Nebula deployment',api,tmp_path)
    assert len(first['memories']) == 1
    row['permission_status']='denied'
    page = read_page('bank',first['research_id'],0,api,tmp_path)
    assert page['memories'] == []
    assert page['filtered'][0]['status'] == 'permission_denied'
    row.pop('permission_status')
    row['scope_verification']={'status':'denied'}
    assert read_page('bank',first['research_id'],0,api,tmp_path)['memories'] == []


def test_live_type_reclassification_obeys_manifest_without_source_failure(tmp_path):
    mid = str(uuid.uuid4())
    row = {'id':mid,'state':'valid','type':'world','text':'Nebula deployment'}
    def api(path, body=None, timeout=8):
        return {'results':[row]} if path.endswith('/recall') else row
    first=search('bank','Nebula deployment',api,tmp_path,types=['world'],relevance_policy='weak')
    manifest=json.loads((tmp_path/(first['research_id']+'.json')).read_text())
    assert manifest['types'] == ['world']
    row['type']='observation'
    page=read_page('bank',first['research_id'],0,api,tmp_path,relevance_policy='weak')
    assert page['memories'] == []
    assert page['filtered'][0]['status'] == 'type_reclassified'
    assert page['unavailable'] == []
    assert page['query_completion'] != 'source_coverage_incomplete'


def test_empty_type_list_retains_unrestricted_source_candidates():
    from lib.history_discovery import discover_history_sources
    mid=str(uuid.uuid4())
    def api(path,body=None,timeout=8):
        if path.endswith('/sources/search'): return {'items':[{'document_id':'d','chunk_id':'c','text':'Nebula5.0'}],'next_cursor':None}
        return {'items':[{'id':mid,'state':'valid','type':'observation','document_id':'d','chunk_id':'c','text':'Nebula API'}],'total':1}
    rows,_=discover_history_sources('bank','Nebula version evolution',[{'text':'Nebula5.0 released'}],api,types=[])
    assert [row['id'] for row in rows] == [mid]


def test_same_chunk_unrelated_topic_cannot_borrow_version_subject(tmp_path):
    seed, soup = str(uuid.uuid4()), str(uuid.uuid4())
    rows = {seed:{'id':seed,'state':'valid','text':'Nebula 5.0 version evolution: deployed the new API.'},
            soup:{'id':soup,'state':'valid','type':'world','document_id':'d','chunk_id':'c','text':'Potato soup tastes delicious.'}}
    chunk = 'Nebula 5.0 version evolution: deployed the new API.\nUnrelated cooking topic: Potato soup tastes delicious.'
    def api(path, body=None, timeout=8):
        if path.endswith('/recall'): return {'results':[rows[seed]]}
        if path.endswith('/sources/search'): return {'items':[{'document_id':'d','chunk_id':'c','text':chunk}],'next_cursor':None}
        if '/memories/list?' in path: return {'items':[rows[soup]],'total':1}
        if path.endswith('/chunks/c'): return {'bank_id':'bank','document_id':'d','chunk_id':'c','chunk_text':chunk}
        return rows[path.rsplit('/',1)[-1]]
    page = search('bank','Nebula version evolution',api,tmp_path,relevance_policy='weak')
    assert soup not in {row['id'] for row in page['memories']}
    navigation=next(row for row in page['source_navigation'] if row['memory_id']==soup)
    assert 'text' not in navigation
    assert navigation['subject_relation']=='unverified'
    assert navigation['next_action']=={'tool':'read_source','arguments':{'memory_id':soup,'scope':'chunk'}}
    rows[soup]['permission_status']='denied'
    assert read_page('bank',page['research_id'],0,api,tmp_path,relevance_policy='weak')['source_navigation'] == []


def test_local_source_subject_does_not_cross_roles_projects_or_institution_topics():
    from lib.recall_source_context import enrich_source_context
    from lib.recall_relevance import apply_relevance_policy
    cases = [
        ('Nebula version evolution', '[role: assistant]\nNebula5.0 deployed the API.\n[assistant:end]\n[role: user]\nPotato soup tastes delicious.\n[user:end]', 'Potato soup tastes delicious.'),
        ('Nebula version evolution', '[role: assistant]\nNebula5.0 deployed the API. Turning to another project, Garden irrigation is efficient.\n[assistant:end]', 'Garden irrigation is efficient.'),
        ('Nebula version evolution', '[role: assistant]\nNebula5.0部署了接口。换个话题，土豆汤很好喝。\n[assistant:end]', '土豆汤很好喝。'),
        ('云岭学院智慧教室版本演进', '# 云岭学院智慧教室\n系统已部署。\n# 云岭学院食堂\n食堂饭菜非常好吃。', '食堂饭菜非常好吃。'),
        ('Nebula version evolution', '[role: assistant]\nNebula 5.0 deployed the API. Separately, Potato soup tastes delicious.\n[assistant:end]', 'Potato soup tastes delicious.'),
        ('Nebula version evolution', '[role: assistant]\nNebula 5.0部署了接口。另外，土豆汤很好喝。\n[assistant:end]', '土豆汤很好喝。'),
        ('Nebula version evolution', '# Project notes\n## Nebula 5.0 release\nDeployed the new API.\n## Cooking\nPotato soup tastes delicious.', 'Potato soup tastes delicious.'),
        ('Nebula version evolution', '[role: assistant]\nNebula 5.0 deployed the API. Potato soup tastes delicious.\n[assistant:end]', 'Potato soup tastes delicious.'),
        ('Nebula version evolution', '[role: assistant]\nNebula 5.0发布了接口。土豆汤很好喝。\n[assistant:end]', '土豆汤很好喝。'),
        ('Nebula version evolution', '# Nebula 5.0\nAPI已发布。土豆汤很好喝。', '土豆汤很好喝。'),
        ('Nebula version evolution', '# Nebula 5.0.1\nNebula 5.0.1 deployed the API; Potato soup tastes delicious.', 'Potato soup tastes delicious.'),
        ('云岭学院智慧教室版本演进', '# 云岭学院智慧教室\n云岭学院智慧教室5.0已发布。云岭学院食堂的土豆汤很好喝。', '土豆汤很好喝。'),
        ('Nebula classroom version evolution', '# Nebula classroom\nNebula classroom 5.0 released. Nebula cafeteria serves delicious soup.', 'The cafeteria serves delicious soup.'),
    ]
    for query, text, body in cases:
        row = {'id':'a','document_id':'d','chunk_id':'c','text':body}
        for variant in {text.replace('Nebula5.0','Nebula 5.0'),text.replace('Nebula 5.0','Nebula5.0')}:
            def api(path, timeout=8): return {'bank_id':'bank','document_id':'d','chunk_id':'c','chunk_text':variant}
            assert apply_relevance_policy(query,enrich_source_context(query,[row],api,'bank'),'weak')[0] == []


def test_attached_version_subject_still_supports_same_clause_omitted_subject():
    from lib.recall_source_context import enrich_source_context
    from lib.recall_relevance import apply_relevance_policy
    row={'id':'a','document_id':'d','chunk_id':'c','text':'Deployed the API after validation.'}
    for version in ['Nebula5.0','Nebula 5.0']:
        def api(path,timeout=8):
            return {'bank_id':'bank','document_id':'d','chunk_id':'c','chunk_text':'[role: assistant]\n'+version+' deployed the API after validation.\n[assistant:end]'}
        assert [r['id'] for r in apply_relevance_policy('Nebula version evolution',enrich_source_context('Nebula version evolution',[row],api,'bank'),'weak')[0]] == ['a']
    def fenced_api(path,timeout=8):
        return {'bank_id':'bank','document_id':'d','chunk_id':'c','chunk_text':'[role: assistant]\n## Nebula 5.0 release\n```python\n# formatting example\n[role: user]\n```\nNebula 5.0 deployed the API after validation.\n[assistant:end]'}
    assert [r['id'] for r in apply_relevance_policy('Nebula version evolution',enrich_source_context('Nebula version evolution',[row],fenced_api,'bank'),'weak')[0]] == ['a']


def test_explicit_pronoun_requires_independent_antecedent_body_relation():
    from lib.recall_source_context import enrich_source_context
    from lib.recall_relevance import apply_relevance_policy
    cases=[
        ('Nebula 5.0 deployed the API. It passed API validation.', 'API validation passed.', True),
        ('Nebula 5.0部署了接口。该版本通过了接口验收。', '接口验收通过。', True),
        ('Nebula 5.0 deployed the API. It tastes like delicious soup.', 'Delicious soup tastes great.', False),
        ('Nebula 5.0 deployed the API. Garden 2.0 was launched. It passed API validation.', 'API validation passed.', False),
    ]
    for text,body,want in cases:
        def api(path,timeout=8): return {'bank_id':'bank','document_id':'d','chunk_id':'c','chunk_text':text}
        row={'id':'a','document_id':'d','chunk_id':'c','text':body}
        kept,_=apply_relevance_policy('Nebula version evolution',enrich_source_context('Nebula version evolution',[row],api,'bank'),'weak')
        assert bool(kept) is want


def test_specific_subject_contract_is_shared_by_native_and_source_scope():
    from lib.recall_relevance import apply_relevance_policy
    cases=[
        ('云岭学院智慧教室版本演进','云岭学院食堂土豆汤很好喝。',False),
        ('Nebula classroom version evolution','Nebula cafeteria serves delicious soup.',False),
        ('Nebula Classroom version evolution','Nebula cafeteria serves delicious soup.',False),
        ('nebula classroom version evolution','Nebula cafeteria serves delicious soup.',False),
        ('Web Search Router version evolution','Web Search Browser serves advertising.',False),
        ('Web Search Router version evolution','Web Search Router 5.0 released.',True),
        ('云岭学院智慧教室版本演进','云岭学院智慧教室5.0接口已部署。',True),
        ('Nebula classroom version evolution','Nebula classroom 5.0 deployed the API.',True),
        ('Nebula Classroom version evolution','Nebula Classroom 5.0 deployed the API.',True),
        ('nebula classroom version evolution','Nebula Classroom 5.0 deployed the API.',True),
        ('云岭学院版本演进','云岭学院食堂5.0已发布。',True),
        ('Nebula version evolution','Nebula cafeteria 5.0 released.',True),
        ('Nebula classroom and Orion studio version evolution','Orion studio 4.0 released.',True),
        ('云岭学院智慧教室和云岭学院图书馆版本演进','云岭学院图书馆4.0已发布。',True),
    ]
    for query,body,want in cases:
        kept,_=apply_relevance_policy(query,[{'id':'a','text':body}],'weak')
        assert bool(kept) is want
    generic={'id':'method','text':'For reporting pagination, filter rows before calculating offsets.'}
    assert apply_relevance_policy('Nebula classroom pagination filtering offsets',[generic],'weak')[0]
    alias={'id':'alias','text':'星河教学空间 5.0 released.', 'primary_context':{'project':'Nebula classroom'},
        'scope_verification':{'status':'verified','source':'explicit_source_receipt','aliases':['星河教学空间']}}
    assert apply_relevance_policy('Nebula classroom version evolution',[alias],'weak')[0]
    alias['scope_verification']['status']='unknown'
    assert apply_relevance_policy('Nebula classroom version evolution',[alias],'weak')[0] == []
    alias['scope_verification']['status']='verified';alias['permission_status']='denied'
    assert apply_relevance_policy('Nebula classroom version evolution',[alias],'weak')[0] == []


def test_same_specific_object_can_resolve_qualifier_omitted_in_native_body():
    from lib.recall_source_context import enrich_source_context
    from lib.recall_relevance import apply_relevance_policy
    cases=[('Nebula classroom version evolution','Nebula API validation passed.','Nebula classroom 5.0 API validation passed.'),
           ('ep 版本演进','接口验收通过。','EP5.0接口验收通过。'),
           ('云岭学院智慧教室版本演进','云岭学院接口验收通过。','云岭学院智慧教室5.0接口验收通过。')]
    for query,body,text in cases:
        def api(path,timeout=8):return {'bank_id':'bank','document_id':'d','chunk_id':'c','chunk_text':text}
        row={'id':'a','text':body,'document_id':'d','chunk_id':'c'}
        assert apply_relevance_policy(query,enrich_source_context(query,[row],api,'bank'),'weak')[0]


def test_navigation_policy_refresh_rebuilds_pagination_and_explains_identity_change(tmp_path):
    seed,a,b=[str(uuid.uuid4()) for _ in range(3)]
    rows={seed:{'id':seed,'state':'valid','text':'Nebula 5.0 deployed'},
        a:{'id':a,'state':'valid','type':'world','document_id':'d','chunk_id':'c','text':'Soup is delicious.'},
        b:{'id':b,'state':'valid','type':'world','document_id':'d','chunk_id':'c','text':'Lunch is tasty.'}}
    def api(path,body=None,timeout=8):
        if path.endswith('/recall'):return {'results':[rows[seed]]}
        if path.endswith('/sources/search'):return {'items':[{'document_id':'d','chunk_id':'c','text':'Nebula 5.0'}],'next_cursor':None}
        if '/memories/list?' in path:return {'items':[rows[a],rows[b]],'total':2}
        if path.endswith('/chunks/c'):return {'bank_id':'bank','document_id':'d','chunk_id':'c','chunk_text':'Nebula 5.0 deployed. Soup is delicious. Lunch is tasty.'}
        return rows[path.rsplit('/',1)[-1]]
    page=search('bank','Nebula version evolution',api,tmp_path,page_size=1,relevance_policy='weak')
    refreshed=read_page('bank',page['research_id'],0,api,tmp_path,page_size=1,relevance_policy='strong')
    assert refreshed['source_navigation_reference_count']==2
    assert refreshed['source_navigation'][0]['memory_id']==a
    second=read_page('bank',page['research_id'],refreshed['next_offset'],api,tmp_path,page_size=1,relevance_policy='strong')
    assert second['source_navigation'][0]['memory_id']==b
    rows[a].update(document_id='foreign',chunk_id='other')
    changed=read_page('bank',page['research_id'],0,api,tmp_path,page_size=1,relevance_policy='strong')
    assert changed['source_navigation']==[]
    assert changed['unavailable'][0]['status']=='source_identity_changed'
    assert changed['query_completion']=='source_coverage_incomplete'


def test_source_discovery_deadline_bounds_request_timeouts_and_leaves_continuations(monkeypatch):
    from lib.history_discovery import discover_history_sources
    clock=[0.0];timeouts=[]
    monkeypatch.setattr('lib.history_discovery.time.monotonic',lambda:clock[0])
    def api(path,body=None,timeout=8):
        timeouts.append(timeout)
        clock[0]+=min(5.0,timeout)
        return {'items':[{'document_id':'d','chunk_id':'c','text':'Nebula 5.0'}],'next_cursor':str(len(timeouts))}
    rows,audit=discover_history_sources('bank','Nebula version evolution',[{'text':'Nebula 5.0 released'}],api)
    assert rows == []
    assert timeouts == [12.0,7.0,2.0]
    assert audit['deadline_exhausted'] is True
    assert audit['coverage'] == 'partial'
    assert audit['api_call_count'] == 3
    assert audit['continuation_hints'][0]['arguments']['cursor'] == '3'
    assert audit['document_receipts'][0]['status'] == 'deferred_deadline'
    assert audit['document_receipts'][0]['next_read']['method'] == 'GET'


def test_extension_deadline_preserves_original_candidates_and_marks_partial(tmp_path,monkeypatch):
    clock=[0.0]
    monkeypatch.setattr('lib.history_discovery.time.monotonic',lambda:clock[0])
    mid=str(uuid.uuid4());row={'id':mid,'state':'valid','text':'Nebula 5.0 released'}
    def api(path,body=None,timeout=8):
        if path.endswith('/recall'):return {'results':[row]}
        if path.endswith('/sources/search'):
            assert timeout == 12.0
            clock[0]+=timeout
            return {'items':[{'document_id':'d','chunk_id':'c','text':'Nebula 5.0'}],'next_cursor':'1'}
        assert path.endswith('/memories/'+mid)
        return row
    page=search('bank','Nebula version evolution',api,tmp_path,relevance_policy='weak')
    assert [r['id'] for r in page['memories']] == [mid]
    assert page['query_completion'] == 'partial'
    assert page['history_discovery']['deadline_exhausted'] is True


def test_timeout_exhaustion_without_source_hits_is_reported_as_partial(monkeypatch):
    from lib.history_discovery import discover_history_sources
    clock=[0.0]
    monkeypatch.setattr('lib.history_discovery.time.monotonic',lambda:clock[0])
    def api(path,body=None,timeout=8):
        clock[0]+=timeout
        raise TimeoutError('Controlled deadline exhaustion')
    rows,audit=discover_history_sources('bank','Nebula version evolution',[{'text':'Nebula 5.0 released'}],api)
    assert rows == []
    assert audit['deadline_exhausted'] is True
    assert audit['coverage'] == 'partial'
    assert audit['api_call_count'] == 1
    assert audit['continuation_hints'][0]['tool'] == 'find_sources'
