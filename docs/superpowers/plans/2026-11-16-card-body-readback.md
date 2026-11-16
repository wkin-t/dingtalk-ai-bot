# 卡片 Markdown 正文完整回读与部署

## 范围与约束

保留所有现有 Markdown 正文；只在最终状态栏加入高熵、带正文版本摘要的引用码。dws 继续决定当前可见消息、撤回状态、clear 起点和历史预算；Redis 仅补全已确认发送的机器人卡片正文，不建立独立会话，不作为读取失败 fallback。明确调整旧的严格 dws-only 正文约束。无 env、模型、数据库表或 compose 修改。

## 实现接口

新建 `app/card_body.py`：纯函数准备不可变 draft（企业、会话、由服务端 robotCode 唯一确认的 dws 发送者、卡片实例、正文、随机引用码、正文摘要）；异步确认写入和批量补全。确认写入仅发生在最终卡片全量更新成功或含同一引用码的 fallback 流式最终更新成功之后。状态栏小字 `ref:cb1-<random>.<digest>` 可读；正文不加引用码。记录共享 Redis，短超时、72小时 TTL、最多1024项、单条正文最多64KiB；故障跳过补全，绝不覆盖主回复或泄露正文。

补全只接受有 message_id、sender_id、正确 conversation_id 的当前 dws 记录；唯一精确 footer 引用码必须与记录企业/群/发送者和正文摘要全部匹配。发送业务确认后立即用 dws 查询同一群最近10条，以唯一 footer 和可信发送者确定精确 message_id 后才写缓存；读路径绝不首次绑定，不可读/重复/未匹配即跳过。恢复要求缓存 message_id 与当前记录精确一致，防同一 bot 的旧 footer 重放。批量读取，保留原消息角色、时间、资源和顺序。缺失 sender、重复/伪造 token、过期/损坏/超限均不补全。正文更新使用新 token，记录不可变；本次只缓存一次最终正文，不缓存部分流式内容。

`dingtalk_bot` 在最终状态构造完成后准备 draft，最后成功写入后确认；存储正文使用 history_response（已处理 @ 为纯文本、表情包为语义标签），其 Markdown 结构保持不变，避免向模型注入钉钉媒体控制符。`dws_context` 在撤回判断之后、历史预算筛选之后补全普通历史；显式引用仅对已经 list/mget 确认可读的对象补全。引用邻居同样只补全经 dws 读回的条目。必须使用企业校验结果 identity.corp_id，不依赖配置猜测。

## 任务与验证

1. 独立设计挑战：撤回/跨群跨bot、最终确认与缓存失败、版本/引用码伪造。
2. 核心模块与回归测试先红后绿；主线程负责 bot/dws 接入和集成测试，避免编辑冲突。
3. 针对正文完整性、窗口/clear、撤回引用、群聊多 bot 身份、发送失败、Redis 故障运行测试；compileall；全量 pytest。独立实现审查并修复高风险发现。
4. 本地记录可审查差异；无 push。线上先核对版本和共享 Redis 非敏感设置，备份源码、运行镜像与 inspect（不保存 Env 原文）；最小补丁镜像预先构建，再逐个 targeted recreate。不能 down、remove-orphans 或全局清理。
5. Clawbot 群固定 Markdown 卡片实际发送和 dws 回读，调用实际上下文组装验证完整正文；撤回/跨群等没有真实执行时分别标注。真实模型追问验证作为辅助，不替代输入断言。
6. 完成复查：源码 hash、三个容器状态/重启次数、生产健康与 Clawbot 验收；记录回滚命令和残余限制。

## 设计挑战处置

- 同 sender 引用码重放：采用发送后 dws 精确 message_id 绑定；reader 不建绑定。
- HTTP 200 业务失败：卡片流式与全量更新须同时检查 SDK body.success / body.result；fallback 只有含 footer 的提交获业务确认才进入缓存确认。
- clear 前显式引用：沿用已有明确约束“显式引用仍可重供旧内容”；普通历史仍先 clear/N 条过滤。精确 message_id 绑定阻止将旧 footer 复制进新消息绕过普通历史起点。
- 多 bot 身份：生产验收核对三个机器人实际 sender_id / robotCode 不同；缓存匹配平台发送身份，不能用昵称识别。
- 全局1024项容量：这是本项目三个 bot 共用的总容量，不是每群配额；超限仅丢补全能力，不改变 dws 可见性。Redis Lua 原子写入与淘汰，避免并发孤儿记录。
- 本机SDK版本不一致：局部纯函数回归仅辅助；全量测试在生产基础镜像的隔离容器执行，测试依赖不进入生产镜像。

