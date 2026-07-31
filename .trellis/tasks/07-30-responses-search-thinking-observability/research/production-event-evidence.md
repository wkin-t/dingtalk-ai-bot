# Production event evidence

## Environment

- Date: 2026-07-30 (Asia/Shanghai)
- sub2api image: `weishaw/sub2api:0.1.168`
- OpenRouter/Claude container uses `AI_BACKEND=openai` and sub2api `/v1/responses`.
- Web Search Emulation global setting is disabled; providers list is empty.

## Search observations

An Antigravity Responses request with native `web_search` returned current repository information and Google Grounding redirect sources, while the OpenAI SDK stream exposed only normal message events. It did not expose `web_search_call` or citation annotation events. Consequently the application emitted `requested/native_enabled`, but never `executed=True`.

Observed normal-event shape:

```text
response.created
response.output_item.added (message)
response.content_part.added
response.output_text.delta
response.output_text.done
response.content_part.done
response.output_item.done
response.completed
```

生产回复中的来源表现为完整 Google Grounding redirect source-link，而非只有固定
前缀。由于 SDK 流没有结构化搜索事件，本任务只把“native search 已挂载 + 固定
host/path + 至少 32 个受限字符的 opaque path”作为启发式证据。该规则支持跨
delta、有界缓冲和 once-only 上报，并拒绝裸前缀/短伪造；但它不能密码学证明工具
执行，也不能抵御完整合法形态链接的复述。实现和日志均不保存或打印 URL/token。

因此生产门禁仍需通过实时问题 canary、固定搜索执行探针和 DingTalk footer 三者
联合观察。若上游未来恢复 `web_search_call` 或 citation annotation，应优先信任
结构化事件。

## Claude reasoning differential

Without a reasoning parameter, Claude Responses returned only a message item and output text events.

With `reasoning={"effort":"medium"}`, Claude Responses returned:

```text
response.output_item.added (reasoning)
response.reasoning_summary_text.delta
response.reasoning_summary_text.done
response.output_item.done (reasoning)
response.output_item.added (message)
response.output_text.delta
...
```

The application currently listens for `response.reasoning_text.delta`, so the actual summary is discarded.

## GPT differential

The current GPT pro model returned no reasoning item or reasoning delta when called with
`reasoning={"effort":"medium"}`. A follow-up production probe with
`reasoning={"effort":"medium","summary":"auto"}` was accepted by sub2api, but still
returned only a message item and output-text events. This task therefore does not add
`summary="auto"` to GPT requests.

## Configuration mismatch

The routing layer may report `thinking_level=low`, while the shared
`EFFORT_MAPPING` maps both `minimal` and `low` to `none`. The implementation
therefore overrides low only for Claude Responses model IDs (with or without
the `anthropic/` prefix). GPT Responses and Gemini Chat Completions retain the
previous request parameters.

## Usage-model caveat

sub2api usage fields can retain the mapped Claude model before the Antigravity request transformer mutates an internal web-search payload. Do not use the outer response model or usage `upstream_model` alone as proof of the actual execution model in search scenarios.

## Privacy boundary

The task artifacts intentionally omit API keys, Authorization headers, request IDs, user identifiers, prompts, and full model responses.
