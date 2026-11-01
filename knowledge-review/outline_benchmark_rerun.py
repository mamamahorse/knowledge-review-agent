"""Rerun only the three failed tasks, preserving all per-request result stages."""

import argparse
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from unittest.mock import patch

import httpx

import outline_benchmark_generation as generation
from outline_benchmark_generation import GenerationRunner, GenerationTransport
from outline_benchmark_live import ForwardTransport
from outline_benchmark_guard import EvaluationStopped, require_approval, review_digest
from outline_benchmark_prepare import write_json
from structured_recovery import audit_scope, read_trace, _audit

PREVIOUS = generation.DEFAULT_RUN
DEFAULT_RUN = PREVIOUS.parent / 'outline-v2-20261028-repaired-rerun'
DOCUMENTS = ('D03', 'D04', 'D06')


def prepare(directory=DEFAULT_RUN, previous=PREVIOUS):
    directory=Path(directory).resolve(); previous=Path(previous).resolve()
    if (previous/'live/running.lock').exists(): raise EvaluationStopped('Prior experiment is still running')
    for doc in DOCUMENTS:
        job=json.loads((previous/'live/jobs'/(doc+'-B.json')).read_text(encoding='utf-8'))
        if job['status']=='completed': raise EvaluationStopped('Requested task is not a previously failed task: '+doc)
    # Reuse the same preparation/guards, but inherit the most recent123 requests,
    # not the older57-request checkpoint. No budget is reset by creating this run.
    with patch.object(generation,'HISTORY_RUN',previous): generation.prepare(directory)
    plan=json.loads((directory/'planning.json').read_text(encoding='utf-8'))
    plan['tasks']=[t for t in plan['tasks'] if t['algorithm']=='v1' and t['document_id'] in DOCUMENTS]
    plan['tasks'].sort(key=lambda t:DOCUMENTS.index(t['document_id']))
    write_json(directory/'planning.json',plan)
    protocol=json.loads((directory/'PROTOCOL.json').read_text(encoding='utf-8'))
    protocol['scope']='Only previously failed D03/D04/D06 V1 tasks; fresh complete jobs on current code; no old result import'
    protocol['prior_run']=str(previous)
    protocol['result_capture']='Raw public response, original arguments, parsed object, repair diff, full schema result, evidence result and final candidates'
    protocol['generation']['task_request_limit_overrides']={t['task_id']:t['actual_request_limit'] for t in plan['tasks']}
    write_json(directory/'PROTOCOL.json',protocol)
    mapping=json.loads((directory/'internal/anonymous_mapping.json').read_text(encoding='utf-8'))
    mapping['tasks']={t['task_id']:mapping['tasks'][t['task_id']] for t in plan['tasks']}
    write_json(directory/'internal/anonymous_mapping.json',mapping)
    approval=json.loads((directory/'gold-approved.json').read_text(encoding='utf-8'))
    approval.update(review_digest=review_digest(directory),workflow_authorization='User explicitly requested current-code rerun of failed D03/D04/D06 and complete result-stage retention')
    write_json(directory/'gold-approved.json',approval)
    events=[json.loads(line) for line in (directory/'live/requests.jsonl').read_text(encoding='utf-8').splitlines()]
    starts={e['request_id']:e for e in events if e['event']=='start'}
    remaining=128-sum(e['kind']=='generation' for e in starts.values())
    lower=sum(t['generation_logical_lower_bound'] for t in plan['tasks'])
    write_json(directory/'RUN_REFERENCE.json',dict(previous_run=str(previous),inherited_http_requests=len(starts),
        inherited_generation_requests=sum(e['kind']=='generation' for e in starts.values()),
        inherited_reserved_usd=sum(e['reserved_usd'] for e in starts.values()),
        remaining_generation_requests=remaining,logical_lower_bound=lower,
        completion_not_guaranteed=lower>remaining,limits_not_expanded=True))
    require_approval(directory)
    print(json.dumps(dict(selected_tasks=[t['document_id'] for t in plan['tasks']],
        remaining_generation_requests=remaining,logical_lower_bound=lower,context_tokens=131072,output_tokens=12288)))


class RecordingTransport(httpx.BaseTransport):
    """Capture after policy injection and HTTP reservation; never persist headers/reasoning."""
    def __init__(self,inner,runner,task): self.inner=inner; self.runner=runner; self.task=task

    def handle_request(self,request):
        start=next(e for e in reversed(self.runner.ledger.events) if e['event']=='start' and e['task']==self.task)
        path=self.runner.live/'raw-responses'/(start['request_id']+'.json')
        path.parent.mkdir(exist_ok=True)
        record=dict(request_id=start['request_id'],task=self.task,utc=start['utc'],
            request_payload=json.loads(request.content),http_status=None,public_response=None,
            capture_policy='Exact public response fields and tool arguments; no auth headers or hidden reasoning content')
        try:
            response=self.inner.handle_request(request); response.read()
            record.update(http_status=response.status_code,response_body_sha256=sha256(response.content).hexdigest())
            try:
                public=deepcopy(response.json())
                if isinstance(public,dict):
                    for choice in public.get('choices',[]):
                        for field in ('message','delta'):
                            if isinstance(choice.get(field),dict): choice[field].pop('reasoning_content',None)
                # Keep even provider errors private, but remove the configured secret if echoed.
                text=json.dumps(public,ensure_ascii=False)
                secret=getattr(self.runner,'options',{}).get('api_key')
                record['public_response']=json.loads(text.replace(secret,'[REDACTED]')) if secret else public
            except (ValueError,UnicodeError):
                record['public_response_parse_error']=True
                secret=getattr(self.runner,'options',{}).get('api_key')
                body=response.text
                record['unparsed_response_text']=body.replace(secret,'[REDACTED]') if secret else body
            return response
        except Exception as exc:
            record.update(transport_error_type=type(exc).__name__,usage=None)
            raise
        finally:
            write_json(path,record)

    def close(self): self.inner.close()


