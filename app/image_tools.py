"""图片函数工具的固定权限、每轮预算与执行适配。"""
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field

_ASPECT_RATIOS = ("1:1", "3:4", "4:3", "9:16", "16:9")
_MAX_ARGUMENT_BYTES = 8192


def image_tool_declarations() -> list[dict]:
    prompt = {"type": "string", "description": "Faithful visual instructions for the requested image."}
    ratio = {"type": "string", "enum": list(_ASPECT_RATIOS)}
    return [
        {
            "name": "generate_image",
            "description": "Create new images when the user asks to draw or generate an image. This tool actually renders images. Do not invent image URLs. Do not call for existing stickers or for a text-only answer.",
            "parameters": {
                "type": "object",
                "properties": {"prompt": prompt, "aspect_ratio": ratio, "number_of_images": {"type": "integer", "minimum": 1, "maximum": 4}},
                "required": ["prompt"],
                "additionalProperties": False,
            },
        },
        {
            "name": "edit_image",
            "description": "Edit a current uploaded or explicitly quoted image when the user requests a visual change. image_index is 1-based. Ordinary historical images are not eligible. Do not call merely to describe or identify an image.",
            "parameters": {
                "type": "object",
                "properties": {"prompt": prompt, "image_index": {"type": "integer", "minimum": 1}, "aspect_ratio": ratio},
                "required": ["prompt", "image_index"],
                "additionalProperties": False,
            },
        },
    ]


@dataclass
class ImageToolResult:
    images: list[bytes] = field(default_factory=list)
    response: dict = field(default_factory=dict)


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


class ImageToolSession:
    max_calls = 2

    def __init__(self, backend: str, input_images=None):
        if backend not in {"gemini", "openai"}:
            raise ValueError("不支持的图片工具后端")
        self.backend = backend
        self.input_images = list(input_images or [])[:4]
        self.calls = 0
        self._image_count = 0
        self._seen: set[bytes] = set()
        self._lock = asyncio.Lock()

    @property
    def remaining_images(self) -> int:
        return 4 - self._image_count

    async def execute(self, name: str, arguments) -> ImageToolResult:
        async with self._lock:
            if self.calls >= self.max_calls or self.remaining_images <= 0:
                return ImageToolResult(response={"ok": False, "error": "本轮图片工具预算已用完，请在下一条消息继续。"})
            # 失败和无效调用同样占用次数，防止模型反复试探参数绕过每轮预算。
            self.calls += 1
            try:
                encoded = arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)
                if len(encoded.encode("utf-8")) > _MAX_ARGUMENT_BYTES:
                    raise ValueError("argument size")
                args = json.loads(encoded, object_pairs_hook=_unique_json_object)
                if not isinstance(args, dict):
                    raise ValueError("arguments")
                allowed = {"prompt", "aspect_ratio", "number_of_images"} if name == "generate_image" else {"prompt", "aspect_ratio", "image_index"}
                if name not in {"generate_image", "edit_image"} or set(args) - allowed:
                    raise ValueError("tool or fields")
                prompt = args.get("prompt")
                if not isinstance(prompt, str) or not prompt.strip():
                    raise ValueError("prompt")
                ratio = args.get("aspect_ratio", "1:1")
                if ratio not in _ASPECT_RATIOS:
                    raise ValueError("ratio")
                count = args.get("number_of_images", 1)
                index = args.get("image_index", 1)
                if type(count) is not int or not 1 <= count <= self.remaining_images:
                    raise ValueError("count")
                if name == "edit_image" and (type(index) is not int or not 1 <= index <= len(self.input_images)):
                    raise ValueError("input image")
            except (TypeError, ValueError, OverflowError, RecursionError):
                return ImageToolResult(response={"ok": False, "error": "图片工具参数无效，或没有当前上传/显式引用的可编辑图片。"})

            # 避免 image_gen → gemini_client → image_tools 的模块初始化环。
            from app.image_gen import edit_image, generate_image

            expected = count if name == "generate_image" else 1
            self._image_count += expected
            try:
                if name == "generate_image":
                    output = await generate_image(prompt, backend=self.backend, aspect_ratio=ratio, number_of_images=count)
                else:
                    output = await edit_image(self.input_images[index - 1], prompt, backend=self.backend, aspect_ratio=ratio)
                images = []
                for data in output:
                    if not isinstance(data, bytes) or not data:
                        continue
                    digest = hashlib.sha256(data).digest()
                    if digest not in self._seen:
                        self._seen.add(digest)
                        images.append(data)
                if len(images) != expected or any(len(data) > 20 * 1024 * 1024 for data in images):
                    return ImageToolResult(response={"ok": False, "error": "图片工具未返回所需数量的不同图片，请在下一条消息重试。"})
                self.input_images.extend(images)
                return ImageToolResult(images=images, response={"ok": True, "image_count": len(images), "available_image_count": len(self.input_images), "delivery": "The application will display the real images in this same reply card. Do not output or invent image URLs."})
            except asyncio.CancelledError:
                raise
            except Exception:
                return ImageToolResult(response={"ok": False, "error": "图片生成或修改失败，请稍后重试；不要声称已完成图片。"})
