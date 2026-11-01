"""One-shot thinking-off smoke: separate run, frozen source reference, no auto-run.

The CLI only prepares a proposal. Live execution additionally requires a separate
explicit execution marker; the earlier Gold approval is not that authorization.
"""

import argparse
from copy import deepcopy
from datetime import datetime,timezone
from hashlib import sha256
import json
from pathlib import Path
import shutil

from outline_benchmark_compact import COMPACT_RUN
from outline_benchmark_guard import require_approval,review_digest,RequestLedger,limits_from_protocol,EvaluationStopped
from outline_benchmark_live import Runner
from outline_benchmark_prepare import digest,write_json
from structured_output import FUNCTION_OUTPUT_PROVIDERS

RETRY_RUN=COMPACT_RUN.parent/'outline-v2-20261027-retry1-thinking-off'
ADAPTERS=('outline_benchmark_live.py','outline_benchmark_retry.py')


def retry_digest(directory):
    directory=Path(directory)
    return sha256(json.dumps({n:digest(directory/n) for n in ('PROTOCOL.json','SOURCE_REFERENCE.json')},sort_keys=True).encode()).hexdigest()


def verify_reference(directory):
    ref=json.loads((Path(directory)/'SOURCE_REFERENCE.json').read_text(encoding='utf-8'))
    source=Path(ref['source_run']);require_approval(source)
    if review_digest(source)!=ref['approved_source_digest']:
        raise EvaluationStopped('Approved source version changed')
    for name,expected in ref['parent_file_sha256'].items():
        if digest(source/name)!=expected:raise EvaluationStopped('Historical artifacts changed: '+name)
    for name,expected in ref['adapter_sha256'].items():
        if digest(Path(__file__).parent/name)!=expected:raise EvaluationStopped('Retry adapter changed: '+name)
    env=Path(__file__).parent/'.env'
    if (digest(env) if env.exists() else None)!=ref['formal_env_sha256']:
        raise EvaluationStopped('Formal configuration file changed; do not overwrite it')
    return source,ref


def require_retry_authorization(directory):
    directory=Path(directory);marker=directory/'retry-approved.json'
    if not marker.exists():raise EvaluationStopped('Retry configuration submitted for review; live execution not authorized')
    approved=json.loads(marker.read_text(encoding='utf-8'))
    if approved.get('approved_by')!='user' or approved.get('retry_digest')!=retry_digest(directory):
        raise EvaluationStopped('Retry authorization is stale or invalid')
    verify_reference(directory)


def prepare(directory=RETRY_RUN):
    directory=Path(directory)
    if directory.exists():raise ValueError('Use a new run version; never overwrite a prior proposal or failure')
    require_approval(COMPACT_RUN)
    before={str(p.relative_to(COMPACT_RUN)):digest(p) for p in COMPACT_RUN.rglob('*') if p.is_file()}
    protocol=deepcopy(json.loads((COMPACT_RUN/'PROTOCOL.json').read_text(encoding='utf-8')))
    protocol.update(version=3,run_id=directory.name,status='proposal_only_not_executed',
        source_run=str(COMPACT_RUN),strict_protocol_errors=True,
        stage_policy=dict(allowed_tasks=['D01-A'],allowed_kinds=['generation'],max_new_http_attempts=3,
            max_logical_tasks=1,judge_enabled=False,D01_C_enabled=False,
            stop_on=['length','schema_or_citation_validation_failure','unclassified_400','thinking_mode_constraint',
                     'thinking_not_disabled','missing_thinking_verification','authentication_or_service_error']),
        previous_failure_preserved=True,
        budget_instruction='User retained original USD10 conservative reservation; CNY10/account-hard-cap changes canceled',
        budget_scope='Generation and Judge share one USD10 pool, including imported historical attempts; no fresh USD10 per run',
        execution_authorization='pending; no real requests in preparation')
    protocol['generation']['thinking']={'type':'disabled'}
    protocol['generation'].pop('reasoning_effort',None)
    protocol['generation']['protocol_cache']='start fresh: failed/truncated old calls do not confirm a capability; keep original schemas and validated compatibility flow'
    protocol['scope']='D01-A only. No D01-C,other document or Judge until separately considered after success.'
    directory.mkdir(parents=True);(directory/'live').mkdir()
    history=COMPACT_RUN/'live/requests.jsonl';shutil.copyfile(history,directory/'live/requests.jsonl')
    starts=[json.loads(line) for line in history.read_text(encoding='utf-8').splitlines() if json.loads(line)['event']=='start']
    env=Path(__file__).parent/'.env'
    ref=dict(source_run=str(COMPACT_RUN),approved_source_digest=review_digest(COMPACT_RUN),
        parent_file_sha256=before,history_sha256=digest(history),historical_request_ids=[s['request_id'] for s in starts],
        historical_actual_requests=len(starts),historical_reserved_usd=sum(s['reserved_usd'] for s in starts),
        gold_sha256=digest(COMPACT_RUN/'GOLD.json'),
        adapter_sha256={name:digest(Path(__file__).parent/name) for name in ADAPTERS},
        formal_env_sha256=digest(env) if env.exists() else None,
        schema_policy='Existing FullOutlineDraft/DirectOutlineDraft and validate_learning unchanged; no json_object or text-repair downgrade')
    write_json(directory/'PROTOCOL.json',protocol);write_json(directory/'SOURCE_REFERENCE.json',ref)
    write_json(directory/'STATUS.json',dict(status='proposal_only',new_real_http_attempts=0,
        inherited_real_http_attempts=len(starts),inherited_reserved_usd=ref['historical_reserved_usd'],
        currency='USD',shared_reservation_stop=10,waiting_for_execution_instruction=True))
    verify_reference(directory)
    print('Retry proposal prepared; thinking disabled; original USD10 retained; new real requests0',flush=True)


