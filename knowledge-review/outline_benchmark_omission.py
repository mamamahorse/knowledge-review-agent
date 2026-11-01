"""Rerun the small comparison with the explicitly authorized V2 risk revision.

New input copies retain frozen bytes. Generation reads the original source paths
to retain Gold source IDs. Historical run files and their old code stay intact.
"""

import argparse
from copy import deepcopy
from datetime import datetime,timezone
import json
from pathlib import Path
from unittest.mock import patch

from outline_benchmark import CODE_FILES,load_source,plan_one
from outline_benchmark_compact import COMPACT_RUN
from outline_benchmark_guard import require_approval,review_digest,EvaluationStopped
from outline_benchmark_live import Runner
from outline_benchmark_pair import PairRunner,PAIR_RUN,ALLOWED_TASKS,run as run_pair
from outline_benchmark_prepare import write_json,digest
from outline_v2 import DIRECT_STRATEGY

OMISSION_RUN=PAIR_RUN.parent/'outline-v2-20261027-omission-pair'


def verify_history(before):
    for root,files in before['history_sha256'].items():
        for name,expected in files.items():
            if digest(Path(root)/name)!=expected:raise EvaluationStopped('Historical file changed: '+name)
    for name,expected in before['adapter_sha256'].items():
        if digest(Path(__file__).parent/name)!=expected:raise EvaluationStopped('Historical adapter changed: '+name)


def prepare(directory=OMISSION_RUN):
    directory=Path(directory);before=json.loads((directory/'BEFORE_CHANGE.json').read_text(encoding='utf-8'))
    if (directory/'PROTOCOL.json').exists():raise EvaluationStopped('Already prepared; never overwrite a run')
    verify_history(before)
    for name,expected in before['previous_core_sha256'].items():
        if digest(directory/'baseline-core'/name)!=expected:raise EvaluationStopped('Old core snapshot changed')
        if name not in before['authorized_core_change'] and digest(Path(name))!=expected:
            raise EvaluationStopped('Unapproved core change: '+name)
    # Copy, never edit, approved Gold/raw/canonical bytes. Runtime source paths
    # remain the original paths through generate_once's narrow loader binding.
    for name in ('corpus.json','GOLD.json','RUBRIC.md','environment.json'):
        (directory/name).write_bytes((COMPACT_RUN/name).read_bytes())
    corpus=json.loads((directory/'corpus.json').read_text(encoding='utf-8'))
    paths=set()
    for doc in corpus['documents']:
        paths.add(doc['source_file']);paths.update(item['path'] for item in doc['raw_files'])
        if doc.get('document_snapshot'):paths.add(doc['document_snapshot'])
    for name in paths:
        origin=(COMPACT_RUN/name).resolve();origin.relative_to(COMPACT_RUN.resolve())
        target=(directory/name).resolve();target.relative_to(directory.resolve())
        target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(origin.read_bytes())
    protocol=deepcopy(json.loads((PAIR_RUN/'PROTOCOL.json').read_text(encoding='utf-8')))
    protocol.update(version=6,run_id=directory.name,status='prepared',
        algorithm_revision=dict(v1='unchanged',v2=DIRECT_STRATEGY,
            omission_risk='possible missed targets in provided PRIMARY source; may split',
            missing_context='external knowledge absent from source; record only, never split by itself'),
        source_identity_policy='Generation loads original frozen source paths; new copies only bind current code/input approval',
        prior_failures_preserved=True)
    old_plan=json.loads((COMPACT_RUN/'planning.json').read_text(encoding='utf-8'));docs={d['id']:d for d in corpus['documents']}
    tasks=[]
    for task_id in ALLOWED_TASKS:
        doc_id,arm=task_id.split('-')
        if arm in ('C','D'):
            task=plan_one(COMPACT_RUN,docs[doc_id],arm,model_options={'base_url':protocol['generation']['endpoint']})
            task['task_id']=task_id
        else:task=deepcopy(next(t for t in old_plan['tasks'] if t['task_id']==task_id))
        tasks.append(task)
    write_json(directory/'planning.json',dict(version=3,stage='offline_replanned_authorized_V2_revision',
        tasks=tasks,code_sha256={name:digest(Path(name)) for name in CODE_FILES},live_model_requests=0))
    write_json(directory/'PROTOCOL.json',protocol)
    write_json(directory/'gold-approved.json',dict(approved_by='user',review_digest=review_digest(directory),
        original_gold_approval=json.loads((COMPACT_RUN/'gold-approved.json').read_text(encoding='utf-8')),
        unchanged_gold_sha256=digest(directory/'GOLD.json'),
        code_change_authorization='User explicitly requested omission_risk/missing_context revision and rerun; unchanged approved Gold reused'))
    history=PAIR_RUN/'live/requests.jsonl';(directory/'live').mkdir()
    (directory/'live/requests.jsonl').write_bytes(history.read_bytes())
    adapters=['outline_benchmark_omission.py','test_outline_benchmark_omission.py','test_outline_v2.py']
    write_json(directory/'RUN_REFERENCE.json',dict(before_change_sha256=digest(directory/'BEFORE_CHANGE.json'),
        history_sha256=digest(history),historical_requests=sum(json.loads(l)['event']=='start' for l in history.read_text(encoding='utf-8').splitlines()),
        current_adapter_sha256={n:digest(Path(n)) for n in adapters},
        frozen_gold_sha256=before['old_gold_sha256']))
    write_json(directory/'STATUS.json',dict(status='prepared',new_real_http_attempts=0))
    verify_revision(directory)


