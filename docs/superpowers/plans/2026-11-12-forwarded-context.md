# 合并转发引用读取

真实 v1.0.67 样本：Clawbot 最近记录中，原消息同时返回原始 `forwardMessages` 和投影 `forwarded`；引用上下文只有 `quotedMessage.forwardMessages`。三条子消息包含 content/createTime/openConversationId/openMessageId/sender/senderOpenDingTalkId/sendType，其中一条正文2919字符。不要使用正文短摘要冒充完整读取。

沿用现有 dws_context 清洗/引用数据结构和 dws_files 资源读取入口，不增加 CLI 权限或跨会话查询。统一识别 forwardMessages/forwarded/forward_messages（同一消息优先原始forwardMessages避免重复）；有界展开子消息，保留归属、时间、嵌套层次，深度3、总节点30、总正文12000字符，超限标注。原记录仍计作一条历史；clear 按外层消息过滤，显式引用可以重供旧内容。

子消息媒体仅在同一有界展开范围内汇集，保留其真实message/conversation定位，交由已有受控下载器和图片/文档独立预算读取，不恢复旧库、不递归调用子消息查询。按官方固定版本契约，仅子消息缺会话ID时可继承父会话ID；绝不继承父消息ID。兼容resourceRefs数组、媒体描述的冒号/等号、media-id/file-id、结构化content和$iw图片Markdown。无可用资源的图片占位明确标记不可读。引用格式化不再把合并正文截成800字符；router摘要仍短。

实现文件 app/dws_context.py（标准化/引用合并/展示）、app/dws_files.py（仅在必要时调整媒体定位）、tests/test_dws_forwarded.py 与 tests/test_dws_files.py，README/CLAUDE更新。先写失败回归，覆盖原始引用结构、超过6条/80字、三种字段、嵌套/截断、同条历史计数、clear、资源定位与媒体上限。独立设计门禁后实现，局部/全量测试，独立提交门禁，保留服务器回滚镜像后部署，真实样本只读验证展开字符数和子消息数，不打印群正文。

设计审查：approve；特别约束merge必须保留has_forwarded，原始字段即使空数组也优先，不拼接有损投影。官方契约核对v1.0.67 internal/shortcut/chatmsg/chatmsg.go和chatmsg_test.go；不需要升级CLI。

验证：初始回归8 failed/1 passed，补官方媒体契约5 failed，缺子消息定位用例1 failed；实现后相关103 passed，全量806 passed（2条既有SDK流关闭警告）、compileall通过。真实Clawbot样本独立进程验证3子消息2919/21/165字符完整进入3394字符引用prompt，旧解析引用仅62字符；未模型调用、未发送消息。真实媒体合并样本尚未出现，媒体定位使用固定版契约与mock回归验证，不能当作真实下载验收。
