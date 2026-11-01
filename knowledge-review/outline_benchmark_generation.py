"""Latest-source, serial generation only. No Judge import, execution or scoring gate."""

import argparse
from collections import Counter
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
from importlib.metadata import version
import json
from pathlib import Path
import time
from unittest.mock import patch

import httpx
from openai import OpenAI, APIConnectionError, APITimeoutError, APIStatusError
from pydantic import ValidationError

from generation_budget import GenerationBudget, BudgetError
from model_client import model_settings
from outline_benchmark import OBJECTIVE, load_source, snapshot, validate_artifacts, reduction_minimum
from outline_benchmark_compact import COMPACT_RUN
from outline_benchmark_forced_smoke import ForcedAuditTransport
from outline_benchmark_guard import EvaluationStopped, require_approval, review_digest
from outline_benchmark_live import Runner, ForwardTransport
from outline_benchmark_prepare import digest, write_json
from structured_output import request_structured as project_request_structured
from structured_recovery import audit_scope

ROOT = Path(__file__).resolve().parent
DEFAULT_RUN = COMPACT_RUN.parent / 'outline-v2-20261028-generation-only'
HISTORY_RUN = COMPACT_RUN.parent / 'outline-v2-20261027-local-retry2'
BUDGET = GenerationBudget(131072, 12288, 2048, 'latest-source isolated comparison; explicit fixed budget')
TRANSIENT_HTTP = {408, 500, 502, 503, 504}


def failure_info(exc):
    chain = []; current = exc
    while current is not None and id(current) not in {id(v) for v in chain}:
        chain.append(current); current = current.__cause__
    guard = next((v for v in chain if isinstance(v, EvaluationStopped)), None)
    if guard:
        local = 'Task actual-request cap' in str(guard)
        return dict(category='task_limit' if local else 'execution_guard', retryable=False, fatal=not local)
    if any(isinstance(v, BudgetError) for v in chain):
        return dict(category='request_budget', retryable=False, fatal=False)
    if getattr(exc,'structured_recovery_exhausted',False):
        stage=exc.structured_recovery_stage
        category='format' if stage=='parse' else 'schema' if stage=='schema' else 'network' if stage=='request' else \
            'citation' if any(s in str(exc) for s in ('引用','依据','证据ID','citation')) else 'validation'
        return dict(category=category,retryable=False,fatal=False)
    if isinstance(exc, APIStatusError):
        return dict(category='provider_http', http_status=exc.status_code,
                    retryable=exc.status_code in TRANSIENT_HTTP, fatal=exc.status_code not in TRANSIENT_HTTP)
    if isinstance(exc, (httpx.TransportError, APIConnectionError, APITimeoutError)):
        return dict(category='network', retryable=True, fatal=False)
    if isinstance(exc, ValidationError):
        category = 'format' if any(e['type'] == 'json_invalid' for e in exc.errors()) else 'schema'
        return dict(category=category, retryable=True, fatal=False)
    if isinstance(exc, RuntimeError):
        if 'truncated' in str(exc).lower():
            return dict(category='output_truncated', retryable=True, fatal=False)
        if any(s in str(exc).lower() for s in ('structured result', 'no result choices')):
            return dict(category='structured_protocol', retryable=True, fatal=False)
    if isinstance(exc, ValueError):
        citation = any(s in str(exc) for s in ('引用', '依据', '证据ID', 'cited', 'citation'))
        return dict(category='citation' if citation else 'validation', retryable=True, fatal=False)
    return dict(category='unexpected', retryable=False, fatal=True)


