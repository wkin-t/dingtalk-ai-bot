# SG-Webhook - 腾讯云安全组动态开门服务

一个轻量级的 Webhook 服务，用于根据客户端 IP 自动更新腾讯云安全组规则。

## 功能

- 接收 HTTP 请求，获取客户端真实 IP
- 自动在指定安全组中添加/更新入站规则
- 支持设备标识，每个设备独立管理规则
- 自动清理旧的 IP 规则

## 快速开始

### 1. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env 填写你的配置
```

### 2. 启动服务

```bash
docker-compose up -d --build
```

### 3. 配置反向代理 (推荐)

使用 Nginx 反向代理并配置 HTTPS：

```nginx
location /open-door {
    proxy_pass http://127.0.0.1:35555;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
}
```

## API

### GET /open-door

更新安全组规则，放行请求者 IP。

**参数：**
- `key` (必填): 访问密钥
- `device` (可选): 设备标识，默认为 "Unknown-Device"

**示例：**
```bash
curl "https://your-domain.com/open-door?key=your_token&device=my-laptop"
```

**响应：**
- 200: 成功更新或 IP 未变化
- 403: 密钥错误
- 500: API 调用失败

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `TENCENT_SECRET_ID` | 腾讯云 SecretId | - |
| `TENCENT_SECRET_KEY` | 腾讯云 SecretKey | - |
| `TENCENT_REGION` | 腾讯云区域 | ap-guangzhou |
| `SECURITY_GROUP_ID` | 安全组 ID | - |
| `TARGET_PORT` | 放行端口 | ALL |
| `ACCESS_TOKEN` | 访问密钥 | - |

## 本地开发

```bash
pip install -r requirements.txt
python webhook_sg.py
```

## 注意事项

- 生产环境请配置 HTTPS
- ACCESS_TOKEN 请使用强密码
- 建议配合 Nginx 使用，正确传递 `X-Real-IP` 头
