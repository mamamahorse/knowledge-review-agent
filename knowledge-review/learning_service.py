"""Opt-in enrollment and source-grounded variants of a fixed learning target."""

import json
from pydantic import Field

from context import format_context
from learning_types import LearningDraft
from practice_model import (QuestionDraft, LearningQuestionDraft, _request_structured,
                            validate_question, validate_grade, DiscussionQuestion, DiscussionLearning, parse_question, parse_grade)
from practice_service import _visible_chunks
from practice_store import archived_sources, public_session
from response_preferences import preference_messages, validate_language
from answers import StrictModel


class EnrollmentDraft(StrictModel):
    learning: LearningDraft
    criterion_indices: list[int] = Field(min_length=2, max_length=4,
        description="Each saved rubric point's 1-based reusable target criterion index; together cover all target criteria.")


class VariantDraft(QuestionDraft):
    criterion_indices: list[int] = Field(min_length=2, max_length=4,
        description="Each rubric point's 1-based fixed target criterion index; cover every target criterion.")


def source_context(store, session_id):
    record = store.get(session_id)
    store.assert_basis(session_id)
    sources = archived_sources(record["snapshots"])
    return record, sources, _visible_chunks(record, sources)


def enroll_question(store, session_id, question_id, *, unit_id=None, response_language="zh", collection_id="root"):
    record, sources, visible = source_context(store, session_id)
    question = next((q for q in record["questions"] if q["id"] == question_id), None)
    if question is None:
        raise KeyError("找不到这道题。")
    if question["target_id"]:
        # Retrying an enrollment must not call the model again.
        for group in store.list_plan():
            for item in group["items"]:
                if item["target_id"] == question["target_id"]:
                    return item["id"]
    draft = question["draft"]
    learning = draft.learning
    if learning is None:
        messages = [{"role": "system", "content":
            "Describe the reusable knowledge and observable learning target assessed by this existing exercise. "
            "Use the saved source material and scope. This task identifies the target, without reassessing an answer."},
            {"role": "user", "content": json.dumps({"question": draft.model_dump(),
                "sources": format_context(visible, sources)}, ensure_ascii=False)}] + preference_messages(response_language)
        extracted = _request_structured(messages, EnrollmentDraft)
        learning = extracted.learning
        checked = draft.model_copy(update={"learning": learning, "criterion_indices": extracted.criterion_indices})
    else:
        checked = draft
    validate_question(checked, sources, visible)
    return store.enroll(session_id, question_id, learning, unit_id=unit_id, criterion_indices=checked.criterion_indices,
                        collection_id=collection_id)


def start_review(store, item_id, *, mode="variant", response_language="zh"):
    validate_language(response_language)
    if mode not in {"variant", "original"}:
        raise ValueError("未知的复习方式。")
    item = store.item(item_id)
    if item["status"] != "active":
        raise ValueError("这个目标已暂停复习。")
    record, sources, visible = source_context(store, item["source_session_id"])
    if item["pending_session_id"]:
        store.assert_basis(item['pending_session_id'])
        return public_session(store.get(item["pending_session_id"]))
    original = parse_question(item['original'])
    learning = (DiscussionLearning if isinstance(original, DiscussionQuestion) else LearningDraft).model_validate({"knowledge": item["knowledge"], "target": item["target"]})
    if mode == "original":
        draft = original.model_copy(update={"learning": learning,
            "criterion_indices": item["original_criterion_indices"] or original.criterion_indices or list(range(1, len(original.rubric_points) + 1))})
    elif isinstance(original, DiscussionQuestion):
        from chat_practice import variant
        draft = variant(store, record, item, response_language)
    else:
        messages = [{"role": "system", "content":
            "Create a fresh exercise assessing the saved learning target, with comparable scope and support conditions. "
            "Vary the scenario or wording while retaining the target's observable criteria. "
            "Consider the learner's recent answers and avoid simply repeating the previous question."},
            {"role": "user", "content": json.dumps({"knowledge": item["knowledge"], "target": item["target"],
                "original_question": item["original"], "recent_performance": item["history"],
                "other_capability_evidence": item["related_history"],
                "sources": format_context(visible, sources)}, ensure_ascii=False)}] + preference_messages(response_language)
        if record.get('basis'):
            from chat_practice import live_basis
            messages.append({'role': 'user', 'content': json.dumps({'discussion_angles': live_basis(store, record)}, ensure_ascii=False)})
        draft = _request_structured(messages, VariantDraft,
            **({'before_request': lambda: store.assert_basis(record['id'])} if record.get('basis') else {})).model_copy(update={"learning": learning})
    validate_question(draft, sources, visible)
    session_id = store.create(item["target"]["title"], record["focus_source_id"], record["focus_start"], record["focus_end"],
        record["snapshots"], record["visible_ranges"], draft, response_language=response_language,
        review_item_id=item_id, review_mode=mode, target_id=item["target_id"],
        collection_id=item["collection_id"],
        basis=record.get('basis'),
        question_ref=item["original_question_id"] if mode == "original" else None)
    return public_session(store.get(session_id))


