# Gemini 熔断 + 自动恢复机制

> 来源：`docs/superpowers/plans/2026-07-29-gemini-circuit-breaker.md`；本 Trellis 任务是迁移后的规划与执行入口。

## Goal

当 Gemini 主路径（antigravity）在 provider 预输出阶段出现任何请求异常时，当前请求自动切换到 Vertex
完成响应；同时用 Redis 记录短期熔断状态，让后续请求在冷却窗口内直接走 Vertex，
窗口结束后再允许主路径重新探测。由于 Vertex 使用 pay-as-you-go 作为保底，目标是
避免上游账号池故障直接暴露给用户。

## Requirements

- 主路径失败策略不依赖错误文本或 HTTP 状态码：主路径 provider 在预输出阶段的异常即触发熔断并尝试 Vertex fallback。
- 异步流的预输出阶段包括 `generate_content_stream` 的 await、异步迭代器首次及后续拉取，直到首个用户可见的 thinking/content chunk；本地 search metadata 不算 provider 输出。首个用户可见 chunk 之后的异常不重放、不立即熔断。
- fallback 仅在配置了 fallback client 时启用；模型映射只作为可选 override。未配置 fallback 时保持现有控制流、chunk 结构和返回形状，但异常文本统一遵守本任务的安全脱敏边界。
- 熔断状态只存 Redis；Redis 不可用时不应阻塞或误伤主路径请求。
- Redis 熔断读写失败采用 fail-open：视为未熔断，继续尝试主路径；写失败只记录日志，不阻塞请求。
- 熔断模块使用独立的短 Redis 超时，并在异步调用点通过线程隔离同步 Redis I/O；Redis 黑洞不能阻塞整个 asyncio event loop。client 获取、ping、exists、setex 的任意异常都必须被熔断模块消化。
- 熔断 key 按 `BOT_ID` 隔离，格式为 `gemini_circuit:{BOT_ID}:antigravity`，不同 bot/部署不共享熔断状态。
- 熔断 TTL 固定为 600 秒；TTL 过期后允许主路径重新尝试，失败则重新打开熔断。
- fallback 成功不刷新或延长熔断 TTL；不解析上游动态 `quotaResetDelay`。
- TTL 到期后的并发请求不使用 half-open 分布式锁，允许各自探测主路径；失败请求各自可写回熔断并 fallback。接受“恢复成功后，稍晚完成的并发失败仍可能重新写入 600 秒熔断”的竞态，以换取实现简单；通过日志和测试记录该取舍，不把它伪装成严格的半开协调。
- 当前部署前提：sub2api 的 Vertex 路径已配置 `gemini-3.6-flash-tiered` 别名，并将其映射到 `gemini-3.6-flash`；模型名兼容性由该服务端 alias 保证。
- fallback 默认沿用原始主路径模型名；`MODEL_ROUTER_FALLBACK`、`MODEL_LITE_FALLBACK`、`MODEL_FAST_FALLBACK`、`MODEL_PRO_FALLBACK` 仅作为可选 override，不是 fallback 启用前提。调用方必须显式传递 `router`/`lite`/`fast`/`pro` 档位，禁止根据模型字符串反推 override；主模型名相同时也必须保持档位映射确定。
- 配置了 `GEMINI_API_BASE_FALLBACK` 时必须显式配置 `GEMINI_API_BASE_FALLBACK_KEY`；缺 key 不构建 fallback client，不静默复用直连 Google 的 `GEMINI_API_KEY`。
- Redis 中存在熔断标记但 fallback client 不可用时，忽略 stale 熔断状态、继续尝试主路径，并记录配置告警。
- 覆盖对话文本、路由预分析和 Soul 进化三个 Gemini 调用点；不改动生图/改图路径。
- 只处理 provider 预输出阶段异常：流式响应已经产生用户可见输出后发生的中途异常不做当前请求重试，也不立即写熔断；下一次调用仍正常探测主路径。
- 任务取消（包括 `asyncio.CancelledError`）不视为 provider 故障，不触发 fallback 或熔断；fallback 决策与熔断写入不能放在线程 worker 内，避免 `run_in_executor` 被取消后线程继续产生副作用。当前系统虽暂无用户取消入口，仍保留该异步边界。
- fallback 成功时，在钉钉卡片 footer 中先显示实际 fallback 模型，再显示主路径模型名和较完整的异常信息，例如 `🤖 gemini-3.6-flash | ⚠️ 主模型 gemini-3.6-flash-tiered: <长异常>`；不修改模型正文，不把成功响应标记成 API 错误。
- 请求开始时熔断已打开、没有新的主路径异常时，footer 保持相同顺序但只显示 `circuit open`，不重复展示 Redis 中的历史异常详情。
- 用户可见的长异常必须先经过允许列表式安全摘要，移除凭据、Authorization、URL 查询参数、请求体、控制字符和完整堆栈；已知 provider 异常只保留经验证的类别、状态码和安全 reason，未知异常不直接输出原始 message。
- 用户可见和服务端日志中的异常都必须脱敏，最多 1000 字符；禁止 `repr(exception)`、traceback 和 exception chain 进入任何 sink。原始异常只在当前进程内短暂用于判断和构造 fallback，不写入日志、Redis、文件或用户回复。
- 主对话 fallback 元数据复用现有 `usage` chunk 传递，至少包含实际 `model`、`requested_model`、`fallback` 和脱敏后的 `fallback_error`；不新增全局状态或正文事件。
- fallback 也失败时，错误卡片按“fallback 实际模型异常 → 主路径模型异常”的顺序展示两段带模型名的脱敏长异常。
- 预分析和 Soul 后台调用的 fallback 只记录服务端日志，不为后台调用自身产生 usage/footer；它们仍可写共享健康熔断标记。若该标记导致同一消息或后续主对话直接走 Vertex，footer 只描述当前主对话实际使用的 fallback，不显示后台调用的历史异常详情。
- 原生模型 SDK 的联网搜索行为保持不变；现有独立 `google_search()` 注入路径需要单独处理。代码核对显示它仍由 `SEARCH_FALLBACK_PROVIDER=gemini` 控制，并可从 `app/openai_client.py` 被调用，不能假设为不可达。
- 本次只关闭旧的独立搜索 fallback，不删除兼容代码：`SEARCH_FALLBACK_PROVIDER` 默认改为 `none`，部署配置也明确设为 `none`；需要回滚时仍可显式设回 `gemini`。
- 上线验收必须包含不破坏主路径的 fallback-only canary，并分别记录熔断/Vertex 配置回滚与 `SEARCH_FALLBACK_PROVIDER` 回滚；不得用恢复整份 `.env` 作为唯一回滚动作。
- 熔断重试的异常边界、流式输出重复风险和 fallback 失败行为必须在设计与测试中明确。
- `design.md` 和 `implement.md` 写完后，必须进行 subagent 对抗性评审；评审 findings 持久化到 Trellis task 目录，阻塞项解决或明确记录后才能进入实现阶段。

