"""Small isolated outline comparison support; planning cannot issue model calls.

Gold and raw materials stay outside generation prompts. Live execution requires a
separate human-approved manifest; this module's prepare/validate path is offline.
"""

import argparse
from contextlib import ExitStack, contextmanager
from dataclasses import asdict
from hashlib import sha256
import json
import math
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from generation_budget import GenerationBudget
from outline_benchmark_prepare import DEFAULT_RUN, digest, write_json
from sources import load_sources, source_from_document
from document_model import Document

ARMS = dict(A=('v1',32768),B=('v1',131072),C=('v2',32768),D=('v2',131072))
OBJECTIVE = '系统提取资料中值得学习的知识与可判分能力目标，保留事实、适用条件、例外及跨章关系，避免无必要重复。'
CODE_FILES = ('outline_context.py','outline_service.py','outline_v2.py','outline_store.py',
              'generation_budget.py','structured_output.py','practice_model.py','document_model.py','chunking.py')


def load_source(directory, doc):
    if doc.get('document_snapshot'):
        parsed = Document.from_dict(json.loads((directory/doc['document_snapshot']).read_text(encoding='utf-8')))
        raw = next(p for p in doc['raw_files'] if p['path'].endswith('.pdf'))
        return source_from_document(directory/raw['path'],parsed)
    return load_sources([directory/doc['source_file']])[0]


def snapshot(doc, source):
    result = dict(document_id=doc['id'],source_id=source.source_id,path=str(source.path),
                  name=doc['title'],text=source.text)
    if source.document and source.document.format=='pdf':
        result['document']=source.document.to_dict()
    return result


@contextmanager
def offline_scope(capacity, model_options=None):
    """Only request accounting is adapted; planners and output schemas are unchanged."""
    budget=GenerationBudget(capacity,4096,2048,'isolated benchmark application policy')
    with ExitStack() as stack:
        for name in ('model_client.model_settings','outline_service.model_settings'):
            stack.enter_context(patch(name,return_value=('deepseek-flash',model_options or {})))
        stack.enter_context(patch.object(GenerationBudget,'from_env',return_value=budget))
        stack.enter_context(patch('openai.OpenAI',side_effect=AssertionError('offline planning must not construct SDK clients')))
        stack.enter_context(patch('practice_model.OpenAI',side_effect=AssertionError('offline planning cannot create model clients')))
        yield budget


def reduction_minimum(size):
    if size==0:return 0
    total=0
    while True:
        size=math.ceil(size/4)
        total+=size
        if size==1:return total


def plan_one(directory,doc,arm,*,model_options=None,actual_request_limit=16):
    from outline_context import plan_outline, request_diagnostic
    from outline_v2 import plan_direct, source_units, window_for, direct_messages, DirectOutlineDraft
    source=load_source(directory,doc); saved=snapshot(doc,source)
    algorithm,capacity=ARMS[arm]
    with offline_scope(capacity,model_options) as budget:
        if algorithm=='v1':
            sections,batches,plan=plan_outline([source],[saved],OBJECTIVE,'zh',budget)
            complete=plan['documents'][0]['budget'] if plan['documents'] else budget.diagnostic(0)
            mode=plan['documents'][0]['mode'] if plan['documents'] else 'empty'
            active=[b for b in batches if b.get('status')!='completed']
            leaves=len(active) if mode=='hierarchical' else 0
            section_sizes=[sum(b.get('section_id')==s['id'] for b in active) for s in sections if s['has_content']]
            chapters=sum(reduction_minimum(n) for n in section_sizes) if mode=='hierarchical' else 0
            root=reduction_minimum(sum(n>0 for n in section_sizes)) if mode=='hierarchical' else 0
            overview_minimum=leaves+chapters+root
        else:
            sections,batches,plan=plan_direct([source],[saved],OBJECTIVE,'zh',capacity)
            full=window_for(source,list(source_units(source,sections))) if source.text.splitlines() else window_for(source,[])
            complete=request_diagnostic(budget,direct_messages(source,full,OBJECTIVE,'zh'),DirectOutlineDraft)
            mode='direct'; overview_minimum=0
        pending=[b for b in batches if b.get('status','pending')=='pending']
        failures=[b for b in batches if b.get('status')=='failed']
        minimum=overview_minimum+len(pending)
        return dict(document_id=doc['id'],arm=arm,algorithm=algorithm,context_tokens=capacity,
            source_id=source.source_id,source_sha256=doc['sha256'],mode=mode,sections=len(sections),
            complete_request=complete,initial_batches=len(batches),initial_pending=len(pending),
            initial_unprocessed=[{k:b[k] for k in ('line_start','line_end','error') if k in b} for b in failures],
            overview_logical_calls_minimum=overview_minimum,successful_logical_calls_lower_bound=minimum,
            actual_request_limit=actual_request_limit,lower_bound_over_limit=minimum>actual_request_limit,
            planned_dynamic_splits='unknown until actual output saturation/risk/truncation',
            planned_output_limit=6 if mode=='full' else 4 if algorithm=='v1' else 8,
            call_range_note='逻辑请求下界不含协议回退、实际概述大小及动态拆批；实际可能在上限内提前失败。',
            sections_plan=sections,batches_plan=batches,plan=plan)


