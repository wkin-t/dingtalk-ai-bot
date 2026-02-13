---
name: deploy-pipeline
description: 自愈式部署流水线 — 测试、构建、部署、健康检查、诊断修复、回滚。适用于 Docker + SSH 部署
disable-model-invocation: true
user-invocable: true
argument-hint: "[服务名或 deploy 目标]"
allowed-tools:
  - Bash
  - AskUserQuestion
  - Read
---

# 自愈式部署流水线

对 `$ARGUMENTS` 指定的服务执行完整部署循环。

## Step 1: 本地验证
1. 运行测试（任何失败立即中止）
2. 检查 `.env` / 配置文件完整性（禁止提交敏感文件）

建议命令：
```bash
cd /e/TsangKinWah/Projects/dingtalk-ai-bot
pytest -q
```

## Step 2: 构建
1. `docker-compose build` 构建镜像
2. 本地启动验证（日志检查编码错误/缺依赖/import 错误）
3. 构建失败则停止，不进入部署

建议命令：
```bash
docker-compose build
docker-compose up -d
docker logs --tail 100 gemini-app
```

## Step 3: 部署到服务器
1. 列出 `~/.ssh/config` host alias，确认使用 `tencent_cloud_server`
2. SSH 到服务器，拉代码并 `docker-compose up -d --build`
3. 等待容器启动并检查前 50 行日志

部署目标映射：
- `gemini`: `docker-compose.yml`
- `openclaw`: `docker-compose.openclaw.yml`
- `wecom`: `docker-compose.wecom.yml`
- 空参数或 `all`: 同时部署 gemini + openclaw

## Step 4: 健康检查
对关键 API 端点逐项检查：
1. `curl` 返回 HTTP 200
2. 响应为有效 UTF-8
3. 关键功能路径可用

## Step 5: 故障自愈（失败时）
1. 抓取完整 `docker logs`
2. 诊断根因（编码/依赖/配置）
3. 修复后重新部署
4. 最多重试 2 次；第 3 次失败进入回滚

## Step 6: 回滚
1. 回滚到上一个正常镜像
2. 验证服务恢复
3. 输出失败原因，停止自动尝试

## 输出要求
将每一步和实际命令输出写入 `deployment-log.md`。

> 禁止“假设成功”，每一步必须有可验证输出。
