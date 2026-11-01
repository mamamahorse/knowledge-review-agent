"""Bounded next step: D01 A/C scoring, then one preregistered RFC B/D pair."""

import argparse
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime,timezone
import json
from pathlib import Path
from unittest.mock import patch

import httpx
from openai import OpenAI,APIError

from generation_budget import GenerationBudget
from outline_benchmark_compact import COMPACT_RUN
from outline_benchmark_forced_smoke import (FORCED_RUN,SHARED_PROTOCOL,ForcedAuditTransport,
    request_forced_structured,verify_forced)
from outline_benchmark_guard import RequestLedger,limits_from_protocol,EvaluationStopped,review_digest
from outline_benchmark_live import Runner,ForwardTransport
from outline_benchmark_prepare import write_json,digest
from outline_benchmark_retry import retry_digest,require_retry_authorization,verify_reference
from outline_benchmark_judge import (anonymize,packets,judge_messages,check_coverage,check_quality,metrics)

PAIR_RUN=COMPACT_RUN.parent/'outline-v2-20261027-quality-pair'
ALLOWED_TASKS=['D01-C','D04-D','D04-B']


def verify_pair(directory):
    source,ref=verify_reference(directory)
    for root,files in ref['extra_history_sha256'].items():
        for name,expected in files.items():
            if digest(Path(root)/name)!=expected:raise EvaluationStopped('Historical run changed: '+name)
    return source,ref


def prepare(directory=PAIR_RUN):
    directory=Path(directory)
    if directory.exists():raise EvaluationStopped('Existing run is immutable; use a new version')
    verify_forced(FORCED_RUN)
    protocol=deepcopy(json.loads((FORCED_RUN/'PROTOCOL.json').read_text(encoding='utf-8')))
    protocol.update(version=5,run_id=directory.name,status='prepared',
        acceptance=dict(thinking='actual HTTP thinking.type=disabled',
            generation_pass='completed structured result + original Schema + original visible citation validation',
            reasoning_tokens_missing='unknown; not a failure or reason to stop',
            inherited_D01_A='User explicitly accepted the prior business result; import, do not rerun'),
        stage_policy=dict(allowed_tasks=ALLOWED_TASKS,judge_enabled=True,
            order=['D01-C','score D01-A/C','budget review','D04-D','D04-B','score D04-B/D'],
            task_failure='Stop; no additional allowance or retry',judge_failure='Keep uncertain rows; service failures stop'),
        complex_document_selection=dict(document='D04',reason='RFC9111 compact Vary/freshness rules with exceptions; V1 hierarchical lower bound8 versus V2 direct1, smallest nontrivial normative-source pair; selected before new results',
            configuration='B/D both 131072; same model/thinking/output/safety/protocol',lower_bound_is_not_cost=True),
        scope='Import D01-A; generate D01-C; score A/C; if successful and budget sufficient generate one D04 B/D pair only',
        implementation_status='Small serial comparison; frozen Gold and rubric, automatic judge, no default switch')
    protocol['judge']['protocol']='forced submit_result; same no-fallback transport; missing reasoning details unknown'
    directory.mkdir(parents=True);(directory/'live').mkdir()
    history=FORCED_RUN/'live/requests.jsonl';(directory/'live/requests.jsonl').write_bytes(history.read_bytes())
    ref=deepcopy(json.loads((FORCED_RUN/'SOURCE_REFERENCE.json').read_text(encoding='utf-8')))
    ref['extra_history_sha256'][str(FORCED_RUN)]={str(p.relative_to(FORCED_RUN)):digest(p) for p in FORCED_RUN.rglob('*') if p.is_file()}
    starts=[json.loads(l) for l in history.read_text(encoding='utf-8').splitlines() if json.loads(l)['event']=='start']
    ref.update(history_sha256=digest(history),historical_request_ids=[s['request_id'] for s in starts],
        historical_actual_requests=len(starts),historical_reserved_usd=sum(s['reserved_usd'] for s in starts))
    for name in ('outline_benchmark_pair.py','outline_benchmark_judge.py','test_outline_benchmark_pair.py'):
        ref['adapter_sha256'][name]=digest(Path(__file__).parent/name)
    write_json(directory/'PROTOCOL.json',protocol);write_json(directory/'SOURCE_REFERENCE.json',ref)
    write_json(directory/'retry-approved.json',dict(approved_by='user',approved_at=datetime.now(timezone.utc).isoformat(),
        retry_digest=retry_digest(directory),instruction='Accept missing reasoning tokens as unknown; D01-C, blind A/C evaluation, then budget-reviewed one complex B/D pair'))
    write_json(directory/'STATUS.json',dict(status='prepared',new_real_http_attempts=0))
    verify_pair(directory)


