# -*- coding: utf-8 -*-
"""
企业微信消息加解密
参考: https://developer.work.weixin.qq.com/document/path/90930
"""
import base64
import hashlib
import struct
import xml.etree.ElementTree as ET
from Crypto.Cipher import AES


class WXBizMsgCrypt:
    """企业微信消息加解密工具"""

    def __init__(self, token: str, encoding_aes_key: str, corp_id: str):
        """
        初始化加解密工具

        Args:
            token: 企业微信回调 Token
            encoding_aes_key: 企业微信回调加密密钥 (43位字符)
            corp_id: 企业ID
        """
        self.token = token
        self.corp_id = corp_id

        # EncodingAESKey 是 Base64 编码的 32 字节密钥
        # 需要补齐 '=' 让长度为 44 (43位 + 1个 '=')
        aes_key_base64 = encoding_aes_key + '='
        self.aes_key = base64.b64decode(aes_key_base64)

    def verify_url(self, msg_signature: str, timestamp: str, nonce: str, echostr: str) -> str:
        """
        验证 URL (企业微信后台配置回调时使用)

        Args:
            msg_signature: 签名
            timestamp: 时间戳
            nonce: 随机数
            echostr: 加密的随机字符串

        Returns:
            解密后的 echostr 明文
        """
        # 1. 验证签名
        if not self._verify_signature(msg_signature, timestamp, nonce, echostr):
            raise ValueError("签名验证失败")

        # 2. 解密 echostr
        plaintext = self._decrypt(echostr)

        return plaintext

    def decrypt_msg(self, msg_signature: str, timestamp: str, nonce: str, encrypt_msg: str) -> dict:
        """
        解密企业微信推送的消息

        Args:
            msg_signature: 签名
            timestamp: 时间戳
            nonce: 随机数
            encrypt_msg: 加密的消息体 (XML 格式)

        Returns:
            解密后的消息字典
        """
        # 1. 解析 XML,提取 Encrypt 字段
        root = ET.fromstring(encrypt_msg)
        encrypt = root.find('Encrypt').text

        # 2. 验证签名
        if not self._verify_signature(msg_signature, timestamp, nonce, encrypt):
            raise ValueError("签名验证失败")

        # 3. 解密消息
        plaintext_xml = self._decrypt(encrypt)

        # 4. 解析 XML 为字典
        msg_root = ET.fromstring(plaintext_xml)
        msg_dict = {}
        for child in msg_root:
            msg_dict[child.tag] = child.text

        return msg_dict

    def encrypt_msg(self, reply_msg: str, nonce: str, timestamp: str) -> str:
        """
        加密回复消息

        Args:
            reply_msg: 明文消息 (XML 格式)
            nonce: 随机数
            timestamp: 时间戳

        Returns:
            加密后的 XML 字符串
        """
        # 1. 加密消息
        encrypt = self._encrypt(reply_msg)

        # 2. 生成签名
        signature = self._generate_signature(timestamp, nonce, encrypt)

        # 3. 构造 XML
        xml_template = """<xml>
<Encrypt><![CDATA[{encrypt}]]></Encrypt>
<MsgSignature><![CDATA[{signature}]]></MsgSignature>
<TimeStamp>{timestamp}</TimeStamp>
<Nonce><![CDATA[{nonce}]]></Nonce>
</xml>"""

        return xml_template.format(
            encrypt=encrypt,
            signature=signature,
            timestamp=timestamp,
            nonce=nonce
        )

    def _verify_signature(self, msg_signature: str, timestamp: str, nonce: str, encrypt: str) -> bool:
        """验证签名"""
        expected_signature = self._generate_signature(timestamp, nonce, encrypt)
        return msg_signature == expected_signature

    def _generate_signature(self, timestamp: str, nonce: str, encrypt: str) -> str:
        """生成签名"""
        # 将 token、timestamp、nonce、encrypt 按字典序排序
        params = sorted([self.token, timestamp, nonce, encrypt])
        # SHA1 哈希
        sha1 = hashlib.sha1(''.join(params).encode('utf-8')).hexdigest()
        return sha1

    def _encrypt(self, plaintext: str) -> str:
        """加密消息"""
        # 1. 随机 16 字节 (AES 要求)
        import os
        random_bytes = os.urandom(16)

        # 2. 消息长度 (4字节网络字节序)
        msg_len = struct.pack('!I', len(plaintext))

        # 3. 拼接: random(16B) + msg_len(4B) + plaintext + corp_id
        plaintext_bytes = plaintext.encode('utf-8')
        corp_id_bytes = self.corp_id.encode('utf-8')
        raw_msg = random_bytes + msg_len + plaintext_bytes + corp_id_bytes

        # 4. PKCS7 填充
        raw_msg = self._pkcs7_pad(raw_msg)

        # 5. AES-CBC 加密
        cipher = AES.new(self.aes_key, AES.MODE_CBC, self.aes_key[:16])
        encrypted = cipher.encrypt(raw_msg)

        # 6. Base64 编码
        return base64.b64encode(encrypted).decode('utf-8')

    def _decrypt(self, encrypt: str) -> str:
        """解密消息"""
        # 1. Base64 解码
        encrypted_bytes = base64.b64decode(encrypt)

        # 2. AES-CBC 解密
        cipher = AES.new(self.aes_key, AES.MODE_CBC, self.aes_key[:16])
        decrypted = cipher.decrypt(encrypted_bytes)

        # 3. 去除 PKCS7 填充
        decrypted = self._pkcs7_unpad(decrypted)

        # 4. 解析: random(16B) + msg_len(4B) + plaintext + corp_id
        msg_len = struct.unpack('!I', decrypted[16:20])[0]
        plaintext = decrypted[20:20 + msg_len].decode('utf-8')

        # 5. 验证 corp_id
        corp_id_from_msg = decrypted[20 + msg_len:].decode('utf-8')
        if corp_id_from_msg != self.corp_id:
            raise ValueError(f"corp_id 不匹配: 预期 {self.corp_id}, 实际 {corp_id_from_msg}")

        return plaintext

    @staticmethod
    def _pkcs7_pad(data: bytes) -> bytes:
        """PKCS7 填充"""
        block_size = 32  # 企业微信使用 32 字节块
        padding_len = block_size - (len(data) % block_size)
        padding = bytes([padding_len] * padding_len)
        return data + padding

    @staticmethod
    def _pkcs7_unpad(data: bytes) -> bytes:
        """去除 PKCS7 填充"""
        padding_len = data[-1]
        return data[:-padding_len]
