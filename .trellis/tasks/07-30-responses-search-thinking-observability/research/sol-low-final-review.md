# Sol Low Final Review：Responses 搜索与思考可观测性

## 结论

按最低思考深度 `low` 对当前完整 diff 做了最终只读复核。未发现 P0/P1。

前一轮 Sol 的三个主要 P2 已经得到实质修复：

- 带/不带 `anthropic/` 前缀的 Claude 均以 `store=False` 调用，成功流不会读取或写入 `responses_state`。
- GPT 仍以 `store=True` 调用，保留 `previous_response_id` 读取、精简续接和成功 response ID 写入。
- 正文开始后的迟到 reasoning 不会重新开启 thinking；`response.failed` 与普通流异常会先发 `thinking_end`，再保留既有 error contract。
- DingTalk consumer 测试确实运行真实 `GeminiBotHandler.handle_ai_stream()` 收尾逻辑，覆盖 thinking summary 进入 `full_thinking`/最终状态摘要，以及无 reasoning 时不生成虚假摘要。

本轮发现 1 个 P2：真实 task cancellation 会正确传播 `CancelledError`，但若取消发生在 thinking 已启动之后，不会发出 `thinking_end`。因此取消路径的 chunk 状态机仍不平衡。另有 2 个已披露 P3 残余风险，不阻塞本地代码提交，但建议修复 P2 后再进入 production canary。

## P0

无。

## P1

无。

## P2

### P2-1：取消传播正确，但 thinking 状态未闭合

位置：

- `app/openai_client.py:549-560`
- 关键分支：`app/openai_client.py:555`

当前 `_events_with_thinking_cleanup()` 对普通异常使用 sentinel，让外层先发送 `thinking_end` 再重新抛出；但 `CancelledError` 被单独直接抛出：

```python
except asyncio.CancelledError:
    raise
```

真实 task cancellation 反例序列：

```text
reasoning_summary_text.delta("sanitized-summary")
→ consumer task 在等待下一个 event 时被 cancel
```

观测结果：

```text
CANCEL_PROPAGATED=1
THINKING_END_COUNT=0
CHUNKS=[
  {"thinking_start": True},
  {"thinking": "sanitized-summary"}
]
```

取消没有被吞掉，这是正确的；但统一 chunk contract 停在已启动的 thinking。若调用方在取消场景仍保留/复用当前卡片状态，可能留下未闭合的展示状态。

建议让取消路径复用同一个 cleanup 机制：若 `thinking_sent=True`，先向外发送一次 `thinking_end`，随后重新抛出原始 `CancelledError`。必须补真实 `asyncio.Task.cancel()` 回归，证明：

- `thinking_end` 恰好一次；
- `CancelledError` 仍传播；
- 不生成普通 error chunk；
- 不写 response ID；
- 未启动 thinking 时不补虚假 `thinking_end`。

## P3

### P3-1：Claude 判定仍混合了模型身份与 provider 存储语义

`_is_claude_model()` 会把所有 `anthropic/*` 以及任意 provider 下基名以 `claude-` 开头的模型视作 Claude。当前生产模型：

```text
anthropic/claude-opus-4-6-thinking
claude-opus-4-6-thinking
```

均判断正确，未发现当前配置下的回归。但未来若出现非 Claude 的 `anthropic/*` 或仅兼容 Claude 命名的其他 provider，该 helper 会同时影响 temperature clamp、low reasoning、store 和 system message 形态。建议后续把“Anthropic/Claude 不支持 store”与“Claude 支持 low reasoning”拆成语义独立判定。

### P3-2：Grounding source-link 仍是启发式证据

当前检测具备 native-search 门控、固定 HTTPS host/path、至少 32 个受限 ASCII path 字符、有界 rolling tail 和 once-only 门闩；裸前缀、短 token、普通 URL 与阈值前非法字符均不会命中。

但模型完整复述一个合法形态的 Grounding redirect link 仍会点亮 `🌐`。PRD、design、production evidence 和 implementation plan 已诚实披露该边界，因此不升级为 P1。生产验收仍须联合观察回答时效性、固定搜索探针与 DingTalk footer，不能把 heuristic 当作结构化工具执行证明。

## 前一轮 P2 重放结果

### Claude response state

- `_supports_store = not _is_claude_model(model_name)`。
- Claude 获取旧 ID 的调用受 `_supports_store` 门控。
- 成功 response ID 写入同样受 `_supports_store` 门控。
- 带/不带 provider 前缀的成功事件 fixture 均验证 `get_response_id()` / `set_response_id()` 未调用。

