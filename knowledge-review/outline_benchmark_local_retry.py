"""Bounded evaluation recovery: two task resumes / two packet retries.

SDK retries remain zero. No model output repair, protocol fallback or extra quota.
"""

import argparse
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime,timezone
import json
from pathlib import Path
import time
from unittest.mock import patch

import httpx
from openai import OpenAI,APIConnectionError,APITimeoutError,APIStatusError
from pydantic import ValidationError

from generation_budget import GenerationBudget,BudgetError
from outline_benchmark import OBJECTIVE,load_source,snapshot
from outline_benchmark_compact import COMPACT_RUN
from outline_benchmark_forced_smoke import ForcedAuditTransport,request_forced_structured
from outline_benchmark_guard import EvaluationStopped,review_digest
from outline_benchmark_live import ForwardTransport
from outline_benchmark_omission import OMISSION_RUN,RevisedPairRunner,verify_revision
from outline_benchmark_pair import run as run_pair
from outline_benchmark_prepare import write_json,digest

RETRY_RUN=OMISSION_RUN.parent/'outline-v2-20261027-local-retry2'
TRANSIENT_HTTP={408,500,502,503,504}
MAX_RETRIES=2


def retryable(exc):
    # A guard rejected before sending can be wrapped by the SDK as a connection
    # error. Never mistake its cause for a retryable network failure.
    chain=[];current=exc
    while current is not None and id(current) not in {id(e) for e in chain}:
        chain.append(current);current=current.__cause__
    if any(isinstance(e,(EvaluationStopped,BudgetError)) for e in chain):return False
    if isinstance(exc,APIStatusError):return exc.status_code in TRANSIENT_HTTP
    if isinstance(exc,(httpx.TransportError,APIConnectionError,APITimeoutError,ValidationError)):return True
    return isinstance(exc,RuntimeError) and any(term in str(exc).lower() for term in (
        'structured result','no result choices','truncated'))


def bounded_request(operation,on_failure,sleep=time.sleep):
    for attempt in range(MAX_RETRIES+1):
        try:return operation()
        except Exception as exc:
            allowed=retryable(exc);on_failure(attempt,exc,allowed)
            if not allowed or attempt==MAX_RETRIES:raise
            sleep(attempt+1)


class RetryAuditTransport(ForcedAuditTransport):
    def handle_request(self,request):
        response=super().handle_request(request)
        # The historical transport stops on all5xx. Only these declared transient
        # statuses may be retried; auth/quota/model-drift/guard errors stay fatal.
        if response.status_code in TRANSIENT_HTTP and self.runner.fatal in (
                f'provider HTTP {response.status_code}',f'provider_HTTP_{response.status_code}'):
            self.runner.fatal=None
        return response


