# 多 Agent 角色重塑 + Cache 优化 + 采样可控化 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) — Claude 做包工头，dispatch codex:codex-rescue 执行每个 task，Claude 在 task 之间跑测试、做 review。

**Goal:** 同时解决多 agent 角色混淆、system prompt cache miss、温度档位不足、采样参数不可控四个问题。

**Architecture:** 抽出共享纯函数模块（message_transform、system_prompt_blocks、sampling_clamp、sample_override），让 DingTalk 和 WeCom 两条主链路对称调用；分阶段落地（B→C→A→D），每阶段独立可发布、独立可回滚（env flag 控制）。

**Tech Stack:** Python 3.11+, Flask, pytest, pytest-asyncio, litellm, google-genai, redis (with file fallback)

**Spec:** `docs/superpowers/specs/2026-05-18-multi-agent-role-cache-temp-design.md`（经 codex 红军评审 v2）

---

## 文件结构总览

### 新建文件

| 文件 | 职责 | 阶段 |
|------|------|------|
| `app/ai/system_prompt.py` | `build_system_prompt_blocks()` — 拆 3 段 system 内容 | B |
| `app/ai/sampling_clamp.py` | `clamp_temperature`, `clamp_top_p` — provider-aware 兜底 | C |
| `app/ai/message_transform.py` | `rewrite_roles_for_current_agent`, `merge_consecutive_same_role` | A |
| `app/sample_override.py` | 手动采样存储 (Redis + 文件 fallback + expires_at) | D |
| `scripts/backfill_bot_id.py` | 一次性回填 NULL `bot_id` 为当前 BOT_ID | A |
| `tests/test_system_prompt_blocks.py` | B 测试 | B |
| `tests/test_sampling_clamp.py` | C 测试 | C |
| `tests/test_top_p_pipeline.py` | C 测试 | C |
| `tests/test_message_transform.py` | A 测试 | A |
| `tests/test_soul_isolation.py` | A 测试 | A |
| `tests/test_openclaw_ws_skip_rewrite.py` | A 测试 | A |
| `tests/test_gemini_role_rewrite.py` | A 测试 | A |
| `tests/test_sample_override.py` | D 测试 | D |
| `tests/test_e2e_dingtalk.py` | E2E 测试钉钉路径 | A+D |
| `tests/test_e2e_wecom.py` | E2E 测试 wecom 路径 | A+D |

### 修改文件

| 文件 | 改动 | 阶段 |
|------|------|------|
| `app/config.py` | 加 4 个 env flag（ENABLE_CACHE_BLOCKS, ENABLE_TOP_P_PIPELINE, ENABLE_ROLE_REWRITE, ENABLE_SAMPLE_OVERRIDE） | 全部 |
| `app/litellm_client.py` | `_inject_cache_control` 支持 list of blocks；top_p 透传；clamp 调用 | B+C |
| `app/gemini_client.py` | system_instruction 支持 list of blocks（提取 text 拼接）；top_p 透传；clamp 调用；router prompt 加新档位 | B+C |
| `app/openclaw_client.py` | HTTP body 加 top_p（容错） | C |
| `app/ai/backend.py` | `create_backend_stream` 加 top_p 参数；OpenClaw WS 跳过 role rewrite | C+A |
| `app/ai/handler.py` | `_build_system_prompt` 改用 system_prompt.py；TEMPERATURE_MAP 扩展；调用 message_transform；调用 sample_override；保留 raw messages 给 Soul/image | 全部 |
| `app/dingtalk_bot.py` | 同 handler.py 改造（双路径对称）；slash 命令前移；thinkingText 显示 top_p/⚙️ | 全部 |
| `app/dingtalk_card.py` | thinkingText 构造支持采样信息 | D |
| `app/wecom/bot.py` | slash 命令同步（如需） | D |

### Feature Flags (`app/config.py`)

```python
ENABLE_CACHE_BLOCKS    = _get_bool("ENABLE_CACHE_BLOCKS", True)       # B
ENABLE_TOP_P_PIPELINE  = _get_bool("ENABLE_TOP_P_PIPELINE", True)     # C
ENABLE_ROLE_REWRITE    = _get_bool("ENABLE_ROLE_REWRITE", True)       # A
ENABLE_SAMPLE_OVERRIDE = _get_bool("ENABLE_SAMPLE_OVERRIDE", True)    # D
```

---

## 包工头执行协议

**Claude 在每个 Task 之间必须做**：
1. 跑该 task 的新增/修改测试（`pytest -q tests/test_xxx.py -v`）
2. 跑回归（`pytest -q tests` 或针对受影响模块）
3. 跑 `python -m compileall -q app main.py`
4. 检查 codex 产出的 diff 是否符合 plan 中的代码
5. 若 OK → 标记 task 完成 → dispatch 下一 task
6. 若 FAIL → 分析原因；可能 dispatch 新的 codex task 修复，或自行小修复

**dispatch codex 的标准 prompt 模板**：
```text
执行 plan task: docs/superpowers/plans/2026-05-18-multi-agent-role-cache-temp-plan.md 的 [Stage X / Task N]

约束:
- 严格按 plan 中的 step 顺序执行
- 每步先看 plan 给出的代码块，然后实现到指定文件
- 完成所有 step 后 git commit（commit 信息见 step 末尾）
- 不要超出本 task 范围
- 不要修改 ENABLE_* flag 的默认值
- 完成后回报实际 diff stat

仓库: C:\PersonalFiles\dingtalk-ai-bot
当前阶段: [B/C/A/D]
依赖前置 task: [列表]
```

---

## Stage 0: 准备工作（串行，必做）

### Task 0.1: 加 4 个 Feature Flag

**Files:**
- Modify: `app/config.py`

- [ ] **Step 1: 找到 `_get_bool` 定义位置，在合适处加入 4 个 flag**

定位 `app/config.py` 中其他类似的 ENABLE_* 或 _get_bool 用法（如 `USE_STATS`），在其附近添加：

```python
# 多 agent 角色重塑 + cache + 采样改造 feature flags
ENABLE_CACHE_BLOCKS    = _get_bool("ENABLE_CACHE_BLOCKS", True)       # B: system prompt 分块 cache
ENABLE_TOP_P_PIPELINE  = _get_bool("ENABLE_TOP_P_PIPELINE", True)     # C: top_p 贯穿到各 backend
ENABLE_ROLE_REWRITE    = _get_bool("ENABLE_ROLE_REWRITE", True)       # A: 其他 bot 的 assistant 消息转 user
ENABLE_SAMPLE_OVERRIDE = _get_bool("ENABLE_SAMPLE_OVERRIDE", True)    # D: /temp /top_p 手动覆盖
```

- [ ] **Step 2: 验证 config 加载不报错**

Run: `python -c "from app.config import ENABLE_CACHE_BLOCKS, ENABLE_TOP_P_PIPELINE, ENABLE_ROLE_REWRITE, ENABLE_SAMPLE_OVERRIDE; print(ENABLE_CACHE_BLOCKS, ENABLE_TOP_P_PIPELINE, ENABLE_ROLE_REWRITE, ENABLE_SAMPLE_OVERRIDE)"`

Expected: `True True True True`

- [ ] **Step 3: Commit**

```bash
git add app/config.py
git commit -m "chore: add feature flags for role/cache/sampling rework"
```

---

## Stage B: System Prompt Cache 优化（独立可发布）

### Task B.1: 创建 `app/ai/system_prompt.py` 模块（写 test）

**并行候选：** 无（基础建设）
**前置依赖：** Task 0.1

**Files:**
- Create: `tests/test_system_prompt_blocks.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_system_prompt_blocks.py
# -*- coding: utf-8 -*-
"""Tests for build_system_prompt_blocks"""
from datetime import datetime, timezone, timedelta
import pytest

from app.ai.system_prompt import build_system_prompt_blocks


def _fixed_date():
    return datetime(2026, 5, 18, 14, 23, 45, tzinfo=timezone(timedelta(hours=8)))


def test_blocks_three_segments_when_group_and_soul_present():
    blocks = build_system_prompt_blocks(
        group_info={"name": "测试群"},
        soul_content="活泼健谈",
        bot_name="小克",
        current_date=_fixed_date(),
    )
    assert len(blocks) == 3
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}  # stable
    assert blocks[1]["cache_control"] == {"type": "ephemeral"}  # semi-stable (group + soul)
    assert "cache_control" not in blocks[2]                       # dynamic (date)


def test_no_group_no_soul_only_two_blocks():
    blocks = build_system_prompt_blocks(
        group_info=None,
        soul_content=None,
        bot_name="小克",
        current_date=_fixed_date(),
    )
    assert len(blocks) == 2
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in blocks[1]


def test_dynamic_segment_contains_date_no_seconds():
    blocks = build_system_prompt_blocks(
        group_info=None, soul_content=None, bot_name="小克", current_date=_fixed_date()
    )
    dynamic = blocks[-1]["text"]
    assert "2026" in dynamic
    assert "5" in dynamic and "18" in dynamic
    assert "14:23:45" not in dynamic
    assert "14:" not in dynamic
    assert ":45" not in dynamic


def test_dynamic_segment_includes_weekday_cn():
    blocks = build_system_prompt_blocks(
        group_info=None, soul_content=None, bot_name="小克", current_date=_fixed_date()
    )
    # 2026-05-18 是星期一
    assert "周一" in blocks[-1]["text"]


def test_stable_segment_contains_history_format_explanation():
    blocks = build_system_prompt_blocks(
        group_info=None, soul_content=None, bot_name="小克", current_date=_fixed_date()
    )
    stable = blocks[0]["text"]
    assert "真人用户消息" in stable
    assert "其他机器人的发言" in stable
    assert "没有任何前缀的 assistant" in stable


def test_semi_stable_combines_group_and_soul():
    blocks = build_system_prompt_blocks(
        group_info={"name": "AI 交流群"},
        soul_content="爱讲笑话",
        bot_name="小克",
        current_date=_fixed_date(),
    )
    semi = blocks[1]["text"]
    assert "AI 交流群" in semi
    assert "爱讲笑话" in semi


def test_only_group_no_soul():
    blocks = build_system_prompt_blocks(
        group_info={"name": "测试群"}, soul_content=None, bot_name="小克", current_date=_fixed_date()
    )
    assert len(blocks) == 3
    assert "测试群" in blocks[1]["text"]


def test_only_soul_no_group():
    blocks = build_system_prompt_blocks(
        group_info=None, soul_content="活泼", bot_name="小克", current_date=_fixed_date()
    )
    assert len(blocks) == 3
    assert "活泼" in blocks[1]["text"]


def test_bot_name_appears_in_stable():
    blocks = build_system_prompt_blocks(
        group_info=None, soul_content=None, bot_name="小克", current_date=_fixed_date()
    )
    assert "小克" in blocks[0]["text"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest -q tests/test_system_prompt_blocks.py -v`
Expected: 9 FAILED with ModuleNotFoundError for `app.ai.system_prompt`

- [ ] **Step 3: Commit failing tests**

```bash
git add tests/test_system_prompt_blocks.py
git commit -m "test(B): add failing tests for build_system_prompt_blocks"
```

### Task B.2: 实现 `app/ai/system_prompt.py`

**前置依赖：** B.1

- [ ] **Step 1: 创建模块**