def plan_task(doc, algorithm, endpoint, run_name):
    from outline_context import plan_outline
    from outline_v2 import plan_direct
    source = load_source(COMPACT_RUN, doc); saved = snapshot(doc, source)
    with patch.object(GenerationBudget, 'from_env', return_value=BUDGET), \
         patch('model_client.model_settings', return_value=('deepseek-flash', {'base_url': endpoint})), \
         patch('outline_service.model_settings', return_value=('deepseek-flash', {'base_url': endpoint})), \
         patch('practice_model.OpenAI', side_effect=AssertionError('Planning must remain offline')):
        if algorithm == 'v1':
            sections, batches, plan = plan_outline([source], [saved], OBJECTIVE, 'zh', BUDGET)
            hierarchical = bool(plan['documents'] and plan['documents'][0]['mode'] == 'hierarchical')
            leaves = [b for b in batches if b.get('status', 'pending') == 'pending'] if hierarchical else []
            sizes = [sum(b['section_id'] == s['id'] for b in leaves) for s in sections]
            overview = len(leaves) + sum(reduction_minimum(n) for n in sizes) + reduction_minimum(sum(n > 0 for n in sizes))
        else:
            sections, batches, plan = plan_direct([source], [saved], OBJECTIVE, 'zh', BUDGET.context_tokens)
            overview = 0
    arm = 'B' if algorithm == 'v1' else 'D'
    cap = 24 if algorithm == 'v1' and doc['id'] in ('D05', 'D06') else 16
    return dict(task_id=f'{run_name}:{doc["id"]}:{algorithm}', document_id=doc['id'], algorithm=algorithm,
        arm=arm, context_tokens=BUDGET.context_tokens, output_tokens=BUDGET.output_tokens,
        source_id=source.source_id, actual_request_limit=cap, sections_plan=sections, batches_plan=batches,
        plan=plan, overview_logical_lower_bound=overview,
        generation_logical_lower_bound=overview + sum(b.get('status', 'pending') == 'pending' for b in batches),
        dynamic_splits='unknown; latest algorithm, no count-based split')


def prepare(directory=DEFAULT_RUN):
    import secrets
    directory = Path(directory).resolve()
    if directory.exists():
        raise EvaluationStopped('Use a fresh run directory; historical outputs cannot be overwritten')
    approval = json.loads((COMPACT_RUN / 'gold-approved.json').read_text(encoding='utf-8'))
    if approval['approved_by'] != 'user' or approval['review_digest'] != review_digest(COMPACT_RUN):
        raise EvaluationStopped('Original approved Gold/materials changed')
    corpus = validate_artifacts(COMPACT_RUN)
    model, options = model_settings()
    old = json.loads((HISTORY_RUN / 'PROTOCOL.json').read_text(encoding='utf-8'))
    if model != old['generation']['model'] or options.get('base_url') != old['generation']['endpoint']:
        raise EvaluationStopped('Model/endpoint must match previously authorized experiment')
    directory.mkdir(); (directory / 'internal').mkdir(); (directory / 'live').mkdir()
    names = {'corpus.json', 'GOLD.json', 'RUBRIC.md', 'environment.json'}
    for doc in corpus['documents']:
        names.add(doc['source_file']); names.update(v['path'] for v in doc['raw_files'])
        if doc.get('document_snapshot'): names.add(doc['document_snapshot'])
    for name in names:
        target = (directory / name).resolve(); target.relative_to(directory.resolve())
        target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes((COMPACT_RUN / name).read_bytes())
    # Every root Python source/test is pinned, including current schema dependencies and adapters.
    code = {p.name: digest(p) for p in sorted(ROOT.glob('*.py'))}
    archive = directory / 'internal' / 'code'; archive.mkdir()
    for name in code: (archive / name).write_bytes((ROOT / name).read_bytes())
    tasks = []
    for index, doc in enumerate(corpus['documents']):
        for algorithm in (('v1', 'v2') if index % 2 == 0 else ('v2', 'v1')):
            tasks.append(plan_task(doc, algorithm, options['base_url'], directory.name))
    goals=json.loads((directory/'GOLD.json').read_text(encoding='utf-8'))['goals']
    identities={t['document_id']:t['source_id'] for t in tasks}
    if any(g['source_id'] != identities[g['document_id']] for g in goals):
        raise EvaluationStopped('Latest source identities do not match frozen Gold; no reannotation allowed')
    protocol = dict(version=8, run_id=directory.name, request_execution='serial', max_in_flight=1,
        generation={**old['generation'], 'context_tokens':131072, 'output_tokens':12288,
            'safety_margin':2048, 'task_request_limit_overrides':{t['task_id']:t['actual_request_limit'] for t in tasks}},
        judge=dict(enabled=False, cap_total=96), all_actual_requests_cap=224,
        soft_conservative_reservation_usd=10, per_request_reservation_usd=old['per_request_reservation_usd'],
        price=old['price'], strict_protocol_errors=True,
        acceptance='Actual thinking=disabled; full current schema and citation checks; missing reasoning usage remains unknown',
        local_retry='Project JSON repair then bounded validation feedback; initial plus at most two retries; SDK retries=0',
        scope='Six approved compact documents, both current algorithms, fixed128k/12k; no auto Judge or default switch',
        source_identity_policy='Load original approved paths to preserve frozen source IDs',
        code_snapshot_sha256=sha256(json.dumps(code, sort_keys=True).encode()).hexdigest(),
        structured_output_recovery=dict(library='json-repair',library_version=version('json-repair'),
            max_retries=2,audit_directory='live/structured-output',business_validation='original validators retained'),
        latest_source_authorization='Current user explicitly requested latest code,131072/12288, generation and offline export only')
    write_json(directory / 'PROTOCOL.json', protocol)
    write_json(directory / 'planning.json', dict(version=4, code_sha256=code, tasks=tasks,
        logical_lower_bound_is_not_actual_cost=True, live_model_requests=0))
    # This is derived authorization, not a claim that the user reviewed a new Gold version.
    write_json(directory / 'gold-approved.json', dict(approved_by='user', review_digest=review_digest(directory),
        original_gold_approval=approval, unchanged_gold_sha256=digest(COMPACT_RUN / 'GOLD.json'),
        workflow_authorization=protocol['latest_source_authorization']))
    history = HISTORY_RUN / 'live' / 'requests.jsonl'
    (directory / 'live' / 'requests.jsonl').write_bytes(history.read_bytes())
    histories = {str(p):digest(p) for parent in COMPACT_RUN.parent.glob('outline-v2-*') if parent != directory
                 for p in parent.rglob('*') if p.is_file()}
    write_json(directory / 'internal' / 'history_sha256.json', histories)
    groups = ['X', 'Y']; secrets.SystemRandom().shuffle(groups)
    mapping = dict(algorithm_to_group=dict(zip(('v1', 'v2'), groups)), candidate_order_seed=secrets.token_hex(16),
                   tasks={t['task_id']:dict(document_id=t['document_id'], group=dict(zip(('v1','v2'),groups))[t['algorithm']]) for t in tasks})
    write_json(directory / 'internal' / 'anonymous_mapping.json', mapping)
    write_json(directory / 'STATUS.json', dict(status='prepared', judge_enabled=False, new_real_http_attempts=0))
    require_approval(directory)
    print(json.dumps(dict(tasks=len(tasks), logical_lower_bound=sum(t['generation_logical_lower_bound'] for t in tasks),
        budget=asdict(BUDGET), task_lower_bounds=[(t['document_id'],t['algorithm'],t['generation_logical_lower_bound']) for t in tasks])))


