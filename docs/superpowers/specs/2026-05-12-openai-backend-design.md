# OpenAI 兼容后端设计文档

**日期**: 2026-05-12
**状态**: Codex 评审后修订
**关联**: 替代方案讨论记录见本对话上下文

## 1. 目标

新增独立容器，通过 LiteLLM SDK 接入任意 OpenAI 兼容模型（DeepSeek、Qwen、GLM、Ollama 等），复用现有智能路由和钉钉消息处理基础设施。

## 2. 约束与决策

| 决策项 | 选择 | 理由 |
|--------|------|------|
| 部署形态 | 独立容器，独立 compose 文件 | 与 openclaw 模式一致，互不干扰 |
| 客户端 | LiteLLM SDK | 统一 100+ provider，支持 thinking/search 归一化 |
| 智能路由 | 复用现有三级漏斗 | 预分析仍用 gemini-3.1-flash-lite |
| 运行模式 | 完全独立 | 自含钉钉 Stream 接入 + 消息缓冲 + 路由 + 模型调用 + 卡片输出 |
| 作用域 | 仅钉钉/企微消息处理 | `/v1/chat/completions` API 路由不在本次范围内，仍走 Gemini |

## 3. 架构

```
新容器 (AI_BACKEND=openai)
├── 钉钉 Stream 接入       ← 复用 dingtalk_bot.py
├── 企微 Webhook 接入      ← 复用 wecom/callback.py
├── 消息缓冲 (2s 窗口)     ← 复用 ai/buffer.py
├── 智能路由 (预分析)       ← 复用 gemini_client.py::analyze_complexity_with_model()
│   └── gemini-3.1-flash-lite 做预分析（需 GEMINI_API_KEY）
├── 模型调用                ← 新增 litellm_client.py ★
│   └── LiteLLM SDK → 任意 OpenAI 兼容 provider
├── 钉钉卡片输出            ← 复用 dingtalk_card.py
└── 历史记录                ← 复用 memory.py + database.py
```

三个后端容器共用 `network_mode: host` + 端口 35000，同一时间只能运行一个。

## 4. 核心接口：litellm_client.py

```python
async def call_litellm_stream(
    messages: List[Dict[str, Any]],
    target_model: str,              # LiteLLM 格式: "deepseek/deepseek-chat"
    thinking_level: str = "low",    # minimal/low/medium/high
    enable_search: bool = False,    # Google Search grounding（仅 Gemini 模型）
) -> AsyncGenerator[Dict[str, str], None]:
```

### 4.1 参数映射与 Capability 检查

模型映射表带 capability 声明，调用前按能力决定传哪些参数：

```python
LITELLM_MODEL_CONFIG = {
    "fast": {
        "model": os.getenv("OPENAI_MODEL_FLASH", "deepseek/deepseek-chat"),
        "supports_reasoning": True,      # 是否传 reasoning_effort
        "supports_search": False,        # 是否支持 Google Search tool
        "supports_vision": True,         # 是否支持图片输入
    },
    "pro": {
        "model": os.getenv("OPENAI_MODEL_PRO", "deepseek/deepseek-reasoner"),
        "supports_reasoning": True,
        "supports_search": False,
        "supports_vision": True,
    },
}
```

**调用时按 capability 过滤参数：**

```python
config = get_model_config(route_key)  # "fast" or "pro"

kwargs = {
    "model": config["model"],
    "messages": messages,
    "stream": True,
}

# 仅支持 reasoning 的 provider 才传 reasoning_effort
if config["supports_reasoning"] and thinking_level != "minimal":
    kwargs["reasoning_effort"] = effort_mapping[thinking_level]

# 仅支持 search 的 provider 才传 Google Search tool
if config["supports_search"] and enable_search:
    kwargs["tools"] = [{"googleSearch": {}}]

# 不支持 vision 的 provider：过滤掉图片消息，转成文本描述
if not config["supports_vision"]:
    messages = _strip_images(messages)
```

**thinking_level → LiteLLM reasoning_effort：**

| thinking_level | reasoning_effort | 说明 |
|----------------|-----------------|------|
| minimal | 不传此参数 | 不请求推理 |
| low | low | 轻度推理 |
| medium | medium | 中度推理 |
| high | high | 深度推理 |

### 4.2 网络控制：代理、超时、重试

不依赖 main.py 的 monkey patch，在 litellm_client.py 内显式控制：