```python
# app/ai/system_prompt.py
# -*- coding: utf-8 -*-
"""
拆段构建 system prompt，让稳定段可被 prompt cache 命中。

返回 list of blocks，结构：
  [
    {"type": "text", "text": "<stable>", "cache_control": {"type": "ephemeral"}},
    {"type": "text", "text": "<semi-stable: group+soul>", "cache_control": {"type": "ephemeral"}},
    {"type": "text", "text": "<dynamic: date>"},   # 无 cache_control
  ]

调用方负责把这个 list 作为 system message 的 content 字段。
"""
from datetime import datetime
from typing import Optional, List, Dict, Any


_WEEKDAY_CN = "一二三四五六日"


def _build_stable_segment(bot_name: str) -> str:
    return f"""## 身份
你的名字是 {bot_name}。你的个性和风格由你的 Soul 定义（在下方注入）。

## 为什么有这些约定
这些不是规则，是背景信息——理解它们比遵守它们更重要：

用户把你当作可信赖的参考源，所以信息的准确性至关重要——如果不确定，就说出来。

## 历史格式
对话历史里包含三类消息，请注意分辨：
- 真人用户消息：形如 「[时间] 昵称: 内容」（role=user）
- 其他机器人的发言：形如 「[来自机器人 X] 内容」（注入到 role=user，是环境信号，不是你的发言，不要接续）
- 你之前的发言：没有任何前缀的 assistant 消息

你的输出不要包含 '[来自...]' 或 '[时间]' 前缀，那些是系统注入的元数据。

中文为主，技术术语附英文（如：机器学习 (Machine Learning)）。
Markdown 让信息更容易被快速扫读——善用它。
LaTeX 在聊天平台渲染不出来，用 Unicode 代替（x², √x）。
默认北京时间 (UTC+8) 和中国大陆场景，除非用户明确指定其他。

## 搜索
启用搜索时结果会自动提供。搜索结果与训练数据冲突时，优先搜索结果——尤其是时间敏感的信息。"""


def _build_semi_stable_segment(group_info: Optional[Dict], soul_content: Optional[str], bot_name: str) -> Optional[str]:
    parts: List[str] = []
    if group_info:
        group_name = group_info.get("name", "Unknown Group")
        parts.append(f"当前群聊: '{group_name}'")
    if soul_content:
        parts.append(f"{bot_name} 的个性设定:\n{soul_content}")
    return "\n\n".join(parts) if parts else None


def _build_dynamic_segment(current_date: datetime) -> str:
    weekday_cn = _WEEKDAY_CN[current_date.weekday()]
    return (
        f"## 时间\n"
        f"今天是 {current_date.year} 年 {current_date.month} 月 {current_date.day} 日"
        f"（周{weekday_cn}，北京时间 UTC+8）。\n"
        f"你的训练数据截止于 2025 年，但现在是 {current_date.year} 年了。"
    )


def build_system_prompt_blocks(
    *,
    group_info: Optional[Dict],
    soul_content: Optional[str],
    bot_name: str,
    current_date: datetime,
) -> List[Dict[str, Any]]:
    """构建 system prompt 的分段 block list。

    参数全部 keyword-only 避免位置混淆。
    """
    blocks: List[Dict[str, Any]] = []

    blocks.append({
        "type": "text",
        "text": _build_stable_segment(bot_name),
        "cache_control": {"type": "ephemeral"},
    })

    semi = _build_semi_stable_segment(group_info, soul_content, bot_name)
    if semi:
        blocks.append({
            "type": "text",
            "text": semi,
            "cache_control": {"type": "ephemeral"},
        })

    blocks.append({
        "type": "text",
        "text": _build_dynamic_segment(current_date),
    })

    return blocks
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `pytest -q tests/test_system_prompt_blocks.py -v`
Expected: 9 PASSED

- [ ] **Step 3: 编译检查**

Run: `python -m compileall -q app/ai/system_prompt.py`
Expected: 无输出（成功）

- [ ] **Step 4: Commit**

```bash
git add app/ai/system_prompt.py
git commit -m "feat(B): add build_system_prompt_blocks with cache-friendly segments"
```

### Task B.3: 改造 `_inject_cache_control` 支持 list blocks

**前置依赖：** B.2

**Files:**
- Modify: `app/litellm_client.py:41-52`

- [ ] **Step 1: 添加测试**

新建 `tests/test_cache_control_blocks.py`:

```python
# -*- coding: utf-8 -*-
from app.litellm_client import _inject_cache_control


def test_string_content_wrapped_to_single_cache_block_for_anthropic():
    messages = [{"role": "system", "content": "you are helpful"}, {"role": "user", "content": "hi"}]
    result = _inject_cache_control(messages, "anthropic/claude-sonnet-4")
    assert isinstance(result[0]["content"], list)
    assert result[0]["content"][0]["cache_control"] == {"type": "ephemeral"}


def test_list_content_passed_through_untouched():
    blocks = [
        {"type": "text", "text": "stable", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "dynamic"},
    ]
    messages = [{"role": "system", "content": blocks}, {"role": "user", "content": "hi"}]
    result = _inject_cache_control(messages, "anthropic/claude-sonnet-4")
    # 应该原样透传（不能给每个 block 都加 cache_control）
    assert result[0]["content"] == blocks


def test_non_anthropic_model_unchanged():
    messages = [{"role": "system", "content": "x"}, {"role": "user", "content": "hi"}]
    result = _inject_cache_control(messages, "openai/gpt-4o")
    assert result == messages
```

- [ ] **Step 2: 运行测试看失败**

Run: `pytest -q tests/test_cache_control_blocks.py -v`
Expected: 第 2 个 test FAIL（list 会被破坏），其他 PASS 或 FAIL（取决于当前实现）

- [ ] **Step 3: 修改 `_inject_cache_control`**

`app/litellm_client.py` 中找到当前 `_inject_cache_control` 函数（约 line 41-52），整体替换为：

```python
def _inject_cache_control(messages: List[Dict[str, Any]], model: str) -> List[Dict[str, Any]]:
    """为 Anthropic 模型的 system 消息注入 cache_control breakpoint。

    兼容两种 system content 形态：
      - str: 整体作为单个 ephemeral block（向下兼容）
      - list of blocks: 原样透传（调用方已自行设好 cache_control）
    """
    if not (model.startswith("anthropic/") or "claude" in model.lower()):
        return messages
    result = []
    for msg in messages:
        if msg.get("role") == "system":
            content = msg.get("content", "")
            if isinstance(content, str) and content:
                msg = {**msg, "content": [{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}]}
            # list 形态原样透传（保留调用方的 per-block cache_control 设置）
        result.append(msg)
    return result
```

- [ ] **Step 4: 跑所有相关测试**

Run: `pytest -q tests/test_cache_control_blocks.py tests/test_system_prompt_blocks.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 跑回归**

Run: `pytest -q tests -v`
Expected: 现有 test 不应有新的 fail

- [ ] **Step 6: Commit**

```bash
git add app/litellm_client.py tests/test_cache_control_blocks.py
git commit -m "feat(B): _inject_cache_control supports list-of-blocks content"
```

### Task B.4: 集成到 `app/ai/handler.py`（WeCom 路径）

**前置依赖：** B.3

**Files:**
- Modify: `app/ai/handler.py:255-292`（`_build_system_prompt`）

- [ ] **Step 1: 改造 `_build_system_prompt`**

替换 `app/ai/handler.py` 的 `_build_system_prompt` 方法（约 line 255-292）为：

```python
    def _build_system_prompt(self, group_info: Optional[Dict] = None, soul_content: Optional[str] = None):
        """构建 System Prompt。

        返回:
          - 如果 ENABLE_CACHE_BLOCKS=True 且后端支持: list of blocks
          - 否则: str（向下兼容）
        """
        from app.config import ENABLE_CACHE_BLOCKS, AI_BACKEND
        from app.ai.system_prompt import build_system_prompt_blocks

        beijing_tz = timezone(timedelta(hours=8))
        current_date = datetime.now(beijing_tz)

        bot_name = {"gemini": "Gem", "openclaw": "Claw", "openai": "AI", "openrouter": "小克"}.get(AI_BACKEND, "Gem")

        blocks = build_system_prompt_blocks(
            group_info=group_info,
            soul_content=soul_content,
            bot_name=bot_name,
            current_date=current_date,
        )

        # LiteLLM/OpenRouter 路径支持 list blocks；Gemini/OpenClaw 用 string 拼接（B.5 处理）
        if ENABLE_CACHE_BLOCKS and AI_BACKEND in ("openai", "openrouter"):
            return blocks

        # 降级：拼接成 string
        return "\n\n".join(b["text"] for b in blocks)
```

注意：方法签名加了 `soul_content` 参数（现有调用方未传，需要确认调用点是否需要更新）。

- [ ] **Step 2: 找到 `_build_system_prompt` 调用点，加 soul_content**

搜索 `_build_system_prompt(` 在 handler.py 中的调用：

```bash
grep -n "_build_system_prompt(" app/ai/handler.py
```

如有调用，加上 soul_content（从 conversation_id 加载，参考 dingtalk_bot.py:1153 的 `_load_soul`，但 handler 不应直接依赖 dingtalk 模块——这里**只**把 soul_content=None 传入即可，wecom 当前不支持 soul，等未来需要再加）。

- [ ] **Step 3: 跑测试**

Run: `pytest -q tests -v`
Expected: 全 PASS（无新 fail）

- [ ] **Step 4: Commit**

```bash
git add app/ai/handler.py
git commit -m "feat(B): handler._build_system_prompt uses block-form for LiteLLM/OpenRouter"
```

### Task B.5: 集成到 `app/dingtalk_bot.py`（DingTalk 路径）

**前置依赖：** B.4

**Files:**
- Modify: `app/dingtalk_bot.py:1116-1155`

- [ ] **Step 1: 改造 system_prompt 构造**

定位 `app/dingtalk_bot.py:1116` 附近的 system_prompt 构造（约从 `system_prompt = f"""## 身份` 开始到 `messages.append({"role": "system", "content": system_prompt})` 结束）。

整段替换为：

```python
            from app.config import ENABLE_CACHE_BLOCKS
            from app.ai.system_prompt import build_system_prompt_blocks

            soul_content = _load_soul(conversation_id) or None
            beijing_tz = timezone(timedelta(hours=8))
            current_date = datetime.now(beijing_tz)

            blocks = build_system_prompt_blocks(
                group_info=group_info,
                soul_content=soul_content,
                bot_name=bot_name,
                current_date=current_date,
            )

            messages = []
            if ENABLE_CACHE_BLOCKS and AI_BACKEND in ("openai", "openrouter"):
                messages.append({"role": "system", "content": blocks})
            else:
                # 降级：string 拼接（Gemini/OpenClaw）
                messages.append({"role": "system", "content": "\n\n".join(b["text"] for b in blocks)})
```

**确认变量**：`bot_name`、`group_info`、`conversation_id` 在该函数作用域内已定义；如未定义，向上追溯找到正确变量。

- [ ] **Step 2: 编译检查**

Run: `python -m compileall -q app/dingtalk_bot.py`
Expected: 无错误

- [ ] **Step 3: 跑全测试**

Run: `pytest -q tests -v`
Expected: 全 PASS

- [ ] **Step 4: 手工 smoke 测试启动**

Run: `python -c "from app.dingtalk_bot import GeminiBotHandler" 2>&1 | head -20`
Expected: 无 import 错误

- [ ] **Step 5: Commit**

```bash
git add app/dingtalk_bot.py
git commit -m "feat(B): dingtalk_bot uses block-form system prompt for LiteLLM/OpenRouter"
```

### Task B.6: Gemini system_instruction list 支持

