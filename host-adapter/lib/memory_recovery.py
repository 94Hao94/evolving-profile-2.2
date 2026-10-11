"""Durable, Bank-scoped manual recovery coordinator.

Private plans/intents retain exact source fingerprints. Public projections have
an explicit allowlist. Processors own evidence gates; transport acceptance never
counts as completion. The automatic queue workers keep their original contracts.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
import uuid
import time

from .client import HindsightClient
from .retention_queue import RetentionQueue

REGISTRY_PATH = Path(__file__).resolve().parents[2] / 'config/memory-recovery.json'
TERMINAL = {'complete', 'partial', 'failed', 'cancelled', 'review_pending'}
DIMENSION_TERMINAL = {'complete','disabled','no_eligible_evidence','unsupported_scope','review_pending'}


class RecoveryError(ValueError):
    """Stable, public-safe protocol error; never contains provider response text."""


def now():
    return datetime.now(timezone.utc).isoformat()


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',',':')).encode()).hexdigest()


def atomic(path, value):
    path=Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    fd, name=tempfile.mkstemp(dir=path.parent,prefix=path.name+'.',suffix='.tmp')
    try:
        os.fchmod(fd,0o600)
        with os.fdopen(fd,'w',encoding='utf-8') as stream:
            json.dump(value,stream,ensure_ascii=False,indent=2); stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name): os.unlink(name)


def read_json(path, default=None):
    try: return json.loads(Path(path).read_text())
    except FileNotFoundError: return default


@contextmanager
def lock(path, *, blocking=True):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a+') as stream:
        os.chmod(path,0o600)
        fcntl.flock(stream,fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        try: yield
        finally: fcntl.flock(stream,fcntl.LOCK_UN)


def identity(value, field):
    try: return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError): raise RecoveryError('invalid_'+field) from None


def time_value(value):
    try:
        result=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)
    except (ValueError,TypeError): raise RecoveryError('invalid_range') from None


def in_scope(row, scope, *, timestamp='captured_at'):
    sessions=scope.get('session_ids') or []
    sid=(row.get('metadata') or {}).get('original_session_id') or row.get('session_id') or (row.get('primary_context') or {}).get('session_id')
    if sessions and sid not in sessions: return False
    if scope.get('from') or scope.get('to'):
        value=row.get(timestamp) or row.get('created_at') or row.get('updated_at')
        if not value: return False
        try: at=time_value(value)
        except RecoveryError: return False
        if scope.get('from') and at<time_value(scope['from']): return False
        if scope.get('to') and at>time_value(scope['to']): return False
    return True


def range_scope_unknown(row,scope,*,timestamp='captured_at'):
    if not scope.get('from') and not scope.get('to'): return False
    without_dates={**scope,'from':None,'to':None}
    if not in_scope(row,without_dates,timestamp=timestamp): return False
    value=row.get(timestamp) or row.get('created_at') or row.get('updated_at')
    if not value: return True
    try: time_value(value)
    except RecoveryError: return True
    return False


class RecoveryEngine:
    def __init__(self,state_root=None,*,config=None,settings=None,client=None):
        self.state_root=Path(state_root or os.environ.get('EVOLVING_PROFILE_STATE_ROOT') or Path.home()/'.evolving-profile').expanduser().resolve()
        self.root=self.state_root/'memory-recovery'
        self.registry=read_json(REGISTRY_PATH)
        if config is None:
            from .config import DEFAULTS, load_config
            # Alternate roots isolate source/config as well as job files.
            config=load_config() if self.state_root==Path.home()/'.evolving-profile' else {**DEFAULTS,**(read_json(self.state_root/'codex.json',{}) or {})}
        self.config=dict(config)
        if settings is None:
            # Runtime settings contain trusted credentials; never persist them.
            import sys
            sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'guidance'))
            from runtime_settings import load_runtime_settings
            settings=load_runtime_settings(self.state_root/'config/runtime-settings.json')
        self.settings=settings
        self.client=client or HindsightClient(self.config.get('evolvingProfileApiUrl') or 'http://127.0.0.1:9077',self.config.get('evolvingProfileApiToken'))
        self.queue=RetentionQueue(self.config.get('retainQueuePath') or self.state_root/'codex/state/retention-queue.json',self.config.get('retainTokenThreshold',12000))

    def enabled(self, dimension):
        module=next(d['module'] for d in self.registry['dimensions'] if d['id']==dimension)
        modules=self.settings.get('modules') or {}
        if not (self.settings.get('routing') or {}).get('ep_enabled',True): return False
        if dimension.startswith('agent_') and not (modules.get('agent_process_memory') or {}).get('record',True): return False
        return (modules.get(module) or {}).get('record',True) is not False

    def scoped_items(self,scope):
        from .retention_policy import retention_exclusion
        rows=self.queue._read()['items']
        return [r for r in rows if r.get('bank_id')==scope['bank_id'] and in_scope(r,scope)
                and (scope.get('queue_item_ids') is None or r['id'] in scope['queue_item_ids'])
                and not r.get('retention_origin_hold') and (r.get('metadata') or {}).get('prompt_origin')!='test_probe'
                and r.get('session_id') not in self.config.get('diagnosticSessionIds',[])
                and not retention_exclusion(r.get('project'),[r.get('session_id')],self.config)]

    def unknown_scope_items(self,scope):
        return [r for r in self.queue._read()['items'] if r.get('bank_id')==scope['bank_id'] and range_scope_unknown(r,scope)]

    def preview(self,args):
        from .memory_recovery_processors import ProcessorAdapter
        mode=args.get('mode','pending')
        if mode not in {'pending','all','date_range'}: raise RecoveryError('invalid_mode')
        dimensions=args.get('dimensions') or [d['id'] for d in self.registry['dimensions']]
        known={d['id'] for d in self.registry['dimensions']}
        if not isinstance(dimensions,list) or any(not isinstance(d,str) or d not in known for d in dimensions): raise RecoveryError('invalid_dimensions')
        sessions=args.get('session_ids') or []
        if not isinstance(sessions,list) or any(not isinstance(s,str) or not s or len(s)>128 for s in sessions): raise RecoveryError('invalid_session_ids')
        lower,upper=args.get('from'),args.get('to')
        if mode=='date_range' and not (lower or upper): raise RecoveryError('invalid_range')
        for value in (lower,upper):
            if value is not None: time_value(value)
        if lower and upper and time_value(lower)>time_value(upper): raise RecoveryError('invalid_range')
        scope={'bank_id':args['bank_id'],'mode':mode,'dimensions':list(dict.fromkeys(dimensions)),
               'from':lower,'to':upper,'session_ids':list(dict.fromkeys(sessions))}
        adapter=ProcessorAdapter(self)
        snapshot=adapter.inventory(scope)
        dimension_rows=[]
        for dim in scope['dimensions']:
            row=snapshot['dimensions'].get(dim,{'status':'source_unavailable','pending_count':None,'source_count':None})
            dimension_rows.append({'id':dim,**row} if self.enabled(dim) else {'id':dim,'status':'disabled','pending_count':0,'source_count':row.get('source_count')})
        revision=fingerprint(snapshot['revision'])
        plan_id=str(uuid.uuid4())
        public={'plan_id':plan_id,'bank_id':scope['bank_id'],'source_revision':revision,'mode':mode,'dimensions':dimension_rows,
                'estimated_tokens':snapshot.get('estimated_tokens'),'requires_provider':any(self.enabled(d) and d in {'facts','experiences','entities','observations','preferences','mental_models','session_summaries','project_summaries'} for d in dimensions)}
        atomic(self.root/'plans'/f'{plan_id}.json',{'preview':public,'scope':scope,'created_at':now(),'snapshot':snapshot['revision']})
        return {'preview':public}

    def public(self,job):
        if job is None: return None
        fields=('job_id','bank_id','status','created_at','updated_at','completed_at','can_resume','cancel_requested','error_code')
        result={key:job[key] for key in fields if key in job}
        result['stages']=[{k:v for k,v in s.items() if k in {'id','status','processed','total','failed','error_code','detail'}} for s in job['stages']]
        result['dimensions']=[{k:v for k,v in d.items() if k in {'id','status','pending_count','source_count','processed','failed','error_code','detail'}} for d in job.get('dimensions',[])]
        return result

    def reconcile_runner(self,job):
        if not job or job['status'] not in {'queued','running'}: return job
        # Detached launcher and between-pass polling get a grace period. A
        # model call may run longer, but the held runner lock proves liveness.
        age=time.time()-time_value(job['updated_at']).timestamp()
        if age<30: return job
        try:
            with lock(self.job_path(job['job_id']).with_suffix('.runner.lock'),blocking=False):
                with lock(self.root/'registry.lock'):
                    current=read_json(self.job_path(job['job_id']))
                    if not current or current['status'] not in {'queued','running'}: return current
                    if time.time()-time_value(current['updated_at']).timestamp()<30: return current
                    current.update(status='failed',can_resume=True,error_code='recovery_worker_interrupted',updated_at=now())
                    atomic(self.job_path(current['job_id']),current)
                    return current
        except BlockingIOError: return job

    def job_path(self,jid): return self.root/'jobs'/f'{identity(jid,"job_id")}.json'

    def save(self,job):
        # Merge cancellation that can arrive while a processor holds runner lock.
        with lock(self.root/'registry.lock'):
            previous=read_json(self.job_path(job['job_id']),{})
            if previous.get('cancel_requested'): job['cancel_requested']=True
            job['updated_at']=now(); atomic(self.job_path(job['job_id']),job)

    def load_job(self,bank,jid=None):
        if jid:
            job=read_json(self.job_path(jid))
            if not job or job['bank_id']!=bank: raise RecoveryError('job_not_found')
            return job
        jobs=[read_json(p) for p in (self.root/'jobs').glob('*.json')]
        jobs=[j for j in jobs if j and j['bank_id']==bank]
        return max(jobs,key=lambda j:j['created_at']) if jobs else None

    def dispatch(self,args):
        if not isinstance(args,dict): raise RecoveryError('invalid_request')
        bank=args.get('bank_id')
        if not isinstance(bank,str) or not bank.strip() or len(bank)>256 or any(ord(c)<32 for c in bank): raise RecoveryError('invalid_bank_id')
        action=args.get('action')
        if action=='preview': return self.preview(args)
        if action=='status': return {'job':self.public(self.reconcile_runner(self.load_job(bank,args.get('job_id'))))}
        if action=='start':
            key=identity(args.get('idempotency_key'),'idempotency_key'); plan_id=identity(args.get('plan_id'),'plan_id')
            with lock(self.root/'registry.lock'):
                index=read_json(self.root/'idempotency.json',{})
                ix=fingerprint([bank,key])
                if ix in index:
                    if index[ix]['plan_id']!=plan_id: raise RecoveryError('idempotency_conflict')
                    return {'job':self.public(self.load_job(bank,index[ix]['job_id']))}
                plan=read_json(self.root/'plans'/f'{plan_id}.json')
                if not plan or plan['preview']['bank_id']!=bank: raise RecoveryError('plan_not_found')
                from .memory_recovery_processors import ProcessorAdapter
                adapter=ProcessorAdapter(self); current_snapshot=adapter.inventory(plan['scope'])['revision']
                if not adapter.compatible_snapshot(plan['snapshot'],current_snapshot): raise RecoveryError('plan_source_changed')
                scope={**plan['scope'],'queue_item_ids':[r[0] for r in plan['snapshot'].get('queue',[])],
                       'process_record_ids':[r[0] for r in plan['snapshot'].get('process',[])],
                       'source_watermarks':{r[0]:r[1] or [] for r in plan['snapshot'].get('sources',[])}}
                dimensions=plan['preview']['dimensions']; wanted={d['id'] for d in dimensions}
                stages=[{'id':s['id'],'status':'queued','processed':0,'total':None,'failed':0,
                         **({'detail':'as_of_preview_source_snapshot; new arrivals deferred'} if s['id'] in {'raw_capture','retain','scenarios','agent_process'} else {})}
                        for s in self.registry['stages'] if wanted.intersection(s['dimensions'])]
                job={'job_id':str(uuid.uuid4()),'bank_id':bank,'status':'queued','stages':stages,'dimensions':dimensions,
                     'created_at':now(),'updated_at':now(),'can_resume':False,'cancel_requested':False,'scope':scope,
                     'source_revision':plan['preview']['source_revision'],'work':{},'plan_id':plan_id}
                atomic(self.job_path(job['job_id']),job); index[ix]={'job_id':job['job_id'],'plan_id':plan_id}; atomic(self.root/'idempotency.json',index)
                return {'job':self.public(job)}
        if action not in {'resume','cancel'}: raise RecoveryError('invalid_action')
        with lock(self.root/'registry.lock'):
            job=self.load_job(bank,args.get('job_id'))
            if job is None: raise RecoveryError('job_not_found')
            if action=='cancel':
                if job['status']=='complete': return {'job':self.public(job)}
                job['cancel_requested']=True
                if job['status']!='running': job['status']='cancelled'; job['can_resume']=True
                else:
                    try:
                        with lock(self.job_path(job['job_id']).with_suffix('.runner.lock'),blocking=False):
                            job['status']='cancelled'; job['can_resume']=True
                    except BlockingIOError: pass
            else:
                if job['status']=='complete': return {'job':self.public(job)}
                job.update(status='queued',cancel_requested=False,can_resume=False); job.pop('error_code',None); job.pop('completed_at',None)
                for stage in job['stages']:
                    if stage['status'] not in {'complete','disabled','no_eligible_evidence'}: stage['status']='queued'
                # Explicit resume after credentials/provider repair grants a
                # fresh retry attempt. Identities/intents remain unchanged.
                retain_work=job.get('work',{}).get('retain',{})
                for entry in list((retain_work.get('batches') or {}).values())+list((retain_work.get('remote_operations') or {}).values()):
                    if not entry.get('completed'): entry.pop('retried',None)
                (job.get('work',{}).get('consolidation') or {}).pop('retried',None)
            job['updated_at']=now(); atomic(self.job_path(job['job_id']),job)
            return {'job':self.public(job)}

    def set_dimension(self,job,dimension,**values):
        row=next((d for d in job['dimensions'] if d['id']==dimension),None)
        if row is not None: row.update(values)

    def run_job(self,jid):
        from .memory_recovery_processors import ProcessorAdapter, ProcessorBlocked
        path=self.job_path(jid)
        try:
            with lock(path.with_suffix('.runner.lock'),blocking=False):
                job=read_json(path)
                if not job: raise RecoveryError('job_not_found')
                if job['status']=='complete': return {'job':self.public(job)}
                adapter=ProcessorAdapter(self); job['status']='running'; self.save(job)
                for stage in job['stages']:
                    current=read_json(path)
                    if current.get('cancel_requested'):
                        job.update(status='cancelled',cancel_requested=True,can_resume=True); self.save(job); return {'job':self.public(job)}
                    if stage['status'] in {'complete','disabled','no_eligible_evidence'}: continue
                    ids=[d['id'] for d in self.registry['dimensions'] if d['stage_id']==stage['id'] and d['id'] in job['scope']['dimensions']]
                    enabled=[d for d in ids if self.enabled(d)]
                    for dim in ids:
                        if dim not in enabled: self.set_dimension(job,dim,status='disabled',processed=0,failed=0)
                    if not enabled: stage.update(status='disabled',total=0); self.save(job); continue
                    stage.update(status='running'); self.save(job)
                    try:
                        report=adapter.run(stage['id'],job,enabled)
                        stage.update(report)
                    except ProcessorBlocked as error:
                        stage.update(status=error.status,error_code=error.code,failed=stage.get('failed',0)+(1 if error.status=='failed' else 0))
                        for dim in enabled: self.set_dimension(job,dim,status=error.status,error_code=error.code)
                    except Exception:
                        stage.update(status='failed',error_code='processor_failed',failed=stage.get('failed',0)+1)
                        for dim in enabled: self.set_dimension(job,dim,status='failed',error_code='processor_failed')
                    self.save(job)
                    if job.get('cancel_requested') or stage['status']=='cancelled':
                        job.update(status='cancelled',can_resume=True,cancel_requested=True); self.save(job)
                        return {'job':self.public(job)}
                    if stage['status'] in {'running','waiting_provider','failed','queued'}:
                        job.update(status=stage['status'],can_resume=True)
                        if stage.get('error_code'): job['error_code']=stage['error_code']
                        self.save(job); return {'job':self.public(job)}
                statuses=[s['status'] for s in job['stages']]
                final='review_pending' if 'review_pending' in statuses else ('partial' if any(s in {'partial','unsupported_scope','source_unavailable'} for s in statuses) else 'complete')
                job.update(status=final,can_resume=final!='complete',completed_at=now()); self.save(job)
                return {'job':self.public(job)}
        except BlockingIOError:
            job=read_json(path)
            return {'job':self.public(job)}
