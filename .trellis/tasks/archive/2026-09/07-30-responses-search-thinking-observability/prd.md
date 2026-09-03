# 修复 Responses 搜索与思考可观测性

## Goal

让经 sub2api `/v1/responses` 调用的模型在钉钉卡片中准确呈现本次请求是否真实联网，以及上游是否实际返回了可展示的思考摘要；显示状态必须以真实回流证据为准，不得把“工具已挂载”或路由状态文案冒充为模型行为。

## Requirements

- Antigravity Responses 请求真实执行联网搜索后，钉钉 footer 显示 `🌐`。
- 仅挂载搜索工具、但没有真实搜索证据时，不显示 `🌐`。
- 保留标准 Responses 搜索事件的识别能力，并兼容 sub2api/Antigravity 当前返回的 Grounding 证据。
- Claude Responses 返回 reasoning summary 时，将其作为现有 thinking chunk contract 交给消费层。
- footer 显示 `thinking=low` 时，请求应真实下发 low reasoning；`minimal` 继续代表关闭 reasoning。
- 不生成或补写上游没有返回的思考内容。
- 探测 GPT Responses 的 reasoning summary 能力；只有经真实验证兼容后才启用额外参数，探测失败不得阻塞 Claude 修复。
- 搜索和思考信号在 `dingtalk_bot` 与 `AIHandler` 消费路径上保持一致。
- 旧 `google_search()` 摘要 fallback 保持默认关闭。

## Constraints

- 不把 `native_enabled=True` 单独作为联网成功证据。
- 不通过用户可自由控制的普通正文短语轻易触发联网图标。
- 不改变 lite/fast/pro 模型映射、Antigravity/Vertex 路由或 Gemini 熔断逻辑。
- 不实现新的 Claude→Gemini→Claude 工具循环。
- 不根据 Responses 外层 `model` 字段宣称真实执行模型。
- 不记录 prompt、完整响应、API key、Authorization header 或 provider 原始异常。
- 保持现有流式 chunk contract 和钉钉卡片布局。

## Evidence Boundary

- 标准 `web_search_call` / citation annotation 是结构化执行证据。
- 当前 Antigravity 生产链路不返回结构化搜索事件时，只能依据完整的
  Google Grounding source-link 形态作启发式判断。该判断要求 native search
  已挂载、固定 host/path，以及足够长度且字符集受限的 opaque path。
- 该 heuristic 能排除裸前缀和短伪造，但不能密码学证明工具执行，也不能抵御模型
  完整复述一个合法形态链接。生产 canary 必须同时核对固定探针、实际回答时效性与
  DingTalk footer；在上游提供结构化事件前，不得把自动化测试包装成绝对证明。

## Acceptance Criteria

- [x] 标准 `web_search_call` 或 citation annotation 仍会产生一次 `{"search":{"executed":true}}`。
- [x] Antigravity Grounding source-link 启发式证据即使跨多个 text delta，也会产生且只产生一次 `executed=true`。
- [x] 只有工具挂载、没有标准搜索事件或 Grounding 强证据时，不产生 `executed=true`。
- [x] 普通网页 URL、普通 Sources 文案或用户讨论搜索语法不会误点亮 `🌐`。
- [x] `response.reasoning_summary_text.delta` 与现有 reasoning delta 均转换为 thinking start/content/end。
- [x] 没有 reasoning 事件时，不生成 thinking 内容。
- [x] Claude Responses 的 `low` 下发 `reasoning.effort=low`；`minimal` 不下发 reasoning，GPT Responses 与 Gemini Chat 参数不变。
- [x] 若 GPT `summary=auto` 探针不兼容，代码不启用该参数，并把结果记录到 research。
- [ ] 钉钉最终 footer 在真实搜索后显示 `🌐`，未搜索时不显示。
- [x] targeted tests、`compileall`、全量 pytest、Trellis validate 和 CRLF-aware `git diff --check` 通过。
- [ ] 生产 canary 覆盖普通闲聊、实时联网、medium/high reasoning 三种请求，并记录容器状态和验证边界。

## Out of Scope

- 修复 sub2api usage 中搜索场景 `upstream_model` 可能记录转换前模型的问题。
- 改造 sub2api 或 Antigravity 服务端。
- 在卡片正文展示完整 chain-of-thought。
- 调整模型价格、路由档位或搜索费用策略。
