# Sol high 对抗性规划评审（2026-07-31）

## 结论

**当前结论：不建议进入 `task.py start`。未确认 P0；存在 6 项 P1、3 项 P2、1 项 P3。**

方案的核心方向成立：在 Sub2API v0.1.168 中，Responses `type=function` 会转换为普通 Anthropic client tool；后续 Antigravity 搜索识别只检查工具 `type` 是否以 `web_search` 开头/等于 `google_search`，或 `name` 是否精确等于 `web_search`、`google_search`、`web_search_20250305`。因此 `type=function` + `name=search_current_web` 不会仅因 description 或 JSON Schema 中含有“Search the public web”而被该版本源码误判为 server-side web search。

但规划目前只靠这一静态结论避免模型切换，没有定义运行时 fail-closed 身份校验；同时，Sub2API 实际 SSE 事件与 OpenAI 官方 typed event 存在字段差异，medium/high thinking 的工具续接也没有闭环证据。以下 P1 应在启动实现前写回 `prd.md` / `design.md` / `implement.md`。

## 证据范围

- 任务材料：`prd.md`、`design.md`、`implement.md`、`research/tool-loop-design-20260731.md`。
- 父任务研究：`07-30-responses-search-thinking-observability/research/sub2api-source-search-20260731.md`。
- 当前实现：`app/openai_client.py`、`app/gemini_client.py`、`app/config.py`、`app/ai/backend.py`、`app/dingtalk_bot.py`。
- 相关测试：`tests/test_openai_client.py`、`tests/test_responses_search_normalization.py`、`tests/test_search_icon.py`、`tests/test_autonomous_search.py`、`tests/test_backend.py`、`tests/test_compose_backends.py`、`tests/test_gemini_client.py`。
- Sub2API：本地只读副本 commit `99c8e4bf7564823bafbab369acab6539e734c1bb`，与父任务记录的 v0.1.168 peeled commit 一致；重点核对 `request_transformer.go`、`responses_to_anthropic_request.go`、`anthropic_to_responses_response.go`、`types.go`。
- 客户端 wrapper：当前安装的 OpenAI Python SDK 为 2.20.0；仅做类型字段内省，未发送真实请求。

本轮没有运行测试、没有读取任何 `.env`、没有启动任务、没有触碰服务器或部署，也没有修改运行代码。

## P0

没有确认的 P0。当前问题会阻断核心验收或造成错误供应商输出，但尚无证据表明会直接导致凭据泄露、远程代码执行、不可逆数据损坏或全服务中断。

## P1

### P1-1：缺少运行时“最终响应必须是 Claude”的 fail-closed 门闩

- **结论**：静态中性命名可以避开已读 v0.1.168 源码的 web-search allowlist，但设计没有规定：若生产二进制、路由配置或后续版本仍把请求切到 Gemini，客户端必须在任何正文、thinking、usage 或 footer 发布前拒绝该响应。
- **可利用/触发条件**：服务器镜像与公开 tag 不同；Sub2API 升级后扩大识别规则；网关按 description/schema 做启发式分类；模型映射错误；续接请求意外挂回工具；响应 `model` 明确显示非 Claude。
- **影响**：Gemini 生成的正文或 thinking 可被 `app/openai_client.py` 现有通用事件循环直接作为 `content`/`thinking` yield；终态 `response.model` 会进入 usage，并由 `app/dingtalk_bot.py` 写入统计和 footer，直接违反“最终可见回复必须由 Claude 生成”和“不暴露 Gemini/Antigravity”。
- **精确位置**：
  - `design.md:44-53` 只描述 function-call loop，没有 provider identity invariant；`design.md:80` 只写“footer 的 model 仍取最终 Claude usage”。
  - `prd.md:13-19` 是硬要求，但没有对应 fail-closed acceptance case。
  - `app/openai_client.py:787-823, 856-863` 当前在确认 provider 身份前发布正文，并接受终态 `response.model`。
  - `app/dingtalk_bot.py:1430-1441, 1497-1508, 1593-1610` 消费 usage/search 并展示模型。
