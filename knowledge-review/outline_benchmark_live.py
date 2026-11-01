"""Serial, isolated execution of the human-approved compact outline experiment.

Business algorithms are called unchanged. The adapter supplies a scoped budget,
explicit model controls, and an HTTP-attempt ledger including failed fallbacks.
No key, HTTP authentication header or hidden reasoning is written to artifacts.
"""

import argparse
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime,timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import time
from unittest.mock import patch

import httpx
from openai import OpenAI

from generation_budget import GenerationBudget
from model_client import model_settings
from outline_benchmark import OBJECTIVE,load_source,snapshot,validate_artifacts
from outline_benchmark_compact import COMPACT_RUN,verify_parent
from outline_benchmark_guard import (AuditTransport,RequestLedger,EvaluationStopped,
    require_approval,limits_from_protocol,dollar_cost)
from outline_benchmark_prepare import write_json,digest
from structured_output import FUNCTION_OUTPUT_PROVIDERS


def safe_error_details(data, secrets=()):
    """Keep actionable provider errors without headers, keys or hidden reasoning."""
    error=data.get('error') if isinstance(data,dict) else None
    if not isinstance(error,dict):return None
    result={}
    for key in ('message','param','code','type'):
        value=error.get(key)
        if value is not None:
            value=str(value)
            for secret in secrets:
                if secret:value=value.replace(secret,'[REDACTED]')
            value=re.sub(r'(?i)bearer\s+\S+','Bearer [REDACTED]',value)
            value=re.sub(r'\bsk-[A-Za-z0-9_-]+','[REDACTED]',value)
            if key=='message':result['message_truncated']=len(value)>8000
            value=value[:8000]
        result[key]=value
    return result


def classify_protocol_400(error,payload):
    """Narrow evidence, never HTTP400 alone. Unclassified errors stop the smoke."""
    unknown=dict(category='unclassified_400',allow_fallback=False)
    if not error or error.get('message_truncated'):return unknown
    message=(error.get('message') or '').lower().strip().rstrip('.')
    parameter=(error.get('param') or '').lower()
    code=(error.get('code') or '').lower()
    if 'thinking' in message or 'reasoning_effort' in message:
        return dict(category='thinking_mode_constraint',allow_fallback=False)
    if any(token in message for token in ('invalid schema','invalid json schema','schema validation',
            'additionalproperties','unsupported keyword','$ref','required property')):
        return dict(category='schema_validation_error',allow_fallback=False)
    unsupported=any(token in message for token in ('unsupported','unavailable','not supported','does not support'))
    specific_codes={'unsupported_parameter','unsupported_value','unsupported_response_format','not_supported'}
    form=payload.get('response_format')
    if isinstance(form,dict) and form.get('type')=='json_schema':
        exact={"response_format unsupported","response_format is unavailable",
            "response_format type 'json_schema' is not supported","response_format type 'json_schema' is unavailable",
            "response_format.type 'json_schema' is not supported","response_format json_schema is not supported"}
        if message in exact or (parameter in {'response_format','response_format.type'} and code in specific_codes and unsupported):
            return dict(category='json_schema_mode_unavailable',allow_fallback=True)
    if isinstance(payload.get('tool_choice'),dict):
        exact={'forced tool_choice is not supported','forced tool_choice is unavailable','tool_choice function is not supported'}
        if message in exact or (parameter=='tool_choice' and code in specific_codes and unsupported):
            return dict(category='forced_tool_mode_unavailable',allow_fallback=True)
    return unknown


class ForwardTransport(httpx.BaseTransport):
    """Keep normal httpx proxy/TLS behavior underneath the audit transport."""
    def __init__(self,client):self.client=client
    def handle_request(self,request):return self.client.send(request)
    def close(self):self.client.close()


