# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

多平台 AI 机器人服务，支持钉钉（生产使用中）和企业微信（**Deprecated，2026-07-08 起暂不使用**，代码保留但不再维护），后端可切换 Gemini、OpenAI（含 s2a 中转站，覆盖 GPT/Claude/Gemini）、OpenRouter（原生，生产未部署）或 OpenClaw（**Deprecated，暂不使用**，代码保留但不再维护）。Python + Flask，Docker 部署。LiteLLM 已完全移除，所有 OpenAI 兼容路径走官方 `openai` SDK。

## 常用命令

```bash
# 本地开发
python main.py                    # 启动服务 (默认端口 35000)

# 测试
pytest -q tests                   # 运行全部测试
pytest -q tests/test_memory.py    # 运行单个测试文件
python -m compileall -q app main.py  # 编译检查 (CI 也用这个)

# Docker
docker-compose up -d --build      # gemini 版本
docker-compose -f docker-compose.openai.yml up -d --build    # openai 版本（含中转站路径）
docker-compose -f docker-compose.anthropic.yml up -d --build # anthropic/Claude 版本 (端口 35002，容器名 dingtalk-ai-bot-anthropic，2026-08-25 前叫 openrouter)
docker-compose -f docker-compose.openclaw.yml up -d --build  # openclaw 版本（Deprecated，暂不使用；与 openai 同占 35001）
docker-compose -f docker-compose.wecom.yml up -d --build     # 企业微信版本（Deprecated，暂不使用；与 anthropic 同占 35002）
docker logs -f dingtalk-ai-bot-gemini   # 查看日志

# 部署 (使用 skill)
/deploy [all|gemini|openai|anthropic]   # 自动 git push + SSH 部署到腾讯云
```

## 架构

```
main.py                      # 入口: Monkey patch + Flask + 多平台启动
├── app/
│   ├── config.py            # 所有配置 (环境变量 + 常量)
│   ├── __init__.py          # Flask app 初始化
│   ├── routes.py            # OpenAI 兼容 API (/v1/chat/completions)
│   ├── dingtalk_bot.py      # 钉钉 Stream 消息处理
│   ├── dingtalk_card.py     # 钉钉 AI 卡片管理 (创建/流式更新)
│   ├── gemini_client.py     # Gemini API 流式调用 (google-genai SDK, 仅 AI_BACKEND=gemini 用)
│   ├── openai_client.py     # OpenAI SDK 流式客户端 (含中转站，按 upstream 分支：Claude/GPT→Responses, Gemini→Chat)
│   ├── openrouter_client.py # OpenRouter 原生客户端（仅 AI_BACKEND=openrouter 用）
│   ├── openclaw_client.py   # ⚠️ Deprecated: OpenClaw 客户端 (HTTP SSE + WebSocket)
│   ├── openclaw_tools_client.py  # ⚠️ Deprecated: OpenClaw Tools Invoke (图片识别等)
│   ├── image_gen.py         # 生图+改图 (Gemini Imagen/Flash / OpenAI gpt-image-2)
│   ├── image_store.py       # 生图存储 → 腾讯云 COS → 预签名 URL
│   ├── reference.py         # 历史引用（智能触发）
│   ├── memory.py            # 对话历史 (Redis+MySQL → 文件降级)
│   ├── database.py          # 数据层 (Redis 缓存 + MySQL 持久化)
│   ├── sample_override.py   # Stage D: /temp /top_p /sample 手动覆盖 (sticky 24h, Redis+文件)
│   ├── ai/
│   │   ├── handler.py       # AIHandler - 统一 AI 处理层，抽象平台差异
│   │   ├── backend.py       # 后端分派 (gemini/openclaw/openai → 各客户端)
│   │   ├── router.py        # 智能路由 (关键词匹配 → 模型/thinking level)
│   │   ├── buffer.py        # 消息缓冲器 (2秒窗口合并连续消息)
│   │   ├── system_prompt.py      # Stage B: system prompt 分块 cache (稳定/半稳/变动三段)
│   │   ├── message_transform.py  # Stage A: 消息角色重塑 (他bot assistant→user, 合并连续同role)
│   │   ├── sampling_clamp.py     # Stage C: 温度/top_p provider-aware clamp (Claude≤1.0)
│   │   ├── sampling_pipeline.py  # Stage C: top_p/temperature pipeline (router→最终下发)
│   │   ├── messages_pipeline.py  # 消息预处理 pipeline 入口
│   │   └── history_format.py     # 历史记录格式化工具
│   └── wecom/                # ⚠️ Deprecated（暂不使用，服务器未部署，代码保留）
│       ├── crypto.py        # 企业微信消息加解密 (WXBizMsgCrypt)
│       ├── callback.py      # Webhook 回调路由 (Flask Blueprint)
│       ├── bot.py           # 消息处理器
│       └── message.py       # 消息发送器
└── webhook_sg/
    └── webhook_sg.py        # 独立服务: 安全组动态开门 (端口 35555)
```

