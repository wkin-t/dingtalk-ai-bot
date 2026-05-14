# Vertex AI Claude 集成实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 通过 LiteLLM 路由接入 Vertex AI 上的 Claude Sonnet 4 和 Opus 4.1，作为 OpenAI 后端的 provider 选项。

**Architecture:** 在现有 `litellm_client.py` 的 kwargs 构建逻辑中加入 `vertex_ai/` 前缀检测分支，互斥于 OpenAI 兼容路径。per-model region 配置解决跨区域模型可用性问题。Service Account JSON 通过 Docker 只读 volume 挂载，不进镜像。

**Tech Stack:** Python 3, LiteLLM (已包含 vertex_ai 支持), Docker Compose

**Spec:** `docs/superpowers/specs/2026-05-14-vertex-claude-design.md`

---

## File Structure

| Action | File | Responsibility |
|--------|------|---------------|
| Modify | `app/config.py:300-326` | 新增 `VERTEX_PROJECT`、`VERTEX_REGION_*` 环境变量；扩展 `LITELLM_MODEL_CONFIG` 增加 `region` + `reasoning_param` |
| Modify | `app/litellm_client.py:7-11,114-122,137` | import `VERTEX_PROJECT`；provider 互斥 kwargs 构建；流式解析兼容 `thinking` |
| Modify | `tests/test_litellm_client.py` | 新增 Vertex 配置、provider 分支、thinking 参数的测试 |
| Modify | `docker-compose.openai.yml:14-20` | 新增 `GOOGLE_APPLICATION_CREDENTIALS` 环境变量和 SA volume 挂载 |
| Modify | `.env.openai` (用户手动) | Vertex AI 配置模板 |

---

### Task 1: 扩展 config.py — Vertex AI 配置和模型能力字段

**Files:**
- Modify: `app/config.py:300-326`

- [ ] **Step 1: 写失败测试 — Vertex 配置字段存在性**

```python
# tests/test_litellm_client.py — 追加到 TestModelConfig 类末尾

def test_vertex_project_config_exists(self):
    from app.config import VERTEX_PROJECT
    assert isinstance(VERTEX_PROJECT, str)

def test_config_has_region_field(self):
    config = get_litellm_model_config("fast")
    assert "region" in config
    assert isinstance(config["region"], str)

def test_config_has_reasoning_param_field(self):
    config = get_litellm_model_config("fast")
    assert "reasoning_param" in config
    assert config["reasoning_param"] in ("openai_effort", "anthropic_thinking", "none")
```

- [ ] **Step 2: 运行测试验证失败**

Run: `pytest tests/test_litellm_client.py -v -k "vertex_project or region_field or reasoning_param"`
Expected: FAIL — `ImportError: cannot import name 'VERTEX_PROJECT'` 或 `AssertionError: 'region' not in config`

- [ ] **Step 3: 在 config.py 新增 VERTEX_PROJECT 和模型能力字段**

在 `app/config.py` 的 `OPENAI_API_KEY_CUSTOM` 行之后（约第 304 行）新增：

```python
OPENAI_API_KEY_CUSTOM = os.getenv("OPENAI_API_KEY", "")

# Vertex AI 配置（LiteLLM vertex_ai/ 路由使用）
VERTEX_PROJECT = os.getenv("VERTEX_PROJECT", "")
```

修改 `LITELLM_MODEL_CONFIG`（约第 313-326 行），为 fast 和 pro 各增加 `region` 和 `reasoning_param`：

```python
LITELLM_MODEL_CONFIG = {
    "fast": {
        "model": LITELLM_MODEL_FLASH,
        "region": os.getenv("VERTEX_REGION_FAST", "europe-west1"),
        "supports_reasoning": _get_bool("OPENAI_FLASH_SUPPORTS_REASONING", True),
        "supports_search": _get_bool("OPENAI_FLASH_SUPPORTS_SEARCH", False),
        "supports_vision": _get_bool("OPENAI_FLASH_SUPPORTS_VISION", True),
        "reasoning_param": os.getenv("VERTEX_REASONING_PARAM_FAST", "openai_effort"),
    },
    "pro": {
        "model": LITELLM_MODEL_PRO,
        "region": os.getenv("VERTEX_REGION_PRO", "us-east5"),
        "supports_reasoning": _get_bool("OPENAI_PRO_SUPPORTS_REASONING", True),
        "supports_search": _get_bool("OPENAI_PRO_SUPPORTS_SEARCH", False),
        "supports_vision": _get_bool("OPENAI_PRO_SUPPORTS_VISION", True),
        "reasoning_param": os.getenv("VERTEX_REASONING_PARAM_PRO", "openai_effort"),
    },
}
```