- **修复建议**：在设计中加入独立的 provider-identity gate：两次 Claude Responses 请求都必须绑定 expected Claude route；明确哪些 created/completed model 形态可接受、何时身份已确认。若出现明确非 Claude model，必须在发布其正文/thinking/usage 前抑制并返回固定安全错误；不得把搜索模块的 model/usage 合并进共享 chunk。若网关不提供可验证身份，必须把“原始 SSE + 最终模型 canary”设为启动前阻断门禁，而不是部署后观察项。
- **验证方法**：加入第一请求和 continuation 分别返回明确非 Claude `response.model` 的测试，断言零 content、零 thinking、零 usage、零 executed；加入缺失/别名 model 的决策测试；生产 canary 同时核对脱敏原始 SSE、最终 usage/footer 和实际网关路由记录。

### P1-2：function-call 事件归一化规则不足，`done` 事件不能作为参数真值源

- **结论**：设计列出 delta/done/output item，但没有定义 call identity、参数来源优先级和去重规则。Sub2API v0.1.168 生成的 `response.function_call_arguments.done` 不携带 `arguments`，而当前 OpenAI SDK 2.20.0 的 typed done event把 `arguments` 标为必需字段；SDK 采用宽松构造时该字段仍可能为缺失值。完整参数只稳定出现在累积 delta、`response.output_item.done.item.arguments` 或 `response.completed.response.output[*].arguments`。
- **可利用/触发条件**：只收到 done；delta 丢失或乱序；done 缺字段；output_item.done 与 completed 重复报告同一 call；SDK 对缺字段构造出半完整对象；同一 call_id 对应不同 item_id/name/arguments。
- **影响**：解析空 JSON、漏搜、执行错误 query、同一 call 被执行两次，或构造 call/output 不一致导致 continuation 被 Sub2API pairing normalizer 丢弃。
- **精确位置**：
  - `design.md:46-53` 和 `implement.md:19-22` 只有事件名称，没有归一化算法。
  - Sub2API v0.1.168 `backend/internal/pkg/apicompat/anthropic_to_responses_response.go:380-391, 415-426, 502-544`：delta 带片段，done 未写 arguments，output_item.done 带完整 item。
  - `app/openai_client.py:83-190, 757-837` 现有 helper/事件循环尚无 function-call ledger。
- **修复建议**：设计一个以 `item_id` 为主、`call_id` 为交叉校验的有界 ledger；参数来源优先级明确为完整 done item/completed item校验累积 delta，而非 function-call-arguments.done；name/call_id 在不同事件间不一致时 fail closed。为每个 call 记录 `seen/complete/executed/output_built`，仅允许一次状态跃迁，且对参数累计字节数设置上限。
- **验证方法**：使用脱敏 fixture 覆盖 delta-only、缺 arguments 的 done、output_item.done-only、completed-only、三者重复、乱序、冲突 call_id/name、截断 JSON、超长 delta；断言最多一次网络搜索和一对匹配的 function_call/function_call_output。

### P1-3：Claude medium/high thinking 的 store=False 工具续接没有协议闭环证据

- **结论**：设计承诺“相同 reasoning”续接，但 v0.1.168 的流式 Anthropic→Responses 转换会忽略 `signature_delta`，生成的 reasoning output item没有可重放的 `encrypted_content`；反向 `convertResponsesInputToAnthropic` 的 input switch 也没有处理 `type=reasoning`。因此仅追加 function_call + function_call_output 不能证明 medium/high thinking 工具轮能被 Anthropic 上游接受或保持思考连续性。
- **可利用/触发条件**：路由选择 medium/high；第一轮在 thinking 后调用工具；continuation 仍发送 `reasoning.effort=medium/high`；Anthropic 要求上一 assistant turn 的 thinking/signature 与 tool_use 一起重放。
- **影响**：第二次请求可能 400/502、丢失 thinking 上下文、重复推理，或退化为安全错误；这会使“Claude 搜索后继续回答”在复杂实时问题上最容易失败。
- **精确位置**：
  - `design.md:52` 明确要求“相同 instructions/reasoning”，但未讨论签名。
  - `app/openai_client.py:664-670, 799-805` 当前 medium/high 会发送 reasoning 并展示 summary。
  - Sub2API `anthropic_to_responses_response.go:278-290, 368-395, 403-426, 502-526`；`responses_to_anthropic_request.go:100-205`。
  - 父任务研究 `sub2api-source-search-20260731.md:68-83, 111-116` 已提示 encrypted/signature 与可见 summary 不是同一字段，但子任务设计没有收口该风险。
