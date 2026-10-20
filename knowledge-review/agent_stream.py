"""Shared bounded workers for selected synchronous JSON endpoints and SSE.

Disconnecting the consumer requests cooperative cancellation. The admission
slot still stays held until that task actually exits, including I/O cleanup.
"""

import asyncio
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from functools import wraps
import inspect
import json
import logging
import os
from queue import Empty, Full, Queue
import threading
import time

from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse

from agent_cancel import Cancellation, GenerationCancelled, cancellation_scope, checkpoint
from agent_work import DetachedTask, interrupt_job, job_status, work_scope


class WorkLease:
    def __init__(self, kind, token, key=None):
        self.kind, self.token, self.key = kind, token, key
        self.aliases = set()
        self.state = 'reserved'


class EventLimit(RuntimeError):
    pass


def encode(event, data, limit):
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    frame = b"event: " + event.encode("ascii") + b"\ndata: " + payload + b"\n\n"
    if len(frame) > limit:
        raise EventLimit("SSE event exceeds the transport budget")
    return frame


class EventBridge:
    def __init__(self, loop, *, queue_size=32, progress_limit=256 * 1024, result_limit=4 * 1024 * 1024):
        self.ready = loop.create_future()
        self.loop = loop
        self.queue = Queue(maxsize=queue_size)
        self.progress_limit = progress_limit
        self.result_limit = result_limit
        self.progress_bytes = 0
        self.detached = threading.Event()
        self.finished = threading.Event()
        self.delivered = False
        self.terminal = b""
        self.turn_id = None
        self.cancellation = Cancellation()

    def announce(self, error=None):
        def deliver():
            if not self.ready.done():
                if error is None:
                    self.ready.set_result(None)
                else:
                    self.ready.set_exception(error)
        # During server shutdown the task may finish after its event loop closes.
        try:
            self.loop.call_soon_threadsafe(deliver)
        except RuntimeError:
            self.detached.set()

    def emit(self, event, data):
        if event != "meta":
            checkpoint()
        if self.detached.is_set():
            return
        frame = encode(event, data, 8192)
        if self.progress_bytes + len(frame) > self.progress_limit:
            raise EventLimit("SSE progress budget exhausted")
        try:
            self.queue.put_nowait(frame)
        except Full as exc:
            raise EventLimit("SSE progress queue exhausted") from exc
        self.progress_bytes += len(frame)
        if event == "meta":
            self.turn_id = data["turn_id"]
            self.announce()

    def complete(self, turn=None, *, code="GENERATION_FAILED"):
        # Terminal frames use a separate reserved slot: a full progress queue
        # must never swallow the error/done explaining why generation stopped.
        if self.detached.is_set():
            self.finished.set()
            return
        status = "failed"
        try:
            if turn is not None and turn["status"] == "completed":
                self.terminal = encode("result", {"turn": turn}, self.result_limit)
                status = "completed"
            else:
                raise EventLimit("No completed result")
        except EventLimit:
            if turn is not None and turn["status"] == "completed":
                code = "RESULT_TOO_LARGE"
            self.terminal = encode("error", {"code": code, "message": "生成或传输未完成，请读取聊天记录确认状态。",
                                            "turn_id": self.turn_id}, 8192)
        self.terminal += encode("done", {"status": status, "turn_id": self.turn_id}, 8192)
        self.finished.set()

    async def events(self, heartbeat_seconds=15):
        last_write = time.monotonic()
        try:
            while True:
                try:
                    frame = self.queue.get_nowait()
                except Empty:
                    if self.finished.is_set():
                        # A producer can enqueue its last progress event between
                        # get_nowait and finished; drain it before terminal frames.
                        while not self.queue.empty():
                            yield self.queue.get_nowait()
                        if self.terminal:
                            yield self.terminal
                        self.delivered = True
                        return
                    if time.monotonic() - last_write >= heartbeat_seconds:
                        yield b": ping\n\n"
                        last_write = time.monotonic()
                    await asyncio.sleep(0.02)
                else:
                    yield frame
                    last_write = time.monotonic()
        finally:
            if not self.delivered:
                self.cancellation.cancel()
            self.detached.set()
            self.terminal = b""
            while not self.queue.empty():
                self.queue.get_nowait()


