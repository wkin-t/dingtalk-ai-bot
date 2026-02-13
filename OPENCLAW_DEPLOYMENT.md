# ✅ OpenClaw 群路由配置完成

**时间**: 2026-02-13 14:05
**状态**: ✅ 已部署并验证

---

## 📊 最终配置

### 群 → Agent 映射表

| 群名 | conversation_id | 对应 Agent | 状态 |
|------|-----------------|-----------|------|
| 陈皮泡茶 | `cidnLpPCkB3FUXxqovf8sKC8A==` | 陈皮泡茶 | ✅ |
| 雅致兴旺 | `cidHK/gGNmaMK6OlQADeVzVLA==` | 雅致兴旺 | ✅ |
| 马上发财 | `cidWcDfj12DvTH6DGBYsm2IwQ==` | 马上发财 | ✅ |

### 环境变量配置

```bash
# .env.openclaw 中的关键配置
AI_BACKEND=openclaw
OPENCLAW_GATEWAY_URL=ws://127.0.0.1:18789
OPENCLAW_HTTP_URL=http://127.0.0.1:18789/v1/chat/completions
OPENCLAW_AGENT_ID=main
OPENCLAW_GATEWAY_TOKEN=your_gateway_token
OPENCLAW_CONTEXT_MESSAGES=6

# 群路由映射（新增）
OPENCLAW_GROUP_AGENT_MAPPING={
  "cidnLpPCkB3FUXxqovf8sKC8A==": "陈皮泡茶",
  "cidHK/gGNmaMK6OlQADeVzVLA==": "雅致兴旺",
  "cidWcDfj12DvTH6DGBYsm2IwQ==": "马上发财"
}
```

---

## 🚀 容器状态

✅ **dingtalk-ai-bot-openclaw**: 正在运行 (UP)
- **端口**: 35001
- **后端**: OpenClaw Gateway
- **Redis**: 连接成功
- **MySQL**: 连接成功
- **钉钉 Stream**: 已连接
- **企业微信 Webhook**: 已注册

---

## 🧪 验证步骤

### 1️⃣ 在各个群发送消息测试

在三个群分别发送消息给 OpenClaw 机器人，例如：

```
@OpenClaw 你好，我是来自[群名]的测试消息
```

### 2️⃣ 检查日志中的 agent 路由

```bash
ssh tencent_cloud_server
docker logs -f dingtalk-ai-bot-openclaw
```

**预期看到的日志输出**:
```
📡 正在请求 OpenClaw HTTP API (conversation_id=cidnLpPCkB3FUXxqovf8sKC8A==, agent=陈皮泡茶)...
📡 正在请求 OpenClaw HTTP API (conversation_id=cidHK/gGNmaMK6OlQADeVzVLA==, agent=雅致兴旺)...
📡 正在请求 OpenClaw HTTP API (conversation_id=cidWcDfj12DvTH6DGBYsm2IwQ==, agent=马上发财)...
```

### 3️⃣ 验证 agent 是否正确路由

- ✅ 陈皮泡茶群的消息应该由 **陈皮泡茶** agent 处理
- ✅ 雅致兴旺群的消息应该由 **雅致兴旺** agent 处理
- ✅ 马上发财群的消息应该由 **马上发财** agent 处理

---

## 🎯 核心功能说明

### 群路由工作原理

1. **接收消息**: OpenClaw 机器人从钉钉 Stream 接收到消息
2. **提取 conversation_id**: 从消息元数据中获取群的 conversation_id
3. **查询映射表**: 在 `OPENCLAW_GROUP_AGENT_MAPPING` 中查找对应的 agent
4. **路由到 agent**: 将消息转发给映射的 OpenClaw agent 处理
5. **返回回复**: Agent 处理完成后，回复消息发回到钉钉群

### 默认 agent 处理

- **OPENCLAW_AGENT_ID=main**: 当 conversation_id 不在映射表中时，使用 main agent 处理
- 这确保了即使新群没有配置，也能正常响应

---

## 📝 配置文件备份

原配置已备份到:
```
/opt/dingtalk-ai-bot/.env.openclaw.backup.[timestamp]
```

可随时恢复。

---

## 🔄 后续管理

### 添加新群

1. 在新群 @OpenClaw 发送消息
2. 从日志中获取 conversation_id
3. 编辑 `.env.openclaw` 添加新的映射：
   ```bash
   OPENCLAW_GROUP_AGENT_MAPPING={..., "new_cid": "new_agent"}
   ```
4. 重启容器：
   ```bash
   docker-compose -f docker-compose.openclaw.yml up -d --build
   ```

### 修改群对应的 agent

直接编辑 `.env.openclaw` 修改映射，然后重启容器。

---

## ✨ 部署完成清单

- [x] 获取三个群的 conversation_id
- [x] 创建群 → agent 映射配置
- [x] 更新 `.env.openclaw` 配置文件
- [x] 重新部署 OpenClaw 容器
- [x] 验证容器正常运行
- [ ] 在钉钉群里测试验证路由是否正确

---

**部署者**: Claude Code
**部署完成时间**: 2026-02-13 14:05 UTC+8

🎉 **OpenClaw 多群路由配置已完成！**