```python
import litellm

# 代理：从 SOCKS_PROXY 派生
litellm_client = AsyncOpenAI(
    base_url=OPENAI_API_BASE or None,   # 支持自建 endpoint
    api_key=OPENAI_API_KEY or "sk-placeholder",
    http_client=httpx.AsyncClient(
        proxy=LITELLM_PROXY,            # socks5://127.0.0.1:1080
        timeout=httpx.Timeout(
            connect=LITELLM_CONNECT_TIMEOUT,    # 默认 30s
            read=LITELLM_READ_TIMEOUT,          # 默认 120s
            write=30.0,
            pool=30.0,
        ),
    ),
)

# 调用时显式传 timeout + num_retries
response = await litellm.acompletion(
    **kwargs,
    timeout=LITELLM_READ_TIMEOUT,
    num_retries=LITELLM_MAX_RETRIES,   # 默认 2
)
```

### 4.3 返回格式

与 `call_gemini_stream` 完全一致：

```python
{"content": "..."}           # 正式回复
{"thinking": "..."}          # 推理过程（reasoning_content 归一化）
{"thinking_start": True}     # thinking 区段标记
{"thinking_end": True}
{"usage": {"model": "...", "input_tokens": N, "output_tokens": N, "latency_ms": N}}
{"error": "..."}
```

### 4.4 流式解析

```python
async for chunk in response:
    if not chunk.choices:
        continue

    delta = chunk.choices[0].delta

    # 推理内容（DeepSeek reasoning_content / Gemini thinking）
    if getattr(delta, "reasoning_content", None):
        if not thinking_sent:
            yield {"thinking_start": True}
            thinking_sent = True
        yield {"thinking": delta.reasoning_content}

    # 正文内容
    if getattr(delta, "content", None):
        if thinking_sent:
            yield {"thinking_end": True}
            thinking_sent = False
        yield {"content": delta.content}

    # Token 统计（最后一条 chunk）
    if hasattr(chunk, "usage") and chunk.usage:
        yield {"usage": {
            "model": config["model"],
            "input_tokens": chunk.usage.prompt_tokens or 0,
            "output_tokens": chunk.usage.completion_tokens or 0,
            "latency_ms": latency_ms,
        }}
```

## 5. 模型映射

智能路由输出的模型名需要先归一化为路由键（route key），再映射到目标 provider 模型：

```python
# config.py

# 路由名归一化：把各种别名统一到 "fast" / "pro"
ROUTE_KEY_MAP = {
    "gemini-3-flash-preview": "fast",
    "gemini-3-flash": "fast",
    "gemini-3.1-pro-preview": "pro",
    "gemini-3-pro-preview": "pro",
}

def get_route_key(target_model: str) -> str:
    key = ROUTE_KEY_MAP.get(target_model)
    if key is None:
        print(f"⚠️ 未知模型 {target_model}，降级到 fast")
        return "fast"
    return key

def get_model_config(route_key: str) -> dict:
    return LITELLM_MODEL_CONFIG.get(route_key, LITELLM_MODEL_CONFIG["fast"])
```

**切换 provider 只需改环境变量：**

| 场景 | OPENAI_MODEL_FLASH | OPENAI_MODEL_PRO | API KEY 环境变量 |
|------|-------------------|------------------|-----------------|
| DeepSeek | `deepseek/deepseek-chat` | `deepseek/deepseek-reasoner` | `DEEPSEEK_API_KEY` |
| Qwen | `qwen/qwen-plus` | `qwen/qwen-max` | `DASHSCOPE_API_KEY` |
| GLM | `glm/glm-4-flash` | `glm/glm-4-plus` | `GLM_API_KEY` |
| 本地 Ollama | `ollama_chat/qwen3:8b` | `ollama_chat/qwen3:32b` | (无需 key) |
| Gemini via LiteLLM | `gemini/gemini-3-flash` | `gemini/gemini-3-pro` | `GEMINI_API_KEY` |
| 自建 endpoint | `openai/model-name` | `openai/model-name` | `OPENAI_API_KEY` + `OPENAI_API_BASE` |

## 6. Handler 集成

### 6.1 统一入口函数（新增）

在 `app/ai/backend.py`（新文件）中统一后端选择逻辑，供 handler 和 dingtalk_bot 共同调用：