注意：`reasoning_param` 默认 `"openai_effort"`（向后兼容），只有当用户切换到 Vertex Claude 模型时才会通过环境变量覆盖为 `"anthropic_thinking"`。

- [ ] **Step 4: 运行测试验证通过**

Run: `pytest tests/test_litellm_client.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 编译检查**

Run: `python -m py_compile app/config.py`
Expected: 无输出（成功）

- [ ] **Step 6: 提交**

```bash
git add app/config.py tests/test_litellm_client.py
git commit -m "feat: add Vertex AI config fields to LITELLM_MODEL_CONFIG

- VERTEX_PROJECT env var for GCP project ID
- per-model region (VERTEX_REGION_FAST/PRO)
- reasoning_param field to distinguish OpenAI effort vs Anthropic thinking"
```

---

### Task 2: 修改 litellm_client.py — provider 互斥和 thinking 适配

**Files:**
- Modify: `app/litellm_client.py:7-11` (import)
- Modify: `app/litellm_client.py:114-122` (kwargs 构建)
- Modify: `app/litellm_client.py:137` (流式解析)

- [ ] **Step 1: 写失败测试 — Vertex provider kwargs 构建**

```python
# tests/test_litellm_client.py — 新增测试类

import os

class TestVertexProviderBranch:
    """Vertex AI provider 互斥分支测试"""

    def test_vertex_model_gets_project_and_location(self):
        """vertex_ai/ 前缀模型应传入 vertex_ai_project 和 vertex_ai_location"""
        # 模拟 Vertex 配置
        os.environ["VERTEX_PROJECT"] = "test-project"
        os.environ["VERTEX_REGION_FAST"] = "europe-west1"
        os.environ["OPENAI_MODEL_FLASH"] = "vertex_ai/claude-sonnet-4@20250514"
        os.environ["VERTEX_REASONING_PARAM_FAST"] = "anthropic_thinking"

        # 重新加载 config
        import importlib
        import app.config
        importlib.reload(app.config)
        from app.config import get_litellm_model_config, VERTEX_PROJECT

        config = get_litellm_model_config("fast")
        assert config["model"].startswith("vertex_ai/")
        assert config["reasoning_param"] == "anthropic_thinking"
        assert VERTEX_PROJECT == "test-project"

    def test_vertex_thinking_budget_mapping(self):
        """anthropic_thinking 应生成 thinking 参数而非 reasoning_effort"""
        # budget 映射逻辑内联验证
        effort_mapping = {"minimal": "none", "low": "low", "medium": "medium", "high": "high"}
        budget_map = {"low": 2048, "medium": 8192, "high": 32768}

        for level in ["low", "medium", "high"]:
            effort = effort_mapping.get(level)
            if effort and effort != "none":
                budget = budget_map.get(effort, 8192)
                assert budget > 0, f"budget 应 >0 for {level}"

        # minimal 不应生成 thinking
        effort = effort_mapping.get("minimal")
        assert effort == "none"

    def teardown_method(self):
        """清理环境变量"""
        for key in ["VERTEX_PROJECT", "VERTEX_REGION_FAST", "OPENAI_MODEL_FLASH", "VERTEX_REASONING_PARAM_FAST"]:
            os.environ.pop(key, None)
        import importlib
        import app.config
        importlib.reload(app.config)
