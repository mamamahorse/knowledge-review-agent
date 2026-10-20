"""Generate a bounded, recoverable route; human decisions never enroll or gate."""
from agent_work import job_boundary
import json
from contextlib import closing
from uuid import uuid4
from generation_budget import GenerationBudget
from learning_types import LearningDraft
from outline_context import generation_error, request_diagnostic
from outline_service import require_current, candidate_context, candidate_visible, validate_learning
from outline_store import OutlineBusy, timestamp
from outline_route_store import (route_record,route_pages,route_edges,decode,require_attempt,
    claim_page,publish_page,recover_routes)
from outline_route_model import RoutePage,all_pairs,prepare_page,validate_page,ordered_nodes
from practice_model import _request_structured
from response_preferences import validate_language


def selection_view(db, outline_id):
    candidates=[dict(r) for r in db.execute('SELECT * FROM outline_candidates WHERE outline_id=? ORDER BY rowid',(outline_id,))]
    groups=[dict(r) for r in db.execute('SELECT * FROM outline_groups WHERE outline_id=? ORDER BY rowid',(outline_id,))]
    excluded={}
    for g in groups:
        members=json.loads(g['members_json']); p=json.loads(g['proposal_json'])
        if g['status']=='accepted' and p['kind']=='duplicate_targets':
            excluded.update({i:'已确认重复，请使用代表目标。' for i in members if i!=p['canonical_id']})
        elif g['status']=='proposed' and p['kind'] in ('needs_review','duplicate_targets'):
            excluded.update({i:'此候选仍有待核对或待确认的重复建议，请先审核。' for i in members})
    return candidates,groups,excluded


def input_stamp(db, outline_id, ids):
    candidates,groups,excluded=selection_view(db,outline_id)
    nodes=[{'id':c['id'],'revision':c['revision'],'learning':json.loads(c['proposal_json'])} for c in candidates if c['id'] in ids]
    if len(nodes)!=len(ids) or any(i in excluded for i in ids): raise OutlineBusy('所选候选已变化、待核对或不再是重复代表，请重新审核路线范围。')
    tokens=[{'id':g['id'],'revision':g['revision'],'status':g['status'],'proposal':json.loads(g['proposal_json'])}
        for g in groups if g['status'] in ('accepted','rejected') and set(json.loads(g['members_json'])) & set(ids)]
    return {'nodes':nodes,'groups':tokens}


def check_stamp(db, route):
    ids=[n['id'] for n in route['inputs']['nodes']]
    if input_stamp(db,route['outline_id'],ids)!= {k:route['inputs'][k] for k in ('nodes','groups')}:
        raise OutlineBusy('候选或归组审核版本已变化，旧路线仅供查看，请重新生成。')


def current_route(store, route, current):
    job=store.get(route['outline_id']);require_current(job,current)
    with closing(store.practice._connect()) as db: check_stamp(db,route)
    return job


def start_route(store, outline_id, request_id, language, candidate_ids, current, call_limit=24):
    validate_language(language)
    if type(call_limit)!=int or not 1<=call_limit<=200: raise ValueError('请求额度必须是 1–200 的整数。')
    job=store.get(outline_id)
    with store.lock(outline_id):
        job=store.get(outline_id)
        with closing(store.practice._connect()) as db:
            old=db.execute('SELECT id,language FROM outline_routes WHERE outline_id=? AND request_id=?',(outline_id,request_id)).fetchone()
        if old:
            if old['language']!=language: raise OutlineBusy('此请求标识已用于另一种语言。')
            return old['id']
        require_current(job,current)
        if job['status']!='completed': raise OutlineBusy('请先完成资料分析，再审核候选并生成路线。')
        from outline_grouping import public_grouping
        public_grouping(store,outline_id);recover_routes(store,outline_id)
        with closing(store.practice._connect()) as db:
            candidates,_,excluded=selection_view(db,outline_id)
            ids=candidate_ids if candidate_ids is not None else [c['id'] for c in candidates if c['id'] not in excluded]
            if not ids or len(set(ids))!=len(ids): raise ValueError('请选择至少一个经核对且不重复的候选。')
            inputs=input_stamp(db,outline_id,ids)
        for n in inputs['nodes']:
            c,job,source,chunk=candidate_context(store,n['id'])
            validate_learning(LearningDraft.model_validate(n['learning']),candidate_visible(c,job,source,chunk))
        inputs['scope_fingerprint']=job['scope_fingerprint']
        identity,now=uuid4().hex,timestamp()
        route={'id':identity,'outline_id':outline_id,'inputs':inputs}
        with store.practice.transaction() as db:
            check_stamp(db,route)
            if db.execute("SELECT 1 FROM outline_routes WHERE outline_id=? AND status IN ('queued','running')",(outline_id,)).fetchone():
                raise OutlineBusy('这个清单已有正在生成的路线，请等待或暂停。')
            db.execute("INSERT INTO outline_routes (id,outline_id,request_id,language,inputs_json,status,allowance,created_at,updated_at) VALUES (?,?,?,?,?,'queued',?,?,?)",
                (identity,outline_id,request_id,language,json.dumps(inputs,ensure_ascii=False),call_limit,now,now))
        return identity


