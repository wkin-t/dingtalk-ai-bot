# DingTalk AI Bot

钉钉 AI 机器人服务，同一套代码可部署为多个独立机器人（Gemini / GPT / Claude），在同一个群里协作对话。Python + Flask，Docker 部署。

> **平台与后端状态**
> - 钉钉：生产使用中
> - 企业微信：⚠️ **Deprecated**（2026-07-08 起暂不使用，代码保留但不再维护）
> - OpenClaw 后端：⚠️ **Deprecated**（暂不使用，代码保留但不再维护）

## 核心功能

### 多后端

通过 `AI_BACKEND` 环境变量切换协议后端，切换点在 `app/ai/backend.py::create_backend_stream()`，消息处理层不感知具体后端：

| `AI_BACKEND` | 客户端 | 说明 |
|---|---|---|
| `gemini` | `app/gemini_client.py`（google-genai SDK） | 可直连 Google，也可通过 `GEMINI_API_BASE` 走中转站的 `/v1beta` 原生协议；支持原生 Google Search |
| `openai` | `app/openai_client.py`（官方 `openai` SDK） | 任意 OpenAI 兼容端点（官方 API 或自建中转站）。模型名含 `gemini` 走 Chat Completions，其余走 Responses API |
| `openrouter` | `app/openrouter_client.py`（官方 `openrouter` SDK） | 原生 OpenRouter，支持模型 fallback、provider 路由与 prompt cache。代码可用，当前生产未部署 |
| `openclaw` | `app/openclaw_client.py` | ⚠️ Deprecated。自建 OpenClaw Gateway（HTTP SSE / WebSocket） |

LiteLLM 已完全移除，所有 OpenAI 兼容路径统一走官方 `openai` SDK。

### 当前生产部署

三个容器同时运行，各自对应一个钉钉应用：

| 容器 | compose 文件 | 端口 | `AI_BACKEND` | 模型家族 |
|---|---|---|---|---|
| `dingtalk-ai-bot-gemini` | `docker-compose.yml` | 35000 | `gemini` | Gemini |
| `dingtalk-ai-bot-openai` | `docker-compose.openai.yml` | 35001 | `openai` | GPT |
| `dingtalk-ai-bot-anthropic` | `docker-compose.anthropic.yml` | 35002 | `openai` | Claude |

容器名表示接入的**模型家族**，`AI_BACKEND` 表示使用的**协议**。Claude 容器走 OpenAI 兼容中转站的 Responses API，不经过 OpenRouter。该容器 2026-08-25 前名为 `openrouter`，改名是为了消除这个误解。

### 三档智能路由

路由分析在**卡片创建前**完成，卡片一出现就能显示正确的思考文字。路由由轻量模型 `MODEL_ROUTER` 执行，输出：

- 档位：`lite`（简单问候）/ `fast`（日常问答，默认）/ `pro`（复杂推理）
- `thinking_level`：minimal / low / medium / high
- `temperature`：precise / balanced / creative → 0.1 / 0.7 / 0.9
- `need_search` / `need_image_gen` / `need_image_edit`

路由模型不可用时，降级为 `app/ai/router.py` 的关键词匹配。

### AI 特性

- **流式 AI 卡片**：钉钉卡片逐字更新，展示思考过程；更新节流由 `STREAM_UPDATE_THROTTLE` 控制（默认 1.5s）
- **原生联网搜索**：Gemini 用 `google_search`，GPT 走 Responses API 的 `web_search`，原生 OpenRouter 使用 annotations。默认全自主（`SEARCH_AUTONOMOUS=true`），fast/pro 档始终挂载搜索工具，由模型自己决定是否搜索。🌐 图标只在确实发生搜索时点亮
- **Claude 搜索暂时禁用**：s2a 中转站的已知行为是，Claude（anthropic 容器）一旦触发联网搜索就会被静默换成非 Claude 模型（实测常见 gemini-2.5-flash）再作答，属于身份冒充。因此 `CLAUDE_SEARCH_BRIDGE_ENABLED` 默认 `false`，Claude 不挂任何搜索工具；当用户确实要求联网时，改为注入 system 提示，让 Claude 自己说明"搜索工具会把我换成别的模型，所以现在用不了"。相关的桥接实现（`app/antigravity_search.py`）与身份校验保留在代码里，作为将来重新打开该开关时的护栏
- **生图 + 改图**：Gemini Imagen / OpenAI `gpt-image-2` 生图；Gemini Flash / OpenAI images.edit 改图。图片上传腾讯云 COS，以预签名 URL 展示
- **多模态**：单图/多图识别，图片 MIME 按文件魔数检测
- **Soul 自主进化**：每次对话后 AI 反思并进化个性，30 分钟冷却，保留 changelog。Soul 文件按 `{BOT_ID}__{cid}.md` 隔离

