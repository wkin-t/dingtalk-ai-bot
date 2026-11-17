# 生图模型与钉钉媒体迁移

## 授权与目标

用户指定 Gemini 生图使用 gemini-nano-banana-2.1 与本轮新中转 key；GPT 使用 gpt-image-2.6-sunburst，要求最高思考深度；生图与改图结果改为机器人媒体上传并在原卡片中展示。仅部署 Gemini 与 OpenAI 容器，不推送 Git、不操作其他服务，不删除 COS 存量对象或凭据。

## 已知接口契约

- Gemini 原生图片输出调用 generate_content，response_modalities 为 IMAGE/TEXT，image_config.aspect_ratio 控制比例，从 candidates.content.parts.inline_data 读取图片。新模型走当前 Gemini 中转 client，Imagen 历史兼容仍使用 direct_client。
- GPT 图片模型官方仅公布 Images generation/edit 端点，图片规划复杂度 complexity 取 low/medium/high，最高为 high，没有聊天 reasoning.effort=max。需明确向用户说明最高档字段，并验证中转实际兼容性，不把 HTTP 200 宣称为上游执行证据。
- 现有 DingTalkCardHelper.upload_media 返回机器人应用所属 media_id，卡片使用单个 @ 前缀引用。不得借用 dws 个人身份，不放宽 dws_context 的只读白名单。
- 上传支持格式与大小须验证；PNG/JPEG/GIF/BMP 魔数识别可复用 bqb_image，WebP 需转换；不使用表情包的缩小画布策略处理生成大图。

## 实施步骤

1. 红军设计审查：客户端与凭据隔离、图片协议和最高规划档、应用身份与媒体失败恢复。
2. image_gen.py 与 test_image_gen.py：先补协议测试并观察失败，再实现 Gemini native 图片生成、正确 MIME 的编辑与 GPT complexity 字段；保持旧 Imagen 兼容。config.py 增加图片复杂度设置并解析。
3. dingtalk_card.py 与对应测试：增加生成图片上传适配，识别真实格式、WebP 转换、限制体积、校验上传返回与 @ 规范化。bot 生图/改图分支改走该接口，失败明确显示，不回落 COS，不调用 dws 写接口。保留旧 image_store 供 Deprecated 企业微信路径，不删除旧测试。
4. 更新 README/DEPLOY 中对应生图、凭据、COS 边界。先定向测试，再全套 pytest 与 compileall；独立代理审查最终 diff。
5. 服务器只读确认新模型可见、SDK 支持及源文件一致性，准备当前镜像派生的候选镜像，完整备份原 env/镜像/涉及源码与摘要。传输只包含指定非敏感文件并校验 SHA256。
6. 候选镜像中真实生成两张（各模型一张）与媒体上传验证，不向任何聊天发消息。保留非敏感生成图片以供用户检查；同时说明卡片 UI 与完整群聊回归的验证边界。
7. 验证通过后只重建 Gemini/OpenAI 容器，核对实际模型/认证、原有对话模型、健康、日志与其他容器 ID。不重建其他服务。失败回滚原 env 与原镜像；最多两次修复重试。

## 配置变更范围

- Gemini .env: IMAGE_BACKEND=gemini、GEMINI_IMAGE_MODEL=gemini-nano-banana-2.1、GEMINI_IMAGE_EDIT_MODEL=gemini-nano-banana-2.1；GEMINI_API_BASE_KEY 使用用户指定新 key（已在上轮设置，需核对）；保留 Google 直连 key 与 OpenAI 兼容 key。
- OpenAI .env.openai: IMAGE_BACKEND=openai、OPENAI_IMAGE_MODEL=gpt-image-2.6-sunburst、OPENAI_IMAGE_COMPLEXITY=high。对话思考深度不变。

## 验收

协议测试必须覆盖有图/无图、正确 base/key、数量/比例、GPT generation/edit 字段、图片格式和上传失败、机器人卡片引用以及两条分支均不调用 COS。线上验收只报告实际请求结果，不把模型列表、SDK 序列化或健康接口等同生图成功。

## 设计审查处置

独立红军提出 needs-attention，以下列为实现验收门禁：原生图片路径在配置中转地址时校验显式中转 key，缺失禁止调用，不能隐式复用 Google key；最多 n 次生成补调用并摘要去重，数量不足明确报错；GPT generation/edit 分别验证 complexity 与 output_format，2.6 分支不沿用旧 response_format；通用 upload_media 保持原始 ID 返回，新适配器单独产生 Markdown 引用；上传前验证真实格式、大小和像素，WebP 转 PNG；生成、上传、卡片阶段测试取消传播。图片分支维持现有直接结束返回，不新增临时 media_id 的正文缓存或长期持久化。同步 SDK 已开始的线程请求无法保证被协程取消中止，报告该边界。

