"""Recognize and retry V1 overview pointer validation failures, never relax them."""

from contextlib import contextmanager
from datetime import datetime,timezone
import json
from pathlib import Path
from unittest.mock import patch

from outline_benchmark_continue import ContinuationRunner
from outline_benchmark_local_retry import RETRY_RUN
from outline_benchmark_omission import verify_revision
from outline_benchmark_prepare import write_json,digest
from outline_benchmark_guard import EvaluationStopped

LOCAL_OVERVIEW_ERRORS={'背景概述不合法。','背景概述依据超出了输入证据。'}


class OverviewRecoveryRunner(ContinuationRunner):
    @contextmanager
    def model_scope(self,kind,task,budget):
        from outline_context import execute_step
        def checked(*a,**kw):
            try:return execute_step(*a,**kw)
            except ValueError as exc:
                if str(exc) in LOCAL_OVERVIEW_ERRORS:self.failed_request(0,exc,True)
                raise
        with super().model_scope(kind,task,budget),patch('outline_context.execute_step',checked):yield


def diagnose(job,calls):
    from outline_context import DocumentBrief,pointers
    from outline_service import excerpt
    from practice_store import archived_sources
    from context import citation_is_visible
    source_map={s.source_id:s for s in archived_sources(job['snapshots'])}
    failed=next(s for s in job['context_steps'] if s['status']=='failed')
    dependencies=[next(s for s in job['context_steps'] if s['id']==d['step_id']) for d in failed['inputs']]
    visible=[excerpt(source_map[c['source_id']],c['line_start'],c['line_end']) for s in dependencies for c in pointers(s['result'])]
    latest=calls[-1];choice=latest['response']['choices'][0]
    result=DocumentBrief.model_validate_json(choice['tool_calls'][0]['function']['arguments'])
    invalid=[c.model_dump() for p in result.points for c in p.citations
        if not citation_is_visible(c.source_id,c.line_start,c.line_end,visible)]
    if not invalid:raise EvaluationStopped('Known local overview validation failure was not reproduced')
    return dict(kind='local_overview_pointer_validation',step_id=failed['id'],invalid_citations=invalid,
        allowed_ranges=[dict(source_id=c.source_id,line_start=c.line_start,line_end=c.line_end) for c in visible],
        request_id=latest['request_id'],reproduced_offline=True)


def run(directory=RETRY_RUN):
    directory=Path(directory);verify_revision(directory)
    output=directory/'continuation-v1';output.mkdir()
    path=directory/'live/jobs/D04-B.json';old=json.loads(path.read_text(encoding='utf-8'))
    write_json(output/'before.json',old)
    events=[json.loads(l) for l in (directory/'live/requests.jsonl').read_text(encoding='utf-8').splitlines()]
    starts=[e for e in events if e['event']=='start' and e['kind']=='generation' and e['task']=='D04-B']
    calls=[json.loads((directory/'live/calls'/(e['request_id']+'.json')).read_text(encoding='utf-8')) for e in starts]
    proof=diagnose(old['job'],calls);write_json(output/'LOCAL_FAILURE_PROOF.json',proof)
    write_json(output/'POLICY.json',dict(date='2026-10-28',authorized_by='user',scope='Continue known local failed V1 step with its remaining per-unit retries',
        validation_unchanged=True,code_sha256=digest(Path(__file__)),error_classification='Narrow two known ValueError messages only',
        maximum_retries_per_step=2,limits_unchanged=True))
    # Add an explicitly derived failure classification to the latest job view;
    # original API output and prior job snapshot above remain intact.
    old['last_local_failure']=dict(error_type='ValueError',retryable=True,origin='offline_reproduction_of_overview_citation_check')
    write_json(path,old)
    previous=(directory/'continuation/D04-B').resolve();archive=(directory/'continuation/D04-B-before-overview-classification').resolve()
    previous.relative_to(directory.resolve());archive.relative_to(directory.resolve())
    if archive.exists():raise EvaluationStopped('Do not overwrite previous continuation')
    previous.rename(archive)
    lock=directory/'live/running.lock'
    with lock.open('x',encoding='utf-8') as stream:stream.write(datetime.now(timezone.utc).isoformat())
    runner=None
    try:
        runner=OverviewRecoveryRunner(directory)
        for task in ('D01-A','D01-C','D04-B','D04-D'):
            runner.generated[task]=json.loads((runner.live/'jobs'/(task+'.json')).read_text(encoding='utf-8'))
            metrics=runner.live/'judge'/task/'metrics.json'
            if metrics.exists():runner.scored[task]=json.loads(metrics.read_text(encoding='utf-8'))
        b=runner.resume_saved('D04-B')
        if runner.fatal:raise EvaluationStopped(runner.fatal)
        if b['status']=='completed':runner.score('D04-B',b)
        runner.phase='bounded small comparison finished'
        runner.checkpoint('completed' if all(r['status']=='completed' for r in runner.generated.values()) else 'completed_with_partial_generation')
        write_json(output/'RESULT.json',dict(generation={k:v['status'] for k,v in runner.generated.items()},scored=list(runner.scored)))
    except Exception as exc:
        if runner:runner.checkpoint('stopped',str(exc) if isinstance(exc,EvaluationStopped) else type(exc).__name__)
        print('STOP',str(exc) if isinstance(exc,EvaluationStopped) else type(exc).__name__,flush=True)
    finally:lock.unlink(missing_ok=True)


if __name__=='__main__':run()
