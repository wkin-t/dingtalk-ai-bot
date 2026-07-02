import os
from dotenv import load_dotenv

# 加载 .env 文件
load_dotenv()


def _get_int(name: str, default: int) -> int:
    """安全读取 int 环境变量。"""
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _get_float(name: str, default: float) -> float:
    """安全读取 float 环境变量。"""
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _get_bool(name: str, default: bool) -> bool:
    """安全读取 bool 环境变量。"""
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


# Google Gemini API Key
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Gemini API 中转地址（如 sub2api 的 /v1beta 原生协议层）。
# 非空时 gemini_client 的对话/搜索 client 打到该地址且不走 SOCKS_PROXY（中转站在本机/内网）；
# 生图等中转站不覆盖的端点仍直连 Google（见 gemini_client.direct_client）。
GEMINI_API_BASE = os.getenv("GEMINI_API_BASE", "").strip().rstrip("/")
# 中转站的认证 key（作为 x-goog-api-key 下发）；不设则复用 GEMINI_API_KEY
GEMINI_API_BASE_KEY = os.getenv("GEMINI_API_BASE_KEY", "").strip() or GEMINI_API_KEY

# 钉钉配置
DINGTALK_CLIENT_ID = os.getenv("DINGTALK_CLIENT_ID")
DINGTALK_CLIENT_SECRET = os.getenv("DINGTALK_CLIENT_SECRET")
DINGTALK_CORP_ID = os.getenv("DINGTALK_CORP_ID")  # 新增 CorpId
DINGTALK_COOL_APP_CODE = os.getenv("DINGTALK_COOL_APP_CODE")  # 新增 CoolAppCode

# 钉钉 API 抗抖动配置
DINGTALK_FORCE_DIRECT = _get_bool("DINGTALK_FORCE_DIRECT", True)
DINGTALK_RETRY_ATTEMPTS = max(1, _get_int("DINGTALK_RETRY_ATTEMPTS", 5))
DINGTALK_RETRY_BASE_DELAY = max(0.1, _get_float("DINGTALK_RETRY_BASE_DELAY", 0.8))
DINGTALK_RETRY_MAX_DELAY = max(
    DINGTALK_RETRY_BASE_DELAY,
    _get_float("DINGTALK_RETRY_MAX_DELAY", 8.0),
)
DINGTALK_RETRY_JITTER = max(0.0, _get_float("DINGTALK_RETRY_JITTER", 0.35))
DINGTALK_CONNECT_TIMEOUT_MS = max(1000, _get_int("DINGTALK_CONNECT_TIMEOUT_MS", 15000))
DINGTALK_READ_TIMEOUT_MS = max(1000, _get_int("DINGTALK_READ_TIMEOUT_MS", 60000))
DINGTALK_RUNTIME_MAX_ATTEMPTS = max(1, _get_int("DINGTALK_RUNTIME_MAX_ATTEMPTS", 2))
DINGTALK_FILE_DOWNLOAD_TIMEOUT = max(5, _get_int("DINGTALK_FILE_DOWNLOAD_TIMEOUT", 30))
DINGTALK_TOKEN_EARLY_REFRESH_SEC = max(
    30,
    _get_int("DINGTALK_TOKEN_EARLY_REFRESH_SEC", 120),
)

# 企业微信机器人配置 (新)
WECOM_BOT_WEBHOOK_KEY = os.getenv("WECOM_BOT_WEBHOOK_KEY", "")
WECOM_BOT_WEBHOOK_URL = os.getenv("WECOM_BOT_WEBHOOK_URL", "")
WECOM_BOT_TOKEN = os.getenv("WECOM_BOT_TOKEN", os.getenv("WECOM_TOKEN", ""))
WECOM_BOT_ENCODING_AES_KEY = os.getenv(
    "WECOM_BOT_ENCODING_AES_KEY",
    os.getenv("WECOM_ENCODING_AES_KEY", "")
)
WECOM_BOT_RECEIVE_ID = os.getenv("WECOM_BOT_RECEIVE_ID", "")
WECOM_BOT_REPLY_MODE = os.getenv("WECOM_BOT_REPLY_MODE", "response_url").strip().lower()
if WECOM_BOT_REPLY_MODE not in {"response_url", "passive_stream"}:
    WECOM_BOT_REPLY_MODE = "response_url"