def prepare(directory=RETRY_RUN):
    directory=Path(directory);verify_revision(OMISSION_RUN)
    if directory.exists():raise EvaluationStopped('Use a new run; historical records cannot be overwritten')
    directory.mkdir()
    # Copy only frozen inputs/reference and code archive, never old live state.
    names={'BEFORE_CHANGE.json','RUN_REFERENCE.json','corpus.json','GOLD.json','RUBRIC.md','environment.json','planning.json'}
    corpus=json.loads((OMISSION_RUN/'corpus.json').read_text(encoding='utf-8'))
    for d in corpus['documents']:
        names.add(d['source_file']);names.update(f['path'] for f in d['raw_files'])
        if d.get('document_snapshot'):names.add(d['document_snapshot'])
    names.update(str(p.relative_to(OMISSION_RUN)) for p in (OMISSION_RUN/'baseline-core').rglob('*') if p.is_file())
    for name in names:
        target=directory/name;target.resolve().relative_to(directory.resolve())
        target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes((OMISSION_RUN/name).read_bytes())
    before=json.loads((directory/'BEFORE_CHANGE.json').read_text(encoding='utf-8'))
    before['history_sha256'][str(OMISSION_RUN)]={str(p.relative_to(OMISSION_RUN)):digest(p) for p in OMISSION_RUN.rglob('*') if p.is_file()}
    write_json(directory/'BEFORE_CHANGE.json',before)
    protocol=json.loads((OMISSION_RUN/'PROTOCOL.json').read_text(encoding='utf-8'))
    protocol.update(version=7,run_id=directory.name,status='prepared',
        local_retry=dict(max_retries=2,maximum_attempts=3,sdk_max_retries=0,
            generation='At most two resumes per task; preserve completed batches and context steps',
            judge='At most two retries per identical packet for transport/JSON/Schema/tool-contract errors',
            recoverable='network/timeout, HTTP408/500/502/503/504, malformed JSON/schema, local citation validation',
            fatal='auth/402/quota/429/other400, changed sources/model/config, capacity/budget limits',
            semantic_judge='Never retry a low score, uncertain or invalid semantic judgement to seek a better score',
            delay_seconds=[1,2],repair_output=False,all_attempts_count_in_original_limits=True),
        budget_scope='Original shared USD10; inherited eight attempts and USD0.40 reservation, no reset')
    protocol['stage_policy']['task_failure']='After at most two local resumes stop failed prerequisite; no extra quota'
    protocol['stage_policy']['judge_failure']='Two bounded operational retries; semantic invalid/uncertain rows retained without rejudging'
    write_json(directory/'PROTOCOL.json',protocol)
    approval=json.loads((OMISSION_RUN/'gold-approved.json').read_text(encoding='utf-8'))
    approval.update(review_digest=review_digest(directory),retry_authorization='User explicitly allowed two retries; frozen Gold and scoring rules unchanged')
    write_json(directory/'gold-approved.json',approval)
    (directory/'live').mkdir();history=OMISSION_RUN/'live/requests.jsonl'
    (directory/'live/requests.jsonl').write_bytes(history.read_bytes())
    ref=json.loads((directory/'RUN_REFERENCE.json').read_text(encoding='utf-8'))
    ref.update(before_change_sha256=digest(directory/'BEFORE_CHANGE.json'),history_sha256=digest(history),
        historical_requests=sum(json.loads(l)['event']=='start' for l in history.read_text(encoding='utf-8').splitlines()))
    for name in ('outline_benchmark_local_retry.py','test_outline_benchmark_local_retry.py'):
        ref['current_adapter_sha256'][name]=digest(Path(name))
    write_json(directory/'RUN_REFERENCE.json',ref)
    write_json(directory/'STATUS.json',dict(status='prepared',new_real_http_attempts=0))
    verify_revision(directory)


