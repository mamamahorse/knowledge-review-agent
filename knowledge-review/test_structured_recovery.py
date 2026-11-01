"""Controlled replies prove local repair, full validation, feedback and bounded requests."""

from contextlib import ExitStack
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import httpx
from openai import OpenAI, InternalServerError, BadRequestError
from pydantic import Field, ValidationError

from answers import StrictModel
from generation_budget import GenerationBudget, BudgetError
from structured_output import request_structured, FUNCTION_OUTPUT_PROVIDERS
from structured_recovery import audit_scope, validation_scope, read_trace, list_traces, trace_id_from_error
from test_support import temporary_notes


class Result(StrictModel):
    count: int = Field(ge=0)
    evidence_ids: list[str]


class StreamResult(StrictModel):
    blocks: list[dict]


class NestedBatch(StrictModel):
    proposals: list[Result] = Field(max_length=4)
    note: str


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=temporary_notes(); self.root=self.temp.__enter__()
        self.scope=audit_scope(self.root); self.scope.__enter__()
        self.seen=[]; self.guards=[]; self.usage=[]
        FUNCTION_OUTPUT_PROVIDERS.clear()

    def tearDown(self):
        self.scope.__exit__(None,None,None); self.temp.__exit__(None,None,None)
        FUNCTION_OUTPUT_PROVIDERS.clear()

    @staticmethod
    def response(arguments, tools=True, finish=None):
        message=dict(role='assistant',content=None if tools else arguments,reasoning_content='HIDDEN_REASONING')
        if tools: message['tool_calls']=[dict(id='call-result',type='function',
            function=dict(name='submit_result',arguments=arguments))]
        return httpx.Response(200,json=dict(id='response',object='chat.completion',created=0,model='controlled',
            choices=[dict(index=0,finish_reason=finish or ('tool_calls' if tools else 'stop'),message=message)],
            usage=dict(prompt_tokens=100,completion_tokens=20,total_tokens=120)))

    def run_replies(self, replies, *, native=False, validator=None, budget=None, schema=Result):
        def respond(request):
            self.seen.append(json.loads(request.content))
            value=replies[min(len(self.seen)-1,len(replies)-1)]
            return value if isinstance(value,httpx.Response) else self.response(value,tools=not native)
        with OpenAI(api_key='SECRET_API_KEY',max_retries=8,
                    http_client=httpx.Client(transport=httpx.MockTransport(respond))) as client:
            return request_structured(client,'controlled',[dict(role='user',content='Use ref1 only.')],schema,
                protocol_policy='compatible' if native else 'forced_tools',validator=validator,budget=budget,
                before_request=lambda:self.guards.append(True),telemetry=lambda response,protocol:self.usage.append(response.usage))

    def records(self):
        return read_trace(list_traces(directory=self.root)[0]['trace_id'],self.root)

    def test_extra_and_missing_closing_braces_repair_without_second_request(self):
        for raw in ['{"count":1,"evidence_ids":["ref1"]}}','{"count":1,"evidence_ids":["ref1"]']:
            with self.subTest(raw=raw):
                before=len(self.seen)
                result=self.run_replies([raw],validator=lambda value:self.assertEqual(value.evidence_ids,['ref1']))
                self.assertEqual(result.count,1); self.assertEqual(len(self.seen)-before,1)
                records=self.records(); repair=next(e for e in records if e['event']=='repair_candidate')
                self.assertTrue(repair['changes']); self.assertEqual(json.loads(repair['repaired'])['count'],1)
                self.assertTrue(any(e['event']=='business_validation_passed' for e in records))

    def test_trailing_comma_is_syntax_only_repair(self):
        result=self.run_replies(['{"count":1,"evidence_ids":["ref1"],}'])
        self.assertEqual(result.count,1); self.assertEqual(len(self.seen),1)

    def test_native_content_uses_same_repair_and_original_schema(self):
        self.run_replies(['{"count":1,"evidence_ids":["ref1"]}}'],native=True)
        self.assertEqual(self.seen[0]['response_format']['type'],'json_schema')
        self.assertEqual(len(self.seen),1)

    def test_unsafe_repair_gets_specific_feedback_then_corrects(self):
        self.run_replies(['{"count":1,"evidence_ids":["unfinished',
                         '{"count":1,"evidence_ids":["ref1"]}'])
        self.assertEqual(len(self.seen),2)
        self.assertEqual(self.seen[1]['messages'][-1]['role'],'tool')
        self.assertIn('JSON repair could not safely restore syntax',self.seen[1]['messages'][-1]['content'])
        self.assertIn('repair_rejected',[e['event'] for e in self.records()])

    def test_schema_validation_is_not_weakened_by_repair(self):
        with self.assertRaises(ValidationError) as caught:
            self.run_replies(['{"count":"1","evidence_ids":["ref1"]}}'])
        self.assertEqual(len(self.seen),3)
        self.assertTrue(caught.exception.structured_recovery_exhausted)
        self.assertIn('count',self.seen[1]['messages'][-1]['content'])
        self.assertEqual(len(self.guards),3); self.assertEqual(len(self.usage),3)

    def test_missing_schema_field_is_not_invented(self):
        with self.assertRaises(ValidationError): self.run_replies(['{"count":1}'])
        self.assertEqual(len(self.seen),3)
        self.assertIn('evidence_ids',self.seen[1]['messages'][-1]['content'])

    def test_nested_values_are_preserved_and_original_proposal_cap_still_applies(self):
        value=dict(proposals=[dict(count=i,evidence_ids=['ref1']) for i in range(5)],note='Keep all facts')
        with self.assertRaises(ValidationError):
            self.run_replies([json.dumps(value)+'}'],schema=NestedBatch)
        self.assertEqual(len(self.seen),3)
        repair=next(e for e in self.records() if e['event']=='repair_candidate')
        self.assertEqual(json.loads(repair['repaired']),value)

    def test_missing_value_or_multiple_objects_or_duplicate_keys_are_not_silently_salvaged(self):
        for raw in ['{"count":,"evidence_ids":["ref1"]}',
                    '{"count":1,"evidence_ids":[]} {"count":2,"evidence_ids":[]}',
                    '{"count":1,"count":2,"evidence_ids":[]}']:
            with self.subTest(raw=raw):
                before=len(self.seen)
                with self.assertRaises(ValueError): self.run_replies([raw])
                self.assertEqual(len(self.seen)-before,3)

    def test_repaired_result_with_invalid_evidence_is_rejected_and_feedback_corrected(self):
        def validate(value):
            if value.evidence_ids != ['ref1']: raise ValueError('证据ID ref99 未提供；只允许 ref1')
        with validation_scope(validate):
            result=self.run_replies(['{"count":1,"evidence_ids":["ref99"]}}',
                                     '{"count":1,"evidence_ids":["ref1"]}'])
        self.assertEqual(result.evidence_ids,['ref1']); self.assertEqual(len(self.seen),2)
        feedback=json.loads(self.seen[1]['messages'][-1]['content'])
        self.assertEqual(feedback['phase'],'business_validation')
        self.assertIn('ref99',feedback['validation_errors'][0]['message'])
        self.assertNotIn('complete',[e['event'] for e in self.records() if e['attempt']==0])

    def test_exhausted_evidence_validation_has_three_attempts_only(self):
        def validate(value): raise ValueError('引用超出实际可见范围')
        with self.assertRaises(ValueError) as caught:
            self.run_replies(['{"count":1,"evidence_ids":["ref99"]}'],validator=validate)
        self.assertEqual(len(self.seen),3)
        self.assertTrue(caught.exception.structured_recovery_exhausted)
        self.assertEqual(caught.exception.structured_recovery_stage,'business_validation')

    def test_provider_length_is_not_repaired_or_retried(self):
        with self.assertRaisesRegex(RuntimeError,'truncated'):
            self.run_replies([self.response('{"count":1',finish='length')])
        self.assertEqual(len(self.seen),1)
        self.assertNotIn('repair_candidate',[e['event'] for e in self.records()])

    def test_sdk_and_project_retries_do_not_multiply(self):
        with self.assertRaises(InternalServerError):
            self.run_replies([httpx.Response(503,json={'error':{'message':'temporary'}})])
        self.assertEqual(len(self.seen),3)

    def test_authentication_or_unknown400_are_not_retried(self):
        with self.assertRaises(BadRequestError):
            self.run_replies([httpx.Response(400,json={'error':{'message':'unsupported schema'}})])
        self.assertEqual(len(self.seen),1)

    def test_correction_request_still_obeys_original_budget(self):
        with self.assertRaises(BudgetError):
            self.run_replies(['{"count":1,"evidence_ids":["'+'x'*9000],
                             budget=GenerationBudget(7000,1024,512))
        self.assertEqual(len(self.seen),1)

    def test_audit_inspection_has_raw_repair_changes_but_no_secret_or_reasoning(self):
        raw='{"count":1,"evidence_ids":[]}}'
        self.run_replies([raw])
        entries=self.records(); serialized=json.dumps(entries)
        self.assertEqual(next(e for e in entries if e['event']=='original')['original'],raw)
        self.assertTrue(next(e for e in entries if e['event']=='repair_candidate')['changes'])
        self.assertNotIn('SECRET_API_KEY',serialized); self.assertNotIn('HIDDEN_REASONING',serialized)
        with self.assertRaises(ValueError): read_trace('../escape',self.root)

    def test_service_exception_wrapping_keeps_audit_discoverable(self):
        with self.assertRaises(ValidationError) as caught:
            self.run_replies(['{"count":1}'])
        wrapper=RuntimeError('existing service message'); wrapper.__cause__=caught.exception
        identity=trace_id_from_error(wrapper)
        self.assertEqual(identity,caught.exception.structured_output_trace_id)
        self.assertTrue(read_trace(identity,self.root))

    def test_same_original_messages_are_not_mutated_by_feedback(self):
        messages=[dict(role='user',content='Original selection')]
        seen=[]
        def respond(request):
            seen.append(json.loads(request.content))
            return self.response('{"count":1}' if len(seen)==1 else '{"count":1,"evidence_ids":[]}')
        with OpenAI(api_key='test',http_client=httpx.Client(transport=httpx.MockTransport(respond))) as client:
            request_structured(client,'controlled',messages,Result,protocol_policy='forced_tools')
        self.assertEqual(messages,[dict(role='user',content='Original selection')])
        self.assertGreater(len(seen[1]['messages']),len(messages))

    def test_stream_tail_brace_repair_preserves_preview_once(self):
        text='{"blocks":[{"text":"visible"}]}}'; output=[]
        def respond(request):
            self.seen.append(json.loads(request.content))
            chunks=[dict(id='s',object='chat.completion.chunk',created=0,model='controlled',
                choices=[dict(index=0,delta=dict(content=text),finish_reason=None)]),
                dict(id='s',object='chat.completion.chunk',created=0,model='controlled',
                choices=[dict(index=0,delta={},finish_reason='stop')])]
            return httpx.Response(200,headers={'content-type':'text/event-stream'},
                content=''.join('data: '+json.dumps(c)+'\n\n' for c in chunks)+'data: [DONE]\n\n')
        with OpenAI(api_key='test',http_client=httpx.Client(transport=httpx.MockTransport(respond))) as client:
            result=request_structured(client,'controlled',[],StreamResult,on_delta=output.append)
        self.assertEqual(result.blocks,[dict(text='visible')])
        self.assertEqual(''.join(output),'visible'); self.assertEqual(len(self.seen),1)


if __name__=='__main__': unittest.main()