WECOM_BOT_STREAM_STYLE = os.getenv("WECOM_BOT_STREAM_STYLE", "stream").strip().lower()
if WECOM_BOT_STREAM_STYLE not in {"stream", "stream_with_template_card"}:
    WECOM_BOT_STREAM_STYLE = "stream"

# 企业微信应用配置 (兼容旧配置，逐步废弃)
WECOM_CORP_ID = os.getenv("WECOM_CORP_ID", "")
WECOM_AGENT_ID = os.getenv("WECOM_AGENT_ID", "")
WECOM_SECRET = os.getenv("WECOM_SECRET", "")
WECOM_TOKEN = os.getenv("WECOM_TOKEN", WECOM_BOT_TOKEN)
WECOM_ENCODING_AES_KEY = os.getenv("WECOM_ENCODING_AES_KEY", WECOM_BOT_ENCODING_AES_KEY)

# platform 选择: dingtalk | wecom | both
PLATFORM = os.getenv("PLATFORM", "dingtalk")

# 代理设置 (优先读取环境变量)
# 默认假设 v2rayA 的 SOCKS5 端口是 1080
DEFAULT_PROXY_HOST = "172.16.0.8"
DEFAULT_SOCKS_PORT = "1080"

# 如果环境变量里配了 SOCKS_PROXY，读取它
# 仅用于 Gemini API，不设置全局代理 (避免影响钉钉等国内服务)
SOCKS_PROXY = os.getenv("SOCKS_PROXY", f"socks5h://{DEFAULT_PROXY_HOST}:{DEFAULT_SOCKS_PORT}")

# 注意: 不再设置全局 HTTP_PROXY/HTTPS_PROXY 环境变量
# 钉钉是国内服务，不需要代理；Gemini 在 SDK 中显式配置代理

# aiohttp 代理字符串 (aiohttp 不支持 socks5h 协议头，需要转为 socks5)
if SOCKS_PROXY.startswith("socks5h://"):
    AIOHTTP_PROXY = SOCKS_PROXY.replace("socks5h://", "socks5://")
else:
    AIOHTTP_PROXY = SOCKS_PROXY

# HTTP 代理 (用于不支持 SOCKS5 的 SDK，如 alibabacloud_dingtalk)
# v2rayA 的 HTTP 代理端口通常是 1087
HTTP_PROXY_URL = os.getenv("HTTP_PROXY_URL", "http://127.0.0.1:1087")

# httpx 代理配置 (用于 google-generativeai SDK)
# httpx 支持 socks5 但需要 httpx[socks] 依赖
HTTPX_PROXY = SOCKS_PROXY.replace("socks5h://", "socks5://")

# OpenClaw Gateway 配置
OPENCLAW_GATEWAY_URL = os.getenv("OPENCLAW_GATEWAY_URL", "ws://openclaw-gateway:18789")
OPENCLAW_GATEWAY_TOKEN = os.getenv("OPENCLAW_GATEWAY_TOKEN", "")
OPENCLAW_AGENT_ID = os.getenv("OPENCLAW_AGENT_ID", "default")
# OpenClaw Gateway transport for bot-to-gateway calls:
# - http: OpenAI-compatible /v1/chat/completions (our current default)
# - ws: Gateway WebSocket protocol (closer to official channel plugins; supports image attachments)
OPENCLAW_GATEWAY_TRANSPORT = os.getenv("OPENCLAW_GATEWAY_TRANSPORT", "http").strip().lower()
if OPENCLAW_GATEWAY_TRANSPORT not in {"http", "ws"}:
    OPENCLAW_GATEWAY_TRANSPORT = "http"

# WebSocket URL for OpenClaw Gateway protocol (defaults to OPENCLAW_GATEWAY_URL for backward compat).
OPENCLAW_GATEWAY_WS_URL = os.getenv("OPENCLAW_GATEWAY_WS_URL", OPENCLAW_GATEWAY_URL).strip()
# OpenClaw HTTP API (OpenAI 兼容端点，经过 Safeline WAF)
OPENCLAW_HTTP_URL = os.getenv("OPENCLAW_HTTP_URL", "http://172.17.0.1:48789/v1/chat/completions")
# OpenClaw Gateway 主力模型显示名 (Gateway SSE 固定返回 "openclaw"，需手动配置)
# OPENCLAW_DISPLAY_MODEL 已废弃 - OpenClaw 模式不再显示模型名