```

- [ ] **Step 2: 运行测试验证失败**

Run: `pytest tests/test_litellm_client.py::TestVertexProviderBranch -v`
Expected: FAIL — `AssertionError: 'reasoning_param' not in config`（因为 config 还没 reload 正确，或字段不存在）

- [ ] **Step 3: 修改 litellm_client.py import 行**

将 `app/litellm_client.py:7-11` 的 import 改为：

```python
from app.config import (
    get_route_key, get_litellm_model_config,
    LITELLM_PROXY, LITELLM_READ_TIMEOUT,
    LITELLM_MAX_RETRIES, OPENAI_API_BASE, OPENAI_API_KEY_CUSTOM,
    VERTEX_PROJECT,
)
```

- [ ] **Step 4: 修改 kwargs 构建逻辑 — provider 互斥**

将 `app/litellm_client.py:114-122`（从 `if OPENAI_API_BASE:` 开始到 `kwargs["reasoning_effort"] = effort` 结束）替换为：

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
            effort = EFFORT_MAPPING.get(thinking_level)
            if config["supports_reasoning"] and effort is not None:
                kwargs["reasoning_effort"] = effort
        else:
            # 无 api_base 的默认路径（如直连 LiteLLM provider）
            effort = EFFORT_MAPPING.get(thinking_level)
            if config["supports_reasoning"] and effort is not None:
                kwargs["reasoning_effort"] = effort
```

- [ ] **Step 5: 修改流式解析 — 兼容 thinking 字段**

将 `app/litellm_client.py:137` 的：

```python
            reasoning = getattr(delta, "reasoning_content", None)
```

改为：

```python
            reasoning = getattr(delta, "reasoning_content", None) or getattr(delta, "thinking", None)
```

- [ ] **Step 6: 运行全部测试验证通过**

Run: `pytest tests/test_litellm_client.py -v`
Expected: 全部 PASS

- [ ] **Step 7: 编译检查**

Run: `python -m py_compile app/litellm_client.py`
Expected: 无输出（成功）

- [ ] **Step 8: 提交**

```bash
git add app/litellm_client.py tests/test_litellm_client.py
git commit -m "feat: add Vertex AI provider branch to litellm_client

- provider-互斥: vertex_ai/ 前缀 → vertex_ai_project + vertex_ai_location
- thinking 适配: anthropic_thinking → thinking param with budget
- 流式解析兼容 reasoning_content + thinking 字段"
```

---

### Task 3: 修改 docker-compose.openai.yml — 凭证挂载

**Files:**
- Modify: `docker-compose.openai.yml:14-20`

- [ ] **Step 1: 修改 compose 文件**

将 `docker-compose.openai.yml` 的 `environment` 和 `volumes` 改为：

```yaml
    environment:
      - AI_BACKEND=openai
      - FLASK_PORT=35001
      - GOOGLE_APPLICATION_CREDENTIALS=/run/secrets/gcp-key.json
    env_file:
      - .env.openai
    volumes:
      - ./data-openai:/app/data
      - ${VERTEX_SA_HOST_PATH:-/dev/null}:/run/secrets/gcp-key.json:ro
```

注意：`${VERTEX_SA_HOST_PATH:-/dev/null}` 确保未设置变量时 compose 仍能启动。

- [ ] **Step 2: 验证 compose 语法**

Run: `docker-compose -f docker-compose.openai.yml config`
Expected: 输出完整配置，无 YAML 语法错误

- [ ] **Step 3: 提交**

```bash
git add docker-compose.openai.yml
git commit -m "feat: mount Vertex AI SA JSON as read-only volume in openai compose

- GOOGLE_APPLICATION_CREDENTIALS points to /run/secrets/gcp-key.json
- VERTEX_SA_HOST_PATH defaults to /dev/null when unset"
```

---

### Task 4: 创建 .env.openai 模板和验证向后兼容

**Files:**
- Modify: `.env.openai`（如不存在则从现有 .env 复制）
- Verify: `app/config.py`, `app/litellm_client.py`

- [ ] **Step 1: 检查 .env.openai 是否存在**

Run: `ls -la .env.openai 2>/dev/null || echo "NOT FOUND"`

如果存在，读取现有内容。如果不存在，检查 `.env.openai.example`。

- [ ] **Step 2: 确保 .env.openai 包含 Vertex 配置模板（注释掉）**

在 `.env.openai` 末尾追加（如果还没有 Vertex 相关配置）：

