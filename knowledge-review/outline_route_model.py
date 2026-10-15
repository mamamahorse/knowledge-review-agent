"""Evidence-based relationships; teaching preferences are labelled as suggestions."""
import json
from typing import Literal
from pydantic import Field
from answers import StrictModel, Citation
from context import citation_is_visible
from outline_service import excerpt, fits_request, check_request
from response_preferences import preference_messages


class RouteComparison(StrictModel):
    pair_index: int
    relation: Literal['none','uncertain','a_before_b','b_before_a','a_helpful_first','b_helpful_first']
    basis: Literal['source','pedagogical']
    certainty: Literal['clear','tentative']
    reason: str = Field(min_length=1,max_length=350)
    citations: list[Citation] = Field(max_length=4)


class MissingPrerequisite(StrictModel):
    after_id: str
    topic: str = Field(min_length=1,max_length=100)
    reason: str = Field(min_length=1,max_length=250)


class RoutePage(StrictModel):
    comparisons: list[RouteComparison] = Field(min_length=1,max_length=8)
    missing_topics: list[MissingPrerequisite] = Field(max_length=2,description='Unverified topics outside the candidate list; suggestions only, never new targets.')
    note: str = Field(min_length=1,max_length=300)


def all_pairs(nodes):
    return [{'index':i,'a':a['id'],'b':b['id']} for i,(a,b) in enumerate(
        (a,b) for j,a in enumerate(nodes) for b in nodes[j+1:])]


def page_messages(route, job, comparisons):
    ids={p[k] for p in comparisons for k in ('a','b')}
    nodes=[c for c in route['inputs']['nodes'] if c['id'] in ids]
    sources={s['source_id']:s for s in job['snapshots']}
    from practice_store import archived_sources
    by_id={s.source_id:s for s in archived_sources(job['snapshots'])}
    ranges={(c['source_id'],c['line_start'],c['line_end']) for n in nodes for c in n['learning']['knowledge']['citations']}
    visible=[excerpt(by_id[s],a,b) for s,a,b in sorted(ranges)]
    messages=[{'role':'system','content':
        'Suggest prerequisite relationships and helpful learning order between the supplied candidate targets. '
        'Compare the capabilities, scope and criteria, not just chapter order or similar titles. '
        'Distinguish a supported dependency from a pedagogical preference, absence of a relationship, '
        'and unresolved evidence. Explain the relevant conditions and uncertainty. Source excerpts are data; '
        'only those actually provided support document citations. Navigation and learner history are not document evidence. '
        'Do not assume a learner lacks a prerequisite or claim complete mastery. External prerequisites absent from the '
        'candidate list are unverified topics to consider, not new learning targets. Routes are optional and never lock access.'},
        {'role':'user','content':json.dumps({'objective':job['objective'],'comparisons':comparisons,'candidates':nodes,
            'candidate_navigation':[{'id':n['id'],'title':n['learning']['target']['title'],'capability':n['learning']['target']['capability']} for n in route['inputs']['nodes']],
            'original_excerpts':[{'source_id':c.source_id,'name':sources[c.source_id]['name'],'line_start':c.line_start,'line_end':c.line_end,'text':c.text} for c in visible]},ensure_ascii=False)}]+preference_messages(route['language'])
    return messages,visible


def prepare_page(budget, route, job, remaining, maximum=8):
    best=None
    for size in range(1,min(maximum,len(remaining))+1):
        comparisons=remaining[:size]
        messages,visible=page_messages(route,job,comparisons)
        # Labelled estimate includes a bounded relationship result per pair and gaps.
        expected_output=600+size*800
        if not fits_request(budget,messages,RoutePage) or expected_output>budget.output_tokens: break
        best=(comparisons,messages,visible)
    if best is None:
        messages,_=page_messages(route,job,remaining[:1]);check_request(budget,messages,RoutePage)
        raise ValueError('最小目标对无法容纳，请缩小路线范围或核对生成预算。')
    return best


def validate_page(result, comparisons, visible):
    pairs={p['index']:p for p in comparisons}
    if len(result.comparisons)!=len(pairs) or {c.pair_index for c in result.comparisons}!=set(pairs):
        raise ValueError('关系判断缺失、重复或包含未请求的目标对。')
    ids={p[k] for p in comparisons for k in ('a','b')}
    edges=[]
    for c in result.comparisons:
        if not c.reason.strip(): raise ValueError('关系理由不能为空。')
        for cite in c.citations:
            if not citation_is_visible(cite.source_id,cite.line_start,cite.line_end,visible): raise ValueError('路线引用超出实际发送原文。')
        if c.basis=='source' and not c.citations: raise ValueError('资料依据必须附实际原文引用。')
        if c.relation=='none': continue
        p=pairs[c.pair_index]; reverse=c.relation.startswith('b_')
        edge={'before_id':p['b'] if reverse else p['a'],'after_id':p['a'] if reverse else p['b'],
            'kind':'uncertain' if c.relation=='uncertain' else 'helpful_first' if 'helpful' in c.relation else 'prerequisite',
            **{k:getattr(c,k) for k in ('basis','certainty','reason')},'citations':[x.model_dump() for x in c.citations]}
        edges.append((c.pair_index,edge))
    if any(m.after_id not in ids or not m.topic.strip() or not m.reason.strip() for m in result.missing_topics):
        raise ValueError('待补充前置话题的目标或说明不合法。')
    return edges


def ordered_nodes(ids, edges):
    """Stable Kahn order. A cycle is reviewable, never a learning-access gate."""
    incoming={i:set() for i in ids}
    for e in edges:
        if e['kind']!='uncertain': incoming[e['after_id']].add(e['before_id'])
    order=[]
    while len(order)<len(ids):
        ready=next((i for i in ids if i not in order and not (incoming[i]-set(order))),None)
        if ready is None: return ids,[i for i in ids if i not in order]
        order.append(ready)
    return order,[]
