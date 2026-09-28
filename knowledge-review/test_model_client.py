import json
import unittest
from pathlib import Path
from unittest.mock import patch
import httpx
from openai import OpenAI
from answers import NoteAnswer
from model_client import answer_question
from sources import Source
from chunking import Chunk

SOURCES = [Source('doc-a', Path('a.md'), 'First note'), Source('doc-b', Path('b.md'), 'Second note')]

class ModelClientTests(unittest.TestCase):
    def run_response(self, data, strict=False, finish_reason='stop', chunks=None, sources=SOURCES):
        def respond(request):
            body = json.loads(request.content)
            self.assertEqual(body['response_format']['type'], 'json_schema')
            prompt = body['messages'][1]['content']
            if chunks is None:
                self.assertIn('doc-a', prompt)
                self.assertIn('doc-b', prompt)
            else:
                self.assertNotIn('HIDDEN', prompt)
                self.assertNotIn('doc-b', prompt)
                self.assertIn('retrieved excerpts', body['messages'][0]['content'])
                for chunk in chunks:
                    self.assertIn(chunk.source_id, prompt)
                    self.assertIn(f'[L{chunk.line_start}]', prompt)
            self.assertIn('STRICT MODE' if strict else 'SUPPLEMENT MODE', body['messages'][0]['content'])
            return httpx.Response(200, json={
                'id': 'test', 'object': 'chat.completion', 'created': 0,
                'model': 'test-model', 'choices': [{
                    'index': 0, 'finish_reason': finish_reason,
                    'message': {'role': 'assistant', 'content': json.dumps(data)},
                }],
            })
        with OpenAI(api_key='test', http_client=httpx.Client(transport=httpx.MockTransport(respond))) as client:
            with patch('model_client.OpenAI', return_value=client), patch('model_client.load_dotenv'), patch.dict(
                'os.environ', {'LLM_API_KEY': 'test', 'LLM_MODEL': 'test-model'}
            ):
                return answer_question(sources, 'Question', strict=strict, chunks=chunks)

    def test_supplement_and_strict_insufficient_evidence(self):
        data = {'coverage': 'none', 'coverage_note': 'Not provided.', 'blocks': [
            {'kind': 'supplement', 'text': 'General explanation.', 'citations': []}
        ]}
        self.assertEqual(self.run_response(data), NoteAnswer.model_validate(data))
        data['blocks'] = []
        self.assertEqual(self.run_response(data, strict=True), NoteAnswer.model_validate(data))

    def test_invalid_json_structure_reports_clear_error(self):
        with self.assertRaisesRegex(RuntimeError, 'structure'):
            self.run_response({'unexpected': True})

    def test_falls_back_to_plain_json_when_provider_rejects_response_format(self):
        data = {'coverage': 'none', 'coverage_note': 'Not provided.', 'blocks': []}
        calls = []

        def respond(request):
            body = json.loads(request.content)
            calls.append(body)
            if len(calls) == 1:
                self.assertEqual(body['response_format']['type'], 'json_schema')
                return httpx.Response(400, json={'error': {
                    'message': 'This response_format type is unavailable now',
                    'type': 'invalid_request_error',
                }})
            self.assertNotIn('response_format', body)
            self.assertIn('Return only one JSON object', body['messages'][-1]['content'])
            return httpx.Response(200, json={
                'id': 'test', 'object': 'chat.completion', 'created': 0,
                'model': 'test-model', 'choices': [{
                    'index': 0, 'finish_reason': 'stop',
                    'message': {'role': 'assistant', 'content': json.dumps(data)},
                }],
            })

        with OpenAI(api_key='test', http_client=httpx.Client(transport=httpx.MockTransport(respond))) as client:
            with patch('model_client.OpenAI', return_value=client), patch('model_client.load_dotenv'), patch.dict(
                'os.environ', {'LLM_API_KEY': 'test', 'LLM_MODEL': 'test-model'}
            ):
                self.assertEqual(answer_question(SOURCES, 'Question'), NoteAnswer.model_validate(data))
        self.assertEqual(len(calls), 2)

    def test_truncation_reports_clear_error(self):
        with self.assertRaisesRegex(RuntimeError, 'truncated'):
            self.run_response({}, finish_reason='length')

    def test_rag_http_request_excludes_hidden_lines_and_enforces_citations(self):
        sources = [Source('doc-a', Path('a.md'), 'HIDDEN\nVisible'),
                   Source('doc-b', Path('b.md'), 'HIDDEN other file')]
        chunk = Chunk('chunk', 'doc-a', (), 2, 2, 'Visible')
        data = {'coverage': 'sufficient', 'coverage_note': '', 'blocks': [{
            'kind': 'grounded', 'text': 'Explanation', 'citations': [
                {'source_id': 'doc-a', 'line_start': 2, 'line_end': 2},
            ],
        }]}
        self.run_response(data, strict=True, chunks=[chunk], sources=sources)
        data['blocks'][0]['citations'][0].update(line_start=1, line_end=1)
        with self.assertRaisesRegex(RuntimeError, 'outside'):
            self.run_response(data, chunks=[chunk], sources=sources)

    def test_empty_retrieval_and_strict_mode(self):
        data = {'coverage': 'none', 'coverage_note': 'Retrieved excerpts are insufficient.', 'blocks': []}
        self.run_response(data, strict=True, chunks=[])
        data['blocks'] = [{'kind': 'supplement', 'text': 'General knowledge.', 'citations': []}]
        self.run_response(data, chunks=[])
        with self.assertRaisesRegex(RuntimeError, 'strict'):
            self.run_response(data, strict=True, chunks=[])

    def test_invalid_context_fails_before_api_call(self):
        bad = Chunk('chunk', 'doc-a', (), 1, 1, 'Forged text')
        with patch('model_client.OpenAI') as client, self.assertRaises(ValueError):
            answer_question(SOURCES, 'Question', chunks=[bad])
        client.assert_not_called()

if __name__ == '__main__':
    unittest.main()