def validate_job(result):
    from practice_store import archived_sources
    from outline_service import LearningProposal,validate_learning,candidate_visible,excerpt
    job=result['job'];sources=archived_sources(job['snapshots']);by_id={s.source_id:s for s in sources}
    if job['status']!='completed' or not job['candidates']:raise EvaluationStopped('No completed nonempty structured candidate result')
    for c in job['candidates']:
        batch=next(b for b in job['batches'] if b['id']==c['batch_id']);source=by_id[batch['source_id']]
        visible=candidate_visible(c,job,source,excerpt(source,batch['line_start'],batch['line_end']))
        validate_learning(LearningProposal.model_validate(c['learning']),visible)
    return by_id


class PairRunner(Runner):
    def __init__(self,directory=PAIR_RUN):
        self.work=Path(directory);require_retry_authorization(self.work);source,ref=verify_pair(self.work)
        validation=json.loads((self.work/'OFFLINE_VALIDATION.json').read_text(encoding='utf-8'))
        if validation['status']!='passed':raise EvaluationStopped('Offline checks not passed')
        super().__init__(source)
        self.live=self.work/'live'
        for name in ('calls','jobs','judge'):(self.live/name).mkdir(exist_ok=True)
        self.protocol=json.loads((self.work/'PROTOCOL.json').read_text(encoding='utf-8'))
        self.ledger=RequestLedger(self.live/'requests.jsonl',limits_from_protocol(self.protocol))
        self.returned_models=json.loads((FORCED_RUN/'live/summary.json').read_text(encoding='utf-8'))['returned_models']
        self.gold=json.loads((self.directory/'GOLD.json').read_text(encoding='utf-8'))['goals']
        self.rubric=(self.directory/'RUBRIC.md').read_text(encoding='utf-8')
        self.phase='prepared';self.scored={};self.generated={}

    def before_http(self,kind,task):
        require_retry_authorization(self.work);verify_pair(self.work)
        if self.fatal:raise EvaluationStopped(self.fatal)
        if kind=='generation' and task not in ALLOWED_TASKS:raise EvaluationStopped('Generation outside approved small comparison')
        if kind=='judge' and not any(task.startswith(t+':') for t in ['D01-A','D01-C','D04-B','D04-D']):
            raise EvaluationStopped('Judge packet outside approved comparison')

    def on_provider_result(self,status,data,classification):
        # Missing reasoning_tokens is recorded by the transport, not an acceptance gate.
        if status>=400:self.fatal=f'provider_HTTP_{status}'

    def client(self,kind,task,budget):
        self.before_http(kind,task);controls=self.protocol['generation' if kind=='generation' else 'judge']
        transport=ForcedAuditTransport(ForwardTransport(httpx.Client(timeout=controls['timeout_seconds'],follow_redirects=False)),
            self,kind,task,budget)
        return OpenAI(api_key=self.options['api_key'],base_url=controls['endpoint'],max_retries=0,
            timeout=controls['timeout_seconds'],http_client=httpx.Client(transport=transport,follow_redirects=False))

    @contextmanager
    def model_scope(self,kind,task,budget):
        with super().model_scope(kind,task,budget),patch('practice_model.request_structured',request_forced_structured):yield

    def checkpoint(self,status='running',error=None):
        write_json(self.live/'summary.json',self.summary())
        write_json(self.work/'STATUS.json',dict(status=status,phase=self.phase,error=error,
            generated=list(self.generated),scored=list(self.scored),remaining_original_tasks_not_started=True))

    def generate_once(self,task_id):
        self.phase='generation '+task_id
        with (self.live/(task_id+'-started.json')).open('x',encoding='utf-8') as stream:stream.write('one logical task; no retry')
        task=next(t for t in self.plan['tasks'] if t['task_id']==task_id)
        result=self.generate(task);validate_job(result)
        self.generated[task_id]=result;self.checkpoint();return result

    def score(self,task_id,result):
        sources=validate_job(result);doc=result['document_id'];goals=[g for g in self.gold if g['document_id']==doc]
        candidates,mapping=anonymize(result['job']['candidates'],sources)
        output=self.live/'judge'/task_id;output.mkdir()
        write_json(output/'anonymous_mapping.json',mapping)
        coverage=[];quality=[];failed=[]
        for index,(kind,packet,schema) in enumerate(packets(goals,candidates),1):
            self.phase=f'judge {task_id} {kind} {index}'
            identity=f'{task_id}:{kind}:{index:02}';messages=judge_messages(kind,packet,self.rubric)
            write_json(output/(f'{index:02}-input.json'),dict(kind=kind,packet=packet,schema=schema.model_json_schema()))
            budget=GenerationBudget(65536,4096,2048,'frozen judge configuration')
            try:
                with self.client('judge',identity,budget) as client:
                    response=request_forced_structured(client,self.protocol['judge']['model'],messages,schema,budget=budget)
                checked=check_coverage(response,packet,sources) if kind=='coverage' else check_quality(response,packet,sources)
                (coverage if kind=='coverage' else quality).extend(checked)
                write_json(output/(f'{index:02}-result.json'),dict(raw=response.model_dump(),checked=checked))
            except Exception as exc:
                failed.append(dict(packet=index,kind=kind,error_type=type(exc).__name__))
                write_json(output/(f'{index:02}-failed.json'),failed[-1])
                if self.fatal or isinstance(exc,(EvaluationStopped,APIError)):
                    raise EvaluationStopped('Judge service/request stopped: '+type(exc).__name__) from exc
            self.checkpoint()
        value=metrics(goals,candidates,coverage,quality)
        value.update(task_id=task_id,program_reference_valid_rate=1.0,coverage_rows=coverage,quality_rows=quality,failed_packets=failed)
        self.scored[task_id]=value;write_json(output/'metrics.json',value);self.checkpoint()
        print('SCORED',task_id,value['complete_gold'],value['gold_count'],value['valid_targets'],value['candidates'],flush=True)
        return value

    def assess_remaining_budget(self):
        starts=[e for e in self.ledger.events if e['event']=='start'];reserved=sum(e['reserved_usd'] for e in starts)
        gen=sum(e['kind']=='generation' for e in starts);judge=sum(e['kind']=='judge' for e in starts)
        # Keep both task caps and the ORIGINAL global budgets; do not enlarge any.
        value=dict(selected='D04',selection_fixed_before_results=True,remaining_generation_requests=128-gen,
            remaining_judge_requests=96-judge,remaining_total_requests=224-len(starts),remaining_reserved_usd=10-reserved,
            offline_generation_lower_bound=9,planning_bound_not_actual_cost=True,
            worst_case_remaining_generation_for_pair=32,remaining_judge_reservation=(96-judge)*.025,
            generation_pair_max_reservation=32*.05)
        value['sufficient_for_pair_with_full_remaining_judge_reserve']=(128-gen>=32 and 224-len(starts)>=32+96-judge
            and 10-reserved>=32*.05+(96-judge)*.025)
        write_json(self.work/'BUDGET_REVIEW.json',value)
        if not value['sufficient_for_pair_with_full_remaining_judge_reserve']:raise EvaluationStopped('Insufficient remaining allowance; do not expand')
        return value


