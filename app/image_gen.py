# -*- coding: utf-8 -*-
"""
统一生图模块
Gemini: imagen-4.0-generate-001 (google-genai SDK)
OpenAI: gpt-image-2 (openai SDK)
"""
import asyncio
import base64
from typing import List

from app.config import (
    GEMINI_IMAGE_MODEL,
    OPENAI_IMAGE_MODEL,
    SOCKS_PROXY,
    OPENAI_API_BASE,
    OPENAI_API_KEY_CUSTOM,
)

# 复用 gemini_client.py 的 genai.Client 实例（已配置代理）
from app.gemini_client import client as genai_client


def _map_openai_size(aspect_ratio: str) -> str:
    """将 aspect_ratio 映射为 OpenAI images.generate 的 size 参数"""
    mapping = {
        "1:1": "1024x1024",
        "3:4": "1024x1536",
        "4:3": "1536x1024",
        "9:16": "1024x1792",
        "16:9": "1792x1024",
    }
    return mapping.get(aspect_ratio, "1024x1024")


def _get_openai_client():
    """创建 OpenAI 客户端（复用代理配置）"""
    from openai import OpenAI
    import httpx

    proxy_url = SOCKS_PROXY.replace("socks5h://", "socks5://") if SOCKS_PROXY else None
    kwargs = {
        "timeout": 300.0,
    }
    if proxy_url:
        kwargs["http_client"] = httpx.Client(proxy=proxy_url, timeout=300.0)
    if OPENAI_API_BASE:
        kwargs["base_url"] = OPENAI_API_BASE
    if OPENAI_API_KEY_CUSTOM:
        kwargs["api_key"] = OPENAI_API_KEY_CUSTOM

    return OpenAI(**kwargs)


async def _generate_with_gemini(
    prompt: str,
    aspect_ratio: str = "1:1",
    number_of_images: int = 1,
) -> List[bytes]:
    """调用 Gemini Imagen 4 生图"""
    from google.genai import types

    loop = asyncio.get_running_loop()

    def _call():
        response = genai_client.models.generate_images(
            model=GEMINI_IMAGE_MODEL,
            prompt=prompt,
            config=types.GenerateImagesConfig(
                number_of_images=number_of_images,
                aspect_ratio=aspect_ratio,
            ),
        )
        return response

    response = await loop.run_in_executor(None, _call)

    if not response.generated_images:
        raise RuntimeError("图片生成被安全过滤器拒绝，或无法生成图片")

    images = []
    for img in response.generated_images:
        images.append(img.image.image_bytes)

    return images


async def _generate_with_openai(
    prompt: str,
    aspect_ratio: str = "1:1",
    number_of_images: int = 1,
) -> List[bytes]:
    """调用 OpenAI gpt-image-2 生图"""
    loop = asyncio.get_running_loop()
    client = _get_openai_client()
    size = _map_openai_size(aspect_ratio)

    def _call():
        return client.images.generate(
            model=OPENAI_IMAGE_MODEL,
            prompt=prompt,
            n=number_of_images,
            size=size,
            response_format="b64_json",
        )

    response = await loop.run_in_executor(None, _call)

    images = []
    for img in response.data:
        if img.b64_json:
            images.append(base64.b64decode(img.b64_json))

    if not images:
        raise RuntimeError("OpenAI 未返回有效图片数据")

    return images


async def generate_image(
    prompt: str,
    backend: str = "gemini",
    aspect_ratio: str = "1:1",
    number_of_images: int = 1,
) -> List[bytes]:
    """
    统一生图接口

    Args:
        prompt: 图片描述（英文）
        backend: "gemini" 或 "openai"
        aspect_ratio: "1:1" | "3:4" | "4:3" | "9:16" | "16:9"
        number_of_images: 1-4

    Returns:
        图片 bytes 列表
    """
    if backend == "gemini":
        return await _generate_with_gemini(prompt, aspect_ratio, number_of_images)
    elif backend == "openai":
        return await _generate_with_openai(prompt, aspect_ratio, number_of_images)
    else:
        raise ValueError(f"不支持的后端: {backend}")
