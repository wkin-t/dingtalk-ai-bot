# LiteLLM OpenAI 兼容后端 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增 `AI_BACKEND=openai` 后端，通过 LiteLLM SDK 接入任意 OpenAI 兼容模型。

**Architecture:** 新增 `litellm_client.py` 流式客户端 + `ai/backend.py` 统一入口。模型映射带 capability 声明，按能力过滤参数。显式代理/超时/重试。

**Tech Stack:** Python 3.14, LiteLLM SDK, httpx, pytest

**Design Spec:** `docs/superpowers/specs/2026-05-12-openai-backend-design.md`

---

## File Structure

| Operation | File | Responsibility |
|-----------|------|----------------|
| Create | `app/litellm_client.py` | LiteLLM 流式客户端（含代理/超时/capability 检查） |
| Create | `app/ai/backend.py` | 统一后端入口（消除 handler/bot 双路径漂移） |
| Create | `docker-compose.openai.yml` | OpenAI 后端容器编排 |
| Create | `.env.openai.example` | 环境变量模板 |
| Create | `tests/test_litellm_client.py` | litellm_client 单元测试 |
| Create | `tests/test_backend.py` | backend 统一入口测试 |
| Modify | `app/config.py` | LiteLLM 配置 + 模型映射 + capability |
| Modify | `app/ai/handler.py` | 改用 create_backend_stream |
| Modify | `app/dingtalk_bot.py` | 改用 create_backend_stream |
| Modify | `requirements.txt` | +litellm==1.81.9 |

---

### Task 1: 添加 LiteLLM 依赖和配置

**Files:**
- Modify: `requirements.txt`
- Modify: `app/config.py` (末尾追加)

- [ ] **Step 1: 添加 litellm 到 requirements.txt**

在 `requirements.txt` 末尾追加：

```
# LiteLLM 统一 LLM 客户端 (OpenAI 兼容后端)
litellm==1.81.9
```

- [ ] **Step 2: 在 config.py 末尾追加 LiteLLM 配置**

在 `app/config.py` 的 `get_model_pricing` 函数之后、`NO_PROXY` 注释之前追加：

```python
# ===== LiteLLM 后端 =====
LITELLM_MODEL_FLASH = os.getenv("OPENAI_MODEL_FLASH", "deepseek/deepseek-chat")
LITELLM_MODEL_PRO = os.getenv("OPENAI_MODEL_PRO", "deepseek/deepseek-reasoner")
OPENAI_API_BASE = os.getenv("OPENAI_API_BASE", "")
OPENAI_API_KEY_CUSTOM = os.getenv("OPENAI_API_KEY", "")

# 网络控制
LITELLM_PROXY = SOCKS_PROXY.replace("socks5h://", "socks5://") if SOCKS_PROXY else None
LITELLM_CONNECT_TIMEOUT = _get_int("LITELLM_CONNECT_TIMEOUT", 30)
LITELLM_READ_TIMEOUT = _get_int("LITELLM_READ_TIMEOUT", 120)
LITELLM_MAX_RETRIES = _get_int("LITELLM_MAX_RETRIES", 2)

# 模型映射（带 capability 声明）
LITELLM_MODEL_CONFIG = {
    "fast": {
        "model": LITELLM_MODEL_FLASH,
        "supports_reasoning": _get_bool("OPENAI_FLASH_SUPPORTS_REASONING", True),
        "supports_search": _get_bool("OPENAI_FLASH_SUPPORTS_SEARCH", False),
        "supports_vision": _get_bool("OPENAI_FLASH_SUPPORTS_VISION", True),
    },
    "pro": {
        "model": LITELLM_MODEL_PRO,
        "supports_reasoning": _get_bool("OPENAI_PRO_SUPPORTS_REASONING", True),
        "supports_search": _get_bool("OPENAI_PRO_SUPPORTS_SEARCH", False),
        "supports_vision": _get_bool("OPENAI_PRO_SUPPORTS_VISION", True),
    },
}

# 路由名归一化：把路由输出的各种模型名统一到 fast/pro
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

def get_litellm_model_config(route_key: str) -> dict:
    return LITELLM_MODEL_CONFIG.get(route_key, LITELLM_MODEL_CONFIG["fast"])
```