### 多机器人协作

- **消息身份区分**：dws 历史按发送者区分本机器人的 `assistant` 回复与其他成员 / 机器人的 `user` 消息，保留发送人和时间；共享角色重塑层继续处理带 `bot_id` 的其他来源消息
- **软清空**：`/clear` 只记录当前机器人的上下文起点，不删数据，也不影响其他机器人
- **采样覆盖**：`/temp`、`/top_p` 按会话手动设置，24 小时后自动失效；Claude 温度上限 1.0，超出自动截断
- **System Prompt 分块缓存**：拆成稳定 / 半稳定 / 变动三段，分别标注 `cache_control`

### 对话管理

- **dws 会话上下文**：群聊和单聊统一从 dws 读取近期消息、引用及媒体资源，不再读写应用自己的 Redis / MySQL / 文件对话历史；旧数据保留
- **各机器人独立窗口**：继续读取各自 `.env*` 的 `MAX_HISTORY_LENGTH`，先按本机器人的 `/clear` 起点过滤，再取最近 N 条历史消息
- **纯 @ 接话**：只 @机器人、不加文字，也会读取当前可见上下文并接话；上下文不足时简短询问
- **群成员与定向 @**：通过 dws 获取当前群成员，优先识别群昵称 / 个人昵称；需要提醒时发送真实 @，并附带该轮完整回复
- **消息合并**：2 秒缓冲窗口合并连续消息
- **幂等去重**：钉钉 Stream 重推的消息按 message_id 去重
- **会话隔离**：群聊共享上下文，单聊独立

## 架构

```
┌──────────────┐  Stream   ┌────────────────────────────────────────────────┐
│ 钉钉群 / 单聊 │◄─────────►│ main.py (Monkey patch + Flask)                 │
└──────────────┘           │  ├─ dingtalk_bot.py   消息处理 / 路由 / 卡片    │
                           │  ├─ app/ai/*          角色重塑 / 采样 / prompt │
                           │  ├─ dws_context.py    会话 / 引用 / 媒体读取   │
                           │  └─ routes.py         HTTP 端点                │
                           └───────────────────────┬────────────────────────┘
                                                   │ AI_BACKEND
              ┌──────────────────┬─────────────────┼──────────────────┐
              ▼                  ▼                 ▼                  ▼
      ┌───────────────┐  ┌───────────────┐ ┌─────────────────┐ ┌─────────────────┐
      │ gemini_client │  │ openai_client │ │openrouter_client│ │ openclaw_client │
      │ google-genai  │  │ Responses/Chat│ │  OpenRouter SDK │ │  (Deprecated)   │
      └───────────────┘  └───────────────┘ └─────────────────┘ └─────────────────┘
```

钉钉消息由 `app/dingtalk_bot.py::handle_ai_stream` 处理。`app/ai/handler.py` 的 `AIHandler` 只服务企业微信（Deprecated），两条路径共享 `app/ai/` 下的工具函数。

dws 提供会话、引用、资源和成员读取，以及定向 @ 发送。应用负责上下文截取、媒体解析、模型调用和流式卡片展示；Soul、统计、采样配置仍由应用管理。钉钉会话每轮提交当前可见历史，不使用 `previous_response_id` 续接。

