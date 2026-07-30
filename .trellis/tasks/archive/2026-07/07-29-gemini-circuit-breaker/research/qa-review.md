# QA 验收整改工单 — Gemini 熔断 + 自动恢复机制

> 审查人：QA（team-lead 会话，level: max）| 日期：2026-07-30
> 审查对象：固定点 `b69ed1c` 起的全部未提交工作区改动
> 审查方式：Standards / Spec 双轴独立子代理 + 逐项二次验证（关键发现均经字节级/代码级复核，非转述）
> 回归基线：compileall OK；`pytest -q tests` **497 passed / 0 failed**（HEAD 基线 464，+33 无回归）

## 总裁定：有条件通过

核心机制零缺陷（预输出窗口、重放保护、取消语义、fail-open、脱敏架构、旧 plan 错误设计未混入——均逐项验证通过）。
整改集中在外围。**W1 修完前禁止 commit**；W2-W5 建议随本次一并处理；P2 项可跟进。

25 条 AC 判定：18 PASS / 4 PARTIAL / 1 FAIL（AC12）/ 1 未完成（AC24 canary）。

---

## ❌ W1 · 提交前必须修：混合行尾（阻塞 commit）

**问题**：3 个纯 CRLF 文件被注入 LF 新行（字节级实测）：

| 文件 | CRLF 行 | 混入 LF 行 |
|---|---|---|
| `app/config.py` | 514 | **25** |
| `app/dingtalk_bot.py` | 1928 | **176** |
| `app/ai/handler.py` | 319 | **33** |

**后果**：`core.autocrlf=false` 且无 `.gitattributes`，照此提交会把混合行尾烧进 git 历史；未来任何编辑器/工具做行尾规范化都会产生数百行假 diff，污染 blame。当前 diff 已有此症状（config.py 34 行改动中仅 15 行是真实内容变化，其余为 EOL 噪音假重复对）。

**修法**：仅将上述 3 文件中的 LF 行转为 CRLF（保持文件原有风格，勿动本就是 LF 的 `gemini_client.py` 等）。

**验证**：
```bash
python -c "
for f in ['app/config.py','app/dingtalk_bot.py','app/ai/handler.py']:
    d=open(f,'rb').read(); crlf=d.count(b'\r\n'); lf=d.count(b'\n')-crlf
    print(f, 'CRLF:',crlf,'bare-LF:',lf)   # bare-LF 必须全为 0
"
```
另跑 `pytest -q tests` 确认 497 passed 不变。

---

## ⚠️ W2 · P1：错误卡片多行文案被压平

**问题**：`app/dingtalk_bot.py:1442` 通用 error chunk 出口套了 `safe_display_text`，其 `" ".join(text.split())`（`app/error_safety.py:122`）把 `\n` 全部折叠。受害面：
- 新增的双失败两段式错误（`fallback 模型 X:\n…\n\n主模型 Y:\n…`）退化为单行，削弱 AC17（PRD 62）"分别显示两段"的设计意图；
- **既有路径回退**：SAFETY 过滤提示（`gemini_client.py:628`）、"没有返回任何内容"的编号引导列表（`:669`）全部塌成一行。

**修法**（二选一）：
- 推荐：`error_safety.py` 拆成两个函数——脱敏清洗（保留 `\n`，仅去控制字符/Cf + escape）与单行化（现行为），Markdown sink（`msgContent`）用前者；
- 或最小改：`safe_display_text` 加 `keep_newlines: bool = False` 参数，`dingtalk_bot.py:1442` 传 True（`\n` 不参与折叠）。

**验证**：新增测试断言含 `\n\n` 的 error chunk 渲染后仍保留换行；SAFETY 提示的编号列表结构不丢。

---

## ⚠️ W3 · P1：finish_reason 日志被洗成常量

**问题**：`app/gemini_client.py:631` 用 `safe_error_summary(ValueError(finish_reason), 'stream')` "净化"日志——`ValueError` 不属 genai 异常类型，输出恒为 `Gemini provider error`，`RECITATION`/`PROHIBITED_CONTENT`/`MALFORMed_FUNCTION_CALL` 等真实值全部丢失。HEAD 原代码直接打印原值，本次属可观测性回退。

**根因备注**：`.trellis/spec/backend/logging-guidelines.md` 的"禁止插值异常对象"写成了无豁免绝对规则，催生了这个过度清洗。finish_reason 是 SDK 有界枚举，不是上游注入面。

**修法**：直接打印枚举字符串 `print(f"⚠️ 异常的 finish_reason: {finish_reason}")`；同时给 logging-guidelines.md 补一条豁免："SDK 有界枚举值（finish_reason 等）可直接记录"。

**验证**：人为构造非 STOP finish_reason 的单测，断言日志含原始枚举值。

