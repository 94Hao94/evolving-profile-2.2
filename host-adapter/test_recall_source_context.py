from lib.recall_relevance import apply_relevance_policy


def test_source_identity_validated_before_omitted_subject_is_admitted():
    from lib.recall_source_context import enrich_source_context
    rows = [{'id':'a','text':'接口验收仍有缺口，部署后需要补读运行日志。','chunk_id':'c','document_id':'d'},
            {'id':'b','text':'Potato soup tastes delicious.','chunk_id':'c','document_id':'d'}]
    def api(path, timeout=8):
        return {'bank_id':'bank','chunk_id':'c','document_id':'d', 'chunk_text':'Nebula 版本演进：接口验收与部署仍有缺口，补读运行日志。'}
    result = enrich_source_context('Nebula 版本演进', rows, api, 'bank')
    kept, _ = apply_relevance_policy('Nebula 版本演进', result)
    assert [r['id'] for r in kept] == ['a']
    assert rows[0].get('source_context') is None
    assert kept[0]['relevance_level'] == 'weak'
    bad = enrich_source_context('Nebula 版本演进', rows, lambda *_a,**_kw:{'bank_id':'other','chunk_id':'c','document_id':'d','chunk_text':'Nebula 接口验收部署'}, 'bank')
    assert apply_relevance_policy('Nebula 版本演进', bad)[0] == []


def test_chunk_context_is_local_not_far_away_topic_and_does_not_satisfy_literal_query():
    from lib.recall_source_context import enrich_source_context
    row = {'id':'a','text':'Potato soup tastes delicious.', 'chunk_id':'c','document_id':'d'}
    def api(path, timeout=8):
        return {'bank_id':'bank','chunk_id':'c','document_id':'d', 'chunk_text':'Nebula 版本演进。' + 'x'*4000 + 'Potato soup tastes delicious.'}
    assert apply_relevance_policy('Nebula 版本演进', enrich_source_context('Nebula 版本演进', [row], api, 'bank'))[0] == []
    assert apply_relevance_policy('Find exact identifier `NEBULA_888`', enrich_source_context('Find exact identifier `NEBULA_888`', [row], api, 'bank'))[0] == []


def test_real_workspace_search_and_readback_keep_source_supported_background(tmp_path):
    from evidence_workspace import search, read_page
    import uuid
    mid = str(uuid.uuid4())
    row = {'id':mid,'state':'valid', 'text':'接口验收仍有缺口，部署后需要补读运行日志。','chunk_id':'c','document_id':'d'}
    def api(path, body=None, timeout=8):
        if path.endswith('/recall'): return {'results':[row]}
        if path.endswith('/chunks/c'): return {'bank_id':'bank','chunk_id':'c','document_id':'d', 'chunk_text':'Nebula 版本演进：接口验收与部署仍有缺口，补读运行日志。'}
        if path.endswith('/memories/'+mid): return row
        raise AssertionError(path)
    result = search('bank', 'Nebula 版本演进', api, tmp_path, relevance_policy='weak')
    assert len(result['memories']) == 1
    page = read_page('bank', result['research_id'], 0, api, tmp_path, relevance_policy='weak')
    assert len(page['memories']) == 1
    assert page['memories'][0]['relevance_level'] == 'weak'
