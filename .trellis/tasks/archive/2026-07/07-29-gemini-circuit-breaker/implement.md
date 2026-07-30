# Gemini 熔断 + 自动恢复机制实施计划

> 本文件是执行清单。规划评审已完成，用户已批准实现，`task.py start` 已成功执行；以下勾选项记录本次实现和可复现验证证据。

## 实施结果（2026-07-29）

- 已实现 Gemini antigravity 预输出熔断、Vertex fallback、共享 Redis marker、route slot override、错误脱敏和钉钉 footer 元数据。
- 已覆盖主对话、路由预分析、Soul 三条调用点；中途已有可见输出的异常不重试、不写熔断，取消直接传播。
- 新进程导入（一次性 `GEMINI_API_KEY=test-dummy-key`）、`compileall` 和相关回归测试均通过；pytest 只报告既有缓存目录权限及 asyncio 配置警告。
- 三个 `gpt-5.6-sol` 子代理已完成实现后对抗性复审；处置记录见 [`research/implementation-review.md`](./research/implementation-review.md)。
- 未执行生产 env 修改、部署或 fallback-only canary；需要真实 Vertex 凭据和明确部署授权，不能由本地测试替代。

## 0. 实施前门禁

- [x] 确认 [`prd.md`](./prd.md)、[`design.md`](./design.md) 与本文件内容一致。
- [x] 进行至少一轮 subagent 对抗性评审：可靠性、错误脱敏/安全、集成与部署各覆盖一个视角。
- [x] 将评审 findings 和处理结论写入 `research/adversarial-review.md`；未解决的阻塞项不得启动任务。
- [x] 当前规划阶段的第一轮评审已完成；已核对 `research/adversarial-review.md` 中的 13 条去重 finding、已确认的无 half-open 锁取舍和文档修订结果。
- [x] 用户明确批准进入实现后，执行 `python ./.trellis/scripts/task.py start ./.trellis/tasks/07-29-gemini-circuit-breaker`。
- [x] `task.py start` 成功后进入 Phase 2；本次实现授权已由用户明确给出。

## 1. 先写回归测试：搜索开关与配置

**目标**：先固定旧搜索 fallback 默认关闭、显式回滚仍可用，以及 fallback client 的 fail-closed key 约束。

- [x] 验证 `SEARCH_FALLBACK_PROVIDER` 默认是 `none`，并保留显式 `gemini` 兼容路径。
- [x] 验证 fallback base/key、尾斜杠清理、route slot override 和熔断专用短超时；测试覆盖 alias 模型名不反推 slot。
- [x] 运行相关配置/搜索回归测试；最终证据汇总于本文件第 9 节。

## 2. 熔断状态模块 TDD

**文件**：新增 `app/gemini_circuit.py`，新增 `tests/test_gemini_circuit.py`。

- [x] 测试 Redis mock 下 key 使用 `gemini_circuit:{BOT_ID}:antigravity`，TTL 为 600 秒，并验证 bot 隔离。
- [x] 测试 client 构建、首次 `ping`、client 缺失、`exists`/`setex` 失败及非连接异常均 fail-open。
- [x] marker 固定为无敏感内容的 `open`；generic provider error 不再依赖错误文本白名单。
- [x] 实现 lazy、bounded-timeout 专用 Redis client，并用 `asyncio.to_thread` 隔离同步 I/O；加入失败退避以避免 Redis 黑洞连接风暴。
- [x] 保持用户确认的无 half-open 锁简化取舍，并在实现评审记录其竞态边界。
- [x] `tests/test_gemini_circuit.py` 已纳入最终相关测试并通过。

## 3. fallback client 与模型选择

**文件**：修改 `app/config.py`、`app/gemini_client.py`；扩展 `tests/test_gemini_client.py`。

- [x] 无 fallback base 或无显式 fallback key 时不构建第三 client；不复用 `direct_client`，不输出 key。
- [x] 模型默认原样传递，route slot 只覆盖对应 fallback override，支持 `gemini-3.6-flash-tiered` Vertex alias。
- [x] 实现模块级 `fallback_client`、模型选择 helper，以及 stale marker + client 缺失时的告警和主路径探测。
- [x] fallback client/base/模型选择测试已纳入 `tests/test_gemini_client.py` 并通过。

## 4. 错误脱敏与 fallback metadata

- [x] 实现允许列表式安全摘要：只保留受控类别、整数状态码和固定安全 reason；未知异常不输出原始 message。
- [x] 收紧日志、Redis、usage、response 和 footer 的异常出口；fallback 双失败固定为 fallback 模型异常后主模型异常。
- [x] usage 增加实际模型、主模型、fallback、fallback_error、circuit_open 元数据；无 fallback 保持控制流和 chunk 结构。
- [x] 错误矩阵覆盖 await/首拉流、中途 iterator、chunk 解析、本地转换/JSON、取消、双失败及 footer 编码。

## 5. 主对话 `call_gemini_stream`

- [x] generic provider、网络、HTTP/SDK 预输出异常均触发 fallback，不依赖 `no available accounts` 文本。
- [x] 覆盖 provider await、首次 `__anext__()`、既有 circuit、fallback 未配置、成功 metadata 和双失败。
- [x] 覆盖首个可见 thinking/content 前失败与已有输出后失败；后者不 retry、不 open circuit；取消直接传播。
- [x] 实现显式 pre-output 状态机，消息转换、配置构造、chunk 解析异常不触发 fallback；保留原生搜索工具、thinking、usage、search icon 和 chunk 顺序。
- [x] 使用异步隔离的熔断 helper；Redis 故障 fail-open 并通过短 timeout/退避降低 event-loop 影响。
- [x] `tests/test_gemini_client.py` 中主对话相关测试已通过。

