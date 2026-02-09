# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

Gemini Stack 是一个集成 Google Gemini AI 的多平台 AI 机器人服务，支持钉钉和企业微信双平台部署。

### 核心服务

1. **dingtalk-ai-bot-gemini** - 钉钉 AI 机器人 (Gemini 后端, 端口 35000)
2. **dingtalk-ai-bot-openclaw** - 钉钉 AI 机器人 (OpenClaw 后端, 端口 35001)
3. **dingtalk-ai-bot-wecom** - 企业微信 + 钉钉双平台 AI 机器人 (端口 35002)
4. **sg-webhook** - 安全组动态开门服务 (端口 35555)

### 平台支持

| 平台 | 连接方式 | 公网要求 | 流式更新 | 外部群支持 |
|------|----------|----------|----------|-----------|
| **钉钉** | Stream 长连接 | ❌ 不需要 | ✅ 支持 | ✅ 支持 |
| **企业微信** | HTTPS Webhook | ✅ 需要 | ❌ 完整回复 | ❌ 仅内部群 |

## 常用命令

```bash
# 本地开发 (需先启动代理)
python main.py

# Docker 部署
docker-compose up -d --build
docker logs -f gemini-app
docker logs -f sg-webhook

# 健康检查
curl http://localhost:35000/
curl http://localhost:35000/v1/models
```

## 架构

```
main.py                      # 入口: Monkey patch + Flask + 多平台启动
├── app/
│   ├── __init__.py          # Flask app 初始化
│   ├── config.py            # 环境变量 + 代理配置 + 平台选择
│   ├── routes.py            # OpenAI 兼容 API (/v1/chat/completions)
│   ├── dingtalk_bot.py      # 钉钉 Stream 消息处理
│   ├── dingtalk_card.py     # 钉钉 AI 卡片管理 (创建/流式更新)
│   ├── gemini_client.py     # Gemini API 流式调用
│   ├── openclaw_client.py   # OpenClaw Gateway 客户端
│   ├── memory.py            # 对话历史持久化 (支持平台前缀)
│   ├── ai/                  # 统一 AI 处理层 (NEW)
│   │   ├── handler.py       # AIHandler 抽象平台差异
│   │   ├── router.py        # 智能路由 (模型选择)
│   │   └── buffer.py        # 消息缓冲器
│   └── wecom/               # 企业微信模块 (NEW)
│       ├── crypto.py        # 消息加解密 (WXBizMsgCrypt)
│       ├── callback.py      # Webhook 回调处理
│       ├── bot.py           # 消息处理器
│       └── message.py       # 消息发送器
└── webhook_sg/
    └── webhook_sg.py        # 独立 Flask 服务 (腾讯云 VPC API)
```

### 关键设计

- **Monkey Patch**: `main.py` 顶部对 `aiohttp` 和 `requests` 打补丁，统一注入代理和重试逻辑
- **消息缓冲**: 2 秒缓冲窗口，合并用户连续消息 (支持异步/同步)
- **会话隔离**:
  - 钉钉: `dingtalk_{conversation_id}`
  - 企业微信: `wecom_{user_id}`
  - 群聊共享上下文，单聊独立上下文
- **统一 AI 处理层**: `app/ai/handler.py` 抽象平台差异，钉钉/企业微信共享相同的 AI 逻辑
- **流式更新**:
  - 钉钉: AI 卡片实时更新
  - 企业微信: 发送 "思考中" + 完整回复

## 配置

### 环境变量

通过 `.env` 文件加载 (根据部署类型选择不同的配置文件):

| 文件 | 用途 |
|------|------|
| `.env` | Gemini 后端 (钉钉) |
| `.env.openclaw` | OpenClaw 后端 (钉钉) |
| `.env.wecom` | 企业微信 + 钉钉双平台 |

#### 核心配置项

| 变量 | 用途 | 必填 |
|------|------|------|
| `GEMINI_API_KEY` | Google Gemini API | ✅ |
| `DINGTALK_CLIENT_ID` / `DINGTALK_CLIENT_SECRET` | 钉钉应用凭证 | 钉钉必填 |
| `WECOM_CORP_ID` / `WECOM_AGENT_ID` / `WECOM_SECRET` | 企业微信应用凭证 | 企业微信必填 |
| `WECOM_TOKEN` / `WECOM_ENCODING_AES_KEY` | 企业微信回调配置 | 企业微信必填 |
| `PLATFORM` | 平台选择 (dingtalk/wecom/both) | ✅ |
| `AI_BACKEND` | AI 后端 (gemini/openclaw) | ✅ |
| `SOCKS_PROXY` | 代理 (国内服务器必填) | 可选 |

代码内配置 (`app/config.py`):
- `DEFAULT_MODEL`: 默认 Gemini 模型
- `MAX_HISTORY_LENGTH`: 发送给 Gemini 的最大历史条数 (默认 50)

## 数据存储

对话历史保存在 `data/history/` 目录，每个会话一个 JSON 文件，7 天自动过期。

## 开发规范

### 安全红线
- 删除操作需确认: "⚠️ **Security Check**: I need to delete `[path]`. Do you authorize this action? (y/n)"
- 禁止硬编码 API 密钥

### 代码完整性
- 禁止截断 (`// ... rest of code`)，输出完整代码
- 修改前必须 Read 文件，不依赖记忆
- 文件操作显式指定 `encoding='utf-8'`

### 语言
- 回复使用中文
- 代码注释使用中文

## 部署

使用 `/deploy` skill 进行自动部署。详见 `.claude/skills/deploy.md`

企业微信版本需要额外配置 Nginx 反向代理和 HTTPS 证书，详见 [WECOM_DEPLOYMENT.md](./WECOM_DEPLOYMENT.md)
