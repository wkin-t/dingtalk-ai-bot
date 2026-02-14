import asyncio
import random
import time
import base64
import json
import re
import dingtalk_stream
from dingtalk_stream import AckMessage
from app.config import (
    DINGTALK_CLIENT_ID,
    DINGTALK_CLIENT_SECRET,
    MAX_HISTORY_LENGTH,
    DEFAULT_MODEL,
    CARD_TEMPLATE_ID,
    get_model_pricing,
    AVAILABLE_MODELS,
    AI_BACKEND,
    BOT_ID,
    OPENCLAW_CONTEXT_MESSAGES,
    OPENCLAW_TOOLS_URL,
    OPENCLAW_TOOLS_TOKEN,
    OPENCLAW_ASR_TOOL_NAME,
    OPENCLAW_FILE_TOOL_NAME,
    OPENCLAW_VISION_TOOL_NAME,
    OPENCLAW_GATEWAY_TRANSPORT,
    DINGTALK_TYPING_ENABLED,
    DINGTALK_TYPING_INTERVAL_MS,
    DINGTALK_TYPING_FRAMES_RAW,
    DINGTALK_REFERENCE_AUTO_ENABLED,
    DINGTALK_IMAGE_MSG_KEY,
    DINGTALK_IMAGE_MSG_PARAM_TEMPLATE,
)
from app.memory import get_history, update_history, clear_history, get_session_key
from app.dingtalk_card import DingTalkCardHelper
from app.gemini_client import call_gemini_stream, analyze_complexity_with_model
from app.openclaw_tools_client import invoke_tool, build_asr_arguments, build_file_arguments, build_vision_arguments
from app.reference import maybe_inject_reference

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
group_info_cache = {}  # 群信息缓存 (conversation_id -> {"name": str, "timestamp": float})

# 消息去重缓存 (message_id -> timestamp)
# 使用 dict 存储最近处理过的消息 ID，定期清理过期条目
processed_messages = {}
MESSAGE_ID_CACHE_SIZE = 1000  # 最多缓存 1000 条
MESSAGE_ID_TTL = 300  # 消息 ID 缓存 5 分钟


def _extract_image_gen_json_block(text: str) -> tuple[str, dict | None]:
    """
    Extract an image generation result JSON block from model output.

    Expected marker: "【生图结果JSON】"
    Supported formats:
    - 【生图结果JSON】```json { ... } ```
    - 【生图结果JSON】{ ... }

    Returns:
    - cleaned_text: original text with the JSON block removed (trimmed)
    - payload: parsed JSON dict or None
    """
    marker = "【生图结果JSON】"
    if marker not in (text or ""):
        return (text or "").strip(), None

    src = text or ""
    start = src.find(marker)
    if start < 0:
        return src.strip(), None

    tail = src[start + len(marker):]

    # Prefer fenced ```json blocks
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", tail, flags=re.IGNORECASE | re.DOTALL)
    json_str = None
    end_in_tail = None
    if m:
        json_str = m.group(1)
        end_in_tail = m.end()
    else:
        # Fallback: parse from first '{' to matching '}' using brace counting.
        i = tail.find("{")
        if i >= 0:
            depth = 0
            in_str = False
            esc = False
            for j in range(i, len(tail)):
                ch = tail[j]
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == "\"":
                        in_str = False
                    continue
                if ch == "\"":
                    in_str = True
                    continue
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        json_str = tail[i : j + 1]
                        end_in_tail = j + 1
                        break

    if not json_str or end_in_tail is None:
        return src.strip(), None

    payload = None
    try:
        payload = json.loads(json_str)
    except Exception:
        payload = None

    # Remove marker + parsed block segment
    remove_end = start + len(marker) + end_in_tail
    cleaned = (src[:start] + src[remove_end:]).strip()
    return cleaned, payload if isinstance(payload, dict) else None

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


def _cleanup_expired_message_ids():
    """清理过期的消息 ID 缓存"""
    global processed_messages
    current_time = time.time()

    # 移除超过 TTL 的消息 ID
    expired_ids = [msg_id for msg_id, timestamp in processed_messages.items()
                   if current_time - timestamp > MESSAGE_ID_TTL]

    for msg_id in expired_ids:
        processed_messages.pop(msg_id, None)

    # 如果缓存超过上限，移除最旧的条目
    if len(processed_messages) > MESSAGE_ID_CACHE_SIZE:
        sorted_items = sorted(processed_messages.items(), key=lambda x: x[1])
        excess_count = len(processed_messages) - MESSAGE_ID_CACHE_SIZE
        for msg_id, _ in sorted_items[:excess_count]:
            processed_messages.pop(msg_id, None)