## 项目结构

```
dingtalk-ai-bot/
├── main.py                       # 入口：Monkey patch（必须最先执行）+ Flask + 平台启动
├── app/
│   ├── config.py                 # 全部配置（环境变量 + 模型默认值 + capability）
│   ├── routes.py                 # HTTP 端点
│   ├── dingtalk_bot.py           # 钉钉 Stream 消息处理、命令、三档路由
│   ├── dingtalk_card.py          # 钉钉 AI 卡片（创建 / 流式更新）
│   ├── gemini_client.py          # Gemini 客户端（google-genai SDK）
│   ├── gemini_sse_patch.py       # google-genai SSE keep-alive 注释行兼容补丁
│   ├── gemini_circuit.py         # Gemini 主路径熔断（配合保底路径）
│   ├── openai_client.py          # OpenAI 兼容客户端（Responses / Chat Completions）
│   ├── responses_state.py        # Responses API 续接状态（previous_response_id）
│   ├── openrouter_client.py      # 原生 OpenRouter 客户端
│   ├── openclaw_client.py        # OpenClaw 客户端（Deprecated）
│   ├── openclaw_tools_client.py  # OpenClaw Tools Invoke（Deprecated）
│   ├── image_gen.py              # 生图 + 改图
│   ├── image_store.py            # 图片上传 COS → 预签名 URL
│   ├── dws_context.py           # dws 会话读取、引用展开、企业校验
│   ├── dws_files.py             # 图片 / 文档下载与解析
│   ├── dws_members.py           # 群成员快照、受限的定向 @ 发送
│   ├── memory.py / database.py   # 旧路径对话存储 / 应用统计，钉钉会话不读写旧历史
│   ├── agent_history.py          # 旧路径按 BOT_ID 的 cutoff 过滤历史
│   ├── clear_cutoff.py           # /clear 软清空时间戳
│   ├── context_inspector.py      # /since 上下文起点
│   ├── sample_override.py        # /temp /top_p /sample 手动覆盖
│   ├── help_text.py              # /help 命令清单
│   ├── reference.py              # 历史引用（智能触发）
│   ├── error_safety.py           # 跨后端异常摘要（允许列表，未知响应正文不外泄）
│   ├── ai/
│   │   ├── backend.py            # 后端分派 + 搜索策略
│   │   ├── router.py             # 关键词路由（降级用）
│   │   ├── buffer.py             # 2 秒消息缓冲
│   │   ├── system_prompt.py      # System prompt 分块缓存
│   │   ├── message_transform.py  # 消息角色重塑
│   │   ├── messages_pipeline.py  # 消息预处理入口
│   │   ├── history_format.py     # 历史格式化（<other_bot> 标签）
│   │   ├── sampling_pipeline.py  # 温度 / top_p 最终取值
│   │   ├── sampling_clamp.py     # 按 provider 截断采样参数
│   │   └── handler.py            # AIHandler（仅企业微信使用）
│   └── wecom/                    # ⚠️ Deprecated 企业微信模块
├── webhook_sg/                   # 独立服务：安全组动态开门（端口 35555）
├── tests/                        # pytest 测试套件
├── docker-compose.yml            # Gemini 容器（35000）
├── docker-compose.openai.yml     # GPT 容器（35001）
├── docker-compose.anthropic.yml  # Claude 容器（35002）
├── docker-compose.openclaw.yml   # ⚠️ Deprecated
└── docker-compose.wecom.yml      # ⚠️ Deprecated
```

## 快速开始

### 1. 准备环境变量

| 部署 | 模板 | 复制为 |
|---|---|---|
| Gemini 容器 | `.env.example` | `.env` |
| GPT 容器 | `.env.openai.example` | `.env.openai` |
| Claude 容器 | `.env.anthropic.example` | `.env.anthropic` |
| 原生 OpenRouter（未部署） | `.env.openrouter.example` | 自定义 |
| OpenClaw（Deprecated） | `.env.openclaw.example` / `.env.multi-agent.example` | `.env.openclaw` |
| 企业微信（Deprecated） | `.env.wecom.example` | `.env.wecom` |

