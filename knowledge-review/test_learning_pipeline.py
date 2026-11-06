"""Focused controlled checks for the new complete learning path; no paid models."""

from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from learning_pipeline import (FormalStep, ExerciseStep, merge_formal, resolve_exercise,
                               commit_exercise, selected_evidence)
from learning_planner import PlanningStore, PlanningLimit, run_planning, remember
from knowledge_tools import KnowledgeTools
from embedding_config import select_embedding_config
from learning_service import enroll_question, start_review
from practice_model import validate_question, visual_receipts, visual_messages
from practice_store import public_session
from sources import Source, source_from_document
from structured_recovery import current_guard
from test_support import temporary_notes


class LearningPipelineTests(unittest.TestCase):
    def setUp(self):
        # Product/business fixture; native request semantics have dedicated SDK tests.
        from test_learning_reading_fixture import prepared_reading
        reading=patch('learning_runtime.ReadingRuntime.run',new=prepared_reading)
        reading.start(); self.addCleanup(reading.stop)
        self.temp=temporary_notes(); self.root=self.temp.__enter__()
        self.addCleanup(self.temp.__exit__,None,None,None)
        self.store=PlanningStore(self.root/'practice.sqlite3')
        self.source=Source('s',self.root/'note.md','Condition one.\nCondition two.\nException.')
        settings=patch('learning_planner.model_settings',return_value=('test',dict(api_key='test')))
        settings.start(); self.addCleanup(settings.stop)
        self.identity=self.store.create(self.source,'Learn the conditions','pipeline',formalize=True)

    def goal(self, state, identity='g'):
        return dict(id=identity,ability_ids=['a'],reason='One coherent ability; retain five necessary conditions.',
            learning=dict(knowledge=dict(name='Condition',statement='A condition with exceptions.',content_tags=[],
                evidence_ids=list(state['evidence'])),target=dict(title='Explain the condition',capability='explain',
                description='Explain its use.',scope='Only the supplied condition.',
                criteria=[f'Necessary condition {i}' for i in range(1,6)])))

    def controlled(self, client, model, messages, schema, **kwargs):
        current_guard()()
        state=self.store.get(self.identity)
        stage=state['stage']
        if stage=='planning' and not state['evidence']:
            data=dict(action='read',tool='read_source',arguments_json=json.dumps(dict(
                source_id=self.source.source_id,line_start=1,line_end=len(self.source.text.splitlines()))))
        elif stage=='planning':
            refs=list(state['evidence'])
            data=dict(action='save',complete=True,items=[dict(id='a',ability='Explain conditions',
                conditions=['Retain exceptions'],evidence_ids=refs)],dispositions=[dict(
                    unit_id=u['unit_id'],status='analyzed',reason='Read actual source.',evidence_ids=refs) for u in state['units']])
        elif stage=='refine':
            data=dict(action='save',complete=True,goals=[self.goal(state)],abilities=[dict(
                ability_id='a',status='addressed',goal_ids=['g'],reason='All conditions retained.')])
        elif stage=='review':
            data=dict(action='save',complete=True,reviews=[dict(goal_id='g',decision='kept',replacement_ids=['g'],
                reason='Reviewed full catalog and source conditions; no redundant capability found.')])
        else:
            data=dict(action='save',exercise=dict(objective='Explain conditions',question='Explain all conditions and exceptions.',
                rubric_points=[f'Point {i}' for i in range(1,6)],criterion_indices=[1,2,3,4,5],evidence_ids=list(state['evidence'])))
        result=schema.model_validate(data)
        kwargs['validator'](result)
        return result

    def run_ready(self):
        with patch('learning_planner.request_structured',side_effect=self.controlled):
            return run_planning(self.store,self.identity,lambda:self.source)

    def test_complete_path_automatic_review_question_and_idempotency(self):
        state=self.run_ready()
        self.assertEqual((state['stage'],state['status'],state['calls_reserved']),('ready','completed',4))
        self.assertEqual(len(state['formal']['goals']['g']['learning']['target']['criteria']),5)
        self.assertEqual(state['pre_review']['g'],state['formal']['goals']['g'])
        with patch('learning_planner.request_structured',side_effect=self.controlled):
            state=run_planning(self.store,self.identity,lambda:self.source,question_goal_id='g')
        session_id=state['questions']['g']['session_id']
        record=self.store.practice.get(session_id)
        self.assertEqual(len(record['questions'][0]['draft'].criterion_indices),5)
        self.assertEqual(public_session(record)['questions'][0]['rubric_points'],None)
        with patch('learning_planner.request_structured',side_effect=AssertionError('no second paid request')):
            run_planning(self.store,self.identity,lambda:self.source,question_goal_id='g')
        with self.store.practice.transaction() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM practice_sessions').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM review_items').fetchone()[0],0)

    def test_review_budget_pause_preserves_original_and_resumes_only_review(self):
        state=self.store.get(self.identity); state['call_limit']=3; self.store.save(state)
        with patch('learning_planner.request_structured',side_effect=self.controlled), self.assertRaises(PlanningLimit):
            run_planning(self.store,self.identity,lambda:self.source)
        state=self.store.get(self.identity)
        self.assertEqual((state['stage'],state['status']),('review','paused'))
        self.assertIn('g',state['pre_review'])
        with patch('learning_planner.request_structured',side_effect=self.controlled) as request:
            resumed=run_planning(self.store,self.identity,lambda:self.source,additional_calls=1)
        self.assertEqual(request.call_count,1)
        self.assertEqual(resumed['stage'],'ready')

    def test_evidence_and_revision_mapping_fail_closed(self):
        state=self.run_ready(); state['stage']='review'
        bad=self.goal(state); bad['learning']['knowledge']['evidence_ids']=['invented']
        with self.assertRaisesRegex(ValueError,'未提供'):
            merge_formal(FormalStep(action='save',goals=[bad]),state,self.source)
        with self.assertRaises(ValueError):
            merge_formal(FormalStep(action='save',retirements=[dict(goal_id='g',replacement_ids=[],reason='Drop it.')]),state,self.source)
        revised=self.goal(state); revised['learning']['target']['criteria'].append('Another necessary condition')
        with self.assertRaisesRegex(ValueError,'回执后修改'):
            merge_formal(FormalStep(action='save',goals=[revised],complete=True),state,self.source)
        # A legitimate split has explicit old->new mapping and updates the ability.
        state['formal']['reviews']={}
        updated=merge_formal(FormalStep(action='save',goals=[self.goal(state,'g1'),self.goal(state,'g2')],
            retirements=[dict(goal_id='g',replacement_ids=['g1','g2'],reason='Separate assessable abilities.')],
            abilities=[dict(ability_id='a',status='addressed',goal_ids=['g1','g2'],reason='Split retains conditions.')],
            reviews=[dict(goal_id='g',decision='split',replacement_ids=['g1','g2'],reason='Two independently assessable abilities.')],
            complete=True),state,self.source)
        self.assertEqual(set(updated['goals']),{'g1','g2'})
        self.assertEqual(state['formal']['goals'].keys(),{'g'})

    def test_atomic_question_publication_rejects_revoked_attempt(self):
        state=self.run_ready(); state.update(stage='question',question_goal_id='g',attempt='old',status='running')
        self.store.save(state)
        step=ExerciseStep(action='save',exercise=dict(objective='Explain',question='Explain conditions',
            rubric_points=['A','B','C','D','E'],criterion_indices=[1,2,3,4,5],evidence_ids=list(state['evidence'])))
        question=resolve_exercise(step,state,self.source)
        self.store.pause(self.identity)
        with self.assertRaises(RuntimeError):
            commit_exercise(self.store,state,self.source,question,'old')
        with self.store.practice.transaction() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM practice_sessions').fetchone()[0],0)

    def test_pdf_visual_evidence_survives_question_enrollment_and_review(self):
        from pdf_parser import parse_pdf
        from pdf_vision import VisualReading
        from test_pdf_documents import text_pdf
        path=self.root/'input.pdf'; path.write_bytes(text_pdf())
        self.source=source_from_document(path,parse_pdf(path.read_bytes(),raw_file=path.name,config={'engine':'pymupdf','vlm':False}))
        self.identity=self.store.create(self.source,'Read the diagram','visual',formalize=True)
        state=self.store.get(self.identity)
        tools=KnowledgeTools([self.source],select_embedding_config())
        with patch('pdf_vision.vision_request',return_value=VisualReading(observation='Five diagram conditions.',limitations='Synthetic viewing only.')):
            remember(state,self.source,tools.execute('inspect_visual',dict(source_id=self.source.source_id,
                asset_id='page-1',question='Read conditions.')))
        refs=list(state['evidence'])
        state.update(stage='ready',status='completed',items=[dict(id='a')],formal=dict(goals={'g':self.goal(state)},abilities={},reviews={}))
        self.store.save(state)
        with patch('learning_planner.request_structured',side_effect=self.controlled):
            state=run_planning(self.store,self.identity,lambda:self.source,question_goal_id='g')
        record=self.store.practice.get(state['questions']['g']['session_id'])
        draft=record['questions'][0]['draft']
        from practice_service import _visible_chunks
        from practice_store import archived_sources
        sources=archived_sources(record['snapshots']); visible=_visible_chunks(record,sources)
        with self.assertRaises(ValueError):
            validate_question(draft,sources,visible)
        validate_question(draft,sources,visible,visual_evidence=visual_receipts(record))
        self.assertIn('Five diagram conditions',visual_messages(visual_receipts(record))[0]['content'])
        self.assertEqual(public_session(record)['questions'][0]['citations'][0]['excerpt'],'Five diagram conditions.')
        from learning_store import LearningStore
        learning_store=LearningStore(self.store.practice.path)
        item=enroll_question(learning_store,record['id'],record['questions'][0]['id'])
        learning_store.submit_answer(record['id'],'The idea matters')
        from practice_model import SemanticGradeDraft
        from practice_service import grade_pending_answer
        def grade_model(messages,schema,**kwargs):
            self.assertIn('Five diagram conditions',json.dumps(messages))
            return SemanticGradeDraft.model_validate(dict(covered_indices=[1],missing_indices=[2,3,4,5],
                answer_quotes=['The idea matters'],feedback='Several necessary conditions are missing.',
                next_action='finish',teaching_note='',follow_up=None,overall_assessment='developing',
                suggested_rating='Again',rating_reason='Conditions are incomplete.',target_fit='within_scope',dimension_observations=[]))
        with patch('practice_model._request_structured',side_effect=grade_model):
            graded=grade_pending_answer(learning_store,record['id'])
        self.assertEqual(graded['status'],'finished')
        reviewed=start_review(learning_store,item,mode='original')
        self.assertEqual(reviewed['status'],'waiting_for_answer')
        self.assertEqual(visual_receipts(self.store.practice.get(reviewed['id'])),visual_receipts(record))
        # A suggested/unviewed asset still cannot become a reference.
        altered=deepcopy(state); altered['evidence'][refs[0]]['citation']['asset_id']='page-2'
        with self.assertRaises(ValueError):
            selected_evidence(altered,refs,self.source,path=['exercise','evidence_ids'])

    def test_web_full_pipeline_and_selected_question_use_existing_practice(self):
        import test_pdf_documents
        from practice_store import archived_sources
        from test_agent_governance import wait_for
        fixture=test_pdf_documents.PDFStorageAndWebTests('runTest'); fixture.setUp()
        self.addCleanup(fixture.tearDown)
        client=fixture.client
        self.addCleanup(client.app.state.stream_runner.pool.shutdown,wait=True)
        client.__enter__(); self.addCleanup(client.__exit__,None,None,None)
        self.store=client.app.state.planning_store
        document=client.get('/api/documents/example-retrieval-demo').json()
        def web_controlled(*args,**kwargs):
            job=self.store.list_for(document['id'])[0]
            self.identity=job['id']; self.source=archived_sources([job['inputs']['snapshot']])[0]
            return self.controlled(*args,**kwargs)
        with patch('learning_planner.request_structured',side_effect=web_controlled):
            accepted=client.post(f'/api/documents/{document["id"]}/learning-plans',json=dict(
                source_id=document['source_id'],request_id='web-pipeline'))
            self.assertEqual(accepted.status_code,202,accepted.text)
            identity=accepted.json()['id']
            wait_for(lambda:self.store.get(identity)['stage']=='ready')
            wait_for(lambda:('planning',identity) not in client.app.state.stream_runner.keys)
            response=client.post(f'/api/learning-plans/{identity}/questions',json=dict(goal_id='g'))
            self.assertEqual(response.status_code,202,response.text)
            wait_for(lambda:'g' in self.store.get(identity)['questions'])
            wait_for(lambda:('planning',identity) not in client.app.state.stream_runner.keys)
        public=client.get(f'/api/learning-plans/{identity}').json()
        self.assertEqual(public['calls_reserved'],5)
        self.assertEqual(len(public['revisions']),2)
        self.assertNotIn('draft',public['questions']['g'])
        session=client.get('/api/practice/'+public['questions']['g']['session_id'])
        self.assertEqual(session.status_code,200)
        self.assertIsNone(session.json()['questions'][0]['rubric_points'])


if __name__=='__main__': unittest.main()
