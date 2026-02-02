import asyncio
import random
import time
import base64
import dingtalk_stream
from dingtalk_stream import AckMessage
from app.config import DINGTALK_CLIENT_ID, DINGTALK_CLIENT_SECRET, MAX_HISTORY_LENGTH, DEFAULT_MODEL, CARD_TEMPLATE_ID, get_model_pricing, AVAILABLE_MODELS, AI_BACKEND
from app.memory import get_history, update_history, clear_history, get_session_key
from app.dingtalk_card import DingTalkCardHelper
from app.gemini_client import call_gemini_stream, analyze_complexity_with_model

# 尝试导入使用统计模块
try:
    from app.database import usage_stats, UsageStats
    USE_STATS = True
except Exception as e:
    USE_STATS = False
    print(f"⚠️ 使用统计模块不可用: {e}")

# --- 全局变量定义 ---
message_buffer = {}
session_locks = {}  # 会话级锁字典
processing_sessions = set()  # 正在处理的会话集合

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


def analyze_complexity(content: str, has_images: bool = False) -> dict:
    """
    分析问题复杂度，返回推荐的模型和 thinking level

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

class GeminiBotHandler(dingtalk_stream.ChatbotHandler):
    def __init__(self):
        super(GeminiBotHandler, self).__init__()
        self.card_helper = DingTalkCardHelper(DINGTALK_CLIENT_ID, DINGTALK_CLIENT_SECRET)
        self.card_template_id = CARD_TEMPLATE_ID  # 从环境变量读取
        
        self.thinking_phrases = [
            "CPU 正在燃烧 🔥", "正在翻阅百科全书 📖", "让我想想... 🤔",
            "正在连接宇宙意识 🌌", "头都要炸了 🤯", "正在疯狂码字中 ✍️",
            "正在调取量子算力 ⚛️", "大脑飞速运转中 🧠", "正在和数据打架 ⚔️",
            "稍等，灵感马上就来 💡"
        ]

    def _calculate_cost(self, model_usage: list) -> float:
        """根据模型用量计算费用 (美元)"""
        total_cost = 0.0
        for usage in model_usage:
            model = usage.get('model', 'default')
            input_tokens = usage.get('input_tokens', 0)
            output_tokens = usage.get('output_tokens', 0)
            pricing = get_model_pricing(model)
            # 价格是每百万 token
            input_cost = (input_tokens / 1_000_000) * pricing['input']
            output_cost = (output_tokens / 1_000_000) * pricing['output']
            total_cost += input_cost + output_cost
        return total_cost

    async def _show_stats(self, incoming_message, session_key: str, user_id: str):
        """显示使用统计"""
        try:
            # 获取用户统计
            user_stats = UsageStats.get_user_stats(user_id, days=7)
            # 获取群/会话统计
            session_stats = UsageStats.get_session_stats(session_key, days=7)
            # 获取全局统计
            global_stats = UsageStats.get_global_stats(days=7)

            # 格式化统计信息
            lines = ["## 📊 使用统计 (近7天)\n"]

            # 用户统计
            lines.append("### 👤 你的使用情况")
            if user_stats and user_stats.get('total_requests', 0) > 0:
                lines.append(f"- 请求次数: **{user_stats.get('total_requests', 0)}** 次")
                lines.append(f"- 输入 Token: **{user_stats.get('total_input_tokens', 0):,}**")
                lines.append(f"- 输出 Token: **{user_stats.get('total_output_tokens', 0):,}**")
                lines.append(f"- 平均延迟: **{int(user_stats.get('avg_latency_ms', 0)):,}** ms")
                # 计算用户费用
                model_usage = user_stats.get('model_usage', [])
                if model_usage:
                    user_cost = self._calculate_cost(model_usage)
                    lines.append(f"- 💰 预估费用: **${user_cost:.4f}** (约 ¥{user_cost * 7.2:.2f})")
            else:
                lines.append("- 暂无使用记录")

            # 群/会话统计
            lines.append("\n### 💬 本群使用情况")
            if session_stats and session_stats.get('total_requests', 0) > 0:
                lines.append(f"- 请求次数: **{session_stats.get('total_requests', 0)}** 次")
                lines.append(f"- 参与用户: **{session_stats.get('unique_users', 0)}** 人")
                total_tokens = session_stats.get('total_input_tokens', 0) + session_stats.get('total_output_tokens', 0)
                lines.append(f"- 总 Token: **{total_tokens:,}**")
            else:
                lines.append("- 暂无使用记录")

            # 全局统计
            lines.append("\n### 🌐 全局统计")
            if global_stats and global_stats.get('total_requests', 0) > 0:
                lines.append(f"- 总请求: **{global_stats.get('total_requests', 0)}** 次")
                lines.append(f"- 活跃用户: **{global_stats.get('unique_users', 0)}** 人")
                lines.append(f"- 活跃群聊: **{global_stats.get('unique_sessions', 0)}** 个")
                total_tokens = global_stats.get('total_input_tokens', 0) + global_stats.get('total_output_tokens', 0)
                lines.append(f"- 总 Token: **{total_tokens:,}**")

                # 模型分布和费用
                model_dist = global_stats.get('model_distribution', [])
                if model_dist:
                    lines.append("\n**模型分布及费用:**")
                    total_cost = 0.0
                    for m in model_dist[:5]:
                        model_name = m.get('model', 'unknown')
                        count = m.get('count', 0)
                        input_t = m.get('input_tokens', 0)
                        output_t = m.get('output_tokens', 0)
                        pricing = get_model_pricing(model_name)
                        cost = (input_t / 1_000_000) * pricing['input'] + (output_t / 1_000_000) * pricing['output']
                        total_cost += cost
                        # 简化模型名显示
                        model_short = model_name.replace("gemini-", "").replace("-preview", "")
                        lines.append(f"- {model_short}: {count}次, ${cost:.4f}")

                    lines.append(f"\n💰 **总费用: ${total_cost:.4f}** (约 ¥{total_cost * 7.2:.2f})")
            else:
                lines.append("- 暂无使用记录")

            lines.append(f"\n---\n<font color='gray' size='1'>定价参考: ai.google.dev/pricing | 汇率: 1 USD = 7.2 CNY</font>")

            self.reply_markdown("使用统计", "\n".join(lines), incoming_message)

        except Exception as e:
            print(f"⚠️ 获取统计失败: {e}")
            self.reply_markdown("系统提示", f"⚠️ 获取统计失败: {str(e)}", incoming_message)

    def _build_display_content(self, thinking: str, response: str, is_thinking: bool = False) -> str:
        """
        构建显示内容，包含 thinking 和正式回复

        Args:
            thinking: 模型的思考过程
            response: 模型的正式回复
            is_thinking: 是否正在思考中

        Returns:
            格式化的显示内容
        """
        parts = []

        # 显示 thinking 内容 (折叠样式)
        if thinking:
            # 截取 thinking 内容，避免过长
            thinking_display = thinking
            if len(thinking) > 2000:
                thinking_display = thinking[:2000] + "..."

            if is_thinking:
                parts.append(f"<details open>\n<summary>🧠 **正在思考中...**</summary>\n\n{thinking_display}\n</details>")
            else:
                parts.append(f"<details>\n<summary>🧠 **思考过程** (点击展开)</summary>\n\n{thinking_display}\n</details>")

        # 显示正式回复
        if response:
            # 过滤摘要
            display_response = response.replace("[AILoading]", "……")
            lines = display_response.split('\n')
            filtered_lines = [line for line in lines if not line.strip().startswith("> 📝 概要：")]
            display_response = "\n".join(filtered_lines).strip()

            if thinking:
                parts.append("\n---\n")
            parts.append(display_response)
        elif is_thinking:
            parts.append("\n\n⏳ *等待回复生成...*")

        return "".join(parts)

    async def _update_card_throttled(self, out_track_id: str, content: str, last_update_time: float, is_first: bool) -> float:
        """节流更新卡片 - 增加节流间隔以减少 API 压力"""
        import time
        current_time = time.time()

        # 增加节流间隔：第一次立即更新，后续至少间隔 1 秒
        if is_first or current_time - last_update_time > 1.0:
            await self.card_helper.stream_update(out_track_id, content, is_finalize=False, content_key="msgContent")
            return current_time

        return last_update_time

    async def handle_gemini_stream(self, incoming_message, content, conversation_id, at_user_ids, image_data_list=None, group_info=None):
        print(f"🚀 开始处理 Gemini 请求: {content} (User: {incoming_message.sender_id})")
        if image_data_list:
            print(f"🖼️ 收到图片数量: {len(image_data_list)}")
        
        session_key = get_session_key(conversation_id, incoming_message.sender_id)
        
        # 获取完整历史记录
        full_history = get_history(session_key)
        
        # 截取最近的 N 条发送给 Gemini
        if len(full_history) > MAX_HISTORY_LENGTH:
            history_messages = full_history[-MAX_HISTORY_LENGTH:]
        else:
            history_messages = full_history
            
        # 构造 System Prompt
        from datetime import datetime, timezone, timedelta
        # 获取北京时间 (UTC+8)
        beijing_tz = timezone(timedelta(hours=8))
        current_time = datetime.now(beijing_tz).strftime("%Y-%m-%d %H:%M:%S")

        # 提取日期信息
        current_date = datetime.now(beijing_tz)
        year = current_date.year
        month = current_date.month
        day = current_date.day

        system_prompt = f"""你是 Gem，一个有帮助的 AI 助手。你的回答应该准确，不要产生幻觉。

