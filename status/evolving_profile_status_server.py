# -*- coding: utf-8 -*-
#!/usr/bin/env python3
"""Read-only Evolving Profile observability console."""
from __future__ import annotations
import json,os,re,subprocess,threading,time,urllib.parse,urllib.request,hashlib,importlib.util,sys
from collections import deque
import sqlite3
from datetime import datetime,timedelta,timezone
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
HISTORY_TOOL_NAMES={f'mcp__{controller}__{operation}'
 for controller in ('evolving_profile_controller','hindsight_controller')
 for operation in ('recall','research','user_recall','user_research','agent_recall','agent_research','read_research','read_source','read_agent_process_memory','find_sources','search_scenario_summary','search_scenario_contexts','scenario_gate','read_scenario_summary','read_context_summary')}
HOME=Path.home()
ROOT=Path(__file__).resolve().parent.parent
STATE_ROOT=Path(os.environ.get('EVOLVING_PROFILE_STATE_ROOT', str(HOME/'.evolving-profile')))
RUNTIME_ROOT=Path(os.environ.get('EVOLVING_PROFILE_RUNTIME_ROOT', str(STATE_ROOT/'runtime')))
HOST_ADAPTER_ROOT=(ROOT/'host-adapter') if (ROOT/'host-adapter').is_dir() else (RUNTIME_ROOT/'host-adapter')
if str(HOST_ADAPTER_ROOT) not in sys.path:sys.path.insert(0,str(HOST_ADAPTER_ROOT))
from topic_catalog import TopicCatalog,knowledge_review,redact_unreviewed_page
from lib.memory_policy import classify_memory_policy
API=os.environ.get('EVOLVING_PROFILE_API_URL','http://127.0.0.1:12088')
CONTROLLER=os.environ.get('EVOLVING_PROFILE_CONTROLLER_URL','http://127.0.0.1:12079')
BANK='personal-memory'
PAGE_FILE=STATE_ROOT/'control-plane/recall-observability.html'
GUIDANCE_V1_SRC=ROOT/'guidance'
GUIDANCE_V1_CONFIG=STATE_ROOT/'guidance-v1/guidance-v1.json'
GUIDANCE_V1_PAGE=ROOT/'guidance-web/guidance.html'
GUIDANCE_TRACE_PAGE=ROOT/'guidance-web/guidance-trace.html'
GUIDANCE_CANDIDATES=STATE_ROOT/'guidance-v1/codex-consolidation.json'
SELECTOR_REVIEWED_EVAL=STATE_ROOT/'control-plane/selector-reviewed-eval.json'
TOPIC_CATALOG_PATH=STATE_ROOT/'catalog/topics.sqlite3'
if str(GUIDANCE_V1_SRC) not in sys.path:sys.path.insert(0,str(GUIDANCE_V1_SRC))
from prompt_origin import PromptOriginResolver,select_prompt_occurrences
from status_projection import prompt_population_projection
CODEX_SESSION_ROOTS=[Path(os.environ.get('EVOLVING_PROFILE_HOST_SESSIONS_ROOT',str(HOME/'.codex/sessions' if STATE_ROOT==HOME/'.evolving-profile' else STATE_ROOT/'host-sessions'))),
                     Path(os.environ.get('EVOLVING_PROFILE_HOST_ARCHIVED_SESSIONS_ROOT',str(HOME/'.codex/archived_sessions' if STATE_ROOT==HOME/'.evolving-profile' else STATE_ROOT/'archived-host-sessions')))]
_cache_spec=importlib.util.spec_from_file_location('dashboard_cache',RUNTIME_ROOT/'host-adapter/dashboard_cache.py')
_cache_module=importlib.util.module_from_spec(_cache_spec);_cache_spec.loader.exec_module(_cache_module)
_receipts_spec=importlib.util.spec_from_file_location('turn_host_receipts',RUNTIME_ROOT/'host-adapter/host_receipt_projection.py')
_receipts_module=importlib.util.module_from_spec(_receipts_spec);_receipts_spec.loader.exec_module(_receipts_module)
_turn_contract_spec=importlib.util.spec_from_file_location('turn_receipt_contract',RUNTIME_ROOT/'host-adapter/lib/turn_receipt_contract.py')
_turn_contract_module=importlib.util.module_from_spec(_turn_contract_spec);_turn_contract_spec.loader.exec_module(_turn_contract_module)
READ_CACHE=_cache_module.SnapshotCache(ttl=30,backoff=60)
SERIALIZED={};SERIALIZED_LOCK=threading.Lock()
def api_payload(key,value):
 with SERIALIZED_LOCK:
  previous=SERIALIZED.get(key)
  if previous and previous[0] is value:return previous[1:]
  body=json.dumps(value,ensure_ascii=False).encode();etag='"'+hashlib.sha256(body).hexdigest()+'"'
  SERIALIZED[key]=(value,body,etag)
  return body,etag
def _tail_lines(path,limit):
 try:
  with Path(path).open(encoding='utf-8',errors='ignore') as stream:
   return list(deque(stream,maxlen=max(1,int(limit))))
 except OSError:return []
def guidance_v1_snapshot():
 try:
  if str(GUIDANCE_V1_SRC) not in sys.path:sys.path.insert(0,str(GUIDANCE_V1_SRC))
  from mcp_runtime import load_repository
  from status_projection import snapshot as guidance_snapshot
  from receipts import delivery_snapshot
  repo=load_repository(GUIDANCE_V1_CONFIG)
  value=guidance_snapshot(repo,deliveries=delivery_snapshot(STATE_ROOT/'guidance-v1/receipts'),queue={'pending':[],'completed':[]})
  value['instruction_delivery']=guidance_instruction_status();return value
 except Exception as error:
  return {'schema':'guidance.status.v1','snapshot_at':time.time(),'service_health':'failed','guidance_health':'unknown','processing_health':'unknown','adoption_health':'unknown','guidance':{'active':None,'held':None,'invalid_dependencies':None},'processing':{'incremental':{'state':'unknown','pending':None},'migration':{'state':'unknown'}},'deliveries':[],'errors':['guidance_snapshot:'+type(error).__name__+':'+str(error)[:300]]}
def guidance_v1_units():
 try:
  if str(GUIDANCE_V1_SRC) not in sys.path:sys.path.insert(0,str(GUIDANCE_V1_SRC))
  from mcp_runtime import load_repository
  return {'items':load_repository(GUIDANCE_V1_CONFIG).active_units(),'candidate_visibility':'not_returned'}
 except Exception as error:
  return {'items':[],'candidate_visibility':'not_returned','errors':['guidance_units:'+type(error).__name__+':'+str(error)[:300]]}

def memory_map_snapshot():
 """Return routing metadata; catalog entries are not memory evidence."""
 units=guidance_v1_units().get('items') or []
 models=guidance_v1_models().get('items') or []
 try: stats=get(API,f'/v1/default/banks/{urllib.parse.quote(BANK,safe="")}/stats',3)
 except Exception: stats={}
 fact_counts=stats.get('nodes_by_fact_type') or {}
 link_counts=stats.get('links_by_link_type') or {}
 try:catalog_topics=TopicCatalog(TOPIC_CATALOG_PATH).list(20)
 except Exception:catalog_topics=[]
 try:knowledge_roots=get(API,f'/v1/default/banks/{urllib.parse.quote(BANK,safe="")}/knowledge-base/tree',3).get('roots') or []
 except Exception:knowledge_roots=[]
 try:
  from entry_navigation import build_navigation_map
  _context,navigation=build_navigation_map(GUIDANCE_V1_CONFIG,{'history_allowed':True,'guidance_memory_policy':'allowed'})
 except Exception:navigation=None
 return {'schema':'evolving-profile.memory-map.v2','generated_at':datetime.now().isoformat(),
  'navigation':navigation,
  'layers':[{'id':'L0','name':'主题摘要','purpose':'快速相关性判断','body_limit':'short'},
            {'id':'L1','name':'主题概览','purpose':'重排、导航和路线选择','body_limit':'medium'},
            {'id':'L2','name':'证据详情','purpose':'按需读取事实、经历和原始来源','body_limit':'on_demand'}],
  'nodes':[
   {'id':'guidance','label':'多维度偏好','lane':'guidance','count':len(units),'route':'get_preference','freshness':'active','summary':'五个行为维度的条件化指导；仅作 advisory reference','topic_scope':'reviewed_active_units','time_range':None,'source_count':sum(len(x.get('evidence_refs') or []) for x in units),'state_summary':'active_reviewed'},
   {'id':'mental-models','label':'融合心智模型','lane':'guidance','count':len(models),'route':'get_preference → read_preference_unit','freshness':'active','summary':'跨维度稳定主题摘要；需要来源时下沉到 Bank','topic_scope':'active_model_sections','time_range':None,'source_count':None,'state_summary':'active'},
   {'id':'world','label':'事实','lane':'facts','count':fact_counts.get('world'),'route':'recall','freshness':'live_stats' if fact_counts else 'unknown','summary':'状态、版本、主体和关系事实','topic_scope':'query_scoped_catalog_hints','time_range':'returned_per_hint','source_count':None,'state_summary':'live_validity_checked_on_read'},
   {'id':'experience','label':'经历','lane':'facts','count':fact_counts.get('experience'),'route':'recall','freshness':'live_stats' if fact_counts else 'unknown','summary':'发生过的事件、过程、结果和失败经验','topic_scope':'query_scoped_catalog_hints','time_range':'returned_per_hint','source_count':None,'state_summary':'live_validity_checked_on_read'},
   {'id':'entities','label':'实体与关系','lane':'graph','count':link_counts.get('entity'),'route':'research','freshness':'live_stats' if link_counts else 'unknown','summary':'多实体、时间线和关系闭包的扩展入口','topic_scope':'query_scoped_entities','time_range':'returned_per_hint','source_count':None,'state_summary':'live'},
   {'id':'observations','label':'观察候选','lane':'consolidation','count':fact_counts.get('observation'),'route':'background','freshness':'live_stats' if fact_counts else 'unknown','summary':'由多条事实归纳的候选模式，需审核后发布','topic_scope':'reviewed_candidates','time_range':None,'source_count':None,'state_summary':'not_automatically_guidance'},
   {'id':'sources','label':'原始来源','lane':'evidence','count':None,'route':'find_sources → read_source','freshness':'live_on_read','summary':'逐字原文、版本和证据核对','topic_scope':'literal_terms_or_memory_locator','time_range':'source_recorded_time','source_count':None,'state_summary':'source_role_requires_verification'}
  ],'topic_catalog':{'status':'available' if catalog_topics or knowledge_roots else 'unavailable',
                     'topic_count':len(knowledge_roots)+(TopicCatalog(TOPIC_CATALOG_PATH).count() if catalog_topics else 0),
                     'knowledge_page_count':len(knowledge_roots),'navigation_topic_count':TopicCatalog(TOPIC_CATALOG_PATH).count() if catalog_topics else 0,
                     'knowledge_pages':[{'topic_id':row.get('id'),'title':row.get('name'),'abstract':row.get('description'),'refreshed_at':row.get('timestamp'),'pending_changes':1 if row.get('is_stale') else 0,'source':'hindsight_knowledge_page','review':knowledge_review(str(row.get('id') or ''))} for row in knowledge_roots],
                     'roots':[{k:row.get(k) for k in ('topic_id','title','abstract','source_count','coverage','pending_changes','conflicts','pending_changes_status','conflict_status','content_status','overview_status','refreshed_at')} for row in catalog_topics],
                     'boundary':'navigation_only_not_fact_evidence'},
  'route_policy':{'targeted':'recall','multi_entity_or_timeline':'research','exact_source':'read_source','current_prompt_first':True,'unknown_requires_receipt':True}}

def _catalog_probe(prompt):
 """Low-budget Bank probe for route choice; never returns fact bodies."""
 try:
  try:
   tree=get(API,f'/v1/default/banks/{urllib.parse.quote(BANK,safe="")}/knowledge-base/tree',3).get('roots') or []
   terms=[value.casefold() for value in re.findall(r'[a-z][a-z0-9_.+-]{1,}|[\u4e00-\u9fff]{2,}',str(prompt or '').casefold())]
   ranked=[]
   for row in tree:
    corpus=(str(row.get('name') or '')+' '+str(row.get('description') or '')).casefold();score=sum(1 for term in terms if term in corpus)
    if score:ranked.append((score,row))
   pages=[row for _,row in sorted(ranked,key=lambda item:item[0],reverse=True)[:5]]
  except Exception:pages=[]
  if pages:
   return {'status':'knowledge_pages_observed','candidate_count':len(pages),'matched_entities':[str(row.get('name')) for row in pages],
           'entity_count':len(pages),'catalog_coverage':'background_reconciled_pages',
           'hints':[{'topic_id':row.get('id'),'title':row.get('name'),'abstract':row.get('description'),'refreshed_at':row.get('timestamp'),
                     'source_count':None,'coverage':{'kind':'knowledge_page_navigation'},'pending_changes':1 if row.get('is_stale') else 0,
                     'review':knowledge_review(str(row.get('id') or '')),'boundary':'navigation_only_not_fact_evidence'} for row in pages]}
  topics=TopicCatalog(TOPIC_CATALOG_PATH).search(prompt,5)
  if topics:
   entities=list(dict.fromkeys(entity for row in topics for entity in row.get('entities') or []))[:12]
   fields=('topic_id','title','abstract','overview','entities','time_range','source_count','source_count_semantics','coverage','pending_changes','conflicts','pending_changes_status','conflict_status','content_status','overview_status','refreshed_at','boundary')
   return {'status':'catalog_observed','candidate_count':len(topics),'matched_entities':entities,
           'entity_count':sum(len(row.get('entities') or []) for row in topics),'catalog_coverage':'partial_navigation_projection',
           'hints':[{k:row.get(k) for k in fields} for row in topics]}
  return {'status':'catalog_miss','candidate_count':0,'matched_entities':[],'entity_count':0,'catalog_coverage':'partial_navigation_projection','hints':[]}
 except Exception as error:
  return {'status':'unavailable','candidate_count':None,'matched_entities':[],'error_type':type(error).__name__}

def memory_check(prompt=''):
 from system_probe import plan_history
 plan=plan_history(prompt)
 policy=plan['memory_policy']
 if not policy['history_allowed']:
  probe={'status':'forbidden','candidate_count':None,'hints':[]}
 elif plan['minimum_action']=='skip':
  probe={'status':'skipped_self_contained','candidate_count':None,'hints':[]}
 elif 'catalog' in policy['denied_tools']:
  probe={'status':'forbidden_tool','candidate_count':None,'hints':[]}
 else:probe=_catalog_probe(prompt)
 text=str(prompt).casefold()
 nodes=[]
 if plan['history_dependency']=='complex':nodes.append('entities')
 if any(term in text for term in ('原文','来源','出处')):nodes.append('sources')
 return {**plan,'schema':'evolving-profile.memory-check.v3',
         'matched_nodes':nodes,'catalog_probe':{k:v for k,v in probe.items() if k!='hints'},
         'catalog_hints':probe.get('hints') or [],'confidence':None,
         'confidence_semantics':'heuristic_hint_not_probability','requires_receipt':True}
def guidance_v1_models():
 try:
  if str(GUIDANCE_V1_SRC) not in sys.path:sys.path.insert(0,str(GUIDANCE_V1_SRC))
  from mcp_runtime import load_repository
  repo=load_repository(GUIDANCE_V1_CONFIG);inventory=repo.model_inventory()
  return {'items':inventory['active'],'counts':inventory['counts'],
          'candidates':inventory['candidates'],'archived_legacy':inventory['archived_legacy'],
          'model_semantics':'dynamic_atomic_cross_dimensional',
          'legacy_models_remain_bank_visible':True}
 except Exception as error:return {'items':[],'errors':['guidance_models:'+type(error).__name__+':'+str(error)[:300]]}
def guidance_v1_jobs():
 try:
  if str(GUIDANCE_V1_SRC) not in sys.path:sys.path.insert(0,str(GUIDANCE_V1_SRC))
  from mcp_runtime import load_repository
  rows=[]
  for row in load_repository(GUIDANCE_V1_CONFIG).jobs():
   rows.append({k:v for k,v in row.items() if k!='payload_json'})
  return {'items':rows}
 except Exception as error:return {'items':[],'errors':['guidance_jobs:'+type(error).__name__+':'+str(error)[:300]]}
def guidance_v1_candidates(limit=500):
 try:
  value=json.loads(GUIDANCE_CANDIDATES.read_text(encoding='utf-8')) if GUIDANCE_CANDIDATES.exists() else {'items':[]}
  items=[]
  for row in value.get('items') or []:
   if row.get('state') in {'question_or_test_prompt','existing_preference_reinforcement'}: continue
   candidate=row.get('candidate') or {}
   items.append({"family_key":row.get('family_key'),"canonical_text":row.get('canonical_text'),"state":row.get('state'),"flags":candidate.get('flags') or [],"independent_threads":candidate.get('independent_threads',0),"occurrences":candidate.get('occurrences',0),"source_family_ids":candidate.get('thread_ids') or []})
  return {'schema':'guidance.observation-candidates.v1','items':items[:max(1,min(2000,int(limit)))] ,'total':len(items),'source':str(GUIDANCE_CANDIDATES),'publication':'not_active'}
 except Exception as error:return {'schema':'guidance.observation-candidates.v1','items':[],'errors':['guidance_candidates:'+type(error).__name__+':'+str(error)[:300]]}
def guidance_delivery_list(limit=50):
 try:
  from receipts import delivery_list
  return delivery_list(STATE_ROOT/'guidance-v1/receipts',limit=limit)
 except Exception as error:return {'schema':'guidance.delivery-list.v1','items':[],'errors':['guidance_deliveries:'+type(error).__name__+':'+str(error)[:300]]}
def guidance_delivery_detail(occurrence_id):
 try:
  from receipts import delivery_detail
  return delivery_detail(STATE_ROOT/'guidance-v1/receipts',occurrence_id)
 except Exception as error:return {'status':'unavailable','occurrence_id':occurrence_id,'errors':['guidance_delivery:'+type(error).__name__+':'+str(error)[:300]]}

def selector_evaluation_snapshot():
 try:
  value=json.loads(SELECTOR_REVIEWED_EVAL.read_text(encoding='utf-8'))
  metrics=value.get('metrics') or {}
  return {'schema':value.get('schema'),'label_state':value.get('label_state'),'cases':len(value.get('cases') or []),'metrics':{k:metrics.get(k) for k in ('precision','recall','abstention_accuracy','duplicate_rate','stale_suppression','conflict_suppression')}}
 except Exception as error:
  return {'schema':'evolving-profile.selector-reviewed-eval.v1','label_state':'unavailable','cases':0,'metrics':{},'error':type(error).__name__}

def _norm_prompt(value):
 return ' '.join(str(value or '').split()).strip()

def _prompt_ingress_rows(detail_id=None,**priority_options):
 path=STATE_ROOT/'audit/prompt-ingress.jsonl';raw_rows=[];resolver=PromptOriginResolver(CODEX_SESSION_ROOTS)
 try:
  for line in _tail_lines(path,2000):
   try:
    row=json.loads(line)
   except Exception: continue
   raw_rows.append(row)
 except OSError: pass
 return resolver.resolve_rows(select_prompt_occurrences(raw_rows),detail_id=detail_id,**priority_options)

def _hook_output_rows():
 root=STATE_ROOT/'audit/hook-output-receipts';rows=[]
 # Diagnostic receipts are intentionally excluded here. They are useful in
 # raw audit tools, but they are not owner-visible UserPromptSubmit turns and
 # account for most of the 70MB receipt directory. Parse the bounded
 # projection in a short-lived jq process so nested candidate/source payloads
 # never accumulate in this long-lived status server.
 paths=sorted((root/'production').glob('*.json'),key=lambda p:p.stat().st_mtime,reverse=True)[:300]
 jq_filter=('{raw_user_prompt,session_id,turn_id,hook_invocation_id,memory_action,history_decision,history_decision_evidence,candidate_count,injected_count,memory_needs,system_probe,history_plan,'
   'memory_effectiveness:{actual_injected_count:(.memory_effectiveness.actual_injected_count // .memory_effectiveness.injected_count // .injected_count),'
   'injected_ids:(.memory_effectiveness.injected_ids // [] | .[:100]),'
   'source_reads:(.memory_effectiveness.source_reads // [] | .[:20]),'
   'injection_receipt_state:.memory_effectiveness.injection_receipt_state}}')
 for path in paths:
  try:
   proc=subprocess.run(['jq','-c',jq_filter,str(path)],capture_output=True,text=True,timeout=2)
   if proc.returncode!=0: continue
   row=json.loads(proc.stdout)
   raw=_norm_prompt(row.get('raw_user_prompt'))
   if raw:
    rows.append({'_raw_prompt':raw,'session_id':row.get('session_id'),'turn_id':row.get('turn_id'),'hook_invocation_id':row.get('hook_invocation_id'),
      'memory_action':row.get('memory_action'),'history_decision':row.get('history_decision'),'history_decision_evidence':row.get('history_decision_evidence'),'candidate_count':row.get('candidate_count'),'memory_needs':row.get('memory_needs') or {},
      'memory_effectiveness':row.get('memory_effectiveness') or {},'system_probe':row.get('system_probe'),'history_plan':row.get('history_plan')})
  except Exception: pass
 return rows

def _route_receipt_rows():
 root=STATE_ROOT/'audit/memory-route-receipts'; rows=[]
 for path in sorted(root.glob('*.json'),key=lambda p:p.stat().st_mtime,reverse=True)[:500]:
  try:
   value=json.loads(path.read_text(encoding='utf-8'))
   if isinstance(value,dict): rows.append(_sanitize_route_receipt(value))
  except (OSError,ValueError,TypeError): pass
 return rows

def _global_mcp_activity_rows(limit=4000):
 rows=[];path=STATE_ROOT/'audit/mcp-tool-activity.jsonl'
 for line in _tail_lines(path,limit):
  try:
   value=json.loads(line)
   if isinstance(value,dict):rows.append(value)
  except (ValueError,TypeError):continue
 return rows

def _as_utc(value):
 try:parsed=datetime.fromisoformat(str(value).replace('Z','+00:00'))
 except (TypeError,ValueError,OverflowError):return None
 return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed

def _bound_to_prompt(event, prompt_binding, prompt_ingress, fallback_binding=None):
 """Require an exact check/hook ID and reject it after a newer same-session Prompt."""
 event=dict(event or {});fallback_binding=dict(fallback_binding or {})
 event_id=str(event.get('hook_invocation_id') or event.get('check_id') or event.get('tool_call_id') or fallback_binding.get('hook_invocation_id') or '')
 prompt_id=str((prompt_binding or {}).get('hook_invocation_id') or '')
 if not prompt_id or event_id!=prompt_id:return False
 for scope in (event,fallback_binding):
  for field in ('hook_invocation_id','check_id'):
   if scope.get(field) and str(scope[field])!=prompt_id:return False
  for field in ('session_id','turn_id'):
   if scope.get(field) and (prompt_binding or {}).get(field) and str(scope[field])!=str(prompt_binding[field]):return False
 session_id=str(event.get('session_id') or fallback_binding.get('session_id') or '')
 prompt_session=str((prompt_binding or {}).get('session_id') or '')
 if session_id and prompt_session and session_id!=prompt_session:return False
 event_at=_as_utc(event.get('at'))
 if event_at and session_id:
  latest=None;latest_at=None
  for row in prompt_ingress or []:
   if str(row.get('session_id') or '')!=session_id:continue
   at=_as_utc(row.get('at'))
   if at is not None and at<=event_at and (latest_at is None or at>latest_at):
    latest=row;latest_at=at
  if latest and str(latest.get('hook_invocation_id') or '')!=prompt_id:return False
 return True

def _event_key(event):
 return (str(event.get('check_id') or event.get('hook_invocation_id') or ''),
         str(event.get('tool') or ''),str(event.get('research_id') or ''),
         str(event.get('memory_id') or ''),tuple(event.get('memory_ids') or []),
         int(event.get('returned_count') or 0),str(event.get('at') or ''))

def _activity_counts(events):
 by_tool={}
 for event in events:
  tool=str(event.get('tool') or 'unknown');bucket=by_tool.setdefault(tool,{'calls':0,'returned':0,'candidates':0,'latest_at':None})
  bucket['calls']+=1;bucket['returned']+=int(event.get('returned_count') or 0)
  bucket['candidates']+=int(event.get('candidate_count') or 0)
  bucket['latest_at']=max(bucket['latest_at'] or event['at'],event['at'])
 return by_tool

