# Antigravity Claude 搜索工具桥接

## Goal

在继续使用 Antigravity 订阅的前提下，让 Claude Responses 路径自主决定是否调用抽象联网工具；应用层用同一订阅的 Gemini 搜索，随后把结构化结果返回给 Claude，由 Claude 继续生成最终回复。用户只能收到通过 Claude 身份门闩的最终输出。

## Requirements

- 仅作用于 openrouter 容器的 `AI_BACKEND=openai` Claude Responses 路径；GPT Responses、Gemini Chat、OpenClaw 及 Gemini 熔断/Vertex fallback 不变。
- 工具使用中性名称 `search_current_web`，不得使用 `web_search`、`google_search` 或 `web_search*`，避免被 Sub2API 改写成 Gemini 最终回答。
- `enable_search` 为真且 bridge 配置 ready 时，首次 Claude 请求挂载自定义 function tool；Claude 不调用时保持一次请求和原有流式输出。
- 工具调用后，应用解析并校验 query，调用明确配置为 Antigravity Gemini `/v1beta` 的搜索客户端；不得调用旧的 `app.gemini_client.google_search()`，不得回退直连 Google key。
- Gemini 结果只作为不可信 `function_call_output` 返回 Claude，包含有界事实文本和来源 URL；不显式注入 Gemini/Antigravity、endpoint、账号或日志字段。
- 续接请求包含原始 function call 和 tool result，且不再挂搜索工具，防止循环和 Sub2API 原生 `web_search` 改写。
- Gemini 搜索文本不得产生用户可见 content chunk；最终正文必须来自通过身份门闩的 Claude 响应。
- 第一轮 Claude 在工具调用前产生的 thinking/content可以保留；发现工具调用时先闭合 thinking；Gemini 中间文本永不发布。一次用户请求最多一次联网和一次 Claude continuation；续接再次请求工具时终止，不递归。
- 只有完整搜索流正常结束、存在有界事实文本和至少一个合法公开 HTTP(S) grounding source 时才发送 `search.executed=true`；metadata 对象、query 列表或空 chunk 不足以点亮图标。
- 两次 Claude Responses 都必须在发布正文/thinking/usage 前通过显式 Claude model gate；出现明确 Gemini/non-Claude model、模型冲突或 bridge 状态不一致时 fail closed，返回固定安全错误。缺少可验证 model 的生产 canary 不得宣称“最终由 Claude 生成”。
- function-call 使用有界 ledger：以 item_id 为主、call_id 交叉校验；参数来源优先使用完整 output item/response output，不能把缺 arguments 的 done 事件当真值；冲突、截断 JSON 和重复事件不得重复联网。
- 工具参数、搜索结果长度、来源、JSON 深度、调用次数和递归深度必须有界；网页内容按不可信资料处理，不得覆盖系统指令。
- `asyncio.CancelledError` 必须传播；取消发生在 Gemini 搜索期间时不得发起 Claude 续接。
- provider 名称不得进入 footer、正文、thinking、usage model 或普通日志；日志只记录固定类别和安全计数。
- medium/high thinking 只有在脱敏真实网关 canary证明 function-call continuation可重放后才能启用；未证明前不得静默宣称支持，按能力矩阵安全关闭 bridge 或降级为无搜索 Claude 请求。
- `SEARCH_FALLBACK_PROVIDER=none` 保持默认关闭，并证明本任务不调用 `google_search()`。

## Acceptance Criteria

- [ ] Claude bridge 请求含中性 function tool，payload 不含 `web_search` 或 `google_search`；continuation 不含任何 tools。
- [ ] 通过模型 gate 的模拟 Claude function call → Gemini grounding → Claude continuation：最终正文来自第二次 Claude，Gemini 文本不作为正文 chunk；显式非-Claude响应在任何可见输出前被拒绝。
- [ ] Claude 不调用工具时只发生一次 Responses 请求，thinking/content/usage 不回归。
- [ ] function-call delta-only、缺 arguments 的 done、output-item-only、completed-only、重复/乱序/冲突事件均至多执行一次搜索，并构造唯一匹配的 call/output。
- [ ] 第一轮 thinking/content 后再出现 tool call、多 call、非法 call、第二轮再次 call、各阶段异常/取消均符合显式状态机；thinking_end、continuation 和用户正文策略有测试。
- [ ] grounding 成功谓词满足时只产生一次 `search.executed=true` 并显示 🌐；空 metadata、query-only、无效/危险 URL、流异常或无来源时不显示。
- [ ] 搜索失败、缺配置、错误 key/base、JSON 参数错误和超时均不泄露 provider 异常，不点亮图标，并允许 Claude 得到安全工具失败结果后继续作答。
- [ ] 专用 base/key/model 缺失或非法时 bridge 不挂工具、不调用网络、不使用 `GEMINI_API_KEY`；Google 官方 host、userinfo、query/fragment 等非法 base 被拒绝。
- [ ] low 至少完成真实 tool continuation canary；medium/high 若未通过协议/签名 canary不得启用，且行为和提示明确记录。
- [ ] usage 只发布一次：model 来自最终通过 gate 的 Claude；Claude 两轮 token 聚合规则和端到端 latency 已测试；Gemini usage 不进入共享 usage。
- [ ] 网页 prompt injection、控制字符、超长多字节文本、危险 URL 和深嵌套结果均被有界/清洗，不进入日志或突破 tool result 边界。
- [ ] targeted tests、脱敏 v0.1.168 SSE fixtures、compileall、全量 pytest、Trellis validate、CRLF-aware diff check 通过。
- [ ] 部署验证普通闲聊无搜索图标；实时信息请求出现一次搜索探针、🌐 和 Claude 最终模型标识；同时记录脱敏原始 SSE/网关路由证据，不能只看外层 model 字段。

## Notes

- 这是复杂跨层任务，start 前必须完成 `design.md` 与 `implement.md`，并清除 Sol high 的 P1 findings。
- “Claude 不知道 Gemini”只保证不在工具 schema、tool result、用户输出和普通日志中显式披露；模型可能根据来源或行为自行推断。
- 外层 response.model 只能作为负向安全门闩和路由证据，不能单独证明 Antigravity 内部实际执行模型；真实 canary必须单列这条证据边界。
- 本任务不实现 Anthropic 原生 Claude server-side search，不 fork/修改 Sub2API。

