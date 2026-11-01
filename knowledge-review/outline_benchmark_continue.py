"""Continue the saved small experiment; two retries per failed local unit.

Old counters/results remain; no regeneration of completed windows or rescoring
of existing semantic judgments. The continuation policy is saved before calls.
"""

from collections import Counter
from datetime import datetime,timezone
import json
from pathlib import Path
import time
from unittest.mock import patch

from generation_budget import GenerationBudget
from outline_benchmark_guard import EvaluationStopped
from outline_benchmark_local_retry import RETRY_RUN,LocalRetryRunner
from outline_benchmark_omission import verify_revision
from outline_benchmark_pair import validate_job
from outline_benchmark_prepare import write_json,digest


def failed_units(job):
    return [dict(id=b['id'],kind='batch',attempts=b.get('calls',0)) for b in job['batches'] if b['status']=='failed']+[
        dict(id=s['id'],kind='context',attempts=0) for s in job['context_steps'] if s['status']=='failed']


def allowance(job,counts):
    units=failed_units(job)
    return bool(units) and all(max(u['attempts'],counts.get(u['id'],1))<3 for u in units)


def validate_saved_candidates(result):
    """Allow explicitly partial jobs, but only candidates from completed leaves."""
    from practice_store import archived_sources
    from outline_service import LearningProposal,validate_learning,candidate_visible,excerpt
    job=result['job'];sources={s.source_id:s for s in archived_sources(job['snapshots'])}
    if job['status'] not in ('completed','failed','paused') or not job['candidates']:raise EvaluationStopped('No saved valid candidates to assess')
    for c in job['candidates']:
        b=next(b for b in job['batches'] if b['id']==c['batch_id'])
        if b['status']!='completed':raise EvaluationStopped('Only completed leaf candidates may be assessed')
        source=sources[b['source_id']]
        validate_learning(LearningProposal.model_validate(c['learning']),candidate_visible(c,job,source,excerpt(source,b['line_start'],b['line_end'])))
    return sources


class ContinuationRunner(LocalRetryRunner):
    def before_http(self,kind,task):
        super().before_http(kind,task)
        policy=json.loads((self.work/'continuation/POLICY.json').read_text(encoding='utf-8'))
        if digest(Path(__file__))!=policy['continuation_code_sha256']:raise EvaluationStopped('Continuation code changed')

    def resume_saved(self,task_id):
        from learning_store import LearningStore
        from outline_store import OutlineStore
        from outline_service import run_outline
        path=self.live/'jobs'/(task_id+'.json');result=json.loads(path.read_text(encoding='utf-8'))
        output=self.work/'continuation'/task_id;output.mkdir()
        write_json(output/'before.json',result)
        store=OutlineStore(LearningStore(self.live/'runs.sqlite3'));identity=result['job']['id']
        counts=Counter()
        for file in sorted((self.live/'task-attempts'/task_id).glob('*.json')):
            for u in failed_units(json.loads(file.read_text(encoding='utf-8'))['job']):counts[u['id']]+=1
        first=result['first_formal_candidate_seconds'];start=time.monotonic();base_elapsed=result['elapsed_seconds']
        before=sum(e['event']=='start' for e in self.ledger.events);original_finish=store.finish_batch
        def finish(*args,**kwargs):
            nonlocal first
            ok=original_finish(*args,**kwargs)
            if ok and args[1] and first is None:first=base_elapsed+time.monotonic()-start
            return ok
        task=next(t for t in self.plan['tasks'] if t['task_id']==task_id)
        budget=GenerationBudget(task['context_tokens'],4096,2048,'unchanged experiment budget')
        job=store.get(identity);failure=result.get('last_local_failure');resumes=0
        while job['status'] in ('failed','paused') and allowance(job,counts) and (failure or {}).get('retryable') and not self.fatal:
            self.phase='continue '+task_id;self.last_failure=None
            time.sleep(1);store.resume(identity);resumes+=1
            print('CONTINUE',task_id,resumes,flush=True)
            with self.model_scope('generation',task_id,budget),patch.object(store,'finish_batch',side_effect=finish):
                run_outline(store,identity,lambda _:result['job']['snapshots'])
            job=store.get(identity);failure=self.last_failure
            for u in failed_units(job):counts[u['id']]+=1
            write_json(output/(f'{resumes:02}.json'),dict(job=job,failure=failure,unit_failure_counts=dict(counts)))
        active=time.monotonic()-start
        result.update(status=job['status'],job=job,elapsed_seconds=base_elapsed+active,
            completed_seconds=base_elapsed+active if job['status']=='completed' else None,
            first_formal_candidate_seconds=first,
            actual_requests=result['actual_requests']+sum(e['event']=='start' for e in self.ledger.events)-before,
            continuation_resume_count=resumes,last_local_failure=failure,
            continuation_active_seconds=active,retry_granularity='per persisted failed unit; initial+2 max')
        write_json(path,result);self.generated[task_id]=result;self.checkpoint()
        print('CONTINUED',task_id,result['status'],len(job['candidates']),flush=True)
        return result


