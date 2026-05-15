# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

多平台 AI 机器人服务，支持钉钉和企业微信，后端可切换 Gemini、OpenClaw 或 LiteLLM（OpenAI/Vertex AI）。Python + Flask，Docker 部署。

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
docker-compose -f docker-compose.openclaw.yml up -d --build  # openclaw 版本
docker-compose -f docker-compose.openai.yml up -d --build    # openai/litellm 版本
docker-compose -f docker-compose.openrouter.yml up -d --build # openrouter 版本 (端口 35002)
docker-compose -f docker-compose.wecom.yml up -d --build     # 企业微信版本
docker logs -f dingtalk-ai-bot-gemini   # 查看日志

# 部署 (使用 skill)
/deploy [gemini|openclaw|wecom]   # 自动 git push + SSH 部署到腾讯云
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
│   ├── gemini_client.py     # Gemini API 流式调用 (google-genai SDK)
│   ├── litellm_client.py    # LiteLLM 统一流式客户端 (OpenAI/Vertex AI 兼容模型)
│   ├── openclaw_client.py   # OpenClaw 客户端 (HTTP SSE + WebSocket)
│   ├── openclaw_tools_client.py  # OpenClaw Tools Invoke (图片识别等)
│   ├── image_gen.py         # 生图 (Gemini Imagen / OpenAI gpt-image-2)
│   ├── image_store.py       # 生图存储 → 腾讯云 COS → 预签名 URL
│   ├── reference.py         # 历史引用（智能触发）
│   ├── memory.py            # 对话历史 (Redis+MySQL → 文件降级)
│   ├── database.py          # 数据层 (Redis 缓存 + MySQL 持久化)
│   ├── ai/
│   │   ├── handler.py       # AIHandler - 统一 AI 处理层，抽象平台差异
│   │   ├── backend.py       # 后端分派 (gemini/openclaw/openai → 各客户端)
│   │   ├── router.py        # 智能路由 (关键词匹配 → 模型/thinking level)
│   │   └── buffer.py        # 消息缓冲器 (2秒窗口合并连续消息)
│   └── wecom/
│       ├── crypto.py        # 企业微信消息加解密 (WXBizMsgCrypt)
│       ├── callback.py      # Webhook 回调路由 (Flask Blueprint)
│       ├── bot.py           # 消息处理器
│       └── message.py       # 消息发送器
└── webhook_sg/
    └── webhook_sg.py        # 独立服务: 安全组动态开门 (端口 35555)
```

### 关键设计

- **Monkey Patch**: `main.py` 顶部对 `aiohttp` 和 `requests` 打补丁，统一注入代理和重试逻辑。必须在所有其他导入之前执行。
- **三后端切换**: `AI_BACKEND` 环境变量选择 `gemini`、`openclaw` 或 `openai`（LiteLLM/Vertex AI）。OpenClaw 支持 HTTP SSE 和 WebSocket 两种传输。切换点是 `app/ai/backend.py` 的 `create_backend_stream()`，handler/bot 层不感知具体后端。
- **统一 AI 层**: `app/ai/handler.py` 的 `AIHandler` 抽象了钉钉/企业微信的平台差异，共享相同的 AI 调用逻辑。
- **智能路由**: `app/ai/router.py` 先做本地关键词匹配（`COMPLEX_KEYWORDS`/`PRO_KEYWORDS`/`SIMPLE_KEYWORDS`）决定模型和 thinking level，无需额外 API 调用；Gemini 后端也可选用 `gemini-flash-lite` 做更细粒度分析。
- **生图流水线**: 用户发送 `/draw` 触发 `image_gen.py`（Gemini Imagen 或 OpenAI gpt-image-2）→ `image_store.py` 上传腾讯云 COS → 预签名 URL → 钉钉原生图片消息。
- **Soul 自主进化**: 每次对话后 AI 自主反思并进化个性，JSON 格式输出，30 分钟冷却，changelog 存档。命令：`/soul` 查看、`/soul <text>` 设置、`/soul reset` 重置、`/soul evolve` 手动进化、`/soul log` 历史。管理员权限：`SOUL_ADMIN_IDS` 环境变量控制 reset/set/evolve 权限（空=允许所有）。
- **消息缓冲**: 2 秒窗口合并用户连续消息，避免重复触发 AI 请求。
- **会话隔离**: 钉钉 `dingtalk_{conversation_id}`，企业微信 `wecom_{user_id}`；群聊共享上下文，单聊独立。
- **数据降级**: Redis+MySQL 优先，不可用时自动降级到本地文件 (`data/history/`)。
- **OpenClaw 多 Agent 路由**: `OPENCLAW_GROUP_AGENT_MAPPING` 按 conversationId 映射到不同 agent，严格模式 (`OPENCLAW_STRICT_ROUTING=true`) 下未映射的群拒绝访问。

## 配置

环境变量通过 `.env` 文件加载，根据部署类型选择不同文件：
- `.env` → Gemini 后端
- `.env.openclaw` → OpenClaw 后端
- `.env.wecom` → 企业微信+钉钉双平台

核心变量: `AI_BACKEND`（gemini/openclaw/openai）, `PLATFORM`（dingtalk/wecom/both）, `GEMINI_API_KEY`, `DINGTALK_CLIENT_ID/SECRET`, `SOCKS_PROXY`, `OPENCLAW_HTTP_URL`, `OPENCLAW_GATEWAY_TOKEN`, `FLASK_PORT`（默认 35000）。

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
