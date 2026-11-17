# -*- coding: utf-8 -*-
"""
统一生图模块，支持 Gemini 原生图片、旧 Imagen 和 OpenAI Images 协议。
"""
import asyncio
import base64
import hashlib
import os
from typing import List

from app import config as app_config
from app.bqb_image import detect_image_format
from app.config import (
    GEMINI_IMAGE_MODEL,
    GEMINI_IMAGE_EDIT_MODEL,
    GEMINI_API_BASE,
    OPENAI_IMAGE_MODEL,
    SOCKS_PROXY,
    OPENAI_API_BASE,
    OPENAI_API_KEY_CUSTOM,
)

# Gemini 原生图片模型通过对话/原生 generate_content 端点走中转；旧 Imagen
# :predict 端点仍由直连 client 调用。两者必须明确分开，避免把中转 key 发给
# Google 直连端点或把 Imagen 请求发到中转站不支持的路径。
from app.gemini_client import client as genai_client
from app.gemini_client import direct_client

OPENAI_IMAGE_COMPLEXITY = getattr(app_config, "OPENAI_IMAGE_COMPLEXITY", "")


def _is_gemini_content_image_model(model: str) -> bool:
    """判断模型是否使用 Gemini 原生图片输出协议。"""
    normalized = (model or "").strip().lower()
    return normalized.startswith("gemini-") and "imagen" not in normalized


def _gemini_client_for_model(model: str):
    """按协议选择 Gemini 中转 client 或 Google 直连 client。"""
    return genai_client if _is_gemini_content_image_model(model) else direct_client


def _ensure_gemini_content_auth():
    """中转原生图片路径必须使用显式中转 key，禁止隐式复用直连 key。"""
    if GEMINI_API_BASE and not os.getenv("GEMINI_API_BASE_KEY", "").strip():
        raise RuntimeError("Gemini 图片中转 key 未配置")


def _deduplicate_images(images: List[bytes], limit: int) -> List[bytes]:
    """按图片 bytes 的 SHA-256 去重并保留原始顺序。"""
    unique: List[bytes] = []
    seen: set[bytes] = set()
    for image in images:
        if not image:
            continue
        digest = hashlib.sha256(image).digest()
        if digest in seen:
            continue
        seen.add(digest)
        unique.append(image)
        if len(unique) >= limit:
            break
    return unique


def _extract_gemini_inline_images(response) -> List[bytes]:
    """从 Gemini response 的 candidates.parts 中提取图片 inline_data。"""
    parts = []
    for candidate in getattr(response, "candidates", None) or []:
        content = getattr(candidate, "content", None)
        parts.extend(getattr(content, "parts", None) or [])
    if not parts:
        parts.extend(getattr(response, "parts", None) or [])

    images = []
    for part in parts:
        if getattr(part, "thought", False):
            continue
        inline_data = getattr(part, "inline_data", None)
        mime_type = getattr(inline_data, "mime_type", "") if inline_data else ""
        data = getattr(inline_data, "data", None) if inline_data else None
        if data and isinstance(mime_type, str) and mime_type.startswith("image/"):
            images.append(bytes(data))
    return images


def _openai_image_kwargs(model_name: str, size: str, **kwargs):
    """构造 OpenAI Images 参数，并只向 GPT Image 2.6 下发其专属字段。"""
    normalized = (model_name or "").strip().lower()
    params = dict(kwargs)
    params["size"] = size
    if normalized.startswith("gpt-image-2.6-"):
        complexity = str(OPENAI_IMAGE_COMPLEXITY or "").strip().lower()
        if complexity:
            params["complexity"] = complexity
        params["output_format"] = "png"
        params.pop("response_format", None)
    return params


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


_openai_image_client = None


def _get_openai_client():
    """创建或复用 OpenAI 客户端"""
    global _openai_image_client
    if _openai_image_client is None:
        from openai import OpenAI
        import httpx

        proxy_url = SOCKS_PROXY.replace("socks5h://", "socks5://") if SOCKS_PROXY else None
        # 图片超时后的服务端结果不确定，自动重发可能重复计费。
        kwargs = {"timeout": 300.0, "max_retries": 0}
        if OPENAI_API_BASE:
            kwargs["http_client"] = httpx.Client(trust_env=False, timeout=300.0)
        elif proxy_url:
            kwargs["http_client"] = httpx.Client(proxy=proxy_url, timeout=300.0)
        if OPENAI_API_BASE:
            kwargs["base_url"] = OPENAI_API_BASE
        if OPENAI_API_KEY_CUSTOM:
            kwargs["api_key"] = OPENAI_API_KEY_CUSTOM
        _openai_image_client = OpenAI(**kwargs)
    return _openai_image_client