class LocalRetryRunner(RevisedPairRunner):
    def __init__(self,directory=RETRY_RUN):
        super().__init__(directory);self.last_failure=None;self.active_kind=None;self.active_task=None

    def on_provider_result(self,status,data,classification):
        if status>=400 and status not in TRANSIENT_HTTP:self.fatal=f'provider_HTTP_{status}'

    def client(self,kind,task,budget):
        self.active_kind=kind;self.active_task=task
        self.before_http(kind,task);controls=self.protocol['generation' if kind=='generation' else 'judge']
        transport=RetryAuditTransport(ForwardTransport(httpx.Client(timeout=controls['timeout_seconds'],follow_redirects=False)),
            self,kind,task,budget)
        return OpenAI(api_key=self.options['api_key'],base_url=controls['endpoint'],max_retries=0,
            timeout=controls['timeout_seconds'],http_client=httpx.Client(transport=transport,follow_redirects=False))

    def failed_request(self,attempt,exc,allowed):
        self.last_failure=dict(error_type=type(exc).__name__,retryable=allowed)
        identity=f'{self.active_kind}-{self.active_task}-{len(self.ledger.events):05}-{attempt}'
        (self.live/'recovery').mkdir(exist_ok=True)
        write_json(self.live/'recovery'/(identity.replace(':','_')+'.json'),dict(
            utc=datetime.now(timezone.utc).isoformat(),kind=self.active_kind,task=self.active_task,
            retry_index=attempt,error_type=type(exc).__name__,retryable=allowed,
            cause_type=type(exc.__cause__).__name__ if exc.__cause__ else None))

    def request_adapter(self,*args,**kwargs):
        operation=lambda:request_forced_structured(*args,**kwargs)
        if self.active_kind=='judge':return bounded_request(operation,self.failed_request)
        try:return operation()
        except Exception as exc:
            self.failed_request(0,exc,retryable(exc));raise

    @contextmanager
    def model_scope(self,kind,task,budget):
        from outline_service import validate_learning
        from outline_v2 import validate_direct
        def capture_validation(original):
            def checked(*args,**kwargs):
                try:return original(*args,**kwargs)
                except ValueError as exc:
                    self.failed_request(0,exc,True);raise
            return checked
        self.active_kind=kind;self.active_task=task
        with (super().model_scope(kind,task,budget),
                patch('practice_model.request_structured',self.request_adapter),
                patch('outline_service.validate_learning',capture_validation(validate_learning)),
                patch('outline_v2.validate_direct',capture_validation(validate_direct))):yield

    def score(self,*args,**kwargs):
        # PairRunner.score calls the request helper directly rather than model_scope.
        def judge_request(*a,**kw):
            self.active_kind='judge'
            return bounded_request(lambda:request_forced_structured(*a,**kw),self.failed_request)
        with patch('outline_benchmark_pair.request_forced_structured',judge_request):return super().score(*args,**kwargs)

    def generate(self,task):
        from learning_store import LearningStore
        from library_store import LibraryStore
        from outline_store import OutlineStore
        from outline_service import scope_fingerprint,run_outline
        task_id=task['task_id'];source=load_source(COMPACT_RUN,self.docs[task['document_id']])
        snapshots=[snapshot(self.docs[task['document_id']],source)]
        practice=LearningStore(self.live/'runs.sqlite3');library=LibraryStore(practice);store=OutlineStore(practice)
        collection=library.create(task_id)['id']
        identity=store.create(collection,task_id,OBJECTIVE,'zh',scope_fingerprint(snapshots),snapshots,
            deepcopy(task['sections_plan']),deepcopy(task['batches_plan']),deepcopy(task['plan']))
        budget=GenerationBudget(task['context_tokens'],4096,2048,'same frozen comparison budget')
        start=time.monotonic();first=None;original_finish=store.finish_batch
        def finish(*a,**kw):
            nonlocal first
            ok=original_finish(*a,**kw)
            if ok and a[1] and first is None:first=time.monotonic()-start
            return ok
        ledger_start=sum(e['event']=='start' for e in self.ledger.events)
        attempt_dir=self.live/'task-attempts'/task_id;attempt_dir.mkdir(parents=True)
        print('START',task_id,flush=True)
        resumes=0;thrown=None
        for attempt in range(MAX_RETRIES+1):
            self.last_failure=None
            try:
                with self.model_scope('generation',task_id,budget),patch.object(store,'finish_batch',side_effect=finish):
                    run_outline(store,identity,lambda _:snapshots)
            except Exception as exc:
                thrown=type(exc).__name__;self.failed_request(attempt,exc,retryable(exc))
            job=store.get(identity)
            write_json(attempt_dir/(f'{attempt:02}.json'),dict(job=job,failure=self.last_failure,
                elapsed_seconds=time.monotonic()-start,retry_index=attempt))
            if job['status']=='completed' or self.fatal or not (self.last_failure or {}).get('retryable') or attempt==MAX_RETRIES:break
            if job['status'] not in ('failed','paused'):break
            time.sleep(attempt+1);store.resume(identity);resumes+=1
            print('RESUME',task_id,resumes,flush=True)
        elapsed=time.monotonic()-start
        result=dict(task_id=task_id,document_id=task['document_id'],arm=task['arm'],status=job['status'],job=job,
            elapsed_seconds=elapsed,first_formal_candidate_seconds=first,completed_seconds=elapsed if job['status']=='completed' else None,
            actual_requests=sum(e['event']=='start' for e in self.ledger.events)-ledger_start,
            thrown_type=thrown,local_resume_count=resumes,last_local_failure=self.last_failure)
        write_json(self.live/'jobs'/(task_id+'.json'),result);self.results.append(result)
        print('END',task_id,job['status'],result['actual_requests'],len(job['candidates']),round(elapsed,3),flush=True)
        if self.fatal:raise EvaluationStopped(self.fatal)
        return result


def run(directory=RETRY_RUN):
    verify_revision(directory)
    with patch('outline_benchmark_pair.PairRunner',LocalRetryRunner):run_pair(directory)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('command',choices=['prepare','run']);args=parser.parse_args()
    prepare() if args.command=='prepare' else run()