- **修复建议**：把 low/medium/high 分开设计。实现前先以 v0.1.168 原始 SSE 做无敏感内容 canary，确认每档工具调用和 continuation。若 medium/high 无法重放，不得静默宣称保留 reasoning；应在 PRD 中选择并记录：桥接请求固定为已验证档位、medium/high 禁用 bridge，或另行修复/更换协议路径。该选择会改变用户行为，必须先对齐。
- **验证方法**：至少对 low、medium、high 分别执行一次“thinking→function_call→tool result→continuation”契约测试；断言第二请求成功、没有 400/502、thinking_start/end 配对、最终正文来自 continuation。mock 测试不能替代该网关 canary。

### P1-4：晚到 function call、多个 call 和重复事件下的用户可见状态机未定义

- **结论**：规划只写“最多一次搜索”，没有说明第一轮已输出 thinking/正文后出现 function call怎么办，也没有说明 Claude 一次返回多个或重复 call 时如何为每个 call 配对结果。现有 DingTalk 消费端会立即显示每个 content/thinking chunk，已发布内容不可撤回。
- **可利用/触发条件**：text block 后 tool_use；thinking 后 tool_use；并行产生两个 `search_current_web`；同一 call在 done/output_item.done/completed 重复；先产生一个非法 call再产生合法 call；continuation 再次返回 function call。
- **影响**：用户看到第一轮半成品与第二轮答案拼接；thinking 状态不闭合或重复开启；执行超过一次搜索；多余 call被 Sub2API 规范化丢弃；续接缺失配对结果而失败；递归工具循环。
- **精确位置**：
  - `prd.md:14, 17-18` 给出目标但无事件序列 acceptance matrix。
  - `design.md:44-53, 82-89` 未定义状态转换和 late-content policy。
  - `implement.md:19-22, 26-30` 未列多 call/晚到 call/continuation call测试。
  - `app/openai_client.py:697-805` 当前状态只覆盖单流的 thinking/content；`app/dingtalk_bot.py:1428-1480` 立即发布。
- **修复建议**：在 design 中写出显式状态机，例如 `FIRST_STREAM → NO_TOOL_FINAL` 或 `TOOL_PENDING → SEARCHING → SECOND_STREAM → FINAL`。明确：第一轮哪些 chunk允许立即发布；发现 tool call时如何关闭 thinking；第一轮正文是否缓冲、丢弃或保留；所有 call如何成对返回（只执行第一个合法 call，其余返回固定 `limit_reached/unsupported` 输出但不联网）；continuation 的任何新 function call都作为固定 terminal contract violation，不再续接。
- **验证方法**：表驱动测试覆盖 thinking-before-call、content-before-call、两个合法 call、合法+非法 call、重复 call、第二轮再次 call、第一轮/搜索/第二轮各阶段异常和取消；断言搜索调用数≤1、continuation数≤1、每个保留 call都有唯一 output、thinking_end exactly once、用户正文策略符合设计。

### P1-5：grounding“存在”不等于有可用证据，当前图标成功判定仍可能误报

