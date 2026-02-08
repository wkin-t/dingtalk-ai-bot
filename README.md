# DingTalk & WeCom Gemini AI Bot

钉钉 & 企业微信 AI 机器人，基于 Google Gemini API，支持智能路由、Thinking 模式、实时搜索等高级特性。

> **新增**: 支持企业微信内部群,统一 AI 处理层,钉钉/企业微信会话隔离。详见 [企业微信部署指南](./WECOM_DEPLOYMENT.md)
>
> **客户群方案设计**: 见 [WECOM_CUSTOMER_GROUP_DESIGN.md](./WECOM_CUSTOMER_GROUP_DESIGN.md)

## 核心功能

### 智能路由

自动分析用户问题复杂度，动态选择最优配置：

| 决策项 | 选项 | 说明 |
|--------|------|------|
| **模型** | `gemini-3-flash-preview` / `gemini-3-pro-preview` | Flash 用于日常问答，Pro 用于复杂推理 |
| **Thinking Level** | `minimal` / `low` / `medium` / `high` | 思考深度，影响响应速度和质量 |
| **Google Search** | 开启 / 关闭 | 需要实时信息时自动启用 |

### AI 特性

- **Thinking 模式**：展示 AI 思考过程（可折叠）
- **Google Search**：实时搜索最新信息（天气、新闻、股价等）
- **多模态**：支持图片识别和分析（单张或多张）
- **流式输出**：钉钉 AI 卡片逐字显示

### 对话管理

- **上下文记忆**：群聊共享上下文，支持多人协作
- **消息合并**：2 秒缓冲窗口，自动合并连续消息
- **全量监听**：不 @机器人也会记录上下文
- **快捷指令**：清空、重试、总结、翻译

### 使用统计

- 记录每次请求的模型、Token 用量、延迟
- 支持用户/群聊/全局统计
- 自动计算费用（基于 Gemini 官方定价）

## 技术架构

```
┌──────────────┐   Stream   ┌────────────────────────────────────┐
│   钉钉群     │◄───────────►│      dingtalk-wecom-gemini        │
│  (机器人)    │             │  ├─ Flask (35000)                 │
└──────────────┘             │  ├─ DingTalk Stream               │
                             │  ├─ WeCom Webhook (/api/wecom)    │
┌──────────────┐   HTTPS     │  ├─ 统一 AI 处理层 (app/ai)       │
│ 企业微信群   │◄───────────►│  ├─ Redis (缓存)                  │
│  (内部群)    │             │  └─ MySQL (持久化)                │
└──────────────┘             └──────────────┬─────────────────────┘
                                            │
                         ┌──────────────────┼──────────────────┐
                         │                  │                  │
                         ▼                  ▼                  ▼
                ┌─────────────────┐   ┌─────────────────┐   ┌─────────────────┐
                │ gemini-flash-   │   │ gemini-3-flash  │   │ gemini-3-pro    │
                │ lite-latest     │   │ -preview        │   │ -preview        │
                │ (智能路由分析)   │   │ (日常问答)       │   │ (复杂推理)       │
                └─────────────────┘   └─────────────────┘   └─────────────────┘
```

## 项目结构

```
dingtalk-gemini/
├── app/                       # 核心代码
│   ├── config.py             # 配置管理
│   ├── database.py           # Redis + MySQL 数据层
│   ├── dingtalk_bot.py       # 钉钉消息处理 + 智能路由
│   ├── dingtalk_card.py      # AI 卡片管理
│   ├── gemini_client.py      # Gemini API 客户端
│   ├── memory.py             # 对话历史管理
│   └── routes.py             # OpenAI 兼容 API
├── webhook_sg/               # 安全组 Webhook (独立服务)
│   ├── webhook_sg.py
│   └── Dockerfile
├── main.py                   # 主入口
├── Dockerfile
├── requirements.txt
├── .env.example              # 环境变量模板
└── README.md
```

## 快速开始

### 1. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env 填写以下配置
```

**必填配置：**

| 变量 | 说明 |
|------|------|
| `GEMINI_API_KEY` | Google Gemini API Key |
| `DINGTALK_CLIENT_ID` | 钉钉应用 AppKey |
| `DINGTALK_CLIENT_SECRET` | 钉钉应用 AppSecret |
| `SOCKS_PROXY` | 代理地址 (如 `socks5h://127.0.0.1:1080`) |

**可选配置：**

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `GEMINI_MODEL` | `gemini-3-flash-preview` | 默认模型 |
| `ENABLE_THINKING` | `true` | 是否显示思考过程 |
| `ENABLE_SEARCH` | `true` | 是否启用 Google Search |
| `REDIS_HOST` | `127.0.0.1` | Redis 地址 |
| `MYSQL_HOST` | `127.0.0.1` | MySQL 地址 |

### 2. Docker 部署

```bash
# 构建并启动
docker-compose up -d --build

# 查看日志
docker logs -f dingtalk-gemini
```

### 3. 验证服务

```bash
# 健康检查
curl http://localhost:35000/

# 模型列表
curl http://localhost:35000/v1/models
```

## 使用说明

### 钉钉对话

| 操作 | 说明 |
|------|------|
| @机器人 + 问题 | 普通对话 |
| 发送图片 + @机器人 | 图片分析 |
| `/clear` 或 `🧹 清空记忆` | 清空上下文 |
| `/stats` 或 `📊 统计` | 查看使用统计 |

### 快捷按钮

回复底部提供快捷按钮：
- **🧹 清空**：清空对话历史
- **🔄 重试**：重新生成回答
- **📝 总结**：总结对话内容
- **🇬🇧 翻译**：翻译成英文

### 状态栏

回复底部显示：
- 🧠 思考摘要（如有）
- 🤖 使用的模型
- 🧠 Thinking Level
- 🌐 是否启用搜索

## Gemini 定价参考

| 模型 | 输入 ($/1M tokens) | 输出 ($/1M tokens) |
|------|-------------------|-------------------|
| gemini-3-flash | $0.50 | $3.00 |
| gemini-3-pro | $2.00 | $12.00 |
| gemini-flash-lite | $0.10 | $0.40 |

## 相关项目

- [security-gate](https://github.com/wkin-t/security-gate) - 腾讯云安全组动态开门服务

## 依赖

| 包 | 用途 |
|---|------|
| `google-genai` | Gemini API SDK |
| `dingtalk-stream` | 钉钉 Stream SDK |
| `alibabacloud_dingtalk` | 钉钉 OpenAPI SDK |
| `redis` | Redis 客户端 |
| `pymysql` | MySQL 客户端 |
| `flask` / `gunicorn` | Web 服务 |
| `httpx[socks]` | HTTP 客户端 (支持 SOCKS 代理) |

## License

MIT License. See `LICENSE`.