```bash
cp .env.example .env
# 至少填写 DINGTALK_CLIENT_ID / DINGTALK_CLIENT_SECRET / GEMINI_API_KEY / CHAT_COMPLETIONS_BEARER_TOKEN
```

### 2. Docker 部署

```bash
docker compose up -d --build                                    # Gemini
docker compose -f docker-compose.openai.yml up -d --build       # GPT
docker compose -f docker-compose.anthropic.yml up -d --build    # Claude

docker logs -f dingtalk-ai-bot-gemini
```

> 修改 `.env*` 后必须执行 `docker compose ... up -d` 重建容器。`docker restart` **不会重新读取** env_file。

完整的部署、升级与排障说明见 [DEPLOY.md](DEPLOY.md)。

### 3. 本地开发

```bash
pip install -r requirements.txt
python main.py                     # 默认端口 35000
```

## 核心环境变量

完整列表见 `app/config.py`。

| 变量 | 必填 | 说明 |
|---|---|---|
| `DINGTALK_CLIENT_ID` / `DINGTALK_CLIENT_SECRET` | 是 | 钉钉应用凭证，每个容器使用独立应用 |
| `AI_BACKEND` | compose 已设 | `gemini` / `openai` / `openrouter` / `openclaw` |
| `BOT_ID` | **多容器必填** | 机器人实例标识，默认等于 `AI_BACKEND`。多个容器共用同一 `AI_BACKEND` 时必须显式设置不同值，否则角色重塑失效。它还是历史归属、Soul 文件、cutoff 的持久化键，改名须同步迁移数据 |
| `CHAT_COMPLETIONS_BEARER_TOKEN` | **是** | `/v1/chat/completions` 鉴权。fail-closed：未配置时该端点拒绝服务 |
| `MODEL_ROUTER` / `MODEL_LITE` / `MODEL_FAST` / `MODEL_PRO` | 否 | 路由模型与三档模型，所有后端通用；默认值按 `AI_BACKEND` 选择（见下表） |
| `GEMINI_API_KEY` | 视后端 | Google 直连 key。Gemini 后端需要；生图 / 改图也始终直连 Google |
| `GEMINI_API_BASE` / `GEMINI_API_BASE_KEY` | 否 | Gemini 走中转站时的 `/v1beta` 原生协议地址（不带路径后缀）与 key |
| `OPENAI_API_BASE` / `OPENAI_API_KEY` | `openai` 后端 | OpenAI 兼容端点与 key |
| `OPENAI_{LITE,FLASH,PRO}_SUPPORTS_{REASONING,SEARCH,VISION}` | 否 | `openai` 后端各档 capability 声明 |
| `OPENROUTER_API_KEY` | `openrouter` 后端 | 原生 OpenRouter key |
| `OPENROUTER_PROVIDER_ORDER` | 否 | 默认 `Anthropic`。改成其他 provider 时，prompt cache 可能被静默忽略 |
| `PLATFORM` | 否 | 默认 `dingtalk`（`wecom` / `both` 已 Deprecated） |
| `SOCKS_PROXY` | 否 | 访问境外 API 的代理，如 `socks5h://127.0.0.1:1080`；钉钉不走代理 |
| `MAX_HISTORY_LENGTH` | 否 | 当前容器的历史消息条数上限，默认 50；各 `.env*` 可独立设置，不是 token / 字符总量限制。dws 单次读取安全上限 100 条，含当前消息去重余量 |
| `DWS_CONTEXT_LIMIT` | 否 | dws 基础读取窗口，默认 40；钉钉主链路会扩大到足以覆盖当前 bot 的历史预算及去重余量，不会把 50 条配置缩成 40 条 |
| `DWS_EXPECTED_CORP_ID` | 否 | 可显式绑定企业；未设置时使用钉钉 Stream 消息中的机器人企业。无法确认企业或企业不一致时不读取会话 |
| `DWS_MAX_QUOTE_IMAGES` | 否 | 自动补全的历史图与引用图总上限，默认 4；本轮用户上传图片另行处理 |
| `DWS_MAX_DOC_BYTES` / `DWS_MAX_DOC_CHARS` | 否 | 单文档大小 / 正文截取上限，默认 10 MiB / 12000 字符 |
| `REDIS_*` / `MYSQL_*` | 否 | 用于统计、采样等应用状态及旧路径；钉钉对话历史不再通过它们持久化或降级 |
| `COS_SECRET_ID` / `COS_SECRET_KEY` / `COS_BUCKET` / `COS_REGION` | 生图 | 腾讯云 COS 图片存储 |
| `SOUL_ADMIN_IDS` | 否 | 允许修改 Soul 的用户 ID（逗号分隔，留空表示所有人） |
| `CLAUDE_SEARCH_BRIDGE_ENABLED` | 否 | Claude 搜索桥接总开关，默认 `false`（见上文"Claude 搜索暂时禁用"）。重新打开前需要下面 4 个变量都配齐，否则 `bridge_ready()` 仍为 false |
| `ANTIGRAVITY_GEMINI_API_BASE` / `ANTIGRAVITY_GEMINI_API_KEY` | 桥接重新启用时 | 独立于 `GEMINI_API_KEY` 的专用凭据，避免共享 Google key 被旁路搜索。`API_BASE` 需为 sub2api 的 `/v1beta` 地址，不能指向 `generativelanguage.googleapis.com` |
| `ANTIGRAVITY_GEMINI_SEARCH_MODEL` | 桥接重新启用时 | 桥接实际调用的 Gemini 模型名 |
| `ANTIGRAVITY_CLAUDE_BRIDGE_THINKING_LEVELS` | 否 | 只在这些 thinking level 下允许桥接生效，默认仅 `low`（逗号分隔） |

