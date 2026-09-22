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

- **消息角色重塑**：其他机器人的回复在发给模型前转为 `user` 角色，并包上 `<other_bot name="X">…</other_bot>` 标签，避免模型把别人的话当成自己说的
- **软清空**：`/clear` 只记录当前机器人的上下文起点，不删数据，也不影响其他机器人
- **采样覆盖**：`/temp`、`/top_p` 按会话手动设置，24 小时后自动失效；Claude 温度上限 1.0，超出自动截断
- **System Prompt 分块缓存**：拆成稳定 / 半稳定 / 变动三段，分别标注 `cache_control`

### 对话管理

- **上下文记忆**：Redis + MySQL 双层存储，不可用时自动降级到本地文件
- **消息合并**：2 秒缓冲窗口合并连续消息
- **幂等去重**：钉钉 Stream 重推的消息按 message_id 去重
- **会话隔离**：群聊共享上下文，单聊独立

## 架构

```
┌──────────────┐  Stream   ┌────────────────────────────────────────────────┐
│ 钉钉群 / 单聊 │◄─────────►│ main.py (Monkey patch + Flask)                 │
└──────────────┘           │  ├─ dingtalk_bot.py   消息处理 / 路由 / 卡片    │
                           │  ├─ app/ai/*          角色重塑 / 采样 / prompt │
                           │  ├─ memory.py         Redis + MySQL → 文件降级  │
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
│   ├── memory.py / database.py   # 对话历史（Redis + MySQL → 文件降级）
│   ├── agent_history.py          # 按 BOT_ID 的 cutoff 过滤历史
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
| `REDIS_*` / `MYSQL_*` | 否 | 不配置时降级到本地文件存储 |
| `COS_SECRET_ID` / `COS_SECRET_KEY` / `COS_BUCKET` / `COS_REGION` | 生图 | 腾讯云 COS 图片存储 |
| `SOUL_ADMIN_IDS` | 否 | 允许修改 Soul 的用户 ID（逗号分隔，留空表示所有人） |

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
| 发图 + `@机器人 + 问题` | 图片分析或改图 |
| `/help` | 查看命令清单 |
| `/clear` | 软清空：只对当前机器人隐藏此刻之前的历史 |
| `/resume` | 撤销 `/clear`，恢复全部历史 |
| `/since` | 查看当前可见的最早消息时间与 cutoff |
| `/stats` | Token 用量与费用统计 |
| `/soul` / `/soul <设定>` / `/soul reset` / `/soul evolve` / `/soul log` | 查看 / 设置 / 重置 / 手动进化 / 进化历史（仅群聊，修改需要管理员） |
| `/temp [值\|reset]` | 查看或设置温度（0.0–2.0，24 小时后失效） |
| `/top_p [值\|reset]` | 查看或设置 top_p（0.01–1.0） |
| `/sample` / `/sample reset [temp\|top_p]` | 采样参数总览 / 清除手动设置 |

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
