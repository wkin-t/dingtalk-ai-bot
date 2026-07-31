# Sol Final Review：Responses 搜索与思考可观测性

## 结论

最终对抗性复核未发现 P0/P1 阻塞问题。首次 Sol 的 2 个 P1 和 2 个 P2
主反例已经得到实质修复：裸/短 Grounding 伪造不再命中、Claude `low`
不再改变 GPT/Gemini 参数、空正文 delta 不再关闭 thinking、typed SDK
fixture 不再在旧 SDK 上破坏测试收集。

但当前仍有 3 个 P2 和 2 个 P3：

- Claude 已正确使用 `store=False` 且不读取旧 ID，但成功响应仍会写
  `response_id`；`final-check.md` 的“不读也不写”结论不成立。
- 正文开始后若出现迟到 reasoning，thinking 会被重新开启；terminal failure
  也会在 thinking 未闭合时直接返回。
- 实施计划中的两条 consumer-level thinking 回归仍未完成。
- `_is_claude_model()` 对当前生产模型无 false negative，但名称与实际
  provider-semantics 判定范围不完全一致。
- Grounding token 终止与“非法字符不命中”的任务表述比实际 regex 更强；
  完整合法形态链接的复述风险仍然存在。

这些问题没有推翻当前生产 trace 的主路径，但前两个 P2 建议在部署前修复，
consumer-level 测试应在本地完成后再进入生产 canary。

## 首次 Sol 反例重放

### 已修复：P1 Grounding 裸前缀/短伪造

- `native_search=True` + 裸前缀、`short`、20 字符或 31 字符 opaque path
  均不产生 `executed=True`。
- 跨 delta 的完整 source-link 仍会产生且只产生一次 `executed=True`。
- 标准 `web_search_call` / citation annotation 与 Grounding 共用 once-only
  门闩，未发现重复上报。

### 已修复：P1 共享 `low→low` 跨 provider 回归

- `EFFORT_MAPPING["low"]` 保持 `none`。
- 只有 `_is_claude_model(model_name)` 且 `supports_reasoning=True` 的
  Responses 请求覆盖为 `effort=low`。
- GPT Responses 的 low 不下发 `reasoning`；Gemini Chat Completions 的 low
  不下发 `extra_body.reasoning`。

### 已修复：P2 空正文 delta 提前关闭 thinking

序列 `reasoning("A") → output_text("") → reasoning("B") → content`
现在只产生一组 `thinking_start/end`，空正文不会截断状态。

### 已修复：P2 typed SDK 测试收集

- 运行时代码只按 `event.type` / `event.delta` 做属性归一化，不导入新 SDK
  typed event。
- 测试在函数内按能力获取
  `ResponseReasoningSummaryTextDeltaEvent`；类型缺失时只 skip 该兼容探针。
- 本机 OpenAI SDK `2.20.0` 的真实 typed event 测试通过。

## Findings

### P2-1：Claude `store=False` 仍会持久化 response ID

位置：

- `app/openai_client.py:452`
- `app/openai_client.py:629-631`
- `tests/test_openai_client.py:1285-1305`
- `research/final-check.md:13-16`

`_supports_store=False` 已阻止 Claude 读取 `previous_response_id`，但流结束后的
写入没有检查 `_supports_store`：

```python
if new_response_id and conversation_id:
    responses_state.set_response_id(conversation_id, new_response_id)
```

内存 fixture 使用无前缀 `claude-opus-4-6-thinking` 和真实形态的
`response.created(id="resp_claude")` 重放，结果为：

```text
STATE_READS 0
STATE_WRITES [call('conv-redacted', 'resp_claude')]
```

新增测试使用空事件流，因此 `new_response_id` 永远为空；
`mock_set_rid.assert_not_called()` 没有覆盖真实成功流。直接影响是每次 Claude
成功请求仍写 Redis/降级文件。若同一 `BOT_ID` 后续切换为支持 store 的 GPT，
旧 Claude ID 还可能导致首轮 `previous_response_id` 失败并触发一次恢复重试。

建议把成功写入也限定为 `_supports_store`，并用 `response.created` /
`response.completed` fixture 分别证明：

- 带前缀和无前缀 Claude 都不读、不写；
- GPT 仍会读写 response ID。

### P2-2：正文后迟到 reasoning 会重新开启 thinking

位置：

- `app/openai_client.py:567-590`
- `design.md` 的 “Reasoning normalization”

当前只有 `thinking_sent`，没有 `content_started`。序列
`reasoning("A") → content("answer") → reasoning("B")` 实际产生：

```text
thinking_start, thinking("A"), thinking_end, content("answer"),
thinking_start, thinking("B"), thinking_end
```

这违反设计中的“正文开始或流结束时发送一次 `thinking_end`”。当前生产证据
显示 reasoning item 在 message item 之前，因此尚未确认线上会出现该乱序；
但中转层重排或未来 SDK 事件变化会让钉钉卡片在正文后重新进入思考状态。

同一状态机还有一个 terminal 边界：`reasoning → response.failed` 会直接返回
error，不发送 `thinking_end`。钉钉当前会立即 finalize 错误卡，因此不会留下
跨请求状态，但 provider chunk contract 本身不平衡。

建议增加 `content_started`/terminal 收口规则：正文开始后不再重新开启 thinking，
并在 terminal failure 前闭合已开始的 thinking；为两种序列补回归测试。

### P2-3：consumer-level thinking 验证仍未完成

位置：