@job_boundary("route")
def run_route(store, identity, current):
    attempt=page=None; locked=False
    try:
        route=route_record(store,identity)
        with store.lock(identity):
            locked=True;route=route_record(store,identity)
            if route['status']!='queued': return
            job=current_route(store,route,current);attempt=uuid4().hex
            with store.practice.transaction() as db:
                if not db.execute("UPDATE outline_routes SET status='running',attempt=?,error=NULL,updated_at=? WHERE id=? AND status='queued'",(attempt,timestamp(),identity)).rowcount:return
            budget=GenerationBudget.from_env();pairs=all_pairs(route['inputs']['nodes']); maximum=8
            while True:
                route=route_record(store,identity);job=current_route(store,route,current)
                if route['cursor']==len(pairs):
                    with store.practice.transaction() as db:
                        require_attempt(db,route,attempt)
                        db.execute("UPDATE outline_routes SET status='completed',attempt=NULL,error=NULL,updated_at=? WHERE id=?",(timestamp(),identity))
                    return
                comparisons,messages,visible=prepare_page(budget,route,job,pairs[route['cursor']:],maximum)
                diag=request_diagnostic(budget,messages,RoutePage)
                diag['visible']=[{'source_id':c.source_id,'line_start':c.line_start,'line_end':c.line_end} for c in visible]
                diag['output_estimate']=600+len(comparisons)*800
                diag['output_estimate_method']='application heuristic; provider tokenizer unknown'
                page=claim_page(store,route,attempt,comparisons,diag)
                if page is None:return
                try: result=_request_structured(messages,RoutePage)
                except RuntimeError as exc:
                    from openai import LengthFinishReasonError
                    truncated='was truncated' in str(exc) or isinstance(exc.__cause__,LengthFinishReasonError)
                    if not truncated or len(comparisons)==1:raise
                    with store.practice.transaction() as db:
                        require_attempt(db,route,attempt)
                        db.execute("UPDATE outline_route_pages SET status='failed',attempt=NULL,error=? WHERE id=? AND attempt=?",('输出截断，缩小工作单元继续；未保存不完整关系。',page['id'],page['attempt']))
                    maximum=max(1,len(comparisons)//2);page=None;continue
                edges=validate_page(result,comparisons,visible)
                current_route(store,route,current)
                publish_page(store,route,attempt,page,result,edges);page=None
    except (OSError,RuntimeError,ValueError,KeyError) as exc:
        error=generation_error(exc,'路线生成')
        if attempt:
            with store.practice.transaction() as db:
                if page: db.execute("UPDATE outline_route_pages SET status='failed',attempt=NULL,error=? WHERE id=? AND attempt=?",(error,page['id'],page['attempt']))
                db.execute("UPDATE outline_routes SET status='failed',attempt=NULL,error=?,updated_at=? WHERE id=? AND attempt=?",(error,timestamp(),identity,attempt))
        elif locked:
            with store.practice.transaction() as db:
                db.execute("UPDATE outline_routes SET status='failed',error=? WHERE id=? AND status='queued' AND updated_at=?",(error,identity,route['updated_at']))


def continue_route(store, identity, current, call_limit=24):
    if type(call_limit)!=int or not 1<=call_limit<=200:raise ValueError('请求额度必须是 1–200 的整数。')
    route=route_record(store,identity)
    with store.lock(identity):
        route=route_record(store,identity);current_route(store,route,current)
        if route['status'] in ('completed','queued'):return
        with store.practice.transaction() as db:
            if db.execute("SELECT 1 FROM outline_routes WHERE outline_id=? AND id!=? AND status IN ('queued','running')",(route['outline_id'],identity)).fetchone():raise OutlineBusy('另一个路线任务正在生成。')
            db.execute("UPDATE outline_routes SET status='queued',attempt=NULL,error=NULL,allowance=calls_used+?,updated_at=? WHERE id=?",(call_limit,timestamp(),identity))
            db.execute("UPDATE outline_route_pages SET status='failed',attempt=NULL WHERE route_id=? AND status='running'",(identity,))


def displayed_order(route, edges):
    ids=[n['id'] for n in route['inputs']['nodes']]
    accepted=[e['decision'] for e in edges if e['status']=='accepted']
    preview=accepted or [e['decision'] for e in edges if e['status']=='proposed']
    order,cycle=ordered_nodes(ids,preview)
    return route['order'] or order,cycle,'user' if route['order'] else 'accepted' if accepted else 'preview'


def edit_edge(store, identity, revision, before_id, after_id, kind, reason, decision, current):
    with closing(store.practice._connect()) as db: row=db.execute('SELECT * FROM outline_route_edges WHERE id=?',(identity,)).fetchone()
    if not row:raise KeyError('前置关系不存在。')
    route=route_record(store,row['route_id']);current_route(store,route,current)
    if route['status']!='completed':raise OutlineBusy('请先完成路线生成。')
    if kind not in ('prerequisite','helpful_first','uncertain') or decision not in ('proposed','accepted','rejected') or not reason.strip() or len(reason)>350:raise ValueError('关系类型、审核决定或理由不合法。')
    ids={n['id'] for n in route['inputs']['nodes']}
    if before_id not in ids or after_id not in ids or before_id==after_id:raise ValueError('关系必须连接两个不同的路线目标。')
    if kind=='uncertain' and decision=='accepted':raise ValueError('不确定关系须先明确类型，或跳过。')
    with store.practice.transaction() as db:
        check_stamp(db,route); saved=db.execute('SELECT * FROM outline_route_edges WHERE id=?',(identity,)).fetchone()
        if saved['revision']!=revision:raise OutlineBusy('关系已被修改，请刷新。')
        original=json.loads(saved['decision_json'])
        update={**original,'before_id':before_id,'after_id':after_id,'kind':kind,'reason':reason}
        if any(update[k]!=original[k] for k in ('before_id','after_id','kind','reason')):
            update.update(basis='user',certainty='tentative',citations=[])
        accepted=[e['decision'] for e in route_edges(db,route['id']) if e['status']=='accepted' and e['id']!=identity]
        if decision=='accepted':
            if any(e['before_id']==before_id and e['after_id']==after_id for e in accepted):raise ValueError('已有相同方向的关系，请修改原关系。')
            if ordered_nodes(list(ids),accepted+[update])[1]:raise ValueError('接受后将形成循环，请修改方向或跳过其中一条；学习入口仍可自由使用。')
        db.execute('UPDATE outline_route_edges SET decision_json=?,status=?,revision=revision+1 WHERE id=?',(json.dumps(update,ensure_ascii=False),decision,identity))
        db.execute('UPDATE outline_routes SET revision=revision+1,updated_at=? WHERE id=?',(timestamp(),route['id']))
    return revision+1


def save_order(store, identity, revision, order, skipped, current):
    route=route_record(store,identity);current_route(store,route,current)
    ids={n['id'] for n in route['inputs']['nodes']}
    if route['status']!='completed':raise OutlineBusy('请先完成路线生成。')
    if order is not None and (len(order)!=len(ids) or set(order)!=ids):raise ValueError('顺序必须恰好包含所有路线目标。')
    if len(set(skipped))!=len(skipped) or not set(skipped)<=ids:raise ValueError('跳过列表包含未知或重复目标。')
    with store.practice.transaction() as db:
        check_stamp(db,route)
        if not db.execute('UPDATE outline_routes SET order_json=?,skipped_json=?,revision=revision+1,updated_at=? WHERE id=? AND revision=?',
            (json.dumps(order) if order is not None else None,json.dumps(skipped),timestamp(),identity,revision)).rowcount:raise OutlineBusy('路线顺序已被修改，请刷新。')
    return revision+1


def add_edge(store, identity, revision, before_id, after_id, kind, reason, current):
    """A manual suggestion has user attribution and never invented citations."""
    route=route_record(store,identity);current_route(store,route,current)
    ids={n['id'] for n in route['inputs']['nodes']}
    if route['status']!='completed':raise OutlineBusy('请先完成路线生成。')
    if before_id not in ids or after_id not in ids or before_id==after_id or kind not in ('prerequisite','helpful_first','uncertain') or not reason.strip() or len(reason)>350:
        raise ValueError('请选择两个不同目标、关系类型及非空理由。')
    edge={'before_id':before_id,'after_id':after_id,'kind':kind,'reason':reason,'basis':'user','certainty':'tentative','citations':[]}
    key=uuid4().hex
    with store.practice.transaction() as db:
        check_stamp(db,route)
        if not db.execute('UPDATE outline_routes SET revision=revision+1,updated_at=? WHERE id=? AND revision=?',(timestamp(),identity,revision)).rowcount:
            raise OutlineBusy('路线已变化，请刷新后添加。')
        if any(e['status']!='rejected' and e['decision']['before_id']==before_id and e['decision']['after_id']==after_id for e in route_edges(db,identity)):
            raise ValueError('已有相同方向的建议，请修改原关系。')
        db.execute('INSERT INTO outline_route_edges (id,route_id,decision_json) VALUES (?,?,?)',(key,identity,json.dumps(edge,ensure_ascii=False)))
    return key


def public_routes(store, outline_id, current):
    recover_routes(store,outline_id)
    job=store.get(outline_id)
    with closing(store.practice._connect()) as db:
        candidates,_,excluded=selection_view(db,outline_id)
        identities=[r[0] for r in db.execute('SELECT id FROM outline_routes WHERE outline_id=? ORDER BY rowid DESC',(outline_id,))]
    runs=[]
    for identity in identities:
        r=route_record(store,identity)
        try:current_route(store,r,current);stale=False
        except (OSError,ValueError,KeyError):stale=True
        with closing(store.practice._connect()) as db:edges=route_edges(db,identity)
        order,cycle,mode=displayed_order(r,edges)
        approved=[e['decision'] for e in edges if e['status']=='accepted']
        conflicts=[{'before_id':e['before_id'],'after_id':e['after_id']} for e in approved if order.index(e['before_id'])>order.index(e['after_id'])]
        snapshots={s['source_id']:s for s in job['snapshots']}
        def cite(c):
            s=snapshots[c['source_id']]
            return {**c,'document_id':s['document_id'],'name':s['name'],'excerpt':'\n'.join(s['text'].splitlines()[c['line_start']-1:c['line_end']])}
        for edge in edges:
            edge['citations']=[cite(c) for c in edge['decision']['citations']]
            edge['original_citations']=[cite(c) for c in (edge['model'] or {}).get('citations',[])]
        pages=route_pages(store,identity)
        from route_recommendation import recommend_next
        recommendation_order=r['order'] or ordered_nodes([n['id'] for n in r['inputs']['nodes']],approved)[0]
        recommendation=recommend_next(store,r,recommendation_order,approved) if r['status']=='completed' and not stale else None
        runs.append({k:r[k] for k in ('id','status','language','error','cursor','calls_used','allowance','revision','skipped','created_at')} |
            {'stale':stale,'nodes':r['inputs']['nodes'],'total_pairs':len(all_pairs(r['inputs']['nodes'])),
             'edges':edges,'order':order,'order_mode':mode,'cycle':cycle,'order_conflicts':conflicts,'pages':pages,'recommendation':recommendation})
    return {'eligible_ids':[c['id'] for c in candidates if c['id'] not in excluded],'excluded':excluded,'runs':runs}
