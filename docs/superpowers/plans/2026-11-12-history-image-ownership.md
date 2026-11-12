# 历史图片归属与图文顺序修复

目标：历史图片随所属消息传递，当前 user 只附本次上传和显式引用；合并同角色消息时保留 text/image 原始顺序。保持 dws 只读门禁、可见历史 N 条、clear 起点、引用邻居和媒体总预算、卡片内 @。

## 方案与边界

- 不采用仅提示词补丁：无法消除当前消息持有旧图的结构问题。
- `dws_context` 将历史图片字节挂在对应 `merged_history` 条目的 `images` 中。`context_images` 保留为预算/诊断聚合，钉钉主链路不再把它附到当前消息。
- 显式引用邻居若不在可见历史窗口，用新增 `context_image_messages` 保留其消息来源与图片；作为明确的引用邻居上下文在当前消息之前组装，不扩展普通历史窗口。
- `history_format` 将图片编码为 SDK 多模态块，文本标明历史身份/时间，避免被理解为当前上传。机器人历史图片使用明确的历史机器人来源 user 上下文，以兼容 Responses 的 input_image 角色约束；保留原历史正文角色。
- `dingtalk_bot` 仅将 quoted_images 与本次上传图片放在当前消息；路由 has_images 描述全部视觉上下文，并附历史/引用/当前数量来源；只有本次上传可进入既有改图分支。
- `message_transform` 按序连接多模态块，仅合并相邻 text；禁止把全部文字和全部图片分桶重新排序。
- 不改历史图取舍策略，不删除旧图，不新增模型工具或跨群读取。

## 执行

1. 独立审查上述方案，针对窗口/引用归属、provider 角色约束、图文顺序提出失败模式。
2. 先写失败测试：历史图不在当前输入、历史消息可见原图、混合历史/当前图片、同角色图文交错、引用邻居、同名 @ 既有回归。
3. 按边界实现，保留测试和上一轮未提交 @ 修复。
4. 项目锁定 google-genai==2.34.0 的隔离路径下运行相关测试、compileall、全量测试；独立审查并处置。
5. 本地完成后上传最小代码集合，校验 hash，备份服务器文件与三个旧镜像，构建完再逐个 targeted recreate；不读 env、不 remove-orphans、不 push。
6. Clawbot 群验证真实生产上下文组装与模型调用：旧图历史后接无关纯文字，检查传给客户端的图片位置、模型响应和唯一卡片；结构修复是确定验收，单次模型回答只作为描述性抽样。
7. 完成复查、记录验证限制，达成后 complete goal。

## 用户追加范围：撤回内容不送模型

- 在消息清洗、引用解析、合并转发子消息入口过滤明确撤回标志和精确撤回占位；不能因普通正文讨论“撤回”而误删。
- dws-only 每轮以当前读取结果为准。Clawbot 实测自发测试消息撤回后 list 不再返回，mget 返回 not_returned；引用不能从 Stream 中保留的旧正文绕过此状态。
- 显式引用若目标不在当前可见读取中，验证 mget；目标不可读/撤回则移除其正文和媒体，不恢复本地旧库。
- 当前触发消息若有明确撤回证据，本轮停止送模型；消息暂未同步或无匹配本身不作为已撤回的证明。
- 新增纯文本、历史图片、显式引用、合并转发子消息撤回回归，并实测撤回后模型输入不含测试标记。

## 设计挑战处置

- assistant/model 图片角色：保留原历史机器人正文角色，附独立 role=user 的历史视觉上下文；用 Responses/Gemini 转换测试验收。
- 引用邻居：独立 context_image_messages 保留时间、发送者、message_id 和图片；不丢失，也不放入当前上传。
- same-role 合并：允许同角色合并，要求最终块顺序为历史文本→历史图片→当前文本，历史图紧前来源说明；不新增角色分隔协议。
- 视觉路由与改图：has_images 仍包含历史；路由输入增加来源数量，本次无上传时显式清除 need_image_edit，兼容视觉选择且不误改图。
- 预算/clear/窗口仍由 dws 层统一过滤，新增图片字段与对应 record 同进同出。

