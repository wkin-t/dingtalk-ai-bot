# sub2api 0.1.168：Responses / Anthropic 搜索与思考可观测性源码研究

> 调查日期：2026-07-31（北京时间）  
> 调查对象：官方公开仓库 [`Wei-Shaw/sub2api`](https://github.com/Wei-Shaw/sub2api)，镜像标签 `weishaw/sub2api:0.1.168` 对应 Git tag `v0.1.168`  
> tag object：`58106606685b1b59c2986e77fb799ba27ea7d75e`；peeled commit：`99c8e4bf7564823bafbab369acab6539e734c1bb`

## 结论摘要

1. **公开源码存在，且 0.1.168 可精确定位。** Docker Hub 页面把镜像链接到官方 GitHub 仓库；本次按 `v0.1.168` 检出源码并以该 tag 的永久 URL 引用。官方发布说明还明确称 0.1.168 修复了“API Key 方式接入的 Codex 客户端丢失网页搜索工具声明”。
2. **Responses 与 Anthropic Messages 之间有双向直接转换。** `AnthropicToResponses` 的注释明确说绕过 Chat Completions 中间层以保留 thinking、cache_control 和结构化 system prompt；反向也把 Responses reasoning 映射为 Anthropic thinking，把 function call 映射为 `tool_use`。
3. **web search 的请求声明能双向映射。** Anthropic `web_search_*` → Responses `{"type":"web_search"}`；Responses 的 `web_search` / `google_search` / `web_search_20250305` → Anthropic `web_search_20250305`。
4. **但 Responses→Anthropic 的搜索结果/引用转换不完整。** 非流式转换看到 `web_search_call` 时，只合成 `server_tool_use` 和一个**空数组** `web_search_tool_result`；`ResponsesContentPart` 结构只有 `type/text/image_url`，没有 annotations / `url_citation` 字段。流式事件分派不处理 `response.web_search_call.*`，`response.output_item.added` 对 `web_search_call` 也无分支。因此，按 0.1.168 公开源码，不能期待 OpenAI Responses 的搜索调用细节、真实搜索结果或 `url_citation` 被完整翻译为 Anthropic 协议。
5. **普通 client tool 的 `tool_use/tool_result` 处理较完整，但会做规范化丢弃。** 两方向分别映射 `function_call`/`function_call_output` 与 `tool_use`/`tool_result`，并保持 call ID；Responses→Anthropic 为满足相邻配对约束，会重排结果，同时丢弃无结果的 dangling call 和无对应 call 的 orphan result。
6. **thinking/reasoning 可见摘要与加密续接材料被分别处理。** Anthropic→Responses 固定请求 `reasoning.encrypted_content`；带签名的历史 thinking 变成 reasoning item，未签名 thinking 被忽略。Responses→Anthropic 把 reasoning summary 变成 thinking 文本、`encrypted_content` 变成 thinking signature；流式支持 reasoning summary/text delta 和最终 signature delta。
7. **没有发现 Gemini Grounding 元数据进入上述桥接结构的证据。** 仓库其他 Antigravity/Gemini 类型定义包含 `GroundingMetadata`，但本次重点的 `apicompat` Responses↔Anthropic DTO/转换链未定义或消费 `grounding_metadata`。因此不能据此推断 `/v1/responses` 或 `/v1/messages` 会透传 Gemini grounding。

## 版本与公开源码证据

- 官方 Docker Hub：[`weishaw/sub2api`](https://hub.docker.com/r/weishaw/sub2api/)，Overview 的 OCI source/仓库指向 `Wei-Shaw/sub2api`。
- 官方仓库：[`github.com/Wei-Shaw/sub2api`](https://github.com/Wei-Shaw/sub2api)。
- 0.1.168 tag：[`v0.1.168`](https://github.com/Wei-Shaw/sub2api/tree/v0.1.168)。发布说明中的网页搜索修复可见 [`v0.1.168 release`](https://github.com/Wei-Shaw/sub2api/releases/tag/v0.1.168)。
- 本地只读核验：`git show-ref --tags v0.1.168` 得到 tag object `5810660...`；`git rev-parse HEAD` 得到 peeled commit `99c8e4b...`。Git clone 提示该 tag 是 annotated tag 而非直接 commit，二者并不矛盾。

## 1. Responses API 转换入口

### Anthropic Messages → Responses

- [`anthropic_to_responses.go#L9-L28`](https://github.com/Wei-Shaw/sub2api/blob/v0.1.168/backend/internal/pkg/apicompat/anthropic_to_responses.go#L9-L28)：转换器明确宣称直接转换，以避免 Chat Completions 中间往返丢失 thinking/cache_control/system blocks；输出默认包含 `reasoning.encrypted_content`。
- [`anthropic_to_responses.go#L58-L69`](https://github.com/Wei-Shaw/sub2api/blob/v0.1.168/backend/internal/pkg/apicompat/anthropic_to_responses.go#L58-L69)：`thinking.type` 不决定 effort；缺省 effort 为 `medium`，`output_config.effort` 才覆盖，Responses reasoning summary 设为 `auto`。
- [`anthropic_to_responses.go#L190-L230`](https://github.com/Wei-Shaw/sub2api/blob/v0.1.168/backend/internal/pkg/apicompat/anthropic_to_responses.go#L190-L230)：用户消息中的 `tool_result` 变成 `function_call_output`；其中图片另拆成用户消息，因为该 output 字段只接收字符串。
- [`anthropic_to_responses.go#L257-L337`](https://github.com/Wei-Shaw/sub2api/blob/v0.1.168/backend/internal/pkg/apicompat/anthropic_to_responses.go#L257-L337)：assistant 的 text、`tool_use`、thinking 分别变成 assistant message、`function_call`、reasoning item，并原样保持 tool call ID。

### Responses → Anthropic Messages

- [`responses_to_anthropic_request.go#L54-L83`](https://github.com/Wei-Shaw/sub2api/blob/v0.1.168/backend/internal/pkg/apicompat/responses_to_anthropic_request.go#L54-L83)：Responses reasoning effort 映射为 Anthropic `output_config.effort`；非 low effort 同时启用 thinking，并按 effort 配预算。
- [`responses_to_anthropic_request.go#L135-L164`](https://github.com/Wei-Shaw/sub2api/blob/v0.1.168/backend/internal/pkg/apicompat/responses_to_anthropic_request.go#L135-L164)：`function_call` → assistant `tool_use`，`function_call_output` → user `tool_result`。
- [`responses_to_anthropic.go#L14-L61`](https://github.com/Wei-Shaw/sub2api/blob/v0.1.168/backend/internal/pkg/apicompat/responses_to_anthropic.go#L14-L61)：非流式响应把 reasoning → thinking、message output text → text、function call → `tool_use`。

## 2. Anthropic web search 与搜索可观测性

### 请求侧：工具声明被转换

- Anthropic → Responses：[`anthropic_to_responses.go#L442-L454`](https://github.com/Wei-Shaw/sub2api/blob/v0.1.168/backend/internal/pkg/apicompat/anthropic_to_responses.go#L442-L454) 将任何 `Type` 以 `web_search` 开头的 Anthropic server tool 转成 Responses `{"type":"web_search"}`。
- Responses → Anthropic：[`responses_to_anthropic_request.go#L560-L573`](https://github.com/Wei-Shaw/sub2api/blob/v0.1.168/backend/internal/pkg/apicompat/responses_to_anthropic_request.go#L560-L573) 将 `web_search`、`google_search`、`web_search_20250305` 统一转成 `type=web_search_20250305, name=web_search`。

这与 Anthropic 官方协议一致：web search 是 server-executed tool，响应应包含 `server_tool_use`，随后是按 `tool_use_id` 配对的 `web_search_tool_result`；应用不应自己回传普通 `tool_result`。见 [Anthropic Server tools](https://platform.claude.com/docs/en/agents-and-tools/tool-use/server-tools) 与 [Web search tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool)。

### 响应侧：调用被“占位模拟”，真实结果和 citation 未建模

- [`responses_to_anthropic.go#L62-L80`](https://github.com/Wei-Shaw/sub2api/blob/v0.1.168/backend/internal/pkg/apicompat/responses_to_anthropic.go#L62-L80)：遇到 Responses `web_search_call`，转换器合成 `server_tool_use`，但紧接着合成的 `web_search_tool_result.content` 是空数组，而不是上游真实搜索结果。
- [`types.go#L296-L301`](https://github.com/Wei-Shaw/sub2api/blob/v0.1.168/backend/internal/pkg/apicompat/types.go#L296-L301)：Responses 文本 part 只定义 `Type/Text/ImageURL`。没有 `annotations`、`url_citation`、citation title/URL/start/end index 等字段，JSON 反序列化时这些未知字段不会进入模型。
- [`types.go#L359-L385`](https://github.com/Wei-Shaw/sub2api/blob/v0.1.168/backend/internal/pkg/apicompat/types.go#L359-L385)：`ResponsesOutput` 虽识别 `web_search_call`，但仅建模 `action.type/query`，不含搜索结果或引用。
- [`responses_to_anthropic.go#L211-L249`](https://github.com/Wei-Shaw/sub2api/blob/v0.1.168/backend/internal/pkg/apicompat/responses_to_anthropic.go#L211-L249)：流式 dispatcher 只处理 created、output item、文本、函数参数、reasoning 与终止事件；没有 `response.web_search_call.*` 或 citation/annotation 事件分支。
- [`responses_to_anthropic.go#L340-L389`](https://github.com/Wei-Shaw/sub2api/blob/v0.1.168/backend/internal/pkg/apicompat/responses_to_anthropic.go#L340-L389)：`response.output_item.added` 的 item 类型分支覆盖 function/custom tool、reasoning、message，但不覆盖 `web_search_call`，故该流式 item 会落到无输出。

**可观测性含义：** 请求中有 `tools:[{"type":"web_search"}]` 只能证明“工具被声明”；0.1.168 的 Anthropic 输出桥接不能可靠证明搜索已执行，更不能据此提取真实 URL citation。若客户端直连 Responses 透传路径，仍需检查未经 DTO 重编码的原始 SSE；但一旦进入上述 `apicompat` 转换链，公开源码显示这些字段没有完整承载面。

## 3. tool_use / tool_result

- Anthropic client tool 的官方契约是：assistant 返回 `tool_use`，应用执行后在下一条 user 消息中返回同 ID 的 `tool_result`。见 [How tool use works](https://platform.claude.com/docs/en/agents-and-tools/tool-use/how-tool-use-works) 与 [Handle tool calls](https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls)。
- [`anthropic_to_responses.go#L312-L337`](https://github.com/Wei-Shaw/sub2api/blob/v0.1.168/backend/internal/pkg/apicompat/anthropic_to_responses.go#L312-L337)：`tool_use` → `function_call`，Anthropic ID 原样作为 Responses `call_id`。
- [`anthropic_to_responses.go#L365-L408`](https://github.com/Wei-Shaw/sub2api/blob/v0.1.168/backend/internal/pkg/apicompat/anthropic_to_responses.go#L365-L408)：`tool_result` 文本作为字符串 output；图片另转输入图片。
- [`responses_to_anthropic_request.go#L198-L205`](https://github.com/Wei-Shaw/sub2api/blob/v0.1.168/backend/internal/pkg/apicompat/responses_to_anthropic_request.go#L198-L205)：转换后执行两次 same-role merge，中间进行 tool pairing 修复。
- [`responses_to_anthropic_request.go#L254-L282`](https://github.com/Wei-Shaw/sub2api/blob/v0.1.168/backend/internal/pkg/apicompat/responses_to_anthropic_request.go#L254-L282)：修复要求 call/result 紧邻并交替角色；源码明确说明会丢弃 unanswered/dangling calls 和 orphan results，并把可配对结果重排到对应 call 后。

因此，正常成对工具调用可延续；但该桥接不是字节级透传，异常/不完整历史会被有意规范化，排障时不能把“上游没看到某 tool item”直接归因于上游模型。

## 4. Claude thinking / Responses reasoning

### 请求与历史续接

- [`anthropic_to_responses.go#L257-L298`](https://github.com/Wei-Shaw/sub2api/blob/v0.1.168/backend/internal/pkg/apicompat/anthropic_to_responses.go#L257-L298)：带 signature 的 thinking 作为 `type=reasoning, encrypted_content=<signature>` 重放；未签名 thinking 被忽略。代码还过滤不接受的签名形态。
- [`anthropic_to_responses.go#L26-L28`](https://github.com/Wei-Shaw/sub2api/blob/v0.1.168/backend/internal/pkg/apicompat/anthropic_to_responses.go#L26-L28)：请求显式 include `reasoning.encrypted_content`，目的是拿回可供后续轮次续接的密文。

### 响应与流式事件

- [`responses_to_anthropic.go#L27-L45`](https://github.com/Wei-Shaw/sub2api/blob/v0.1.168/backend/internal/pkg/apicompat/responses_to_anthropic.go#L27-L45)：reasoning summary 拼为 `thinking`，`encrypted_content` 放入 `signature`；即使没有可见 summary，只要有 signature 也生成 thinking block。
- [`responses_to_anthropic.go#L232-L248`](https://github.com/Wei-Shaw/sub2api/blob/v0.1.168/backend/internal/pkg/apicompat/responses_to_anthropic.go#L232-L248)：流式接收 `response.reasoning_summary_text.delta` 和 `response.reasoning_text.delta`，都映射为 thinking；summary done 时暂不关闭 block，等待 finished item 上的密文签名。
- [`responses_to_anthropic.go#L365-L383`](https://github.com/Wei-Shaw/sub2api/blob/v0.1.168/backend/internal/pkg/apicompat/responses_to_anthropic.go#L365-L383)：reasoning item added 时建立 Anthropic thinking block，并暂存 item 上的 `EncryptedContent`。
- [`responses_to_anthropic.go#L677-L706`](https://github.com/Wei-Shaw/sub2api/blob/v0.1.168/backend/internal/pkg/apicompat/responses_to_anthropic.go#L677-L706)：关闭 thinking block 前发送 `signature_delta`。
- 反方向（Anthropic 流 → Responses 流）也实现 thinking：[`anthropic_to_responses_response.go#L277-L290`](https://github.com/Wei-Shaw/sub2api/blob/v0.1.168/backend/internal/pkg/apicompat/anthropic_to_responses_response.go#L277-L290) 建 reasoning item；[`#L368-L378`](https://github.com/Wei-Shaw/sub2api/blob/v0.1.168/backend/internal/pkg/apicompat/anthropic_to_responses_response.go#L368-L378) 将 `thinking_delta` 转为 `response.reasoning_summary_text.delta`。

**边界：** 这里可见的是 reasoning summary/text 与加密续接 signature，不等同于保证暴露模型完整私有 chain-of-thought。源码本身把可见文本放在 summary 事件/字段中，把续接材料放在 encrypted/signature 字段中。

## 5. url_citation 与 Gemini Grounding 元数据

### url_citation

在 `backend/internal/pkg/apicompat` 的 0.1.168 Go 源码中定向检索 `url_citation`、`annotation`、`citation`，未发现承载 Responses 文本 annotation 的 DTO 或转换逻辑。结合 `ResponsesContentPart` 的字段定义，可得出限定结论：**上述 Responses↔Anthropic 桥接不会完整保存 OpenAI Responses 的 `url_citation` annotations。**

Anthropic 官方 web search 文档说明搜索引用始终启用，返回的 citation 自身含 URL/标题/引用文本等信息；这反衬出 sub2api 当前空结果占位和无 citation DTO 的信息缺口。见 [Anthropic Web search tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool)。

### Gemini Grounding

- 仓库中 Gemini/Antigravity 响应类型确有 `GroundingMetadata`：[`gemini_types.go#L149-L191`](https://github.com/Wei-Shaw/sub2api/blob/v0.1.168/backend/internal/pkg/antigravity/gemini_types.go#L149-L191)。
- 但 `backend/internal/pkg/apicompat` 的 Responses/Anthropic DTO 和转换器没有 `grounding_metadata` 字段或消费逻辑；`ResponsesContentPart` 也没有 citation annotations。

因此只能确认“某条 Gemini 专用路径能解析该类型”，不能确认它会被转换为 Responses `url_citation`、Anthropic citation，或进入本项目当前消费的搜索执行信号。需要路径级运行证据或继续追踪具体 Gemini handler 才能扩大结论。

## 查找范围与方法

- 精确检出官方 `v0.1.168`，核对 tag object、peeled commit 和 release note。
- 重点检索目录：
  - `backend/internal/pkg/apicompat/`：Responses、Anthropic、Chat Completions 的 DTO、双向请求/响应/SSE 转换与测试；
  - `backend/internal/service/`：Responses SSE 处理、输出重建及搜索配置相关代码；
  - `backend/internal/pkg/antigravity/`：Anthropic/Gemini thinking、web search、grounding 类型与转换；
  - 官方 README、release、Docker Hub 与 Anthropic 官方 API 文档。
- 关键词：`responses`、`web_search`、`tool_use`、`tool_result`、`url_citation`、`annotation`、`citation`、`grounding`、`thinking`、`reasoning`、`encrypted_content`、`signature_delta`。
- 本报告只陈述 0.1.168 公开源码能证明的行为；没有以 issue、博客或第三方文章作为行为依据，也没有检查服务器二进制是否与公开 tag 存在私有构建差异。

## 对当前任务的直接提示

- 若目标是让应用层判断“Claude/sub2api 真的执行过 web search”，不要把“请求挂载了 web_search tool”当作执行证据。
- 对经 Responses→Anthropic bridge 的流，0.1.168 会漏掉 `web_search_call` 流式可观测事件，且非流式只给空结果占位；客户端侧采用“无真实信号则不亮图标”是与源码缺口相容的保守策略。
- reasoning 的可见文本应监听 summary/text delta；多轮续接完整性还依赖 finished reasoning item 的 `encrypted_content`→`signature_delta`，二者不能混为同一个字段。
- 若要判断当前生产镜像是否具有不同表现，下一步必须是针对 `weishaw/sub2api:0.1.168` 的原始请求/原始 SSE canary 或镜像 digest/二进制 provenance 核验；本次按用户要求未触碰服务器、运行配置或运行时代码。