**各后端模型默认值**（`app/config.py::_BACKEND_MODEL_DEFAULTS`，生产环境均通过环境变量显式覆盖）：

| `AI_BACKEND` | router | lite | fast | pro |
|---|---|---|---|---|
| `gemini` | gemini-3.1-flash-lite | gemini-3-flash-preview | gemini-3-flash-preview | gemini-3.1-pro-preview |
| `openai` | deepseek/deepseek-chat | deepseek/deepseek-chat | deepseek/deepseek-chat | deepseek/deepseek-reasoner |
| `openrouter` | anthropic/claude-haiku-4-5 | anthropic/claude-haiku-4-5 | anthropic/claude-sonnet-4-5 | anthropic/claude-opus-4-5 |

> 旧变量 `GEMINI_MODEL`、`OPENAI_MODEL_FLASH`、`OPENROUTER_MODEL_*` 自 2026-05-20 起不再读取。

**Feature flags**（默认均为 `true`，可以单独关闭回滚）：`ENABLE_ROLE_REWRITE`、`ENABLE_CACHE_BLOCKS`、`ENABLE_TOP_P_PIPELINE`、`ENABLE_SAMPLE_OVERRIDE`、`SEARCH_AUTONOMOUS`。

`CLAUDE_SEARCH_BRIDGE_ENABLED` 默认 `false`（与上面相反），是 Claude 搜索桥接的总开关，见上文"Claude 搜索暂时禁用"。

## 使用说明

### 对话指令

群聊中 @机器人 后发送。指令只对当前机器人生效：

