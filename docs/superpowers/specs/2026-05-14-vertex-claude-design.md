# Vertex AI Claude 集成设计

日期: 2026-05-14
状态: Draft（经过两轮 codex 对抗性评审）

## 概述

通过现有 LiteLLM 客户端接入 Google Cloud Vertex AI 上的 Claude 模型。
LiteLLM 只在 `AI_BACKEND=openai` 时被调用，因此 Vertex Claude 本质上是 **OpenAI 后端的 provider 配置**，不需要额外安装 SDK。

## 模型矩阵

| 路由 key | 模型 | 区域 | 用途 |
|----------|------|------|------|
| fast | `vertex_ai/claude-sonnet-4@20250514` | europe-west1 | 日常对话 |
| pro | `vertex_ai/claude-opus-4-1@20250514` | us-east5 | 复杂任务 |

注意：不同模型在不同区域可用，需要 per-model region 配置。
本次不做 haiku 路由（路由器不会输出 haiku key，不可达）。

## 认证

- 方式：Service Account JSON（GCP Console 生成）
- 宿主机存放：`secrets/vertex-sa.json`（已 `.gitignore` + `.dockerignore` 排除）
- Docker 部署：通过 compose 只读 volume 挂载到容器内 `/run/secrets/gcp-key.json`
- 本地开发：`GOOGLE_APPLICATION_CREDENTIALS` 指向 `secrets/vertex-sa.json`
- LiteLLM 通过 `GOOGLE_APPLICATION_CREDENTIALS` 环境变量自动读取

### 双变量设计（解决宿主/容器路径语义矛盾）

- `VERTEX_SA_HOST_PATH`：宿主机上的 SA JSON 绝对路径，仅用于 compose volume 挂载
- `GOOGLE_APPLICATION_CREDENTIALS`：容器内路径 `/run/secrets/gcp-key.json`（本地开发时指向 `secrets/vertex-sa.json`）

## 改动文件

### 1. app/config.py

新增环境变量：

```python
# Vertex AI 配置
VERTEX_PROJECT = os.getenv("VERTEX_PROJECT", "")
```

扩展 `LITELLM_MODEL_CONFIG`，增加 `region` 和 `reasoning_param` 字段：

```python
LITELLM_MODEL_CONFIG = {
    "fast": {
        "model": LITELLM_MODEL_FLASH,
        "region": os.getenv("VERTEX_REGION_FAST", "europe-west1"),
        "supports_reasoning": True,
        "supports_search": False,
        "supports_vision": True,
        "reasoning_param": "anthropic_thinking",  # "openai_effort" | "anthropic_thinking" | "none"
    },
    "pro": {
        "model": LITELLM_MODEL_PRO,
        "region": os.getenv("VERTEX_REGION_PRO", "us-east5"),
        "supports_reasoning": True,
        "supports_search": False,
        "supports_vision": True,
        "reasoning_param": "anthropic_thinking",
    },
}
```

`ROUTE_KEY_MAP` 无需改动——路由器输出 `gemini-3-flash` → `fast`，`gemini-3.1-pro` → `pro`，模型由环境变量覆盖。

### 2. app/litellm_client.py

**必须更新 import**：

```python
from app.config import (
    ...,
    VERTEX_PROJECT,  # 新增
)
```

Provider 互斥的 kwargs 构建（替换现有的 OPENAI_API_BASE 逻辑）：

```python
if config["model"].startswith("vertex_ai/"):
    # Vertex AI 路径 — 互斥，不走 OpenAI 兼容逻辑
    kwargs["vertex_ai_project"] = VERTEX_PROJECT
    kwargs["vertex_ai_location"] = config.get("region", "us-east5")
    # thinking 参数适配
    reasoning_param = config.get("reasoning_param", "openai_effort")
    if reasoning_param == "anthropic_thinking":
        effort = EFFORT_MAPPING.get(thinking_level)
        if effort and effort != "none":
            budget = {"low": 2048, "medium": 8192, "high": 32768}.get(effort, 8192)
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}
elif OPENAI_API_BASE:
    kwargs["api_base"] = OPENAI_API_BASE
    kwargs["custom_llm_provider"] = "openai"
    if OPENAI_API_KEY_CUSTOM:
        kwargs["api_key"] = OPENAI_API_KEY_CUSTOM
    # 原有 reasoning_effort 逻辑
    effort = EFFORT_MAPPING.get(thinking_level)
    if config["supports_reasoning"] and effort is not None:
        kwargs["reasoning_effort"] = effort
```

流式解析扩展（兼容 thinking block）：

```python
# 现有: reasoning = getattr(delta, "reasoning_content", None)
# 新增: 也检测 thinking 字段
reasoning = getattr(delta, "reasoning_content", None) or getattr(delta, "thinking", None)
```

### 3. docker-compose.openai.yml

