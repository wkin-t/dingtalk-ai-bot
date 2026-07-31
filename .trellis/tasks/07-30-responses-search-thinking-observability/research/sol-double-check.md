# Sol Double Check：Responses 搜索与思考可观测性

## 结论

当前改动的主路径结构合理，标准 Responses 搜索事件、Grounding 跨 delta
缓冲、once-only 门闩、reasoning summary 归一化、`minimal` 关闭 reasoning、
搜索状态合并、日志固定枚举以及行尾约束均未发现阻塞性实现错误。

但本轮对抗性审查确认了 2 个 P1 问题、2 个 P2 问题和 1 个 P3 流程问题。
其中 Grounding 普通正文伪造会直接破坏“图标只代表真实联网”的核心语义；
`low` 的共享映射还会改变任务范围之外的 GPT Responses 与 Gemini Chat
Completions 请求。建议修复或明确接受这些边界后，再进入提交和生产部署。

## Findings

### P1-1：Grounding 普通正文前缀可在 native search 已挂载时伪造 `executed=True`

- 位置：
  - `app/openai_client.py:525`
  - `app/openai_client.py:554-559`
  - `tests/test_openai_client.py:750-772`
  - `tests/test_openai_client.py:780-805`
- 影响：
  - `SEARCH_AUTONOMOUS=true` 时 fast/pro 通常会挂载原生搜索工具，因此
    `native_search=True` 并不代表模型实际调用了搜索。
  - 用户只要要求模型原样复述
    `https://vertexaisearch.cloud.google.com/grounding-api-redirect/`
    前缀，普通 `response.output_text.delta` 就会产生
    `{"search":{"executed":True}}`，最终 footer 错误显示 `🌐`。
  - 这违反 PRD 的“真实搜索后才显示”核心语义，以及
    `prd.md:22` 对用户可控普通正文的约束。
- 证据：
  - 当前负例只覆盖 `enable_search=False` 时出现完整 Grounding 前缀，以及
    `enable_search=True` 时出现普通 `Sources:`/普通 URL；没有覆盖
    `enable_search=True` + 完整前缀复述。
  - 本轮直接调用当前 `_stream_via_responses()`，输入单个普通
    `response.output_text.delta`，正文为固定前缀加 `echo-only`，实际得到：

    ```text
    FORGED_PREFIX_SEARCH= [{'search': {'executed': True}}]
    ```

- 建议：
  1. 最可靠方案是让 sub2api/上游返回结构化搜索执行事件，并只信任结构化事件。
  2. 若本任务必须保留正文 heuristic，应在 PRD/footer 语义中明确它只是
     “检测到 Grounding-shaped evidence”，不是可证明的工具执行；同时至少要求
     经生产证据确认的完整 source-link 结构和足够长度的 opaque token，并增加
     “native search 已挂载但模型复述前缀”的负例。该加强仍不能从根本上消除伪造。

### P1-2：共享 `EFFORT_MAPPING` 让 `low→low` 超出 Responses/Claude 范围

- 位置：
  - `app/openai_client.py:25-31`
  - `app/openai_client.py:331-333`
  - `app/openai_client.py:485-487`
  - `tests/test_openai_client.py:456-483`
  - `.trellis/tasks/07-30-responses-search-thinking-observability/design.md:70`
  - `.trellis/tasks/07-30-responses-search-thinking-observability/design.md:90`
- 影响：
  - `EFFORT_MAPPING` 同时被 Gemini Chat Completions 路径和所有 Responses
    模型使用。
  - 改为 `low→low` 后，任何 `supports_reasoning=True` 的 GPT Responses
    请求都会新增 `reasoning={"effort":"low"}`；Gemini Chat Completions
    路径也会新增 `extra_body.reasoning.effort=low`。
  - 这可能改变延迟、费用和输出行为，而设计文档称“本任务不修改 GPT 请求参数”，
    部署计划又只 canary 35002 Claude 容器。35001 GPT 或未来 Gemini 上游重建后
    会静默继承这一行为。