# OpenClaw 多 Agent 路由配置
# 钉钉群 conversationId → OpenClaw agent ID 的映射
# 格式: JSON 对象字符串，例如: {"cid123":"group-1","cid456":"group-2"}
OPENCLAW_GROUP_AGENT_MAPPING_RAW = os.getenv("OPENCLAW_GROUP_AGENT_MAPPING", "{}")
try:
    import json
    OPENCLAW_GROUP_AGENT_MAPPING = json.loads(OPENCLAW_GROUP_AGENT_MAPPING_RAW)
except json.JSONDecodeError:
    print(f"⚠️ OPENCLAW_GROUP_AGENT_MAPPING 解析失败，使用空映射")
    OPENCLAW_GROUP_AGENT_MAPPING = {}

# OpenClaw 严格路由模式 (Security)
# 如果启用，未在 mapping 中的群组将直接被拒绝访问 (不会 fallback 到 default agent)
OPENCLAW_STRICT_ROUTING = _get_bool("OPENCLAW_STRICT_ROUTING", True)
# OpenClaw 请求携带的历史条数（仅用于客户端轻量上下文）
OPENCLAW_CONTEXT_MESSAGES = max(0, _get_int("OPENCLAW_CONTEXT_MESSAGES", 6))

# OpenClaw Tools Invoke HTTP API
OPENCLAW_TOOLS_URL = os.getenv("OPENCLAW_TOOLS_URL", "").strip()
OPENCLAW_TOOLS_TOKEN = os.getenv("OPENCLAW_TOOLS_TOKEN", "").strip()
OPENCLAW_ASR_TOOL_NAME = os.getenv("OPENCLAW_ASR_TOOL_NAME", "asr").strip()
OPENCLAW_FILE_TOOL_NAME = os.getenv("OPENCLAW_FILE_TOOL_NAME", "file_summarize").strip()
OPENCLAW_VISION_TOOL_NAME = os.getenv("OPENCLAW_VISION_TOOL_NAME", "vision").strip()

def get_agent_for_conversation(conversation_id: str) -> str | None:
    """
    根据钉钉 conversationId 获取对应的 OpenClaw agent ID

    严格路由模式（推荐）：
    - 当群 conversationId 在 OPENCLAW_GROUP_AGENT_MAPPING 中有映射时，返回对应 agent
    - 当群未配置映射时，返回 None（调用者需要返回错误提示给用户）

    兼容模式（OPENCLAW_STRICT_ROUTING=false）：
    - 未配置的群回退到 OPENCLAW_AGENT_ID（可能有隐私/隔离风险）

    Args:
        conversation_id: 钉钉会话 ID (群 ID)

    Returns:
        agent ID 字符串，或 None（严格模式下未映射）

    Safety:
        严格模式避免未配置的群误打到默认 agent，防止隐私泄露。
    """
    # 首先尝试从映射表查询
    if conversation_id in OPENCLAW_GROUP_AGENT_MAPPING:
        return OPENCLAW_GROUP_AGENT_MAPPING[conversation_id]

    # 未在映射表中
    if OPENCLAW_STRICT_ROUTING:
        # 严格模式：返回 None，让调用者返回错误提示
        return None
    else:
        # 兼容模式：回退到默认 agent
        return OPENCLAW_AGENT_ID

# AI 后端选择: gemini | openclaw
AI_BACKEND = os.getenv("AI_BACKEND", "gemini")

def _auto_image_backend() -> str:
    """按 OPENAI_IMAGE_MODEL 名称自动推断生图后端。
    sub2api 的 Gemini 渠道不支持 /v1/images/generations（inline_data 无法转 OpenAI 格式），
    因此 Gemini/Imagen 模型走 google-genai SDK 直连，GPT 模型走 sub2api images 端口。
    可通过 IMAGE_BACKEND 环境变量显式覆盖。
    """
    explicit = os.getenv("IMAGE_BACKEND")
    if explicit:
        return explicit
    img_model = os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-2")
    if any(x in img_model.lower() for x in ("gemini", "imagen")):
        return "gemini"
    return "openai"

IMAGE_BACKEND = _auto_image_backend()

# Bot 实例标识 (多 bot 共存时区分消息来源)
BOT_ID = os.getenv("BOT_ID", AI_BACKEND)
BOT_NAME = os.getenv("BOT_NAME", "")  # 显示名称，不设则按后端 + 实际模型自动推断

