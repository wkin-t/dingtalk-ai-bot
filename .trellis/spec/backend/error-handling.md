# Error Handling

> How errors are handled in this project.

---

## Overview

Backend errors are classified by the boundary that can recover them. Provider
pre-output failures may select a configured fallback; local construction and
parsing failures do not open the provider circuit. Redis is auxiliary state and
must fail open so a Redis outage cannot block an AI request.

## Error Types

The Gemini streaming path uses an internal pre-output/stream distinction rather
than matching provider message text. `asyncio.CancelledError` is propagated and
never converted into a fallback request. `app.error_safety.safe_error_summary`
is the shared boundary for messages that must be logged or shown to a user.

## Error Handling Patterns

- Wrap the provider await and async iterator pulls until the first visible
  thinking/content chunk. A failure there may open the circuit and run one
  fallback attempt.
- After visible output has started, do not replay the request and do not open
  the circuit. Propagate cancellation; for other failures use a safe category.
- A fallback failure is terminal for that attempt. The error order is fallback
  model first, then primary model, both passed through the safe summary helper.
- `app.gemini_circuit` catches ordinary Redis/client errors internally and
  returns the fail-open result. It stores only the fixed `open` marker.
- Do not catch an exception merely to log and continue unless the surrounding
  code has a defined recovery action.

## API Error Responses

For a DingTalk card, fallback metadata is carried in the existing usage chunk.
The footer shows the actual fallback model, the primary model, and the safe
primary failure summary. A request that starts because of an existing circuit
uses the fixed text `circuit open` and never displays historical error details.
The normal primary footer remains unchanged. Error chunks must contain safe
summaries only; provider response bodies, request objects, URLs, headers and
tracebacks are not client data.

Provider-returned model identifiers are also untrusted display/log fields. When
`chunk.model_version` is present, normalize it with
`app.error_safety.safe_model_name()` before putting it into the usage payload;
normalize `requested_model` at the same usage boundary. This prevents control
characters or markup-like text from reaching statistics, logs, or later
consumers even when the DingTalk footer performs a second display encoding.

## Common Mistakes

- Matching only `no available accounts` and missing generic 401/404/503,
  timeout, or network failures.
- Treating a successfully returned async iterator as a successful provider
  request without pulling its first item.
- Retrying after content has already been sent, which duplicates the reply.
- Returning `str(exception)`, `repr(exception)`, traceback text, or SDK message
  bodies into logs, usage, Redis, or HTML footer content.
- Deriving fallback slot from a model name when multiple route slots share one
  model alias.
