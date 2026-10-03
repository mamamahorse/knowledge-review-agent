"""Learning-unit workflow: validate source context, ask, save, then assess real answers."""

from hashlib import sha256

from chunking import Chunk
from embedding_config import select_embedding_config
from knowledge_tools import KnowledgeTools
from agent_loop import focus_on_lines
from practice_model import generate_question, grade_answer
from practice_store import (
    MAX_QUESTIONS_PER_SESSION, PracticeStore, archived_sources, public_session,
)
from sources import Source
from response_preferences import ResponseLanguage, validate_language

MAX_VISIBLE_CHARS = 24_000


def start_practice(
    store: PracticeStore, sources: list[Source], document_ids_by_source: dict[str, str],
    focus_source_id: str, start: int, end: int, objective: str,
    *, response_language: ResponseLanguage = "zh",
) -> dict:
    validate_language(response_language)
    tools = KnowledgeTools(sources, select_embedding_config("e5-small"))
    focus = focus_on_lines(tools, focus_source_id, start, end)
    focus_source = next(source for source in sources if source.source_id == focus_source_id)
    fallback_goal = (f"理解 {focus_source.path.name} 的 L{start}–L{end} 所选内容"
                     if response_language == "zh" else
                     f"Understand the selected content in {focus_source.path.name}, L{start}–L{end}")
    goal = objective.strip() or fallback_goal
    selected_text = "\n".join(focus_source.text.splitlines()[start - 1:end])
    visible = [focus.chunk]
    if len(sources) > 1:
        query = objective.strip() or selected_text[:240]
        hits = tools.search_notes(query, top_k=3).hits
        visible.extend(hit.chunk for hit in hits if hit.chunk not in visible)
    if sum(len(chunk.text) for chunk in visible) > MAX_VISIBLE_CHARS:
        raise ValueError("选段上下文过长，请选择更短的原文。")
    try:
        draft = generate_question(sources, visible, focus_source_id, start, end, goal,
                                  response_language=response_language)
    except ValueError as exc:
        raise RuntimeError("the model returned an invalid practice question.") from exc
    visible_source_ids = {chunk.source_id for chunk in visible}
    snapshots = [{
        "document_id": document_ids_by_source[source.source_id],
        "source_id": source.source_id,
        "path": str(source.path), "name": source.path.name, "text": source.text,
    } for source in sources if source.source_id in visible_source_ids]
    ranges = [{"source_id": chunk.source_id, "line_start": chunk.line_start,
               "line_end": chunk.line_end} for chunk in visible]
    session_id = store.create(goal, focus_source_id, start, end, snapshots, ranges, draft,
                              response_language=response_language)
    return public_session(store.get(session_id))


def _visible_chunks(record: dict, sources: list[Source]) -> list[Chunk]:
    by_id = {source.source_id: source for source in sources}
    chunks = []
    for item in record["visible_ranges"]:
        source = by_id[item["source_id"]]
        start, end = item["line_start"], item["line_end"]
        text = "\n".join(source.text.splitlines()[start - 1:end])
        identity = f"{source.source_id}:archived:{start}:{end}"
        chunks.append(Chunk(sha256(identity.encode()).hexdigest(), source.source_id,
                            (), start, end, text))
    return chunks


def grade_pending_answer(
    store: PracticeStore, session_id: str, *, response_language: ResponseLanguage | None = None,
) -> dict:
    if response_language is not None:
        store.set_grading_language(session_id, response_language)
    record = store.get(session_id)
    if record["status"] != "grading":
        raise ValueError("当前没有等待评分的答案。")
    question = record["questions"][-1]
    if question["answer_text"] is None:
        raise ValueError("请先提交真实回答。")
    sources = archived_sources(record["snapshots"])
    visible = _visible_chunks(record, sources)
    can_follow_up = question["position"] < MAX_QUESTIONS_PER_SESSION
    item = store.item(record["review_item_id"]) if record["review_item_id"] and hasattr(store, "item") else None
    grade, rating = grade_answer(
        question["draft"], question["answer_text"], sources, visible,
        can_follow_up=can_follow_up,
        response_language=record["response_language"],
        learning_context={"target": question["target"] or (question["draft"].learning.target.model_dump() if question["draft"].learning else None),
                          "assistance": question["assistance"], "recent_performance": item["history"] if item else [],
                          "other_capability_evidence": item["related_history"] if item else []},
    )
    store.save_grade(session_id, grade, rating, grade.follow_up)
    return public_session(store.get(session_id))
