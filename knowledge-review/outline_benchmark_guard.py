"""Isolated request accounting. No real client or network is constructed here.

The transport observes actual HTTP attempts, including rejected requests. The
algorithm's own per-batch telemetry is useful diagnostics, not the billing ledger.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import time
import uuid

import httpx


class EvaluationStopped(RuntimeError):
    pass


def review_digest(directory):
    directory=Path(directory)
    names=['corpus.json','GOLD.json','PROTOCOL.json','RUBRIC.md','planning.json','environment.json']
    return sha256(json.dumps({n:sha256((directory/n).read_bytes()).hexdigest() for n in names},
        sort_keys=True).encode()).hexdigest()


def require_approval(directory):
    directory=Path(directory)
    marker=directory/'gold-approved.json'
    if not marker.exists():
        raise EvaluationStopped('Gold has not been explicitly approved by the user')
    value=json.loads(marker.read_text(encoding='utf-8'))
    if value.get('approved_by')!='user' or value.get('review_digest')!=review_digest(directory):
        raise EvaluationStopped('Approval digest is missing or frozen inputs changed')
    corpus=json.loads((directory/'corpus.json').read_text(encoding='utf-8'))
    for doc in corpus.get('documents',[]):
        files=[dict(path=doc['source_file'],sha256=doc['sha256']),*doc.get('raw_files',[])]
        for item in files:
            path=(directory/item['path']).resolve()
            path.relative_to(directory.resolve())
            if sha256(path.read_bytes()).hexdigest()!=item['sha256']:
                raise EvaluationStopped('Frozen corpus bytes changed: '+doc['id'])
    plan=json.loads((directory/'planning.json').read_text(encoding='utf-8'))
    for name,expected in plan.get('code_sha256',{}).items():
        path=(Path(__file__).parent/name).resolve()
        path.relative_to(Path(__file__).parent.resolve())
        if sha256(path.read_bytes()).hexdigest()!=expected:
            raise EvaluationStopped('Core algorithm changed: '+name)
    return value


@dataclass(frozen=True)
class Limits:
    generation_per_task: int=16
    generation_total: int=128
    judge_total: int=96
    all_total: int=224
    reserved_usd: float=10.0
    generation_task_overrides: tuple[tuple[str,int],...]=()

    def __post_init__(self):
        pairs=self.generation_task_overrides
        if len(dict(pairs))!=len(pairs) or any(not key or not isinstance(cap,int) or isinstance(cap,bool) or cap<=0 for key,cap in pairs):
            raise ValueError('Task request overrides must have unique IDs and positive integer limits')


def limits_from_protocol(protocol):
    """Explicit per-task exceptions do not expand the campaign-wide allowance."""
    generation=protocol['generation']
    return Limits(generation_per_task=generation['cap_per_task'],generation_total=generation['cap_total'],
        judge_total=protocol['judge']['cap_total'],all_total=protocol['all_actual_requests_cap'],
        reserved_usd=protocol['soft_conservative_reservation_usd'],
        generation_task_overrides=tuple(sorted(generation.get('task_request_limit_overrides',{}).items())))


class RequestLedger:
    """Append-only start/end events survive failures and count starts, not copies."""

    def __init__(self,path,limits=Limits()):
        self.path=Path(path);self.limits=limits
        self.events=[json.loads(line) for line in self.path.read_text(encoding='utf-8').splitlines()] if self.path.exists() else []

    def append(self,event):
        event['utc']=datetime.now(timezone.utc).isoformat()
        with self.path.open('a',encoding='utf-8') as stream:
            stream.write(json.dumps(event,ensure_ascii=False)+'\n')
            stream.flush()
            os.fsync(stream.fileno())
        self.events.append(event)

    def reserve(self,kind,task,reserve_usd,metadata):
        if kind not in ('generation','judge') or reserve_usd<=0:
            raise ValueError('Known request category and positive conservative reservation required')
        starts=[e for e in self.events if e['event']=='start']
        same=[e for e in starts if e['kind']==kind]
        cap=self.limits.generation_total if kind=='generation' else self.limits.judge_total
        if len(starts)>=self.limits.all_total or len(same)>=cap:
            raise EvaluationStopped('Campaign request cap reached')
        task_limit=dict(self.limits.generation_task_overrides).get(task,self.limits.generation_per_task)
        if kind=='generation' and sum(e['task']==task for e in same)>=task_limit:
            raise EvaluationStopped('Task actual-request cap reached')
        # Never refund the conservative reservation, even for failures/no usage.
        # This bounds local authorization; it is not a provider billing guarantee.
        if sum(e['reserved_usd'] for e in starts)+reserve_usd>self.limits.reserved_usd+1e-9:
            raise EvaluationStopped('Conservative campaign cost reservation cap reached')
        identity=uuid.uuid4().hex
        self.append(dict(event='start',request_id=identity,kind=kind,task=task,
            reserved_usd=reserve_usd,**metadata))
        return identity

    def finish(self,identity,**metadata):
        if not any(e['event']=='start' and e['request_id']==identity for e in self.events):
            raise ValueError('Unknown actual request ID')
        if any(e['event']=='end' and e['request_id']==identity for e in self.events):
            raise ValueError('Request already finalized; diagnostic copies cannot be re-billed')
        self.append(dict(event='end',request_id=identity,**metadata))


class AuditTransport(httpx.BaseTransport):
    """Wrap a supplied transport only after the executor has checked approval.

    Freeze the endpoint/model/thinking settings in the executor. Never persist
    authentication headers, endpoint query strings, or hidden reasoning content.
    """
    def __init__(self,inner,ledger,kind,task,reserve_usd,approval_directory):
        require_approval(approval_directory)
        self.inner=inner;self.ledger=ledger;self.kind=kind;self.task=task;self.reserve_usd=reserve_usd
        self.approval_directory=approval_directory

    def handle_request(self,request):
        require_approval(self.approval_directory)
        if request.method!='POST' or not request.url.path.rstrip('/').endswith('/chat/completions'):
            raise EvaluationStopped('Unexpected evaluation HTTP operation')
        payload=json.loads(request.content)
        if payload.get('stream'):
            raise EvaluationStopped('Benchmark uses non-streamed auditable requests')
        protocol='tools' if payload.get('tools') else 'response_format' if payload.get('response_format') else 'plain'
        identity=self.ledger.reserve(self.kind,self.task,self.reserve_usd,
            dict(protocol=protocol,requested_model=payload.get('model'),
                 payload_sha256=sha256(request.content).hexdigest(),payload_bytes=len(request.content)))
        start=time.monotonic()
        try:
            response=self.inner.handle_request(request)
            response.read()
        except Exception as exc:
            self.ledger.finish(identity,status='transport_error',error_type=type(exc).__name__,
                elapsed_seconds=time.monotonic()-start,usage=None,usage_status='unknown')
            raise
        try:value=response.json()
        except (ValueError,UnicodeError):value={}
        if not isinstance(value,dict):value={}
        usage=value.get('usage')
        # Only usage aggregates and finish reasons: no response content/reasoning.
        self.ledger.finish(identity,status='response',http_status=response.status_code,
            elapsed_seconds=time.monotonic()-start,usage=usage if isinstance(usage,dict) else None,
            usage_status='reported' if isinstance(usage,dict) else 'unknown',
            returned_model=value.get('model'),response_id=value.get('id'),
            finish_reasons=[c.get('finish_reason') for c in value.get('choices',[]) if isinstance(c,dict)])
        return response

    def close(self):self.inner.close()


def dollar_cost(usage,prices):
    """No double-counting cache-hit input. Unknown/inconsistent usage stays null."""
    if not isinstance(usage,dict):return None
    prompt=usage.get('prompt_tokens');output=usage.get('completion_tokens')
    hit=usage.get('prompt_cache_hit_tokens')
    if hit is None:hit=(usage.get('prompt_tokens_details') or {}).get('cached_tokens')
    if any(not isinstance(v,int) or isinstance(v,bool) or v<0 for v in (prompt,output,hit)) or hit>prompt:
        return None
    return ((prompt-hit)*prices['input_miss']+hit*prices['input_hit']+output*prices['output'])/1_000_000
