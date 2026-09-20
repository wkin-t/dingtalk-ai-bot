# 部署指南

## 架构概述

同一份代码按不同 compose 文件部署为多个独立机器人。每个机器人对应**独立的钉钉应用、独立的容器、独立的端口和独立的数据目录**。所有容器都使用 `network_mode: host`。

| 容器 | compose 文件 | env 文件 | 端口 | `AI_BACKEND` | `BOT_ID` | 数据目录 | 状态 |
|---|---|---|---|---|---|---|---|
| `dingtalk-ai-bot-gemini` | `docker-compose.yml` | `.env` | 35000 | `gemini` | `gemini` | `./data` | ✅ 生产 |
| `dingtalk-ai-bot-openai` | `docker-compose.openai.yml` | `.env.openai` | 35001 | `openai` | `openai` | `./data-openai` | ✅ 生产 |
| `dingtalk-ai-bot-anthropic` | `docker-compose.anthropic.yml` | `.env.anthropic` | 35002 | `openai` | `anthropic` | `./data-anthropic` | ✅ 生产 |
| `dingtalk-ai-bot-openclaw` | `docker-compose.openclaw.yml` | `.env.openclaw` | 35001 | `openclaw` | `openclaw` | `./data` | ⚠️ Deprecated |
| `dingtalk-ai-bot-wecom` | `docker-compose.wecom.yml` | `.env.wecom` | 35002 | `gemini` | `gemini` | `./data-wecom` | ⚠️ Deprecated |

> ⚠️ 因为使用 host 网络，Deprecated 的 openclaw（35001）会与 openai 容器冲突，wecom（35002）会与 anthropic 容器冲突，**不能同时运行**。

`anthropic` 容器名表示接入的是 Claude 模型，协议后端是 `AI_BACKEND=openai`，通过 OpenAI 兼容中转站调用 Responses API，**不经过 OpenRouter**。它在 2026-08-25 前名为 `openrouter`，详见文末「从 openrouter 迁移到 anthropic」。

## 前置依赖

| 依赖 | 用途 | 说明 |
|---|---|---|
| OpenAI 兼容中转站（如自建 sub2api） | GPT / Claude 对话，可选 Gemini 对话 | `openai`、`anthropic` 容器通过 `OPENAI_API_BASE` 访问；Gemini 容器可通过 `GEMINI_API_BASE` 走同一中转站的 `/v1beta` 原生协议 |
| SOCKS5 代理（如 v2rayA） | 直连 Google 等境外 API | 生图 / 改图始终直连 Google，需要 `SOCKS_PROXY` |
| Redis / MySQL | 对话历史 | 可选，不可用时降级到容器内 `data/history/` |
| 腾讯云 COS | 生图存储 | 可选，生图功能需要 |

## 部署步骤

以下命令在服务器的代码目录执行（如 `/opt/1panel/docker/compose/dingtalk-ai-bot`）。

### 1. 配置环境变量

```bash
cp .env.example .env                        # Gemini
cp .env.openai.example .env.openai          # GPT
cp .env.anthropic.example .env.anthropic    # Claude
```

每个文件至少需要填写：

- `DINGTALK_CLIENT_ID` / `DINGTALK_CLIENT_SECRET`：各容器使用**不同的**钉钉应用
- `CHAT_COMPLETIONS_BEARER_TOKEN`：`openssl rand -hex 32` 生成。未配置时 `/v1/chat/completions` 拒绝服务
- 模型与上游：
  - Gemini：`GEMINI_API_KEY`（直连 Google，生图同样需要），可选 `GEMINI_API_BASE` + `GEMINI_API_BASE_KEY`
  - GPT / Claude：`OPENAI_API_BASE` + `OPENAI_API_KEY`
  - 三档模型：`MODEL_ROUTER` / `MODEL_LITE` / `MODEL_FAST` / `MODEL_PRO`

> 使用中转站时模型名裸写，不带 provider 前缀（如 `gpt-5.5`、`claude-sonnet-4-6`）。中转站的账号池和模型别名会变化，改模型名前先用 `curl {OPENAI_API_BASE}/models` 核对，改完做一次端到端对话验证。

### 2. 构建并启动

```bash
docker compose up -d --build
docker compose -f docker-compose.openai.yml up -d --build
docker compose -f docker-compose.anthropic.yml up -d --build
```

### 3. 验证

```bash
docker ps --filter name=dingtalk-ai-bot
docker logs --tail 50 dingtalk-ai-bot-anthropic

curl http://localhost:35000/     # {"status": "ok", ...}
curl http://localhost:35001/
curl http://localhost:35002/
```

最后在钉钉里 @ 每个机器人发一条消息做端到端验证。只看到容器启动成功，不代表模型名和上游 key 都可用。

## 环境变量要点

