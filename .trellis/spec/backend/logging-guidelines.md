# Logging Guidelines

> How logging is done in this project.

---

## Overview

The service currently uses the project logging/`print` conventions rather than
introducing a new logging framework for one feature. New Gemini diagnostics use
fixed prefixes and safe categories so an upstream response cannot become a log
injection or credential disclosure.

## Log Levels

Informational messages describe a successful route or fallback choice. Warning
messages describe fail-open Redis behavior, stale circuit markers, or missing
fallback configuration. Error messages describe a request that could not
recover after the allowed fallback attempt.

## Structured Logging

Keep the fixed event prefix and include only bounded, safe fields such as route
slot, sanitized model identifier, and the safe error category. Do not interpolate
the exception object or use `repr(error)` in a log statement. SDK-provided
bounded enum values, such as `finish_reason`, are an exception and may be logged
verbatim for provider observability.

## What to Log

Log primary pre-output failure and whether one fallback was attempted, existing
circuit probes and fallback results, Redis fail-open operation names
(`client/ping`, `exists`, or `setex`), missing fallback base/key, stale markers,
and real search execution signals where the existing search layer supports them.

## What NOT to Log

Never log API keys, bearer tokens, authorization headers, full URLs with query
parameters, request/response objects, prompt contents, exception reprs,
tracebacks, or nested SDK response bodies. Redis stores only the fixed `open`
marker with a 600-second TTL. If a safe reason is needed, use the allowlisted
phrases from `app.error_safety`.
