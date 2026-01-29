# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

Gemini Stack 是一个集成 Google Gemini AI 和腾讯云安全组自动化的多功能服务栈，包含两个核心服务：

1. **gemini-app** - 钉钉 AI 机器人 (端口 35000)
2. **sg-webhook** - 安全组动态开门服务 (端口 35555)

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
main.py                      # 入口: Monkey patch + Flask + DingTalk Stream 线程
├── app/
│   ├── __init__.py          # Flask app 初始化
│   ├── config.py            # 环境变量 + 代理配置
│   ├── routes.py            # OpenAI 兼容 API (/v1/chat/completions)
│   ├── dingtalk_bot.py      # Stream 消息处理 + 消息缓冲合并
│   ├── dingtalk_card.py     # AI 卡片管理 (创建/流式更新)
│   ├── gemini_client.py     # Gemini API 流式调用
│   └── memory.py            # 对话历史持久化 (JSON 文件)
└── webhook_sg/
    └── webhook_sg.py        # 独立 Flask 服务 (腾讯云 VPC API)
```

### 关键设计

- **Monkey Patch**: `main.py` 顶部对 `aiohttp` 和 `requests` 打补丁，统一注入代理和重试逻辑
- **消息缓冲**: `dingtalk_bot.py` 中 2 秒缓冲窗口，合并用户连续消息
- **会话隔离**: 群聊使用 `conversation_id` 作为 session_key (共享上下文)，单聊使用 `sender_id`
- **流式更新**: AI 卡片支持 500ms 节流的实时内容更新

## 配置

环境变量通过 `.env` 文件加载：

| 变量 | 用途 |
|------|------|
| `GEMINI_API_KEY` | Google Gemini API |
| `DINGTALK_CLIENT_ID` / `DINGTALK_CLIENT_SECRET` | 钉钉应用凭证 |
| `SOCKS_PROXY` | 代理 (默认 socks5h://172.16.0.8:1080) |

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
