# 多 Agent 角色重塑 + Cache 优化 + 采样可控化 设计

日期: 2026-05-18
状态: Draft v2（经过 codex 对抗评审修订）

## 概述

本 spec 同时解决四个相互关联的问题：

| 编号 | 问题 | 用户视角 | 技术根因 |
|------|------|---------|---------|
| A | Agent 角色混淆 | Claude/OpenRouter 把其他 bot 的回复当成"自己说过的话"接续 | 多 bot 共享 session 时，所有 bot 的回复都以 `role: assistant` 注入；文本前缀 `[来自X]` 弱于 role 训练语义 |
| B | System prompt cache miss | OpenRouter→Anthropic 链路 cache 命中率几乎为 0 | system prompt 含秒级时间戳 + Soul + 群名等变动内容混在 cache breakpoint 内，每次请求都失效 |
| C | 温度档位不足 + top_p 未启用 | 用户想试 >0.9 的温度，想调 top_p | `TEMPERATURE_MAP` 只有 precise/balanced/creative；top_p 完全未接入 |
| D | 采样参数不可控 | 用户无法手动指定 temp/top_p 做实验 | 无 slash 命令入口，无 per-session 覆盖存储 |

## 设计原则

1. **Role 语义对齐物理语义**：`role: assistant` = "当前 bot 说过的话"，其他全部归到 `user`。
2. **存储不动**：所有改动只影响**发给模型前的临时转换**，DB 中 `conversation_history` 保持原 role + bot_id。
3. **Cache 友好**：变动信息放 breakpoint **之外**；稳定信息分块放 breakpoint **之内**（B 的关键升级）。
4. **Provider-aware clamp**：温度/top_p 上限因 provider 不同，clamp 在各 backend client 内部，silent + warning。
5. **手动覆盖可见**：非默认参数必须在 thinkingText 显眼提示（⚙️ 图标 + 数值 + 设置人）。
6. **双路径同步**：所有改动**同时落到 DingTalk 和 WeCom 路径**，避免分叉。

---

## §0 集成范围与双路径处理

### 0.1 当前架构现状（codex 评审揭示）

项目存在**两条并行的主链路**，两者结构相似但代码独立：

| 路径 | 入口 | 主体位置 |
|------|------|---------|
| **DingTalk** | `app/dingtalk_bot.py` `handle_ai_stream()` | line 1116–1236（system_prompt 构造 + _format_history 等价逻辑 + 当前消息追加） |
| **WeCom + 统一层** | `app/ai/handler.py` `AIHandler.process_message()` | line 142–250（同上逻辑，但封装为类方法） |

历史原因：项目最早只支持 DingTalk，后来增加 WeCom 时抽出 AIHandler，但 DingTalk 保留了内联实现（卡片流式更新等钉钉专属逻辑与之耦合）。

### 0.2 本次 spec 的策略

**不做 AIHandler 大一统重构**（改动太大，独立 spec 处理）。本次所有改动**对称落地到两条路径**：

| 改动项 | DingTalk 位置 | WeCom 位置 |
|--------|---------------|------------|
| system_prompt 构造 | `dingtalk_bot.py:1116-1155` | `handler.py:255-292` |
| history 格式化 | `dingtalk_bot.py:1163-1192` | `handler.py:294-319` |
| 当前消息追加 | `dingtalk_bot.py:1194-1236` | `handler.py:155-186` |
| Slash 命令分支 | `dingtalk_bot.py:2010+` | `wecom/bot.py` 同等位置 |
| 卡片/回复 thinkingText | `dingtalk_card.py` | wecom 无卡片，跳过 |

### 0.3 共享代码抽取

为避免双路径手改导致后续分叉，**新建模块统一持有纯逻辑**：

```text
app/ai/message_transform.py    — rewrite_roles, merge_consecutive 等纯函数
app/ai/system_prompt.py        — build_system_prompt_blocks(group, soul, bot_id) → blocks
app/sample_override.py         — 手动采样存储/读取
```

两条主链路调用这些共享模块，但 system message 拼接、卡片更新等平台耦合逻辑保留在各自路径。

### 0.4 验收门槛

- 单元测试覆盖共享模块
- **端到端测试两条路径**：钉钉模拟消息 + WeCom 模拟消息各跑一遍核心 4 个场景

---

## §A Agent 角色重塑

### A.1 数据流契约（修订：merge 必须在追加当前 user 之后）

