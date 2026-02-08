# -*- coding: utf-8 -*-
"""
企业微信 Webhook 回调处理
"""
import json
import threading
import time
from flask import Blueprint, request, make_response
import requests
from app.wecom.crypto import WXBizMsgCrypt
from app.config import (
    WECOM_BOT_TOKEN,
    WECOM_BOT_ENCODING_AES_KEY,
    WECOM_BOT_RECEIVE_ID,
    WECOM_BOT_REPLY_MODE,
)

# 创建蓝图
wecom_bp = Blueprint('wecom', __name__, url_prefix='/api/wecom')

# 全局消息处理器 (由 main.py 注入)
message_handler = None


def set_message_handler(handler):
    """设置消息处理器"""
    global message_handler
    message_handler = handler


def _truncate_utf8(content: str, max_bytes: int = 20480) -> str:
    """按 UTF-8 字节长度截断字符串（企业微信 markdown.content 上限 20480 字节）。"""
    if not content:
        return ""
    encoded = content.encode("utf-8")
    if len(encoded) <= max_bytes:
        return content
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _template_card_to_markdown(template_card: dict) -> str:
    """将模板卡片降级为 markdown 文本，用于群聊主动回复场景。"""
    if not isinstance(template_card, dict):
        return ""

    lines = []
    main_title = template_card.get("main_title")
    if isinstance(main_title, dict):
        title = (main_title.get("title") or "").strip()
        if title:
            lines.append(f"**{title}**")

    sub_title = (template_card.get("sub_title_text") or "").strip()
    if sub_title:
        lines.append(sub_title)

    quote_area = template_card.get("quote_area")
    if isinstance(quote_area, dict):
        quote_text = (quote_area.get("quote_text") or "").strip()
        if quote_text:
            lines.append(f"> {quote_text}")

    return "\n\n".join(lines)


def _extract_payload_content(payload_dict: dict) -> str:
    """从 stream/markdown/template_card 里提取可展示文本。"""
    if not isinstance(payload_dict, dict):
        return ""

    stream = payload_dict.get("stream")
    if isinstance(stream, dict):
        content = stream.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()

    markdown = payload_dict.get("markdown")
    if isinstance(markdown, dict):
        content = markdown.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()

    template_card = payload_dict.get("template_card")
    if isinstance(template_card, dict):
        return _template_card_to_markdown(template_card).strip()

    text = payload_dict.get("text")
    if isinstance(text, dict):
        content = text.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()

    return ""


def _to_active_reply_payload(msg_dict: dict, payload_dict: dict) -> dict:
    """
    将本地处理结果转换为 response_url 可接受的主动回复格式。
    参考官方文档：
    - 主动回复支持 markdown
    - template_card 主动回复仅支持单聊
    """
    msgtype = str(payload_dict.get("msgtype") or "").lower()
    chattype = str(msg_dict.get("chattype") or "").lower()

    if msgtype == "markdown":
        content = _extract_payload_content(payload_dict)
        return {"msgtype": "markdown", "markdown": {"content": _truncate_utf8(content)}}

    if msgtype == "template_card" and chattype == "single":
        template_card = payload_dict.get("template_card")
        if isinstance(template_card, dict):
            return {"msgtype": "template_card", "template_card": template_card}

    # stream / stream_with_template_card / template_card(群聊) / text 等统一降级为 markdown
    content = _extract_payload_content(payload_dict)
    if not content:
        content = "已收到消息，处理中。"
    return {
        "msgtype": "markdown",
        "markdown": {
            "content": _truncate_utf8(content),
        },
    }


def _async_respond_via_response_url(msg_dict: dict):
    """企业微信机器人模式：通过 response_url 异步回推消息。"""
    if not message_handler:
        return

    response_url = msg_dict.get("response_url")
    if not response_url:
        return

    try:
        stream_payload = message_handler.handle_message(msg_dict)
        if not stream_payload:
            return
        payload_dict = json.loads(stream_payload)
        active_payload = _to_active_reply_payload(msg_dict, payload_dict)
        resp = requests.post(response_url, json=active_payload, timeout=10)

        resp_json = {}
        try:
            resp_json = resp.json()
        except Exception:
            pass

        errcode = resp_json.get("errcode")
        errmsg = resp_json.get("errmsg") or resp.text[:200]
        if resp.status_code != 200 or (errcode is not None and errcode != 0):
            print(
                f"❌ [企业微信] response_url 回推失败: status={resp.status_code}, "
                f"errcode={errcode}, errmsg={errmsg}, payload={json.dumps(active_payload, ensure_ascii=False)[:280]}"
            )
            return
        print(
            f"✅ [企业微信] response_url 回推成功: msgtype={active_payload.get('msgtype')}, "
            f"errcode={errcode}, errmsg={errmsg}"
        )
    except Exception as e:
        print(f"❌ [企业微信] response_url 回推异常: {e}")


@wecom_bp.route('/callback', methods=['GET', 'POST'])
def callback():
    """
    企业微信回调入口
    - GET: URL 验证
    - POST: 接收消息
    """
    # 获取查询参数
    msg_signature = request.args.get('msg_signature', '')
    timestamp = request.args.get('timestamp', '')
    nonce = request.args.get('nonce', '')

    # 初始化加解密工具
    crypto = WXBizMsgCrypt(WECOM_BOT_TOKEN, WECOM_BOT_ENCODING_AES_KEY, WECOM_BOT_RECEIVE_ID)

    # GET: URL 验证
    if request.method == 'GET':
        echostr = request.args.get('echostr', '')
        try:
            plaintext = crypto.verify_url(msg_signature, timestamp, nonce, echostr)
            return make_response(plaintext, 200)
        except Exception as e:
            print(f"❌ URL 验证失败: {e}")
            return make_response('Verification failed', 403)

    # POST: 接收消息
    elif request.method == 'POST':
        raw_body = request.data.decode('utf-8', errors='ignore')
        try:
            # 解密消息
            msg_dict = crypto.decrypt_msg(msg_signature, timestamp, nonce, raw_body)
            print(f"📩 [企业微信] 收到消息: {msg_dict}")

            # 机器人模式：根据配置选择回包方式
            # response_url: 异步主动回复（仅支持非流式）
            # passive_stream: 回调内加密返回（支持 stream/stream_with_template_card）
            if msg_dict.get("response_url") and WECOM_BOT_REPLY_MODE == "response_url":
                threading.Thread(
                    target=_async_respond_via_response_url,
                    args=(msg_dict,),
                    daemon=True,
                ).start()
                return make_response('success', 200)

            # 被动回包模式（含旧兼容）：同步回调内加密应答
            if message_handler:
                response_msg = message_handler.handle_message(msg_dict)
                if response_msg:
                    # 企业微信机器人回调响应为加密 JSON
                    safe_nonce = nonce or "nonce"
                    safe_timestamp = timestamp or str(int(time.time()))
                    encrypted_response = crypto.encrypt_msg(response_msg, safe_nonce, safe_timestamp)
                    return make_response(encrypted_response, 200, {'Content-Type': 'text/plain; charset=utf-8'})

            # 无需回复时返回 success
            return make_response('success', 200)

        except Exception as e:
            print(f"❌ 消息处理失败: {e}")
            import traceback
            traceback.print_exc()
            return make_response('error', 500)
