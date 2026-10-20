"""Actual worker ownership, detached acceptance and title scheduling semantics."""
import asyncio
from contextlib import closing
from datetime import datetime, timezone
import json
import os
import shutil
import threading
import time
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from agent_cancel import GenerationCancelled, checkpoint
from agent_stream import StreamRunner
from agent_work import job_boundary
from test_support import temporary_notes
from web_app import create_app


def wait_for(predicate, timeout=3):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(.01)
    raise AssertionError('Timed out waiting for actual worker transition')


class TitleStore:
    def __init__(self):
        self.chat = {'title_state': 'pending', 'memory_revision': 1,
                     'title_started_at': None, 'title': '新聊天'}
        self.claims = 0

    def get_conversation(self, identity):
        return dict(self.chat)

    def opening_messages(self, identity):
        return [{'content': 'private-question'}, {'content': 'private-answer'}]

    def claim_title(self, identity):
        self.claims += 1
        self.chat.update(title_state='generating', title_started_at=datetime.now(timezone.utc).isoformat())
        return 'public-attempt'

    def assert_dependencies(self, value):
        pass

    def publish_title(self, identity, attempt, title=None):
        self.chat['title_state'] = 'generated' if title else 'failed'
        if title:
            self.chat['title'] = title


class WorkerGovernanceTests(unittest.TestCase):
    def setUp(self):
        self.runner = StreamRunner(workers=2)
        self.release = threading.Event()
        self.started = threading.Event()

    def tearDown(self):
        self.release.set()
        self.runner.pool.shutdown(wait=True)

    def held(self):
        return self.runner.snapshot()['held_slots']

    def test_reserved_is_distinct_from_running_and_snapshot_has_no_identity(self):
        lease = self.runner._reserve('background')
        try:
            snapshot = self.runner.snapshot()
            self.assertEqual(snapshot['types']['background']['reserved'], 1)
            self.assertEqual(snapshot['types']['background']['running'], 0)
            self.assertEqual(snapshot['held_slots'], 1)
            self.assertEqual(snapshot['queue_limit'], 0)
        finally:
            self.runner._release(lease)
        self.assertEqual(self.held(), 0)
        self.assertEqual(self.runner.snapshot()['types']['background']['submitted'], 0)

    def test_title_deduplication_is_bounded_and_leaves_request_capacity(self):
        store = TitleStore()
        def generate(*args):
            self.started.set()
            self.release.wait(3)
            return 'Public title'
        with patch('conversation_title.generate_title', side_effect=generate):
            self.runner.title(store, 'private-key-and-id', 'zh')
            self.assertTrue(self.started.wait(2))
            for _ in range(100):
                self.runner.title(store, 'private-key-and-id', 'zh')
            self.assertEqual(store.claims, 1)
            self.assertEqual(self.held(), 1)
            self.assertEqual(self.runner._submit(lambda: 42).result(2), 42)
            stats = self.runner.snapshot()
            self.assertGreaterEqual(stats['types']['title']['deduplicated'], 100)
            self.assertEqual(stats['owned_keys'], 1)
            for private in ('private-key-and-id', 'private-question', 'private-answer', 'public-attempt'):
                self.assertNotIn(private, json.dumps(stats))
            self.release.set()
            wait_for(lambda: self.held() == 0)
        self.assertEqual(store.chat['title_state'], 'generated')
        self.assertEqual(self.runner.snapshot()['types']['title']['completed'], 1)

    def test_at_most_one_title_runs_even_for_different_conversations(self):
        def generate(*args):
            self.started.set()
            self.release.wait(3)
            return 'Public title'
        with patch('conversation_title.generate_title', side_effect=generate):
            self.runner.title(TitleStore(), 'first', 'zh')
            self.assertTrue(self.started.wait(2))
            self.runner.title(TitleStore(), 'second', 'zh')
            self.assertEqual(self.runner.snapshot()['types']['title']['running'], 1)
            self.assertEqual(self.runner.snapshot()['types']['title']['rejected'], 1)
            self.release.set()
            wait_for(lambda: self.held() == 0)

    def test_full_capacity_skips_title_without_claim_or_executor_backlog(self):
        store = TitleStore()
        futures = [self.runner._submit(lambda: self.release.wait(3)) for _ in range(2)]
        self.runner.title(store, 'public', 'zh')
        self.assertEqual(store.claims, 0)
        self.assertEqual(store.chat['title_state'], 'pending')
        self.assertEqual(self.runner.pool._work_queue.qsize(), 0)
        self.release.set()
        for future in futures:
            future.result(2)

    def test_failed_title_has_cooldown_and_retry_is_not_retained_in_memory(self):
        store = TitleStore()
        with patch('conversation_title.generate_title', side_effect=RuntimeError('private-provider-body')) as model:
            self.runner.title(store, 'public', 'zh')
            wait_for(lambda: self.held() == 0)
            self.assertEqual(store.chat['title_state'], 'failed')
            self.runner.title(store, 'public', 'zh')
            self.assertEqual(model.call_count, 1)
            self.assertEqual(self.runner.snapshot()['owned_keys'], 0)
            self.assertEqual(self.runner.snapshot()['types']['title']['failed'], 1)

    def test_manual_title_is_preserved_without_model_call(self):
        store = TitleStore()
        store.chat.update(title_state='manual', title='Keep this')
        with patch('conversation_title.generate_title') as model:
            self.runner.title(store, 'public', 'zh')
            model.assert_not_called()
        self.assertEqual(self.held(), 0)
        self.assertEqual(store.chat['title'], 'Keep this')

    def test_actual_error_and_business_failure_are_both_counted(self):
        def failing():
            raise RuntimeError('private')
        with self.assertRaises(RuntimeError):
            self.runner._submit(failing).result(2)
        self.runner._submit(lambda: {'status': 'failed'}).result(2)
        wait_for(lambda: self.held() == 0)
        row = self.runner.snapshot()['types']['request']
        self.assertEqual(row['submitted'], 2)
        self.assertEqual(row['exited'], 2)
        self.assertEqual(row['failed'], 2)

    def test_submit_failure_does_not_leak_reserved_capacity(self):
        self.runner.pool.shutdown(wait=True)
        with self.assertRaises(HTTPException):
            self.runner._submit(lambda: 1)
        self.assertEqual(self.held(), 0)
        self.assertEqual(len(self.runner.tokens), 0)

    def test_cancel_request_is_not_reported_as_observed_cancellation(self):
        from agent_cancel import Cancellation
        token = Cancellation()
        def uncooperative():
            self.started.set()
            self.release.wait(3)
            return {'status': 'completed'}
        future = self.runner._submit(uncooperative, token)
        self.assertTrue(self.started.wait(1))
        token.cancel()
        self.assertEqual(self.held(), 1)
        self.release.set()
        future.result(2)
        wait_for(lambda: self.held() == 0)
        row = self.runner.snapshot()['types']['request']
        self.assertEqual(row['cancel_requested'], 1)
        self.assertEqual(row['cancelled'], 0)
        self.assertEqual(row['completed'], 1)