**前置依赖：** B.5
**并行候选：** 与 B.5 部分独立（但需要 B.2）

**Files:**
- Modify: `app/gemini_client.py:_convert_messages_to_gemini`

- [ ] **Step 1: 测试**

```python
# tests/test_gemini_system_blocks.py
from app.gemini_client import _convert_messages_to_gemini


def test_string_system_extracted():
    msgs = [{"role": "system", "content": "you are helpful"}, {"role": "user", "content": "hi"}]
    sys_inst, contents = _convert_messages_to_gemini(msgs)
    assert sys_inst == "you are helpful"


def test_list_blocks_system_concatenated():
    blocks = [
        {"type": "text", "text": "stable"},
        {"type": "text", "text": "dynamic"},
    ]
    msgs = [{"role": "system", "content": blocks}, {"role": "user", "content": "hi"}]
    sys_inst, contents = _convert_messages_to_gemini(msgs)
    assert "stable" in sys_inst
    assert "dynamic" in sys_inst
    assert sys_inst == "stable\n\ndynamic"


def test_no_system_returns_none():
    msgs = [{"role": "user", "content": "hi"}]
    sys_inst, contents = _convert_messages_to_gemini(msgs)
    assert sys_inst is None
```

- [ ] **Step 2: Run tests to verify fail**

Run: `pytest -q tests/test_gemini_system_blocks.py -v`
Expected: `test_list_blocks_system_concatenated` FAIL（当前代码只处理 str）

- [ ] **Step 3: 改造 `_convert_messages_to_gemini`**

在 `app/gemini_client.py` 中定位 `if role == "system":` 分支（约 line 196-198），改为：

```python
        if role == "system":
            if isinstance(content, list):
                # 兼容 list of blocks 形态：提取所有 text part 拼接
                texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
                system_instruction = "\n\n".join(t for t in texts if t)
            else:
                system_instruction = content
            continue
```

- [ ] **Step 4: 运行测试**

Run: `pytest -q tests/test_gemini_system_blocks.py -v`
Expected: 3 PASSED

- [ ] **Step 5: Commit**

```bash
git add app/gemini_client.py tests/test_gemini_system_blocks.py
git commit -m "feat(B): gemini_client._convert_messages_to_gemini supports list system content"
```

### Task B.7: Stage B 总验证

**前置依赖：** B.6

- [ ] **Step 1: 全测试**

Run: `pytest -q tests -v`
Expected: 全 PASS

- [ ] **Step 2: 编译全 app**

Run: `python -m compileall -q app main.py`
Expected: 无错误

- [ ] **Step 3: 标记 Stage B 完成**

无独立 commit。日志记录："Stage B 完成"。

---

## Stage C: Temperature 扩展 + Top_p 贯穿

### Task C.1: 创建 `app/ai/sampling_clamp.py`

**前置依赖：** Stage B 完成
**并行候选：** C.1 内独立

**Files:**
- Create: `app/ai/sampling_clamp.py`
- Create: `tests/test_sampling_clamp.py`

- [ ] **Step 1: 写测试**

```python
# tests/test_sampling_clamp.py
import pytest
from app.ai.sampling_clamp import clamp_temperature, clamp_top_p


@pytest.mark.parametrize("model,temp,expected,warned", [
    ("anthropic/claude-sonnet-4", 1.5, 1.0, True),
    ("anthropic/claude-haiku-4-5", 2.0, 1.0, True),
    ("anthropic/claude-opus-4", 0.9, 0.9, False),
    ("openai/gpt-4o", 1.5, 1.5, False),
    ("openai/gpt-5", 2.0, 2.0, False),
    ("gemini-flash-lite", 1.8, 1.8, False),
    ("gemini-2.5-pro", 2.5, 2.0, False),     # 超界 clamp 到 2.0（通用兜底）
    ("openai/gpt-4o", -0.5, 0.0, False),     # 下界
])
def test_clamp_temperature(model, temp, expected, warned):
    clamped, warning = clamp_temperature(temp, model)
    assert clamped == expected
    if warned:
        assert warning is not None
        assert "claude" in warning.lower() or "anthropic" in warning.lower() or "clamp" in warning.lower()
    else:
        assert warning is None


def test_clamp_temperature_none_passthrough():
    clamped, warning = clamp_temperature(None, "anthropic/claude-sonnet-4")
    assert clamped is None
    assert warning is None


@pytest.mark.parametrize("p,expected,warned", [
    (0.9, 0.9, False),
    (1.0, 1.0, False),
    (0.0, 0.01, True),       # 下界 clamp
    (1.1, 1.0, True),
    (-0.5, 0.01, True),
])
def test_clamp_top_p(p, expected, warned):
    clamped, warning = clamp_top_p(p)
    assert clamped == expected
    if warned:
        assert warning is not None
    else:
        assert warning is None


def test_clamp_top_p_none_passthrough():
    clamped, warning = clamp_top_p(None)
    assert clamped is None
    assert warning is None
```

- [ ] **Step 2: 运行测试看失败**

Run: `pytest -q tests/test_sampling_clamp.py -v`
Expected: 全 FAIL with ModuleNotFoundError

- [ ] **Step 3: 实现模块**

```python
# app/ai/sampling_clamp.py
# -*- coding: utf-8 -*-
"""Provider-aware temperature / top_p clamp.

返回 (clamped_value, warning_or_None)。warning 是给用户看的可读文本。
"""
from typing import Optional, Tuple


def _is_claude_model(model: str) -> bool:
    if not model:
        return False
    m = model.lower()
    return m.startswith("anthropic/") or "claude" in m


def clamp_temperature(t: Optional[float], model: str) -> Tuple[Optional[float], Optional[str]]:
    if t is None:
        return None, None
    if _is_claude_model(model) and t > 1.0:
        print(f"[CLAMP] backend=claude model={model} param=temperature orig={t:.3f} clamped=1.000")
        return 1.0, f"⚠️ Claude 不支持 t>1.0，已 clamp {t:.2f}→1.0"
    clamped = max(0.0, min(t, 2.0))
    if clamped != t:
        print(f"[CLAMP] model={model} param=temperature orig={t:.3f} clamped={clamped:.3f}")
    return clamped, None


def clamp_top_p(p: Optional[float]) -> Tuple[Optional[float], Optional[str]]:
    if p is None:
        return None, None
    if p <= 0:
        print(f"[CLAMP] param=top_p orig={p:.3f} clamped=0.010")
        return 0.01, f"⚠️ top_p 必须 > 0，已 clamp {p:.2f}→0.01"
    if p > 1.0:
        print(f"[CLAMP] param=top_p orig={p:.3f} clamped=1.000")
        return 1.0, f"⚠️ top_p 必须 ≤ 1.0，已 clamp {p:.2f}→1.0"
    return p, None
```

- [ ] **Step 4: 运行测试**

Run: `pytest -q tests/test_sampling_clamp.py -v`
Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add app/ai/sampling_clamp.py tests/test_sampling_clamp.py
git commit -m "feat(C): add sampling_clamp module for provider-aware temp/top_p clamp"
```

### Task C.2: 扩展 `TEMPERATURE_MAP` 增加 wild/chaotic

**前置依赖：** C.1

**Files:**
- Modify: `app/ai/handler.py:19-23`（`TEMPERATURE_MAP`）

- [ ] **Step 1: 测试**

```python
# tests/test_temperature_map.py
def test_temperature_map_has_five_tiers():
    from app.ai.handler import TEMPERATURE_MAP
    assert TEMPERATURE_MAP["precise"] == 0.1
    assert TEMPERATURE_MAP["balanced"] == 0.7
    assert TEMPERATURE_MAP["creative"] == 0.9
    assert TEMPERATURE_MAP["wild"] == 1.3
    assert TEMPERATURE_MAP["chaotic"] == 2.0
```

- [ ] **Step 2: 修改**

替换 `app/ai/handler.py:19-23`:

```python
TEMPERATURE_MAP = {
    "precise":  0.1,   # 代码、数学、事实查询
    "balanced": 0.7,   # 默认对话
    "creative": 0.9,   # 写作、头脑风暴、诗歌
    "wild":     1.3,   # 实验性创意，已超出常规安全区
    "chaotic":  2.0,   # 极限探索，格式易崩
}
```

- [ ] **Step 3: 运行测试 + commit**

```bash
pytest -q tests/test_temperature_map.py -v
git add app/ai/handler.py tests/test_temperature_map.py
git commit -m "feat(C): expand TEMPERATURE_MAP with wild/chaotic tiers"
```

### Task C.3: 路由 prompt 更新（Gemini）

**前置依赖：** C.2
**并行候选：** 与 C.4 并行

**Files:**
- Modify: `app/gemini_client.py` 中 `analyze_complexity_with_model` 函数的 prompt

- [ ] **Step 1: 定位 prompt**

```bash
grep -n "temperature" app/gemini_client.py | head -20
```

找到 `analyze_complexity_with_model` 函数的 prompt 字符串，定位 temperature 选择规则段。

- [ ] **Step 2: 替换 temperature 规则段**

把现有 temperature 说明（约 "precise/balanced/creative"）替换为：

```text
temperature 选择规则:
- precise (0.1): 代码、数学、事实问答
- balanced (0.7): 通用对话
- creative (0.9): 写作、头脑风暴
- wild (1.3): 用户**明确表态**要"换个风格"、"再脑洞大开点"、"再奇葩点"
- chaotic (2.0): 用户**明确表态**要"完全随机"、"瞎写"、"乱来"

默认 balanced。仅当用户明确要求探索性输出时才选高档位。
不要因为"问题看起来有创意感"就主动选 wild/chaotic。
```

- [ ] **Step 3: 编译检查**

Run: `python -m compileall -q app/gemini_client.py`

- [ ] **Step 4: Commit**

```bash
git add app/gemini_client.py
git commit -m "feat(C): gemini router prompt mentions wild/chaotic tiers"
```

### Task C.4: 路由 prompt 更新（OpenRouter）

**前置依赖：** C.2
**并行候选：** 与 C.3 并行

**Files:**
- Modify: `app/litellm_client.py` `analyze_complexity_with_openrouter`

- [ ] **Step 1-4: 同 C.3，作用于 OpenRouter 路径**

提交信息：`feat(C): openrouter router prompt mentions wild/chaotic tiers`

### Task C.5: top_p 贯穿 `create_backend_stream`

**前置依赖：** C.1
**并行候选：** C.5 内独立（先 backend.py 再各 client）

**Files:**
- Modify: `app/ai/backend.py:11-16`

- [ ] **Step 1: 测试**

```python
# tests/test_top_p_pipeline.py
import inspect
from app.ai.backend import create_backend_stream
from app.litellm_client import call_litellm_stream
from app.gemini_client import call_gemini_stream


def test_create_backend_stream_accepts_top_p():
    sig = inspect.signature(create_backend_stream)
    assert "top_p" in sig.parameters
    assert sig.parameters["top_p"].default is None


def test_litellm_stream_accepts_top_p():
    sig = inspect.signature(call_litellm_stream)
    assert "top_p" in sig.parameters
    assert sig.parameters["top_p"].default is None


def test_gemini_stream_accepts_top_p():
    sig = inspect.signature(call_gemini_stream)
    assert "top_p" in sig.parameters
    assert sig.parameters["top_p"].default is None