Vertex Claude 是 OpenAI 后端的 provider 配置，**只改这一个 compose 文件**。
gemini/openclaw 的 compose 不动，不受影响。

```yaml
services:
  bot:
    environment:
      - GOOGLE_APPLICATION_CREDENTIALS=/run/secrets/gcp-key.json
    volumes:
      - ./data:/app/data
      - ${VERTEX_SA_HOST_PATH:-/dev/null}:/run/secrets/gcp-key.json:ro
```

注意：`${VERTEX_SA_HOST_PATH:-/dev/null}` 确保未设置时不破坏启动。非 Vertex 部署不设此变量即可。

### 4. .dockerignore

已创建。凭证相关规则：

```
secrets/
*.sa.json
*.key.json
*service-account*.json
credentials/
```

### 5. .env.openai 配置模板

```env
# OpenAI 后端通用
AI_BACKEND=openai
OPENAI_API_BASE=
OPENAI_API_KEY=

# Vertex AI Claude（覆盖 OpenAI 默认模型）
VERTEX_PROJECT=vertex-485510
VERTEX_SA_HOST_PATH=./secrets/vertex-sa.json   # compose volume 挂载用
GOOGLE_APPLICATION_CREDENTIALS=/run/secrets/gcp-key.json  # 容器内路径

# 模型
OPENAI_MODEL_FLASH=vertex_ai/claude-sonnet-4@20250514
OPENAI_MODEL_PRO=vertex_ai/claude-opus-4-1@20250514

# 区域
VERTEX_REGION_FAST=europe-west1
VERTEX_REGION_PRO=us-east5
```

## 两轮 Codex 评审问题应对

### 第一轮

| # | 问题 | 严重度 | 应对 |
|---|------|--------|------|
| 1 | SA JSON 打进 Docker 镜像 | Critical | `.dockerignore` + 只读 volume |
| 2 | 容器内找不到凭证 | Critical | compose volume 映射 |
| 3 | OPENAI_API_BASE 冲突 | High | provider 互斥分支 |
| 4 | thinking 参数不兼容 | High | `reasoning_param` 字段 + budget 映射 |
| 5 | 流式解析缺 thinking | High | 兼容 `reasoning_content` + `thinking` |
| 6 | Gemini 路由依赖 | High | 保持现状，文档说明 |
| 7 | 全局代理副作用 | Medium | 不设全局 HTTP_PROXY |
| 8 | 错误处理太粗 | Medium | 暂不处理 |
| 9 | usage 缺失误报 | Medium | content_seen 判断 |
| 10 | vision 默认值 | Medium | 环境变量显式控制 |

### 第二轮

| # | 问题 | 严重度 | 应对 |
|---|------|--------|------|
| 1 | `.gitignore` 凭证模式不全 | Critical | 已补 `secrets/`、`*.sa.json`、`*.key.json` |
| 2 | compose volume 破坏非 Vertex 部署 | High | 只改 `docker-compose.openai.yml`，用 `${VAR:-/dev/null}` 兜底 |
| 3 | 目标 compose 文件搞错 | High | 修正：Vertex Claude 是 `AI_BACKEND=openai` 的 provider |
| 4 | `VERTEX_CREDENTIALS` 语义矛盾 | High | 拆成 `VERTEX_SA_HOST_PATH`（宿主）+ `GOOGLE_APPLICATION_CREDENTIALS`（容器内） |
| 5 | 缺 `VERTEX_PROJECT` import | High | spec 已加入 import 更新 |
| 6 | haiku route key 不可达 | Medium | 删出本次范围 |
| 7 | 漏掉 wecom 部署 | Medium | 本次不覆盖，明确写在范围外 |
| 8 | `.dockerignore` 模式过宽 | Low | 改用更精确的模式 |

## 向后兼容

- 不设 `VERTEX_PROJECT` 时，行为完全不变（走 OpenAI/DeepSeek）
- 只改 `docker-compose.openai.yml`，gemini/openclaw compose 不受影响
- 所有新配置项都有默认值，不破坏现有部署

## 不在本次范围

- LiteLLM Proxy Server 部署
- 新增 AI_BACKEND 模式
- 脱离 Gemini 路由的独立路由
- Provider-specific 错误分类和 fallback 策略
- haiku 模型路由（路由器不可达）
- 企业微信独立部署（wecom compose）的 Vertex 配置

## 上线前 Smoke Test

1. 纯文本对话（fast / pro 各一次）
2. Thinking 启用（pro + medium thinking level）
3. 图片消息（验证 vision 支持）
4. 凭证错误模拟（错误的 SA 路径）
5. 代理可达性（从部署服务器 curl Vertex endpoint）
6. 流式响应完整性（确认 usage 返回）
7. 不设 VERTEX_PROJECT 时确认走原有 DeepSeek 后端