## 线上试验发现与修正

- dws 1.0.67 当前卡片返回顶层 `text` / `content` 字符串；实际投影会删去引用块、剥除状态栏 font 标签。解析器同时接受裸 footer 和本应用生成的精确 font 包装，不接受非末尾、重复或其他嵌套标签。
- 最终更新获得业务成功回执之后，dws 列表仍可能暂未返回新状态。确认阶段最多查询3次，等待0.4/0.8秒，查询及等待共用5秒预算；超时不写缓存。Redis 写入另受0.2秒连接/读写超时限制。
- 手工发送卡片和真实 Stream 路径必须分别验收。第一张手工卡确认后，真实 @GPT 追问已准确读出只存在于引用块内的随机校验词；这不能证明 Stream 自己生成的卡片保存成功。
- 进一步让 GPT 自己生成全部为引用块的回复后，发现 Stream `chatbotUserId` 不能直接与 dws `openDingtalkId` 比较。身份解析改为：同企业、同群的完整机器人名单中，按服务端配置 `DINGTALK_CLIENT_ID` 精确匹配唯一 `robotCode`，获取 `openDingtalkId`。缺失、重复、部分名单均不猜测；群聊历史自身角色和卡片保存使用同一确认身份。

## 部署与回滚定位

- 目标主机：`tencent_lighthouse_server`；工作目录：`/opt/1panel/docker/compose/dingtalk-ai-bot`。
- 备份：`deployment-backups/card-body-20261116-172859/`，包含原运行源码、原镜像标签、补丁清单、隔离测试日志、真实试验证据和 `rollback.sh`。
- 三个容器逐个按服务 recreate，无其他服务重启；不执行远端 git pull、push 或 compose down。
- 回滚：在服务器运行 `sh /opt/1panel/docker/compose/dingtalk-ai-bot/deployment-backups/card-body-20261116-172859/rollback.sh`。

## 最终验收结果

- 生产基础镜像隔离测试：`1013 passed, 3 warnings`，约29秒；警告来自既有 Gemini `xhigh` 能力测试。隔离容器无网络、无生产数据挂载，测试依赖未进入生产镜像。
- 本地编译检查、差异空白检查通过；独立实现审查最终 `approve`。
- GPT、Gemini、Claude 三个服务分别 recreate，健康端点 HTTP 200、重启计数0；四个运行源码文件的 SHA256 均与本次构建清单一致。
- Clawbot 固定卡片 `CBLIVE-5e2c2dd7d5`：最终业务确认和 dws 精确 messageId 绑定成功，Redis 原文恢复逐字节一致；三个容器实际调用上下文组装都含完整引用块，GPT 视为自身 assistant，另外两个 bot 视为其他发言 user。三者均解析到自身正确 `openDingtalkId`。
- 真实 Stream 自身回复验收：GPT 自行生成 `> SELF_7kP3mQ9xR2vB`，消息ID `msgsjqL5HzVSRPnrNxc+O+DCg==`。dws 原始正文只有状态栏、不含校验词；共享缓存成功恢复该完整引用块。随后真实个人 @GPT 追问（问题不含这个词），GPT 通过真实模型调用准确回复 `SELF_7kP3mQ9xR2vB`，回复ID `msgqhiqWGO+2EMC76RQ7Kq1MQ==`。
- 所有真实写入试验仅发生于 Clawbot 群；没有实际撤回群消息，没有检查桌面渲染截图。撤回、clear/窗口、失败确认、身份伪造/重放、Redis 故障等由自动化回归覆盖。
- 只为部署后成功确认的群聊卡片建立新缓存；旧卡片没有对应原文记录不能追溯恢复。当前单聊不建立新缓存。72小时、全局1024项、单条64KiB限制仍然生效。