async def _generate_with_gemini(
    prompt: str,
    aspect_ratio: str = "1:1",
    number_of_images: int = 1,
) -> List[bytes]:
    """调用 Gemini Imagen 或原生图片模型生图。"""
    from google.genai import types

    loop = asyncio.get_running_loop()

    target_count = max(1, min(4, int(number_of_images)))

    if _is_gemini_content_image_model(GEMINI_IMAGE_MODEL):
        client = _gemini_client_for_model(GEMINI_IMAGE_MODEL)
        collected: List[bytes] = []
        # 某些中转/模型会在一次响应中重复同一张图片；有界重试补齐数量，
        # 避免无限请求，也不把重复图片当成多张结果。
        max_attempts = target_count
        _ensure_gemini_content_auth()

        def _call_content():
            return client.models.generate_content(
                model=GEMINI_IMAGE_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    http_options=types.HttpOptions(retry_options=types.HttpRetryOptions(attempts=1)),
                    response_modalities=["IMAGE", "TEXT"],
                    image_config=types.ImageConfig(aspect_ratio=aspect_ratio),
                ),
            )

        for _ in range(max_attempts):
            response = await loop.run_in_executor(None, _call_content)
            collected.extend(_extract_gemini_inline_images(response))
            unique = _deduplicate_images(collected, target_count)
            if len(unique) >= target_count:
                return unique

        unique = _deduplicate_images(collected, target_count)
        if not unique:
            raise RuntimeError("图片生成被安全过滤器拒绝，或无法生成图片")
        raise RuntimeError(
            f"图片生成返回数量不足（需要 {target_count} 张，实际 {len(unique)} 张）"
        )

    # Deprecated：旧 Imagen 直连协议；当前生产使用原生 Gemini 图片模型。
    def _call():
        response = direct_client.models.generate_images(
            model=GEMINI_IMAGE_MODEL,
            prompt=prompt,
            config=types.GenerateImagesConfig(
                number_of_images=target_count,
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

    unique = _deduplicate_images(images, target_count)
    if not unique:
        raise RuntimeError("图片生成被安全过滤器拒绝，或无法生成图片")
    if len(unique) < target_count:
        raise RuntimeError(
            f"图片生成返回数量不足（需要 {target_count} 张，实际 {len(unique)} 张）"
        )
    return unique


async def _generate_with_openai(
    prompt: str,
    aspect_ratio: str = "1:1",
    number_of_images: int = 1,
) -> List[bytes]:
    """调用 OpenAI Images 生图。"""
    loop = asyncio.get_running_loop()
    client = _get_openai_client()
    size = _map_openai_size(aspect_ratio)

    def _call():
        params = _openai_image_kwargs(
            OPENAI_IMAGE_MODEL,
            size,
            model=OPENAI_IMAGE_MODEL,
            prompt=prompt,
            n=number_of_images,
            response_format="b64_json",
        )
        return client.images.generate(**params)

    response = await loop.run_in_executor(None, _call)

    images = []
    for img in response.data:
        if img.b64_json:
            images.append(base64.b64decode(img.b64_json))

    if not images:
        raise RuntimeError("OpenAI 未返回有效图片数据")

    return images


async def _edit_with_openai(
    image_bytes: bytes,
    prompt: str,
    aspect_ratio: str = "1:1",
) -> List[bytes]:
    """OpenAI gpt-image-2 图片编辑（img2img）"""
    import io
    loop = asyncio.get_running_loop()
    client = _get_openai_client()
    size = _map_openai_size(aspect_ratio)
    image_format = detect_image_format(image_bytes, allow_webp=True)
    if image_format is None:
        raise RuntimeError("无法识别输入图片格式")
    extension, mime_type = image_format
    filename = f"image.{extension}"

    def _call():
        params = _openai_image_kwargs(
            OPENAI_IMAGE_MODEL,
            size,
            model=OPENAI_IMAGE_MODEL,
            image=(filename, io.BytesIO(image_bytes), mime_type),
            prompt=prompt,
            n=1,
            response_format="b64_json",
        )
        return client.images.edit(**params)

    response = await loop.run_in_executor(None, _call)
    images = []
    for img in response.data:
        if img.b64_json:
            images.append(base64.b64decode(img.b64_json))
    if not images:
        raise RuntimeError("OpenAI edit 未返回有效图片数据")
    return images


async def _edit_with_gemini(
    image_bytes: bytes,
    prompt: str,
) -> List[bytes]:
    """Gemini 图片编辑（generate_content 多模态输出）。"""
    from google.genai import types
    loop = asyncio.get_running_loop()
    client = _gemini_client_for_model(GEMINI_IMAGE_EDIT_MODEL)
    if _is_gemini_content_image_model(GEMINI_IMAGE_EDIT_MODEL):
        _ensure_gemini_content_auth()
    image_format = detect_image_format(image_bytes, allow_webp=True)
    if image_format is None:
        raise RuntimeError("无法识别输入图片格式")
    _, mime_type = image_format

    def _call():
        response = client.models.generate_content(
            model=GEMINI_IMAGE_EDIT_MODEL,
            contents=[
                types.Part(inline_data=types.Blob(mime_type=mime_type, data=image_bytes)),
                types.Part(text=prompt),
            ],
            config=types.GenerateContentConfig(
                http_options=types.HttpOptions(retry_options=types.HttpRetryOptions(attempts=1)),
                response_modalities=["IMAGE", "TEXT"],
            ),
        )
        return _deduplicate_images(_extract_gemini_inline_images(response), 4)

    images = await loop.run_in_executor(None, _call)
    if not images:
        raise RuntimeError("Gemini 图片编辑未返回图片，模型可能拒绝了该操作")
    return images


async def edit_image(
    image_bytes: bytes,
    prompt: str,
    backend: str = "gemini",
    aspect_ratio: str = "1:1",
) -> List[bytes]:
    """
    统一改图接口（img2img）

    Args:
        image_bytes: 原图 bytes
        prompt: 修改描述（英文）
        backend: "gemini" 或 "openai"
        aspect_ratio: 输出比例（仅 openai 有效）

    Returns:
        改后图片 bytes 列表
    """
    if backend == "gemini":
        return await _edit_with_gemini(image_bytes, prompt)
    elif backend == "openai":
        return await _edit_with_openai(image_bytes, prompt, aspect_ratio)
    else:
        raise ValueError(f"后端 {backend} 不支持图片编辑（Claude/OpenRouter 无图像生成能力）")


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
