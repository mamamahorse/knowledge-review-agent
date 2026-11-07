"""Source-grounded learning product contracts, independent of generation algorithms."""
from typing import Annotated
from pydantic import Field
from answers import StrictModel
from learning_types import TargetDraft, CRITERIA_GUIDANCE


class TargetProposal(TargetDraft):
    description: str = Field(min_length=1, max_length=400)
    scope: str = Field(min_length=1, max_length=300)
    criteria: list[Annotated[str, Field(min_length=1, max_length=240)]] = Field(min_length=1,
        description=CRITERIA_GUIDANCE)


class EvidenceKnowledgeProposal(StrictModel):
    name: str = Field(min_length=1, max_length=160)
    statement: str = Field(min_length=1, max_length=600)
    content_tags: list[str] = Field(max_length=8)
    evidence_ids: list[Annotated[str, Field(min_length=1)]] = Field(min_length=1,
        description='Select IDs only from this request available_evidence. Keep all necessary references; never generate source coordinates.')


class EvidenceLearningProposal(StrictModel):
    knowledge: EvidenceKnowledgeProposal
    target: TargetProposal

