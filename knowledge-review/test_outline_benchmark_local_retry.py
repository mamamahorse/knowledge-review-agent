"""In-memory responses prove bounded retries without weakening result validation."""

import json
from contextlib import contextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
from openai import OpenAI,APIConnectionError,BadRequestError
from pydantic import ValidationError

from generation_budget import BudgetError
from outline_context import FullOutlineDraft
from outline_benchmark_guard import EvaluationStopped
from outline_benchmark_forced_smoke import request_forced_structured
from outline_benchmark_local_retry import bounded_request,retryable
import test_outline_benchmark_retry as previous


class LocalRetryTest(unittest.TestCase):
    def test_invalid_json_is_rejected_then_fresh_result_passes(self):
        seen=[];failures=[]
        def reply(request):
            seen.append(json.loads(request.content));response=previous.RetryAdapterTest.response()
            data=response.json()
            if len(seen)==1:data['choices'][0]['message']['tool_calls'][0]['function']['arguments']+='}'
            return httpx.Response(200,json=data)
        with OpenAI(api_key='test',max_retries=0,http_client=httpx.Client(transport=httpx.MockTransport(reply))) as client:
            result=bounded_request(lambda:request_forced_structured(client,'test',[],FullOutlineDraft),
                lambda n,e,a:failures.append((n,type(e).__name__,a)),sleep=lambda _:None)
        self.assertEqual(result.note,'controlled fixture');self.assertEqual(len(seen),2)
        self.assertEqual(seen[0],seen[1]);self.assertEqual(failures,[(0,'ValidationError',True)])

    def test_two_retries_means_three_attempts_maximum(self):
        calls=[];failures=[]
        def fail():calls.append(True);raise httpx.ConnectError('controlled')
        with self.assertRaises(httpx.ConnectError):bounded_request(fail,lambda *a:failures.append(a),sleep=lambda _:None)
        self.assertEqual(len(calls),3);self.assertEqual(len(failures),3)

    def test_auth_quota_and_other_400_are_not_retried(self):
        for status in (400,401,402,403,429):
            seen=[]
            def reply(req):seen.append(req);return httpx.Response(status,json={'error':{'message':'controlled'}})
            with OpenAI(api_key='test',max_retries=0,http_client=httpx.Client(transport=httpx.MockTransport(reply))) as client:
                with self.assertRaises(Exception):bounded_request(lambda:request_forced_structured(client,'test',[],FullOutlineDraft),lambda *a:None,sleep=lambda _:None)
            self.assertEqual(len(seen),1)

    def test_transient_503_can_recover(self):
        seen=[]
        def reply(req):
            seen.append(req)
            return httpx.Response(503,json={'error':{'message':'temporary'}}) if len(seen)==1 else previous.RetryAdapterTest.response()
        with OpenAI(api_key='test',max_retries=0,http_client=httpx.Client(transport=httpx.MockTransport(reply))) as client:
            result=bounded_request(lambda:request_forced_structured(client,'test',[],FullOutlineDraft),lambda *a:None,sleep=lambda _:None)
        self.assertEqual(len(seen),2);self.assertEqual(result.note,'controlled fixture')

    def test_guard_wrapped_by_sdk_is_not_retried(self):
        for cause in (EvaluationStopped('cap'),BudgetError('too large')):
            error=APIConnectionError(request=httpx.Request('POST','https://unit.test'))
            error.__cause__=cause
            self.assertFalse(retryable(error))

    def test_semantic_low_scores_are_never_retried(self):
        calls=[]
        value=bounded_request(lambda:(calls.append(True) or {'label':'uncertain'}),lambda *a:None,sleep=lambda _:None)
        self.assertEqual(value,{'label':'uncertain'});self.assertEqual(len(calls),1)


class GenerationResumeTest(unittest.TestCase):
    import test_outline_context as previous_context
    setUp=previous_context.OutlineContextTests.setUp

    def tearDown(self):
        import shutil
        path=(self.root/'retry-live').resolve()
        path.relative_to(self.root.resolve())
        if path.exists():shutil.rmtree(path)
        self.previous_context.OutlineContextTests.tearDown(self)

    def test_bad_model_result_resumes_same_job_and_retains_failed_attempt(self):
        from outline_benchmark_local_retry import LocalRetryRunner
        from outline_benchmark_guard import RequestLedger,Limits
        from outline_v2 import plan_direct
        from test_outlines import proposal
        runner=object.__new__(LocalRetryRunner);runner.live=self.root/'retry-live';runner.live.mkdir()
        (runner.live/'jobs').mkdir();runner.work=self.root;runner.docs={'D01':{'id':'D01','title':'Controlled'}}
        runner.ledger=RequestLedger(runner.live/'requests.jsonl',Limits());runner.fatal=None;runner.results=[]
        runner.active_kind='generation';runner.active_task='D01-C';runner.last_failure=None
        sections,batches,plan=plan_direct([self.source],self.snapshots,'','zh',32768)
        task=dict(task_id='D01-C',document_id='D01',arm='C',context_tokens=32768,
            sections_plan=sections,batches_plan=batches,plan=plan)
        calls=[]
        def model(messages,schema,*,budget=None,before_request=None,telemetry=None):
            before_request();calls.append(True)
            if len(calls)==1:
                try:schema.model_validate_json('{bad')
                except ValidationError as exc:runner.failed_request(0,exc,True);raise
            data=json.loads(messages[1]['content'])
            from test_outlines import wire_proposal
            ref=data['primary_evidence_ids'][0]
            primary=data['available_evidence'][ref]['line_start']
            return schema(proposals=[wire_proposal(proposal(self.source.source_id,primary),data['available_evidence'])],note='controlled recovered')
        @contextmanager
        def scope(*args):
            with patch('outline_service._request_structured',model):yield
        runner.model_scope=scope
        with patch('outline_benchmark_local_retry.load_source',return_value=self.source),patch('outline_benchmark_local_retry.time.sleep'):
            result=runner.generate(task)
        self.assertEqual(result['status'],'completed');self.assertEqual(result['local_resume_count'],1)
        self.assertEqual(len(calls),2);self.assertEqual(len(result['job']['candidates']),1)
        batch=result['job']['batches'][0]
        self.assertEqual(batch['calls'],2)
        self.assertEqual(len(json.loads(batch['request_json'])['attempts']),2)
        self.assertTrue((runner.live/'task-attempts/D01-C/00.json').exists())
        self.assertTrue((runner.live/'task-attempts/D01-C/01.json').exists())


if __name__=='__main__':unittest.main()
