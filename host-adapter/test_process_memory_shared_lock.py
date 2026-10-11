from concurrent.futures import ThreadPoolExecutor

from lib.process_memory import ProcessMemoryStore


def test_concurrent_append_across_store_instances_preserves_all_records(tmp_path):
    path=tmp_path/'records.json'
    def append(worker):
        for i in range(15):
            ProcessMemoryStore(path).record_trajectory({'process_memory_id':f'trace-{worker}-{i}','text':'Actual tool receipt','bank_id':'bank-a'})
    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(append,range(12)))
    assert len(ProcessMemoryStore(path).all())==180


def test_reentrant_shared_path_and_capability_source_ownership(tmp_path):
    path=tmp_path/'records.json'; first=ProcessMemoryStore(path); second=ProcessMemoryStore(path)
    with first.locked():
        with second.locked():
            result=second.record_capability_observation({'model_family':'model-a','model_version':'1','task_archetype':'coding','phase':'verify',
                'outcome':'correct','verifier_kind':'automated_test','status':'passed','sample_id':'sample-actual',
                'bank_id':'bank-a','primary_context':{'session_id':'session-a','bank_id':'bank-a'},
                'verification_evidence':[{'verifier_kind':'automated_test','status':'passed','id':'test-receipt'}]})
    assert result['bank_id']=='bank-a' and result['primary_context']['session_id']=='session-a'


def test_capability_profile_and_sample_commit_in_one_atomic_write(tmp_path,monkeypatch):
    store=ProcessMemoryStore(tmp_path/'records.json'); original=store._write; snapshots=[]
    def record(value):
        snapshots.append(value.copy()); original(value)
    monkeypatch.setattr(store,'_write',record)
    observation={'model_family':'model','task_archetype':'coding','phase':'verify','outcome':'correct','sample_id':'real-sample',
        'bank_id':'bank-a','verifier_kind':'automated_test','status':'passed'}
    store.record_capability_observation(observation)
    assert len(snapshots)==1
    assert len(snapshots[0]['records'])==1 and snapshots[0]['profiles']['model|coding|verify']['sample_count']==1
    store.record_capability_observation(observation)
    assert len(snapshots)==1 # response loss/resume does not count the sample twice
