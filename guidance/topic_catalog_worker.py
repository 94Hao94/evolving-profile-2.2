#!/usr/bin/env python3
"""Build an entity-backed topic projection without copying Bank fact bodies."""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
from hashlib import sha256
import json
import os
import re
import sys
import time
from pathlib import Path

HOST_ADAPTER=Path(__file__).resolve().parent.parent/'host-adapter'
if str(HOST_ADAPTER) not in sys.path:sys.path.insert(0,str(HOST_ADAPTER))
from topic_catalog import TopicCatalog
from topic_semantics import merge_semantic_snapshot

MANIFESTS=Path(__file__).with_name('topic_manifests.json')
REFRESH_SECONDS=60
STALE_AFTER_SECONDS=180
RECONCILE_SECONDS=3600
GENERATOR_VERSION='navigation-v2-20260920'


def build_topic(row:dict)->dict:
    related=list(row.get('related') or [])[:8]
    fact_types=dict(row.get('fact_types') or {})
    parts=[]
    if fact_types:parts.append('记录类型：'+'、'.join(f'{key} {value}' for key,value in fact_types.items()))
    if related:parts.append('相关入口：'+'、'.join(related))
    overview='；'.join(parts) or '该主题已有Bank来源定位，可按需下钻到原始证据。'
    source_count=int(row.get('source_count') or 0)
    locators=list(row.get('source_locators') or [])
    return {
        'topic_id':'entity:'+str(row['id']),'title':str(row['canonical_name']),
        'abstract':f"有关{row['canonical_name']}的世界事实、经历与来源导航。",
        'overview':overview,'entities':[str(row['canonical_name']),*related],
        'time_range':{'start':row.get('first_source_at') or row.get('first_seen'),'end':row.get('latest_source_at') or row.get('last_seen')},
        'time_range_semantics':'source_record_lifecycle_not_event_dates' if row.get('latest_source_at') else 'entity_observation_times',
        'latest_source_at':row.get('latest_source_at'),'bank_id':row.get('bank_id'),
        'source_count':source_count,'source_locators':locators,
        'coverage':{'sampled':len(locators),'total':source_count},'pending_changes':row.get('pending_changes'),
        'conflicts':row.get('conflicts'),'children':row.get('children'),
        'content_status':'entity_navigation_only','overview_status':'structural_counts_and_relations_not_semantic_summary',
        'pending_changes_status':'not_computed' if row.get('pending_changes') is None else 'computed',
        'conflict_status':'not_computed' if row.get('conflicts') is None else 'computed',
        'refreshed_at':row.get('refreshed_at') or dt.datetime.now(dt.timezone.utc).isoformat(),
    }