def validate_artifacts(directory):
    manifest=json.loads((directory/'corpus.json').read_text(encoding='utf-8'))
    for doc in manifest['documents']:
        if digest(directory/doc['source_file'])!=doc['sha256']:
            raise ValueError('Frozen source hash changed: '+doc['id'])
        for raw in doc['raw_files']:
            if digest(directory/raw['path'])!=raw['sha256']:
                raise ValueError('Raw snapshot changed: '+raw['path'])
        source=load_source(directory,doc)
        if source.text!=(directory/doc['source_file']).read_text(encoding='utf-8'):
            raise ValueError('Parsed/canonical source text differs: '+doc['id'])
    return manifest


def offline_plan(directory):
    manifest=validate_artifacts(directory)
    docs={d['id']:d for d in manifest['documents']}
    # Pre-matrix diagnostics select by request scale, not model outputs or gold score.
    diagnostics=[]
    for doc in manifest['documents']:
        print('Planning full request:',doc['id'],flush=True)
        diagnostics.append(plan_one(directory,doc,'D'))
    ranked=sorted(diagnostics,key=lambda d:d['complete_request']['estimated_input_tokens'])
    small=ranked[0]['document_id']
    if small!='D01' and next(d for d in diagnostics if d['document_id']=='D01')['complete_request']['estimated_input_tokens']<26624:
        small='D01'  # Explicit API-rule document is the smoke document if it is small.
    large=ranked[-1]['document_id']
    middle_candidates=[d for d in ranked if d['document_id'] not in (small,large)]
    middle=max(middle_candidates,key=lambda d:min(d['complete_request']['estimated_input_tokens'],124928))['document_id']
    full=[small,middle,large]
    tasks=[]
    for index,doc in enumerate(manifest['documents']):
        arms=list(ARMS) if doc['id'] in full else ['B','D']
        # Rotate order deterministically; actual execution remains serial.
        offset=index%len(arms);arms=arms[offset:]+arms[:offset]
        for arm in arms:
            print('Planning matrix:',doc['id'],arm,flush=True)
            tasks.append(deepcopy(next(d for d in diagnostics if d['document_id']==doc['id']))
                         if arm=='D' else plan_one(directory,doc,arm))
    if len(tasks)!=18:
        raise AssertionError('Matrix must contain exactly 18 tasks')
    from learning_store import LearningStore
    from library_store import LibraryStore
    from outline_store import OutlineStore
    from outline_service import scope_fingerprint
    database=directory/'offline-plans.sqlite3'
    if database.exists():
        raise ValueError('Offline planning artifacts already exist; use a new preparation directory')
    practice=LearningStore(database);library=LibraryStore(practice);store=OutlineStore(practice)
    for task in tasks:
        doc=docs[task['document_id']]; source=load_source(directory,doc)
        collection=library.create(task['document_id']+'-'+task['arm'])['id']
        snapshots=[snapshot(doc,source)]
        task['offline_job_id']=store.create(collection,'offline-plan',OBJECTIVE,'zh',scope_fingerprint(snapshots),
            snapshots,task['sections_plan'],task['batches_plan'],task['plan'])
        store.pause(task['offline_job_id'])
    core={p:digest(Path(__file__).parent/p) for p in CODE_FILES}
    result=dict(version=1,stage='offline_only',live_model_requests=0,gold_review_required=True,
        code_revision='4f06786',code_sha256=core,full_matrix_documents=full,
        matrix_selection_reason='依据V2完整请求估计选择小/中/大，不读取候选或模型评分；小API文档优先冒烟。',
        request_scale_diagnostics=diagnostics,tasks=tasks)
    write_json(directory/'planning.json',result)
    lines=['# 18任务离线规划（未调用模型）','',
        f'完整矩阵：{", ".join(full)}；其余仅B/D。额度是每任务16次实际请求、生成总128次，逻辑下界超过16时不能承诺完成。','',
        '| 资料 | 组 | 路径 | 全文完整请求估计 | 初始提取批次 | 概述调用下界 | 成功逻辑调用下界 | 16次风险 |',
        '| --- | --- | --- | ---: | ---: | ---: | ---: | --- |']
    for t in tasks:
        lines.append(f"| {t['document_id']} | {t['arm']} | {t['mode']} | {t['complete_request']['estimated_input_tokens']} | {t['initial_pending']} | {t['overview_logical_calls_minimum']} | {t['successful_logical_calls_lower_bound']} | {'必超下界' if t['lower_bound_over_limit'] else '仍可能回退/拆批超限'} |")
    lines += ['', '所有估计使用项目现有完整协议、输出预留4096及安全余量2048；不是实际token。',
        '动态拆分数量、输出饱和、真实usage及实际费用在离线阶段均未知，不预造数值。',
        'SQLite仅为独立离线计划，作业均已暂停，不能被正式服务自动运行。']
    (directory/'PLANNING.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print(json.dumps(dict(tasks=len(tasks),full_matrix=full,
        scales=[dict(id=t['document_id'],estimate=t['complete_request']['estimated_input_tokens']) for t in diagnostics],
        over_limit=sum(t['lower_bound_over_limit'] for t in tasks)),ensure_ascii=False))


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('command',choices=['plan','validate'])
    parser.add_argument('--directory',type=Path,default=DEFAULT_RUN)
    args=parser.parse_args(); directory=args.directory.resolve()
    if args.command=='plan':offline_plan(directory)
    else:
        manifest=validate_artifacts(directory)
        print('Source hashes and canonical parse snapshots valid:',len(manifest['documents']))


if __name__=='__main__':main()
