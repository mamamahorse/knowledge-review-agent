"""Learning-unit workflow: validate source context, ask, save, then assess real answers."""

from hashlib import sha256
import re
from itertools import zip_longest

from chunking import Chunk, chunk_source
from embedding_config import select_embedding_config
from knowledge_tools import KnowledgeTools
from agent_loop import document_focus
from practice_model import generate_question, grade_answer, question_messages, LearningQuestionDraft
from generation_budget import GenerationBudget, BudgetError, fit_chunks
from structured_output import structured_protocols
from library_store import LibraryStore
from practice_store import (
    MAX_QUESTIONS_PER_SESSION, PracticeStore, archived_sources, public_session,
)
from sources import Source
from response_preferences import ResponseLanguage, validate_language


def start_practice(
    store: PracticeStore, sources: list[Source], document_ids_by_source: dict[str, str],
    focus_source_id: str, start: int | None, end: int | None, objective: str,
    *, response_language: ResponseLanguage = "zh", collection_id="root", before_request=None, document_dependencies=(),
) -> dict:
    validate_language(response_language)
    LibraryStore(store).get(collection_id)
    focus_source = next(source for source in sources if source.source_id == focus_source_id)
    whole_scope = start is None and end is None
    if (start is None) != (end is None):
        raise ValueError("选段起止行需同时提供或同时省略。")
    if whole_scope:
        start, end = 1, len(focus_source.text.splitlines())
    fallback_goal = (f"理解 {focus_source.path.name} 的 L{start}–L{end} 所选内容"
                     if response_language == "zh" else
                     f"Understand the selected content in {focus_source.path.name}, L{start}–L{end}")
    goal = objective.strip() or fallback_goal
    if whole_scope:
        goal = objective.strip() or ("理解当前资料中的一个重要知识点" if response_language == "zh" else
                                    "Understand an important concept in the current material")
    required = [] if whole_scope else [document_focus(focus_source, start, end).chunk]
    if whole_scope:
        # Round-robin documents and spaced sections avoid spending the entire budget on the first file.
        terms = set(re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]{2}", objective.lower()))
        groups = []
        for source in sources:
            chunks = chunk_source(source)
            spaced = sorted(range(len(chunks)), key=lambda i: (i % 4, i))
            ordered = [chunks[i] for i in spaced]
            if terms:
                ordered.sort(key=lambda c: -sum(term in c.text.lower() for term in terms))
            groups.append(ordered)
        optional = [c for row in zip_longest(*groups) for c in row if c is not None]
    else:
        optional = []
        if len(sources) > 1:
            tools = KnowledgeTools(sources, select_embedding_config("e5-small"))
            query = objective.strip() or required[0].text[:240]
            optional = [hit.chunk for hit in tools.search_notes(query, top_k=3).hits if hit.chunk not in required]
    budget = GenerationBudget.from_env()
    protocols = structured_protocols(LearningQuestionDraft)
    # A whole-scope question should leave room for its saved rubric, learner answer
    # and the larger grading request. Every later request still checks its own budget.
    assessment_headroom = max(4096, budget.input_limit // 4) if whole_scope else 0
    def accepts(chunks):
        messages = question_messages(sources, required + chunks, focus_source_id, start, end, goal,
                                     response_language=response_language, whole_scope=whole_scope)
        return all(budget.usage(messages, **protocol) + 512 + assessment_headroom <= budget.input_limit for protocol in protocols)
    if not accepts([]):
        raise BudgetError("选段或学习目标超过生成预算，请缩小选段；也可取消选段，以整篇范围出题。")
    visible = required + fit_chunks(optional, accepts)
    if not visible:
        raise BudgetError("当前预算无法容纳资料的完整原文行，未生成题目。请调整资料范围或生成预算。")
    try:
        draft = generate_question(sources, visible, focus_source_id, start, end, goal,
                                  response_language=response_language, whole_scope=whole_scope,
                                  **({'before_request': before_request} if before_request else {}))
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
                              response_language=response_language, collection_id=collection_id,
                              document_dependencies=document_dependencies)
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
    store.assert_basis(session_id)
    if record["status"] != "grading":
        raise ValueError("当前没有等待评分的答案。")
    question = record["questions"][-1]
    if question["answer_text"] is None:
        raise ValueError("请先提交真实回答。")
    sources = archived_sources(record["snapshots"])
    visible = _visible_chunks(record, sources)
    can_follow_up = question["position"] < MAX_QUESTIONS_PER_SESSION
    item = store.item(record["review_item_id"]) if record["review_item_id"] and hasattr(store, "item") else None
    if record.get('basis'):
        from chat_practice import grade_discussion
        grade, rating = grade_discussion(store, record, question, sources, visible, can_follow_up)
        store.save_grade(session_id, grade, rating, grade.follow_up)
        return public_session(store.get(session_id))
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