比例接口保留现有映射，本轮真实预检仅覆盖 1:1，不能把序列化测试当所有比例已在线验收。Gemini 与 GPT 两个指定模型真实生成预检均成功，GPT 请求包含 complexity=high；该结果只证明中转接受并返回图片，不证明上游内部规划量。

## 2026-11-17 实施与验收结果

- 本地隔离环境与生产锁定一致：google-genai 2.34.0、openai 3.46.0；冻结代码相关测试 114 passed，完整 tests 1032 passed、5 个 SDK/异步清理 warning；compileall 与 diff whitespace 检查通过。
- 独立红军终审 approve。旧本机全局 SDK 与仓库锁定不一致不作为验证环境使用。
- Gemini/GPT 原始接口生图、改图均真实成功；候选镜像再走统一 generate_image/edit_image 与 upload_generated_image，四张结果均上传成功，COS spy 未调用，不发送聊天消息。
- Gemini 候选结果为 1024×1024 PNG，生图 537463 bytes、改图 520210 bytes；GPT 为 1312×1199 PNG，生图 1055007 bytes、改图 1157416 bytes。GPT 请求 size=1024x1024 但上游返回上述不同尺寸，保留原图，不宣称像素/比例服从配置。GPT 两条请求均明确发送 complexity=high、output_format=png。
- 两个候选镜像均从各自原生产镜像派生，只覆盖四个运行时源码文件，无依赖升级。传输包与四个源文件均校验 SHA256。
- 生产仅重建 Gemini/OpenAI，健康均 HTTP 200、restart count=0；运行容器源码摘要匹配候选，其他原有环境项与其他容器 ID 均未改变。原对话模型分工保持。
- Gemini 生图/改图原生中转继续使用本轮用户指定的专用 key，Google 直连及 OpenAI 兼容 key 均保留，没有写入仓库或本记录。
- 备份与回滚镜像、源码、环境、构建和验证记录位于服务器 deployment-backups/image-media-20261117T020813Z/。原生媒体属于临时资源；尚未在真实钉钉客户端完成新生图卡片的 UI、过期回看或引用改图端到端验收。

## CR 修复计划（用户已授权全部修复）

1. Gemini 提取先排除 thought=True 的 part，再对最终图片去重计数；测试草图与最终图、只有草图、跨调用补足最终图，以及改图复用该过滤器。
2. 图片格式识别集中到 bqb_image.detect_image_format；增加显式允许 WebP 的选项，默认行为不变，BQB 不会误上传 WebP。模型输入与生成图上传共用检测器，不重复维护 MIME/扩展名清单。
3. 生成图的钉钉上传前保留文件字节数、像素数和解压炸弹检查，先 verify，再重新打开逐帧完整 load；所有帧累计解码像素不超过 4000 万，避免为校验动画引入无界资源开销。WebP 转换在同一受限解码路径内完成。损坏、截断或不支持格式在调用 upload_media 之前拒绝，完整图片原样保留。
4. 生图/改图取消收尾共用辅助函数，以 1 秒独立预算执行最后卡片更新；超时或普通异常仅保留安全摘要，原 CancelledError 继续传播。测试真实异步阻塞、取消传播、清理协程结束和同群锁释放，两条图片分支均覆盖。
5. 对上述边界先补失败测试再实现；独立设计/最终审查。相关测试及全量通过后，从当前各自生产镜像构建候选，备份当前镜像与涉及源码，校验传输 SHA256。候选验证离线负例与实际媒体上传，保留当前模型/key/env；只切换 Gemini/OpenAI。不得重新引入 COS 或 dws 写操作。

设计审查结论为 approve；明确补充 GIF 后续帧完整性与累计解码预算，保留 BQB 默认不支持 WebP、第二次取消继续传播，以及同步 SDK 线程可能继续运行的既有边界。图片侧先观察 9 个负例失败，再观察动画 2 个负例失败；实现后 image_gen/dingtalk_card/bqb 三个文件的测试 99 passed。取消路径先观察 2 个失败，再通过专项 6 个测试和完整 bot 测试 76 个；测试包含真实卡片 stream 锁阻塞，超时后释放群锁且不遗留延迟 SDK 调用。

本轮完整 tests 为 1053 passed、5 warnings（79.13 秒）；warning 为既有的 SDK xhigh 枚举提示与模拟异步流清理告警。compileall、diff whitespace 和暂存区密钥形状审计通过。模型/key/env 不在本轮变更范围。

独立边界复审与独立提交终审均为 approve；终审额外运行 22 个定向测试通过。对照基线确认旧 OpenAI 数量处理和改图输入的完整解码预检属于相邻既有行为，不纳入本轮四项 CR 的修复范围。
