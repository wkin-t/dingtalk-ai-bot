# Gemini 熔断 + 自动恢复机制：sol 子代理对抗性评审

## 评审范围

- 时间：2026-07-29
- 对象：`prd.md`、`design.md`、`implement.md`，并交叉核对现有 `app/gemini_client.py`、`app/dingtalk_bot.py`、`app/config.py`、`app/database.py`、`app/openai_client.py`。
- 评审方式：3 个 `gpt-5.6-sol` 子代理并行只读评审，分别负责可靠性/并发、代码集成/测试、安全/部署。
- 限制：未读取 `.env`、`secrets/**` 或凭据；未修改业务代码；未启动 `task.py start`；未执行生产验证。

三份报告均判断原始规划存在阻塞项，未发现 P0。以下为去重后的 13 条 finding，以及本次规划层处置。

## Findings 与处置

### AR-01（P1）：SDK 的首次异步迭代可能才真正发出请求

- 证据：`app/gemini_client.py::call_gemini_stream`、`analyze_complexity_with_model`；原设计只包住 `await generate_content_stream(...)`。
- 场景：await 成功返回 async iterator，但首次 `__anext__()` 才抛 503/网络/认证异常。
- 影响：主路径故障绕过 fallback/熔断，核心目标失效。
- 处置：已写入 `design.md` 的 pre-output 状态机：从 await 到首个用户可见 thinking/content chunk 前都可触发一次 fallback；实现清单补充 await 抛错与首拉流抛错测试。search metadata、空 candidate 和 usage metadata 不算可见输出。

### AR-02（P1）：同步 Redis I/O 会阻塞异步 event loop

- 证据：`app/database.py::RedisClient.get_instance` 使用同步 Redis，连接/读写 timeout 为 5 秒；原设计计划在 async 调用点直接读写。
- 影响：Redis 黑洞会拖住其他群聊、卡片更新和流式请求；fail-open 只能保证最终不抛错，不能保证不放大延迟故障。
- 处置：设计改为熔断专用短 timeout（默认 0.2 秒量级）和 `asyncio.to_thread` 隔离同步 I/O；实现清单补充 event-loop responsiveness 测试。

### AR-03（P1）：Soul executor 取消后线程可能继续写熔断或调用付费 fallback

- 证据：`app/dingtalk_bot.py::_ask_lightweight_model` 使用 `run_in_executor`；取消等待者不会强制终止已运行线程。
- 影响：违反取消不触发 fallback/熔断，可能产生 Vertex 付费请求和全 bot 副作用。
- 处置：设计改为 worker 只执行一次 provider call；熔断写入和 fallback 决策回到 event loop，并在 task 未取消时执行。实现清单加入线程屏障测试，确认取消后无 circuit write/fallback submit。已开始的底层单次调用无法强制终止，但不得拥有后续副作用权限。

### AR-04（P1，已确认取舍）：无 half-open 协调存在恢复竞态

- 证据：`gemini_circuit:{BOT_ID}:antigravity`、600 秒 TTL；原规划明确不加 half-open 分布式锁。
- 场景：TTL 到期后并发探测，较晚失败可能覆盖较早成功，重新写入 600 秒 marker。
- 影响：恢复窗口可能被延长，增加 Vertex 使用量。
- 处置：不改变用户已确认的“保持简单、不要 half-open 锁”。`prd.md`/`design.md` 现在明确记录这是可接受的成本/恢复稳定性取舍，不声称严格恢复协调；保留并发竞态测试和日志证据要求。该项不是待用户重新决策的阻塞项，但实现不得掩饰其语义。

### AR-05（P2）：Redis client 获取/首次 ping 的异常覆盖不完整

- 证据：`app/database.py::RedisClient.get_instance` 当前主要捕获 `redis.ConnectionError`；认证、构造、协议或其他 timeout 异常可能在熔断读写前逃逸。
- 影响：Redis 故障可能反过来阻断 Gemini 请求，违反 fail-open。
- 处置：设计和实施清单要求熔断模块包住 client 构建、ping、exists、setex 的全部普通异常，并增加非 `ConnectionError` 测试；不依赖现有数据层捕获范围。

### AR-06（P1）：黑名单式 `str(exception)` 脱敏无法兑现绝对安全边界

- 证据：`app/gemini_client.py` 及 `app/dingtalk_bot.py` 的既存异常出口；SDK `repr` 可能包含未标记的 URL、header、request/response 对象或嵌套数据。
- 影响：凭据、请求体、Authorization 或 traceback 可能进入日志、usage、footer 或 error chunk。
- 处置：设计改为允许列表式安全摘要：已知类型只取受控类别/状态码/安全 reason，未知异常不直接输出原始 message；禁止 `repr`、traceback、exception chain 和 request/response 原文。实现清单补齐裸 key、嵌套 JSON、URL、Bearer 换行、HTML、异常链等测试。

### AR-07（P1）：流中及本地异常仍有既存原始日志出口

