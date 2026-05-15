# DingTalk & WeCom AI Bot

钉钉 & 企业微信 AI 机器人，支持 **Gemini / OpenRouter (Claude) / LiteLLM (OpenAI/Vertex AI) / OpenClaw** 四套后端。Python + Flask，Docker 部署。

## 核心功能

### 多后端支持

通过 `AI_BACKEND` 环境变量一键切换：

| 后端 | 变量值 | 特点 |
|------|--------|------|
| Google Gemini | `gemini` | 原生 SDK，支持 Google Search Grounding |
| OpenRouter | `openrouter` | 统一接入 Claude/GPT，支持 fallback、prompt cache |
| LiteLLM (OpenAI/Vertex AI) | `openai` | 兼容 OpenAI 协议的任意模型 |
| OpenClaw | `openclaw` | 自建 Gateway，HTTP SSE + WebSocket |

### 三档智能路由

路由分析在**卡片创建前**完成，让思考文字第一时间显示正确内容。路由大脑由轻量模型（Haiku / flash-lite）担任：

| 档位 | OpenRouter 模型 | 适用场景 |
|------|----------------|---------|
| `lite` | Claude Haiku | 简单问候、一句话闲聊 |
| `fast` | Claude Sonnet | 日常问答、代码、图片分析（默认） |
| `pro` | Claude Opus/Sonnet | 复杂推理、学术研究、系统架构 |

路由同时决定：`thinking_level`（minimal/low/medium/high）、`temperature`（precise/balanced/creative）、`need_search`、`need_image_gen`、`need_image_edit`。

### AI 特性

- **Thinking 模式**：展示 AI 思考过程（可折叠卡片）
- **实时搜索**：天气/新闻/股价等自动联网（Gemini Search Grounding 或 OpenRouter web_search）
- **生图 + 改图**：Gemini Imagen 或 OpenAI gpt-image-2，改图支持 Gemini Flash exp / OpenAI images.edit
- **多模态**：图片识别与分析（单张或多张）
- **流式输出**：钉钉 AI 卡片逐字显示
- **Soul 自主进化**：AI 每次对话后反思并进化个性，30 分钟冷却，changelog 存档

### 对话管理

- **上下文记忆**：Redis + MySQL 双层存储，自动降级到本地文件
- **消息合并**：2 秒缓冲窗口，自动合并连续消息
- **会话隔离**：群聊共享上下文，单聊独立；Soul 文件按 `{BOT_ID}__{cid}` 隔离，防多容器互污
- **快捷指令**：`/clear` 清空、`/stats` 统计、`/soul` 查看/设置 Soul

## 架构

```
┌──────────────────┐            ┌─────────────────────────────────────────┐
│    钉钉群/单聊    │◄──Stream──►│              main.py (Flask)            │
└──────────────────┘            │  ├─ dingtalk_bot.py  (消息处理+路由)     │
┌──────────────────┐            │  ├─ app/ai/handler.py (统一AI处理层)     │
│  企业微信内部群   │◄──HTTPS───►│  ├─ app/ai/backend.py (后端分派)        │
└──────────────────┘            │  ├─ app/wecom/       (企业微信模块)      │
                                │  ├─ Redis + MySQL (对话历史)             │
                                └──────────────┬──────────────────────────┘
                                               │ AI_BACKEND 分派
                    ┌──────────────────────────┼──────────────────────────┐
                    ▼                          ▼                          ▼
         ┌─────────────────┐       ┌─────────────────┐       ┌─────────────────┐
         │  gemini_client  │       │  litellm_client  │       │ openclaw_client │
         │  (Gemini SDK)   │       │ (OpenRouter/OAI) │       │  (HTTP SSE/WS)  │
         └─────────────────┘       └─────────────────┘       └─────────────────┘
```

## 项目结构

```
dingtalk-ai-bot/
├── app/
│   ├── ai/
│   │   ├── handler.py       # AIHandler — 统一 AI 处理层，屏蔽平台差异
│   │   ├── backend.py       # 后端分派 (gemini/openclaw/openai/openrouter)
│   │   ├── router.py        # 关键词路由降级（无 LLM 时的 fallback）
│   │   └── buffer.py        # 消息缓冲器（2 秒窗口合并连续消息）
│   ├── wecom/
│   │   ├── crypto.py        # 企业微信消息加解密
│   │   ├── callback.py      # Webhook 回调路由
│   │   ├── bot.py           # 消息处理器
│   │   └── message.py       # 消息发送器
│   ├── config.py            # 所有配置（环境变量 + ROUTE_KEY_MAP + 模型定价）
│   ├── dingtalk_bot.py      # 钉钉 Stream 消息处理 + 三档路由
│   ├── dingtalk_card.py     # 钉钉 AI 卡片（创建/流式更新）
│   ├── gemini_client.py     # Gemini API 客户端（google-genai SDK）
│   ├── litellm_client.py    # LiteLLM 客户端（OpenRouter/OpenAI/Vertex AI）
│   ├── openclaw_client.py   # OpenClaw 客户端
│   ├── image_gen.py         # 生图 + 改图（Gemini Imagen / OpenAI gpt-image-2）
│   ├── image_store.py       # 图片上传 COS → 预签名 URL
│   ├── memory.py            # 对话历史（Redis+MySQL → 文件降级）
│   ├── database.py          # Redis 缓存 + MySQL 持久化
│   ├── reference.py         # 历史引用（智能触发）
│   └── routes.py            # OpenAI 兼容 API (/v1/chat/completions)
├── webhook_sg/              # 独立服务：安全组动态开门（端口 35555）
├── tests/                   # pytest 测试套件
├── main.py                  # 入口：Monkey patch + Flask + 多平台启动
├── docker-compose.yml               # Gemini 后端
├── docker-compose.openai.yml        # OpenAI/LiteLLM 后端
├── docker-compose.openrouter.yml    # OpenRouter 后端（端口 35002）
├── docker-compose.wecom.yml         # 企业微信后端
├── .env.example             # Gemini 环境变量模板
└── .env.openrouter.example  # OpenRouter 环境变量模板
```