```text
DB conversation_history (持久层)
  schema: {role, content, sender_nick, bot_id, timestamp, created_at}
        │
        ▼ get_history(session_key) → List[Dict]
        │
        ▼ _format_history_with_meta(messages, current_bot_id)
        │   ─ 加文本前缀（时间戳+昵称、bot 名）
        │   ─ 保留 bot_id 字段（供下一步用）
        │   ─ 输出: [{role, content, bot_id?}, ...]
        │
        ▼ rewrite_roles_for_current_agent(messages, current_bot_id)
        │   ─ 按 bot_id 决定 assistant→user 转换
        │   ─ STRIP bot_id 字段
        │   ─ 输出: [{role, content}, ...]
        │
        ▼ append_current_user_message(messages, current_content, images)
        │   ─ 把当前消息以 role:user 加在末尾
        │   ─ 多模态: content 为 List[Dict]
        │
        ▼ merge_consecutive_same_role(messages)        ← 关键：在追加当前 user 之后做合并
        │   ─ 处理"历史尾部 user + 当前 user"的连续 user
        │   ─ 字符串用 \n\n 拼接；list 内容用规则 §A.2
        │
        ▼ prepend system message
        │
        ▼ backend client
```

**关键不变量**：
- 出 `rewrite_roles_for_current_agent` 后 dict 只剩 `role` 和 `content`（移除 `bot_id` 避免泄漏到 SDK）
- 出 `merge_consecutive_same_role` 后保证整个非 system 列表 user/assistant **严格交替**

### A.1.1 转换示例（当前 bot = `openrouter`）

DB 历史：
```python
[
  {role: "user",      content: "帮我写代码",  timestamp: "2026-05-18 14:23:10", sender_nick: "张三", bot_id: "gemini"},
  {role: "assistant", content: "Gemini 的回答...", bot_id: "gemini"},
  {role: "user",      content: "不对，改一下", timestamp: "2026-05-18 14:25:32", sender_nick: "张三", bot_id: "openrouter"},
  {role: "assistant", content: "OpenRouter 的回答...", bot_id: "openrouter"},
]
```

本轮用户消息: `"再问一个"` (无图片)

**Step 1** `_format_history_with_meta`：
```python
[
  {role: "user",      content: "[2026-05-18 14:23:10] 张三: 帮我写代码",   bot_id: "gemini"},
  {role: "assistant", content: "[来自机器人 Gem] Gemini 的回答...",          bot_id: "gemini"},
  {role: "user",      content: "[2026-05-18 14:25:32] 张三: 不对，改一下", bot_id: "openrouter"},
  {role: "assistant", content: "OpenRouter 的回答...",                       bot_id: "openrouter"},  # 当前 bot 不加前缀
]
```

**Step 2** `rewrite_roles_for_current_agent`：
```python
[
  {role: "user",      content: "[2026-05-18 14:23:10] 张三: 帮我写代码"},
  {role: "user",      content: "[来自机器人 Gem] Gemini 的回答..."},    # assistant→user
  {role: "user",      content: "[2026-05-18 14:25:32] 张三: 不对，改一下"},
  {role: "assistant", content: "OpenRouter 的回答..."},
]
```

**Step 3** `append_current_user_message`：
```python
[
  {role: "user",      content: "[2026-05-18 14:23:10] 张三: 帮我写代码"},
  {role: "user",      content: "[来自机器人 Gem] Gemini 的回答..."},
  {role: "user",      content: "[2026-05-18 14:25:32] 张三: 不对，改一下"},
  {role: "assistant", content: "OpenRouter 的回答..."},
  {role: "user",      content: "[2026-05-18 14:30:01] 张三: 再问一个"},  # 当前消息
]
```

**Step 4** `merge_consecutive_same_role`：
```python
[
  {role: "user", content:
    "[2026-05-18 14:23:10] 张三: 帮我写代码\n\n"
    "[来自机器人 Gem] Gemini 的回答...\n\n"
    "[2026-05-18 14:25:32] 张三: 不对，改一下"
  },
  {role: "assistant", content: "OpenRouter 的回答..."},
  {role: "user", content: "[2026-05-18 14:30:01] 张三: 再问一个"},
]
```

### A.2 多模态合并规则

历史在 DB 中**只存文本**（图片是 `[图片xN]` 占位符，见 `dingtalk_bot.py:1724-1728`），所以历史里没有真正的 image_url list。唯一的多模态来源是**本轮请求**（`dingtalk_bot.py:1201-1224`、`handler.py:160-175`）。