- 证据：
  - 新测试只使用 `anthropic/claude-opus-4-6-thinking`，没有 GPT low 或
    Chat Completions low 回归。
  - `minimal` 仍正确不下发 reasoning；问题不是 minimal，而是 low 改动的
    共享作用域。
- 建议：
  - 若需求只针对当前 Claude Responses，把 low 映射限定在 Responses/Claude
    请求构造边界，不修改 Chat Completions/GPT。
  - 若产品决策确实要求所有 provider 的 footer low 都真实下发 low，则应修改
    PRD/design，补 GPT Responses 与 Gemini Chat Completions 测试，并把 35001
    和相关 Gemini 路径纳入 canary、费用与延迟观察。

### P2-1：空 `response.output_text.delta` 会提前关闭并重新开启 thinking

- 位置：
  - `app/openai_client.py:548-553`
  - `tests/test_openai_client.py:582-605`
  - `.trellis/tasks/07-30-responses-search-thinking-observability/design.md:50`
- 影响：
  - 代码在读取和判断 `delta` 是否非空之前就发送 `thinking_end`。
  - 若事件序列是 reasoning summary → 空 output delta → 后续 reasoning
    summary → 正文，消费层会收到两组 thinking start/end，造成卡片状态闪断或
    思考块在正文前重复开启。
  - 设计要求“正文开始”才结束 thinking；空 output delta 不应算正文开始。
- 证据：
  - 现有空 delta 测试只覆盖空 reasoning delta，不覆盖空 output delta。
  - 本轮直接调用当前实现得到：

    ```text
    [
      {'thinking_start': True}, {'thinking': 'A'}, {'thinking_end': True},
      {'thinking_start': True}, {'thinking': 'B'}, {'thinking_end': True},
      {'content': 'answer'}, ...
    ]
    ```

- 建议：
  - 先读取 `delta`，仅在 `delta` 为非空字符串时关闭 thinking 并发送正文。
  - 增加空 output delta 位于两个 reasoning summary delta 之间的回归测试。

### P2-2：事件测试是 MagicMock 自证，未锁定真实 SDK 对象和生产模型形态

- 位置：
  - `tests/test_openai_client.py:222-231`
  - `tests/test_openai_client.py:510-805`
  - `requirements.txt:36`
- 影响：
  - `_response_event()` 用任意 `MagicMock` 属性构造事件，无法发现 OpenAI SDK
    类型字段、联合类型或未知事件解析方式变化。
  - `openai>=1.50.0` 没有锁定上限；当前本机是 `openai 2.20.0`，未来 rebuild
    可能安装不同版本。
  - 新 Claude 测试统一使用 `anthropic/` 前缀，未覆盖当前 Antigravity 常见的
   无前缀模型 ID；无前缀会改变 `_supports_store` 分支，虽然事件循环本身相同，
    但测试没有证明真实生产请求 kwargs 与事件对象组合。
- 证据：
  - 本机 `openai 2.20.0` 的
    `ResponseReasoningSummaryTextDeltaEvent.delta` 确为必填 `str`，当前代码与
    该版本兼容。
  - 生产 research 记录了事件类型文本，但没有将脱敏后的真实对象/dict 解析为
    typed fixture；`implement.md` 的 fixture 项仍未完成。
- 建议：
  - 至少增加一条使用 OpenAI SDK typed event 或脱敏 SSE payload 解析后的测试，
    并使用生产实际模型 ID 构造请求。
  - 在部署记录中固定并核对容器内 OpenAI SDK 版本；若项目接受可重复构建要求，
    再单独决定是否收紧依赖版本。

### P3-1：`implement.md` 状态未反映实际完成度

- 位置：
  - `.trellis/tasks/07-30-responses-search-thinking-observability/implement.md:3-46`
- 影响：
  - 大量已完成项仍是 `[ ]`，而 cross-layer tests 也同样显示未完成，后续会话
    无法仅凭任务资产区分“已实现但未勾选”和“确实缺失”。