### 关键设计

- **Monkey Patch**: `main.py` 顶部对 `aiohttp` 和 `requests` 打补丁，统一注入代理和重试逻辑。必须在所有其他导入之前执行。
- **四后端切换**: `AI_BACKEND` 环境变量选择 `gemini`、`openai`（含 s2a 中转站路径）、`openrouter`（原生 OpenRouter，生产未部署）或 `openclaw`（Deprecated）。切换点是 `app/ai/backend.py` 的 `create_backend_stream()`，handler/bot 层不感知具体后端。
- **openai_client 双协议分支**: `app/openai_client.py::call_openai_stream` 按 `is_gemini_upstream = "gemini" in model_name.lower()` 朴素子串匹配选 API——含 "gemini" 字样的模型名走 Chat Completions，其余走 Responses API（历史上是为了绕开旧中转站 chat completions 在多轮对话下的 400 bug，2026-09-20 在 s2a 上实测 Claude 多轮 chat completions 已不再 400，但分支保留未变）。**这是子串匹配，不是严格判断**：中转站的 Gemini 风味 alias（如曾经的 `gemini-claude-opus-4-6-thinking`）会被误判走 Chat Completions；裸模型名（如 `claude-opus-4-6-thinking`）才会正确走 Responses API。Stage B system cache_control 在 Responses 路径丢失（`instructions` 是 string 字段无法挂 ephemeral）。带图请求会按 Responses API 词汇表转换 content block（`text → input_text/output_text`，`image_url → input_image`）。
- **路由/Soul 调用不限 max_tokens**: `analyze_complexity_with_*` 和 `call_*_simple` 不下发 `max_tokens`，让上游用默认值（通常 4096+）。原因：Gemini-3.5-flash / Claude reasoning / GPT-5 等思考模型的 `max_tokens` 控制的是 **thinking + output 总额**，设小（如 300）会让思考吃光预算、输出为空，bot 拿到空 content 后 JSON 解析失败降级到默认（thinking_text 缺失等）。
- **统一 AI 层**: `app/ai/handler.py` 的 `AIHandler` 目前只被企业微信（Deprecated）使用；钉钉走自己在 `app/dingtalk_bot.py::handle_ai_stream` 里的独立实现，并不经过 `AIHandler`——两条路径共享 `app/ai/system_prompt.py::build_system_prompt_content` 等下游工具函数，但顶层调用骨架是分开的，改动一处不会自动同步到另一处。System Prompt 构建口径已在 2026-07-08 统一收敛到 `build_system_prompt_content`（此前钉钉/AIHandler 各自复刻过一份相同逻辑，属于已修复的维护风险）。**已知限制（不修复，因 WeCom Deprecated）**：`AIHandler.process_message` 对 `soul_content`/`group_info` 硬编码传 `None`，企业微信从未获得 Soul 个性注入或群名上下文，也没有 `/soul` 命令；钉钉三生产后端（gemini/openai/openrouter）已用真实日志验证 Soul 正确注入。
- **三档智能路由**: 路由分析在**卡片创建前**完成（让卡片初始就显示正确思考文字）。所有后端均使用 `MODEL_ROUTER`（默认值按后端自动选，如 Haiku/gemini-flash-lite）。路由输出：lite/fast/pro 三档 + thinking level + need_search + temperature（precise/balanced/creative → 0.1/0.7/0.9）+ need_image_gen/need_image_edit。
- **生图+改图流水线**: 路由检测 `need_image_gen` → `image_gen.generate_image()`（Gemini `GEMINI_IMAGE_MODEL` 或 OpenAI `gpt-image-2`）；检测 `need_image_edit`（用户发图+修改指令）→ `image_gen.edit_image()`（Gemini `GEMINI_IMAGE_EDIT_MODEL` 或 OpenAI images.edit，OpenRouter/OpenClaw 不支持）。图片经 `image_store.py` 上传 COS → 预签名 URL 展示。
- **Soul 自主进化**: 每次对话后 AI 自主反思并进化个性，JSON 格式输出，30 分钟冷却，changelog 存档。后台调用 `MODEL_ROUTER` 轻量模型。命令：`/soul` 查看、`/soul <text>` 设置、`/soul reset` 重置、`/soul evolve` 手动进化、`/soul log` 历史。管理员权限：`SOUL_ADMIN_IDS` 环境变量控制（空=允许所有）。
- **System Prompt Cache（Stage B）**: `app/ai/system_prompt.py` 将 system prompt 拆成稳定段/半稳定段/变动段三块，分别打 `cache_control`。OpenRouter 原生后端传 list-of-blocks（已有测试证明 `cache_control` 和 `OPENROUTER_PROVIDER_ORDER` 都能原样到达 SDK 调用，而不只是文档描述）；Gemini 后端合并回字符串，Gemini SDK 本身没有 `cache_control` 概念，唯一能吃到的是 Google 自己的隐式缓存。**openai_client 的 Responses API 路径（GPT+Claude 共用）按 `_supports_store` 分流，不按模型名分流**：GPT（`store=True`，会用 `previous_response_id` 精简续接）继续走 `instructions` string 字段——精简路径只精简 `input` 里的历史消息，`instructions` 每轮无条件重发，所以不会丢内容，但 `cache_control` 在这条路径上确实丢失，是刻意选择；Claude（`store=False`，每轮全量重发 `input`）把 system blocks 转成一条 `role="system"` 的 `input` 消息插到最前面，保留 `cache_control`，让每轮重发有机会命中 sub2api 转译层的缓存。**原生 OpenRouter 必须设 `OPENROUTER_PROVIDER_ORDER=Anthropic`**，否则 Bedrock 静默忽略 `cache_control`。**实测提醒**：`app/ai/system_prompt.py` 当前 stable+semi-stable 段合计约 428 token（`tiktoken` o200k_base 实测），低于 OpenAI/Anthropic 都要求的 1024 token 最低缓存前缀——`💾 [Cache]` 日志探针（三个 client 流式结束时打印，格式 `cached=X/Y (Z%)`）目前预期显示 `cached=0`，这是符合预期的已知结果，不是 bug；等 Soul 进化出更长人设或群信息变多后自然会反映出变化，不需要为凑门槛人为加长 prompt。
- **消息角色重塑（Stage A）**: `app/ai/message_transform.py` 在发给 AI 前把非本 bot 发出的 assistant 消息转为 user 角色，并合并连续同 role 消息，避免多 bot 群聊时上下文混乱。Soul/image_gen 调用直接用 raw messages 绕过重塑。**跨 bot 历史标签用 XML 而非方括号**：`app/ai/history_format.py::format_history_with_meta` 给别的 bot 的历史消息包 `<other_bot name="X">内容</other_bot>`（而非旧版 `[来自机器人 X]` 方括号前缀）。原因：GPT/Gemini 能正确分清"这不是我说的"，但 Claude 经常混淆——根因是 OpenAI Responses API（GPT/Claude 现在共用同一套消息构造代码）移除了 Chat Completions 时代专为多发言人设计的 `message.name` 字段，只能靠文本约定表达"这是谁说的"；Anthropic 官方文档与 OpenAI 自己 Codex CLI 的 subagent 上下文注入机制（`role=user` + XML 标签，不分模型统一处理）都印证 XML 标签比方括号前缀更可靠。全后端统一使用同一格式，不按目标模型分叉。幂等判断按 bot 专属前缀 `<other_bot name="{bot_source}">` 匹配，不能用泛化的 `<other_bot` 前缀（否则一旦某条历史恰好在讨论/引用别的 bot 的标签语法，会被误判成"已包裹"而漏加真正的归属标签）。
- **采样可控化（Stage C/D）**: `/temp`、`/top_p`、`/sample` 命令写入 Redis（TTL 24h），`app/ai/sampling_pipeline.py` 在每次请求时读取并覆盖路由给出的默认值；`sampling_clamp.py` 按 provider 做边界 clamp（Claude 温度≤1.0）。卡片底部常显当前 top_p 值，手动设置时加 ⚙️ 标记。
- **消息缓冲**: 2 秒窗口合并用户连续消息，避免重复触发 AI 请求。
- **会话隔离**: 钉钉 `dingtalk_{conversation_id}`，企业微信 `wecom_{user_id}`；群聊共享上下文，单聊独立。Soul 文件命名 `{BOT_ID}__{cid}.md`（双下划线），防止多容器共享 soul（群聊 cid 跨容器相同）。
- **数据降级**: Redis+MySQL 优先，不可用时自动降级到本地文件 (`data/history/`)。
- **OpenClaw 多 Agent 路由（⚠️ Deprecated，不再维护）**: `OPENCLAW_GROUP_AGENT_MAPPING` 按 conversationId 映射到不同 agent，严格模式 (`OPENCLAW_STRICT_ROUTING=true`) 下未映射的群拒绝访问。