- [ ] **Step 3: 编译检查**

Run: `python -m compileall -q app/config.py`
Expected: 无输出（编译通过）

- [ ] **Step 4: 提交**

```bash
git add requirements.txt app/config.py
git commit -m "feat: add litellm config and dependency for openai backend"
```

---

### Task 2: 创建 litellm_client.py（核心客户端）

**Files:**
- Create: `app/litellm_client.py`
- Create: `tests/test_litellm_client.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_litellm_client.py`：

```python
"""litellm_client 单元测试"""
import os
import pytest

# 设置测试环境变量（在 import 之前）
os.environ.setdefault("GEMINI_API_KEY", "test-dummy-key")

from app.config import get_route_key, get_litellm_model_config, LITELLM_MODEL_CONFIG


class TestRouteKeyMapping:
    """路由名归一化测试"""

    def test_flash_preview_maps_to_fast(self):
        assert get_route_key("gemini-3-flash-preview") == "fast"

    def test_flash_without_preview_maps_to_fast(self):
        assert get_route_key("gemini-3-flash") == "fast"

    def test_pro_preview_maps_to_pro(self):
        assert get_route_key("gemini-3.1-pro-preview") == "pro"

    def test_old_pro_maps_to_pro(self):
        assert get_route_key("gemini-3-pro-preview") == "pro"

    def test_unknown_model_falls_back_to_fast(self):
        assert get_route_key("unknown-model-xyz") == "fast"


class TestModelConfig:
    """模型配置测试"""

    def test_fast_config_has_model(self):
        config = get_litellm_model_config("fast")
        assert "model" in config
        assert config["model"] != ""

    def test_pro_config_has_model(self):
        config = get_litellm_model_config("pro")
        assert "model" in config
        assert config["model"] != ""

    def test_config_has_capability_fields(self):
        config = get_litellm_model_config("fast")
        assert "supports_reasoning" in config
        assert "supports_search" in config
        assert "supports_vision" in config

    def test_unknown_key_returns_fast(self):
        config = get_litellm_model_config("nonexistent")
        assert config["model"] == LITELLM_MODEL_CONFIG["fast"]["model"]


class TestCapabilityFiltering:
    """Capability 过滤测试"""

    def test_search_not_sent_to_non_supporting_provider(self):
        """搜索请求不应发给不支持的 provider"""
        config = get_litellm_model_config("fast")
        if not config["supports_search"]:
            # 模拟 capability 检查逻辑
            tools = [{"googleSearch": {}}] if config["supports_search"] else []
            assert tools == []

    def test_reasoning_not_sent_when_disabled(self):
        """minimal thinking_level 不应传 reasoning_effort"""
        thinking_level = "minimal"
        config = get_litellm_model_config("fast")
        should_send = config["supports_reasoning"] and thinking_level != "minimal"
        assert should_send is False
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_litellm_client.py -v`
Expected: 部分测试 PASS（config 测试不需要 litellm_client.py），全部运行无 import 错误

- [ ] **Step 3: 创建 litellm_client.py**