def run(directory=PAIR_RUN):
    directory=Path(directory);lock=directory/'live/running.lock'
    with lock.open('x',encoding='utf-8') as stream:stream.write(datetime.now(timezone.utc).isoformat())
    runner=None
    try:
        with (directory/'campaign-started.json').open('x',encoding='utf-8') as stream:stream.write('no automatic rerun')
        runner=PairRunner(directory)
        old=json.loads((FORCED_RUN/'live/jobs/D01-A.json').read_text(encoding='utf-8'));validate_job(old)
        runner.generated['D01-A']=old
        write_json(runner.live/'jobs/D01-A.json',dict(**old,imported_from=str(FORCED_RUN),accepted_by_user=True))
        c=runner.generate_once('D01-C')
        runner.score('D01-A',old);runner.score('D01-C',c)
        runner.assess_remaining_budget()
        # Reverse algorithm order for the second pair; the choice/order is preregistered.
        d=runner.generate_once('D04-D');b=runner.generate_once('D04-B')
        runner.score('D04-B',b);runner.score('D04-D',d)
        runner.phase='small comparison finished';runner.checkpoint('completed')
    except Exception as exc:
        if runner:runner.checkpoint('stopped',str(exc) if isinstance(exc,EvaluationStopped) else type(exc).__name__)
        print('STOP',str(exc) if isinstance(exc,EvaluationStopped) else type(exc).__name__,flush=True)
    finally:lock.unlink(missing_ok=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('command',choices=['prepare','run']);args=parser.parse_args()
    prepare() if args.command=='prepare' else run()