## 配置

环境变量通过 `.env` 文件加载，根据部署类型选择不同文件：
- `.env` → Gemini 后端（直连 google-genai 或走 s2a 中转）
- `.env.openai` → OpenAI/GPT 后端（走 s2a 中转，Responses API）
- `.env.anthropic` → Claude 后端（`AI_BACKEND=openai`，走 s2a 中转，容器/文件 2026-08-25 前叫 `openrouter`，模型名裸写不带前缀，如 `claude-sonnet-4-6`）
- `.env.openclaw` → OpenClaw 后端（Deprecated）
- `.env.wecom` → 企业微信+钉钉双平台（Deprecated）
- `.env.openrouter.example` → 原生 OpenRouter 后端（`AI_BACKEND=openrouter`）模板，与 anthropic 容器无关，生产未部署

核心变量: `AI_BACKEND`（gemini/openclaw/openai/openrouter）, `BOT_ID`（**多容器场景必须显式设独立值**，否则默认派生自 AI_BACKEND，多容器共用 AI_BACKEND 时会撞键导致 Stage A 角色重塑失效）, `PLATFORM`（dingtalk/wecom/both）, `GEMINI_API_KEY`, `DINGTALK_CLIENT_ID/SECRET`, `SOCKS_PROXY`, `OPENCLAW_HTTP_URL`, `OPENCLAW_GATEWAY_TOKEN`, `FLASK_PORT`（默认 35000）, `CHAT_COMPLETIONS_BEARER_TOKEN`（`/v1/chat/completions` 鉴权，**fail-closed 必配**——该端点用服务端 GEMINI_API_KEY 代付转发，未配置时拒绝服务而非裸奔）。

