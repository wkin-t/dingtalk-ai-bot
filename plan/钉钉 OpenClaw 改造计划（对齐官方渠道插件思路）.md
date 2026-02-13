## 钉钉 OpenClaw 改造计划（对齐官方渠道插件思路）

### Summary

将当前“钉钉服务本地编排 + OpenClaw 仅做模型代理”迁移为“OpenClaw 主导编排”。
目标是：减少本地 prompt/历史拼装，统一在 Gateway 管理 agent、路由、工具与会话。

### Public APIs / Interfaces 变更

1. 入站链路

- 现状：钉钉服务接收消息后调用 /v1/chat/completions
- 目标：钉钉消息接入 OpenClaw channel（插件化），或保持过渡代理但仅透传标准消息事件

2. Chat 调用协议

- 保留 /v1/chat/completions 兼容层
- 客户端请求体简化为：
    - model: openclaw:{agentId}（或 header 指定 agent）
    - messages: 最近必要消息
    - user: 稳定 session key（如 dingtalk:{conversationId}:{senderId}）

3. Tool 调用接口

- 新增/启用 /api/tools/invoke 路径用于显式工具调用，不混入 chat prompt

4. 配置接口

- 逐步废弃 OPENCLAW_GROUP_AGENT_MAPPING
- 迁移到 Gateway channel/bindings 策略（群->agent 的绑定在 Gateway 管理）

### 实施步骤

1. 安全整改（先做）

- 立即轮换泄露 token
- 清理文档与环境模板中的真实密钥

- 删除/降级本地 system prompt（仅保留极小平台约束）
- 去除 Gemini 专属路由字段在 OpenClaw 模式下的展示与逻辑
- chat 仅负责对话，工具负责动作

5. 清理重复代码（第4阶段）

- 统一 app/ai/handler.py 与 app/dingtalk_bot.py 的 OpenClaw 分支
- 保留单一构造消息入口，降低维护成本

### 测试与验收

1. 会话连续性

- 同一 user 键多轮对话，确认上下文连续
- 不同群/不同用户隔离

2. 路由正确性

- 群 A/B 分别命中预期 agent
- 未绑定群的行为符合 Gateway 策略（拒绝或默认）

3. Token 与响应质量

- 对比改造前后 prompt tokens 明显下降
- 回复一致性提升（减少“被本地 prompt 覆盖”）

4. 工具调用

- /api/tools/invoke 成功调用并返回结构化结果
- chat 与 tool 失败路径可观测（日志+错误码）

5. 回归

- 钉钉卡片流式更新、重试、清空记忆等功能可用
- OpenAI 兼容接口仍可被现有调用方使用

### Assumptions / Defaults

- 你选择了“迁移为渠道插件”方向（默认推荐）。
- 默认采用 Gateway 管理路由与策略，应用侧仅做平台适配和展示。

- 过渡期允许保留 /v1/chat/completions，但不再维护重 prompt 编排。
参考：

- OpenClaw OpenAI HTTP API: https://docs.openclaw.ai/gateway/openai-http-api
- OpenClaw Tools Invoke HTTP API: https://docs.openclaw.ai/zh-CN/gateway/tools-invoke-http-api
- OpenClaw 会话与路由文档: https://docs.openclaw.ai/gateway/configuration/chat-and-routing

- 你仓库中的相关实现：app/openclaw_client.py, app/ai/handler.py, app/dingtalk_bot.py, app/config.py, OPENCLAW_DEPLOYMENT.md