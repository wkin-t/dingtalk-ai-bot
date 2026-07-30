# Gemini 主路径熔断与 Vertex 保底上线说明

本文只描述本任务相关的配置核对、验证和回滚边界，不包含任何真实 key、生产地址或账号材料。

## 配置约定

主路径继续使用 antigravity 的 `GEMINI_API_BASE` 与 `GEMINI_API_BASE_KEY`。Vertex 保底单独配置：

```text
GEMINI_API_BASE_FALLBACK=<Vertex/sub2api fallback base>
GEMINI_API_BASE_FALLBACK_KEY=<explicit fallback key>
SEARCH_FALLBACK_PROVIDER=none
```

`GEMINI_API_BASE_FALLBACK` 非空时必须同时提供显式 `GEMINI_API_BASE_FALLBACK_KEY`；应用不会复用直连 Google key。fallback 默认沿用主模型名。当前 Vertex 路径已配置 `gemini-3.6-flash-tiered` → `gemini-3.6-flash` 的别名，因此不需要在应用内维护模型转换表。只有确有需要时，才为明确档位设置 `MODEL_ROUTER_FALLBACK`、`MODEL_LITE_FALLBACK`、`MODEL_FAST_FALLBACK` 或 `MODEL_PRO_FALLBACK`。

熔断状态保存在：

```text
gemini_circuit:{BOT_ID}:antigravity
```

TTL 固定 600 秒。Redis 仅是保护性状态存储，读写失败按 fail-open 处理；fallback 成功不会清除或延长已有 marker。

## 上线前顺序

1. 先核对最终 Compose/env 解析结果中的 fallback base、fallback key 是否显式存在，以及 `SEARCH_FALLBACK_PROVIDER=none`；只确认存在，不打印值。
2. 使用无敏感 prompt 做 fallback-only canary：直接调用 fallback base/key，记录命令退出码、HTTP/SDK 成功、实际模型名和非敏感错误摘要。容器启动或 env 存在不能替代真实请求证据。
3. 配置变更后使用项目约定的 `docker compose up -d`/recreate 流程，让 `env_file` 被重新读取；仅 `docker restart` 不足以应用 env 变化。
4. 生产观察分开记录：主路径成功请求、Vertex fallback 成功、熔断命中/TTL、钉钉 footer，以及旧搜索开关。不要为了验收人为打坏 antigravity。

## 回滚

只修改本任务相关键，并重新执行 Compose recreate：

- 熔断/Vertex 配置回滚：移除或恢复 `GEMINI_API_BASE_FALLBACK`、`GEMINI_API_BASE_FALLBACK_KEY` 及本任务的 `MODEL_*_FALLBACK` 键；代码回滚遵循 Git 可追溯流程。
- 旧搜索路径回滚：单独把 `SEARCH_FALLBACK_PROVIDER` 从 `none` 改为 `gemini`。这只恢复旧的摘要注入兼容路径，不影响模型 SDK 自身的原生搜索工具。

不要用恢复整份 `.env` 的方式回滚，以免覆盖其他合法配置。
