import json
import unittest
from unittest.mock import patch
import httpx

from learning_metrics import measured,MeteredClient,_current,summary,tool_scope
from learning_planner import PlanningStore
from structured_recovery import audit_scope,RecoveryTrace
from answers import StrictModel
from sources import Source
from test_support import temporary_notes


class LearningMetricsTests(unittest.TestCase):
    def setUp(self):
        self.temp=temporary_notes(); self.root=self.temp.__enter__()
        self.addCleanup(self.temp.__exit__,None,None,None)
        self.store=PlanningStore(self.root/'practice.sqlite3')
        with patch('learning_planner.model_settings',return_value=('test',dict(api_key='test'))):
            self.identity=self.store.create(Source('s',self.root/'note.md','Actual source'),'Learn','metrics')

    def test_http_rejections_success_timeout_unknown_usage_and_secret_filter(self):
        unrun=summary(self.store,self.identity)
        self.assertEqual(unrun['coverage_status'],'unavailable')
        self.assertIsNone(unrun['http_attempts'])
        def transport(request):
            if request.url.path=='/timeout': raise httpx.ReadTimeout('private detail',request=request)
            if request.url.path=='/reject': return httpx.Response(400,json={'error':{'message':'private body'}})
            return httpx.Response(200,json=dict(model='actual-model',usage=dict(prompt_tokens=12,completion_tokens=4,prompt_cache_hit_tokens=3)))
        @measured
        def action(store,identity):
            with MeteredClient(_current.get(),transport=httpx.MockTransport(transport),follow_redirects=True) as client:
                for endpoint in ('reject','ok','timeout'):
                    try:
                        with tool_scope('inspect_visual'):
                            client.post('https://example.test/'+endpoint+'?api_key=SECRET',headers={'Authorization':'SECRET'},
                                json=dict(model='requested',thinking={'type':'disabled'},tools=[{}],tool_choice={'type':'function'}))
                    except httpx.ReadTimeout: pass
            state=store.get(identity); state.update(formal=dict(goals={'g':{}}),status='completed'); store.save(state)
        action(self.store,self.identity)
        result=summary(self.store,self.identity)
        self.assertEqual((result['http_attempts'],result['failed_http_attempts']),(3,2))
        self.assertEqual(result['coverage_status'],'observed_attempts_only')
        self.assertEqual(result['tokens']['input_tokens'],dict(observed_total=12,unknown_calls=2))
        self.assertEqual(result['tokens']['reasoning_tokens'],dict(observed_total=0,unknown_calls=3))
        self.assertIsNone(result['cost_estimate']); self.assertIsNotNone(result['first_candidate_active_ms'])
        with self.store.practice.transaction() as db:
            rows=[json.loads(r[0]) for r in db.execute('SELECT payload FROM learning_metrics_http')]
        self.assertNotIn('SECRET',json.dumps(rows)); self.assertNotIn('private body',json.dumps(rows))
        self.assertTrue(all(r['tool']=='inspect_visual' and r['protocol']=='forced_tool' for r in rows))
        self.assertEqual(rows[1]['model'],'actual-model')

    def test_redirect_hops_and_recovery_events_are_counted_separately(self):
        class Schema(StrictModel): value:int
        def transport(request):
            return httpx.Response(307,headers={'Location':'/end'}) if request.url.path=='/start' else httpx.Response(200,json={'usage':{'prompt_tokens':0,'completion_tokens':0}})
        @measured
        def action(store,identity):
            with MeteredClient(_current.get(),transport=httpx.MockTransport(transport),follow_redirects=True) as client:
                client.post('https://example.test/start',json={})
            trace=RecoveryTrace(Schema)
            trace.write('attempt_failed',phase='schema',error_type='ValidationError',retry=True,exhausted=False,errors=[dict(message='private')])
        with audit_scope(self.root): action(self.store,self.identity)
        result=summary(self.store,self.identity)
        self.assertEqual(result['http_attempts'],2)
        self.assertEqual(result['tokens']['input_tokens']['unknown_calls'],1)
        self.assertEqual(len(result['recovery_failures']),1)
        self.assertNotIn('private',json.dumps(result['recovery_failures']))

    def test_parent_child_summary_does_not_double_count_transport(self):
        parent=self.store.get(self.identity); parent['batches']=[{}]; self.store.save(parent)
        with patch('learning_planner.model_settings',return_value=('test',dict(api_key='test'))):
            child=self.store.create(Source('child',self.root/'child.md','Source'),'Learn','child',parent_id=self.identity,primary_range=(1,1),batching=False)
        @measured
        def inner(store,identity):
            with MeteredClient(_current.get(),transport=httpx.MockTransport(lambda _:httpx.Response(200,json={}))) as client:
                client.post('https://example.test',json={})
        @measured
        def outer(store,identity): inner(store,child)
        outer(self.store,self.identity)
        self.assertEqual(summary(self.store,self.identity)['http_attempts'],1)
        self.assertEqual(summary(self.store,child)['http_attempts'],1)

    def test_real_sdk_rejects_undeclared_parallel_tools_and_retains_http_success(self):
        from openai import OpenAI
        from structured_output import request_structured
        class Schema(StrictModel): value:int
        def transport(request):
            return httpx.Response(200,json=dict(id='fixture',object='chat.completion',created=1,model='test',
                choices=[dict(index=0,finish_reason='tool_calls',message=dict(role='assistant',content='',tool_calls=[
                    dict(id='a',type='function',function=dict(name='read_source',arguments='{}')),
                    dict(id='b',type='function',function=dict(name='discover_visual',arguments='{}'))]))],
                usage=dict(prompt_tokens=12,completion_tokens=8,total_tokens=20)))
        @measured
        def action(store,identity):
            http=MeteredClient(_current.get(),transport=httpx.MockTransport(transport))
            with OpenAI(api_key='synthetic',base_url='https://fixture.test',http_client=http,max_retries=0) as client:
                request_structured(client,'test',[dict(role='user',content='Submit one result')],Schema,protocol_policy='forced_tools')
        with audit_scope(self.root),self.assertRaisesRegex(RuntimeError,'one structured result'):
            action(self.store,self.identity)
        result=summary(self.store,self.identity)
        self.assertEqual((result['http_attempts'],result['failed_http_attempts']),(1,0))
        self.assertEqual(result['recovery_failures'][0]['phase'],'request')


if __name__=='__main__': unittest.main()
