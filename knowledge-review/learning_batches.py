"""Sequential source batches: one user action processes at most one batch.

No cross-batch semantic consolidation. Child jobs reuse the existing learning
workflow; the parent owns source identity, the shared call allowance and cursor.
"""

from copy import deepcopy
from dataclasses import replace
import json
from uuid import uuid4

from generation_budget import GenerationBudget, BudgetError
from source_reader import StoredSource, freeze, line_count, read_lines


def batch_units(source, start, end):
    from learning_planner import navigation
    result=[]
    for unit in navigation(source,(start,end)):
        left,right=max(start,unit['line_start']),min(end,unit['line_end'])
        if left>right:
            continue
        result.append({**unit,'unit_id':unit['unit_id'] if (left,right)==(unit['line_start'],unit['line_end'])
                       else f'{unit["unit_id"]}:{left}:{right}', 'line_start':left,'line_end':right})
    return result


def plan_batches(source, objective, language, budget, *, model):
    """Pack adjacent structural units using complete request+schema estimates.

Reserve additional input space for subsequent generated catalogs, not a semantic
goal count. Actual requests are always checked again by request_structured.
"""
    from learning_pipeline import FormalStep, formal_messages
    from source_excerpt import excerpt
    from outline_evidence import extraction_evidence
    from structured_output import structured_protocols
    from contextlib import closing
    from chunking import protect_selection
    import sqlite3
    headroom=min(2*budget.output_tokens,budget.input_limit//3)
    working=replace(budget,margin_tokens=budget.margin_tokens+headroom)
    def fits(start,end):
        if isinstance(source,StoredSource):
            with closing(sqlite3.connect(source.archive)) as db:
                size=db.execute('SELECT SUM(length(CAST(text AS BLOB))+1) FROM lines WHERE number BETWEEN ? AND ?',
                                (start,end)).fetchone()[0] or 0
            if size>working.input_limit:
                return False
        text=read_lines(source,start,end)
        if len(text.encode())>working.input_limit:
            return False
        registry=extraction_evidence(source,protect_selection(excerpt(source,start,end),source))
        state=dict(inputs=dict(objective=objective,language=language,
                   snapshot=dict(source_id=source.source_id,path=str(source.path))),stage='refine',
            units=batch_units(source,start,end),evidence={key:dict(kind='source_text',citation=c,
                text=read_lines(source,c['line_start'],c['line_end'])) for key,c in registry.items()},
            items=[],dispositions={},events=[],formal=dict(goals={},abilities={},reviews={}))
        request=formal_messages(state)
        return all(working.fits(request,model=model,max_tokens=budget.output_tokens,**protocol)
                   for protocol in structured_protocols(FormalStep))
    total=line_count(source)
    if fits(1,total):
        return []
    # Prefer existing heading/block boundaries. Overlapping page anchors do not
    # become separate semantic batches; coverage remains the original line union.
    boundaries=sorted({0,total,*[min(total,b.line_end) for b in source.document.blocks]}) if source.document else [0,total]
    preferred=sorted({b.line_start-1 for b in source.document.blocks if b.kind=='heading' and b.line_start>1} |
                     {b.line_end for b in source.document.blocks if b.kind=='page'}) if source.document else []
    batches=[]; start=1
    while start<=total:
        candidates=[end for end in boundaries if end>=start]
        low,high,best=0,len(candidates)-1,None
        while low<=high:
            mid=(low+high)//2
            if fits(start,candidates[mid]):
                best=candidates[mid]; low=mid+1
            else:
                high=mid-1
        if best is None:
            # An oversized paragraph/table can cross batches; exact original
            # headers are supplied separately by the existing reading tools.
            low,high=start,candidates[0]
            while low<=high:
                mid=(low+high)//2
                if fits(start,mid): best=mid; low=mid+1
                else: high=mid-1
        if best is None:
            raise BudgetError(f'原文 L{start} 的单行或必要结构超过预算，未静默截断。')
        # Prefer ending before the next section (or after a complete PDF page),
        # provided this does not create an empty/header-only batch.
        candidates=[end for end in preferred if start<end<=best]
        if candidates and best<total and fits(start,candidates[-1]):
            best=candidates[-1]
        batches.append(dict(index=len(batches)+1,line_start=start,line_end=best,job_id=None))
        start=best+1
    return batches


def create_batches(store, source, objective, language, budget, *, model):
    specs=plan_batches(source,objective,language,budget,model=model)
    if not specs:
        return source,[]
    return freeze(source,store.practice.path.parent/'learning-sources'),specs


def run_batched(store, identity, current_source, *, additional_calls=0, next_batch=None,
                question_goal_id=None, question_batch=None, formalize=False):
    from learning_planner import run_planning, PlanningStopped, PlanningLimit
    from practice_store import archived_sources
    from agent_cancel import checkpoint, GenerationCancelled
    from learning_planner import model_settings
    with store.lock(identity):
        state=store.get(identity); previous=deepcopy(state)
        source=archived_sources([state['inputs']['snapshot']])[0]
        if current_source().source_id!=source.source_id:
            raise ValueError('资料版本已变化。')
        model,options=model_settings()
        if dict(name=model,endpoint=options.get('base_url','https://api.openai.com/v1'))!=state['inputs']['model']:
            raise ValueError('模型连接与任务创建时不同，请新建任务。')
        if state['inputs'].get('vision_model'):
            from pdf_vision import vision_settings
            name,options=vision_settings(required=False)
            if dict(name=name,endpoint=options.get('base_url','https://api.openai.com/v1'))!=state['inputs']['vision_model']:
                raise ValueError('视觉模型连接已变化，请新建任务。')
        if type(additional_calls) is not int or additional_calls<0:
            raise ValueError('新增额度无效。')
        cursor=state['batch_cursor']
        is_question=question_goal_id is not None or (state.get('working_kind')=='question' and
                                                     state['status'] not in ('waiting_next','completed'))
        if question_goal_id is not None:
            selected=question_batch or cursor+1
            if not 1<=selected<=cursor+1:
                raise ValueError('不能对尚未处理的批次出题。')
            index=selected-1
        else:
            index=state['working_batch']-1 if is_question else cursor
            if next_batch is not None:
                if type(next_batch) is not int or not 1<=next_batch<=len(state['batches']):
                    raise ValueError('批次编号无效。')
                if next_batch<=cursor+1:
                    return state  # A repeated click cannot advance a second time.
                if next_batch!=cursor+2 or state['status']!='waiting_next':
                    raise ValueError('请先完成当前批次，再继续紧邻的下一批。')
                index=cursor+1
            elif state['status'] in ('waiting_next','completed') and not formalize:
                return state
        state['call_limit']+=additional_calls
        attempt=uuid4().hex
        state.update(status='running',attempt=attempt,error=None,working_batch=index+1,
                     working_kind='question' if is_question else 'generation')
        if not is_question:
            state['batch_cursor']=index
        spec=state['batches'][index]
        if not spec['job_id']:
            spec['job_id']=store.create(source,state['inputs']['objective'],f'{identity}-batch-{index+1}',
                language=state['inputs']['language'],max_requests=state['call_limit'],
                budget=GenerationBudget(**state['inputs']['budget']),document_id=state['inputs'].get('document_id'),
                document_revision=state['inputs'].get('document_revision'),formalize=state['inputs']['formalize'],
                primary_range=(spec['line_start'],spec['line_end']),parent_id=identity,batch_index=index+1,
                visual_coverage=state['inputs'].get('visual_coverage',False))
            if state['inputs'].get('visual_coverage'):
                from visual_discovery import initialize,register
                child=store.get(spec['job_id']); initialize(child)
                for prior in state['batches'][:index]:
                    if not prior['job_id']: continue
                    old=store.get(prior['job_id']).get('visual_coverage',{})
                    for key,result in old.get('targets',{}).items():
                        if key in child['visual_coverage']['targets'] and result['status']=='reviewed':
                            register(child,{**result,'cache_source_job':prior['job_id']})
                store.save(child)
        store.save(state,expected=previous)
        child=store.get(spec['job_id'])
        child['parent_attempt']=attempt
        child['call_limit']=max(child['call_limit'],child['calls_reserved']+state['call_limit']-state['calls_reserved'])
        store.save(child)
        def guard():
            checkpoint()
            latest=store.get(identity)
            if latest.get('attempt')!=attempt:
                raise PlanningStopped('分批任务已暂停。')
            if current_source().source_id!=source.source_id:
                raise ValueError('资料版本已变化。')
            return source
        def reserve():
            guard()
            if state['calls_reserved']>=state['call_limit']:
                raise PlanningLimit('任务总请求额度耗尽；已有批次保留，继续前需明确增加额度。')
            state['calls_reserved']+=1
            store.save(state,attempt=attempt)
        try:
            child=run_planning(store,spec['job_id'],guard,formalize=formalize,
                question_goal_id=question_goal_id,_parent_reserve=reserve)
            guard()
            if not is_question:
                state['batch_cursor']=index
            if child['status']=='completed':
                state['status']='completed' if state['batch_cursor']==len(state['batches'])-1 else 'waiting_next'
            else:
                state.update(status=child['status'],error=child['error'])
            if child['status']=='completed':
                state.pop('working_batch',None)
                state.pop('working_kind',None)
            store.save(state,attempt=attempt)
            return state
        except PlanningStopped:
            return store.get(identity)
        except Exception as exc:
            latest=store.get(identity)
            latest.update(status='paused' if isinstance(exc,(GenerationCancelled,PlanningLimit,BudgetError)) else 'failed',
                          error=type(exc).__name__)
            try: store.save(latest,attempt=attempt)
            except PlanningStopped: pass
            raise


def batch_summary(store, job):
    result=[]
    for spec in job['batches']:
        child=store.get(spec['job_id']) if spec['job_id'] else None
        result.append({**spec,'status':child['status'] if child else 'pending',
            'stage':child.get('stage') if child else None,
            'generation_complete':bool(child and (child.get('stage') in ('ready','question') or
                                                  child['status']=='completed')),
            'goal_count':len(child.get('formal',{}).get('goals',{})) if child else 0})
    return result