## Acceptance Criteria

- [ ] 主路径 provider 在 await 或首个用户可见输出前的异步迭代拉取阶段出现任意异常时，已配置 fallback 的当前请求切换到 Vertex。
- [ ] 已产生用户可见输出后的中途异常不触发当前请求 fallback；下一次调用重新按主路径探测。
- [ ] 任务取消不触发 fallback 或熔断；网络/超时/4xx/5xx/SDK 异常仍按主路径失败处理。
- [ ] 熔断打开期间的新请求跳过主路径，直接使用 Vertex；TTL 到期后恢复探测。
- [ ] fallback 成功不会重置熔断计时；主路径重新探测失败时重新设置 600 秒 TTL。
- [ ] TTL 到期后的并发探测不引入额外锁或租约状态，任一失败请求均能重新建立熔断。
- [ ] fallback 模型调用默认沿用主路径模型名并遵循 Vertex alias；如配置某个 `MODEL_*_FALLBACK`，由显式 route slot 仅覆盖对应档位，不重复维护必需的应用侧模型转换表。
- [ ] fallback base 已配置但 fallback key 缺失时，fallback client 不构建，并有明确日志/配置提示。
- [ ] stale 熔断标记不会在 fallback client 缺失时锁死主路径；该配置异常可从日志定位。
- [ ] 不同 `BOT_ID` 的熔断 key 相互隔离，不会因一个实例的故障阻断其他实例。
- [ ] Redis 不可用时，熔断读写失败不会阻塞正常请求或卡住 asyncio event loop；client 获取异常也 fail-open。
- [ ] 未配置 fallback 时，现有三个调用点的控制流、chunk 结构和返回形状保持不变，但错误输出仍通过统一安全摘要，不能以兼容为由泄露原始异常。
- [ ] 生图/改图仍使用现有直连 client，不受本次熔断逻辑影响。
- [ ] fallback 成功的钉钉 footer 先显示实际 fallback 模型，再显示主路径模型名和脱敏后的较完整异常。
- [ ] 已有熔断状态时，钉钉 footer 先显示实际 fallback 模型，再显示主路径模型名和 `circuit open`，不显示历史异常详情。
- [ ] 主对话 `usage` chunk 能携带实际模型、主模型、fallback 标记和脱敏异常，钉钉 footer 能消费这些字段。
- [ ] fallback 双失败时，错误卡片分别显示 fallback 实际模型和主路径模型的脱敏异常，且顺序固定。
- [ ] 预分析/Soul fallback 自身不产生最终对话 footer；同一消息/后续主对话若实际命中 fallback，footer 只反映该主对话，不显示后台异常详情。
- [ ] provider 建流、首拉流、后续流迭代、chunk 解析、本地转换/JSON 和取消均有明确错误矩阵；用户可见异常不包含原始 URL、请求体、凭据或完整堆栈，所有既存日志出口也只输出安全摘要。
- [ ] 用户可见和服务端日志中的异常脱敏后不超过 1000 字符；原始异常不进入日志、Redis、文件或用户回复。
- [ ] footer 对模型名和安全异常做显示编码，不能被 provider 返回的 HTML/Markdown/控制字符改变卡片结构。
- [ ] 旧的 `google_search()` 注入 fallback 默认关闭且可通过配置重新开启；模型自身 SDK 的原生搜索不受影响。
- [ ] 规划产物完成至少一轮 subagent 对抗性评审，评审证据和处理结论已持久化，未遗留未决阻塞项。
- [ ] 上线前完成 fallback-only canary，并将主路径、Vertex fallback、熔断路由、钉钉 footer 和搜索开关的证据分开记录；回滚只改变本任务相关键。
- [ ] 完成针对熔断状态、配置、主对话、预分析和 Soul 调用的测试及编译检查。

## Notes

- Keep `prd.md` focused on requirements, constraints, and acceptance criteria.
- Lightweight tasks can remain PRD-only.
- For complex tasks, add `design.md` for technical design and `implement.md` for execution planning before `task.py start`.
- **有意偏离 AC57（QA W5）**：当 `ENABLE_THINKING=false` 时，生产实现会直接抑制 thinking chunk，而不是恢复旧版“无 thinking_start 标记的孤儿 thinking chunk”。这是对 HEAD 缺陷的修正，与“首个用户可见输出”的预输出定义一致；因此 AC57 按此说明豁免，正常 `ENABLE_THINKING=true` 路径保持原有 thinking chunk 协议。