def verify_revision(directory=OMISSION_RUN):
    directory=Path(directory);require_approval(directory)
    ref=json.loads((directory/'RUN_REFERENCE.json').read_text(encoding='utf-8'))
    if digest(directory/'BEFORE_CHANGE.json')!=ref['before_change_sha256']:raise EvaluationStopped('Baseline reference changed')
    before=json.loads((directory/'BEFORE_CHANGE.json').read_text(encoding='utf-8'));verify_history(before)
    for name,expected in before['previous_core_sha256'].items():
        if digest(directory/'baseline-core'/name)!=expected:raise EvaluationStopped('Old code archive changed')
    for name,expected in ref['current_adapter_sha256'].items():
        if digest(Path(name))!=expected:raise EvaluationStopped('New adapter changed: '+name)
    env=Path('.env')
    if (digest(env) if env.exists() else None)!=before['formal_env_sha256']:raise EvaluationStopped('Formal .env changed')
    if digest(directory/'GOLD.json')!=ref['frozen_gold_sha256']:raise EvaluationStopped('Frozen Gold changed')
    return ref


class RevisedPairRunner(PairRunner):
    def __init__(self,directory=OMISSION_RUN):
        self.work=Path(directory);verify_revision(self.work)
        validation=json.loads((self.work/'OFFLINE_VALIDATION.json').read_text(encoding='utf-8'))
        if validation['status']!='passed':raise EvaluationStopped('Offline checks not passed')
        Runner.__init__(self,self.work)
        self.returned_models=json.loads((PAIR_RUN/'live/summary.json').read_text(encoding='utf-8'))['returned_models']
        self.gold=json.loads((self.work/'GOLD.json').read_text(encoding='utf-8'))['goals']
        self.rubric=(self.work/'RUBRIC.md').read_text(encoding='utf-8')
        self.phase='prepared';self.scored={};self.generated={}

    def before_http(self,kind,task):
        verify_revision(self.work)
        if self.fatal:raise EvaluationStopped(self.fatal)
        if kind=='generation' and task not in ALLOWED_TASKS:raise EvaluationStopped('Out-of-scope generation')
        if kind=='judge' and not any(task.startswith(t+':') for t in ['D01-A','D01-C','D04-B','D04-D']):
            raise EvaluationStopped('Out-of-scope judge')

    def generate_once(self,task_id):
        with patch('outline_benchmark_live.load_source',side_effect=lambda directory,doc:load_source(COMPACT_RUN,doc)):
            return super().generate_once(task_id)


def run(directory=OMISSION_RUN):
    verify_revision(directory)
    with patch('outline_benchmark_pair.PairRunner',RevisedPairRunner):run_pair(directory)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('command',choices=['prepare','run']);args=parser.parse_args()
    prepare() if args.command=='prepare' else run()
