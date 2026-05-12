# -*- coding: utf-8 -*-
"""生图结果上传腾讯云 COS（私有读写）+ 预签名 URL"""
import uuid

from qcloud_cos import CosConfig, CosS3Client

from app.config import COS_SECRET_ID, COS_SECRET_KEY, COS_BUCKET, COS_REGION

_client = None

# 预签名 URL 有效期（秒），略大于生命周期 TTL 避免卡片渲染时过期
_PRESIGN_EXPIRES = 600


def _get_client() -> CosS3Client:
    global _client
    if _client is None:
        if not all([COS_SECRET_ID, COS_SECRET_KEY, COS_BUCKET]):
            raise RuntimeError("COS 配置不完整（COS_SECRET_ID / COS_SECRET_KEY / COS_BUCKET）")
        config = CosConfig(Region=COS_REGION, SecretId=COS_SECRET_ID, SecretKey=COS_SECRET_KEY, Scheme="https")
        _client = CosS3Client(config)
        print(f"[COS] 客户端已初始化 (region={COS_REGION}, bucket={COS_BUCKET})")
    return _client


def save_image(image_bytes: bytes, ext: str = ".png") -> tuple[str, str]:
    """
    上传图片到 COS（私有读写），返回 (object_key, presigned_url)
    URL 10 分钟有效，配合 COS 生命周期自动清理。
    """
    client = _get_client()
    key = f"images/{uuid.uuid4().hex}{ext}"
    client.put_object(
        Bucket=COS_BUCKET,
        Body=image_bytes,
        Key=key,
        ContentType="image/png",
    )
    url = client.get_presigned_url(
        Method="GET",
        Bucket=COS_BUCKET,
        Key=key,
        Expires=_PRESIGN_EXPIRES,
    )
    return key, url