merge 规则：
- 两条都是 str → str 拼接（`\n\n` 分隔）
- 一条 str 一条 list → str 包装成 `{type: "text", text: ...}`，**插到 list 头部**，list 第一个 text part 与原 str 用 `\n\n` 分隔（让历史文本上下文出现在图片之前）
- 两条都是 list → list 顺序拼接；如果两个 list 都以 text 部分开头，相邻 text 部分用 `\n\n` 合并
- 边界："历史尾部 user + 当前图片 user" 的合并：历史文本块 + 当前的 `{type:text, text:"..."}` + 图片列表

**LiteLLM 非视觉模型剥离图片**（已有逻辑 `litellm_client.py:_strip_images` line 205-206）：merge 后的 list 仍然能被正确剥离（提取所有 `type:text` 拼接）。

### A.3 bot_id=None 历史遗留（修订：保守策略）

数据库 `bot_id` 列允许 NULL（`database.py:115-121`），旧数据普遍 NULL。codex 评审指出：若一律转 user 会**严重削弱旧历史的多轮连续性**。

**新策略**：
- `assistant + bot_id is None` → **保留为 assistant 角色**（不转换）
- 仅 `assistant + bot_id != current_bot_id` 才转 user
- 部署后**附带一次性回填脚本** `scripts/backfill_bot_id.py`：把当前 BOT_ID 的最近 30 天 NULL assistant 消息回填为当前 BOT_ID

文档警告：若旧历史中包含多个老 bot 的混合输出（且都没 bot_id），仍会有混淆，但属于历史包袱，业务影响通过回填脚本和时间衰减自然消化。

### A.4 OpenClaw 边界（新增）

OpenClaw 有两个 transport，行为不同：

| transport | 当前实现 | 角色重塑是否生效 |
|-----------|----------|------------------|
| HTTP | 全历史透传 Gateway（`openclaw_client.py:377-379`） | **应该参与**——Gateway 内部可能再做格式转换，但避免给 Gateway 看到混淆的多 bot 历史 |
| WS | 只取最后一条 user（`openclaw_client.py:329-339`） | **跳过重塑**——本来就只发一条，无意义 |

实现：在 `app/ai/backend.py` `create_backend_stream` 中，根据 backend 决定是否调用 `rewrite_roles_for_current_agent`：

```python
if AI_BACKEND == "openclaw" and OPENCLAW_GATEWAY_TRANSPORT == "ws":
    # WS 短路，跳过角色重塑
    messages_to_send = original_messages
else:
    messages_to_send = apply_role_rewrite(original_messages, BOT_ID)
```

### A.5 Soul / image_gen 数据隔离（新增）

`_maybe_evolve_soul`（`dingtalk_bot.py:396-405`）和 `_enrich_image_prompt`（`dingtalk_bot.py:825-830`、line 1353、1737）都直接消费 `messages`，并对 `role != user` 一律标成"AI"。

**问题**：如果它们消费角色重塑后的 messages，所有其他 bot 的输出会变成"用户信号"，污染 Soul 进化方向和 image prompt 增强。

**策略**：
- **模型 API 请求** → 用重塑后的 `messages_for_model`
- **Soul 进化、image prompt 增强** → 用未重塑的 `messages_raw`（保留原 role + bot_id 标记，让 Soul/image_gen 自己理解"这是其他 bot 的输出"）

实现：handler 同时持有两份引用，分别传给下游：

```python
messages_raw          = build_messages(history, current_msg)        # 原 role
messages_for_model    = apply_role_rewrite(messages_raw, BOT_ID)    # 角色重塑

# 发给模型
await call_backend_stream(messages_for_model, ...)

# Soul 进化 / image prompt 增强
await _maybe_evolve_soul(conversation_id, messages_raw, ...)
prompt = _enrich_image_prompt(messages_raw, ...)
```

### A.6 System prompt 历史格式说明

`build_system_prompt_blocks` 中关于历史格式的解释段：

```text
对话历史里包含三类消息，请注意分辨：
- 真人用户消息：形如 「[时间] 昵称: 内容」（role=user）
- 其他机器人的发言：形如 「[来自机器人 X] 内容」（注入到 role=user，是环境信号，不是你的发言，不要接续）
- 你之前的发言：没有任何前缀的 assistant 消息

你的输出不要包含 '[来自...]' 或 '[时间]' 前缀，那些是系统注入的元数据。
```