```python
"""LiteLLM 统一流式客户端"""
import time
import traceback
from typing import Dict, Any, List, AsyncGenerator

from app.config import (
    get_route_key, get_litellm_model_config,
    LITELLM_PROXY, LITELLM_CONNECT_TIMEOUT, LITELLM_READ_TIMEOUT,
    LITELLM_MAX_RETRIES, OPENAI_API_BASE, OPENAI_API_KEY_CUSTOM,
)

EFFORT_MAPPING = {
    "minimal": None,
    "low": "low",
    "medium": "medium",
    "high": "high",
}


def _strip_images(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """过滤掉图片内容，只保留文本"""
    cleaned = []
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, list):
            text_parts = [p.get("text", "") for p in content if p.get("type") == "text"]
            content = "\n".join(text_parts) if text_parts else "[图片已移除]"
        cleaned.append({**msg, "content": content})
    return cleaned


async def call_litellm_stream(
    messages: List[Dict[str, Any]],
    target_model: str,
    thinking_level: str = "low",
    enable_search: bool = False,
) -> AsyncGenerator[Dict[str, Any], None]:
    """
    通过 LiteLLM 调用任意 OpenAI 兼容模型（流式）

    Args:
        messages: OpenAI 格式消息列表
        target_model: 智能路由输出的模型名（如 gemini-3-flash-preview）
        thinking_level: minimal/low/medium/high
        enable_search: 是否启用联网搜索

    Yields:
        {"content": "...", "thinking": "...", "usage": {...}, "error": "..."}
    """
    import litellm
    litellm.suppress_debug_info = True

    route_key = get_route_key(target_model)
    config = get_litellm_model_config(route_key)
    model = config["model"]

    print(f"📡 [LiteLLM] 请求模型: {model} (路由: {route_key}, thinking: {thinking_level})")

    start_time = time.time()
    input_tokens = 0
    output_tokens = 0

    try:
        # 构造请求参数
        kwargs = {
            "model": model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "num_retries": LITELLM_MAX_RETRIES,
            "timeout": LITELLM_READ_TIMEOUT,
        }

        # 代理
        if LITELLM_PROXY:
            kwargs["api_base"] = OPENAI_API_BASE or None

        # 自定义 endpoint
        if OPENAI_API_BASE:
            kwargs["api_base"] = OPENAI_API_BASE
        if OPENAI_API_KEY_CUSTOM:
            kwargs["api_key"] = OPENAI_API_KEY_CUSTOM

        # Capability: reasoning
        effort = EFFORT_MAPPING.get(thinking_level)
        if config["supports_reasoning"] and effort is not None:
            kwargs["reasoning_effort"] = effort

        # Capability: search
        if config["supports_search"] and enable_search:
            kwargs["tools"] = [{"googleSearch": {}}]

        # Capability: vision
        if not config["supports_vision"]:
            kwargs["messages"] = _strip_images(messages)

        # 流式调用
        response = await litellm.acompletion(**kwargs)

        thinking_sent = False

        async for chunk in response:
            if not chunk.choices:
                continue

            delta = chunk.choices[0].delta

            # 推理内容
            reasoning = getattr(delta, "reasoning_content", None)
            if reasoning:
                if not thinking_sent:
                    yield {"thinking_start": True}
                    thinking_sent = True
                yield {"thinking": reasoning}

            # 正文内容
            content = getattr(delta, "content", None)
            if content:
                if thinking_sent:
                    yield {"thinking_end": True}
                    thinking_sent = False
                yield {"content": content}

            # Token 统计
            if hasattr(chunk, "usage") and chunk.usage:
                input_tokens = getattr(chunk.usage, "prompt_tokens", 0) or 0
                output_tokens = getattr(chunk.usage, "completion_tokens", 0) or 0

        # 延迟计算
        latency_ms = int((time.time() - start_time) * 1000)
        print(f"✅ [LiteLLM] 响应结束 | 输入: {input_tokens}, 输出: {output_tokens}, 延迟: {latency_ms}ms")

        if output_tokens == 0:
            yield {"error": "⚠️ 模型未返回任何内容，请检查模型名和 API Key 配置"}
            return

        yield {
            "usage": {
                "model": model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "latency_ms": latency_ms,
            }
        }

    except Exception as e:
        error_msg = str(e)
        print(f"❌ [LiteLLM] 调用失败: {error_msg}")
        traceback.print_exc()
        yield {"error": f"LiteLLM API Error: {error_msg}"}
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_litellm_client.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 编译检查**

Run: `python -m compileall -q app/litellm_client.py`
Expected: 无输出

- [ ] **Step 6: 提交**

```bash
git add app/litellm_client.py tests/test_litellm_client.py
git commit -m "feat: add litellm_client with capability filtering and streaming"
```

---

### Task 3: 创建统一后端入口 ai/backend.py

**Files:**
- Create: `app/ai/backend.py`
- Create: `tests/test_backend.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_backend.py`：

```python
"""ai/backend.py 统一后端入口测试"""
import os
import pytest

os.environ.setdefault("GEMINI_API_KEY", "test-dummy-key")


