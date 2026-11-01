"""All HTTP responses are in-process fixtures; no provider request or fee."""

from types import SimpleNamespace
from unittest.mock import patch
import json
import unittest

import httpx
from openai import OpenAI, BadRequestError, APIConnectionError
from pydantic import ValidationError

from generation_budget import GenerationBudget, BudgetError
from outline_context import FullOutlineDraft
from outline_benchmark_compact import compact_protocol
from outline_benchmark_guard import RequestLedger, Limits, EvaluationStopped
from outline_benchmark_live import ControlledAuditTransport, classify_protocol_400, safe_error_details
from outline_benchmark_retry import RetryRunner, require_retry_authorization
from structured_output import FUNCTION_OUTPUT_PROVIDERS, request_structured, structured_protocols
import test_outline_benchmark as previous


class RetryAdapterTest(unittest.TestCase):
    setUp=previous.LedgerTest.setUp
    tearDown=previous.LedgerTest.tearDown
    approve_fixture=previous.LedgerTest.approve_fixture

    def runner(self):
        self.approve_fixture()
        live=self.directory/'live';(live/'calls').mkdir(parents=True)
        protocol=compact_protocol();protocol['strict_protocol_errors']=True
        protocol['generation']['thinking']={'type':'disabled'}
        protocol['generation'].pop('reasoning_effort',None)
        runner=SimpleNamespace(protocol=protocol,directory=self.directory,live=live,
            ledger=RequestLedger(self.directory/'retry-ledger.jsonl',Limits()),
            returned_models={},fatal=None,options={'api_key':'test-secret-key'},new_attempts=[])
        def before_http(kind,task):
            if runner.fatal:raise EvaluationStopped(runner.fatal)
        runner.before_http=before_http
        runner.on_provider_result=lambda status,data,classification:RetryRunner.on_provider_result(runner,status,data,classification)
        return runner

    def transport(self,runner,reply,budget=None):
        return ControlledAuditTransport(httpx.MockTransport(reply),runner,'generation','D01-A',budget or GenerationBudget())

    @staticmethod
    def response(arguments=None,finish='tool_calls',reasoning=0):
        usage={'prompt_tokens':12,'completion_tokens':50,'prompt_cache_hit_tokens':0}
        if reasoning is not None:usage['completion_tokens_details']={'reasoning_tokens':reasoning}
        return httpx.Response(200,json=dict(id='controlled',object='chat.completion',created=0,model='deepseek-flash',
            choices=[dict(index=0,finish_reason=finish,message=dict(role='assistant',content=None,
                tool_calls=[dict(id='call',type='function',function=dict(name='submit_result',arguments=json.dumps(
                    {'proposals':[],'note':'controlled fixture'} if arguments is None else arguments)))]))],usage=usage))

    def client(self,runner,reply):
        return OpenAI(api_key='test-secret-key',base_url='https://api.deepseek.com',max_retries=0,
            http_client=httpx.Client(transport=self.transport(runner,reply)))

    def test_wire_disables_thinking_omits_effort_and_preserves_contract(self):
        runner=self.runner();seen=[];protocols=structured_protocols(FullOutlineDraft)
        def reply(req):seen.append(json.loads(req.content));return self.response()
        with httpx.Client(transport=self.transport(runner,reply)) as client:
            for protocol in protocols:
                client.post('https://api.deepseek.com/chat/completions',json=dict(model='deepseek-flash',messages=[],
                    max_tokens=4096,thinking={'type':'enabled'},reasoning_effort='high',**protocol))
        for actual,original in zip(seen,protocols):
            self.assertEqual(actual['thinking'],{'type':'disabled'});self.assertNotIn('reasoning_effort',actual)
            self.assertEqual(actual['max_tokens'],4096)
            for name,value in original.items():self.assertEqual(actual[name],value)
        self.assertTrue(all(a['reasoning_tokens']==0 for a in runner.new_attempts))

    def test_full_wire_budget_checked_before_reservation_and_send(self):
        runner=self.runner();seen=[]
        with httpx.Client(transport=self.transport(runner,lambda req:seen.append(req),GenerationBudget(7000,4096,2048))) as client:
            with self.assertRaises(BudgetError):client.post('https://api.deepseek.com/chat/completions',
                json=dict(model='deepseek-flash',messages=[{'role':'user','content':'x'*2000}],max_tokens=4096))
        self.assertEqual(seen,[]);self.assertEqual(runner.ledger.events,[])

    def test_400_classification_requires_specific_mode_evidence(self):
        payload={'response_format':{'type':'json_schema'}}
        for message in ('Invalid model','response_format schema unsupported keyword additionalProperties',
                        'response_format unsupported because of invalid schema','response_format unsupported in thinking mode'):
            with self.subTest(message=message):
                self.assertFalse(classify_protocol_400({'message':message},payload)['allow_fallback'])
        self.assertTrue(classify_protocol_400({'message':'response_format unsupported'},payload)['allow_fallback'])
        self.assertTrue(classify_protocol_400({'message':'forced tool_choice is unavailable'},
            {'tool_choice':{'type':'function'}})['allow_fallback'])
        self.assertFalse(classify_protocol_400({'message':'tool_choice unsupported because of thinking mode'},
            {'tool_choice':{'type':'function'}})['allow_fallback'])
        self.assertFalse(classify_protocol_400({'message':'unknown'}, {'response_format':None})['allow_fallback'])

    def test_schema_400_stops_even_if_original_core_would_try_fallback(self):
        runner=self.runner();seen=[];FUNCTION_OUTPUT_PROVIDERS.clear()
        def reply(req):
            seen.append(req)
            return httpx.Response(400,json={'error':{'message':'response_format unsupported keyword additionalProperties',
                'param':'response_format','code':'invalid_json_schema','type':'invalid_request_error'}})
        with self.client(runner,reply) as client:
            # The SDK wraps a transport-level guard in APIConnectionError;
            # the original stop reason is retained on runner.fatal.
            with self.assertRaises((EvaluationStopped,BadRequestError,APIConnectionError)):
                request_structured(client,'deepseek-flash',[],FullOutlineDraft)
        self.assertEqual(len(seen),1);self.assertEqual(runner.fatal,'schema_validation_error')
        record=json.loads(next((runner.live/'calls').glob('*.json')).read_text(encoding='utf-8'))
        self.assertEqual(record['response']['error']['code'],'invalid_json_schema')
        self.assertEqual(record['response']['error']['param'],'response_format')

    def test_explicit_compatibility_attempts_all_count_without_schema_changes(self):
        runner=self.runner();seen=[];FUNCTION_OUTPUT_PROVIDERS.clear()
        def reply(req):
            body=json.loads(req.content);seen.append(body)
            if len(seen)<=2:
                msg='response_format unsupported' if len(seen)==1 else 'forced tool_choice is unavailable'
                return httpx.Response(400,json={'error':{'message':msg,'type':'invalid_request_error'}})
            return self.response()
        with self.client(runner,reply) as client:
            result=request_structured(client,'deepseek-flash',[],FullOutlineDraft)
        self.assertEqual(result.note,'controlled fixture');self.assertEqual(len(seen),3)
        self.assertEqual(seen[1]['tools'],seen[2]['tools']);self.assertEqual(seen[2]['tool_choice'],'auto')
        self.assertEqual(len([e for e in runner.ledger.events if e['event']=='start']),3)
        self.assertEqual([e['usage_status'] for e in runner.ledger.events if e['event']=='end'],['unknown','unknown','reported'])
        self.assertAlmostEqual(sum(e['reserved_usd'] for e in runner.ledger.events if e['event']=='start'),.15)
        FUNCTION_OUTPUT_PROVIDERS.clear()

    def test_malformed_result_is_rejected_without_repair_or_success_cache(self):
        runner=self.runner();FUNCTION_OUTPUT_PROVIDERS.clear();seen=[]
        def reply(req):
            seen.append(req)
            if len(seen)==1:return httpx.Response(400,json={'error':{'message':'response_format unsupported'}})
            return self.response({'proposals':[],'extra':'wrong; required note missing'})
        with self.client(runner,reply) as client:
            with self.assertRaises(ValidationError):request_structured(client,'deepseek-flash',[],FullOutlineDraft)
        self.assertFalse(FUNCTION_OUTPUT_PROVIDERS);self.assertEqual(len(seen),2)

    def test_existing_visible_citation_validation_remains_required(self):
        from pathlib import Path
        from outline_service import LearningProposal, validate_learning, excerpt
        from sources import Source
        source=Source('fixture-version',Path('fixture.md'),'A transaction is atomic.\nThis line was not sent.')
        value=dict(knowledge=dict(name='Atomicity',statement='A transaction is atomic.',content_tags=[],
            citations=[dict(source_id=source.source_id,line_start=1,line_end=1)]),
            target=dict(title='Explain atomicity',capability='explain',description='Explain all-or-nothing behavior.',
                scope='The supplied transaction statement.',criteria=['Explain all-or-nothing behavior.']))
        visible=[excerpt(source,1,1)]
        draft=LearningProposal.model_validate(value)
        validate_learning(draft,visible)
        value['knowledge']['citations'][0].update(line_start=2,line_end=2)
        with self.assertRaises(ValueError):validate_learning(LearningProposal.model_validate(value),visible)

    def test_length_and_nonzero_reasoning_block_further_requests(self):
        runner=self.runner();seen=[]
        def reply(req):seen.append(req);return self.response(finish='length',reasoning=7)
        with httpx.Client(transport=self.transport(runner,reply)) as client:
            payload=dict(model='deepseek-flash',messages=[],max_tokens=4096)
            client.post('https://api.deepseek.com/chat/completions',json=payload)
            with self.assertRaises(EvaluationStopped):client.post('https://api.deepseek.com/chat/completions',json=payload)
        self.assertEqual(len(seen),1);self.assertEqual(runner.fatal,'thinking_not_disabled')
        self.assertEqual(runner.new_attempts[0]['finish_reasons'],['length'])

    def test_missing_reasoning_usage_stays_unknown(self):
        runner=self.runner()
        with httpx.Client(transport=self.transport(runner,lambda _:self.response(reasoning=None))) as client:
            client.post('https://api.deepseek.com/chat/completions',json=dict(model='deepseek-flash',messages=[],max_tokens=4096))
        self.assertIsNone(runner.new_attempts[0]['reasoning_tokens'])
        record=json.loads(next((runner.live/'calls').glob('*.json')).read_text(encoding='utf-8'))
        self.assertIsNone(record['reported_reasoning_tokens'])

    def test_error_details_redact_secrets_but_keep_parameter_and_code(self):
        details=safe_error_details({'error':dict(message='wrong test-secret-key Bearer abc sk-12345',
            param='tool_choice',code='bad',type='invalid_request_error')},['test-secret-key'])
        for secret in ('test-secret-key','abc','sk-12345'):self.assertNotIn(secret,json.dumps(details))
        self.assertEqual(details['param'],'tool_choice');self.assertEqual(details['code'],'bad')

    def test_no_new_authorization_blocks_runner_before_reading_credentials(self):
        with self.assertRaises(EvaluationStopped):require_retry_authorization(self.directory)
        with patch('outline_benchmark_live.model_settings',side_effect=AssertionError('must not read settings')):
            with self.assertRaises(EvaluationStopped):RetryRunner(self.directory)

    def test_retry_stage_blocks_other_tasks_judge_and_fourth_new_request(self):
        runner=SimpleNamespace(retry_directory=self.directory,ledger=self.ledger,imported_ids={'old'},fatal=None)
        with patch('outline_benchmark_retry.require_retry_authorization'):
            for kind,task in [('judge','D01-A'),('generation','D01-C'),('generation','D02-B')]:
                with self.assertRaises(EvaluationStopped):RetryRunner.before_http(runner,kind,task)
            runner.ledger.events=[{'event':'start','request_id':i} for i in ('old','new1','new2','new3')]
            with self.assertRaises(EvaluationStopped):RetryRunner.before_http(runner,'generation','D01-A')

    def test_shared_usd10_pool_keeps_historical_unknown_and_judge_consumption(self):
        ledger=RequestLedger(self.directory/'shared.jsonl',Limits())
        for _ in range(3):
            identity=ledger.reserve('generation','D01-A',.05,{})
            ledger.finish(identity,usage=None)
        restored=RequestLedger(ledger.path,Limits())
        self.assertAlmostEqual(sum(e['reserved_usd'] for e in restored.events if e['event']=='start'),.15)
        restored.reserve('judge','J',9.80,{})
        restored.reserve('generation','new',.05,{})
        with self.assertRaises(EvaluationStopped):restored.reserve('generation','new',.05,{})
        self.assertEqual(restored.limits.reserved_usd,10)


if __name__=='__main__':unittest.main()