class RetryRunner(Runner):
    def __init__(self,directory=RETRY_RUN):
        self.retry_directory=Path(directory)
        require_retry_authorization(self.retry_directory)
        source,ref=verify_reference(self.retry_directory)
        # Source paths stay unchanged so frozen Source IDs/Gold remain valid.
        super().__init__(source)
        self.live=self.retry_directory/'live'
        for name in ('calls','jobs','judge'):(self.live/name).mkdir(exist_ok=True)
        self.protocol=json.loads((self.retry_directory/'PROTOCOL.json').read_text(encoding='utf-8'))
        self.ledger=RequestLedger(self.live/'requests.jsonl',limits_from_protocol(self.protocol))
        self.imported_ids=set(ref['historical_request_ids']);self.new_attempts=[]
        self.returned_models=json.loads((source/'live/summary.json').read_text(encoding='utf-8')).get('returned_models',{})

    def client(self,kind,task,budget):
        if kind!='generation' or task!='D01-A':raise EvaluationStopped('This retry permits only D01-A; Judge/D01-C are disabled')
        return super().client(kind,task,budget)

    def before_http(self,kind,task):
        require_retry_authorization(self.retry_directory)
        if kind!='generation' or task!='D01-A':raise EvaluationStopped('Out-of-scope retry request')
        if self.fatal:raise EvaluationStopped('Retry stopped: '+self.fatal)
        fresh=[e for e in self.ledger.events if e['event']=='start' and e['request_id'] not in self.imported_ids]
        if len(fresh)>=3:raise EvaluationStopped('One smoke permits at most3 explicit protocol attempts')

    def on_provider_result(self,status,data,classification):
        usage=(data.get('usage') or {}) if isinstance(data,dict) else {}
        reasoning=(usage.get('completion_tokens_details') or {}).get('reasoning_tokens')
        choices=data.get('choices',[]) if isinstance(data,dict) else []
        self.new_attempts.append(dict(http_status=status,classification=classification,
            reasoning_tokens=reasoning,finish_reasons=[c.get('finish_reason') for c in choices]))
        if any(c.get('finish_reason')=='length' for c in choices):self.fatal='output_truncated'
        if (isinstance(reasoning,int) and reasoning>0) or any(c.get('message',{}).get('reasoning_content') for c in choices):
            self.fatal='thinking_not_disabled'
        if status>=400 and not (status==400 and classification and classification['allow_fallback']):
            self.fatal=(classification or {}).get('category') or f'provider_HTTP_{status}'

    def run_once(self):
        marker=self.retry_directory/'D01-A-started.json'
        # Persist before requests; rerunning a failed/incomplete smoke is prohibited.
        with marker.open('x',encoding='utf-8') as f:
            json.dump(dict(started_at=datetime.now(timezone.utc).isoformat(),task='D01-A'),f)
        FUNCTION_OUTPUT_PROVIDERS.clear()
        task=next(t for t in self.plan['tasks'] if t['task_id']=='D01-A')
        try:
            result=self.generate(task)
            if result['status']!='completed' or not result['job']['candidates']:
                raise EvaluationStopped('Smoke produced no complete validated formal result')
            successes=[a for a in self.new_attempts if a['http_status']==200]
            if not successes or any(a['reasoning_tokens'] is None for a in successes):
                raise EvaluationStopped('thinking disabled sent, but provider did not report enough usage to verify its effect')
        except Exception as exc:
            # Persist only in the new run; Runner.save_stop targets the old source directory.
            write_json(self.live/'summary.json',self.summary())
            write_json(self.retry_directory/'RESULT.json',dict(status='stopped',task='D01-A',
                error_type=type(exc).__name__,error=str(exc) if isinstance(exc,EvaluationStopped) else None,
                attempts=self.new_attempts,D01_C_executed=False,judge_executed=False))
            raise
        write_json(self.retry_directory/'RESULT.json',dict(status='D01-A_smoke_success_only',task='D01-A',
            actual_new_requests=result['actual_requests'],formal_candidates=len(result['job']['candidates']),
            thinking_wire='disabled',provider_reported_reasoning_tokens=0,structural_validation='passed',
            D01_C_executed=False,judge_executed=False,shared_budget_summary=self.summary()))
        return result


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('command',choices=['prepare']);parser.parse_args()
    prepare() # No live-execution CLI is exposed in the proposal stage.
