# Gemini Stack

一个集成了 **Google Gemini AI** 和 **腾讯云安全组自动化** 的多功能服务栈。通过 Docker Compose 一键部署，包含多个核心服务。

## 目录

- [核心功能](#核心功能)
- [项目结构](#项目结构)
- [技术架构](#技术架构)
- [部署指南](#部署指南)
- [配置说明](#配置说明)
- [使用说明](#使用说明)
- [注意事项](#注意事项)

---

## 核心功能

### 1. Gemini Proxy (AI 助手)

一个连接 Google Gemini API 和钉钉机器人的中间件。

**双模支持：**
- **OpenAI 兼容接口**：提供 `/v1/chat/completions` 和 `/v1/models` 接口，让 IDE 插件（如 Xcode、Continue）可以使用 Gemini 模型
- **钉钉群助手 (Stream 模式)**：无需公网 IP，内网穿透接收钉钉消息

**智能特性：**
- **流式打字机**：在钉钉中实现逐字显示效果（基于 AI 卡片）
- **多模态支持**：支持发送图片（单张或多张），Gemini 可以识别并分析图片内容
- **富文本消息**：支持图文混排的富文本消息解析
- **上下文记忆**：
  - 发送给 Gemini 的最大历史记录：50 条
  - 本地存储的最大历史记录：1000 条
  - 自动过期时间：7 天
  - **群聊上下文共享**：同一群内所有成员共享上下文
- **消息合并**：2 秒缓冲窗口，自动合并连续消息
- **自动 @回复**：谁问 @ 谁，支持多人互动
- **快捷指令**：回复底部提供 [清空]、[重试]、[总结]、[翻译] 等快捷按钮
- **群名感知**：自动识别群名，AI 根据群名调整回复风格
- **全量监听**：即使不 @机器人，机器人也会默默记录群聊内容，以便在被 @ 时能理解上下文

### 2. SG Webhook (安全组自动化)

一个轻量级的 Webhook 服务，用于动态更新腾讯云安全组规则。

- **场景**：当你身处动态 IP 环境（如家庭宽带、咖啡厅），需要访问服务器（SSH/RDP）时
- **原理**：访问特定 URL，服务会自动获取你的当前 IP，并将其添加到腾讯云安全组白名单
- **双协议支持**：自动放行 TCP 和 UDP 的所有端口（或指定端口）
- **自动清理**：自动删除该设备旧 IP 的规则，防止规则堆积
- **设备标识**：通过 `device` 参数区分不同设备的规则

### 3. 其他集成服务

通过 `docker-compose.yml` 还集成了以下服务：

| 服务 | 用途 | 端口 |
|------|------|------|
| v2rayA | 科学上网网关 | 2017 (Web UI), 10809 (SOCKS) |
| Tailscale | 内网穿透 | - |
| RustDesk (hbbs/hbbr) | 远程桌面服务 | 21115-21119 |

---

## 项目结构

```
gemini-stack/
├── app/                       # AI 助手核心代码
│   ├── __init__.py           # Flask 应用初始化
│   ├── config.py             # 配置管理（API Key, 代理, 模型等）
│   ├── dingtalk_bot.py       # 钉钉机器人消息处理器
│   ├── dingtalk_card.py      # 钉钉 AI 卡片管理
│   ├── gemini_client.py      # Gemini API 流式客户端
│   ├── memory.py             # 上下文记忆（文件持久化）
│   └── routes.py             # OpenAI 兼容 API 路由
├── webhook_sg/                # 安全组 Webhook 服务
│   ├── Dockerfile
│   ├── requirements.txt
│   └── webhook_sg.py         # Flask 服务入口
├── data/                      # 数据存储目录
│   └── history/              # 对话历史记录（JSON 文件）
├── demo/                      # 钉钉卡片示例代码
├── docker-compose.yml         # Docker 编排配置
├── Dockerfile                 # AI 助手镜像构建
├── main.py                    # 主入口（Flask + 钉钉 Stream）
├── requirements.txt           # Python 依赖
├── .env                       # 环境变量配置
└── README.md
```

---

## 技术架构

```
                                     ┌─────────────────┐
                                     │  Google Gemini  │
                                     │      API        │
                                     └────────▲────────┘
                                              │
┌─────────────┐     Stream      ┌────────────┴────────────┐
│   钉钉群    │◄───────────────►│     gemini-app          │
│  (机器人)   │                 │  ├─ Flask (35000)       │
└─────────────┘                 │  ├─ DingTalk Stream     │
                                │  └─ Memory (JSON)       │
                                └────────────┬────────────┘
                                             │
                                     ┌───────▼───────┐
                                     │    v2rayA     │
                                     │   (代理网关)   │
                                     └───────────────┘

┌─────────────┐     HTTP        ┌─────────────────────────┐
│  用户浏览器  │────────────────►│     sg-webhook          │
│  (动态 IP)  │                 │  ├─ Flask (35555)       │
└─────────────┘                 │  └─ 腾讯云 VPC API       │
                                └─────────────────────────┘
```

---

## 部署指南

### 1. 准备工作

- 一台 Linux 服务器 (Ubuntu/CentOS)
- Docker 和 Docker Compose 环境
- **Google Gemini API Key**
- **钉钉机器人配置** (Stream 模式)
- **钉钉 AI 卡片模板 ID**
- **腾讯云 API 密钥** (用于安全组功能)

### 2. 克隆项目

```bash
git clone <repository-url>
cd gemini-stack
```

### 3. 配置环境变量

复制并编辑 `.env` 文件：

```bash
cp .env.example .env
vim .env
```

### 4. 启动服务

```bash
# 构建并启动所有服务
docker-compose up -d --build

# 查看日志
docker logs -f gemini-app      # AI 助手日志
docker logs -f sg-webhook      # 安全组服务日志
```

### 5. 验证服务

```bash
# 检查 AI 助手健康状态
curl http://localhost:35000/

# 检查模型列表
curl http://localhost:35000/v1/models
```

---

## 配置说明

### 环境变量 (.env)

| 变量名 | 说明 | 示例 | 归属服务 |
|--------|------|------|----------|
| `GEMINI_API_KEY` | Google Gemini API Key | `AIzaSy...` | AI 助手 |
| `DINGTALK_CLIENT_ID` | 钉钉应用 AppKey | `dinga7...` | AI 助手 |
| `DINGTALK_CLIENT_SECRET` | 钉钉应用 AppSecret | `9hwCG9...` | AI 助手 |
| `HTTP_PROXY` | HTTP 代理地址 | `http://127.0.0.1:10808` | AI 助手 |
| `HTTPS_PROXY` | HTTPS 代理地址 | `http://127.0.0.1:10808` | AI 助手 |

### 代码内配置 (app/config.py)

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `DEFAULT_MODEL` | 默认 Gemini 模型 | `gemini-3-pro-preview` |
| `MAX_HISTORY_LENGTH` | 发送给 Gemini 的最大历史条数 | 50 |
| `MAX_STORAGE_LENGTH` | 本地存储的最大历史条数 | 1000 |
| `HISTORY_TTL` | 历史记录过期时间 | 7 天 |

### 安全组配置 (docker-compose.yml)

| 变量名 | 说明 | 示例 |
|--------|------|------|
| `TENCENT_SECRET_ID` | 腾讯云 SecretId | `AKID...` |
| `TENCENT_SECRET_KEY` | 腾讯云 SecretKey | `Rlvsd...` |
| `TENCENT_REGION` | 腾讯云区域 | `ap-guangzhou` |
| `SECURITY_GROUP_ID` | 安全组 ID | `sg-f9xm...` |
| `TARGET_PORT` | 目标端口 | `ALL` 或 `22` |
| `ACCESS_TOKEN` | Webhook 访问密码 | `your-secret-token` |

### 钉钉卡片模板

在钉钉开发者后台创建 AI 卡片模板，确保包含以下变量：

| 变量名 | 类型 | 说明 |
|--------|------|------|
| `msgTitle` | 普通变量 | 卡片标题 |
| `msgContent` | 流式变量 | 消息内容（支持 Markdown） |
| `thinkingText` | 普通变量 | 思考中提示文本 |
| `statusText` | 普通变量 | 状态栏文本 |
| `msgButtons` | 普通变量 | 快捷按钮配置 |
| `isError` | 普通变量 | 错误状态标识 |
| `flowStatus` | 普通变量 | 流式状态 (1=思考中, 3=完成) |

---

## 使用说明

### 钉钉 AI 助手

1. 在钉钉群中添加机器人
2. **对话**：直接 @机器人 并输入问题
3. **图片分析**：发送图片（或多张图片）并 @机器人，或发送图文混排消息
4. **快捷指令**：

| 指令 | 功能 |
|------|------|
| `清空上下文` / `/clear` / `🧹 清空记忆` | 清空当前群聊的上下文 |
| `📝 总结摘要` | 让 AI 总结刚才的对话 |
| `🇬🇧 翻译成英文` | 翻译上一条回答 |

### OpenAI 兼容接口

```bash
# 发送对话请求
curl -X POST http://localhost:35000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3-pro-preview",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

### 安全组开门

在浏览器或快捷指令中访问：

```
http://<服务器IP>:35555/open-door?key=<ACCESS_TOKEN>&device=<设备名>
```

**参数说明：**
- `key`: 在 docker-compose.yml 中设置的 `ACCESS_TOKEN`
- `device`: 设备标识（如 `MacBook`, `HomePC`），用于区分不同设备的规则

**成功响应示例：**
```
✅ 更新: [MacBook] -> 1.2.3.4 (TCP+UDP)
```

---

## 注意事项

### 网络配置

- `gemini-app` 使用 `network_mode: "host"` 以便连接宿主机的代理
- `sg-webhook` 默认只监听 `127.0.0.1:35555`，建议通过 Nginx 反代

### 代理配置

- 推荐使用 `socks5h://` 协议，确保 DNS 解析也走代理
- 如遇 `Network is unreachable` 错误，检查代理配置是否正确

### 模型版本

- 默认使用 `gemini-3-pro-preview`
- 如遇 404 错误，可在 `app/config.py` 中修改 `DEFAULT_MODEL`
- 可用模型：`gemini-3-pro-preview`, `gemini-3-flash-preview`, `gemini-1.5-flash`

### 卡片模板

- 确保在钉钉后台创建了兼容的 AI 卡片模板
- 在 `app/dingtalk_bot.py` 中更新 `card_template_id`

### 数据持久化

- 对话历史保存在 `data/history/` 目录
- 每个会话对应一个 JSON 文件
- 历史记录超过 7 天自动过期

---

## 依赖说明

主要 Python 依赖：

| 包名 | 用途 |
|------|------|
| `flask` | Web 框架 |
| `gunicorn` | 生产级 WSGI 服务器 |
| `dingtalk-stream` | 钉钉 Stream 模式 SDK |
| `alibabacloud_dingtalk` | 钉钉 OpenAPI SDK |
| `aiohttp` | 异步 HTTP 客户端 |
| `python-socks` | SOCKS 代理支持 |
| `tencentcloud-sdk-python` | 腾讯云 SDK（安全组） |
| `python-dotenv` | 环境变量管理 |

---

## License

MIT License
