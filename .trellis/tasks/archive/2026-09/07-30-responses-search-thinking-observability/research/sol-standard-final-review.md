# Sol Standard Final Review

Date: 2026-07-31

## 结论

按用户指定的 `low` 思考深度、standard 速度，对当前完整 diff 与任务资产完成最终只读对抗性复核。除本报告外未修改任何文件；未部署、未提交、未推送，也未触碰 `.pytest-basetemp/`。

- P0：无。
- P1：无。
- P2：无。
- P3：保留 2 项已披露残余风险，不阻塞本地提交，但必须通过 production canary 观察。

上一轮 Sol 发现的取消路径 P2 已修复并由真实 `asyncio.Task.cancel()` 测试重放通过。前序 Claude/GPT store 隔离、正文后迟到 reasoning、terminal failure、DingTalk consumer、搜索图标与日志安全回归均通过。

## P0

无。

## P1

无。

## P2

无。

## P3

### P3-1：Claude 模型分类仍同时承载多种 provider 语义

位置：`app/openai_client.py::_is_claude_model`

当前 helper 会把 `anthropic/*` 或模型基名以 `claude-` 开头的模型统一视作 Claude，并同时控制 temperature clamp、Responses `store=False`、system message 形态及 Claude 专属 low reasoning。

当前生产模型 `anthropic/claude-opus-4-6-thinking` 与 `claude-opus-4-6-thinking` 均匹配正确，且测试已覆盖带/不带 provider 前缀的实际配置。残余风险仅在未来引入语义不同但命名相似的 provider/model 时出现。后续若模型矩阵扩展，建议把“是否支持 Responses store”与“是否支持 Claude low reasoning”拆成独立 capability。

### P3-2：Grounding source-link 是启发式搜索证据

当前检测已具备：

- 只有实际挂载 native search 时才启用；
- 固定 HTTPS host/path；
- 至少 32 个受限 ASCII opaque path 字符；
- 有界 rolling buffer；
- 与标准 `web_search_call` / citation 共用 once-only 门闩；
- 日志只输出固定类别，不输出 URL、正文、prompt、凭据或原始响应。

模型若完整复述一个合法形态的 Grounding redirect link，理论上仍可能误点亮 `🌐`。PRD、design 和 implementation plan 已明确披露这一边界，因此不升级为 P1/P2。生产验收不能把该 heuristic 单独当作结构化工具执行证明。

## 重点重放

### 真实 asyncio.Task.cancel()

thinking 已启动：

- 测试使用真实 `asyncio.create_task()` 与 `Task.cancel()`，取消发生在流等待下一事件时。
- 先收到且只收到一次 `{"thinking_end": True}`。
- 原始 `asyncio.CancelledError` 继续向调用方传播。
- 没有普通 `error` chunk。
- Claude 路径没有读取或写入 response ID。

thinking 未启动：

- `CancelledError` 正常传播。
- 不生成虚假的 `thinking_end`。
- 不生成普通 `error` chunk。
- 不读写 response ID。

结论：取消 contract 通过。

### Claude / GPT response state

- 带与不带 `anthropic/` 前缀的 Claude 均为 `store=False`，不读取 `previous_response_id`，成功、失败和取消路径均不写 response ID。
- GPT 仍为 `store=True`，保留旧 response ID 读取、精简续接、失效后全量历史重试及成功 ID 写入。
- Claude 专属 `low -> effort=low` 没有改变 GPT Responses 或 Gemini Chat Completions 的历史参数。

结论：跨 provider 隔离通过。

### Thinking 状态机与失败路径

- 空 reasoning / output delta 不启动或提前关闭 thinking。
- 非空正文开始时恰好关闭一次 thinking。
- 正文后的迟到 reasoning 被忽略，不重新开启 thinking。
- 正常流无正文结束时仍闭合 thinking。
- `response.failed` 先发送 `thinking_end`，再发送既有 Responses error chunk。
- 普通流异常先发送 `thinking_end`，再由外层保留既有 `OpenAI API Error` contract。
- 取消路径按上一节所述闭合后传播，不转换为普通错误。

结论：通过。

### DingTalk consumer

- 测试运行真实 `GeminiBotHandler.handle_ai_stream()` 消费逻辑，仅 mock 后端流及外部卡片/存储依赖。
- reasoning summary 会进入 `full_thinking` 并形成最终 `statusText` 摘要。
- 无 reasoning chunk 时不会生成虚假 `🧠` 摘要。
- provider SDK event 仍只在 `openai_client` 归一化，DingTalk 与 `AIHandler` 不新增 provider 分支。
- search chunk 继续合并状态，不覆盖已有搜索字段。

