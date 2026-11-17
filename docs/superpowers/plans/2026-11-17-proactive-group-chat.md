# 工作日主动群聊

用户已确认：北京时间中国法定工作日（含调休，排除节假日），09:00–12:00、14:00–18:00；覆盖所有已接入且有真人活跃的群，三个助手合计每群每天随机 2–4 次，至少间隔 90 分钟。每次发送前必须由当前 dws 重新确认过去 24 小时存在真人消息；机器人消息不延长窗口，不在无人群自聊。

## 选择与分层

- 每容器独立定时会导致跨助手撞车；额外部署调度服务增加运维组件。采用现有 Stream 进程内任务 + 共享 Redis 原子协调，复用机器人 OpenAPI 卡片发送。Redis 只存调度元数据，不存对话正文；Redis 故障关闭主动功能，不降级为各容器独立发送。
- `app/proactive_policy.py`：东八区法定工作日、随机时隙、真人活跃判断、轮换选择，纯逻辑。日历依赖 `chinesecalendar`，锁定核实支持 2026/2027 的版本；未知年份或日历异常不发送。
- `app/proactive_state.py`：共享 Redis 的心跳、群内本 bot 成员登记、每日随机计划、原子抢占和限频。全局按企业+会话隔离，日额度跨三个 BOT_ID 共用。
- `app/proactive_dws.py`：只读群发现与当前可见性快照。仅列举当前 dws 登录企业的群，且当前 bot 的 robotCode 在同群完整机器人名单中唯一匹配，才视为“已接入”；不按群名/机器人昵称猜测。
- `app/proactive_chat.py`：调度、生成、发送编排；启动与 Stream 同一事件循环，重连保持同一 worker、会话退出时取消任务；与普通响应共用 `(BOT_ID,cid)` 串行锁，并在用户缓冲/正在处理时跳过。
- `app/dingtalk_bot.py` 只提供生成/发送和锁的薄适配；`main.py` 负责启停。主动闲聊采用非流式真实完成协议，Gemini 只接受 STOP 和有效模型版本；GPT/Claude Responses 只接受 completed、有效模型且无 error/incomplete/refusal/function call，store=false、不挂任何工具。复用现有 SDK client 构造和消息转换，不改普通对话协议。

## 调度与并发契约

1. 每日每群一次性生成 2–4 个随机时隙，落在工作时段且相隔至少 5400 秒，Redis NX 持久到跨重启，过期时隙不补发。单时隙宽限最多 10 分钟；午休/18:00 后绝不补发。
2. 各 bot 心跳 180 秒、群成员校验登记 1800 秒。用同一每日随机种子和时隙，在当前在线且验证在群的 bot 中选择发言者；有其他候选时避免连续同一 bot。一个 bot 不可用不立即触发其他 bot 连续补发。
3. 原子 claim 校验计划/时隙/上次尝试间隔，抢占群租约并消耗本时隙，失败/结果未知/取消也不退额度。租约 120 秒，整个主动轮次不超过 90 秒，生成最多 45 秒。发送前复核租约所有权。
4. 用户明确允许真人刚发言时主动接话，不设置安静期，不因生成期间出现新消息而自动避让。生成前取最新上下文；发送前再取当前 dws 快照：同企业、同会话、bot 仍在群、真人仍在 24 小时内、仍为工作时段，否则丢弃草稿。只有机器人发言时不续期真人活跃。
5. 主动发送使用确定 outTrackId（企业/会话/日期/时隙），用 SDK model.to_map 序列化创建和投递请求。取得 token 并通过门禁后，先创建未投递卡片，发送完整 streaming finalize，再更新完成态；准备全部成功后再次核验当前 dws、成员、企业、真人活动、时间与租约，最后只执行一次 deliver。四次 HTTP 均可取消且无自动重试，任一步失败或未知不补发，准备阶段不向群暴露卡片。发送阶段最多30秒，整体90秒和120秒租约不变。

## 上下文与内容

- 正文只来自当前 dws 的本 bot 可见历史，尊重 MAX_HISTORY_LENGTH 与 /clear；不读旧 Redis/MySQL/文件历史。主动读取不下载历史图片/文档、不合并旧数据，不虚构当前用户消息。
- 结合各自 Soul 和近期话题，允许自然发散，输出 1–3 句、最多 240 汉字。没有合适话题可明确返回跳过；不提“定时任务/活跃气氛”等实现说明，不伪装真人，不 @ 群成员，不自动生图、发文件或触发其他机器人。
- 模型失败、空内容、不合规标记、dws/企业/日历/Redis 不确定时静默跳过，仅记录不含正文和凭据的原因码，不发送错误卡片。
- 机器人卡片和其他 bot 发言可作为话题上下文，但不得用于判定“最近 24 小时有人说话”。群内贴纸/转发作为用户确实发送的消息可计入活动，不能拿引用子消息的时间刷新活动。

## 接口分工

策略模块：`BEIJING`, `in_working_hours(now)`, `build_daily_slots(day, rng) -> list[float]`, `last_human_activity(rows, cid, now, bot_ids) -> float | None`, `select_speaker(seed, slot_index, bot_ids, last_bot='') -> str`。

状态模块 `ProactiveStore(redis_client)` 同步方法，由调度器 `asyncio.to_thread` 调用：