- **结论**：设计要求 `candidate.grounding_metadata` 即标记 executed，但没有定义必须存在的 evidence 字段。truthy metadata 可能只有查询、空 support、无效 URI 或与正文无映射；这不足以证明搜索结果成功且可返回来源。
- **可利用/触发条件**：metadata 对象非空但没有 grounding chunk；只有 `web_search_queries`；来源 URI 为空、非 HTTP(S)、含控制字符或超过上限；metadata 到达后搜索流随后失败；多个 candidate 中非目标 candidate含 metadata。
- **影响**：发送 `search.executed=true` 并显示 🌐，但 Claude收到 unavailable/无来源结果；或把无效 URL/供应商内部字段带入 tool output。
- **精确位置**：`prd.md:13, 16, 27-30`；`design.md:65-68, 72-80`；现有弱判定可见 `app/gemini_client.py:618-622`，不能直接复制到新模块。
- **修复建议**：定义 `SearchEvidence.success` 的唯一谓词：至少一个经归一化的公开 HTTP(S) source（无 userinfo、控制字符，长度/数量有界），并且有有界事实文本；metadata 对象本身、query 列表或 finish_reason均不能单独置 success。只有 search 流完整结束且该谓词成立后，才允许产生一次 executed chunk。失败结果仍可作为中性 tool error返回 Claude，但不点亮。
- **验证方法**：覆盖空 metadata、query-only、空 chunks、无效 scheme、超长 URL、重复来源、metadata 后流异常、多个 candidate、完整有效 evidence；断言只有最后一种产生一次 executed。

### P1-6：专用配置的 fail-closed 与部署作用域不够精确

- **结论**：设计只要求 base/key 非空，未规定 endpoint合法性、禁止 Google 官方 host、bridge 是否在缺配置时仍挂工具，以及如何从代码层保证只在 35002 Claude 路径启用。当前共享 config 中存在 `GEMINI_API_BASE_KEY = ... or GEMINI_API_KEY` 的历史复用行为，新模块若复用现有 client/config会违反硬约束。
- **可利用/触发条件**：只配 base或只配 key；base误填 Google 官方域名、带 userinfo/query/fragment、写成 `/v1`/`/v1beta` 错层级；新模块 import `app.gemini_client.client`；专用键误加到其他容器；Claude 模型在 35001 出现且 `supports_search=true`。
- **影响**：误用直连 Google key、请求发往错误 endpoint、在非目标容器启用 bridge、长期返回 unavailable，或泄露 endpoint类别到普通日志。
- **精确位置**：`prd.md:9, 12, 31`；`design.md:60-68`；`implement.md:9-13, 47-54`；现有回退行为位于 `app/config.py:38-50`、全局 Gemini client构造位于 `app/gemini_client.py:58-92`。
- **修复建议**：新模块只接受专用 base/key/model的显式参数或专用 config，不 import共享 Gemini client/key。base需做 URL 结构校验并明确拒绝 Google 官方 API host、userinfo、query、fragment；base或key缺一即 bridge disabled且不挂 tool。将“只在 Claude + dedicated config ready +目标部署能力开启”写成单一 predicate，并加入 compose/env隔离测试。日志只能输出固定 ready/unavailable类别，不能输出 base或 key。
- **验证方法**：环境隔离加载 config，逐项删除/误配三个键；patch `GEMINI_API_KEY` 为哨兵值并断言从未传给新 client；检查四个 compose文件只有 openrouter env_file具备专用键；测试非 Claude、35001等路径不会挂中性工具。

## P2

### P2-1：网页 prompt injection 与输出边界只有原则，没有可执行 contract

