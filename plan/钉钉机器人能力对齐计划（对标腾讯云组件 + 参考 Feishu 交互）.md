   ## 钉钉机器人能力对齐计划（对标腾讯云组件 + 参考 Feishu 交互）

  ### Summary

  目标是把当前钉钉接入从“文本+图片主流程”扩展为“单聊/群聊统一、语音/文件可处理、可主动推送、可发送图片、带可爱打字状态、支持历史引用”的完整通道能力，并保持 OpenClaw 主导编排。
  本计划按你确认的偏好落地：语音走 OpenClaw Tool、主动推送先群聊后单聊、打字状态用卡片状态栏动画、文件先做二进制透传、历史引用走智能自动模式、群聊继续 @ 触发、单聊默认直回。

  ### 当前基线（已确认）

  1. 已有能力

  - 群聊与单聊都在同一处理器里（conversation_type 区分）。
  - 已支持接收 text/picture/richText，图片可通过 downloadCode 下载。
  - 已有 AI 卡片流式更新能力（创建、stream update、final update）。
  - OpenClaw 模式已支持轻量上下文透传。

  2. 缺口

  - 未处理 audio、file 入站消息类型。
  - 无“主动推送”HTTP 接口与统一发送服务。
  - 无“发送图片”统一能力（原生优先 + 回退）。
  - 无“历史消息引用”逻辑。
  - 打字状态目前是思考文本展示，不是独立“敲键盘状态生命周期”。

  3. 官方约束（已纳入方案）

  - 入站消息类型含 audio/file/picture/richText/text。
  - 单聊场景 responseWebhook 不可用，主动发送必须走 OpenAPI 发送接口。

  ### Public APIs / Interfaces / Types 变更

  1. 新增配置项（.env* + app/config.py）

  - DINGTALK_PUSH_BEARER_TOKEN：主动推送接口 Bearer。
  - DINGTALK_PUSH_IP_ALLOWLIST：逗号分隔 CIDR/IP 白名单。
  - OPENCLAW_TOOLS_URL：Tools Invoke HTTP 地址。
  - OPENCLAW_TOOLS_TOKEN：Tools Invoke Token。
  - OPENCLAW_ASR_TOOL_NAME：语音转写工具名。
  - OPENCLAW_FILE_TOOL_NAME：文件理解工具名。
  - DINGTALK_TYPING_ENABLED：是否开启打字状态动画。
  - DINGTALK_TYPING_FRAMES：动画帧（默认 ⌨️ 正在敲键盘. .. ...）。
  - DINGTALK_TYPING_INTERVAL_MS：动画刷新间隔。
  - DINGTALK_REFERENCE_AUTO_ENABLED：历史引用智能模式开关。
  - DINGTALK_IMAGE_MSG_KEY：原生图片 msgKey（默认 sampleImageMsg，可配置）。
  - DINGTALK_IMAGE_MSG_PARAM_TEMPLATE：图片 msgParam 模板（JSON 字符串，可配置）。

  2. 新增内部类型

  - NormalizedIncomingMessage
    字段：conversation_id conversation_type sender_id sender_nick at_user_ids text attachments raw_message.
  - Attachment
    字段：kind(image|audio|file) download_code filename mime_type size bytes.

  3. 新增模块

  - app/dingtalk_message_parser.py：统一解析 text/picture/richText/audio/file。
  - app/openclaw_tools_client.py：封装 /api/tools/invoke 调用。
  - app/dingtalk_sender.py：统一发送群聊/单聊（text/markdown/image，后续 file）。
  - app/dingtalk_typing.py：打字状态生命周期管理。
  - app/reference.py：历史引用智能触发与引用块拼装。

  4. 新增 HTTP 接口

  - POST /api/dingtalk/push
    鉴权：Authorization: Bearer <token> + 来源 IP 白名单。
    请求体：target_type(group|single) conversation_id user_id(单聊可选) message_type(text|markdown|image) content/url trace_id。
    返回：{ok, message_id, mode, fallback_used, error}。
  - POST /api/dingtalk/push/file（阶段 2 预留）
    先返回 501 planned，并给出预计支持字段定义，避免调用方反复改协议。

  ### 详细实施步骤（可直接执行）

  1. 入站消息统一化

  - 将 GeminiBotHandler.process() 的消息分支替换为 parser 输出。
  - picture/richText 中图片保持现有下载逻辑。
  - 新增 audio：读取 downloadCode 下载二进制，生成 Attachment(kind=audio)。
  - 新增 file：读取 downloadCode 下载二进制，生成 Attachment(kind=file)。
  - 解析失败时返回可观测错误并不中断主线程（Ack OK + 业务日志）。

  2. OpenClaw Tool 接入（语音与文件）

  - 语音：Attachment(kind=audio) 走 OPENCLAW_ASR_TOOL_NAME，产出文本后并入用户输入。
  - 文件：Attachment(kind=file) 走 OPENCLAW_FILE_TOOL_NAME，把工具摘要/结构化结果并入上下文。
  - Tool 失败策略：回退为“已收到附件但处理失败，请稍后重试”，不丢原消息。
  - 工具请求都携带 conversation_id/sender_id/trace_id 用于可观测性。

  3. 单聊/群聊策略固化

  - 群聊：仅 @ 触发回复（保持现状）。
  - 单聊：默认直回（保持现状）。
  - 主动推送阶段 1 仅支持群聊 org_group_send；阶段 1.5 再加单聊 private_chat_send。

  4. 发送图片能力（原生优先）

  - 在 dingtalk_sender 中实现 send_image()。
  - 首选原生图片消息（msg_key/msg_param 通过配置驱动）。
  - 原生失败自动回退 Markdown 图片链接或卡片展示，返回 fallback_used=true。
  - 保证群聊和单聊共享同一发送抽象，底层自动选 org_group_send/private_chat_send。

  5. 打字状态（“敲键盘”）

  - 在创建卡片后立即显示状态栏动画：⌨️ 正在敲键盘...。
  - AI 流式期间按 DINGTALK_TYPING_INTERVAL_MS 更新动画帧。
  - 一旦 is_finalize=true 或错误终止，清空状态栏字段（彻底“删除状态”）。
  - 与已有思考摘要分离：打字状态是短生命周期 UI，思考摘要可配置是否保留。

  6. 历史消息引用（智能自动）

  - 启用 DINGTALK_REFERENCE_AUTO_ENABLED=true 时，使用轻量规则触发器：
    规则词示例：你刚才 上条 前面 那个文件 这张图 继续。
  - 触发后从最近历史中选最相关一条，拼装引用块注入当前请求（不污染永久记忆）。
  - 引用块格式统一：[引用 yyyy-mm-dd HH:MM:SS 昵称] 内容摘要。
  - 无触发词不引用，避免冗长。

  7. 主动推送 API

  - 新增 /api/dingtalk/push，实现 Bearer + IP 双重鉴权。
  - 支持消息类型：text markdown image。
  - 阶段 1：target_type=group。
  - 阶段 1.5：放开 target_type=single。
  - 全链路记录 trace_id、目标会话、发送结果、回退路径。

  8. 文档与运维

  - 更新 DEPLOY.md OPENCLAW_SETUP.md：新增配置与 Push API 调用示例。
  - 新增“单聊/群聊行为矩阵”和“附件类型支持矩阵”。
  - 更新告警建议：Tool 错误率、发送失败率、回退率。

  ### 测试用例与验收场景

  1. 入站解析

  - text 正常。
  - picture 单图/多图。
  - richText 混合文本+图。
  - audio 下载成功/失败。
  - file 下载成功/失败。

  2. Tool 调用

  - 文件理解成功并参与回答。

  - /api/dingtalk/push 群聊版 + 发送图片原生优先/回退。

  3. M3（次周后半）

  - 单聊主动推送 + 智能历史引用 + 文档和监控完善。

  4. M4（预留）

  - /api/dingtalk/push/file 真正落地（当前先 501 planned）。

  ### 假设与默认值（已锁定）

  - 语音转写：OpenClaw Tool 优先。
  - 主动推送：先群聊，后单聊。
  - 打字状态：卡片状态栏动画并在完成后清空。
  - 文件处理：阶段 1 走二进制透传到工具链，不做本地解析。
  - 历史引用：智能自动触发。
  - 会话策略：单聊直回、群聊 @ 触发。
  - 图片发送：原生优先，失败回退。
  - Push 鉴权：Bearer + IP 白名单。

  ### 钉钉接口能力

  | 功能 | 结论 | 关键限制/说明 |
  |---|---|---|
  | 接收图片 | ✅ 支持 | 支持 picture 和 richText 中图片。 |
  | 接收语音 | ⚠️ 有条件支持 | 下载接口文档明确：群聊@机器人、人与人会话里不支持语音；“人与机器人会话”支持。 |
  | 接收文件 | ⚠️ 有条件支持 | 同上，群聊@机器人、人与人会话里不支持文件；“人与机器人会话”支持。 |
  | 发送图片 | ✅ 支持 | 机器人消息类型里有 sampleImageMsg；另外 Markdown 可用 mediaId 显示图。 |
  | 主动推送消息 | ✅ 支持 | Stream FAQ 明确可以主动发；AI 助理文档也有“主动发送模式”（单聊 UnionID / 群聊 OpenConversationID）。 |
  | 回复文件 | ✅（OpenAPI） | 机器人消息类型里有 sampleFile（需 mediaId）；但 Webhook 方式偏简单（文本/Markdown）。 |
  | 历史消息引用 | ❌ 文档未见原生“引用接口” | 需要你在应用层自己实现引用逻辑（从历史中抽取并拼成引用块）。 |
  | “敲键盘/打字中”状态 | ⚠️ 分模式 | Stream FAQ说“原生打字机模式当前不支持”，可用互动卡片更新模拟；AI 助理“分步发送（预备/更新/结束）”可实现类似效果。 |

  你给的两个链接里，第二个（下载文件）是关键证据，里面直接写了语音/文件/视频在不同会话类型下的支持差异。
  第一个页面是 JS 动态页，我用官方开发者百科与 API 镜像交叉核对了同类条款。

  参考链接：

  - 开发者百科：接收消息类型
    https://opensource.dingtalk.com/developerpedia/docs/learn/bot/message/
  - 下载机器人接收消息文件（与你给的链接对应）
    https://open.dingtalk.com/document/development/download-the-file-content-of-the-robot-receiving-message
    （可读镜像：/v1.0/robot/messageFiles/download）https://dingtalk.apifox.cn/api-140599627
  - Stream FAQ（主动发消息、打字机模式说明）
    https://open-dingtalk.github.io/developerpedia/docs/learn/stream/faq/
  - 机器人回复/发送消息（Webhook vs OpenAPI）
    https://open-dingtalk.github.io/developerpedia/docs/learn/bot/appbot/reply/
  - AI 助理发送消息（主动发送、分步发送）
    https://opensource.dingtalk.com/developerpedia/docs/develop/agent/send-message/
  - 机器人发送消息类型（含 sampleImageMsg、sampleFile）
    https://dingtalk.apifox.cn/doc-3550110
  - 单聊发送接口（/v1.0/robot/privateChatMessages/send）
    https://dingtalk.apifox.cn/api-140273509