def _time_window_tool_activity(prompt_at, route_receipts=None, window_minutes=2, global_activity=None,
                               prompt_binding=None, prompt_ingress=None):
 """Separate same-Prompt MCP calls from anonymous global events in the same time window."""
 anchor=_as_utc(prompt_at)
 if anchor is None:
  return {'state':'not_observed','event_count':0,'by_tool':{},'events':[],
          'unattributed_activity':{'state':'not_observed','event_count':0,'by_tool':{}},
          'boundary':'same_prompt_binding_only','reason':'prompt_time_unavailable'}
 minutes=max(1,min(60,int(window_minutes or 2)));start=anchor;end=anchor+timedelta(minutes=minutes)
 binding=dict(prompt_binding or {})
 if not binding:
  binding=dict(next(((receipt.get('prompt_binding') or {}) for receipt in route_receipts or []
                     if (receipt.get('prompt_binding') or {}).get('hook_invocation_id')),{}))
 exact=[];unattributed=[];seen_exact=set();seen_unattributed=set()
 allowed={'recall','research','user_recall','user_research','agent_recall','agent_research','read_research','read_source','find_sources','read_agent_process_memory','search_scenario_summary','search_scenario_contexts','scenario_gate','read_scenario_summary','read_context_summary'}
 def in_window(event):
  at=_as_utc(event.get('at'))
  return at is not None and start<=at<=end
 def compact(event):
  raw=str(event.get('tool') or 'unknown');tool=raw.rsplit('__',1)[-1]
  return {'tool':tool,'at':event.get('at'),'check_id':event.get('check_id') or event.get('hook_invocation_id'),
          'tool_call_id':event.get('tool_call_id'),'session_id':event.get('session_id'),'turn_id':event.get('turn_id'),
          'mapping_items':list(event.get('mapping_items') or [])[:200],
          'returned_content_snapshot':event.get('returned_content_snapshot'),
          'delivered_count':event.get('delivered_count'),'delivery':event.get('delivery') or {},
          'source_navigation':list(event.get('source_navigation') or [])[:200],
          'source_navigation_returned_count':event.get('source_navigation_returned_count'),
          'source_navigation_returned_ids':list(event.get('source_navigation_returned_ids') or [])[:200],
          'returned_count':event.get('returned_count'),'candidate_count':event.get('candidate_count'),
          'research_id':event.get('research_id'),'memory_id':event.get('memory_id'),
          'memory_ids':list(event.get('memory_ids') or [])[:50],
                   'scenario_ids':list(event.get('scenario_ids') or [])[:20],
                   'scenario_navigation_roles':list(event.get('scenario_navigation_roles') or [])[:20],
                   'scope_hypothesis_count':event.get('scope_hypothesis_count'),
                   'scope_route_policy':event.get('scope_route_policy') or {},
          'scenario_decision':event.get('scenario_decision')}
 for receipt in route_receipts or []:
  receipt_binding=receipt.get('prompt_binding') or {}
  receipt_id=str(receipt_binding.get('hook_invocation_id') or '')
  if receipt_id and binding.get('hook_invocation_id') and receipt_id!=str(binding['hook_invocation_id']):continue
  for event in receipt.get('tool_events') or []:
   if not isinstance(event,dict) or not in_window(event):continue
   normalized=compact(event)
   if normalized['tool'] not in allowed:continue
   value={**event,'tool':normalized['tool']}
   key=_event_key(value)
   if _bound_to_prompt(value,binding,prompt_ingress,receipt_binding):
    if key not in seen_exact:exact.append(normalized);seen_exact.add(key)
   elif key not in seen_unattributed:
    unattributed.append({'tool':normalized['tool'],'at':normalized['at'],'returned_count':normalized['returned_count']});seen_unattributed.add(key)
 for event in global_activity or []:
  if not isinstance(event,dict) or not in_window(event):continue
  normalized=compact(event)
  if normalized['tool'] not in allowed:continue
  key=_event_key({**event,'tool':normalized['tool']})
  if _bound_to_prompt(event,binding,prompt_ingress):
   if key not in seen_exact:exact.append(normalized);seen_exact.add(key)
  elif key not in seen_unattributed:
   unattributed.append({'tool':normalized['tool'],'at':normalized['at'],'returned_count':normalized['returned_count']});seen_unattributed.add(key)
 return {'state':'observed' if exact else 'not_observed','window_minutes':minutes,'start':start.isoformat(),'end':end.isoformat(),
         'event_count':len(exact),'by_tool':_activity_counts(exact),'events':sorted(exact,key=lambda item:item['at'])[-20:],
         'unattributed_activity':{'state':'observed' if unattributed else 'not_observed','window_minutes':minutes,
          'start':start.isoformat(),'end':end.isoformat(),'event_count':len(unattributed),'by_tool':_activity_counts(unattributed),
          'boundary':'global_window_activity_not_bound_to_this_prompt; candidate_details_omitted'},
         'boundary':'same_prompt_binding_only'}

def _time_window_guidance_activity(prompt_at, entry_receipts=None, deliveries=None, window_minutes=2,
                                   global_activity=None, prompt_binding=None, prompt_ingress=None):
 """Show guidance deliveries only when tied to this Prompt; aggregate the rest without IDs."""
 anchor=_as_utc(prompt_at)
 if anchor is None:
  return {'state':'not_observed','window_minutes':window_minutes,'returned_count':0,
          'unattributed_activity':{'state':'not_observed','event_count':0,'returned_count':0},
          'boundary':'same_prompt_binding_only'}
 minutes=max(1,min(60,int(window_minutes or 2)));end=anchor+timedelta(minutes=minutes)
 records=[];unattributed=[];seen=set()
 sources=list(entry_receipts or [])+list(deliveries or [])
 sources.extend(row for row in (global_activity or [])
                if str(row.get('tool') or '').rsplit('__',1)[-1] in {'user_preference','get_preference','read_preference','read_preference_unit','get_task_guidance','read_guidance','read_guidance_unit'})
 for row in sources:
  raw=row.get('at')
  if raw is None:continue
  at=datetime.fromtimestamp(float(raw),timezone.utc) if isinstance(raw,(int,float)) else _as_utc(raw)
  if at is None or not anchor<=at<=end:continue
  tool=str(row.get('tool') or 'guidance').rsplit('__',1)[-1]
  row_binding={'session_id':row.get('session_id'),'turn_id':row.get('turn_id'),
               'hook_invocation_id':row.get('hook_invocation_id') or row.get('tool_call_id')}
  value={**row,'tool':tool,'at':at.isoformat()}
  if not _bound_to_prompt(value,prompt_binding,prompt_ingress,row_binding):
   unattributed.append({'tool':tool,'at':at.isoformat(),
                        'guidance_count':int(row.get('guidance_count') or row.get('entry_context_included_count') or row.get('included_count') or 0),
                        'deferred_count':int(row.get('deferred_count') or 0)})
   continue
  key=_event_key(value)
  if key in seen:continue
  seen.add(key)
  guidance=(row.get('rendered_guidance') or row.get('result') or {}) if isinstance(row,dict) else {}
  ids=[]
  for field in ('included','stable_profile','guidance_items','model_sections'):
   ids.extend(item.get('id') or item.get('section_id') for item in (guidance.get(field) or []) if isinstance(item,dict))
  records.append({'tool':tool,'at':at.isoformat(),
   'guidance_count':int(row.get('guidance_count') or row.get('entry_context_included_count') or row.get('included_count') or 0),
   'deferred_count':int(row.get('deferred_count') or 0),'ids':list(dict.fromkeys(value for value in ids if value))[:50],
   'host_state':row.get('host_state') or row.get('delivery_stage') or 'observed'})
 all_ids=list(dict.fromkeys(value for item in records for value in item.get('ids') or []))
 unattributed_by_tool={}
 for item in unattributed:
  bucket=unattributed_by_tool.setdefault(item['tool'],{'calls':0,'returned':0,'deferred':0,'latest_at':None})
  bucket['calls']+=1;bucket['returned']+=item['guidance_count'];bucket['deferred']+=item['deferred_count']
  bucket['latest_at']=max(bucket['latest_at'] or item['at'],item['at'])
 return {'state':'observed' if records else 'not_observed','window_minutes':minutes,'start':anchor.isoformat(),'end':end.isoformat(),
  'event_count':len(records),'returned_count':sum(item['guidance_count'] for item in records),
  'deferred_count':sum(item['deferred_count'] for item in records),'ids':all_ids[:100],
  'events':sorted(records,key=lambda item:item['at'])[-20:],
  'unattributed_activity':{'state':'observed' if unattributed else 'not_observed','window_minutes':minutes,
   'start':anchor.isoformat(),'end':end.isoformat(),'event_count':len(unattributed),
   'returned_count':sum(item['guidance_count'] for item in unattributed),'deferred_count':sum(item['deferred_count'] for item in unattributed),
   'by_tool':unattributed_by_tool,'boundary':'global_window_activity_not_bound_to_this_prompt; candidate_details_omitted'},
  'boundary':'same_prompt_binding_only'}

def _route_receipt_for_prompt(receipt, prompt_binding, prompt_ingress):
 if not receipt:return None
 value=dict(receipt);binding=dict(value.get('prompt_binding') or {})
 events=[];unattributed_count=0
 for event in value.get('tool_events') or []:
  if _bound_to_prompt(event,prompt_binding,prompt_ingress,binding):events.append(event)
  else:unattributed_count+=1
 value['tool_events']=events
 if unattributed_count:value['unattributed_tool_event_count']=unattributed_count
 return value

def _hydrate_time_window_content(activity,limit=16):
 value=dict(activity or {});ids=[]
 candidate_ids=[];research_ids=[];queries=[];anchors=[]
 for event in value.get('events') or []:
  ids.extend(event.get('memory_ids') or [])
  if event.get('memory_id'):ids.append(event['memory_id'])
  research_id=str(event.get('research_id') or '')
  if research_id and re.fullmatch(r'[a-zA-Z0-9_-]{1,80}',research_id):
   research_ids.append(research_id)
   try:
    research_path=STATE_ROOT/'memory-os/research'/(research_id+'.json')
    research=json.loads(research_path.read_text(encoding='utf-8'))
    candidate_ids.extend(str(item) for item in (research.get('memory_ids') or []) if item)
    queries.append({'research_id':research_id,'query':research.get('query'),
                    'candidate_count':len(research.get('memory_ids') or []),
                    'anchors':research.get('explicit_anchor_terms') or []})
   except (OSError,ValueError,TypeError):pass
 ids=list(dict.fromkeys(str(item) for item in ids if item))[:max(1,min(30,int(limit)))]
 candidate_ids=[item for item in dict.fromkeys(candidate_ids) if item not in ids]
 candidate_count=len(candidate_ids)
 candidate_ids=candidate_ids[:max(1,min(30,int(limit)))]
 items=[]
 for memory_id in ids:
  try:memory=get(API,f'/v1/default/banks/{urllib.parse.quote(BANK,safe="")}/memories/{urllib.parse.quote(memory_id,safe="")}',3)
  except Exception:continue
  if memory.get('id'):
   items.append({'id':memory.get('id'),'type':memory.get('type') or memory.get('fact_type'),'text':str(memory.get('text') or memory.get('content') or '')[:500]})
 candidate_items=[]
 for memory_id in candidate_ids:
  try:memory=get(API,f'/v1/default/banks/{urllib.parse.quote(BANK,safe="")}/memories/{urllib.parse.quote(memory_id,safe="")}',3)
  except Exception:continue
  if memory.get('id')==memory_id and memory.get('state')=='valid':
   candidate_items.append({'id':memory.get('id'),'type':memory.get('type') or memory.get('fact_type'),
    'text':str(memory.get('text') or memory.get('content') or '')[:500],'candidate_only':True})
 event_candidates=max([int(event.get('candidate_count') or 0) for event in value.get('events') or []] or [0])
 value['items']=items;value['candidate_items']=candidate_items;value['candidate_count']=max(event_candidates,candidate_count)
 value['candidate_queries']=queries;value['candidate_preview_count']=len(candidate_items)
 value['candidate_research_ids']=list(dict.fromkeys(research_ids));return value

def _evidence_decision(check_id):
 if not check_id:return None
 path=STATE_ROOT/'audit/evidence-decisions'/(hashlib.sha256(str(check_id).encode()).hexdigest()+'.json')
 try:return json.loads(path.read_text(encoding='utf-8'))
 except (OSError,ValueError,TypeError):return None

def _candidate_events(row):
 events=list((row.get('memory_route_receipt') or {}).get('tool_events') or [])
 activity=row.get('time_window_activity') or {}
 if activity.get('boundary')=='same_prompt_binding_only':events+=list(activity.get('events') or [])
 seen=set();result=[]
 for event in events:
  key=(event.get('tool'),event.get('research_id'),event.get('at'))
  if key not in seen:seen.add(key);result.append(event)
 return result

def candidate_groups_for_prompt(row):
 groups=[]
 if row.get('system_probe') and row['system_probe'].get('calls'):
  groups.append({'id':'system_probe','actor':'system_probe','count':row['system_probe'].get('candidate_count')})
 for event in _candidate_events(row):
  rid=event.get('research_id')
  if rid and not any(group['id']==rid for group in groups):
   groups.append({'id':rid,'actor':'agent_mcp','count':event.get('candidate_count')})
 return groups

def candidate_audit_page(row,group,offset=0,limit=10):
 from lib.candidate_audit import page,mark_delivery
 if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=20:
  raise ValueError('invalid_candidate_pagination')
 if group=='system_probe':
  probe=row.get('system_probe') or (row.get('memory_route_receipt') or {}).get('system_probe') or {}
  snapshots=probe.get('candidate_audit') or []
  value=page(snapshots,offset=offset,limit=limit,actor='system_probe')
  if not snapshots and probe.get('candidate_count'):
   value.update(total=probe['candidate_count'],snapshot_status='historical_snapshot_missing')
  value['query']=(row.get('memory_route_receipt') or {}).get('query') or row.get('user_prompt')
  return value
 events=_candidate_events(row)
 allowed={str(event.get('research_id')) for event in events if event.get('research_id')}
 if group not in allowed or not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}',group):
  return {'actor':'agent_mcp','items':[],'total':0,'snapshot_status':'not_bound_to_prompt'}
 try:state=json.loads((STATE_ROOT/'memory-os/research'/(group+'.json')).read_text())
 except (OSError,ValueError):return {'actor':'agent_mcp','items':[],'total':0,'snapshot_status':'source_receipt_unavailable'}
 snapshots={item['id']:item for item in state.get('candidate_audit') or []}
 all_rows=[snapshots.get(mid,{'id':mid,'text':'','reason':'historical_snapshot_missing',
                            'delivery':'not_returned','snapshot_stage':'missing'}) for mid in state.get('memory_ids') or []]
 check_id=row.get('hook_invocation_id') or (row.get('memory_route_receipt') or {}).get('check_id')
 delivered=[item for event in state.get('delivery_events') or []
            if check_id and event.get('check_id')==check_id and event.get('binding_state','prompt_bound')=='prompt_bound'
            for item in event.get('delivered_items') or []]
 value=page(mark_delivery(all_rows,delivered),offset=offset,limit=limit,actor='agent_mcp')
 value['query']=state.get('query')
 value['discovered_count']=len(state.get('memory_ids') or [])
 missing=sum(item.get('snapshot_stage')!='discovery' for item in all_rows)
 value['missing_discovery_snapshot_count']=missing
 if missing:value['snapshot_status']='partial_history' if snapshots else 'historical_snapshot_missing'
 return value

def _sanitize_route_receipt(value):
 result=dict(value or {});probe=dict(result.get('catalog_probe') or {})
 entities=[str(item) for item in probe.get('matched_entities') or []][:8]
 safe=[]
 for hint in (result.get('catalog_hints') or [])[:5]:
  if not isinstance(hint,dict):continue
  kind=str(hint.get('type') or 'memory');kind_label={'world':'事实','experience':'经历','observation':'观察'}.get(kind,kind)
  topic=('、'.join(entities[:3])+'相关'+kind_label+'记录') if entities else ('与当前问题语义相关的'+kind_label+'记录')
  safe.append({k:hint.get(k) for k in ('memory_id','type','mentioned_at','occurred_start','occurred_end','document_id','state') if hint.get(k) is not None}|{'topic':topic})
 result['catalog_probe']=probe;result['catalog_hints']=safe
 return result

def _match_recall_trace(prompt_row, entries):
 hook_id=str(prompt_row.get('hook_invocation_id') or '')
 turn_id=str(prompt_row.get('turn_id') or '')
 session_id=str(prompt_row.get('session_id') or '')
 candidates=[]
 for key,value in (entries or {}).items():
  if not isinstance(value,dict) or value.get('event')!='recall': continue
  exact_hook=bool(hook_id and (str(key)==hook_id or str(value.get('hook_invocation_id') or '')==hook_id))
  exact_turn=bool(turn_id and str(value.get('turn_id') or '')==turn_id and (not session_id or str(value.get('session_id') or '')==session_id))
  if exact_hook or exact_turn: candidates.append((2 if exact_hook else 1,str(value.get('at') or ''),value))
 return max(candidates,key=lambda row:(row[0],row[1]))[2] if candidates else None

def _compact_trace_item(item):
 admission=item.get('admission') or {}
 return {'id':item.get('id'),'type':item.get('type') or item.get('fact_type'),
  'text':str(item.get('text') or item.get('text_preview') or '')[:1600],
  'score':item.get('score') or (item.get('scores') or {}).get('final'),
  'document_id':item.get('document_id'),'mentioned_at':item.get('mentioned_at'),
  'entities':list(item.get('entities') or [])[:20],
  'admission_decision':admission.get('decision'),'admission_reason':admission.get('reason')}

def _project_recall_trace(trace):
 if not trace:return None
 admission=trace.get('relevance_admission') or {};effectiveness=trace.get('memory_effectiveness') or {};claims=trace.get('claim_receipt') or {}
 items=[_compact_trace_item(item) for item in (trace.get('selected_results') or [])[:20] if isinstance(item,dict)]
 delivered_ids=effectiveness.get('injected_ids') or effectiveness.get('actual_injected_ids') or claims.get('actual_hook_injected_claim_ids')
 delivery_measured=bool(effectiveness.get('injection_receipt_state')) or bool(delivered_ids)
 return {'route':'recall','state':'returned' if items or int(trace.get('result_count') or 0)>0 else ('executed_empty' if trace.get('execution_complete') else 'incomplete'),
  'execution_id':trace.get('execution_id') or trace.get('query_id'),'query':trace.get('full_prompt') or trace.get('query_preview'),
  'shape':trace.get('shape'),'strategies':list(trace.get('strategies') or []),
  'queries_requested':int(trace.get('queries_requested') or 0),'queries_completed':int(trace.get('queries_completed') or 0),
  'elapsed_ms':trace.get('elapsed_ms'),'coverage_complete':bool(trace.get('coverage_complete')),
  'candidate_count':int(admission.get('candidate_count') or admission.get('fused_candidate_count') or 0),
  'qualified_count':int(admission.get('qualified_count') or admission.get('admitted_count') or len(items)),
  'rejected_count':int(admission.get('rejected_count') or 0),
  'deferred_count':int(admission.get('deferred_for_token_budget_count') or len(admission.get('deferred_items') or [])),
  'prepared_item_count':len(items),'returned_to_host_count':len(delivered_ids or []) if delivery_measured else None,
  'delivery_state':effectiveness.get('injection_receipt_state') or ('observed' if delivered_ids else 'not_measured'),'items':items,
  'errors':list(trace.get('errors') or [])[:10]}

def _research_binding_from_envelopes(research_id,envelopes):
 ordered=sorted((row for row in envelopes if isinstance(row,dict)),key=lambda row:int(row.get('capture_order') or 0))
 tool=None
 for row in ordered:
  payload=row.get('source_payload') or {}
  if row.get('origin_class')=='tool_result' and research_id in str(row.get('body_preview') or '') and payload.get('tool_name') in HISTORY_TOOL_NAMES: tool=row;break
 if not tool:return None
 preceding=[row for row in ordered if int(row.get('capture_order') or 0)<int(tool.get('capture_order') or 0) and row.get('origin_class')=='user_request' and row.get('session_id')==tool.get('session_id') and row.get('task_id')==tool.get('task_id')]
 if not preceding:return None
 user=max(preceding,key=lambda row:int(row.get('capture_order') or 0));payload=user.get('source_payload') or {}
 return {'session_id':user.get('session_id'),'turn_id':payload.get('turn_id'),'hook_invocation_id':payload.get('hook_invocation_id'),'user_prompt':_norm_prompt(payload.get('prompt') or user.get('body_preview'))}

def _extract_research_tool_result(research_id,envelope,capture_path):
 payload=(envelope or {}).get('source_payload') or {};response=_receipts_module.resolve_tool_response(payload,capture_path) or {}
 value=None
 for block in response.get('content') or []:
  if not isinstance(block,dict) or not isinstance(block.get('text'),str):continue
  try: candidate=json.loads(block['text'])
  except (ValueError,TypeError):continue
  if str(candidate.get('research_id') or '')==str(research_id):value=candidate;break
 if not value:return None
 items=[_compact_trace_item(item) for item in (value.get('memories') or [])[:20] if isinstance(item,dict)]
 delivery=value.get('delivery') or {}
 return {'tool_name':payload.get('tool_name'),'offset':int(value.get('offset') or 0),'next_offset':value.get('next_offset'),
  'discovered_count':int(value.get('discovered_reference_count') or len(items)),
  'returned_to_host_count':len(items),'items':items,'transport':delivery.get('transport') or 'mcp_tool_result',
  'host_visibility':delivery.get('host_visibility') or 'unknown','answer_use':delivery.get('answer_use') or 'not_measured'}

def _research_capture_projection(capture_path,research_id):
 envelopes=[]
 try:
  connection=sqlite3.connect(str(capture_path));connection.row_factory=sqlite3.Row
  matches=connection.execute("SELECT capture_order,envelope_json FROM captures WHERE envelope_json LIKE ? ORDER BY capture_order",('%'+str(research_id)+'%',)).fetchall()
  for match in matches:
   try: envelope=json.loads(match['envelope_json']);envelope['capture_order']=match['capture_order']
   except (ValueError,TypeError):continue
   payload=envelope.get('source_payload') or {}
   if envelope.get('origin_class')!='tool_result' or payload.get('tool_name') not in HISTORY_TOOL_NAMES:continue
   envelopes.append(envelope)
  if envelopes:
   tool=envelopes[0]
   previous=connection.execute("SELECT capture_order,envelope_json FROM captures WHERE capture_order<? AND json_extract(envelope_json,'$.session_id')=? AND json_extract(envelope_json,'$.task_id')=? AND json_extract(envelope_json,'$.origin_class')='user_request' ORDER BY capture_order DESC LIMIT 1",(tool['capture_order'],tool.get('session_id'),tool.get('task_id'))).fetchone()
   if previous:
    user=json.loads(previous['envelope_json']);user['capture_order']=previous['capture_order'];envelopes.append(user)
  connection.close()
 except Exception:return {'binding':None,'pages':[],'items':[],'returned_to_host_count':None}
 binding=_research_binding_from_envelopes(research_id,envelopes)
 pages=[];items=[];seen=set()
 for envelope in envelopes:
  page=_extract_research_tool_result(research_id,envelope,capture_path)
  if not page:continue
  pages.append({k:page.get(k) for k in ('tool_name','offset','next_offset','returned_to_host_count','transport','host_visibility','answer_use')})
  for item in page.get('items') or []:
   if item.get('id') in seen:continue
   seen.add(item.get('id'));items.append(item)
 return {'binding':binding,'pages':pages,'items':items[:20],'returned_to_host_count':len(seen)}

def _research_capture_projection_map(capture_path,research_ids):
 result={};research_ids={str(value) for value in research_ids if value}
 if not research_ids:return result
 try:
  connection=sqlite3.connect(str(capture_path));connection.row_factory=sqlite3.Row
  maximum=int(connection.execute('SELECT COALESCE(MAX(capture_order),0) FROM captures').fetchone()[0]);floor=max(0,maximum-6000)
  tool_rows=connection.execute("SELECT capture_order,envelope_json FROM captures WHERE capture_order>=? AND json_extract(envelope_json,'$.origin_class')='tool_result' AND envelope_json LIKE '%research_id%' ORDER BY capture_order",(floor,)).fetchall()
  user_rows=connection.execute("SELECT capture_order,envelope_json FROM captures WHERE capture_order>=? AND json_extract(envelope_json,'$.origin_class')='user_request' ORDER BY capture_order",(floor,)).fetchall();connection.close()
  users=[]
  for raw in user_rows:
   try: value=json.loads(raw['envelope_json']);value['capture_order']=raw['capture_order'];users.append(value)
   except (ValueError,TypeError):pass
  tools=[]
  for raw in tool_rows:
   try: value=json.loads(raw['envelope_json']);value['capture_order']=raw['capture_order']
   except (ValueError,TypeError):continue
   tool_name=(value.get('source_payload') or {}).get('tool_name')
   if tool_name in HISTORY_TOOL_NAMES and any(research_id in str(value.get('body_preview') or '') for research_id in research_ids):tools.append(value)
  for research_id in research_ids:
   relevant=[row for row in tools if research_id in str(row.get('body_preview') or '')]
   if not relevant:continue
   tool=relevant[0];preceding=[row for row in users if int(row.get('capture_order') or 0)<int(tool.get('capture_order') or 0) and row.get('session_id')==tool.get('session_id') and row.get('task_id')==tool.get('task_id')]
   envelopes=list(relevant)+([max(preceding,key=lambda row:int(row.get('capture_order') or 0))] if preceding else [])
   binding=_research_binding_from_envelopes(research_id,envelopes);pages=[];items=[];seen=set()
   for envelope in relevant:
    page=_extract_research_tool_result(research_id,envelope,capture_path)
    if not page:continue
    pages.append({k:page.get(k) for k in ('tool_name','offset','next_offset','returned_to_host_count','transport','host_visibility','answer_use')})
    for item in page.get('items') or []:
     if item.get('id') in seen:continue
     seen.add(item.get('id'));items.append(item)
   result[research_id]={'binding':binding,'pages':pages,'items':items[:20],'returned_to_host_count':len(seen)}
 except Exception:return {}
 return result

