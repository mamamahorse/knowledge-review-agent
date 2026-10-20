"""Scoped BM25 and rank fusion shared by production search and evaluation.

Text normalization is used only for matching. SearchHit always retains the
original prepared Chunk, so lexical features cannot become citation evidence.
"""

from collections import Counter, defaultdict
from dataclasses import dataclass, asdict
from math import isfinite, log1p
import re
import unicodedata

from retrieval import SearchHit

TOKENIZER_VERSION = 'nfkc-casefold-code-parts-han-uni-bigram-v1'
_TERMS = re.compile(r'[a-z0-9]+(?:(?:[_./:+-])[a-z0-9]+|::[a-z0-9]+)*(?:\+\+|#)?|[\u3400-\u4dbf\u4e00-\u9fff\U00020000-\U0002ebef]+')
_PARTS = re.compile(r'[a-z0-9]+')


def lexical_tokens(text):
    """Dictionary-free Han features and both whole/code component identifiers.

    This is a documented lexical heuristic, not linguistic Chinese segmentation.
    No stopword/stemming rules or relevance labels are involved.
    """
    normalized = unicodedata.normalize('NFKC', text).casefold()
    tokens = []
    for match in _TERMS.finditer(normalized):
        term = match.group()
        if term[0].isascii():
            tokens.append(term)
            parts = _PARTS.findall(term)
            if len(parts) > 1 or parts[0] != term:
                tokens.extend(parts)
        else:
            tokens.extend(term)
            tokens.extend(term[i:i+2] for i in range(len(term)-1))
    return tokens


@dataclass(frozen=True)
class HybridConfig:
    k1: float = 1.2
    b: float = .75
    rrf_k: float = 60.0
    branch_m: int = 50
    candidate_m: int = 50

    def __post_init__(self):
        if any(type(v) not in (int, float) or not isfinite(v) for v in (self.k1, self.b, self.rrf_k)):
            raise ValueError('BM25/RRF 参数必须是有限数值。')
        if self.k1 < 0 or not 0 <= self.b <= 1 or self.rrf_k < 0:
            raise ValueError('k1、RRF 常数须非负，b 须在0到1之间。')
        if any(type(v) is not int or v <= 0 for v in (self.branch_m, self.candidate_m)):
            raise ValueError('候选数量须是正整数。')
        if self.candidate_m > self.branch_m:
            raise ValueError('融合候选上限不能大于每路的候选上限。')

    def protocol(self):
        return {**asdict(self), 'tokenizer': TOKENIZER_VERSION,
                'idf': 'log(1+(N-df+0.5)/(df+0.5))',
                'tf': 'tf*(k1+1)/(tf+k1*(1-b+b*length/avg_length))',
                'query_tf': 'unique terms; no query-frequency weighting',
                'index_scope': 'statistics and postings built only from permitted prepared chunks',
                'text_field': 'original chunk text; structured table windows include original headers; no filename boost',
                'fusion': 'equal-weight sum(1/(rrf_k+one_based_rank)); absent branch contributes zero',
                'tie_break': 'original manifest chunk order',
                'zero_overlap': 'BM25 returns no zero-score filler',
                'tuning': 'caller-selected values; runner does not tune using labels'}


class BM25Index:
    """In-memory postings; rebuild per immutable permission scope for this run."""
    def __init__(self, chunks, config=HybridConfig()):
        self.chunks = tuple(chunks)
        self.config = config
        if len({c.chunk_id for c in self.chunks}) != len(self.chunks):
            raise ValueError('BM25 片段身份不能重复。')
        self.postings = defaultdict(list)
        self.lengths = []
        for i, chunk in enumerate(self.chunks):
            terms = lexical_tokens((chunk.retrieval_context + '\n' if chunk.retrieval_context else '') + chunk.text)
            self.lengths.append(len(terms))
            for term, count in Counter(terms).items():
                self.postings[term].append((i, count))
        self.average_length = sum(self.lengths) / len(self.lengths) if self.lengths else 0.0

    def search(self, query, top_k=50):
        if not isinstance(query, str) or not query.strip():
            raise ValueError('BM25 查询不能为空。')
        if type(top_k) is not int or top_k <= 0:
            raise ValueError('BM25 返回数量须为正整数。')
        scores = defaultdict(float)
        if not self.average_length:
            return []
        n, k1, b = len(self.chunks), self.config.k1, self.config.b
        # Preserve query feature order to make floating-point summation repeatable.
        for term in dict.fromkeys(lexical_tokens(query)):
            posting = self.postings.get(term, ())
            if not posting:
                continue
            idf = log1p((n - len(posting) + .5) / (len(posting) + .5))
            for i, frequency in posting:
                norm = k1 * (1 - b + b * self.lengths[i] / self.average_length)
                scores[i] += idf * frequency * (k1 + 1) / (frequency + norm)
        order = sorted(scores, key=lambda i: (-scores[i], i))[:top_k]
        return [SearchHit(self.chunks[i], scores[i]) for i in order]


def reciprocal_rank_fusion(chunks, dense_hits, lexical_hits, config=HybridConfig()):
    """Fuse ranks, never incomparable raw cosine and BM25 scores.

    The supplied chunk sequence is the entire allowed scope, including a stable
    tie order. Reject alien/conflicting evidence instead of widening permissions.
    """
    by_id = {c.chunk_id: c for c in chunks}
    if len(by_id) != len(chunks):
        raise ValueError('融合范围不能包含重复片段。')
    scope_order = {c.chunk_id: i for i, c in enumerate(chunks)}
    contributions = {}
    for name, hits in (('dense', dense_hits), ('bm25', lexical_hits)):
        if len(hits) > config.branch_m:
            raise ValueError('每路候选超过已记录的上限。')
        seen = set()
        for rank, hit in enumerate(hits, 1):
            identity = hit.chunk.chunk_id
            if identity in seen or identity not in by_id or hit.chunk != by_id[identity] or not isfinite(hit.score):
                raise ValueError('融合候选重复、越权、原文不一致或得分非法。')
            seen.add(identity)
            value = 1 / (config.rrf_k + rank)
            contributions.setdefault(identity, {})[name] = {'rank': rank, 'raw_score': hit.score, 'rrf_contribution': value}
    score = {identity: sum(c['rrf_contribution'] for c in branches.values())
             for identity, branches in contributions.items()}
    ordered = sorted(score, key=lambda identity: (-score[identity], scope_order[identity]))[:config.candidate_m]
    return [SearchHit(by_id[identity], score[identity]) for identity in ordered], contributions