## 快速开始

### 1. 选择后端并配置环境变量

**Gemini 后端**
```bash
cp .env.example .env
# 填写 GEMINI_API_KEY, DINGTALK_CLIENT_ID, DINGTALK_CLIENT_SECRET
```

**OpenRouter 后端（推荐，支持 Claude）**
```bash
cp .env.openrouter.example .env.openrouter
# 填写 OPENROUTER_API_KEY, DINGTALK_CLIENT_ID, DINGTALK_CLIENT_SECRET
```

### 2. Docker 部署

```bash
# Gemini 后端
docker-compose up -d --build

# OpenRouter 后端（Claude，端口 35002）
docker-compose -f docker-compose.openrouter.yml up -d --build

# OpenAI/LiteLLM 后端
docker-compose -f docker-compose.openai.yml up -d --build

# 企业微信
docker-compose -f docker-compose.wecom.yml up -d --build

# 查看日志
docker logs -f dingtalk-ai-bot-gemini     # Gemini 容器
docker logs -f dingtalk-ai-bot-openai     # OpenAI 容器
```

### 3. 验证服务

```bash
curl http://localhost:35000/         # 健康检查
curl http://localhost:35000/v1/models  # 模型列表
```

## 核心环境变量

| 变量 | 必填 | 说明 |
|------|------|------|
| `AI_BACKEND` | 是 | `gemini` / `openrouter` / `openai` / `openclaw` |
| `PLATFORM` | 是 | `dingtalk` / `wecom` / `both` |
| `DINGTALK_CLIENT_ID` | 是（钉钉） | 钉钉应用 AppKey |
| `DINGTALK_CLIENT_SECRET` | 是（钉钉） | 钉钉应用 AppSecret |
| `GEMINI_API_KEY` | Gemini | Google Gemini API Key |
| `OPENROUTER_API_KEY` | OpenRouter | OpenRouter API Key |
| `SOCKS_PROXY` | 境外 | `socks5h://127.0.0.1:1080` |

**OpenRouter 专属：**

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `OPENROUTER_MODEL_LITE` | `anthropic/claude-haiku-4-5` | lite 档模型 |
| `OPENROUTER_MODEL_FAST` | `anthropic/claude-sonnet-4-5` | fast 档模型（默认） |
| `OPENROUTER_MODEL_PRO` | `anthropic/claude-opus-4-5` | pro 档模型 |
| `OPENROUTER_ROUTER_MODEL` | `anthropic/claude-haiku-4-5` | 路由大脑模型 |
| `OPENROUTER_PROVIDER_ORDER` | `Anthropic` | 强制走 Anthropic 官方 provider（命中 prompt cache） |

## 使用说明

### 对话指令

| 指令 | 说明 |
|------|------|
| `@机器人 + 问题` | 普通对话 |
| 发图 + `@机器人 + 问题` | 图片分析或改图 |
| `/clear` | 清空上下文 |
| `/stats` | 查看使用统计（Token、费用、延迟） |
| `/soul` | 查看当前 Soul 设定 |
| `/soul <内容>` | 手动设置 Soul 个性 |
| `/soul reset` | 重置为默认 Soul |
| `/soul evolve` | 手动触发 Soul 进化 |
| `/soul log` | 查看 Soul 进化历史 |

### 快捷按钮

钉钉卡片底部提供：**🧹 清空 / 🔄 重试 / 📝 总结 / 🇬🇧 翻译**

### 状态栏格式

```
🤖 claude-sonnet-4-5 | 🧠 medium | t=0.7 🌐
```

## 测试

```bash
pytest -q tests                   # 运行全部测试
pytest -q tests/test_memory.py    # 运行单个测试文件
python -m compileall -q app main.py  # 编译检查
```

## 依赖

| 包 | 用途 |
|---|------|
| `google-genai` | Gemini API SDK |
| `litellm` | OpenAI/OpenRouter/Vertex AI 统一客户端 |
| `dingtalk-stream` | 钉钉 Stream SDK |
| `alibabacloud_dingtalk` | 钉钉 OpenAPI SDK |
| `redis` | Redis 客户端 |
| `pymysql` | MySQL 客户端 |
| `flask` / `gunicorn` | Web 服务 |
| `httpx[socks]` | HTTP 客户端（Gemini SDK 代理） |
| `cos-python-sdk-v5` | 腾讯云 COS（生图存储） |

## 相关

- [security-gate](https://github.com/wkin-t/security-gate) — 腾讯云安全组动态开门服务

## License

MIT License. See `LICENSE`.
