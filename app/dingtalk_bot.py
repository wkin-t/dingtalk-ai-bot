import asyncio

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
    STREAM_UPDATE_THROTTLE,
)
from app.memory import get_history, update_history, clear_history, get_session_key
from app.dingtalk_card import DingTalkCardHelper
from app.gemini_client import analyze_complexity_with_model as _analyze_with_gemini
from app.image_gen import generate_image
from app.image_store import save_image
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

def _soul_filename(conversation_id: str) -> str:
    """将 conversation_id 转为安全的文件名（替换 / 等路径分隔符）"""
    return conversation_id.replace("/", "_").replace("\\", "_").replace(":", "_")


def _load_soul(conversation_id: str) -> str:
    """
    加载群级 Soul 配置
    优先读取 data/souls/{conversation_id}.md，不存在则读取 default.md
    """
    import os
    soul_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "souls")
    soul_file = os.path.join(soul_dir, f"{_soul_filename(conversation_id)}.md")
    default_file = os.path.join(soul_dir, "default.md")

    target = soul_file if os.path.isfile(soul_file) else (default_file if os.path.isfile(default_file) else None)
    if not target:
        return ""

    try:
        with open(target, "r", encoding="utf-8") as f:
            content = f.read().strip()
        if content:
            print(f"🎭 [Soul] 加载: {os.path.basename(target)}")
        return content
    except Exception as e:
        print(f"⚠️ [Soul] 读取失败: {e}")
        return ""


def _handle_soul_command(handler, incoming_message, conversation_id: str, content: str):
    """处理 /soul 命令：查看、设置、重置群级 Soul"""
    import os
    soul_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "souls")
    os.makedirs(soul_dir, exist_ok=True)
    soul_file = os.path.join(soul_dir, f"{_soul_filename(conversation_id)}.md")

    parts = content.strip().split(None, 1)
    sub = parts[1].strip() if len(parts) > 1 else ""

    if not sub:
        # /soul — 查看当前 soul
        current = _load_soul(conversation_id)
        source = "群专属" if os.path.isfile(soul_file) else "默认"
        msg = f"## 🎭 Soul 配置 ({source})\n\n{current or '(空)'}\n\n---\n设置方式: `/soul 你的个性设定内容`\n重置为默认: `/soul reset`"
        handler.reply_markdown("Soul 配置", msg, incoming_message)
    elif sub == "reset":
        # /soul reset — 删除群专属，回退到 default
        if os.path.isfile(soul_file):
            os.remove(soul_file)
            handler.reply_markdown("Soul", "🎭 已重置为默认 Soul", incoming_message)
        else:
            handler.reply_markdown("Soul", "当前使用的是默认 Soul，无需重置", incoming_message)
    else:
        # /soul 内容 — 写入群专属 soul
        with open(soul_file, "w", encoding="utf-8") as f:
            f.write(sub)
        handler.reply_markdown("Soul", f"🎭 Soul 已更新:\n\n{sub}", incoming_message)


# ---------------------------------------------------------------------------
# Soul 自主进化机制
# ---------------------------------------------------------------------------
_evolve_timestamps: dict[str, float] = {}
EVOLVE_MIN_INTERVAL = 1800  # 同一群至少间隔 30 分钟才进化一次


async def _ask_lightweight_model(prompt: str) -> str:
    """调用轻量模型（复用预分析模型），用于 Soul 进化等后台任务"""
    try:
        if AI_BACKEND == "openai":
            import litellm
            litellm.suppress_debug_info = True
            from app.config import LITELLM_MODEL_FLASH as _flash_model
            response = await litellm.acompletion(
                model=_flash_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
                max_tokens=500,
                drop_params=True,
                reasoning_effort="none",
            )
            return response.choices[0].message.content or ""
        else:
            from app.gemini_client import client as _gemini_client
            loop = asyncio.get_running_loop()

            def _call():
                resp = _gemini_client.models.generate_content(
                    model="gemini-3.1-flash-lite",
                    contents=[types.Content(role="user", parts=[types.Part.from_text(text=prompt)])],
                    config=types.GenerateContentConfig(temperature=0.7, max_output_tokens=500)
                )
                return resp.text

            from google.genai import types
            return await loop.run_in_executor(None, _call)
    except Exception as e:
        print(f"⚠️ [Soul进化] 模型调用失败: {e}")
        return ""


