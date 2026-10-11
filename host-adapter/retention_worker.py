#!/usr/bin/env python3
"""Bounded, restart-safe dispatcher for the existing local token queue.

No new extraction policy: uses the configured strategy, origin exclusions,
batch size and tail age. One operation at a time. Completion (not acceptance)
is the only condition that removes raw queue records.
"""
import argparse
import fcntl
import hashlib
import json
import os
import time
import urllib.parse
import uuid
from datetime import datetime, timezone
from pathlib import Path

from lib.client import HindsightClient
from lib.config import load_config
from lib.retention_policy import allowed_retention_batches, item_write_exclusion, retention_exclusion, submission_metadata, batch_content
from lib.retention_queue import RetentionQueue


def atomic(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix('.tmp');temp.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
    os.chmod(temp,0o600);os.replace(temp,path)


def run_once(config,root):
    q=RetentionQueue(config.get('retainQueuePath') or root/'codex/state/retention-queue.json',threshold_tokens=config.get('retainTokenThreshold',12000))
    state_path=root/'retention/worker-state.json'
    previous=json.loads(state_path.read_text()) if state_path.exists() else {}
    base={k:previous.get(k,0) for k in ('submitted_total','completed_total')}
    base['retry_counts']=previous.get('retry_counts',{})
    def finish(status,**extra):
        rows=q._read()['items']
        report={**base,'status':status,'at':datetime.now(timezone.utc).isoformat(),'queue_items':len(rows),
                'inflight_items':sum(bool(r.get('operation_id')) for r in rows),
                'held_items':sum(bool(r.get('retention_origin_hold')) for r in rows),**extra}
        atomic(state_path,report);return report
    if not config.get('autoRetain',True): return finish('disabled')
    if previous.get('next_retry_at_epoch',0)>time.time(): return previous
    client=HindsightClient(config['evolvingProfileApiUrl'],config.get('evolvingProfileApiToken'))
    statuses={}
    rows=q._read()['items']
    for bank,op in sorted({(r['bank_id'],r['operation_id']) for r in rows if r.get('operation_id')}):
        try: statuses[op]=client.operation_status(bank,op,timeout=10).get('status')
        except Exception as e: return finish('status_unavailable',error=type(e).__name__,next_retry_at_epoch=time.time()+30)
        if statuses[op] in ('failed','cancelled'):
            operation_rows=[r for r in rows if r.get('operation_id')==op]
            if any(item_write_exclusion(r) or retention_exclusion(r.get('project'),[r.get('session_id')],config) for r in operation_rows):
                return finish('retry_policy_held',operation_id=op,bank_id=bank,
                              reason='knowledge_write_policy_requires_scoped_recovery')
            attempts=base['retry_counts'].get(op,0)
            if attempts>=config.get('retainWorkerMaxRetries',3): return finish('retry_limit_reached',operation_id=op,bank_id=bank)
            try:
                client._request('POST',f"/v1/default/banks/{urllib.parse.quote(bank,safe='')}/operations/{op}/retry",timeout=10)
                base['retry_counts'][op]=attempts+1
                return finish('retry_requested',operation_id=op,bank_id=bank,next_retry_at_epoch=time.time()+60)
            except Exception as e:return finish('retry_unavailable',operation_id=op,error=type(e).__name__,next_retry_at_epoch=time.time()+60)
    completed=sum(s=='completed' for s in statuses.values())
    if completed:
        q.reconcile(statuses);base['completed_total']+=completed
    if q.pending_operations(): return finish('processing',operations=q.pending_operations())
    # Recover a lost acknowledgement before selecting a different batch. The
    # caller-supplied UUID makes a resend idempotent on the actual API.
    intent=previous.get('intent')
    if intent:
        try:
            remote=client.operation_status(intent['bank_id'],intent['operation_id'],timeout=10)
        except RuntimeError as e:
            if 'HTTP 404 ' not in str(e): return finish('status_unavailable',intent=intent,error=type(e).__name__,next_retry_at_epoch=time.time()+30)
        except Exception as e: return finish('status_unavailable',intent=intent,error=type(e).__name__,next_retry_at_epoch=time.time()+30)
        else:
            if not q.mark_submitted(intent['batch_id'],intent['operation_id'],item_ids=intent['item_ids']): return finish('acknowledgement_conflict',intent=intent)
            base['submitted_total']+=1
            return finish('recovered_acknowledgement',operation_id=intent['operation_id'])
    batches=allowed_retention_batches(q.ready_batches(config=config),config)
    tails=allowed_retention_batches(q.ready_batches(force_tail=True,min_age_seconds=config.get('retainTailMinAgeSeconds',1800),config=config),config)
    priority=allowed_retention_batches(q.ready_batches(force_tail=True,min_age_seconds=config.get('shortTaskTailMinAgeSeconds',600),config=config),config)
    tails.extend(b for b in priority if b.get('priority_tail'))
    seen={b['batch_id'] for b in batches}
    for b in tails:
        if b['batch_id'] not in seen: batches.append(b);seen.add(b['batch_id'])
    batches.sort(key=lambda b:b['oldest_at'])
    if intent:
        # Keep exactly the submitted item snapshot after concurrent captures.
        chosen=[r for r in q._read()['items'] if r['id'] in intent['item_ids']]
        if len(chosen)!=len(intent['item_ids']): return finish('intent_conflict',intent=intent)
        chosen.sort(key=lambda r:intent['item_ids'].index(r['id']))
        if any(item_write_exclusion(r) or retention_exclusion(r.get('project'),[r.get('session_id')],config) for r in chosen):
            return finish('intent_policy_held',intent=intent,reason='knowledge_write_prohibited')
        batch={**intent,'content':batch_content(chosen),
               'project':chosen[0]['project'],'session_ids':sorted({r['session_id'] for r in chosen}),'estimated_tokens':sum(r.get('estimated_tokens',0) for r in chosen)}
    elif batches: batch=batches[0]
    else: return finish('idle')
    op=intent['operation_id'] if intent else str(uuid.uuid5(uuid.NAMESPACE_URL,f"ep-retain:{batch['bank_id']}:{batch['batch_id']}"))
    intent={'batch_id':batch['batch_id'],'bank_id':batch['bank_id'],'operation_id':op,'item_ids':batch['item_ids']}
    atomic(state_path,{**base,'status':'submitting','intent':intent,'at':datetime.now(timezone.utc).isoformat()})
    project_key=hashlib.sha256(batch['project'].encode()).hexdigest()[:12]
    vars={'project_key':project_key,'bank_id':batch['bank_id'],'session_id':','.join(batch['session_ids']),
          'timestamp':datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}
    def expand(s):
        for k,v in vars.items():s=str(s).replace('{'+k+'}',v)
        return s
    item={'content':batch['content'],'document_id':'codex-batch-'+batch['batch_id'],
          'context':config.get('retainContext','codex'),
          'metadata':{'source':'codex-hook-token-batch','project':batch['project'],'session_ids':vars['session_id'],'retained_at':vars['timestamp'],'estimated_tokens':str(batch.get('estimated_tokens',0)),
                      **{k:expand(v) for k,v in config.get('retainMetadata',{}).items()}, **submission_metadata(batch)},
          'tags':[expand(t) for t in config.get('retainTags',[])],
          'observation_scopes':config.get('retainObservationScopes','shared')}
    if config.get('retainStrategy'):item['strategy']=config['retainStrategy']
    try:
        response=client._request('POST',f"/v1/default/banks/{urllib.parse.quote(batch['bank_id'],safe='')}/memories",{'async':True,'operation_id':op,'items':[item]},timeout=config.get('retainSubmitTimeout',30))
        accepted=response.get('operation_id')
        if not response.get('success') or not accepted: raise RuntimeError('async_acceptance_missing')
        if not q.mark_submitted(batch['batch_id'],accepted,item_ids=batch['item_ids']): return finish('acknowledgement_conflict',intent=intent)
        base['submitted_total']+=1
        return finish('submitted',operation_id=accepted,document_id=item['document_id'],item_count=len(batch['item_ids']),eligible_batches=len(batches))
    except Exception as e:
        return finish('retry_pending',intent=intent,error=type(e).__name__,next_retry_at_epoch=time.time()+30)


def main():
    p=argparse.ArgumentParser();p.add_argument('--once',action='store_true');p.add_argument('--config');p.add_argument('--state-root',default=os.path.expanduser('~/.evolving-profile'));a=p.parse_args()
    root=Path(a.state_root);c=load_config()
    if a.config:c.update(json.loads(Path(a.config).read_text()))
    lock_path=Path(c.get('retainQueuePath') or root/'codex/state/retention-queue.json').with_suffix('.dispatch.lock');lock_path.parent.mkdir(parents=True,exist_ok=True)
    with lock_path.open('a+') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:print(json.dumps({'status':'already_running'}));return
        try:report=run_once(c,root)
        except Exception as e:
            report={'status':'worker_error','error':type(e).__name__,'at':datetime.now(timezone.utc).isoformat()};atomic(root/'retention/worker-state.json',report)
        print(json.dumps(report,ensure_ascii=False),flush=True)

if __name__=='__main__':main()
