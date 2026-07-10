# 钉钉主动推送能力 —— Soul 互动后主动追一句

> **状态：WON'T DO（2026-07-10，规划阶段决定不实现，已归档）**
>
> 经 `/grilling` 压力测试后放弃。理由：**@-only 群消息可见性**把"自主发言"从"感知群氛围择机插话"降级为"对话后硬追一句"，鲜活感有限，却要同时扛下三重代价——(1) 回声室（追句若写回历史会喂给 `_maybe_evolve_soul`，Soul 被自己的即兴噪音带偏）；(2) 忙群自我静音（R10 陈旧取消在活跃群反而频繁弃发，恰在最该"活"的场景失声）；(3) ① 卡片 👍/👎 反馈上线前，本功能质量**完全无信号**，只能靠 `/soul followup off` 事后灭火。价值/复杂度比不划算。
>
> **保留的可复用勘探结论（对 ① 及未来有用）：**
> - 主动发送**底层已是成品**：`app/dingtalk_card.py:695 send_group_message` / `:752 send_private_chat_message`（钉钉原生 `OrgGroupSendRequest`/`PrivateChatSendRequest` + token 刷新 + 重试）、HTTP 推送端点 `app/routes.py:110-173`。任何"往群/单聊推东西"的需求直接复用，勿重写。
> - 本部署 = **@-only 可见性**：Stream 只收被 @ 的群消息；多 bot 的跨 bot 历史来自**共享 conversation_id 历史库**，非各自收到非@消息。
> - `get_session_key` 忽略 sender_id（群历史按 conversation_id 共享）。
>
> 以下为放弃前已完成的规划正文，留档备查。

---


## Goal

在钉钉 @-only 可见性约束下，让 bot 在一次被 @ 的对话回复完成后，以低概率、带冷却地**自发补一句没被要求的话**（追问 / 调侃 / 延伸想法），制造"比你问的多说一点"的鲜活感，让已有的 Soul 人设真正"开口"。

## Background（代码实证，2026-07-10）

- **主动发送底层已完成且在生产用**：`app/dingtalk_card.py:695 send_group_message` / `:752 send_private_chat_message` 走钉钉原生 `OrgGroupSendRequest` / `PrivateChatSendRequest`，带 `get_access_token`（`:244`）刷新 + `@async_retry`。生图流程（`app/dingtalk_bot.py:1451-1458`）已用 `incoming_message.conversation_id` 直接当 `open_conversation_id` 成功主动推消息。HTTP 推送端点 `app/routes.py:110-173` 亦复用同一对方法。→ **本 task 不重写发送层**。
- **群消息 = @-only**（用户确认）：bot 在 Stream 只收到被 @ 的群消息，对群内正在进行的闲聊无实时可见性。代码中"非@消息进历史 + Stage A 角色重塑"的真相是**多 bot 共享同一 conversation_id 历史库**读到彼此发言，非自己收到非@消息。→ 排除"上下文感知择机插话"，自主发言只能是 @-互动上下文内的**事后追一句**。
- **可复用的成熟骨架**（全在 `app/dingtalk_bot.py`）：冷却 `_evolve_timestamps` + `EVOLVE_MIN_INTERVAL=1800`（`:254-275`，含防泄漏清理）；轻量模型调用 `_ask_lightweight_model`（`:305`）；防注入 `_sanitize_evolution_input`（`:333`）；JSON 解析 `_parse_evolution_json`（`:348`）。post-reply 异步钩子位于 `:1559-1563`（`_maybe_evolve_soul` 旁）。

## 决策记录（用户 2026-07-10）

1. 触发器选型 = **A. Soul 自主发言**（B 定时播报可外部 cron 打现成端点覆盖；C 长任务回推、D 现端点已够 → 排除）。
2. 形态 = **Design 1 互动后主动追一句**（Design 2 定时 check-in 需调度器+注册表、对当前群盲、近似已排除的定时播报 → 排除）。
3. 铺开姿态 = **默认开**：feature flag `ENABLE_SOUL_FOLLOWUP` 默认 true，群+单聊都生效，仍带低概率 + 冷却 + 逐会话静音阀。

## Requirements

