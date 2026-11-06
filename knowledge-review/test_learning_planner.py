import json
from pathlib import Path
import unittest
from unittest.mock import patch

from agent_cancel import Cancellation, GenerationCancelled, cancellation_scope
from embedding_config import select_embedding_config
from knowledge_tools import KnowledgeTools
from learning_planner import (PlanningStore, PlanningStep, run_planning, validate_step,
                              remember, PlanningLimit, covered)
from structured_recovery import current_guard
from sources import Source, source_from_document
from pdf_parser import parse_pdf
from pdf_vision import VisualReading
from test_support import temporary_notes
from test_pdf_documents import text_pdf


class PlanningTests(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_notes()
        self.root = self.temp.__enter__()
        self.addCleanup(self.temp.__exit__,None,None,None)
        self.store = PlanningStore(self.root/'planning.sqlite3')
        self.source = Source('s',self.root/'note.md','Important condition.')
        settings = patch('learning_planner.model_settings',return_value=('test',dict(api_key='test')))
        settings.start(); self.addCleanup(settings.stop)
        self.identity = self.store.create(self.source,'Learn conditions','request')

    def read_step(self):
        return dict(action='read',tool='read_source',arguments_json=json.dumps(dict(
            source_id=self.source.source_id,line_start=1,line_end=1)))

    def save_step(self,state,complete=True):
        refs = list(state['evidence'])
        return dict(action='save',complete=complete,items=[dict(id='a',ability='Explain the condition',
            conditions=['Preserve the exception'],evidence_ids=refs)],dispositions=[dict(
                unit_id=u['unit_id'],status='analyzed',reason='Read and considered.',evidence_ids=refs)
                for u in state['units']])

    def script(self, steps):
        # Replay old semantic scripts over the new native reading/submission wire.
        from contextlib import ExitStack
        import httpx
        from openai import OpenAI
        from structured_recovery import audit_scope
        from test_agent_loop import AgentLoopTests
        iterator=iter(steps); pending=[]
        def respond(request):
            body=json.loads(request.content)
            if not pending:
                value=next(iterator); state=self.store.get(self.identity)
                pending.append(value(state) if callable(value) else value)
            value=pending[0]
            reading=any(t['function']['name']=='finish_reading' for t in body.get('tools',[]))
            if reading:
                name=value['tool'] if value['action']=='read' else 'finish_reading'
                args=json.loads(value['arguments_json']) if value['action']=='read' else {'note':'Ready'}
                if value['action']=='read': pending.pop()
                message=dict(role='assistant',content=None,tool_calls=[dict(id='call',type='function',function=dict(name=name,arguments=json.dumps(args)))])
            else:
                pending.pop()
                if value['action']=='read': value=dict(action='need_more_evidence',reading_reason='Read next')
                message=dict(role='assistant',content=json.dumps(value))
            return AgentLoopTests.completion(message)
        def client(**kwargs):
            return OpenAI(api_key='test',http_client=httpx.Client(transport=httpx.MockTransport(respond)))
        audit=self.root/'audit'
        import shutil
        self.addCleanup(lambda:shutil.rmtree(audit) if audit.exists() else None)
        stack=ExitStack(); stack.enter_context(audit_scope(audit))
        stack.enter_context(patch('learning_planner.OpenAI',side_effect=client))
        return stack

    def test_read_plan_and_idempotent_completion_persist(self):
        with self.script([self.read_step(),self.save_step]):
            state = run_planning(self.store,self.identity,lambda:self.source)
        self.assertEqual(state['status'],'completed')
        self.assertEqual(state['calls_reserved'],3)
        self.assertEqual(len(state['items']),1)
        with patch('learning_planner.request_structured',side_effect=AssertionError('no re-run')):
            self.assertEqual(run_planning(self.store,self.identity,lambda:self.source),state)

    def test_budget_pause_and_resume_preserve_reads(self):
        state = self.store.get(self.identity); state['call_limit']=1; self.store.save(state)
        with self.script([self.read_step()]), self.assertRaises(PlanningLimit):
            run_planning(self.store,self.identity,lambda:self.source)
        state = self.store.get(self.identity)
        self.assertEqual(state['status'],'paused')
        self.assertEqual(len(state['evidence']),1)
        with self.script([self.save_step]):
            result = run_planning(self.store,self.identity,lambda:self.source,additional_calls=2)
        self.assertEqual(result['status'],'completed')
        self.assertEqual(len([e for e in result['events'] if e['action']=='read']),1)

    def test_unknown_evidence_and_false_completion_rejected(self):
        state=self.store.get(self.identity)
        with self.assertRaises(ValueError):
            validate_step(PlanningStep(action='save',complete=True),state)
        step=self.save_step(state); step['items'][0]['evidence_ids']=['made-up']
        with self.assertRaises(ValueError):
            validate_step(PlanningStep.model_validate(step),state)

    def test_changed_source_rejected_before_model_request(self):
        other=Source('new',self.source.path,'Changed')
        with patch('learning_planner.request_structured') as request, self.assertRaises(ValueError):
            run_planning(self.store,self.identity,lambda:other)
        request.assert_not_called()

    def test_model_can_save_and_pause_without_filling_all_remaining_calls(self):
        with self.script([dict(action='save',pause=True,note='Need clearer original material.')]):
            state=run_planning(self.store,self.identity,lambda:self.source)
        self.assertEqual(state['status'],'paused')
        self.assertEqual(state['calls_reserved'],2)
        self.assertEqual(state['events'][-1]['note'],'Need clearer original material.')

    def test_request_identity_cannot_be_reused_with_new_objective(self):
        self.assertEqual(self.store.create(self.source,'Learn conditions','request'),self.identity)
        with self.assertRaises(ValueError):
            self.store.create(self.source,'Different objective','request')

    def test_partial_save_and_cancellation_keep_catalog(self):
        token=Cancellation()
        def cancel_after_saved(state):
            token.cancel()
            return self.read_step()
        with self.script([self.read_step(),lambda s:self.save_step(s,False),cancel_after_saved]), \
             cancellation_scope(token), self.assertRaises(GenerationCancelled):
            run_planning(self.store,self.identity,lambda:self.source)
        self.assertEqual(len(self.store.get(self.identity)['items']),1)
        self.assertEqual(self.store.get(self.identity)['status'],'paused')
        self.assertEqual(len([e for e in self.store.get(self.identity)['events'] if e['action']=='save'][0]['items']),1)

    def test_visual_region_cannot_mark_full_page_complete(self):
        path=self.root/'source.pdf'; path.write_bytes(text_pdf())
        source=source_from_document(path,parse_pdf(path.read_bytes(),raw_file=path.name,config={'engine':'pymupdf','vlm':False}))
        identity=self.store.create(source,'Visual abilities','pdf-request')
        state=self.store.get(identity)
        tools=KnowledgeTools([source],select_embedding_config())
        with patch('pdf_vision.vision_request',return_value=VisualReading(observation='Condition in the figure.',limitations='Only a crop.')):
            execution=tools.execute('inspect_visual',dict(source_id=source.source_id,asset_id='page-1',
                question='Read conditions.',bbox=[.1,.1,.4,.4]))
        remember(state,source,execution)
        ref=next(iter(state['evidence'].values()))
        page=next(u for u in state['units'] if u['kind']=='page' and u['page']==1)
        self.assertEqual(ref['kind'],'visual_observation')
        self.assertFalse(covered(ref,page))
        self.assertEqual(len(state['evidence']),1)

    def test_more_than_eight_abilities_are_accepted(self):
        state=self.store.get(self.identity)
        tools=KnowledgeTools([self.source],select_embedding_config())
        remember(state,self.source,tools.execute('read_source',dict(source_id='s',line_start=1,line_end=1)))
        step=self.save_step(state)
        step['items']=[dict(step['items'][0],id=str(i),ability=f'Ability {i}') for i in range(12)]
        validate_step(PlanningStep.model_validate(step),state)

    def test_disjoint_reads_can_cover_unit_but_cannot_bridge_unread_gap(self):
        state=self.store.get(self.identity)
        state['units'][0].update(line_start=1,line_end=3)
        state['evidence']={str(i):dict(kind='source_text',citation=dict(line_start=i,line_end=i)) for i in (1,3)}
        step=self.save_step(state)
        with self.assertRaises(ValueError):
            validate_step(PlanningStep.model_validate(step),state)
        state['evidence']['2']=dict(kind='source_text',citation=dict(line_start=2,line_end=2))
        validate_step(PlanningStep.model_validate(self.save_step(state)),state)

    def test_real_structured_transport_rejects_invalid_completion_then_recovers(self):
        import httpx
        from openai import OpenAI
        from structured_output import FUNCTION_OUTPUT_PROVIDERS
        from structured_recovery import audit_scope
        from test_agent_loop import AgentLoopTests
        FUNCTION_OUTPUT_PROVIDERS.clear(); self.addCleanup(FUNCTION_OUTPUT_PROVIDERS.clear)
        count=[]
        def respond(request):
            state=self.store.get(self.identity); count.append(json.loads(request.content))
            if len(count)<=2:
                name='read_source' if len(count)==1 else 'finish_reading'
                args=json.loads(self.read_step()['arguments_json']) if len(count)==1 else dict(note='Ready')
                return AgentLoopTests.completion(dict(role='assistant',content=None,tool_calls=[dict(id='call',type='function',function=dict(name=name,arguments=json.dumps(args)))]))
            value=self.save_step(state)
            if len(count)==3: value['items'][0]['evidence_ids']=['unknown']
            return AgentLoopTests.completion(dict(role='assistant',content=json.dumps(value)))
        def client(**kwargs):
            return OpenAI(api_key='test',http_client=httpx.Client(transport=httpx.MockTransport(respond)))
        audit=self.root/'audit'; audit.mkdir()
        import shutil
        self.addCleanup(shutil.rmtree,audit)
        with patch('learning_planner.OpenAI',side_effect=client),audit_scope(audit):
            state=run_planning(self.store,self.identity,lambda:self.source)
        self.assertEqual(state['status'],'completed')
        self.assertEqual(len(count),4)
        self.assertEqual(state['calls_reserved'],4)
        self.assertTrue(any('未提供' in str(message) for message in count[-1]['messages']))
        records=[json.loads(line) for file in audit.glob('*.jsonl') for line in file.read_text(encoding='utf-8').splitlines()]
        self.assertTrue(any(r.get('event')=='attempt_failed' for r in records))

    def test_planner_reads_visual_pages_and_counts_nested_request_reservations(self):
        path=self.root/'input.pdf'; path.write_bytes(text_pdf())
        self.source=source_from_document(path,parse_pdf(path.read_bytes(),raw_file=path.name,
            config={'engine':'pymupdf','vlm':False}))
        self.identity=self.store.create(self.source,'Read visual conditions','visual-loop')
        def view(page):
            return dict(action='read',tool='inspect_visual',arguments_json=json.dumps(dict(
                source_id=self.source.source_id,asset_id=f'page-{page}',question='Find abilities and conditions.')))
        def visual(*args,**kwargs):
            current_guard()()
            return VisualReading(observation='Supported ability in the page.',limitations='Synthetic result.')
        with self.script([view(1),view(2),self.save_step]), \
             patch('pdf_vision.vision_settings',return_value=('test',dict(api_key='test'))), \
             patch('pdf_vision.request_structured',side_effect=visual):
            state=run_planning(self.store,self.identity,lambda:self.source)
        self.assertEqual(state['status'],'completed')
        self.assertEqual(state['calls_reserved'],6)
        self.assertEqual({r['kind'] for r in state['evidence'].values()},{'visual_observation'})


if __name__=='__main__':
    unittest.main()
