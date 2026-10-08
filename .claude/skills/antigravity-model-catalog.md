---
name: antigravity-model-catalog
description: 按 sub2api 的方式读取 antigravity OAuth 账号的官方模型目录（直连 Google fetchAvailableModels，经代理，不过滤）。用于判断某个模型（如 Claude 5.5）在上游账号里是否可见，排查中转站 model_not_found 是否属于账号侧问题。
argument-hint: "<账号标识，由用户提供>"
user-invocable: true
allowed-tools:
  - Bash
  - Read
---

# Antigravity 官方模型目录读取

按 sub2api 的 `fetchAntigravityOAuthUpstreamModels` 同样的流程，读取一个 antigravity OAuth 账号在 Google 侧能看到的模型 ID。

## 何时使用

- 中转站 `/antigravity/v1/messages` 返回 `model_not_found`（"not supported by any configured account in this group"），需要判断是模型名不对，还是上游账号本身看不到该模型。
- 需要确认 Claude 5.5 / Gemini 新版本是否已对账号开放。

**不用于**：查中转站自己的模型列表（那是 `/antigravity/v1/models`，见 `feedback_model_list_via_antigravity` memory）。

## 前提

- 账号的 `access_token` 与 `project_id` 属于敏感凭据。**每次执行前必须得到用户明确授权**，且只在内存或管道中使用，不打印到对话，不写入任何文件。
- 凭据由用户指定来源（例如 sub2api 后台导出，或用户自己提供的值）。本 skill 不规定读取凭据的具体命令。

## 流程

与 sub2api 源码保持一致（`backend/internal/service/upstream_models.go`、`backend/internal/pkg/antigravity/client.go`、`backend/internal/pkg/antigravity/oauth.go`）：

1. **端点顺序**：先 `https://cloudcode-pa.googleapis.com`（prod），失败（网络错误或非 200）再试 `https://daily-cloudcode-pa.googleapis.com`（daily）。成功即停止。
2. **请求**：`POST {base}/v1internal:fetchAvailableModels`，请求体 `{"project": "<project_id>"}`，头 `Authorization: Bearer <access_token>`、`Content-Type: application/json`、`User-Agent: antigravity`。
3. **代理**：经 `<代理地址>` 出网（即 CLIProxyAPI 配置中的 `proxy-url`，具体值见服务器运维配置，不写入本文件）。服务器宿主机与 sub2api 容器直连 Google 不通，必须走代理。
4. **结果**：取响应 `models` 对象的 key，去空白、去重、排序。**不做过滤**。sub2api 会保留 `chat_*`、`tab_*` 等内部名；CLIProxyAPI 会过滤，二者不要混用。

请求模板（token 通过环境变量或管道传入，不写进命令历史）：

```bash
curl -s -m 30 -x "$PROXY" -X POST "$BASE/v1internal:fetchAvailableModels" \
  -H "Authorization: Bearer $ACCESS_TOKEN" -H "Content-Type: application/json" -H "User-Agent: antigravity" \
  -d "{\"project\": \"$PROJECT_ID\"}" -w "\nHTTP_CODE:%{http_code}\n"
```

解析时用 Python 取 `models` 的 key，只输出 ID 列表和状态码。

## 结果解读

- **HTTP 401 `UNAUTHENTICATED`**：access_token 已过期。sub2api 会在同步时用 refresh_token 续期，直接取到的旧值会失效。处理方式：让用户在 sub2api 后台对该账号重新授权或触发刷新，然后用新值重新执行。
- **HTTP 200 但目标模型不在列表里**：上游账号在 Google 侧看不到该模型。中转站的 `model_not_found` 与之一致，属于账号侧问题，不是模型名拼写或中转站丢失。
- **两个 base 都失败**：检查代理（`$PROXY`）是否在线，以及 sub2api 容器能否访问该代理。
- **模型 ID 格式**：Claude 5.5 的 antigravity 官方 ID 带 `-high` 后缀，如 `claude-opus-5-5-high`、`claude-sonnet-5-5-high`（来源：router-for-me/CLIProxyAPI 的 `internal/registry/models/models.json`）。不带后缀的名字不是 antigravity 官方 ID。

## 已知结论（时间点快照，2026-10-08）

- 两个 antigravity OAuth 账号（均重新授权过）返回的目录完全相同，共 27 个 ID。
- Claude 仅 `claude-opus-4-6-thinking`、`claude-sonnet-4-6`，无 `*-5-5`。
- Gemini 含 `gemini-3.8-flash-tiered`，无 `gemini-3.8-flash-high`。

快照会过期，执行时以实际输出为准，不要直接引用这里的结论。

## 相关

- sub2api 源码：`backend/internal/service/upstream_models.go`（同步入口）、`backend/internal/pkg/antigravity/client.go`（`FetchAvailableModels`）、`backend/internal/pkg/antigravity/oauth.go`（`BaseURLs` 顺序）。
- CLIProxyAPI 对照：`cmd/fetch_antigravity_models/main.go`（会过滤内部模型）、`internal/registry/models/models.json`（官方 ID 快照）。
- 中转站架构：`project_s2a_relay_setup` memory。