class RerunRunner(GenerationRunner):
    def __init__(self,directory=DEFAULT_RUN):
        super().__init__(directory); self.trace_links={}; self.claimed_request_ids=set()

    def client(self,kind,task,budget):
        from openai import OpenAI
        self.before_http(kind,task)
        controls=self.protocol['generation']
        inner=RecordingTransport(ForwardTransport(httpx.Client(timeout=controls['timeout_seconds'],follow_redirects=False)),self,task)
        transport=GenerationTransport(inner,self,kind,task,budget)
        return OpenAI(api_key=self.options['api_key'],base_url=controls['endpoint'],max_retries=0,
            timeout=controls['timeout_seconds'],http_client=httpx.Client(transport=transport,follow_redirects=False))

    @contextmanager
    def model_scope(self,kind,task,budget):
        with super().model_scope(kind,task,budget):
            directory,original_observer=_audit.get()
            def observe(event):
                if original_observer: original_observer(event)
                if event['event'] not in ('response','stream_response','attempt_failed'): return
                key=event['trace_id']+':'+str(event['attempt'])
                if key in self.trace_links: return
                starts=[e for e in self.ledger.events if e['event']=='start' and e['task']==task]
                latest=starts[-1]['request_id'] if starts else None
                if latest in self.claimed_request_ids: latest=None
                if latest: self.claimed_request_ids.add(latest)
                self.trace_links[key]=latest
                write_json(self.live/'request-trace-links.json',self.trace_links)
            with audit_scope(directory,observe): yield

    def generate(self,task):
        result=super().generate(task)
        export_stages(self.directory)
        return result


def export_stages(directory):
    directory=Path(directory); live=directory/'live'
    plan=json.loads((directory/'planning.json').read_text(encoding='utf-8'))
    links=json.loads((live/'request-trace-links.json').read_text(encoding='utf-8')) if (live/'request-trace-links.json').exists() else {}
    index=[]
    for task in plan['tasks']:
        name=task['document_id']+'-'+task['arm']; root=live/'artifacts'/name; root.mkdir(parents=True,exist_ok=True)
        job_file=live/'jobs'/(name+'.json')
        if not job_file.exists():
            write_json(root/'final_candidates.json',dict(status='not_run',candidates=[]))
            index.append(dict(task_id=task['task_id'],status='not_run',request_count=0,formal_candidates=0))
            continue
        result=json.loads(job_file.read_text(encoding='utf-8')); attempts=[]
        for identity in result['structured_output_trace_ids']:
            records=read_trace(identity,live/'structured-output')
            for attempt in sorted({e['attempt'] for e in records}):
                events=[e for e in records if e['attempt']==attempt]
                request_id=links.get(identity+':'+str(attempt))
                if request_id is None: continue # budget/guard rejection is not another HTTP request
                folder=root/request_id; folder.mkdir(exist_ok=True)
                def last(event): return next((e for e in reversed(events) if e['event']==event),None)
                raw=live/'raw-responses'/(request_id+'.json')
                original=last('original'); parsed=last('parsed'); repair=last('repair_candidate'); schema=last('schema_result')
                failure=last('attempt_failed')
                parse_result=dict(status='passed' if parsed else 'rejected' if original else 'not_run',
                    original_result=original['original'] if original else None,
                    parsed_result=parsed['parsed_result'] if parsed else None,
                    repaired=bool(parsed and parsed['repaired']),repair_candidate=repair,
                    repair_rejection=last('repair_rejected'))
                schema_result=schema or dict(status='not_run',reason='No safely parsed result was available')
                evidence=dict(status='passed' if last('business_validation_passed') else
                    'failed' if failure and failure['phase']=='business_validation' else 'not_run',
                    errors=failure['errors'] if failure and failure['phase']=='business_validation' else [])
                write_json(folder/'parse_result.json',parse_result)
                write_json(folder/'schema_result.json',schema_result)
                write_json(folder/'evidence_result.json',evidence)
                write_json(folder/'original_response.json',json.loads(raw.read_text(encoding='utf-8')))
                attempts.append(dict(request_id=request_id,trace_id=identity,attempt=attempt,
                    parse_status=parse_result['status'],schema_status=schema_result['status'],evidence_status=evidence['status'],
                    artifact_directory=str(folder.relative_to(directory))))
        write_json(root/'model_attempts.json',attempts)
        write_json(root/'final_candidates.json',dict(task_id=task['task_id'],status=result['status'],
            source_sha256=result['source_sha256'],candidates=result['job']['candidates']))
        index.append(dict(task_id=task['task_id'],status=result['status'],request_count=len(attempts),
            formal_candidates=len(result['job']['candidates']),artifact_directory=str(root.relative_to(directory))))
    write_json(directory/'ARTIFACT_INDEX.json',index)
    return index


def run(directory=DEFAULT_RUN):
    try:
        with patch.object(generation,'GenerationRunner',RerunRunner): generation.run(directory)
    finally:
        export_stages(directory)


if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('command',choices=['prepare','run','export'])
    parser.add_argument('--directory',type=Path,default=DEFAULT_RUN); args=parser.parse_args()
    if args.command=='prepare': prepare(args.directory)
    elif args.command=='run': run(args.directory)
    else: export_stages(args.directory)
