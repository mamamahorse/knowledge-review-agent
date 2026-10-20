"""Answer contracts and checks; source existence is not semantic correctness."""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sources import Source
from chunking import Chunk
from context import citation_is_visible, validate_chunks


class StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class Citation(StrictModel):
    source_id: str = Field(description="Identity of the supplied source version supporting this content.")
    line_start: int = Field(description="First original source line in the supporting visible excerpt, 1-based.")
    line_end: int = Field(description="Last original source line in the supporting visible excerpt, inclusive.")
    asset_id: str | None = Field(default=None, description='Only for a successfully viewed visual asset; otherwise null.')


class AnswerBlock(StrictModel):
    kind: Literal["grounded", "inference", "supplement"] = Field(description=(
        "grounded reports source content; inference explains a conclusion and its assumptions "
        "from source premises; supplement is unverified background knowledge."
    ))
    text: str = Field(description="Explanation for the learner; source references are represented separately.")
    citations: list[Citation] = Field(description="Source evidence for grounded/inference content; empty for background knowledge.")


class NoteAnswer(StrictModel):
    coverage: Literal["sufficient", "partial", "none"] = Field(description="How well the supplied evidence supports the requested answer.")
    coverage_note: str = Field(description="Explanation of partial or missing evidence, including unknown private facts.")
    blocks: list[AnswerBlock]


class SourceAnswerBlock(AnswerBlock):
    kind: Literal["grounded", "inference"] = Field(
        description="Source content or a conclusion drawn from visible source premises.",
    )
    citations: list[Citation] = Field(min_length=1, description="Visible source evidence supporting this content.")


class SourceOnlyAnswer(NoteAnswer):
    """The strict-mode API contract excludes background knowledge at generation."""
    blocks: list[SourceAnswerBlock]


def validate_answer(
    result: NoteAnswer, sources: list[Source], strict: bool = False,
    *, chunks: list[Chunk] | None = None,
    visual_evidence: list[dict] | None = None,
) -> None:
    if chunks is not None:
        validate_chunks(chunks, sources)
    by_id = {source.source_id: source for source in sources}
    if re.search(r"\[L\d+", result.coverage_note):
        raise RuntimeError("line references must be in citations, not coverage notes.")
    if result.coverage != "sufficient" and not result.coverage_note.strip():
        raise RuntimeError("partial or missing evidence must include an explanation.")
    if not result.blocks and result.coverage != "none":
        raise RuntimeError("an answer with evidence must contain answer blocks.")
    has_evidence = False
    for block in result.blocks:
        if not block.text.strip():
            raise RuntimeError("the model returned an empty answer block.")
        if re.search(r"\[L\d+", block.text):
            raise RuntimeError("line references must be in citations, not answer text.")
        if block.kind == "supplement":
            if strict:
                raise RuntimeError("strict mode does not allow model supplements.")
            if block.citations:
                raise RuntimeError("model supplements must not claim document citations.")
        else:
            if not block.citations:
                raise RuntimeError("grounded statements and inferences need source citations.")
            has_evidence = True
        for citation in block.citations:
            source = by_id.get(citation.source_id)
            if source is None:
                raise RuntimeError("the model cited an unknown source or source version.")
            if citation.asset_id is not None and not any(
                v['source_id'] == citation.source_id and v['asset_id'] == citation.asset_id
                and v['line_start'] <= citation.line_start <= citation.line_end <= v['line_end']
                for v in visual_evidence or []):
                raise RuntimeError('the model cited a visual asset that was not successfully viewed.')
            if citation.asset_id is None and source.document and source.document.format == 'pdf':
                evidence_blocks = [b for b in source.document.blocks
                    if b.line_start <= citation.line_end and b.line_end >= citation.line_start]
                if not any(b.kind not in ('page', 'figure') for b in evidence_blocks):
                    raise RuntimeError('image navigation labels are not textual evidence; view the visual asset first.')
            if not 1 <= citation.line_start <= citation.line_end <= len(source.text.splitlines()):
                raise RuntimeError("the model cited an invalid line range.")
            if chunks is not None and not citation_is_visible(
                citation.source_id, citation.line_start, citation.line_end, chunks,
            ):
                raise RuntimeError("the model cited lines outside the retrieved context it received.")
    if result.coverage == "none" and has_evidence:
        raise RuntimeError("evidence coverage contradicts the answer citations.")
    if result.coverage != "none" and not has_evidence:
        raise RuntimeError("evidence coverage contradicts the answer citations.")


def render_answer(result: NoteAnswer, sources: list[Source]) -> str:
    by_id = {source.source_id: source for source in sources}
    coverage_labels = {"sufficient": "充分", "partial": "部分", "none": "不足"}
    kind_labels = {"grounded": "资料依据", "inference": "分析与推导", "supplement": "模型补充（未外部核验）"}
    lines = [f"资料覆盖：{coverage_labels[result.coverage]}"]
    if result.coverage_note.strip():
        lines.append(result.coverage_note)
    for block in result.blocks:
        lines.extend(["", f"【{kind_labels[block.kind]}】", block.text])
        for citation in block.citations:
            source = by_id[citation.source_id]
            lines.append(
                f"来源：{source.path} | 版本 {source.source_id[:12]} | "
                f"L{citation.line_start}-L{citation.line_end}"
            )
    return "\n".join(lines)