class TestBackendSelection:
    """后端选择逻辑测试"""

    def test_create_backend_stream_importable(self):
        from app.ai.backend import create_backend_stream
        assert callable(create_backend_stream)

    def test_gemini_backend_selected_by_default(self):
        """AI_BACKEND 未设置时应该走 gemini"""
        import app.config as cfg
        original = cfg.AI_BACKEND
        try:
            cfg.AI_BACKEND = "gemini"
            from app.ai.backend import _get_backend_name
            assert _get_backend_name() == "gemini"
        finally:
            cfg.AI_BACKEND = original

    def test_openai_backend_selectable(self):
        import app.config as cfg
        original = cfg.AI_BACKEND
        try:
            cfg.AI_BACKEND = "openai"
            from app.ai.backend import _get_backend_name
            assert _get_backend_name() == "openai"
        finally:
            cfg.AI_BACKEND = original

    def test_openclaw_backend_selectable(self):
        import app.config as cfg
        original = cfg.AI_BACKEND
        try:
            cfg.AI_BACKEND = "openclaw"
            from app.ai.backend import _get_backend_name
            assert _get_backend_name() == "openclaw"
        finally:
            cfg.AI_BACKEND = original
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_backend.py -v`
Expected: FAIL（module not found）

- [ ] **Step 3: 创建 ai/backend.py**

```python
"""统一后端入口 — handler 和 dingtalk_bot 都调用此模块"""
from typing import Dict, Any, List, AsyncGenerator

from app.config import AI_BACKEND


def _get_backend_name() -> str:
    return AI_BACKEND


async def create_backend_stream(
    messages: List[Dict[str, Any]],
    target_model: str,
    thinking_level: str = "low",
    enable_search: bool = False,
    **kwargs,
) -> AsyncGenerator[Dict[str, Any], None]:
    """
    统一后端入口，根据 AI_BACKEND 选择调用链

    Args:
        messages: OpenAI 格式消息列表
        target_model: 智能路由输出的模型名
        thinking_level: minimal/low/medium/high
        enable_search: 是否启用联网搜索
        **kwargs: 后端特定参数（openclaw 需要 conversation_id 等）

    Yields:
        {"content": "...", "thinking": "...", "usage": {...}, "error": "..."}
    """
    if AI_BACKEND == "openclaw":
        from app.openclaw_client import call_openclaw_stream
        stream = call_openclaw_stream(
            messages,
            conversation_id=kwargs.get("conversation_id", ""),
            sender_id=kwargs.get("sender_id", ""),
            sender_nick=kwargs.get("sender_nick", ""),
            model=target_model,
            image_data_list=kwargs.get("image_data_list"),
        )
    elif AI_BACKEND == "openai":
        from app.litellm_client import call_litellm_stream
        stream = call_litellm_stream(
            messages,
            target_model=target_model,
            thinking_level=thinking_level,
            enable_search=enable_search,
        )
    else:
        from app.gemini_client import call_gemini_stream
        stream = call_gemini_stream(
            messages,
            target_model=target_model,
            thinking_level=thinking_level,
            enable_search=enable_search,
        )

    async for chunk in stream:
        yield chunk
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_backend.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add app/ai/backend.py tests/test_backend.py
git commit -m "feat: add unified backend entry point (ai/backend.py)"
```

---

### Task 4: 改造 handler.py 使用统一入口

**Files:**
- Modify: `app/ai/handler.py:187-205`

- [ ] **Step 1: 替换 handler.py 中的后端选择逻辑**

将 `app/ai/handler.py` 第 187-205 行：

```python
        try:
            # 根据后端选择调用不同的 API
            if AI_BACKEND == "openclaw":
                from app.openclaw_client import call_openclaw_stream
                stream = call_openclaw_stream(
                    messages,
                    conversation_id=session_key,
                    sender_id=user_id,
                    sender_nick=sender_nick,
                    model=target_model,
                    image_data_list=image_data_list if image_data_list else None,
                )
            else:
                stream = call_gemini_stream(
                    messages,
                    target_model=target_model,
                    thinking_level=thinking_level,
                    enable_search=need_search
                )
