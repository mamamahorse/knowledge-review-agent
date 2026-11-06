import json
import threading
import unittest
from unittest.mock import patch

from structured_recovery import current_guard
from test_agent_governance import wait_for
from test_pdf_documents import PDFStorageAndWebTests


class PlanningWebTests(unittest.TestCase):
    def setUp(self):
        # Product/business fixture; native request semantics have dedicated SDK tests.
        from test_learning_reading_fixture import prepared_reading
        reading=patch('learning_runtime.ReadingRuntime.run',new=prepared_reading)
        reading.start(); self.addCleanup(reading.stop)
        self.fixture=PDFStorageAndWebTests('runTest'); self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.client=self.fixture.client
        self.addCleanup(self.client.app.state.stream_runner.pool.shutdown,wait=True)
        self.client.__enter__(); self.addCleanup(self.client.__exit__,None,None,None)
        self.app=self.client.app; self.store=self.app.state.planning_store
        self.document=self.client.get('/api/documents/example-retrieval-demo').json()
        self.doc_id=self.document['id']
        settings=patch('learning_planner.model_settings',return_value=('test',dict(api_key='test')))
        settings.start(); self.addCleanup(settings.stop)

    def create(self,request_id='web-test'):
        return self.client.post(f'/api/documents/{self.doc_id}/learning-plans',json=dict(
            source_id=self.document['source_id'],request_id=request_id,formalize=False))

    def controlled(self,client,model,messages,schema,**kwargs):
        current_guard()()
        state=json.loads(messages[1]['content'])
        if not state['evidence']:
            step=dict(action='read',tool='read_source',arguments_json=json.dumps(dict(
                source_id=self.document['source_id'],line_start=1,line_end=self.document['line_count'])))
        else:
            refs=list(state['evidence'])
            step=dict(action='save',complete=True,items=[dict(id='ability',ability='Explain the source',
                conditions=['Keep necessary conditions'],evidence_ids=refs)],dispositions=[dict(
                    unit_id=u['unit_id'],status='analyzed',reason='Read actual source.',evidence_ids=refs) for u in state['units']])
        result=schema.model_validate(step); kwargs['validator'](result)
        return result

    def test_web_create_read_list_and_idempotency(self):
        with patch('learning_planner.request_structured',side_effect=self.controlled):
            response=self.create()
            self.assertEqual(response.status_code,202,response.text)
            identity=response.json()['id']
            wait_for(lambda:self.store.get(identity)['status']=='completed')
            again=self.create()
        self.assertEqual(again.json()['id'],identity)
        result=self.client.get(f'/api/learning-plans/{identity}').json()
        self.assertEqual(result['items'][0]['id'],'ability')
        self.assertNotIn('inputs',result)
        self.assertEqual(next(iter(result['evidence'].values()))['citation']['document_id'],self.doc_id)
        self.assertEqual(len(self.client.get(f'/api/documents/{self.doc_id}/learning-plans').json()),1)

    def test_pool_full_rejects_before_creating_job(self):
        runner=self.app.state.stream_runner
        leases=[runner._reserve('background') for _ in range(runner.workers)]
        try:
            self.assertEqual(self.create().status_code,503)
            self.assertEqual(self.store.list_for(self.doc_id),[])
        finally:
            for lease in leases: runner._release(lease)

    def test_pause_discards_late_model_result_and_holds_slot_until_exit(self):
        entered=threading.Event(); release=threading.Event()
        def slow(*args,**kwargs):
            entered.set(); release.wait(4)
            return self.controlled(*args,**kwargs)
        with patch('learning_planner.request_structured',side_effect=slow):
            identity=self.create().json()['id']
            self.assertTrue(entered.wait(2))
            try:
                self.assertEqual(self.client.post(f'/api/learning-plans/{identity}/pause').status_code,200)
                self.assertEqual(self.client.post(f'/api/learning-plans/{identity}/resume',json={}).status_code,409)
                self.assertEqual(self.store.get(identity)['status'],'paused')
            finally: release.set()
        wait_for(lambda:self.store.get(identity).get('attempt') is None)
        wait_for(lambda:('planning',identity) not in self.app.state.stream_runner.keys)
        self.assertEqual(self.store.get(identity)['items'],[])

    def test_document_revision_change_during_generation_blocks_publish(self):
        def changed(*args,**kwargs):
            result=self.controlled(*args,**kwargs)
            if result.action=='save':
                with self.app.state.practice_store.transaction() as db:
                    db.execute('UPDATE document_states SET revision=revision+1 WHERE document_id=?',(self.doc_id,))
            return result
        with patch('learning_planner.request_structured',side_effect=changed):
            identity=self.create('revision-race').json()['id']
            wait_for(lambda:self.store.get(identity)['status']=='failed')
            wait_for(lambda:('planning',identity) not in self.app.state.stream_runner.keys)
        self.assertEqual(self.store.get(identity)['items'],[])
        self.assertTrue(self.client.get(f'/api/learning-plans/{identity}').json()['stale'])

    def test_stale_source_rejected_before_creating_job(self):
        response=self.client.post(f'/api/documents/{self.doc_id}/learning-plans',json=dict(
            source_id='outdated',request_id='stale'))
        self.assertEqual(response.status_code,409)
        self.assertEqual(self.store.list_for(self.doc_id),[])

    def test_batched_web_next_view_previous_question_and_revision_guard(self):
        from test_learning_batches import controlled
        total=self.document['line_count']; middle=total//2
        specs=[dict(index=1,line_start=1,line_end=middle,job_id=None),
               dict(index=2,line_start=middle+1,line_end=total,job_id=None)]
        with patch('learning_batches.plan_batches',return_value=specs), \
             patch('learning_planner.request_structured',side_effect=controlled):
            response=self.client.post(f'/api/documents/{self.doc_id}/learning-plans',json=dict(
                source_id=self.document['source_id'],request_id='web-batches'))
            self.assertEqual(response.status_code,202,response.text)
            identity=response.json()['id']
            wait_for(lambda:self.store.get(identity)['status']=='waiting_next')
            wait_for(lambda:('planning',identity) not in self.app.state.stream_runner.keys)
            # Detail/guards use range snapshots rather than rebuilding the full source.
            with patch('web_app._source_for',side_effect=AssertionError('full source reload')):
                first=self.client.get(f'/api/learning-plans/{identity}').json()
                self.assertEqual((first['can_next'],first['completed_batches']),(True,1))
                self.assertEqual(first['stage'],'ready')
                self.assertEqual(self.client.post(f'/api/learning-plans/{identity}/next-batch',
                    json=dict(batch_index=2)).status_code,202)
                wait_for(lambda:self.store.get(identity)['status']=='completed')
                wait_for(lambda:('planning',identity) not in self.app.state.stream_runner.keys)
            self.assertEqual(len(self.store.list_for(self.doc_id)),1)
            second=self.client.get(f'/api/learning-plans/{identity}').json()
            self.assertEqual(second['batch_index'],2)
            older=self.client.get(f'/api/learning-plans/{identity}?batch_index=1').json()
            self.assertEqual(older['batch_range'],[1,middle])
            self.assertEqual(self.client.get(f'/api/learning-plans/{identity}?batch_index=0').status_code,409)
            self.assertEqual(self.client.post(f'/api/learning-plans/{identity}/questions',json=dict(
                goal_id='g',batch_index=1)).status_code,202)
            child_id=self.store.get(identity)['batches'][0]['job_id']
            wait_for(lambda:bool(self.store.get(child_id)['questions']))
            wait_for(lambda:('planning',identity) not in self.app.state.stream_runner.keys)
        before=self.store.get(identity)
        self.assertEqual(self.client.post(f'/api/learning-plans/{identity}/next-batch',
            json=dict(batch_index=2,additional_calls=10)).status_code,202)
        self.assertEqual(self.store.get(identity),before)
        with self.app.state.practice_store.transaction() as db:
            db.execute('UPDATE document_states SET revision=revision+1 WHERE document_id=?',(self.doc_id,))
        self.assertTrue(self.client.get(f'/api/learning-plans/{identity}').json()['stale'])
        self.assertEqual(self.client.post(f'/api/learning-plans/{identity}/next-batch',
            json=dict(batch_index=2)).status_code,409)


if __name__=='__main__': unittest.main()
