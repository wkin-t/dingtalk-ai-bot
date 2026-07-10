# Implement — Soul 互动后主动追一句（TDD，inline 执行）

## 顺序清单（每步先写测试后写实现）

1. **config.py**（`:309-317` 段附近，紧邻既有 `ENABLE_*`）
   - `ENABLE_SOUL_FOLLOWUP = _get_bool("ENABLE_SOUL_FOLLOWUP", True)`
   - `SOUL_FOLLOWUP_PROBABILITY = _get_float("SOUL_FOLLOWUP_PROBABILITY", 0.15)`
   - `SOUL_FOLLOWUP_MIN_INTERVAL = _get_int("SOUL_FOLLOWUP_MIN_INTERVAL", 1800)`
   - `SOUL_FOLLOWUP_DELAY_SECONDS = _get_int("SOUL_FOLLOWUP_DELAY_SECONDS", 5)`

2. **app/soul_followup.py**（新模块）— 按 design.md 接口实现（含 `schedule()` 持引用、`_turn_marker` 防陈旧、冷却 on-send、`update_history` 注入）。先写 `tests/test_soul_followup.py`（**所有编排用例注入 `delay=0`**）：
   - `test_flag_off_never_sends`（AC1）
   - `test_optout_blocks`（AC5 门）
   - `test_cooldown_after_send_blocks_second` + `test_model_no_does_not_burn_cooldown`（AC4：发送后占冷却；should=false 不占）
   - `test_probability_gate`（rng 注入，roll 失败不发）
   - `test_should_false_no_send` / `test_empty_text_no_send`（AC6）
   - `test_group_uses_group_api` / `test_single_uses_private_api`（AC3，mock sender 断言调用与 msg_key/msg_param）
   - `test_all_gates_pass_sends_once`（AC2 正路径，delay=0）
   - `test_persists_to_history_on_send` + `test_no_history_when_not_sent`（AC8，mock update_history 断言恰一次/零次）
   - `test_stale_turn_skipped`（AC9：调用 A 进入后、send 前，模拟 B 推进 `_turn_marker[cid]` → A 不发送）
   - `test_send_or_model_exception_swallowed`（AC7，注入抛异常的 sender/model/update_history，断言不抛、return False、主流程不受累）
   - `test_set_pref_persists`（写 off → is_enabled_for False；on → 删文件 → True）；monkeypatch soul_dir 到 tmp

3. **app/dingtalk_bot.py 接线①**：`:1561` 旁加 `soul_followup.schedule(soul_followup.maybe_send_followup(sender=self.card_helper, model_call=_ask_lightweight_model, sanitize=_sanitize_evolution_input, parse_json=_parse_evolution_json, update_history=update_history, conversation_id=..., conversation_type=incoming_message.conversation_type, session_key=session_key, messages=messages_raw, ai_response=clean_response, soul_text=_load_soul(conversation_id)))`，`try/except` 包裹调度（同既有进化调度写法）。顶部 `from app import soul_followup`。**确认 `process()` 跑在 SDK `start_forever` 常驻 loop（非 routes.py 的 `asyncio.run`）**——`schedule()` 的 `create_task` 才有持久 loop 承载。

4. **app/dingtalk_bot.py 接线②**：`_handle_soul_command`（`:202`）在 **232 行 catch-all 之前**加：
   ```python
   elif sub.split(None,1)[0] == "followup":
       arg = (sub.split(None,1)[1].strip().lower() if len(sub.split(None,1))>1 else "")
       if arg == "off":   soul_followup.set_pref(conversation_id, False); reply "🔕 本会话已静音主动追句"
       elif arg == "on":  soul_followup.set_pref(conversation_id, True);  reply "🔔 本会话已开启主动追句"
       else:              reply 当前状态(is_enabled_for)
       return
   ```
   注意：`followup` 分支不要求管理员权限（群成员皆可静音打扰自己的群），与 `/soul 内容` 的 admin 门分开。

5. 更新 `CLAUDE.md`（Feature flags 段）与 `.env*` 注释补 4 个新变量说明（文档同步，非功能）。

## 验证命令

```bash
python -m compileall -q app main.py
pytest -q tests/test_soul_followup.py
pytest -q tests
```

## 风险文件 / 回滚点

- `app/dingtalk_bot.py` 是最热文件（1900+ 行、生产主路径）。两处接线务必最小侵入；接线③④改完立即 `compileall` + 跑既有 dingtalk 相关测试防回归。
- 回滚点：`ENABLE_SOUL_FOLLOWUP=false`（运行时）/ 删两处接线（代码）。

## task.py start 前检查

- [ ] prd.md 有可测 AC（已）
- [ ] design.md / implement.md 就位（已）
- [ ] 用户已 review 并批准 → 方可 start + 进入 Phase 2 实现