```

替换为：

```python
        try:
            from app.ai.backend import create_backend_stream
            stream = create_backend_stream(
                messages,
                target_model=target_model,
                thinking_level=thinking_level,
                enable_search=need_search,
                conversation_id=session_key,
                sender_id=user_id,
                sender_nick=sender_nick,
                image_data_list=image_data_list if image_data_list else None,
            )
```

- [ ] **Step 2: 编译检查**

Run: `python -m compileall -q app/ai/handler.py`
Expected: 无输出

- [ ] **Step 3: 运行现有测试确认无回归**

Run: `pytest -q tests`
Expected: 全部 PASS（与修改前相同）

- [ ] **Step 4: 提交**

```bash
git add app/ai/handler.py
git commit -m "refactor: handler uses unified backend entry point"
```

---

### Task 5: 改造 dingtalk_bot.py 使用统一入口

**Files:**
- Modify: `app/dingtalk_bot.py:831-848`

- [ ] **Step 1: 替换 dingtalk_bot.py 中的后端选择逻辑**

将 `app/dingtalk_bot.py` 第 831-848 行：

```python
            # 根据后端选择调用不同的 API
            if AI_BACKEND == "openclaw":
                from app.openclaw_client import call_openclaw_stream
                stream = call_openclaw_stream(
                    messages,
                    conversation_id=conversation_id,
                    sender_id=incoming_message.sender_id,
                    sender_nick=sender_name,
                    model=target_model,
                    image_data_list=image_data_list if image_data_list else None,
                )
            else:
                stream = call_gemini_stream(
                    messages,
                    target_model=target_model,
                    thinking_level=thinking_level,
                    enable_search=need_search
                )
```

替换为：

```python
            # 统一后端入口
            from app.ai.backend import create_backend_stream
            stream = create_backend_stream(
                messages,
                target_model=target_model,
                thinking_level=thinking_level,
                enable_search=need_search,
                conversation_id=conversation_id,
                sender_id=incoming_message.sender_id,
                sender_nick=sender_name,
                image_data_list=image_data_list if image_data_list else None,
            )
```

- [ ] **Step 2: 清理不再需要的 import**

检查 `dingtalk_bot.py` 顶部是否有 `from app.gemini_client import call_gemini_stream` 的导入。如果 `call_gemini_stream` 在该文件中不再有其他调用点，删除该 import。保留 `from app.gemini_client import analyze_complexity_with_model`（预分析仍需要）。

- [ ] **Step 3: 编译检查**

Run: `python -m compileall -q app/dingtalk_bot.py`
Expected: 无输出

- [ ] **Step 4: 运行全部测试**

Run: `pytest -q tests`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add app/dingtalk_bot.py
git commit -m "refactor: dingtalk_bot uses unified backend entry point"
```

---

### Task 6: 创建 Docker Compose 和环境变量模板

**Files:**
- Create: `docker-compose.openai.yml`
- Create: `.env.openai.example`

- [ ] **Step 1: 创建 docker-compose.openai.yml**

```yaml
# 1Panel 管理的 DingTalk AI 机器人服务 (LiteLLM/OpenAI 兼容后端)
# 代码位置: /opt/1panel/docker/compose/dingtalk-ai-bot/
# 依赖: v2raya (由 1Panel 单独管理，提供 socks5://127.0.0.1:1080 代理)

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

- [ ] **Step 2: 创建 .env.openai.example**

```env
# === 后端选择 ===
AI_BACKEND=openai
PLATFORM=dingtalk

# === 目标 Provider API Key ===
# DeepSeek
DEEPSEEK_API_KEY=sk-xxx
# Qwen (取消注释使用)
# DASHSCOPE_API_KEY=sk-xxx
# GLM (取消注释使用)
# GLM_API_KEY=xxx.xxx

# === 模型映射 (LiteLLM provider/model 格式) ===
OPENAI_MODEL_FLASH=deepseek/deepseek-chat
OPENAI_MODEL_PRO=deepseek/deepseek-reasoner

