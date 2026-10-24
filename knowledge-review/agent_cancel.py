"""Cooperative request cancellation without killing Python worker threads.

Only StreamRunner work binds a token. Existing CLI/background calls keep their
original OpenAI transport. Responses own an async client and event loop until
their synchronous byte stream closes, allowing incremental reads and cancellation.
"""
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
import threading

import httpx


class GenerationCancelled(BaseException):
    # Deliberately bypass SDK retries and broad business `except Exception`.
    # StreamRunner and execute_turn handle this explicitly at their boundaries.
    pass


class Cancellation:
    def __init__(self):
        self.requested = threading.Event()
        self.observed = threading.Event()

    def cancel(self):
        self.requested.set()

    def check(self):
        if self.requested.is_set():
            self.observed.set()
            raise GenerationCancelled()


_current = ContextVar("knowledge_review_cancellation", default=None)


@contextmanager
def cancellation_scope(token):
    previous = _current.set(token)
    try:
        yield
    finally:
        _current.reset(previous)


def checkpoint():
    token = _current.get()
    if token is not None:
        token.check()


class CancellableBody(httpx.SyncByteStream):
    def __init__(self, token):
        self.token = token
        self.runner = asyncio.Runner()
        self.client = None
        self.response = None
        self.closed = False

    async def wait(self, operation):
        task = asyncio.create_task(operation)
        try:
            while not task.done():
                self.token.check()
                await asyncio.wait({task}, timeout=0.02)
            self.token.check()
            return task.result()
        finally:
            if not task.done():
                task.cancel()
            # Await async cleanup before returning to the worker / releasing
            # its permit; merely cancelling a Future is not completion.
            await asyncio.gather(task, return_exceptions=True)

    async def open(self, request):
        self.client = httpx.AsyncClient(follow_redirects=True)
        outbound = httpx.Request(request.method, request.url, headers=request.headers,
                                 content=request.read(), extensions=request.extensions)
        self.response = await self.client.send(outbound, stream=True)
        return self.response

    def __iter__(self):
        iterator = self.response.aiter_raw().__aiter__()
        try:
            while True:
                try:
                    block = self.runner.run(self.wait(anext(iterator)))
                except StopAsyncIteration:
                    break
                yield block
        finally:
            self.close()

    async def cleanup(self):
        try:
            if self.response is not None:
                await self.response.aclose()
        finally:
            if self.client is not None:
                await self.client.aclose()

    def close(self):
        if not self.closed:
            self.closed = True
            try:
                self.runner.run(self.cleanup())
            finally:
                self.runner.close()


class CancellableTransport(httpx.BaseTransport):
    def __init__(self, token):
        self.token = token

    def handle_request(self, request):
        self.token.check()
        stream = CancellableBody(self.token)
        try:
            response = stream.runner.run(stream.wait(stream.open(request)))
            return httpx.Response(response.status_code, headers=response.headers, stream=stream,
                                  extensions={key: value for key, value in response.extensions.items()
                                              if key in ("http_version", "reason_phrase")})
        except BaseException:
            stream.close()
            raise


def model_http_options():
    token = _current.get()
    if token is None:
        return {}
    token.check()
    return {"http_client": httpx.Client(transport=CancellableTransport(token),
                                      timeout=30.0, follow_redirects=True), "max_retries": 0}