def add_dimension(store, item_id, capability, *, response_language="zh"):
    item = store.item(item_id)
    record, sources, visible = source_context(store, item["source_session_id"])
    if isinstance(parse_question(item['original']), DiscussionQuestion):
        from chat_practice import variant
        draft = variant(store, record, item, response_language, capability=capability)
        session_id = store.create(draft.objective, '', 1, 0, [], [], draft,
            response_language=response_language, collection_id=item['collection_id'], basis=record['basis'])
        question_id = store.get(session_id)['questions'][0]['id']
        store.enroll(session_id, question_id, draft.learning, unit_id=item['unit_id'], collection_id=item['collection_id'])
        return public_session(store.get(session_id))
    messages = [{"role": "system", "content":
        "Design an exercise assessing the requested capability for this saved knowledge unit. "
        "Describe a concrete observable target and source-grounded reference criteria. Learning routes remain suggestions."},
        {"role": "user", "content": json.dumps({"knowledge": item["knowledge"], "requested_capability": capability,
            "sources": format_context(visible, sources)}, ensure_ascii=False)}] + preference_messages(response_language)
    if record.get('basis'):
        from chat_practice import live_basis
        messages.append({'role': 'user', 'content': json.dumps({'discussion_angles': live_basis(store, record)}, ensure_ascii=False)})
    draft = _request_structured(messages, LearningQuestionDraft,
        **({'before_request': lambda: store.assert_basis(record['id'])} if record.get('basis') else {}))
    if draft.learning.target.capability != capability:
        raise ValueError("生成的能力维度与选择不一致，请重试。")
    draft = draft.model_copy(update={"learning": draft.learning.model_copy(update={"knowledge": LearningDraft.model_validate(
        {"knowledge": item["knowledge"], "target": item["target"]}).knowledge})})
    validate_question(draft, sources, visible)
    session_id = store.create(draft.objective, record["focus_source_id"], record["focus_start"], record["focus_end"],
        record["snapshots"], record["visible_ranges"], draft, response_language=response_language,
        collection_id=item["collection_id"], basis=record.get('basis'))
    question_id = store.get(session_id)["questions"][0]["id"]
    store.enroll(session_id, question_id, draft.learning, unit_id=item["unit_id"], collection_id=item["collection_id"])
    return public_session(store.get(session_id))


def correct_grade(store, session_id, question_id, updates, reason, expected_revision):
    record, sources, visible = source_context(store, session_id)
    question = next((q for q in record["questions"] if q["id"] == question_id), None)
    if question is None or question["grade"] is None:
        raise KeyError("找不到可修改的评分。")
    from practice_model import GradeDraft
    grade = parse_grade({**question["grade"].model_dump(), **updates, "rating_reason": reason})
    validate_grade(grade, question["draft"], question["answer_text"], sources, visible,
                   can_follow_up=question["position"] < 3)
    store.revise_grade(session_id, question_id, grade, reason, expected_revision)
    return public_session(store.get(session_id))