## 6. 路由预分析与 Soul 调用

- [x] 路由预分析覆盖 generic/await/首拉流/circuit-open/fallback-fail；本地 JSON 解析和默认路由降级不打开熔断。
- [x] Soul 覆盖同步 provider、circuit-open、fallback 未配置、Redis 写失败和取消；worker 只执行一次 provider call。
- [x] 后台路径只写安全日志，不为自身生成 footer；共享 marker 命中时主对话只展示当前请求的 `circuit open`。
- [x] 相关 Gemini、钉钉和 Soul 测试已通过。

## 7. 钉钉 footer 与错误卡片

- [x] usage 消费保留新增 metadata；正常主路径 footer 形状保持不变。
- [x] fallback footer 按“实际 fallback 模型 → 主模型名 → 脱敏长异常”显示；已有 circuit 只显示 `circuit open`。
- [x] 双失败按 fallback 模型异常后主模型异常显示；异常文本不进入正文/历史消息。
- [x] 模型名、异常摘要经过 HTML/Markdown/控制字符安全编码；footer/usage 入口测试已通过。
- [ ] 真实 DingTalk UI 字符长度/卡片观察未执行；需要线上权限和真实消息，不能以静态测试替代。

## 8. 关闭旧搜索 fallback 与文档

- [x] 将 `SEARCH_FALLBACK_PROVIDER` 默认改为 `none`，保留 `google_search()`、显式 `gemini` 分支及兼容测试。
- [x] 更新 `CLAUDE.md` 和部署说明，区分原生 SDK 搜索与旧摘要 fallback。
- [x] 新增部署说明，包含 fallback key 显式配置、Vertex alias、`SEARCH_FALLBACK_PROVIDER=none`、Compose recreate 和精确回滚步骤；未写真实 secret。
- [ ] 部署前执行 fallback-only canary：直接使用 fallback base/key、Vertex alias 和无敏感 prompt，记录真实退出码/响应成功/实际模型，不以容器启动或 env 存在代替。
- [x] 将熔断/fallback 配置回滚与 `SEARCH_FALLBACK_PROVIDER` 回滚拆成两组精确键值变更；禁止恢复整份 `.env` 覆盖其他合法配置。

## 9. 验证、评审与回滚

- [x] `python -m compileall -q app main.py`（退出码 0）。
- [x] 相关 targeted tests（181 passed；pytest 仅有环境警告）。
- [x] 全量 `pytest -q --basetemp=.pytest-basetemp tests`（QA 整改后 `500 passed`；默认系统临时目录权限导致的首次 setup 错误已通过显式 workspace basetemp 排除）。
- [x] `python ./.trellis/scripts/task.py validate ./.trellis/tasks/07-29-gemini-circuit-breaker`（退出码 0）。
- [x] 相关搜索/配置/后端回归测试已包含在 targeted 测试命令中并通过。
- [x] 三位 `gpt-5.6-sol` subagents 已复审已实现 diff；实现后结论见 `research/implementation-review.md`。
- [ ] 生产配置/日志/TTL/真实成功链路和 fallback-only canary未执行；部署授权和真实凭据仍是上线前门禁。
- [x] 回滚点已写入部署文档：只恢复本任务相关键并重新 recreate，旧搜索开关单独处理。

## QA 整改（2026-07-30）

- [x] W1–W4 已修复并有回归测试；W5 按 QA 推荐方案记录为 PRD 有意偏离。
- [x] QA 整改后全量测试 `500 passed`，三个指定文件 bare-LF 均为 0。
- [x] P2-1、P2-2、P2-5、P2-8 已处理；P2-3、P2-4、P2-6、P2-7、P2-9 保留为跟进项。
- [ ] fallback-only canary、sub2api alias 真实验证及生产 `.env`/Compose recreate 仍是部署前门禁。

## 二次 Sol double check 整改（2026-07-30）

- [x] usage 边界清洗 provider `model_version`，并补控制字符/恶意换行回归测试。
- [x] 补充 backend → Gemini → Vertex fallback 的 `route_slot` 跨层测试，覆盖相同主模型的 `lite/fast/pro` override。
- [x] 将搜索默认值测试改为隔离环境加载 `config.py`，不依赖开发机进程环境或 `.env`。
- [ ] P2-6 真实 event-loop/ticker 与线程屏障测试：记录于 `research/follow-ups.md`，不在本轮扩 scope。
- [ ] 既存 prompt/system 调试日志治理：另立任务，当前不与熔断提交混合。
- [x] 二次整改 targeted tests：`112 passed`；全量 `pytest -q --basetemp=.pytest-basetemp tests`：`502 passed`。

## 10. 提交与收尾

- [x] 检查可用的 `trellis-check` 入口；当前环境未提供该命令，已用实现后 sol 评审、任务 validate 和本地证据替代并记录。
- [x] 更新相关 backend spec：错误处理、日志脱敏和质量门禁已写入 `.trellis/spec/backend/`。
- [ ] `git diff --check`，审计 diff 中的 key/secret/token/password 字样，确认没有凭据。
- [ ] 按项目提交规范分小步 commit；不提交 `.env`、真实 key 或临时测试产物。
- [ ] 运行 `/trellis:finish-work` 前确认 working tree 只含本任务变更和用户原有未提交文档。
