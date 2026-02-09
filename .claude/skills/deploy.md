# Deploy Skill

自动部署钉钉 AI 机器人到腾讯云服务器。

## 使用方法

```
/deploy [服务类型]
```

**参数：**
- `gemini` - 部署 Gemini 后端版本（默认）
- `openclaw` - 部署 OpenClaw 后端版本
- `wecom` - 部署企业微信+钉钉双平台版本

## 工作流程

1. 推送本地代码到 GitHub
2. SSH 连接到腾讯云服务器
3. 拉取最新代码
4. 使用 Docker Compose 重新构建并部署
5. 显示服务状态和日志

## 服务配置

| 服务 | compose 文件 | 环境文件 | 端口 |
|------|-------------|----------|------|
| gemini | `docker-compose.yml` | `.env` | 35000 |
| openclaw | `docker-compose.openclaw.yml` | `.env.openclaw` | 35001 |
| wecom | `docker-compose.wecom.yml` | `.env.wecom` | 35002 |

## 部署路径

- 代码仓库：`/opt/dingtalk-ai-bot`
- 服务器别名：`tencent_cloud_server`（SSH 配置）

## 注意事项

- 需要提前配置 SSH 免密登录
- 部署前会自动提交并推送本地修改
- 企业微信版本需要额外配置 Nginx 反向代理和 HTTPS 证书

---

## 执行指令

**你必须按照以下步骤执行：**

1. **检查本地修改：**
   ```bash
   cd /e/TsangKinWah/Projects/dingtalk-ai-bot && git status
   ```

2. **如果有未提交的修改，询问用户是否提交**（使用 AskUserQuestion）

3. **推送代码到 GitHub：**
   ```bash
   cd /e/TsangKinWah/Projects/dingtalk-ai-bot && git push origin master
   ```

4. **部署到服务器：**

   根据服务类型选择对应的命令：

   - **gemini（默认）：**
     ```bash
     ssh tencent_cloud_server "cd /opt/dingtalk-ai-bot && git pull origin master && docker-compose up -d --build"
     ```

   - **openclaw：**
     ```bash
     ssh tencent_cloud_server "cd /opt/dingtalk-ai-bot && git pull origin master && docker-compose -f docker-compose.openclaw.yml up -d --build"
     ```

   - **wecom：**
     ```bash
     ssh tencent_cloud_server "cd /opt/dingtalk-ai-bot && git pull origin master && docker-compose -f docker-compose.wecom.yml up -d --build"
     ```

5. **查看服务状态：**
   ```bash
   ssh tencent_cloud_server "docker ps --filter name=dingtalk"
   ```

6. **显示最近日志：**

   根据服务类型选择容器名：

   - gemini: `dingtalk-ai-bot-gemini`
   - openclaw: `dingtalk-ai-bot-openclaw`
   - wecom: `dingtalk-ai-bot-wecom`

   ```bash
   ssh tencent_cloud_server "docker logs --tail 30 <容器名>"
   ```

7. **输出部署结果：**

   使用以下格式汇报：
   ```
   ✅ 部署完成 - <服务类型>

   📦 代码版本：<git commit hash>
   🚀 服务状态：<运行中/已停止>
   📝 最新日志：
   <显示关键日志行>
   ```

## 错误处理

- 如果 git push 失败，检查是否有冲突并提示用户
- 如果 SSH 连接失败，提示检查网络和 SSH 配置
- 如果 Docker 构建失败，显示完整错误信息
- 如果环境文件缺失，提示用户需要创建配置文件

## 示例

```
用户: /deploy
你: [执行 gemini 版本部署...]

用户: /deploy wecom
你: [执行企业微信版本部署...]
```
