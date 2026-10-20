"""Small lifecycle boundaries for existing persisted outline jobs.

No new scheduler/database: StreamRunner owns capacity, original stores own
attempts and results. Nested reviews reuse the parent's worker and cancellation.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

from agent_cancel import GenerationCancelled, checkpoint


_work = ContextVar('knowledge_review_work', default=None)


@contextmanager
def work_scope(runner, lease):
    previous = _work.set((runner, lease))
    try:
        yield
    finally:
        _work.reset(previous)


def job_status(kind, store, identity):
    if kind == 'outline':
        return store.get(identity)['status']
    if kind == 'grouping':
        from outline_group_store import get_run
        return get_run(store, identity)['status']
    if kind == 'review':
        from outline_review_store import get_review
        return get_review(store, identity)['status']
    if kind == 'route':
        from outline_route_store import route_record
        return route_record(store, identity)['status']
    raise ValueError('Unknown background kind')


def interrupt_job(kind, store, identity):
    # Reuse original attempts/status transitions, preserving completed results.
    if kind == 'outline':
        with store.lock(identity):
            store.recover(identity)
    elif kind == 'grouping':
        from outline_group_steps import pause_grouping
        pause_grouping(store, identity)
    elif kind == 'review':
        from outline_review_store import pause_review
        pause_review(store, identity)
    elif kind == 'route':
        from outline_route_store import pause_route
        pause_route(store, identity)
    else:
        raise ValueError('Unknown background kind')


def job_boundary(kind):
    def decorate(action):
        @wraps(action)
        def run(store, identity, *args, **kwargs):
            owner = _work.get()
            key = (kind, identity)
            if owner is not None and not owner[0]._claim_key(owner[1], key):
                return  # Already owned by another actual worker in this process.
            try:
                checkpoint()
                return action(store, identity, *args, **kwargs)
            except GenerationCancelled:
                interrupt_job(kind, store, identity)
                raise
            finally:
                if owner is not None:
                    owner[0]._drop_alias(owner[1], key)
        return run
    return decorate


class DetachedTask:
    """One existing BackgroundTasks call, collected before reserving execution."""
    def __init__(self):
        self.call = None

    def add_task(self, function, *args, **kwargs):
        if self.call is not None:
            raise RuntimeError('A job submission must schedule exactly one workflow')
        self.call = (function, args, kwargs)
