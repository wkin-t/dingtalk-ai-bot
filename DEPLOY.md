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
| dws CLI 与有效登录态 | 钉钉会话、引用、媒体及群成员读取 | 镜像安装固定版本；三个生产 compose 共享 `./data-dws:/app/data-dws`。成员提及在同一张流式卡片内渲染，不另发提醒；登录企业须与机器人企业一致 |
| Redis / MySQL | 统计、采样等应用状态及旧路径存储 | 钉钉会话不再读写此处的历史，也不会回退到 `data/history/` |
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

> 使用中转站时模型名裸写，不带 provider 前缀（如 `gpt-6.1-sol`、`claude-sonnet-5-5-low`）。中转站的账号池和模型别名会变化，改模型名前先核对对应分组的 `/models`，改完在额度允许时做一次端到端对话验证；暂不可调用时明确记录未完成上游执行验收。

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

最后在钉钉里分别验证 `@机器人 + 问题`、纯 `@机器人` 和 `/since`。涉及成员提醒时确认正文完整、@ 可点击；涉及 `/clear` 时确认只改变被指定机器人的可见范围，`/resume` 后仍受其历史条数上限限制。只看到容器启动成功，不代表会话读取、模型调用或客户端展示可用。

旧历史表初始化会尝试补齐 `external_msg_id` 列和幂等唯一索引。仅 MySQL 的重复列（1060）和重复索引（1061）视为已完成；其他迁移错误会让数据库初始化返回失败，并保留原有降级处理，不能据此宣称旧历史表已升级成功。MySQL DDL 可能部分落地，例如列已添加但索引失败；排查权限、锁等待或索引冲突后可幂等重试，代码回滚不会撤销已完成的 DDL。先备份再处理数据库，不手工忽略错误。钉钉 dws 历史不会因此回退到旧库。

### dws 登录与数据目录

先检查现有登录态，避免覆盖正在使用的账号：

```bash
docker exec dingtalk-ai-bot-anthropic dws auth status --readonly -f json
```

没有有效登录态时，通过 dws 官方登录流程登录与机器人一致的企业。镜像使用 `/app/data-dws/config` 和 `/app/data-dws/keychain`，三个容器共享同一挂载目录；这些目录含登录凭据，不提交、不打入镜像。`DWS_EXPECTED_CORP_ID` 可显式固定企业，未设置时逐请求使用 Stream 自带的 `chatbot_corp_id` 校验。

各 bot 的 Soul、采样文件和 `/clear` 起点仍位于各自 `/app/data` 挂载中。升级时保留 `data/`、`data-openai/`、`data-anthropic/` 和 `data-dws/`，不需要把旧 Redis / MySQL 历史导入 dws。

## 环境变量要点

