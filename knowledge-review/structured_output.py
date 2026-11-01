"""Send output contracts through API parameters, never appended prompt text."""

from copy import deepcopy
from typing import TypeVar

from openai import APIError, APIConnectionError, APITimeoutError, APIStatusError
from pydantic import BaseModel, ValidationError
from openai.lib._parsing._completions import type_to_response_format_param

from generation_budget import GenerationBudget, BudgetError
from structured_recovery import RecoveryTrace, MAX_RETRIES, current_validator, current_guard, error_details

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


def request_structured(client, model: str, messages: list[dict], schema: type[Result], *, budget=None,
                       before_request=None, on_delta=None, telemetry=None, validator=None,
                       protocol_policy='compatible') -> Result:
    """One result, up to two corrective retries; every attempt uses the original schema/guards."""
    if protocol_policy not in ('compatible', 'forced_tools'):
        raise ValueError('Unknown structured output protocol policy')
    # Avoid multiplying SDK transport retries with the explicit three-attempt limit.
    if client.max_retries:
        client = client.with_options(max_retries=0)
    trace = RecoveryTrace(schema)
    working = deepcopy(messages)
    validate = validator if validator is not None else current_validator()
    scoped_guard = current_guard()
    def guarded_request():
        if scoped_guard:
            scoped_guard()
        if before_request:
            before_request()
    def display(text):
        trace.emitted = True
        on_delta(text)
    for attempt in range(MAX_RETRIES + 1):
        trace.attempt = attempt; trace.original = None; trace.repaired = None
        trace.assistant = None; trace.stage = 'request'
        try:
            delta = (display if not trace.emitted else lambda _:None) if on_delta is not None else None
            result = _request_once(client, model, working, schema, budget=budget,
                before_request=guarded_request, on_delta=delta, telemetry=telemetry,
                trace=trace, protocol_policy=protocol_policy)
            return trace.validate(result, validate)
        except BaseException as exc:
            validation = isinstance(exc, (ValidationError, ValueError)) and not isinstance(exc, BudgetError) \
                and trace.stage in ('parse', 'schema', 'business_validation')
            transient = isinstance(exc, (APIConnectionError, APITimeoutError)) or \
                (isinstance(exc, APIStatusError) and exc.status_code in (408,500,502,503,504))
            retry = (validation or transient) and attempt < MAX_RETRIES
            trace.write('attempt_failed', errors=error_details(exc), error_type=type(exc).__name__, phase=trace.stage,
                        retry=retry, exhausted=(validation or transient) and not retry)
            exc.structured_output_trace_id = trace.id
            if validation or transient:
                exc.structured_recovery_exhausted = not retry
                exc.structured_recovery_stage = trace.stage
            if not retry:
                raise
            if validation:
                working = trace.feedback(working, exc)


def _request_once(client, model, messages, schema, *, budget=None, before_request=None,
                  on_delta=None, telemetry=None, trace, protocol_policy='compatible'):
    budget = budget or GenerationBudget.from_env()
    if on_delta is not None:
        return request_streamed(client, model, messages, schema, budget, before_request, on_delta,
                                trace=trace, protocol_policy=protocol_policy)
    provider = (str(client.base_url), model)
    if protocol_policy == 'forced_tools':
        return request_tool_result(client, model, messages, schema, provider, budget,
                                   before_request, telemetry, trace=trace, forced_only=True)
    if provider not in FUNCTION_OUTPUT_PROVIDERS and not getattr(trace, 'tool_mode', None):
        try:
            if before_request:
                before_request()
            budget.check(messages, model=model, max_tokens=budget.output_tokens,
                         response_format=type_to_response_format_param(schema))
            # Same SDK-expanded schema on the wire; retain the raw content before local parsing.
            response = client.chat.completions.create(
                model=model, messages=messages, response_format=type_to_response_format_param(schema),
                max_tokens=budget.output_tokens,
            )
        except APIError as exc:
            if not response_format_is_unavailable(exc):
                raise
        else:
            if telemetry is not None:
                telemetry(response, 'json_schema')
            if response.choices:
                trace.response(response.choices[0].message, response.id, 'json_schema', response.choices[0].finish_reason)
            message = checked_message(response)
            if message.content is None:
                raise RuntimeError("the model returned no structured result.")
            return trace.decode(message.content)
    return request_tool_result(client, model, messages, schema, provider, budget, before_request, telemetry, trace=trace)