class SseTitleTests(unittest.IsolatedAsyncioTestCase):
    async def test_sse_title_starts_after_request_releases_single_worker(self):
        runner = StreamRunner(workers=1)
        store = TitleStore()
        started, release = threading.Event(), threading.Event()
        def generate(*args):
            started.set()
            release.wait(3)
            return 'Public title'
        def action(emit):
            emit('meta', {'turn_id': 'public'})
            return {'status': 'completed'}
        try:
            with patch('conversation_title.generate_title', side_effect=generate):
                response = await runner.response(action, after_released=lambda: runner.title(store, 'public', 'zh'))
                frames = b''.join([part async for part in response.body_iterator])
                self.assertIn(b'event: done', frames)
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                snapshot = runner.snapshot()
                self.assertEqual(snapshot['types']['request']['running'], 0)
                self.assertEqual(snapshot['types']['title']['running'], 1)
                release.set()
                await asyncio.to_thread(wait_for, lambda: runner.snapshot()['held_slots'] == 0)
        finally:
            release.set()
            await asyncio.to_thread(runner.pool.shutdown, wait=True)


class DetachedApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_notes()
        self.root = self.temp.__enter__()
        self.environment = patch.dict(os.environ, {'KNOWLEDGE_REVIEW_MODEL_WORKERS': '2',
            'LLM_API_KEY': 'public', 'LLM_MODEL': 'controlled'})
        self.environment.start()
        self.app = create_app(self.root / 'uploads')
        self.runner = self.app.state.stream_runner
        self.release = threading.Event()
        self.started = threading.Event()
        self.client = TestClient(self.app)
        self.client.__enter__()

    def tearDown(self):
        self.release.set()
        self.runner.pool.shutdown(wait=True)
        self.client.__exit__(None, None, None)
        self.environment.stop()
        shutil.rmtree(self.root / 'uploads', ignore_errors=True)
        self.temp.__exit__(None, None, None)

    def stats(self):
        response = self.client.get('/internal/metrics')
        self.assertEqual(response.status_code, 200)
        return response.json()

    @staticmethod
    @job_boundary('outline')
    def blocked(store, identity, current):
        # Read the test gates from the store, retaining real persisted attempts.
        store.start(identity)
        store._test_started.set()
        while not store._test_release.wait(.01):
            checkpoint()
        with store.practice.transaction() as db:
            db.execute("UPDATE learning_outlines SET status='completed' WHERE id=?", (identity,))

    def prepare_block(self):
        store = self.app.state.outline_store
        store._test_started, store._test_release = self.started, self.release
        return patch('web_app.run_outline', side_effect=self.blocked)

    def submit(self, request='public-one'):
        return self.client.post('/api/collections/root/outlines', json={'request_id': request})

    def test_202_returns_while_actual_background_slot_remains_held(self):
        with self.prepare_block():
            start = time.monotonic()
            response = self.submit()
            self.assertEqual(response.status_code, 202, response.text)
            self.assertLess(time.monotonic() - start, 1)
            self.assertTrue(self.started.wait(1))
            self.assertEqual(self.stats()['types']['background']['running'], 1)
            self.assertEqual(self.client.get('/healthz').status_code, 200)
            self.release.set()
            wait_for(lambda: self.stats()['held_slots'] == 0)
            self.assertEqual(self.stats()['types']['background']['completed'], 1)

    def test_duplicate_submission_does_not_launch_another_worker(self):
        with self.prepare_block() as work:
            first = self.submit()
            self.assertTrue(self.started.wait(1))
            replay = self.submit()
            self.assertEqual(first.json(), replay.json())
            self.assertEqual(work.call_count, 1)
            self.assertEqual(self.stats()['held_slots'], 1)
            self.assertEqual(self.stats()['types']['background']['submitted'], 1)
            self.release.set()
            wait_for(lambda: self.stats()['held_slots'] == 0)

    def test_full_pool_rejects_before_creating_background_records(self):
        futures = [self.runner._submit(lambda: self.release.wait(3)) for _ in range(2)]
        response = self.submit('must-not-exist')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers['Retry-After'], '1')
        self.assertEqual(self.app.state.outline_store.list('root'), [])
        self.assertEqual(self.stats()['types']['background']['rejected'], 1)
        self.release.set()
        for future in futures:
            future.result(2)

    def test_invalid_scope_releases_preparation_reservation(self):
        response = self.client.post('/api/collections/missing/outlines', json={'request_id': 'public'})
        self.assertIn(response.status_code, (404, 409))
        self.assertEqual(self.stats()['held_slots'], 0)
        self.assertEqual(self.stats()['types']['background']['submitted'], 0)

    def test_pause_cancels_owned_worker_and_preserves_resumable_job(self):
        with self.prepare_block():
            identity = self.submit().json()['id']
            self.assertTrue(self.started.wait(1))
            self.assertEqual(self.client.post(f'/api/outlines/{identity}/pause').status_code, 200)
            wait_for(lambda: self.stats()['held_slots'] == 0)
            self.assertEqual(self.app.state.outline_store.get(identity)['status'], 'paused')
            self.assertEqual(self.stats()['types']['background']['cancelled'], 1)
            self.assertEqual(self.stats()['owned_keys'], 0)

    def test_all_eight_submission_endpoints_keep_background_out_of_request_schema(self):
        names = ('create_outline', 'resume_outline', 'group_outline', 'retry_outline_grouping',
                 'source_review', 'continue_review', 'generate_route', 'retry_route')
        endpoints = {route.endpoint.__name__: route.endpoint for route in self.app.routes if hasattr(route, 'endpoint')}
        for name in names:
            self.assertTrue(hasattr(endpoints[name], '__wrapped__'), name)
        schema = self.client.get('/openapi.json').json()
        for path, item in schema['paths'].items():
            if 'post' in item and item['post'].get('responses', {}).get('202'):
                self.assertNotIn('background', [p['name'] for p in item['post'].get('parameters', [])])


if __name__ == '__main__':
    unittest.main()
