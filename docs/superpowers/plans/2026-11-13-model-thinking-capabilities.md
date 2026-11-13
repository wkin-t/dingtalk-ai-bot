# 按模型能力选择独立思考深度

目标：模型别名与思考字段独立。Hydrogen 支持 low/medium/high/xhigh；其余 Gemini 4 支持 low/medium/high。当前中转 Claude 5 与 GPT 5/6 支持 max，具体别名可用显式配置覆盖；原生 OpenRouter 不自动继承中转能力。

用户追加最低档希望完全不思考。官方明确 GPT-6 Luna / GPT-6 Sol / GPT-6.1 Terra 支持 none；GPT-6.1 Sol、Gemini 4、Claude Sonnet / Opus 5.5 不支持完全关闭。只有明确支持的型号才下发 none；minimal 不等于关闭，作为旧输入映射至最低支持档。

## 实现边界

1. `app/ai/thinking_policy.py` 提供纯能力查询、规范化与路由提示生成。识别模型家族而非别名末尾的档位；高级请求降到模型支持且不高于请求的最高档，不回落 low。非法值默认 low。Gemini 4 minimal 映射 low。
2. 配置 `THINKING_LEVEL_OVERRIDES` 为 JSON 对象，键为完整模型名或 `backend:model`，值为有序合法档位列表；启动时拒绝非法结构，不打印配置正文。配置优先于内置能力。
3. 路由 prompt 显示每个 slot 的实际模型与支持档位，解析时先确定最终 slot（含图片升级），再校验深度。客户端发送前再校验；Gemini fallback 更换模型必须重新校验。
4. 钉钉主入口在卡片创建前规范化，状态栏与实际下发一致；backend 作为直接调用保护层。Deprecated 路径不做大规模重构。
5. 保持已有搜索、缓存、采样、会话上下文和模型分工；low 后缀不改变实际 thinking 字段。现有账号额度不足不重复请求、不以真实费用探测矩阵。

## 验证和发布

- 先负测能力矩阵、路由返回 xhigh/max、SDK 请求独立字段、图片升级及 fallback 重校验。
- 运行相关测试、compileall、全套测试和独立审查；源码经哈希校验传输，备份源文件与运行镜像，构建完成后逐个 recreate 三容器。
- 生产离线请求捕获校验真实运行配置、路由和 SDK payload；健康检查不替代真实上游验收，额度不足明确报告。
- README/维护文档同步新能力和配置覆盖规则；只提交任务相关文件，保留无关本地配置。

## 设计挑战与处置

- 图片升级必须先确定实际 slot 再校验深度；Gemini 已程序化执行 lite→fast，且独立使用 MODEL_LITE，不能把 MODEL_ROUTER 的兼容别名当作简单任务模型。
- Keyword fallback 明确保留 pro/lite/fast slot，防止相同别名或预分析异常后选错 fallback override。
- 原生 OpenRouter 的服务端 fallback 共享一个请求字段，取所有候选能力交集；无共同档位则拒绝，不把主模型的扩展档位盲传给备用模型。
- 深度传输被 capability flag 关闭时状态栏显示 default；此次生产启用 GPT lite/fast 与 Claude lite 的 supports_reasoning，让独立字段实际下发，其他参数保持一致。
- 路由分析自己的最低深度按 MODEL_ROUTER 决定，与 lite 的 capability flag 分离；OPENAI_ROUTER_SUPPORTS_REASONING 可显式覆盖推断，未知模型默认不擅自加字段。
- 固定 SDK 的 Python enum 尚未列出 xhigh，会告警但保留扩展值。真实 HTTP MockTransport 验证最终 Gemini thinkingConfig.thinking_level=xhigh/high 以及 Responses reasoning.effort=none/max；不以“Python 对象里有值”替代序列化证明，不以离线证明冒充真实上游执行。
- Gemini fallback 重新构建 ThinkingConfig，usage 报实际深度，最终卡片取 usage 中深度；关闭摘要回传不等于关闭推理。

## 本地验收

- 全套 969 passed，compileall、diffcheck 通过。
- 5 条警告：3 条是锁定 google-genai 对 xhigh 扩展枚举的提示，HTTP 序列化合同证明字段保留；2 条是既有 OpenAI SDK AsyncStream 关闭警告。
- 能力矩阵、合法覆盖、none/max SDK HTTP JSON、独立路由模型最低深度、图片升级、fallback 重校验与状态显示边界均有回归。
- 独立边界复审 approve。显式运维 override 可以扩展内置模型能力，这是受信配置边界，不接受模型生成的覆盖。
- 2026-11-13 09:14–09:15 已依次 recreate Claude / Gemini / GPT。三个 HTTP 根端点 200，RestartCount=0，八个相关生产源码与 README 的运行哈希全部匹配本地。
- 容器内使用 httpx.MockTransport 捕获实际 SDK HTTP：Gemini lite Hydrogen xhigh→xhigh，fast Argon xhigh→high，pro Carbon max→high；GPT lite Luna none→none，fast Terra 和 pro Sol max→max；Claude lite Sonnet none→low，fast Sonnet/pro Opus max→max。路由自己的最浅字段分别为 GPT none、Claude low；没有发出外部模型请求。
- 更新 GPT 的 OPENAI_LITE_SUPPORTS_REASONING / OPENAI_FLASH_SUPPORTS_REASONING 与 Claude 的 OPENAI_LITE_SUPPORTS_REASONING 为 true，其他环境配置经解析比对保持一致。
- 回滚：deployment-backups/20261113-thinking-capabilities/ 备份源码和修改前环境文件，运行镜像保留各 backend-before-thinking-20261113；新运行镜像为 thinking-capabilities-20261113 的三份后端标签。旧环境备份保留已切换的 Gemini Hydrogen 模型分工。
- 验证边界：未以额度不足的真实上游做 none/xhigh/max 执行验收；离线请求捕获证明字段已序列化，不证明中转站内部执行或实际节省的 token 数。
