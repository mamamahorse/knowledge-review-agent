"""Send output contracts through API parameters, never appended prompt text."""

from typing import TypeVar

from openai import APIError
from pydantic import BaseModel
from openai.lib._parsing._completions import type_to_response_format_param

from generation_budget import GenerationBudget

Result = TypeVar("Result", bound=BaseModel)
RESULT_TOOL = "submit_result"
# Learn a provider limitation once per process instead of repeating a rejected
# response_format request for every answer. Cache only successful tool results.
FUNCTION_OUTPUT_PROVIDERS: dict[tuple[str, str], str] = {}


def response_format_is_unavailable(error: APIError) -> bool:
    message = str(error).lower()
    return (
        getattr(error, "status_code", None) == 400
        and "response_format" in message
        and ("unavailable" in message or "unsupported" in message)
    )


def result_tools(schema):
    return [{"type": "function", "function": {
        "name": RESULT_TOOL,
        "description": "Deliver the completed task, with its assessment and source evidence, to the learner in the learning workspace.",
        "parameters": schema.model_json_schema(),
    }}]


def structured_protocols(schema):
    # Account for the strict schema actually expanded by this installed SDK,
    # and for the existing provider-compatible result tool independently.
    return [{"response_format": type_to_response_format_param(schema)},
            {"tools": result_tools(schema), "tool_choice": {"type": "function", "function": {"name": RESULT_TOOL}}}]


def request_structured(client, model: str, messages: list[dict], schema: type[Result], *, budget=None, before_request=None) -> Result:
    budget = budget or GenerationBudget.from_env()
    provider = (str(client.base_url), model)
    if provider not in FUNCTION_OUTPUT_PROVIDERS:
        try:
            if before_request:
                before_request()
            budget.check(messages, model=model, max_tokens=budget.output_tokens,
                         response_format=type_to_response_format_param(schema))
            response = client.beta.chat.completions.parse(
                model=model, messages=messages, response_format=schema,
                max_tokens=budget.output_tokens,
            )
        except APIError as exc:
            if not response_format_is_unavailable(exc):
                raise
        else:
            message = checked_message(response)
            if message.parsed is None:
                raise RuntimeError("the model returned no structured result.")
            return message.parsed
    return request_tool_result(client, model, messages, schema, provider, budget, before_request)


def request_tool_result(client, model, messages, schema, provider, budget, before_request=None):
    # This is a result submission, not an executable or state-writing tool.
    # Compatible providers may not enforce strict schemas; validate locally too.
    tools = result_tools(schema)
    mode = FUNCTION_OUTPUT_PROVIDERS.get(provider, "forced")
    choice = "auto" if mode == "auto" else {"type": "function", "function": {"name": RESULT_TOOL}}
    try:
        if before_request:
            before_request()
        budget.check(messages, model=model, max_tokens=budget.output_tokens, tools=tools, tool_choice=choice)
        response = client.chat.completions.create(
            model=model, messages=messages, tools=tools, tool_choice=choice,
            max_tokens=budget.output_tokens,
        )
    except APIError as exc:
        error = str(exc).lower()
        if (mode != "forced" or getattr(exc, "status_code", None) != 400
                or "tool_choice" not in error
                or not any(term in error for term in ("unsupported", "unavailable", "does not support"))):
            raise
        # Some reasoning providers support tools only with automatic selection.
        # Do not execute a tool or start another assistant turn to obtain the result.
        mode = "auto"
        if before_request:
            before_request()
        budget.check(messages, model=model, max_tokens=budget.output_tokens, tools=tools, tool_choice="auto")
        response = client.chat.completions.create(
            model=model, messages=messages, tools=tools, tool_choice="auto",
            max_tokens=budget.output_tokens,
        )
    message = checked_message(response)
    calls = message.tool_calls or []
    if len(calls) != 1 or calls[0].type != "function" or calls[0].function.name != RESULT_TOOL:
        raise RuntimeError("the model did not submit one structured result.")
    result = schema.model_validate_json(calls[0].function.arguments)
    FUNCTION_OUTPUT_PROVIDERS[provider] = mode
    return result


def checked_message(response):
    if not response.choices:
        raise RuntimeError("the model returned no result choices.")
    choice = response.choices[0]
    if choice.finish_reason == "length":
        raise RuntimeError("the model response was truncated.")
    if choice.finish_reason == "content_filter":
        raise RuntimeError("the model response was blocked by the service's content filter.")
    if getattr(choice.message, "refusal", None):
        raise RuntimeError("the model declined the request.")
    return choice.message