def build_manifest_topics(entity_topics:list[dict],manifests:list[dict])->list[dict]:
    """Build question-oriented navigation manifests without asserting facts."""
    result=[]
    for manifest in manifests:
        aliases=[str(value) for value in manifest.get('aliases') or []]
        matched=[]
        for row in entity_topics:
            # A co-mentioned neighbour is not membership of this topic. Latin
            # word boundaries also keep EP from matching "step" or "report".
            corpus=str(row.get('title') or '').casefold()
            if any((bool(re.search(r'(?<![a-z0-9])'+re.escape(alias.casefold())+r'(?![a-z0-9])',corpus))
                    if re.search(r'[a-zA-Z]',alias) else alias.casefold() in corpus) for alias in aliases):matched.append(row)
        locators=[];seen=set()
        for row in matched:
            for locator in row.get('source_locators') or []:
                key=(locator.get('memory_id'),locator.get('document_id'))
                if key in seen:continue
                seen.add(key);locators.append(locator)
        starts=[(row.get('time_range') or {}).get('start') for row in matched if (row.get('time_range') or {}).get('start')]
        ends=[(row.get('time_range') or {}).get('end') for row in matched if (row.get('time_range') or {}).get('end')]
        questions=[str(value) for value in manifest.get('questions') or []]
        unique_documents={str(locator.get('document_id')) for locator in locators if locator.get('document_id')}
        result.append({
            'topic_id':'manifest:'+str(manifest['id']),'title':manifest['title'],
            'abstract':'用于判断是否需要读取该主题历史证据的问题清单；不直接陈述事实结论。',
            'navigation_summary':manifest.get('navigation_summary'),
            'example_entities':[row['title'] for row in sorted(matched,key=lambda row:(-row.get('source_count',0),row['title']))[:3]],
            'latest_source_at':max((row.get('latest_source_at') or '' for row in matched),default='') or None,
            'overview':'可导航问题：\n- '+'\n- '.join(questions),
            'entities':aliases,'time_range':{'start':min(starts) if starts else None,'end':max(ends) if ends else None},
            'time_range_semantics':'sampled_source_record_lifecycle_not_event_dates',
            'source_count':len(unique_documents),'source_count_semantics':'sampled_unique_documents_lower_bound','source_locators':locators[:20],
            'coverage':{'sampled':len(locators[:20]),'total':None,'semantics':'sampled_locators_not_exhaustive'},
            'pending_changes':None,'conflicts':None,'children':[row['topic_id'] for row in matched],
            'content_status':'reviewed_navigation_manifest','overview_status':'question_oriented_navigation_not_fact_evidence',
            'pending_changes_status':'not_computed','conflict_status':'not_computed',
            'refreshed_at':dt.datetime.now(dt.timezone.utc).isoformat(),
        })
    return result


