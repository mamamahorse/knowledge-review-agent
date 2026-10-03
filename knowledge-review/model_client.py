"""Model transport: send source snapshots, parse and validate the answer."""

import os
from pathlib import Path

from dotenv import load_dotenv
from openai import APIError, ContentFilterFinishReasonError, LengthFinishReasonError, OpenAI
from pydantic import ValidationError

from answers import NoteAnswer, SourceOnlyAnswer, validate_answer
from sources import Source, format_sources
from chunking import Chunk
from context import format_context
from response_preferences import ResponseLanguage, preference_messages
from structured_output import request_structured

PROJECT_DIR = Path(__file__).resolve().parent


def model_settings() -> tuple[str, dict]:
    """Read the configured Chat Completions-compatible model once per request."""
    load_dotenv(PROJECT_DIR / ".env")
    api_key = os.getenv("LLM_API_KEY", "").strip()
    model = os.getenv("LLM_MODEL", "").strip()
    base_url = os.getenv("LLM_BASE_URL", "").strip()
    if not api_key:
        raise ValueError("LLM_API_KEY is missing from .env.")
    if not model:
        raise ValueError("LLM_MODEL is missing from .env.")
    options = {"api_key": api_key, "timeout": 30.0}
    if base_url:
        options["base_url"] = base_url
    return model, options


def answer_question(
    sources: list[Source], question: str, strict: bool = False,
    *, chunks: list[Chunk] | None = None, response_language: ResponseLanguage | None = None,
) -> NoteAnswer:
    if not question.strip():
        raise ValueError("The question must not be blank.")
    if not sources:
        raise ValueError("At least one source is required.")
    # None explicitly means full text. An empty retrieved list must stay empty.
    context = format_sources(sources) if chunks is None else format_context(chunks, sources)
    preferences = preference_messages(response_language)
    model, client_options = model_settings()
    messages = [
        {"role": "system", "content": (
            "Help the learner understand the technical question, its mechanism and relevant "
            "conditions. Distinguish what the supplied material states from conclusions drawn "
            "from it and background knowledge. Explain gaps and uncertainty using the evidence "
            "actually available. Sources are reference material for the task."
        )},
        {"role": "user", "content": (
            f"Evidence mode: {'source_only' if strict else 'sources_with_background'}\n"
            f"Material scope: {'retrieved excerpts' if chunks is not None else 'complete documents'}\n"
            f"Sources:\n{context}\n\nQuestion:\n{question}"
        )},
    ] + preferences
    try:
        with OpenAI(**client_options) as client:
            schema = SourceOnlyAnswer if strict else NoteAnswer
            generated = request_structured(client, model, messages, schema)
            result = NoteAnswer.model_validate(generated.model_dump())
    except APIError as exc:
        raise RuntimeError(f"model request failed: {exc}") from exc
    except ValidationError as exc:
        mode = "strict source-only answer" if strict else "answer"
        raise RuntimeError(f"the model response did not match the required {mode} structure.") from exc
    except LengthFinishReasonError as exc:
        raise RuntimeError("the model response was truncated; try a narrower question.") from exc
    except ContentFilterFinishReasonError as exc:
        raise RuntimeError("the model response was blocked by the service's content filter.") from exc
    validate_answer(result, sources, strict=strict, chunks=chunks)
    return result