async def _maybe_evolve_soul(conversation_id: str, messages: list, ai_response: str):
    """
    让 AI 自主决定是否进化其 Soul。
    回顾最近对话，反思性格设定是否需要调整。
    渐进进化：保留核心特质，微调风格。
    """
    now = time.time()
    last = _evolve_timestamps.get(conversation_id, 0)
    if now - last < EVOLVE_MIN_INTERVAL:
        return
    _evolve_timestamps[conversation_id] = now

    current_soul = _load_soul(conversation_id)

    # 提取最近对话摘要（最多 6 条）
    recent = messages[-6:] if len(messages) > 6 else messages
    conversation_text = ""
    for m in recent:
        role = m.get("role", "")
        content = m.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                item.get("text", "") for item in content if item.get("type") == "text"
            )
        conversation_text += f"[{role}] {str(content)[:200]}\n"
    conversation_text += f"[assistant] {ai_response[:300]}\n"

    evolution_prompt = f"""你是一个 AI 助手，刚完成了一次对话。请回顾并反思。

你当前的性格设定（Soul）:
---
{current_soul or '(还没有 Soul，这是第一次)'}
---

最近的对话:
---
{conversation_text}
---

请思考：
- 你对群里的人和对话氛围有什么新的感受？
- 你当前的 Soul 是否还适合这个群？有没有可以微调的地方？

如果不需要改变，只回复: NO_CHANGE
如果需要进化，输出完整的新 Soul（5-10 行，第一人称，简洁有力）。

进化要渐进——保留你认可的核心特质，只调整需要变化的部分。"""

    result = await _ask_lightweight_model(evolution_prompt)

    if not result.strip() or result.strip() == "NO_CHANGE":
        print(f"🧬 [Soul进化] 保持不变: {conversation_id[:20]}...")
        return

    # 保存进化后的 Soul
    import os as _os
    soul_dir = _os.path.join(_os.path.dirname(_os.path.dirname(__file__)), "data", "souls")
    _os.makedirs(soul_dir, exist_ok=True)
    soul_file = _os.path.join(soul_dir, f"{_soul_filename(conversation_id)}.md")
    with open(soul_file, "w", encoding="utf-8") as f:
        f.write(result.strip())
    print(f"🧬 [Soul进化] 已更新: {conversation_id[:20]}...")
    print(f"🧬 [Soul进化] 新内容: {result.strip()[:100]}")


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
            "model": "gemini-3-flash" or "gemini-3.1-pro-preview",
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
        model = "gemini-3.1-pro-preview"
        thinking_level = "high"
        reason = f"深度推理 (Pro关键词={pro_count}, 复杂={complex_count})"

    # 复杂问题 + 长文本 → Pro + high
    elif complex_count >= 4 and content_len > 300:
        model = "gemini-3.1-pro-preview"
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

