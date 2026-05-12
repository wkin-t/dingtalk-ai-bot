# --- Monkey Patch aiohttp & requests to use proxy and retry by default ---
# 必须在所有其他导入之前执行
import os
import aiohttp
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from dotenv import load_dotenv

# 加载 .env 以获取代理配置
load_dotenv()

HTTP_PROXY = os.getenv("HTTP_PROXY")

# 1. Patch aiohttp (Proxy)
# 如果是 socks5h，aiohttp 不支持，需要转为 socks5
if HTTP_PROXY and HTTP_PROXY.startswith("socks5h://"):
    AIOHTTP_PROXY_PATCH = HTTP_PROXY.replace("socks5h://", "socks5://")
else:
    AIOHTTP_PROXY_PATCH = HTTP_PROXY

if AIOHTTP_PROXY_PATCH:
    print(f"🔧 Applying aiohttp proxy patch: {AIOHTTP_PROXY_PATCH}")
    _original_request = aiohttp.ClientSession._request

    async def _proxy_request(self, method, url, **kwargs):
        if 'proxy' not in kwargs:
            kwargs['proxy'] = AIOHTTP_PROXY_PATCH
        return await _original_request(self, method, url, **kwargs)

    aiohttp.ClientSession._request = _proxy_request

# 2. Patch requests (Retry & Proxy)
print(f"🔧 Applying requests retry patch")
_original_session_init = requests.Session.__init__

def _retry_session_init(self, *args, **kwargs):
    _original_session_init(self, *args, **kwargs)
    
    retry = Retry(
        total=5,
        backoff_factor=0.5,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["HEAD", "GET", "PUT", "DELETE", "OPTIONS", "TRACE", "POST"],
        connect=5,
        read=5
    )
    adapter = HTTPAdapter(max_retries=retry)
    self.mount('http://', adapter)
    self.mount('https://', adapter)

requests.Session.__init__ = _retry_session_init
# ----------------------------------------------------

import threading
import dingtalk_stream
from app import app
from app.config import (
    DINGTALK_CLIENT_ID, DINGTALK_CLIENT_SECRET,
    WECOM_BOT_TOKEN, WECOM_BOT_ENCODING_AES_KEY,
    PLATFORM
)
from app.dingtalk_bot import GeminiBotHandler
from app.memory import DATA_DIR # 导入数据目录

def start_dingtalk_stream_async():
    if not DINGTALK_CLIENT_ID or not DINGTALK_CLIENT_SECRET:
        print("⚠️ 未配置 DINGTALK_CLIENT_ID 或 DINGTALK_CLIENT_SECRET，跳过启动钉钉 Stream 客户端")
        return

    print("🚀 正在启动钉钉 Stream 客户端...")
    credential = dingtalk_stream.Credential(DINGTALK_CLIENT_ID, DINGTALK_CLIENT_SECRET)
    client = dingtalk_stream.DingTalkStreamClient(credential)
    client.register_callback_handler(dingtalk_stream.chatbot.ChatbotMessage.TOPIC, GeminiBotHandler())
    client.start_forever()

def run_stream_in_thread():
    try:
        start_dingtalk_stream_async()
    except Exception as e:
        print(f"❌ 钉钉 Stream 线程异常退出: {e}")

def register_wecom_webhook():
    """注册企业微信 Webhook 路由"""
    if not all([WECOM_BOT_TOKEN, WECOM_BOT_ENCODING_AES_KEY]):
        print("⚠️ 未配置 WECOM_BOT_TOKEN/WECOM_BOT_ENCODING_AES_KEY，跳过注册企业微信 Webhook")
        return
    print("🚀 正在注册企业微信 Webhook 路由...")
    from app.wecom.callback import wecom_bp, set_message_handler
    from app.wecom.bot import WeComBotHandler

    # 注册蓝图
    app.register_blueprint(wecom_bp)

    # 设置消息处理器
    wecom_handler = WeComBotHandler()
    set_message_handler(wecom_handler)

    print("✅ 企业微信 Webhook 已注册: /api/wecom/callback")

# 根据配置启动服务
if PLATFORM in ["dingtalk", "both"]:
    stream_thread = threading.Thread(target=run_stream_in_thread, daemon=True)
    stream_thread.start()

if PLATFORM in ["wecom", "both"]:
    register_wecom_webhook()

if __name__ == '__main__':
    # 支持通过环境变量配置端口 (默认 35000)
    flask_port = int(os.getenv("FLASK_PORT", "35000"))
    print(f"📂 History Data Directory: {os.path.abspath(DATA_DIR)}") # 打印绝对路径
    print(f"🚀 Proxy running at http://0.0.0.0:{flask_port}")
    app.run(host='0.0.0.0', port=flask_port, threaded=True)
