# Implementation Plan

## 1. Freeze evidence

- [x] 将脱敏的 Claude reasoning summary 事件、无 reasoning 事件、标准搜索事件和 Grounding 文本 delta 写成测试 fixture。
- [x] 记录 GPT `summary=auto` 生产只读探针结果到 research。
- [x] 确认 fixture 不含 prompt、完整回复、token、用户标识或凭据。

## 2. Write failing regressions

- [x] 测试 `response.reasoning_summary_text.delta` 当前未产生 thinking chunks。
- [x] 测试 Grounding source-link 跨 delta 时当前未产生 `executed=True`。
- [x] 测试只有 `native_enabled` 时不点亮。
- [x] 测试普通 URL/Sources 文案、裸前缀和短伪造不误报。
- [x] 测试 Claude Responses `low` 下发 `effort=low`，GPT/Chat 参数保持原状。

## 3. Implement event normalization

- [x] 在 `app/openai_client.py` 中集中定义 Responses reasoning event types。
- [x] 同时消费 reasoning text 与 reasoning summary delta。
- [x] 保证 thinking start/end 在空 delta、正文切换、迟到 reasoning、terminal failure、流结束和真实 Task.cancel() 时顺序正确。
- [x] 增加有界 Grounding source-link rolling detector。
- [x] 标准事件与 Grounding 信号共用单次 `executed=True` 门闩。
- [x] 仅为 Claude Responses 将 `low` 覆盖为真实 low effort，保留共享 minimal/low=none。
- [x] 保持 GPT 与 Gemini Chat 请求参数不变；当前 `summary=auto` 探针无 summary 事件。

## 4. Cross-layer tests

- [x] 验证 search chunk 在 bot 层合并而非覆盖。
- [x] 验证 `should_show_search_icon()` 只认 executed/fallback injected。
- [x] 验证 thinking summary 进入 DingTalk `full_thinking` 并形成最终状态摘要。
- [x] 验证无 reasoning event 时 status 不生成虚假摘要。

## 5. Quality gates

```powershell
pytest -q tests/test_openai_client.py tests/test_search_icon.py
python -m compileall -q app main.py
pytest -q tests
python .\.trellis\scripts\task.py validate 07-30-responses-search-thinking-observability
git diff --check
```

## 6. Adversarial review

- [x] 使用 Sol 对搜索信号伪造、chunk 边界、重复事件、thinking 状态机、provider 回归和日志泄露做对抗性检查。
- [x] 修复 findings 后重新执行 targeted 与 full-suite。

修复后本地证据（2026-07-31）：

- 取消专项：`2 passed`，退出码 `0`。
- 本轮 targeted（openai/search-icon/DingTalk consumer）：`90 passed`，退出码 `0`。
- `compileall`：退出码 `0`。
- 本轮 full pytest：`539 passed, 1 warning`，退出码 `0`。
- Trellis validate：退出码 `0`。
- CRLF-aware `diff --check`：退出码 `0`。
- `app/openai_client.py` 纯 LF；`tests/test_openai_client.py` 纯 CRLF。
- `tests/test_dingtalk_bot.py` 保持纯 CRLF；本轮没有新增 RuntimeWarning，剩余 warning
  为既有 pytest-asyncio 配置提示与 Google SDK DeprecationWarning。
- 最终检查额外修复无前缀 Claude 被错误下发 `store=True` /
  `previous_response_id` 的跨层分类不一致，并补 SDK 类型可选兼容及
  Grounding ASCII/Unicode token 边界测试。
- 本轮根据 Sol 最终复核补齐 Claude 成功事件不写 response ID、正文后迟到
  reasoning 忽略、terminal failure/流异常先闭合 thinking，以及 DingTalk consumer
  `full_thinking`/最终状态摘要回归测试。
- 本轮根据 Sol low 最终复核修复取消 contract P2：真实 `asyncio.Task.cancel()`
  在 thinking 已启动时恰好补发一次 `thinking_end` 后继续传播 `CancelledError`，
  不生成普通 error chunk、不写 response ID；未启动 thinking 时不补结束事件。
- 未执行 deployment / production canary，下面项目保持未勾选。

## 7. Deployment

- [x] 展示本地/远端当前值与目标值；备份三个生产 env 文件。
- [x] 提交并推送经确认的代码变更。
- [x] 服务器 fast-forward 后分别执行三个 Compose 重建：

```bash
docker compose -f docker-compose.openrouter.yml up -d --build
```

- [x] 验证 server commit、容器状态、RestartCount、端口和镜像代码哈希。

部署证据（2026-07-31）：

- 服务器项目 fast-forward 到 `cc047d2`。
- `.env`、`.env.openai`、`.env.openrouter` 已备份到
  `.deploy-backup-20260731-143833/`；原文件与备份 SHA-256 一致，部署后仍一致。
- 三个 Compose 均以 `up -d --build` 重建；三个容器均 `running`、`RestartCount=0`，无配置的 Docker healthcheck。
- 35000、35001、35002 根端点均返回 HTTP 200，Gunicorn 均监听对应端口。
- 服务器工作区与三个容器内的 `app/openai_client.py` SHA-256 均为
  `27ff6848ee76e2fd38de435ed96fabcbd2a718a6dba03e88fec0e5cf07ecb14d`。
- 服务器既存未跟踪目录 `data-openai/`、`data-openrouter/` 未修改；未执行生产模型 canary。

## 8. Production canary

- [ ] 普通闲聊：无 Grounding、无 `🌐`。
- [ ] 实时新闻：真实 Grounding、只出现一次搜索执行探针、footer 显示 `🌐`。
- [ ] Claude medium/high 推理：真实 reasoning summary event、`thinking > 0`、最终状态显示摘要。
- [ ] low 请求：确认真实下发 low reasoning，并记录延迟/费用影响。
- [ ] 明确区分自动化验证、用户钉钉见证和仍未验证边界。

## Rollback points

1. 单元测试阶段：撤销本任务代码，不触及生产。
2. canary 前：保持当前生产提交。
3. low 成本异常：仅移除 Claude Responses 的 low 专属覆盖。
4. 运行时异常：恢复部署前提交并 recreate 35002。