# 上下文配置
MAX_HISTORY_LENGTH = int(os.getenv("MAX_HISTORY_LENGTH", 50)) # 发送给 Gemini 的最大条数
MAX_STORAGE_LENGTH = int(os.getenv("MAX_STORAGE_LENGTH", 1000)) # 本地存储的最大条数
HISTORY_TTL = 3600 * 24 * 7 # 本地存储保留 7 天

# Google Endpoint
GOOGLE_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"

# 统一三档模型变量 —— 所有后端共用，切换后端时改值即可
# 默认值按 AI_BACKEND 自动选，可通过环境变量覆盖
_BACKEND_MODEL_DEFAULTS = {
    "gemini": {
        "router": "gemini-3.1-flash-lite",   # 路由分析、soul 进化、搜索
        "lite":   "gemini-3-flash-preview",  # 简单问候
        "fast":   "gemini-3-flash-preview",  # 日常问答
        "pro":    "gemini-3.1-pro-preview",  # 复杂推理
    },
    "openrouter": {
        "router": "anthropic/claude-haiku-4-5",
        "lite":   "anthropic/claude-haiku-4-5",
        "fast":   "anthropic/claude-sonnet-4-5",
        "pro":    "anthropic/claude-opus-4-5",
    },
    "openai": {
        "router": "deepseek/deepseek-chat",
        "lite":   "deepseek/deepseek-chat",
        "fast":   "deepseek/deepseek-chat",
        "pro":    "deepseek/deepseek-reasoner",
    },
    "openclaw": {
        "router": "gemini-3.1-flash-lite",
        "lite":   "default",
        "fast":   "default",
        "pro":    "default",
    },
}
_md = _BACKEND_MODEL_DEFAULTS.get(AI_BACKEND, _BACKEND_MODEL_DEFAULTS["gemini"])

MODEL_ROUTER = os.getenv("MODEL_ROUTER", _md["router"])  # 路由分析 / soul 进化 / 搜索
MODEL_LITE   = os.getenv("MODEL_LITE",   _md["lite"])    # lite 档（简单问候）
MODEL_FAST   = os.getenv("MODEL_FAST",   _md["fast"])    # fast 档（日常问答）
MODEL_PRO    = os.getenv("MODEL_PRO",    _md["pro"])     # pro 档（复杂推理）

# 兼容别名，避免破坏现有引用
DEFAULT_MODEL     = MODEL_PRO
GEMINI_MODEL_LITE = MODEL_ROUTER
GEMINI_MODEL_FAST = MODEL_FAST

# 搜索 fallback（google_search）专用模型：必须是真实 Gemini 型号，与路由模型解耦。
# 若借用 MODEL_ROUTER，openai/openrouter 后端下它是 gpt-*/claude-* 名，发给 Gemini
# 搜索接口会 404（见 google_search）。默认取当前最新的 Gemini flash（与生产 MODEL_FAST 一致）。
GEMINI_SEARCH_MODEL = os.getenv("GEMINI_SEARCH_MODEL", "gemini-3.5-flash")
# 搜索 fallback 超时（秒）：代理半死/网络黑洞时避免挂死整个对话流
SEARCH_TIMEOUT_SECONDS = _get_int("SEARCH_TIMEOUT_SECONDS", 15)


def get_bot_display_name() -> str:
    """当前 bot 的显示名称。
    优先使用 BOT_NAME 环境变量；否则按 AI_BACKEND 推断，
    openai 后端额外看 MODEL_PRO/MODEL_FAST 以区分 s2a 透传的不同 provider。
    """
    if BOT_NAME:
        return BOT_NAME
    if AI_BACKEND == "gemini":
        return "Gem"
    if AI_BACKEND == "openclaw":
        return "Claw"
    if AI_BACKEND == "openrouter":
        return "小克"
    if AI_BACKEND == "openai":
        model = MODEL_PRO or MODEL_FAST or ""
        if model.startswith("anthropic/"):
            return "小克"
        if "gemini" in model.lower() or model.startswith("google/"):
            return "Gem"
        return "小G"
    return "Gem"


# 是否启用 thinking 模式 (显示模型的思考过程)
ENABLE_THINKING = os.getenv("ENABLE_THINKING", "true").lower() == "true"

# 是否启用 Google Search (让 AI 自动搜索实时信息)
ENABLE_SEARCH = os.getenv("ENABLE_SEARCH", "true").lower() == "true"
SEARCH_FALLBACK_PROVIDER = os.getenv("SEARCH_FALLBACK_PROVIDER", "gemini").strip().lower()