def select_entities(rows:list[dict],limit:int)->list[dict]:
    """Reserve space for recent source changes, even for rarely mentioned entities."""
    limit=max(1,int(limit));recent_slots=max(1,limit//5)
    popular=sorted(rows,key=lambda row:(-int(row.get('mention_count') or 0),row['id']))
    recent=sorted(rows,key=lambda row:(str(row.get('latest_source_at') or ''),row['id']),reverse=True)
    chosen=[];seen=set()
    for row in [*recent[:recent_slots],*popular]:
        if row['id'] in seen:continue
        chosen.append(row);seen.add(row['id'])
        if len(chosen)>=limit:break
    return chosen


def source_snapshot(cursor,bank_id):
    """Fingerprint only index dependencies, scoped to one Bank, including deletions."""
    cursor.execute('''SELECT count(*) AS memory_count,max(greatest(updated_at,created_at))::text AS source_updated_at,
        md5(coalesce(string_agg(concat_ws('|',id,document_id,fact_type,created_at,updated_at),'\n' ORDER BY id),'')) AS revision
        FROM memory_units WHERE bank_id=%s''',(bank_id,))
    memory=dict(cursor.fetchone())
    cursor.execute('''SELECT count(*) AS entity_count,
        md5(coalesce(string_agg(concat_ws('|',id,canonical_name,mention_count),'\n' ORDER BY id),'')) AS revision
        FROM entities WHERE bank_id=%s''',(bank_id,))
    entities=dict(cursor.fetchone())
    cursor.execute('''SELECT md5(coalesce(string_agg(concat_ws('|',ue.entity_id,ue.unit_id),'\n' ORDER BY ue.entity_id,ue.unit_id),'')) AS revision
        FROM unit_entities ue JOIN memory_units m ON m.id=ue.unit_id JOIN entities e ON e.id=ue.entity_id
        WHERE m.bank_id=%s AND e.bank_id=%s''',(bank_id,bank_id))
    memberships=dict(cursor.fetchone())
    cursor.execute('''SELECT md5(coalesce(string_agg(concat_ws('|',ec.entity_id_1,ec.entity_id_2,ec.cooccurrence_count,ec.last_cooccurred),'\n' ORDER BY ec.entity_id_1,ec.entity_id_2),'')) AS revision
        FROM entity_cooccurrences ec JOIN entities e1 ON e1.id=ec.entity_id_1 JOIN entities e2 ON e2.id=ec.entity_id_2
        WHERE e1.bank_id=%s AND e2.bank_id=%s''',(bank_id,bank_id))
    relations=dict(cursor.fetchone())
    return {'bank_id':bank_id,'memory_count':memory['memory_count'],'entity_count':entities['entity_count'],
            'source_updated_at':memory['source_updated_at'],
            'revision':sha256(json.dumps([memory,entities,memberships,relations],sort_keys=True).encode()).hexdigest()}


def read_entity_projection(cursor,bank_id,limit):
    cursor.execute('''WITH facts AS (
        SELECT ue.entity_id,count(DISTINCT m.document_id) AS source_count,
            min(m.created_at)::text AS first_source_at,max(greatest(m.updated_at,m.created_at))::text AS latest_source_at
        FROM unit_entities ue JOIN memory_units m ON m.id=ue.unit_id WHERE m.bank_id=%s GROUP BY ue.entity_id
    ), types AS (
        SELECT entity_id,jsonb_object_agg(fact_type,n) AS fact_types FROM (
            SELECT ue.entity_id,m.fact_type,count(*) AS n FROM unit_entities ue JOIN memory_units m ON m.id=ue.unit_id
            WHERE m.bank_id=%s GROUP BY ue.entity_id,m.fact_type
        ) t GROUP BY entity_id
    ) SELECT e.id::text,e.canonical_name,e.bank_id,e.mention_count,facts.*,types.fact_types
      FROM entities e JOIN facts ON facts.entity_id=e.id JOIN types ON types.entity_id=e.id
      WHERE e.bank_id=%s AND e.canonical_name <> ALL(%s)
    ''',(bank_id,bank_id,bank_id,['示例用户','助手','用户','user','assistant']))
    rows=[dict(row) for row in cursor.fetchall()];selected=select_entities(rows,limit)
    entity_index=[{'id':'entity:'+row['id'],'title':row['canonical_name'],'source_count':row['source_count'],
                   'latest_source_at':row.get('latest_source_at')} for row in rows]
    by_id={row['id']:row for row in selected};ids=list(by_id)
    for row in selected:row.update(related=[],source_locators=[])
    if ids:
        cursor.execute('''SELECT entity_id::text,id::text AS memory_id,document_id FROM (
            SELECT ue.entity_id,m.id,m.document_id,row_number() OVER(PARTITION BY ue.entity_id ORDER BY greatest(m.updated_at,m.created_at) DESC,m.id) AS rn
            FROM unit_entities ue JOIN memory_units m ON m.id=ue.unit_id
            WHERE ue.entity_id=ANY(%s::uuid[]) AND m.bank_id=%s
        ) s WHERE rn<=5''',(ids,bank_id))
        for row in cursor.fetchall():by_id[row['entity_id']]['source_locators'].append({'memory_id':row['memory_id'],'document_id':row['document_id']})
        cursor.execute('''SELECT entity_id::text,canonical_name FROM (
            SELECT links.entity_id,e.canonical_name,row_number() OVER(PARTITION BY links.entity_id ORDER BY links.n DESC,e.id) AS rn FROM (
                SELECT entity_id_1 AS entity_id,entity_id_2 AS other,cooccurrence_count AS n FROM entity_cooccurrences WHERE entity_id_1=ANY(%s::uuid[])
                UNION ALL SELECT entity_id_2,entity_id_1,cooccurrence_count FROM entity_cooccurrences WHERE entity_id_2=ANY(%s::uuid[])
            ) links JOIN entities e ON e.id=links.other WHERE e.bank_id=%s
        ) r WHERE rn<=8''',(ids,ids,bank_id))
        for row in cursor.fetchall():by_id[row['entity_id']]['related'].append(row['canonical_name'])
    return selected,len(rows),entity_index


def _write_state(path,value):
    path=Path(path);temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8');temporary.replace(path)


def refresh_catalog(database_url,output,bank_id,*,limit=500,markdown=None,force=False):
    import psycopg2
    from psycopg2.extras import RealDictCursor
    output=Path(output);output.parent.mkdir(parents=True,exist_ok=True)
    state_path=output.with_suffix('.refresh.json')
    with output.with_suffix('.lock').open('a+') as lock:
        try:fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return {'status':'already_running'}
        catalog=TopicCatalog(output);previous=catalog.metadata();started=dt.datetime.now(dt.timezone.utc).isoformat()
        base={'schema':'evolving-profile.catalog-refresh.v2','checked_at':started,'refresh_interval_seconds':REFRESH_SECONDS,
              'stale_after_seconds':STALE_AFTER_SECONDS,'last_success_at':previous.get('generated_at'),
              'snapshot_revision':previous.get('revision'),'bank_id':bank_id,
              'semantic_status':previous.get('semantic_status','pending_or_stale')}
        _write_state(state_path,{**base,'status':'refreshing'})
        try:
            manifests=json.loads(MANIFESTS.read_text(encoding='utf-8'))
            generator_digest=sha256(Path(__file__).read_bytes()+(HOST_ADAPTER/'topic_catalog.py').read_bytes()+Path(__file__).with_name('bank_hierarchy.py').read_bytes()).hexdigest()
            semantic_path=output.with_name('semantic-topics.json')
            semantic=json.loads(semantic_path.read_text()) if semantic_path.is_file() else {}
            hierarchy_path=output.with_name('corpus-navigation.json')
            hierarchy=json.loads(hierarchy_path.read_text()) if hierarchy_path.is_file() else {}
            if hierarchy and hierarchy.get('bank_id')!=bank_id:raise ValueError('hierarchy_bank_mismatch')
            semantic_digest=sha256(json.dumps(semantic,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
            config_revision=sha256(json.dumps([GENERATOR_VERSION,generator_digest,semantic_digest,hierarchy.get('generated_at'),limit,manifests],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
            with psycopg2.connect(database_url,connect_timeout=3,options='-c statement_timeout=25000') as db:
                db.set_session(readonly=True,isolation_level='REPEATABLE READ')
                with db.cursor(cursor_factory=RealDictCursor) as cursor:
                    source=source_snapshot(cursor,bank_id)
                    last_generated=dt.datetime.fromisoformat(previous['generated_at']).timestamp() if previous.get('generated_at') else 0
                    changed=previous.get('source_revision')!=source['revision'] or previous.get('config_revision')!=config_revision
                    if not force and not changed and time.time()-last_generated<RECONCILE_SECONDS:
                        ready={**base,'status':'ready','checked_at':dt.datetime.now(dt.timezone.utc).isoformat(),
                               'last_success_at':previous['generated_at'],'source_revision':source['revision'],'rebuilt':False}
                        _write_state(state_path,ready);return ready
                    entities,total,entity_index=read_entity_projection(cursor,bank_id,limit)
                    live_records=[]
                    if hierarchy:
                        cursor.execute('''SELECT id::text,document_id,fact_type,created_at::text,updated_at::text,left(text,180) AS preview,
                            md5(text||coalesce(updated_at::text,'')) AS revision FROM memory_units WHERE bank_id=%s''',(bank_id,))
                        live_records=[dict(row) for row in cursor.fetchall()]
                        from source_safety import mask_text
                        for row in live_records:row['preview']=mask_text(row['preview'])
            entity_topics=[build_topic(row) for row in entities]
            entity_topics=merge_semantic_snapshot(entity_topics,semantic,source['revision'])
            roots=merge_semantic_snapshot(build_manifest_topics(entity_topics,manifests),semantic,source['revision'],include_dynamic=False)
            hierarchy_topics=[];hierarchy_coverage={};hierarchy_members=None
            if hierarchy:
                from bank_hierarchy import materialize_topics
                known={row[0]:row for row in hierarchy.get('members',[])}
                assignments={row['id']:known[row['id']][2] for row in live_records
                             if row['id'] in known and row['revision']==known[row['id']][3]}
                hierarchy_topics,hierarchy_coverage=materialize_topics(live_records,assignments,hierarchy.get('leaves',{}),hierarchy.get('roots',[]))
                leaf_ids=set(hierarchy.get('leaves',{}))
                hierarchy_members=[[r['id'],r['document_id'],assignments[r['id']] if assignments.get(r['id']) in leaf_ids else 'domain:pending',r['revision']] for r in live_records]
                hierarchy_coverage['generated_at']=hierarchy.get('generated_at')
                hierarchy_coverage['summary_sample_count']=hierarchy.get('summary_sample_count')
            mapped={identity for root in roots for identity in root['children']}
            outside=sorted((row for row in entity_topics if row['topic_id'] not in mapped),key=lambda row:(row.get('latest_source_at') or '',row['topic_id']),reverse=True)
            semantic_ready=semantic.get('quality_gate')=='passed' and semantic.get('source_revision')==source['revision']
            semantic_recent=False
            try:semantic_recent=(time.time()-dt.datetime.fromisoformat(semantic['generated_at']).timestamp())<1200
            except (KeyError,ValueError,TypeError):pass
            semantic_status='ready' if semantic_ready else 'fresh_with_pending_changes' if semantic.get('quality_gate')=='passed' and semantic_recent else 'pending_or_stale'
            if hierarchy:
                semantic_status='ready' if hierarchy_coverage.get('unassigned_memory_count')==0 and not hierarchy_coverage.get('pending_summary_leaf_count') else 'fresh_with_pending_changes'
            metadata={**source,'source_revision':source['revision'],'config_revision':config_revision,
                      'revision':sha256((source['revision']+config_revision).encode()).hexdigest(),
                      'generated_at':started,'eligible_entities':total,'indexed_entities':len(entities),
                      'selection':'popular_plus_recent_source_changes','recent_reserved':max(1,limit//5),
                      'semantic_status':semantic_status,
                      'semantic_generated_at':hierarchy.get('generated_at') if hierarchy else semantic.get('generated_at'),
                      'hierarchy_coverage':hierarchy_coverage,
                      'outside_manifest_count':len(outside),'recent_outside_manifests':[
                          {key:row.get(key) for key in ('topic_id','title','latest_source_at')} for row in outside[:6]],
                      'refresh_interval_seconds':REFRESH_SECONDS,'stale_after_seconds':STALE_AFTER_SECONDS}
            catalog.replace_topics([*hierarchy_topics,*roots,*entity_topics],metadata,entity_index,members=hierarchy_members)
            if markdown:catalog.project_markdown(Path(markdown))
            ready={**base,'status':'ready','checked_at':dt.datetime.now(dt.timezone.utc).isoformat(),
                   'last_success_at':started,'snapshot_revision':metadata['revision'],'source_revision':source['revision'],
                   'semantic_status':metadata['semantic_status'],'rebuilt':True}
            _write_state(state_path,ready);return ready
        except Exception as error:
            _write_state(state_path,{**base,'status':'failed','error_type':type(error).__name__})
            raise


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--database-url',default=os.getenv('EVOLVING_PROFILE_API_DATABASE_URL'))
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--markdown',type=Path);parser.add_argument('--limit',type=int,default=500)
    parser.add_argument('--bank-id');parser.add_argument('--force',action='store_true')
    args=parser.parse_args();
    if not args.database_url:raise SystemExit('database URL required')
    bank_id=args.bank_id or json.loads((Path.home()/'.evolving-profile/guidance-v1/guidance-v1.json').read_text())['bank_id']
    try:print(refresh_catalog(args.database_url,args.output,bank_id,limit=args.limit,markdown=args.markdown,force=args.force))
    except Exception as error:raise SystemExit('catalog_refresh_failed:'+type(error).__name__)


if __name__=='__main__':main()
