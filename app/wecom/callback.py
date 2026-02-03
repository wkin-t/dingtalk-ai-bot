# -*- coding: utf-8 -*-
"""
企业微信 Webhook 回调处理
"""
from flask import Blueprint, request, make_response
from app.wecom.crypto import WXBizMsgCrypt
from app.config import WECOM_TOKEN, WECOM_ENCODING_AES_KEY, WECOM_CORP_ID

# 创建蓝图
wecom_bp = Blueprint('wecom', __name__, url_prefix='/api/wecom')

# 全局消息处理器 (由 main.py 注入)
message_handler = None


def set_message_handler(handler):
    """设置消息处理器"""
    global message_handler
    message_handler = handler


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
    crypto = WXBizMsgCrypt(WECOM_TOKEN, WECOM_ENCODING_AES_KEY, WECOM_CORP_ID)

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
        encrypt_msg = request.data.decode('utf-8')
        try:
            # 解密消息
            msg_dict = crypto.decrypt_msg(msg_signature, timestamp, nonce, encrypt_msg)
            print(f"📩 [企业微信] 收到消息: {msg_dict}")

            # 调用消息处理器
            if message_handler:
                response_msg = message_handler.handle_message(msg_dict)
                if response_msg:
                    # 加密回复
                    encrypted_response = crypto.encrypt_msg(response_msg, nonce, timestamp)
                    return make_response(encrypted_response, 200, {'Content-Type': 'application/xml'})

            # 无需回复时返回 success
            return make_response('success', 200)

        except Exception as e:
            print(f"❌ 消息处理失败: {e}")
            import traceback
            traceback.print_exc()
            return make_response('error', 500)
