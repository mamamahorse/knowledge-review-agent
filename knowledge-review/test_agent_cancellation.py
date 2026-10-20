"""Real local SDK transport cancellation and actual worker cleanup semantics."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import select
import threading
import time
import unittest

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from openai import OpenAI

from agent_cancel import (Cancellation, GenerationCancelled, cancellation_scope,
                          checkpoint, model_http_options)
from agent_stream import StreamRunner


class Model(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        mode = body['model']
        self.server.calls.append(mode)
        self.server.entered.set()
        if mode in ('before', 'body'):
            if mode == 'body':
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', '1000')
                self.end_headers()
                self.wfile.write(b'{')
                self.wfile.flush()
            if select.select([self.connection], [], [], 4)[0]:
                try:
                    if self.connection.recv(1) == b'':
                        self.server.closed.set()
                except ConnectionResetError:
                    self.server.closed.set()
            self.close_connection = True
            return
        message = {'role': 'assistant', 'content': '{"answer":"public"}'}
        status, finish = 200, 'stop'
        if mode == 'fallback' and 'response_format' in body:
            status = 400
            value = {'error': {'message': 'response_format unsupported', 'type': 'invalid_request_error'}}
        else:
            if mode == 'fallback':
                message = {'role': 'assistant', 'content': None, 'tool_calls': [
                    {'id': 'public', 'type': 'function', 'function': {
                        'name': 'submit_result', 'arguments': '{"answer":"public"}'}}]}
                finish = 'tool_calls'
            value = {'id': 'public', 'object': 'chat.completion', 'created': 0, 'model': mode,
                     'choices': [{'index': 0, 'finish_reason': finish, 'message': message}]}
        data = json.dumps(value).encode()
        if mode == 'gzip':
            data = gzip.compress(data)
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        if mode == 'gzip':
            self.send_header('Content-Encoding', 'gzip')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        self.wfile.flush()


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.model = ThreadingHTTPServer(('127.0.0.1', 0), Model)
        self.model.daemon_threads = True
        self.model.entered, self.model.closed = threading.Event(), threading.Event()
        self.model.calls = []
        self.thread = threading.Thread(target=self.model.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.model.shutdown()
        self.model.server_close()
        self.thread.join(2)

    def request(self, mode):
        with OpenAI(api_key='public', base_url=f'http://127.0.0.1:{self.model.server_port}/v1',
                    timeout=3, **model_http_options()) as client:
            return client.chat.completions.create(model=mode, messages=[{'role': 'user', 'content': 'public'}])

    def cancelled(self, mode):
        token = Cancellation()
        def work():
            with cancellation_scope(token):
                return self.request(mode)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(work)
            self.assertTrue(self.model.entered.wait(2))
            start = time.monotonic()
            token.cancel()
            with self.assertRaises(GenerationCancelled):
                future.result(2)
            self.assertTrue(self.model.closed.wait(1), 'HTTP transport did not close its socket')
            self.assertLess(time.monotonic() - start, 1.5)
            self.assertEqual(self.model.calls, [mode], 'cancelled SDK call was retried')

    def test_cancel_during_response_header_wait(self):
        self.cancelled('before')

    def test_cancel_during_partial_response_body(self):
        self.cancelled('body')

    def test_sdk_success_and_compressed_response_are_unchanged(self):
        for mode in ('normal', 'gzip'):
            with self.subTest(mode=mode), cancellation_scope(Cancellation()):
                result = self.request(mode)
                self.assertEqual(result.choices[0].message.content, '{"answer":"public"}')

    def test_structured_output_parsing_still_works(self):
        from pydantic import BaseModel
        class Answer(BaseModel):
            answer: str
        with cancellation_scope(Cancellation()):
            with OpenAI(api_key='public', base_url=f'http://127.0.0.1:{self.model.server_port}/v1',
                        **model_http_options()) as client:
                result = client.beta.chat.completions.parse(model='normal', response_format=Answer,
                    messages=[{'role': 'user', 'content': 'public'}])
                self.assertEqual(result.choices[0].message.parsed.answer, 'public')

    def test_cancel_before_call_never_opens_model_connection(self):
        token = Cancellation()
        token.cancel()
        with cancellation_scope(token), self.assertRaises(GenerationCancelled):
            self.request('normal')
        self.assertEqual(self.model.calls, [])

    def test_context_is_restored_and_cli_keeps_original_transport(self):
        self.assertEqual(model_http_options(), {})
        token = Cancellation()
        with cancellation_scope(token):
            token.cancel()
            with self.assertRaises(GenerationCancelled):
                checkpoint()
        checkpoint()
        self.assertEqual(model_http_options(), {})

    def test_provider_structured_fallback_works_over_new_transport(self):
        from pydantic import BaseModel
        from structured_output import request_structured
        class Answer(BaseModel):
            answer: str
        with cancellation_scope(Cancellation()):
            with OpenAI(api_key='public', base_url=f'http://127.0.0.1:{self.model.server_port}/v1',
                        **model_http_options()) as client:
                result = request_structured(client, 'fallback', [{'role': 'user', 'content': 'public'}], Answer)
        self.assertEqual(result.answer, 'public')
        self.assertEqual(self.model.calls, ['fallback', 'fallback'])

    def test_cancelling_one_exchange_does_not_cancel_another_worker(self):
        tokens = [Cancellation(), Cancellation()]
        def work(token):
            with cancellation_scope(token):
                return self.request('before')
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(work, token) for token in tokens]
            try:
                end = time.monotonic() + 2
                while len(self.model.calls) < 2:
                    self.assertLess(time.monotonic(), end)
                    time.sleep(.01)
                tokens[0].cancel()
                with self.assertRaises(GenerationCancelled):
                    futures[0].result(1)
                self.assertFalse(futures[1].done())
            finally:
                tokens[1].cancel()
                with self.assertRaises(GenerationCancelled):
                    futures[1].result(1)


class WorkerCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_sse_response_start_failure_cancels_before_iterator_starts(self):
        from starlette.requests import ClientDisconnect
        runner = StreamRunner(workers=1)
        exited = threading.Event()
        def work(emit):
            emit('meta', {'turn_id': 'public'})
            try:
                while True:
                    checkpoint()
                    time.sleep(.005)
            finally:
                exited.set()
        async def send(message):
            raise OSError('synthetic closed socket')
        async def receive():
            return {'type': 'http.disconnect'}
        try:
            response = await runner.response(work)
            with self.assertRaises(ClientDisconnect):
                await response({'type': 'http', 'asgi': {'spec_version': '2.4'}}, receive, send)
            self.assertTrue(await asyncio.to_thread(exited.wait, 1))
        finally:
            await asyncio.to_thread(runner.pool.shutdown, wait=True)

    async def test_disconnect_after_result_queued_still_cancels_title_work(self):
        runner = StreamRunner(workers=1)
        title_started, title_exited = threading.Event(), threading.Event()
        def title():
            title_started.set()
            try:
                while True:
                    checkpoint()
                    time.sleep(.005)
            finally:
                title_exited.set()
        def action(emit):
            emit('meta', {'turn_id': 'public'})
            return {'status': 'completed'}
        try:
            response = await runner.response(action, title)
            self.assertTrue(await asyncio.to_thread(title_started.wait, 1))
            await anext(response.body_iterator)
            await response.body_iterator.aclose()
            self.assertTrue(await asyncio.to_thread(title_exited.wait, 1))
        finally:
            await asyncio.to_thread(runner.pool.shutdown, wait=True)

    async def test_cancel_before_worker_start_resolves_sse_preheader_waiter(self):
        runner = StreamRunner(workers=1)
        submit = runner._submit
        def cancel_before_submit(action, token=None):
            token.cancel()
            return submit(action, token)
        runner._submit = cancel_before_submit
        try:
            with self.assertRaises(HTTPException) as raised:
                await asyncio.wait_for(runner.response(lambda emit: None), 1)
            self.assertEqual(raised.exception.status_code, 499)
        finally:
            await asyncio.to_thread(runner.pool.shutdown, wait=True)

    async def test_cancellation_does_not_release_slot_before_cleanup(self):
        runner = StreamRunner(workers=1)
        started, cleaning, release = threading.Event(), threading.Event(), threading.Event()
        def work():
            started.set()
            try:
                while True:
                    checkpoint()
                    time.sleep(.005)
            finally:
                cleaning.set()
                release.wait(3)
        task = asyncio.create_task(runner.run(work))
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(await asyncio.to_thread(cleaning.wait, 1))
            with self.assertRaises(HTTPException) as raised:
                await runner.run(lambda: 1)
            self.assertEqual(raised.exception.status_code, 503)
            release.set()
            await asyncio.to_thread(runner.pool.shutdown, wait=True)
            self.assertTrue(runner.slots.acquire(blocking=False))
            runner.slots.release()
        finally:
            release.set()
            await asyncio.to_thread(runner.pool.shutdown, wait=True)

    async def test_disconnected_request_cancels_cooperative_json_work(self):
        runner = StreamRunner(workers=1)
        started = threading.Event()
        class Request:
            async def is_disconnected(self):
                return started.is_set()
        def action():
            started.set()
            while True:
                checkpoint()
                time.sleep(.005)
        try:
            with self.assertRaises(HTTPException) as raised:
                await asyncio.wait_for(runner.run(action, Request()), 2)
            self.assertEqual(raised.exception.status_code, 499)
            self.assertEqual(await runner.run(lambda: 42), 42)
        finally:
            await asyncio.to_thread(runner.pool.shutdown, wait=True)

    async def test_closing_sse_cancels_work_and_skips_title(self):
        runner = StreamRunner(workers=1)
        exited, title = threading.Event(), threading.Event()
        def work(emit):
            emit('meta', {'turn_id': 'public'})
            try:
                while True:
                    checkpoint()
                    time.sleep(.005)
            finally:
                exited.set()
        try:
            response = await runner.response(work, title.set)
            await anext(response.body_iterator)
            await response.body_iterator.aclose()
            self.assertTrue(await asyncio.to_thread(exited.wait, 1))
            self.assertFalse(title.is_set())
        finally:
            await asyncio.to_thread(runner.pool.shutdown, wait=True)

    async def test_lifespan_requests_cancellation_and_drains_actual_work(self):
        runner = StreamRunner(workers=1)
        started = threading.Event()
        def work():
            started.set()
            while True:
                checkpoint()
                time.sleep(.005)
        async with runner.lifespan(None):
            future = runner._submit(work)
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
        try:
            with self.assertRaises(GenerationCancelled):
                await asyncio.wrap_future(future)
        finally:
            await asyncio.to_thread(runner.pool.shutdown, wait=True)
        self.assertEqual(len(runner.tokens), 0)


class SignatureTests(unittest.TestCase):
    def test_request_injection_preserves_schema_and_background_tasks(self):
        runner = StreamRunner(workers=1)
        app = FastAPI(lifespan=runner.lifespan)
        @app.post('/work')
        @runner.bounded
        def work(value: int):
            return {'value': value}
        try:
            with TestClient(app) as client:
                self.assertEqual(client.post('/work?value=3').json(), {'value': 3})
                parameters = client.get('/openapi.json').json()['paths']['/work']['post']['parameters']
                self.assertEqual([item['name'] for item in parameters], ['value'])
                self.assertEqual(client.post('/work?value=bad').status_code, 422)
        finally:
            runner.pool.shutdown(wait=True)


if __name__ == '__main__':
    unittest.main()