- 建议：
  - 修复 findings 后按真实证据更新复选框；未完成的 cross-layer/production
    项保持未勾选，避免把本地测试包装成生产验收。

## 已确认通过的链路

- 标准 `response.output_item.added(web_search_call)` 和
  `response.output_text.annotation.added(url_citation/url)` 仍产生一次
  `executed=True`。
- 标准事件与 Grounding 检测共用 `search_executed_sent`，once-only 正确。
- Grounding rolling tail 上限为前缀长度减一，可覆盖任意跨 delta 分割且不会
  随回复增长。
- Grounding heuristic 受 `enable_search && supports_search` 门控；未挂载搜索
  时不会因正文前缀点亮。
- `response.reasoning_summary_text.delta` 和原有
  `response.reasoning_text.delta` 均进入统一 thinking contract。
- 空 reasoning delta 不启动 thinking；无正文直接结束时会补
  `thinking_end`。
- `dingtalk_bot` 与 `AIHandler` 都使用 `dict.update()` 合并 search chunk，
  没有覆盖早期 `requested/native_enabled` 状态。
- `should_show_search_icon()` 只认 `executed` 或 `fallback_injected`。
- 新增搜索日志只记录固定 reason 枚举或固定 `grounding_redirect` 标签，不打印
  Grounding URL、正文或响应对象。
- `minimal` 不下发 reasoning；未添加 GPT `summary=auto`。
- `app/openai_client.py` 为纯 LF；`tests/test_openai_client.py` 为纯 CRLF。
- `.pytest-basetemp/` 未删除、未修改、未纳入检查。

## 测试与静态验证

本轮 Sol 独立执行：

- targeted pytest：
  `python -m pytest -q tests/test_openai_client.py tests/test_search_icon.py`
  → `53 passed`，退出码 `0`。
- full pytest：
  `python -m pytest -q tests`
  → `515 passed, 1 warning`，退出码 `0`。
- `python -m compileall -q app main.py` → 退出码 `0`。
- Trellis validate → 退出码 `0`。
- `git -c core.whitespace=cr-at-eol diff --check` → 退出码 `0`。
- 行尾：
  - `app/openai_client.py`: `crlf=0`, `bare_lf=738`
  - `tests/test_openai_client.py`: `crlf=1215`, `bare_lf=0`

全量测试通过与 P1/P2 反例并不矛盾：这些反例当前没有对应测试。

## 验证边界

- 未向生产 sub2api、Antigravity、GPT 或 DingTalk 发送新请求。
- 没有证明 reasoning summary/text 两类事件会在真实上游同一响应内同时出现；
  当前代码会按到达顺序拼接，若内容重叠可能重复，需真实 trace 再决定是否去重。
- 没有证明未知或未来 OpenAI SDK 版本仍将事件暴露为相同属性对象。
- 没有重新审查与本任务无关的既存 prompt/system 调试日志。
- 既存 `call_openai_stream()` 异常路径仍会打印异常文本/traceback，并把 provider
  错误文本送给消费层；这不是本次 diff 新增，建议继续作为独立日志治理范围，
  不应借本任务无界扩 scope。

## Production-only gates

以下阻塞生产验收，但不阻塞修复本地 findings：

1. 35002 普通闲聊：无 Grounding、无 `🌐`。
2. 35002 实时联网：确认真实 Grounding、一次固定搜索探针、DingTalk footer
   显示 `🌐`。
3. 35002 Claude medium/high：确认真实
   `response.reasoning_summary_text.delta`、thinking 字符数大于零、卡片思考块和
   最终摘要正常闭合。
4. low canary：确认真实下发 low，并记录延迟/费用变化。
5. 若保留共享 low 映射，增加 35001 GPT 与 Gemini Chat Completions canary；
   若不保留，则确认这些路径请求参数不变。
6. 核对容器内 OpenAI SDK 版本、server commit、RestartCount，以及日志中不出现
   URL、prompt、凭据或原始响应。
7. 明确区分自动化证据、服务器日志证据与用户亲眼看到的 DingTalk 卡片 UI。

