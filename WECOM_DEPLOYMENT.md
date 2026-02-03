# 企业微信 AI 机器人部署指南

> **重要提示**: 企业微信群机器人**仅支持内部群**（成员全部为同一企业的企业微信用户）,不支持外部群/客户群。

## 📋 前提条件

### 服务器要求
- ✅ 公网可访问的 HTTPS 域名 (企业微信要求)
- ✅ 已部署钉钉 AI 机器人 (可选,如仅用企业微信可跳过)
- ✅ 腾讯云服务器 + 1Panel 管理面板

### 企业微信配置
1. 登录 [企业微信管理后台](https://work.weixin.qq.com/)
2. **应用管理** -> **自建** -> **创建应用**
3. 记录以下信息:
   - **企业ID** (CorpID)
   - **AgentId**
   - **Secret**

---

## 🚀 部署步骤

### Step 1: 更新依赖

安装企业微信消息加解密依赖:

```bash
pip install pycryptodome
```

或使用 requirements.txt:

```bash
pip install -r requirements.txt
```

### Step 2: 配置环境变量

编辑 `.env` 文件,添加企业微信配置:

```env
# 企业微信配置
WECOM_CORP_ID=ww1234567890abcdef
WECOM_AGENT_ID=1000002
WECOM_SECRET=your_wecom_secret

# 回调配置 (自定义)
WECOM_TOKEN=your_custom_token_min_3_chars
WECOM_ENCODING_AES_KEY=your_43_char_base64_encoding_aes_key

# 平台选择
PLATFORM=both  # dingtalk | wecom | both
```

**生成 EncodingAESKey 的方法**:

```python
import base64
import os

# 生成 32 字节随机密钥
aes_key = os.urandom(32)
# Base64 编码后取前 43 位
encoding_aes_key = base64.b64encode(aes_key).decode('utf-8')[:43]
print(f"WECOM_ENCODING_AES_KEY={encoding_aes_key}")
```

### Step 3: 配置 Nginx 反向代理

在 1Panel 中添加反向代理规则:

```nginx
location /api/wecom/callback {
    proxy_pass http://127.0.0.1:35000;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

**HTTPS 证书**: 使用 1Panel 自动申请 Let's Encrypt 证书或手动配置。

### Step 4: 重启服务

```bash
# Docker 部署
docker-compose down
docker-compose up -d --build

# 或者本地运行
python main.py
```

### Step 5: 企业微信后台配置回调 URL

1. 进入 **企业微信管理后台** -> **应用管理** -> 选择你的应用
2. **接收消息** -> **设置API接收**
3. 填入以下信息:

| 字段 | 值 |
|------|------|
| **URL** | `https://your-domain.com/api/wecom/callback` |
| **Token** | 你的 `WECOM_TOKEN` |
| **EncodingAESKey** | 你的 `WECOM_ENCODING_AES_KEY` |

4. 点击 **保存** 并验证
   - 如果验证成功,说明回调配置正确
   - 如果失败,检查日志: `docker logs -f gemini-app`

### Step 6: 测试机器人

1. 在企业微信中创建一个**内部群**
2. 在群中发送消息 `@你的机器人 你好`
3. 机器人应该回复 AI 生成的内容

---

## 🔧 故障排查

### 1. URL 验证失败

**症状**: 企业微信后台提示 "URL 验证失败"

**排查步骤**:

```bash
# 检查服务是否运行
curl -I https://your-domain.com/api/wecom/callback

# 查看服务日志
docker logs -f gemini-app

# 测试本地回调
curl "http://localhost:35000/api/wecom/callback?msg_signature=xxx&timestamp=xxx&nonce=xxx&echostr=xxx"
```

**常见原因**:
- HTTPS 证书未配置或过期
- Nginx 反向代理配置错误
- `WECOM_TOKEN` 或 `WECOM_ENCODING_AES_KEY` 填写错误
- 防火墙未开放 80/443 端口

### 2. 收不到消息

**症状**: URL 验证成功,但机器人不回复

**排查步骤**:

```bash
# 查看日志
docker logs -f gemini-app | grep "企业微信"

# 检查平台配置
grep PLATFORM .env
```

**常见原因**:
- `PLATFORM` 未设置为 `wecom` 或 `both`
- 企业微信应用未添加到群聊
- 消息加解密失败 (检查 `WECOM_ENCODING_AES_KEY`)

### 3. AI 不响应

**症状**: 收到消息但 AI 无回复

**排查步骤**:

```bash
# 检查 Gemini API Key
grep GEMINI_API_KEY .env

# 检查代理配置 (如在国内)
grep SOCKS_PROXY .env

# 查看 AI 处理日志
docker logs -f gemini-app | grep "AIHandler"
```

---

## 📊 功能对比

| 功能 | 钉钉 | 企业微信 |
|------|------|----------|
| **外部群/客户群** | ✅ 支持 | ❌ 不支持 |
| **消息接收方式** | Stream 长连接 | HTTPS 回调 |
| **流式卡片更新** | ✅ 实时更新 | ❌ 完整回复 |
| **发送频率限制** | 相对宽松 | 20条/分钟/机器人 |
| **部署要求** | 无需公网 IP | 需要 HTTPS 域名 |

---

## 🔐 安全建议

1. **Token 保密**: `WECOM_TOKEN` 和 `WECOM_SECRET` 不要泄露
2. **定期更新证书**: HTTPS 证书到期前续期
3. **限流保护**: 企业微信有 20条/分钟 的频率限制,建议在代码层增加限流
4. **日志审计**: 定期检查 `docker logs` 中的异常请求

---

## 📖 参考文档

- [企业微信开发文档](https://developer.work.weixin.qq.com/document/)
- [接收消息与事件](https://developer.work.weixin.qq.com/document/path/90930)
- [消息加解密](https://developer.work.weixin.qq.com/document/path/90968)

---

## 🆘 常见问题

### Q: 企业微信支持外部群吗?

**A**: 不支持。企业微信群机器人仅支持**内部群**(成员全部为同一企业的企业微信用户)。如需服务外部客户,建议:
- 使用钉钉 (支持外部群)
- 开发 H5 微应用 (通过链接分享)

### Q: 企业微信支持流式更新吗?

**A**: 不支持。企业微信不支持类似钉钉的流式卡片更新。目前实现方案:
1. 发送 "🤔 AI 正在思考中..."
2. 后台完整生成 AI 回复
3. 发送完整的 Markdown 消息

### Q: 能否同时支持钉钉和企业微信?

**A**: 可以。设置 `PLATFORM=both` 即可同时启用两个平台,会话隔离互不干扰。

---

_最后更新: 2026-02-03_