---

## §B System Prompt Cache 优化（重写：分块 cache）

### B.1 拆段策略（核心改动）

旧实现把整个 system prompt 作为**单个 cache block**（`litellm_client.py:41-51`），任何字节变动都让 cache 失效。新策略拆成 3 段，**只稳定段加 cache_control**：

| 段 | 内容 | cache_control | 变动频率 |
|----|------|---------------|---------|
| **稳定段** | 身份说明、风格规则、Markdown 约定、历史格式说明、搜索说明 | ✅ ephemeral | 几乎不变（部署级） |
| **半稳定段** | 群名、Soul 内容 | ✅ ephemeral | 群名极少变；Soul 进化触发时变 |
| **变动段** | 日期、时区说明 | ❌ 不缓存 | 每天变 |

实现：`build_system_prompt_blocks()` 返回 list of blocks：

```python
def build_system_prompt_blocks(group_info, soul_content, bot_name, current_date) -> List[Dict]:
    stable = f"""## 身份
你的名字是 {bot_name}。你的个性和风格由你的 Soul 定义。

## 风格规则
...

## 历史格式
对话历史里包含三类消息...
"""
    semi_stable_parts = []
    if group_info:
        semi_stable_parts.append(f"当前群聊: '{group_info['name']}'")
    if soul_content:
        semi_stable_parts.append(f"{bot_name} 的个性设定:\n{soul_content}")
    semi_stable = "\n\n".join(semi_stable_parts) if semi_stable_parts else None

    weekday_cn = "一二三四五六日"[current_date.weekday()]
    dynamic = f"## 时间\n今天是 {current_date.year} 年 {current_date.month} 月 {current_date.day} 日（周{weekday_cn}，北京时间 UTC+8）。"

    blocks = [{"type": "text", "text": stable, "cache_control": {"type": "ephemeral"}}]
    if semi_stable:
        blocks.append({"type": "text", "text": semi_stable, "cache_control": {"type": "ephemeral"}})
    blocks.append({"type": "text", "text": dynamic})  # 不加 cache_control
    return blocks
```

### B.2 _inject_cache_control 改造

旧版 (`litellm_client.py:41-52`) 假设 system content 是字符串。新版需要接受 content 已经是 list of blocks：

```python
def _inject_cache_control(messages, model):
    if not model.startswith("anthropic/") and "claude" not in model.lower():
        return messages
    result = []
    for msg in messages:
        if msg.get("role") == "system":
            content = msg.get("content", "")
            if isinstance(content, str) and content:
                # 旧路径：单 block
                msg = {**msg, "content": [{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}]}
            elif isinstance(content, list):
                # 新路径：list of blocks，已含或未含 cache_control，原样透传
                pass
        result.append(msg)
    return result
```

### B.3 双路径同步

| 路径 | 现位置 | 改造 |
|------|--------|------|
| DingTalk | `dingtalk_bot.py:1116-1155`（拼接到字符串） | 改为调用 `build_system_prompt_blocks` 并以 list of blocks 形式赋给 system message content |
| WeCom | `handler.py:255-292`（同上） | 同上 |

### B.4 兼容性

- LiteLLM 对 system message 支持 list of blocks（已被 `_inject_cache_control` 用过）
- Gemini SDK：`_convert_messages_to_gemini` 仅取 system 的 string，不支持 list blocks——需要扩展（提取所有 text part 拼接）
- OpenClaw HTTP：会被 Gateway 接管，list 形式可能不被支持，保险起见**仅在 LiteLLM 路径启用分块 cache**，其他后端仍用 string 拼接（向下兼容）

实现：handler 根据 backend 决定输出格式：
- LiteLLM → list of blocks
- Gemini / OpenClaw → string 拼接（性能损失但功能可用）

---

## §C Temperature 档位扩展 + Top_p 贯穿

### C.1 档位表

`app/ai/handler.py`：

```python
TEMPERATURE_MAP = {
    "precise":  0.1,
    "balanced": 0.7,
    "creative": 0.9,
    "wild":     1.3,
    "chaotic":  2.0,
}
```

top_p 无预设档位（连续值，用户/路由直接给数字）。

### C.2 参数贯穿（重大改动）

`top_p` 当前完全未接入。需要从入口贯穿到各 client：

