"""Selected JSON endpoints and SSE must share actual bounded worker slots."""
import asyncio
import inspect
import os
import threading
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from agent_stream import StreamRunner
from test_support import temporary_notes
from web_app import create_app


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runner = StreamRunner(workers=1)
        self.release = threading.Event()
        self.started = threading.Event()

    async def asyncTearDown(self):
        self.release.set()
        await asyncio.to_thread(self.runner.pool.shutdown, wait=True)

    def blocked(self):
        self.started.set()
        self.release.wait(5)
        return {'status': 'completed'}

    async def test_json_blocks_sse_without_executor_backlog(self):
        task = asyncio.create_task(self.runner.run(self.blocked))
        self.assertTrue(await asyncio.to_thread(self.started.wait, 2))
        called = threading.Event()
        with self.assertRaises(HTTPException) as raised:
            await self.runner.response(lambda emit: called.set())
        self.assertEqual(raised.exception.status_code, 503)
        self.assertFalse(called.is_set())
        self.assertEqual(self.runner.pool._work_queue.qsize(), 0)
        self.release.set()
        self.assertEqual((await task)['status'], 'completed')

    async def test_detached_sse_blocks_json_until_real_work_exits(self):
        def action(emit):
            emit('meta', {'turn_id': 'public'})
            return self.blocked()
        response = await self.runner.response(action)
        await anext(response.body_iterator)
        await response.body_iterator.aclose()
        with self.assertRaises(HTTPException):
            await self.runner.run(lambda: 1)
        self.release.set()
        await asyncio.to_thread(self.runner.pool.shutdown, wait=True)
        self.assertTrue(self.runner.slots.acquire(blocking=False))
        self.runner.slots.release()

    async def test_cancelled_json_await_does_not_release_actual_worker(self):
        task = asyncio.create_task(self.runner.run(self.blocked))
        self.assertTrue(await asyncio.to_thread(self.started.wait, 2))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        with self.assertRaises(HTTPException):
            await self.runner.run(lambda: 1)
        self.release.set()
        await asyncio.to_thread(self.runner.pool.shutdown, wait=True)
        self.assertTrue(self.runner.slots.acquire(blocking=False))
        self.runner.slots.release()

    async def test_exception_preserves_status_and_releases_slot(self):
        def action():
            raise HTTPException(409, 'public conflict')
        with self.assertRaises(HTTPException) as raised:
            await self.runner.run(action)
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(await self.runner.run(lambda: 42), 42)

    async def test_shutdown_rejects_both_entry_types(self):
        async with self.runner.lifespan(None):
            pass
        with self.assertRaises(HTTPException):
            await self.runner.run(lambda: 1)
        with self.assertRaises(HTTPException):
            await self.runner.response(lambda emit: None)


class ApiTests(unittest.TestCase):
    def test_worker_configuration_rejects_bad_limits(self):
        for value in ('0', '-1', '33', '1.5', 'private-key-value'):
            with self.subTest(value=value), patch.dict(os.environ, KNOWLEDGE_REVIEW_MODEL_WORKERS=value):
                with self.assertRaisesRegex(ValueError, '^KNOWLEDGE_REVIEW_MODEL_WORKERS must be'):
                    StreamRunner()

    def test_real_routes_share_worker_but_schema_and_health_still_work(self):
        with temporary_notes() as root, patch.dict(os.environ, KNOWLEDGE_REVIEW_MODEL_WORKERS='1'):
            app = create_app(root / 'uploads')
            gate = threading.Event()
            started = threading.Event()
            def occupy():
                started.set()
                gate.wait(5)
            with TestClient(app) as client:
                runner = app.state.stream_runner
                work = runner._submit(occupy)
                self.assertTrue(started.wait(2))
                try:
                    routes = {route.endpoint.__name__: route for route in app.routes if hasattr(route, 'endpoint')}
                    names = ('ask', 'submit_turn', 'retry_turn', 'create_practice',
                             'submit_practice_answer', 'retry_practice_grade', 'review')
                    for name in names:
                        endpoint = routes[name].endpoint
                        self.assertTrue(inspect.iscoroutinefunction(endpoint), name)
                        self.assertTrue(hasattr(endpoint, '__wrapped__'), name)
                    doc = client.get('/api/documents/example-retrieval-demo').json()
                    chat = client.post('/api/conversations', json={'title': 'Public worker test'}).json()['id']
                    payload = {'question': 'Explain', 'document_ids': [doc['id']],
                               'focus_document_id': doc['id'], 'focus_source_id': doc['source_id']}
                    self.assertEqual(client.post('/api/ask', json=payload).status_code, 503)
                    turn = {**payload, 'request_id': 'public-admission'}
                    self.assertEqual(client.post(f'/api/conversations/{chat}/turns', json=turn).status_code, 503)
                    self.assertEqual(client.post(f'/api/conversations/{chat}/turns/stream', json=turn).status_code, 503)
                    self.assertEqual(client.post('/api/ask', json={}).status_code, 422)
                    self.assertEqual(client.get('/healthz').status_code, 200)
                    self.assertEqual(client.get(f'/api/conversations/{chat}/messages').json()['turns'], [])
                finally:
                    gate.set()
                    work.result(3)
                # A validly parsed request reaches the original scope check again.
                self.assertEqual(client.post('/api/ask', json={**payload, 'focus_source_id': 'stale'}).status_code, 409)


if __name__ == '__main__':
    unittest.main()