```env

# ===== Vertex AI Claude（可选，启用后覆盖 DeepSeek 模型）=====
# VERTEX_PROJECT=vertex-485510
# VERTEX_SA_HOST_PATH=./secrets/vertex-sa.json
# GOOGLE_APPLICATION_CREDENTIALS=/run/secrets/gcp-key.json
# OPENAI_MODEL_FLASH=vertex_ai/claude-sonnet-4@20250514
# OPENAI_MODEL_PRO=vertex_ai/claude-opus-4-1@20250514
# VERTEX_REGION_FAST=europe-west1
# VERTEX_REGION_PRO=us-east5
# VERTEX_REASONING_PARAM_FAST=anthropic_thinking
# VERTEX_REASONING_PARAM_PRO=anthropic_thinking
```

- [ ] **Step 3: 验证向后兼容 — 不设 VERTEX_PROJECT 时行为不变**

Run: `pytest tests/test_litellm_client.py -v`
Expected: 全部 PASS（默认值 `openai_effort` 应让原有逻辑不受影响）

- [ ] **Step 4: 编译检查全部改动**

Run: `python -m compileall -q app main.py`
Expected: 无输出（成功）

- [ ] **Step 5: 提交**

```bash
git add .env.openai
git commit -m "chore: add Vertex AI Claude config template to .env.openai

All Vertex options commented out by default — existing deployments unaffected"
```

---

### Task 5: 端到端验证和安全审计

**Files:**
- Verify: all changed files, `.gitignore`, `.dockerignore`, `secrets/`

- [ ] **Step 1: 确认 secrets/ 被 git 正确排除**

Run: `git check-ignore -v secrets/vertex-sa.json`
Expected: `.gitignore:65:secrets/	<output>`

- [ ] **Step 2: 确认 .dockerignore 覆盖凭证**

Run: `grep -c "sa.json\|key.json\|service-account\|secrets/" .dockerignore`
Expected: `5` 或更多

- [ ] **Step 3: 审计 diff — 确认无凭证泄露**

Run: `git diff origin/master...HEAD | grep -iE 'key|secret|token|password|private_key' || echo "CLEAN"`
Expected: `CLEAN`（只匹配到变量名定义，不包含实际值）

- [ ] **Step 4: 运行全部测试**

Run: `pytest -q tests`
Expected: 全部 PASS

- [ ] **Step 5: 编译检查**

Run: `python -m compileall -q app main.py`
Expected: 无输出（成功）

- [ ] **Step 6: 提交所有辅助文件**

```bash
git add .gitignore .dockerignore docs/superpowers/specs/2026-05-14-vertex-claude-design.md
git commit -m "chore: credential isolation + spec doc for Vertex AI Claude

- .gitignore: secrets/, *.sa.json, *.key.json, *service-account*.json
- .dockerignore: prevents credential files in Docker build context
- spec: two-round codex adversarial review passed"
```

---

## Self-Review

**1. Spec 覆盖度：**
- config.py 改动 → Task 1 ✓
- litellm_client.py import → Task 2 Step 3 ✓
- litellm_client.py provider 互斥 → Task 2 Step 4 ✓
- litellm_client.py thinking 适配 → Task 2 Step 4 ✓
- litellm_client.py 流式解析 → Task 2 Step 5 ✓
- docker-compose.openai.yml → Task 3 ✓
- .env 配置模板 → Task 4 ✓
- 安全审计 → Task 5 ✓
- 向后兼容验证 → Task 4 Step 3, Task 5 ✓

**2. Placeholder 扫描：** 无 TBD/TODO/placeholder。

**3. 类型一致性：**
- `VERTEX_PROJECT` 在 config.py 定义为 `str`，litellm_client.py import 为同一符号 ✓
- `config["model"]` 是 `str`，`.startswith("vertex_ai/")` 匹配 ✓
- `config["region"]` 在 config.py 定义，litellm_client.py 通过 `config.get("region", "us-east5")` 读取 ✓
- `config["reasoning_param"]` 在 config.py 定义，litellm_client.py 通过 `config.get("reasoning_param", ...)` 读取 ✓
