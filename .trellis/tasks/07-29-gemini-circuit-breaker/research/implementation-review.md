# Gemini 熔断 + 自动恢复机制：实现后对抗性复审

## 复审范围与方法

- 时间：2026-07-29。
- 范围：本任务实现 diff、`app/gemini_client.py`、`app/gemini_circuit.py`、`app/error_safety.py`、`app/dingtalk_bot.py`、`app/ai/backend.py`、`app/ai/handler.py`、配置、测试和部署说明。
- 方法：3 个 `gpt-5.6-sol` 子代理并行只读复审，分别从可靠性/并发、代码集成/测试、安全/部署视角检查；子代理未修改工作区。
- 限制：未读取 `.env`/`secrets/**`，未修改生产 env，未执行部署或真实 Vertex/DingTalk 请求。

## Findings 与处置

### 已修复的阻塞项

1. **Soul 的 Redis 写入异常会阻断 fallback**：将熔断写入放入独立 fail-open 处理；普通 Redis 异常只记录固定安全日志，仍继续一次 fallback；取消仍传播。
2. **可见输出缺少 usage 时被误判为空响应**：空响应判断现在只针对“没有可见输出且 output token 为 0”；已经产生可见内容的流即使 SDK 没有 usage 也不会被伪造为失败。
3. **Gemini SDK 首次 `__anext__()` 才报 provider 异常**：主对话和预分析都把 provider await、异步迭代器拉取纳入预输出状态机。
4. **异常允许列表失效**：新增纯 `app.error_safety` 模块；未知异常不回传 SDK `.message`，只输出固定类别、经校验的 HTTP 状态码和有限的固定 reason。该模块同时修复了新进程导入循环。
5. **模型名和 route slot 语义不可靠**：调用方显式传递 `router/lite/fast/pro` slot，不再从重复的主模型名反推档位；成功 fallback 优先显示响应 `model_version`，同时保留主模型请求名。
6. **已有熔断 footer 可能沿用旧错误详情**：消费端对 `circuit_open=True` 强制显示固定 `circuit open`，不读取历史错误；新 fallback 才显示“实际 fallback 模型 → 主模型名 → 长安全异常”。
7. **stale marker 与 fallback client 缺失时无告警**：增加一次性固定告警并继续主路径探测；不把 marker 当作永久硬依赖。
8. **熔断模块导入业务数据层造成副作用**：改为直接读取 Redis 连接配置，避免导入时触发历史数据层的 5 秒 ping；加入 client/ping/exists/setex 的 fail-open 和短退避。
9. **图片数据 URL 本地转换异常仍打印原文**：改为取消继续传播，其余异常只写安全类别日志。
10. **Soul 文件与钉钉 error chunk 仍可绕过统一安全边界**：读取/截断日志和最终 error card consumer 已改为安全摘要/显示编码；生图/改图路径按本任务非目标保持不变。

### 已确认并保留的边界

- 流中已有可见 thinking/content 后的异常不 retry、不打开熔断；这是为了避免把已发送半段回复从头重复生成。
- 未实现 half-open 分布式锁；TTL 到期后的并发探测仍可能发生，较晚失败可重新写入 600 秒 marker。这是用户确认的“保持简单”取舍，不宣称严格恢复协调。
- `asyncio.to_thread` 无法强制终止已经开始的同步 Redis 写入。若取消恰好发生在该线程已获准写 marker 之后，底层写入可能完成；取消不会继续提交 provider/fallback 决策。当前产品没有用户主动取消入口，因此保留专用同步 Redis helper，避免为低概率竞态引入更复杂的异步 Redis 依赖。

## 运行证据

- `python -c "import app.gemini_client; print('fresh import ok')"`：使用一次性测试 dummy key 后退出码 0，输出 `fresh import ok`。
- `python -m compileall -q app main.py`：退出码 0。
- 相关测试命令覆盖熔断、Gemini 流式、钉钉 footer、Soul、搜索开关、OpenAI 兼容路径和 backend 契约：`181 passed`。
- 全量测试首次受默认临时目录权限影响；改用 workspace 内专用 basetemp 后，整改前 `497 passed`，QA 整改后 `500 passed`。
- pytest 的缓存目录权限和 pytest-asyncio 默认 loop scope 仅产生 warning，未导致测试失败。

## 未完成的上线证据

- 未执行 fallback-only canary、Vertex alias 真实请求、Redis TTL 线上观察、DingTalk 卡片 UI 观察或生产配置 recreate；这些需要真实 fallback base/key 和明确部署授权。
- 因此本记录证明的是代码路径和本地回归通过，不证明生产 Vertex fallback 已可用。