- 证据：`app/gemini_client.py::call_gemini_stream` 的 chunk 处理 catch、外层 catch；`analyze_complexity_with_model` 的 `traceback.print_exc()`；`app/dingtalk_bot.py::_ask_lightweight_model` 的日志及错误消费路径。
- 影响：只给新 fallback 分支加 sanitizer 仍会泄露流迭代、chunk 解码、本地解析或 Soul 异常。
- 处置：`design.md` 增加 provider await/首拉流、已有输出后的 iterator、chunk 解析、本地转换/JSON、取消、fallback 双失败的错误矩阵；实施清单要求逐项封闭现有 sink，并用 stdout/stderr 捕获做回归测试。流中异常不 fallback，但也不得输出原文。

### AR-08（P2）：外部错误文本直接进入 DingTalk HTML 状态栏

- 证据：`app/dingtalk_bot.py` 的 footer 使用 `<font>`，原设计只规定脱敏，未规定渲染编码。
- 影响：`</font>`、Markdown 链接、换行或双向控制符可能伪造/破坏卡片状态栏，即使没有凭据泄露。
- 处置：设计和实施清单加入独立 footer 显示编码：模型名与安全摘要均转义、折叠控制字符并限制长度；增加恶意 HTML/Markdown/控制字符测试。

### AR-09（P1）：fallback-only 可用性没有真实证据，整份 env 回滚会混合风险

- 证据：原设计只有容器/成功请求/Redis TTL 观察，回滚写成恢复 env 备份；`SEARCH_FALLBACK_PROVIDER` 是独立风险开关。
- 影响：Vertex base/key/alias/权限不兼容可能直到主路径故障才暴露；整份 env 恢复可能意外重新打开旧 `google_search()` 注入。
- 处置：设计和实施计划增加 fallback-only canary（实际 fallback base/key、Vertex alias、无敏感 prompt、退出码/响应/模型证据），并要求熔断/fallback 配置回滚与 `SEARCH_FALLBACK_PROVIDER` 回滚分组、精确改键；不以容器 Up 或配置存在替代真实请求证据。

### AR-10（P1）：预分析/Soul 的共享熔断可能影响主对话 footer 语义

- 证据：三类调用共享 `gemini_circuit:{BOT_ID}:antigravity`，主对话在 `app/dingtalk_bot.py` 消费 usage/footer。
- 场景：预分析或 Soul 先失败并 fallback，主对话随后命中共享 marker，显示 `circuit open`。
- 影响：若不区分调用归属，容易把后台异常详情错误显示为主对话详情。
- 处置：保留共享 marker（后台失败代表同一主 provider 健康状态），但明确后台调用自身不产生 usage/footer；如果主对话实际命中 Vertex，footer 只描述主对话实际 fallback，使用 `circuit open` 而不读取后台历史详情。实施清单增加该完整链路测试。

### AR-11（P1）：只按模型名无法可靠选择四档 fallback override

- 证据：`app/config.py` 中档位可能配置相同主模型名；`app/gemini_client.py::call_gemini_stream` 原签名只有 `target_model`。
- 影响：无法确定 `MODEL_ROUTER_FALLBACK`/`MODEL_FAST_FALLBACK` 等对应关系，可能把错误的 override 用到调用点。
- 处置：设计和实施计划要求调用方显式传递 `router`/`lite`/`fast`/`pro` route slot，模型选择 helper 按 slot 查配置，禁止模型字符串反推；增加主模型名相同、override 不同的测试。

### AR-12（P1）：未配置 fallback 的“保持现状”与安全错误边界冲突

- 证据：原 PRD 同时要求未配置 fallback 保持现有错误文本、又要求原始异常不得进入日志/用户回复。
- 影响：实现者可能为了兼容继续泄露原始 SDK 异常，或无法判断验收口径。
- 处置：明确保持的是控制流、chunk 结构和返回形状；异常文本安全边界是本任务有意收紧的行为变化。无论 fallback 是否配置，三个调用点都使用统一安全摘要。

### AR-13（P2）：footer 验收没有绑定可执行入口

- 证据：原实施计划只要求“真实 UI 字符长度检查”，未固定 footer 纯函数或最终 `stream_update` 消费断言。
- 影响：usage 传输测试通过仍可能出现字段顺序、覆盖、转义或 1000 字符边界错误。
- 处置：实施计划要求抽取纯函数表驱动测试，或 mock `stream_update` 做消费测试；覆盖正常、即时 fallback、已有 circuit、双失败、特殊字符和长度上限，再做真实运行观察。

## 二次处置结论

- 规划文档已补齐上述边界、测试和部署验收要求；AR-04 按用户已确认的无 half-open 锁取舍保留并显式化。
- 本记录不代表业务代码已经实现或生产 fallback 已验证；实现阶段仍必须按 `implement.md` 完成测试、代码、真实 canary 和第二轮已实现 diff 对抗性评审。
- 当前仍保持 Trellis `planning`，未执行 `task.py start`；需要用户明确批准实现后才可进入实现阶段。