结论：通过。

### GPT store 行为

- GPT 仍发送 `store=True`。
- 有旧 ID 时仍发送 `previous_response_id` 并只提交最后一条 user input。
- 成功 `response.created/completed` 后仍写入新的 response ID。
- 旧 ID 失效时仍清理状态并以全量历史重试。

结论：通过。

### Thinking 顺序与错误传播

- 空 output delta 不关闭 thinking。
- 非空正文开始后关闭一次 thinking。
- 正文后的迟到 reasoning 被忽略，不重开 thinking。
- `response.failed` 先发 `thinking_end`，再发原有 Responses error chunk。
- 普通 async iterator 异常先发 `thinking_end`，再由 `call_openai_stream()` 生成原有 `OpenAI API Error` chunk。
- task cancellation 传播，但未闭合 thinking，见 P2-1。

结论：除取消边界外通过。

### DingTalk consumer

新增测试调用真实 `GeminiBotHandler.handle_ai_stream()`，只 mock 后端 stream 和外部卡片/数据依赖：

- reasoning chunks 会累计到 `full_thinking`，最终进入 `statusText` 摘要；
- 无 reasoning chunks 时不会生成 `🧠` 摘要；
- provider event type 仍只在 `openai_client` 归一化，DingTalk 与 `AIHandler` 不解析 SDK event；
- 两个 consumer 对 search chunk 均使用 `dict.update()` 合并，未破坏统一 contract。

结论：通过。

## 模型、搜索、日志与任务资产

- Claude `low` 仅在 Responses/Claude 边界覆盖为 `effort=low`；GPT Responses 与 Gemini Chat Completions 保持原有 omitted-low 行为。
- 标准 `web_search_call` / citation 与 Grounding heuristic 共用 once-only 搜索门闩。
- 新增日志只输出固定搜索类别，不打印 Grounding URL、正文、prompt、凭据、Authorization header 或原始响应对象。
- `implement.jsonl` / `check.jsonl` 均为 5 条真实上下文记录，无 `_example` 残留。
- `implement.md` 已勾选本地实现与验证，deployment / production canary 保持未勾选；PRD 的生产 footer/canary 验收也保持未勾选，资产表述诚实。
- `research/sol-final-review.md` 保留前一轮未修复 findings，属于历史审查证据；本报告记录当前复核状态。

## 验证结果

所有测试均使用 `.trellis/.runtime/` 下独立目录，没有读取、修改或删除 `.pytest-basetemp/`。

- Targeted：
  - 命令范围：`tests/test_openai_client.py tests/test_search_icon.py tests/test_dingtalk_bot.py`
  - 结果：`88 passed`
  - 退出码：`0`
- `python -m compileall -q app main.py`
  - 退出码：`0`
- 全量 pytest：
  - 结果：`537 passed, 1 warning`
  - 退出码：`0`
  - warning：既有 Google SDK `DeprecationWarning`；pytest-asyncio 另输出未配置默认 loop scope 的 deprecation 提示。
- Trellis validate：
  - 结果：`implement.jsonl` / `check.jsonl` 各 5 条，全部通过
  - 退出码：`0`
- `git -c core.whitespace=cr-at-eol diff --check`
  - 退出码：`0`
- cancellation probe：
  - `CancelledError` 传播：是
  - `thinking_end` 数量：`0`
  - 探针进程退出码：`0`
- 行尾：
  - `app/openai_client.py`：纯 LF（bare LF `784`）
  - `tests/test_openai_client.py`：纯 CRLF（CRLF `1605`，bare LF `0`）
  - `tests/test_dingtalk_bot.py`：纯 CRLF（CRLF `190`，bare LF `0`）

## Production-only gates

以下仍未执行，不应包装为已完成：

1. 修复并回归验证 P2-1 的真实 task cancellation 边界。
2. 提交、推送后只重建 35002 容器；核对 server commit、健康状态和 `RestartCount`。
3. 普通闲聊：无 Grounding、无搜索探针、无 `🌐`。
4. 实时联网：回答具备可核实时效性、出现完整 Grounding source-link、只出现一次固定搜索探针，DingTalk footer 显示 `🌐`。
5. Claude medium/high：真实出现 reasoning summary event、thinking 字符数非零、思考块和最终摘要正确闭合。
6. Claude low：确认上游真实收到 low，并观察延迟和费用。
7. 核对容器 OpenAI SDK 版本与脱敏日志，明确区分自动化证据、服务器日志和用户 DingTalk UI 见证。