class ControlledAuditTransport(AuditTransport):
    def __init__(self,inner,runner,kind,task,budget,**kwargs):
        self.runner=runner;self.budget=budget
        self.controls=runner.protocol['generation' if kind=='generation' else 'judge']
        super().__init__(inner,runner.ledger,kind,task,
            runner.protocol['per_request_reservation_usd'][kind],runner.directory,**kwargs)

    def handle_request(self,request):
        body=json.loads(request.content)
        before_http=getattr(self.runner,'before_http',None)
        if before_http:before_http(self.kind,self.task)
        if body.get('model')!=self.controls['model']:
            raise EvaluationStopped('Requested model differs from approved configuration')
        if body.get('max_tokens')!=self.budget.output_tokens:
            raise EvaluationStopped('Output limit differs from the saved task budget')
        if str(request.url).split('/chat/completions')[0].rstrip('/') not in (
            self.controls['endpoint'].rstrip('/'),self.controls['endpoint'].rstrip('/')+'/v1'):
            raise EvaluationStopped('Requested endpoint differs from approved configuration')
        body['thinking']=self.controls['thinking']
        if self.controls['thinking'].get('type')=='disabled':body.pop('reasoning_effort',None)
        elif self.controls.get('reasoning_effort'):body['reasoning_effort']=self.controls['reasoning_effort']
        self.budget.check(body['messages'],**{k:v for k,v in body.items() if k!='messages'})
        content=json.dumps(body,ensure_ascii=False).encode('utf-8')
        headers=request.headers.copy();headers['content-length']=str(len(content))
        outbound=httpx.Request(request.method,request.url,headers=headers,content=content,extensions=request.extensions)
        count=len(self.ledger.events);cache_before={str(k):v for k,v in FUNCTION_OUTPUT_PROVIDERS.items()}
        response=super().handle_request(outbound)
        end=self.ledger.events[-1];starts=[e for e in self.ledger.events[count:] if e['event']=='start']
        if starts:
            identity=starts[-1]['request_id']
            # Public source material only; schema/result text, never hidden reasoning.
            try:data=response.json()
            except ValueError:data={}
            choices=[]
            for c in data.get('choices',[]) if isinstance(data,dict) else []:
                message=c.get('message',{})
                choices.append(dict(finish_reason=c.get('finish_reason'),
                    content=message.get('content'),tool_calls=message.get('tool_calls'),refusal=message.get('refusal')))
            secrets=[getattr(self.runner,'options',{}).get('api_key')]
            details=safe_error_details(data,secrets)
            classification=classify_protocol_400(details,body) if response.status_code==400 else None
            usage=data.get('usage') if isinstance(data,dict) else None
            reported_reasoning=((usage.get('completion_tokens_details') or {}).get('reasoning_tokens')
                                if isinstance(usage,dict) else None)
            reasoning_present=any(bool(c.get('message',{}).get('reasoning_content'))
                                  for c in data.get('choices',[])) if isinstance(data,dict) else False
            write_json(self.runner.live/'calls'/(identity+'.json'),dict(request_id=identity,task=self.task,kind=self.kind,
                payload=body,response=dict(id=data.get('id'),model=data.get('model'),choices=choices,
                    usage=usage,error_type=(details or {}).get('type'),error=details) if isinstance(data,dict) else {},
                http_status=response.status_code,error_classification=classification,
                thinking_sent=body['thinking'],reasoning_effort_sent=body.get('reasoning_effort'),
                reported_reasoning_tokens=reported_reasoning,reasoning_content_present=reasoning_present,
                cache_before=cache_before))
            if self.runner.protocol.get('strict_protocol_errors') and classification and not classification['allow_fallback']:
                self.runner.fatal=classification['category']
            on_result=getattr(self.runner,'on_provider_result',None)
            if on_result:on_result(response.status_code,data,classification)
        if response.status_code in (401,403,429) or response.status_code>=500:
            self.runner.fatal=f'provider HTTP {response.status_code}'
        returned=end.get('returned_model')
        if returned:
            baseline=self.runner.returned_models.get(self.kind)
            if baseline is not None and baseline!=returned:
                self.runner.fatal='returned model identifier drift'
                raise EvaluationStopped(self.runner.fatal)
            self.runner.returned_models[self.kind]=returned
        return response


