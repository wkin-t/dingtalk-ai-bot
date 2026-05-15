# OpenRouter 集成实现计划（已完成）

> **状态:** 已实现 (2026-05-15)
> **架构决策:** 扩展现有 `litellm_client.py` 而非新建独立客户端（避免代码重复）

**Goal:** 接入 OpenRouter 统一 API 网关，支持模型自动回退、供应商价格路由、Web Search 原生工具

**Architecture:**
- `app/config.py` 新增 `OPENROUTER_MODEL_CONFIG` + `OPENROUTER_API_KEY` / `OPENROUTER_BASE_URL`
- `app/litellm_client.py` 扩展：检测到 `OPENROUTER_API_KEY` 时使用 OpenRouter 分支（extra_body: fallbacks + provider routing + web search tool）
- `app/ai/backend.py` 添加 `"openrouter"` 分支，复用 `call_litellm_stream`
- `AI_BACKEND=openrouter` 环境变量启用

**Tech Stack:** LiteLLM (custom_llm_provider=openai + api_base), OpenRouter API

---

## 关键设计决策

### Web Search 格式
`tools: [{"type": "openrouter:web_search"}]` via `extra_body`
- `plugins: [{"id": "web"}]` 是废弃格式，不要用
- 两者都走 `/v1/chat/completions`

### 省钱配置
- `OPENROUTER_PROVIDER_SORT=price` 自动选最便宜供应商
- `OPENROUTER_FALLBACK_FAST` 主模型失败时切换，可以配更便宜的模型
- Prompt Caching 对 Anthropic/Google 模型自动生效，无需额外配置

### Config 选择逻辑
```python
if OPENROUTER_API_KEY:
    config = OPENROUTER_MODEL_CONFIG.get(route_key, OPENROUTER_MODEL_CONFIG["fast"])
else:
    config = get_litellm_model_config(route_key)
```

### 搜索注入跳过
```python
if enable_search and not OPENROUTER_API_KEY:
    # Gemini Search 注入（OpenRouter 用原生 web_search tool，跳过此块）
```

### Usage 统计 Bug 修复（顺带）
OpenAI 流式最后一个 chunk 含 `usage` 但 `choices` 为空，原代码被 `continue` 跳过。
修复：usage 采集移到 `if not chunk.choices: continue` 之前。

## 已修改文件

| 文件 | 变更 |
|------|------|
| `app/config.py` | 新增 `OPENROUTER_API_KEY`, `OPENROUTER_BASE_URL`, `OPENROUTER_MODEL_CONFIG`, `_parse_fallbacks()` |
| `app/litellm_client.py` | 导入 OR 配置；config 双路选择；OR 分支 extra_body；搜索注入跳过；usage bug 修复 |
| `app/ai/backend.py` | `backend in ("openai", "openrouter")` 共用 litellm_stream |
| `tests/test_litellm_client.py` | 新增 `TestOpenRouterConfig` (7 个测试) |
| `tests/test_backend.py` | 新增 openrouter 分发测试 |
| `docker-compose.openrouter.yml` | 新建，端口 35002 |
| `.env.openrouter.example` | 新建，含省钱配置注释 |
| `CLAUDE.md` | 补充 openrouter 部署命令 |

## 环境变量速查

```env
AI_BACKEND=openrouter
OPENROUTER_API_KEY=sk-or-v1-...
OPENROUTER_MODEL_FAST=openai/gpt-4.1-mini
OPENROUTER_FALLBACK_FAST=google/gemini-2.5-flash,anthropic/claude-haiku-4-5
OPENROUTER_MODEL_PRO=anthropic/claude-opus-4-5
OPENROUTER_FALLBACK_PRO=openai/gpt-4.1
OPENROUTER_PROVIDER_SORT=price
```