class EventResponse(StreamingResponse):
    def __init__(self, bridge):
        self.bridge = bridge
        super().__init__(bridge.events(), media_type="text/event-stream",
                         headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # A failed response-start write can happen before the generator
            # ever starts, so its finally alone cannot own cancellation.
            if not self.bridge.delivered:
                self.bridge.cancellation.cancel()
                self.bridge.detached.set()


class StreamRunner:
    def __init__(self, workers=None):
        if workers is None:
            configured = os.environ.get("KNOWLEDGE_REVIEW_MODEL_WORKERS", "4")
            if not configured.isascii() or not configured.isdecimal():
                raise ValueError("KNOWLEDGE_REVIEW_MODEL_WORKERS must be an integer from 1 to 32")
            workers = int(configured)
        if not 1 <= workers <= 32:
            raise ValueError("KNOWLEDGE_REVIEW_MODEL_WORKERS must be an integer from 1 to 32")
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="agent-model")
        # ThreadPoolExecutor's own queue is unbounded; admit before submit.
        self.slots = threading.BoundedSemaphore(workers)
        self.workers = workers
        self.started_at = time.monotonic()
        self.closed = False
        self.tokens = set()
        self.tokens_lock = threading.RLock()
        self.keys = {}
        fields = ('reserved', 'running', 'submitted', 'started', 'exited', 'completed', 'failed',
                  'cancelled', 'cancel_requested', 'paused', 'stale', 'other', 'rejected', 'deduplicated', 'skipped')
        self.work_counts = {kind: Counter(dict.fromkeys(fields, 0)) for kind in ('request', 'background', 'title')}

    @asynccontextmanager
    async def lifespan(self, app):
        try:
            yield
        finally:
            self.closed = True
            with self.tokens_lock:
                for token in self.tokens:
                    token.cancel()
            self.pool.shutdown(wait=False, cancel_futures=False)

    def snapshot(self):
        with self.tokens_lock:
            counts = {kind: dict(row) for kind, row in self.work_counts.items()}
            return {'schema_version': 1, 'scope': 'python_process', 'workers': self.workers,
                    'uptime_ms': int((time.monotonic() - self.started_at) * 1000),
                    'held_slots': sum(row['reserved'] + row['running'] for row in counts.values()),
                    'queued': 0, 'queue_limit': 0, 'owned_keys': len(self.keys), 'types': counts}

    def _reserve(self, kind, token=None, key=None):
        with self.tokens_lock:
            row = self.work_counts[kind]
            if key is not None and key in self.keys:
                row['deduplicated'] += 1
                return None
            occupied = sum(value['reserved'] + value['running'] for value in self.work_counts.values())
            title_busy = (kind == 'title' and (row['reserved'] + row['running'] >= 1 or
                          (self.workers > 1 and occupied >= self.workers - 1)))
            if self.closed or title_busy or not self.slots.acquire(blocking=False):
                row['rejected'] += 1
                raise HTTPException(503, "模型执行槽位已满，请稍后重试。", headers={"Retry-After": "1"})
            lease = WorkLease(kind, token or Cancellation(), key)
            self.tokens.add(lease.token)
            row['reserved'] += 1
            if key is not None:
                self.keys[key] = lease
            return lease

    def _claim_key(self, lease, key):
        with self.tokens_lock:
            previous = self.keys.get(key)
            if previous is not None and previous is not lease:
                self.work_counts[lease.kind]['deduplicated'] += 1
                return False
            self.keys[key] = lease
            if key != lease.key:
                lease.aliases.add(key)
            return True

    def _drop_alias(self, lease, key):
        with self.tokens_lock:
            if key in lease.aliases and self.keys.get(key) is lease:
                del self.keys[key]
                lease.aliases.discard(key)

    def cancel_job(self, kind, identity):
        with self.tokens_lock:
            lease = self.keys.get((kind, identity))
            if lease is not None:
                lease.token.cancel()

    def _release(self, lease, outcome=None):
        with self.tokens_lock:
            if lease.state == 'released':
                return
            row = self.work_counts[lease.kind]
            row[lease.state] -= 1
            if outcome is not None:
                row['exited'] += 1
                if lease.token.requested.is_set():
                    row['cancel_requested'] += 1
                row[outcome if outcome in ('completed', 'failed', 'cancelled', 'paused', 'stale', 'skipped') else 'other'] += 1
            for key in [lease.key, *lease.aliases]:
                if self.keys.get(key) is lease:
                    del self.keys[key]
            self.tokens.discard(lease.token)
            lease.state = 'released'
            self.slots.release()

    def _submit(self, action, token=None, *, lease=None, after_released=None):
        lease = lease or self._reserve('request', token)
        def work():
            with self.tokens_lock:
                row = self.work_counts[lease.kind]
                row['reserved'] -= 1
                row['running'] += 1
                row['started'] += 1
                lease.state = 'running'
            with cancellation_scope(lease.token), work_scope(self, lease):
                # Background domain boundaries must see pre-start cancellation
                # too, so their already-persisted queued record is settled.
                if lease.kind != 'background':
                    lease.token.check()
                return action()
        try:
            with self.tokens_lock:
                if self.closed:
                    raise RuntimeError('Worker entry closed')
                future = self.pool.submit(work)
                self.work_counts[lease.kind]['submitted'] += 1
        except RuntimeError:
            self._release(lease)
            raise HTTPException(503, "服务正在停止。") from None
        # Release after actual completion, even if the awaiting HTTP task was cancelled.
        def finished(done):
            error = GenerationCancelled() if done.cancelled() else done.exception()
            outcome = 'cancelled' if lease.token.observed.is_set() or isinstance(error, GenerationCancelled) else 'failed' if error else 'completed'
            result = None if error else done.result()
            if not error and outcome != 'cancelled':
                if isinstance(result, dict) and result.get('status') in ('completed', 'failed'):
                    outcome = result['status']
                elif lease.kind != 'request' and isinstance(result, str):
                    outcome = result
                elif lease.kind == 'title' and result is None:
                    outcome = 'skipped'
            self._release(lease, outcome)
            if after_released is not None and not error and outcome == 'completed' and not lease.token.requested.is_set():
                try:
                    after_released()
                except Exception as exc:
                    logging.getLogger(__name__).warning('Work postprocessing failed (%s)', type(exc).__name__)
        future.add_done_callback(finished)
        return future

    def title(self, store, identity, language):
        from conversation_title import auto_title
        try:
            chat = store.get_conversation(identity)
        except KeyError:
            return
        cooling = False
        if chat['title_state'] == 'failed' and chat.get('title_started_at'):
            cooling = (datetime.now(timezone.utc) - datetime.fromisoformat(chat['title_started_at'])).total_seconds() < 30
        if chat['title_state'] in ('manual', 'generated') or cooling:
            with self.tokens_lock:
                self.work_counts['title']['skipped'] += 1
            return
        try:
            lease = self._reserve('title', key=('title', identity))
        except HTTPException:
            return  # Best effort, no queue or unbounded retained retry closures.
        if lease is not None:
            try:
                self._submit(lambda: auto_title(store, identity, language), lease=lease)
            except HTTPException:
                self._release(lease)

    def detached(self, kind, *, resume=False):
        def decorate(action):
            signature = inspect.signature(action)
            @wraps(action)
            def endpoint(*args, **kwargs):
                arguments = signature.bind(*args, **kwargs)
                if resume:
                    with self.tokens_lock:
                        if (kind, arguments.arguments['identity']) in self.keys:
                            raise HTTPException(409, '任务仍在执行或清理，请稍后继续。')
                lease = self._reserve('background')
                collector = DetachedTask()
                arguments.arguments['background'] = collector
                try:
                    result = action(*arguments.args, **arguments.kwargs)
                    if collector.call is not None:
                        function, positional, keywords = collector.call
                        store, identity = positional[:2]
                        key = (kind, identity)
                        if self._claim_key(lease, key):
                            lease.key = key
                            lease.aliases.discard(key)
                            def work():
                                function(*positional, **keywords)
                                return job_status(kind, store, identity)
                            try:
                                self._submit(work, lease=lease)
                            except BaseException:
                                interrupt_job(kind, store, identity)
                                raise
                            lease = None  # Completion callback now owns the slot.
                    return result
                finally:
                    if lease is not None:
                        self._release(lease)
            return endpoint
        return decorate

    async def _watch_disconnect(self, request, token):
        while True:
            if await request.is_disconnected():
                token.cancel()
                return
            await asyncio.sleep(0.02)

    async def run(self, action, request=None):
        token = Cancellation()
        future = asyncio.wrap_future(self._submit(action, token))
        # Observe late exceptions after the caller stops awaiting this work.
        future.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
        watcher = asyncio.create_task(self._watch_disconnect(request, token)) if request is not None else None
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            token.cancel()
            raise
        except GenerationCancelled:
            raise HTTPException(499, "请求已取消，已保存的数据请读取记录确认。") from None
        finally:
            if watcher is not None:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)

    def bounded(self, action):
        @wraps(action)
        async def endpoint(*args, **kwargs):
            request = kwargs.pop("gateway_request")
            return await self.run(lambda: action(*args, **kwargs), request)
        # FastAPI injects Request without adding a query/body field. Preserve all
        # existing payload/path/BackgroundTasks parameters for the seven routes.
        signature = inspect.signature(action)
        endpoint.__signature__ = signature.replace(parameters=[*signature.parameters.values(),
            inspect.Parameter("gateway_request", inspect.Parameter.KEYWORD_ONLY, annotation=Request)])
        return endpoint

    async def response(self, action, on_completed=None, request=None, after_released=None):
        bridge = EventBridge(asyncio.get_running_loop())

        def work():
            turn = None
            try:
                turn = action(bridge.emit)
                bridge.complete(turn, code="GENERATION_CANCELLED" if bridge.cancellation.requested.is_set()
                                else "GENERATION_FAILED")
                return turn
            except GenerationCancelled:
                if bridge.turn_id is None:
                    bridge.announce(HTTPException(499, "请求已取消。"))
                else:
                    bridge.complete(code="GENERATION_CANCELLED")
                return {'status': 'failed'}
            except Exception as exc:
                if bridge.turn_id is None:
                    bridge.announce(exc if isinstance(exc, HTTPException) else HTTPException(500, "流式任务未启动。"))
                else:
                    bridge.complete(code="STREAM_LIMIT" if isinstance(exc, EventLimit) else "GENERATION_FAILED")
                return {'status': 'failed'}
            finally:
                try:
                    if (not bridge.cancellation.requested.is_set() and on_completed is not None
                            and turn is not None and turn["status"] == "completed"):
                        on_completed()
                except GenerationCancelled:
                    pass
                except Exception as exc:
                    logging.getLogger(__name__).warning("SSE postprocessing failed (%s)", type(exc).__name__)

        options = {'after_released': after_released} if after_released is not None else {}
        submitted = self._submit(work, bridge.cancellation, **options)
        def before_start_failure(done):
            # Cancellation can win before the worker enters work() / emits meta.
            # Resolve the pre-header waiter even in that narrow race.
            if bridge.turn_id is None:
                error = GenerationCancelled() if done.cancelled() else done.exception()
                bridge.announce(HTTPException(499 if isinstance(error, GenerationCancelled) else 500,
                                              "流式任务未启动。"))
        submitted.add_done_callback(before_start_failure)
        watcher = (asyncio.create_task(self._watch_disconnect(request, bridge.cancellation))
                   if request is not None else None)
        try:
            await bridge.ready
        except BaseException:
            bridge.cancellation.cancel()
            bridge.detached.set()
            raise
        finally:
            if watcher is not None:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
        return EventResponse(bridge)