---

## ⚠️ W4 · P1：搜索默认值翻转缺行为测试

**问题**：`SEARCH_FALLBACK_PROVIDER` 默认 `gemini`→`none`（`app/config.py:322`）改变生产搜索路径，但唯一新增覆盖是常量断言（`tests/test_gemini_circuit.py:23`，且放错了文件）；两个既有测试用 `patch(..., "gemini")` 钉回旧值保活（`tests/test_openai_client.py:431,463`）。**没有任何测试验证 `none` 时 openai_client 真的不调用 `google_search()` 注入摘要**——而这正是本次行为变更本体。

**修法**：在 `tests/test_openai_client.py` 新增一条：`SEARCH_FALLBACK_PROVIDER="none"` 时，路由 need_search=True 且模型不支持原生搜索的场景下，`gemini_client.google_search` **不被调用**、消息里无注入的 system 搜索摘要。顺手把 `test_gemini_circuit.py:21-23` 的搜索配置断言挪进 openai/search 相关测试文件（放错位置，Divergent Change）。

---

## ⚠️ W5 · P1：AC12 唯一 Spec FAIL —— thinking chunk 门控（推荐记录偏离而非改码）

**问题**：`app/gemini_client.py:646` 把 `yield {"thinking": ...}` 也包进 `if ENABLE_THINKING:`；HEAD 只门控 `thinking_start` 标记、thinking 本体无条件下发。`ENABLE_THINKING=false` 时 chunk 结构改变，违反 PRD 16 / AC57"未配置 fallback 时 chunk 结构不变"的字面。

**QA 裁定（减刑情节）**：HEAD 行为本身是缺陷（false 时下发无 start 标记的孤儿 thinking chunk，消费端 `is_thinking` 未置位、实际丢弃）；新门控与 PRD 15"首个**用户可见**输出"语义自洽；生产三容器 `ENABLE_THINKING` 全为默认 true，实际零暴露。

**修法（二选一，需拍板）**：
- **推荐**：不改代码，在 `prd.md` Notes 补一行有意偏离记录："ENABLE_THINKING=false 时 thinking chunk 由生产端直接抑制（修正 HEAD 孤儿 chunk 缺陷），与 PRD 15 用户可见定义对齐，AC57 据此豁免"；
- 严格守字面：恢复无条件 yield，`output_started` 仍仅在 `ENABLE_THINKING` 为真时置位。

---

## 💡 P2 · 跟进项（不阻塞本次提交）

| # | 项 | 位置 | 说明 |
|---|---|---|---|
| P2-1 | 用户可见裸异常残留 | `app/dingtalk_bot.py:919`（统计失败回复）、`:1042`（图片识别失败进正文） | 违反 AC64 字面且无 PRD 28 豁免；换 `safe_error_summary`。`:1353`/`:1569` 属生图改图路径，PRD 28 保护，**勿动** |
| P2-2 | 死代码清理 | `error_safety.py:69`（`secrets` 死参数）、`gemini_circuit.py:130-131`（`is_open`/`is_open_async` 零调用别名）、`gemini_client.py:10`（与 `:193` 重复的 `import re`）、`dingtalk_bot.py:409`（`_target_model` 未读取） | 与"不留死配置"原则相悖；YAGNI |
| P2-3 | 熔断状态机三份重复 | `gemini_client.py:311-336` / `:696-756` / `dingtalk_bot.py:353-402` | 三调用点形状确有差异（async 生成器/收集式/线程同步），重构可选；改熔断策略时记得三处同步 |
| P2-4 | route_slot 归一化三份重复 | `gemini_client.py:349-355` / `ai/handler.py:340-342` / `dingtalk_bot.py:409-417` | 提取单一 `normalize_route_slot()` |
| P2-5 | 截断-转义顺序 | `error_safety.py:123` | 先截 1000 再 escape，转义可超限。实测当前输入不可达（允许列表摘要无 HTML 字符），属加固：改为转义后截断 |
| P2-6 | AR-02/AR-03 承诺的测试未兑现 | `tests/test_gemini_circuit.py:95`、`tests/test_soul.py:93` | event-loop responsiveness 测试与真实线程屏障测试缺失，现只有委托断言 |
| P2-7 | stale 告警进程级 once | `gemini_client.py:146,152` | 配置修好又配错不再告警；换按 TTL/次数节流 |
| P2-8 | spec 文档自查 | `.trellis/spec/backend/index.md`、`quality-guidelines.md` | 未填模板被标 "Existing template" 属状态粉饰，恢复 To fill；任务级执行约束（canary、.env 禁改）移出项目代码规范 |
| P2-9 | footer fallback 分支不缩短模型名 | `dingtalk_bot.py:193-206` | 与 PRD 31 示例逐字一致故不算违规，仅与正常分支展示风格不一致，可留 |

