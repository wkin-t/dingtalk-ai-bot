import asyncio
import aiohttp
import ipaddress
import json
import base64
from flask import request, Response, jsonify
from app import app
from app.config import (
    GEMINI_API_KEY,
    GOOGLE_ENDPOINT,
    AIOHTTP_PROXY,
    DINGTALK_CLIENT_ID,
    DINGTALK_CLIENT_SECRET,
    DINGTALK_PUSH_BEARER_TOKEN,
    DINGTALK_PUSH_IP_ALLOWLIST_RAW,
    DINGTALK_IMAGE_MSG_KEY,
    DINGTALK_IMAGE_MSG_PARAM_TEMPLATE,
)
from app.dingtalk_card import DingTalkCardHelper

@app.route('/', methods=['GET'])
def index():
    return jsonify({"status": "ok", "service": "Gemini Proxy"}), 200

@app.route('/v1/models', methods=['GET'])
def models():
    return jsonify({
        "object": "list",
        "data": [
            {"id": "gemini-flash-latest", "object": "model", "created": 1677610602, "owned_by": "google"},
            {"id": "gemini-3-flash-preview", "object": "model", "created": 1677610602, "owned_by": "google"}
        ]
    })

async def async_chat_completions():
    data = request.json
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {GEMINI_API_KEY}"
    }
    unsupported_keys = ['frequency_penalty', 'presence_penalty', 'logit_bias', 'top_logprobs']
    safe_data = {k: v for k, v in data.items() if k not in unsupported_keys}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                GOOGLE_ENDPOINT,
                json=safe_data,
                headers=headers,
                proxy=AIOHTTP_PROXY,
                timeout=aiohttp.ClientTimeout(total=60)
            ) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    return Response(error_text, status=resp.status, mimetype='application/json')
                
                result = await resp.read()
                return Response(result, mimetype='application/json')

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/v1/chat/completions', methods=['POST'])
def chat_completions():
    return asyncio.run(async_chat_completions())


def _get_request_ip() -> str:
    xff = (request.headers.get("X-Forwarded-For") or "").strip()
    if xff:
        # first hop
        return xff.split(",")[0].strip()
    return (request.remote_addr or "").strip()


def _ip_allowed(ip_str: str) -> bool:
    raw = (DINGTALK_PUSH_IP_ALLOWLIST_RAW or "").strip()
    if not raw:
        return True
    try:
        ip_obj = ipaddress.ip_address(ip_str)
    except ValueError:
        return False

    for item in [x.strip() for x in raw.split(",") if x.strip()]:
        try:
            if "/" in item:
                if ip_obj in ipaddress.ip_network(item, strict=False):
                    return True
            else:
                if ip_obj == ipaddress.ip_address(item):
                    return True
        except ValueError:
            continue
    return False


_sender_singleton = None


def _get_sender() -> DingTalkCardHelper:
    global _sender_singleton
    if _sender_singleton is None:
        _sender_singleton = DingTalkCardHelper(DINGTALK_CLIENT_ID, DINGTALK_CLIENT_SECRET)
    return _sender_singleton


@app.route("/api/dingtalk/push", methods=["POST"])
def dingtalk_push():
    auth = (request.headers.get("Authorization") or "").strip()
    if not DINGTALK_PUSH_BEARER_TOKEN:
        return jsonify({"ok": False, "error": "DINGTALK_PUSH_BEARER_TOKEN 未配置"}), 500
    if auth != f"Bearer {DINGTALK_PUSH_BEARER_TOKEN}":
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    ip_str = _get_request_ip()
    if not _ip_allowed(ip_str):
        return jsonify({"ok": False, "error": "forbidden"}), 403

    data = request.get_json(silent=True) or {}
    target_type = (data.get("target_type") or "group").strip().lower()
    conversation_id = (data.get("conversation_id") or "").strip()
    # Default to markdown for a better user experience.
    msg_type = (data.get("message_type") or "markdown").strip().lower()

    if not conversation_id:
        return jsonify({"ok": False, "error": "conversation_id required"}), 400

    sender = _get_sender()

    async def _do_send():
        if msg_type == "text":
            msg_key = "sampleText"
            msg_param = json.dumps({"content": str(data.get("content") or "")}, ensure_ascii=False)
        elif msg_type == "markdown":
            msg_key = "sampleMarkdown"
            msg_param = json.dumps({"title": str(data.get("title") or "通知"), "text": str(data.get("content") or "")}, ensure_ascii=False)
        elif msg_type == "image":
            # native image message with media_id
            b64 = (data.get("image_base64") or "").strip()
            if not b64:
                return False, "image_base64 required"
            try:
                image_bytes = base64.b64decode(b64)
            except Exception:
                return False, "invalid image_base64"
            media_id = await sender.upload_media(image_bytes, filetype="image", filename="image.png", mimetype="image/png")
            if not media_id:
                return False, "upload_media failed"
            msg_key = DINGTALK_IMAGE_MSG_KEY
            msg_param = DINGTALK_IMAGE_MSG_PARAM_TEMPLATE.replace("{mediaId}", media_id)
        else:
            return False, f"unsupported message_type={msg_type}"

        if target_type == "group":
            ok = await sender.send_group_message(conversation_id, msg_key=msg_key, msg_param=msg_param)
            return ok, None if ok else "send_group_message failed"
        if target_type == "single":
            ok = await sender.send_private_chat_message(conversation_id, msg_key=msg_key, msg_param=msg_param)
            return ok, None if ok else "send_private_chat_message failed"
        return False, f"unsupported target_type={target_type}"

    ok, err = asyncio.run(_do_send())
    if not ok:
        return jsonify({"ok": False, "error": err or "send failed"}), 500
    return jsonify({"ok": True}), 200
