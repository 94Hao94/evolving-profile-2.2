import hashlib,json,copy
import pytest
from test_memory_recovery_scenario import fixture,intent_manifest
from lib import memory_recovery_scenario as module
from lib.memory_recovery_scenario import validate_coverage
from lib.scenario_model import fingerprint_episode_bundle

def bind_native_source_coverage_review(*args,**kwargs):
 helper=getattr(module,'bind_native_source_coverage_review',None)
 assert callable(helper),'Native review needs its own honest non-HTTP binding contract'
 return helper(*args,**kwargs)


def review(source,bundle):
 return {'schema':'evolving-profile.native-agent-coverage-review.v1','thread_id':source['thread_id'],
  'source_revision':source['source_revision'],'bundle_sha256':fingerprint_episode_bundle(bundle),
  'reviewed_message_ids':[m['evidence_id'] for m in source['messages']],
  'reviewed_episode_ids':[e['episode_id'] for e in bundle['episodes']],
  'reviewer':{'name':'independent_reviewer','model':'unknown_native_host_model','transport':'native_agent_review_not_provider_http'},
  'whole_source_topics_covered':True,'corrections_preserved':True,'assistant_claims_labeled':True,
  'partition_exact':True,'accept':True,'issues':[],'user_intent_coverage':intent_manifest(source,bundle)}


def test_native_review_retains_explicit_non_http_transport_and_exact_source_proofs(tmp_path):
 _,_,s,b,_=fixture(tmp_path);r=review(s,b);raw=json.dumps(r,ensure_ascii=False).encode()
 a=bind_native_source_coverage_review(s,b,r,artifact_bytes=raw)
 receipt=a['model_review_receipt']
 assert receipt['schema']=='evolving-profile.native-source-coverage-receipt.v1'
 assert receipt['native_review_sha256']==hashlib.sha256(raw).hexdigest()
 assert receipt['transport']=='native_agent_review_not_provider_http'
 assert 'request_sha256' not in receipt and 'response_sha256' not in receipt
 assert receipt['model_exact_identity_verified'] is False
 assert a['fact_verification'] is False and a['no_human_confirmation_claim'] is True
 assert validate_coverage(s,b,a)['accept'] is True


@pytest.mark.parametrize('change',['source','bundle','ids','verdict','manifest','transport','wire_claim'])
def test_native_review_rejects_stale_incomplete_or_mislabeled_artifacts(tmp_path,change):
 _,_,s,b,_=fixture(tmp_path);r=review(s,b)
 if change=='source':r['source_revision']='wrong'
 if change=='bundle':r['bundle_sha256']='wrong'
 if change=='ids':r['reviewed_message_ids'].pop()
 if change=='verdict':r['accept']=False;r['issues']=['incomplete']
 if change=='manifest':r['user_intent_coverage'].pop()
 if change=='transport':r['reviewer']['transport']='provider_http'
 if change=='wire_claim':r['request_sha256']='a'*64
 with pytest.raises(ValueError):bind_native_source_coverage_review(s,b,r,artifact_bytes=json.dumps(r).encode())


def test_native_artifact_byte_mismatch_and_receipt_hash_tampering_rejected(tmp_path):
 _,_,s,b,_=fixture(tmp_path);r=review(s,b);raw=json.dumps(r).encode()
 with pytest.raises(ValueError):bind_native_source_coverage_review(s,b,r,artifact_bytes=b'{}')
 a=bind_native_source_coverage_review(s,b,r,artifact_bytes=raw)
 for key in ['native_review_sha256','source_manifest_sha256']:
  bad=copy.deepcopy(a);bad['model_review_receipt'][key]='wrong'
  with pytest.raises(ValueError):validate_coverage(s,b,bad)


def test_native_review_timestamp_is_host_binding_time_not_arbitrary_metadata(tmp_path):
 _,_,s,b,_=fixture(tmp_path);r=review(s,b)
 r['reviewed_at']={'api_key':'REVIEW_SENTINEL_NOT_A_SECRET'}
 a=bind_native_source_coverage_review(s,b,r,artifact_bytes=json.dumps(r).encode())
 assert isinstance(a['reviewed_at'],str)
 assert 'REVIEW_SENTINEL_NOT_A_SECRET' not in json.dumps(a)
 assert a['review_time_semantics']=='host_binding_time; original_native_review_timestamp_retained_only_in_artifact'