```python
# app/ai/backend.py

async def create_backend_stream(
    messages: List[Dict],
    target_model: str,
    thinking_level: str,
    enable_search: bool,
    **kwargs,
) -> AsyncGenerator[Dict, None]:
    """统一后端入口，handler 和 dingtalk_bot 都调用此函数"""

    if AI_BACKEND == "openclaw":
        from app.openclaw_client import call_openclaw_stream
        async for chunk in call_openclaw_stream(messages, **kwargs):
            yield chunk

    elif AI_BACKEND == "openai":
        from app.litellm_client import call_litellm_stream
        route_key = get_route_key(target_model)
        config = get_model_config(route_key)
        async for chunk in call_litellm_stream(
            messages, config, thinking_level, enable_search
        ):
            yield chunk

    else:  # gemini
        from app.gemini_client import call_gemini_stream
        async for chunk in call_gemini_stream(
            messages, target_model, thinking_level, enable_search
        ):
            yield chunk
```

### 6.2 handler.py / dingtalk_bot.py 改动

```python
# 改前（两处各自 if/elif/else）
if AI_BACKEND == "openclaw":
    stream = call_openclaw_stream(...)
else:
    stream = call_gemini_stream(...)

# 改后（统一调用）
from app.ai.backend import create_backend_stream
stream = create_backend_stream(messages, target_model, thinking_level, need_search, **kwargs)
```

## 7. 配置

### 7.1 .env.openai（完整版）

```env
# === 后端 ===
AI_BACKEND=openai
PLATFORM=dingtalk

# === 目标 Provider ===
DEEPSEEK_API_KEY=sk-xxx

# === 模型映射（LiteLLM provider/model 格式）===
OPENAI_MODEL_FLASH=deepseek/deepseek-chat
OPENAI_MODEL_PRO=deepseek/deepseek-reasoner

# === 自定义 Endpoint（可选，覆盖 LiteLLM 默认 endpoint）===
# OPENAI_API_BASE=https://api.my-gateway.com/v1
# OPENAI_API_KEY=sk-xxx

# === 网络控制 ===
SOCKS_PROXY=socks5h://172.16.0.8:1080
LITELLM_CONNECT_TIMEOUT=30
LITELLM_READ_TIMEOUT=120
LITELLM_MAX_RETRIES=2

# === 预分析（仍需 Gemini）===
GEMINI_API_KEY=xxx

# === 钉钉 ===
DINGTALK_CLIENT_ID=xxx
DINGTALK_CLIENT_SECRET=xxx

# === 企微（如需双平台）===
# PLATFORM=both
# WECOM_BOT_TOKEN=xxx
# WECOM_BOT_ENCODING_AES_KEY=xxx
# WECOM_BOT_CORP_ID=xxx

# === 数据层 ===
REDIS_HOST=127.0.0.1
REDIS_PORT=36379
```

### 7.2 config.py 新增

```python
# LiteLLM 后端
OPENAI_MODEL_FLASH = os.getenv("OPENAI_MODEL_FLASH", "deepseek/deepseek-chat")
OPENAI_MODEL_PRO = os.getenv("OPENAI_MODEL_PRO", "deepseek/deepseek-reasoner")
OPENAI_API_BASE = os.getenv("OPENAI_API_BASE", "")       # 自定义 endpoint
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")          # 自定义 key

# 网络控制
LITELLM_PROXY = SOCKS_PROXY.replace("socks5h://", "socks5://") if SOCKS_PROXY else None
LITELLM_CONNECT_TIMEOUT = _get_int("LITELLM_CONNECT_TIMEOUT", 30)
LITELLM_READ_TIMEOUT = _get_int("LITELLM_READ_TIMEOUT", 120)
LITELLM_MAX_RETRIES = _get_int("LITELLM_MAX_RETRIES", 2)

# 模型映射（带 capability）
LITELLM_MODEL_CONFIG = {
    "fast": {
        "model": OPENAI_MODEL_FLASH,
        "supports_reasoning": _get_bool("OPENAI_FLASH_SUPPORTS_REASONING", True),
        "supports_search": _get_bool("OPENAI_FLASH_SUPPORTS_SEARCH", False),
        "supports_vision": _get_bool("OPENAI_FLASH_SUPPORTS_VISION", True),
    },
    "pro": {
        "model": OPENAI_MODEL_PRO,
        "supports_reasoning": _get_bool("OPENAI_PRO_SUPPORTS_REASONING", True),
        "supports_search": _get_bool("OPENAI_PRO_SUPPORTS_SEARCH", False),
        "supports_vision": _get_bool("OPENAI_PRO_SUPPORTS_VISION", True),
    },
}

# 路由名归一化
ROUTE_KEY_MAP = {
    "gemini-3-flash-preview": "fast",
    "gemini-3-flash": "fast",
    "gemini-3.1-pro-preview": "pro",
    "gemini-3-pro-preview": "pro",
}
```

