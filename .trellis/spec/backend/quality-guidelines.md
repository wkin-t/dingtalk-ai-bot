# Quality Guidelines

> Code quality standards for backend development.

---

## Overview

Changes must preserve the four backend routing modes and the existing streaming
chunk contract. A feature that changes provider recovery must include tests for
both the provider boundary and the final client-visible metadata.

## Forbidden Patterns

Do not use provider error text matching as the circuit predicate, put raw
exceptions in user-visible fields, or reuse the five-second business Redis
client in an async request path. Do not infer route slots from model names when
the caller can pass the slot explicitly.

## Required Patterns

Use bounded, fail-open auxiliary state operations; propagate cancellation; keep
fallback to one attempt; and sanitize model/error text before inserting it into
DingTalk HTML. Preserve direct image client behavior when changing Gemini text
request recovery.

## Testing Requirements

At minimum run `python -m compileall -q app main.py` and targeted tests for the
changed client, circuit module, route consumer, and compatibility search path.
For a cross-layer change run `pytest -q tests` before completion.

## Cross-Layer Contract: Gemini Route Slots and Usage Models

### 1. Scope / Trigger

This contract applies when a route decision selects a Gemini `lite`, `fast`, or
`pro` slot and the request can cross the backend dispatcher into Vertex
fallback. It also applies when provider metadata becomes usage/statistics data.

### 2. Signatures

- `create_backend_stream(..., route_slot: str | None)` forwards a validated
  slot to the Gemini stream client.
- `call_gemini_stream(..., target_model: str, route_slot: str | None)` selects
  the fallback override for that explicit slot.
- `safe_model_name(value)` normalizes provider model identifiers before usage
  publication.

### 3. Contracts

- Valid route slots are `router`, `lite`, `fast`, and `pro`; the caller owns
  slot selection and the client must not infer it from a model string.
- `MODEL_*_FALLBACK` overrides only the matching slot; when empty, the primary
  model name is passed through unchanged so the configured Vertex alias can
  resolve it.
- Usage `model` and `requested_model` are safe model identifiers, not raw
  provider metadata.

### 4. Validation & Error Matrix

| Condition | Required behavior |
|---|---|
| Valid slot and configured override | Fallback receives that override |
| Valid slot and empty override | Fallback receives the primary model |
| Missing/invalid slot | Do not infer from model name; use the primary model fallback |
| Provider `model_version` contains control/markup characters | Usage contains the `safe_model_name()` result |
| Provider fails before visible output | Open circuit once and attempt one fallback |
| Provider fails after visible output | Do not replay; return a safe stream error |

### 5. Good / Base / Bad Cases

- **Good**: the same primary model with `lite`, `fast`, and `pro` reaches three
  distinct configured fallback overrides through `create_backend_stream()`.
- **Base**: no override is configured, so the primary model is sent to the
  Vertex alias unchanged.
- **Bad**: a test patches `_select_fallback_model()` or only checks the route
  analysis result without proving the final fallback request model.

### 6. Tests Required

- A cross-layer async regression test must call `create_backend_stream()` with
  the same primary model and each slot, mock only external Gemini clients, and
  assert both the final provider `model` argument and usage `model`.
- A stream test must set a malicious `chunk.model_version` and assert usage
  contains no newline or markup characters and equals the safe identifier.
- The default `SEARCH_FALLBACK_PROVIDER=none` test must load `config.py` with
  the environment key absent and dotenv loading disabled; behavior tests must
  separately assert no legacy `google_search()` call/injection.

### 7. Wrong vs Correct

#### Wrong

```python
usage_payload["model"] = chunk.model_version
assert analyze_result["route_slot"] == "fast"
```

#### Correct

```python
usage_payload["model"] = safe_model_name(actual_model)
async for chunk in create_backend_stream(
    messages, target_model="same-primary-model", route_slot="fast"
):
    ...
assert fallback_call.kwargs["model"] == MODEL_FAST_FALLBACK
```

## Code Review Checklist

Reviewers must check await and first iterator failure; no retry after visible
output; cancellation propagation; Redis fail-open and TTL; explicit fallback
key/base; route-slot propagation; `model_version` usage; footer escaping; old
search fallback default; and the separation between local evidence and
production canary evidence.

## Responses Streaming Contract

Normalize provider-specific Responses events at the client boundary. Consumers
must receive the existing stream chunk contract rather than inspect SDK event
types. Treat both `response.reasoning_text.delta` and
`response.reasoning_summary_text.delta` as reasoning input, but ignore empty
deltas and never reopen thinking after visible content has started.

If thinking has started, emit exactly one `thinking_end` before normal content,
terminal failure, stream exception, or task cancellation. Preserve the original
exception or `CancelledError` after cleanup; cancellation must not become a
normal error chunk. Store and retrieve response IDs only for providers/models
that use Responses state continuation; Claude requests with `store=False` must
not touch that state.

Search UI signals must be emitted once per request. Prefer structured provider
search events; when an upstream proxy exposes only a documented Grounding
source-link heuristic, keep it bounded, gated by native-search enablement, and
document that it cannot prove tool execution. Do not log the source URL,
prompt, raw event, or response body.

Responses diagnostics are also a provider boundary: record only a fixed
allowlist of event types, fixed evidence reasons, bounded counters, and boolean
state. Unknown event types become `other`; never log an arbitrary provider
field merely because it matches a safe-looking character pattern. When
normalizing nested `item`/`output`/`content`/`annotation(s)` fields, cap both
recursion depth and candidates per list so a malformed proxy response cannot
turn observability into an unbounded traversal.
