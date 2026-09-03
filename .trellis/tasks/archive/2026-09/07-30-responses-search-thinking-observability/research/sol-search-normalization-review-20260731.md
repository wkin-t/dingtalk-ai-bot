# Sol 搜索归一化复核（2026-07-31）

## 第一轮结论

Sol 发现两个 P1：结构化搜索信号没有受 `native_search` 门控，以及诊断摘要把
任意满足字符集的未知事件类型原样写入日志；另发现候选列表没有上限、未启用
搜索负例和 SDK-like 对象测试缺口。

## 修复

- 结构化事件和 Grounding 启发式均要求本次 `native_search=True`。
- 诊断事件类型改为固定 Responses event allowlist，未知类型统一记为 `other`。
- 每层候选列表最多扫描 64 项，递归深度保持固定上限。
- 增加 dict、SimpleNamespace、嵌套 final output、`enable_search=False`、未知事件
  日志和 bounded-list 测试。
- 底层流 iterator 异常或取消时先打印固定字段探针，再继续原有异常/取消传播。

## 第二轮结论

Sol 复核确认上一轮 P1/P2 均关闭，未发现新的 P0-P2 缺陷。剩余 P3 是普通
`git diff --check` 对既有 CRLF JSONL 行尾的提示；使用
`git -c core.whitespace=cr-at-eol diff --check` 后通过。生产 canary 仍未完成，
不能用本地测试替代真实 sub2api/Antigravity 回流和 DingTalk footer 见证。

## 证据

- targeted：`83 passed`，退出码 `0`（第二轮复核前）。
- 搜索归一化专项补充 bounded-list 后：`10 passed`，退出码 `0`。
- 最终全量 pytest：`549 passed, 3 warnings`，退出码 `0`。
- Trellis validate：退出码 `0`。
- compileall：退出码 `0`。
- CRLF-aware diff check：退出码 `0`。
