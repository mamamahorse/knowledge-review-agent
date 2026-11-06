"""Objective learning-run telemetry; independent of successful result publication."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime,timezone
from functools import wraps
import json
from time import perf_counter
from uuid import uuid4
import httpx

_current=ContextVar('learning_run_metrics',default=None)
_policy=ContextVar('learning_http_policy',default=None)


@contextmanager
def http_policy(policy):
    """Explicit run-local cost/call guard; never changes personal configuration."""
    token=_policy.set(policy)
    try: yield
    finally: _policy.reset(token)


def initialize(db):
    db.executescript('''CREATE TABLE IF NOT EXISTS learning_metrics_runs(
        id TEXT PRIMARY KEY,job_id TEXT,root_id TEXT,is_parent INTEGER,started_utc TEXT,
        elapsed_ms REAL,status TEXT,first_candidate_ms REAL,error_type TEXT);
        CREATE TABLE IF NOT EXISTS learning_metrics_http(id TEXT PRIMARY KEY,run_id TEXT,job_id TEXT,root_id TEXT,payload TEXT);
        CREATE INDEX IF NOT EXISTS metrics_root ON learning_metrics_http(root_id);''')
    db.execute('CREATE TABLE IF NOT EXISTS learning_metrics_events(id TEXT PRIMARY KEY,run_id TEXT,job_id TEXT,root_id TEXT,payload TEXT)')


def number(value): return value if type(value) is int and value>=0 else None


def usage(data):
    value=data.get('usage') or {}
    if not isinstance(value,dict): value={}
    details=value.get('prompt_tokens_details') or {}
    completion=value.get('completion_tokens_details') or {}
    if not isinstance(details,dict): details={}
    if not isinstance(completion,dict): completion={}
    return dict(input_tokens=number(value.get('prompt_tokens',value.get('input_tokens'))),
        output_tokens=number(value.get('completion_tokens',value.get('output_tokens'))),
        cached_tokens=number(value.get('prompt_cache_hit_tokens',details.get('cached_tokens'))),
        reasoning_tokens=number(completion.get('reasoning_tokens')))


class Meter:
    def __init__(self,store,identity):
        self.store=store; self.job_id=identity; state=store.get(identity)
        self.root_id=state['inputs'].get('parent_id') or identity
        self.id=uuid4().hex; self.start=perf_counter(); self.pending={}; self.tool=None
        with store.practice.transaction() as db:
            db.execute('INSERT INTO learning_metrics_runs VALUES (?,?,?,?,?,NULL,?,NULL,NULL)',
                (self.id,identity,self.root_id,int(bool(state.get('batches'))),datetime.now(timezone.utc).isoformat(),'running'))

    def write(self,record):
        with self.store.practice.transaction() as db:
            db.execute('INSERT INTO learning_metrics_http VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload',
                       (record['id'],self.id,self.job_id,self.root_id,json.dumps(record,ensure_ascii=False)))

    def request(self,request):
        state=self.store.get(self.job_id)
        try: payload=json.loads(request.content)
        except Exception: payload={}
        protocol=(payload.get('response_format') or {}).get('type')
        if not protocol and payload.get('tools'): protocol='forced_tool' if payload.get('tool_choice') not in (None,'auto') else 'tool'
        policy=_policy.get()
        if policy is not None: policy.before(request,payload,protocol)
        record=dict(id=uuid4().hex,stage=state.get('stage'),tool=self.tool,
            endpoint=str(request.url.copy_with(username=None,password=None,query=None,fragment=None)),
            requested_model=payload.get('model'),output_budget=payload.get('max_tokens',payload.get('max_completion_tokens')),
            protocol=protocol or 'text',thinking=payload.get('thinking'),status='pending',
            tool_choice=payload.get('tool_choice'),declared_tools=[t.get('function',{}).get('name') for t in payload.get('tools',[])],
            http_status=None,model=None,elapsed_ms=None,error_type=None,
            input_tokens=None,output_tokens=None,cached_tokens=None,reasoning_tokens=None,cost_estimate=None)
        self.pending[id(request)]=(perf_counter(),record); self.write(record)

    def response(self,response):
        try: response.read()
        except BaseException as exc:
            self.finish(response.request,response.status_code,error=type(exc).__name__)
            raise
        try: data=response.json()
        except Exception: data={}
        if not isinstance(data,dict): data={}
        self.finish(response.request,response.status_code,data)

    def finish(self,request,status=None,data=None,error=None):
        entry=self.pending.pop(id(request),None)
        if entry is None: return
        start,record=entry
        record.update(status=('response_body_error' if error else 'response') if status is not None else 'transport_error',http_status=status,
            elapsed_ms=(perf_counter()-start)*1000,error_type=error,**usage(data or {}),model=(data or {}).get('model'))
        policy=_policy.get()
        if policy is not None: policy.after(record)
        self.write(record)

    def audit(self,value):
        if _current.get() is not self or value['event']!='attempt_failed': return
        identity=f'{value["trace_id"]}:{value["attempt"]}:failed'
        record={k:value.get(k) for k in ('trace_id','attempt','event','error_type','phase','retry','exhausted')}
        with self.store.practice.transaction() as db:
            db.execute('INSERT OR IGNORE INTO learning_metrics_events VALUES (?,?,?,?,?)',
                       (identity,self.id,self.job_id,self.root_id,json.dumps(record)))

    def observe_state(self,state,db):
        if state['id']==self.job_id and state.get('formal',{}).get('goals'):
            db.execute('UPDATE learning_metrics_runs SET first_candidate_ms=COALESCE(first_candidate_ms,?) WHERE id=?',
                       ((perf_counter()-self.start)*1000,self.id))


class MeteredClient(httpx.Client):
    def __init__(self,meter,**kwargs):
        self.meter=meter
        super().__init__(event_hooks=dict(request=[meter.request],response=[meter.response]),**kwargs)

    def send(self,request,**kwargs):
        try: return super().send(request,**kwargs)
        except BaseException as exc:
            # Redirect hops are individually recorded by hooks. A transport
            # failure has no response/usage; do not invent zero consumption.
            for _,record in list(self.meter.pending.items()):
                start,value=record
                value.update(status='transport_error',elapsed_ms=(perf_counter()-start)*1000,error_type=type(exc).__name__)
                self.meter.write(value)
            self.meter.pending.clear()
            raise


def http_options(token):
    meter=_current.get()
    if meter is None: return None
    kwargs=dict(timeout=30.,follow_redirects=True)
    if token is not None:
        from agent_cancel import CancellableTransport
        kwargs['transport']=CancellableTransport(token)
    return dict(http_client=MeteredClient(meter,**kwargs),max_retries=0)


@contextmanager
def tool_scope(name):
    meter=_current.get(); previous=meter.tool if meter else None
    if meter: meter.tool=name
    try: yield
    finally:
        if meter: meter.tool=previous


def observe_state(state,db):
    meter=_current.get()
    if meter: meter.observe_state(state,db)


def measured(action):
    @wraps(action)
    def run(store,identity,*args,**kwargs):
        meter=Meter(store,identity); token=_current.set(meter); error=None
        try:
            from structured_recovery import audit_observer
            with audit_observer(meter.audit): return action(store,identity,*args,**kwargs)
        except BaseException as exc:
            error=type(exc).__name__; raise
        finally:
            _current.reset(token)
            state=store.get(identity)
            with store.practice.transaction() as db:
                db.execute('UPDATE learning_metrics_runs SET elapsed_ms=?,status=?,error_type=? WHERE id=?',
                           ((perf_counter()-meter.start)*1000,state['status'],error,meter.id))
    return run


def summary(store,identity):
    with store.practice.transaction() as db:
        requests=[json.loads(r[0]) for r in db.execute('SELECT payload FROM learning_metrics_http WHERE root_id=? OR job_id=?',(identity,identity))]
        runs=[dict(r) for r in db.execute('SELECT * FROM learning_metrics_runs WHERE root_id=? OR job_id=? ORDER BY started_utc,id',(identity,identity))]
        events=[json.loads(r[0]) for r in db.execute('SELECT payload FROM learning_metrics_events WHERE root_id=? OR job_id=?',(identity,identity))]
    active_before=0; first=None
    for run in runs:
        if run['is_parent']: continue
        if first is None and run['first_candidate_ms'] is not None: first=active_before+run['first_candidate_ms']
        active_before+=run['elapsed_ms'] or 0
    fields=('input_tokens','output_tokens','cached_tokens','reasoning_tokens')
    return dict(coverage_status='observed_attempts_only' if runs else 'unavailable',
        http_attempts=len(requests) if runs else None,pending_attempts=sum(r['status']=='pending' for r in requests),
        failed_http_attempts=sum(r['status'] in ('transport_error','response_body_error') or (r['http_status'] or 0)>=400 for r in requests),
        tokens={k:dict(observed_total=sum(r[k] for r in requests if r[k] is not None),unknown_calls=sum(r[k] is None for r in requests)) for k in fields},
        active_ms=sum(r['elapsed_ms'] or 0 for r in runs if not r['is_parent']),
        first_candidate_active_ms=first,
        recovery_failures=events,
        cost_estimate=sum(r['cost_estimate'] for r in requests if r['cost_estimate'] is not None) if any(r['cost_estimate'] is not None for r in requests) else None,
        cost_unknown_calls=sum(r['cost_estimate'] is None for r in requests),
        cost_status='Run-local verified price estimate when configured; otherwise unknown. Not the provider bill.',runs=runs,
        notice='HTTP transport attempts include connection failures; they do not prove server receipt. Missing usage stays unknown. Parent copies are not counted twice. Work predating telemetry is not reconstructed.')
