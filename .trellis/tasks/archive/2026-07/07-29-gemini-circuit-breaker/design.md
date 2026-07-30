# Gemini 熔断 + 自动恢复机制技术设计

## 1. 设计目标与已确认边界

目标是：Gemini 主路径 antigravity 在 provider 建流阶段出现异常时，当前文本请求自动切到 Vertex 完成；同时在当前 bot 的冷却窗口内跳过主路径，避免每条消息重复撞击不可用账号池。

已确认的边界：

- 只覆盖 `call_gemini_stream`、`analyze_complexity_with_model`、`_ask_lightweight_model` 三个文本调用点。
- `app/image_gen.py` 的生图/改图以及 `direct_client` 不改动。
- 只在 provider 请求尚未产生用户可见输出的预输出阶段重试；对异步 SDK 而言，这包括 `generate_content_stream` 的 await、异步迭代器首次及后续拉取，直到首个用户可见的 thinking/content chunk。流中已经产生用户可见输出后的异常不重放。
- provider 建流阶段的网络、超时、HTTP、SDK 等异常都触发熔断，不再匹配 `no available accounts` 文本或特定状态码。
- `asyncio.CancelledError`、本地消息转换/配置构造/解析错误不触发熔断。
- Redis 是保护性状态存储，不是请求硬依赖；Redis 故障 fail-open。
- 原生模型 SDK 的搜索工具保持不变；历史 `google_search()` 摘要注入路径默认关闭但保留显式配置回滚。

## 2. 当前代码事实

`app/gemini_client.py` 目前有两类 client：对话/搜索使用的 `client`，以及生图等中转站不覆盖的端点使用的 `direct_client`。fallback 只能新增第三个对话 client，不能复用或替换 `direct_client`。

主对话和预分析已经使用 `generate_content_stream`；Soul 的 `_ask_lightweight_model` 仍通过线程池调用同步 `generate_content`。三条路径的调用形态不同，但都能在 provider 调用边界执行同一套“选择 client → 建立请求 → 失败切 fallback”的语义。

钉钉 footer 当前从 `usage` chunk 读取模型名，再组装 thinking、温度、top_p 和搜索状态。因此 fallback 元数据放入现有 `usage` 字典，比新增全局变量或把诊断文本混进模型正文更安全。

旧搜索摘要路径仍然可达：`app/config.py` 当前默认 `SEARCH_FALLBACK_PROVIDER=none`，`app/openai_client.py` 只有在显式设置为 `gemini` 时才调用 `gemini_client.google_search()`。本次不删除兼容代码；显式设置 `gemini` 仍可回滚。

## 3. 配置与 client 构建

新增配置：

| 配置 | 语义 | 默认/约束 |
|---|---|---|
| `GEMINI_API_BASE_FALLBACK` | Vertex fallback 的 API base | 空时不构建 fallback client |
| `GEMINI_API_BASE_FALLBACK_KEY` | fallback base 的认证 key | base 非空时必须显式提供；不复用 `GEMINI_API_KEY` |
| `MODEL_ROUTER_FALLBACK` 等四项 | 可选模型名 override | 未设时原样传主模型名，由 Vertex alias 处理 |
| `GEMINI_CIRCUIT_REDIS_TIMEOUT_SECONDS` | 熔断状态 Redis 的连接/读写超时 | 默认 `0.2` 秒量级；仅用于熔断状态，不改变历史数据层超时 |
| `SEARCH_FALLBACK_PROVIDER` | 旧搜索摘要 fallback 开关 | 默认 `none`；设为 `gemini` 可回滚 |

`GEMINI_API_BASE_FALLBACK` 去除尾部 `/`。只有 base 和显式 key 都非空时才构建 `fallback_client`；缺 key 时输出不含凭据的配置告警并保持 `fallback_client=None`。启动时不打印 key。

fallback 模型选择按以下顺序：

1. 当前请求的主模型名；
2. 调用方显式传入的 route slot（`router`、`lite`、`fast`、`pro`）选择对应的 `MODEL_*_FALLBACK` override；禁止根据模型字符串反推 slot，因为多个档位可能配置成同一个主模型名；
3. 如果该 slot 配置了 override，则使用 override；
4. 未配置 override 时把原模型名交给 Vertex 侧 alias。