def _is_message_processed(message_id: str) -> bool:
    """
    检查消息是否已处理过

    Args:
        message_id: 钉钉消息 ID

    Returns:
        True 如果消息已处理，False 否则
    """
    global processed_messages

    # 清理过期条目 (每次检查时执行，性能开销很小)
    _cleanup_expired_message_ids()

    # 检查是否已处理
    if message_id in processed_messages:
        return True

    # 标记为已处理
    processed_messages[message_id] = time.time()
    return False


async def get_cached_group_info(card_helper, conversation_id: str, incoming_message) -> str:
    """
    获取群信息（优先级：消息字段 > 缓存 > API 调用）

    Args:
        card_helper: DingTalkCardHelper 实例
        conversation_id: 群会话 ID
        incoming_message: 钉钉消息对象

    Returns:
        群名称字符串
    """
    # 优先级1: 消息自带的群名
    if hasattr(incoming_message, 'conversation_title') and incoming_message.conversation_title:
        print(f"✅ 使用消息自带的群信息: {incoming_message.conversation_title}")
        return incoming_message.conversation_title

    # 优先级2: 内存缓存（24小时有效）
    if conversation_id in group_info_cache:
        cached = group_info_cache[conversation_id]
        if time.time() - cached["timestamp"] < 86400:  # 24小时
            print(f"✅ 使用缓存的群信息: {cached['name']} (缓存命中)")
            return cached["name"]
        else:
            print(f"⏰ 群信息缓存已过期，重新获取: {conversation_id}")

    # 优先级3: 调用 API（并缓存结果）
    print(f"📡 调用 API 获取群信息: {conversation_id}")
    info = await card_helper.get_group_info(conversation_id)
    group_name = info.title if info and hasattr(info, 'title') else "Unknown Group"

    # 更新缓存
    group_info_cache[conversation_id] = {
        "name": group_name,
        "timestamp": time.time()
    }
    print(f"✅ 群信息已缓存: {group_name}")

    return group_name


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
        print(f"🔍 [调试] handle_gemini_stream 接收到的 content 参数: '{content}'")
        if image_data_list:
            print(f"🖼️ 收到图片数量: {len(image_data_list)}")

        raw_user_content = content

        session_key = get_session_key(conversation_id, incoming_message.sender_id)
        use_openclaw_backend = AI_BACKEND == "openclaw"

        # 获取完整历史记录
        full_history = get_history(session_key)

        # 智能“历史引用”注入（仅用于本次 AI 请求，不写入历史）
        if DINGTALK_REFERENCE_AUTO_ENABLED:
            injected_content, quote = maybe_inject_reference(
                user_content=raw_user_content,
                history=full_history,
            )
            if quote:
                print(f"🧷 [引用] 已注入引用: {quote}")
            content = injected_content

        if use_openclaw_backend:
            # OpenClaw 模式：仅透传轻量上下文，避免与 Gateway 的 agent/system 策略冲突
            if OPENCLAW_CONTEXT_MESSAGES > 0 and len(full_history) > OPENCLAW_CONTEXT_MESSAGES:
                history_messages = full_history[-OPENCLAW_CONTEXT_MESSAGES:]
            else:
                history_messages = full_history if OPENCLAW_CONTEXT_MESSAGES > 0 else []

            messages = []
            for msg in history_messages:
                role = msg.get("role")
                msg_content = msg.get("content", "")
                if role in {"user", "assistant"} and msg_content:
                    messages.append({"role": role, "content": msg_content})

            sender_nick = incoming_message.sender_nick or "User"
            if image_data_list:
                text_content = f"{sender_nick}: [图片x{len(image_data_list)}] {content}".strip()

                if OPENCLAW_GATEWAY_TRANSPORT != "ws":
                    # HTTP(OpenAI-compatible) 路径默认按“无多模态”处理：
                    # 先用 tools-invoke 产出文字描述，再仅发送纯文本消息给 Gateway。
                    vision_sections = []
                    if OPENCLAW_TOOLS_URL and OPENCLAW_TOOLS_TOKEN and OPENCLAW_VISION_TOOL_NAME:
                        max_images = min(len(image_data_list), 3)
                        for idx, img in enumerate(image_data_list[:max_images], start=1):
                            try:
                                tool_res = await invoke_tool(
                                    tools_url=OPENCLAW_TOOLS_URL,
                                    token=OPENCLAW_TOOLS_TOKEN,
                                    tool_name=OPENCLAW_VISION_TOOL_NAME,
                                    arguments=build_vision_arguments(
                                        img,
                                        filename=f"image_{idx}.jpg",
                                        prompt=content or "",
                                    ),
                                    session_key=f"dingtalk:{incoming_message.conversation_id}:{incoming_message.sender_id}",
                                )
                                result_obj = tool_res.get("result") if isinstance(tool_res, dict) else None
                                vision_text = ""
                                if isinstance(result_obj, dict):
                                    vision_text = (result_obj.get("text") or result_obj.get("content") or "").strip()
                                elif isinstance(result_obj, str):
                                    vision_text = result_obj.strip()

                                if vision_text:
                                    vision_sections.append(f"[图片{idx}识别结果]\n{vision_text}")
                                else:
                                    vision_sections.append(f"[图片{idx}识别结果]\n(空结果)")
                            except Exception as e:
                                vision_sections.append(f"[图片{idx}识别失败]\n{e}")
                    else:
                        vision_sections.append(
                            "[系统]\n未配置 OPENCLAW_TOOLS_URL / OPENCLAW_TOOLS_TOKEN / OPENCLAW_VISION_TOOL_NAME，无法识别图片。"
                        )

                    vision_block = "\n\n".join(vision_sections).strip()
                    if vision_block:
                        text_content += f"\n\n{vision_block}"
                messages.append({"role": "user", "content": text_content})
            else:
                text_content = f"{sender_nick}: {content}"
                messages.append({"role": "user", "content": text_content})

            print(f"🔍 [OpenClaw] 透传历史条数: {len(messages) - 1}, 当前消息已附加")
        else:
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

            # 根据 AI_BACKEND 动态设置 bot 名称
            bot_name = {"gemini": "Gem", "openclaw": "Claw"}.get(AI_BACKEND, "Gem")

            system_prompt = f"""你是 {bot_name}，一个有帮助的 AI 助手。你的回答应该准确，不要产生幻觉。

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
- AI 回复可能带有来源标签 [Gem] 或 [Claw]，表示由不同 AI 助手生成。
- 你是 {bot_name}，回复不需要添加来源标签。

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

地理和时区规则:
- 默认按北京时间 (Asia/Shanghai, UTC+8) 回答时间相关问题。
- 用户未明确给出城市时，默认按中国大陆场景理解，并优先追问具体城市。
- 不要仅依据 IP/代理/VPN 推断用户在海外；若定位冲突，以用户明确地点为准。

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
                msg_content = msg.get("content", "")  # 改为 msg_content，避免覆盖参数 content
                timestamp = msg.get("timestamp")
                sender_nick_from_history = msg.get("sender_nick")

                # 如果有时间戳，添加到内容前面
                if timestamp and msg["role"] == "user":
                    # 用户消息格式: [时间] 昵称: 内容
                    # 如果 msg_content 已经包含昵称（旧数据），则不再拼接
                    if sender_nick_from_history and not msg_content.startswith(f"{sender_nick_from_history}:"):
                        formatted_msg["content"] = f"[{timestamp}] {sender_nick_from_history}: {msg_content}"
                    else:
                        formatted_msg["content"] = f"[{timestamp}] {msg_content}"
                elif msg["role"] == "assistant" and msg.get("bot_id"):
                    # assistant 消息有 bot_id 时，加来源标签
                    msg_bot_id = msg["bot_id"]
                    bot_label = {"gemini": "Gem", "openclaw": "Claw"}.get(msg_bot_id, msg_bot_id)
                    formatted_msg["content"] = f"[{bot_label}] {msg_content}"
                else:
                    formatted_msg["content"] = msg_content

                formatted_history.append(formatted_msg)

            if image_data_list:
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
                print(f"🔍 [调试] 构造当前消息 - sender_nick='{sender_nick}', content='{content}'")
                text_content = f"[{current_timestamp}] {sender_nick}: {content}"
                messages.extend(formatted_history)
                messages.append({"role": "user", "content": text_content})

                # 调试：打印发送给 Gemini 的完整消息
                print(f"🔍 [调试] 发送给 Gemini 的历史记录数量: {len(formatted_history)}")
                if formatted_history:
                    print(f"🔍 [调试] 最后一条历史: {formatted_history[-1].get('content', '')[:200]}")
                print(f"🔍 [调试] 当前消息: {text_content}")

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
            # OpenClaw 模式: Gateway 自行决定模型和 thinking，客户端无法控制
            target_model = "openclaw"
            thinking_level = "default"
            need_search = False
            print(f"🎯 OpenClaw 模式: 由 Gateway 处理")
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

        # “敲键盘”状态动画（通过 statusText 流式更新模拟，结束后清空）
        stop_typing = asyncio.Event()
        typing_task = None
        if DINGTALK_TYPING_ENABLED:
            frames = [x.strip() for x in (DINGTALK_TYPING_FRAMES_RAW or "").split("|") if x.strip()]
            if not frames:
                frames = ["⌨️ 正在敲键盘..."]

            async def _typing_loop():
                idx = 0
                interval_s = max(0.2, float(DINGTALK_TYPING_INTERVAL_MS) / 1000.0)
                while not stop_typing.is_set():
                    frame = frames[idx % len(frames)]
                    idx += 1
                    try:
                        await self.card_helper.stream_update(
                            out_track_id,
                            frame,
                            is_finalize=False,
                            is_full=True,
                            content_key="statusText",
                        )
                    except Exception:
                        pass
                    await asyncio.sleep(interval_s)

            typing_task = asyncio.create_task(_typing_loop())

        try:
            # 根据后端选择调用不同的 API
            if AI_BACKEND == "openclaw":
                from app.openclaw_client import call_openclaw_stream
                stream = call_openclaw_stream(
                    messages,
                    conversation_id=conversation_id,
                    sender_id=incoming_message.sender_id,
                    sender_nick=sender_name,
                    model=target_model,
                    image_data_list=image_data_list if image_data_list else None,
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

            # If the model produced an image generation result block, send image back to the same
            # conversation (single chat or group) as a native picture message.
            cleaned_text, image_gen_payload = _extract_image_gen_json_block(clean_response)
            sent_image_ok = False
            image_send_error = ""
            if image_gen_payload and isinstance(image_gen_payload.get("images"), list):
                try:
                    images = image_gen_payload.get("images") or []
                    first = images[0] if images else None
                    b64 = (first or {}).get("base64") if isinstance(first, dict) else None
                    if isinstance(b64, str) and b64.strip():
                        image_bytes = base64.b64decode(b64)
                        media_id = await self.card_helper.upload_media(
                            image_bytes,
                            filetype="image",
                            filename="image.png",
                            mimetype="image/png",
                        )
                        if media_id:
                            msg_param = (DINGTALK_IMAGE_MSG_PARAM_TEMPLATE or "").replace(
                                "{mediaId}",
                                media_id,
                            )
                            if incoming_message.conversation_type == "2":
                                sent_image_ok = await self.card_helper.send_group_message(
                                    incoming_message.conversation_id,
                                    DINGTALK_IMAGE_MSG_KEY,
                                    msg_param,
                                )
                            else:
                                sent_image_ok = await self.card_helper.send_private_chat_message(
                                    incoming_message.conversation_id,
                                    DINGTALK_IMAGE_MSG_KEY,
                                    msg_param,
                                )
                        else:
                            image_send_error = "upload_media 返回为空"
                    else:
                        image_send_error = "images[0].base64 为空"
                except Exception as e:
                    image_send_error = str(e)

                # Avoid storing huge base64 blocks in history/UI.
                if cleaned_text:
                    clean_response = cleaned_text
                if sent_image_ok:
                    clean_response = (clean_response + "\n\n[已发送图片]").strip()
                else:
                    clean_response = (clean_response + f"\n\n[图片发送失败] {image_send_error}").strip()

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
            # Gateway 返回的 model: Gemini 返回实际模型名，OpenClaw 固定返回 "openclaw"
            # OpenClaw 模式不显示模型名（因为返回的是 agent ID，不是实际模型）
            if AI_BACKEND == "openclaw":
                search_icon = "🌐" if need_search else ""
                status_text += f"\n\n<font color='#808080' size='2'>🧠 {thinking_level} {search_icon}</font>"
            else:
                if usage_info and usage_info.get("model"):
                    actual_model = usage_info["model"]
                    model_short = actual_model.replace("gemini-", "").replace("-preview", "")
                else:
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
                        "params": {"url": "dtmd://dingtalkclient/sendMessage?content=" + (raw_user_content or "重试")}
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

            # 记录历史：现在同时保存用户消息和助手消息
            sender_nick = incoming_message.sender_nick or "User"
            history_content = raw_user_content
            if image_data_list:
                history_content += f" [图片x{len(image_data_list)}]"
            # Store the cleaned response so we don't persist base64 blobs.
            update_history(
                session_key,
                user_msg=history_content,
                assistant_msg=clean_response,
                sender_nick=sender_nick,
            )
            
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
                    "[🔄 重试](dtmd://dingtalkclient/sendMessage?content=" + (raw_user_content or "重试") + ") | "
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
        finally:
            stop_typing.set()
            if typing_task:
                try:
                    await asyncio.wait_for(typing_task, timeout=1.0)
                except Exception:
                    pass
            # 清空打字状态，避免残留
            try:
                await self.card_helper.stream_update(
                    out_track_id,
                    "",
                    is_finalize=False,
                    is_full=True,
                    content_key="statusText",
                )
            except Exception:
                pass

    async def process_buffered_messages(self, buffer_key):
        try:
            await asyncio.sleep(2.0)
        except asyncio.CancelledError:
            print(f"⏹️ 定时器被取消（缓冲 {buffer_key[-8:]}），消息已合并到新缓冲区")
            return

        # 获取或创建会话锁（在 sleep 之前检查，避免重复处理）
        if buffer_key not in session_locks:
            session_locks[buffer_key] = asyncio.Lock()

        async with session_locks[buffer_key]:
            # 再次检查，防止在等待锁期间被其他任务处理
            if buffer_key not in message_buffer:
                print(f"⚠️ 缓冲区已被其他任务处理: {buffer_key[-8:]}")
                return

            # 标记正在处理
            processing_sessions.add(buffer_key)

            try:
                data = message_buffer.pop(buffer_key)
                content_list = data["content"]
                image_list = data["images"]
                incoming_message = data["incoming_message"]
                at_user_ids = data["at_user_ids"]

                full_content = "\n".join(content_list)

                print(f"📦 [Buffer] 合并了 {len(content_list)} 条消息 (用户: {incoming_message.sender_nick}): {content_list}")

                # 如果只有图片没有文字，使用默认提示
                if not full_content and image_list:
                    full_content = "请详细描述这张图片的内容，包括主要元素、场景、文字等信息。"

                sender_nick = incoming_message.sender_nick or "User"
                history_content = full_content
                if image_list:
                    history_content += f" [图片x{len(image_list)}]"

                # 获取群信息 (只获取群名，优先使用缓存)
                group_info = None
                if incoming_message.conversation_type == '2':  # 群聊
                    group_name = await get_cached_group_info(
                        self.card_helper,
                        incoming_message.conversation_id,
                        incoming_message
                    )

                    if group_name:
                        group_info = {'name': group_name}

                # 不再提前保存用户消息，延迟到 AI 回复后保存（避免历史记录中包含当前正在处理的消息）
                print(f"📥 [DingTalk Stream] 处理合并消息: {history_content} (User: {sender_nick})")

                await self.handle_gemini_stream(incoming_message, full_content, incoming_message.conversation_id, at_user_ids, image_list, group_info)
            finally:
                # 清除正在处理标记
                processing_sessions.discard(buffer_key)

    async def process(self, callback: dingtalk_stream.CallbackMessage):
        try:
            incoming_message = dingtalk_stream.ChatbotMessage.from_dict(callback.data)

            # 消息去重：检查是否已处理过此消息
            message_id = incoming_message.message_id
            print(f"🔍 [去重调试] message_id={message_id}, type={type(message_id)}")
            if message_id and _is_message_processed(message_id):
                print(f"⚠️ [去重] 消息已处理过，跳过: {message_id}")
                return AckMessage.STATUS_OK, 'OK'
            elif message_id:
                print(f"✅ [去重] 新消息，已加入缓存: {message_id}")
            else:
                print(f"⚠️ [去重警告] message_id 为空，无法去重！")

            msg_type = incoming_message.message_type
            content = ""
            image_data_list = [] 
            file_bytes = None
            file_name = ""
            
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
            elif msg_type in {"audio", "file"}:
                # dingtalk_stream SDK 未内置解析 audio/file，content 会落在 extensions["content"]
                raw_content = incoming_message.extensions.get("content")
                if not isinstance(raw_content, dict):
                    raw_content = {}
                download_code = (
                    raw_content.get("downloadCode")
                    or raw_content.get("download_code")
                    or raw_content.get("download_code".title())
                )
                file_name = (
                    raw_content.get("fileName")
                    or raw_content.get("filename")
                    or raw_content.get("name")
                    or msg_type
                )
                if download_code:
                    print(f"📥 收到 {msg_type} 消息，正在下载... Code: {download_code}")
                    file_bytes = await self.card_helper.download_file(download_code)
                if not file_bytes:
                    content = f"[{msg_type} 下载失败]"
                else:
                    # 优先通过 OpenClaw Tools 做 ASR/文件摘要（不依赖 chat prompt）
                    if msg_type == "audio":
                        tool_res = await invoke_tool(
                            tools_url=OPENCLAW_TOOLS_URL,
                            token=OPENCLAW_TOOLS_TOKEN,
                            tool_name=OPENCLAW_ASR_TOOL_NAME,
                            arguments=build_asr_arguments(file_bytes, filename=file_name or "audio"),
                            session_key=f"dingtalk:{incoming_message.conversation_id}:{incoming_message.sender_id}",
                        )
                        result_obj = tool_res.get("result") if isinstance(tool_res, dict) else None
                        if isinstance(result_obj, dict):
                            transcript = result_obj.get("text") or result_obj.get("content")
                        elif isinstance(result_obj, str):
                            transcript = result_obj
                        else:
                            transcript = tool_res.get("text") if isinstance(tool_res, dict) else None
                        content = (transcript or "").strip() or "语音已收到，但转写失败，请稍后重试。"
                    else:
                        tool_res = await invoke_tool(
                            tools_url=OPENCLAW_TOOLS_URL,
                            token=OPENCLAW_TOOLS_TOKEN,
                            tool_name=OPENCLAW_FILE_TOOL_NAME,
                            arguments=build_file_arguments(file_bytes, filename=file_name or "file"),
                            session_key=f"dingtalk:{incoming_message.conversation_id}:{incoming_message.sender_id}",
                        )
                        result_obj = tool_res.get("result") if isinstance(tool_res, dict) else None
                        if isinstance(result_obj, dict):
                            summary = result_obj.get("summary") or result_obj.get("text") or result_obj.get("content")
                        elif isinstance(result_obj, str):
                            summary = result_obj
                        else:
                            summary = tool_res.get("summary") if isinstance(tool_res, dict) else None
                        content = (summary or "").strip() or f"已收到文件：{file_name}，但解析失败，请稍后重试。"
            
            if not content and not image_data_list:
                return AckMessage.STATUS_OK, 'OK'

            sender_id = incoming_message.sender_staff_id or incoming_message.sender_id
            conversation_id = incoming_message.conversation_id
            session_key = get_session_key(conversation_id, sender_id)
            # 缓冲区使用独立的 key（含 sender_id），避免群聊中不同用户的消息被合并
            buffer_key = f"{session_key}_{sender_id}"

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

            # 消息缓冲逻辑 (使用 buffer_key 隔离不同用户)
            if buffer_key in message_buffer:
                # 已有缓冲区: 取消旧 timer，追加新消息
                existing_timer = message_buffer[buffer_key].get("timer")
                if existing_timer is not None:
                    existing_timer.cancel()
            else:
                # 新建缓冲区
                message_buffer[buffer_key] = {
                    "content": [],
                    "images": [],
                    "incoming_message": incoming_message,
                    "at_user_ids": at_user_ids,
                    "timer": None
                }

            # 追加消息内容
            if content:
                message_buffer[buffer_key]["content"].append(content)
            if image_data_list:
                message_buffer[buffer_key]["images"].extend(image_data_list)

            # 更新元数据 (使用最新消息的上下文)
            message_buffer[buffer_key]["incoming_message"] = incoming_message
            message_buffer[buffer_key]["at_user_ids"] = at_user_ids

            # 启动/重启计时器
            task = asyncio.create_task(self.process_buffered_messages(buffer_key))
            message_buffer[buffer_key]["timer"] = task

        except Exception as e:
            print(f"💥 [DingTalk Stream] Process 异常: {e}")

        return AckMessage.STATUS_OK, 'OK'
