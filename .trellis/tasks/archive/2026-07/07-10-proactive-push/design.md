# Design — Soul 互动后主动追一句

## 架构与边界

新增一个**自包含、纯函数为主、依赖注入**的模块 `app/soul_followup.py`，把可单测的逻辑（门控 / 冷却 / opt-out 持久化 / prompt 构建 / 响应解析 / 编排）全放进去，通过参数注入 `model_call`（模型调用）和 `sender`（card_helper）以**避免与 `dingtalk_bot.py` 的循环导入**。`dingtalk_bot.py` 只做两处极薄的接线。

```
app/dingtalk_bot.py
  :1561  ┌─ asyncio.create_task(_maybe_evolve_soul(...))        # 既有
         └─ asyncio.create_task(                                 # 新增接线
                soul_followup.maybe_send_followup(
                    sender=self.card_helper,
                    model_call=_ask_lightweight_model,
                    sanitize=_sanitize_evolution_input,
                    parse_json=_parse_evolution_json,
                    conversation_id=conversation_id,
                    conversation_type=incoming_message.conversation_type,
                    messages=messages_raw,
                    ai_response=clean_response,
                    soul_text=_load_soul(conversation_id),
                ))
  :202   _handle_soul_command → 在 232 行 catch-all 之前插入 `followup` 分支

app/soul_followup.py            # 新模块，无 dingtalk_bot 依赖
app/config.py                   # 4 个新配置项
tests/test_soul_followup.py     # 新测试（镜像 tests/test_dingtalk_push.py 的 mock sender 风格）
```

## soul_followup.py 接口

```python
# 状态（模块级，镜像 _evolve_timestamps 防泄漏清理）
_followup_timestamps: dict[str, float]       # 冷却：仅在“成功发送后”写入
_turn_seq: itertools.count 或全局 int         # 单调代号发号器
_turn_marker: dict[str, int]                  # cid -> 最新 turn 代号（防陈旧）
_bg_tasks: set                                # 持有后台 task 引用防 GC
def _cleanup_timestamps(...): ...            # 同 _cleanup_evolve_timestamps

def schedule(coro) -> None:                   # 供 dingtalk_bot 钩子调用
    t = asyncio.create_task(coro); _bg_tasks.add(t); t.add_done_callback(_bg_tasks.discard)

# opt-out 持久化：默认 ON，仅落 OFF。文件与 soul 同目录同命名前缀。
#   path = data/souls/{BOT_ID}__{cid}.followup.json   ({"enabled": false})
#   BOT_ID 从 app.config 导入（config 无循环依赖）
def is_enabled_for(conversation_id) -> bool          # 无文件=True
def set_pref(conversation_id, enabled: bool) -> None # enabled=True 删文件；False 写文件

# 门控（纯函数，rng/now 可注入以便测试）——注意：不写冷却时间戳
def should_attempt(conversation_id, *, now=None, rng=None,
                   enabled_flag=ENABLE_SOUL_FOLLOWUP,
                   probability=SOUL_FOLLOWUP_PROBABILITY,
                   min_interval=SOUL_FOLLOWUP_MIN_INTERVAL) -> bool:
    # 顺序（先便宜后昂贵）：flag → is_enabled_for(cid) → 冷却(now - ts >= min_interval) → rng()<probability
    # 只读不写；冷却时间戳的写入移到“成功发送后”（见 maybe_send_followup 第 6 步）

# prompt + 解析
def build_prompt(messages, ai_response, soul_text, sanitize) -> str
def extract_text(model_output, parse_json) -> str|None   # 取 {should:bool, text:str}；should!=true 或 text 空 → None

# 编排（async，被 dingtalk_bot 钩子调用）
async def maybe_send_followup(*, sender, model_call, sanitize, parse_json, update_history,
                              conversation_id, conversation_type, session_key,
                              messages, ai_response, soul_text,
                              delay=SOUL_FOLLOWUP_DELAY_SECONDS) -> bool:
    # 0. marker = next(_turn_seq); _turn_marker[cid] = marker   # 认领本 turn（后到的 turn 会覆盖）
    # 1. if not should_attempt(cid): return False
    # 2. out = await model_call(build_prompt(...))
    # 3. text = extract_text(out, parse_json); if not text: return False
    # 4. await asyncio.sleep(delay)
    # 5. if _turn_marker.get(cid) != marker: return False        # R10：被更新的 @-互动取代 → 弃发
    # 6. msg_param = json.dumps({"content": text}, ensure_ascii=False)
    #    ok = await ( send_group_message if type=="2" else send_private_chat_message )(cid, "sampleText", msg_param)
    #    if ok: _followup_timestamps[cid] = now()                # 冷却仅此刻写
    #           update_history(session_key, user_msg=None, assistant_msg=text, sender_nick=<bot 名>)  # R9
    # 全函数体 try/except 包裹，异常吞掉并 print 探针（📣 [追一句]），return False
```

> **已核实** `app/memory.py:107 update_history(session_key, user_msg: Optional[str], assistant_msg=None, sender_nick=None, ...)`：内部 `if user_msg:` / `if assistant_msg:` 分别守卫，传 `user_msg=None, assistant_msg=text` 只写 assistant 一条，正合 R9，无隐患。

## 数据流

```
被@消息 → handle_ai_stream → 卡片流式回复完成 → update_history
       → create_task(maybe_send_followup)  ← 后台，不阻塞 finalize
            门控四道 → 模型决策(should/text) → sleep(delay)
            → send_group/private_message  → 独立新气泡出现在群/单聊
```

## 关键取舍

- **DI 而非直接 import**：`maybe_send_followup` 收 `model_call`/`sender`/`sanitize`/`parse_json` 作参数，换来零循环导入 + 纯 mock 可测；代价是接线处参数略长，可接受。
- **门控顺序**先便宜后昂贵：flag → opt-out（读文件）→ 冷却（内存）→ 概率 → 才调模型。避免为注定不发的轮次白烧一次模型调用。**概率命中即占用冷却时间戳**（在 `should_attempt` 内写入），防止 delay 窗口内并发轮次叠加。
- **opt-out 用文件而非 DB**：布尔偏好、默认 ON、与 `data/souls/` 局部性一致，免 migration；后续要多端一致再提升到 Redis+文件降级（本 task 不做）。
- **sampleText 而非 markdown**：追的一句是随口一句，纯文本比带标题的 markdown 卡片更像"人话"；且不占卡片、与主回复卡片视觉区分开。
- **异常全吞**：这是"锦上添花"路径，任何失败都不得回灌主回复/历史（R8/AC7）。仅 print 探针便于 grep。

## 兼容 / 回滚

- 纯新增：新模块 + 4 配置项 + 2 处接线 + `/soul followup` 子命令。不改发送层、不改数据库、不改路由。
- 回滚：`ENABLE_SOUL_FOLLOWUP=false` 即整功能静默（AC1）；代码层删两处接线即可。
- 部署注意：改 `.env` 后需 `docker compose up -d`（非 restart）才生效（既有约定）。

## 风险

- 最高风险=工作群不请自来。缓释：默认低概率 0.15 + 30min 冷却 + `/soul followup off` 安全阀 + 模型二次门控（should=false 可主动闭嘴）。
- prompt 注入：用户输入经 `_sanitize_evolution_input`；system 侧固定指令要求"只输出 JSON、简短、in-character、不复述答案"。