# 多 agent 角色重塑 + cache + 采样改造 feature flags
ENABLE_CACHE_BLOCKS    = _get_bool("ENABLE_CACHE_BLOCKS", True)       # B: system prompt 分块 cache
ENABLE_TOP_P_PIPELINE  = _get_bool("ENABLE_TOP_P_PIPELINE", True)     # C: top_p 贯穿到各 backend
ENABLE_ROLE_REWRITE    = _get_bool("ENABLE_ROLE_REWRITE", True)       # A: 其他 bot 的 assistant 消息转 user
ENABLE_SAMPLE_OVERRIDE = _get_bool("ENABLE_SAMPLE_OVERRIDE", True)    # D: /temp /top_p 手动覆盖

# 钉钉 AI 卡片模板 ID
CARD_TEMPLATE_ID = os.getenv("CARD_TEMPLATE_ID", "ea2d035e-20fe-447d-9fbf-c04658772b24.schema")

# 钉钉主动推送 API
DINGTALK_PUSH_BEARER_TOKEN = os.getenv("DINGTALK_PUSH_BEARER_TOKEN", "").strip()
DINGTALK_PUSH_IP_ALLOWLIST_RAW = os.getenv("DINGTALK_PUSH_IP_ALLOWLIST", "").strip()

# 钉钉“敲键盘”状态
# 钉钉侧对 streaming_update 的并发/频率比较敏感；该效果在部分客户端也不明显，默认关闭。
DINGTALK_TYPING_ENABLED = _get_bool("DINGTALK_TYPING_ENABLED", False)
DINGTALK_TYPING_INTERVAL_MS = max(200, _get_int("DINGTALK_TYPING_INTERVAL_MS", 650))

# 卡片流式更新节流间隔（秒）
# 取舍：太快（<1s）会让 thinkingText 副标题被首次 msgContent 更新瞬间盖掉，
# 用户看不到 10 字思考短语；太慢（≥3s）则失去流式体验。1.5s 是兼顾两者的折中。
# dingtalk_card.stream_update 自身另有 150ms 安全网防 burst。
STREAM_UPDATE_THROTTLE = max(0.5, _get_float("STREAM_UPDATE_THROTTLE", 1.5))
DINGTALK_TYPING_FRAMES_RAW = os.getenv(
    "DINGTALK_TYPING_FRAMES",
    "⌨️ 正在敲键盘.|⌨️ 正在敲键盘..|⌨️ 正在敲键盘...",
).strip()

# 历史引用（智能触发）
DINGTALK_REFERENCE_AUTO_ENABLED = _get_bool("DINGTALK_REFERENCE_AUTO_ENABLED", True)

# 发送图片消息（原生优先）
DINGTALK_IMAGE_MSG_KEY = os.getenv("DINGTALK_IMAGE_MSG_KEY", "sampleImageMsg").strip()
# msgParam 为 JSON 字符串，{mediaId} 会被替换
DINGTALK_IMAGE_MSG_PARAM_TEMPLATE = os.getenv(
    "DINGTALK_IMAGE_MSG_PARAM_TEMPLATE",
    "{\"photoURL\":\"@{mediaId}\"}",
).strip()

# ===== 生图配置 =====
GEMINI_IMAGE_MODEL = os.environ.get("GEMINI_IMAGE_MODEL", "imagen-4.0-generate-001")
GEMINI_IMAGE_EDIT_MODEL = os.environ.get("GEMINI_IMAGE_EDIT_MODEL", "gemini-2.0-flash-exp")
OPENAI_IMAGE_MODEL = os.environ.get("OPENAI_IMAGE_MODEL", "gpt-image-2")
DEFAULT_IMAGE_ASPECT_RATIO = os.environ.get("DEFAULT_IMAGE_ASPECT_RATIO", "1:1")
DEFAULT_IMAGE_COUNT = max(1, min(4, _get_int("DEFAULT_IMAGE_COUNT", 1)))

# 腾讯云 COS 图片存储
COS_SECRET_ID = os.getenv("COS_SECRET_ID", "")
COS_SECRET_KEY = os.getenv("COS_SECRET_KEY", "")
COS_BUCKET = os.getenv("COS_BUCKET", "")           # 格式: bucket-appid
COS_REGION = os.getenv("COS_REGION", "ap-guangzhou")
COS_IMAGE_TTL_HOURS = max(1, _get_int("COS_IMAGE_TTL_HOURS", 24))  # COS 生命周期参考（控制台配置）
COS_PRESIGN_EXPIRES = max(60, _get_int("COS_PRESIGN_EXPIRES", 600))

