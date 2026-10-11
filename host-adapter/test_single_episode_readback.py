import copy
import json

from lib.context_summary import build_session_context,write_context_index
from lib.memory_recovery_scenario import publish_automated_session
from lib.scenario_episodes import partition_source,validate_episode_bundle
from lib.scenario_model import fingerprint_episode_bundle
from lib.scenario_source import read_session_source
from test_memory_recovery_scenario import fixture,coverage


def single(tmp_path,monkeypatch):
    import evolving_profile_controller_mcp as mcp
    root,path,source,bundle,review=fixture(tmp_path)
    path.write_text('\n'.join(path.read_text().splitlines()[:3])+'\n')
    source=read_session_source(source['thread_id'],root,max_chars=60000)
    episode={**bundle['episodes'][0],**{k:v for k,v in partition_source(source,[])[0].items() if k!='_messages'}}
    bundle=validate_episode_bundle(source,{**bundle,'parent_source_revision':source['source_revision'],
        'episodes':[episode],'boundary_decisions':[]})
    review={**review,'parent_source_revision':source['source_revision'],'bundle_sha256':fingerprint_episode_bundle(bundle),
        'reviewed_episode_ids':[episode['episode_id']],'episode_reviews':review['episode_reviews'][:1]}
    row=publish_automated_session(build_session_context(source['thread_id'],'project',[],''),source,bundle,review,coverage(source,bundle),root)
    assert row['status']=='model_reviewed' and row['episodes']==[]
    index=tmp_path/'index.json';write_context_index(index,[row],[])
    monkeypatch.setattr(mcp,'CONTEXT_INDEX_PATH',index);monkeypatch.setattr(mcp,'THREAD_SESSION_ROOT',root)
    monkeypatch.setattr(mcp,'runtime_disabled',lambda *_:None)
    return mcp,path,index,row,episode['episode_id']


def test_actual_accepted_single_episode_has_readonly_episode_projection(tmp_path,monkeypatch):
    mcp,path,index,row,eid=single(tmp_path,monkeypatch);before=index.read_bytes()
    result=json.loads(mcp.read_context_summary({'scenario_type':'session','scenario_id':row['context_id'],
        'episode_id':eid,'tier':'full'})['content'][0]['text'])
    assert result['status']=='current'
    assert mcp._returned_count(result,'read_scenario_summary')==1
    assert result['items'][0]['summary']==row['summary']['full']
    assert result['items'][0]['episode_id']==eid
    assert result['items'][0]['projection_basis']=='accepted_single_episode_source_coverage'
    assert index.read_bytes()==before


def test_single_episode_projection_rejects_changed_raw_source(tmp_path,monkeypatch):
    mcp,path,index,row,eid=single(tmp_path,monkeypatch)
    last=json.loads(path.read_text().splitlines()[-1]);last['payload']['content'][0]['text']='changed source';
    lines=path.read_text().splitlines();lines[-1]=json.dumps(last);path.write_text('\n'.join(lines)+'\n')
    result=json.loads(mcp.read_context_summary({'scenario_type':'session','scenario_id':row['context_id'],
        'episode_id':eid})['content'][0]['text'])
    assert result['status']=='stale_source_changed'
    assert not any(item.get('summary') for item in result.get('items') or [])


def test_unaudited_episode_id_is_not_manufactured(tmp_path,monkeypatch):
    mcp,path,index,row,eid=single(tmp_path,monkeypatch)
    result=json.loads(mcp.read_context_summary({'scenario_type':'session','scenario_id':row['context_id'],
        'episode_id':eid+'-other'})['content'][0]['text'])
    assert result['status']=='episode_not_found'