```text
handler / dingtalk_bot
        │ temperature, top_p
        ▼
create_backend_stream(messages, target_model, thinking_level, enable_search, conversation_id, temperature, top_p=None)
        │
        ├── call_gemini_stream(..., temperature, top_p)
        ├── call_litellm_stream(..., temperature, top_p)
        └── call_openclaw_*_stream(..., temperature, top_p)
```

各 client 接收 top_p（可为 None）：
- **LiteLLM**: 直接传给 `litellm.acompletion(..., top_p=top_p)` 如果非 None
- **Gemini**: `GenerationConfig(temperature=..., top_p=top_p)` 如果非 None
- **OpenClaw HTTP**: 放进 body `{"top_p": top_p}`，Gateway 忽略不报错；catch 任何 422 错误后单次 warning + retry without top_p
- **OpenClaw WS**: 不支持，silently ignore

### C.3 Per-backend Clamp

```python
# app/ai/sampling_clamp.py（新增模块，被各 client 复用）
def clamp_temperature(t: float, model: str) -> Tuple[float, Optional[str]]:
    """返回 (clamped_value, warning_message_or_None)"""
    if t is None:
        return None, None
    is_claude = "claude" in model.lower() or model.startswith("anthropic/")
    if is_claude and t > 1.0:
        return 1.0, f"⚠️ Claude 不支持 t>1.0，已 clamp {t:.2f}→1.0"
    return max(0.0, min(t, 2.0)), None

def clamp_top_p(p: float) -> Tuple[float, Optional[str]]:
    if p is None:
        return None, None
    if p <= 0 or p > 1.0:
        clamped = max(0.01, min(p, 1.0))
        return clamped, f"⚠️ top_p 超界，clamp {p:.2f}→{clamped:.2f}"
    return p, None
```

各 client 入口调用 clamp，收集 warning 列表，**通过 yield 一条 thinking 块或 callback** 让 warning 进入卡片 thinkingText（用户可见）。

### C.4 Router prompt 更新

`analyze_complexity_with_model`（Gemini）和 `analyze_complexity_with_openrouter`（OpenRouter）的 prompt 更新：

```text
temperature 选择规则:
- precise (0.1): 代码、数学、事实问答
- balanced (0.7): 通用对话
- creative (0.9): 写作、头脑风暴
- wild (1.3): 用户**明确表态**要"换个风格"、"再脑洞大开点"
- chaotic (2.0): 用户**明确表态**要"完全随机"、"瞎写"

默认 balanced。仅当用户明确要求探索性输出时才选高档位。
不要因为"问题看起来有创意感"就主动选 wild/chaotic。
```

Router **不输出 top_p**——top_p 仅由用户手动设置（D），未手动时为 None（不传给 API，沿用模型默认 1.0）。

---

## §D 手动采样覆盖

### D.0 Slash 命令位置（关键修订）

**当前问题**：`dingtalk_bot.py:1995` 在判断 slash 命令之前就 `update_history()` 写历史。如果在 line 2010+ 加 `/temp` 分支，命令会先进入对话历史，再被 reference/Soul/模型上下文消费。

**修订**：所有 sample slash 命令必须**提前到 `update_history` 之前**检测并直接 return。

改造方案：在 `handle_message` 入口附近、`update_history` 之前增加一个统一的 slash 分发：

```python
# 在 update_history 之前
if content and content.startswith("/"):
    handled = await _handle_meta_slash_commands(content, session_key, sender_id, incoming_message)
    if handled:
        return AckMessage.STATUS_OK, 'OK'
# 然后才 update_history
```

`_handle_meta_slash_commands` 集中处理 `/temp`、`/top_p`、`/sample`、`/sample reset`、`/soul`（迁移）、`/clear`（迁移）、`/stats`（迁移）——所有"元命令"统一前移，不进历史。

**测试**：必须有 `test_meta_slash_commands_not_persisted` 断言历史里没有 slash 文本。

### D.1 命令集

| 命令 | 行为 |
|------|------|
| `/temp` | 查看当前温度 |
| `/temp 1.5` | 设置温度，sticky 24h |
| `/temp reset` | 清除 |
| `/top_p` / `/top_p 0.9` / `/top_p reset` | 同上 |
| `/sample` | 综合视图 |
| `/sample reset` | 一键清两个 |

### D.2 存储模型（修订）