class GenerationTransport(ForcedAuditTransport):
    def handle_request(self, request):
        response = super().handle_request(request)
        if response.status_code in TRANSIENT_HTTP and self.runner.fatal in (
                f'provider HTTP {response.status_code}', f'provider_HTTP_{response.status_code}'):
            self.runner.fatal = None
        return response


class GenerationRunner(Runner):
    def __init__(self, directory=DEFAULT_RUN):
        super().__init__(directory)
        if self.protocol['judge'].get('enabled') is not False:
            raise EvaluationStopped('Automatic Judge must be explicitly disabled')
        self.last_failure = None; self.current_task = None; self.failures = []
        self.output_traces = {}
        self.baseline_ids = {e['request_id'] for e in self.ledger.events if e['event']=='start'}

    def before_http(self, kind, task):
        if kind != 'generation': raise EvaluationStopped('Judge requests are forbidden in this workflow')
        if task not in {v['task_id'] for v in self.plan['tasks']}: raise EvaluationStopped('Unplanned task')
        require_approval(self.directory)
        if self.fatal: raise EvaluationStopped(self.fatal)
        if self.protocol['generation']['context_tokens'] != 131072 or self.protocol['generation']['output_tokens'] != 12288:
            raise EvaluationStopped('Latest experiment requires131072/12288')

    def on_provider_result(self, status, data, classification):
        if status >= 400 and status not in TRANSIENT_HTTP: self.fatal = f'provider_HTTP_{status}'

    def client(self, kind, task, budget):
        self.before_http(kind, task)
        controls = self.protocol['generation']
        transport = GenerationTransport(ForwardTransport(httpx.Client(timeout=controls['timeout_seconds'], follow_redirects=False)),
                                        self, kind, task, budget)
        return OpenAI(api_key=self.options['api_key'], base_url=controls['endpoint'], max_retries=0,
            timeout=controls['timeout_seconds'], http_client=httpx.Client(transport=transport, follow_redirects=False))

    def record_failure(self, exc, phase):
        value = dict(task=self.current_task, phase=phase, error_type=type(exc).__name__, **failure_info(exc))
        starts=[e for e in self.ledger.events if e['event']=='start' and e['task']==self.current_task]
        value['request_id']=starts[-1]['request_id'] if starts and value['category'] not in ('execution_guard','task_limit','request_budget') else None
        # Exception strings may contain credentials/content; keep safe categories, not unrestricted repr.
        self.last_failure = value; self.failures.append(value)
        write_json(self.live / 'failures.json', self.failures)
        if value['fatal']: self.fatal = value['category']

    @contextmanager
    def model_scope(self, kind, task, budget):
        self.current_task = task
        def audit_event(event):
            if event['event']=='start':
                self.output_traces.setdefault(task,[]).append(event['trace_id'])
            if event['event'] != 'attempt_failed':
                return
            errors=event['errors']; message=' '.join(e.get('message','') for e in errors)
            phase=event['phase']
            starts=[e for e in self.ledger.events if e['event']=='start' and e['task']==task]
            latest=starts[-1] if starts else None
            ends=[e for e in self.ledger.events if e['event']=='end' and latest and e['request_id']==latest['request_id']]
            end=ends[-1] if ends else {}
            category='format' if phase=='parse' else 'schema' if phase=='schema' else \
                ('citation' if any(s in message for s in ('引用','依据','证据ID','citation')) else 'validation') \
                if phase=='business_validation' else 'network' if end.get('status')=='transport_error' else \
                'provider_http' if end.get('http_status',200)>=400 else 'output_truncated' if 'length' in end.get('finish_reasons',[]) else 'validation'
            value=dict(task=task,phase=phase,error_type=event['error_type'],category=category,
                retryable=event['retry'],fatal=False,request_id=latest['request_id'] if latest else None,
                structured_output_trace_id=event['trace_id'],correction_attempt=event['attempt'])
            self.last_failure=value; self.failures.append(value)
            write_json(self.live/'failures.json',self.failures)
        def request(*args, **kwargs):
            self.last_failure = None
            try: return project_request_structured(*args, **kwargs, protocol_policy='forced_tools')
            except Exception as exc:
                if self.last_failure and self.last_failure.get('structured_output_trace_id')==getattr(exc,'structured_output_trace_id',None):
                    self.last_failure.update(failure_info(exc))
                    if self.last_failure['fatal']: self.fatal=self.last_failure['category']
                    write_json(self.live/'failures.json',self.failures)
                else: self.record_failure(exc, 'model_result')
                raise
        with super().model_scope(kind, task, budget), \
             audit_scope(self.live/'structured-output',audit_event), \
             patch('practice_model.request_structured', request):
            yield

    def generate(self, task):
        from learning_store import LearningStore
        from library_store import LibraryStore
        from outline_store import OutlineStore
        from outline_service import run_outline, scope_fingerprint
        task_id = task['task_id']; self.current_task = task_id
        source = load_source(COMPACT_RUN, self.docs[task['document_id']]); snapshots = [snapshot(self.docs[task['document_id']], source)]
        practice = LearningStore(self.live / 'runs.sqlite3'); store = OutlineStore(practice)
        collection = LibraryStore(practice).create(task_id)['id']
        identity = store.create(collection, task_id, OBJECTIVE, 'zh', scope_fingerprint(snapshots), snapshots,
            deepcopy(task['sections_plan']), deepcopy(task['batches_plan']), deepcopy(task['plan']))
        attempt_dir = self.live / 'attempts' / task_id.split(':',1)[-1].replace(':','-'); attempt_dir.mkdir(parents=True)
        started = time.monotonic(); first = None; counts = Counter(); attempt = 0
        original_finish = store.finish_batch
        def finish(*args, **kwargs):
            nonlocal first
            ok = original_finish(*args, **kwargs)
            if ok and args[1] and first is None: first = time.monotonic() - started
            return ok
        print('START', task['document_id'], task['algorithm'], flush=True)
        while True:
            self.last_failure = None
            try:
                with self.model_scope('generation', task_id, BUDGET), patch.object(store, 'finish_batch', side_effect=finish):
                    run_outline(store, identity, lambda _:snapshots)
            except Exception as exc:
                if self.last_failure is None: self.record_failure(exc, 'task')
            job = store.get(identity)
            failed = [v for v in [*job['context_steps'], *job['batches']] if v['status']=='failed']
            for unit in failed: counts[unit['id']] += 1
            write_json(attempt_dir / f'{attempt:03}.json', dict(job=job, failure=self.last_failure, unit_failures=dict(counts)))
            if (job['status']=='completed' or self.fatal or not (self.last_failure or {}).get('retryable')
                    or not failed or any(max(counts[v['id']],v.get('calls',0)) >= 3 for v in failed)):
                break
            attempt += 1; time.sleep(min(attempt,2)); store.resume(identity)
            print('RETRY', task['document_id'], task['algorithm'], attempt, flush=True)
        elapsed = time.monotonic()-started
        result = dict(task_id=task_id, document_id=task['document_id'], status=job['status'], job=job,
            elapsed_seconds=elapsed, first_formal_candidate_seconds=first,
            completed_seconds=elapsed if job['status']=='completed' else None,
            actual_requests=sum(e['event']=='start' and e['task']==task_id for e in self.ledger.events),
            failures=[v for v in self.failures if v['task']==task_id], retry_resume_count=attempt,
            structured_output_trace_ids=self.output_traces.get(task_id,[]),
            source_sha256=self.docs[task['document_id']]['sha256'])
        path = self.live / 'jobs' / (task['document_id']+'-'+task['arm']+'.json')
        write_json(path, result); self.results.append(result)
        print('END', task['document_id'], task['algorithm'], job['status'], len(job['candidates']), result['actual_requests'], round(elapsed,3), flush=True)
        return result