## 执行状态（最终部署前）

- 图片修复版本全量测试 829 passed；最终撤回改动后必须重新全量验证。
- 图片独立审查 High 已关闭；生图增强的多模态历史另补 text-only 提取与回归，相关 handler 45 passed。
- 当前消息及缓冲中的每条消息都需要 dws 可读证据，异构 ID 可按既有发送者/时间/正文匹配；暂不可读停止，不确定称为撤回。
- 引用采用正向可读来源：近期 dws 命中可读记录，或 mget 返回目标；未验证不恢复回调正文和资源。
- 远端 `/opt/1panel/docker/compose/dingtalk-ai-bot/deployment-backups/20261112-context-ownership/` 已备份本轮修改前源码；三运行镜像分别保留 `dingtalk-ai-bot:{anthropic,gemini,openai}-before-context-20261112`。
- 2026-11-12 16:10–16:12 三个新镜像均已构建并逐个定向 recreate。三个 HTTP 检查通过，RestartCount=0；每个容器五个生产源文件与本地 SHA256 全部一致。
- Clawbot 实测撤回标记 `撤回过滤技术验收-28c98e6f`；消息撤回后 list 不返回，mget 为 not_returned。部署后回放完整旧引用，验证无正文/媒体回流。
- 真实验收脚本已在当前工具会话的 `context_live_probe` 存储：只在 Clawbot 发送一张 OLD 测试图、下一条无图换话题，回放真实 handler、router、模型与卡片；检查模型入口顺序与当前上传数、dws 读回唯一新 Claude 消息。入口为验收回放，不能声称真实 Stream 入站验收。

## 上下文部分验收结果

- 最终全量 846 passed，2 条既有 OpenAI SDK AsyncStream 关闭警告；compileall、diffcheck 通过。独立最终复审 approve，上下文单文件 88 passed，无 P0/P1。
- Clawbot 标记 `上下文技术验收-fd0f0fa3`。先发送 OLD 图片，再发纯文字换话题；最初验收脚本对图片占位文本的识别过严，修正脚本后复用原消息，未重复发送输入。
- 完整旧撤回引用已移除；真实模型入口的来源文字、旧图片、当前文字索引为 10、11、12，当前上传数 0。
- 真实模型答复：`下班啦，明天见，别加太久班！👋`。一次卡片创建、两次正文更新、finalized=True，dws 读回仅一条新增 Claude 消息。
- `context_live_resume_probe` 保存了不重复发送输入的验收回放脚本。

## 用户追加范围：触发消息上的处理表情

- 开始思考/流式输出时在对应触发用户消息上添加“处理中”表情回应；完成后取消。
- 异常、取消、超时也须清理；表情接口失败不得阻断正文回复，也不能留下无法回收的重试。
- 先核实钉钉原生机器人身份能力、消息 ID 和添加/移除 API，再接入 handler 生命周期；不以个人身份冒充机器人回应。
- 接口能力实测通过：机器人原生 `/v1.0/robot/emotion/reply` 与 `/recall` 均 HTTP 200、success=true；Clawbot 原用户消息的 reaction 从空列表变为 `🤔思考中 / replyUsers=[Claude小助手]`，撤回后恢复空列表。
- 采用原生 HTTP 适配，不升级 SDK；dws 的个人 reaction 命令不作为机器人生产路径。官方字段参照 https://github.com/DingTalk-Real-AI/dingtalk-openclaw-connector/blob/main/src/utils/utils-legacy.ts 。
- 最小接入：`DingTalkCardHelper.set_processing_reaction` 负责 API；平台层 `app/dingtalk_reactions.py` 负责生命周期；`handle_ai_stream` 同签名薄 wrapper 包住原核心实现，不重排已验证的上下文与流式逻辑。
- 添加和撤回均为有界调用；取消时先等已开始的添加结束，再撤销，避免晚到添加留下残留。未知添加结果也撤销本机器人自己的同一精确表情 payload。
- 当前状态：已实现、独立复审通过并部署至三个机器人；新增范围已完成。