**改 .env 注意**：`docker restart` **不重读** env_file，必须 `docker compose -f xxx.yml up -d` 才会 recreate 容器并应用新 env。recreate 会丢失之前 `docker cp` 热推到运行容器内的代码，需要重推。

**统一模型变量**（2026-05-20 起，所有后端共用，旧变量 `GEMINI_MODEL`/`OPENAI_MODEL_FLASH`/`OPENROUTER_MODEL_*` 不再读取）:
- `MODEL_ROUTER` — 路由分析 / Soul 进化 / 搜索（轻量模型）
- `MODEL_LITE` — lite 档（简单问候）
- `MODEL_FAST` — fast 档（日常问答）
- `MODEL_PRO` — pro 档（复杂推理）
- 代码内置默认值按 `AI_BACKEND` 自动选（`config.py::_BACKEND_MODEL_DEFAULTS`：gemini=gemini-3 flash/3.1 pro preview，openai=deepseek-chat/deepseek-reasoner，openrouter=anthropic/claude haiku/sonnet/opus 4-5），已明显滞后于生产；生产容器均在 `.env*` 显式设置四个变量，不依赖默认值
- **s2a 中转站（2026-09-20 起，`https://s2a.ifitnesslog.cn`，sub2api）**：三个容器的 `OPENAI_API_BASE` 都是 `https://s2a.ifitnesslog.cn/v1`，Gemini 容器的 `GEMINI_API_BASE` 是 `https://s2a.ifitnesslog.cn/antigravity`。必须 `https`（`http` 会 302，POST 被重定向后失败）。GPT/Gemini/Claude 全部裸写模型名，不带任何 provider 前缀（`gpt-5.6-terra`、`gemini-3.8-flash-high`、`claude-sonnet-4-6`）。**key 按上游分组绑定，不通用**：GPT key 的 `/v1/models` 只列 GPT，Claude/Gemini 那把才有 Claude+Gemini，换错 key 会看到"模型不存在"而不是鉴权错误。**s2a 中转层会注入隐藏内容**：极小请求（"回复 OK"）也计约 400 input tokens（Claude≈404，GPT≈4380 其中约 3840 命中缓存）；Claude 曾在 5 次里有 1 次不遵守 `system` 角色指令（重测 4/4 遵守），根因未查。**这个中转站是自建服务，账号池/模型别名会被主人持续调整，不能假设今天验证过的模型名明天还有效**——改模型名前用 `curl {base}/v1/models`（带对应分组的 key）拉最新列表核对，改完端到端调用验证一次，不要只看容器有没有启动。