| 指令 | 说明 |
|---|---|
| `@机器人 + 问题` | 普通对话 |
| 只 `@机器人` | 根据当前可见群聊上下文接话；上下文不足时简短询问 |
| 发图 + `@机器人 + 问题` | 图片分析或改图 |
| `/help` | 查看命令清单 |
| `/clear` | 软清空：只对当前机器人隐藏此刻之前的历史 |
| `/resume` | 撤销当前 bot 的 `/clear` 起点，恢复 dws 近期窗口；仍受该 bot 的 `MAX_HISTORY_LENGTH` 限制 |
| `/since` | 查看当前可见的最早消息时间与 cutoff |
| `/stats` | Token 用量与费用统计 |
| `/soul` / `/soul <设定>` / `/soul reset` / `/soul evolve` / `/soul log` | 查看 / 设置 / 重置 / 手动进化 / 进化历史（仅群聊，修改需要管理员） |
| `/temp [值\|reset]` | 查看或设置温度（0.0–2.0，24 小时后失效） |
| `/top_p [值\|reset]` | 查看或设置 top_p（0.01–1.0） |
| `/sample` / `/sample reset [temp\|top_p]` | 采样参数总览 / 清除手动设置 |

### 上下文窗口与软清空

历史来源统一为 dws，读取范围仍由每个机器人自己的配置决定。2026-11-12 核实的生产配置如下；这些是部署值，后续可以在各 `.env*` 中调整：

| 机器人 | 配置文件 | `MAX_HISTORY_LENGTH` |
|---|---|---|
| Gemini | `.env` | 50 |
| GPT | `.env.openai` | 50 |
| Claude | `.env.anthropic` | 25 |

每轮先排除当前触发消息及当前 bot 的 `/clear` 起点之前（含起点时刻）的消息，再取最近 N 条。历史图片和文档只补全这 N 条内的消息，说明文字归入原消息；用户显式引用的消息及引用邻居可以额外展开。N 指历史消息条数，不限制系统提示、当前输入、引用展开后的字符数或图片 token；文档和媒体另有大小限制。

`/clear` 按 `(BOT_ID, 会话)` 保存起点，不删除钉钉记录，也不改变其他机器人的可见范围。例如只对 Claude 执行 `/clear` 后，Gemini / GPT 仍可以读取各自窗口内的旧消息。`/since` 可以查看当前实际可见条数、最早时间和清空状态；`/resume` 取消起点过滤，但不会加载无限历史。清空前的消息如果被用户重新显式引用，会作为本轮提供的内容处理。

dws 登录或会话读取失败时，助手明确提示失败并停止该轮模型调用，不会回退旧数据库。部分互动卡片在 dws 中只有占位内容，会标记正文不可读取；不能把占位信息当作完整回复，也不能据图片占位文字猜测画面。

### 群成员与定向提醒

可以让助手提醒当前群中的某位成员。成员 ID 来自本轮 dws 名单，模型只选择成员编号，程序校验后通过 dws 以当前机器人身份发送 `@成员 + 完整回复`。每轮最多 3 人，不支持 @所有人；同名或身份不明确时先澄清。发送结果未知时不自动重发。

这是回复过程中的定向提醒能力；当前没有新增后台定时巡群或定时提醒任务。

### Deprecated 逻辑

以下代码保留兼容、旧数据维护和回归验证，已在对应模块、类、函数或分支标注 `Deprecated`；新增钉钉能力应沿当前 dws 链路实现。

