# Sol Low 取消 Contract P2 修复记录

## 结论

Sol 以最低思考深度 `low` 复核时发现的取消路径 P2 已修复。该修复仅涉及
Responses 流的 thinking cleanup，不改变 GPT/Claude 的 store 语义、正文后迟到
reasoning 处理、terminal failure cleanup 或已有错误 contract。

## 修复内容

- `_events_with_thinking_cleanup()` 为 `CancelledError` 增加独立 cleanup sentinel。
- thinking 已启动时，取消路径先向消费层发出一次 `{"thinking_end": True}`，再重新
  抛出原始 `CancelledError`。
- thinking 未启动时直接传播取消，不伪造 `thinking_end`。
- 取消路径没有进入普通异常的 error chunk 转换逻辑；成功路径的 response ID 写入仍
  只受 `_supports_store` 控制。

## 回归证据

新增真实 `asyncio.Task.cancel()` 测试覆盖：

- thinking 已启动：`thinking_end` 恰好一次、`CancelledError` 传播、没有 error chunk、
  没有 response ID 读取或写入。
- thinking 未启动：`CancelledError` 传播且没有伪造 `thinking_end` 或 error chunk。

## 本地验证

- 取消专项：`2 passed`，退出码 `0`。
- targeted（openai/search-icon/DingTalk consumer）：`90 passed`，退出码 `0`。
- `python -m compileall -q app main.py`：退出码 `0`。
- 全量 pytest：`539 passed, 1 warning`，退出码 `0`。
- Trellis validate：退出码 `0`。
- `git -c core.whitespace=cr-at-eol diff --check`：退出码 `0`。
- 行尾：`app/openai_client.py` 纯 LF；`tests/test_openai_client.py` 与
  `tests/test_dingtalk_bot.py` 纯 CRLF。

未部署、未提交、未推送；`.pytest-basetemp/` 未触碰。
