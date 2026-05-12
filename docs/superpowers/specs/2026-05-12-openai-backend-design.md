# OpenAI 兼容后端设计文档

**日期**: 2026-05-12
**状态**: 待评审
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

## 3. 架构

```
新容器 (AI_BACKEND=openai)
├── 钉钉 Stream 接入       ← 复用 dingtalk_bot.py
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

### 4.1 参数映射

**thinking_level → LiteLLM reasoning_effort：**

| thinking_level | reasoning_effort | 说明 |
|----------------|-----------------|------|
| minimal | disable | 不推理 |
| low | low | 轻度推理 |
| medium | medium | 中度推理 |
| high | high | 深度推理 |

**enable_search → LiteLLM tools：**

```python
tools = [{"googleSearch": {}}] if enable_search else []
```

仅在 target_model 为 Gemini 模型时生效，其他 provider 忽略。

### 4.2 返回格式

与 `call_gemini_stream` 完全一致：

```python
{"content": "..."}           # 正式回复
{"thinking": "..."}          # 推理过程（reasoning_content 归一化）
{"thinking_start": True}     # thinking 区段标记
{"thinking_end": True}
{"usage": {"model": "...", "input_tokens": N, "output_tokens": N, "latency_ms": N}}
{"error": "..."}
```

### 4.3 流式解析

```python
response = await litellm.acompletion(
    model=target_model,
    messages=messages,
    stream=True,
    reasoning_effort=effort_mapping[thinking_level],
    tools=tools or None,
)

async for chunk in response:
    delta = chunk.choices[0].delta
    if getattr(delta, "reasoning_content", None):
        yield {"thinking": delta.reasoning_content}
    if getattr(delta, "content", None):
        yield {"content": delta.content}
    if hasattr(chunk, "usage") and chunk.usage:
        yield {"usage": { ... }}
```

## 5. 模型映射

智能路由输出的模型名（gemini 系列）需要映射到目标 provider 的模型名：

```python
# config.py
OPENAI_MODEL_MAP = {
    "gemini-3-flash-preview": os.getenv("OPENAI_MODEL_FLASH", "deepseek/deepseek-chat"),
    "gemini-3.1-pro-preview": os.getenv("OPENAI_MODEL_PRO", "deepseek/deepseek-reasoner"),
}

def map_to_litellm_model(target_model: str) -> str:
    return OPENAI_MODEL_MAP.get(target_model, os.getenv("OPENAI_MODEL_FLASH", "deepseek/deepseek-chat"))
```

LiteLLM 通过 `provider/model` 格式自动路由 API 请求，无需配置 base_url：

| target_model (路由输出) | 映射后 (LiteLLM 格式) | Provider API KEY |
|------------------------|----------------------|-----------------|
| gemini-3-flash-preview | deepseek/deepseek-chat | DEEPSEEK_API_KEY |
| gemini-3.1-pro-preview | deepseek/deepseek-reasoner | DEEPSEEK_API_KEY |
| gemini-3-flash-preview | qwen/qwen-plus | DASHSCOPE_API_KEY |
| gemini-3.1-pro-preview | qwen/qwen-max | DASHSCOPE_API_KEY |

切换 provider 只需改 `.env.openai` 中的模型名和 API KEY。

## 6. Handler 集成

### 6.1 ai/handler.py

```python
# _call_ai_backend 方法新增分支
if AI_BACKEND == "openclaw":
    stream = call_openclaw_stream(...)
elif AI_BACKEND == "openai":
    litellm_model = map_to_litellm_model(target_model)
    stream = call_litellm_stream(messages, litellm_model, thinking_level, need_search)
else:
    stream = call_gemini_stream(...)
```

### 6.2 dingtalk_bot.py

dingtalk_bot.py 有类似的分支逻辑，同样新增 `elif AI_BACKEND == "openai"` 分支。

## 7. 配置

### 7.1 .env.openai

```env
# === 后端 ===
AI_BACKEND=openai

# === 目标 Provider API KEY ===
DEEPSEEK_API_KEY=sk-xxx

# === 模型映射 ===
OPENAI_MODEL_FLASH=deepseek/deepseek-chat
OPENAI_MODEL_PRO=deepseek/deepseek-reasoner

# === 预分析（仍需 Gemini）===
GEMINI_API_KEY=xxx

# === 钉钉 ===
DINGTALK_CLIENT_ID=xxx
DINGTALK_CLIENT_SECRET=xxx

# === 数据层 ===
REDIS_HOST=127.0.0.1
REDIS_PORT=36379

# === 代理 ===
SOCKS_PROXY=socks5h://172.16.0.8:1080
```

### 7.2 config.py 新增

```python
# LiteLLM 后端
OPENAI_MODEL_FLASH = os.getenv("OPENAI_MODEL_FLASH", "deepseek/deepseek-chat")
OPENAI_MODEL_PRO = os.getenv("OPENAI_MODEL_PRO", "deepseek/deepseek-reasoner")
OPENAI_MODEL_MAP = {
    "gemini-3-flash-preview": OPENAI_MODEL_FLASH,
    "gemini-3.1-pro-preview": OPENAI_MODEL_PRO,
}

def map_to_litellm_model(target_model: str) -> str:
    return OPENAI_MODEL_MAP.get(target_model, OPENAI_MODEL_FLASH)
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
| 新增 | `app/litellm_client.py` | ~120 行 | LiteLLM 流式客户端 |
| 新增 | `docker-compose.openai.yml` | ~20 行 | 容器编排 |
| 新增 | `.env.openai` | ~15 行 | 环境变量模板 |
| 修改 | `app/config.py` | +15 行 | LiteLLM 配置 + 模型映射 |
| 修改 | `app/ai/handler.py` | +5 行 | elif openai 分支 |
| 修改 | `app/dingtalk_bot.py` | +5 行 | elif openai 分支 |
| 修改 | `requirements.txt` | +1 行 | litellm>=1.80.0 |

不动：`gemini_client.py`、`openclaw_client.py`、`dingtalk_card.py`、`memory.py`、`database.py`、`ai/router.py`。

## 10. 部署

```bash
# 1. 本地提交 + push
git add ... && git commit -m "feat: add litellm backend" && git push origin master

# 2. 服务器拉代码
ssh tencent_cloud_server "cd /opt/1panel/docker/compose/dingtalk-ai-bot && git pull origin master"

# 3. 停其他后端（如需要）
ssh tencent_cloud_server "cd /opt/1panel/docker/compose/dingtalk-ai-bot && docker compose down"

# 4. 构建启动
ssh tencent_cloud_server "cd /opt/1panel/docker/compose/dingtalk-ai-bot && docker compose -f docker-compose.openai.yml up -d --build"

# 5. 验证
ssh tencent_cloud_server "docker logs --tail 20 dingtalk-ai-bot-openai"
```

## 11. 未来演进

- **多 Provider 路由**：通过 `OPENAI_MODEL_MAP` 扩展更多模型映射，如 `"code": "deepseek/deepseek-coder"`，在路由层增加分类
- **替代 gemini_client.py**：LiteLLM 支持 Gemini thinking + search，长期可用 litellm_client.py 统一所有后端
- **LiteLLM Proxy**：如需管理多个 API KEY 或做负载均衡，可部署独立 LiteLLM proxy 容器