def _captured_history_for_turn(capture_path,session_id,turn_id):
 """Project real same-turn MCP receipts even after workspace TTL cleanup."""
 if not session_id or not turn_id:return None
 pages=[];items=[];seen=set()
 try:
  connection=sqlite3.connect(str(capture_path));connection.row_factory=sqlite3.Row
  rows=connection.execute("SELECT envelope_json FROM captures WHERE json_extract(envelope_json,'$.origin_class')='tool_result' AND json_extract(envelope_json,'$.source_payload.session_id')=? AND json_extract(envelope_json,'$.source_payload.turn_id')=? ORDER BY capture_order",(session_id,turn_id)).fetchall();connection.close()
  for row in rows:
   event=json.loads(row['envelope_json']);source=event.get('source_payload') or {};tool=str(source.get('tool_name') or '')
   if tool not in HISTORY_TOOL_NAMES:continue
   response=_receipts_module.resolve_tool_response(source,capture_path) or {}
   for block in response.get('content') or []:
    if not isinstance(block,dict) or block.get('type')!='text':continue
    try:value=json.loads(block.get('text') or '')
    except (ValueError,TypeError):continue
    if not isinstance(value,dict):continue
    memories=[_compact_trace_item(item) for item in (value.get('memories') or [])[:20] if isinstance(item,dict)]
    pages.append({'tool_name':tool,'research_id':value.get('research_id'),'offset':value.get('offset'),'next_offset':value.get('next_offset'),'returned_to_host_count':len(memories)})
    for item in memories:
     if item.get('id') in seen:continue
     seen.add(item.get('id'));items.append(item)
 except (sqlite3.Error,OSError,ValueError,TypeError):return None
 if not pages:return None
 tools={page['tool_name'] for page in pages};research=any(name.endswith('__research') or name.endswith('__read_research') for name in tools)
 return {'route':'agent_mcp_research' if research else 'agent_mcp_recall','mode':'research' if research else 'recall','controller_state':'same_turn_host_receipt','state':'observed','recall':None,'research':pages if research else [],'candidate_count':sum(int(page.get('returned_to_host_count') or 0) for page in pages),'returned_to_host_count':len(seen),'unread_candidate_count':sum(1 for page in pages if page.get('next_offset') is not None),'items':items[:20],'boundary':'same_turn_PostToolUse_MCP_response'}

def guidance_prompt_list(limit=20,cursor='0',host='all',detail_id=None,query_text='',prompt_source='natural'):
 try: offset=max(0,int(cursor or 0))
 except Exception: offset=0
 try: page_limit=max(1,min(50,int(limit)))
 except (ValueError,TypeError,OverflowError):page_limit=20
 ingress=_prompt_ingress_rows(detail_id=detail_id,page_offset=offset,page_limit=page_limit,host=host,query_text=query_text,prompt_source=prompt_source);hooks=_hook_output_rows();needle_terms=[part for part in _norm_prompt(query_text).split() if part]
 population=[row for row in ingress if (str(host or 'all').casefold()=='all' or str(row.get('host_id') or 'codex').casefold()==str(host).casefold()) and
             (not needle_terms or all(term in _norm_prompt(row.get('prompt_preview')) for term in needle_terms))]
 population,source_projection=prompt_population_projection(population,prompt_source,detail_id=detail_id)
 # UserPromptSubmit's forced get_preference call is recorded in the
 # entry-adapter receipt lane, separately from full guidance deliveries.
 entry_receipts=[]
 entry_root=STATE_ROOT/'audit/guidance-entry-receipts'
 for path in sorted(entry_root.glob('*.json'),key=lambda p:p.stat().st_mtime,reverse=True)[:2000]:
  try:
   value=json.loads(path.read_text(encoding='utf-8'))
   if isinstance(value,dict) and value.get('hook_invocation_id'): entry_receipts.append(value)
  except (OSError,ValueError,TypeError): pass
 # The prompt projection must distinguish “recall was executed but returned
 # zero” from “no recall receipt exists”.  The append-only trace index is the
 # authoritative bounded source for that distinction; it does not expose
 # memory text, only execution/result metadata.
 # Do not load the 19MB JSON trace index into the long-lived status process.
 # The prompt list only needs a small execution-keyed lookup for the selected
 # visible turns; the full trace remains available to the detail/audit path.
 recall_index_path=STATE_ROOT/'control-plane/recall-trace-index.json'
 recall_entries={}
 try:
  visible_hook_ids={str(x.get('hook_invocation_id')) for x in entry_receipts if x.get('hook_invocation_id')}
  visible_hook_ids.update(str(x.get('hook_invocation_id')) for x in hooks if x.get('hook_invocation_id'))
  if visible_hook_ids:
   proc=subprocess.run(['jq','-c','--argjson','ids',json.dumps(list(visible_hook_ids)),
     '.entries | to_entries[] | select((.key as $k | ($ids | index($k))) or (.value.hook_invocation_id as $h | ($ids | index($h)))) | {key:.key,value:.value}',str(recall_index_path)],capture_output=True,text=True,timeout=5)
   if proc.returncode==0:
    for line in proc.stdout.splitlines():
     try:
      item=json.loads(line);recall_entries[str(item.get('key'))]=item.get('value') or {}
     except (ValueError,TypeError): pass
 except Exception: pass
 # Include that lane so a successful instruction check is never shown as an
 # unexplained unknown merely because no historical recall was requested.
 delivery=guidance_delivery_list(100).get('items') or []
 research_rows=research_snapshot(include_host_receipts=False).get('items') or []
 selected=[]; used_delivery=set(); used_hook=set(); route_receipts=_route_receipt_rows(); global_activity=_global_mcp_activity_rows()
 for row in population:
  if str(host or 'all').casefold()!='all' and str(row.get('host_id') or 'codex').casefold()!=str(host).casefold(): continue
  prompt=_norm_prompt(row.get('prompt_preview')); fp=row.get('prompt_fingerprint')
  if needle_terms and not all(term in prompt for term in needle_terms): continue
  entry_record=next((entry for entry in entry_receipts if entry.get('hook_invocation_id')==row.get('hook_invocation_id') and entry.get('session_id')==row.get('session_id')), {})
  manual=dict(entry_record.get('instruction') or {}) or None
  try: row_epoch=datetime.fromisoformat(str(row.get('at')).replace('Z','+00:00')).timestamp()
  except Exception: row_epoch=0
  # Bind receipts by immutable host identity first. Text is only a fallback:
  # task guidance commonly normalizes or summarizes the user's wording.
  candidates=[x for x in delivery if id(x) not in used_delivery and (
   (row.get('session_id') and x.get('session_id')==row.get('session_id') and row.get('turn_id') and x.get('turn_id')==row.get('turn_id')) or
   (row.get('hook_invocation_id') and x.get('tool_call_id')==row.get('hook_invocation_id')))]
  if not candidates:
   candidates=[x for x in delivery if id(x) not in used_delivery and _norm_prompt((x.get('task') or {}).get('current_user_message') or (x.get('task') or {}).get('objective'))==prompt]
  if not candidates:
   entry_matches=[x for x in entry_receipts if (
    x.get('session_id')==row.get('session_id') and x.get('turn_id')==row.get('turn_id') and x.get('hook_invocation_id')==row.get('hook_invocation_id'))]
   if entry_matches:
    er=entry_matches[0]
    try: entry_at=datetime.fromisoformat(str(er.get('at')).replace('Z','+00:00')).timestamp()
    except (TypeError,ValueError,OverflowError): entry_at=0
    pseudo={'occurrence_id':'entry:'+str(er.get('hook_invocation_id')),
     'at':entry_at,'session_id':er.get('session_id'),'turn_id':er.get('turn_id'),
     'tool_call_id':er.get('hook_invocation_id'),'guidance_count':er.get('entry_context_included_count',er.get('included_count',0)) or 0,
     'model_section_count':er.get('model_section_count',0) or 0,
     'deferred_count':er.get('deferred_count',0) or 0,'host_state':'hook_context_prepared' if er.get('instruction') else 'historical_receipt',
     'model_attention':'not_measured','task':(er.get('request') or {}).get('task') or {},
     'task_state':er.get('task_state'),'stable_profile_count':er.get('stable_profile_count',0) or 0,
     'preference_candidate_count':er.get('preference_candidate_count',er.get('entry_context_included_count',0)) or 0,
     'stable_profile':(er.get('rendered_guidance', er.get('result')) or {}).get('stable_profile') or [],
     'guidance_items':(er.get('rendered_guidance', er.get('result')) or {}).get('included') or [],
     'model_sections':(er.get('rendered_guidance', er.get('result')) or {}).get('model_sections') or [],
     'deferred':(er.get('rendered_guidance', er.get('result')) or {}).get('deferred') or [],
     'coverage':er.get('coverage') or (er.get('result') or {}).get('coverage')}
    candidates=[pseudo]
  candidates.sort(key=lambda x:abs(float(x.get('at') or 0)-row_epoch) if row_epoch else 0)
  guidance=candidates[0] if candidates and (not row_epoch or abs(float(candidates[0].get('at') or 0)-row_epoch)<=900) else None
  if guidance: used_delivery.add(id(guidance))
  # Prompt list is a projection, not a dump of every guidance source. Keeping
  # full deferred items/evidence_refs for ~1,700 rows caused the status server
  # to retain hundreds of MB. Detail/audit endpoints still re-read the selected
  # receipt; the list only needs counts and a few visible texts.
  if guidance:
   guidance={
    'occurrence_id':guidance.get('occurrence_id'),'at':guidance.get('at'),
    'session_id':guidance.get('session_id'),'turn_id':guidance.get('turn_id'),
    'tool_call_id':guidance.get('tool_call_id'),'guidance_count':guidance.get('guidance_count',0),
    'model_section_count':guidance.get('model_section_count',0),'deferred_count':guidance.get('deferred_count',0),
    'host_state':guidance.get('host_state'),'model_attention':guidance.get('model_attention'),
    'coverage':guidance.get('coverage'),'task':guidance.get('task') or {},
    'stable_profile_count':guidance.get('stable_profile_count',0),'preference_candidate_count':guidance.get('preference_candidate_count',0),
    'guidance_items':[{'id':x.get('id'),'text':str(x.get('text') or '')[:1200]} for x in (guidance.get('guidance_items') or [])[:8] if isinstance(x,dict)],
    'model_sections':[{'section_id':x.get('section_id'),'text':str(x.get('text') or '')[:800]} for x in (guidance.get('model_sections') or [])[:8] if isinstance(x,dict)],
   }
  hook_matches=[x for x in hooks if id(x) not in used_hook and (not row.get('hook_invocation_id') or x.get('hook_invocation_id')==row.get('hook_invocation_id'))]
  if not hook_matches:
   hook_matches=[x for x in hooks if id(x) not in used_hook and x.get('_raw_prompt')==prompt]
  for x in hook_matches: used_hook.add(id(x))
  memory=[]; history_observed=False; source_observed=False; hook_history_decision=None; hook_history_decision_evidence=None
  system_probe=next((item.get('system_probe') for item in hook_matches if item.get('system_probe')),None)
  for h in hook_matches:
   eff=h.get('memory_effectiveness') or {}
   injected=eff.get('actual_injected_count',eff.get('injected_count'))
   action=str(eff.get('memory_action') or h.get('memory_action') or h.get('outcome') or '').casefold()
   history_observed=history_observed or action in {'focused_recall','recall_timeout','agent_mcp_recall','agent_mcp_research'}
   source_observed=source_observed or bool(eff.get('source_reads') or h.get('source_reads'))
   hook_history_decision=hook_history_decision or h.get('history_decision')
   hook_history_decision_evidence=hook_history_decision_evidence or h.get('history_decision_evidence')
   memory.append({'state':eff.get('memory_action') or h.get('memory_action') or h.get('outcome') or 'observed','injected_count':injected,'receipt_state':eff.get('injection_receipt_state') or h.get('injection_receipt_state'),'injected_ids':eff.get('injected_ids') or h.get('injected_ids') or [],'memory_needs':h.get('memory_needs') or {}})
  trace=_match_recall_trace(row,recall_entries)
  # Cache-reused foreground traces carry the immutable predecessor ID. Follow
  # it so the UI reports the original recall execution/result counts instead
  # of the cache shell's zero query counters.
  if trace and trace.get('reused_from_execution_id'):
   predecessor=recall_entries.get(str(trace.get('reused_from_execution_id')))
   if predecessor: trace={**predecessor,'reused_by_execution_id':trace.get('execution_id') or trace.get('query_id')}
  trace_result_count=int((trace or {}).get('result_count') or 0)
  trace_queries=int((trace or {}).get('queries_completed') or 0)
  trace_requested=int((trace or {}).get('queries_requested') or 0)
  trace_items=(trace or {}).get('selected_results') or (trace or {}).get('memory_effectiveness',{}).get('items') or []
  research_matches=[item for item in research_rows if (item.get('binding') or {}).get('turn_id')==row.get('turn_id') and (not row.get('session_id') or (item.get('binding') or {}).get('session_id')==row.get('session_id'))]
  if trace and str((trace or {}).get('event') or '')=='recall':
   if trace_result_count>0 or trace_items: historical_state='observed'
   elif (trace or {}).get('execution_complete') and (trace_queries>0 or trace_requested>0): historical_state='executed_empty'
   else: historical_state='executed_no_result'
  elif history_observed: historical_state='observed'
  elif memory: historical_state='not_observed'
  else: historical_state='unknown'
  historical_audit=None
  if trace and str((trace or {}).get('event') or '')=='recall':
   recall_projection=_project_recall_trace(trace);historical_audit={**recall_projection,'state':historical_state,'recall':recall_projection,'research':[]}
  if system_probe and system_probe.get('calls'):
   historical_state={'returned':'observed','empty':'executed_empty','unavailable':'unknown'}.get(system_probe.get('state'),'unknown')
   historical_audit={'route':'system_probe_recall','mode':'system_probe','state':historical_state,
    'controller_state':'system_probe_direct','candidate_count':system_probe.get('candidate_count'),
    'returned_to_host_count':system_probe.get('returned_count'),'items':system_probe.get('items') or [],
    'delivery_state':system_probe.get('delivery_stage'),'boundary':'system_probe_not_agent_expansion'}
  if research_matches:
   tool_names={str(page.get('tool_name') or '') for item in research_matches for page in (item.get('pages') or [])}
   agent_recall=any(name.endswith('__recall') for name in tool_names)
   agent_research=any(name.endswith('__research') or name.endswith('__read_research') for name in tool_names)
   route='agent_mcp_recall' if agent_recall and not agent_research else 'agent_mcp_research' if agent_research else 'agent_mcp_history'
   candidate_count=sum(int(item.get('discovered_count') or 0) for item in research_matches)
   returned_count=sum(int(item.get('returned_to_host_count') or 0) for item in research_matches)
   candidate_mode='candidate_discovery' if agent_recall else 'research'
   route='recall_and_research' if historical_audit else route;historical_state='observed'
   if historical_audit:historical_audit.update(route=route,state=historical_state,research=research_matches)
   else:historical_audit={'route':route,'mode':candidate_mode,'controller_state':'not_in_candidate_path','state':historical_state,'recall':None,'research':research_matches,'candidate_count':candidate_count,'returned_to_host_count':returned_count,'unread_candidate_count':max(0,candidate_count-returned_count)}
  elif not historical_audit and detail_id and str(fp or hashlib.sha256(prompt.encode()).hexdigest()[:16])+':'+str(row.get('at') or '')==detail_id:
   # The list stays O(1) per row.  A selected detail may pay one bounded
   # same-turn lookup, which recovers receipts after research workspace TTL.
   captured=_captured_history_for_turn(STATE_ROOT/'memory-os/capture/capture.sqlite3',row.get('session_id'),row.get('turn_id'))
   if captured:
    historical_audit=captured;historical_state='observed'
  prompt_binding={'session_id':row.get('session_id'),'turn_id':row.get('turn_id'),'hook_invocation_id':row.get('hook_invocation_id')}
  raw_route_receipt=next((item for item in route_receipts if str((item.get('prompt_binding') or {}).get('hook_invocation_id') or '')==str(row.get('hook_invocation_id') or '')),None)
  route_receipt=_route_receipt_for_prompt(raw_route_receipt,prompt_binding,ingress)
  time_window_activity=_time_window_tool_activity(row.get('at'),[route_receipt] if route_receipt else [],window_minutes=2,
      global_activity=global_activity,prompt_binding=prompt_binding,prompt_ingress=ingress)
  time_window_guidance_activity=_time_window_guidance_activity(row.get('at'),[entry_record] if entry_record else [],
      [guidance] if guidance else [],window_minutes=2,global_activity=global_activity,
      prompt_binding=prompt_binding,prompt_ingress=ingress)
  evidence_decision=_evidence_decision(row.get('hook_invocation_id'))
  history_decision = hook_history_decision or ('needed' if historical_state in ('observed','executed_empty','executed_no_result') else 'unknown')
  history_decision_evidence = hook_history_decision_evidence or ('recall_trace' if trace else 'hook_memory_effectiveness' if history_observed else 'missing_history_receipt')
  navigation=entry_record.get('navigation_map') or None
  row['system_probe']=system_probe
  history_plan = next((item.get('history_plan') for item in hook_matches if item.get('history_plan')), None)
  selected.append({'prompt_id':str(fp or hashlib.sha256(prompt.encode()).hexdigest()[:16])+':'+str(row.get('at') or ''),'at':row.get('at'),'user_prompt':prompt,'prompt_origin':row.get('prompt_origin'),'origin_kind':row.get('origin_kind','unknown'),'origin_status':row.get('origin_status','unknown'),'origin_evidence':row.get('origin_evidence'),'session_id':row.get('session_id'),'turn_id':row.get('turn_id'),'hook_invocation_id':row.get('hook_invocation_id'),'source':row.get('source'),'instruction_receipt':manual,'navigation_map':navigation,'guidance_receipt':guidance,'system_probe':system_probe,'history_plan':history_plan,'task_state':entry_record.get('task_state') or (guidance or {}).get('task_state'),'evidence_decision':evidence_decision,'hook_receipts':memory,'historical_audit':historical_audit,'memory_route_receipt':route_receipt,'time_window_activity':time_window_activity,'time_window_guidance_activity':time_window_guidance_activity,'history_decision':history_decision,'history_decision_evidence':history_decision_evidence,'routes':{'entry_guidance':'observed_entry_adapter','multi_dimensional_preference':'observed' if guidance or memory else 'not_observed','historical_memory':historical_state,'source_read':'observed' if source_observed else 'unknown'}})
 items=([row for row in selected if row.get('prompt_id')==detail_id] if detail_id else selected[offset:offset+page_limit])
 next_cursor=str(offset+len(items)) if offset+len(items)<len(selected) else None
 return {'schema':'guidance.user-prompt-list.v2','items':items,'count':len(items),'total':len(selected),'has_more':next_cursor is not None,'next_cursor':next_cursor,'host':host,**source_projection}

def guidance_prompt_detail(prompt_id,candidate_group=None,offset=0,limit=10):
 all_rows=guidance_prompt_list(1,0,'all',detail_id=prompt_id).get('items') or []
 for row in all_rows:
  if row.get('prompt_id')!=prompt_id:continue
  if candidate_group is not None:
   return candidate_audit_page(row,candidate_group,offset,limit)
  # The list projection intentionally omits large deferred/admission trees.
  # Rehydrate only this selected Prompt from its immutable receipt.
  hook_id=str(row.get('hook_invocation_id') or '')
  guidance=dict(row.get('guidance_receipt') or {})
  if hook_id:
   for path in (STATE_ROOT/'audit/guidance-entry-receipts').glob('*.json'):
    try:
     source=json.loads(path.read_text(encoding='utf-8'))
    except (OSError,ValueError,TypeError):continue
    if str(source.get('hook_invocation_id') or '')!=hook_id:continue
    result=dict(source.get('rendered_guidance', source.get('result')) or {})
    guidance.update({
     'guidance_items':list(result.get('included') or []),
     'model_sections':list(result.get('model_sections') or []),
     'deferred':list(result.get('deferred') or []),
     'budget':dict(result.get('budget') or {}),
     'coverage':result.get('coverage') or guidance.get('coverage'),
    })
    break
  row['guidance_receipt']=guidance
  entries=(load(STATE_ROOT/'control-plane/recall-trace-index.json',{}) or {}).get('entries') or {}
  trace=dict(entries.get(hook_id) or {})
  if row.get('historical_audit') and trace:
   admission=dict(trace.get('relevance_admission') or {})
   row['historical_audit']['admission_items']=[_compact_trace_item(item) for item in list(admission.get('qualified_items') or [])[:20] if isinstance(item,dict)]
   row['historical_audit']['deferred_items']=[_compact_trace_item(item) for item in list(admission.get('deferred_items') or [])[:20] if isinstance(item,dict)]
  row['candidate_groups']=candidate_groups_for_prompt(row)
  return row
 return {'status':'not_found','prompt_id':prompt_id}

AUDIT_CACHE={};AUDIT_CACHE_LOCK=threading.Lock()
def _audit_qwen_summary(payload):
 """Use the configured Evolving Profile Coding Plan model for a read-only explanation.

 The model receives bounded receipt metadata, never writes memory, and its text is
 clearly labelled as an explanation layered over deterministic evidence.
 """
 env={}
 try:
  for line in (STATE_ROOT/'profiles/agentmemory.env').read_text(encoding='utf-8').splitlines():
   if '=' in line and not line.lstrip().startswith('#'):
    k,v=line.split('=',1);env[k.strip()]=v.strip().strip('"').strip("'")
 except OSError: pass
 base=str(env.get('EVOLVING_PROFILE_API_LLM_BASE_URL') or '').rstrip('/');key=str(env.get('EVOLVING_PROFILE_API_LLM_API_KEY') or '');model=str(env.get('EVOLVING_PROFILE_API_LLM_MODEL') or 'qwen3.7-plus')
 if not (base and key): return {'state':'unavailable','reason':'coding_plan_not_configured','model':model}
 instruction=('你是 Evolving Profile 的只读链路审计解释器。根据给定的确定性回执、Codex 回合证据和当前 Bank 核验，'
  '用简体中文输出严格 JSON：{"summary":"一句结论","steps":["最多6条"],"confirmed":["已证实"],"not_confirmed":["未证实"],"next_check":"下一步",'
  '"selected_items":[{"id":"Bank记忆ID或空","reason":"直接证据","confidence":"high|medium|low"}],"selection_state":"confirmed|not_observable|no_selection_evidence"}。'
  '必须把三件事分开：Hook 返回的多维度偏好、Codex 通过 MCP 获得的 Bank 返回、Codex 最终选用的 Bank 内容。'
  'Hook 的 injected_count=0 不能推导 Codex MCP 选择为0；当前 Bank 重新核验的结果也不能冒充历史回合选择。'
  'recall-trace-index 中的 controller/Hook result_count、candidate_count、admitted_count 只描述入口适配器的历史知识分支，不能写成“Codex MCP 返回0”或“模型没有使用”；除非 codex_turn_evidence_detail 明确列出同一回合的 mcp_tool_call 与对应 mcp_tool_result，否则 MCP 选择状态必须是 not_observable。'
  '只有在回合日志包含 MCP 工具返回、或同一回合的模型上下文/答案明确引用对应记忆时，才可把 selected_items 标为 confirmed；否则 selection_state 必须是 not_observable 或 no_selection_evidence，不能猜测。'
  '如果当前 Bank 核验有记录，必须明确列出记录数和代表性内容。不写入任何长期记忆。\n证据：'+json.dumps(payload,ensure_ascii=False))
 body={'model':model,'messages':[{'role':'user','content':instruction}],'temperature':0,'max_tokens':900,'response_format':{'type':'json_object'},'enable_thinking':False}
 try:
  req=urllib.request.Request(base+'/chat/completions',data=json.dumps(body,ensure_ascii=False).encode(),headers={'Content-Type':'application/json','Authorization':'Bearer '+key},method='POST')
  with urllib.request.urlopen(req,timeout=20) as resp: out=json.loads(resp.read().decode())
  content=str((((out.get('choices') or [{}])[0].get('message') or {}).get('content')) or '').strip()
  parsed=json.loads(re.sub(r'^```(?:json)?\s*|\s*```$','',content,flags=re.S))
  return {'state':'complete','model':model,'summary':str(parsed.get('summary') or ''),'steps':parsed.get('steps') or [],'confirmed':parsed.get('confirmed') or [],'not_confirmed':parsed.get('not_confirmed') or [],'next_check':str(parsed.get('next_check') or ''),'selected_items':parsed.get('selected_items') or [],'selection_state':str(parsed.get('selection_state') or 'not_observable')}
 except Exception as error:return {'state':'failed','model':model,'reason':type(error).__name__+':'+str(error)[:240]}