- `implement.md:31-32`
- `app/dingtalk_bot.py:1454-1481`
- `app/dingtalk_bot.py:1585-1589`
- `app/ai/handler.py:254-268`

现有新增测试证明 provider 产生 thinking chunks，但没有证明
`response.reasoning_summary_text.delta` 最终进入 DingTalk 的
`full_thinking`/状态摘要，也没有证明无 reasoning 事件时 consumer 不产生虚假
摘要。`implement.md` 对这两项保持未勾选是诚实的，但意味着本地 cross-layer
验收尚未完成。

建议按计划补两条 consumer 回归；不要用 production canary 代替可重复的本地
contract test。

### P3-1：Claude helper 是“模型名 + provider 语义”的宽判定

位置：

- `app/openai_client.py:50-54`

当前生产 ID：

```text
anthropic/claude-opus-4-6-thinking → True
claude-opus-4-6-thinking           → True
```

均正确，大小写和多级 provider 前缀也没有发现 false negative。但 helper 还会把
`anthropic/not-claude` 和 `vendor/claude-compatible` 判为 True。前者延续了
Anthropic provider 不支持 store 的旧语义，却也会影响 Claude-only low
reasoning 覆盖。

当前配置没有非 Claude 的 `anthropic/*` 模型，因此不是运行时缺陷。建议后续把
“Anthropic Responses 不支持 store”和“Claude 支持 low reasoning”拆成两个
命名准确的判定，或至少增加模型矩阵测试并记录宽判定是有意行为。

### P3-2：Grounding 边界与任务资产有两处表述过强

位置：

- `app/openai_client.py:41-44`
- `research/final-check.md` 的 Sol finding verification
- `implement.jsonl:1`

regex 没有右侧 token boundary。探针结果：

```text
prefix + 31 allowed + "+"  → False
prefix + 32 allowed + "+"  → True
prefix + 32 allowed + "中" → True
```

这与“固定前缀后至少 32 个受限字符”的设计一致：匹配可以在第 32 个字符结束；
但 `final-check.md` 所称“disallowed characters do not match”不能泛化到第 32
字符之后。更重要的是，用户或模型完整复述一个合法形态链接仍会点亮图标。
该 residual 已在 PRD/design 中披露，因此不重新升级为 P1，但生产验收不得把
heuristic 当成结构化工具证明。

持久 `grounding_tail` 被限制为 prefix 长度加 256 字符；单个 SDK delta 的扫描
时间仍与该 delta 长度成正比，但未发现跨 delta 状态无界增长。新增日志只输出
固定 `grounding_redirect` 枚举，不输出 URL、正文、prompt、凭据或原始响应。

此外，`implement.jsonl` 仍保留要求“添加真实条目后删除”的 `_example` 行。
Trellis validator 会忽略它，功能不受影响，但任务资产尚未完全清理。

## 验证结果

本轮只读验证使用 `.trellis/.runtime/sol-final-*` 隔离目录，没有读取、删除或
修改 `.pytest-basetemp/`。

- 首次内存反例 probe：退出码 `1`。原因是独立进程未先注入测试用
  `GEMINI_API_KEY`，模块导入在既存 Gemini client 初始化处失败；不属于本任务
  代码结果。
- 修正测试环境后的内存反例 probe：退出码 `0`。确认 Claude 状态写入、正文后
  reasoning 重开、terminal failure 未闭合，以及 regex/model matrix 结果。
- `python -m pytest -q tests/test_openai_client.py tests/test_search_icon.py
  -p no:cacheprovider --basetemp ...`：`68 passed`，退出码 `0`。
- `python -m pytest -q tests -p no:cacheprovider --basetemp ...`：
  `530 passed, 1 warning`，退出码 `0`。
- `python -m compileall -q app main.py`（pycache 隔离到 `.trellis/.runtime`）：
  退出码 `0`。
- `python .\.trellis\scripts\task.py validate
  07-30-responses-search-thinking-observability`：退出码 `0`。
- `git -c core.whitespace=cr-at-eol diff --check`：退出码 `0`。
- OpenAI SDK：`2.20.0`。
- 行尾：
  - `app/openai_client.py`：`crlf=0`，`bare_lf=755`。
  - `tests/test_openai_client.py`：`crlf=1450`，`bare_lf=0`。

项目没有正式 lint/typecheck 门禁；按仓库 CI 定义执行了 compileall 与 pytest。

## Residual heuristic risk

Grounding 正文 source-link 仍只是启发式证据。完整合法形态链接可由模型复述，
因此即使所有自动化测试通过，也不能绝对证明工具执行。当前控制只降低裸前缀、
短路径、未挂工具和跨 delta 误报；结构化 `web_search_call`/citation 仍应优先。

## Production-only gates

以下项目尚未执行，继续阻塞生产验收：

1. 提交、推送并仅重建 35002 容器；核对 server commit、健康状态和
   `RestartCount`。
2. 普通闲聊：无 Grounding、无搜索探针、无 `🌐`。
3. 实时联网：回答具备可核实时效性、出现完整 Grounding source-link、仅一次固定
   搜索探针，DingTalk footer 显示 `🌐`。
4. Claude medium/high：真实出现 reasoning summary event、thinking 字符数非零，
   思考块和最终状态摘要正确闭合。
5. Claude low：确认真实下发 low，并观察延迟与费用。
6. 核对容器内 OpenAI SDK 版本及脱敏日志；区分自动化证据、服务器日志和用户
   DingTalk UI 见证。

本轮未部署、未提交、未推送，也未修改业务代码。