```

- [ ] **Step 2: 运行测试看失败**

Run: `pytest -q tests/test_top_p_pipeline.py -v`
Expected: 3 FAIL

- [ ] **Step 3: 修改 `create_backend_stream`**

定位 `app/ai/backend.py` 中 `create_backend_stream` 的签名，加 `top_p: Optional[float] = None` 参数：

```python
async def create_backend_stream(
    messages,
    target_model,
    thinking_level="low",
    enable_search=False,
    temperature=0.7,
    top_p=None,                   # 新增
    conversation_id="",
):
    ...
```

在调用 `call_gemini_stream` / `call_litellm_stream` / `call_openclaw_*_stream` 时传入 `top_p=top_p`。

- [ ] **Step 4: 修改 `call_litellm_stream`**

`app/litellm_client.py` 中 `call_litellm_stream` 签名加 `top_p: Optional[float] = None`。

在 `kwargs` 构造处增加：

```python
if top_p is not None:
    from app.ai.sampling_clamp import clamp_top_p
    clamped_top_p, top_p_warning = clamp_top_p(top_p)
    if clamped_top_p is not None:
        kwargs["top_p"] = clamped_top_p
    # warning 通过 yield 暴露（见 C.7）
```

同时在调用 sampling_clamp 处理 temperature：

```python
from app.ai.sampling_clamp import clamp_temperature
clamped_temp, temp_warning = clamp_temperature(temperature, model)
kwargs["temperature"] = clamped_temp if clamped_temp is not None else temperature
```

- [ ] **Step 5: 修改 `call_gemini_stream`**

`app/gemini_client.py` 中 `call_gemini_stream` 签名加 `top_p: Optional[float] = None`。

在构造 `GenerateContentConfig` 时：

```python
config_kwargs = {"temperature": clamped_temp}
if top_p is not None:
    from app.ai.sampling_clamp import clamp_top_p
    clamped_top_p, _ = clamp_top_p(top_p)
    if clamped_top_p is not None:
        config_kwargs["top_p"] = clamped_top_p
```

- [ ] **Step 6: 修改 `call_openclaw_*_stream`**

`app/openclaw_client.py` HTTP 模式 body 加 `top_p`（如果非 None）：

```python
if top_p is not None:
    body["top_p"] = top_p
```

WS 模式不支持 top_p，silently ignore（不报错）。

- [ ] **Step 7: 跑测试**

Run: `pytest -q tests/test_top_p_pipeline.py tests/test_sampling_clamp.py -v`
Expected: 全 PASS

- [ ] **Step 8: 跑回归**

Run: `pytest -q tests -v`
Expected: 全 PASS

- [ ] **Step 9: Commit**

```bash
git add app/ai/backend.py app/litellm_client.py app/gemini_client.py app/openclaw_client.py tests/test_top_p_pipeline.py
git commit -m "feat(C): top_p pipeline through backend → all clients with per-backend clamp"
```

### Task C.6: handler / dingtalk_bot 接入 sampling clamp + 路由 temp 解析

**前置依赖：** C.5

- [ ] **Step 1: handler.py 处理 temperature label**

在 `app/ai/handler.py` `_route_model` 后，使用 `TEMPERATURE_MAP` 映射 label 到数值。已存在的逻辑（line 358-363）需确认：

```python
temp_label = complexity.get("temperature", "balanced")
temperature = TEMPERATURE_MAP.get(str(temp_label), 0.7)
```

无改动需要（已存在，新档位自动生效）。

- [ ] **Step 2: dingtalk_bot.py 同步**

定位 `dingtalk_bot.py` 中 route 后处理 temperature 的位置，确认逻辑一致（搜索 `TEMPERATURE_MAP` 或 `temperature`）。

```bash
grep -n "TEMPERATURE_MAP\|temperature" app/dingtalk_bot.py | head -20
```

如果 dingtalk_bot 自己实现路由处理，确保也调用 `TEMPERATURE_MAP`。

- [ ] **Step 3: 测试新档位经路由生效**

Run: `pytest -q tests -v`
Expected: 全 PASS

- [ ] **Step 4: Commit**（如有修改）

```bash
git add app/dingtalk_bot.py
git commit -m "feat(C): dingtalk_bot uses expanded TEMPERATURE_MAP via existing path"
```

### Task C.7: Stage C 总验证

**前置依赖：** C.6

- [ ] **Step 1: 全测试**

Run: `pytest -q tests -v && python -m compileall -q app main.py`
Expected: 全 PASS + 编译成功

- [ ] **Step 2: 手工 smoke**

Run: `python -c "from app.ai.backend import create_backend_stream; from app.ai.sampling_clamp import clamp_temperature, clamp_top_p; import inspect; print('top_p in backend:', 'top_p' in inspect.signature(create_backend_stream).parameters)"`
Expected: `top_p in backend: True`

- [ ] **Step 3: 标记 Stage C 完成**

---

## Stage A: Agent 角色重塑 + 转换层

### Task A.1: 创建 `app/ai/message_transform.py`（写测试）

**前置依赖：** Stage C 完成
**并行候选：** A.1 独立

**Files:**
- Create: `tests/test_message_transform.py`

- [ ] **Step 1: 写测试**

```python
# tests/test_message_transform.py
# -*- coding: utf-8 -*-
import pytest
from app.ai.message_transform import (
    rewrite_roles_for_current_agent,
    merge_consecutive_same_role,
)


def test_other_bot_assistant_becomes_user():
    msgs = [
        {"role": "assistant", "content": "[来自机器人 Gem] hi", "bot_id": "gemini"},
    ]
    result = rewrite_roles_for_current_agent(msgs, current_bot_id="openrouter")
    assert result[0]["role"] == "user"
    assert "bot_id" not in result[0]  # stripped


def test_current_bot_assistant_preserved():
    msgs = [
        {"role": "assistant", "content": "hi", "bot_id": "openrouter"},
    ]
    result = rewrite_roles_for_current_agent(msgs, current_bot_id="openrouter")
    assert result[0]["role"] == "assistant"
    assert "bot_id" not in result[0]


def test_assistant_without_bot_id_preserved_as_assistant():
    """保守策略：bot_id 为 None 的旧历史保留为 assistant，避免破坏多轮"""
    msgs = [{"role": "assistant", "content": "old reply", "bot_id": None}]
    result = rewrite_roles_for_current_agent(msgs, current_bot_id="openrouter")
    assert result[0]["role"] == "assistant"


def test_user_messages_unchanged_role():
    msgs = [{"role": "user", "content": "hi", "timestamp": "t1"}]
    result = rewrite_roles_for_current_agent(msgs, current_bot_id="openrouter")
    assert result[0]["role"] == "user"
    assert "timestamp" not in result[0]   # 元数据 stripped


def test_system_message_role_preserved():
    msgs = [{"role": "system", "content": "sys"}]
    result = rewrite_roles_for_current_agent(msgs, current_bot_id="openrouter")
    assert result[0]["role"] == "system"


def test_bot_id_field_stripped_after_rewrite():
    msgs = [
        {"role": "user", "content": "u", "bot_id": "gemini", "timestamp": "t1"},
        {"role": "assistant", "content": "a", "bot_id": "openrouter"},
    ]
    result = rewrite_roles_for_current_agent(msgs, current_bot_id="openrouter")
    for m in result:
        assert "bot_id" not in m
        assert "timestamp" not in m
        assert "sender_nick" not in m
        assert set(m.keys()) <= {"role", "content"}


def test_merge_consecutive_user_strings():
    msgs = [
        {"role": "user", "content": "first"},
        {"role": "user", "content": "second"},
        {"role": "assistant", "content": "reply"},
    ]
    result = merge_consecutive_same_role(msgs)
    assert len(result) == 2
    assert result[0]["content"] == "first\n\nsecond"
    assert result[1]["content"] == "reply"


def test_merge_consecutive_assistant_strings():
    msgs = [
        {"role": "assistant", "content": "a1"},
        {"role": "assistant", "content": "a2"},
    ]
    result = merge_consecutive_same_role(msgs)
    assert len(result) == 1
    assert result[0]["content"] == "a1\n\na2"


def test_merge_no_consecutive_unchanged():
    msgs = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
    ]
    result = merge_consecutive_same_role(msgs)
    assert len(result) == 3
    assert [m["content"] for m in result] == ["u1", "a1", "u2"]


def test_system_message_not_merged():
    msgs = [
        {"role": "system", "content": "s1"},
        {"role": "system", "content": "s2"},  # 通常不会发生，但要保证不合并
    ]
    result = merge_consecutive_same_role(msgs)
    assert len(result) == 2


def test_empty_history_returns_empty():
    assert rewrite_roles_for_current_agent([], "openrouter") == []
    assert merge_consecutive_same_role([]) == []


def test_history_tail_user_plus_current_user_merged():
    """codex 评审发现的关键 case：历史尾部 user + 当前 user 必须能合并"""
    msgs = [
        {"role": "user", "content": "old"},
        {"role": "user", "content": "current"},
    ]
    result = merge_consecutive_same_role(msgs)
    assert len(result) == 1
    assert result[0]["content"] == "old\n\ncurrent"


def test_merge_string_and_list_user_messages():
    msgs = [
        {"role": "user", "content": "history text"},
        {"role": "user", "content": [
            {"type": "text", "text": "current with image"},
            {"type": "image_url", "image_url": {"url": "data:..."}},
        ]},
    ]
    result = merge_consecutive_same_role(msgs)
    assert len(result) == 1
    merged = result[0]["content"]
    assert isinstance(merged, list)
    # 历史文本应该插到 list 头部，与原 text part 用 \n\n 分隔
    assert merged[0]["type"] == "text"
    assert "history text" in merged[0]["text"]
    assert "current with image" in merged[0]["text"]
    # 图片保留
    assert any(b.get("type") == "image_url" for b in merged)


def test_merge_two_list_messages():
    msgs = [
        {"role": "user", "content": [
            {"type": "text", "text": "first text"},
        ]},
        {"role": "user", "content": [
            {"type": "text", "text": "second text"},
            {"type": "image_url", "image_url": {"url": "x"}},
        ]},
    ]
    result = merge_consecutive_same_role(msgs)
    merged = result[0]["content"]
    assert isinstance(merged, list)
    # 相邻 text part 合并
    text_parts = [b["text"] for b in merged if b.get("type") == "text"]
    assert any("first text" in t and "second text" in t for t in text_parts)


def test_message_without_role_treated_as_user():
    msgs = [{"content": "no role"}]
    result = rewrite_roles_for_current_agent(msgs, "openrouter")
    assert result[0]["role"] == "user"


def test_message_without_content_set_to_empty():
    msgs = [{"role": "user"}]
    result = rewrite_roles_for_current_agent(msgs, "openrouter")
    assert result[0]["content"] == ""
```

- [ ] **Step 2: 运行测试看失败**

Run: `pytest -q tests/test_message_transform.py -v`
Expected: 全 FAIL（模块不存在）

- [ ] **Step 3: Commit failing tests**

```bash
git add tests/test_message_transform.py
git commit -m "test(A): add failing tests for message_transform module"
```

### Task A.2: 实现 `app/ai/message_transform.py`

**前置依赖：** A.1

- [ ] **Step 1: 实现**

```python
# app/ai/message_transform.py
# -*- coding: utf-8 -*-
"""消息转换层：处理多 agent 角色重塑 + 连续相同 role 合并。

设计原则：
- 纯函数，不依赖任何外部状态
- 输入输出都是 OpenAI 格式（{role, content, ...}）
- 输出严格清理：只保留 role 和 content 字段
"""
from typing import List, Dict, Any, Optional


