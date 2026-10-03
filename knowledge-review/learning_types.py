"""Stable learning content; schema carries structure, not grading policy."""

from typing import Literal
from pydantic import Field
from answers import Citation, StrictModel

Capability = Literal["recall", "explain", "apply", "distinguish"]
ReviewRating = Literal["Again", "Hard", "Good", "Easy"]
Assessment = Literal["meets_standard", "developing", "needs_review", "uncertain"]


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
    criteria: list[str] = Field(min_length=1, max_length=4,
        description="Reusable observable criteria for this target, independent of a particular scenario's names or numbers.")


class LearningDraft(StrictModel):
    knowledge: KnowledgeDraft
    target: TargetDraft


class DimensionObservation(StrictModel):
    capability: Capability
    observation: str = Field(min_length=1, max_length=2000)
    answer_quotes: list[str] = Field(min_length=1)
