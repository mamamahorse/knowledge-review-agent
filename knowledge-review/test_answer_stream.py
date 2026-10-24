"""Controlled HTTP streaming tests; no real provider, .env or business database."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
import unittest

import httpx
from openai import OpenAI
from pydantic import BaseModel

from agent_cancel import Cancellation, CancellableTransport, GenerationCancelled
from answer_stream import AnswerText
from structured_output import FUNCTION_OUTPUT_PROVIDERS, request_structured


class Result(BaseModel):
    blocks: list[dict]


class StreamingTests(unittest.TestCase):
    def setUp(self):
        self.finished = threading.Event()
        self.closed = threading.Event()
        self.requests = []
        self.mode = 'native'
        case = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                case.requests.append(body)
                if case.mode == 'tools' and ('response_format' in body or body.get('tool_choice') != 'auto'):
                    field = 'response_format' if 'response_format' in body else 'tool_choice'
                    raw = json.dumps({'error': {'message': field + ' unsupported'}}).encode()
                    self.send_response(400)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(raw)))
                    self.end_headers(); self.wfile.write(raw)
                    return
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Connection', 'close')
                self.end_headers()
                self.close_connection = True
                tools = 'tools' in body
                pieces = ['{"blocks":[{"text":"', '真正', '\\u9010', '\\u5b57', '到达', '"}]}']
                try:
                    for index, text in enumerate(pieces):
                        if case.mode == 'slow' and index == 2:
                            time.sleep(1)
                        if tools:
                            delta = {'tool_calls': [{'index': 0, 'function': {'arguments': text,
                                      **({'name': 'unexpected_tool' if case.mode == 'wrong_tool' else 'submit_result'} if index == 0 else {})}}]}
                        else:
                            delta = {'content': text, 'reasoning_content': 'never expose this'}
                        raw = json.dumps({'id': 'public-stream', 'object': 'chat.completion.chunk',
                                          'created': 1, 'model': 'controlled',
                                          'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]}).encode()
                        self.wfile.write(b'data: ' + raw + b'\n\n'); self.wfile.flush()
                        time.sleep(.06)
                    tail = json.dumps({'id': 'public-stream', 'object': 'chat.completion.chunk',
                                       'created': 1, 'model': 'controlled', 'choices': [{'index': 0, 'delta': {},
                                       'finish_reason': 'tool_calls' if tools else 'stop'}]}).encode()
                    self.wfile.write(b'data: ' + tail + b'\n\ndata: [DONE]\n\n'); self.wfile.flush()
                    case.finished.set()
                except OSError:
                    case.closed.set()

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.token = Cancellation()
        self.client = OpenAI(api_key='public-fixture', base_url=f'http://127.0.0.1:{self.server.server_port}/v1',
                             http_client=httpx.Client(transport=CancellableTransport(self.token)), max_retries=0)

    def tearDown(self):
        self.client.close()
        self.server.shutdown(); self.server.server_close(); self.thread.join(2)
        FUNCTION_OUTPUT_PROVIDERS.clear()

    def test_native_text_arrives_before_final_response(self):
        deltas = []
        result = request_structured(self.client, 'controlled', [], Result,
                                    on_delta=lambda text: deltas.append((text, self.finished.is_set())))
        self.assertEqual(''.join(s for s, _ in deltas), '真正逐字到达')
        self.assertTrue(deltas)
        self.assertFalse(deltas[0][1])
        self.assertEqual(result.blocks[0]['text'], '真正逐字到达')
        self.assertTrue(self.requests[0]['stream'])

    def test_tool_compatibility_fallback_stays_streaming(self):
        self.mode = 'tools'
        text = []
        request_structured(self.client, 'controlled', [], Result, on_delta=text.append)
        self.assertEqual(''.join(text), '真正逐字到达')
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(self.requests[-1]['tool_choice'], 'auto')
        self.assertTrue(self.requests[-1]['stream'])

    def test_cancel_interrupts_blocked_stream_read(self):
        self.mode = 'slow'
        timer = None
        cancelled_at = []
        def delta(text):
            nonlocal timer
            def cancel():
                cancelled_at.append(time.monotonic())
                self.token.cancel()
            timer = threading.Timer(.05, cancel); timer.start()
        with self.assertRaises(GenerationCancelled):
            request_structured(self.client, 'controlled', [], Result, on_delta=delta)
        if timer: timer.join()
        self.assertTrue(cancelled_at)
        self.assertLess(time.monotonic() - cancelled_at[0], .5)
        self.assertTrue(self.token.observed.is_set())
        self.assertTrue(self.closed.wait(2))

    def test_unexpected_result_tool_never_exposes_arguments(self):
        self.mode = 'wrong_tool'
        FUNCTION_OUTPUT_PROVIDERS[(str(self.client.base_url), 'controlled')] = 'auto'
        output = []
        with self.assertRaisesRegex(RuntimeError, 'Unexpected structured result tool'):
            request_structured(self.client, 'controlled', [], Result, on_delta=output.append)
        self.assertEqual(output, [])


class TextTests(unittest.TestCase):
    def test_every_fragment_boundary_escapes_and_multiple_blocks(self):
        value = json.dumps({'reasoning': 'private', 'blocks': [{'text': '中😀\n"\\'}, {'text': 'second'}]}, ensure_ascii=True)
        for split in range(len(value) + 1):
            output = []; lexer = AnswerText(output.append)
            lexer.feed(value[:split]); lexer.feed(value[split:])
            self.assertEqual(''.join(output), '中😀\n"\\\n\nsecond')

    def test_character_at_a_time_emits_before_string_closes(self):
        output = []; lexer = AnswerText(output.append)
        for c in '{"blocks":[{"text":"early': lexer.feed(c)
        self.assertEqual(''.join(output), 'early')

    def test_large_upstream_fragment_produces_bounded_delta_events(self):
        output = []; lexer = AnswerText(output.append)
        lexer.feed(json.dumps({'blocks': [{'text': '文' * 9000}]}))
        self.assertEqual(''.join(output), '文' * 9000)
        self.assertTrue(all(len(piece) <= 1024 for piece in output))


if __name__ == '__main__':
    unittest.main()