- **结论**：文档写了“网页内容是不可信资料”，但没有规定搜索 prompt的固定安全指令层级、tool output结构、字段 allowlist、字符/字节上限、URL清洗、控制字符处理和递归深度算法。
- **可利用/触发条件**：网页或 Gemini 摘要包含“忽略先前指令”、伪造 tool/system标签、超长 Unicode、嵌套 JSON、data/file/javascript URL、供应商自报身份或敏感回显。
- **影响**：Claude被间接 prompt injection影响；卡片内容被操控；内存/日志膨胀；内部供应商文本被复述。
- **精确位置**：`prd.md:13, 17, 19, 30`；`design.md:42, 67-68, 88-89`；`implement.md:11-13`。
- **修复建议**：在 design中定义最小 JSON tool result schema（固定 status、bounded summary、bounded sources、固定安全提示），只允许 http/https source，限制每字段和总序列化字节，剥离控制字符与未知 provider字段；搜索请求使用固定系统级“资料非指令”约束。不要把原始 SDK对象、原始网页块或 exception文本序列化给 Claude。
- **验证方法**：加入恶意网页文本、伪标签、超长多字节字符、深嵌套对象、危险 scheme、重复 URL和供应商自报字段 fixtures；断言输出有界、结构固定、日志与用户 chunk均无原始材料。

### P2-2：usage、延迟和终态错误的双请求语义未定义

- **结论**：工具轮会有两次 Claude usage和一次内部 Gemini usage。设计仅说 footer取最终 Claude usage，没有说明 tokens/latency是只取第二次还是合计，也没有规定第一轮成功、搜索成功、第二轮失败/取消时哪些元数据可发布。
- **可利用/触发条件**：第一轮只有 tool call但有 usage；第二轮 usage model缺失/异常；搜索成功后 continuation失败；第二轮流中断；first/second response.completed重复更新 model。
- **影响**：成本统计少算一轮、延迟失真、第一轮/搜索模型污染最终 model，或 executed已发出但终态被错误当成功。
- **精确位置**：`design.md:52, 80`；`implement.md:20-22, 53`；现有 usage owner在 `app/openai_client.py:697-703, 807-863`，消费端在 `app/dingtalk_bot.py:1430-1432, 1497-1508, 1593-1610`。
- **修复建议**：定义仅发布一个终态 usage chunk；model只能来自已通过身份门闩的第二次 Claude响应；Claude两轮 token应合计并用明确字段/测试锁定，Gemini usage永不进入共享 usage；latency定义为端到端。第二轮失败或取消不得发布成功 usage。
- **验证方法**：为两轮提供不同的脱敏 token/model/latency，断言只出现一个 usage、token规则明确、model为第二轮 Claude、无搜索模型；覆盖 continuation失败和取消。

### P2-3：测试与发布门禁缺少协议级负例和原始 SSE 证据

- **结论**：`implement.md` 的测试清单过于概括，且“Sol standard”与本次 high审阅要求不一致。现有 tests主要证明旧 native web_search和单流行为，不能证明新桥接。生产验证只看普通聊天/新闻/图标/模型标识，无法排除网关在 payload或路由层做了意外改写。
- **可利用/触发条件**：mock fixture与真实 v0.1.168 SSE字段不同；SDK宽松解析掩盖缺字段；payload仍含 reserved tool type/name；第二请求意外携带 tools；服务器镜像不是公开 tag；旧 google_search通过旁路被调用。
- **影响**：本地全绿但生产切回 Gemini、工具续接失败或图标误报；Trellis任务被过早启动。
- **精确位置**：`implement.md:3-7, 24-45, 47-54`；`research/tool-loop-design-20260731.md:41-51`；`tests/test_openai_client.py:451-475, 586-963, 996-1322`；`tests/test_compose_backends.py:30-39`。
- **修复建议**：把 P1-1 至 P1-6 的负例矩阵逐项写入 implement；新增脱敏原始 v0.1.168 SSE fixture；payload断言递归检查 Claude请求不存在 reserved search type/name且 continuation没有 tools；patch旧 `google_search()` 为必炸哨兵；增加真实 `Task.cancel()` 在 first stream、search await、search iteration、continuation create、continuation iteration五个阶段的测试。发布前记录镜像 digest/tag provenance、脱敏首/续请求结构、原始 SSE事件类型计数与最终 Claude标识。将 `implement.md:44` 改为 Sol high门禁并在 findings清零后再 start。
- **验证方法**：Trellis validate只能证明文档结构；还需 targeted/full pytest、compileall、diff check，以及上述真实 canary。所有命令记录真实 exit code，生产未验证项单列。

