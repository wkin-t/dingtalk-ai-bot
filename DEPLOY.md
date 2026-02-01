# 部署指南

## 架构概述

本项目支持两种 AI 后端:

1. **Gemini Bot** - 使用 Google Gemini API
2. **OpenClaw Bot** - 使用 OpenClaw Gateway

每个机器人使用独立的钉钉应用、独立的 Docker 容器和独立的端口。

## 服务端口

| 服务 | 端口 | 用途 |
|------|------|------|
| dingtalk-gemini | 35000 | Gemini AI 机器人 + OpenAI 兼容 API |
| dingtalk-openclaw | 35001 | OpenClaw AI 机器人 + OpenAI 兼容 API |

## 部署步骤

### 1. 部署 Gemini Bot (默认)

```bash
# 1. 配置环境变量
cp .env.example .env
vim .env  # 填入钉钉凭证和 Gemini API Key

# 2. 构建并启动
docker-compose up -d --build

# 3. 查看日志
docker logs -f dingtalk-gemini

# 4. 健康检查
curl http://localhost:35000/
curl http://localhost:35000/v1/models
```

### 2. 部署 OpenClaw Bot (可选)

**前置条件**: 确保 OpenClaw Gateway 已部署并可访问 (ws://127.0.0.1:18789)

```bash
# 1. 配置环境变量
cp .env.openclaw.example .env.openclaw
vim .env.openclaw  # 填入新的钉钉凭证和 OpenClaw 配置

# 2. 构建并启动
docker-compose -f docker-compose.openclaw.yml up -d --build

# 3. 查看日志
docker logs -f dingtalk-openclaw

# 4. 健康检查
curl http://localhost:35001/
```

### 3. 在 1Panel 中管理

#### 方式 1: 通过 1Panel Web UI

1. 登录 1Panel: http://your-server-ip:端口
2. 进入 "容器" > "编排"
3. 新建编排:
   - 名称: `dingtalk-gemini`
   - 路径: `/opt/dingtalk-ai-bot`
   - 上传 `docker-compose.yml`
4. (可选) 重复以上步骤创建 `dingtalk-openclaw` 编排

#### 方式 2: 手动部署到 1Panel 目录

```bash
# SSH 到服务器
ssh tencent_cloud_server

# 进入 1Panel 编排目录
cd /opt/1panel/docker/compose

# 创建 dingtalk-gemini 目录 (如果不存在)
sudo mkdir -p dingtalk-gemini
cd dingtalk-gemini

# 链接到代码目录的 docker-compose.yml
sudo ln -sf /opt/dingtalk-ai-bot/docker-compose.yml .

# 启动服务
docker-compose up -d --build

# (可选) 创建 openclaw bot
cd /opt/1panel/docker/compose
sudo mkdir -p dingtalk-openclaw
cd dingtalk-openclaw
sudo ln -sf /opt/dingtalk-ai-bot/docker-compose.openclaw.yml docker-compose.yml
docker-compose up -d --build
```

## 环境变量说明

### Gemini Bot (.env)

| 变量 | 必填 | 说明 |
|------|------|------|
| `DINGTALK_CLIENT_ID` | ✓ | 钉钉应用 Client ID |
| `DINGTALK_CLIENT_SECRET` | ✓ | 钉钉应用 Secret |
| `GEMINI_API_KEY` | ✓ | Google Gemini API Key |
| `SOCKS_PROXY` | - | 代理地址 (默认 socks5h://127.0.0.1:1080) |
| `AI_BACKEND` | - | 固定为 `gemini` |
| `FLASK_PORT` | - | Flask 端口 (默认 35000) |

### OpenClaw Bot (.env.openclaw)

| 变量 | 必填 | 说明 |
|------|------|------|
| `DINGTALK_CLIENT_ID` | ✓ | 新钉钉应用 Client ID (与 Gemini 不同) |
| `DINGTALK_CLIENT_SECRET` | ✓ | 新钉钉应用 Secret |
| `OPENCLAW_GATEWAY_URL` | ✓ | OpenClaw Gateway 地址 (默认 ws://127.0.0.1:18789) |
| `OPENCLAW_GATEWAY_TOKEN` | - | Gateway 认证 Token |
| `OPENCLAW_AGENT_ID` | - | Agent ID (默认 default) |
| `AI_BACKEND` | - | 固定为 `openclaw` |
| `FLASK_PORT` | - | Flask 端口 (默认 35001) |

## 数据目录

- **Gemini Bot**: `./data/` - 对话历史存储
- **OpenClaw Bot**: `./data-openclaw/` - 对话历史存储 (独立)

两个机器人的对话历史相互隔离。

## 故障排查

### Gemini Bot 无法连接

```bash
# 检查代理
docker exec dingtalk-gemini curl -x socks5h://127.0.0.1:1080 https://generativelanguage.googleapis.com

# 检查 v2raya 是否运行
systemctl status v2raya
```

### OpenClaw Bot 连接失败

```bash
# 检查 OpenClaw Gateway 是否运行
curl http://127.0.0.1:18789

# 查看 WebSocket 连接日志
docker logs -f dingtalk-openclaw | grep "OpenClaw Gateway"

# 应显示: "✅ 已连接到 OpenClaw Gateway"
```

### 端口冲突

如果端口被占用,可以修改环境变量:

```bash
# .env 或 .env.openclaw
FLASK_PORT=35002  # 使用其他端口
```

## 卸载

```bash
# 停止并删除 Gemini Bot
docker-compose down -v

# 停止并删除 OpenClaw Bot
docker-compose -f docker-compose.openclaw.yml down -v

# 清理镜像
docker rmi dingtalk-gemini:local
docker rmi dingtalk-openclaw:local
```

## 更新

```bash
# 拉取最新代码
cd /opt/dingtalk-ai-bot
git pull

# 重新构建并重启 (Gemini)
docker-compose up -d --build

# 重新构建并重启 (OpenClaw)
docker-compose -f docker-compose.openclaw.yml up -d --build
```

## 参考

- [OpenClaw 文档](https://github.com/clawdeck/openclaw)
- [钉钉机器人开发文档](https://open.dingtalk.com/document/robots/robot-overview)
- [Google Gemini API 文档](https://ai.google.dev/gemini-api/docs)