| 项 | 旧 | 新 |
|----|----|----|
| Redis TTL | 7 天 | **24 小时**（群聊场景更友好） |
| 文件路径 | `data/sample/{BOT_ID}__{cid}.json` | 同上 |
| 文件 schema | 缺 expires_at | **加 expires_at 字段** |
| 文件过期处理 | 永久 | **read 时检查 expires_at，过期则忽略并删除** |

```json
{
  "temperature": 1.5,
  "top_p": 0.9,
  "set_at": "2026-05-18 14:30:15",
  "set_by": "stafffx_xxxxx",
  "set_by_nick": "张三",
  "expires_at": "2026-05-19 14:30:15"
}
```

```python
# app/sample_override.py
def get_override(session_key):
    raw = _read_redis_or_file(session_key)
    if raw is None:
        return None
    if raw.get("expires_at") and datetime.now() > parse(raw["expires_at"]):
        _delete(session_key)
        return None
    return raw
```

### D.3 群聊作用域

仍按 `session_key` 共享（per-user 隔离作为未来扩展）。**显示设置人**降低混淆：
- `/sample` 命令输出显示 `**设置人**: 张三 (stafffx_xxxxx)`
- thinkingText 显示 `temp=1.5⚙️(张三)` 让其他人知道是谁设的

### D.4 优先级链

```python
router_temp_label, router_temp_value, router_top_p_unused = await self._route_model(...)

override = sample_override.get(session_key)
if override and override.get("temperature") is not None:
    final_temp = override["temperature"]
    temp_source = "manual"
else:
    final_temp = router_temp_value
    temp_source = "auto"

final_top_p = override.get("top_p") if override else None  # None 表示不传给 API

# Per-backend clamp 在 client 内部
await create_backend_stream(..., temperature=final_temp, top_p=final_top_p)
```

### D.5 Slash 输出文案

#### `/temp` 输出

```markdown
## 🌡️ Temperature 配置

**当前生效**: `1.5` ⚙️（手动，张三设置于 2026-05-18 14:30，24h 后失效）

或未手动时:
**当前生效**: `auto`（路由自动: creative=0.9）

---

### 是什么
温度（Temperature）控制 AI 回答的"随机性"。在 softmax 前对 logits 做缩放——
值越低，分布越尖锐，越倾向选最可能的 token（更确定/保守）；
值越高，分布越平坦，低概率 token 也有机会出来（更随机/有创意）。

### 档位参考

| 值   | 适用场景 |
|------|---------|
| 0.1  | 代码、数学、事实问答（precise） |
| 0.7  | 默认对话（balanced） |
| 0.9  | 写作、头脑风暴（creative） |
| 1.3  | 实验性创意（wild） |
| 2.0  | 完全随机（chaotic，容易格式崩坏） |

⚠️ Claude 模型上限 1.0，超过自动 clamp（thinkingText 会提示）。
💡 建议：和 top_p 配合使用，不要同时拉到极端。

---

设置: `/temp 1.5`
清除: `/temp reset`
查看 top_p: `/top_p`
综合视图: `/sample`
```

#### `/top_p` 输出（类似结构）

`/top_p` 介绍段强调"和 temperature 通常二选一调节"，提醒同时拉高有风险。

#### `/sample` 输出（综合视图，显示设置人和过期）

```markdown
## ⚙️ 采样配置总览

**当前生效**:
- Temperature: `1.5` ⚙️ 手动（路由档位: creative=0.9）
- Top-P:        `0.9` ⚙️ 手动（默认 1.0）

**设置人**: 张三 (stafffx_xxxxx)
**设置时间**: 2026-05-18 14:30
**过期时间**: 2026-05-19 14:30（约 23h 后）

⚠️ **温度和 top_p 同时手动设置**——风格变化会很强烈，注意观察输出质量。

---

### 两个旋钮的分工

| 参数        | 作用         | 调高会怎样      | 调低会怎样     |
|-------------|--------------|----------------|---------------|
| Temperature | 缩放 logits  | 更随机/有创意   | 更确定/稳定    |
| Top-P       | 候选截断     | 候选多/多样     | 候选少/聚焦    |

### 实战配方
- **"换个风格" / "再脑洞点"** → 先调 top_p（1.0 → 0.95 → 0.9）
- **"代码更稳" / "事实更准"** → 降 temperature（→ 0.1）
- **"完全随机玩玩"**          → 同时拉高 T（1.3+）和 top_p（≥0.95）—— Anthropic 不建议同时

---

设置温度:    `/temp 1.5`
设置 top_p:  `/top_p 0.9`
一键重置:    `/sample reset`
查看 Soul:   `/soul`
```

