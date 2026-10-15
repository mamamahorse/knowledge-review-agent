"""Versioned synthetic evaluation inputs; reference labels never enter live prompts."""

import json
from hashlib import sha256
from pathlib import Path
from typing import Literal

from pydantic import Field
from answers import StrictModel
from learning_types import LearningDraft, Assessment
from practice_model import QuestionDraft
from sources import Source

CORPUS = Path(__file__).resolve().parent / 'evals' / 'suite.json'


class Evidence(StrictModel):
    file: str
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)


class ReferencePoint(StrictModel):
    id: str
    statement: str = Field(min_length=1)
    evidence: list[Evidence] = Field(min_length=1)


class Relation(StrictModel):
    pair: list[str] = Field(min_length=2, max_length=2)
    expected: list[Literal['same_knowledge', 'duplicate_targets', 'needs_review']]
    forbidden: list[Literal['same_knowledge', 'duplicate_targets', 'needs_review']]
    reason: str = Field(min_length=1)


class GradeExample(StrictModel):
    id: str
    question: QuestionDraft
    answer: str = Field(min_length=1)
    expected_assessments: list[Assessment] = Field(min_length=1)
    expected_target_fits: list[Literal['within_scope', 'outside_scope', 'uncertain']] = Field(min_length=1)
    expected_covered: list[int]
    rationale: str = Field(min_length=1)


class EvaluationCase(StrictModel):
    id: str = Field(pattern=r'^[a-z0-9_-]+$')
    title: str
    objective: str
    files: list[str] = Field(min_length=1)
    reference_points: list[ReferencePoint] = Field(min_length=1)
    candidate_fixtures: dict[str, LearningDraft]
    relations: list[Relation]
    grades: list[GradeExample]
    review_checks: list[str] = Field(min_length=1)


class EvaluationSuite(StrictModel):
    version: Literal[1]
    revision: str
    origin: Literal['synthetic']
    reference_status: Literal['draft_reference_human_review_needed']
    file_sha256: dict[str, str]
    cases: list[EvaluationCase] = Field(min_length=1)


def file_for(root, name):
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()) or path.suffix.lower() != '.md':
        raise ValueError('评测资料必须是样例目录内的 Markdown 文件。')
    return path


def sources_for(case, root):
    sources, snapshots, aliases = [], [], {}
    for name in case.files:
        path = file_for(root, name)
        text = path.read_text(encoding='utf-8')
        sid = sha256((name + '\n' + text).encode()).hexdigest()
        source = Source(sid, path, text)
        aliases[name] = source
        sources.append(source)
        snapshots.append({'document_id': name, 'source_id': sid, 'path': str(path), 'name': path.name, 'text': text})
    return sources, snapshots, aliases


def remap_citations(value, aliases):
    """Corpus file aliases become the same concrete source versions used by services."""
    if isinstance(value, list):
        return [remap_citations(x, aliases) for x in value]
    if isinstance(value, dict):
        result = {k: remap_citations(v, aliases) for k, v in value.items()}
        if 'source_id' in result:
            result['source_id'] = aliases[result['source_id']].source_id
        return result
    return value


def validate_suite(suite, root):
    ids = [case.id for case in suite.cases]
    if len(ids) != len(set(ids)):
        raise ValueError('评测 case ID 重复。')
    used = set()
    for case in suite.cases:
        if len(set(case.files)) != len(case.files):
            raise ValueError('case 内文件不能重复。')
        _, _, aliases = sources_for(case, root)
        used.update(case.files)
        for names in ([p.id for p in case.reference_points], [g.id for g in case.grades]):
            if len(names) != len(set(names)):
                raise ValueError('case 内参考点或评分样例 ID 重复。')
        def evidence(file, start, end):
            if file not in aliases or not 1 <= start <= end <= len(aliases[file].text.splitlines()):
                raise ValueError(f'{case.id} 的原文指针不存在或越界。')
        for point in case.reference_points:
            for ref in point.evidence:
                evidence(ref.file, ref.line_start, ref.line_end)
        for draft in case.candidate_fixtures.values():
            for ref in draft.knowledge.citations:
                evidence(ref.source_id, ref.line_start, ref.line_end)
        pairs = set()
        for relation in case.relations:
            pair = tuple(sorted(relation.pair))
            if len(set(pair)) != 2 or not set(pair) <= case.candidate_fixtures.keys() or pair in pairs:
                raise ValueError('关系标注必须是不同的已知候选对，且不能重复。')
            if set(relation.expected).intersection(relation.forbidden):
                raise ValueError('关系标注的允许和禁止类型冲突。')
            pairs.add(pair)
        for example in case.grades:
            for ref in example.question.citations:
                evidence(ref.source_id, ref.line_start, ref.line_end)
            if example.question.learning:
                for ref in example.question.learning.knowledge.citations:
                    evidence(ref.source_id, ref.line_start, ref.line_end)
            if len(set(example.expected_covered)) != len(example.expected_covered) or any(
                not 1 <= i <= len(example.question.rubric_points) for i in example.expected_covered):
                raise ValueError('评分标注的 rubric 索引越界或重复。')
    if used != suite.file_sha256.keys():
        raise ValueError('文件版本清单与实际样例范围不一致。')
    for name, digest in suite.file_sha256.items():
        if sha256(file_for(root, name).read_bytes()).hexdigest() != digest:
            raise ValueError(f'{name} 内容已改变；先核对原文行号并更新参考标注与版本。')


def load_suite(path=CORPUS):
    suite = EvaluationSuite.model_validate_json(path.read_text(encoding='utf-8'))
    validate_suite(suite, path.parent)
    fingerprint = sha256(suite.model_dump_json().encode()).hexdigest()
    return suite, fingerprint