- `heartbeat(corp, bot, now)`；`register_group(corp,cid,bot,now)`；`eligible_bots(corp,cid,now) -> list[str]`。
- `get_or_create_plan(corp,cid,day,slots) -> dict`，返回 `seed`、`slots`、`day`（slots 为 epoch 秒）。
- `last_speaker(corp,cid) -> str`；`claim(corp,cid,day,slot_index,bot,now) -> str | None`；`owns(corp,cid,token) -> bool`；`finish(corp,cid,token)`。未知或损坏状态拒绝发送。

DWS 模块：`discover_groups(corp) -> list[dict]`（字段 cid/name，完整性明确）；`fetch_group_snapshot(corp,cid,robot_code,now,with_context=False)` 返回结构化快照（corp_id,conversation_id,name,bot_sender_id,bot_ids,last_human_at,latest_message_id,history）。失败返回 None。白名单仅新增 `chat +chat-list-all` 的有限分页只读参数；保留所有禁止 dws 写操作的规则。

## 配置与验证

- 新开关 `PROACTIVE_CHAT_ENABLED` 默认 false；启用需绑定受信企业 `PROACTIVE_CHAT_CORP_ID`（可沿用明确 DWS_EXPECTED_CORP_ID）。默认 60 秒轮询、15 分钟重新发现；可选群 allowlist 用于受控验收，生产按用户选择为空=所有验证已接入群。
- TDD 先验证节假日/调休/边界/24h 只真人，再验证真实 Redis Lua 的竞争、重启、额度与失效；用真实 dws 样本验证群发现/身份，不扩大权限。编排测试使用假时钟验证生成耗时跨午休/18:00、租约丢失、真人刚发言可接话、仅机器人、取消、未知发送结果均不发或不重发。
- 独立设计挑战与最终边界复审；全测、编译通过后，Clawbot 受控实际发言验收。不得通过把群里的任意真人消息伪造成新回调绕开条件；发布后开所有验证通过的群，保留关闭开关和旧镜像回滚路径。

日历来源：[chinesecalendar 官方项目](https://github.com/liriansu-opus/chinese-calendar)，[国务院 2026 年节假日安排](https://www.beijing.gov.cn/zhengce/zhengcefagui/202511/t20251104_4258873.html)。Redis 原子协调参照 redis-py 官方 Lua 所有权比较模式。

用户补充硬要求：发送前必须确认真实模型生成链路成功，不向群发送大段 error。先后台完整收取正文和完成状态，任何 error chunk、超时、空正文或明显 API 错误正文均静默跳过；验证成功前不得创建思考卡片、不得错误回退消息。

## 设计挑战修订

- Redis 外另留共享磁盘安全闩（data-dws/proactive-guard），只存非秘密随机 epoch。以 O_EXCL/fsync 初始化，Redis 永不过期 sentinel 必须与之相同；磁盘有闩但 Redis 数据丢失时暂停主动功能，不自动重建已消耗额度。普通服务不受影响。
- 启用时必须显式 BOT_ID 且属于 gemini/openai/anthropic，登记 robotCode 和进程 instance UUID；bot_id 首次绑定 robotCode，同一 BOT_ID 多活动实例不抢占。成员登记携带已核验 dws sender，claim/发送门禁保持同一实例。
- speaker 策略改为候选 SHA1(seed:slot:bot) 排名，排除上次发言者（有其他候选时）；Lua 原子复核当前 roster、last speaker、winner，与策略使用同一规则。名义 slot 和实际 claim 都保证 90 分钟间隔。选中 bot 在 claim 前离线可在宽限内按当前名单重选；claim 后不替补、不退款。
- 活跃判定是存在性条件：找到一条当前可信真人消息即有证据；未找到且查询分页未覆盖窗口时返回未知并跳过。完整 bot bucket 的 openDingtalkId 必须唯一齐全，自己的 robotCode 必须唯一匹配；其他已明确列在 bot bucket 的机器人不要求公开 AppKey 才能排除。
- 主动 history 直接使用当前 raw dws 投影，按本 bot /clear 和 MAX_HISTORY_LENGTH 截取，不调用普通 fetch bundle、正文恢复或媒体下载；真人活动窗口独立于该过滤。
- 项目拥有 async runner，显式跟踪 Stream 与主动任务。Stream 被 shield，关闭时取消并等待主动任务；当前 SDK 吞第一次取消的行为用有界双取消清理并针对真实 SDK 假 websocket 测试验证，不能留下旧 worker。

设计挑战最终结论 approve。实施边界追加：生产使用持久化计划镜像（不含正文），Redis plan/daily-state 缺失、镜像缺失/不一致、created epoch 或任一初始化 slot 字段丢失均暂停，绝不补键重开额度；所有 slot 原子初始化为0，只允许0→1。发送不使用 executor，结果未知不重试。MAX_HISTORY_LENGTH=0 时主动上下文为空；取消 dws 子进程必须 kill/wait，防90秒轮次退出后残留读取。

## 空白卡片回归

2026-11-17 一个已接入群的 GPT 主动卡片在 API 和 dws 中都有80字正文、flowStatus=3，客户端却为空白。对同一张卡补齐标题/布局仍然空白；补发相同正文的 streaming isFinalize=true 和最终实例更新后，用户确认显示恢复。此结果证明仅检查 API/dws 不足以验收渲染。修复采用未投递卡片先完成流式生命周期再投放，避免准备失败在群内留下空卡；Clawbot 新卡片已取得用户客户端正常显示确认，正文与 dws 回读哈希一致。已消耗的当日时隙和 Redis 安全闩保留，不为验收重开额度。
