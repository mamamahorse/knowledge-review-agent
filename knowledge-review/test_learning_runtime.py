"""Actual SDK/MockTransport protocol tests; no paid requests."""
import json
import shutil
import unittest
from unittest.mock import patch
import httpx
from openai import OpenAI
from learning_planner import PlanningStore, run_planning, PlanningLimit, PlanningStep
from learning_pipeline import FormalStep, ExerciseStep
from sources import Source
from structured_recovery import audit_scope
from test_support import temporary_notes


class NativeRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp=temporary_notes(); self.root=self.temp.__enter__()
        self.addCleanup(self.temp.__exit__,None,None,None)
        self.addCleanup(lambda:[shutil.rmtree(p) for p in self.root.iterdir() if p.is_dir()])
        settings=patch('learning_planner.model_settings',return_value=('test',dict(api_key='synthetic',base_url='https://api.deepseek.com')))
        settings.start(); self.addCleanup(settings.stop)
        env=patch.dict('os.environ',{'LLM_THINKING':'enabled','LLM_REASONING_EFFORT':'low'})
        env.start(); self.addCleanup(env.stop)
        self.store=PlanningStore(self.root/'run.sqlite3')
        self.source=Source('s',self.root/'note.md','A condition and exception.')
        self.identity=self.store.create(self.source,'Learn','native',max_requests=16)
        self.requests=[]

    def call(self,name,args,identity='c'):
        return dict(id=identity,type='function',function=dict(name=name,arguments=json.dumps(args)))

    def result(self,calls):
        return httpx.Response(200,json=dict(id='response',object='chat.completion',created=1,model='test',
            choices=[dict(index=0,finish_reason='tool_calls',message=dict(role='assistant',content=None,
                reasoning_content='PRIVATE REASONING',tool_calls=calls))],usage=dict(prompt_tokens=10,completion_tokens=4)))

    def save(self):
        state=self.store.get(self.identity); refs=list(state['evidence'])
        return self.call('submit_result',dict(action='save',complete=True,items=[dict(id='a',ability='Explain',conditions=['Exception'],evidence_ids=refs)],
            dispositions=[dict(unit_id=u['unit_id'],status='analyzed',reason='Read',evidence_ids=refs) for u in state['units']]))

    def run_script(self,script,**kwargs):
        def handler(request):
            body=json.loads(request.content); self.requests.append(body)
            value=script.pop(0)
            value=value(body) if callable(value) else value
            return value if isinstance(value,httpx.Response) else self.result(value)
        def client(**kwargs):
            return OpenAI(api_key='synthetic',base_url='https://api.deepseek.com',max_retries=0,http_client=httpx.Client(transport=httpx.MockTransport(handler)))
        with patch('learning_planner.OpenAI',side_effect=client),audit_scope(self.root/'audit'):
            return run_planning(self.store,self.identity,lambda:self.source,**kwargs)

    def read(self,**changes):
        return self.call('read_source',dict(source_id=self.source.source_id,line_start=1,line_end=1,**changes))

    def test_missing_source_recovered_and_native_reasoning_history(self):
        def correction(body):
            self.assertEqual(body['tool_choice'],'auto')
            tool=next(m for m in body['messages'] if m['role']=='tool')
            error=json.loads(tool['content'])['error']
            self.assertEqual(error['issues'][0]['path'],['source_id'])
            assistant=next(m for m in body['messages'] if m['role']=='assistant')
            self.assertEqual(assistant['reasoning_content'],'PRIVATE REASONING')
            return [self.read()]
        state=self.run_script([[self.call('read_source',dict(line_start=1,line_end=1))],correction,
            [self.call('finish_reading',dict(note='Read enough'))],lambda _:[self.save()]])
        self.assertEqual((state['status'],state['calls_reserved']),('completed',4))
        self.assertEqual(len(state['evidence']),1)
        self.assertTrue(all('submit_result' not in [t['function']['name'] for t in r['tools']] for r in self.requests[:3]))
        self.assertEqual([t['function']['name'] for t in self.requests[-1]['tools']],['submit_result'])
        self.assertNotIn('PRIVATE REASONING',json.dumps(self.store.get(self.identity)))
        for schema in (PlanningStep,FormalStep,ExerciseStep):
            self.assertNotIn('arguments_json',schema.model_json_schema()['properties'])
            self.assertNotIn('read',schema.model_json_schema()['properties']['action']['enum'])

    def test_two_corrections_exhaust_and_resume_does_not_reset(self):
        bad=[self.call('read_source',dict(line_start=1,line_end=1))]
        state=self.run_script([bad,bad,bad])
        self.assertEqual((state['status'],state['calls_reserved']),('paused',3))
        self.assertEqual(state['evidence'],{})
        self.assertEqual(state['runtime']['correction']['attempts'],2)
        state=self.run_script([],additional_calls=1)
        self.assertEqual(state['calls_reserved'],3)

    def test_unknown_tool_denied_before_any_execution(self):
        state=self.run_script([[self.read(),self.call('read_sources',{},'unknown')]])
        self.assertEqual(state['status'],'paused'); self.assertFalse(state['evidence'])
        self.assertTrue(any(e.get('code')=='UNKNOWN_TOOL' for e in state['events']))

    def test_unauthorized_source_denied_without_evidence(self):
        wrong=self.call('read_source',dict(source_id='other-version',line_start=1,line_end=1))
        state=self.run_script([[wrong]])
        self.assertEqual(state['status'],'paused'); self.assertFalse(state['evidence'])
        self.assertEqual(state['events'][-1]['code'],'ACCESS_DENIED')

    def test_resume_submission_without_repeating_reads(self):
        state=self.store.get(self.identity); state['call_limit']=2; self.store.save(state)
        with self.assertRaises(PlanningLimit):
            self.run_script([[self.read()],[self.call('finish_reading',dict(note='ready'))]])
        self.assertEqual(self.store.get(self.identity)['runtime']['phase'],'submission')
        state=self.run_script([lambda _:[self.save()]],additional_calls=1)
        self.assertEqual((state['status'],state['calls_reserved']),('completed',3))

    def test_submission_can_request_more_reading_without_executing_embedded_tool(self):
        state=self.run_script([[self.call('finish_reading',dict(note='try'))],
            [self.call('submit_result',dict(action='need_more_evidence',reading_reason='Need condition'))],
            [self.read()],[self.call('finish_reading',dict(note='ready'))],lambda _:[self.save()]])
        self.assertEqual(state['status'],'completed')
        self.assertTrue(any(e['action']=='needs_reading' for e in state['events']))

    def test_nested_request_limit_is_not_misclassified_as_tool_error(self):
        from learning_runtime import ReadingRuntime
        from generation_budget import GenerationBudget
        from knowledge_tools import knowledge_tool_definitions
        state=self.store.get(self.identity); saves=[]
        def client_handler(_): return self.result([self.read()])
        def exhausted(name,args): raise PlanningLimit('nested visual request quota')
        with OpenAI(api_key='x',base_url='https://api.deepseek.com',max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(client_handler))) as client:
            with self.assertRaises(PlanningLimit):
                ReadingRuntime().run(state,client,'test',GenerationBudget(),knowledge_tool_definitions(),
                    lambda:[dict(role='user',content='read')],lambda:None,lambda:saves.append(1),exhausted,limits=(PlanningLimit,))
        self.assertFalse(any(e['action']=='tool_error' for e in state['events']))

    def test_native_multi_tool_results_keep_each_call_id(self):
        first=self.read(); second=self.read(); second['id']='second'
        def finish(body):
            results=[m['tool_call_id'] for m in body['messages'] if m['role']=='tool']
            self.assertEqual(results,['c','second'])
            return [self.call('finish_reading',dict(note='ready'))]
        state=self.run_script([[first,second],finish,lambda _:[self.save()]])
        self.assertEqual(state['status'],'completed')
        self.assertEqual(len([e for e in state['events'] if e['action']=='read']),2)

    def test_full_formal_review_and_question_keep_evidence_and_budget(self):
        from test_learning_pipeline import LearningPipelineTests
        state=self.store.get(self.identity); state['inputs']['formalize']=True; self.store.save(state)
        finish=[self.call('finish_reading',dict(note='Evidence already available'))]
        def formal(_):
            current=self.store.get(self.identity)
            goal=LearningPipelineTests.goal(None,current)
            return [self.call('submit_result',dict(complete=True,goals=[goal],abilities=[dict(
                ability_id='a',status='addressed',goal_ids=['g'],reason='Conditions retained')]))]
        def review(_):
            return [self.call('submit_result',dict(complete=True,reviews=[dict(goal_id='g',decision='kept',replacement_ids=['g'],reason='Checked source')]))]
        state=self.run_script([[self.read()],finish,lambda _:[self.save()],finish,formal,finish,review])
        self.assertEqual((state['stage'],state['status']),('ready','completed'))
        def exercise(_):
            return [self.call('submit_result',dict(exercise=dict(objective='Explain',question='Explain conditions',
                rubric_points=['A','B','C','D','E'],criterion_indices=[1,2,3,4,5],evidence_ids=list(state['evidence']))))]
        state=self.run_script([finish,exercise],question_goal_id='g')
        self.assertEqual(len(state['questions']),1)
        self.assertEqual(state['calls_reserved'],9)

    def test_stage_change_discards_exhausted_old_model_and_network_budgets(self):
        state=self.store.get(self.identity)
        state['runtime']=dict(stage='old-stage',phase='submission',
            correction=dict(attempts=2,error=dict(code='INVALID_ARGUMENTS',message='old')),transport=dict(attempts=2,error_type='OldTimeout'))
        self.store.save(state)
        def fresh(body):
            self.assertNotIn('previous_tool_error',json.dumps(body['messages']))
            current=self.store.get(self.identity)['runtime']
            self.assertIsNone(current['correction']); self.assertIsNone(current['transport'])
            return [self.read()]
        state=self.run_script([fresh,[self.call('finish_reading',dict(note='ready'))],lambda _:[self.save()]])
        self.assertEqual(state['status'],'completed')

    def test_successful_sibling_does_not_clear_failed_call_in_either_order(self):
        bad=self.call('read_source',dict(line_start=1,line_end=1),'bad')
        good=self.read()
        for calls in ([bad,good],[good,bad]):
            with self.subTest(order=[c['id'] for c in calls]):
                self.identity=self.store.create(self.source,'Learn','siblings-'+calls[0]['id'],max_requests=16)
                state=self.run_script([calls,calls,calls])
                self.assertEqual((state['status'],state['calls_reserved']),('paused',3))
                correction=state['runtime']['correction']
                self.assertEqual(correction['attempts'],2)
                self.assertEqual([e['call_id'] for e in correction['errors']],['bad'])
                self.assertTrue(state['evidence'])

    def test_all_errors_in_one_batch_remain_visible(self):
        calls=[self.call('read_source',{},'source-error'),self.call('read_table',{},'table-error')]
        def again(body):
            feedback=next(m for m in body['messages'] if m['role']=='user' and 'previous_tool_error' in m['content'])
            errors=json.loads(feedback['content'])['previous_tool_error']['errors']
            self.assertEqual([e['call_id'] for e in errors],['source-error','table-error'])
            return calls
        state=self.run_script([calls,again,calls])
        self.assertEqual(state['runtime']['correction']['attempts'],2)
        self.assertEqual(len(state['runtime']['correction']['errors']),2)

    def test_network_failures_do_not_consume_model_correction_or_become_model_feedback(self):
        bad=[self.call('read_source',dict(line_start=1,line_end=1))]
        def timeout(body):
            self.assertEqual(self.store.get(self.identity)['runtime']['correction']['attempts'],0)
            raise httpx.ReadTimeout('synthetic timeout')
        def after_network(body):
            runtime=self.store.get(self.identity)['runtime']
            self.assertEqual(runtime['correction']['attempts'],0)
            self.assertEqual(runtime['transport']['attempts'],2)
            self.assertNotIn('MODEL_TRANSPORT',json.dumps(body['messages']))
            return bad
        def correct(body):
            runtime=self.store.get(self.identity)['runtime']
            self.assertEqual(runtime['correction']['attempts'],1)
            self.assertIsNone(runtime['transport'])
            return [self.read()]
        state=self.run_script([bad,httpx.Response(503,json={'error':{'message':'Unavailable'}}),timeout,after_network,correct,
            [self.call('finish_reading',dict(note='ready'))],lambda _:[self.save()]])
        self.assertEqual((state['status'],state['calls_reserved']),('completed',7))
        self.assertEqual(len([e for e in state['events'] if e['action']=='transport_error']),2)

    def test_network_exhaustion_and_resume_preserve_separate_correction(self):
        bad=[self.call('read_source',{})]
        failures=[httpx.Response(503,json={'error':{'message':'Unavailable'}}) for _ in range(3)]
        state=self.run_script([bad,*failures])
        self.assertEqual((state['status'],state['calls_reserved']),('paused',4))
        self.assertEqual(state['runtime']['correction']['attempts'],0)
        self.assertEqual(state['runtime']['transport']['attempts'],2)
        state=self.run_script([],additional_calls=1)
        self.assertEqual(state['calls_reserved'],4)

    def test_truncated_response_closes_network_incident_without_model_correction(self):
        bad=[self.call('read_source',{})]
        truncated=self.result([])
        data=json.loads(truncated.content)
        data['choices'][0]['finish_reason']='length'
        state=self.run_script([bad,httpx.Response(503,json={'error':{'message':'Unavailable'}}),httpx.Response(200,json=data)])
        self.assertEqual(state['status'],'paused')
        self.assertIsNone(state['runtime']['transport'])
        self.assertEqual(state['runtime']['correction']['attempts'],0)
