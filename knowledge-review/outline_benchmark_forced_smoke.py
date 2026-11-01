"""Explicit forced-tool evaluation protocol; one HTTP smoke, no fallback.

Only the isolated process binds this adapter to practice_model. Existing core
schemas, checked_message, and the outline's visible-citation checks remain used.
"""

import argparse
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from unittest.mock import patch

import httpx
from openai import OpenAI

from generation_budget import GenerationBudget
from outline_benchmark_compact import COMPACT_RUN
from outline_benchmark_guard import EvaluationStopped, review_digest
from outline_benchmark_live import ControlledAuditTransport, ForwardTransport
from outline_benchmark_prepare import digest, write_json
from outline_benchmark_retry import RETRY_RUN, RetryRunner, retry_digest, verify_reference
from structured_output import RESULT_TOOL, checked_message, result_tools

FORCED_RUN=COMPACT_RUN.parent/'outline-v2-20261027-retry2-forced-tool'
FORCED_CHOICE={'type':'function','function':{'name':RESULT_TOOL}}
SHARED_PROTOCOL=dict(name='forced_submit_result',thinking={'type':'disabled'},
    tool_choice=FORCED_CHOICE,native_json_schema=False,fallback=False,output_tokens=4096,safety_margin=2048)


def request_forced_structured(client,model,messages,schema,*,budget=None,before_request=None,on_delta=None,telemetry=None):
    """Send exactly once; reuse original tool schema and Pydantic validation."""
    if on_delta is not None:raise EvaluationStopped('This experiment uses non-streamed requests')
    budget=budget or GenerationBudget.from_env()
    tools=result_tools(schema)
    if before_request:before_request()
    budget.check(messages,model=model,max_tokens=budget.output_tokens,tools=tools,tool_choice=FORCED_CHOICE)
    response=client.chat.completions.create(model=model,messages=messages,max_tokens=budget.output_tokens,
        tools=tools,tool_choice=deepcopy(FORCED_CHOICE))
    if telemetry is not None:telemetry(response,'tool_forced')
    message=checked_message(response)
    calls=message.tool_calls or []
    if len(calls)!=1 or calls[0].type!='function' or calls[0].function.name!=RESULT_TOOL:
        raise RuntimeError('the model did not submit one structured result.')
    return schema.model_validate_json(calls[0].function.arguments)


class ForcedAuditTransport(ControlledAuditTransport):
    def handle_request(self,request):
        body=json.loads(request.content)
        tools=body.get('tools') or []
        if ('response_format' in body or body.get('tool_choice')!=FORCED_CHOICE or len(tools)!=1
                or tools[0].get('type')!='function' or tools[0].get('function',{}).get('name')!=RESULT_TOOL):
            raise EvaluationStopped('Evaluation protocol requires only forced submit_result; native/auto/text are forbidden')
        return super().handle_request(request)


def verify_forced(directory):
    source,ref=verify_reference(directory)
    for root,files in ref['extra_history_sha256'].items():
        for name,expected in files.items():
            if digest(Path(root)/name)!=expected:raise EvaluationStopped('Preserved retry history changed: '+name)
    protocol=json.loads((Path(directory)/'PROTOCOL.json').read_text(encoding='utf-8'))
    if (protocol['algorithm_protocol']!={'v1':SHARED_PROTOCOL,'v2':SHARED_PROTOCOL}
            or protocol['generation']['thinking']!={'type':'disabled'}
            or 'reasoning_effort' in protocol['generation']):
        raise EvaluationStopped('V1/V2 must share the frozen forced-tool protocol')
    return source,ref


