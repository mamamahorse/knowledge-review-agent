"""SSE limits and actual turn persistence; model transport is controlled."""
import asyncio
import json
import shutil
import threading
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from agent_loop import AgentRun
from agent_stream import EventBridge, EventLimit, StreamRunner, encode
from answers import NoteAnswer
from test_support import temporary_notes
from web_app import create_app


def events(text):
    parsed = []
    for block in text.split("\n\n"):
        if block.startswith("event: "):
            name, data = block.split("\n", 1)
            parsed.append((name[7:], json.loads(data[6:])))
    return parsed


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_queue_has_reserved_error_and_done(self):
        bridge = EventBridge(asyncio.get_running_loop(), queue_size=1)
        bridge.emit("progress", {"phase": "one"})
        with self.assertRaises(EventLimit):
            bridge.emit("progress", {"phase": "two"})
        bridge.complete(code="STREAM_LIMIT")
        frames = b"".join([part async for part in bridge.events()]).decode()
        self.assertEqual([e[0] for e in events(frames)], ["progress", "error", "done"])
        self.assertIn("STREAM_LIMIT", frames)

    async def test_result_limit_reports_saved_result_transport_failure(self):
        bridge = EventBridge(asyncio.get_running_loop(), result_limit=32)
        bridge.complete({"status": "completed", "value": "x" * 100})
        frames = b"".join([part async for part in bridge.events()]).decode()
        self.assertIn("RESULT_TOO_LARGE", frames)
        self.assertEqual(events(frames)[-1][1]["status"], "failed")

    async def test_progress_size_and_total_limits(self):
        bridge = EventBridge(asyncio.get_running_loop(), progress_limit=70)
        bridge.emit("progress", {"phase": "one"})
        with self.assertRaises(EventLimit):
            bridge.emit("progress", {"phase": "two"})
        with self.assertRaises(EventLimit):
            encode("progress", {"value": "x" * 8192}, 8192)

    async def test_heartbeat_and_generator_close(self):
        bridge = EventBridge(asyncio.get_running_loop())
        generator = bridge.events(heartbeat_seconds=0.01)
        self.assertEqual(await anext(generator), b": ping\n\n")
        await generator.aclose()
        self.assertTrue(bridge.detached.is_set())
        bridge.emit("progress", {"ignored": True})
        self.assertTrue(bridge.queue.empty())

    async def test_disconnect_does_not_release_running_worker(self):
        runner = StreamRunner(workers=1)
        release = threading.Event()
        finished = threading.Event()
        def action(emit):
            emit("meta", {"turn_id": "synthetic"})
            release.wait(3)
            finished.set()
            return {"status": "completed"}
        try:
            response = await runner.response(action)
            await anext(response.body_iterator)
            await response.body_iterator.aclose()
            with self.assertRaises(HTTPException) as raised:
                await runner.response(action)
            self.assertEqual(raised.exception.status_code, 503)
        finally:
            release.set()
            await asyncio.to_thread(finished.wait, 3)
            runner.pool.shutdown(wait=True)

    async def test_admission_error_keeps_http_status_and_releases_slot(self):
        runner = StreamRunner(workers=1)
        def reject(emit):
            raise HTTPException(409, "conflict")
        try:
            with self.assertRaises(HTTPException) as raised:
                await runner.response(reject)
            self.assertEqual(raised.exception.status_code, 409)
        finally:
            runner.pool.shutdown(wait=True)
        self.assertTrue(runner.slots.acquire(blocking=False))
        runner.slots.release()


class StreamApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_notes()
        self.root = self.temp.__enter__()
        self.app = create_app(self.root / "uploads")
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.store = self.app.state.conversation_store
        self.chat = self.store.create("Public SSE test")["id"]
        doc = self.client.get("/api/documents/example-retrieval-demo").json()
        self.payload = {"request_id": "public-sse", "question": "Explain", "document_ids": [doc["id"]],
                        "focus_document_id": doc["id"], "focus_source_id": doc["source_id"]}
        answer = {"coverage": "none", "coverage_note": "Public controlled response", "blocks": [
            {"kind": "supplement", "text": "Public answer", "citations": []}]}
        self.run = AgentRun(NoteAnswer.model_validate(answer), (), "model_finished", ())
        self.url = f"/api/conversations/{self.chat}/turns/stream"

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.app.state.stream_runner.pool.shutdown(wait=True)
        shutil.rmtree(self.root / "uploads", ignore_errors=True)
        self.temp.__exit__(None, None, None)

    def test_result_is_persisted_and_replay_does_not_rerun_agent(self):
        with patch("web_app.run_agent", return_value=self.run) as agent, patch("web_app.load_dotenv"):
            response = self.client.post(self.url, json=self.payload, headers={"X-Request-ID": "trace-public"})
            replay = self.client.post(self.url, json=self.payload)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
        parsed = events(response.text)
        self.assertEqual([e[0] for e in parsed], ["meta", "progress", "result", "done"])
        self.assertEqual(parsed[0][1]["trace_id"], "trace-public")
        self.assertEqual(agent.call_count, 1)
        history = self.client.get(f"/api/conversations/{self.chat}/messages").json()["turns"]
        self.assertEqual(parsed[-2][1]["turn"], history[0])
        self.assertEqual(events(replay.text)[-1][1]["status"], "completed")
        self.assertEqual(len(history), 1)

    def test_failure_is_safe_and_persistent(self):
        with patch("web_app.run_agent", side_effect=RuntimeError("private-provider-token")), patch("web_app.load_dotenv"):
            response = self.client.post(self.url, json=self.payload)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("private-provider-token", response.text)
        self.assertEqual([e[0] for e in events(response.text)], ["meta", "progress", "error", "done"])
        self.assertEqual(self.store.turns(self.chat)[0]["status"], "failed")

    def test_invalid_scope_is_http_error_without_new_turn(self):
        response = self.client.post(self.url, json={**self.payload, "focus_source_id": "stale"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.store.turns(self.chat), [])

    def test_failed_turn_can_retry_as_stream_without_creating_another_turn(self):
        with patch('web_app.run_agent', side_effect=RuntimeError('public failure')), patch('web_app.load_dotenv'):
            self.client.post(self.url, json=self.payload)
        original = self.store.turns(self.chat)[0]
        retry_url = f'/api/conversations/{self.chat}/turns/{original["id"]}/retry/stream'
        other = self.store.create('Other chat')['id']
        wrong = retry_url.replace(self.chat, other)
        self.assertEqual(self.client.post(wrong, json={}).status_code, 404)
        def answer(*args, **kwargs):
            kwargs['event_sink']('delta', {'text': 'Public answer'})
            return self.run
        with patch('web_app.run_agent', side_effect=answer), patch('web_app.load_dotenv'):
            response = self.client.post(retry_url, json={'response_language': 'en'})
        self.assertEqual(response.status_code, 200)
        parsed = events(response.text)
        self.assertIn('delta', [event for event,_ in parsed])
        self.assertEqual(parsed[-1][1]['status'], 'completed')
        turns = self.store.turns(self.chat)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]['id'], original['id'])

    def test_missing_conversation_and_schema_are_http_errors(self):
        self.assertEqual(self.client.post(self.url, json={}).status_code, 422)
        self.assertEqual(self.client.post(self.url, json={**self.payload, "question": "  "}).status_code, 400)
        self.assertEqual(self.client.post("/api/conversations/missing/turns/stream", json=self.payload).status_code, 404)

    def test_busy_conversation_is_http_409_before_meta(self):
        with self.store.generation_lock(self.chat):
            response = self.client.post(self.url, json=self.payload)
        self.assertEqual(response.status_code, 409)
        self.assertNotIn("event:", response.text)

    def test_health_does_not_call_model(self):
        with patch("web_app.run_agent", side_effect=AssertionError("health must not call model")):
            self.assertEqual(self.client.get("/healthz").json(), {"status": "ok"})


if __name__ == "__main__":
    unittest.main()