不再把四个 `_FALLBACK` 环境变量视为启用前提，也不新增必需的 JSON 模型映射配置。

## 4. 熔断状态机

Redis key 固定为：

```text
gemini_circuit:{BOT_ID}:antigravity
```

状态值只需要一个无敏感内容的 marker；不要把原始异常或可能包含凭据的请求信息写进 Redis。TTL 固定 600 秒。系统不增加 half-open 分布式锁，TTL 到期后的并发请求允许各自探测主路径。

熔断模块使用独立的 Redis client/连接参数，socket connect/read timeout 默认采用 200ms 量级的短超时，而不是复用历史数据层的 5 秒超时。异步调用点通过 `asyncio.to_thread` 调用同步 Redis helper；同步 Soul 路径使用同一个 bounded-timeout helper 的同步入口。client 构建、首次 `ping`、`exists`、`setex` 的任意 `Exception` 都在模块边界内转为 fail-open，不能依赖 `RedisClient.get_instance()` 只捕获某一种连接异常。

| 当前状态 | 事件 | 动作 | 下次请求 |
|---|---|---|---|
| closed/无 key | provider 建流成功 | 使用主路径 | 继续主路径 |
| closed/无 key | provider 建流异常，fallback 可用 | 写入 600 秒 marker，尝试 Vertex | 直接 Vertex |
| closed/无 key | provider 建流异常，fallback 不可用 | 保持现有错误处理，记录配置/故障日志 | 主路径继续探测 |
| open/key 存在 | fallback 可用 | 跳过主路径，直接 Vertex | 仍直接 Vertex，直到 TTL 到期 |
| open/key 存在 | fallback 不可用 | 忽略 stale marker，记录配置告警，尝试主路径 | 主路径继续探测 |
| 任意 | Redis 读写失败 | fail-open；不阻塞请求 | 继续主路径探测 |

`open_circuit()` 只在本次确实存在可用 fallback client 时有价值；无 fallback client 时不应写一个会在未来造成歧义的 marker。所有 Redis 异常都在熔断模块内消化。

不使用 half-open 锁是已确认的简化取舍：TTL 到期后可能有多个主路径探测，较晚完成的失败可以覆盖较早成功并重新写入 600 秒 marker。实现不得声称这提供严格的恢复协调；日志应记录 probe/fallback 结果，测试应覆盖该竞态并确认它不会破坏数据一致性或把异常详情写入 Redis。

## 5. 请求调用边界

### 5.1 主对话 `call_gemini_stream`

保持消息转换、配置构造、搜索工具挂载、thinking 配置等逻辑在重试边界之外。建立 `contents/config` 后：

1. 读取熔断状态；若 open 且 fallback client 可用，选择 fallback client/model，标记 `circuit_open=True`。
2. 否则选择主 client/model，`circuit_open=False`。
3. 对 provider 异步流使用显式的 pre-output 状态机：`await generate_content_stream(...)` 成功只代表拿到 iterator，必须继续拉取 iterator；在首个用户可见的 thinking/content chunk 之前捕获 await 或 `__anext__()` 异常。
4. `search` 请求 metadata、空 candidate、usage metadata 等本地/非可见信息不改变 pre-output 状态。只有真正准备向下游 yield 用户可见 thinking/content 时才将状态切换为 `output_started`。
5. 主路径预输出异常且 fallback 可用时：保存主模型异常的安全摘要到当前进程变量，打开熔断，立即用 fallback client/model 建流并从同一状态机开始一次。fallback 也必须覆盖 await 和首拉流异常。
6. fallback 建流成功后，继续原有 chunk 迭代；fallback 建流失败则返回包含两段模型名和两段脱敏异常的 error chunk。
7. `output_started` 后的 iterator、chunk 属性访问或解码异常不重放、不写熔断；保留现有终止/继续语义，但日志和用户错误 chunk 只能使用安全摘要。