### D.6 卡片 thinkingText 显示（修订：含 top_p 和设置人）

```python
# app/dingtalk_card.py 构建 thinking_text 时
parts = [f"🤔 思考中... ({model_display}, thinking={thinking_level}"]

if manual_temp is not None:
    parts.append(f"temp={manual_temp}⚙️({set_by_nick})")
else:
    parts.append(f"temp={router_temp_label}={router_temp_value}")

if manual_top_p is not None:
    parts.append(f"top_p={manual_top_p}⚙️({set_by_nick})")
# 未手动时不显示 top_p（无信息）

if clamp_warnings:
    parts.extend(clamp_warnings)  # ["⚠️ Claude clamp t→1.0"]

thinking_text = ", ".join(parts) + ")"
```

⚠️ 钉钉卡片对 emoji 渲染：⚙️ 在钉钉客户端已确认能渲染（与 🤔、⚠️ 同类），但服务器端某些 IM webhook 可能丢字符——保留 emoji 但准备 fallback：`if ⚙️ 渲染失败 → 使用 [手动] 文本前缀`（运行时不需 detect，固定使用 emoji，发现回归再切）。

### D.7 输入校验

| 输入 | 行为 |
|------|------|
| `/temp` | 显示当前 + 介绍 |
| `/temp 1.5` | 设置；范围 `[0.0, 2.0]` |
| `/temp abc` | 拒绝："温度必须是 0.0–2.0 之间的数字" |
| `/temp 3` | 拒绝（同上 + 显示超界值） |
| `/temp -0.1` | 拒绝（同上） |
| `/temp reset` | 清除手动设置 |
| `/top_p 0.9` | 设置；范围 `(0.0, 1.0]` |
| `/top_p 0` | 拒绝（top_p=0 无意义） |
| `/top_p 1.1` | 拒绝 |
| `/sample reset` | 等价于 `/temp reset` + `/top_p reset` |
| `/sample reset temp` | 仅 reset temp |
| `/sample reset top_p` | 仅 reset top_p |

---

## §E 错误处理

### E.1 转换层（message_transform.py）

- 输入为空或 None → 返回 `[]`
- 单条消息缺 `role` → 视为 `user`
- 单条消息缺 `content` → 内容设为 `""`
- 合并 list 内容失败 → 降级为提取所有 text part 拼接成 str
- 整个转换层抛任何异常 → 上层 catch，**降级为发送原历史**（保证主流程不挂），同时 print 完整 traceback 供排查

### E.2 Clamp & Top_p

- 100% silent + warning（**结构化日志**: `print(f"[CLAMP] backend={backend} model={model} param=temperature orig={t:.3f} clamped={ct:.3f}")`）
- warning 同时进 thinkingText 让用户看到
- 不抛异常

### E.3 Sample Override

- Redis 不可用 → 自动降级文件
- 文件读写失败 → 降级返回 None
- 过期记录 → 静默删除 + 返回 None
- 用户输入解析失败 → 友好提示，不修改状态

---

## §F 测试

### F.1 共享模块单元测试

```text
tests/test_message_transform.py
  - test_other_bot_assistant_becomes_user
  - test_current_bot_assistant_preserved
  - test_assistant_without_bot_id_preserved_as_assistant   # 修订：保守策略
  - test_consecutive_user_merged_with_separator
  - test_consecutive_assistant_also_merged
  - test_system_message_not_merged
  - test_empty_history_returns_empty
  - test_only_system_message_untouched
  - test_history_tail_user_plus_current_user_merged       # 关键：codex 发现的顺序问题
  - test_multimodal_list_content_merge
  - test_history_text_plus_current_image_merge            # 关键：A.2 多模态
  - test_bot_id_field_stripped_after_rewrite              # 关键：避免泄漏

tests/test_system_prompt_blocks.py
  - test_blocks_three_segments
  - test_only_stable_and_semi_stable_have_cache_control
  - test_dynamic_segment_no_cache_control
  - test_no_group_no_soul_only_two_blocks
  - test_weekday_included
  - test_no_seconds_in_dynamic

tests/test_sampling_clamp.py
  - test_claude_temp_clamped_to_1_0
  - test_anthropic_prefix_also_clamped
  - test_gpt_allowed_to_2_0
  - test_gemini_allowed_to_2_0
  - test_temp_below_zero_clamped_to_zero
  - test_top_p_zero_rejected
  - test_top_p_above_one_clamped
  - test_normal_passthrough_no_warning

tests/test_sample_override.py
  - test_set_get_temp
  - test_set_get_top_p
  - test_reset_clears_both
  - test_reset_only_temp
  - test_temp_validation_rejects_oob
  - test_top_p_validation_rejects_zero
  - test_router_used_when_no_manual_override
  - test_manual_override_takes_priority
  - test_claude_clamp_applied_after_manual_override
  - test_redis_unavailable_falls_back_to_file
  - test_file_expires_at_respected                        # 关键：codex 发现的永久 sticky
  - test_expired_record_auto_deleted
  - test_set_by_persisted_and_displayed

tests/test_top_p_pipeline.py
  - test_top_p_passed_to_litellm
  - test_top_p_passed_to_gemini_generation_config
  - test_top_p_none_omitted_from_api_call
  - test_openclaw_top_p_failure_retries_without
```