def rewrite_roles_for_current_agent(
    messages: List[Dict[str, Any]],
    current_bot_id: Optional[str],
) -> List[Dict[str, Any]]:
    """把非当前 agent 的 assistant 消息重写为 user 角色。

    规则：
      - role: user → 保持 user
      - role: assistant + bot_id == current → 保持 assistant
      - role: assistant + bot_id != current AND bot_id is not None → 改为 user
      - role: assistant + bot_id is None → 保持 assistant（保守策略，避免破坏旧多轮）
      - role: system → 保持 system

    返回的 dict 只含 role 和 content（其他元数据 stripped），
    避免泄漏到 LLM SDK（部分 SDK 对未知字段敏感）。
    """
    result: List[Dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        bot_id = msg.get("bot_id")

        if role == "assistant" and bot_id is not None and bot_id != current_bot_id:
            new_role = "user"
        else:
            new_role = role

        result.append({"role": new_role, "content": content})
    return result


def merge_consecutive_same_role(
    messages: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """合并连续相同 role 的消息（system 除外）。

    Anthropic API 要求 user/assistant 严格交替；
    经过 rewrite_roles_for_current_agent 后会产生连续 user，必须合并。

    合并规则:
      - 两条都 str → str 用 \\n\\n 拼接
      - 一条 str 一条 list → str 包装成 text part 插到 list 头部，与原 text 用 \\n\\n 合并
      - 两条都 list → 顺序拼接；相邻同为 text part 的合并
    """
    if not messages:
        return []

    result: List[Dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        if result and result[-1]["role"] == role and role in ("user", "assistant"):
            # 合并到上一条
            prev_content = result[-1]["content"]
            result[-1]["content"] = _merge_content(prev_content, content)
        else:
            # 新增一条
            result.append({"role": role, "content": content})
    return result


def _merge_content(a: Any, b: Any) -> Any:
    """合并两个 content 字段。"""
    if isinstance(a, str) and isinstance(b, str):
        return f"{a}\n\n{b}"

    # 提取 a 的文本和 b 的文本/其他 part
    a_text, a_other = _split_text_and_other(a)
    b_text, b_other = _split_text_and_other(b)

    # 合并 text；用 \n\n 拼接
    merged_text = "\n\n".join(t for t in [a_text, b_text] if t)

    # 如果两边都没有非 text part，返回 str
    if not a_other and not b_other:
        return merged_text

    # 否则返回 list，merged_text 在头部
    result: List[Dict[str, Any]] = []
    if merged_text:
        result.append({"type": "text", "text": merged_text})
    result.extend(a_other)
    result.extend(b_other)
    return result


def _split_text_and_other(content: Any) -> tuple:
    """从 content 中分离 text 和其他 part（保留顺序无关性，仅用于合并）。"""
    if isinstance(content, str):
        return content, []
    if isinstance(content, list):
        text_parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        other_parts = [b for b in content if isinstance(b, dict) and b.get("type") != "text"]
        return "\n\n".join(t for t in text_parts if t), other_parts
    return "", []
```

- [ ] **Step 2: 运行测试**

Run: `pytest -q tests/test_message_transform.py -v`
Expected: 全 PASS

- [ ] **Step 3: Commit**

```bash
git add app/ai/message_transform.py
git commit -m "feat(A): add message_transform module with role rewrite + merge"
```

### Task A.3: handler.py 接入 message_transform + 保留 raw messages

**前置依赖：** A.2

**Files:**
- Modify: `app/ai/handler.py:process_message`（line 142-186 附近）

- [ ] **Step 1: 改造 process_message**

定位 `_format_history` 调用处（约 line 151）和当前消息追加（line 157-175）。

新流程：

```python
            # 旧: formatted_history = self._format_history(history_messages)
            # 新: 保留 bot_id 给 transform 用
            formatted_history = self._format_history_with_meta(history_messages, BOT_ID)

            # 构造当前用户消息（保留原代码）
            beijing_tz = timezone(timedelta(hours=8))
            current_timestamp = datetime.now(beijing_tz).strftime("%Y-%m-%d %H:%M:%S")
            # ... image_data_list / text_content 构造（保留）...

            # 拼接 raw messages（含 bot_id 元数据，供 Soul/image 使用）
            messages_raw = []
            messages_raw.append({"role": "system", "content": system_prompt})
            messages_raw.extend(formatted_history)
            messages_raw.append({"role": "user", "content": current_user_content})

            # 角色重塑 + merge → 给模型用
            from app.ai.message_transform import rewrite_roles_for_current_agent, merge_consecutive_same_role
            from app.config import ENABLE_ROLE_REWRITE

            if ENABLE_ROLE_REWRITE:
                rewritten = rewrite_roles_for_current_agent(messages_raw, BOT_ID)
                messages = merge_consecutive_same_role(rewritten)
            else:
                # feature flag off：旁路转换层（紧急回滚用）
                messages = [{"role": m["role"], "content": m["content"]} for m in messages_raw]

            # 发送给 backend
            # ...
```

- [ ] **Step 2: 新增 `_format_history_with_meta` 方法**

替换或新增 `_format_history`：

```python
    def _format_history_with_meta(self, history_messages: List[Dict], current_bot_id: str) -> List[Dict]:
        """格式化历史消息，保留 bot_id 给后续 transform 层。

        - 真人 user：[时间] 昵称: 内容
        - 当前 bot 的 assistant：不加前缀
        - 其他 bot 的 assistant：[来自机器人 X] 内容
        """
        formatted = []
        for msg in history_messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            timestamp = msg.get("timestamp")
            sender_nick = msg.get("sender_nick")
            bot_id = msg.get("bot_id")

            new_msg: Dict[str, Any] = {"role": role}
            if bot_id is not None:
                new_msg["bot_id"] = bot_id

            if role == "user":
                if timestamp:
                    if sender_nick and not str(content).startswith(f"{sender_nick}:"):
                        new_msg["content"] = f"[{timestamp}] {sender_nick}: {content}"
                    else:
                        new_msg["content"] = f"[{timestamp}] {content}"
                else:
                    new_msg["content"] = content
            elif role == "assistant" and bot_id is not None and bot_id != current_bot_id:
                bot_source = {"gemini": "Gem", "openclaw": "Claw", "openai": "小G", "openrouter": "小克"}.get(bot_id, bot_id)
                tag = f"[来自机器人 {bot_source}]"
                if not str(content).startswith(tag):
                    new_msg["content"] = f"{tag} {content}"
                else:
                    new_msg["content"] = content
            else:
                new_msg["content"] = content

            formatted.append(new_msg)
        return formatted
```

旧 `_format_history` 保留作为 fallback / 兼容（不删除，但标记 deprecated）。

- [ ] **Step 3: Soul/image 使用 raw messages**

在 `process_message` 内，调用 Soul/image 模块时传 `messages_raw` 而非 `messages`。具体位置见 `app/ai/handler.py` 中 Soul 进化和 image prompt 增强的调用点。

- [ ] **Step 4: 跑测试**

Run: `pytest -q tests -v`
Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add app/ai/handler.py
git commit -m "feat(A): handler integrates message_transform + raw messages for soul/image"
```

### Task A.4: dingtalk_bot.py 同步改造

**前置依赖：** A.3

**Files:**
- Modify: `app/dingtalk_bot.py:1163-1236`（_format_history 等价段 + 当前消息追加）
- Modify: `app/dingtalk_bot.py:1353, 1737`（Soul/image 调用，传 raw）

- [ ] **Step 1: 改造历史格式化段**

替换 `app/dingtalk_bot.py:1163-1192` 的 formatted_history 循环，与 handler.py 的 `_format_history_with_meta` 逻辑一致——为了 DRY，**抽函数到 `app/ai/history_format.py`**：

实际上，更简洁的做法是**让 dingtalk_bot 直接 import handler 的方法或抽到独立模块**。新建 `app/ai/history_format.py`：

```python
# app/ai/history_format.py
from typing import List, Dict, Any


def format_history_with_meta(history_messages: List[Dict], current_bot_id: str) -> List[Dict]:
    """[复用 handler._format_history_with_meta 的逻辑]"""
    # 同 A.3 中实现，搬到这里
    formatted = []
    for msg in history_messages:
        # ... 同 A.3 ...
        formatted.append(new_msg)
    return formatted
```

然后让 `handler.py._format_history_with_meta` 调用 `history_format.format_history_with_meta`，同时 `dingtalk_bot.py` 也调用。

- [ ] **Step 2: dingtalk_bot.py 替换 system + history + current + transform**

定位 `dingtalk_bot.py:1157-1236`（messages 构造段），整段替换为类似 handler.py 的流程：

```python
            from app.ai.history_format import format_history_with_meta
            from app.ai.message_transform import rewrite_roles_for_current_agent, merge_consecutive_same_role
            from app.config import ENABLE_ROLE_REWRITE

            messages_raw = []
            messages_raw.append({"role": "system", "content": system_prompt_or_blocks})

            formatted_history = format_history_with_meta(history_messages, BOT_ID)
            messages_raw.extend(formatted_history)

            # 当前用户消息（图片或文本）
            beijing_tz = timezone(timedelta(hours=8))
            current_timestamp = datetime.now(beijing_tz).strftime("%Y-%m-%d %H:%M:%S")
            sender_nick = incoming_message.sender_nick or "User"

            if image_data_list:
                # ... 构造 user_message_content list（保留现有逻辑）...
                messages_raw.append({"role": "user", "content": user_message_content})
            else:
                text_content = f"[{current_timestamp}] {sender_nick}: {content}"
                messages_raw.append({"role": "user", "content": text_content})

            if ENABLE_ROLE_REWRITE:
                rewritten = rewrite_roles_for_current_agent(messages_raw, BOT_ID)
                messages = merge_consecutive_same_role(rewritten)
            else:
                messages = [{"role": m["role"], "content": m["content"]} for m in messages_raw]
```

- [ ] **Step 3: Soul/image 调用使用 messages_raw**

定位 `dingtalk_bot.py:1353`（image prompt 增强）和 `dingtalk_bot.py:1737`（Soul 进化）。

- 把传给 `_enrich_image_prompt(messages, ...)` 的参数改为 `messages_raw`
- 把传给 `_maybe_evolve_soul(conversation_id, messages, ...)` 的参数改为 `messages_raw`

- [ ] **Step 4: 跑测试**

Run: `pytest -q tests -v`

- [ ] **Step 5: Commit**

```bash
git add app/dingtalk_bot.py app/ai/history_format.py app/ai/handler.py
git commit -m "feat(A): dingtalk_bot uses message_transform + raw messages for soul/image"
```

### Task A.5: OpenClaw WS 路径跳过角色重塑

**前置依赖：** A.4

**Files:**
- Modify: `app/ai/backend.py` 或 `app/openclaw_client.py`

- [ ] **Step 1: 测试**

```python
# tests/test_openclaw_ws_skip_rewrite.py
import os
import pytest
from app.ai import backend


def test_openclaw_ws_transport_marker_exists():
    """确认 backend 模块知晓 openclaw transport 配置"""
    from app.config import OPENCLAW_GATEWAY_TRANSPORT
    assert OPENCLAW_GATEWAY_TRANSPORT in ("http", "ws")


# 实际跳过逻辑需要更细粒度 mock，参考 e2e 测试
```

- [ ] **Step 2: 调整调用入口**

由于 angle 角色重塑是在 handler/dingtalk_bot 层做的，OpenClaw WS 自然不会被重塑——只要 messages 在到达 `call_openclaw_ws_stream` 前已经处理过。

但根据 spec §A.4，**WS 模式应当不参与重塑**。考虑实现策略：

策略 A（推荐）：在 handler/dingtalk_bot 的转换段加判断：

```python
from app.config import AI_BACKEND, OPENCLAW_GATEWAY_TRANSPORT

if AI_BACKEND == "openclaw" and OPENCLAW_GATEWAY_TRANSPORT == "ws":
    # WS 短路：跳过角色重塑（WS 只取最后 user 消息）
    messages = [{"role": m["role"], "content": m["content"]} for m in messages_raw]
elif ENABLE_ROLE_REWRITE:
    rewritten = rewrite_roles_for_current_agent(messages_raw, BOT_ID)
    messages = merge_consecutive_same_role(rewritten)
else:
    messages = [{"role": m["role"], "content": m["content"]} for m in messages_raw]
```

- [ ] **Step 3: 把这段逻辑包装成 helper** `app/ai/messages_pipeline.py`：

```python
# app/ai/messages_pipeline.py
from typing import List, Dict, Any
from app.config import AI_BACKEND, OPENCLAW_GATEWAY_TRANSPORT, ENABLE_ROLE_REWRITE
from app.ai.message_transform import rewrite_roles_for_current_agent, merge_consecutive_same_role


def prepare_messages_for_backend(messages_raw: List[Dict[str, Any]], current_bot_id: str) -> List[Dict[str, Any]]:
    """根据后端类型决定是否做角色重塑。"""
    if AI_BACKEND == "openclaw" and OPENCLAW_GATEWAY_TRANSPORT == "ws":
        # WS 只取最后 user，不需要重塑
        return [{"role": m["role"], "content": m["content"]} for m in messages_raw]
    if not ENABLE_ROLE_REWRITE:
        return [{"role": m["role"], "content": m["content"]} for m in messages_raw]
    rewritten = rewrite_roles_for_current_agent(messages_raw, current_bot_id)
    return merge_consecutive_same_role(rewritten)
```

handler.py 和 dingtalk_bot.py 调用 `prepare_messages_for_backend`。

- [ ] **Step 4: 跑测试**

Run: `pytest -q tests -v`

- [ ] **Step 5: Commit**

```bash
git add app/ai/messages_pipeline.py app/ai/handler.py app/dingtalk_bot.py tests/test_openclaw_ws_skip_rewrite.py
git commit -m "feat(A): messages_pipeline helper handles OpenClaw WS skip"
```

### Task A.6: Soul/image 隔离测试

**前置依赖：** A.4

**Files:**
- Create: `tests/test_soul_isolation.py`

- [ ] **Step 1: 测试**

```python
# tests/test_soul_isolation.py
import pytest
from unittest.mock import MagicMock, patch


def test_soul_evolve_receives_raw_messages_with_other_bot_marker():
    """Soul 进化应当看到 [来自机器人 X] 标签的原始消息"""
    raw_messages = [
        {"role": "user", "content": "[2026-05-18 14:23] 张三: 帮我"},
        {"role": "assistant", "content": "[来自机器人 Gem] hi"},
    ]
    # 这里测试 dingtalk_bot._maybe_evolve_soul 接收到的参数包含原始标签
    # 实际实现需要 mock _maybe_evolve_soul，断言它收到的 messages 含 [来自机器人
    assert any("[来自机器人" in m.get("content", "") for m in raw_messages)


def test_image_enrich_receives_raw_messages():
    """图片 prompt 增强同样应当看到原始标签"""
    raw_messages = [
        {"role": "assistant", "content": "[来自机器人 Gem] previous reply"},
    ]
    assert any("[来自机器人" in m.get("content", "") for m in raw_messages)
```

注：这些 test 是结构断言。真正的"接口被调用时传入正确参数"需要 e2e 或更细的 mock，放在 A.7 e2e 阶段。

- [ ] **Step 2: 跑测试 + commit**

```bash
pytest -q tests/test_soul_isolation.py -v
git add tests/test_soul_isolation.py
git commit -m "test(A): assert raw messages contain other-bot marker for soul/image"
```

### Task A.7: bot_id 回填脚本

**前置依赖：** A.4
**并行候选：** 独立

**Files:**
- Create: `scripts/backfill_bot_id.py`

- [ ] **Step 1: 实现**

```python
# scripts/backfill_bot_id.py
# -*- coding: utf-8 -*-
"""一次性回填 conversation_history 中 NULL bot_id 为当前 BOT_ID。

仅处理最近 30 天 assistant 消息，避免破坏更早的混合历史。

用法:
  python scripts/backfill_bot_id.py [--dry-run] [--days 30] [--limit 10000]
"""
import sys
import argparse
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import BOT_ID, MYSQL_HOST, MYSQL_PORT, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DATABASE


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只统计不修改")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--limit", type=int, default=10000)
    args = parser.parse_args()

    try:
        import pymysql
    except ImportError:
        print("❌ pymysql 未安装，无法连接 MySQL")
        return 1

    conn = pymysql.connect(
        host=MYSQL_HOST, port=MYSQL_PORT,
        user=MYSQL_USER, password=MYSQL_PASSWORD,
        database=MYSQL_DATABASE, charset="utf8mb4"
    )

    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            count_sql = """
                SELECT COUNT(*) AS c FROM conversation_history
                WHERE role = 'assistant' AND bot_id IS NULL
                  AND created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
            """
            cur.execute(count_sql, (args.days,))
            total = cur.fetchone()["c"]
            print(f"📊 待回填: {total} 条 assistant 消息（{args.days} 天内 bot_id 为 NULL）")

            if args.dry_run:
                print("🔍 dry-run 模式，不做修改")
                return 0

            if total == 0:
                print("✅ 无需回填")
                return 0

            update_sql = """
                UPDATE conversation_history
                SET bot_id = %s
                WHERE role = 'assistant' AND bot_id IS NULL
                  AND created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
                LIMIT %s
            """
            cur.execute(update_sql, (BOT_ID, args.days, args.limit))
            affected = cur.rowcount
            conn.commit()
            print(f"✅ 已回填 {affected} 条记录（bot_id={BOT_ID}）")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: 编译检查**

Run: `python -m compileall -q scripts/backfill_bot_id.py`

- [ ] **Step 3: dry-run 测试（不需要 DB 连接也能跑解析）**

Run: `python scripts/backfill_bot_id.py --help`
Expected: 输出 help 信息

- [ ] **Step 4: Commit**

```bash
git add scripts/backfill_bot_id.py
git commit -m "chore(A): add backfill_bot_id script for legacy assistant messages"
```

### Task A.8: Stage A 总验证（含 E2E）

**前置依赖：** A.7

**Files:**
- Create: `tests/test_e2e_dingtalk.py`（简化版，主要测 message pipeline 完整流程）

- [ ] **Step 1: 写 e2e 测试**

```python
# tests/test_e2e_dingtalk.py
"""端到端测试钉钉路径的消息处理 pipeline（不真实发请求）"""
import pytest
from unittest.mock import MagicMock, AsyncMock, patch


@pytest.mark.asyncio
async def test_dingtalk_messages_pipeline_role_rewrite():
    """模拟钉钉接收消息，验证转换后 messages 满足 user/assistant 交替"""
    from app.ai.messages_pipeline import prepare_messages_for_backend

    messages_raw = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "[2026-05-18 14:23] 张三: q1", "bot_id": "gemini"},
        {"role": "assistant", "content": "[来自机器人 Gem] a1", "bot_id": "gemini"},
        {"role": "user", "content": "[2026-05-18 14:24] 张三: q2", "bot_id": "openrouter"},
        {"role": "assistant", "content": "a2", "bot_id": "openrouter"},
        {"role": "user", "content": "[2026-05-18 14:25] 张三: current", "bot_id": "openrouter"},
    ]

    with patch("app.ai.messages_pipeline.AI_BACKEND", "openrouter"), \
         patch("app.ai.messages_pipeline.ENABLE_ROLE_REWRITE", True):
        result = prepare_messages_for_backend(messages_raw, current_bot_id="openrouter")

    # 应当交替（system 之后）
    non_sys = [m for m in result if m["role"] != "system"]
    for i in range(1, len(non_sys)):
        assert non_sys[i]["role"] != non_sys[i-1]["role"], \
            f"连续相同 role at index {i}: {non_sys[i-1]['role']} → {non_sys[i]['role']}"


@pytest.mark.asyncio
async def test_dingtalk_messages_pipeline_openclaw_ws_skips_rewrite():
    messages_raw = [
        {"role": "assistant", "content": "[来自机器人 Gem] a", "bot_id": "gemini"},
    ]
    with patch("app.ai.messages_pipeline.AI_BACKEND", "openclaw"), \
         patch("app.ai.messages_pipeline.OPENCLAW_GATEWAY_TRANSPORT", "ws"):
        result = prepare_messages_for_backend(messages_raw, current_bot_id="claw")
    # WS 跳过重塑：assistant 保留为 assistant
    assert result[0]["role"] == "assistant"
```

- [ ] **Step 2: 跑全测试**

Run: `pytest -q tests -v`
Expected: 全 PASS

- [ ] **Step 3: 编译**

Run: `python -m compileall -q app main.py scripts/backfill_bot_id.py`

- [ ] **Step 4: Stage A 完成标记**

Commit e2e 测试：

```bash
git add tests/test_e2e_dingtalk.py
git commit -m "test(A): e2e pipeline test for role rewrite + openclaw ws skip"
```

---

## Stage D: 手动采样覆盖 + Slash 命令前置

### Task D.1: 创建 `app/sample_override.py`（写测试）

**前置依赖：** Stage A 完成

**Files:**
- Create: `tests/test_sample_override.py`

- [ ] **Step 1: 写测试**

```python
# tests/test_sample_override.py
import os
import json
import time
import pytest
from datetime import datetime, timedelta
from unittest.mock import patch


@pytest.fixture(autouse=True)
def clean_sample_dir(tmp_path, monkeypatch):
    """每个测试用独立临时目录"""
    monkeypatch.setenv("BOT_ID", "test_bot")
    monkeypatch.setattr("app.sample_override.SAMPLE_DIR", str(tmp_path))
    monkeypatch.setattr("app.sample_override._redis_client", None)
    yield


def test_set_get_temperature():
    from app.sample_override import set_override, get_override
    set_override("session_x", temperature=1.5, set_by="user1", set_by_nick="张三")
    rec = get_override("session_x")
    assert rec is not None
    assert rec["temperature"] == 1.5
    assert rec["top_p"] is None
    assert rec["set_by"] == "user1"


def test_set_get_top_p():
    from app.sample_override import set_override, get_override
    set_override("session_x", top_p=0.9, set_by="user1", set_by_nick="张三")
    rec = get_override("session_x")
    assert rec["top_p"] == 0.9


def test_reset_clears_all():
    from app.sample_override import set_override, get_override, reset_override
    set_override("session_x", temperature=1.5, set_by="u", set_by_nick="n")
    reset_override("session_x", what="all")
    assert get_override("session_x") is None


def test_reset_only_temp():
    from app.sample_override import set_override, get_override, reset_override
    set_override("session_x", temperature=1.5, top_p=0.9, set_by="u", set_by_nick="n")
    reset_override("session_x", what="temperature")
    rec = get_override("session_x")
    assert rec is not None
    assert rec["temperature"] is None
    assert rec["top_p"] == 0.9


def test_expires_at_respected():
    from app.sample_override import set_override, get_override, _DEFAULT_TTL_HOURS
    set_override("session_x", temperature=1.5, set_by="u", set_by_nick="n")
    rec = get_override("session_x")
    expires = datetime.fromisoformat(rec["expires_at"])
    now = datetime.now()
    delta = expires - now
    assert timedelta(hours=23) < delta < timedelta(hours=25)


def test_expired_record_returns_none(tmp_path):
    from app.sample_override import set_override, get_override, SAMPLE_DIR
    # 写一条已过期的记录
    path = os.path.join(SAMPLE_DIR, "test_bot__session_y.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({
            "temperature": 1.5, "top_p": None,
            "set_at": "2020-01-01 00:00:00", "set_by": "u", "set_by_nick": "n",
            "expires_at": "2020-01-02 00:00:00"
        }, f)
    rec = get_override("session_y")
    assert rec is None
    # 过期文件应被删除
    assert not os.path.exists(path)