完整变量说明见 [README.md](README.md#核心环境变量) 和 `app/config.py`。部署时容易出错的几项：

| 变量 | 注意事项 |
|---|---|
| `BOT_ID` | compose 已为每个容器设置了不同值。它是历史消息归属、Soul 文件名（`{BOT_ID}__{cid}.md`）、`/clear` cutoff 的持久化键，**不要随意改**；多个容器共用同一个值会让角色重塑失效 |
| `GEMINI_API_BASE` | 填中转站的 Gemini 原生协议入口，SDK 会自动拼接 `/v1beta`。**路径前缀取决于中转站**：sub2api 需要带 `/antigravity`（如 `https://<中转站域名>/antigravity`），不带会返回 400 `API key group platform is not gemini`；标准 `/v1beta` 协议层的中转站则不能带后缀。中转站在公网时用 `https`，`http` 会被 302 跳转导致 POST 失败 |
| `GEMINI_API_KEY` | 保持为 Google 直连 key。生图的 `:predict` 端点中转站不覆盖，始终直连 |
| `OPENAI_{FLASH,PRO}_SUPPORTS_SEARCH` | 只有上游真正执行 Responses `web_search` 时才会有搜索效果。不同中转站对不同上游的处理不一样（如 sub2api 上的 Claude 会在服务端搜索并把结果拼进正文，但不产生标准搜索事件），换中转站后需要重新实测 |
| `SEARCH_FALLBACK_PROVIDER` | 默认 `none`。设为 `gemini` 会启用旧的"Gemini 搜索摘要注入"兼容路径 |
| `OPENROUTER_PROVIDER_ORDER` | 仅原生 `AI_BACKEND=openrouter` 使用，默认 `Anthropic`；改成其他 provider 时 prompt cache 可能被静默忽略 |
| `GEMINI_API_BASE_FALLBACK` / `GEMINI_API_BASE_FALLBACK_KEY` | Gemini 主路径熔断后的保底路径，需要显式配置独立 key，见 `docs/deploy/gemini-circuit-breaker-rollout.md` |

## 更新

```bash
cd /opt/1panel/docker/compose/dingtalk-ai-bot
git pull origin master

docker compose up -d --build
docker compose -f docker-compose.openai.yml up -d --build
docker compose -f docker-compose.anthropic.yml up -d --build
```

- **改了代码**：必须加 `--build`。`git pull` 不等于镜像已经更新，可以用 `docker exec <容器> cat /app/<文件>` 核对容器内的代码
- **只改了 `.env*`**：`docker compose -f <文件> up -d` 即可（Compose 按配置 hash 重建容器）。**`docker restart` 不会重新读取 env_file**
- 容器重建会丢失之前用 `docker cp` 热推进去的文件

## 故障排查

### 机器人不回复，但容器在运行

钉钉 Stream 重连后可能出现心跳正常、却不再处理消息的情况。先看最后一条业务日志的时间，而不是只看进程状态：

```bash
docker logs --tail 100 dingtalk-ai-bot-gemini
docker restart dingtalk-ai-bot-gemini    # 不涉及 env 变更时，restart 即可恢复
```

### 某个档位报 `unknown provider for model` / 404

中转站的模型别名发生了变化。核对模型列表后更新对应的 `MODEL_*`，然后 `up -d`：

```bash
curl -H "Authorization: Bearer <key>" https://<中转站域名>/v1/models
```

### Gemini 容器无法连接

```bash
# 直连 Google 时检查代理
docker exec dingtalk-ai-bot-gemini curl -x socks5h://127.0.0.1:1080 https://generativelanguage.googleapis.com
systemctl status v2raya
```

### 端口冲突

确认没有同时启动 Deprecated 的 openclaw / wecom 容器（见上文端口表）。

## 从 openrouter 迁移到 anthropic

2026-08-25 起 Claude 容器由 `openrouter` 改名为 `anthropic`。已有部署需要**同时**完成以下四项，缺一项都会导致这个机器人"失忆"：

1. 停止旧容器：`docker compose -f docker-compose.openrouter.yml down`（在旧版本代码下执行）
2. 重命名 env 文件：`mv .env.openrouter .env.anthropic`
3. 重命名数据目录和 Soul 文件：`mv data-openrouter data-anthropic`，并把目录内的 `openrouter__cid*.md` 重命名为 `anthropic__cid*.md`（否则 Soul 人格需要从头进化）
4. 迁移 MySQL 历史归属（执行前先备份）：

   ```sql
   UPDATE conversation_history
   SET bot_id = REPLACE(bot_id, 'openrouter', 'anthropic')
   WHERE bot_id LIKE '%openrouter%';
   ```

   不迁移的话，角色重塑会把这个机器人自己的历史回复当成"其他机器人"说的。

5. 迁移 Redis 历史缓存（`dingtalk_gemini:history:*`）中的 `bot_id`：读历史时**优先读 Redis**，而每写入一条消息都会把缓存 TTL 续到 7 天。只要群里还有人聊天，旧缓存就不会过期，也不会从已迁移的 MySQL 重建。改写时先备份原值，用 `WATCH` 事务避免覆盖并发写入，并用 `SET ... KEEPTTL` 保留 TTL。也可以直接删掉这些缓存 key 让它从 MySQL 回填，但 MySQL 写失败时只存在于缓存里的消息会丢失。
6. 迁移 `/clear` 软清空点：把数据目录 `clear_cutoff/` 下的 `openrouter__*.json` 复制为 `anthropic__*.json`（`cp -n`），否则这些群的 `/clear` 状态会失效。

`responses_state/` 与采样覆盖（`sample:*`）自带 7 天 / 24 小时 TTL，而且 Claude 走 `store=False` 不使用 `previous_response_id`，不需要迁移。

完成后执行 `docker compose -f docker-compose.anthropic.yml up -d --build`。

## 卸载

```bash
docker compose -f docker-compose.anthropic.yml down
docker compose -f docker-compose.openai.yml down
docker compose down
```

数据目录（`./data*`）不会被删除，需要时请手动备份后再清理。

## Deprecated 部署

OpenClaw 与企业微信的历史部署文档仍保留，但不再维护：

- OpenClaw：[OPENCLAW_SETUP.md](OPENCLAW_SETUP.md)、[OPENCLAW_DEPLOYMENT.md](OPENCLAW_DEPLOYMENT.md)
- 企业微信：[WECOM_DEPLOYMENT.md](WECOM_DEPLOYMENT.md)

## 参考

- [钉钉机器人开发文档](https://open.dingtalk.com/document/robots/robot-overview)
- [Google Gemini API 文档](https://ai.google.dev/gemini-api/docs)
- [OpenAI Responses API 文档](https://platform.openai.com/docs/api-reference/responses)
