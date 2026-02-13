# 🚀 OpenClaw 机器人配置指南

**部署状态**: ✅ OpenClaw 容器已启动 (端口 35001)

---

## 📋 当前配置

服务器已完成以下配置：

```bash
✅ .env.openclaw 已创建和更新
   - DINGTALK_CLIENT_ID: ✓
   - DINGTALK_CLIENT_SECRET: ✓
   - OPENCLAW_GATEWAY_URL: ws://127.0.0.1:18789
   - OPENCLAW_GATEWAY_TOKEN: ✓
   - OPENCLAW_AGENT_ID: main
   - OPENCLAW_CONTEXT_MESSAGES: 6
   - OPENCLAW_HTTP_URL: http://127.0.0.1:18789/v1/chat/completions
   - AI_BACKEND: openclaw
   - 钉钉直连模式: 已启用

✅ 容器状态
   - dingtalk-ai-bot-openclaw: UP (35001)
   - Redis: 连接成功
   - MySQL: 连接成功
   - 钉钉 Stream: 已连接
   - 企业微信 Webhook: 已注册
```

---

## 🔧 获取 conversation_id 步骤

### 1️⃣ 打开日志窗口

```bash
# 在服务器上运行此命令保持日志窗口打开
ssh tencent_cloud_server
cd /opt/dingtalk-ai-bot
docker logs -f dingtalk-ai-bot-openclaw
```

### 2️⃣ 在钉钉群里测试

- 打开每个需要配置的钉钉群
- 在群里 @OpenClaw 机器人，发送任意一句话，例如：
  ```
  @OpenClaw机器人 你好
  ```

### 3️⃣ 从日志中提取 conversation_id

监听日志中会出现类似的输出：
```
[用户消息] 群 conversation_id="cid_xxx123xxx", 内容="你好"
[处理消息] conversation_id="cid_xxx123xxx", agent="main"
```

💡 **提示**: 查找 `conversation_id=` 后面的值，格式为 `cid_` 开头

---

## ✏️ 更新路由映射

一旦你获得了 conversation_id，更新 `.env.openclaw` 中的映射：

```bash
# 编辑配置
ssh tencent_cloud_server
vi /opt/dingtalk-ai-bot/.env.openclaw

# 修改这一行（找到 OPENCLAW_GROUP_AGENT_MAPPING）:
# 原本:
OPENCLAW_GROUP_AGENT_MAPPING={}

# 改为 (示例):
OPENCLAW_GROUP_AGENT_MAPPING={"cid_xxx123xxx":"group-1","cid_yyy456yyy":"group-2"}
```

---

## 🔄 应用新配置

修改完 `.env.openclaw` 后，重启容器使其生效：

```bash
cd /opt/dingtalk-ai-bot
docker-compose -f docker-compose.openclaw.yml up -d --build

# 验证
docker logs --tail 50 dingtalk-ai-bot-openclaw
```

---

## 📊 验证配置

健康检查命令：

```bash
# 基础健康检查
curl http://127.0.0.1:35001/

# 模型列表
curl http://127.0.0.1:35001/v1/models

# 预期响应
{
  "service": "OpenClaw Proxy",
  "status": "ok"
}
```

---

## 🐛 调试技巧

| 症状 | 排查方法 |
|------|--------|
| 容器无法启动 | `docker logs dingtalk-ai-bot-openclaw` |
| 无法连接到 Gateway | 检查 `OPENCLAW_GATEWAY_URL` 和防火墙 |
| 路由未生效 | 确认 conversation_id 格式（应为 `cid_` 开头） |
| 消息未送达 | 查看 Redis 连接状态、检查 agent 名称 |

---

## 📝 配置清单

- [ ] 从日志中获取所有群的 conversation_id
- [ ] 更新 `.env.openclaw` 中的 `OPENCLAW_GROUP_AGENT_MAPPING`
- [ ] 重启 OpenClaw 容器
- [ ] 在钉钉群里验证消息正常接收和回复
- [ ] 检查 agent 路由是否正确（查看日志中的 agent 名称）

---

**下一步**: 按照上述步骤获取 conversation_id，然后更新路由映射配置 🎯
