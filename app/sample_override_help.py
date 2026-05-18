# app/sample_override_help.py
# -*- coding: utf-8 -*-
"""渲染 /temp /top_p /sample 命令的回复文案。与 sample_override 解耦。"""
from typing import Optional, Dict, Any


def render_temp_status(rec: Optional[Dict[str, Any]]) -> str:
    if rec and rec.get("temperature") is not None:
        current = f"`{rec['temperature']}` ⚙️（手动，{rec.get('set_by_nick','?')} 设置于 {rec.get('set_at','?')}，24h 后失效）"
    else:
        current = "`auto`（由路由自动决定）"
    return f"""## 🌡️ Temperature 配置

**当前生效**: {current}

---

### 是什么
温度（Temperature）控制 AI 回答的"随机性"。值越低越确定，越高越随机。

### 档位参考

| 值   | 适用场景 |
|------|---------|
| 0.1  | 代码、数学、事实问答（precise） |
| 0.7  | 默认对话（balanced） |
| 0.9  | 写作、头脑风暴（creative） |
| 1.3  | 实验性创意（wild） |
| 2.0  | 完全随机（chaotic） |

⚠️ Claude 模型上限 1.0，超过自动 clamp。

---

设置: `/temp 1.5`
清除: `/temp reset`
查看 top_p: `/top_p`
综合视图: `/sample`"""


def render_top_p_status(rec: Optional[Dict[str, Any]]) -> str:
    if rec and rec.get("top_p") is not None:
        current = f"`{rec['top_p']}` ⚙️（手动，{rec.get('set_by_nick','?')} 设置于 {rec.get('set_at','?')}）"
    else:
        current = "`auto`（默认 1.0，不截断）"
    return f"""## 🎯 Top-P 配置

**当前生效**: {current}

---

### 是什么
Top-P（核采样）只在累计概率前 P 的候选 token 里采样，截断长尾噪音。
和 temperature 是不同的"创意旋钮"。

### 档位参考

| 值    | 效果 |
|-------|------|
| 1.0   | 不截断（默认） |
| 0.95  | 轻微截断长尾 |
| 0.9   | 中度截断 |
| 0.5   | 强裁剪，输出聚焦 |

💡 调创意度时，OpenAI 官方建议优先调 top_p 而不是 temperature。

---

设置: `/top_p 0.9`
清除: `/top_p reset`
查看温度: `/temp`
综合视图: `/sample`"""


def render_sample_status(rec: Optional[Dict[str, Any]]) -> str:
    if rec:
        temp_part = f"`{rec['temperature']}` ⚙️ 手动" if rec.get("temperature") is not None else "`auto`"
        top_p_part = f"`{rec['top_p']}` ⚙️ 手动" if rec.get("top_p") is not None else "`auto`（默认 1.0）"
        footer = (
            f"\n**设置人**: {rec.get('set_by_nick','?')} ({rec.get('set_by','?')})\n"
            f"**设置时间**: {rec.get('set_at','?')}\n"
            f"**过期时间**: {(rec.get('expires_at') or '')[:19]}\n"
        )
        warn = (
            "\n⚠️ **温度和 top_p 同时手动设置**——风格变化会很强烈，注意观察输出质量。"
            if (rec.get("temperature") is not None and rec.get("top_p") is not None)
            else ""
        )
    else:
        temp_part = "`auto`"
        top_p_part = "`auto`（默认 1.0）"
        footer = ""
        warn = ""

    return f"""## ⚙️ 采样配置总览

**当前生效**:
- Temperature: {temp_part}
- Top-P:        {top_p_part}
{footer}{warn}

---

### 两个旋钮的分工

| 参数        | 作用         | 调高会怎样     | 调低会怎样    |
|-------------|--------------|---------------|--------------|
| Temperature | 缩放 logits  | 更随机/有创意  | 更确定/稳定   |
| Top-P       | 候选截断     | 候选多/多样    | 候选少/聚焦   |

### 实战配方
- "换个风格" → 先调 top_p (1.0 → 0.95 → 0.9)
- "代码更稳" → 降 temperature (→ 0.1)
- "完全随机" → 同时拉高 T (1.3+) 和 top_p (≥0.95)

---

设置温度:    `/temp 1.5`
设置 top_p:  `/top_p 0.9`
一键重置:    `/sample reset`
查看 Soul:   `/soul`"""