### F.2 端到端测试（双路径）

```text
tests/test_e2e_dingtalk.py
  - test_dingtalk_role_rewrite_active                    # 钉钉路径角色重塑生效
  - test_dingtalk_slash_temp_not_in_history              # /temp 不写历史
  - test_dingtalk_cache_block_split                      # 钉钉路径 system blocks
  - test_dingtalk_openrouter_top_p_passthrough

tests/test_e2e_wecom.py
  - 同上 4 项，WeCom 路径
```

### F.3 Backend-specific 测试

```text
tests/test_gemini_role_rewrite.py
  - test_gemini_system_extracted_to_system_instruction
  - test_gemini_no_bot_id_in_content_parts
  - test_gemini_role_user_after_rewrite

tests/test_openclaw_ws_skip_rewrite.py
  - test_ws_path_skips_role_rewrite
  - test_http_path_applies_role_rewrite

tests/test_soul_isolation.py
  - test_soul_evolve_uses_raw_messages_not_rewritten
  - test_image_prompt_uses_raw_messages_not_rewritten
```

---

## §G 部署与回滚

### G.1 落地顺序

1. **B** (system prompt 分块 cache + 时间精度) —— 改动最少，先发观察 cache 命中率
2. **C** (temperature 扩展 + top_p 贯穿 + clamp) —— 中等改动，影响所有 backend
3. **A** (角色重塑 + 转换层 + Soul/image 隔离 + OpenClaw 边界) —— 改动最大
4. **D** (手动覆盖 + slash 命令前置 + 文件 TTL) —— 集成 A、C，最后发

### G.2 回滚策略

| 改动 | 回滚方式 |
|------|---------|
| B | revert system_prompt 改动；`_inject_cache_control` 恢复单 block 形态 |
| C | 恢复 3 档 TEMPERATURE_MAP；删除 sampling_clamp 调用；create_backend_stream 移除 top_p 参数 |
| A | handler/dingtalk_bot 跳过 `apply_role_rewrite` 调用（feature flag `ENABLE_ROLE_REWRITE`）；转换层文件保留 |
| D | slash 命令分支注释；handler 跳过 override 读取；`data/sample/` 残留无害 |

每个改动**独立可回滚**，通过 env 开关：

```text
ENABLE_CACHE_BLOCKS=true       # B
ENABLE_TOP_P_PIPELINE=true     # C
ENABLE_ROLE_REWRITE=true       # A
ENABLE_SAMPLE_OVERRIDE=true    # D
```

默认全开，紧急情况可在 docker-compose 即时关闭。

### G.3 监控

- B 上线后第一周：观察 cache 命中率（OpenRouter 后台 + cache_creation_input_tokens 比例）
- C 上线后第一周：观察 wild/chaotic 档位使用率 + Claude clamp 触发次数
- A 上线后第一周：观察用户反馈"bot 是否还混淆"+ 多 bot 共享群的对话流畅度
- D 上线后第一周：观察 slash 命令使用率 + sticky 24h 是否合适

---

## §H 未来扩展（不在本次范围）

- **AIHandler 统一**：消除 `dingtalk_bot.py:1116-1236` 重复实现，钉钉走 AIHandler（独立 spec）
- **Per-user 手动覆盖**：sample_override key 加 user_id 维度
- **`/sample preset`**：命名 preset 保存/加载
- **`/temp suggest`**：用 router 分析最近对话推荐温度
- **History 部分入 cache**：把不变的早期历史块也加 breakpoint（在 messages list 适当位置插）