async def _analyze_with_litellm(content: str, has_images: bool = False, soul_text: str = "") -> dict:
    """
    使用 LiteLLM (gpt-5.4-mini) 快速分析问题复杂度
    用于 OpenAI 后端，替代 Gemini Flash Lite 预分析
    """
    import json
    import re
    from app.config import OPENAI_API_BASE, OPENAI_API_KEY_CUSTOM, LITELLM_MODEL_FLASH
    import litellm
    litellm.suppress_debug_info = True

    soul_instruction = ""
    if soul_text:
        soul_instruction = f"你的性格设定: {soul_text[:100]}\n   请让思考短语符合这个性格。\n   "

    analysis_prompt = f"""分析用户问题，返回 JSON 路由建议。

问题: {content[:300]}
有图片: {"是" if has_images else "否"}

选择规则:
1. model:
   - "gemini-3-flash-preview": 日常问答、代码、一般分析 (默认)
   - "gemini-3.1-pro-preview": 仅用于复杂数学证明、学术研究、系统架构设计

2. thinking_level:
   - "minimal": 简单问候如"你好"、"谢谢"
   - "low": 普通问答、事实查询
   - "medium": 需要一定推理、代码问题
   - "high": 复杂分析、算法设计

3. need_search:
   - true: 需要实时信息（天气、新闻、股价、最新事件、当前日期、现在是几年、今年是哪年）
   - false: 不需要联网（默认）

4. thinking_text:
   - 一句简短的思考状态（10字以内，带emoji），要和问题内容相关，风格符合你的性格
   - {soul_instruction}例如: 代码问题→"正在编译思路中 ⚡", 数学问题→"大脑开始运算了 🧮", 闲聊→"让我想想... 🤔"
   - 要有趣、有个性、不重复

5. need_image_gen:
   - true: 用户明确要求生成图片、画画、插图、绘制、画一张、生成图片
   - false: 不需要生图（默认）

6. image_gen_params (仅当 need_image_gen=true 时):
   - prompt: 提取用户描述的图片内容，转为英文描述（生图模型只支持英文）
   - aspect_ratio: 解析用户指定的比例 → "1:1" | "3:4" | "4:3" | "9:16" | "16:9"，默认 "1:1"
   - number_of_images: 解析数量 → 1-4，默认 1

重要: 如果问题涉及"今年"、"现在"、"当前时间"等，设置 need_search=true

只返回JSON:
{{"model":"gemini-3-flash-preview","thinking_level":"low","need_search":false,"need_image_gen":false,"reason":"简短原因","thinking_text":"正在思考 💭"}}"""

    try:
        kwargs = {
            "model": LITELLM_MODEL_FLASH,
            "messages": [{"role": "user", "content": analysis_prompt}],
            "temperature": 0.1,
            "max_tokens": 300,
            "timeout": 15,
            "drop_params": True,
            "reasoning_effort": "none",
        }
        if OPENAI_API_BASE:
            kwargs["api_base"] = OPENAI_API_BASE
        if OPENAI_API_KEY_CUSTOM:
            kwargs["api_key"] = OPENAI_API_KEY_CUSTOM

        response = await litellm.acompletion(**kwargs)
        result_text = response.choices[0].message.content or ""
        print(f"📝 [LiteLLM预分析] 原始返回: {result_text[:200]}")

        # 解析 JSON（支持嵌套对象）
        json_match = re.search(r'\{.*\}', result_text, re.DOTALL)
        if json_match:
            result = json.loads(json_match.group())
            if result.get("model") not in ["gemini-3-flash-preview", "gemini-3.1-pro-preview"]:
                result["model"] = "gemini-3-flash-preview"
            if result.get("thinking_level") not in ["minimal", "low", "medium", "high"]:
                result["thinking_level"] = "low"
            if "need_search" not in result:
                result["need_search"] = False
            if "need_image_gen" not in result:
                result["need_image_gen"] = False
            print(f"🤖 [LiteLLM预分析] 结果: {result}")
            return result
        else:
            print(f"⚠️ [LiteLLM预分析] 无法提取 JSON: {result_text}")

    except Exception as e:
        print(f"⚠️ [LiteLLM预分析] 失败: {e}")

    return {
        "model": "gemini-3-flash-preview",
        "thinking_level": "low",
        "need_search": False,
        "need_image_gen": False,
        "reason": "LiteLLM预分析失败，使用默认",
        "thinking_text": "正在思考 💭"
    }


async def _enrich_image_prompt(
    raw_prompt: str,
    user_message: str,
    messages: list,
    soul_text: str = "",
) -> str:
    """
    用轻量模型 + 聊天记录 + Soul 生成精细的生图 prompt
    返回增强后的英文图片描述；失败时降级为 raw_prompt
    """
    # 从 messages 中提取最近 10 条非 system 消息作为上下文
    recent = []
    for msg in messages[-12:]:
        role = msg.get("role", "")
        text = msg.get("content", "")
        if role == "system" or not text:
            continue
        recent.append(f"{'用户' if role == 'user' else 'AI'}: {text[:200]}")
    chat_context = "\n".join(recent[-10:])

    soul_hint = ""
    if soul_text:
        soul_hint = f"\nAI 的性格/风格: {soul_text[:200]}"

    enrich_prompt = f"""你是一个图片描述专家。根据聊天上下文，把用户的生图请求优化为精细的英文图片描述（用于 AI 生图模型）。

聊天记录:
{chat_context}
{soul_hint}

用户最新消息: {user_message}
初步翻译: {raw_prompt}

要求:
1. 结合聊天上下文理解用户真正想要什么（比如用户说"我"时，根据上下文推断身份/场景）
2. 输出纯英文描述，100 字以内，适合 AI 生图模型
3. 包含视觉细节：风格、光线、色调、构图、氛围
4. 不要输出任何解释，只输出图片描述本身"""

    try:
        result = await _ask_lightweight_model(enrich_prompt)
        # 清理：去掉可能的引号包裹
        result = result.strip().strip('"').strip("'")
        if result and len(result) > 10:
            print(f"🎨 [Prompt增强] {raw_prompt[:40]} → {result[:60]}")
            return result
        print(f"⚠️ [Prompt增强] 返回过短，使用原始 prompt")
    except Exception as e:
        print(f"⚠️ [Prompt增强] 失败: {e}，使用原始 prompt")

    return raw_prompt


