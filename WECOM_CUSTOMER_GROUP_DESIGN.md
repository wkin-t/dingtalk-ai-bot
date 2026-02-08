# 企业微信客户群能力深化设计（面向当前仓库）

更新时间：2026-02-08

## 1. 目标与范围

本设计用于在当前项目（钉钉 + 企业微信智能机器人）基础上，新增一条“客户群运营”能力线。

目标：
- 保留现有内部群智能机器人体验（@、流式、卡片）。
- 增加客户群可用能力（入群方式管理、客户群列表/详情、群发触达、运营自动化）。
- 明确能力边界：客户群侧不假设支持“群内 @ 机器人实时对话”。

非目标：
- 不在本阶段强行模拟“客户群内实时智能机器人”。
- 不把客户群能力和内部群智能机器人共用一个回调协议。

## 2. 官方能力边界（已核对）

### 2.1 智能机器人（内部聊天场景）
- 文档：
  - 接收消息：https://developer.work.weixin.qq.com/document/path/100719
  - 被动回复消息：https://developer.work.weixin.qq.com/document/path/101031
  - 主动回复消息：https://developer.work.weixin.qq.com/document/path/101138
- 已确认能力：
  - 群里 `@` 机器人触发回调。
  - 被动/主动回复。
  - `stream`、`stream_with_template_card`、`template_card`。

### 2.2 客户群（客户联系场景）
- 文档：
  - 客户群「加入群聊」管理：https://developer.work.weixin.qq.com/document/path/92229
  - 获取客户群列表：https://developer.work.weixin.qq.com/document/path/92120
  - 获取客户群详情：https://developer.work.weixin.qq.com/document/path/92122
  - 群发消息到客户群：https://developer.work.weixin.qq.com/document/path/93556
- 已确认接口（可直接落地）：
  - `externalcontact/groupchat/add_join_way`
  - `externalcontact/groupchat/get_join_way`
  - `externalcontact/groupchat/update_join_way`
  - `externalcontact/groupchat/del_join_way`
  - `externalcontact/groupchat/list`
  - `externalcontact/groupchat/get`

结论：
- 客户群是“运营管理 + 触达”导向能力。
- 智能机器人是“会话交互”导向能力。
- 两条链路应在系统内独立建模。

## 3. 产品形态建议（双引擎）

### 3.1 引擎 A：内部群智能机器人（已有）
- 场景：内部协作群、内部问答。
- 特性：@触发、流式回复、模板卡片、会话记忆。
- 代码：`app/wecom/callback.py` + `app/wecom/bot.py`（已存在）。

### 3.2 引擎 B：客户群运营助手（新增）
- 场景：客户拉新、分层触达、群运营自动化。
- 特性：
  - 管理入群活码/自动建群策略。
  - 按标签或规则选择客户群。
  - AI 生成群发内容（文本/markdown/卡片素材）。
  - 发送后效果回收（覆盖率、失败原因、二次补发）。

## 4. 系统架构设计

## 4.1 新增模块结构

建议新增：

```text
app/wecom_customer/
  __init__.py
  api_client.py         # 企业微信客户联系 API 封装
  access_token.py       # token 缓存与刷新
  group_service.py      # 客户群列表/详情/入群方式
  campaign_service.py   # 群发任务编排与投递
  ai_content_service.py # 调用现有 AIHandler 产出运营文案
  policy.py             # 发送频控、重试、幂等等策略
  models.py             # 任务与状态数据结构
```

并在 `app/routes.py` 或新蓝图中提供运维接口：

```text
/api/wecom/customer/groups/sync
/api/wecom/customer/join-way/*
/api/wecom/customer/campaign/create
/api/wecom/customer/campaign/{id}/run
/api/wecom/customer/campaign/{id}/status
```

## 4.2 与现有代码的关系

- 复用：
  - `app/ai/handler.py` 作为统一 AI 文案引擎。
  - `app/memory.py` 存储运营任务上下文（可单独 session_key 前缀）。
- 隔离：
  - 不复用 `app/wecom/callback.py` 的“智能机器人回调协议”作为客户群入口。
  - 客户群任务以“任务驱动”而不是“会话驱动”处理。

## 5. 核心流程设计

## 5.1 入群方式管理（运营前置）

1. 创建/更新 `join_way`（活码）。
2. 配置自动建群参数（群满自动新建、前缀等）。
3. 关联 `state` 用于后续来源归因。
4. 定时回查并落库（便于面板可视化）。

## 5.2 客户群资产同步

1. 周期拉取客户群列表（增量）。
2. 拉取群详情（成员、状态、标签映射）。
3. 生成可投递群清单（过滤沉默群/关闭群）。

## 5.3 群发任务（Campaign）执行

1. 创建任务：
   - 输入：活动主题、目标人群、禁发词、品牌语气、发送窗口。