def run(directory=DEFAULT_RUN):
    from outline_benchmark_export import export_comparison
    directory = Path(directory); require_approval(directory)
    if list((directory/'live'/'jobs').glob('*.json')):
        raise EvaluationStopped('Existing generation results retained; no implicit rerun')
    validation = json.loads((directory/'OFFLINE_VALIDATION.json').read_text(encoding='utf-8'))
    if validation.get('status') != 'passed': raise EvaluationStopped('Offline checks required')
    lock = directory / 'live' / 'running.lock'
    with lock.open('x', encoding='utf-8') as stream: stream.write(datetime.now(timezone.utc).isoformat())
    runner = None; error = None
    try:
        runner = GenerationRunner(directory)
        for task in runner.plan['tasks']:
            result = runner.generate(task)
            write_json(runner.live/'summary.json', runner.summary())
            export_comparison(directory)
            if runner.fatal: raise EvaluationStopped(runner.fatal)
        status = 'finished' if all(v['status']=='completed' for v in runner.results) else 'finished_with_partial_generation'
    except Exception as exc:
        status = 'stopped'; error = str(exc) if isinstance(exc,EvaluationStopped) else type(exc).__name__
    finally:
        try:
            if runner:
                write_json(runner.live/'summary.json',runner.summary())
            write_json(directory/'STATUS.json',dict(status=status,error=error,judge_enabled=False,
                context_tokens=131072, output_tokens=12288))
            export_comparison(directory)
            history=json.loads((directory/'internal/history_sha256.json').read_text(encoding='utf-8'))
            if any(digest(Path(path)) != expected for path,expected in history.items()):
                raise EvaluationStopped('Historical records changed during execution')
        finally:
            lock.unlink(missing_ok=True)
    print('FINISHED',status,error,flush=True)


if __name__ == '__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('command', choices=['prepare','run','export'])
    parser.add_argument('--directory', type=Path, default=DEFAULT_RUN); args=parser.parse_args()
    if args.command=='prepare': prepare(args.directory)
    elif args.command=='run': run(args.directory)
    else:
        from outline_benchmark_export import export_comparison
        export_comparison(args.directory)