⏰ 重要时间信息（请务必记住）:
- 今天是: {year} 年 {month} 月 {day} 日
- 当前完整时间: {current_time} (北京时间, UTC+8)
- 你的训练数据可能截止于 2025 年，但现在已经是 {year} 年了
- 当回答涉及"今年"、"现在"、"当前"等时间相关问题时，请使用上述日期而非训练数据中的时间

格式规则:
1. 不要使用 LaTeX 语法（如 $x^2$ 或 $$...$$）。用纯文本或 Unicode 表示数学公式（如 x^2, sqrt(x)）。
2. 可以使用 Markdown：表格、加粗、斜体、列表、代码块。

上下文感知:
- 对话历史中包含用户昵称和时间戳，格式为 '[时间] 昵称: 消息'。
- 引用用户发言时，可以提及其昵称和时间（如 '正如张三在 14:30 所说...'）。
- 所有时间均为北京时间 (UTC+8)。

重点:
- 直接回应最新用户的输入。
- 仅将之前的上下文作为参考。

输出要求:
- 直接输出答案。不要输出状态指示器或 '[AILoading]'。
- 使用中文回答。技术术语可在中文后加英文括号（如：机器学习 (Machine Learning)）。

搜索和实时信息:
- 如果启用了 Google Search，搜索结果会自动提供给你
- 当搜索结果与你的训练数据冲突时，优先相信搜索结果
- 特别是涉及时间、日期、最新事件时，搜索结果比训练数据更准确
- 如果用户质疑你对时间的认知，请再次确认：今天是 {year} 年 {month} 月 {day} 日

