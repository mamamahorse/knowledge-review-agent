"""Run-local actual HTTP limits for small model operations, including fallbacks."""
from contextlib import contextmanager
from contextvars import ContextVar
import json
from time import perf_counter
import httpx

_current=ContextVar('model_request_budget',default=None)


class RequestLimit(BaseException):
    """A hard resource stop, deliberately not converted into an SDK retry."""


class RequestBudget:
    def __init__(self,limit,changed=lambda:None):
        self.limit=limit; self.records=[]; self.pending={}; self.changed=changed

    def request(self,request):
        if len(self.records)>=self.limit:
            raise RequestLimit('本次实际模型请求额度已用完；已完成描述保留。')
        try: data=json.loads(request.content)
        except (ValueError,UnicodeError): data={}
        record=dict(number=len(self.records)+1,status='pending',http_status=None,
            model=data.get('model'),thinking=data.get('thinking'),
            output_budget=data.get('max_tokens',data.get('max_completion_tokens')),
            endpoint=str(request.url.copy_with(username=None,password=None,query=None,fragment=None)),
            protocol=(data.get('response_format') or {}).get('type') or ('tools' if data.get('tools') else 'text'),
            input_tokens=None,output_tokens=None,cached_tokens=None,reasoning_tokens=None,elapsed_ms=None)
        self.records.append(record); self.pending[id(request)]=(record,perf_counter()); self.changed()

    def response(self,response):
        entry=self.pending.pop(id(response.request),None)
        if entry is None: return
        record,start=entry
        record.update(http_status=response.status_code,elapsed_ms=(perf_counter()-start)*1000,status='response')
        try:
            response.read(); data=response.json()
            if isinstance(data,dict):
                from learning_metrics import usage
                record.update(usage(data),returned_model=data.get('model'))
        except (ValueError,UnicodeError): pass
        except BaseException as exc:
            record.update(status='response_body_error',error_type=type(exc).__name__)
            raise
        finally:
            record['elapsed_ms']=(perf_counter()-start)*1000
            self.changed()

    def failed(self,request,error):
        entry=self.pending.pop(id(request),None)
        if entry:
            record,start=entry
            record.update(status='transport_error_or_cancelled',error_type=type(error).__name__,elapsed_ms=(perf_counter()-start)*1000)
            self.changed()

    def finish(self):
        for record,start in self.pending.values():
            record.update(status='transport_error_or_cancelled',elapsed_ms=(perf_counter()-start)*1000)
        self.pending.clear(); self.changed()


@contextmanager
def request_budget(budget):
    token=_current.set(budget)
    try: yield budget
    finally:
        try: budget.finish()
        finally: _current.reset(token)


def attach(options):
    budget=_current.get()
    if budget is None: return options
    client=options.get('http_client') or httpx.Client(timeout=60,follow_redirects=True)
    client.event_hooks['request'].append(budget.request)
    client.event_hooks['response'].append(budget.response)
    # HTTPX has no transport-error hook. Observe send without replacing the
    # existing cancellation transport or learning telemetry client.
    send=client.send
    def observed_send(request,*args,**kwargs):
        try: return send(request,*args,**kwargs)
        except BaseException as exc:
            budget.failed(request,exc)
            raise
    client.send=observed_send
    return {**options,'http_client':client,'max_retries':0}