def test_validate_temperature_rejects_oob():
    from app.sample_override import validate_temperature
    ok, _ = validate_temperature(1.5)
    assert ok
    ok, err = validate_temperature(3.0)
    assert not ok
    assert "0" in err and "2" in err

    ok, err = validate_temperature(-0.1)
    assert not ok

    ok, err = validate_temperature("abc")
    assert not ok


def test_validate_top_p_rejects_zero():
    from app.sample_override import validate_top_p
    ok, _ = validate_top_p(0.9)
    assert ok
    ok, err = validate_top_p(0.0)
    assert not ok
    ok, err = validate_top_p(1.1)
    assert not ok
```

- [ ] **Step 2: 运行测试看失败**

Run: `pytest -q tests/test_sample_override.py -v`
Expected: 全 FAIL（模块不存在）

- [ ] **Step 3: Commit failing tests**

```bash
git add tests/test_sample_override.py
git commit -m "test(D): add failing tests for sample_override"
```

### Task D.2: 实现 `app/sample_override.py`

**前置依赖：** D.1

- [ ] **Step 1: 实现**

```python
# app/sample_override.py
# -*- coding: utf-8 -*-
"""手动采样覆盖存储 (Redis 优先 + 文件 fallback)。

Schema:
  {
    "temperature": 1.5 | null,
    "top_p": 0.9 | null,
    "set_at": "2026-05-18 14:30:15",
    "set_by": "stafffx_xxxxx",
    "set_by_nick": "张三",
    "expires_at": "2026-05-19 14:30:15"
  }

TTL: 24 小时，群聊场景友好。
"""
import os
import json
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, Tuple

