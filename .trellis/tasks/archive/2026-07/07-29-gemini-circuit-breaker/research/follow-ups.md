# 后续跟进项

记录二次 Sol double check 中确认、但本次不立即扩 scope 的问题，避免随上下文压缩遗忘。

## Phase 3.2 Debug retrospective

本任务不触发 `trellis-break-loop`：多轮修改来自独立的 QA findings（安全显示、行尾、测试证据和日志边界），没有同一个运行时根因被重复修复后再次回归。后续并发测试与日志治理按下面的独立跟进项处理。

## P2-6：真实并发与取消屏障测试

- **范围**：`app/gemini_circuit.py` 的 Redis 黑洞隔离，以及 Soul/主对话在同步线程调用等待期间的取消传播。
- **需要补的证据**：启动真实阻塞的 Redis/mock worker，同时运行 event-loop ticker，证明 ticker 持续推进；在线程屏障释放前取消等待 task，证明不会写熔断 marker、不会提交 Vertex fallback。
- **当前状态**：实现结构已使用 `asyncio.to_thread` 并保留 `CancelledError` 传播；现有测试仍是 wrapper/mock 断言，未证明真实并发时序。
- **触发时机**：下次修改熔断状态机、取消边界或准备生产部署前必须补齐。

## 日志治理：既存 prompt/system 调试输出

- **范围**：`app/gemini_client.py` 的预分析内容日志，以及 `app/dingtalk_bot.py` 中 prompt、历史消息和完整 system prompt 的调试输出。
- **当前状态**：属于本任务之前的既存日志，不是本次熔断/fallback 异常泄露链路；本任务不扩大范围处理。
- **后续要求**：另立日志治理任务，按 logging spec 将内容替换为长度、hash 或固定摘要，并覆盖普通 prompt、历史消息、system prompt、生图/改图 prompt 等出口。
- **触发时机**：下一次日志安全治理或生产日志审计前必须处理；在此之前不得把当前任务宣称为全仓库“无 prompt 日志”。
