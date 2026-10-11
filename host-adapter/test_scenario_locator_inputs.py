import json
import uuid
import pytest
from lib.context_summary import build_session_context,write_context_index


def setup(tmp_path,monkeypatch):
    import evolving_profile_controller_mcp as mcp
    sid=str(uuid.uuid4());index=tmp_path/'index.json'
    write_context_index(index,[build_session_context(sid,'project',[],'navigation content')],[])
    monkeypatch.setattr(mcp,'CONTEXT_INDEX_PATH',index);monkeypatch.setattr(mcp,'runtime_disabled',lambda *_:None)
    return mcp,sid


def test_bare_session_uuid_is_unambiguously_normalized(tmp_path,monkeypatch):
    mcp,sid=setup(tmp_path,monkeypatch)
    value=json.loads(mcp.read_context_summary({'scenario_type':'session','scenario_id':sid})['content'][0]['text'])
    assert value['items'][0]['scenario_id']=='session:'+sid
    assert value['total']==1


def test_conflicting_session_fields_are_rejected(tmp_path,monkeypatch):
    mcp,sid=setup(tmp_path,monkeypatch)
    with pytest.raises(ValueError,match='conflicting_session_scenario_locator'):
        mcp.read_context_summary({'scenario_type':'session','scenario_id':sid,'session_id':str(uuid.uuid4())})


def test_project_locator_is_not_guessed_from_bare_uuid(tmp_path,monkeypatch):
    mcp,sid=setup(tmp_path,monkeypatch)
    value=json.loads(mcp.read_context_summary({'scenario_type':'project','scenario_id':sid})['content'][0]['text'])
    assert value['items']==[]