def run(directory=RETRY_RUN):
    directory=Path(directory);verify_revision(directory)
    output=directory/'continuation';output.mkdir() # exclusive: do not rerun this continuation
    for name in ('STATUS.json','live/summary.json'):
        (output/Path(name).name).write_bytes((directory/name).read_bytes())
    write_json(output/'POLICY.json',dict(authorized_by='user',instruction='继续中断的实验；局部失败允许2次重试',
        date='2026-10-28',continuation_code_sha256=digest(Path(__file__)),
        retry_granularity='Each failed persisted window/context step gets at most2 retries; keep all existing counts',
        previous_policy='Shared two resumes per task proved too restrictive for independent local failures',
        limits='Original task16, generation128, Judge96, total224, USD10 shared; no reset',
        semantic_policy='Never rejudge low/uncertain scores; partial candidates only from completed leaves'))
    lock=directory/'live/running.lock'
    with lock.open('x',encoding='utf-8') as stream:stream.write(datetime.now(timezone.utc).isoformat())
    runner=None
    try:
        runner=ContinuationRunner(directory)
        for task in ('D01-A','D01-C','D04-D'):
            runner.generated[task]=json.loads((runner.live/'jobs'/(task+'.json')).read_text(encoding='utf-8'))
        for task in ('D01-A','D01-C'):
            runner.scored[task]=json.loads((runner.live/'judge'/task/'metrics.json').read_text(encoding='utf-8'))
        starts=[e for e in runner.ledger.events if e['event']=='start']
        write_json(output/'BUDGET.json',dict(cumulative_reserved_usd=sum(e['reserved_usd'] for e in starts),
            remaining_generation=128-sum(e['kind']=='generation' for e in starts),
            remaining_judge=96-sum(e['kind']=='judge' for e in starts),new_allowance_granted=False))
        d=runner.resume_saved('D04-D')
        if runner.fatal:raise EvaluationStopped(runner.fatal)
        try:b=runner.generate_once('D04-B')
        except EvaluationStopped:
            if runner.fatal:raise
            b=json.loads((runner.live/'jobs/D04-B.json').read_text(encoding='utf-8'))
        if b['status']!='completed':b=runner.resume_saved('D04-B')
        runner.generated['D04-B']=b
        for task,result in [('D04-B',b),('D04-D',d)]:
            if not result['job']['candidates']:continue
            with patch('outline_benchmark_pair.validate_job',validate_saved_candidates):
                value=runner.score(task,result)
            value.update(generation_status=result['status'],partial_generation=result['status']!='completed')
            write_json(runner.live/'judge'/task/'metrics.json',value)
        runner.phase='small comparison continuation finished'
        runner.checkpoint('completed' if all(v['status']=='completed' for v in runner.generated.values()) else 'completed_with_partial_generation')
        write_json(output/'RESULT.json',dict(generation={k:v['status'] for k,v in runner.generated.items()},scored=list(runner.scored)))
    except Exception as exc:
        if runner:runner.checkpoint('stopped',str(exc) if isinstance(exc,EvaluationStopped) else type(exc).__name__)
        print('STOP',str(exc) if isinstance(exc,EvaluationStopped) else type(exc).__name__,flush=True)
    finally:lock.unlink(missing_ok=True)


if __name__=='__main__':run()