def request_streamed(client, model, messages, schema, budget, before_request, on_delta, *, trace, protocol_policy='compatible'):
    from answer_stream import collect_answer
    provider = (str(client.base_url), model)
    emitted = False

    def emit(text):
        nonlocal emitted
        emitted = True
        on_delta(text)

    def send(**protocol):
        if before_request:
            before_request()
        budget.check(messages, model=model, max_tokens=budget.output_tokens, **protocol)
        def observe(text, finished):
            trace.original=text
            trace.assistant=dict(role='assistant',content=text)
            trace.write('stream_response', original=text, finish_reason=finished)
        return collect_answer(client, {'model': model, 'messages': messages,
                                      'max_tokens': budget.output_tokens, **protocol},
                              schema, emit, tools='tools' in protocol, decoder=trace.decode, observer=observe)

    if protocol_policy != 'forced_tools' and provider not in FUNCTION_OUTPUT_PROVIDERS and not getattr(trace, 'tool_mode', None):
        try:
            return send(response_format=type_to_response_format_param(schema))
        except APIError as exc:
            if emitted or not response_format_is_unavailable(exc):
                raise
    mode = 'forced' if protocol_policy == 'forced_tools' else getattr(trace,'tool_mode',None) or FUNCTION_OUTPUT_PROVIDERS.get(provider, 'forced')
    choice = 'auto' if mode == 'auto' else {'type': 'function', 'function': {'name': RESULT_TOOL}}
    trace.tool_mode=mode
    try:
        result = send(tools=result_tools(schema), tool_choice=choice)
    except APIError as exc:
        error = str(exc).lower()
        if (protocol_policy == 'forced_tools' or emitted or mode != 'forced' or getattr(exc, 'status_code', None) != 400
                or 'tool_choice' not in error
                or not any(term in error for term in ('unsupported', 'unavailable', 'does not support'))):
            raise
        mode = 'auto'
        trace.tool_mode=mode
        result = send(tools=result_tools(schema), tool_choice='auto')
    FUNCTION_OUTPUT_PROVIDERS[provider] = mode
    return result


def request_tool_result(client, model, messages, schema, provider, budget, before_request=None, telemetry=None,
                        *, trace, forced_only=False):
    # This is a result submission, not an executable or state-writing tool.
    # Compatible providers may not enforce strict schemas; validate locally too.
    tools = result_tools(schema)
    mode = 'forced' if forced_only else getattr(trace,'tool_mode',None) or FUNCTION_OUTPUT_PROVIDERS.get(provider, "forced")
    trace.tool_mode=mode
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
        if (forced_only or mode != "forced" or getattr(exc, "status_code", None) != 400
                or "tool_choice" not in error
                or not any(term in error for term in ("unsupported", "unavailable", "does not support"))):
            raise
        # Some reasoning providers support tools only with automatic selection.
        # Do not execute a tool or start another assistant turn to obtain the result.
        mode = "auto"
        trace.tool_mode=mode
        if before_request:
            before_request()
        budget.check(messages, model=model, max_tokens=budget.output_tokens, tools=tools, tool_choice="auto")
        response = client.chat.completions.create(
            model=model, messages=messages, tools=tools, tool_choice="auto",
            max_tokens=budget.output_tokens,
        )
    if telemetry is not None:
        telemetry(response, 'tool_' + mode)
    if response.choices:
        trace.response(response.choices[0].message, response.id, 'tool_' + mode, response.choices[0].finish_reason)
    message = checked_message(response)
    calls = message.tool_calls or []
    if len(calls) != 1 or calls[0].type != "function" or calls[0].function.name != RESULT_TOOL:
        raise RuntimeError("the model did not submit one structured result.")
    result = trace.decode(calls[0].function.arguments)
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