# Gemini 定价 (美元/百万 tokens)
# 参考: https://ai.google.dev/gemini-api/docs/pricing
GEMINI_PRICING = {
    # Gemini 3 系列
    "gemini-3-flash": {"input": 0.50, "output": 3.00},
    "gemini-3-pro": {"input": 2.00, "output": 12.00},
    # Gemini 3.1 系列
    "gemini-3.1-flash-lite": {"input": 0.25, "output": 1.50},
    "gemini-3.1-pro": {"input": 2.00, "output": 12.00},  # <=200K tokens; >200K: $4/$18
    # Gemini 2.5 系列
    "gemini-2.5-pro": {"input": 1.25, "output": 10.00},
    "gemini-2.5-flash": {"input": 0.15, "output": 3.50},  # 含推理
    "gemini-2.5-flash-lite": {"input": 0.10, "output": 0.40},
    # Gemini 2.0 系列
    "gemini-2.0-flash": {"input": 0.10, "output": 0.40},
    "gemini-2.0-flash-exp": {"input": 0.0, "output": 0.0},  # 免费预览
    # Gemini 1.5 系列
    "gemini-1.5-pro": {"input": 1.25, "output": 5.00},
    "gemini-1.5-flash": {"input": 0.15, "output": 0.60},
    # 默认值
    "default": {"input": 0.50, "output": 3.00}
}

# 可用模型列表（flash/pro 跟随统一配置变量）
AVAILABLE_MODELS = {
    "flash": MODEL_FAST,
    "pro": MODEL_PRO,
    "2.5-flash": "gemini-2.5-flash",
    "2.5-pro": "gemini-2.5-pro",
    "2.0-flash": "gemini-2.0-flash",
}

def get_model_pricing(model_name: str) -> dict:
    """获取模型定价"""
    model_lower = model_name.lower()
    for key in GEMINI_PRICING:
        if key in model_lower:
            return GEMINI_PRICING[key]
    return GEMINI_PRICING["default"]

# ===== LiteLLM 后端 =====
# 模型由统一变量 MODEL_LITE/FAST/PRO 控制，此处保留别名供旧配置兼容
LITELLM_MODEL_LITE  = MODEL_LITE
LITELLM_MODEL_FLASH = MODEL_FAST
LITELLM_MODEL_PRO   = MODEL_PRO
OPENAI_API_BASE = os.getenv("OPENAI_API_BASE", "")
OPENAI_API_KEY_CUSTOM = os.getenv("OPENAI_API_KEY", "")

# Vertex AI 配置（LiteLLM vertex_ai/ 路由使用）
VERTEX_PROJECT = os.getenv("VERTEX_PROJECT", "")

# 模型映射（带 capability 声明）
LITELLM_MODEL_CONFIG = {
    "lite": {
        "model": MODEL_LITE,
        "region": os.getenv("VERTEX_REGION_FAST", "europe-west1"),
        "supports_reasoning": _get_bool("OPENAI_LITE_SUPPORTS_REASONING", False),
        "supports_search": _get_bool("OPENAI_LITE_SUPPORTS_SEARCH", False),
        "supports_vision": _get_bool("OPENAI_LITE_SUPPORTS_VISION", True),
        "reasoning_param": os.getenv("VERTEX_REASONING_PARAM_FAST", "openai_effort"),
    },
    "fast": {
        "model": MODEL_FAST,
        "region": os.getenv("VERTEX_REGION_FAST", "europe-west1"),
        "supports_reasoning": _get_bool("OPENAI_FLASH_SUPPORTS_REASONING", True),
        "supports_search": _get_bool("OPENAI_FLASH_SUPPORTS_SEARCH", False),
        "supports_vision": _get_bool("OPENAI_FLASH_SUPPORTS_VISION", True),
        "reasoning_param": os.getenv("VERTEX_REASONING_PARAM_FAST", "openai_effort"),
    },
    "pro": {
        "model": MODEL_PRO,
        "region": os.getenv("VERTEX_REGION_PRO", "us-east5"),
        "supports_reasoning": _get_bool("OPENAI_PRO_SUPPORTS_REASONING", True),
        "supports_search": _get_bool("OPENAI_PRO_SUPPORTS_SEARCH", False),
        "supports_vision": _get_bool("OPENAI_PRO_SUPPORTS_VISION", True),
        "reasoning_param": os.getenv("VERTEX_REASONING_PARAM_PRO", "openai_effort"),
    },
}