**钉钉卡片流式更新节流**: `STREAM_UPDATE_THROTTLE`（默认 **1.5s**，下限 0.5s）控制 bot 层向钉钉下发更新的最小间隔。这是权衡值：太快会让 thinkingText 副标题被首次 msgContent 更新瞬间盖掉，太慢失去流式体感。`dingtalk_card.stream_update` 自身另有 150ms 安全网防 burst。

OpenRouter 专属变量: `OPENROUTER_API_KEY`，`OPENROUTER_PROVIDER_ORDER=Anthropic`（**必填**，否则 cache blocks 被 Bedrock 静默忽略），`OPENROUTER_PROVIDER_SORT=price`，`OPENROUTER_FALLBACK_LITE/FAST/PRO`（按需设，fallback 路径不走 cache）。

图片模型: `GEMINI_IMAGE_MODEL`（生图，默认 `imagen-4.0-generate-001`），`GEMINI_IMAGE_EDIT_MODEL`（改图，默认 `gemini-2.0-flash-exp`），`OPENAI_IMAGE_MODEL`（默认 `gpt-image-2`）。

**原生联网搜索**（2026-07-02 起三路径全部原生；默认**全自主**——`SEARCH_AUTONOMOUS=true` 时 fast/pro 档始终挂原生搜索工具由模型自决是否搜索，lite 档与无原生搜索的路径保持路由 need_search 门控，策略见 `app/ai/backend.py::resolve_enable_search`）:
- Gemini 路径: `AI_BACKEND=gemini` + `GEMINI_API_BASE=https://s2a.ifitnesslog.cn/antigravity`（sub2api 的 antigravity 通道 `/v1beta` 原生协议层，SDK 自动拼 `/v1beta`；**必须带 `/antigravity` 前缀**，不带会 400 `API key group platform is not gemini`，`/v1` 拼法是 404）+ `GEMINI_API_BASE_KEY`。2026-09-20 实测：`google_search` 工具透传，`groundingMetadata` 回流（`web_search_queries` + `grounding_chunks`，但 chunk 只有 vertexaisearch 跳转 uri、`web.domain` 为空）；流式**每个 chunk 都带 `usage_metadata`**；`thinking_level=minimal` 仍 400（需改 low）；`gemini-3.8-flash-high` 与 `gemini-3.6-flash-low` 会真实路由到不同模型（响应 `model_version` 可核对）。**`GEMINI_API_KEY` 保持为 Google 直连 key 不要动**——生图的 `generate_images(:predict)` 端点中转站不覆盖，`image_gen` 走 `gemini_client.direct_client` 始终直连。
- GPT 路径（openai 容器）: Responses API 下发 `tools=[{"type":"web_search"}]`，s2a 真实执行，流式有完整的 `response.web_search_call.*` 三阶段事件 + `url_citation` annotation。需 `OPENAI_FLASH/PRO_SUPPORTS_SEARCH=true`。**⚠️ s2a 上 `store=True` 后带 `previous_response_id` 续接会 400**（`previous_response_id requires an OpenAI API-key account for HTTP requests`）——`openai_client` 靠报错文案含 `previous_response_id` 降级为清状态 + 全量历史重发，功能正常，代价是每轮多一次立即失败的请求（约 0.1s）。`gpt-5.6-luna/terra/sol` 2026-09-20 各连测 3 次全部成功（旧站上 luna 曾稳定 503）。
- Claude 路径（anthropic 容器，2026-08-25 前叫 openrouter 容器）: 保持 `AI_BACKEND=openai` 走 s2a。2026-09-20 实测 **Claude 在 s2a 上能联网搜索**（`{"type":"web_search"}` 与 Anthropic 原生 `web_search_20250305` 两种工具格式、sonnet-4-6 与 opus-4-6-thinking 都行；不带 tools 时模型答"没有联网能力"，说明是工具声明触发的）。**但搜索在中转站服务端完成，结果以纯文本拼在输出末尾**（"Web search queries: … Sources: [n](vertexaisearch 跳转链接)"），**没有** `response.web_search_call.*` 事件也没有 `url_citation` annotation，标准信号拿不到；🌐 图标能否亮取决于 `openai_client` 里对正文 Grounding 链接的启发式判断，尚未端到端验证。该路径响应 id 前缀是 `req_vrtx_...`（Google vertex 网关转发的 Claude，不是 Anthropic 原生协议）。`store=True`、`previous_response_id`、多轮 chat completions 在 s2a 上都不再报错，但生产仍按 `_supports_store` 对 Claude 用 `store=False`。**不要**切 `AI_BACKEND=openrouter`——服务器无 `OPENROUTER_API_KEY`，切了直接全挂。
- 模型不支持原生搜索时，默认不再调用旧摘要注入路径：`SEARCH_FALLBACK_PROVIDER=none`；如需临时兼容回滚，才显式设为 `gemini`，由 `gemini_client.google_search()` 搜索后注入 system 消息。
- **🌐 图标只在真实搜索时点亮**（`app/ai/backend.py::should_show_search_icon`）：全自主下"挂了工具"≠"搜了"，图标仅在真实搜索信号回流时亮——Gemini 看 `grounding_metadata`、Responses 看 `web_search_call` item / `url_citation` annotation、OpenRouter 看 `delta.annotations`。客户端检测到即补发 `{"search": {"executed": True}}`，消费端（dingtalk_bot/handler）**合并**而非覆盖 search chunk。`🌐 [搜索执行]` 日志是探针，可 grep 确认中转站到底透不透。