def prepare(directory=FORCED_RUN):
    directory=Path(directory)
    if directory.exists():raise EvaluationStopped('Do not overwrite an existing run')
    verify_reference(RETRY_RUN)
    protocol=deepcopy(json.loads((COMPACT_RUN/'PROTOCOL.json').read_text(encoding='utf-8')))
    protocol.update(version=4,run_id=directory.name,status='prepared_for_one_forced_smoke',
        source_run=str(COMPACT_RUN),strict_protocol_errors=True,
        algorithm_protocol={'v1':deepcopy(SHARED_PROTOCOL),'v2':deepcopy(SHARED_PROTOCOL)},
        scope='Exactly one D01-A logical task and at most one new HTTP request; no other tasks or Judge',
        stage_policy=dict(allowed_tasks=['D01-A'],allowed_kinds=['generation'],max_new_http_attempts=1,
            max_logical_tasks=1,judge_enabled=False,D01_C_enabled=False),
        historical_planning_note='Frozen plans retain their original conservative dual-protocol estimate; request wire is checked with forced tool parameters. No rerouting or algorithm changes.',
        gold_approval_status='Approved frozen source Gold; no reannotation',
        implementation_status='Explicit evaluation-only forced tool; offline tests required before live smoke',
        budget_scope='One shared original USD10 reservation including all four historical HTTP attempts; not a new USD10 pool')
    protocol['generation'].update(thinking={'type':'disabled'},protocol='forced submit_result only; no native probe or fallback',
        protocol_cache='Explicit configuration shared by V1/V2; no learned-capability cache needed')
    protocol['generation'].pop('reasoning_effort',None)
    protocol['judge']['protocol']='forced submit_result when separately authorized; disabled in this stage'
    directory.mkdir(parents=True);(directory/'live').mkdir()
    history=RETRY_RUN/'live/requests.jsonl'
    (directory/'live/requests.jsonl').write_bytes(history.read_bytes())
    starts=[json.loads(line) for line in history.read_text(encoding='utf-8').splitlines() if json.loads(line)['event']=='start']
    root=Path(__file__).parent;env=root/'.env'
    adapters=['outline_benchmark_live.py','outline_benchmark_retry.py','outline_benchmark_guard.py',
        'outline_benchmark_forced_smoke.py','test_outline_benchmark_forced_smoke.py']
    ref=dict(source_run=str(COMPACT_RUN),approved_source_digest=review_digest(COMPACT_RUN),
        parent_file_sha256={str(p.relative_to(COMPACT_RUN)):digest(p) for p in COMPACT_RUN.rglob('*') if p.is_file()},
        extra_history_sha256={str(RETRY_RUN):{str(p.relative_to(RETRY_RUN)):digest(p) for p in RETRY_RUN.rglob('*') if p.is_file()}},
        history_sha256=digest(history),historical_request_ids=[s['request_id'] for s in starts],
        historical_actual_requests=len(starts),historical_reserved_usd=sum(s['reserved_usd'] for s in starts),
        gold_sha256=digest(COMPACT_RUN/'GOLD.json'),adapter_sha256={n:digest(root/n) for n in adapters},
        formal_env_sha256=digest(env) if env.exists() else None)
    write_json(directory/'PROTOCOL.json',protocol);write_json(directory/'SOURCE_REFERENCE.json',ref)
    write_json(directory/'retry-approved.json',dict(approved_by='user',approved_at=datetime.now(timezone.utc).isoformat(),
        retry_digest=retry_digest(directory),instruction='优先使用thinking=disabled+强制submit_result；先离线测试，再最多一次D01-A真实冒烟；失败停止，不自动开启其他任务'))
    write_json(directory/'STATUS.json',dict(status='prepared',new_real_http_attempts=0,
        inherited_real_http_attempts=len(starts),cumulative_reserved_usd=ref['historical_reserved_usd']))
    verify_forced(directory)


class ForcedSmokeRunner(RetryRunner):
    def __init__(self,directory=FORCED_RUN):
        verify_forced(directory)
        validation=Path(directory)/'OFFLINE_VALIDATION.json'
        if not validation.exists() or json.loads(validation.read_text(encoding='utf-8')).get('status')!='passed':
            raise EvaluationStopped('Offline validation must pass before the single real smoke')
        super().__init__(directory)

    def before_http(self,kind,task):
        super().before_http(kind,task)
        verify_forced(self.retry_directory)
        if any(e['event']=='start' and e['request_id'] not in self.imported_ids for e in self.ledger.events):
            raise EvaluationStopped('The single new HTTP request has already been used')

    def client(self,kind,task,budget):
        if kind!='generation' or task!='D01-A':raise EvaluationStopped('Only D01-A is authorized in this stage')
        controls=self.protocol['generation']
        forwarded=httpx.Client(timeout=controls['timeout_seconds'],follow_redirects=False)
        transport=ForcedAuditTransport(ForwardTransport(forwarded),self,kind,task,budget)
        return OpenAI(api_key=self.options['api_key'],base_url=controls['endpoint'],max_retries=0,
            timeout=controls['timeout_seconds'],http_client=httpx.Client(transport=transport,follow_redirects=False))

    @contextmanager
    def model_scope(self,kind,task,budget):
        with super().model_scope(kind,task,budget),patch('practice_model.request_structured',request_forced_structured):
            yield


def execute(directory=FORCED_RUN):
    directory=Path(directory);verify_forced(directory)
    lock=directory/'live/running.lock'
    with lock.open('x',encoding='utf-8') as stream:stream.write(datetime.now(timezone.utc).isoformat())
    runner=None
    try:
        runner=ForcedSmokeRunner(directory)
        try:
            runner.run_once();status='D01-A_forced_smoke_success';error=None
        except Exception as exc:
            status='stopped';error=str(exc) if isinstance(exc,EvaluationStopped) else type(exc).__name__
        summary=runner.summary();write_json(directory/'live/summary.json',summary)
        _,ref=verify_forced(directory)
        result=dict(status=status,error=error,new_real_http_attempts=summary['actual_requests']-ref['historical_actual_requests'],
            inherited_real_http_attempts=ref['historical_actual_requests'],cumulative_reserved_usd=summary['reserved_usd'],
            currency='USD',shared_reservation_stop=10,protocol=SHARED_PROTOCOL,D01_C_executed=False,judge_executed=False)
        write_json(directory/'STATUS.json',result)
        print(json.dumps(result,ensure_ascii=False),flush=True)
    finally:
        lock.unlink(missing_ok=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('command',choices=['prepare','run-once']);args=parser.parse_args()
    prepare() if args.command=='prepare' else execute()