这条边界避免“主模型已经输出一半，fallback 又从头输出”造成重复内容，同时覆盖 SDK 延迟到首次 `__anext__()` 才真正发请求的实现。原有 chunk 处理异常也不应被改造成 fallback 触发器。

### 5.2 路由预分析 `analyze_complexity_with_model`

沿用异步 `generate_content_stream`，因为预分析没有向用户输出正文，所以从 await 到完整 provider iterator 收集完成都属于 provider 预输出阶段：主路径 await/任意 iterator 拉取异常使用同一 fallback 选择逻辑；本地 JSON 收集、正则提取和 JSON 解析异常不触发熔断或 fallback，仍返回原有保守路由默认值。预分析 fallback 只写服务端脱敏日志，不为预分析自身产生 usage/footer；如果它写入的共享 marker 让同一消息的主对话直接命中 Vertex，主对话 footer 只描述主对话实际使用的 fallback，并且不显示预分析异常详情。

### 5.3 Soul `_ask_lightweight_model`

保持现有 `run_in_executor`，但把线程 worker 限定为“一次且仅一次同步 provider 调用”，不在线程内串联熔断写入或 fallback 决策：

- 熔断已打开时，外层 coroutine 确认 fallback client 可用后，在线程池提交一次 fallback provider 调用；
- 主调用在线程中抛出 provider 异常后，先回到 event loop 生成安全摘要、检查当前 task 未被取消，再写熔断并提交一次 fallback provider 调用；
- `asyncio.CancelledError` 不转成普通错误。取消发生后，底层已经开始的单次线程调用无法强制终止，但它不会再拥有写 Redis 或提交 fallback 的权限；外层不等待/不处理取消后的 provider 结果；
- 返回值仍为 `str`，fallback 元数据只记录安全日志，不进入对话 footer。

## 6. 错误与 metadata 契约

### 6.1 脱敏

增加一个可单测的允许列表式错误摘要 helper，输入异常对象和调用阶段，输出长度不超过 1000 的用户/日志安全文本。安全摘要只允许进入以下受控字段：

- 本地映射的错误类别/阶段；
- 通过整数校验的 HTTP 状态码；
- 已知 SDK/HTTP 异常中明确标注为 reason/message 的字段，先移除控制字符、URL query/fragment、HTML/Markdown 结构和已知运行时 secret，再截断；
- 未知异常只输出固定类别和固定安全短语，不直接输出未经验证的 `str(exception)`。

明确禁止：

- `repr(exception)`、`traceback.format_exc()`、`traceback.print_exc()` 和 exception chain；
- request/response 对象、header、Authorization/Bearer、API key/token/secret/password、请求体、HTML/JSON 原文；
- 将安全摘要当作可信富文本直接拼到钉钉 footer。

对已知运行时 secret 做二次替换只能作为防线，不能替代允许列表。原始异常只在当前调用栈内短暂参与 provider 故障处理。Redis marker、日志、usage、DingTalk footer 和 error response 都只使用安全摘要或固定 marker。

错误出口矩阵：

| 阶段 | fallback/熔断 | 日志 | 用户输出 |
|---|---|---|---|
| 主 provider await 或首个可见输出前的 iterator 拉取 | 触发一次 fallback，写 marker | 安全主模型摘要 | fallback 成功时通过 usage/footer 展示安全主模型摘要 |
| 已产生可见输出后的 iterator 异常 | 不触发、不写 marker | 安全流中异常摘要 | 沿用现有终止/error chunk 结构，不重放、不输出原文 |
| chunk 属性/解码、消息转换、配置构造、JSON 解析 | 不触发、不写 marker | 固定类别或安全摘要 | 沿用现有结构，不输出原文 |
| task cancellation | 不触发、不写 marker | 不打印原始异常 | 直接传播取消 |
| fallback provider 失败 | 不再尝试第三路径 | 安全 fallback 摘要 + 安全主摘要 | 双失败 error chunk 按固定顺序展示 |

### 6.2 成功 fallback 的 usage

主对话成功后最终 `usage` 至少包含：