class Runner:
    def __init__(self,directory=COMPACT_RUN):
        self.directory=Path(directory);require_approval(self.directory)
        self.protocol=json.loads((self.directory/'PROTOCOL.json').read_text(encoding='utf-8'))
        self.plan=json.loads((self.directory/'planning.json').read_text(encoding='utf-8'))
        self.manifest=validate_artifacts(self.directory);self.docs={d['id']:d for d in self.manifest['documents']}
        self.live=self.directory/'live'
        for name in ('calls','jobs','judge'): (self.live/name).mkdir(parents=True,exist_ok=True)
        self.ledger=RequestLedger(self.live/'requests.jsonl',limits_from_protocol(self.protocol))
        self.returned_models={};self.fatal=None;self.results=[]
        model,options=model_settings();expected=self.protocol['generation']
        if model!=expected['model'] or sha256(str(options.get('base_url','openai-default')).encode()).hexdigest()!=expected['endpoint_sha256']:
            raise EvaluationStopped('Local configured model/endpoint differs from approved identity')
        self.options=options
        self.actual_model=model

    def client(self,kind,task,budget):
        controls=self.protocol['generation' if kind=='generation' else 'judge']
        forwarded=httpx.Client(timeout=controls['timeout_seconds'],follow_redirects=False)
        transport=ControlledAuditTransport(ForwardTransport(forwarded),self,kind,task,budget)
        http=httpx.Client(transport=transport,timeout=controls['timeout_seconds'],follow_redirects=False)
        return OpenAI(api_key=self.options['api_key'],base_url=controls['endpoint'],http_client=http,
            timeout=controls['timeout_seconds'],max_retries=0)

    @contextmanager
    def model_scope(self,kind,task,budget):
        def factory(**options):return self.client(kind,task,budget)
        with patch.object(GenerationBudget,'from_env',return_value=budget),patch('practice_model.OpenAI',side_effect=factory):
            yield

    def generate(self,task):
        from learning_store import LearningStore
        from library_store import LibraryStore
        from outline_store import OutlineStore
        from outline_service import scope_fingerprint,run_outline
        require_approval(self.directory)
        task_id=task['task_id'];doc=self.docs[task['document_id']];source=load_source(self.directory,doc)
        practice=LearningStore(self.live/'runs.sqlite3');library=LibraryStore(practice);store=OutlineStore(practice)
        collection=library.create(task_id)['id'];snapshots=[snapshot(doc,source)]
        identity=store.create(collection,task_id,OBJECTIVE,'zh',scope_fingerprint(snapshots),
            snapshots,deepcopy(task['sections_plan']),deepcopy(task['batches_plan']),deepcopy(task['plan']))
        budget=GenerationBudget(task['context_tokens'],4096,2048,'approved isolated benchmark task')
        started=time.monotonic();first=None;original_finish=store.finish_batch
        def finish(*args,**kwargs):
            nonlocal first
            ok=original_finish(*args,**kwargs)
            if ok and args[1] and first is None:first=time.monotonic()-started
            return ok
        ledger_start=len([e for e in self.ledger.events if e['event']=='start'])
        print('START',task_id,flush=True)
        thrown=None
        try:
            with self.model_scope('generation',task_id,budget),patch.object(store,'finish_batch',side_effect=finish):
                run_outline(store,identity,lambda _collection:snapshots)
        except Exception as exc:
            thrown=type(exc).__name__
        elapsed=time.monotonic()-started;job=store.get(identity)
        # Persist generated proposals and their actual per-batch visible ranges.
        result=dict(task_id=task_id,document_id=task['document_id'],arm=task['arm'],status=job['status'],
            elapsed_seconds=elapsed,first_formal_candidate_seconds=first,
            completed_seconds=elapsed if job['status']=='completed' else None,
            actual_requests=len([e for e in self.ledger.events if e['event']=='start'])-ledger_start,
            thrown_type=thrown,job=job)
        write_json(self.live/'jobs'/(task_id+'.json'),result);self.results.append(result)
        write_json(self.live/'progress.json',dict(phase='generation',tasks=[{k:v for k,v in r.items() if k!='job'} for r in self.results]))
        print('END',task_id,job['status'],result['actual_requests'],len(job['candidates']),round(elapsed,3),flush=True)
        if self.fatal:raise EvaluationStopped(self.fatal)
        if thrown:raise EvaluationStopped('Unexpected task execution error: '+thrown)
        starts=[e for e in self.ledger.events if e['event']=='start' and e['task']==task_id and e['kind']=='generation']
        ends={e['request_id']:e for e in self.ledger.events if e['event']=='end'}
        if starts and ends.get(starts[-1]['request_id'],{}).get('http_status',200)==400 and job['status']!='completed':
            raise EvaluationStopped('Unrecoverable structured-output protocol error')
        return result

    def summary(self):
        starts=[e for e in self.ledger.events if e['event']=='start'];ends={e['request_id']:e for e in self.ledger.events if e['event']=='end'}
        records=[]
        for s in starts:
            e=ends.get(s['request_id'],{})
            records.append(dict(**s,end=e,computed_peak_price_usd=dollar_cost(e.get('usage'),self.protocol['price'])))
        return dict(actual_requests=len(starts),generation_requests=sum(s['kind']=='generation' for s in starts),
            judge_requests=sum(s['kind']=='judge' for s in starts),
            reserved_usd=sum(s['reserved_usd'] for s in starts),
            known_computed_peak_price_usd=sum(r['computed_peak_price_usd'] or 0 for r in records),
            cost_unknown_requests=sum(r['computed_peak_price_usd'] is None for r in records),
            provider_actual_bill_usd=None,requests=records,returned_models=self.returned_models)

    def save_stop(self,phase,error):
        summary=self.summary();write_json(self.live/'summary.json',summary)
        write_json(self.live/'campaign.json',dict(status='stopped',phase=phase,error=str(error),
            executed_task_ids=[r['task_id'] for r in self.results],
            completed_task_ids=[r['task_id'] for r in self.results if r['status']=='completed'],
            failed_task_ids=[r['task_id'] for r in self.results if r['status']!='completed'],
            remaining_tasks=[t['task_id'] for t in self.plan['tasks'] if t['task_id'] not in {r['task_id'] for r in self.results}],
            totals={k:v for k,v in summary.items() if k!='requests'}))
        lines=['# 精简清单实验：停止报告','',f'阶段：{phase}。停止原因：{error}。','',
            '用户已确认Gold；仅使用精简资料。没有修改原资料、Gold或默认算法。','',
            '|任务|状态|实际HTTP|正式候选|运行至结束/停止（秒）|','|---|---|---:|---:|---:|']
        for r in self.results:lines.append(f"|{r['task_id']}|{r['status']}|{r['actual_requests']}|{len(r['job']['candidates'])}|{r['elapsed_seconds']:.3f}|")
        lines += ['',f"实际生成请求{summary['generation_requests']}，Judge请求{summary['judge_requests']}，保守预留US${summary['reserved_usd']:.3f}。",
            f"有usage且计价字段齐全的峰值单价计算值合计US${summary['known_computed_peak_price_usd']:.6f}；{summary['cost_unknown_requests']}次费用未知，不能当零。供应商实际扣费未知。",'',
            '99是原离线逻辑下界，不是真实成本。未完成任务与未评分部分不作质量结论；不能据此比较完整教材/整份RFC或切换默认算法。',
            '没有自动重试到成功、切模型、扩额或重新解析PDF。认证/协议/容量等异常按保护规则停止。','',
            '## D05公式解析问题的证据边界','',
            '代码仅给派生第2页table_structure_needs_review。≠变=及分数/求和压平由准备阶段Agent视觉对照原PDF后识别，不是公式语义校验器报告。',
            'format_context会把partial及通用issue传给生成模型，但不会把Gold中的具体错误诊断发给生成模型。模型可能提示证据不足，也可能照着错误文本提取；未运行到D05时不能声称已观察其行为。','']
        (self.directory/'EXPERIMENT_REPORT.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--smoke-generation-only',action='store_true');args=parser.parse_args()
    runner=Runner();lock=runner.live/'running.lock'
    if (runner.live/'campaign.json').exists() or list((runner.live/'jobs').glob('*.json')):
        print('STOP Existing campaign retained; this command cannot retry or overwrite it',flush=True)
        return
    # An interrupted campaign must be inspected; this command never silently resumes.
    with lock.open('x',encoding='utf-8') as f:f.write(datetime.now(timezone.utc).isoformat())
    try:
        FUNCTION_OUTPUT_PROVIDERS.clear()
        for task in runner.plan['tasks']:
            if task['document_id']!='D01':continue
            r=runner.generate(task)
            if r['status']!='completed':raise EvaluationStopped('Smoke output/store/validation did not complete')
        write_json(runner.live/'summary.json',runner.summary())
        write_json(runner.live/'campaign.json',dict(status='generation_smoke_complete',next='judge smoke required before remaining generation'))
    except Exception as exc:
        runner.save_stop('D01 generation smoke',exc);print('STOP',str(exc),flush=True)
    finally:lock.unlink(missing_ok=True)


if __name__=='__main__':main()
