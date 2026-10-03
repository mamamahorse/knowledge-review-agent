"""Provider compatibility never weakens result validation or changes prompts."""

import json
import unittest

import httpx
from openai import OpenAI, BadRequestError
from pydantic import ValidationError

from answers import NoteAnswer
from structured_output import FUNCTION_OUTPUT_PROVIDERS, request_structured


class StructuredOutputTests(unittest.TestCase):
    def setUp(self):
        FUNCTION_OUTPUT_PROVIDERS.clear()

    @staticmethod
    def result(arguments):
        return httpx.Response(200, json={
            'id': 'test', 'object': 'chat.completion', 'created': 0, 'model': 'test',
            'choices': [{'index': 0, 'finish_reason': 'tool_calls', 'message': {
                'role': 'assistant', 'content': None, 'tool_calls': [{
                    'id': 'result', 'type': 'function', 'function': {
                        'name': 'submit_result', 'arguments': json.dumps(arguments),
                    },
                }],
            }}],
        })

    def test_reasoning_provider_auto_fallback_is_validated_and_cached(self):
        calls = []
        messages = [{'role': 'user', 'content': 'Explain the supplied material.'}]
        data = {'coverage': 'none', 'coverage_note': 'No material.', 'blocks': []}

        def respond(request):
            body = json.loads(request.content)
            calls.append(body)
            self.assertEqual(body['messages'], messages)
            if len(calls) <= 2:
                message = ('response_format is unsupported' if len(calls) == 1 else
                           'Thinking mode does not support this tool_choice')
                return httpx.Response(400, json={'error': {'message': message}})
            self.assertEqual(body['tool_choice'], 'auto')
            return self.result(data)

        with OpenAI(api_key='test', http_client=httpx.Client(transport=httpx.MockTransport(respond))) as client:
            self.assertEqual(request_structured(client, 'test', messages, NoteAnswer).coverage, 'none')
            self.assertEqual(request_structured(client, 'test', messages, NoteAnswer).coverage, 'none')
        self.assertEqual(len(calls), 4)

    def test_malformed_tool_result_is_rejected_and_does_not_cache_success(self):
        def respond(request):
            if 'response_format' in json.loads(request.content):
                return httpx.Response(400, json={'error': {'message': 'response_format unavailable'}})
            return self.result({'unexpected': True})

        with OpenAI(api_key='test', http_client=httpx.Client(transport=httpx.MockTransport(respond))) as client:
            with self.assertRaises(ValidationError):
                request_structured(client, 'test', [], NoteAnswer)
        self.assertFalse(FUNCTION_OUTPUT_PROVIDERS)

    def test_unrelated_provider_error_does_not_trigger_fallback(self):
        calls = []

        def respond(request):
            calls.append(request)
            return httpx.Response(400, json={'error': {'message': 'Invalid model selection'}})

        with OpenAI(api_key='test', http_client=httpx.Client(transport=httpx.MockTransport(respond))) as client:
            with self.assertRaises(BadRequestError):
                request_structured(client, 'test', [], NoteAnswer)
        self.assertEqual(len(calls), 1)


if __name__ == '__main__':
    unittest.main()
