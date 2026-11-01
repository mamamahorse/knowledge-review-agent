"""Request-local evidence selection; persisted citations keep exact source coordinates."""

from hashlib import sha256
import json

from answers import Citation
from context import validate_chunks, citation_is_visible

EVIDENCE_PROTOCOL = 'extraction-evidence-ids-v1'


class EvidenceReferenceError(ValueError):
    def __init__(self, detail):
        super().__init__(detail['message'])
        self.validation_errors = [detail]


def evidence_id(citation):
    canonical = Citation.model_validate(citation).model_dump(exclude_none=True)
    return 'ref_' + sha256(json.dumps(canonical, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def available_evidence(citations):
    registry = {}
    for citation in citations:
        cite = Citation.model_validate(citation)
        if cite.line_start < 1 or cite.line_end < cite.line_start:
            raise ValueError('背景概述引用范围不合法。')
        registry[evidence_id(cite)] = cite.model_dump(exclude_none=True)
    return dict(sorted(registry.items()))


def missing_ranges(start, end, ranges):
    """Find gaps using intervals, without expanding a large citation into every line."""
    cursor, missing = start, []
    for left, right in sorted(ranges):
        if right < cursor or left > end:
            continue
        if left > cursor:
            missing.append([cursor, min(left-1, end)])
        cursor = max(cursor, right+1)
        if cursor > end:
            break
    if cursor <= end:
        missing.append([cursor, end])
    return missing


def extraction_evidence(source, visible):
    """Intersect structural blocks with actual excerpts; never grant unprovided lines.

    A paragraph/list/code/table fragment is selectable. Supplementary fragments stay
    separate across gaps, and split windows clip the same block to their own rights.
    """
    validate_chunks(visible, [source])
    blocks = source.document.blocks if source.document else ()
    lines = source.text.splitlines()
    citations = []
    for chunk in visible:
        pieces = [(max(chunk.line_start,b.line_start), min(chunk.line_end,b.line_end))
                  for b in blocks if b.line_start <= chunk.line_end and b.line_end >= chunk.line_start]
        # Preserve nonblank parser gaps as real text too, rather than silently
        # dropping evidence outside a parser's structural blocks.
        for start, end in missing_ranges(chunk.line_start, chunk.line_end, pieces):
            run = None
            for number in range(start, end+1):
                if lines[number-1].strip():
                    if run is None: run = number
                elif run is not None:
                    pieces.append((run,number-1)); run = None
            if run is not None: pieces.append((run,end))
        citations.extend(Citation(source_id=source.source_id,line_start=a,line_end=b) for a,b in pieces)
    return available_evidence(citations)


def bind_evidence(messages, source, visible, primary=None):
    data = json.loads(messages[1]['content'])
    data['available_evidence'] = extraction_evidence(source, visible)
    if primary is not None:
        data['primary_evidence_ids'] = [ref for ref,c in data['available_evidence'].items()
            if citation_is_visible(c['source_id'],c['line_start'],c['line_end'],primary)]
    messages[1]['content'] = json.dumps(data, ensure_ascii=False)
    return messages


def evidence_diagnostic(draft, registry):
    return dict(evidence_protocol=EVIDENCE_PROTOCOL, available_evidence=registry,
                selected_evidence_ids=[p.knowledge.evidence_ids for p in draft.proposals])


def citation_error(citation, visible, *, proposal_index=None, citation_index=0, reference_id=None):
    ranges = sorted(set((c.line_start,c.line_end) for c in visible if c.source_id==citation.source_id))
    gaps = missing_ranges(citation.line_start,citation.line_end,ranges)
    path = (['proposals',proposal_index] if proposal_index is not None else []) + ['knowledge',
        'evidence_ids' if reference_id else 'citations',citation_index]
    label = f'目标{proposal_index+1}，' if proposal_index is not None else ''
    gap_text = '、'.join(f'L{a}' if a==b else f'L{a}–{b}' for a,b in gaps)
    invalid = not 1 <= citation.line_start <= citation.line_end
    return EvidenceReferenceError(dict(path=path,type='citation_not_visible',
        message=f'{label}引用{citation_index+1}超出实际可见原文：L{citation.line_start}–{citation.line_end}；'
                + ('起止行不合法（应为1起始、包含两端的正序范围）。' if invalid else f'不可见缺口：{gap_text}。')
                + '请仅选择本次 available_evidence 中的证据ID，不得合并或扩大坐标。',
        source_id=citation.source_id,requested_range=[citation.line_start,citation.line_end],
        available_ranges=[list(r) for r in ranges],missing_ranges=gaps,evidence_id=reference_id))
