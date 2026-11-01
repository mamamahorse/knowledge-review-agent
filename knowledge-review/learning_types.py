"""Stable learning content; schema carries structure, not grading policy."""

from typing import Literal
from pydantic import Field
from answers import Citation, StrictModel

Capability = Literal["recall", "explain", "apply", "distinguish"]
ReviewRating = Literal["Again", "Hard", "Good", "Easy"]
Assessment = Literal["meets_standard", "developing", "needs_review", "uncertain"]

CRITERIA_SOFT_LIMIT = 4
CRITERIA_GUIDANCE = (
    'Reusable observable criteria, independent of a particular scenario. Four criteria is a recommendation, '
    'not a limit. When there are more, merge only equivalent standards or separate independently assessable '
    'capabilities into different targets. Never delete necessary conditions to meet a count. '
    'Keep more than four when all are necessary to assess one coherent capability.'
)


def criteria_organization_note(target):
    count = len(target.criteria)
    if count <= CRITERIA_SOFT_LIMIT:
        return ''
    return (f'当前目标包含{count}条考核标准，超过建议的{CRITERIA_SOFT_LIMIT}条。'
            '请判断能否合并等价标准，或拆成独立学习目标；不得删除必要条件。'
            '若共同考察同一种能力，可以保留全部标准。')


class KnowledgeDraft(StrictModel):
    name: str = Field(min_length=1, max_length=160)
    statement: str = Field(min_length=1, max_length=2000)
    content_tags: list[str] = Field(max_length=8)
    citations: list[Citation] = Field(min_length=1)


class TargetDraft(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    capability: Capability
    description: str = Field(min_length=1, max_length=2000)
    scope: str = Field(min_length=1, max_length=2000)
    criteria: list[str] = Field(min_length=1, description=CRITERIA_GUIDANCE)


class LearningDraft(StrictModel):
    knowledge: KnowledgeDraft
    target: TargetDraft


class DimensionObservation(StrictModel):
    capability: Capability
    observation: str = Field(min_length=1, max_length=2000)
    answer_quotes: list[str] = Field(min_length=1)
