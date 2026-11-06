import json
import shutil
import unittest
from unittest.mock import patch

from visual_discovery import Discovery,initialize,register,apply_reviews,require_complete,ObjectReview
from learning_planner import PlanningStore,run_planning,PlanningLimit
from knowledge_tools import KnowledgeTools
from embedding_config import select_embedding_config
from pdf_parser import parse_pdf
from sources import source_from_document
from structured_recovery import current_guard
from test_support import temporary_notes
from test_pdf_documents import text_pdf


class VisualDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=temporary_notes(); self.root=self.temp.__enter__()
        self.addCleanup(self.temp.__exit__,None,None,None); self.addCleanup(self.clean)
        path=self.root/'note.pdf'; path.write_bytes(text_pdf())
        self.source=source_from_document(path,parse_pdf(path.read_bytes(),raw_file=path.name,config=dict(engine='pymupdf',vlm=False)))
        settings=patch('learning_planner.model_settings',return_value=('test',dict(api_key='test')))
        settings.start(); self.addCleanup(settings.stop)
        vision=patch('pdf_vision.vision_settings',return_value=('vision',dict(api_key='test')))
        vision.start(); self.addCleanup(vision.stop)
        self.store=PlanningStore(self.root/'practice.sqlite3')
        self.identity=self.store.create(self.source,'Learn all','visual',visual_coverage=True,max_requests=5)

    def clean(self):
        for path in self.root.iterdir():
            if path.is_dir(): shutil.rmtree(path)

    def test_discovery_finds_formula_without_parser_object_and_grants_no_evidence(self):
        tools=KnowledgeTools([self.source],select_embedding_config())
        result=Discovery(status='reviewed',objects=[dict(kind='formula',label='Inline formula',bbox=[.1,.1,.4,.2],needs_detail=True,uncertainty='Subscript')],note='Checked')
        with patch('pdf_vision.vision_request',return_value=result):
            execution=tools.execute('discover_visual',dict(source_id=self.source.source_id,asset_id='page-1'))
        self.assertEqual(execution.visible_chunks,())
        self.assertIsNone(execution.visual_observation)
        from learning_planner import remember
        state=self.store.get(self.identity); remember(state,self.source,execution)
        self.assertEqual(state['evidence'],{})
        obj=next(iter(state['visual_coverage']['objects'].values()))
        self.assertEqual(obj['kind'],'formula')
        with self.assertRaises(ValueError): apply_reviews(state,[ObjectReview(object_id=obj['object_id'],decision='read',reason='Only detected')])
        from pdf_vision import VisualReading,FormulaReading
        from learning_planner import remember
        with patch('pdf_vision.vision_request',return_value=VisualReading(observation='Formula read',limitations='Subscript unclear',
            formulas=[FormulaReading(latex='x_i',variables=['x'],conditions=['i in range'],unreadable=['index definition'])])):
            reading=tools.execute('inspect_visual',dict(source_id=self.source.source_id,asset_id=obj['asset_id'],question='Read symbols'))
        remember(state,self.source,reading)
        self.assertEqual(reading.visual_observation['formulas'][0]['unreadable'],['index definition'])
        refs=list(state['evidence'])
        reviewed=apply_reviews(state,[ObjectReview(object_id=obj['object_id'],decision='read',reason='Viewed region, uncertainty recorded',evidence_ids=refs)])
        self.assertEqual(reviewed['objects'][obj['object_id']]['status'],'read')
        with self.assertRaises(ValueError): require_complete(state)
        state['visual_coverage']['targets']['page-2']['status']='reviewed'
        ledger=apply_reviews(state,[ObjectReview(object_id=obj['object_id'],decision='excluded',reason='Outside learning objective')])
        require_complete(state,ledger)

    def test_nested_calls_share_allowance_and_resume_skips_checked_pages(self):
        def vision(*args):
            current_guard()()
            return Discovery(status='reviewed',objects=[],note='No objects reported; not proof of absence')
        def model(client,name,messages,schema,**kwargs):
            current_guard()(); state=json.loads(messages[1]['content'])
            pending=next((t for t in state['visual_coverage']['targets'].values() if t['status']!='reviewed'),None)
            if not state['evidence']:
                value=dict(action='read',tool='read_source',arguments_json=json.dumps(dict(source_id=self.source.source_id,line_start=1,line_end=len(self.source.text.splitlines()))))
            elif pending:
                value=dict(action='read',tool='discover_visual',arguments_json=json.dumps(dict(source_id=self.source.source_id,asset_id=pending['asset_id'])))
            else:
                value=dict(action='save',complete=True,dispositions=[dict(unit_id=u['unit_id'],status='analyzed',reason='Read text',evidence_ids=list(state['evidence'])) for u in state['units']])
            result=schema.model_validate(value); kwargs['validator'](result); return result
        def reading(reader,state,client,model_name,budget,definitions,make_messages,reserve,persist,execute,guard,**kwargs):
            from test_learning_reading_fixture import prepared_reading
            prepared_reading(reader,state,client,model_name,budget,definitions,make_messages,reserve,persist,execute,guard)
            for target in list(state['visual_coverage']['targets'].values()):
                if target['status']=='uninspected':
                    reserve(); execute('discover_visual',dict(source_id=self.source.source_id,asset_id=target['asset_id'])); persist()
        with patch('learning_runtime.ReadingRuntime.run',new=reading),patch('learning_planner.request_structured',side_effect=model),patch('pdf_vision.vision_request',side_effect=vision) as call:
            with self.assertRaises(PlanningLimit): run_planning(self.store,self.identity,lambda:self.source)
            self.assertEqual(call.call_count,2)
            paused=self.store.get(self.identity); self.assertEqual(paused['status'],'paused')
            final=run_planning(self.store,self.identity,lambda:self.source,additional_calls=1)
            self.assertEqual(call.call_count,2)
        self.assertEqual((final['status'],final['calls_reserved']),('completed',6))

    def test_unclear_page_and_invalid_coordinates_cannot_be_complete(self):
        state=self.store.get(self.identity); initialize(state)
        with self.assertRaises(ValueError): Discovery.model_validate(dict(status='reviewed',note='',objects=[dict(kind='icon',label='Tiny',bbox=[0,0,2,1],needs_detail=True,uncertainty='')]))
        state['visual_coverage']['targets']['page-1']['status']='needs_detail'
        with self.assertRaises(ValueError): require_complete(state)
        self.assertEqual(self.store.get(self.identity)['evidence'],{})

    def test_web_opt_in_shows_pending_targets_without_spending_visual_calls(self):
        from test_pdf_documents import PDFStorageAndWebTests
        from test_agent_governance import wait_for
        fixture=PDFStorageAndWebTests('runTest'); fixture.setUp()
        try:
            client=fixture.client; client.__enter__()
            response=client.post('/api/documents',files={'files':('book.pdf',text_pdf(),'application/pdf')})
            identity=response.json()[0]['id']; source=client.get('/api/documents/'+identity).json()
            def pause(client,model,messages,schema,**kwargs):
                current_guard()(); value=schema(action='save',pause=True,note='Budgeted pause')
                kwargs['validator'](value); return value
            from test_learning_reading_fixture import prepared_reading
            with patch('learning_runtime.ReadingRuntime.run',new=prepared_reading),patch('learning_planner.request_structured',side_effect=pause),patch('pdf_vision.vision_request',side_effect=AssertionError('No automatic scan')):
                response=client.post(f'/api/documents/{identity}/learning-plans',json=dict(source_id=source['source_id'],request_id='coverage-web',visual_coverage=True))
                self.assertEqual(response.status_code,202,response.text)
                job=response.json()['id']; store=client.app.state.planning_store
                wait_for(lambda:store.get(job)['status']=='paused')
            public=client.get('/api/learning-plans/'+job).json()
            self.assertTrue(public['visual_coverage_enabled'])
            self.assertEqual(len(public['visual_coverage']['targets']),2)
            self.assertTrue(all(t['status']=='uninspected' for t in public['visual_coverage']['targets'].values()))
        finally:
            fixture.client.app.state.stream_runner.pool.shutdown(wait=True)
            client.__exit__(None,None,None); fixture.tearDown()


if __name__=='__main__': unittest.main()