- R1 在 `dingtalk_bot.py:1561` 现有 `_maybe_evolve_soul` 钩子旁，追加一个后台 `asyncio.create_task`，触发"追一句"决策，**不阻塞主卡片 finalize**。
- R2 触发需**门全过**才发送：`ENABLE_SOUL_FOLLOWUP` 开 ∧ 该会话未 opt-out ∧ 冷却已过 ∧ 概率命中 ∧ 模型决策 `should=true` ∧ **发送前该 turn 未被更新的 @-互动取代**（R10）。任一不过则静默不发。
- R3 追的一句经**独立新气泡**投递：群聊用 `send_group_message`，单聊用 `send_private_chat_message`（`msg_key="sampleText"`，`msg_param=json.dumps({"content": text}, ensure_ascii=False)`）。
- R4 冷却按 conversation_id 隔离，间隔 `SOUL_FOLLOWUP_MIN_INTERVAL`（默认 1800s），复用 `_evolve_timestamps` 同款防泄漏清理模式（独立字典）。**冷却时间戳仅在"成功发送后"写入**，不在门控阶段写——否则模型判定"不必追"也白占满冷却窗口，默认开会被悄悄压稀。
- R5 概率 `SOUL_FOLLOWUP_PROBABILITY`（默认 0.15），发送前有短延迟 `SOUL_FOLLOWUP_DELAY_SECONDS`（默认 5s）让其读起来像"事后补的"。
- R6 `/soul followup on|off|status`（或无参显示状态）逐会话开关，**必须在 `_handle_soul_command` 第 232 行 catch-all 之前拦截**，否则 `off` 会被误当"设置人设内容"。opt-out 状态持久化（默认 on，仅持久化 off）。
- R7 追一句的 prompt 用 Soul 人设 + 刚结束的这轮对话；用户输入经 `_sanitize_evolution_input` 防注入；输出走 `_parse_evolution_json` 取 `{should, text}`。
- R8 追一句全过程异常**吞掉**（模型失败 / 发送失败 / 解析失败），绝不影响主回复与历史保存。
- R9 追一句**发送成功后**，用注入的 `update_history` 以 assistant 角色写入该会话历史（Stage A 会自动合并连续 assistant），保证 bot 对自己的自发言有记忆；发送失败则不写。
- R10 进入 `maybe_send_followup` 时记录本 turn 的单调代号（模块级自增序号，按 cid）；`await sleep(delay)` 后、发送前复查——若该会话已出现更新的 @-互动（代号被后续 turn 覆盖），**跳过发送**，防止陈旧追句落在更新回复之后。
- R11 后台任务用模块级 `set` 持有引用 + done 回调丢弃，防 `asyncio.create_task` 火后即忘被 GC（delay 拉长了 GC 窗口）；确认接线跑在 SDK `start_forever` 的常驻 loop 上（非 routes.py 的 `asyncio.run` 请求态 loop）。

## Acceptance Criteria

- [ ] AC1 `ENABLE_SOUL_FOLLOWUP=false` 时，任何对话后都不产生 send 调用（无论模型决策）。
- [ ] AC2 四道门：分别构造"仅某一道不过"的用例，验证均不发送；四道全过才发送一次。
- [ ] AC3 `conversation_type=="2"` → 调 `send_group_message`；`"1"` → 调 `send_private_chat_message`；参数为 sampleText + `{"content": text}`。
- [ ] AC4 冷却：一次**成功发送**后，同 conversation_id 在 `SOUL_FOLLOWUP_MIN_INTERVAL` 内不再发送；且模型判 `should=false`（未发送）**不**触发冷却（下一轮仍可尝试）。
- [ ] AC5 `/soul followup off` 后该会话后续不再追句；`/soul followup on` 恢复；`/soul followup` 显示当前状态。off 状态跨"进程内重建/重读"仍生效（持久化）。
- [ ] AC6 模型决策 `should=false` 或 `text` 空 → 不发送。
- [ ] AC7 追一句逻辑异常时，主回复路径不受影响（用例：注入 sender/model 抛异常，主流程断言正常返回）。
- [ ] AC8 **发送成功**后调用 `update_history`（assistant 角色）恰一次；发送失败或被门控拦下时不调用。
- [ ] AC9 陈旧 turn 取消：turn A 触发进入 delay 期间，turn B（同 cid）后到并推进代号 → turn A 发送前复查代号变化 → 跳过发送（断言 sender 未被 turn A 调用）。
- [ ] AC10 `python -m compileall -q app main.py` 通过；`pytest -q tests` 全绿（新增用例含在内，测试注入 `delay=0`）。

## Out of Scope

- 重写/重复实现发送层（已完成，直接复用）。
- Design 2 定时人格化 check-in（会话注册表 + 调度器）——留待后续 task。
- ① 交互式卡片按钮（👍/👎 反馈信号）——独立 task；本功能安全阀先靠 `/soul followup off`。
- 企业微信路径（Deprecated）。

## 无剩余阻塞性 Open Question。