```python
{
    "model": actual_model,
    "requested_model": primary_model,
    "fallback": True,
    "fallback_error": safe_primary_error,
    "circuit_open": False,  # 当前请求直接命中既有熔断时为 True
    "input_tokens": ...,
    "output_tokens": ...,
}
```

无 fallback 时保持现有 usage 形状和模型值，但所有异常出口仍使用上面的安全摘要。已打开熔断直接走 Vertex 时，`fallback=True`、`circuit_open=True`，`fallback_error` 使用短标记 `circuit open`，不读取上一次异常详情。

钉钉 footer：

- 本次触发 fallback：`🤖 {actual_model} | ⚠️ 主模型 {requested_model}: {fallback_error}`；其中两个模型名和错误摘要先经过显示编码，控制字符折叠，不能改变 `<font>` 状态栏结构；
- 命中已有熔断：`🤖 {actual_model} | ⚠️ 主模型 {requested_model}: circuit open`；其中不读取历史错误详情；
- 正常主路径：沿用当前 footer，不增加标记。

footer 应由独立的纯函数/明确的消费测试构造，不能把未经编码的 provider 文本直接插入现有 HTML 状态栏。

### 6.3 fallback 双失败

error chunk 的文本顺序固定为：

```text
❌ fallback 模型 {fallback_model}:
{safe_fallback_error}

主模型 {primary_model}:
{safe_primary_error}
```

钉钉现有错误消费逻辑可以继续终止卡片；只需确保传入的 error 已脱敏。

## 7. 旧搜索 fallback 的关闭

只改默认开关，不删函数：

- `SEARCH_FALLBACK_PROVIDER` 默认值改为 `none`；
- `app/openai_client.py` 的 `_build_search_fallback_summary` 在默认值下返回 `None`，显式 `gemini` 仍可用；
- 更新 `CLAUDE.md`/部署文档，说明原生 SDK 搜索是默认路径、旧摘要 fallback 是手动回滚路径；
- 保留 `google_search()` 及其单测，保留显式 `gemini` 的兼容测试；
- `app/gemini_client.py::call_gemini_stream` 中的 SDK `google_search` tool 不受影响。

## 8. 部署、验证与回滚

本设计只记录部署步骤；本次实现未执行生产变更。上线前要在不输出 secret 值的前提下核对：

- 主 `GEMINI_API_BASE`/key 指向 antigravity；
- fallback base 指向 Vertex 路径，fallback key 显式存在；
- Vertex alias 能处理主模型名；
- `SEARCH_FALLBACK_PROVIDER=none` 在最终 Compose/env 解析结果中生效；
- 修改 env 后使用 `docker compose up -d`/项目约定的 recreate 流程，不能只 `docker restart`。

验证分层：

1. 单元测试验证状态机、配置、元数据、错误脱敏和三个调用点；
2. 编译检查和全量 pytest 验证回归；
3. 上线前先做 fallback-only canary：使用实际 fallback base/key、Vertex alias 和无敏感测试 prompt 直接完成一次真实响应；记录命令退出码、响应成功与否、实际模型名和非敏感日志，不把容器 `Up` 或“配置存在”当作 fallback 可用证据；
4. 真实部署只观察成功请求、fallback 日志和 Redis TTL，不在生产环境人为制造 antigravity 故障；主路径、fallback-only、熔断路由、钉钉 footer 和 `SEARCH_FALLBACK_PROVIDER=none` 分别留证；
5. 若 fallback 配置错误或熔断行为异常，只回滚本任务相关的 fallback base/key/model 和熔断代码配置并重新 recreate；旧搜索开关单独保持 `none` 或按明确授权显式恢复 `gemini`，不恢复整份 `.env` 覆盖其他合法变更。

## 9. 非目标

- 不删除 `google_search()`；
- 不修改 image generation/editing；
- 不做动态 quota reset 解析；
- 不做半开锁、跨 bot 全局熔断或文件降级；
- 不把无 half-open 锁的并发竞态包装成严格恢复保证；该风险是已确认的简单化取舍；
- 不把异常原文写入日志、Redis、文件、chat 或 memory；
- 本次实现不修改生产 env、不执行部署，也不把本地测试结果当作生产 fallback 可用性证明。