def _codex_turn_evidence(row, limit_files=24):
 """Bounded, read-only scan of Codex trajectory files for the exact turn.

 The Hook receipt cannot tell us which Bank records the host model selected. This
 scanner only reports directly observable MCP calls/returns in the host trajectory;
 absence is explicitly not_observable and never converted to zero.
 """
 turn=str(row.get('turn_id') or ''); session=str(row.get('session_id') or '')
 if not turn and not session:
  return {'state':'not_observable','reason':'missing_turn_or_session_id','files_scanned':0,'mcp_calls':[],'mcp_returns':[],'model_messages':[]}
 root=HOME/'.codex/sessions'; files=[]
 try:
  files=sorted(root.glob('**/*.jsonl'),key=lambda p:p.stat().st_mtime,reverse=True)[:limit_files]
 except OSError: files=[]
 calls=[]; returns=[]; messages=[]; scanned=0; matched_files=[]
 for path in files:
  try:
   # Large rollout files are streamed line by line and capped by matching records.
   found=False
   with path.open(encoding='utf-8',errors='ignore') as fh:
    for raw in fh:
     if (turn not in raw) and (session and session not in raw): continue
     found=True
     try: obj=json.loads(raw)
     except ValueError: continue
     payload=obj.get('payload') or {}; typ=payload.get('type')
     record_turn=payload.get('turn_id') or (payload.get('internal_chat_message_metadata_passthrough') or {}).get('turn_id')
     # A later audit command may mention the old Prompt while running in its own
     # turn. It is not evidence for the old turn and must be excluded.
     if turn and record_turn and str(record_turn)!=turn: continue
     if turn and not record_turn and typ in {'function_call','custom_tool_call','mcp_tool_call','function_call_output','custom_tool_call_output','mcp_tool_result','tool_result','message'}: continue
     # Only keep concise evidence; do not put whole trajectories into the audit response.
     if typ in {'function_call','custom_tool_call','mcp_tool_call'}:
      name=str(payload.get('name') or payload.get('tool') or '')
      blob=str(payload.get('arguments') or payload.get('input') or '')
      is_mcp_name=(name.startswith('mcp__') or name.startswith('hindsight_') or str(payload.get('server') or '').lower().find('hindsight')>=0)
      if is_mcp_name:
       calls.append({'name':name,'call_id':payload.get('call_id') or payload.get('id'),'input_preview':blob[:1600],'timestamp':obj.get('timestamp'),'file':str(path)})
     elif typ in {'function_call_output','custom_tool_call_output','mcp_tool_result','tool_result'}:
      blob=str(payload.get('output') or payload.get('result') or '')
      if payload.get('call_id') and any(payload.get('call_id')==c.get('call_id') for c in calls):
       returns.append({'call_id':payload.get('call_id') or payload.get('id'),'output_preview':blob[:2800],'timestamp':obj.get('timestamp'),'file':str(path)})
     elif typ=='message' and payload.get('role') in {'assistant','tool'}:
      text=' '.join(str(c.get('text') or '') for c in (payload.get('content') or []) if isinstance(c,dict))
      if text and ('老婆' in text or '优优' in text or 'recall' in text.lower() or 'research' in text.lower()):
       messages.append({'role':payload.get('role'),'text_preview':text[:1800],'timestamp':obj.get('timestamp'),'file':str(path)})
   if found: matched_files.append(str(path)); scanned+=1
  except OSError: continue
 if calls or returns or messages:
  state='observed'
 elif matched_files:
  state='not_observable'
 else:
  state='not_observable'
 return {'state':state,'reason':'direct host trajectory evidence only; absence is not evidence of no MCP use','files_scanned':scanned,'matched_files':matched_files[:8], 'mcp_calls':calls[-20:],'mcp_returns':returns[-20:],'model_messages':messages[-20:]}

def _live_bank_lookup(query):
 """Read-only current Bank verification for an on-demand audit."""
 url=API+'/v1/default/banks/'+urllib.parse.quote(BANK,safe='')+'/memories/recall'
 body={'query':str(query or ''),'max_tokens':6000,'budget':'high','types':['world','experience','observation']}
 try:
  req=urllib.request.Request(url,data=json.dumps(body,ensure_ascii=False).encode(),headers={'Content-Type':'application/json'},method='POST')
  with urllib.request.urlopen(req,timeout=45) as resp: payload=json.loads(resp.read().decode())
  results=payload.get('results') or []; compact=[]
  for item in results[:60]:
   compact.append({'id':item.get('id'),'text':str(item.get('text') or '')[:1200],'type':item.get('type'),'entities':item.get('entities') or [],'mentioned_at':item.get('mentioned_at'),'occurred_start':item.get('occurred_start'),'source':(item.get('metadata') or {}).get('source'),'document_id':item.get('document_id'),'context':item.get('context')})
  return {'state':'complete','query':query,'result_count':len(results),'results':compact,'entities':payload.get('entities') or []}
 except Exception as error:return {'state':'failed','query':query,'result_count':0,'results':[],'reason':type(error).__name__+':'+str(error)[:240]}

def guidance_prompt_audit(prompt_id):
 with AUDIT_CACHE_LOCK:
  if prompt_id in AUDIT_CACHE:return AUDIT_CACHE[prompt_id]
 row=guidance_prompt_detail(prompt_id)
 if row.get('status')=='not_found':return row
 entries=(load(STATE_ROOT/'control-plane/recall-trace-index.json',{}) or {}).get('entries') or {}
 trace=entries.get(str(row.get('hook_invocation_id') or ''))
 if trace and trace.get('reused_from_execution_id') and entries.get(str(trace.get('reused_from_execution_id'))):
  trace={**entries[str(trace.get('reused_from_execution_id'))],'reused_by_execution_id':trace.get('execution_id') or trace.get('query_id')}
 trace=trace or {}
 facts={'prompt':row.get('user_prompt'),'hook_invocation_id':row.get('hook_invocation_id'),'guidance':{'count':(row.get('guidance_receipt') or {}).get('guidance_count',0),'model_sections':(row.get('guidance_receipt') or {}).get('model_section_count',0),'state':row.get('routes',{}).get('multi_dimensional_preference')},'recall':{'executed':bool(trace),'execution_id':trace.get('execution_id') or trace.get('query_id'),'queries_requested':trace.get('queries_requested',0),'queries_completed':trace.get('queries_completed',0),'result_count':trace.get('result_count',0),'coverage_complete':bool(trace.get('coverage_complete')),'selected_count':len(trace.get('selected_results') or [])},'host_state':(row.get('guidance_receipt') or {}).get('host_state'),'model_attention':(row.get('guidance_receipt') or {}).get('model_attention')}
 if trace and int(facts['recall']['result_count'] or 0)==0 and int(facts['recall']['queries_completed'] or 0)>0:facts['conclusion_code']='executed_empty'
 elif int(facts['recall']['result_count'] or 0)>0:facts['conclusion_code']='returned_records'
 elif not trace:facts['conclusion_code']='no_trace'
 else:facts['conclusion_code']='incomplete_trace'
 admission=trace.get('relevance_admission') or {}
 pipeline=trace.get('pipeline_stages') or {}
 claim=trace.get('claim_receipt') or {}
 qualified=list(admission.get('qualified_items') or [])
 admitted_ids=list(claim.get('actual_hook_injected_claim_ids') or [])
 facts['validation_pipeline']={
  'candidate_count':admission.get('candidate_count',((pipeline.get('retrieval') or {}).get('fused_candidate_count') or 0)),
  'qualified_count':admission.get('qualified_count',len(qualified)),
  'admitted_count':admission.get('admitted_count',len(admitted_ids)),
  'rejected_count':admission.get('rejected_count',0),
  'deferred_count':admission.get('deferred_for_token_budget_count',0),
  'packet_injected_count':len(admitted_ids),
  'actual_injected_ids':admitted_ids[:100],
  'validated_items':qualified[:20],
  'policy':admission.get('policy') or (pipeline.get('controller_admission') or {}).get('policy') or 'unknown',
  'host_delivery':(row.get('guidance_receipt') or {}).get('host_state') or 'unknown',
  'note':'只有通过准入并出现在 Memory Packet/Hook 注入回执中的条目，才算实际注入上下文。当前 Bank 核验结果不能直接冒充本轮注入。'
 }
 bank_lookup=_live_bank_lookup(row.get('user_prompt') or '')
 facts['bank_lookup']={'state':bank_lookup.get('state'),'result_count':bank_lookup.get('result_count',0),'representative_records':bank_lookup.get('results',[])[:12]}
 turn_evidence=_codex_turn_evidence(row)
 facts['codex_turn_evidence']={k:v for k,v in turn_evidence.items() if k not in {'mcp_returns','mcp_calls','model_messages'}}
 facts['codex_turn_evidence']['mcp_call_count']=len(turn_evidence.get('mcp_calls') or [])
 facts['codex_turn_evidence']['mcp_return_count']=len(turn_evidence.get('mcp_returns') or [])
 audit_payload={**facts,'codex_turn_evidence_detail':turn_evidence}
 result={'schema':'evolving-profile.audit.v2','status':'complete','deterministic':facts,'bank_lookup':bank_lookup,'codex_turn_evidence':turn_evidence,'explanation':_audit_qwen_summary(audit_payload)}
 with AUDIT_CACHE_LOCK:AUDIT_CACHE[prompt_id]=result
 return result
def guidance_instruction_status():
 try:
  if str(GUIDANCE_V1_SRC) not in sys.path:sys.path.insert(0,str(GUIDANCE_V1_SRC))
  from memory_usage_instructions import VERSION,CORE_TEXT,LONG_TEXT,content_sha256,status_snapshot
  rows=[]
  for path in sorted((STATE_ROOT/'guidance-v1/instruction-receipts').glob('*.json'),key=lambda p:p.stat().st_mtime,reverse=True)[:100]:
   try:
    if path.stat().st_size<=128*1024:rows.append(json.loads(path.read_text()))
   except Exception:pass
  hosts={}
  for row in rows:
   host=row.get('host') or 'unknown'
   if host not in hosts:hosts[host]=row
  entry_root=STATE_ROOT/'audit/guidance-entry-receipts'
  entry_rows=[]
  for path in sorted(entry_root.glob('*.json'),key=lambda p:p.stat().st_mtime,reverse=True)[:100]:
   try:
    if path.stat().st_size<=256*1024: entry_rows.append(json.loads(path.read_text()))
   except Exception: pass
  latest_entry=entry_rows[0] if entry_rows else None
  return {'instruction_version':VERSION,'content_sha256':content_sha256(),'core':CORE_TEXT,'detail':LONG_TEXT,'host_latest':hosts,
   'entry_guidance':{'required':True,'tool_name':'get_preference','invocation_mode':'codex_UserPromptSubmit_entry_adapter',
                     'checks_observed':len(entry_rows),'latest':latest_entry,'model_context_visibility':'unknown',
                     'answer_use':'not_measured'},
   'model_context_visibility':'unknown','agent_followed_instruction':'not_measured','boundary':'服务器准备、入口适配层检查、宿主上下文可见、实际工具调用和资料使用分别记账。'}
 except Exception as error:return {'state':'unavailable','error':type(error).__name__+':'+str(error)[:300],'model_context_visibility':'unknown'}
AUDIT=STATE_ROOT/'operational-audit-status.json';EFFECTIVENESS_QUALITY=STATE_ROOT/'audit/memory-effectiveness-quality-latest.json';SEMANTIC=STATE_ROOT/'audit/semantic-audit-latest.json';LIFECYCLE=STATE_ROOT/'observation-lifecycle-status.json';MENTAL_REFRESH=STATE_ROOT/'mental-model-refresh-status.json';MENTAL_CACHE=STATE_ROOT/'control-plane/mental-model-cache.json';REFRESH_QUEUE=STATE_ROOT/'mental-model-refresh-queue.json';GUIDANCE_WORKER_STATE=STATE_ROOT/'guidance-v1/worker-state.json';RESTORE=STATE_ROOT/'shadow-restore-drill-report.json';BENCHMARK=STATE_ROOT/'audits/real-recall-benchmark-latest.json';BACKUP_DIR=STATE_ROOT/'backups/managed/daily';LEDGER=STATE_ROOT/'control-plane/config-ledger-status.json';ENTITY_STATUS=STATE_ROOT/'control-plane/entity-resolution-status.json';NATIVE_RECEIPTS=STATE_ROOT/'audit/untraced-hindsight-context.jsonl';ASSURANCE=STATE_ROOT/'audit/memory-assurance-latest.json'
HAM_PROJECTION=STATE_ROOT/'memory-os/status-projection.json';HAM_API=CONTROLLER+'/v2/memory-os';HAM_RELEASE_GATE=STATE_ROOT/'control-plane/ham-os-release-gate.json';PROMPT_INGRESS=STATE_ROOT/'audit/prompt-ingress.jsonl';CACHE_TTL=4.0;CACHE_LOCK=threading.Lock();CACHE={'at':0.0,'value':None,'building':False}
def load(path,default=None):
 try:return json.loads(Path(path).read_text(encoding='utf-8'))
 except (OSError,ValueError):return {} if default is None else default
def research_snapshot(root=STATE_ROOT/'memory-os/research',capture_path=STATE_ROOT/'memory-os/capture/capture.sqlite3',include_host_receipts=True):
 """Separate MCP evidence audit; never forge a Hook/user-turn injection receipt."""
 rows=[];errors=[];allowed={}
 paths=sorted(Path(root).glob('*.json'),key=lambda p:p.stat().st_mtime,reverse=True)[:30];source_rows=[]
 for path in paths:
  try:source_rows.append((path,json.loads(path.read_text())))
  except (OSError,ValueError,TypeError) as error:errors.append({'file':path.name,'error_type':type(error).__name__})
 capture_map=_research_capture_projection_map(capture_path,{row.get('research_id') for _,row in source_rows})
 for path,row in source_rows:
  try:
   events=[e for e in row.get('delivery_events',[]) if e.get('stage')=='stdout_write_completed']
   allowed[row.get('research_id')]=set(row.get('memory_ids',[]))
   ids=list(dict.fromkeys(mid for e in events for mid in e.get('record_ids',[])))
   capture=capture_map.get(str(row.get('research_id'))) or {}
   rows.append({'research_id':row.get('research_id'),'route':'research','query':row.get('query'),
    'prompt_origin':'agent_tool_query','status':row.get('status'),'at':row.get('created_at_iso'),
    'seconds':row.get('seconds'),'discovered_count':len(row['memory_ids']) if 'memory_ids' in row else None,
    'stdout_record_count':len(ids) if 'delivery_events' in row else None,'stdout_record_ids':ids,
    'binding':capture.get('binding'),'pages':capture.get('pages') or [],'items':capture.get('items') or [],
    'returned_to_host_count':capture.get('returned_to_host_count'),
    'facet_count':len(row.get('facet_receipts') or []),'facets':[{'query':str(f.get('query') or '')[:1000],'status':f.get('status'),'record_count':len(f.get('record_ids') or []),'seconds':f.get('seconds'),'budget':f.get('budget'),'max_tokens':f.get('max_tokens')} for f in (row.get('facet_receipts') or [])[:8]],
    'delivery_event_count':len(events),'host_visibility':'unknown','answer_use':'not_measured',
    'semantic_coverage':'not_independently_verified','error_type':row.get('error_type'),
    'source_state_semantics':'source validity is rechecked per page; historical output may since be withdrawn'})
  except (OSError,ValueError,TypeError,KeyError) as e:errors.append({'file':path.name,'error_type':type(e).__name__})
 if not include_host_receipts:return {'items':rows,'errors':errors,'host_receipt_scan':{'status':'not_requested'}}
 host={'research':{},'status':'unavailable'}
 try:
  import importlib.util
  spec=importlib.util.spec_from_file_location('hindsight_host_receipts',RUNTIME_ROOT/'host-adapter/host_receipt_projection.py')
  module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
  host=module.read_host_receipts(capture_path,allowed)
 except Exception as error:errors.append('host_receipt_projection:'+type(error).__name__)
 for row in rows:
  observed=host['research'].get(row['research_id'])
  row.update(host_tool_response_record_count=len(observed['observed_record_ids']) if observed else None,
   host_tool_response_record_ids=observed['observed_record_ids'] if observed else [],
   host_receipts=observed['receipts'] if observed else [],model_context_visibility='unknown')
 return {'items':rows,'errors':errors,'host_receipt_scan':{k:v for k,v in host.items() if k!='research'},
  'meaning':'MCP证据研究独立审计；Agent查询不是用户原始Prompt。stdout与宿主PostToolUse返回观测分开；均不证明模型最终上下文完整性或答案使用。'}
def reference_snapshot(research=None):
 import importlib.util
 spec=importlib.util.spec_from_file_location('hindsight_reference_audit',RUNTIME_ROOT/'host-adapter/reference_audit.py')
 module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
 if research is None:research=research_snapshot()
 result=module.snapshot(research_rows=research['items'])
 result['research_link_scan']=research.get('host_receipt_scan',{})
 result['errors'].extend(research.get('errors',[]))
 return result
def request_audit_snapshot():
 def build():
  research=research_snapshot()
  return {'research':research,'reference':reference_snapshot(research)}
 return READ_CACHE.get('audit',build,{
  'research':{'items':[],'errors':['状态快照正在采集；尚无可用结果，不表示零检索。'],'host_receipt_scan':{'status':'warming'}},
  'reference':{'profile_receipts':[],'source_receipts':[],'prompt_receipts':[],'errors':['快照正在采集'],
   'boundary':'尚无已完成审计快照；不是零记忆。'}})
def request_turn_audit():
 def build():
  import importlib.util
  spec=importlib.util.spec_from_file_location('hindsight_turn_check',RUNTIME_ROOT/'host-adapter/memory_turn_check.py')
  module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
  result=module.snapshot()
  gate_sessions=(load(STATE_ROOT/'codex.json').get('memoryCompletionGateSessions') or [])
  result['completion_gate_scope']={'session_ids':gate_sessions,'maximum_continuations_per_turn':1}
  result['enforcement']='session_opt_in' if gate_sessions else 'not_enabled'
  native=STATE_ROOT/'memory-os/native-reconciliation/latest.json'
  result['native_reconciliation']=None
  if native.is_file() and native.stat().st_size<=128*1024:
   result['native_reconciliation']=json.loads(native.read_text())
  return result
 return READ_CACHE.get('turn-audit',build,{'items':[],'mode':'warming','enforcement':'not_enabled','boundary':'正在读取审计快照，不表示零检索。'})
def get(base,path,timeout=3):
 with urllib.request.urlopen(base+path,timeout=timeout) as r:
  value=json.loads(r.read().decode() or '{}');return value if isinstance(value,dict) else {'items':value}
def launch(label):
 out=subprocess.getoutput(f'launchctl print gui/{os.getuid()}/{label} 2>/dev/null');pid=next((x.split('=',1)[1].strip() for x in out.splitlines() if x.strip().startswith('pid =')),None)
 return {'label':label,'running':'state = running' in out,'pid':pid,'loaded':bool(out)}
def backup():
 groups={}
 for path in BACKUP_DIR.glob('*'):
  if not path.is_file():continue
  match=re.search(r'(\d{8}-\d{6})',path.name)
  if match:groups.setdefault(match.group(1),[]).append(path)
 if not groups:return {'present':False,'path':str(BACKUP_DIR),'set_count':0,'file_count':0,'total_bytes':0}
 sets=[{'stamp':stamp,'paths':paths,'modified':max(p.stat().st_mtime for p in paths),'bytes':sum(p.stat().st_size for p in paths),'verified':any(p.name.startswith('SHA256SUMS-') for p in paths)} for stamp,paths in groups.items()]
 latest=max(sets,key=lambda row:row['modified'])
 return {'present':True,'path':str(BACKUP_DIR),'name':latest['stamp'],'latest_set':latest['stamp'],'age_seconds':round(time.time()-latest['modified']),'bytes':latest['bytes'],'total_bytes':sum(row['bytes'] for row in sets),'set_count':len(sets),'file_count':sum(len(row['paths']) for row in sets),'verified_complete':latest['verified'],'integrity_note':'当前集合存在 SHA-256 清单；完整恢复仍需独立恢复演练。'}
def _p95(values):
 values=sorted(float(v) for v in values if v is not None)
 if not values:return None
 return round(values[min(len(values)-1,max(0,int(len(values)*.95)-1))],2)

def recent_jsonl_file(path,limit=30):
 try:
  rows=[]
  for line in _tail_lines(path,max(limit*8,240)):
   try: rows.append(json.loads(line))
   except ValueError: pass
  return rows[-limit:]
 except OSError:return []

def actual_delivery_summary(row,diagnostic=False):
 """Project the strongest available delivery proof without inventing a zero."""
 row=dict(row or {});effect=dict(row.get('memory_effectiveness') or {})
 host=dict(row.get('host_context_receipt') or {})
 host_state=str(host.get('state') or '')
 host_count=host.get('visible_record_count')
 base={'at':row.get('at'),'execution_id':str(row.get('execution_id') or row.get('query_id') or ''),
       'session_id':row.get('session_id'),'turn_id':row.get('turn_id'),
       'prompt':str(row.get('raw_user_prompt') or row.get('query_preview') or '')[:220],
       'diagnostic':bool(diagnostic),'host_state':host_state or None,
       'truncated':host.get('truncated') if host else None}
 if host_state=='native_context_observed' and isinstance(host_count,int) and host_count>0:
  return {**base,'state':'host_verified_injected','count':host_count,'evidence':'native_host'}
 if host_state in {'native_context_observed','completed_without_new_memory_packet'} and host_count==0:
  return {**base,'state':'host_verified_empty','count':0,'evidence':'native_host'}
 hook_count=effect.get('actual_injected_count')
 if hook_count is None:hook_count=effect.get('injected_count')
 receipt_state=str(effect.get('injection_receipt_state') or '')
 if isinstance(hook_count,int) and hook_count>0:
  return {**base,'state':'hook_reported_injected','count':hook_count,'evidence':'hook_receipt'}
 if receipt_state=='verified_empty':
  return {**base,'state':'hook_verified_empty','count':0,'evidence':'hook_receipt'}
 return {**base,'state':'delivery_unknown','count':None,'evidence':'unavailable'}

def mental_refresh_attention(refresh):
 """A held model-review lifecycle is outstanding maintenance, never green."""
 return str((refresh or {}).get('status') or '') in {'quality_hold','cooling_down','failed','interrupted_timeout','deferred_observation_review_active'}
def consolidation_backlog_summary(stats):
 """Explain official consolidation bookkeeping separately from profile refresh.

 The upstream counter covers unconsolidated world/experience units. It is not
 the observation accumulator and does not mean that the same number of
 preferences or mental models are waiting to be generated.
 """
 stats=dict(stats or {})
 pending=max(0,int(stats.get('pending_consolidation') or 0))
 failed=max(0,int(stats.get('failed_consolidation') or 0))
 return {
  'pending_units':pending,
  'failed_units':failed,
  'source':'upstream_bank_stats',
  'meaning':'官方事实/经历 consolidation bookkeeping；不等于待生成多维度偏好或心智模型',
  'actionable':bool(pending or failed),
  'safe_for_foreground_recall':True,
 }
INTERNAL_SUPPORT_PREFIX='当前任务需要补齐相关历史事实、已做决策、实施进度、未完成项和回退条件。'
RETIRED_OPTIONAL_ADAPTERS=('openclaw',)
SESSION_SOURCE_CACHE={}
SESSION_METADATA_CACHE={}

def codex_session_metadata(session_id):
 """Read immutable source metadata from the Codex session header.

 ``codex exec`` and the visible desktop both arrive at the same Hook, so the
 Controller audit alone cannot distinguish them.  The session header is the
 authoritative, tiny receipt for that distinction and also carries the
 ``thread_source`` tag used by the background-real lane.  Keep this lookup
 read-only and cache only the header fields; never inspect transcript bodies
 while building the status page.
 """
 key=str(session_id or '')
 if not key:return {}
 if key in SESSION_METADATA_CACHE:return SESSION_METADATA_CACHE[key]
 metadata={}
 try:
  root=Path.home()/'.codex'/'sessions'
  for path in root.rglob('*'+key+'.jsonl'):
   with path.open(encoding='utf-8') as handle:
    first=json.loads(handle.readline() or '{}')
   payload=first.get('payload') or {}
   metadata={
    'source':str(payload.get('source') or '').strip().casefold(),
    'thread_source':str(payload.get('thread_source') or '').strip().casefold(),
    'transcript_path':str(path),
   }
   break
 except (OSError,ValueError,TypeError):
  metadata={}
 SESSION_METADATA_CACHE[key]=metadata
 return metadata

def codex_session_source(session_id):
 """Read only the small session header needed to label visible vs exec input.

 A Codex hook is also invoked by local ``codex exec``.  Both paths otherwise
 look like ``codex-hook`` in the Controller audit, so the status page must not
 call an exec-generated probe a visible user conversation.  This lookup never
 reads a transcript body and is cached for the status process lifetime.
 """
 key=str(session_id or '')
 if not key:return ''
 if key in SESSION_SOURCE_CACHE:return SESSION_SOURCE_CACHE[key]
 source=str(codex_session_metadata(key).get('source') or '')
 SESSION_SOURCE_CACHE[key]=source
 return source

def is_real_background_trace(row):
 """Return True only for a contract-approved real background Codex turn.

 A generic ``exec`` session is still a diagnostic probe.  The exception is a
 separate Codex session created with ``thread_source=hermes-background`` and
 carrying a genuine user-origin prompt plus Hook/Controller identity.  A real
 turn can be represented by a partial Controller row while its foreground
 Hook request is still completing; requiring ``execution_complete`` here used
 to hide exactly that row and made the later itemized Packet look like a
 misleading zero-injection retry.  This keeps synthetic probes hidden without
 dropping the real non-blocking validation lane from 9998.
 """
 row=dict(row or {})
 source=str(row.get('session_source') or '').strip().casefold()
 if not source:
  source=str(codex_session_metadata(row.get('session_id')).get('source') or '').strip().casefold()
 thread_source=str(row.get('thread_source') or '').strip().casefold()
 if not thread_source:
  thread_source=str(codex_session_metadata(row.get('session_id')).get('thread_source') or '').strip().casefold()
 explicit=str(row.get('prompt_origin') or '').strip().casefold()
 # The background harness gives each question a stable lane suffix
 # (e.g. ``hermes-background-q20-v2``) so retries and random backchecks can
 # be audited independently.  Treat that controlled family as real just like
 # the original unsuffixed lane; requiring the exact bare string caused the
 # status page to hide a completed Controller receipt and manufacture a
 # misleading "90 seconds without receipt" ingress failure.
 return bool(
  source=='exec'
  and (thread_source=='hermes-background' or thread_source.startswith('hermes-background-'))
  and explicit in {'user_direct',''}
  and row.get('session_id')
  and row.get('hook_invocation_id')
  and row.get('execution_id')
 )

