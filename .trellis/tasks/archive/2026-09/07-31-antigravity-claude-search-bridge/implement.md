# Implementation Plan

## 1. Pre-start planning gates

- [ ] Review updated prd/design, backend error/quality/logging specs and Sol high report.
- [ ] Add sanitized v0.1.168 raw SSE fixtures for function-call delta/done/output-item/completed shapes.
- [ ] Verify exact Sub2API Antigravity Gemini /v1beta base, key scope and model alias without printing credentials.
- [ ] Run a redacted capability probe for low and medium/high thinking tool continuation; record whether each level is enabled or blocked.
- [ ] Keep the task in planning until the P1 contracts below are explicit and reviewed.

## 2. Configuration and search boundary

- [ ] Add explicit Antigravity Gemini search base/key/model and bounded-limit config; validate URL and reject direct Google/partial/unsafe values.
- [ ] Ensure the dedicated client never imports/reuses `GEMINI_API_KEY`, shared direct Gemini client or old `google_search()`.
- [ ] Add `app/antigravity_search.py` with structured result, grounding extraction, source normalization, success predicate, timeout and safe errors.
- [ ] Test grounding success, metadata-only/no-source, malformed result, dangerous URL, oversized result, timeout, cancellation, missing config and provider failure.

## 3. Claude identity and Responses tool loop

- [ ] Add the neutral `search_current_web` function schema only when Claude bridge readiness and approved thinking capability are true.
- [ ] Remove Claude-path `web_search` injection while preserving GPT native search.
- [ ] Implement a bounded function-call ledger keyed by item_id with call_id/name consistency and argument byte limit.
- [ ] Treat arguments.done as a signal, not sole truth; reconcile complete output item/completed output with accumulated delta and reject conflicts.
- [ ] Implement explicit FIRST_STREAM → TOOL_PENDING → SEARCHING → CONTINUATION → FINAL state machine.
- [ ] Preserve first-pass Claude thinking/content, close thinking before search, suppress all Gemini content, allow only one network search and one continuation.
- [ ] Pair every retained function call with exactly one function_call and function_call_output; return fixed results for extra/unsupported calls; fail on a second continuation call.
- [ ] Gate both Claude responses before releasing visible content/thinking/usage; reject explicit non-Claude output and define missing-identity behavior.
- [ ] Reissue continuation with full Claude input, same approved reasoning level, store=False, no tools; preserve GPT response-state logic.
- [ ] Publish one final usage chunk only after successful final Claude response; aggregate Claude passes, use final Claude model, end-to-end latency, exclude Gemini usage.
- [ ] Preserve safe errors and real cancellation at first stream, search await/iteration and continuation stages.

## 4. Cross-layer tests

- [ ] No-tool Claude path remains one Responses call and streams content.
- [ ] Full Claude tool-call → Gemini grounding → Claude continuation path.
- [ ] Bridge payload contains neither reserved web_search/google_search; continuation contains no tools.
- [ ] Non-Claude response.created/model, missing identity, and Gemini model never publish content/thinking/usage.
- [ ] Delta-only, missing-arguments done, output-item-only, completed-only, duplicate/late/ordered/conflicting calls execute at most once and pair correctly.
- [ ] Thinking/content before call, multiple calls, unsupported call, second continuation call and each state transition.
- [ ] Exactly-once search chunk/icon; empty metadata/query-only/no-source/unsafe URL/stream-error do not execute.
- [ ] Invalid args, provider failure, timeout, prompt injection, control chars, long Unicode, dangerous URLs and deep nesting are bounded/sanitized.
- [ ] Real Task.cancel() in first stream, Gemini search await/iteration, continuation create/iteration propagates and never continues.
- [ ] Low/medium/high capability fixtures/canaries are separated; unapproved levels do not mount the bridge.
- [ ] GPT native search and old SEARCH_FALLBACK_PROVIDER=none behavior do not regress; patch old `google_search` with a must-not-call sentinel.

## 5. Quality gates

```powershell
pytest -q tests/test_antigravity_search.py tests/test_openai_client.py tests/test_search_icon.py tests/test_compose_backends.py
python -m compileall -q app main.py
pytest -q tests
python .\.trellis\scripts\task.py validate 07-31-antigravity-claude-search-bridge
git diff --check
```

- [ ] Run final full-scope Trellis check across affected packages.
- [ ] Run Sol high adversarial review at normal/default speed; fix every P1/P2 and rerun targeted/full tests.
- [ ] Load trellis-update-spec guidance and update backend specs if the bridge/identity/evidence rules are reusable.
- [ ] Record every command exit code and separate local automated evidence from production canary evidence.

## 6. Production rollout

- [ ] Show redacted current/target configuration and ensure only openrouter env has dedicated search keys.
- [ ] Back up .env.openrouter before adding base/key/model; never print or commit secrets.
- [ ] Recreate only openrouter with Compose, not restart.
- [ ] Verify image/revision, container status, restart count, fixed logs and no raw prompt/URL/provider leakage.
- [ ] Verify ordinary chat has no 🌐, no-search Claude has no tool call, and current-news low search has one grounding evidence, one 🌐 and final Claude identity.
- [ ] Run only reasoning levels that passed capability canary; record medium/high as blocked if not proven.
- [ ] Confirm Gemini search text is absent from user content and successful usage model is final Claude.
- [ ] Record deployment evidence and remaining boundaries in research.

## Rollback

1. Before implementation: no runtime/config changes.
2. After tests: revert task code without production changes.
3. Before canary: remove bridge config and keep current 35002 image.
4. Runtime failure: restore env backup and recreate 35002; if needed revert pre-task commit.

