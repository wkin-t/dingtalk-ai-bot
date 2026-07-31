# Responses 搜索信号缺口（2026-07-31）

## 已确认事实

- OpenRouter 容器实际使用 `AI_BACKEND=openai`，目标模型为
  `claude-opus-4-6-thinking`，请求通过 sub2api `/v1/responses`。
- 生产路由日志显示 fast 档已挂载原生搜索工具；因此本次请求的
  `enable_search` 为真，但这只证明工具已下发，不证明执行证据已回流。
- 同一条“搜索一下一周的要闻”在生产长上下文请求中输入约 4098 tokens、输出
  589 tokens；服务日志没有出现 `🌐 [搜索执行]`，应用也没有产生
  `search.executed=True`。
- 隔离短上下文重放走同一个 OpenRouter 容器、同一个 `/v1/responses`、同一个
  Claude 模型和同一个 `enable_search=True`，曾收到完整 Grounding redirect
  source-link，并被现有启发式识别。
- 因此当前证据支持“上游可能真实搜索，但不同请求的 Responses 回流形态不同”；
  不能把没有图标等同于没有搜索，也不能仅凭回答内容宣称已搜索。

## 当前检测边界

`app/openai_client.py` 当前识别三类信号：

1. `response.output_item.added` 中 item type 为 `web_search_call`；
2. `response.output_text.annotation.added` 中 annotation type 为
   `url_citation` 或 `url`；
3. 原生搜索已挂载时，`response.output_text.delta` 文本出现完整的
   Antigravity Grounding redirect source-link。

现有代码没有对以下可能形态做统一归一化：字典事件、`output_item.done`、
`output_text.done`、delta/done 内嵌 annotations、最终 output 中的结构化搜索
item，以及 SDK 对象和字典混合的字段访问。

## 本轮实施边界

- 先增加只记录固定事件类型、字段路径和 allowlist reason 的安全诊断；不记录
  prompt、回复正文、URL、token、header 或原始 SDK 对象。
- 在 Responses 客户端边界集中归一化已确认或通过 canary 发现的结构化搜索信号，
  消费层继续只接收现有 `{"search": {"executed": True}}` contract。
- 仍保持一次请求最多发出一次 executed 信号，且 `native_enabled` 单独不点亮图标。
- 若 canary 证明 sub2api 完全剥离搜索元数据，记录为上游能力边界；不通过常亮
  图标掩盖证据缺失，另行评估 sub2api metadata passthrough。

## 验证顺序

1. 本地脱敏事件 fixture 与单元测试；
2. 仅重建 OpenRouter 容器；
3. 普通闲聊、短上下文实时新闻、长上下文实时新闻三条 canary；
4. 对照固定日志探针、`search.executed` chunk 和 DingTalk footer。