## P3

### P3-1：现有测试注释仍把 openrouter 路径描述为 Sub2API 原生 web_search，易形成错误维护信号

- **结论**：新设计将 Claude路径改为 client function bridge，但 `tests/test_compose_backends.py` 仍声明 Sub2API 会把 web_search翻译成 OpenRouter web plugin；这与父任务已确认的 Antigravity强制 Gemini行为以及新任务目标冲突。
- **可利用/触发条件**：后续维护者依据该测试说明恢复 `{"type":"web_search"}`，或把“supports_search=true”误解为 server-side native search。
- **影响**：文档/测试无法防止架构回退，review时容易误判。
- **精确位置**：`tests/test_compose_backends.py:30-33`；`research/tool-loop-design-20260731.md:34-39`。
- **修复建议**：实现阶段同步更新测试说明与断言：35002仍是 `AI_BACKEND=openai`/Claude Responses，但搜索通过 `search_current_web` client bridge；GPT native web_search只属于GPT路径。
- **验证方法**：文本断言或请求级测试分别锁定 Claude function tool与GPT native web_search，避免共用含糊的 `supports_search`叙述。

## 已确认的非 finding

1. **中性工具不会因 schema/description在 v0.1.168 被误判。** `responses_to_anthropic_request.go:557-579` 按 `type=function` 转普通 Anthropic tool；`request_transformer.go:683-704` 只按 type前缀/三个精确 name识别 web search。description与parameters不参与判定。
2. **function_call/function_call_output 的正常配对路径存在。** `responses_to_anthropic_request.go:135-165` 按 call_id转换为 tool_use/tool_result；`:198-205, 254-330` 会修复相邻配对。但 dangling/orphan项会被丢弃，因此多 call仍必须由应用完整配对。
3. **现有 UI合并边界可复用。** `app/dingtalk_bot.py:1434-1441` 合并 search chunk，`app/ai/backend.py:44-56` 只依据 executed/fallback_injected点亮；新 bridge仍需在生产者端保证 executed exactly once。
4. **旧摘要 fallback默认关闭已有测试。** `app/config.py:320-323` 默认 `none`；`tests/test_openai_client.py:22-39, 1265-1291` 覆盖默认值和行为。本任务仍应加入 bridge路径的必炸哨兵，证明没有旁路调用。

## 剩余不确定性

- 未核验生产镜像 digest是否与公开 v0.1.168 tag逐字节一致；静态源码结论不能替代生产 canary。
- 未读取生产 `.env.openrouter`，因此没有确认专用 base、key、模型 alias或它们是否确属同一 Antigravity订阅；这是有意保留的部署前门禁。
- 未取得真实 Claude tool-use SSE，故 SDK 2.20.0 对 Sub2API缺字段 done事件的实际对象形态、medium/high thinking continuation是否被上游接受，仍需脱敏 canary。
- Gemini grounding metadata在当前目标 endpoint/model alias下的具体 SDK字段形态尚未固定；成功谓词必须以真实脱敏 fixture校准。
- “最终由 Claude生成”只能通过请求/响应路由、model元数据和网关证据建立高可信验证，无法从自然语言正文风格推断。

## 启动前最小门禁

1. 将 P1-1 至 P1-6 的 contract、状态机和测试矩阵写回三个规划文档。
2. 完成 v0.1.168 脱敏原始 SSE canary，至少覆盖 low与一个 medium/high工具续接。
3. 明确 first-pass正文缓冲策略、两轮 usage聚合规则和多 call配对规则。
4. 明确 dedicated config ready predicate与 endpoint fail-closed规则，证明不引用 `GEMINI_API_KEY`/共享 Gemini client。
5. 更新 implement/check context（如规划文档或新增研究发生变化），运行 `task.py validate`；Sol high复审清除 P1后，再请求用户授权 `task.py start`。
