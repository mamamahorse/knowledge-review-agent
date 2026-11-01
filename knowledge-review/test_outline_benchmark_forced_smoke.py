"""No external network: exercise forced protocol through real SDK serialization."""

import json
import unittest
from unittest.mock import patch

import httpx
from openai import OpenAI, BadRequestError
from pydantic import ValidationError

from generation_budget import GenerationBudget, BudgetError
from outline_context import FullOutlineDraft
from outline_v2 import DirectOutlineDraft
from outline_benchmark_forced_smoke import (request_forced_structured,ForcedAuditTransport,FORCED_CHOICE,
    ForcedSmokeRunner)
from outline_benchmark_guard import EvaluationStopped
from structured_output import result_tools, FUNCTION_OUTPUT_PROVIDERS
import test_outline_benchmark_retry as previous


class ForcedProtocolTest(unittest.TestCase):
    setUp=previous.RetryAdapterTest.setUp
    tearDown=previous.RetryAdapterTest.tearDown
    approve_fixture=previous.RetryAdapterTest.approve_fixture
    runner=previous.RetryAdapterTest.runner
    response=staticmethod(previous.RetryAdapterTest.response)

    def client(self,runner,reply,budget=None):
        transport=ForcedAuditTransport(httpx.MockTransport(reply),runner,'generation','D01-A',budget or GenerationBudget())
        return OpenAI(api_key='test-secret-key',base_url='https://api.deepseek.com',max_retries=0,
            http_client=httpx.Client(transport=transport))

    def test_v1_and_v2_share_exact_protocol_and_original_schemas(self):
        runner=self.runner();seen=[];telemetry=[]
        def reply(req):seen.append(json.loads(req.content));return self.response()
        before=[]
        cache_before=dict(FUNCTION_OUTPUT_PROVIDERS)
        with self.client(runner,reply) as client:
            request_forced_structured(client,'deepseek-flash',[],FullOutlineDraft,
                before_request=lambda:before.append(True),telemetry=lambda r,p:telemetry.append(p))
            request_forced_structured(client,'deepseek-flash',[],DirectOutlineDraft)
        self.assertEqual(len(seen),2)
        for body,schema in zip(seen,(FullOutlineDraft,DirectOutlineDraft)):
            self.assertEqual(body['tool_choice'],FORCED_CHOICE);self.assertEqual(body['tools'],result_tools(schema))
            self.assertEqual(body['thinking'],{'type':'disabled'});self.assertNotIn('reasoning_effort',body)
            self.assertNotIn('response_format',body);self.assertEqual(body['max_tokens'],GenerationBudget().output_tokens)
        self.assertEqual(before,[True]);self.assertEqual(telemetry,['tool_forced'])
        self.assertEqual(FUNCTION_OUTPUT_PROVIDERS,cache_before)
        payload=DirectOutlineDraft.model_json_schema()
        self.assertEqual(result_tools(DirectOutlineDraft)[0]['function']['parameters'],payload)

    def test_each_400_stops_without_native_or_auto_fallback(self):
        runner=self.runner();seen=[]
        def reply(req):
            seen.append(json.loads(req.content))
            return httpx.Response(400,json={'error':{'message':'forced tool_choice is unavailable','type':'invalid_request_error'}})
        with self.client(runner,reply) as client:
            with self.assertRaises(BadRequestError):request_forced_structured(client,'deepseek-flash',[],FullOutlineDraft)
        self.assertEqual(len(seen),1);self.assertEqual(seen[0]['tool_choice'],FORCED_CHOICE)

    def test_schema_validation_rejects_missing_and_extra_fields(self):
        for arguments in ({'proposals':[]},{'proposals':[],'note':'n','extra':True}):
            with self.subTest(arguments=arguments):
                with OpenAI(api_key='test',max_retries=0,http_client=httpx.Client(
                        transport=httpx.MockTransport(lambda req:self.response(arguments)))) as client:
                    with self.assertRaises(ValidationError):request_forced_structured(client,'test',[],FullOutlineDraft)

    def test_length_invalid_json_and_wrong_tool_are_not_repaired(self):
        for mode in ('length','json','wrong_name','two_tools','no_tools'):
            with self.subTest(mode=mode):
                def reply(req):
                    response=self.response(finish='length' if mode=='length' else 'tool_calls')
                    data=response.json();msg=data['choices'][0]['message']
                    if mode=='json':msg['tool_calls'][0]['function']['arguments']='{"proposals":'
                    if mode=='wrong_name':msg['tool_calls'][0]['function']['name']='other_tool'
                    if mode=='two_tools':msg['tool_calls']*=2
                    if mode=='no_tools':msg['tool_calls']=[]
                    return httpx.Response(200,json=data)
                with OpenAI(api_key='test',max_retries=0,http_client=httpx.Client(transport=httpx.MockTransport(reply))) as client:
                    with self.assertRaises((RuntimeError,ValidationError)):
                        request_forced_structured(client,'test',[],FullOutlineDraft)

    def test_transport_rejects_native_and_auto_before_actual_request(self):
        runner=self.runner();seen=[]
        transport=ForcedAuditTransport(httpx.MockTransport(lambda req:seen.append(req)),runner,'generation','D01-A',GenerationBudget())
        with httpx.Client(transport=transport) as client:
            for protocol in ({'response_format':{'type':'json_schema'}},
                    {'tools':result_tools(FullOutlineDraft),'tool_choice':'auto'}):
                with self.assertRaises(EvaluationStopped):client.post('https://api.deepseek.com/chat/completions',
                    json=dict(model='deepseek-flash',messages=[],max_tokens=4096,**protocol))
        self.assertEqual(seen,[]);self.assertEqual(runner.ledger.events,[])

    def test_budget_overflow_does_not_send_or_reserve(self):
        runner=self.runner();seen=[]
        with self.client(runner,lambda req:seen.append(req),GenerationBudget(7000,4096,2048)) as client:
            with self.assertRaises(BudgetError):request_forced_structured(client,'deepseek-flash',
                [{'role':'user','content':'x'*2000}],FullOutlineDraft,budget=GenerationBudget(7000,4096,2048))
        self.assertEqual(seen,[]);self.assertEqual(runner.ledger.events,[])

    def test_second_actual_http_and_other_tasks_are_forbidden(self):
        runner=object.__new__(ForcedSmokeRunner);runner.ledger=self.ledger;runner.imported_ids={'old'}
        runner.ledger.events=[{'event':'start','request_id':'old'},{'event':'start','request_id':'new'}]
        with patch('outline_benchmark_retry.RetryRunner.before_http'),patch('outline_benchmark_forced_smoke.verify_forced'):
            runner.retry_directory=self.directory
            with self.assertRaises(EvaluationStopped):ForcedSmokeRunner.before_http(runner,'generation','D01-A')
        for kind,task in [('judge','D01-A'),('generation','D01-C')]:
            with self.assertRaises(EvaluationStopped):ForcedSmokeRunner.client(runner,kind,task,GenerationBudget())

    def test_scoped_binding_restores_original_function(self):
        import practice_model
        runner=object.__new__(ForcedSmokeRunner);runner.client=lambda *args:None
        original=practice_model.request_structured
        # Parent model_scope supplies only local mocks; no SDK client is created.
        with ForcedSmokeRunner.model_scope(runner,'generation','D01-A',GenerationBudget()):
            self.assertIs(practice_model.request_structured,request_forced_structured)
        self.assertIs(practice_model.request_structured,original)


if __name__=='__main__':unittest.main()
