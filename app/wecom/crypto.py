# -*- coding: utf-8 -*-
"""
企业微信机器人消息加解密
"""
import base64
import hashlib
import json
import struct
import xml.etree.ElementTree as ET
from typing import Optional

from Crypto.Cipher import AES


class WXBizMsgCrypt:
    """企业微信消息加解密工具"""

    def __init__(self, token: str, encoding_aes_key: str, receive_id: str = ""):
        self.token = token
        self.receive_id = receive_id or ""

        aes_key_base64 = encoding_aes_key + "="
        self.aes_key = base64.b64decode(aes_key_base64)

    def verify_url(self, msg_signature: str, timestamp: str, nonce: str, echostr: str) -> str:
        if not self._verify_signature(msg_signature, timestamp, nonce, echostr):
            raise ValueError("签名验证失败")
        # URL 验证阶段不强制 receive_id，避免机器人不同形态带来的兼容问题
        return self._decrypt(echostr, verify_receive_id=False)

    def decrypt_msg(self, msg_signature: str, timestamp: str, nonce: str, raw_body: str) -> dict:
        encrypt = self.extract_encrypt(raw_body)
        if not encrypt:
            raise ValueError("未找到 Encrypt 字段")

        if not self._verify_signature(msg_signature, timestamp, nonce, encrypt):
            raise ValueError("签名验证失败")

        plaintext = self._decrypt(encrypt, verify_receive_id=True)
        return self._parse_plaintext_msg(plaintext)

    @staticmethod
    def extract_encrypt(raw_body: str) -> Optional[str]:
        raw = (raw_body or "").strip()
        if not raw:
            return None

        # XML 负载
        if raw.startswith("<"):
            root = ET.fromstring(raw)
            node = root.find("Encrypt")
            return node.text if node is not None else None

        # JSON 负载
        if raw.startswith("{"):
            data = json.loads(raw)
            return data.get("Encrypt") or data.get("encrypt")

        return None

    @staticmethod
    def _parse_plaintext_msg(plaintext: str) -> dict:
        raw = (plaintext or "").strip()
        if raw.startswith("<"):
            root = ET.fromstring(raw)
            result = {}
            for child in root:
                result[child.tag] = child.text
            return result
        if raw.startswith("{"):
            return json.loads(raw)
        return {"Content": raw, "MsgType": "text"}

    def encrypt_msg(self, reply_msg: str, nonce: str, timestamp: str) -> str:
        encrypt = self._encrypt(reply_msg)
        signature = self._generate_signature(timestamp, nonce, encrypt)
        # 机器人回调协议使用 JSON 字段（与官方 demo 保持一致）
        return json.dumps(
            {
                "encrypt": encrypt,
                "msgsignature": signature,
                "timestamp": str(timestamp),
                "nonce": str(nonce),
            },
            ensure_ascii=False,
        )

    def _verify_signature(self, msg_signature: str, timestamp: str, nonce: str, encrypt: str) -> bool:
        return msg_signature == self._generate_signature(timestamp, nonce, encrypt)

    def _generate_signature(self, timestamp: str, nonce: str, encrypt: str) -> str:
        params = sorted([self.token, timestamp, nonce, encrypt])
        return hashlib.sha1("".join(params).encode("utf-8")).hexdigest()

    def _encrypt(self, plaintext: str) -> str:
        import os
        random_bytes = os.urandom(16)
        plaintext_bytes = plaintext.encode("utf-8")
        msg_len = struct.pack("!I", len(plaintext_bytes))
        receive_id = self.receive_id.encode("utf-8")
        raw = random_bytes + msg_len + plaintext_bytes + receive_id
        raw = self._pkcs7_pad(raw)
        cipher = AES.new(self.aes_key, AES.MODE_CBC, self.aes_key[:16])
        encrypted = cipher.encrypt(raw)
        return base64.b64encode(encrypted).decode("utf-8")

    def _decrypt(self, encrypt: str, verify_receive_id: bool) -> str:
        encrypted_bytes = base64.b64decode(encrypt)
        cipher = AES.new(self.aes_key, AES.MODE_CBC, self.aes_key[:16])
        decrypted = cipher.decrypt(encrypted_bytes)
        decrypted = self._pkcs7_unpad(decrypted)

        msg_len = struct.unpack("!I", decrypted[16:20])[0]
        plaintext = decrypted[20:20 + msg_len].decode("utf-8")
        receive_id_from_msg = decrypted[20 + msg_len:].decode("utf-8")

        if verify_receive_id and self.receive_id and receive_id_from_msg and receive_id_from_msg != self.receive_id:
            raise ValueError(f"receive_id 不匹配: 预期 {self.receive_id}, 实际 {receive_id_from_msg}")

        return plaintext

    @staticmethod
    def _pkcs7_pad(data: bytes) -> bytes:
        block_size = 32
        padding_len = block_size - (len(data) % block_size)
        return data + bytes([padding_len] * padding_len)

    @staticmethod
    def _pkcs7_unpad(data: bytes) -> bytes:
        padding_len = data[-1]
        if padding_len < 1 or padding_len > 32:
            raise ValueError("非法的 PKCS7 padding")
        return data[:-padding_len]