## 第二阶段验收（处理表情，历史记录）

- 全量 `869 passed`，2 条既有 OpenAI SDK AsyncStream 关闭警告；compileall、diffcheck 通过。处理表情相关 74 项通过，独立复审 approve，无 P0/P1。
- 第二阶段构建后逐个 recreate 三个机器人，三个 HTTP 检查通过；每个容器七个相关生产源文件与本地 SHA256 全部一致。
- 第二阶段前的上下文修复版本另存于 `deployment-backups/20261112-processing-reaction/`，三个运行镜像另标记 `{anthropic,gemini,openai}-before-reaction-20261112`，保留第一阶段回滚点。
- Clawbot 真实 @ 入站验收标记 `处理表情自动验收-9e621a03`。观察器只发送一次用户 @ 消息并读状态，没有回放 handler、没有手工添加/移除表情。
- 原消息 `🤔思考中` 实际出现，回应者为 Claude小助手；原生回复卡片完成 `flowStatus=3` 后，表情列表恢复为空；新增 Claude 回复数量为1。
- 真实模型回复正确解释 API 与数据库事务的幂等性，没有重复消息；上下文部分此前的模型入口回放仍证明旧图属于历史、当前上传数0，完整撤回引用不回流。
- 完成范围：卡片内提及保持既定方案、历史图片归属与图文顺序、撤回消息/媒体/引用过滤、处理中原生机器人表情与自动清理、三机器人构建部署及 Clawbot 验收。


## 后续追加修复与回归（2026-11-12）

- 前述 829 / 846 / 869 项为各阶段历史结果；合并同群串行、移除按钮、提及去重和状态栏收尾修复后，当前全套为 `885 passed`，保留 2 条既有 OpenAI SDK AsyncStream 关闭警告；compileall 通过。
- 四个清空 / 重试 / 总结 / 翻译按钮从初始和最终卡片移除，失败兜底不再追加同名链接；文字指令保留。
- 移除程序固定的提问者 @ 头部；合并模型开头同一成员的重复编号标记和普通 @ 昵称，同一成员后续结构化提及仅保留姓名，其他成员提及不受影响。
- 完成前先停止并等待打字动画，再写入正式状态栏；成功后不清空正式状态，失败时状态进入正文；取消动画等待也会尝试清理未完成字段并继续传播取消。
- 新增 6 项提及回归及 3 项状态栏正常 / 失败 / 取消回归；相关 handler 测试 `62 passed`，源码独立复审最终 `approve`。
- README 与 CLAUDE 已注明当前 Claude 四档配置：router / lite / fast 使用 `claude-sonnet-5-5-low`，仅 pro 使用 `claude-opus-5-5-low`；应用思考深度策略不变，较早的模型验证保留为历史记录。
- 用户随后明确授权完成后 commit 并 push，覆盖本计划早期“不 push”的执行边界；环境文件、凭据和无关本地工具配置不纳入提交。

- 最终镜像构建后于 17:51–17:52 逐个 recreate Claude / Gemini / GPT，三个根端点 HTTP 200、RestartCount=0；各容器七个相关生产源码及 README 的 SHA256 与本地一致。回滚源文件位于 `deployment-backups/20261112-final-card-cleanup/`，旧运行镜像保留 `{anthropic,gemini,openai}-before-final-card-20261112`。
- Clawbot 最终真实 @ 入站验收标记 `提及去重验收-ad5e8e0e`：只发一次测试请求、新增 Claude 卡片1张、完成状态 `flowStatus=3`；同一成员姓名只出现1次，无固定招呼头、无快捷按钮链接，正式模型状态栏保留，原消息处理表情出现后已取消。模型遵守了单次提及提示；代码强制合并重复输出另由回归测试证明。