思考语言:
- 请使用中文进行思考和推理。你的内部思考过程也应该用中文表达。"""

        # 注入群信息 (只注入群名)
        if group_info:
            group_name = group_info.get('name', 'Unknown Group')
            
            group_context = f"\n\nGROUP CONTEXT:\nYou are currently in a DingTalk group chat named '{group_name}'.\n\nTASK:\nBased on the group name, briefly analyze what technical capabilities or domain knowledge you might need to assist this group effectively. Keep this analysis internal to guide your responses."
            system_prompt += group_context

        messages = []
        messages.append({
            "role": "system",
            "content": system_prompt
        })

        # 格式化历史消息，添加时间戳信息
        formatted_history = []
        for msg in history_messages:
            formatted_msg = {"role": msg["role"]}
            content = msg.get("content", "")
            timestamp = msg.get("timestamp")

            # 如果有时间戳，添加到内容前面
            if timestamp and msg["role"] == "user":
                # 用户消息格式: [时间] 原始内容
                formatted_msg["content"] = f"[{timestamp}] {content}"
            else:
                # AI 回复不添加时间戳前缀（保持简洁）
                formatted_msg["content"] = content

            formatted_history.append(formatted_msg)

        if image_data_list:
            if history_messages and history_messages[-1]['role'] == 'user':
                pass

            from datetime import datetime, timezone, timedelta
            beijing_tz = timezone(timedelta(hours=8))
            current_timestamp = datetime.now(beijing_tz).strftime("%Y-%m-%d %H:%M:%S")

            sender_nick = incoming_message.sender_nick or "User"

            user_message_content = []
            user_message_content.append({"type": "text", "text": f"[{current_timestamp}] {sender_nick}: [图片x{len(image_data_list)}] {content}"})
            
            for i, img_data in enumerate(image_data_list):
                b64_image = base64.b64encode(img_data).decode('utf-8')
                print(f"🖼️ 处理第 {i+1} 张图片，大小: {len(img_data)} bytes")
                user_message_content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{b64_image}"}
                })

            messages.extend(formatted_history)
            messages.append({"role": "user", "content": user_message_content})

        else:
            # 无图片时：先添加历史记录，再添加当前用户消息
            from datetime import datetime, timezone, timedelta
            beijing_tz = timezone(timedelta(hours=8))
            current_timestamp = datetime.now(beijing_tz).strftime("%Y-%m-%d %H:%M:%S")

            sender_nick = incoming_message.sender_nick or "User"
            text_content = f"[{current_timestamp}] {sender_nick}: {content}"
            messages.extend(formatted_history)
            messages.append({"role": "user", "content": text_content})

        # 初始化 AI 卡片
        thinking_text = random.choice(self.thinking_phrases)
        
        card_data = {
            "msgTitle": "Gemini AI",
            "thinkingText": thinking_text,
            "msgContent": "Thinking...", 
            "isError": "false",
            "flowStatus": "1",
            "config": {"autoLayout": True} 
        }
        
        out_track_id = await self.card_helper.create_and_deliver(
            conversation_id, 
            self.card_template_id,
            card_data,
            at_user_ids
        )
        
        if not out_track_id:
            self.reply_markdown("系统错误", "⚠️ 无法创建 AI 卡片，请检查权限或模板 ID。", incoming_message)
            return

        print(f"✅ 卡片创建成功，ID: {out_track_id}")

        # 智能路由：根据 AI_BACKEND 选择后端
        print(f"🔄 [路由] AI 后端: {AI_BACKEND}")
        has_images = bool(image_data_list)

        if AI_BACKEND == "openclaw":
            # OpenClaw 模式: 内部处理模型选择
            target_model = "openclaw"
            thinking_level = "auto"
            need_search = False
            print(f"🎯 OpenClaw 模式: 由 Gateway 自动处理路由")
        else:
            # Gemini 模式: 智能路由分析
            print(f"🔄 [路由] 开始智能路由分析...")
            try:
                complexity = await analyze_complexity_with_model(content, has_images)
                print(f"🔄 [路由] 预分析返回: {complexity}")
            except Exception as e:
                print(f"❌ [路由] 预分析异常: {e}")
                import traceback
                traceback.print_exc()
                complexity = {
                    "model": "gemini-3-flash-preview",
                    "thinking_level": "low",
                    "need_search": False,
                    "reason": "路由异常，使用默认"
                }
            target_model = complexity.get("model", "gemini-3-flash-preview")
            thinking_level = complexity.get("thinking_level", "low")
            need_search = complexity.get("need_search", False)
            print(f"🎯 智能路由: {complexity.get('reason', '默认')} → 模型={target_model}, thinking={thinking_level}, search={need_search}")

        full_response = ""
        full_thinking = ""  # 真实的 thinking 内容
        last_update_time = time.time()
        is_first_chunk = True
        is_thinking = False  # 是否正在输出 thinking
        usage_info = None  # 使用统计信息

        sender_name = incoming_message.sender_nick or "User"
        at_header = f"👋 @{sender_name} \n\n"

        try:
            # 根据后端选择调用不同的 API
            if AI_BACKEND == "openclaw":
                from app.openclaw_client import call_openclaw_stream
                stream = call_openclaw_stream(
                    messages,
                    conversation_id=conversation_id,
                    sender_id=incoming_message.sender_id,
                    sender_nick=sender_name
                )
            else:
                stream = call_gemini_stream(
                    messages,
                    target_model=target_model,
                    thinking_level=thinking_level,
                    enable_search=need_search
                )

            async for chunk in stream:
                # 处理使用统计
                if "usage" in chunk:
                    usage_info = chunk["usage"]
                    continue

                if "error" in chunk:
                    print(f"❌ {chunk['error']}")
                    await self.card_helper.stream_update(out_track_id, f"❌ **API 请求失败**\n\n{chunk['error']}", is_finalize=True, content_key="msgContent")
                    return

                # 处理 thinking 开始/结束标记
                if chunk.get("thinking_start"):
                    is_thinking = True
                    continue
                if chunk.get("thinking_end"):
                    is_thinking = False
                    continue

                # 处理 thinking 内容
                thinking_delta = chunk.get("thinking", "")
                if thinking_delta:
                    full_thinking += thinking_delta
                    # 构造显示内容：thinking + 正式回复
                    display_content = self._build_display_content(full_thinking, full_response, is_thinking=True)
                    await self._update_card_throttled(out_track_id, display_content, last_update_time, is_first_chunk)
                    if is_first_chunk:
                        is_first_chunk = False
                        last_update_time = time.time()
                    continue

                # 处理正式回复内容
                content_delta = chunk.get("content", "")
                if content_delta:
                    content_delta = content_delta.replace("[AILoading]", "")
                    full_response += content_delta

                    # 构造显示内容
                    display_content = self._build_display_content(full_thinking, full_response, is_thinking=False)

                    if is_first_chunk:
                        is_first_chunk = False
                        await self.card_helper.stream_update(out_track_id, display_content, is_finalize=False, content_key="msgContent")
                        last_update_time = time.time()
                        continue

                    current_time = time.time()
                    # 增加节流间隔到 1 秒，减少 API 请求频率
                    if current_time - last_update_time > 1.0:
                        await self.card_helper.stream_update(out_track_id, display_content, is_finalize=False, content_key="msgContent")
                        last_update_time = current_time

            print(f"✅ 流式响应结束，总长度: {len(full_response)}, thinking: {len(full_thinking)}")

            # 记录使用统计
            if USE_STATS and usage_info:
                try:
                    usage_stats.record(
                        session_key=session_key,
                        user_id=incoming_message.sender_id,
                        model=usage_info.get("model", DEFAULT_MODEL),
                        input_tokens=usage_info.get("input_tokens", 0),
                        output_tokens=usage_info.get("output_tokens", 0),
                        latency_ms=usage_info.get("latency_ms", 0)
                    )
                    print(f"📊 已记录使用统计: {usage_info}")
                except Exception as e:
                    print(f"⚠️ 记录统计失败: {e}")

            full_response = full_response.replace("[AILoading]", "")
            clean_response = full_response.strip()

            # 构建状态栏：只有 thinking 时才显示摘要，避免重复显示内容
            status_text = ""
            if full_thinking:
                # 截取 thinking 前 80 个字符作为摘要
                thinking_brief = full_thinking[:80].replace("\n", " ").strip()
                if len(full_thinking) > 80:
                    thinking_brief += "..."
                status_text = f"<font color='#aaaaaa' size='2'>🧠 {thinking_brief}</font>"
            # 没有 thinking 时不显示摘要，避免与主内容重复

            # 显示模型、thinking level 和联网状态
            model_short = target_model.replace("gemini-", "").replace("-preview", "")
            search_icon = "🌐" if need_search else ""
            status_text += f"\n\n<font color='#808080' size='2'>🤖 {model_short} | 🧠 {thinking_level} {search_icon}</font>"

            buttons = [
                {
                    "text": "🧹 清空",
                    "color": "grey", 
                    "event": {
                        "type": "openUrl",
                        "params": {"url": "dtmd://dingtalkclient/sendMessage?content=🧹 清空记忆"}
                    }
                },
                {
                    "text": "🔄 重试",
                    "color": "blue", 
                    "event": {
                        "type": "openUrl",
                        "params": {"url": "dtmd://dingtalkclient/sendMessage?content=" + (content or "重试")}
                    }
                },
                {
                    "text": "📝 总结",
                    "color": "grey",
                    "event": {
                        "type": "openUrl",
                        "params": {"url": "dtmd://dingtalkclient/sendMessage?content=📝 总结摘要"}
                    }
                },
                {
                    "text": "🇬🇧 翻译",
                    "color": "grey",
                    "event": {
                        "type": "openUrl",
                        "params": {"url": "dtmd://dingtalkclient/sendMessage?content=🇬🇧 翻译成英文"}
                    }
                }
            ]
            
            final_content = at_header + clean_response
            
            # 记录历史 (使用 update_history 写入文件)
            update_history(session_key, user_msg=None, assistant_msg=full_response)
            
            await self.card_helper.stream_update(
                out_track_id,
                final_content,
                is_finalize=True,
                content_key="msgContent"
            )
            
            update_data = {
                "msgContent": final_content, 
                "statusText": status_text,
                "msgButtons": buttons,
                "flowStatus": "3" 
            }
            print(f"🔄 正在全量更新卡片: {update_data.keys()}")
            
            success = await self.card_helper.update_card(out_track_id, update_data)
            
            if not success:
                print("⚠️ 全量更新失败，启用兜底方案：拼接按钮到正文")
                buttons_md = (
                    "\n\n"
                    "[🧹 清空](dtmd://dingtalkclient/sendMessage?content=🧹 清空记忆) | "
                    "[🔄 重试](dtmd://dingtalkclient/sendMessage?content=" + (content or "重试") + ") | "
                    "[📝 总结](dtmd://dingtalkclient/sendMessage?content=📝 总结摘要) | "
                    "[🇬🇧 翻译](dtmd://dingtalkclient/sendMessage?content=🇬🇧 翻译成英文)"
                )
                if status_text:
                    final_content += "\n\n---\n" + status_text
                final_content += buttons_md
                
                await self.card_helper.stream_update(
                    out_track_id,
                    final_content,
                    is_finalize=True,
                    content_key="msgContent"
                )
            
            print(f"✅ [DingTalk Stream] AI 卡片流式响应完成")

        except Exception as e:
            error_msg = f"系统异常: {str(e)}"
            print(f"💥 {error_msg}")
            try:
                await self.card_helper.stream_update(
                    out_track_id,
                    f"💥 **系统异常**\n\n{error_msg}",
                    is_finalize=True,
                    content_key="msgContent"
                )
            except:
                pass

    async def process_buffered_messages(self, session_key):
        await asyncio.sleep(2.0)

        if session_key not in message_buffer:
            return

        # 获取或创建会话锁
        if session_key not in session_locks:
            session_locks[session_key] = asyncio.Lock()

        async with session_locks[session_key]:
            # 再次检查，防止在等待锁期间被其他任务处理
            if session_key not in message_buffer:
                return

            # 标记正在处理
            processing_sessions.add(session_key)

            try:
                data = message_buffer[session_key]
                del message_buffer[session_key]

                content_list = data["content"]
                image_list = data["images"]
                incoming_message = data["incoming_message"]
                at_user_ids = data["at_user_ids"]

                full_content = "\n".join(content_list)

                # 如果只有图片没有文字，使用默认提示
                if not full_content and image_list:
                    full_content = "请详细描述这张图片的内容，包括主要元素、场景、文字等信息。"

                sender_nick = incoming_message.sender_nick or "User"
                history_content = full_content
                if image_list:
                    history_content += f" [图片x{len(image_list)}]"

                # 获取群信息 (只获取群名)
                group_info = None
                if incoming_message.conversation_type == '2':  # 群聊
                    group_name = ""

                    if hasattr(incoming_message, 'conversation_title') and incoming_message.conversation_title:
                        group_name = incoming_message.conversation_title
                    else:
                        info = await self.card_helper.get_group_info(incoming_message.conversation_id)
                        if info and hasattr(info, 'title'):
                            group_name = info.title

                    if group_name:
                        group_info = {'name': group_name}
                        print(f"✅ 获取到群信息: {group_name}")

                update_history(session_key, history_content, assistant_msg=None, sender_nick=sender_nick)
                print(f"📥 [DingTalk Stream] 处理合并消息: {history_content} (User: {sender_nick})")

                await self.handle_gemini_stream(incoming_message, full_content, incoming_message.conversation_id, at_user_ids, image_list, group_info)
            finally:
                # 清除正在处理标记
                processing_sessions.discard(session_key)

    async def process(self, callback: dingtalk_stream.CallbackMessage):
        try:
            incoming_message = dingtalk_stream.ChatbotMessage.from_dict(callback.data)
            
            msg_type = incoming_message.message_type
            content = ""
            image_data_list = [] 
            
            if msg_type == "text":
                content = incoming_message.text.content.strip()
            elif msg_type == "picture":
                download_code = incoming_message.image_content.download_code
                print(f"📥 收到图片消息，正在下载... Code: {download_code}")
                img_data = await self.card_helper.download_file(download_code)
                if img_data:
                    print(f"✅ 图片下载成功，大小: {len(img_data)} bytes")
                    image_data_list.append(img_data)
                    content = "请详细描述这张图片的内容，包括主要元素、场景、文字等信息。"
                else:
                    content = "[图片下载失败]"
            elif msg_type == "richText":
                rich_list = incoming_message.rich_text_content.rich_text_list
                print(f"📥 收到富文本消息，包含 {len(rich_list)} 个元素") 
                for item in rich_list:
                    if "text" in item:
                        content += item["text"]
                    if "downloadCode" in item: 
                        download_code = item["downloadCode"]
                        print(f"📥 收到富文本图片，正在下载... Code: {download_code}")
                        img_data = await self.card_helper.download_file(download_code)
                        if img_data:
                            print(f"✅ 图片下载成功")
                            image_data_list.append(img_data)
                        await asyncio.sleep(0.5)
            
            if not content and not image_data_list:
                return AckMessage.STATUS_OK, 'OK'

            sender_id = incoming_message.sender_staff_id or incoming_message.sender_id
            conversation_id = incoming_message.conversation_id 
            session_key = get_session_key(conversation_id, sender_id)

            should_reply = False
            if incoming_message.conversation_type == '1': 
                should_reply = True
            elif incoming_message.is_in_at_list: 
                should_reply = True
            
            if not should_reply:
                sender_nick = incoming_message.sender_nick or "User"
                update_history(session_key, content if content else "[图片]", assistant_msg=None, sender_nick=sender_nick)
                return AckMessage.STATUS_OK, 'OK'

            at_users = incoming_message.at_users or []
            at_user_ids = []
            for user in at_users:
                if hasattr(user, 'dingtalk_id'):
                    at_user_ids.append(user.dingtalk_id)
                elif hasattr(user, 'staff_id'):
                    at_user_ids.append(user.staff_id)
                elif isinstance(user, dict):
                    at_user_ids.append(user.get('dingtalkId'))
            if sender_id and sender_id not in at_user_ids:
                at_user_ids.append(sender_id)

            if content == "/clear" or content == "清空上下文" or content == "🧹 清空记忆":
                clear_history(session_key)
                self.reply_markdown("系统提示", "🧹 你的上下文已清空", incoming_message)
                return AckMessage.STATUS_OK, 'OK'

            # 查看统计命令
            if content == "/stats" or content == "📊 统计":
                if USE_STATS:
                    await self._show_stats(incoming_message, session_key, sender_id)
                else:
                    self.reply_markdown("系统提示", "⚠️ 统计功能不可用", incoming_message)
                return AckMessage.STATUS_OK, 'OK'

            # 检查是否正在处理该会话 (防止并发竞态)
            # 如果正在处理且没有缓冲区，创建缓冲区让消息排队
            if session_key in processing_sessions and session_key not in message_buffer:
                print(f"⏳ 会话正在处理中，将消息加入缓冲区排队: {session_key}")
                message_buffer[session_key] = {
                    "content": [],
                    "images": [],
                    "incoming_message": incoming_message,
                    "at_user_ids": at_user_ids
                }

            if session_key in message_buffer:
                message_buffer[session_key]["timer"].cancel()
            else:
                message_buffer[session_key] = {
                    "content": [],
                    "images": [],
                    "incoming_message": incoming_message, 
                    "at_user_ids": at_user_ids
                }
            
            if content:
                message_buffer[session_key]["content"].append(content)
            if image_data_list:
                message_buffer[session_key]["images"].extend(image_data_list)
            
            message_buffer[session_key]["incoming_message"] = incoming_message
            
            task = asyncio.create_task(self.process_buffered_messages(session_key))
            message_buffer[session_key]["timer"] = task

        except Exception as e:
            print(f"💥 [DingTalk Stream] Process 异常: {e}")

        return AckMessage.STATUS_OK, 'OK'