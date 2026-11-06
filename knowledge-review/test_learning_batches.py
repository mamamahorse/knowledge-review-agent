"""Bounded sequential learning: controlled models and isolated source archives."""
from copy import deepcopy
from contextlib import closing
import json
import shutil
import sqlite3
import unittest
from unittest.mock import patch

from generation_budget import GenerationBudget
from knowledge_tools import KnowledgeTools
from embedding_config import select_embedding_config
from learning_batches import plan_batches
from learning_planner import PlanningStore, PlanningLimit, PlanningStopped, run_planning, validate_step, PlanningStep
from learning_pipeline import ExerciseStep, resolve_exercise, commit_exercise
from practice_store import archived_sources, public_session
from source_reader import read_lines, freeze, snapshot
from sources import Source
from structured_recovery import current_guard
from test_support import temporary_notes


def controlled(client,model,messages,schema,**kwargs):
    current_guard()()
    state=json.loads(messages[1]['content'])
    refs=list(state['evidence'])
    stage=state.get('stage','planning')
    if stage=='planning':
        data=dict(action='save',complete=True,items=[dict(id='a',ability='Explain the conditions',
            conditions=['Retain conditions'],evidence_ids=refs)],dispositions=[dict(unit_id=u['unit_id'],
            status='analyzed',reason='Actual batch source read.',evidence_ids=refs) for u in state['units']])
    elif stage=='refine':
        goal=dict(id='g',ability_ids=['a'],reason='One assessable ability in this batch.',learning=dict(
            knowledge=dict(name='Conditions',statement='Conditions and exceptions.',content_tags=[],evidence_ids=refs),
            target=dict(title='Explain conditions',capability='explain',description='Explain both conditions.',
                        scope='Supplied conditions',criteria=['Explain condition one','Explain condition two'])))
        data=dict(action='save',complete=True,goals=[goal],abilities=[dict(ability_id='a',status='addressed',
            goal_ids=['g'],reason='Both conditions retained.')])
    elif stage=='review':
        data=dict(action='save',complete=True,reviews=[dict(goal_id='g',decision='kept',replacement_ids=['g'],
            reason='Reviewed this batch; no cross-batch claim.')])
    else:
        data=dict(action='save',exercise=dict(objective='Explain conditions',question='Explain both conditions.',
            rubric_points=['First condition','Second condition'],criterion_indices=[1,2],evidence_ids=refs))
    step=schema.model_validate(data); kwargs['validator'](step)
    return step