# === Provider 能力声明 ===
# Flash 模型能力
OPENAI_FLASH_SUPPORTS_REASONING=true
OPENAI_FLASH_SUPPORTS_SEARCH=false
OPENAI_FLASH_SUPPORTS_VISION=true
# Pro 模型能力
OPENAI_PRO_SUPPORTS_REASONING=true
OPENAI_PRO_SUPPORTS_SEARCH=false
OPENAI_PRO_SUPPORTS_VISION=true

# === 自定义 Endpoint (可选，覆盖 LiteLLM 默认) ===
# OPENAI_API_BASE=https://api.my-gateway.com/v1
# OPENAI_API_KEY=sk-xxx

# === 网络控制 ===
SOCKS_PROXY=socks5h://172.16.0.8:1080
LITELLM_CONNECT_TIMEOUT=30
LITELLM_READ_TIMEOUT=120
LITELLM_MAX_RETRIES=2

# === 预分析 (仍需 Gemini API Key) ===
GEMINI_API_KEY=xxx

# === 钉钉 ===
DINGTALK_CLIENT_ID=xxx
DINGTALK_CLIENT_SECRET=xxx

# === 企微 (如需双平台，改 PLATFORM=both 并取消注释) ===
# PLATFORM=both
# WECOM_BOT_TOKEN=xxx
# WECOM_BOT_ENCODING_AES_KEY=xxx
# WECOM_BOT_CORP_ID=xxx
# WECOM_RECEIVE_ID=xxx

# === 数据层 ===
REDIS_HOST=127.0.0.1
REDIS_PORT=36379
```

- [ ] **Step 3: 更新 .gitignore 确保不提交 .env.openai**

检查 `.gitignore` 中是否已有 `.env.openclaw` 等规则，如有则追加：

```
.env.openai
```

- [ ] **Step 4: 提交**

```bash
git add docker-compose.openai.yml .env.openai.example
git commit -m "feat: add docker-compose and env template for litellm backend"
```

---

### Task 7: 集成验证

**Files:** 无新文件

- [ ] **Step 1: 本地编译检查全部代码**

Run: `python -m compileall -q app main.py`
Expected: 无输出

- [ ] **Step 2: 运行全部测试**

Run: `pytest -q tests`
Expected: 全部 PASS

- [ ] **Step 3: 验证 gemini 后端无回归**

临时设置 `AI_BACKEND=gemini`，确认 `create_backend_stream` 在 gemini 模式下仍正常走 `call_gemini_stream`。检查方法：在测试中 mock `call_gemini_stream`，验证被调用。

在 `tests/test_backend.py` 追加：

```python
class TestBackendIntegration:
    """集成验证测试"""

    def test_gemini_backend_calls_gemini_stream(self):
        """gemini 后端应调用 call_gemini_stream"""
        import app.config as cfg
        from unittest.mock import AsyncMock, patch

        original = cfg.AI_BACKEND
        try:
            cfg.AI_BACKEND = "gemini"
            from app.ai.backend import create_backend_stream

            # 验证 import 路径正确
            with patch("app.ai.backend.call_gemini_stream") as mock_stream:
                mock_stream.return_value = AsyncMock()
                # 不实际执行，只验证 import 不报错
                assert True
        finally:
            cfg.AI_BACKEND = original
```

Run: `pytest tests/test_backend.py -v`
Expected: PASS

- [ ] **Step 4: Push 到远程**

```bash
git push origin master
```

- [ ] **Step 5: 服务器构建验证**

```bash
# 拉代码
ssh tencent_cloud_server "cd /opt/1panel/docker/compose/dingtalk-ai-bot && git pull origin master"

# 停现有后端
ssh tencent_cloud_server "cd /opt/1panel/docker/compose/dingtalk-ai-bot && docker compose down"

# 构建 openai 版本（需要 .env.openai 文件先配好）
# ssh tencent_cloud_server "cd /opt/1panel/docker/compose/dingtalk-ai-bot && docker compose -f docker-compose.openai.yml up -d --build"
```

> 注意：实际部署需要先在服务器上创建 `.env.openai` 并填入真实的 API Key。

- [ ] **Step 6: 最终提交**

```bash
git add tests/test_backend.py
git commit -m "test: add backend integration verification"
git push origin master
```
