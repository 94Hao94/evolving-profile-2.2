from concurrent.futures import ThreadPoolExecutor
import json

from lib.context_summary import read_context_index, update_context_index, write_context_index


def test_parallel_session_project_updates_preserve_all_rows_and_metadata(tmp_path):
    path=tmp_path/'index.json'; write_context_index(path,[],[])
    value=json.loads(path.read_text()); value['unrelated_metadata']={'preserve':True}; path.write_text(json.dumps(value))
    def append(i):
        def transform(index):
            return {**index,'sessions':index['sessions']+[{'context_id':f'session:{i}','session_id':str(i)}],
                    'projects':index['projects']+[{'context_id':f'project:{i}','project_key':str(i)}]}
        update_context_index(path,transform)
    with ThreadPoolExecutor(max_workers=12) as pool: list(pool.map(append,range(40)))
    result=read_context_index(path)
    assert len(result['sessions'])==len(result['projects'])==40
    assert result['unrelated_metadata']=={'preserve':True}


def test_explicit_index_rebuild_still_replaces_old_rows(tmp_path):
    path=tmp_path/'index.json'; write_context_index(path,[{'session_id':'old'}],[])
    update_context_index(path,lambda data:{**data,'sessions':data['sessions']+[{'session_id':'concurrent'}]})
    write_context_index(path,[{'session_id':'new-explicit-rebuild'}],[])
    assert [s['session_id'] for s in read_context_index(path)['sessions']]==['new-explicit-rebuild']