## 8. Docker Compose

### docker-compose.openai.yml

```yaml
services:
  dingtalk-ai-bot-openai:
    build:
      context: .
      dockerfile: Dockerfile
    image: dingtalk-ai-bot:openai
    container_name: dingtalk-ai-bot-openai
    restart: always
    network_mode: "host"
    environment:
      - AI_BACKEND=openai
      - FLASK_PORT=35000
    env_file:
      - .env.openai
    volumes:
      - ./data-openai:/app/data
    deploy:
      resources:
        limits:
          memory: 512M
```

## 9. 文件变更清单

| 操作 | 文件 | 改动量 | 说明 |
|------|------|--------|------|
| 新增 | `app/litellm_client.py` | ~150 行 | LiteLLM 流式客户端（含代理/超时/retry） |
| 新增 | `app/ai/backend.py` | ~30 行 | 统一后端入口，消除双路径漂移 |
| 新增 | `docker-compose.openai.yml` | ~20 行 | 容器编排 |
| 新增 | `.env.openai` | ~30 行 | 环境变量模板（含能力声明） |
| 修改 | `app/config.py` | +35 行 | LiteLLM 配置 + 模型映射 + capability |
| 修改 | `app/ai/handler.py` | ~10 行 | 改用 create_backend_stream |
| 修改 | `app/dingtalk_bot.py` | ~10 行 | 改用 create_backend_stream |
| 修改 | `requirements.txt` | +1 行 | `litellm==1.81.9` |

不动：`gemini_client.py`、`openclaw_client.py`、`dingtalk_card.py`、`memory.py`、`database.py`、`ai/router.py`、`routes.py`。

## 10. 部署

```bash
# 1. 本地提交 + push
git add ... && git commit -m "feat: add litellm backend" && git push origin master

# 2. 服务器拉代码
ssh tencent_cloud_server "cd /opt/1panel/docker/compose/dingtalk-ai-bot && git pull origin master"

# 3. 显式停所有可能运行的后端
ssh tencent_cloud_server "cd /opt/1panel/docker/compose/dingtalk-ai-bot && \
  docker compose down 2>/dev/null; \
  docker compose -f docker-compose.openclaw.yml down 2>/dev/null; \
  docker compose -f docker-compose.wecom.yml down 2>/dev/null; \
  docker ps --filter name=dingtalk-ai-bot"

# 4. 构建启动
ssh tencent_cloud_server "cd /opt/1panel/docker/compose/dingtalk-ai-bot && \
  docker compose -f docker-compose.openai.yml up -d --build"

# 5. 验证
ssh tencent_cloud_server "docker ps --filter name=dingtalk-ai-bot && \
  docker logs --tail 30 dingtalk-ai-bot-openai && \
  curl -s http://localhost:35000/"
```

## 11. /v1/chat/completions 路由声明

`routes.py` 的 `/v1/chat/completions` 端点在 `AI_BACKEND=openai` 时仍直连 Google Gemini endpoint，这是已知行为。本次设计不改动此路由，原因：

- 该端点用于外部 API 调用，与钉钉/企微消息处理无关
- 如需 openai 后端也支持此端点，可单独迭代，改动量小（替换 `routes.py` 内的 Gemini 直连为 LiteLLM 调用）

## 12. 未来演进

- **多 Provider 路由**：扩展 `LITELLM_MODEL_CONFIG` 增加 `"code"` / `"vision"` 等路由键
- **替代 gemini_client.py**：LiteLLM 支持 Gemini thinking + search，长期可用 `litellm_client.py` 统一所有后端
- **LiteLLM Proxy**：如需管理多个 API KEY 或做负载均衡，可部署独立 LiteLLM proxy 容器
- **routes.py 统一**：将 `/v1/chat/completions` 也接入 `create_backend_stream`
