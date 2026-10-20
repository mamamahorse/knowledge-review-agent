"""Cooperative request cancellation without killing Python worker threads.

Only StreamRunner work binds a token. Existing CLI/background calls keep their
original OpenAI transport. The synchronous SDK still receives complete JSON;
its HTTP exchange runs on an owned async client so cancellation can close I/O.
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


class CancellableTransport(httpx.BaseTransport):
    def __init__(self, token):
        self.token = token

    def handle_request(self, request):
        self.token.check()
        # These SDK paths send bounded, already serialized JSON, not streaming
        # uploads. Copy before entering asyncio so no sync iterator runs there.
        content = request.read()
        return asyncio.run(self._exchange(request, content))

    async def _exchange(self, request, content):
        async def exchange():
            async with httpx.AsyncClient(follow_redirects=True) as client:
                outbound = httpx.Request(request.method, request.url, headers=request.headers,
                                         content=content, extensions=request.extensions)
                response = await client.send(outbound, stream=True)
                try:
                    # Keep wire encoding intact: the outer sync httpx client
                    # performs its normal decompression exactly once.
                    body = b"".join([block async for block in response.aiter_raw()])
                    return httpx.Response(response.status_code, headers=response.headers, content=body,
                                          extensions={key: value for key, value in response.extensions.items()
                                                      if key in ("http_version", "reason_phrase")})
                finally:
                    await response.aclose()

        task = asyncio.create_task(exchange())
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


def model_http_options():
    token = _current.get()
    if token is None:
        return {}
    token.check()
    return {"http_client": httpx.Client(transport=CancellableTransport(token),
                                      timeout=30.0, follow_redirects=True), "max_retries": 0}