| 弃用范围 | 代码位置 | 当前路径 / 保留原因 |
|---|---|---|
| 企业微信接入 | `app/wecom/`、`main.register_wecom_webhook`、`app.ai.handler.AIHandler` | 2026-07-08 起不再维护；生产使用 `app.dingtalk_bot` |
| OpenClaw Gateway / Tools | `app/openclaw_client.py`、`app/openclaw_tools_client.py`，以及后端分派、配置和钉钉内的 OpenClaw 分支 | 保留旧 HTTP / WebSocket / ASR / 文件工具兼容；当前钉钉历史图片和文档走 dws。语音 / 直接文件消息的旧 Tools 分支尚未迁移 |
| 旧多平台缓冲器 | `app.ai.buffer.MessageBuffer` | 保留兼容导出；钉钉使用 `GeminiBotHandler.process_buffered_messages` |
| Redis / MySQL / 文件对话历史 | `app.memory` 的历史读写函数、`app.database.HistoryStorage` / `DistributedLock`、`app.agent_history` | 钉钉改用 dws；`get_session_key`、数据库连接、使用统计、Token 缓存继续使用 |
| dws 与旧库混合模式 | `fetch_dws_context_bundle(dws_only=False)`、旧历史合并 / 消息入库 / 卡片正文补全，以及 `render_since(history=None)` | 钉钉始终使用 `dws_only=True`，`/since` 显式传入 dws 历史 |
| 旧历史配置 | `MAX_STORAGE_LENGTH`、`HISTORY_TTL`、`ENABLE_DWS_GROUP_CONTEXT`、`DWS_PERSIST_GROUP_MESSAGES`、`DWS_IGNORE_SENDERS` | 仅服务旧历史 / 混合模式；当前钉钉历史条数由 `MAX_HISTORY_LENGTH` 控制 |
| 钉钉 Responses 服务端续接 | `app.responses_state` 与 `app.openai_client` 内的 `previous_response_id` 兼容分支 | 仅钉钉会话弃用续接；通用客户端能力、清理旧状态的逻辑仍保留 |
| 旧搜索摘要注入 | `app.gemini_client.google_search`、`_build_search_fallback_summary`、`SEARCH_FALLBACK_PROVIDER` | 仅保留显式兼容回滚，默认 `none`；后端原生搜索继续使用 |
| 旧历史 bot_id 回填 | `scripts/backfill_bot_id.py` | 只供旧数据维护，当前 dws 部署无需执行 |

这些标记仅说明适用范围，不改变执行行为。`/clear`、`/resume`、`/since`、共享 AI 工具、原生 OpenRouter 后端以及当前关闭但保留的 Claude 搜索桥接均未整体弃用。

### 卡片

底部快捷按钮：**🧹 清空 / 🔄 重试 / 📝 总结 / 🇬🇧 翻译**

状态栏示例：

```
🤖 claude-sonnet-4-6 | 🧠 medium | t=0.7 | top_p=default 🌐
```

手动设置过的温度 / top_p 会带 ⚙️ 标记；🌐 表示本次回复实际执行了联网搜索。

### HTTP 端点

| 端点 | 鉴权 | 说明 |
|---|---|---|
| `GET /` | 无 | 健康检查 |
| `GET /v1/models` | 无 | 模型列表（OpenAI 兼容格式） |
| `POST /v1/chat/completions` | `CHAT_COMPLETIONS_BEARER_TOKEN` | OpenAI 兼容接口，以服务端 `GEMINI_API_KEY` 转发到 Gemini |
| `POST /api/dingtalk/push` | `DINGTALK_PUSH_BEARER_TOKEN` + 可选 IP 白名单 | 主动向钉钉群 / 用户推送消息 |

## 测试

```bash
pip install pytest pytest-asyncio
python -m compileall -q app main.py   # 编译检查
pytest -q tests                        # 全部测试
pytest -q tests/test_memory.py         # 单个文件
```

CI（GitHub Actions）在 Python 3.11 上运行编译检查和全部测试，另有 CodeQL 扫描。

## 依赖

| 包 | 用途 |
|---|---|
| `dingtalk-stream` / `alibabacloud_dingtalk` | 钉钉 Stream SDK / OpenAPI SDK |
| `google-genai` | Gemini SDK |
| `openai` | OpenAI 兼容客户端（Responses / Chat Completions） |
| `openrouter` | 原生 OpenRouter SDK |
| `websockets` | OpenClaw Gateway（Deprecated） |
| `pycryptodome` | 企业微信消息加解密（Deprecated） |
| `redis` / `pymysql` | 数据层 |
| `flask` / `gunicorn` | Web 服务 |
| `aiohttp` / `httpx[socks]` / `requests[socks]` | HTTP 客户端与代理 |
| `cos-python-sdk-v5` | 腾讯云 COS |

## 相关

- [security-gate](https://github.com/wkin-t/security-gate)：腾讯云安全组动态开门服务
- [SECURITY.md](SECURITY.md)：漏洞报告方式

## License

MIT License. See `LICENSE`.
