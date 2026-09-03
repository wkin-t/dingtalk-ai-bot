# Sol 最终复核 P2 修复证据

## 修复范围

本轮只处理 `research/sol-final-review.md` 中的 P2：

1. Claude Responses 的成功 `response.created` / `response.completed` 事件不再写入
   `responses_state`；同时保留 GPT 的 store 续接和成功写入。
2. Responses thinking 状态机增加正文已开始状态。正文后的迟到 reasoning 不再重新
   开启 thinking；`response.failed` 和底层 async iterator 异常在错误返回/重新抛出前
   先闭合已启动的 thinking。
3. 通过实际 `GeminiBotHandler.handle_ai_stream()` 补齐两条 consumer 回归：summary
   chunk 进入最终 DingTalk `statusText` 摘要；没有 reasoning chunk 时不生成浅灰色思考
   摘要。

## 测试证据

- Claude 模型矩阵覆盖 `anthropic/claude-haiku-4.5` 与无前缀
  `claude-opus-4-6-thinking`，事件流包含真实形态的 created/text/completed 序列；两者
  均断言 `store=False`、不读旧 ID、不写新 ID。
- GPT 成功 created/text/completed 序列仍断言写入 response ID。
- Responses 序列覆盖：正文后 reasoning、`response.failed`、async iterator 抛异常；
  均断言 thinking 先结束，异常路径仍由外层产出原有 error contract。
- DingTalk consumer 测试不复制 provider 归一化实现，而是通过真实 handler 收尾逻辑
  检查最终卡片全量更新的 `statusText`。

## 边界

本地 consumer 测试验证卡片状态构造和统一 chunk contract，不替代真实钉钉 UI 见证；
生产 canary、部署、提交和推送仍属于后续门禁。

## 本轮验证

- targeted（`test_openai_client.py`、`test_search_icon.py`、`test_dingtalk_bot.py`）：
  `88 passed`，退出码 `0`。
- `python -m compileall -q app main.py`（隔离 `PYTHONPYCACHEPREFIX`）：退出码 `0`。
- 全量 pytest（隔离 basetemp）：`537 passed, 1 warning`，退出码 `0`。
- Trellis validate：退出码 `0`。
- `git -c core.whitespace=cr-at-eol diff --check`：退出码 `0`。
- 行尾：`app/openai_client.py` 纯 LF；两个测试文件均纯 CRLF。
- `.pytest-basetemp/` 未读、未改、未删、未纳入变更。