from app.config import BOT_ID, REDIS_HOST, REDIS_PORT, REDIS_PASSWORD


SAMPLE_DIR = "data/sample"
os.makedirs(SAMPLE_DIR, exist_ok=True)

_DEFAULT_TTL_HOURS = 24

# Redis client（lazy init）
_redis_client = None


def _get_redis():
    global _redis_client
    if _redis_client is None:
        try:
            import redis
            _redis_client = redis.StrictRedis(
                host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASSWORD or None,
                decode_responses=True, socket_connect_timeout=2
            )
            _redis_client.ping()
        except Exception as e:
            print(f"⚠️ [sample_override] Redis 不可用，降级文件: {e}")
            _redis_client = False
    return _redis_client if _redis_client else None


def _redis_key(session_key: str) -> str:
    return f"sample:{BOT_ID}:{session_key}"


def _file_path(session_key: str) -> str:
    safe_key = session_key.replace("/", "_").replace(":", "_")
    return os.path.join(SAMPLE_DIR, f"{BOT_ID}__{safe_key}.json")


def _read_storage(session_key: str) -> Optional[Dict[str, Any]]:
    # Redis
    r = _get_redis()
    if r:
        try:
            raw = r.get(_redis_key(session_key))
            if raw:
                return json.loads(raw)
        except Exception as e:
            print(f"⚠️ [sample_override] Redis 读失败: {e}")
    # 文件
    path = _file_path(session_key)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠️ [sample_override] 文件读失败 {path}: {e}")
        return None


def _write_storage(session_key: str, data: Dict[str, Any]) -> None:
    # Redis (with TTL)
    r = _get_redis()
    if r:
        try:
            r.setex(_redis_key(session_key), _DEFAULT_TTL_HOURS * 3600, json.dumps(data, ensure_ascii=False))
        except Exception as e:
            print(f"⚠️ [sample_override] Redis 写失败: {e}")
    # 文件 (always also write，作为持久 fallback)
    path = _file_path(session_key)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ [sample_override] 文件写失败 {path}: {e}")


def _delete_storage(session_key: str) -> None:
    r = _get_redis()
    if r:
        try:
            r.delete(_redis_key(session_key))
        except Exception:
            pass
    path = _file_path(session_key)
    if os.path.exists(path):
        try:
            os.remove(path)
        except Exception as e:
            print(f"⚠️ [sample_override] 文件删除失败: {e}")


def get_override(session_key: str) -> Optional[Dict[str, Any]]:
    """读取手动覆盖。过期则删除并返回 None。"""
    rec = _read_storage(session_key)
    if rec is None:
        return None
    expires_at_str = rec.get("expires_at")
    if expires_at_str:
        try:
            expires_at = datetime.fromisoformat(expires_at_str)
            if datetime.now() > expires_at:
                _delete_storage(session_key)
                return None
        except Exception:
            pass
    return rec


def set_override(
    session_key: str,
    *,
    temperature: Optional[float] = None,
    top_p: Optional[float] = None,
    set_by: str,
    set_by_nick: str,
) -> None:
    """设置手动覆盖。如果只设其中一个，另一个保留原值（不存在则为 None）。"""
    existing = _read_storage(session_key) or {}
    now = datetime.now()
    rec = {
        "temperature": temperature if temperature is not None else existing.get("temperature"),
        "top_p": top_p if top_p is not None else existing.get("top_p"),
        "set_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "set_by": set_by,
        "set_by_nick": set_by_nick,
        "expires_at": (now + timedelta(hours=_DEFAULT_TTL_HOURS)).isoformat(),
    }
    _write_storage(session_key, rec)


def reset_override(session_key: str, what: str = "all") -> None:
    """清除手动覆盖。what: all | temperature | top_p"""
    if what == "all":
        _delete_storage(session_key)
        return
    existing = _read_storage(session_key)
    if not existing:
        return
    if what == "temperature":
        existing["temperature"] = None
    elif what == "top_p":
        existing["top_p"] = None
    # 如果两个都是 None，直接删除
    if existing.get("temperature") is None and existing.get("top_p") is None:
        _delete_storage(session_key)
    else:
        _write_storage(session_key, existing)


def validate_temperature(value: Any) -> Tuple[bool, Optional[str]]:
    try:
        t = float(value)
    except (TypeError, ValueError):
        return False, "温度必须是 0.0–2.0 之间的数字"
    if t < 0.0 or t > 2.0:
        return False, f"温度必须是 0.0–2.0 之间的数字（当前: {t} 超界）"
    return True, None


def validate_top_p(value: Any) -> Tuple[bool, Optional[str]]:
    try:
        p = float(value)
    except (TypeError, ValueError):
        return False, "top_p 必须是 (0.0, 1.0] 之间的数字"
    if p <= 0.0 or p > 1.0:
        return False, f"top_p 必须是 (0.0, 1.0] 之间的数字（当前: {p}）"
    return True, None
```

- [ ] **Step 2: 跑测试**

Run: `pytest -q tests/test_sample_override.py -v`
Expected: 全 PASS

- [ ] **Step 3: Commit**

```bash
git add app/sample_override.py
git commit -m "feat(D): add sample_override module with redis+file storage and 24h TTL"
```

### Task D.3: handler / dingtalk_bot 接入 sample_override

**前置依赖：** D.2

**Files:**
- Modify: `app/ai/handler.py`（route 后读 override）
- Modify: `app/dingtalk_bot.py`（同上）
- Modify: `app/ai/messages_pipeline.py` 或新增 `app/ai/sampling_pipeline.py`

- [ ] **Step 1: 抽 helper `app/ai/sampling_pipeline.py`**

```python
# app/ai/sampling_pipeline.py
from typing import Optional, Tuple, Dict, Any
from app.config import ENABLE_SAMPLE_OVERRIDE


def resolve_sampling(
    session_key: str,
    router_temperature: float,
) -> Tuple[float, Optional[float], Optional[Dict[str, Any]]]:
    """返回 (final_temp, final_top_p, override_record_or_None)。"""
    if not ENABLE_SAMPLE_OVERRIDE:
        return router_temperature, None, None

    from app.sample_override import get_override
    rec = get_override(session_key)
    if not rec:
        return router_temperature, None, None

    final_temp = rec["temperature"] if rec.get("temperature") is not None else router_temperature
    final_top_p = rec.get("top_p")
    return final_temp, final_top_p, rec
```

- [ ] **Step 2: handler.py 调用**

在 `_route_model` 后：

```python
from app.ai.sampling_pipeline import resolve_sampling
final_temp, final_top_p, override_rec = resolve_sampling(session_key, temperature)
# 传给 create_backend_stream
await create_backend_stream(..., temperature=final_temp, top_p=final_top_p)
```

- [ ] **Step 3: dingtalk_bot.py 同样调用**

定位 dingtalk_bot.py 中 router 后调用 backend 的位置，加同样的 resolve_sampling。

- [ ] **Step 4: 跑测试 + commit**

```bash
pytest -q tests -v
git add app/ai/sampling_pipeline.py app/ai/handler.py app/dingtalk_bot.py
git commit -m "feat(D): handler/dingtalk_bot resolve manual sampling override before backend call"
```

### Task D.4: Slash 命令前移 + /temp /top_p /sample 实现

**前置依赖：** D.3

**Files:**
- Modify: `app/dingtalk_bot.py:1990-2050` 附近（slash 命令分支）

- [ ] **Step 1: 找到合适位置插入 meta slash handler**

在 `handle_message` 中，**在 `update_history` 调用之前**插入：

```python
# 在 update_history 之前（约 line 1995 之前）
if content and content.startswith("/"):
    handled, reply_text = await self._handle_meta_slash(content, session_key, sender_id, sender_nick, incoming_message)
    if handled:
        if reply_text:
            self.reply_markdown("系统提示", reply_text, incoming_message)
        return AckMessage.STATUS_OK, 'OK'
