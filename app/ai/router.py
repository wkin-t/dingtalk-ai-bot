# -*- coding: utf-8 -*-
"""
智能路由模块 - 从 dingtalk_bot.py 抽取
"""

# 复杂度关键词
COMPLEX_KEYWORDS = [
    # 代码相关
    "代码", "编程", "code", "python", "java", "javascript", "sql", "debug", "bug", "报错", "error",
    "函数", "算法", "实现", "开发", "api", "接口",
    # 数学/推理
    "计算", "数学", "公式", "证明", "推导", "分析", "逻辑", "推理",
    # 深度分析
    "详细", "深入", "全面", "比较", "对比", "优缺点", "原理", "架构", "设计",
    "为什么", "如何", "怎么", "解释", "分析",
    # 创作
    "写一篇", "撰写", "创作", "文章", "报告", "方案",
]

# Pro 专用关键词 (需要更强推理能力)
PRO_KEYWORDS = [
    # 高级推理
    "证明", "推导", "论证", "推理过程", "逻辑链",
    # 复杂架构
    "系统设计", "架构设计", "技术方案", "设计模式",
    # 深度分析
    "深度分析", "全面分析", "详细分析", "根本原因",
    # 复杂数学
    "微积分", "线性代数", "概率论", "统计", "优化",
    # 专业领域
    "论文", "研究", "学术", "专业",
    # 用户明确要求
    "用pro", "使用pro", "pro模型", "深度思考",
]

SIMPLE_KEYWORDS = [
    "你好", "hi", "hello", "谢谢", "thanks", "再见", "bye",
    "是什么", "什么是", "定义", "简单",
]


def analyze_complexity_unified(content: str, has_images: bool = False) -> dict:
    """
    统一的复杂度分析函数

    路由策略:
    - Flash + minimal: 简单问候
    - Flash + low: 普通问题
    - Flash + medium: 中等复杂度
    - Flash + high: 复杂问题
    - Pro + high: 超复杂问题 (需要深度推理)

    Returns:
        {
            "model": "gemini-3-flash" or "gemini-3-pro-preview",
            "thinking_level": "minimal" | "low" | "medium" | "high",
            "reason": "分析原因"
        }
    """
    content_lower = content.lower()
    content_len = len(content)

    # 默认值
    model = "gemini-3-flash"
    thinking_level = "low"
    reason = "普通问题"

    # 1. 检查是否是简单问候/闲聊
    if content_len < 20:
        for kw in SIMPLE_KEYWORDS:
            if kw in content_lower:
                return {
                    "model": "gemini-3-flash",
                    "thinking_level": "minimal",
                    "reason": "简单问候"
                }

    # 2. 统计关键词匹配
    complex_count = sum(1 for kw in COMPLEX_KEYWORDS if kw in content_lower)
    pro_count = sum(1 for kw in PRO_KEYWORDS if kw in content_lower)

    # 3. 检查是否包含代码块
    has_code = "```" in content or content.count("\n") > 5

    # 4. 决定模型和 thinking level

    # 超复杂问题 → Pro + high
    if pro_count >= 2 or (pro_count >= 1 and complex_count >= 3):
        model = "gemini-3-pro-preview"
        thinking_level = "high"
        reason = f"深度推理 (Pro关键词={pro_count}, 复杂={complex_count})"

    # 复杂问题 + 长文本 → Pro + high
    elif complex_count >= 4 and content_len > 300:
        model = "gemini-3-pro-preview"
        thinking_level = "high"
        reason = f"复杂长文 (关键词={complex_count}, 长度={content_len})"

    # 复杂代码问题 → Flash + high (Flash 代码能力也很强)
    elif has_code and complex_count >= 2:
        model = "gemini-3-flash"
        thinking_level = "high"
        reason = f"代码问题 (关键词={complex_count})"

    # 复杂问题 → Flash + high
    elif complex_count >= 3:
        model = "gemini-3-flash"
        thinking_level = "high"
        reason = f"复杂问题 (关键词={complex_count})"

    # 中等复杂 → Flash + medium
    elif complex_count >= 1 or has_code:
        model = "gemini-3-flash"
        thinking_level = "medium"
        reason = f"中等复杂 (关键词={complex_count})"

    # 长文本 → 提升 thinking level
    if content_len > 500:
        if thinking_level == "low":
            thinking_level = "medium"
        elif thinking_level == "medium" and model == "gemini-3-flash":
            thinking_level = "high"
        reason += f" + 长文本({content_len}字)"

    # 图片分析 → 至少 medium
    if has_images:
        if thinking_level in ["minimal", "low"]:
            thinking_level = "medium"
        reason += " + 图片"

    return {
        "model": model,
        "thinking_level": thinking_level,
        "reason": reason
    }