**QA 明确驳回不需处理**：双异常类合并建议（`_ProviderPreOutputError`/`_ProviderStreamError` 靠类型区分 except 分支是惯用法）；`ai/handler.py:307` openclaw 3-tuple 解包修复（计划外但有益的 latent bug 修复，**建议 commit message 点名**）。

---

## 🚪 上线门（不阻塞合并，阻塞部署）

1. **canary 未执行**：`research/implementation-review.md:39-42` 已诚实声明。部署前必须完成 fallback-only 真实验证（构造主路径失败 → 观察 Vertex 接管 + footer + Redis marker TTL）。
2. **sub2api 服务端 alias 前提未验证**：整个方案依赖 Vertex 路径已配 `gemini-3.6-flash-tiered → gemini-3.6-flash` 别名（PRD 24），代码不校验。部署前在 sub2api 侧实测该别名真实生效。
3. **.env 变更**：新增 `GEMINI_API_BASE_FALLBACK` + **显式** `GEMINI_API_BASE_FALLBACK_KEY`（antigravity key id=22 为主路径、vertex key id=10 为保底），按 `docs/deploy/gemini-circuit-breaker-rollout.md` 分项执行与回滚；记得 `docker compose up -d`（restart 不重读 env_file）。

---

## 完成定义（DoD）

- [x] W1：3 文件 bare-LF 归零（`app/config.py`、`app/dingtalk_bot.py`、`app/ai/handler.py` 均为 0）
- [x] W2：error 出口保留换行 + 新增断言；`safe_display_text(..., keep_newlines=True)` 只用于 Markdown error card
- [x] W3：finish_reason 原值可见 + logging spec 豁免条款 + 回归测试
- [x] W4：`SEARCH_FALLBACK_PROVIDER=none` 不调用/不注入的行为测试落地，并将默认值断言移至 `test_openai_client.py`
- [x] W5：选择 QA 推荐方案，在 PRD Notes 记录 `ENABLE_THINKING=false` 对 AC57 的有意偏离，不恢复孤儿 thinking chunk 缺陷
- [x] `python -m compileall -q app main.py` 通过（整改后复跑）
- [x] `pytest -q --basetemp=.pytest-basetemp tests` 全绿：`500 passed`，数量 ≥ 497
- [x] P2/上线门未处理项已在本节和上文对应行标注“跟进”并保留

## 本轮 P2 与上线门处置（2026-07-30）

- [x] P2-1：统计失败回复和 OpenClaw 图片识别失败文本改用安全摘要；生图/改图路径按 QA 指定保持不动。
- [x] P2-2：移除 `secrets` 死参数、熔断短别名、重复局部 `re` import 和未使用的 route slot helper 参数。
- [x] P2-5：显示文本改为先 HTML escape 后截断；单行状态栏与多行 Markdown error card 显式区分。
- [x] P2-8：恢复未填写 backend spec 的 `To fill` 状态，并移除 quality spec 中属于任务级的 `.env`/生产容器约束。
- [ ] P2-3/P2-4/P2-6/P2-7：标记跟进；分别是三调用点状态机重构、route slot helper 收敛、线程屏障/事件循环测试和 stale 告警节流，不阻塞本轮整改。
- [ ] P2-9：标记跟进/保持现状；fallback footer 保留完整模型 ID 是本任务“显示主模型名字和长异常”的明确要求。
- [ ] 上线门：fallback-only canary、sub2api alias 真实验证和 `.env`/Compose recreate 仍待部署前执行；本轮不修改生产配置。

## 二次 Sol double check 处置（2026-07-30）

- [x] 模型标识：usage 边界对 provider `model_version` 使用 `safe_model_name()`，并补恶意换行/控制字符回归测试。
- [x] 跨层证据：补充 `create_backend_stream()` → `call_gemini_stream()` → Vertex fallback 的 `lite/fast/pro` route slot 回归测试，验证相同主模型下实际 fallback model。
- [x] 搜索默认测试：使用隔离环境和禁用 dotenv 加载的配置执行路径，验证变量缺失时默认 `none`，不受开发机配置污染。
- [x] 二次整改验证：targeted tests `112 passed`，全量 `pytest -q --basetemp=.pytest-basetemp tests` 为 `502 passed`。
- [ ] P2-6 真实并发/取消屏障测试：不立即扩 scope，已记录于 [`research/follow-ups.md`](./follow-ups.md)，下次修改熔断或部署前补齐。
- [ ] 既存 prompt/system 调试日志：不属于本任务新增链路，已记录为独立日志治理跟进，见 [`research/follow-ups.md`](./follow-ups.md)。