结论：通过。

### 搜索图标与日志安全

- 标准 `web_search_call`、citation annotation 与 Grounding heuristic 均产生至多一次 `executed=True`。
- 只有工具挂载、没有真实标准信号或合法 Grounding link 时不点亮。
- 裸前缀、短 token、非法字符、普通 URL 和普通 Sources 文案不会命中。
- 新增日志不记录 Grounding URL、输出正文、prompt、Authorization、API key、用户标识或原始响应。

结论：自动化合同通过；真实 Antigravity 事件与钉钉图标仍属于 production-only gate。

## 任务资产诚实性

- `task.json` 状态仍为 `in_progress`。
- `implement.jsonl` 与 `check.jsonl` 各 5 条真实记录，无 `_example` 种子。
- PRD 与 implementation plan 只勾选已完成的本地实现和自动化验证。
- 部署、server commit、35002 容器状态、真实搜索、真实 reasoning、费用/延迟与钉钉 UI 见证均保持未勾选。
- 历史 Sol 报告保留当时 findings，后续修复记录和最终检查报告明确说明当前状态，没有把旧问题从审计轨迹中抹除。

结论：任务资产表述诚实。

## 实际验证

所有 pytest 使用 `.trellis/.runtime/` 下独立 basetemp；没有读取、修改或删除 `.pytest-basetemp/`。

- Targeted：
  - 范围：`tests/test_openai_client.py tests/test_search_icon.py tests/test_dingtalk_bot.py`
  - 结果：`90 passed`
  - 退出码：`0`
- 全量 pytest：
  - 范围：`tests`
  - 结果：`539 passed, 1 warning`
  - 退出码：`0`
  - warning：既有 Google SDK `DeprecationWarning`；pytest-asyncio 另输出未配置默认 loop scope 的提示。
- `python -m compileall -q app main.py`
  - 退出码：`0`
- `python .trellis/scripts/task.py validate 07-30-responses-search-thinking-observability`
  - 结果：`implement.jsonl` / `check.jsonl` 各 5 条，验证通过
  - 退出码：`0`
- `git -c core.whitespace=cr-at-eol diff --check`
  - 退出码：`0`
- 行尾：
  - `app/openai_client.py`：纯 LF（CRLF `0`，bare LF `788`，lone CR `0`）
  - `tests/test_openai_client.py`：纯 CRLF（CRLF `1710`，bare LF `0`，lone CR `0`）
  - `tests/test_dingtalk_bot.py`：纯 CRLF（CRLF `190`，bare LF `0`，lone CR `0`）
  - `check.jsonl` / `implement.jsonl`：各 5 行纯 CRLF，无 bare LF 或 lone CR

## 残余风险

1. Grounding link 检测不能排除模型完整复述合法链接造成的误报。
2. `_is_claude_model()` 目前把模型身份、store capability 和 reasoning capability 绑定在同一命名判断上；当前模型矩阵正确，未来扩展时需复核。
3. 本地 fixture 与 typed SDK event 只能证明客户端归一化合同，不能证明生产 sub2api/Antigravity 始终返回相同事件形态。
4. 本地 handler 测试证明消费逻辑，不等于真实钉钉卡片渲染和连续流式展示已被人类见证。

## Production-only gates

以下均未执行，不应包装为完成：

1. 提交和推送经确认的变更。
2. 部署前展示本地/远端当前值与目标值，并备份 `.env.openrouter`。
3. 服务器 fast-forward 后只重建 35002 openrouter 容器。
4. 核对 server commit、容器健康状态、RestartCount、OpenAI SDK 版本和脱敏日志。
5. 普通闲聊 canary：无 Grounding、无搜索探针、footer 无 `🌐`。
6. 实时联网 canary：回答具备可核实时效性、出现生产 Grounding/标准搜索证据、固定探针只出现一次、DingTalk footer 显示 `🌐`。
7. Claude medium/high canary：真实 reasoning summary event、thinking 字符数非零、思考块和最终摘要正确闭合。
8. Claude low canary：确认上游真实收到 low，并记录延迟和费用影响。
9. 明确区分自动化证据、服务器日志、用户钉钉 UI 见证和仍未验证边界。

## 最终判断

当前 diff 在本地自动化与代码审查范围内通过最终 Sol 对抗性复核，无 P0/P1/P2。两个 P3 均已在任务资产中诚实披露。可以进入提交确认与 35002 production canary 阶段，但在上述 production-only gates 完成前，不能宣称端到端上线验收通过。