完整变量说明见 [README.md](README.md#核心环境变量) 和 `app/config.py`。部署时容易出错的几项：

| 变量 | 注意事项 |
|---|---|
| `BOT_ID` | compose 已为每个容器设置了不同值。它是历史消息归属、Soul 文件名（`{BOT_ID}__{cid}.md`）、`/clear` cutoff 的持久化键，**不要随意改**；多个容器共用同一个值会让角色重塑失效 |
| `MAX_HISTORY_LENGTH` | 各 `.env*` 独立设置历史消息条数。2026-11-12 生产值：Gemini 50、GPT 50、Claude 25。先应用当前 bot 的 `/clear` 过滤，再取最近 N 条；不是 token 总量上限 |
| `DWS_CONTEXT_LIMIT` | 默认 40。钉钉主链路会按 `MAX_HISTORY_LENGTH` 和当前消息去重余量扩大窗口，单次读取最多 100 条；保持现有 50 / 50 / 25 配置即可 |
| `DWS_EXPECTED_CORP_ID` | 可选企业绑定。登录不可用或企业无法确认时停止读取，不使用旧库兜底；不要混用其他企业的共享登录态 |
| `GEMINI_API_BASE` | 填中转站的 Gemini 原生协议入口，SDK 会自动拼接 `/v1beta`。**路径前缀取决于中转站**：sub2api 需要带 `/antigravity`（如 `https://<中转站域名>/antigravity`），不带会返回 400 `API key group platform is not gemini`；标准 `/v1beta` 协议层的中转站则不能带后缀。中转站在公网时用 `https`，`http` 会被 302 跳转导致 POST 失败 |
| `GEMINI_API_KEY` | 保持为 Google 直连 key。生图的 `:predict` 端点中转站不覆盖，始终直连 |
| `OPENAI_{FLASH,PRO}_SUPPORTS_SEARCH` | 只有上游真正执行 Responses `web_search` 时才会有搜索效果。不同中转站对不同上游的处理不一样（如 sub2api 上的 Claude 会在服务端搜索并把结果拼进正文，但不产生标准搜索事件），换中转站后需要重新实测 |
| `SEARCH_FALLBACK_PROVIDER` | 默认 `none`。设为 `gemini` 会启用旧的"Gemini 搜索摘要注入"兼容路径 |
| `OPENROUTER_PROVIDER_ORDER` | 仅原生 `AI_BACKEND=openrouter` 使用，默认 `Anthropic`；改成其他 provider 时 prompt cache 可能被静默忽略 |
| `GEMINI_API_BASE_FALLBACK` / `GEMINI_API_BASE_FALLBACK_KEY` | Gemini 主路径熔断后的保底路径，需要显式配置独立 key，见 `docs/deploy/gemini-circuit-breaker-rollout.md` |

## 思考深度配置与验收

当前模型分工和支持档位以 [README 生产配置](README.md#当前生产部署) 与 [能力表](README.md#按模型区分思考深度) 为准。模型别名的 `-low` / `-high` 后缀不会决定实际请求深度，不能只改模型名就声称已关闭思考。

### 允许客户端实际下发字段

GPT 与 Claude 生产容器均使用 `AI_BACKEND=openai`，独立字段由下面的 capability 声明控制。2026-11-13 已在两个容器启用三档的 reasoning 声明；这些是生产部署值，不代表所有上游都默认接受该参数。

```dotenv
OPENAI_LITE_SUPPORTS_REASONING=true
OPENAI_FLASH_SUPPORTS_REASONING=true
OPENAI_PRO_SUPPORTS_REASONING=true
```

`FLASH` 对应 `MODEL_FAST`。设为 false 时，客户端不发送该档位的思考字段，卡片显示 `default`；这意味着交给上游默认处理，不是“关闭思考”。支持关闭的模型须实际发送 `reasoning.effort="none"`。Gemini 原生协议使用 `ThinkingConfig.thinking_level`；`include_thoughts=False` 只控制摘要回传。

`MODEL_ROUTER` 的能力不复用 lite 声明。可用 `OPENAI_ROUTER_SUPPORTS_REASONING=true/false` 单独声明；不设置时，客户端识别已知支持思考字段的家族，未知模型不自动加字段。启用后路由分析与后台简单调用使用该模型支持的最低档；不要把这里的参数误当成业务回复的固定深度。

### 覆盖单个模型的能力

仅在确认所用中转通道支持后，才在对应 `.env*` 添加覆盖。示例中的 `custom-model` 是配置格式示例，不是可直接调用的生产型号：

```dotenv
THINKING_LEVEL_OVERRIDES='{"openai:custom-model":["none","low","medium","high","xhigh","max"]}'
```

- `backend:完整模型名` 优先于不带后端的完整模型名；不会模糊匹配相似别名。
- 列表必须非空、无重复，并按 `none → low → medium → high → xhigh → max` 排序；不接受 `minimal` 作为能力声明。
- 已知模型默认不支持的档位也可以由受信运维配置覆盖，因此不能仅凭模型列表里有 `-disabled` 别名就添加 `none`。模型返回内容不能修改这份配置。
- 不支持的高档位会降到该模型支持且不高于请求的最高档；不支持关闭时使用最低支持档，不能标成已关闭。

### 应用、验证与回滚

1. 先备份对应 `.env*` 和当前镜像，只查看模型名、能力布尔值与覆盖规则，避免输出密钥。
2. 只改配置时用对应 compose 的 `up -d --no-deps --no-build --pull never <服务名>` 重新创建容器；修改代码则先构建镜像。`docker restart` 不读取新环境文件。
3. 核对运行中的模型分工和 capability 声明，再检查健康端点。字段被禁用时的 `default` 不能作为 none/low 生效证据。
4. 请求层回归：`python -m pytest -q tests/test_thinking_policy.py tests/test_thinking_config.py tests/test_thinking_dispatch.py tests/test_thinking_wire.py`。覆盖 Gemini Hydrogen xhigh、其他 Gemini 4 降级、GPT 显式 none、Claude/GPT max，以及能力开关。
5. 上游额度允许时，再做一次实际调用确认中转站接受并执行参数。额度耗尽时仅完成离线 HTTP 请求捕获与配置验证，记录未验证边界，不循环消耗额度探测。
6. 回滚时恢复环境备份、旧源码与旧镜像标签，再用相同 compose 定向 recreate；只回滚镜像不会撤销环境文件变更。

2026-11-13 的源码与环境回滚点为服务器项目下 `deployment-backups/20261113-thinking-capabilities/`，旧镜像标签为 `dingtalk-ai-bot:{gemini,openai,anthropic}-before-thinking-20261113`。Gemini 的 Hydrogen 模型分工已经包含在这份回滚点中。完整测试、部署和离线验收记录见 [本轮实施记录](docs/superpowers/plans/2026-11-13-model-thinking-capabilities.md)。

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

> 以下是 2026-08-25 的旧存储迁移记录。2026-11-12 起钉钉会话仅从 dws 读取，下面第 4、5 项数据库历史归属迁移仅适用于仍使用旧历史存储的路径，不是当前 dws 上线的必需操作。Soul 文件和 `/clear` 起点仍需保留正确的 `BOT_ID` 归属。

2026-08-25 起 Claude 容器由 `openrouter` 改名为 `anthropic`。当时使用旧历史存储的部署需要同时迁移以下内容：

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