def owner_visible_trace(row):
    """Do not present controller helper calls as separate owner conversations."""
    # A foreground completion is a Controller deep pass that finished before
    # Hook returned.  It is therefore part of the actual user-visible turn,
    # even though it carries the continuation marker used to prevent recursive
    # scheduling.  Treating it as a hidden helper made 9998 show the focused
    # packet (often 0/5 items) while the synchronous deep packet was the one
    # actually available to the Hook.
    if bool(row.get('foreground_continuation')):
        return True
    query=str(row.get('query_preview') or '').lstrip()
    # A background continuation reuses the user's wording, so checking only
    # the support-query prefix used to render it as a second, zero-injection
    # conversation.  It is an internal coverage helper, not another user
    # prompt; merge it into the owner receipt by prompt fingerprint instead.
    return not (
        query.startswith(INTERNAL_SUPPORT_PREFIX)
        or bool(row.get('continuation_mode'))
        or str(row.get('event') or '') == 'background_continuation'
    )

def diagnostic_trace(row):
    """Identify synthetic/diagnostic executions before owner-page grouping.

    The controller and the read-only regression tests share an append-only
    audit stream.  A test client can therefore have the same ``client`` and
    ``prompt_origin`` fields as a real Hook row while still using a synthetic
    session.  If it reaches ``latest_trace`` it masks the last visible user
    turn and makes the dashboard claim a false zero/missing receipt.  Keep
    those rows in the raw audit files, but never let them enter the owner
    visible projection.
    """
    row=dict(row or {})
    if str(row.get('execution_mode') or '').casefold() in {'replay','shadow_replay','cassette_replay','mixed_execution_origins'}:
        return True
    explicit=str(row.get('prompt_origin') or '').strip().casefold()
    source=str(row.get('session_source') or '').strip().casefold()
    session=str(row.get('session_id') or '').strip().casefold()
    # Explicit operator diagnostic registration also classifies older receipts.
    # This is a display projection; immutable ingress and failures stay intact.
    config=load(STATE_ROOT/'codex.json',{})
    if session and session in {str(s).casefold() for s in config.get('diagnosticSessionIds',[])}:
        return True
    full_source=str(row.get('full_prompt_source') or '').strip().casefold()
    client=str(row.get('client') or '').strip().casefold()
    query=str(row.get('query_preview') or '').lstrip()
    if client=='regression-gate':
        return True
    if explicit in {'test_probe','diagnostic','fixture','benchmark'}:
        return True
    if session.startswith('automated-five-question-') or session.startswith('self-contained-red-test'):
        return True
    # Memory consolidation and the local audit harness can deliberately write
    # ``user_direct``-looking rows into the same append-only stream.  They are
    # useful raw evidence, but they are not a user-visible Codex turn and must
    # never displace one in the owner dashboard.  Use stable lane markers,
    # never Prompt wording or a project-specific allow-list.
    if query.startswith(('## Memory Writing Agent', '<hindsight_checkpoint>')):
        return True
    if session.startswith('audit-session-') and client in {'codex-live-audit','hindsight-live-audit'}:
        return True
    if client in {'codex-live-audit','hindsight-live-audit'} and source != 'vscode':
        return True

    if not session and client in {'zero-diagnostic-probe', 'curl/8.7.1', 'agent-tool'}:
        return True
    # A contract-approved REAL_BACKGROUND_CODEX turn is deliberately visible in
    # its own lane.  Other exec rows (legacy probes, fixture calls and ad-hoc
    # CLI checks) remain diagnostic-only and are filtered below.
    if source=='exec' and is_real_background_trace(row):
        return False
    if source in {'exec','diagnostic','fixture','test','benchmark'}:
        return True
    if session == 'test-session' or session.startswith((
        'test-', 'fixture-', 'probe-', 'diagnostic-', 'benchmark-',
        'goldset-', 'holdout-',
    )):
        return True
    if full_source.startswith(('live_regression','test_','test-','fixture_','fixture-','diagnostic_','diagnostic-')):
        return True
    # A bare synthetic row has no immutable Hook identity.  Do not hide old
    # records solely for missing fields, because pre-governance real rows may
    # legitimately lack them; the explicit synthetic markers above are the
    # only safe exclusion boundary.
    return False

def bounded_diagnostic_rows(rows, limit=30, native_limit=8):
 """Keep proven native-context examples visible during replay bursts."""
 ordered=sorted((dict(row) for row in rows),key=_trace_time,reverse=True)
 native=[row for row in ordered if re.fullmatch(r'[0-9a-f-]{36}',str(row.get('session_id') or ''))]
 other=[row for row in ordered if row not in native]
 chosen=native[:max(0,min(native_limit,limit))]
 chosen.extend(other[:max(0,limit-len(chosen))])
 return sorted(chosen,key=_trace_time,reverse=True)

def _trace_time(row):
 try:return datetime.fromisoformat(str(row.get('at') or '').replace('Z','+00:00')).timestamp()
 except (TypeError,ValueError):return 0.0
def _effectiveness_score(row):
 effect=row.get('memory_effectiveness') or {}
 return (int(effect.get('injected_count') or 0),int(effect.get('retrieved_count') or 0),int(row.get('result_count') or 0))

def _clean_raw_prompt(value):
 """Remove only the Hook's internal support wrapper, never user wording."""
 raw=str(value or '').strip()
 # The contextual resolver may append a private search hint and excerpts
 # after the user's literal input.  Those excerpts are not authored by the
 # user and must never appear in the “原始 Prompt” box.
 raw=raw.split('\n\n[受控上下文检索提示]',1)[0].strip()
 if raw.startswith(INTERNAL_SUPPORT_PREFIX):
  marker='\n当前问题：'
  if marker in raw:
   return raw.split(marker,1)[1].strip()
 return raw

def _prompt_provenance(row):
 """State the strongest provenance available without guessing old records."""
 client=str(row.get('client') or '')
 if not owner_visible_trace(row):
  return {'kind':'controller_internal_helper','label':'Controller 内部辅助检索','detail':'不是用户或 Agent 新说的话；由 Controller 为同一用户回合补齐覆盖而生成。'}
 if is_real_background_trace(row):
  return {'kind':'background_real','label':'后台真实 Codex 输入','detail':'由独立 Codex 进程的真实 UserPromptSubmit 提交；不抢占桌面窗口，按 REAL_BACKGROUND_CODEX 单独验收。'}
 if str(row.get('session_source') or '').casefold()=='exec':
  return {'kind':'test_probe','label':'命令行测试输入','detail':'该 Hook 来自本机 codex exec，不是 Codex 窗口中的可见用户交互；可保留用于诊断，但不计入真实对话验收。'}
 source=str(row.get('full_prompt_source') or '').casefold()
 if source.startswith(('live_regression','test_','test-')):
  return {'kind':'test_probe','label':'受控测试输入','detail':'由回归脚本或诊断工具提交；不应当被解释为用户实际说的话。'}
 explicit=str(row.get('prompt_origin') or '').strip().casefold()
 if explicit=='test_probe':
  return {'kind':'test_probe','label':'受控测试输入','detail':'由回归脚本或诊断工具提交；不应当被解释为用户实际说的话。'}
 if explicit=='agent_generated':
  return {'kind':'agent_generated','label':'Agent 生成输入','detail':'调用方明确标记为 Agent 生成；不是用户直接输入。'}
 if explicit=='agent_tool_call':
  return {'kind':'agent_tool_call','label':'Agent 工具调用','detail':'Agent 在回答过程中按需调用只读 Evolving Profile（EP）MCP；这是工具查询，不是用户新说的话，也不计作 Hook 自动注入。'}
 if explicit=='user_direct':
  return {'kind':'user_direct','label':'用户原始输入','detail':'调用方明确记录为用户直接输入；Full Prompt 只是检索语义扩展。'}
 if client in ('codex-hook','xiaodai-codex-hook'):
  return {'kind':'user_direct','label':'用户原始输入','detail':'由该 Agent 的用户输入 Hook 捕获；Full Prompt 只是检索语义扩展。'}
 if client=='hermes-hindsight-full-prompt-adapter':
  if explicit in ('user_direct','agent_generated'):
   return {'kind':explicit,'label':'用户原始输入' if explicit=='user_direct' else 'Hermes 生成输入','detail':'由 Hermes Adapter 在本轮写入的来源字段确认。'}
  return {'kind':'adapter_origin_unknown','label':'Hermes Adapter 输入（历史来源未记录）','detail':'旧记录没有保存“用户输入/Agent 生成”来源，页面不猜测。'}
 return {'kind':'agent_or_adapter_unknown','label':'调用方输入（历史来源未记录）','detail':'该历史记录未保存输入作者字段，页面不把调用方误标为用户。'}

def latest_owner_trace(rows, bank_id=None):
 """Select the newest owner turn, not a later MCP/tool read.

 Agent MCP reads remain in the history list so their provenance is visible,
 but they are not a new user turn. A diagnostic oracle can also arrive after
 the real Hook with no session identity. Choosing the first timestamp would
 make the top card show that read (often as a misleading zero injection).
 """
 candidates=[row for row in (rows or []) if bank_id is None or row.get('bank_id')==bank_id]
 visible=[row for row in candidates if owner_visible_trace(row) and not diagnostic_trace(row)]
 if visible:
  candidates=visible
 for row in candidates:
  kind=str((_prompt_provenance(row) or {}).get('kind') or '')
  if kind not in {'agent_tool_call','controller_internal_helper','test_probe'}:
   return row
 return candidates[0] if candidates else None

def normalize_trace_for_status(value):
 """Make count semantics and provenance explicit in the read-only projection."""
 row=dict(value or {})
 effect=dict(row.get('memory_effectiveness') or {})
 packet_delivery=effect.get('packet_delivery') or {}
 # The current Hook receipt calls these IDs ``transport_record_ids`` rather
 # than ``injected_ids``.  They are the immutable record IDs that crossed the
 # additionalContext boundary, so they are the authoritative itemized actual
 # injection evidence.  The previous projection ignored this field and
 # consequently showed a positive injected count with zero visible IDs and a
 # misleading ``none`` receipt state.
 injected_ids=list(effect.get('injected_ids') or [])
 if not injected_ids and isinstance(packet_delivery,dict):
  transport_ids=packet_delivery.get('transport_record_ids')
  if isinstance(transport_ids,list) and transport_ids:
   injected_ids=list(transport_ids)
   effect['injected_ids']=list(transport_ids)
 if injected_ids and effect.get('actual_injected_count') is None:
  effect['actual_injected_count']=len(injected_ids)
 elif not injected_ids and effect.get('actual_injected_count') is None and isinstance(packet_delivery,dict) and packet_delivery:
  # An explicit empty Packet is still a verified Hook outcome, distinct from a
  # missing receipt.  Preserve zero as evidence rather than letting the UI
  # conflate it with an uninstrumented historical row.
  try:
   if int(effect.get('injected_count') or 0)==0:
    effect['actual_injected_count']=0
  except (TypeError,ValueError):
   pass
 reported=int(effect.get('actual_injected_count') or 0)
 injected=int(effect.get('injected_count') or 0)
 # Old Hermes wrote a verified count but no IDs. It is not a zero injection;
 # preserve the limitation visibly instead of inventing item-level evidence.
 if reported>0 and not injected_ids and injected==0:
  effect['injected_count']=reported
  effect['injection_receipt_state']='count_only_historical'
  effect['injection_receipt_note']='底层记录确认注入数量，但该历史版本未写逐条 ID；不能逐条核验。'
 elif injected_ids and injected==len(injected_ids):
  effect['injection_receipt_state']='verified_itemized'
 elif injected_ids:
  effect['injection_receipt_state']='count_id_mismatch'
  effect['injection_receipt_note']='注入总数与逐条 ID 数量不一致，需审计。'
 else:
  packet_state=str(packet_delivery.get('state') or '').casefold() if isinstance(packet_delivery,dict) else ''
  if packet_state in {'not_delivered_empty_packet','verified_empty','empty_packet','hook_context_prepared','hook_stdout_write_completed'} and packet_delivery.get('transport_record_ids')==[]:
   effect['injection_receipt_state']='verified_empty'
  else:
   effect.setdefault('injection_receipt_state','none')
 if effect.get('memory_action')=='recall_timeout' or effect.get('retrieval_failure') or str(effect.get('injection_receipt_state') or '').startswith('failed_'):
  effect['injection_receipt_state']='failed_timeout' if ('timeout' in str(effect.get('retrieval_failure') or '').lower() or effect.get('memory_action')=='recall_timeout' or effect.get('injection_receipt_state')=='failed_timeout') else 'failed_delivery'
  # The Hook recorded that no memory entered this turn. A later successful
  # Controller lookup cannot turn that failed delivery into a success.
  effect['injected_count']=0
  effect['actual_injected_count']=0
  row['outcome']='failed'
 elif not injected_ids and not reported and effect.get('injection_receipt_state') in {'none','unknown','not_observed','missing_controller_receipt','awaiting_controller_receipt'}:
  effect['injected_count']=None
  effect['actual_injected_count']=None
 # `retrieved_count` in legacy effectiveness is a bounded feedback sample,
 # not the cardinality of all controller candidates. Keep both units.  When
 # the v9 four-stage receipt exists, it is the canonical source of truth:
 # older Hook feedback occasionally wrote a compact candidate sample (for
 # example 12) even though Controller actually examined 101 records.  Using
 # that stale sample here made the status page display impossible numbers such
 # as “候选 12 · 实际注入 16”, while the stage ledger correctly said 101 → 30 →
 # 16.  Prefer stage counts without changing old records that have no stages.
 hook_candidate_count=int((effect.get('claim_delivery') or {}).get('candidate_record_count') or 0)
 stage_source=row.get('pipeline_stages') or effect.get('pipeline_stages') or {}
 candidate_ledger=stage_source.get('candidate_ledger') if isinstance(stage_source,dict) and isinstance(stage_source.get('candidate_ledger'),dict) else {}
 retrieval_stage=stage_source.get('retrieval') if isinstance(stage_source,dict) and isinstance(stage_source.get('retrieval'),dict) else {}
 controller_stage=stage_source.get('controller_admission') if isinstance(stage_source,dict) and isinstance(stage_source.get('controller_admission'),dict) else {}
 hook_stage=stage_source.get('hook_postprocessing') if isinstance(stage_source,dict) and isinstance(stage_source.get('hook_postprocessing'),dict) else {}
 packet_stage=stage_source.get('packet_delivery') if isinstance(stage_source,dict) and isinstance(stage_source.get('packet_delivery'),dict) else {}
 def _first_int(*values):
  for candidate in values:
   if candidate is None or candidate=='':
    continue
   try:
    return int(candidate)
   except (TypeError,ValueError):
    continue
  return None
 stage_candidate=_first_int(controller_stage.get('input_count'),retrieval_stage.get('controller_input_count'),retrieval_stage.get('fused_candidate_count'))
 legacy_candidate=_first_int(effect.get('candidate_record_count'),hook_candidate_count,effect.get('candidate_count'),(row.get('claim_receipt') or {}).get('candidate_record_count'),row.get('result_count'))
 candidate_record_count=stage_candidate if stage_candidate is not None else (legacy_candidate if legacy_candidate is not None else 0)
 effect['candidate_record_count']=candidate_record_count
 # ``queries_requested`` is the Controller's planned base-lane count, while
 # ``queries_completed`` is the number of response objects after graph and
 # guidance sidecars are appended.  They therefore can legitimately be 3 and
 # 5, but that pair is not a valid user-facing "facets completed" fraction.
 # Derive an explicit facet receipt from the actual per-facet rows so the UI
 # never renders an impossible 5 / 3 progress value.
 facets=row.get('facets')
 if isinstance(facets,list):
  facet_requested_count=len(facets)
  facet_completed_count=sum(1 for facet in facets if isinstance(facet,dict) and str(facet.get('status') or '').casefold()=='completed')
  row['facet_queries_requested']=facet_requested_count
  row['facet_queries_completed']=facet_completed_count
  effect['facet_requested_count']=facet_requested_count
  effect['facet_completed_count']=facet_completed_count
 # Keep the Controller denominator and the complete Hook candidate ledger
 # separate.  A local governance/authority sidecar can be appended after the
 # Controller response; it is a real Hook candidate but not a missing or
 # duplicated Controller row.  Expose the boundary so the UI can explain it.
 controller_candidate_count=_first_int(candidate_ledger.get('controller_candidate_count'),stage_candidate,legacy_candidate)
 hook_candidate_total=_first_int(candidate_ledger.get('hook_candidate_count'),hook_candidate_count,effect.get('candidate_count'),legacy_candidate)
 hook_only_count=_first_int(candidate_ledger.get('hook_only_candidate_count'))
 if hook_only_count is None and controller_candidate_count is not None and hook_candidate_total is not None:
  hook_only_count=max(0,hook_candidate_total-controller_candidate_count)
 if controller_candidate_count is not None: effect['controller_candidate_count']=controller_candidate_count
 if hook_candidate_total is not None: effect['hook_candidate_count']=hook_candidate_total
 if hook_only_count is not None: effect['hook_only_candidate_count']=hook_only_count
 if candidate_ledger: effect['candidate_boundary_scope']='controller_admission_plus_hook_sidecars'
 # Keep the legacy field aligned for older UI extensions (v34/v36).  It is an
 # exact controller-candidate count whenever a stage receipt is present, not
 # the bounded feedback sample shown elsewhere on the page.
 if stage_source:
  effect['candidate_count']=candidate_record_count
  rejected=_first_int(controller_stage.get('rejected_count'),effect.get('rejected_before_injection_count'))
  deferred=_first_int(controller_stage.get('deferred_count'),effect.get('deferred_for_token_budget_count'))
  qualified=_first_int(controller_stage.get('qualified_count'),controller_stage.get('eligible_after_budget_count'))
  if rejected is not None: effect['rejected_before_injection_count']=rejected
  if deferred is not None: effect['deferred_for_token_budget_count']=deferred
  if qualified is not None: effect['qualified_count']=qualified
  for name,stage in (('controller',controller_stage),('hook',hook_stage),('packet',packet_stage)):
   if stage:
    for field in ('input_count','qualified_count','rejected_count','deferred_count','admitted_count','delivered_count','not_delivered_count'):
     value_int=_first_int(stage.get(field))
     if value_int is not None: effect[f'{name}_{field}']=value_int
 effect['stage_accounting_source']='pipeline_stages' if stage_source else 'legacy_effectiveness'
 effect.setdefault('feedback_sample_count',int(effect.get('retrieved_count') or 0))
 row['memory_effectiveness']=effect
 row['raw_user_prompt']=_clean_raw_prompt(row.get('raw_user_prompt') or row.get('query_preview'))
 row['prompt_provenance']=_prompt_provenance(row)
 return row

def compact_trace_for_status(value,detail_limit=12):
 """Bound owner-page projection size without hiding actual deliveries.

 Controller/audit files retain the full immutable ledger.  The browser only
 renders a bounded sample of raw/rejected candidates, so carrying those same
 nested rows three times in every `/api/status` response made a 30-row page
 exceed 20 MB and intermittently time out during live writes.  Counts remain
 exact and every item that actually crossed the Packet boundary is preserved.
 """
 row=dict(value or {});effect=dict(row.get('memory_effectiveness') or {})
 stages=row.get('pipeline_stages') or effect.get('pipeline_stages') or {}
 compact_stages={}
 for name,raw_stage in dict(stages).items():
  if not isinstance(raw_stage,dict):compact_stages[name]=raw_stage;continue
  stage=dict(raw_stage)
  for field in ('candidate_items','sidecar_candidate_items','qualified_items','rejected_items','deferred_items','admitted_items','not_delivered_items'):
   if isinstance(stage.get(field),list):
    original=stage[field];stage[field]=original[:detail_limit]
    if len(original)>len(stage[field]):stage[field+'_projection_omitted_count']=len(original)-len(stage[field])
  # Actual delivery is the user's primary audit question: never sample it.
  compact_stages[name]=stage
 if compact_stages:row['pipeline_stages']=compact_stages
 relevance=dict(row.get('relevance_admission') or effect.get('relevance_admission') or {})
 for field in ('rejected_items','deferred_items'):
  if isinstance(relevance.get(field),list):
   original=relevance[field];relevance[field]=original[:detail_limit]
   if len(original)>len(relevance[field]):relevance[field+'_projection_omitted_count']=len(original)-len(relevance[field])
 if relevance:row['relevance_admission']=relevance
 items=list(effect.get('items') or []);kept=[];non_delivered=0
 for item in items:
  if bool(item.get('injected')):
   kept.append(item)
  elif non_delivered<detail_limit:
   kept.append(item);non_delivered+=1
 effect['items']=kept
 if len(items)>len(kept):effect['items_projection_omitted_count']=len(items)-len(kept)
 # These are exact duplicates of the compact top-level ledgers above.
 effect.pop('pipeline_stages',None);effect.pop('relevance_admission',None)
 row['memory_effectiveness']=effect
 return row


_TRACE_LIST_FIELDS=(
 'at','bank_id','client','role','shape','strategies','outcome','query_preview','raw_user_prompt',
 'input_query_tokens','queries_completed','queries_requested','facet_queries_completed','facet_queries_requested',
 'coverage_complete','coverage_dimensions','fallback_used','errors','facets','prompt_provenance','execution_id',
 'query_id','session_id','turn_id','session_source','thread_source','execution_mode','full_prompt_source',
 'continuation_mode','continuation_status','authority_only_override','event','result_count','memory_action','elapsed_ms',
)
_EFFECT_LIST_FIELDS=(
 'actual_injected_count','injected_count','injected_ids','injection_receipt_state','injection_receipt_note',
 'candidate_record_count','candidate_count','controller_candidate_count','hook_candidate_count','hook_only_candidate_count',
 'qualified_count','rejected_before_injection_count','deferred_for_token_budget_count','memory_action',
 'retrieval_failure','stage_accounting_source','candidate_boundary_scope','feedback_sample_count',
 'source_reads','answer_feedback_state','answer_feedback_submitted','packet_delivery',
)


def trace_list_projection(value):
 """Produce a small owner-list row; immutable raw traces stay in Controller audit."""
 row=dict(value or {})
 out={field:row[field] for field in _TRACE_LIST_FIELDS if field in row}
 effect=dict(row.get('memory_effectiveness') or {})
 compact_effect={field:effect[field] for field in _EFFECT_LIST_FIELDS if field in effect}
 if isinstance(compact_effect.get('injected_ids'),list):
  compact_effect['injected_ids']=compact_effect['injected_ids'][:100]
 if isinstance(compact_effect.get('source_reads'),list):
  compact_effect['source_reads']=compact_effect['source_reads'][:20]
 if isinstance(compact_effect.get('packet_delivery'),dict):
  delivery=compact_effect['packet_delivery']
  compact_effect['packet_delivery']={key:delivery.get(key) for key in ('state','transport_record_ids','delivered_count','not_delivered_count') if key in delivery}
 out['memory_effectiveness']=compact_effect
 admission=dict(row.get('relevance_admission') or {})
 admission_counts={key:admission[key] for key in ('candidate_count','fused_candidate_count','qualified_count','rejected_count','deferred_for_token_budget_count') if key in admission}
 if admission_counts:out['relevance_admission']=admission_counts
 for key in ('mcp_audit','answer_memory_evidence','turn_receipt_projection'):
  value=row.get(key)
  if isinstance(value,dict):
   out[key]={field:value.get(field) for field in ('status','state','record_count','record_ids','successful_calls','failed_calls','content_status','boundary','identity_state','reason_codes') if field in value}
 return out


def effectiveness_list_projection(value):
 """Keep aggregate status in the polling response; itemized receipts stay at Controller."""
 value=dict(value or {})
 return {key:value.get(key) for key in ('schema','event','count','limit','aggregate','aggregate_scope','identity_semantics','semantics','diagnostic_trace_count') if key in value} | {
  'itemized_receipts':'available_from_controller_audit_on_demand'
 }

