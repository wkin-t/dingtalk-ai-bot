# Claude 工具桥接研究记录（2026-07-31）

## 结论

继续使用 Antigravity 订阅时，当前 Sub2API 不能在同一个带 `web_search` 的请求中实现“Gemini 搜索、Claude 最终回答”。它会把请求改成 Gemini `gemini-2.5-flash`，用 Gemini `googleSearch` 并生成最终文本。

本任务选择应用层 function-call bridge：Claude 请求中性工具 `search_current_web`，应用调用 Antigravity Gemini 搜索，把结果作为 `function_call_output` 返回 Claude，再发起无工具的 Claude continuation。

## Sub2API 证据

服务器镜像为 `weishaw/sub2api:0.1.168`，公开源码对应 commit `99c8e4bf7564823bafbab369acab6539e734c1bb`。

- 搜索请求强制改 target model：
  <https://github.com/Wei-Shaw/sub2api/blob/v0.1.168/backend/internal/pkg/antigravity/request_transformer.go#L86-L100>
- 工具转换成 Gemini `googleSearch`：
  <https://github.com/Wei-Shaw/sub2api/blob/v0.1.168/backend/internal/pkg/antigravity/request_transformer.go#L706-L779>
- Grounding 在流结束时作为普通文本 block 输出：
  <https://github.com/Wei-Shaw/sub2api/blob/v0.1.168/backend/internal/pkg/antigravity/stream_transformer.go#L483-L510>
- Anthropic → Responses 只映射 allowlisted message/content/thinking/tool_use 事件：
  <https://github.com/Wei-Shaw/sub2api/blob/v0.1.168/backend/internal/pkg/apicompat/anthropic_to_responses_response.go#L192-L214>

## Responses function-call 证据

OpenAI Responses 流协议提供：

- `response.function_call_arguments.delta`；
- `response.function_call_arguments.done`；
- `function_call` output item；
- 后续 `function_call_output` input item。

官方参考：
<https://platform.openai.com/docs/api-reference/responses-streaming/response/function_call_arguments/done?api-mode=responses>

## 代码现状

- `app/openai_client.py` 当前 Claude 请求使用 `{"type":"web_search"}`，没有通用 function-call continuation。
- `app.openai_client.google_search` 是旧摘要 fallback 入口，但 `SEARCH_FALLBACK_PROVIDER` 默认 `none`；本任务不调用它。
- `app/gemini_client.py` 已有 Google Search grounding client，但 `google_search()` 返回摘要字符串，不能直接作为结构化 bridge。
- `app/ai/backend.py` 和 `app/dingtalk_bot.py` 已有 `search.executed` chunk contract，可继续复用。

## 生产验证项

1. openrouter 容器需显式配置 Antigravity Gemini `/v1beta` base/key，不能默认使用 `GEMINI_API_KEY` 直连 Google。
2. 需要用脱敏 fixture 和真实 canary 验证 Sub2API Claude 路径完整回流 function-call 参数和续接。
3. 需要确认服务器搜索模型 alias；代码默认 `gemini-2.5-flash`，不能从外层显示模型名推断实际模型。

## 风险

- Claude 可能根据来源、措辞或延迟推断搜索后端，但 schema、tool result、用户输出和普通日志不显式披露 Gemini/Antigravity。
- 网页内容是不可信资料，tool result 必须有边界，不能覆盖系统指令。
- 续接失败不能把 Gemini 搜索文本冒充最终回答，应沿用安全错误 contract。

