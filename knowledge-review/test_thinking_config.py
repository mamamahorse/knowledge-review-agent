import json
import unittest
from unittest.mock import patch
import httpx
from openai import OpenAI
from pydantic import BaseModel
from structured_output import request_structured
from structured_recovery import audit_scope
from test_support import temporary_notes
from thinking_config import thinking_options


class Result(BaseModel):
    value: int


class ThinkingTests(unittest.TestCase):
    def test_deepseek_retry_preserves_reasoning_without_logging_it(self):
        requests = []
        def handler(request):
            body=json.loads(request.content); requests.append(body)
            value='{"value":"bad"}' if len(requests)==1 else '{"value":7}'
            return httpx.Response(200,json={'id':'a','object':'chat.completion','created':1,'model':'test',
                'choices':[{'index':0,'finish_reason':'tool_calls','message':{'role':'assistant','content':None,
                'reasoning_content':'private reasoning','tool_calls':[{'id':'call1','type':'function',
                'function':{'name':'submit_result','arguments':value}}]}}]})
        with temporary_notes() as root, audit_scope(root), patch.dict('os.environ',{'LLM_THINKING':'enabled','LLM_REASONING_EFFORT':'low'}):
            with OpenAI(api_key='x',base_url='https://api.deepseek.com',max_retries=0,
                        http_client=httpx.Client(transport=httpx.MockTransport(handler))) as client:
                result=request_structured(client,'test',[{'role':'user','content':'test'}],Result,protocol_policy='forced_tools')
            self.assertEqual(result.value,7)
            self.assertTrue(all(r['tool_choice']=='auto' and r['thinking']=={'type':'enabled'} and r['reasoning_effort']=='low' for r in requests))
            self.assertEqual(requests[1]['messages'][-2]['reasoning_content'],'private reasoning')
            self.assertNotIn('private reasoning',''.join(p.read_text(encoding='utf-8') for p in root.glob('*.jsonl')))

    def test_disabled_and_other_provider(self):
        with patch.dict('os.environ',{'LLM_THINKING':'disabled'}):
            with OpenAI(api_key='x',base_url='https://api.deepseek.com') as client:
                self.assertEqual(thinking_options(client),({'extra_body':{'thinking':{'type':'disabled'}}},False))
            with OpenAI(api_key='x',base_url='https://example.test') as client:
                self.assertEqual(thinking_options(client),({},False))