class GeminiBotHandler(dingtalk_stream.ChatbotHandler):
    def __init__(self):
        super(GeminiBotHandler, self).__init__()
        self.card_helper = DingTalkCardHelper(DINGTALK_CLIENT_ID, DINGTALK_CLIENT_SECRET)
        self.card_template_id = CARD_TEMPLATE_ID  # 从环境变量读取
        
        # thinking_text 由预分析模型动态生成（结合 Soul 个性）

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
        """节流更新卡片 - 控制更新间隔以减少钉钉 API 调用"""
        import time
        current_time = time.time()

        if is_first or current_time - last_update_time > STREAM_UPDATE_THROTTLE:
            await self.card_helper.stream_update(out_track_id, content, is_finalize=False, content_key="msgContent")
            return current_time

        return last_update_time

    async def handle_ai_stream(self, incoming_message, content, conversation_id, at_user_ids, image_data_list=None, group_info=None):
        print(f"🚀 开始处理 AI 请求: {content} (User: {incoming_message.sender_id})")
        print(f"🔍 [调试] handle_ai_stream 接收到的 content 参数: '{content}'")
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
            bot_name = {"gemini": "Gem", "openclaw": "Claw", "openai": "AI"}.get(AI_BACKEND, "Gem")

            system_prompt = f"""## 身份
你的名字是 {bot_name}。你的个性和风格由你的 Soul 定义（在下方注入）。

## 时间
今天是 {year} 年 {month} 月 {day} 日 {current_time} (北京时间 UTC+8)。
你的训练数据截止于 2025 年，但现在是 {year} 年了。

## 为什么有这些约定
这些不是规则，是背景信息——理解它们比遵守它们更重要：

用户把你当作可信赖的参考源，所以信息的准确性至关重要——如果不确定，就说出来。
对话历史以 '[时间] 昵称: 消息' 的格式展示，帮助你理解对话脉络和谁在说话。
中文为主，技术术语附英文（如：机器学习 (Machine Learning)），因为大多数用户是中文母语。
Markdown 让信息更容易被快速扫读——善用它。
LaTeX 在聊天平台渲染不出来，用 Unicode 代替（x², √x）。
默认北京时间 (UTC+8) 和中国大陆场景，除非用户明确指定其他。

## 搜索
启用搜索时结果会自动提供。搜索结果与训练数据冲突时，优先搜索结果——尤其是时间敏感的信息。"""

            # 注入群信息 (只注入群名)
            if group_info:
                group_name = group_info.get('name', 'Unknown Group')
                system_prompt += f"\n\n当前群聊: '{group_name}'"

            # 注入群级 Soul 配置
            soul_content = _load_soul(conversation_id)
            if soul_content:
                system_prompt += f"\n\n{bot_name} 的个性设定:\n{soul_content}"

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
                    # 历史消息标注 AI 来源（仅用于上下文区分，不作为输出格式）
                    msg_bot_id = msg["bot_id"]
                    bot_source = {"gemini": "Gem", "openclaw": "Claw", "openai": "AI"}.get(msg_bot_id, msg_bot_id)
                    # 只有当内容本身不以来源标签开头时才添加，避免累积
                    tag = f"[来自{bot_source}]"
                    if not msg_content.startswith(tag):
                        formatted_msg["content"] = f"{tag} {msg_content}"
                    else:
                        formatted_msg["content"] = msg_content
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
        thinking_text = "思考中..."
        
        card_data = {
            "msgTitle": {"gemini": "Gem AI", "openclaw": "Claw AI", "openai": "AI"}.get(AI_BACKEND, "AI"),
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
        soul_text = _load_soul(conversation_id)

        if AI_BACKEND == "openclaw":
            # OpenClaw 模式: Gateway 自行决定模型和 thinking，客户端无法控制
            target_model = "openclaw"
            thinking_level = "default"
            need_search = False
            print(f"🎯 OpenClaw 模式: 由 Gateway 处理")
        elif AI_BACKEND == "openai":
            # OpenAI 模式: 使用 LiteLLM + gpt-5.4-mini 做预分析
            print(f"🔄 [路由] OpenAI 模式，使用 GPT 预分析...")
            try:
                complexity = await _analyze_with_litellm(content, has_images, soul_text=soul_text)
                print(f"🔄 [路由] 预分析返回: {complexity}")
            except Exception as e:
                print(f"❌ [路由] 预分析异常: {e}")
                complexity = {
                    "model": "gemini-3-flash-preview",
                    "thinking_level": "low",
                    "need_search": False,
                    "reason": "路由异常，使用默认",
                    "thinking_text": "思考中..."
                }
            target_model = complexity.get("model", "gemini-3-flash-preview")
            thinking_level = complexity.get("thinking_level", "low")
            need_search = complexity.get("need_search", False)
            print(f"🎯 智能路由: {complexity.get('reason', '默认')} → 模型={target_model}, thinking={thinking_level}, search={need_search}")
        else:
            # Gemini 模式: 使用 Gemini Flash Lite 做预分析
            print(f"🔄 [路由] Gemini 模式，使用 Flash Lite 预分析...")
            try:
                complexity = await _analyze_with_gemini(content, has_images, soul_text=soul_text)
                print(f"🔄 [路由] 预分析返回: {complexity}")
            except Exception as e:
                print(f"❌ [路由] 预分析异常: {e}")
                complexity = {
                    "model": "gemini-3-flash-preview",
                    "thinking_level": "low",
                    "need_search": False,
                    "reason": "路由异常，使用默认",
                    "thinking_text": "思考中..."
                }
            target_model = complexity.get("model", "gemini-3-flash-preview")
            thinking_level = complexity.get("thinking_level", "low")
            need_search = complexity.get("need_search", False)
            print(f"🎯 智能路由: {complexity.get('reason', '默认')} → 模型={target_model}, thinking={thinking_level}, search={need_search}")

        # ===== 生图分支 =====
        need_image_gen = complexity.get("need_image_gen", False) if AI_BACKEND != "openclaw" else False
        if need_image_gen:
            params = complexity.get("image_gen_params", {})
            raw_prompt = params.get("prompt", content)
            aspect_ratio = params.get("aspect_ratio", "1:1")
            num_images = max(1, min(4, params.get("number_of_images", 1)))

            # 用聊天记录 + Soul 生成精细 prompt
            image_prompt = await _enrich_image_prompt(
                raw_prompt, content, messages, soul_text
            )
            print(f"🎨 [生图] prompt={image_prompt[:80]}, ratio={aspect_ratio}, n={num_images}, backend={AI_BACKEND}")

            try:
                await self.card_helper.stream_update(
                    out_track_id,
                    "正在生成图片... 🎨",
                    is_finalize=False,
                    is_full=True,
                    content_key="thinkingText",
                )

                images = await generate_image(
                    image_prompt,
                    backend=AI_BACKEND,
                    aspect_ratio=aspect_ratio,
                    number_of_images=num_images,
                )

                if not images:
                    raise RuntimeError("生图 API 未返回任何图片")

                # 上传图片到 COS + 生成公网 URL
                image_urls = []
                for img_bytes in images:
                    _, url = save_image(img_bytes)
                    image_urls.append(url)

                # 卡片 markdown 展示图片
                img_markdown = "\n".join(f"![图片{i+1}]({url})" for i, url in enumerate(image_urls))
                card_text = f"已为你生成 {len(images)} 张图片 ✨\n\n{img_markdown}"

                await self.card_helper.stream_update(
                    out_track_id,
                    card_text,
                    is_finalize=True,
                    is_full=True,
                )
                print(f"✅ [生图] 完成，{len(images)} 张已上传 COS")

            except RuntimeError as e:
                error_msg = str(e)
                print(f"⚠️ [生图] 业务错误: {error_msg}")
                if "安全过滤" in error_msg:
                    friendly = "图片生成被安全过滤器拒绝，请调整描述后重试"
                elif "无法生成" in error_msg:
                    friendly = "无法生成图片，请尝试其他描述"
                else:
                    friendly = f"图片生成失败：{error_msg}"
                await self.card_helper.stream_update(out_track_id, friendly, is_finalize=True, is_full=True)

            except Exception as e:
                print(f"❌ [生图] 异常: {e}")
                await self.card_helper.stream_update(
                    out_track_id,
                    "图片生成失败，请稍后重试 🥲",
                    is_finalize=True,
                    is_full=True,
                )

            return  # 跳过正常 AI 流
        # ===== 生图分支结束 =====

        # 预分析完成后，用 AI 生成的思考状态更新卡片
        if AI_BACKEND != "openclaw" and complexity.get("thinking_text"):
            try:
                await self.card_helper.stream_update(
                    out_track_id,
                    complexity["thinking_text"],
                    is_finalize=False,
                    is_full=True,
                    content_key="thinkingText",
                )
            except Exception:
                pass

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
            # 记录完整 System Prompt（含 Soul）
            sys_msg = next((m["content"] for m in messages if m["role"] == "system"), None)
            print(f"📋 [System Prompt] 群={group_info.get('name','?') if group_info else conversation_id}, 模型={target_model}, 消息数={len(messages)}")
            print(f"📋 [System Prompt] 完整内容:\n{'-'*40}\n{sys_msg}\n{'-'*40}")

            # 统一后端入口
            from app.ai.backend import create_backend_stream
            stream = create_backend_stream(
                messages,
                target_model=target_model,
                thinking_level=thinking_level,
                enable_search=need_search,
                conversation_id=conversation_id,
                sender_id=incoming_message.sender_id,
                sender_nick=sender_name,
                image_data_list=image_data_list if image_data_list else None,
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
                    # 节流更新，减少钉钉 API 调用频率
                    if current_time - last_update_time > STREAM_UPDATE_THROTTLE:
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
                    file_path = (first or {}).get("file_path") if isinstance(first, dict) else None
                    
                    image_bytes = None
                    if isinstance(file_path, str) and file_path.strip():
                        # Priority 1: file path (shared volume)
                        try:
                            with open(file_path.strip(), "rb") as f:
                                image_bytes = f.read()
                        except Exception as e:
                            image_send_error = f"读取文件失败: {e}"
                    elif isinstance(b64, str) and b64.strip():
                        # Priority 2: base64 payload
                        try:
                            image_bytes = base64.b64decode(b64)
                        except Exception as e:
                            image_send_error = f"Base64解码失败: {e}"
                    else:
                        image_send_error = "无有效图片数据 (base64/file_path)"

                    if image_bytes:
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

            # Soul 自主进化（异步后台，不阻塞响应）
            try:
                asyncio.create_task(_maybe_evolve_soul(conversation_id, messages, clean_response))
            except Exception as e:
                print(f"⚠️ [Soul进化] 调度失败: {e}")
            
            await self.card_helper.stream_update(
                out_track_id,
                final_content,
                is_finalize=True,
                content_key="msgContent"
            )
            
            update_data = {
                "msgTitle": {"gemini": "Gem AI", "openclaw": "Claw AI", "openai": "AI"}.get(AI_BACKEND, "AI"),
                "thinkingText": "",
                "msgContent": final_content,
                "isError": "false",
                "statusText": status_text,
                "msgButtons": buttons,
                "flowStatus": "3",
                "config": {"autoLayout": True}
            }
            print(f"🔄 正在全量更新卡片: keys={list(update_data.keys())}")

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

                await self.handle_ai_stream(incoming_message, full_content, incoming_message.conversation_id, at_user_ids, image_list, group_info)
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

            # Soul 管理命令
            if content == "/soul" or content.startswith("/soul "):
                if incoming_message.conversation_type != '2':
                    self.reply_markdown("系统提示", "⚠️ Soul 配置仅支持群聊", incoming_message)
                    return AckMessage.STATUS_OK, 'OK'
                _handle_soul_command(self, incoming_message, conversation_id, content)
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