2. AI 生成内容：
   - 使用 `AIHandler`，但 prompt 切换为“客户运营文案模式”。
3. 审核与冻结：
   - 人工确认后发布（默认人工闸门，防误发）。
4. 投递执行：
   - 分批发送，限流，失败重试，幂等控制。
5. 结果回收：
   - 记录成功/失败与错误码，形成复盘报告。

## 6. 数据模型（建议）

最小表集：
- `wecom_customer_group`
  - `chat_id`, `name`, `owner`, `member_count`, `status`, `updated_at`
- `wecom_join_way`
  - `config_id`, `state`, `auto_create_room`, `room_base_name`, `qr_code`, `updated_at`
- `wecom_campaign`
  - `campaign_id`, `name`, `status`, `created_by`, `prompt`, `content_snapshot`, `schedule_at`
- `wecom_campaign_target`
  - `campaign_id`, `chat_id`, `send_status`, `error_code`, `error_msg`, `updated_at`
- `wecom_campaign_log`
  - `campaign_id`, `event`, `payload`, `created_at`

状态机（`wecom_campaign.status`）：
- `draft` -> `approved` -> `running` -> `finished`
- 任意阶段可进入 `failed` / `cancelled`

## 7. 配置与安全设计

新增环境变量建议：

```env
# 客户联系 API 鉴权
WECOM_CORP_ID=
WECOM_SECRET=
WECOM_AGENT_ID=

# 客户群任务调度
WECOM_CUSTOMER_SYNC_CRON=*/10 * * * *
WECOM_CUSTOMER_SEND_QPS=5
WECOM_CUSTOMER_RETRY_MAX=3
WECOM_CUSTOMER_DRY_RUN=true

# 内容安全
WECOM_CUSTOMER_MANUAL_APPROVAL=true
WECOM_CUSTOMER_BLOCK_WORDS=返现,保本,医疗治愈
```

安全原则：
- 默认 `dry-run` + 人工审批，防止上线即误发。
- 所有投递请求写审计日志（谁发的、发给谁、发了什么）。
- 按 campaign 做幂等键，防重放。

## 8. 可靠性与可观测性

可靠性：
- API 调用统一重试（退避 + 幂等）。
- 任务队列化，避免单点阻塞。
- 长任务可断点续跑。

可观测性：
- 指标：
  - campaign 成功率、失败率、平均耗时
  - 单群失败码分布
  - AI 生成耗时与 token 用量
- 日志字段统一：
  - `trace_id`, `campaign_id`, `chat_id`, `api`, `errcode`

## 9. 分阶段落地计划

### Phase 1（1-2 天）：骨架与只读能力
- 新增 `wecom_customer` 模块骨架。
- 实现客户群列表/详情同步。
- 实现入群方式 CRUD 封装。
- 输出最小管理接口（仅查询）。

验收标准：
- 能从企业微信拉到客户群并落库。
- 能创建并读取 `join_way`。

### Phase 2（2-3 天）：群发任务 MVP
- 实现 campaign 数据模型与状态机。
- 接入 AI 文案生成（人工审批后发送）。
- 支持 dry-run 与真实发送切换。

验收标准：
- 能创建任务 -> 生成内容 -> 审批 -> 发送 -> 看到结果。

### Phase 3（2-4 天）：自动化与优化
- 增加定时任务（自动分群、自动草稿）。
- 增加失败补偿和复盘报表。
- 加入运营策略模板（欢迎、活动预热、沉默召回）。

验收标准：
- 运营同学可在不改代码下发起标准任务。

## 10. 对当前仓库的具体改造点

优先改造文件：
- `app/config.py`
  - 增加客户群配置项与默认值。
- `main.py`
  - 注册客户群管理蓝图（与 `wecom_bp` 并存）。
- `app/ai/handler.py`
  - 增加 `scenario` 参数（`chat` / `customer_campaign`），按场景切换 system prompt。
- 新增目录 `app/wecom_customer/*`
  - 放置 API 客户端、服务、任务编排。
- `README.md` + `WECOM_DEPLOYMENT.md`
  - 明确“智能机器人”和“客户群运营”两套能力边界和配置方法。

## 11. 风险与规避

- 风险：把客户群当实时机器人做，最终能力不匹配。
  - 规避：架构上分两引擎，避免协议混用。
- 风险：群发内容质量不稳定，触发投诉。
  - 规避：默认人工审批 + 黑词过滤 + 发送窗口控制。
- 风险：接口限流/失败导致部分群遗漏。
  - 规避：任务分批 + 重试 + 对账补发机制。

## 12. 下一步建议（马上可做）

1. 先落 Phase 1：把客户群与 join_way 管理能力接进来，形成“可见可管”底座。  
2. 再做 Phase 2：引入 campaign MVP，先人工审批发送，不直接全自动。  
3. 最后做 Phase 3：加自动化策略与运营报表。  
