# Final full-scope Trellis check

## Findings

### Fixed P2: Claude no longer writes response IDs on success

The previous implementation already kept Claude on `store=False` and avoided
reading `previous_response_id`, but still persisted `response.id` from
successful `response.created` / `response.completed` events. The final P2 fix
guards the success write with the same `_supports_store` flag. Regression
coverage now proves both prefixed and unprefixed Claude neither read nor write
response state, while GPT `store=True` still reads and writes response IDs.

### Fixed P2: late reasoning and terminal errors close thinking safely

Responses reasoning events are now ignored after the first non-empty content
delta, so a late summary cannot reopen the DingTalk thinking block after body
text has started. `response.failed` and async iterator exceptions close an
active thinking block before emitting the existing error contract. Cancellation
is still re-raised and is not converted into an error chunk.

### Fixed P2: DingTalk consumer-level thinking regressions added

The provider tests are now backed by DingTalk handler tests that run the real
consumer loop: reasoning summary chunks reach final `statusText` via
`full_thinking`, and a response with no reasoning chunks does not invent a fake
thinking summary.

### Fixed P3: manifest seed row removed

`implement.jsonl` now contains only the five curated context entries. The
validator already ignored the `_example` row, but removing it keeps the task
asset clean for future agents.

### Fixed P1: unprefixed Claude used the GPT storage path

The previous Sol fix recognized both `anthropic/claude-*` and unprefixed
`claude-*` for low reasoning, but `_stream_via_responses()` still disabled
server-side storage only for the prefixed form. The production Antigravity
model shape could therefore send `store=True`, read a GPT
`previous_response_id`, and route system blocks through the GPT shape.

The final check introduced one `_is_claude_model()` owner and reused it for
temperature clamping, low reasoning, Responses storage, and message shape.
Regression coverage proves that unprefixed Claude sends `store=False`, never
sends `previous_response_id`, and the later P2 fix also prevents success-path
response ID writes.

### Fixed P2: typed SDK fixture raised the implicit minimum SDK version

The typed reasoning summary event was imported at test module collection time,
while `requirements.txt` declares a lower OpenAI SDK floor that may not expose
that class. The fixture now uses the real typed class when the installed SDK
provides it and skips only that compatibility probe on older SDKs. Runtime event
normalization remains attribute-based and provider-neutral.

## Sol finding verification

- Grounding detection requires native search, the fixed HTTPS host/path, and at
  least 32 consecutive ASCII URL-path characters. Bare prefixes, short tokens,
  ordinary URLs, Unicode or disallowed-character interruption before the
  32-character boundary, and ordinary Sources text do not match. A fully
  reproduced legitimate-shape source link remains a known heuristic residual
  risk and must be checked by production canary evidence.
- The rolling tail is capped at prefix length plus 256 characters and matching
  stops after the once-only latch is set.
- Empty output deltas do not close thinking. Non-empty summary/text reasoning
  starts thinking once before content; non-empty content, stream end, provider
  terminal failure, or stream exception closes it. Reasoning after body text is
  ignored rather than reopening thinking.
- Claude low is scoped to prefixed and unprefixed Claude Responses. GPT
  Responses and Gemini Chat Completions retain the prior omitted-low behavior.
- Standard `web_search_call` and citation annotation signals still share the
  once-only latch with Grounding evidence.
- `dingtalk_bot` and `AIHandler` merge search dictionaries with `update()`;
  `should_show_search_icon()` still requires `executed` or
  `fallback_injected`. No consumer parses provider events.
- New logs contain only fixed event categories. They do not print Grounding
  URLs, response bodies, prompts, credentials, or raw exceptions.

## Verification

- Targeted pytest
  `python -m pytest -q tests/test_openai_client.py tests/test_search_icon.py tests/test_dingtalk_bot.py -p no:cacheprovider --basetemp .trellis/.runtime/pytest-targeted`:
  `88 passed`, exit code `0`.
- `PYTHONPYCACHEPREFIX=.trellis/.runtime/pycache-check python -m compileall -q app main.py`:
  exit code `0`.
- Full pytest
  `python -m pytest -q tests -p no:cacheprovider --basetemp .trellis/.runtime/pytest-full`:
  `537 passed, 1 warning`, exit code `0`.
- Trellis validate: exit code `0`.
- `git -c core.whitespace=cr-at-eol diff --check`: exit code `0`.
- `app/openai_client.py`: pure LF.
- `tests/test_openai_client.py`: pure CRLF.
- `tests/test_dingtalk_bot.py`: pure CRLF.
- `.pytest-basetemp/` was not read, modified, removed, or included.

## Production-only gates

1. Rebuild only the 35002 container after recording the server commit and
   `.env.openrouter` backup.
2. Verify ordinary chat produces neither Grounding evidence nor the globe icon.
3. Verify a current-events request produces a real Grounding source link, one
   fixed search-execution probe, and the globe icon in DingTalk.
4. Verify medium/high Claude streams real reasoning-summary events, nonzero
   thinking content, and a correctly closed card summary.
5. Verify low is actually sent upstream and observe latency/cost impact.
6. Record container OpenAI SDK version, health, restart count, and sanitized
   logs; distinguish automated evidence from the user's DingTalk UI witness.