def hydrate_trace_effectiveness(rows, effectiveness_payload):
 """Join all feedback stages before projection; never fabricate missing IDs.

 A single execution normally writes at least two append-only events: an
 ``injection`` event containing the exact Packet/transport receipt and a later
 ``answer`` event containing answer-observation feedback.  The old projection
 used ``receipts[key] = receipt`` and therefore let the later answer event
 replace the injection event.  That made the status page show ``actual=0`` or
 ``injection_receipt_state=none`` even though the Hook had delivered a real
 Packet.  Aggregate by execution and copy only non-null fields; injection
 fields are never erased by a later answer-only event.
 """
 receipts={}
 by_hook={}
 for receipt in (effectiveness_payload or {}).get('items',[]):
  hook_key=(str(receipt.get('session_id') or ''),str(receipt.get('hook_invocation_id') or ''))
  if all(hook_key) and receipt.get('stage')=='injection':
   by_hook.setdefault(hook_key,[]).append(receipt)
  key=str(receipt.get('execution_id') or receipt.get('query_id') or '')
  if not key: continue
  target=receipts.setdefault(key,{})
  for field,value in dict(receipt).items():
   if value is None:
    continue
   # An empty answer event must not erase a non-empty injection receipt.  An
   # explicit empty list/count from the injection stage remains meaningful and
   # is retained when no earlier value exists.
   if field in {'injected_ids','actual_injected_record_ids','packet_delivery','claim_delivery','pipeline_stages','candidate_items','injected_items','rejected_items','deferred_items'}:
    if value or field not in target:
     target[field]=value
   elif field in {'injected_count','actual_injected_count','candidate_count','rejected_count','deferred_count','injection_receipt_state'}:
    if field not in target or target.get(field) in (None,'',0):
     target[field]=value
   else:
    # Keep the first non-empty pipeline value and allow later answer events to
    # contribute their answer_observation/feedback fields.
    if field not in target or target.get(field) in (None,'',[],{}):
     target[field]=value
 hydrated=[]
 for value in rows:
  row=dict(value)
  key=str(row.get('execution_id') or row.get('query_id') or '')
  receipt=receipts.get(key)
  hook_id=(str(row.get('session_id') or ''),str(row.get('hook_invocation_id') or ''))
  if all(hook_id) and by_hook.get(hook_id):
   merged=dict(receipt or {})
   for event in by_hook[hook_id]:
    for field,value in event.items():
     if field in {'at','event','execution_id','query_id','stage'} or value is None: continue
     if field in {'injected_ids','actual_injected_record_ids','packet_delivery','claim_delivery','pipeline_stages','candidate_items','injected_items','rejected_items','deferred_items'}:
      if value or field not in merged: merged[field]=value
     elif field in {'injected_count','actual_injected_count','candidate_count','rejected_count','deferred_count','injection_receipt_state'}:
      if field not in merged or merged.get(field) in (None,'',0): merged[field]=value
     elif field not in merged or merged.get(field) in (None,'',[],{}): merged[field]=value
   merged['receipt_execution_id']=str(by_hook[hook_id][-1].get('execution_id') or by_hook[hook_id][-1].get('query_id') or '')
   # The injection stage owns delivery, including an explicit zero/failure.
   # Never use a larger later Controller count as a substitute.
   authoritative=max(by_hook[hook_id],key=lambda event:str(event.get('at') or ''))
   for field in ('injected_count','actual_injected_count','injected_ids','packet_delivery','claim_delivery','injection_receipt_state','memory_action','retrieval_failure','outcome','memory_needs','guidance_receipt','guidance_sidecar'):
    if field in authoritative:merged[field]=authoritative[field]
   receipt=merged
  if receipt:
   effect=dict(row.get('memory_effectiveness') or {})
   # Copy the merged receipt rather than a short allow-list.  In particular,
   # ``injected_count``, ``packet_delivery`` and ``pipeline_stages`` come from
   # the injection event while ``answer_observation``/``likely_used_ids`` come
   # from the later answer event.  Transport metadata is kept out because it
   # is already represented by the controller row identity.
   for field,value in receipt.items():
    if field in {'at','event','execution_id','query_id','stage'} or value is None:
     continue
    effect[field]=value
   row['memory_effectiveness']=effect
  hydrated.append(normalize_trace_for_status(row))
 return hydrated

def restore_hook_receipt_rows(rows, ingress_rows, receipts):
 """Restore visible turns from exact native occurrence IDs and Hook receipts.

 No wording-only joining and no inferred injected IDs. The immutable receipt
 is the source, while the Controller trace is optional retrieval evidence.
 """
 result=[dict(row) for row in rows]
 for row in result:bind_ingress_identity(row,ingress_rows)
 for ingress in ingress_rows:
  session=ingress.get('session_id');hook=ingress.get('hook_invocation_id')
  if not session or not hook:continue
  matching=[r for r in receipts if r.get('stage')=='injection' and r.get('session_id')==session and r.get('hook_invocation_id')==hook
            and r.get('execution_mode') not in {'replay','shadow_replay','cassette_replay'}
            and (not r.get('prompt_origin') or not ingress.get('prompt_origin') or r.get('prompt_origin')==ingress.get('prompt_origin'))]
  if not matching:continue
  if any(r.get('session_id')==session and r.get('hook_invocation_id')==hook for r in result):continue
  receipt=max(matching,key=lambda r:str(r.get('at') or ''))
  execution=receipt.get('execution_id') or receipt.get('query_id')
  effect=dict(receipt)
  if not effect.get('items') and effect.get('injected_items'):
   effect['items']=[dict(item,injected=True) for item in effect['injected_items']]
  result.append({'at':ingress.get('at'),'event':'hook_receipt_owner','client':'codex-hook','role':'codex',
   'bank_id':receipt.get('bank_id') or BANK,'session_id':session,'turn_id':ingress.get('turn_id'),
   'hook_invocation_id':hook,'execution_id':execution,'query_id':execution,
   'raw_user_prompt':ingress.get('prompt_preview') or receipt.get('raw_user_prompt') or '',
   'query_preview':ingress.get('prompt_preview') or '', 'prompt_origin':ingress.get('prompt_origin') or 'user_direct',
   'user_prompt_fingerprint':ingress.get('prompt_fingerprint'),
   'outcome':receipt.get('outcome') or 'completed','execution_complete':True,
   'memory_action':receipt.get('memory_action'),'memory_effectiveness':effect,'source_guard':receipt.get('source_guard'),
   'retrieval_trace_available':False})
 return hydrate_trace_effectiveness(result,{'items':receipts})

def source_driven_ingress_trace(ingress,receipts):
 """Recognize an explicit thin-Hook output, never infer it from cwd alone."""
 matches=[]
 for r in receipts:
  if r.get('execution_mode') in {'replay','shadow_replay','cassette_replay'}:continue
  if r.get('kind')!='source_driven_prompt' or r.get('delivery_stage')!='hook_stdout_write_completed':continue
  if not ingress.get('session_id') or r.get('session_id')!=ingress['session_id']:continue
  if ingress.get('turn_id') and ingress['turn_id']!=r.get('turn_id'):continue
  normalized=' '.join(str(r.get('raw_prompt') or '').split())
  if hashlib.sha256(normalized.encode()).hexdigest()[:16]!=ingress.get('prompt_fingerprint'):continue
  if abs(_trace_time(r)-_trace_time(ingress))>5:continue
  matches.append(r)
 if len(matches)!=1:return None
 r=matches[0]
 return {'at':ingress.get('at'),'event':'prompt_ingress_owner','client':'codex-hook','role':'codex','bank_id':BANK,
  'query_id':'source-driven:'+str(r.get('invocation_id') or r.get('turn_id')),
  'raw_user_prompt':r.get('raw_prompt'),'query_preview':r.get('raw_prompt'),
  'user_prompt_fingerprint':ingress.get('prompt_fingerprint'),'session_id':r.get('session_id'),'turn_id':r.get('turn_id'),
  'hook_invocation_id':ingress.get('hook_invocation_id') or r.get('invocation_id'),
  'memory_action':'source_driven','outcome':'completed','execution_complete':True,'errors':[],
  'source_driven_receipt':r,'result_count':None,
  'memory_effectiveness':{'injected_count':None,'injection_receipt_state':'source_driven_separate_receipts',
   'note':'Hook已输出默认参考及MCP引导；历史检索/接收另核，不要求旧Controller回执。'}}

def merge_owner_turns(rows):
 """Present one visible user turn, not one row per Hook-internal lookup.

 The Hook may issue a focused follow-up query to fill the same visible prompt.
 Its execution id is deliberately distinct, so controller feedback cannot be
 attached to the raw query id.  The owner page must join them by the immutable
 original-prompt fingerprint and a short time window, while retaining every
 execution id for audit.  A missing receipt from one duplicate must never
 overwrite the other execution's proven injection receipt.
 """
 groups=[]
 for row in sorted((normalize_trace_for_status(x) for x in rows),key=_trace_time):
  # An explicit occurrence identity outranks wording/time heuristics. A single
  # Hook can have multiple visible Controller executions; a new human retry
  # has a different Hook ID even if the prompt text is identical.
  identity=(row.get('session_id'),row.get('hook_invocation_id'),row.get('role'))
  if identity[0] and identity[1]:
   for group in groups:
    if any((m.get('session_id'),m.get('hook_invocation_id'),m.get('role'))==identity for m in group):
     group.append(row);break
   else:groups.append([row])
   continue
  fingerprint=str(row.get('user_prompt_fingerprint') or row.get('query_fingerprint') or '')
  if not fingerprint:
   groups.append([row]);continue
  # Foreground completion is a second Controller execution of the same live
  # Hook turn, not a new user prompt.  Join it to the preceding owner before
  # the ordinary visible-owner branch can create a separate card.  A strict
  # scope/fingerprint/time match prevents two genuinely repeated user turns
  # from being collapsed merely because their wording is identical.
  if bool(row.get('foreground_continuation')):
   match=None
   for group in reversed(groups):
    first=group[0]
    first_fp=str(first.get('user_prompt_fingerprint') or first.get('query_fingerprint') or '')
    same_scope=(str(first.get('client') or '')==str(row.get('client') or '') and str(first.get('role') or '')==str(row.get('role') or '') and str(first.get('bank_id') or '')==str(row.get('bank_id') or ''))
    if first_fp==fingerprint and same_scope and any(not member.get('foreground_continuation') for member in group) and _trace_time(row)-_trace_time(group[-1])<=90:
     match=group;break
   if match is None:
    groups.append([row])
   else:
    match.append(row)
   continue
  # A Hook retry/reuse can publish a completed owner receipt whose
  # ``reused_from_execution_id`` points at the earlier execution that actually
  # built the Packet.  Treat that explicit immutable edge as the same user
  # turn.  Without this branch the status projection exposed two cards: the
  # itemized Packet (often marked partial) and a later reused row reporting
  # zero injection, so the UI appeared to contradict the real Hook receipt.
  reused_from=str(row.get('reused_from_execution_id') or '')
  if reused_from:
   match=None
   for group in reversed(groups):
    first=group[0]
    first_fp=str(first.get('user_prompt_fingerprint') or first.get('query_fingerprint') or '')
    same_scope=(str(first.get('client') or '')==str(row.get('client') or '') and str(first.get('role') or '')==str(row.get('role') or '') and str(first.get('bank_id') or '')==str(row.get('bank_id') or ''))
    if first_fp==fingerprint and same_scope and any(str(member.get('execution_id') or member.get('query_id') or '')==reused_from for member in group) and _trace_time(row)-_trace_time(group[-1])<=90:
     match=group;break
   if match is not None:
    match.append(row)
    continue
  # A user-visible trace is a new turn even when its wording/fingerprint is
  # identical to the immediately preceding turn. Only controller-generated
  # helper traces may join an existing visible owner.
  if owner_visible_trace(row):
   # A source-first owner may arrive after its internal helper. Keep that
   # helper attached only so the UI can explicitly disclose its separate
   # historical receipt. Ordinary visible retries start a fresh owner turn.
   if str(row.get('memory_action') or '')=='source_first':
    for group in reversed(groups):
     first=group[0]
     first_fp=str(first.get('user_prompt_fingerprint') or first.get('query_fingerprint') or '')
     same_scope=(str(first.get('client') or '')==str(row.get('client') or '') and str(first.get('role') or '')==str(row.get('role') or '') and str(first.get('bank_id') or '')==str(row.get('bank_id') or ''))
     if first_fp==fingerprint and same_scope and not any(owner_visible_trace(member) for member in group) and _trace_time(row)-_trace_time(group[-1])<=90:
      group.append(row);break
    else:
     groups.append([row])
   else:
    groups.append([row])
   continue
  match=None
  for group in reversed(groups):
   first=group[0]
   first_fp=str(first.get('user_prompt_fingerprint') or first.get('query_fingerprint') or '')
   same_scope=(str(first.get('client') or '')==str(row.get('client') or '') and str(first.get('role') or '')==str(row.get('role') or '') and str(first.get('bank_id') or '')==str(row.get('bank_id') or ''))
   if first_fp==fingerprint and same_scope and _trace_time(row)-_trace_time(group[-1])<=90:
    match=group;break
  if match is None:
   groups.append([row])
  else:
   match.append(row)
 merged=[]
 for group in groups:
  visible=[row for row in group if owner_visible_trace(row)]
  if not visible:
   continue
  # Preserve the actual user wording and the actual owner execution.  An
  # internal breadth/continuation lookup may finish later with more results,
  # but those results did not cross the Hook boundary for this user turn and
  # therefore cannot become the representative retrieval or delivery chain.
  base=min(visible,key=_trace_time)
  # An internal helper or an earlier coalesced attempt may be marked failed
  # after the same user turn has already completed through another execution.
  # The owner view must report the final user-turn outcome, not paint the whole
  # turn red just because a recoverable duplicate probe failed.
  completed=[row for row in visible if bool(row.get('execution_complete'))]
  strongest=max(completed or visible,key=_effectiveness_score)
  # A Hook delivery receipt is stronger evidence than a helper's larger
  # retrieval count.  In particular, a zero-injection Hook receipt can still
  # prove that the current task context was reactivated and that the Packet
  # was empty.  Do not let an internal continuation with a few controller
  # candidates overwrite that truth merely because it has result_count > 0.
  delivery_rows=[
   row for row in visible
   if isinstance((row.get('memory_effectiveness') or {}).get('packet_delivery'),dict)
   and bool((row.get('memory_effectiveness') or {}).get('packet_delivery'))
  ]
  delivery_representative=max(delivery_rows,key=_effectiveness_score) if delivery_rows else None
  # A source-first owner is a stricter boundary than an internal breadth helper.
  # The helper may have a real historical-memory receipt, but it must never be
  # presented as if it belonged to the attachment/current-source execution.
  # Otherwise the page simultaneously says "old memory paused" and "3 actually
  # injected", which is both visually confusing and factually wrong.
  source_first_rows=[row for row in visible if str(row.get('memory_action') or '')=='source_first' and bool((row.get('source_guard') or {}).get('active'))]
  source_owner=max(source_first_rows,key=_trace_time) if source_first_rows else None
  representative=source_owner or delivery_representative or strongest
  out=dict(representative)
  owner_wording=source_owner or base
  out.update({key:owner_wording.get(key) for key in ('at','query_preview','raw_user_prompt','prompt_provenance','input_query_tokens','client','role','bank_id','query_id','execution_id','query_fingerprint','user_prompt_fingerprint','session_id','session_source','hook_invocation_id','prompt_origin','full_prompt','full_prompt_source') if owner_wording.get(key) is not None})
  out['memory_effectiveness']=dict((representative.get('memory_effectiveness') or {}))
  # Legacy owner rows without any Packet receipt can be paired with a helper
  # that carries the only historical injection count. If the owner has an
  # explicit empty Packet receipt, preserve that verified zero.
  owner_effect = dict(out['memory_effectiveness'])
  if source_owner is None and not owner_effect.get('packet_delivery') and not int(owner_effect.get('injected_count') or 0):
   helper_delivery=max((row for row in group if int(((row.get('memory_effectiveness') or {}).get('injected_count') or 0)) > 0), key=_effectiveness_score, default=None)
   if helper_delivery is not None:
    helper_effect=dict(helper_delivery.get('memory_effectiveness') or {})
    for field in ('injected_count','actual_injected_count','injected_ids','actual_hook_injected_record_ids','packet_delivery','claim_delivery','injection_receipt_state'):
     if helper_effect.get(field) not in (None,'',[],{}): out['memory_effectiveness'][field]=helper_effect[field]
  if len({r.get('bank_id') for r in group})>1 and representative.get('hook_invocation_id'):
   # Human wording comes from the occurrence; transport identity must remain
   # attached to the execution whose delivery receipt is actually displayed.
   for field in ('bank_id','execution_id','query_id'):
    out[field]=representative.get(field)
  # Reuse receipts are the completion edge of the same turn even when the
  # itemized Packet row itself was persisted as ``partial``.  Carry completion
  # from any linked visible row while keeping the delivery representative's
  # exact packet counts and IDs.
  if bool(representative.get('execution_complete')) or any(bool(row.get('execution_complete')) for row in visible):
   out['execution_complete']=True
   out['outcome']='failed' if str((representative.get('memory_effectiveness') or {}).get('injection_receipt_state') or '').startswith('failed_') else 'completed'
   # Keep only diagnostics from the successful representative.  Failed helper
   # details remain in the merged execution list, but do not masquerade as a
   # failed visible conversation.
   out['errors']=list(representative.get('errors') or [])
  separate_helper_receipts=[]
  if source_owner is not None:
   for row in group:
    if row is source_owner:
     continue
    effect=row.get('memory_effectiveness') or {}
    count=int(effect.get('injected_count') or 0)
    if count:
     separate_helper_receipts.append({'execution_id':str(row.get('execution_id') or row.get('query_id') or ''),'injected_count':count,'retrieved_count':int(effect.get('retrieved_count') or 0),'kind':'internal_helper_or_prior_execution'})
  helper_executions=[]
  foreground_rows=[]
  for row in group:
   if bool(row.get('foreground_continuation')):
    effect=row.get('memory_effectiveness') or {}
    foreground_rows.append({
     'execution_id':str(row.get('execution_id') or row.get('query_id') or ''),
     'at':row.get('at'),
     'parent_execution_id':str(row.get('foreground_parent_execution_id') or ''),
     'result_count':int(effect.get('candidate_record_count') or row.get('result_count') or 0),
     'injected_count':int(effect.get('injected_count') or 0),
     'coverage_complete':bool(row.get('coverage_complete')),
     'outcome':'completed' if bool(row.get('execution_complete')) else str(row.get('outcome') or 'unknown'),
    })
    continue
   if owner_visible_trace(row):
    continue
   effect=row.get('memory_effectiveness') or {}
   helper_executions.append({
    'execution_id':str(row.get('execution_id') or row.get('query_id') or ''),
    'at':row.get('at'),'kind':'controller_internal_helper',
    'result_count':int(effect.get('candidate_record_count') or row.get('result_count') or 0),
    'feedback_sample_count':int(effect.get('feedback_sample_count') or effect.get('retrieved_count') or 0),
    'injected_count':int(effect.get('injected_count') or 0),
    'outcome':'completed' if bool(row.get('execution_complete')) else str(row.get('outcome') or 'unknown'),
   })
  out['owner_turn_receipt']={
   'bank_ids':list(dict.fromkeys(r.get('bank_id') for r in group if r.get('bank_id'))),
   'execution_count':len(group),
   'helper_execution_count':sum(not owner_visible_trace(row) for row in group),
   'foreground_continuation_count':len(foreground_rows),
   'foreground_continuations':foreground_rows,
   'execution_ids':[str(row.get('execution_id') or row.get('query_id') or '') for row in group],
   'helper_executions':helper_executions,
   'actual_injected_count':int((out['memory_effectiveness']).get('injected_count') or 0),
   'historical_injection_is_separate':bool(separate_helper_receipts),
   'separate_helper_receipts':separate_helper_receipts,
   'reason':('当前来源优先执行单独展示；任何内部历史检索回执均保留为独立审计，不计入本次当前来源链路。' if source_owner is not None else ('同一用户回合的前台深度补齐已在 Hook 返回前合并；实际 Packet 以覆盖完整的最终执行为准。' if foreground_rows else '同一用户问题的内部补充检索已合并；优先显示已完成且有 Hook 注入回执的执行。辅助检索未产生单独注入回执时，不影响已确认的最终投递结果。')),
  }
  merged.append(out)
 return merged
def active_audit_failures(rows):
 """Retired optional adapters remain observable but cannot fail core health."""
 return [row for row in rows if not any(adapter in str(row).casefold() for adapter in RETIRED_OPTIONAL_ADAPTERS)]
def recall_analytics(traces):
 groups={}
 for row in traces:
  profile=str(row.get('recall_profile') or 'controller')
  bucket=groups.setdefault(profile,{'profile':profile,'count':0,'elapsed_ms':[],'execution_complete':0,'coverage_complete':0,'continuation_ready':0,'continuation_running':0,'coalesced':0})
  bucket['count']+=1;bucket['elapsed_ms'].append(row.get('elapsed_ms') or 0)
  bucket['execution_complete']+=int(bool(row.get('execution_complete')))
  bucket['coverage_complete']+=int(bool(row.get('coverage_complete')))
  bucket['continuation_ready']+=int(row.get('continuation_status')=='ready')
  bucket['continuation_running']+=int(row.get('continuation_status')=='background_running')
  bucket['coalesced']+=int(bool(row.get('coalesced')))
 for bucket in groups.values():
  bucket['p95_ms']=_p95(bucket.pop('elapsed_ms'))
 return {'profiles':sorted(groups.values(),key=lambda x:x['profile']),'sample_count':len(traces),'meaning':'执行完成与覆盖完整分开统计；后台续查和共享请求均不表示失败。'}

def retrieval_quality_analytics(traces):
 """Aggregate bounded evidence-quality receipts for the owner dashboard.

 This is a projection only: it reads the Controller's per-execution receipt,
 never re-runs recall, reads source text, or infers model attention.
 """
 rows=[]
 for trace in traces or []:
  controller=trace.get('query_controller') or trace
  quality=controller.get('evidence_quality') or {}
  if not isinstance(quality,dict): continue
  rows.append((quality, controller))
 latencies=sorted(float(c.get('elapsed_ms')) for _,c in rows if isinstance(c.get('elapsed_ms'),(int,float)) and float(c.get('elapsed_ms'))>=0)
 reason_counts={}
 for quality,_ in rows:
  for reason in quality.get('reasons') or []:
   key=str(reason)[:80];reason_counts[key]=reason_counts.get(key,0)+1
 import math
 p95=latencies[min(len(latencies)-1,max(0,math.ceil(len(latencies)*0.95)-1))] if latencies else None
 return {
  'schema':'evolving-profile.retrieval-quality-analytics.v1',
  'sample_count':len(rows),
  'escalation_count':sum(bool(q.get('needs_escalation')) for q,_ in rows),
  'research_recommendation_count':sum(q.get('recommended_route')=='research' for q,_ in rows),
  'recall_expand_recommendation_count':sum(q.get('recommended_route')=='recall_expand' for q,_ in rows),
  'p95_latency_ms':int(round(p95)) if p95 is not None else None,
  'reason_counts':reason_counts,
  'meaning':'基于同回合 Controller 回执统计证据质量与升级建议；不等同于实际注入或模型采用。',
 }
def unmatched_ingress_trace(row, *, collection_available, now=None):
 age=(time.time() if now is None else now)-_trace_time(row)
 if not collection_available:
  outcome,state,note='unknown','collection_unavailable','状态采集不可用；不能据此断言召回失败或注入为零。'
 elif age<210:
  outcome,state,note='pending','awaiting_controller_receipt','等待本次 Hook/Controller 回执；尚未超过前台执行期限。'
 else:
  outcome,state,note='unknown','missing_controller_receipt','当前审计窗口未找到本回合回执；需要核对原始日志，不能据此断言实际注入为零。'
 fp=row.get('prompt_fingerprint')
 return {'at':row.get('at'),'event':'prompt_ingress_owner','query_id':'ingress:'+str(row.get('hook_invocation_id') or fp),
  'query_preview':row.get('prompt_preview'),'raw_user_prompt':row.get('prompt_preview'),
  'prompt_origin':row.get('prompt_origin') or 'unknown','user_prompt_fingerprint':fp,'query_fingerprint':fp,
  'session_id':row.get('session_id'),'turn_id':row.get('turn_id'),'hook_invocation_id':row.get('hook_invocation_id'),
  'client':'codex-hook','role':'codex','bank_id':BANK,'input_query_tokens':None,'result_count':None,
  'outcome':outcome,'execution_complete':False,'errors':[],
  'memory_effectiveness':{'injected_count':None,'actual_injected_count':None,
   'injection_receipt_state':state,'injection_receipt_note':note}}

def bind_ingress_identity(trace, ingress_rows):
 """Recover omitted projection identity only from a unique native occurrence."""
 fp=str(trace.get('user_prompt_fingerprint') or trace.get('query_fingerprint') or '')
 matches=[]
 for row in ingress_rows:
  if trace.get('session_id') and trace['session_id']!=row.get('session_id'):continue
  if trace.get('hook_invocation_id'):
   if trace['hook_invocation_id']!=row.get('hook_invocation_id'):continue
  elif not fp or fp!=str(row.get('prompt_fingerprint') or '') or abs(_trace_time(trace)-_trace_time(row))>90:continue
  if trace.get('turn_id') and trace['turn_id']!=row.get('turn_id'):continue
  matches.append(row)
 if len(matches)!=1:return
 for field in ('session_id','turn_id','hook_invocation_id','prompt_origin'):
  if not trace.get(field) and matches[0].get(field):trace[field]=matches[0][field]
 if not trace.get('raw_user_prompt'):trace['raw_user_prompt']=matches[0].get('prompt_preview')

def turn_projection_ingress(trace, ingress_rows):
 """Return one exact ingress record, never a wording-only dashboard join."""
 matches=[]
 for row in ingress_rows:
  if trace.get('session_id') and trace.get('session_id')!=row.get('session_id'):continue
  if trace.get('turn_id') and trace.get('turn_id')!=row.get('turn_id'):continue
  if trace.get('hook_invocation_id') and trace.get('hook_invocation_id')!=row.get('hook_invocation_id'):continue
  if not row.get('session_id') or not row.get('turn_id') or not row.get('hook_invocation_id'):continue
  matches.append(row)
 return matches[0] if len(matches)==1 else None