Feature flags（默认全 `true`，独立可回滚）:
- `ENABLE_CACHE_BLOCKS` — Stage B system prompt 分块 cache
- `ENABLE_TOP_P_PIPELINE` — Stage C top_p 贯穿各 backend
- `ENABLE_ROLE_REWRITE` — Stage A 消息角色重塑
- `ENABLE_SAMPLE_OVERRIDE` — Stage D /temp /top_p 手动覆盖
- `SEARCH_AUTONOMOUS` — 全自主搜索（fast/pro 档强制挂原生搜索工具，模型自决；lite 豁免）

所有配置集中在 `app/config.py`，含环境变量读取辅助函数 (`_get_int`, `_get_bool`, `_get_float`)。

## 提交规范

来自 `coding-policy.md`:

- 格式: `feat:` / `fix:` / `refactor:` / `docs:` / `chore:` + 描述
- 小步提交: 每个 commit 单一目的
- 涉及部署/鉴权/数据迁移的变更标记高风险
- 部署失败最多重试 2 次，第 3 次回滚

## 测试

- 测试在 `tests/` 目录，使用 pytest + pytest-asyncio
- `conftest.py` 全局设置 `GEMINI_API_KEY=test-dummy-key` 防止模块初始化失败
- CI (GitHub Actions) 运行: `pip install -r requirements.txt && pip install pytest pytest-asyncio && python -m compileall -q app main.py && pytest -q tests`

## 开发注意

- 回复和代码注释使用中文
- 代理配置分层: Gemini SDK 通过 httpx 单独配代理 (`SOCKS_PROXY`)，钉钉国内服务不走代理
- `socks5h://` → `socks5://` 转换: aiohttp 和 httpx 不支持 `socks5h` 前缀，需要转换
- 文件操作显式指定 `encoding='utf-8'`
- 不提交 `.env` 文件或密钥
- Push 前审计：`git diff origin/master...HEAD | grep -iE 'key|secret|token|password'`，仓库是公有的

## Agent skills

### Issue tracker

Issues 与 PRD 存放于本仓库的 GitHub Issues（通过 `gh` CLI 操作）；外部贡献者的 PR 也作为 triage 队列的一种需求来源。详见 `docs/agents/issue-tracker.md`。

### Triage labels

五个规范角色使用默认标签字符串（`needs-triage`、`needs-info`、`ready-for-agent`、`ready-for-human`、`wontfix`）。详见 `docs/agents/triage-labels.md`。

### Domain docs

单上下文布局——根目录一个 `CONTEXT.md` + `docs/adr/`。详见 `docs/agents/domain.md`。