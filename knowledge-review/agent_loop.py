"""Bounded question-answering loop over the current knowledge tools."""

import json
from dataclasses import dataclass
from hashlib import sha256

from openai import APIError, OpenAI

from answers import NoteAnswer
from chunking import Chunk
from context import format_context
from knowledge_tools import KnowledgeTools, MAX_READ_LINES, knowledge_tool_definitions
from model_client import answer_question, model_settings
from response_preferences import ResponseLanguage, preference_messages
from sources import Source

MAX_TOOL_CALLS = 6


@dataclass(frozen=True)
class AgentStep:
    name: str
    arguments: object | None
    error: str | None = None


@dataclass(frozen=True)
class AgentRun:
    answer: NoteAnswer
    steps: tuple[AgentStep, ...]
    stop_reason: str
    visible_chunks: tuple[Chunk, ...]


@dataclass(frozen=True)
class FocusSelection:
    """Original lines selected by the user, plus a small visible context window."""

    chunk: Chunk
    line_start: int
    line_end: int


def focus_on_lines(
    tools: KnowledgeTools, source_id: str, line_start: int, line_end: int,
    *, context_lines: int = 5,
) -> FocusSelection:
    """Resolve a selection against the same immutable snapshots used by tools."""
    selected = tools.read_source(source_id, line_start, line_end)
    if type(context_lines) is not int or context_lines < 0:
        raise ValueError("Focus context_lines must be a nonnegative integer.")
    total = len(selected.source.text.splitlines())
    room = MAX_READ_LINES - (line_end - line_start + 1)
    before = min(context_lines, line_start - 1, room // 2)
    after = min(context_lines, total - line_end, room - before)
    context = tools.read_source(source_id, line_start - before, line_end + after)
    return FocusSelection(context.chunk, line_start, line_end)


def document_focus(source: Source, line_start: int, line_end: int) -> FocusSelection:
    """An explicit web selection is separate from the Agent's bounded read tool."""
    lines = source.text.splitlines()
    if not 1 <= line_start <= line_end <= len(lines):
        raise ValueError("The selected lines are outside the current document.")
    start, end = max(1, line_start - 5), min(len(lines), line_end + 5)
    identity = f"{source.source_id}:web-focus-v1:{start}:{end}"
    chunk = Chunk(sha256(identity.encode()).hexdigest(), source.source_id, (),
                  start, end, "\n".join(lines[start - 1:end]))
    return FocusSelection(chunk, line_start, line_end)


def run_agent(
    sources: list[Source], question: str, tools: KnowledgeTools, *,
    strict: bool = False, max_tool_calls: int = 4,
    focus: FocusSelection | None = None,
    response_language: ResponseLanguage | None = None,
) -> AgentRun:
    """Let the model select tools; let the program execute and validate them."""
    if not question.strip():
        raise ValueError("The question must not be blank.")
    if not sources or sources != tools.sources:
        raise ValueError("Agent tools must use the same source snapshots.")
    if type(max_tool_calls) is not int or not 1 <= max_tool_calls <= MAX_TOOL_CALLS:
        raise ValueError(f"max_tool_calls must be between 1 and {MAX_TOOL_CALLS}.")

    focus_prompt = ""
    if focus is not None:
        focus_context = format_context([focus.chunk], sources)
        if not focus.chunk.line_start <= focus.line_start <= focus.line_end <= focus.chunk.line_end:
            raise ValueError("The selected lines must be inside the visible focus context.")
        focus_prompt = (
            f"\n\nUser-selected lines: source_id={focus.chunk.source_id}, "
            f"L{focus.line_start}-L{focus.line_end}. Explain these lines first. "
            "The surrounding lines below are context, not the user's selection.\n"
            f"{focus_context}"
        )

    preferences = preference_messages(response_language)
    model, client_options = model_settings()
    catalog = "\n".join(
        f"- {source.path.name} | source_id={source.source_id} | {len(source.text.splitlines())} lines"
        for source in sources
    )
    messages = [
        {"role": "system", "content": (
            "Consider the learner's question and the evidence already visible. Existing "
            "passages may be enough to explain the question; general conversation may need "
            "no source lookup. When a question depends on missing source evidence, semantic "
            "search can discover relevant material and reading original lines can resolve "
            "missing context. Choose whether to search, read, reformulate a query, or finish. "
            "The source catalog provides navigation metadata rather than document contents, "
            "and search scores describe similarity. Begin with the selected passage when supplied."
        )},
        {"role": "user", "content": f"Question: {question}\n\nLoaded sources:\n{catalog}{focus_prompt}"},
    ] + preferences
    definitions = [
        {"type": "function", "function": definition}
        for definition in knowledge_tool_definitions()
    ]
    steps: list[AgentStep] = []
    visible: dict[tuple[str, int, int], Chunk] = {}
    if focus is not None:
        chunk = focus.chunk
        visible[(chunk.source_id, chunk.line_start, chunk.line_end)] = chunk
    stop_reason = "tool_budget"
    try:
        with OpenAI(**client_options) as client:
            while len(steps) < max_tool_calls:
                response = client.chat.completions.create(
                    model=model, messages=messages, tools=definitions, tool_choice="auto",
                )
                if not response.choices:
                    raise RuntimeError("the model returned no agent choices.")
                choice = response.choices[0]
                if choice.finish_reason == "length":
                    raise RuntimeError("the agent tool decision was truncated.")
                if choice.finish_reason == "content_filter":
                    raise RuntimeError("the agent tool decision was blocked by the service.")
                calls = choice.message.tool_calls or []
                if not calls:
                    stop_reason = "model_finished"
                    break

                messages.append({
                    "role": "assistant", "content": choice.message.content,
                    "tool_calls": [call.model_dump(exclude_none=True) for call in calls],
                })
                for call in calls:
                    arguments = None
                    try:
                        if len(steps) >= max_tool_calls:
                            raise ValueError("Tool-call budget exhausted.")
                        if call.type != "function":
                            raise ValueError("Only function tools are supported.")
                        arguments = json.loads(call.function.arguments)
                        execution = tools.execute(call.function.name, arguments)
                        for chunk in execution.visible_chunks:
                            visible[(chunk.source_id, chunk.line_start, chunk.line_end)] = chunk
                        output = execution.output
                        error = None
                    except (ValueError, TypeError) as exc:
                        error = str(exc)
                        output = {"error": error}
                    if len(steps) < max_tool_calls:
                        steps.append(AgentStep(call.function.name, arguments, error))
                    messages.append({
                        "role": "tool", "tool_call_id": call.id,
                        "content": json.dumps(output, ensure_ascii=False),
                    })
    except APIError as exc:
        raise RuntimeError(f"agent model request failed: {exc}") from exc

    # The final request receives only source text actually returned by successful tools.
    # Existing structured-output and citation validation remain the single answer path.
    chunks = list(visible.values())
    final_question = question
    if focus is not None:
        final_question += (
            f"\n\nUser-selected lines: source_id={focus.chunk.source_id}, "
            f"L{focus.line_start}-L{focus.line_end}. Explain the selection first; "
            "use surrounding visible lines for context."
        )
    answer = answer_question(sources, final_question, strict=strict, chunks=chunks,
                             response_language=response_language)
    return AgentRun(answer, tuple(steps), stop_reason, tuple(chunks))