def attach_turn_receipt_projection(trace, ingress_rows):
 """Attach a versioned read-only ledger; unknown evidence stays unknown."""
 ingress=turn_projection_ingress(trace,ingress_rows)
 trace['turn_receipt_projection']=_turn_contract_module.project_trace_turn(
  trace,ingress,trace.get('host_context_receipt'))

def attach_native_receipts(rows, max_sessions=4, native_sessions=None):
 """Attach only same-session, same-turn native context observations."""
 sessions=native_sessions if native_sessions is not None else {}
 for trace in rows:
  session=trace.get('session_id');turn=trace.get('turn_id')
  if not session or not turn:continue
  if session not in sessions and len(sessions)<max_sessions:
   source=codex_session_metadata(session).get('transcript_path')
   sessions[session]=_receipts_module.read_native_context_receipts(source,session) if source else {}
  receipt=(sessions.get(session) or {}).get(turn)
  if receipt:trace['host_context_receipt']=receipt
 return sessions

def bounded_effectiveness_receipts(limit=480):
 """Read only the receipt fields needed by the status projection.

 The durable effectiveness index and Hook JSON files intentionally retain
 full candidates/source payloads for forensic audit. Loading those files with
 Python ``json.load`` on every status refresh duplicated tens of MB in the
 long-lived server. jq parses them in a short-lived process and emits a small
 execution-keyed projection; the raw files remain the audit authority.
 """
 fields=('{at,stage,event,execution_id,query_id,session_id,turn_id,hook_invocation_id,'
   'execution_mode,prompt_origin,outcome,memory_action,retrieval_failure,'
   'injected_count,actual_injected_count,injected_ids,actual_hook_injected_record_ids,'
   'injection_receipt_state,packet_delivery,claim_delivery,candidate_count,rejected_count,'
   'deferred_count,prompt_fingerprint}')
 result=[]
 try:
  index=STATE_ROOT/'control-plane/memory-effectiveness-receipts.json'
  if index.is_file():
   proc=subprocess.run(['jq','-c',f'[.entries[][] | {fields}]',str(index)],capture_output=True,text=True,timeout=8)
   if proc.returncode==0:
    value=json.loads(proc.stdout or '[]')
    if isinstance(value,list):result.extend(value[-limit:])
  paths=[]
  root=STATE_ROOT/'audit/hook-output-receipts'
  for lane in ('production','diagnostic'):
   paths.extend(sorted((root/lane).glob('*.json'),key=lambda p:p.stat().st_mtime,reverse=True)[:limit//2])
  if paths:
   proc=subprocess.run(['jq','-c','-s',f'[.[] | {fields}]',*(str(p) for p in paths)],capture_output=True,text=True,timeout=12)
   if proc.returncode==0:
    value=json.loads(proc.stdout or '[]')
    if isinstance(value,list):result.extend(value)
 except (OSError,ValueError,TypeError,subprocess.SubprocessError):
  return result[-limit:]
 # Same execution may be present in the index and its immutable file. Keep the
 # most recent row, never concatenate nested payloads.
 dedup={}
 for row in result:
  if not isinstance(row,dict):continue
  key=str(row.get('hook_invocation_id') or row.get('execution_id') or row.get('query_id') or '')
  if key:dedup[key]=row
 return list(dedup.values())[-limit:]

def snapshot(force=False):
 now=time.time()
 with CACHE_LOCK:
  if not force and CACHE['value'] is not None and now-CACHE['at']<CACHE_TTL:return CACHE['value']
 errors=[]
 try:health=get(API,'/health')
 except Exception as e:health={'status':'unavailable','error':repr(e)};errors.append(f'hindsight:{e}')
 try:stats=get(API,f'/v1/default/banks/{urllib.parse.quote(BANK,safe="")}/stats',5)
 except Exception as e:
  stats={'status':'slow_or_unavailable','error_type':type(e).__name__}
  # Stats is a secondary display metric. Preserve the last trace/health
  # projection and label this metric explicitly instead of treating it as a
  # recall or injection failure.
  errors.append(f'stats_noncritical:{type(e).__name__}')
 try:controller=get(CONTROLLER,'/v1/status')
 except Exception as e:controller={'status':'unavailable','functional_status':'degraded','error':repr(e)};errors.append(f'controller:{e}')
 trace_collection_available=True
 # The owner dashboard keeps only the most recent bounded visible turns; the
 # immutable Controller audit endpoint remains available for deep audit. A
 # 100-row projection can still carry several MB of nested trace metadata.
 try:traces=get(CONTROLLER,'/v1/traces?limit=10&projection=status').get('items',[])
 except Exception as e:
  trace_collection_available=False
  with CACHE_LOCK:traces=list((CACHE.get('value') or {}).get('traces') or [])
  traces=[dict(row,collection_stale=True) for row in traces]
  errors.append(f'traces:{e}')
 try:effectiveness=get(CONTROLLER,'/v1/effectiveness?limit=20')
 except Exception as e:effectiveness={'items':[],'aggregate':{},'status':'unavailable'};errors.append(f'effectiveness:{e}')
 traces=[compact_trace_for_status(row) for row in hydrate_trace_effectiveness(traces,effectiveness)]
 # Earlier Controller records predate propagation of the Hook invocation ID.
 # The append-only Hook ingress ledger already has that immutable identity, so
 # enrich only a unique, same-fingerprint, same-time record.  This is a
 # backward-compatible projection repair: it never invents an ID or joins two
 # visible turns solely because their wording happens to match.
 ingress_rows=recent_jsonl_file(PROMPT_INGRESS,120)
 # Read the bounded durable receipt index as well as the recent event tail.
 # It survives broad traces and diagnostic bursts without scanning the full
 # immutable ledger on every page refresh. The helper uses a short-lived jq
 # process and never loads the 55MB JSON index into this server.
 indexed_receipts=bounded_effectiveness_receipts(120)
 traces=restore_hook_receipt_rows(traces,ingress_rows,list(effectiveness.get('items') or [])+indexed_receipts)
 for trace in traces:
  bind_ingress_identity(trace,ingress_rows)
 for trace in traces:
  metadata=codex_session_metadata(trace.get('session_id'))
  if not trace.get('session_source'):
   trace['session_source']=metadata.get('source') or codex_session_source(trace.get('session_id'))
  if not trace.get('thread_source') and metadata.get('thread_source'):
   trace['thread_source']=metadata['thread_source']
 traces=[normalize_trace_for_status(trace) for trace in traces]
 # Regression probes are retained in raw controller audit logs but never shown
 # as real conversations in the owner-facing dashboard.  Native receipts are
 # merged only to make a proven context arrival visible; they never claim a
 # retrieval count or turn a missing Controller receipt into a failure.
 # Diagnostic/fixture rows stay available in the immutable audit stream, but
 # they must not become the owner-facing "latest" conversation.  This is
 # intentionally applied before ingress merging so a synthetic row cannot
 # shadow a real user receipt merely by having a newer timestamp.
 diagnostic_rows=bounded_diagnostic_rows((row for row in traces if diagnostic_trace(row)))
 traces=[row for row in traces if not diagnostic_trace(row)]
 # A cache/continuation may reuse the completed result without emitting a raw
 # owner trace; its helper trace still carries the immutable user-prompt
 # fingerprint and exact injection receipt.  Recreate a lightweight owner
 # shell from Hook ingress so the page displays the user's wording and joins
 # the real receipt, rather than silently dropping the entire chain.
 ingress=[]
 source_prompt_receipts=[]
 for path in sorted((STATE_ROOT/'memory-os/source-prompt-receipts').glob('*.json'),key=lambda p:p.stat().st_mtime,reverse=True)[:30]:
  try:
   if path.stat().st_size<=256*1024:source_prompt_receipts.append(json.loads(path.read_text()))
  except (OSError,ValueError):pass
 for row in ingress_rows:
  fp=str(row.get('prompt_fingerprint') or '')
  if not fp: continue
  # Synthetic/benchmark ingress is already retained in the diagnostic lane.
  # Never manufacture a missing-controller owner row for it after filtering
  # diagnostic traces; doing so made successful automated packets appear as
  # user-visible failures in the dashboard.
  if diagnostic_trace(row):
   continue
  if not any(str(trace.get('user_prompt_fingerprint') or trace.get('query_fingerprint') or '')==fp and abs(_trace_time(trace)-_trace_time(row))<=90 for trace in traces):
   source_entry=source_driven_ingress_trace(row,source_prompt_receipts)
   if source_entry:
    ingress.append(source_entry);continue
   # An ingress with no matching controller receipt is not a harmless zero.
   # Expose it as an incomplete/failed chain so a real Hook exception cannot
   # be mistaken for “no relevant memory”.  This remains a read-only status
   # projection; it does not manufacture a Controller execution or injection.
   ingress.append(unmatched_ingress_trace(row,collection_available=trace_collection_available))
 traces=merge_owner_turns(traces+ingress+recent_jsonl_file(NATIVE_RECEIPTS,30))
 known_diagnostics={(r.get('session_id'),r.get('turn_id'),r.get('execution_id') or r.get('query_id')) for r in diagnostic_rows}
 for row in traces:
  identity=(row.get('session_id'),row.get('turn_id'),row.get('execution_id') or row.get('query_id'))
  if diagnostic_trace(row) and identity not in known_diagnostics:
   diagnostic_rows.append(row);known_diagnostics.add(identity)
 diagnostic_rows.sort(key=_trace_time,reverse=True)
 # Native/context receipts are merged after the Controller filter above.  A
 # diagnostic receipt can therefore re-enter here unless the provenance gate
 # is applied once more on the final owner projection.
 traces=[row for row in traces if owner_visible_trace(row) and not diagnostic_trace(row)]
 traces.sort(key=lambda row:str(row.get('at') or ''),reverse=True)
 traces=traces[:30]
 contribution=_receipts_module.read_turn_contributions(traces,
  STATE_ROOT/'memory-os/turn-checks/checks.sqlite3',STATE_ROOT/'memory-os/capture/capture.sqlite3',
  ingress_path=PROMPT_INGRESS)
 for trace in traces:
  entry=contribution['turns'].get((trace.get('session_id'),trace.get('turn_id')))
  if entry:
   trace['mcp_audit']=entry['mcp']
   trace['answer_memory_evidence']=entry['answer']
 # Native host reception is separate from Hook stdout. Incremental bounded
 # reads expose real truncation without parsing pasted logs as received memory.
 # Host transcript inspection is expensive and can retain a large in-memory
 # receipt map. Only inspect the newest native owner turn for the status page;
 # the detail/audit endpoints perform the deeper same-turn scan on demand.
 native_sessions=attach_native_receipts(traces[:1],max_sessions=1)
 diagnostic_native_rows=[]
 for trace in traces+diagnostic_rows[:30]:
  try:attach_turn_receipt_projection(trace,ingress_rows)
  except Exception as error:
   # Projection is observability only. It must never make a completed recall
   # look failed or block the dashboard; expose this as an explicit unknown.
   errors.append('turn_projection:'+type(error).__name__)
   trace['turn_receipt_projection']={'schema':'hindsight.turn_receipt_projection.v1','identity_state':'unknown','reason_codes':['projection_error']}
 delivery_receipts=[];seen_delivery=set()
 for diagnostic,rows in ((False,traces),(True,diagnostic_rows[:30])):
  for row in rows:
   summary=actual_delivery_summary(row,diagnostic=diagnostic)
   key=(summary.get('execution_id'),summary.get('session_id'),summary.get('turn_id'))
   if key in seen_delivery:continue
   seen_delivery.add(key);delivery_receipts.append(summary)
 delivery_receipts=delivery_receipts[:12]
 try:ham_health=get(CONTROLLER,'/v2/memory-os/health'); ham_capabilities=get(CONTROLLER,'/v2/memory-os/capabilities')
 except Exception as e:ham_health={'status':'unavailable','error':repr(e)};ham_capabilities={};errors.append(f'ham-os:{e}')
 ham_projection=load(HAM_PROJECTION) or {};ham_release_gate=load(HAM_RELEASE_GATE) or {}
 audit=load(AUDIT) or {};semantic=load(SEMANTIC) or {};benchmark=load(BENCHMARK) or {};ledger=load(LEDGER) or {};life=load(LIFECYCLE) or {};refresh=load(MENTAL_REFRESH) or {};cache=load(MENTAL_CACHE) or {};guidance_worker=load(GUIDANCE_WORKER_STATE) or {};dynamic_models=guidance_v1_models();restore=load(RESTORE) or {};entity_status=load(ENTITY_STATUS) or {};assurance=load(ASSURANCE) or {}
 entity_ok=entity_status.get('status') in ('healthy','completed_with_notes')
 audit_failures=active_audit_failures(audit.get('failures',[]));audit_status='healthy' if audit.get('status')=='unhealthy' and not audit_failures else audit.get('status','unknown')
 refresh_attention=str(guidance_worker.get('status') or '') in {'failed','error'}
 overall=health.get('status')=='healthy' and controller.get('status')=='healthy' and controller.get('functional_status')=='healthy' and audit_status=='healthy' and semantic.get('passed') is True and benchmark.get('overall_pass') is True and entity_ok and assurance.get('status','healthy') in ('healthy','warming_up') and not refresh_attention and not errors
 status_traces=[trace_list_projection(row) for row in traces]
 status_latest=trace_list_projection(latest_owner_trace(traces,BANK) or (traces[0] if traces else None))
 status_effectiveness=effectiveness_list_projection(effectiveness)
 value={'schema':2,'updated_at':time.strftime('%Y-%m-%d %H:%M:%S'),'overall':'healthy' if overall else 'attention','bank_id':BANK,
 'contribution_scan':{k:v for k,v in contribution.items() if k!='turns'},
 'topology':[{'title':'提问入口','detail':'Codex / 小黛 / Trainer / Hermes'},{'title':'Full Prompt','detail':'优先使用 Agent 结合完整任务上下文形成的完整问题；缺失时明确标注降级来源'},{'title':'Hook / Adapter','detail':'传递角色、当前来源、Full Prompt 和可审计上下文解析回执'},{'title':'HAM-OS Ledger','detail':'shadow event / capsule / pack projection'},{'title':'当前项目状态','detail':'当前附件/打开文件和用户纠正优先；不会写入第二个记忆库'},{'title':'Evolving Profile Controller · 12079','detail':'每轮混合 Bank 检索、实体别名、Guidance 探测、图谱候选与覆盖缺口'},{'title':'Evolving Profile Data Plane · 12088','detail':'向量、关键词、时间、来源和结构化证据候选'},{'title':'实体与图谱层','detail':'规范实体、别名、星座/图谱关系路径与不确定别名治理'},{'title':'事实归并与时序','detail':'Claim Bundle、当前/历史/冲突状态、Required Slot 覆盖检查'},{'title':'Memory Packet','detail':'事实、关联闭包、Guidance、冲突与来源按命题打包；不按固定条数截断'},{'title':'注入与回答闭环','detail':'检索→准入→归并→渲染→实际传输→回答可用性分别回执'}],
 'actual_injection_receipts':delivery_receipts,
 'diagnostic_traces':[{'at':r.get('at'),'execution_id':r.get('execution_id') or r.get('query_id'),'execution_mode':r.get('execution_mode'),'prompt_origin':r.get('prompt_origin'),'prompt':str(r.get('raw_user_prompt') or r.get('query_preview') or '')[:160],'recorded_output_count':(r.get('memory_effectiveness') or {}).get('injected_count'),'record_ids':(r.get('memory_effectiveness') or {}).get('injected_ids') or [],'memory_needs':r.get('memory_needs') or (r.get('memory_effectiveness') or {}).get('memory_needs'),'guidance_receipt':(r.get('memory_effectiveness') or {}).get('guidance_receipt'),'host_context_receipt':r.get('host_context_receipt'),'turn_receipt_projection':r.get('turn_receipt_projection'),'session_id':r.get('session_id'),'turn_id':r.get('turn_id')} for r in diagnostic_rows[:30]],
 'source_governance':{k:v for k,v in load(STATE_ROOT/'control-plane/guidance-source-review.json',{}).items() if k in ('checked_at','policy_summary','models_reviewed','observation_count','bank_curation_receipts')},
 'latest_trace':status_latest,'traces':status_traces,'recall_analytics':recall_analytics(traces),'retrieval_quality':retrieval_quality_analytics(traces),'memory_effectiveness':status_effectiveness,'memory_effectiveness_quality':load(EFFECTIVENESS_QUALITY,{}),'controller':controller,'project_state':(controller.get('last_plan') or {}).get('project_state') or (controller.get('last_recall') or {}).get('project_state') or {},'evolving_profile':health,'stats':stats,'consolidation_backlog':consolidation_backlog_summary(stats),
 'operational_audit':{'status':audit_status,'checked_at':audit.get('checked_at'),'failures':audit_failures,'warnings':audit.get('warnings',[]),'retired_optional_failures':[row for row in audit.get('failures',[]) if row not in audit_failures]},
 'semantic_audit':{'passed':semantic.get('passed'),'score':semantic.get('score'),'max_score':semantic.get('max_score'),'at':semantic.get('at'),'failed_cases':[x for x in semantic.get('cases',[]) if not x.get('passed')]},
 'real_recall_benchmark':{'passed':benchmark.get('passed'),'total':benchmark.get('total'),'overall_pass':benchmark.get('overall_pass'),'at':benchmark.get('generated_at'),'p95_latency_seconds':benchmark.get('p95_latency_seconds'),'partition_summary':benchmark.get('partition_summary',{}),'failed_cases':[x for x in benchmark.get('cases',[]) if not x.get('pass')]}, 'config_ledger':{'status':ledger.get('status'),'head':ledger.get('head'),'tracked_files':ledger.get('tracked_files'),'updated_at':ledger.get('updated_at'),'secrets_policy':ledger.get('secrets_policy')},
 'observations':{'total':life.get('observations',stats.get('total_observations')),'pending':life.get('new_observation_pending',0),'threshold':life.get('refresh_threshold',8),'max_age_hours':life.get('refresh_max_age_hours',24),'refresh_due':life.get('refresh_due',False)},
 'mental_models':{'count':(dynamic_models.get('counts') or {}).get('active',len(dynamic_models.get('items') or [])),'candidate_count':(dynamic_models.get('counts') or {}).get('candidates',0),'archived_legacy_count':(dynamic_models.get('counts') or {}).get('archived_legacy',0),'model_semantics':'dynamic_atomic_cross_dimensional','refresh_status':guidance_worker.get('status','unknown'),'refresh_last_attempt':guidance_worker.get('at'),'acceptance':'cross-dimension+active-guidance+scope+counterevidence','needs_attention':refresh_attention,'worker_pending':guidance_worker.get('pending',0)},
 'entity_resolution':entity_status,'memory_assurance':assurance,'ham_os':{'health':ham_health,'capabilities':ham_capabilities,'projection':ham_projection,'release_gate':ham_release_gate,'official_control_plane_url':'http://127.0.0.1:12000/'},
 'selector_evaluation':selector_evaluation_snapshot(),
 'backup':backup(),'restore':{'status':restore.get('status'),'checked_at':restore.get('checked_at'),'duration_seconds':restore.get('duration_seconds'),'production_routing_changed':restore.get('production_routing_changed')},
 'services':[launch(x) for x in ('com.evolving-profile.api-shadow','com.evolving-profile.query-controller-shadow','com.evolving-profile.status-shadow','com.evolving-profile.shadow')],
 'collection':{'extra_model_calls':0,'extra_recall_calls':0,'audit_write':'入口、工具返回与回答回执分别记录，状态页只读','dashboard_read':'单一后台采集线程；各快照最短30秒更新，异常退避60秒；两个审计面板共享采集。页面后台暂停，不变数据不重复传输与重绘。','performance_effect':'存在有界的本地读取、序列化和浏览器开销；不以“没有模型调用”推断零性能影响。显示的是带时间戳的快照，不是每次请求都重新检索。'},'errors':errors}
 with CACHE_LOCK:CACHE['at']=now;CACHE['value']=value
 return value

def _refresh_snapshot_background():
 try:snapshot(force=True)
 finally:
  with CACHE_LOCK:CACHE['building']=False

def request_snapshot():
 """Never collect synchronously in HTTP workers, including cold start."""
 value=READ_CACHE.get('status',lambda:snapshot(force=True),
  {'overall':'attention','updated_at':'正在采集','traces':[],'errors':['状态快照正在采集；不是无记忆或无故障。']})
 info=READ_CACHE.info('status')
 if info.get('error'):
  return {**value,'overall':'attention','collection_state':'failed','collection_stale':True,
          'errors':list(value.get('errors') or [])+['状态采集失败：'+str(info['error'])+'；当前数值不可当作新鲜验收证据。']}
 return value
PAGE=r'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="icon" href="data:,"><title>Evolving Profile｜召回链路观测台</title><style>
:root{--ink:#152039;--muted:#748096;--line:#e5eaf2;--green:#13a36d;--orange:#e78124;--red:#d94d61;--bg:#f3f6fb;--card:#fff}*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 12% -8%,#dfe9ff 0,transparent 34%),var(--bg);color:var(--ink);font:14px -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}main{max-width:1240px;margin:auto;padding:26px 18px 60px}.header{display:flex;justify-content:space-between;gap:16px}.header h1{font-size:28px;margin:0}.sub{color:var(--muted);margin-top:7px}.badge{padding:8px 12px;border-radius:999px;font-size:12px;background:#e7f8f0;color:#087c53;border:1px solid #c8ecd9;height:max-content}.badge.attention{background:#fff3e6;color:#a85b10;border-color:#f4d2ac}.panel,.metric,.trace{background:var(--card);border:1px solid var(--line);box-shadow:0 5px 18px #1730520a;border-radius:16px}.panel{padding:18px;margin-top:14px}.panel-head{display:flex;align-items:baseline;justify-content:space-between;gap:12px;margin-bottom:13px}.panel h2{font-size:17px;margin:0}.hint{font-size:12px;color:var(--muted)}.topology{display:grid;grid-template-columns:repeat(6,1fr);gap:18px}.node{position:relative;padding:15px 13px;border:1px solid #dce4f1;border-radius:13px;background:linear-gradient(180deg,#fff,#f8faff)}.node:not(:last-child):after{content:"→";position:absolute;right:-17px;top:35%;color:#8da0c1;font-weight:800}.node b,.node small{display:block}.node small{color:var(--muted);line-height:1.5;margin-top:6px;font-size:11px}.latest{padding:22px;background:linear-gradient(125deg,#173867,#285fd1 62%,#10a79e);color:#fff;border-radius:18px}.latest-top{display:flex;justify-content:space-between;gap:16px}.latest h2{font-size:21px;margin:5px 0 9px}.latest-origin{display:inline-block;margin-left:6px;padding:3px 7px;border:1px solid #ffffff42;border-radius:99px;font-size:11px;font-weight:550;vertical-align:middle}.latest-origin.diagnostic{background:#ffb55b33;color:#fff3db}.query{font-size:15px;line-height:1.65;white-space:pre-wrap;word-break:break-word}.latest-meta{display:grid;grid-template-columns:repeat(6,1fr);gap:8px;margin-top:16px}.latest-meta div{background:#071d482b;border:1px solid #ffffff1c;border-radius:10px;padding:9px}.latest-meta small,.latest-meta b{display:block}.latest-meta small{opacity:.7;font-size:10px}.latest-meta b{font-size:12px;margin-top:4px}.facets{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:13px}.facet{background:#ffffff12;border:1px solid #ffffff24;border-radius:11px;padding:11px}.facet .status{float:right;font-size:10px;padding:3px 7px;border-radius:99px;background:#28c18c33}.facet .status.bad{background:#ff6b7c38}.facet p{font-size:11px;opacity:.78;line-height:1.5;margin:7px 0 0;word-break:break-word}.metrics{display:grid;grid-template-columns:repeat(6,1fr);gap:10px;margin-top:14px}.metric{padding:14px}.metric span,.metric small{display:block;color:var(--muted);font-size:11px}.metric b{display:block;font-size:20px;margin:6px 0}.controls{display:flex;gap:8px}.controls select{border:1px solid var(--line);background:#fff;border-radius:9px;padding:7px 9px}.history{display:grid;gap:8px}.trace{padding:0;overflow:hidden}.trace.diagnostic-trace{border-color:#efb164;background:#fffaf2}.trace summary{list-style:none;cursor:pointer;padding:12px 14px;display:grid;grid-template-columns:135px 105px 85px 1fr 100px 75px;gap:10px}.trace summary::-webkit-details-marker{display:none}.trace summary:hover{background:#f8faff}.trace.diagnostic-trace summary:hover{background:#fff3e0}.trace .q{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.provenance{display:inline-block;margin-top:4px;padding:2px 5px;border-radius:5px;font-size:10px;line-height:1.2;background:#eef2f8;color:#526078;white-space:nowrap}.provenance.diagnostic{background:#ffe1b6;color:#92510d}.provenance.owner{background:#e4f7ee;color:#087c53}.ok{color:var(--green)}.partial{color:var(--orange)}.failed{color:var(--red)}.trace-body{border-top:1px solid var(--line);padding:14px;background:#fafbfd}.trace.diagnostic-trace .trace-body{background:#fffaf2}.chain-row{display:grid;grid-template-columns:125px 1fr 90px 75px 90px;gap:10px;padding:8px 0;border-bottom:1px dashed #e4e9f1}.pre{white-space:pre-wrap;word-break:break-word;color:#526078;line-height:1.55}.bottom{display:grid;grid-template-columns:1fr 1fr;gap:14px}.issue{padding:9px 11px;background:#fff5e9;border:1px solid #f2d6b8;border-radius:10px;color:#915216;margin-top:7px}.footer{display:flex;justify-content:space-between;color:var(--muted);font-size:12px;margin-top:14px}a{color:#2868f0;text-decoration:none}@media(max-width:900px){.topology{grid-template-columns:repeat(2,1fr)}.node:after{display:none}.latest-meta,.metrics{grid-template-columns:repeat(3,1fr)}.facets{grid-template-columns:1fr}.trace summary{grid-template-columns:105px 75px 1fr 70px}.trace summary span:nth-child(3),.trace summary span:nth-child(6){display:none}.bottom{grid-template-columns:1fr}}@media(max-width:560px){main{padding:18px 11px 44px}.header{display:block}.badge{display:inline-block;margin-top:10px}.latest-meta,.metrics{grid-template-columns:repeat(2,1fr)}.topology{grid-template-columns:1fr}.trace summary{grid-template-columns:88px 64px 1fr}.trace summary span:nth-child(5){display:none}.chain-row{grid-template-columns:90px 1fr}.chain-row span:nth-child(n+3){display:none}.footer{display:block;line-height:1.8}}
</style></head><body><main><header class="header"><div><h1>Evolving Profile <span style="font-weight:480;color:#8390a5">召回链路观测台</span></h1><div class="sub">看清每次问题如何识别、拆分、召回、融合并注入；仅观测，不额外调用模型。</div></div><div id="overall" class="badge">正在连接</div></header><section class="panel"><div class="panel-head"><h2>真实运行链路</h2><span class="hint">来自 Evolving Profile Controller 的真实审计轨迹</span></div><div id="topology" class="topology"></div></section><section id="latest" class="panel"><div class="hint">等待召回记录</div></section><section id="metrics" class="metrics"></section><section class="panel"><div class="panel-head"><h2>最近召回历史</h2><div class="controls"><select id="statusFilter"><option value="">全部结果</option><option value="completed">完成</option><option value="partial">部分完成</option><option value="failed">失败</option></select><select id="shapeFilter"><option value="">全部类型</option></select></div></div><div id="history" class="history"></div></section><section class="bottom"><section class="panel"><div class="panel-head"><h2>质量门与维护</h2><span class="hint">区分服务存活与功能质量</span></div><div id="quality"></div></section><section class="panel"><div class="panel-head"><h2>性能影响</h2><span class="hint">本页不会触发记忆召回</span></div><div id="performance"></div></section></section><footer class="footer"><span id="updated">—</span><a href="http://127.0.0.1:12000/">打开 Evolving Profile 控制面（稳定链接） →</a></footer></main><script>
function esc(s){return String(s==null?'—':s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]})}function fmtBytes(n){return !n?'—':n>1e9?(n/1e9).toFixed(2)+' GB':n>1e6?(n/1e6).toFixed(1)+' MB':n+' B'}function fmtAge(s){return s==null?'—':s<3600?Math.round(s/60)+' 分钟':s<86400?(s/3600).toFixed(1)+' 小时':(s/86400).toFixed(1)+' 天'}function fmtTime(v){if(!v)return '—';var d=new Date(v);return isNaN(d)?String(v):new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(d)}
function facet(f){var bad=f.status==='failed';return '<div class="facet"><span class="status '+(bad?'bad':'')+'">'+(bad?'失败':'完成')+'</span><b>'+esc(f.label)+'</b><p>'+esc(f.query_preview)+'</p><p>'+esc(f.query_tokens)+' tokens · '+esc(f.elapsed_ms)+' ms · '+esc(f.result_count)+' 条'+(f.compacted?' · 已压缩':'')+'</p></div>'}
function latest(t){if(!t)return '<div class="hint">暂无召回记录。下一次真实提问会自动出现。</div>';var fs=(t.facets||[]).map(facet).join(''),p=t.prompt_provenance||{},pk=String(p.kind||''),diagnostic=['agent_tool_call','controller_internal_helper','test_probe'].indexOf(pk)>=0,pl=p.label||'来源未记录';return '<div class="latest"><div class="latest-top"><div><small>'+esc(fmtTime(t.at))+' · '+esc(t.client)+' · '+esc(t.role)+'</small><h2>'+esc(t.shape)+' · '+esc((t.strategies||[]).join(' / '))+'<span class="latest-origin '+(diagnostic?'diagnostic':'')+'">'+esc(pl)+'</span></h2></div><b>'+(t.outcome==='completed'?'链路完成':t.outcome==='partial'?'部分完成':'链路失败')+'</b></div><div class="query">'+esc(t.query_preview)+'</div><div class="latest-meta"><div><small>输入长度</small><b>'+esc(t.input_query_tokens)+' tokens</b></div><div><small>分面</small><b>'+esc(t.queries_completed)+' / '+esc(t.queries_requested)+'</b></div><div><small>合并结果</small><b>'+esc(t.result_count)+' 条</b></div><div><small>总耗时</small><b>'+esc(t.elapsed_ms)+' ms</b></div><div><small>覆盖</small><b>'+(t.coverage_complete?'完整':'不完整')+'</b></div><div><small>兜底</small><b>'+(t.fallback_used?'已使用':'未使用')+'</b></div></div>'+(fs?'<div class="facets">'+fs+'</div>':'')+'</div>'}
function trace(t){var fs=(t.facets||[]).map(function(f){return '<div class="chain-row"><b>'+esc(f.label)+'</b><span class="pre">'+esc(f.query_preview)+'</span><span>'+esc(f.query_tokens)+' tok</span><span>'+esc(f.result_count)+' 条</span><span class="'+(f.status==='failed'?'failed':'ok')+'">'+esc(f.elapsed_ms)+' ms</span></div>'}).join('');var p=t.prompt_provenance||{},pk=String(p.kind||''),diagnostic=['agent_tool_call','controller_internal_helper','test_probe'].indexOf(pk)>=0,pl=p.label||'来源未记录',detail=p.detail||'页面没有保存更细的输入来源说明。';return '<details class="trace '+(diagnostic?'diagnostic-trace':'')+'" data-outcome="'+esc(t.outcome)+'" data-shape="'+esc(t.shape)+'"><summary><span>'+esc(fmtTime(t.at))+'</span><span>'+esc(t.client)+'<em class="provenance '+(diagnostic?'diagnostic':'owner')+'">'+esc(pl)+'</em></span><span>'+esc(t.shape)+'</span><span class="q">'+esc(t.query_preview)+'</span><span>'+esc(t.result_count)+' 条 · '+esc(t.elapsed_ms)+' ms</span><span class="'+esc(t.outcome)+'">'+esc(t.outcome)+'</span></summary><div class="trace-body"><div><b>输入来源：</b><span class="provenance '+(diagnostic?'diagnostic':'owner')+'">'+esc(pl)+'</span>　<span class="hint">'+esc(detail)+'</span></div><div style="margin:8px 0"><b>原始 Prompt：</b><span class="pre">'+esc(t.raw_user_prompt||t.query_preview)+'</span></div><div><b>策略：</b>'+esc((t.strategies||[]).join(' / '))+'　<b>覆盖维度：</b>'+esc((t.coverage_dimensions||[]).join(' / '))+'</div><div style="margin:8px 0"><b>检索问题：</b><span class="pre">'+esc(t.query_preview)+'</span></div>'+(fs||'<div class="hint">旧记录没有分面明细；新记录开始完整展示。</div>')+((t.errors||[]).length?'<div class="issue">'+esc(t.errors.join('\n'))+'</div>':'')+'</div></details>'}
function filters(){var s=document.getElementById('statusFilter').value,q=document.getElementById('shapeFilter').value;document.querySelectorAll('.trace').forEach(function(el){el.style.display=(!s||el.dataset.outcome===s)&&(!q||el.dataset.shape===q)?'block':'none'})}
function render(d){var good=d.overall==='healthy',o=document.getElementById('overall');o.className='badge '+(good?'':'attention');o.textContent=good?'全链路正常':'有项目需要处理';document.getElementById('topology').innerHTML=(d.topology||[]).map(function(n){return '<div class="node"><b>'+esc(n.title)+'</b><small>'+esc(n.detail)+'</small></div>'}).join('');document.getElementById('latest').innerHTML=latest(d.latest_trace);var c=d.controller||{},s=d.stats||{},ob=d.observations||{},mm=d.mental_models||{},b=d.backup||{},rq=d.retrieval_quality||{},se=d.selector_evaluation||{},cards=[['Controller',c.functional_status||c.status,'8879 · '+(c.version||'—')],['正式 Bank',s.total_nodes==null?'—':s.total_nodes,'结构化记忆'],['证据观察',ob.total==null?'—':ob.total,'待累计 '+(ob.pending||0)+' / '+(ob.threshold||8)],['心智模型',mm.count==null?'—':mm.count,'候选 '+(mm.candidate_count||0)+' · 历史汇总 '+(mm.archived_legacy_count||0)],['最新备份',b.present?'已验证':'缺失',b.present?fmtAge(b.age_seconds)+' · '+fmtBytes(b.bytes):'—'],['最近召回',c.last_recall?c.last_recall.elapsed_ms:'—',(c.last_recall?c.last_recall.result_count:0)+' 条结果'],['证据升级',rq.escalation_count==null?'—':rq.escalation_count,'research 建议 '+(rq.research_recommendation_count==null?'—':rq.research_recommendation_count)],['选择器评测',se.cases==null?'—':se.cases,'P '+(se.metrics&&se.metrics.precision==null?'—':se.metrics.precision)+' · R '+(se.metrics&&se.metrics.recall==null?'—':se.metrics.recall)],['HAM-OS',((d.ham_os||{}).health||{}).status||'—','Phase '+((((d.ham_os||{}).health||{}).flags||{}).phase||'—')]];document.getElementById('metrics').innerHTML=cards.map(function(x){return '<div class="metric"><span>'+esc(x[0])+'</span><b>'+esc(x[1])+'</b><small>'+esc(x[2])+'</small></div>'}).join('');var shapes=Array.from(new Set((d.traces||[]).map(function(x){return x.shape}).filter(Boolean))),sf=document.getElementById('shapeFilter'),old=sf.value;sf.innerHTML='<option value="">全部类型</option>'+shapes.map(function(x){return '<option value="'+esc(x)+'">'+esc(x)+'</option>'}).join('');sf.value=old;document.getElementById('history').innerHTML=(d.traces||[]).map(trace).join('')||'<div class="hint">暂无历史轨迹。</div>';var oa=d.operational_audit||{},sa=d.semantic_audit||{},eq=d.memory_effectiveness_quality||{},issues=[];(oa.failures||[]).forEach(function(x){issues.push('运行审计：'+x)});(sa.failed_cases||[]).forEach(function(x){issues.push('语义验收：'+x.id)});(d.errors||[]).forEach(function(x){issues.push('采集：'+x)});var hold=(mm.refresh_status==='quality_hold'),holdText=hold?'观察归纳：质量暂缓（'+(ob.pending||0)+' 条待后续候选复审；原始证据仍可召回）':'';document.getElementById('quality').innerHTML='<p>运行审计：<b class="'+(oa.status==='healthy'?'ok':'failed')+'">'+esc(oa.status)+'</b></p><p>语义验收：<b class="'+(sa.passed?'ok':'failed')+'">'+esc(sa.score)+' / '+esc(sa.max_score)+'</b></p><p>动态模型加工：<b>'+esc(mm.refresh_status)+'</b></p><p>恢复演练：<b class="'+(d.restore&&d.restore.status==='passed'?'ok':'failed')+'">'+esc(d.restore?d.restore.status:'—')+'</b></p><p>回答回执：<b class="'+(eq.status==='attention'?'failed':'ok')+'">'+esc(eq.status||'—')+'</b>　闭环 '+esc(eq.closed_answer_feedback==null?'—':eq.closed_answer_feedback)+'；可验证复用 '+esc(eq.visible_answer_evidence==null?'—':eq.visible_answer_evidence)+'</p>'+((hold?'<div class="issue">'+esc(holdText)+'</div>':'')+(issues.map(function(x){return '<div class="issue">'+esc(x)+'</div>'}).join('')||'<div class="hint">没有发现待处理项目。</div>'));var p=d.collection||{};document.getElementById('performance').innerHTML='<p>额外模型调用：<b>'+esc(p.extra_model_calls)+'</b></p><p>额外召回调用：<b>'+esc(p.extra_recall_calls)+'</b></p><p>轨迹记录：'+esc(p.audit_write)+'</p><p>页面读取：'+esc(p.dashboard_read)+'</p><div class="hint">'+esc(p.performance_effect)+'</div>';document.getElementById('updated').textContent='状态刷新：'+d.updated_at;filters()}
async function refresh(){try{var r=await fetch('/api/status',{cache:'no-store'});render(await r.json())}catch(e){var o=document.getElementById('overall');o.className='badge attention';o.textContent='状态页连接异常'}}document.getElementById('statusFilter').addEventListener('change',filters);document.getElementById('shapeFilter').addEventListener('change',filters);refresh();setInterval(refresh,10000);
</script></body></html>'''
class H(BaseHTTPRequestHandler):
 def do_OPTIONS(self):
  origin=self.headers.get('Origin')
  self.send_response(204)
  if origin in ('http://127.0.0.1:9999','http://localhost:12000'):
   self.send_header('Access-Control-Allow-Origin',origin);self.send_header('Vary','Origin')
  self.send_header('Access-Control-Allow-Methods','GET, OPTIONS');self.send_header('Access-Control-Allow-Headers','Content-Type');self.send_header('Content-Length','0');self.end_headers()
 def _guidance_admin(self):
  try:cfg=json.loads(GUIDANCE_V1_CONFIG.read_text())
  except Exception:return None
  origin=self.headers.get('Origin')
  local=self.client_address[0] in ('127.0.0.1','::1')
  origin_ok=not origin or origin in ('http://127.0.0.1:12098','http://localhost:12098')
  token_ok=self.headers.get('X-Guidance-Admin-Token')==cfg.get('owner_token')
  return cfg if local and origin_ok and token_ok else None
 def do_POST(self):
  path=urllib.parse.urlparse(self.path).path;cfg=self._guidance_admin()
  if not cfg:self.send_error(403,'local guidance admin authorization required');return
  try:
   length=int(self.headers.get('Content-Length','0'))
   if length<0 or length>1024*1024:raise ValueError('request_too_large')
   body=json.loads(self.rfile.read(length) or b'{}')
   if str(GUIDANCE_V1_SRC) not in sys.path:sys.path.insert(0,str(GUIDANCE_V1_SRC))
   from mcp_runtime import load_repository
   repo=load_repository(GUIDANCE_V1_CONFIG);key=self.headers.get('Idempotency-Key')
   if not key:raise ValueError('Idempotency-Key required')
   if path=='/api/guidance/proposals':
    result=repo.idempotent(key,'proposal',lambda:{'proposal_id':repo.record_job('proposal','candidate',body),'state':'candidate'})
   elif re.fullmatch(r'/api/guidance/proposals/[^/]+/review',path):
    result=repo.idempotent(key,'review',lambda:{'review_id':repo.record_job('review','recorded',{'proposal_id':path.split('/')[-2],'review':body}),'state':'recorded'})
   elif path=='/api/guidance/publications':
    from publisher import prepare_publication,commit_publication
    def publish():
     prepared=prepare_publication(repo,body['proposal'],body['review'],cfg['owner_token'])
     return commit_publication(repo,prepared['publication_id'],body.get('expected_active_revision'),prepared['source_tokens'],cfg['owner_token'])
    result=repo.idempotent(key,'publication',publish)
   elif re.fullmatch(r'/api/guidance/units/[^/]+/withdraw',path):
    unit_id=urllib.parse.unquote(path.split('/')[-2]);expected=self.headers.get('If-Match')
    if not expected:raise ValueError('If-Match required')
    result=repo.idempotent(key,'withdraw:'+unit_id,lambda:repo.withdraw_unit(unit_id,expected,str(body.get('reason') or 'admin_withdrawal')))
   else:self.send_error(404);return
   payload=json.dumps(result,ensure_ascii=False).encode();self.send_response(200);self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Content-Length',str(len(payload)));self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(payload)
  except Exception as error:self.send_error(409,str(error)[:500])
 def do_GET(self):
  parsed=urllib.parse.urlparse(self.path);path=parsed.path
  try:
   etag=None
   if path=='/api/status':body,etag=api_payload(path,request_snapshot());ctype='application/json; charset=utf-8'
   elif path=='/api/guidance/status':body,etag=api_payload(path,guidance_v1_snapshot());ctype='application/json; charset=utf-8'
   elif path=='/api/guidance/units':body,etag=api_payload(path,guidance_v1_units());ctype='application/json; charset=utf-8'
   elif path=='/api/guidance/models':body,etag=api_payload(path,guidance_v1_models());ctype='application/json; charset=utf-8'
   elif path=='/api/guidance/candidates':
    query=urllib.parse.parse_qs(parsed.query);limit=int((query.get('limit') or ['500'])[0]);body,etag=api_payload(path+':'+str(limit),guidance_v1_candidates(limit));ctype='application/json; charset=utf-8'
   elif path=='/api/guidance/jobs':body,etag=api_payload(path,guidance_v1_jobs());ctype='application/json; charset=utf-8'
   elif path=='/api/guidance/instructions':body,etag=api_payload(path,guidance_instruction_status());ctype='application/json; charset=utf-8'
   elif path=='/api/guidance/memory-map':body,etag=api_payload(path,memory_map_snapshot());ctype='application/json; charset=utf-8'
   elif path=='/api/guidance/topics':
    query=urllib.parse.parse_qs(parsed.query);term=(query.get('q') or [''])[0];limit=int((query.get('limit') or ['30'])[0]);catalog=TopicCatalog(TOPIC_CATALOG_PATH)
    try:pages=get(API,f'/v1/default/banks/{urllib.parse.quote(BANK,safe="")}/knowledge-base/tree',3).get('roots') or []
    except Exception:pages=[]
    if term:
     terms=[value.casefold() for value in re.findall(r'[a-z][a-z0-9_.+-]{1,}|[\u4e00-\u9fff]{2,}',term.casefold())]
     pages=[row for row in pages if any(value in (str(row.get('name') or '')+' '+str(row.get('description') or '')).casefold() for value in terms)]
    rows=(catalog.search(term,limit) if term else catalog.list(limit)) or [redact_unreviewed_page(row) for row in pages[:limit]];body,etag=api_payload(path+':'+term+':'+str(limit),{'schema':'evolving-profile.topic-catalog.v1','items':rows,'boundary':'navigation_only_not_fact_evidence'});ctype='application/json; charset=utf-8'
   elif re.fullmatch(r'/api/guidance/topics/[^/]+',path):
    topic_id=urllib.parse.unquote(path.rsplit('/',1)[-1]);row=None
    if topic_id.startswith('kp-'):
     try:row=get(API,f'/v1/default/banks/{urllib.parse.quote(BANK,safe="")}/knowledge-base/pages/{urllib.parse.quote(topic_id,safe="")}',3)
     except Exception:row=None
    if row is None:row=TopicCatalog(TOPIC_CATALOG_PATH).get(topic_id)
    query=urllib.parse.parse_qs(parsed.query);offset=max(0,int((query.get('offset') or ['0'])[0]))
    if row and (row.get('level')=='L1' or topic_id=='domain:pending'):row['evidence_page']=TopicCatalog(TOPIC_CATALOG_PATH).evidence_page(topic_id,offset,8)
    body,etag=api_payload(path+':'+str(offset),{'status':'found' if row else 'not_found','topic':redact_unreviewed_page(row) if row and topic_id.startswith('kp-') else row,'boundary':'navigation_only_not_fact_evidence'});ctype='application/json; charset=utf-8'
   elif path=='/api/guidance/memory-check':
    query=urllib.parse.parse_qs(parsed.query);prompt=(query.get('q') or [''])[0];body,etag=api_payload(path+':'+prompt,memory_check(prompt));ctype='application/json; charset=utf-8'
   elif path=='/api/guidance/deliveries':
    query=urllib.parse.parse_qs(parsed.query);limit=int((query.get('limit') or ['50'])[0]);body,etag=api_payload(path+':'+str(limit),guidance_delivery_list(limit));ctype='application/json; charset=utf-8'
   elif re.fullmatch(r'/api/guidance/deliveries/[^/]+',path):
    occurrence_id=urllib.parse.unquote(path.rsplit('/',1)[-1]);body,etag=api_payload(path,guidance_delivery_detail(occurrence_id));ctype='application/json; charset=utf-8'
   elif path=='/api/guidance/prompts':
    query=urllib.parse.parse_qs(parsed.query);limit=(query.get('limit') or ['20'])[0];cursor=(query.get('cursor') or ['0'])[0];host=(query.get('host') or ['all'])[0];search=(query.get('q') or [''])[0];prompt_source=(query.get('prompt_source') or ['natural'])[0];body,etag=api_payload(path+':'+str(limit)+':'+str(cursor)+':'+str(host)+':'+str(search)+':'+str(prompt_source),guidance_prompt_list(limit,cursor,host,query_text=search,prompt_source=prompt_source));ctype='application/json; charset=utf-8'
   elif re.fullmatch(r'/api/guidance/prompts/[^/]+',path):
    prompt_id=urllib.parse.unquote(path.rsplit('/',1)[-1]);query=urllib.parse.parse_qs(parsed.query)
    group=(query.get('candidate_group') or [None])[0];offset=int((query.get('offset') or ['0'])[0]);limit=int((query.get('limit') or ['10'])[0])
    body,etag=api_payload(path+':'+str(group)+':'+str(offset)+':'+str(limit),guidance_prompt_detail(prompt_id,group,offset,limit));ctype='application/json; charset=utf-8'
   elif re.fullmatch(r'/api/guidance/audit/[^/]+',path):
    prompt_id=urllib.parse.unquote(path.rsplit('/',1)[-1]);body,etag=api_payload(path,guidance_prompt_audit(prompt_id));ctype='application/json; charset=utf-8'
   elif path=='/api/traces':body,etag=api_payload(path,{'items':request_snapshot().get('traces',[])});ctype='application/json; charset=utf-8'
   elif path=='/api/research':body,etag=api_payload(path,request_audit_snapshot()['research']);ctype='application/json; charset=utf-8'
   elif path=='/api/reference-views':body,etag=api_payload(path,request_audit_snapshot()['reference']);ctype='application/json; charset=utf-8'
   elif path=='/api/turn-audit':body,etag=api_payload(path,request_turn_audit());ctype='application/json; charset=utf-8'
   elif path=='/status-poller.js':body=(STATE_ROOT/'control-plane/status-poller.js').read_bytes();ctype='application/javascript; charset=utf-8'
   elif path in ('/guidance-trace','/guidance-trace/','/guidance-trace/index.html'):body=(GUIDANCE_TRACE_PAGE.read_bytes() if GUIDANCE_TRACE_PAGE.exists() else b'guidance trace unavailable');ctype='text/html; charset=utf-8'
   elif path in ('/guidance','/guidance/','/guidance/index.html'):body=(GUIDANCE_V1_PAGE.read_bytes() if GUIDANCE_V1_PAGE.exists() else b'guidance page unavailable');ctype='text/html; charset=utf-8'
   elif path in ('/favicon.ico','/guidance/favicon.ico'):body=b'';ctype='image/x-icon'
   elif path in ('/','/index.html'):
    # Keep the historical 9998 bookmark valid while presenting one unified
    # Hindsight control plane. API routes above remain on 9998 for the native
    # 9999 views; only the human-facing root is redirected.
    self.send_response(302);self.send_header('Location','http://127.0.0.1:12000/en/banks/'+urllib.parse.quote(BANK,safe='')+'?view=flow');self.send_header('Cache-Control','no-store');self.end_headers();return
   else:self.send_error(404);return
   unchanged=etag and self.headers.get('If-None-Match')==etag
   self.send_response(304 if unchanged else 200)
   if etag:
    self.send_header('ETag',etag)
    info=READ_CACHE.info('turn-audit' if path=='/api/turn-audit' else 'audit' if path in ('/api/research','/api/reference-views') else 'status')
    self.send_header('X-Snapshot-Builds',str(info.get('builds',0)))
    self.send_header('X-Snapshot-Refreshing',str(bool(info.get('building'))).lower())
    self.send_header('X-Snapshot-Error',info.get('error') or 'none')
   self.send_header('Content-Type',ctype);self.send_header('Content-Length',str(0 if unchanged else len(body)));self.send_header('Cache-Control','no-cache');self.send_header('X-Content-Type-Options','nosniff')
   origin=self.headers.get('Origin')
   if origin in ('http://127.0.0.1:9999','http://localhost:12000'):
    self.send_header('Access-Control-Allow-Origin',origin);self.send_header('Vary','Origin')
   self.end_headers()
   if not unchanged:self.wfile.write(body)
  except Exception as e:self.send_error(500,str(e))
 def log_message(self,*_):pass
if __name__=='__main__':ThreadingHTTPServer(('127.0.0.1',int(os.environ.get('EVOLVING_PROFILE_STATUS_PORT','12098'))),H).serve_forever()
