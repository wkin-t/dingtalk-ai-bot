# Design: Responses 搜索与思考可观测性

## 1. Evidence and problem boundary

当前 Responses 客户端只把 `web_search_call` 和 `url_citation` 视为真实搜索信号。生产 Antigravity 联网响应会返回 Grounding 来源，但可能丢弃这两类标准事件，导致 `search_info` 只有 `requested/native_enabled`，最终 footer 不显示 `🌐`。

Claude medium reasoning 的真实流返回 `response.reasoning_summary_text.delta`，当前客户端只监听 `response.reasoning_text.delta`，因此 summary 被丢弃。与此同时，`EFFORT_MAPPING` 将 `low` 映射为 `none`，造成 footer 显示 low、请求却没有下发 reasoning。

## 2. Data flow

```text
Responses SSE event
  → app/openai_client.py 事件归一化
  → backend stream chunk contract
  → dingtalk_bot / AIHandler 合并状态
  → should_show_search_icon()
  → DingTalk footer
```

事件归一化层是 provider 差异的唯一 owner。消费层只处理：

```python
{"search": {"executed": True}}
{"thinking_start": True}
{"thinking": "..."}
{"thinking_end": True}
```

消费层不得解析 provider URL、SSE event type 或 Responses SDK 对象。

## 3. Search execution detection

按强度从高到低识别：

1. 标准 `response.output_item.added`，item type 为 `web_search_call`。
2. 标准 citation annotation。
3. 本次 `native_search=True` 且流式文本包含完整的 Antigravity Google Grounding
   source-link：固定 redirect host/path 后跟至少 32 个受限字符组成的 opaque path。

Grounding URL 可能跨 delta。客户端维护固定上限的 rolling buffer；检测成功后立即
发送一次 `executed=True` 并停止继续匹配。裸前缀、短 token、普通 URL 和非法字符
路径均不命中。

不以 `Web search queries:`、`Sources:`、普通 URL 或 `native_enabled` 单独判定成功，避免用户文本或模型模板误报。

由于该生产链路没有结构化搜索事件，source-link 只能作为生产形态启发式证据，
不能密码学证明工具执行。模型若完整复述一个合法形态链接仍可能误报；因此生产
验收必须把 canary 回答、固定探针与 footer 一并观察，未来上游恢复结构化事件后
应优先以结构化事件为准。

### 3.1 2026-07-31 回流缺口补充

生产长上下文请求没有产生现有 `🌐 [搜索执行]` 探针，但隔离重放曾产生
Grounding redirect 信号。该差异说明“上游执行搜索”和“客户端收到可识别证据”
必须分开记录。事件归一化 helper 应兼容 SDK 对象与字典，并只读取固定的
`type`、`item`、`annotation(s)`、`output`、`delta`、`text` 字段；不对未知响应
对象做完整序列化，避免日志泄露。

在得到生产事件形状前，不扩大到任意包含 `search` 的字符串匹配。结构化类型必须
使用 allowlist；Grounding source-link 继续使用固定 host/path、有界 rolling buffer
和 native-search gate。若上游没有任何结构化或 Grounding 证据，应用层只能报告
“证据未回流”，不能可靠地补判“已搜索”。

## 4. Reasoning normalization

以下事件均归一化为 thinking chunks：

- `response.reasoning_text.delta`
- `response.reasoning_summary_text.delta`

第一个非空 reasoning delta 且正文尚未开始时发送 `thinking_start`；正文开始或流结束时发送一次 `thinking_end`。空 delta 不启动或关闭 thinking 状态。正文开始后迟到的 reasoning delta 被忽略，避免状态机重新进入 thinking。

`response.failed` 等 provider terminal failure 在产出 error chunk 前先闭合已启动的 thinking；底层 async iterator 异常先发同一个结束标记，再重新抛给 `call_openai_stream()`，由外层继续生成既有 `OpenAI API Error: ...` contract，不吞异常也不改变错误归属。

共享 `EFFORT_MAPPING` 保持历史行为：

```text
minimal → none
low → none
medium → medium
high → high
xhigh → xhigh
```

仅在 Responses 请求构造边界，当去掉可选 `anthropic/` 前缀后的模型基名以
`claude-` 开头且 level 为 `low` 时，覆盖为 `effort=low`。GPT Responses 与
Gemini Chat Completions 保持原参数，不添加 GPT `summary=auto`。reasoning level
只控制请求参数；只有上游真实返回 summary delta 时才产生 thinking 内容。

同一 Claude 模型识别 helper 也控制 temperature clamp、Responses `store=False`
以及 system blocks 的输入形态，避免无前缀 Antigravity 模型在 low reasoning
 路径被识别为 Claude、却在存储路径被误识别为 GPT。

只有 `_supports_store=True` 的 Responses 请求才会把成功事件中的 `response.id`
写入 `responses_state`。Claude（带 `anthropic/` 前缀或无前缀）既不读取也不写入
response ID；GPT 的 `previous_response_id` 续接和成功写入保持不变。

## 5. GPT summary compatibility result

研究阶段已向当前 GPT pro 模型发送
`reasoning={"effort":"medium","summary":"auto"}`。sub2api 接受请求，但返回流仍只有
普通 message/output text，没有 reasoning item 或 summary delta。

因此本任务不修改 GPT 请求参数。Responses 事件归一化保持 provider-neutral；未来 GPT
上游开始返回标准 summary event 时可以自动复用同一消费者。

## 6. Security and false-positive controls

- Grounding 检测只在本次已启用原生搜索时运行。
- 只匹配固定 HTTPS host/path 加足够长度、受限字符集的 opaque path，不匹配裸前缀或任意 `Sources:` 文案。
- rolling buffer 有固定上限，不随输出增长。
- 新日志只记录固定 reason 枚举，不记录 URL 或正文。
- `executed=True` 每次请求最多发送一次。

## 7. Compatibility

- Gemini Chat Completions 与原生 Gemini grounding metadata 路径不变。
- OpenRouter 原生 annotations 路径不变。
- 旧搜索摘要 fallback 默认值与行为不变。
- `dingtalk_bot` 和 `AIHandler` 继续消费统一 chunk contract，不增加 provider 分支。

## 8. Rollout and rollback

部署仅重建 35002 openrouter 容器。先验证普通请求无图标，再验证实时新闻有 Grounding 和图标，最后验证 Claude medium/high reasoning summary。若 Claude low reasoning 显著增加延迟或费用，可独立移除 Responses/Claude 专属覆盖，保留共享映射、事件兼容和搜索检测。

完整回滚使用部署前提交及 `.env.openrouter` 备份重新 recreate。