# 路由名归一化：把路由输出的各种模型名统一到 lite/fast/pro
ROUTE_KEY_MAP = {
    # 抽象 tier 名直通（各后端路由 prompt 输出这些）
    "lite": "lite",
    "fast": "fast",
    "pro": "pro",
    # 当前配置的模型名（动态注入）
    MODEL_ROUTER: "lite",
    MODEL_LITE:   "lite",
    MODEL_FAST:   "fast",
    MODEL_PRO:    "pro",
    # 历史 Gemini 模型名兼容
    "gemini-3-flash-lite": "lite",
    "gemini-3-flash-lite-preview": "lite",
    "gemini-3-flash-preview": "fast",
    "gemini-3-flash": "fast",
    "gemini-3.1-pro-preview": "pro",
    "gemini-3-pro-preview": "pro",
}

def get_route_key(target_model: str) -> str:
    key = ROUTE_KEY_MAP.get(target_model)
    if key is None:
        print(f"⚠️ 未知模型 {target_model}，降级到 fast")
        return "fast"
    return key

def get_litellm_model_config(route_key: str) -> dict:
    return LITELLM_MODEL_CONFIG.get(route_key, LITELLM_MODEL_CONFIG["fast"])

# ===== OpenRouter 后端 =====
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
# 路由大脑别名，由统一变量 MODEL_ROUTER 控制
OPENROUTER_ROUTER_MODEL = MODEL_ROUTER

def _parse_fallbacks(env_val: str) -> list:
    return [m.strip() for m in env_val.split(",") if m.strip()]

OPENROUTER_MODEL_CONFIG = {
    # lite: 简单问候、闲聊
    "lite": {
        "model": MODEL_LITE,
        "fallbacks": _parse_fallbacks(os.getenv("OPENROUTER_FALLBACK_LITE", "")),
        "provider_order": [p.strip() for p in os.getenv("OPENROUTER_PROVIDER_ORDER", "Anthropic").split(",") if p.strip()],
        "provider_sort": os.getenv("OPENROUTER_PROVIDER_SORT", ""),
        "supports_reasoning": False,
        "supports_search": _get_bool("OPENROUTER_LITE_SUPPORTS_SEARCH", True),
        "supports_vision": _get_bool("OPENROUTER_LITE_SUPPORTS_VISION", True),
    },
    # fast: 普通工作
    "fast": {
        "model": MODEL_FAST,
        "fallbacks": _parse_fallbacks(os.getenv("OPENROUTER_FALLBACK_FAST", "")),
        "provider_order": [p.strip() for p in os.getenv("OPENROUTER_PROVIDER_ORDER", "Anthropic").split(",") if p.strip()],
        "provider_sort": os.getenv("OPENROUTER_PROVIDER_SORT", ""),
        "supports_reasoning": _get_bool("OPENROUTER_FAST_SUPPORTS_REASONING", True),
        "supports_search": _get_bool("OPENROUTER_FAST_SUPPORTS_SEARCH", True),
        "supports_vision": _get_bool("OPENROUTER_FAST_SUPPORTS_VISION", True),
    },
    # pro: 高阶推理
    "pro": {
        "model": MODEL_PRO,
        "fallbacks": _parse_fallbacks(os.getenv("OPENROUTER_FALLBACK_PRO", "")),
        "provider_order": [p.strip() for p in os.getenv("OPENROUTER_PROVIDER_ORDER", "Anthropic").split(",") if p.strip()],
        "provider_sort": os.getenv("OPENROUTER_PROVIDER_SORT", ""),
        "supports_reasoning": _get_bool("OPENROUTER_PRO_SUPPORTS_REASONING", True),
        "supports_search": _get_bool("OPENROUTER_PRO_SUPPORTS_SEARCH", True),
        "supports_vision": _get_bool("OPENROUTER_PRO_SUPPORTS_VISION", True),
    },
}

# 注意: 代理配置在 gemini_client.py 中设置
# 使用 NO_PROXY 排除钉钉域名，确保钉钉 SDK 不走代理