class LearningBatchTests(unittest.TestCase):
    def setUp(self):
        # Product/business fixture; native request semantics have dedicated SDK tests.
        from test_learning_reading_fixture import prepared_reading
        reading=patch('learning_runtime.ReadingRuntime.run',new=prepared_reading)
        reading.start(); self.addCleanup(reading.stop)
        self.temp=temporary_notes(); self.root=self.temp.__enter__()
        self.addCleanup(self.temp.__exit__,None,None,None)
        self.addCleanup(self.clean_dirs)
        self.store=PlanningStore(self.root/'practice.sqlite3')
        path=self.root/'note.md'; path.write_text('# One\nCondition one.\n# Two\nCondition two.',encoding='utf-8')
        self.source=Source('version',path,path.read_text(encoding='utf-8'))
        settings=patch('learning_planner.model_settings',return_value=('test',dict(api_key='test')))
        settings.start(); self.addCleanup(settings.stop)
        specs=[dict(index=1,line_start=1,line_end=2,job_id=None),dict(index=2,line_start=3,line_end=4,job_id=None)]
        with patch('learning_batches.plan_batches',return_value=deepcopy(specs)):
            self.identity=self.store.create(self.source,'Learn conditions','batch-test',formalize=True,max_requests=10)

    def clean_dirs(self):
        for path in self.root.iterdir():
            if path.is_dir(): shutil.rmtree(path)

    def run_job(self,**kwargs):
        with patch('learning_planner.request_structured',side_effect=controlled):
            return run_planning(self.store,self.identity,lambda:self.source,**kwargs)

    def test_sequential_batches_old_batch_question_and_no_implicit_advance(self):
        first=self.run_job()
        self.assertEqual((first['status'],first['calls_reserved']),('waiting_next',3))
        self.assertIsNone(first['batches'][1]['job_id'])
        child=self.store.get(first['batches'][0]['job_id'])
        self.assertNotIn('text',child['inputs']['snapshot'])
        archived=archived_sources([child['inputs']['snapshot']])[0]
        self.assertFalse(hasattr(archived,'text'))
        self.assertEqual(read_lines(archived,3,4),'# Two\nCondition two.')
        # Already completed batches can produce questions while the remainder waits.
        first=self.run_job(question_goal_id='g',question_batch=1)
        self.assertEqual(first['status'],'waiting_next')
        saved=self.store.get(first['batches'][0]['job_id'])
        session=self.store.practice.get(saved['questions']['g']['session_id'])
        self.assertNotIn('text',session['snapshots'][0])
        self.assertIn('Condition one.',public_session(session)['questions'][0]['citations'][0]['excerpt'])
        with patch('learning_planner.request_structured',side_effect=AssertionError('must not advance')):
            run_planning(self.store,self.identity,lambda:self.source)
        final=self.run_job(next_batch=2)
        self.assertEqual((final['status'],final['calls_reserved']),('completed',7))
        self.assertEqual(saved,self.store.get(first['batches'][0]['job_id']))
        second=self.store.get(final['batches'][1]['job_id'])
        self.assertTrue(all(ref['citation']['line_start']>=3 for ref in second['evidence'].values()))
        with patch('learning_planner.request_structured',side_effect=AssertionError('duplicate click')):
            again=run_planning(self.store,self.identity,lambda:self.source,next_batch=2,additional_calls=9)
        self.assertEqual(again,final)

    def test_shared_allowance_pause_resume_and_parent_transaction_revocation(self):
        state=self.store.get(self.identity); state['call_limit']=4; self.store.save(state)
        self.run_job()
        before=self.store.get(self.identity)
        with patch('learning_planner.model_settings',return_value=('changed',dict(api_key='test'))), \
             self.assertRaisesRegex(ValueError,'模型连接'):
            self.run_job(next_batch=2)
        self.assertEqual(self.store.get(self.identity),before)
        with self.assertRaises(PlanningLimit): self.run_job(next_batch=2)
        parent=self.store.get(self.identity)
        self.assertEqual((parent['status'],parent['calls_reserved']),('paused',4))
        child=self.store.get(parent['batches'][1]['job_id'])
        self.assertEqual(child['stage'],'refine')
        final=self.run_job(additional_calls=2)
        self.assertEqual((final['status'],final['calls_reserved']),('completed',6))
        child=self.store.get(final['batches'][1]['job_id'])
        with self.assertRaisesRegex(ValueError,'父任务'):
            run_planning(self.store,child['id'],lambda:self.source)
        child.update(stage='question',question_goal_id='g',status='running',attempt='late')
        self.store.save(child)
        parent=self.store.get(self.identity); parent.update(status='running'); self.store.save(parent)
        self.store.pause(self.identity)
        evidence=list(child['evidence'])
        step=ExerciseStep(action='save',exercise=dict(objective='Explain',question='Explain conditions.',
            rubric_points=['One','Two'],criterion_indices=[1,2],evidence_ids=evidence))
        question=resolve_exercise(step,child,archived_sources([child['inputs']['snapshot']])[0])
        with self.assertRaises(PlanningStopped): commit_exercise(self.store,child,self.source,question,'late')
        with self.store.practice.transaction() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM practice_sessions').fetchone()[0],0)

    def test_auxiliary_only_goal_rejected_and_archive_corruption_detected(self):
        state=self.run_job(); child=self.store.get(state['batches'][0]['job_id'])
        from learning_planner import remember
        archived=archived_sources([child['inputs']['snapshot']])[0]
        tools=KnowledgeTools([archived],select_embedding_config(),reading_range=(1,2))
        remember(child,archived,tools.execute('read_source',dict(source_id=archived.source_id,line_start=3,line_end=4)))
        auxiliary=[key for key,ref in child['evidence'].items() if ref['citation']['line_start']>=3]
        with self.assertRaisesRegex(ValueError,'辅助阅读'):
            validate_step(PlanningStep(action='save',items=[dict(id='outside',ability='Another chapter',
                conditions=[],evidence_ids=auxiliary)]),child)
        with closing(sqlite3.connect(archived.archive)) as db, db:
            db.execute("UPDATE lines SET text='corrupted' WHERE number=3")
        with self.assertRaisesRegex(ValueError,'损坏'): read_lines(archived,3,4)

    def test_real_budget_partition_has_no_gaps_and_preserves_table_header(self):
        text='# Capacity\n| Item | Meaning |\n| --- | --- |\n'+'\n'.join(
            f'| {i} | '+('condition '*35)+'|' for i in range(120))
        source=Source('large',self.root/'large.md',text)
        plan=plan_batches(source,'Learn conditions','zh',GenerationBudget(32768,2048,1024),model='test')
        self.assertGreater(len(plan),1)
        self.assertEqual(plan[0]['line_start'],1)
        self.assertEqual(plan[-1]['line_end'],len(text.splitlines()))
        self.assertTrue(all(a['line_end']+1==b['line_start'] for a,b in zip(plan,plan[1:])))
        archive=freeze(source,self.root/'learning-sources')
        later=plan[-1]
        tools=KnowledgeTools([archive],select_embedding_config(),reading_range=(later['line_start'],later['line_end']))
        read=tools.execute('read_source',dict(source_id='large',line_start=later['line_start'],line_end=later['line_end']))
        self.assertTrue(any('Item' in chunk.text for chunk in read.visible_chunks))
        self.assertNotIn('text',snapshot(archive))


if __name__=='__main__': unittest.main()