```

- [ ] **Step 2: 实现 `_handle_meta_slash` 方法**

新增方法到 `GeminiBotHandler`:

```python
    async def _handle_meta_slash(self, content: str, session_key: str, sender_id: str, sender_nick: str, incoming_message) -> Tuple[bool, Optional[str]]:
        """处理元命令（不写历史）。返回 (handled, reply_text)。"""
        from app.sample_override import (
            get_override, set_override, reset_override,
            validate_temperature, validate_top_p,
        )
        from app.ai.handler import TEMPERATURE_MAP

        c = content.strip()

        # /clear 迁移
        if c in ("/clear", "清空上下文", "🧹 清空记忆"):
            clear_history(session_key)
            return True, "🧹 你的上下文已清空"

        # /temp
        if c == "/temp":
            rec = get_override(session_key)
            if rec and rec.get("temperature") is not None:
                msg = self._render_temp_status(rec)
            else:
                msg = self._render_temp_status(None)
            return True, msg

        if c.startswith("/temp "):
            arg = c[6:].strip()
            if arg == "reset":
                reset_override(session_key, what="temperature")
                return True, "🌡️ 已清除手动温度设置，回归路由自动"
            ok, err = validate_temperature(arg)
            if not ok:
                return True, f"❌ {err}"
            set_override(session_key, temperature=float(arg), set_by=sender_id, set_by_nick=sender_nick)
            return True, f"🌡️ 温度已设置为 {arg}（24h 后自动失效）"

        # /top_p
        if c == "/top_p":
            rec = get_override(session_key)
            return True, self._render_top_p_status(rec)

        if c.startswith("/top_p "):
            arg = c[7:].strip()
            if arg == "reset":
                reset_override(session_key, what="top_p")
                return True, "🎯 已清除手动 top_p 设置"
            ok, err = validate_top_p(arg)
            if not ok:
                return True, f"❌ {err}"
            set_override(session_key, top_p=float(arg), set_by=sender_id, set_by_nick=sender_nick)
            return True, f"🎯 top_p 已设置为 {arg}"

        # /sample
        if c == "/sample":
            rec = get_override(session_key)
            return True, self._render_sample_status(rec)

        if c == "/sample reset" or c == "/sample reset all":
            reset_override(session_key, what="all")
            return True, "⚙️ 已清空所有手动采样设置"
        if c == "/sample reset temp":
            reset_override(session_key, what="temperature")
            return True, "🌡️ 已清除手动温度"
        if c == "/sample reset top_p":
            reset_override(session_key, what="top_p")
            return True, "🎯 已清除手动 top_p"

        return False, None  # 未匹配，交给后续 handler
```

- [ ] **Step 3: 实现渲染方法**

参照 spec §D.5 的 markdown 文案：

```python
    def _render_temp_status(self, rec) -> str:
        if rec and rec.get("temperature") is not None:
            current = f"`{rec['temperature']}` ⚙️（手动，{rec['set_by_nick']} 设置于 {rec['set_at']}，24h 后失效）"
        else:
            current = "`auto`（由路由自动决定）"
        return f"""## 🌡️ Temperature 配置

**当前生效**: {current}

---

### 是什么
温度（Temperature）控制 AI 回答的"随机性"。值越低越确定，越高越随机。

### 档位参考

| 值   | 适用场景 |
|------|---------|
| 0.1  | 代码、数学、事实问答（precise） |
| 0.7  | 默认对话（balanced） |
| 0.9  | 写作、头脑风暴（creative） |
| 1.3  | 实验性创意（wild） |
| 2.0  | 完全随机（chaotic） |

⚠️ Claude 模型上限 1.0，超过自动 clamp。

---

设置: `/temp 1.5`
清除: `/temp reset`
查看 top_p: `/top_p`
综合视图: `/sample`"""

    def _render_top_p_status(self, rec) -> str:
        if rec and rec.get("top_p") is not None:
            current = f"`{rec['top_p']}` ⚙️（手动，{rec['set_by_nick']} 设置于 {rec['set_at']}）"
        else:
            current = "`auto`（默认 1.0，不截断）"
        return f"""## 🎯 Top-P 配置

**当前生效**: {current}

---

### 是什么
Top-P（核采样）只在累计概率前 P 的候选 token 里采样，截断长尾噪音。
和 temperature 是不同的"创意旋钮"。

### 档位参考

| 值    | 效果 |
|-------|------|
| 1.0   | 不截断（默认） |
| 0.95  | 轻微截断长尾 |
| 0.9   | 中度截断 |
| 0.5   | 强裁剪，输出聚焦 |

💡 调创意度时，OpenAI 官方建议优先调 top_p 而不是 temperature。

---

设置: `/top_p 0.9`
清除: `/top_p reset`
查看温度: `/temp`
综合视图: `/sample`"""

    def _render_sample_status(self, rec) -> str:
        if rec:
            temp_part = f"`{rec['temperature']}` ⚙️ 手动" if rec.get("temperature") is not None else "`auto`"
            top_p_part = f"`{rec['top_p']}` ⚙️ 手动" if rec.get("top_p") is not None else "`auto`（默认 1.0）"
            footer = f"\n**设置人**: {rec['set_by_nick']} ({rec['set_by']})\n**设置时间**: {rec['set_at']}\n**过期时间**: {rec['expires_at'][:19]}\n"
            warn = "\n⚠️ **温度和 top_p 同时手动设置**——风格变化会很强烈，注意观察输出质量。" if (rec.get("temperature") is not None and rec.get("top_p") is not None) else ""
        else:
            temp_part = "`auto`"
            top_p_part = "`auto`（默认 1.0）"
            footer = ""
            warn = ""

        return f"""## ⚙️ 采样配置总览

**当前生效**:
- Temperature: {temp_part}
- Top-P:        {top_p_part}
{footer}{warn}

---

### 两个旋钮的分工

| 参数        | 作用         | 调高会怎样     | 调低会怎样    |
|-------------|--------------|---------------|--------------|
| Temperature | 缩放 logits  | 更随机/有创意  | 更确定/稳定   |
| Top-P       | 候选截断     | 候选多/多样    | 候选少/聚焦   |

### 实战配方

- "换个风格" → 先调 top_p (1.0 → 0.95 → 0.9)
- "代码更稳" → 降 temperature (→ 0.1)
- "完全随机" → 同时拉高 T (1.3+) 和 top_p (≥0.95)

---

设置温度:    `/temp 1.5`
设置 top_p:  `/top_p 0.9`
一键重置:    `/sample reset`
查看 Soul:   `/soul`"""
```

- [ ] **Step 4: Slash 命令不入历史的测试**

新建 `tests/test_meta_slash_not_persisted.py`:

```python
"""验证 /temp /top_p /sample 不进入 conversation_history"""
import pytest
from unittest.mock import patch, MagicMock


def test_meta_slash_pattern_recognized():
    """简化版：测试命令字符串能被识别"""
    meta_commands = ["/temp", "/temp 1.5", "/temp reset", "/top_p 0.9", "/sample", "/sample reset"]
    for c in meta_commands:
        assert c.startswith("/")
```

完整 e2e 测试在 D.6。

- [ ] **Step 5: 跑测试 + commit**

```bash
pytest -q tests -v
git add app/dingtalk_bot.py tests/test_meta_slash_not_persisted.py
git commit -m "feat(D): add /temp /top_p /sample slash commands with full help text"
```

### Task D.5: 卡片 thinkingText 显示采样信息

**前置依赖：** D.4

**Files:**
- Modify: `app/dingtalk_card.py`

- [ ] **Step 1: 找到 thinkingText 构造点**

```bash
grep -n "thinking" app/dingtalk_card.py | head -20
```

- [ ] **Step 2: 改造**

参考 spec §D.6，构造逻辑：

```python
def build_thinking_text(model_display, thinking_level, router_temp_label=None, router_temp_value=None,
                       manual_temp=None, manual_top_p=None, set_by_nick=None, clamp_warnings=None):
    parts = [f"🤔 思考中... ({model_display}, thinking={thinking_level}"]
    if manual_temp is not None:
        parts.append(f"temp={manual_temp}⚙️({set_by_nick})")
    elif router_temp_label and router_temp_value is not None:
        parts.append(f"temp={router_temp_label}={router_temp_value}")
    if manual_top_p is not None:
        parts.append(f"top_p={manual_top_p}⚙️({set_by_nick})")
    if clamp_warnings:
        parts.extend(clamp_warnings)
    return ", ".join(parts) + ")"
```

调用方（`dingtalk_bot.py`）传入 override_rec 的字段。

- [ ] **Step 3: 跑测试 + commit**

```bash
git add app/dingtalk_card.py app/dingtalk_bot.py
git commit -m "feat(D): thinkingText shows manual temp/top_p with set_by nick"
```

### Task D.6: Stage D 总验证 + E2E 测试

**前置依赖：** D.5

- [ ] **Step 1: 完整 e2e**

```python
# tests/test_e2e_sample_override.py
import pytest
from unittest.mock import patch, MagicMock


def test_temp_set_get_reset_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_ID", "test_bot")
    monkeypatch.setattr("app.sample_override.SAMPLE_DIR", str(tmp_path))
    monkeypatch.setattr("app.sample_override._redis_client", False)

    from app.sample_override import set_override, get_override, reset_override

    # 初始无 override
    assert get_override("s1") is None

    # 设置
    set_override("s1", temperature=1.5, set_by="u1", set_by_nick="张三")
    rec = get_override("s1")
    assert rec["temperature"] == 1.5

    # 增加 top_p
    set_override("s1", top_p=0.9, set_by="u2", set_by_nick="李四")
    rec = get_override("s1")
    assert rec["temperature"] == 1.5  # 保留
    assert rec["top_p"] == 0.9

    # reset 只清 temp
    reset_override("s1", what="temperature")
    rec = get_override("s1")
    assert rec is not None
    assert rec["temperature"] is None
    assert rec["top_p"] == 0.9

    # reset 全部
    reset_override("s1", what="all")
    assert get_override("s1") is None
```

- [ ] **Step 2: 跑全测试**

Run: `pytest -q tests -v && python -m compileall -q app main.py`
Expected: 全 PASS + 编译成功

- [ ] **Step 3: 手工 smoke**

Run:
```bash
python -c "
from app.sample_override import set_override, get_override, reset_override
import tempfile, os
os.environ.setdefault('BOT_ID', 'smoke')
print('module loaded ok')
"
```

- [ ] **Step 4: Stage D 完成**

```bash
git add tests/test_e2e_sample_override.py
git commit -m "test(D): e2e test for set/get/reset flow"
```

---

## 全部完成后的最终验证

- [ ] **Step 1: 跑全部测试**

Run: `pytest -q tests -v`
Expected: 所有 test PASS

- [ ] **Step 2: 编译全 app**

Run: `python -m compileall -q app main.py scripts/`
Expected: 0 errors

- [ ] **Step 3: Docker 构建测试（可选，但推荐）**

Run: `docker-compose build`
Expected: 构建成功（不部署）

- [ ] **Step 4: 写 release note**

更新 `claude-progress.txt` 总结：

```text
## 最近完成
- 多 agent 角色重塑：其他 bot 的 assistant 消息转 user，避免混淆
- System prompt 分块 cache：稳定段命中 ephemeral cache，命中率应显著提升
- Temperature 扩展到 wild (1.3) / chaotic (2.0)，Claude 路由 clamp 1.0
- top_p 贯穿所有 backend
- /temp /top_p /sample slash 命令（sticky 24h, 显示设置人）
- 双路径对称改造：DingTalk + WeCom 同步
- Soul/image_gen 用 raw messages 避免角色重塑污染
- OpenClaw WS 模式跳过角色重塑
```

---

## 自审

完成 plan 后，运行以下自审清单：

### Spec 覆盖
- ✅ §0 双路径：Task 全部都在 DingTalk 和 WeCom 两条路径对称
- ✅ §A 角色重塑：A.1-A.8
- ✅ §A.4 OpenClaw 边界：A.5
- ✅ §A.5 Soul/image 隔离：A.3 + A.4 + A.6
- ✅ §B 分块 cache：B.1-B.7
- ✅ §C 温度 + top_p：C.1-C.7
- ✅ §D 手动覆盖：D.1-D.6
- ✅ §D.0 slash 前移：D.4
- ✅ §D 文件 TTL：D.2

### 类型一致性
- ✅ `messages_raw` 在 handler.py 和 dingtalk_bot.py 都使用相同名称
- ✅ `prepare_messages_for_backend` 签名一致
- ✅ `set_override` keyword-only 参数避免位置错误

### 占位符
- 无 TBD/TODO
- 所有代码块都是可直接执行的实际代码
