import os
from dotenv import load_dotenv

# 加载 .env 文件
load_dotenv()

# Google Gemini API Key
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# 钉钉配置
DINGTALK_CLIENT_ID = os.getenv("DINGTALK_CLIENT_ID")
DINGTALK_CLIENT_SECRET = os.getenv("DINGTALK_CLIENT_SECRET")
DINGTALK_CORP_ID = os.getenv("DINGTALK_CORP_ID") # 新增 CorpId
DINGTALK_COOL_APP_CODE = os.getenv("DINGTALK_COOL_APP_CODE") # 新增 CoolAppCode

# 企业微信机器人配置 (新)
WECOM_BOT_WEBHOOK_KEY = os.getenv("WECOM_BOT_WEBHOOK_KEY", "")
WECOM_BOT_WEBHOOK_URL = os.getenv("WECOM_BOT_WEBHOOK_URL", "")
WECOM_BOT_TOKEN = os.getenv("WECOM_BOT_TOKEN", os.getenv("WECOM_TOKEN", ""))
WECOM_BOT_ENCODING_AES_KEY = os.getenv(
    "WECOM_BOT_ENCODING_AES_KEY",
    os.getenv("WECOM_ENCODING_AES_KEY", "")
)
WECOM_BOT_RECEIVE_ID = os.getenv("WECOM_BOT_RECEIVE_ID", "")

# 企业微信应用配置 (兼容旧配置，逐步废弃)
WECOM_CORP_ID = os.getenv("WECOM_CORP_ID", "")
WECOM_AGENT_ID = os.getenv("WECOM_AGENT_ID", "")
WECOM_SECRET = os.getenv("WECOM_SECRET", "")
WECOM_TOKEN = os.getenv("WECOM_TOKEN", WECOM_BOT_TOKEN)
WECOM_ENCODING_AES_KEY = os.getenv("WECOM_ENCODING_AES_KEY", WECOM_BOT_ENCODING_AES_KEY)

# 平台选择: dingtalk | wecom | both
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

# AI 后端选择: gemini | openclaw
AI_BACKEND = os.getenv("AI_BACKEND", "gemini")

# 上下文配置
MAX_HISTORY_LENGTH = int(os.getenv("MAX_HISTORY_LENGTH", 50)) # 发送给 Gemini 的最大条数
MAX_STORAGE_LENGTH = int(os.getenv("MAX_STORAGE_LENGTH", 1000)) # 本地存储的最大条数
HISTORY_TTL = 3600 * 24 * 7 # 本地存储保留 7 天

# Google Endpoint
GOOGLE_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"

# 默认模型 (可通过环境变量配置)
# 可选: gemini-2.0-flash, gemini-2.0-flash-thinking-exp, gemini-3-pro-preview
DEFAULT_MODEL = os.getenv("GEMINI_MODEL", "gemini-3-pro-preview")

# 是否启用 thinking 模式 (显示模型的思考过程)
ENABLE_THINKING = os.getenv("ENABLE_THINKING", "true").lower() == "true"

# 是否启用 Google Search (让 AI 自动搜索实时信息)
ENABLE_SEARCH = os.getenv("ENABLE_SEARCH", "true").lower() == "true"

# 钉钉 AI 卡片模板 ID
CARD_TEMPLATE_ID = os.getenv("CARD_TEMPLATE_ID", "ea2d035e-20fe-447d-9fbf-c04658772b24.schema")

# Gemini 定价 (美元/百万 tokens)
# 参考: https://ai.google.dev/gemini-api/docs/pricing
GEMINI_PRICING = {
    # Gemini 3 系列
    "gemini-3-flash": {"input": 0.50, "output": 3.00},
    "gemini-3-pro": {"input": 2.00, "output": 12.00},
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

# 可用模型列表
AVAILABLE_MODELS = {
    "flash": "gemini-3-flash",
    "pro": "gemini-3-pro-preview",
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

# 注意: 代理配置在 gemini_client.py 中设置
# 使用 NO_PROXY 排除钉钉域名，确保钉钉 SDK 不走代理
