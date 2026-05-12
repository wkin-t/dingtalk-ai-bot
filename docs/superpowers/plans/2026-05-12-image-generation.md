# 双后端生图功能 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为钉钉 AI 机器人增加图片生成能力，Gemini 用 Imagen 4，GPT 用 gpt-image-2，通过预分析路由触发，结果以独立图片消息发送。

**Architecture:** 扩展现有预分析模块增加 `need_image_gen` 字段检测。新建 `app/image_gen.py` 统一生图模块，封装两个后端 API。在 `handle_gemini_stream()` 的路由判断后插入生图分支，跳过正常 AI 流，直接上传图片并推送独立消息。

**Tech Stack:** `google-genai` SDK (`generate_images`), `openai` SDK (`images.generate`), 钉钉 `upload_media` + `sampleImageMsg`

---

## File Structure

| 文件 | 操作 | 职责 |
|------|------|------|
| `app/image_gen.py` | **新建** | 统一生图模块：Gemini Imagen 4 + OpenAI gpt-image-2 |
| `app/config.py` | 修改 | 增加生图相关配置常量 |
| `app/gemini_client.py` | 修改 | `analyze_complexity_with_model()` 预分析 prompt 增加 `need_image_gen` 字段 |
| `app/dingtalk_bot.py` | 修改 | `_analyze_with_litellm()` 增加 `need_image_gen`；`handle_gemini_stream()` 增加生图分支 |
| `tests/test_image_gen.py` | **新建** | 生图模块单元测试 |

---

### Task 1: 配置常量 — `app/config.py`

**Files:**
- Modify: `app/config.py:236-241` (在 `DINGTALK_IMAGE_MSG_PARAM_TEMPLATE` 后面)

- [ ] **Step 1: 添加生图配置常量**

在 `DINGTALK_IMAGE_MSG_PARAM_TEMPLATE` 定义之后（约 line 241）追加：

```python
# ===== 生图配置 =====
GEMINI_IMAGE_MODEL = os.environ.get("GEMINI_IMAGE_MODEL", "imagen-4.0-generate-001")
OPENAI_IMAGE_MODEL = os.environ.get("OPENAI_IMAGE_MODEL", "gpt-image-2")
DEFAULT_IMAGE_ASPECT_RATIO = os.environ.get("DEFAULT_IMAGE_ASPECT_RATIO", "1:1")
DEFAULT_IMAGE_COUNT = max(1, min(4, _get_int("DEFAULT_IMAGE_COUNT", 1)))
```

- [ ] **Step 2: 验证编译通过**

Run: `python -m compileall -q app/config.py`
Expected: 无输出（编译成功）

- [ ] **Step 3: Commit**

```bash
git add app/config.py
git commit -m "feat: add image generation config constants"
```

---

### Task 1.5: 修复 JSON 解析器 — `app/gemini_client.py` + `app/dingtalk_bot.py`

**问题**: 两个预分析函数都用 `re.search(r'\{[^}]+\}', text)` 提取 JSON。`[^}]+` 遇到嵌套对象 `{"image_gen_params":{"prompt":"..."}}` 会在第一个 `}` 截断，导致 JSON 解析失败 → 生图路由永远不触发。

**Files:**
- Modify: `app/gemini_client.py:121` (JSON 解析)
- Modify: `app/dingtalk_bot.py:576` (JSON 解析)

- [ ] **Step 1: 修复 `app/gemini_client.py` 的 JSON 解析**

将 line 121：
```python
        json_match = re.search(r'\{[^}]+\}', result_text)
```
改为：
```python
        # 提取最外层 JSON 对象（支持嵌套）
        json_match = re.search(r'\{.*\}', result_text, re.DOTALL)
```

- [ ] **Step 2: 修复 `app/dingtalk_bot.py` 的 JSON 解析**

将 line 576：
```python
        json_match = re.search(r'\{[^}]+\}', result_text)
```
改为：
```python
        # 提取最外层 JSON 对象（支持嵌套）
        json_match = re.search(r'\{.*\}', result_text, re.DOTALL)
```

- [ ] **Step 3: 验证编译 + 运行已有测试**

Run: `python -m compileall -q app/gemini_client.py app/dingtalk_bot.py && pytest -q tests/test_gemini_client.py`
Expected: 编译通过，全部测试 PASS

- [ ] **Step 4: Commit**

```bash
git add app/gemini_client.py app/dingtalk_bot.py
git commit -m "fix: update JSON parser to support nested objects for image_gen_params"
```

---

### Task 2: 统一生图模块 — `app/image_gen.py`

**Files:**
- Create: `app/image_gen.py`
- Test: `tests/test_image_gen.py`

- [ ] **Step 1: 写失败测试 `tests/test_image_gen.py`**

```python
# -*- coding: utf-8 -*-
"""生图模块单元测试"""
import pytest
import asyncio
import base64
from unittest.mock import patch, MagicMock


class TestGenerateImage:
    """generate_image() 统一接口测试"""

    @pytest.mark.asyncio
    async def test_gemini_backend_success(self):
        """Gemini 后端正常生图"""
        from app.image_gen import generate_image

        fake_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        mock_img = MagicMock()
        mock_img.image.image_bytes = fake_png
        mock_response = MagicMock()
        mock_response.generated_images = [mock_img]

        with patch("app.image_gen._generate_with_gemini") as mock_gen:
            mock_gen.return_value = [fake_png]
            images = await generate_image("a cat", backend="gemini")

        assert len(images) == 1
        assert images[0] == fake_png

    @pytest.mark.asyncio
    async def test_openai_backend_success(self):
        """OpenAI 后端正常生图"""
        from app.image_gen import generate_image

        fake_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100

        with patch("app.image_gen._generate_with_openai") as mock_gen:
            mock_gen.return_value = [fake_png]
            images = await generate_image("a cat", backend="openai")

        assert len(images) == 1
        assert images[0] == fake_png

    @pytest.mark.asyncio
    async def test_invalid_backend_raises(self):
        """无效后端抛出 ValueError"""
        from app.image_gen import generate_image

        with pytest.raises(ValueError, match="不支持的后端"):
            await generate_image("a cat", backend="unknown")

    @pytest.mark.asyncio
    async def test_default_params(self):
        """默认参数传递正确"""
        from app.image_gen import generate_image

        with patch("app.image_gen._generate_with_gemini") as mock_gen:
            mock_gen.return_value = [b"fake"]
            await generate_image("test", backend="gemini")
            mock_gen.assert_called_once_with("test", "1:1", 1)

    @pytest.mark.asyncio
    async def test_custom_params(self):
        """自定义参数传递正确"""
        from app.image_gen import generate_image

        with patch("app.image_gen._generate_with_openai") as mock_gen:
            mock_gen.return_value = [b"fake"]
            await generate_image("test", backend="openai", aspect_ratio="16:9", number_of_images=3)
            mock_gen.assert_called_once_with("test", "16:9", 3)


class TestGeminiBackend:
    """Gemini Imagen 4 后端测试"""

    @pytest.mark.asyncio
    async def test_success(self):
        """正常调用返回图片 bytes"""
        from app.image_gen import _generate_with_gemini

        fake_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        mock_img = MagicMock()
        mock_img.image.image_bytes = fake_png
        mock_response = MagicMock()
        mock_response.generated_images = [mock_img]

        with patch("app.image_gen.genai_client") as mock_client:
            mock_client.models.generate_images.return_value = mock_response
            images = await _generate_with_gemini("a cat", "1:1", 1)

        assert len(images) == 1
        assert images[0] == fake_png

    @pytest.mark.asyncio
    async def test_empty_response_raises(self):
        """安全过滤器拒绝时抛出异常"""
        from app.image_gen import _generate_with_gemini

        mock_response = MagicMock()
        mock_response.generated_images = []

        with patch("app.image_gen.genai_client") as mock_client:
            mock_client.models.generate_images.return_value = mock_response
            with pytest.raises(RuntimeError, match="无法生成图片"):
                await _generate_with_gemini("inappropriate content", "1:1", 1)


class TestOpenAIBackend:
    """OpenAI gpt-image-2 后端测试"""

    @pytest.mark.asyncio
    async def test_success(self):
        """正常调用返回图片 bytes"""
        from app.image_gen import _generate_with_openai

        fake_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        b64_data = base64.b64encode(fake_png).decode()

        mock_img = MagicMock()
        mock_img.b64_json = b64_data
        mock_response = MagicMock()
        mock_response.data = [mock_img]

        with patch("app.image_gen._get_openai_client") as mock_get_client:
            mock_client = MagicMock()
            mock_client.images.generate.return_value = mock_response
            mock_get_client.return_value = mock_client
            images = await _generate_with_openai("a cat", "1:1", 1)

        assert len(images) == 1
        assert images[0] == fake_png

    @pytest.mark.asyncio
    async def test_size_mapping(self):
        """aspect_ratio 正确映射为 size"""
        from app.image_gen import _map_openai_size

        assert _map_openai_size("1:1") == "1024x1024"
        assert _map_openai_size("3:4") == "1024x1536"
        assert _map_openai_size("4:3") == "1536x1024"
        assert _map_openai_size("9:16") == "1024x1792"
        assert _map_openai_size("16:9") == "1792x1024"
        assert _map_openai_size("unknown") == "1024x1024"  # fallback


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_image_gen.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'app.image_gen'`）

- [ ] **Step 3: 实现 `app/image_gen.py`**

```python
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
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_image_gen.py -v`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add app/image_gen.py tests/test_image_gen.py
git commit -m "feat: add unified image generation module (Imagen 4 + gpt-image-2)"
```

---

### Task 3: 扩展 Gemini 预分析 — `app/gemini_client.py`

**Files:**
- Modify: `app/gemini_client.py:70-98` (analysis_prompt 模板)

- [ ] **Step 1: 修改预分析 prompt**

在 `analysis_prompt` 的 "4. thinking_text:" 规则块之后、"重要:" 之前，插入 `need_image_gen` 和 `image_gen_params` 规则。同时修改 JSON 返回格式。

将 `analysis_prompt` 的规则部分从：

```
4. thinking_text:
   ...
重要: 如果问题涉及"今年"、"现在"、"当前时间"等，设置 need_search=true
只返回JSON:
{{"model":"gemini-3-flash-preview","thinking_level":"low","need_search":false,"reason":"简短原因","thinking_text":"正在思考 💭"}}
```

改为：

```
4. thinking_text:
   ...

5. need_image_gen:
   - true: 用户明确要求生成图片、画画、插图、绘制、画一张、生成图片
   - false: 不需要生图（默认）

6. image_gen_params (仅当 need_image_gen=true 时):
   - prompt: 提取用户描述的图片内容，转为英文描述（生图模型只支持英文）
   - aspect_ratio: 解析用户指定的比例 → "1:1" | "3:4" | "4:3" | "9:16" | "16:9"，默认 "1:1"
   - number_of_images: 解析数量 → 1-4，默认 1

重要: 如果问题涉及"今年"、"现在"、"当前时间"等，设置 need_search=true
只返回JSON:
{{"model":"gemini-3-flash-preview","thinking_level":"low","need_search":false,"need_image_gen":false,"reason":"简短原因","thinking_text":"正在思考 💭"}}
```

当 `need_image_gen=true` 时，JSON 示例：
```json
{"model":"gemini-3-flash-preview","thinking_level":"low","need_search":false,"need_image_gen":true,"image_gen_params":{"prompt":"A cute cat running under moonlight","aspect_ratio":"1:1","number_of_images":1},"reason":"用户要求画图","thinking_text":"正在画画 🎨"}
```

- [ ] **Step 2: 增加 `need_image_gen` 字段验证**

在 `analyze_complexity_with_model()` 函数的 JSON 解析和字段修正块（约 line 123-131）中，在 `if "need_search" not in result:` 之后追加：

```python
            if "need_image_gen" not in result:
                result["need_image_gen"] = False
```

- [ ] **Step 3: 增加 `max_output_tokens`**

将 `config=types.GenerateContentConfig(` 中的 `max_output_tokens=150` 改为 `max_output_tokens=300`（嵌套 `image_gen_params` + 英文 prompt 可达 250+ 字符）。

- [ ] **Step 4: 验证编译通过**

Run: `python -m compileall -q app/gemini_client.py`
Expected: 无输出

- [ ] **Step 5: 运行已有测试确认不破坏**

Run: `pytest tests/test_gemini_client.py -v`
Expected: 全部 PASS（旧测试不涉及 `need_image_gen` 字段，默认值处理兼容）

- [ ] **Step 6: Commit**

```bash
git add app/gemini_client.py
git commit -m "feat: extend Gemini pre-analysis with need_image_gen detection"
```

---

### Task 4: 扩展 LiteLLM 预分析 — `app/dingtalk_bot.py`

**Files:**
- Modify: `app/dingtalk_bot.py:529-557` (`_analyze_with_litellm` 的 analysis_prompt)

- [ ] **Step 1: 修改 LiteLLM 预分析 prompt**

在 `_analyze_with_litellm()` 函数中，对 `analysis_prompt` 做与 Task 3 完全相同的修改——在 "4. thinking_text" 之后增加 `need_image_gen` 和 `image_gen_params` 规则，更新 JSON 返回格式。

具体变更：将 `analysis_prompt` 的规则部分从：

```
4. thinking_text:
   ...
重要: 如果问题涉及"今年"、"现在"、"当前时间"等，设置 need_search=true
只返回JSON:
{{"model":"gemini-3-flash-preview","thinking_level":"low","need_search":false,"reason":"简短原因","thinking_text":"正在思考 💭"}}
```

改为：

```
4. thinking_text:
   ...

5. need_image_gen:
   - true: 用户明确要求生成图片、画画、插图、绘制、画一张、生成图片
   - false: 不需要生图（默认）

6. image_gen_params (仅当 need_image_gen=true 时):
   - prompt: 提取用户描述的图片内容，转为英文描述（生图模型只支持英文）
   - aspect_ratio: 解析用户指定的比例 → "1:1" | "3:4" | "4:3" | "9:16" | "16:9"，默认 "1:1"
   - number_of_images: 解析数量 → 1-4，默认 1

重要: 如果问题涉及"今年"、"现在"、"当前时间"等，设置 need_search=true
只返回JSON:
{{"model":"gemini-3-flash-preview","thinking_level":"low","need_search":false,"need_image_gen":false,"reason":"简短原因","thinking_text":"正在思考 💭"}}
```

- [ ] **Step 2: 增加 `need_image_gen` 字段验证**

在 `_analyze_with_litellm()` 函数的 JSON 解析和字段修正块（约 line 578-584）中，在 `if "need_search" not in result:` 之后追加：

```python
            if "need_image_gen" not in result:
                result["need_image_gen"] = False
```

同时修改函数末尾的降级返回值（约 line 593-599），在 dict 中增加 `"need_image_gen": False`。

- [ ] **Step 3: 增加 `max_tokens`**

将 `kwargs` 中的 `"max_tokens": 150` 改为 `"max_tokens": 300`（嵌套 JSON + 英文 prompt 需要更多 token）。

- [ ] **Step 4: 验证编译通过**

Run: `python -m compileall -q app/dingtalk_bot.py`
Expected: 无输出

- [ ] **Step 5: Commit**

```bash
git add app/dingtalk_bot.py
git commit -m "feat: extend LiteLLM pre-analysis with need_image_gen detection"
```

---

### Task 5: 生图分支逻辑 — `app/dingtalk_bot.py`

**Files:**
- Modify: `app/dingtalk_bot.py:1041` (路由判断后、AI 流开始前)

- [ ] **Step 1: 添加 import**

在文件顶部 import 区域（约 line 35 附近）追加：

```python
from app.image_gen import generate_image
```

- [ ] **Step 2: 在路由判断后插入生图分支**

在 `handle_gemini_stream()` 方法中，找到这段代码（约 line 1041）：

```python
            print(f"🎯 智能路由: {complexity.get('reason', '默认')} → 模型={target_model}, thinking={thinking_level}, search={need_search}")

        # 预分析完成后，用 AI 生成的思考状态更新卡片
```

在 `print(f"🎯 ...")` 这行之后、`# 预分析完成后` 注释之前，插入生图分支判断：

```python
        # ===== 生图分支 =====
        need_image_gen = complexity.get("need_image_gen", False) if AI_BACKEND != "openclaw" else False
        if need_image_gen:
            params = complexity.get("image_gen_params", {})
            image_prompt = params.get("prompt", content)
            aspect_ratio = params.get("aspect_ratio", "1:1")
            num_images = max(1, min(4, params.get("number_of_images", 1)))
            print(f"🎨 [生图] prompt={image_prompt[:80]}, ratio={aspect_ratio}, n={num_images}, backend={AI_BACKEND}")

            try:
                await self.card_helper.stream_update(
                    out_track_id,
                    "正在生成图片... 🎨",
                    is_finalize=False,
                    is_full=True,
                    content_key="thinkingText",
                )

                images = await generate_image(
                    image_prompt,
                    backend=AI_BACKEND,
                    aspect_ratio=aspect_ratio,
                    number_of_images=num_images,
                )

                if not images:
                    raise RuntimeError("生图 API 未返回任何图片")

                # 上传第一张图片
                media_id = await self.card_helper.upload_media(
                    images[0],
                    filetype="image",
                    filename="image.png",
                    mimetype="image/png",
                )

                if media_id:
                    msg_param = DINGTALK_IMAGE_MSG_PARAM_TEMPLATE.replace("{mediaId}", media_id)
                    if incoming_message.conversation_type == "2":
                        sent = await self.card_helper.send_group_message(
                            incoming_message.conversation_id,
                            DINGTALK_IMAGE_MSG_KEY,
                            msg_param,
                        )
                    else:
                        sent = await self.card_helper.send_private_chat_message(
                            incoming_message.conversation_id,
                            DINGTALK_IMAGE_MSG_KEY,
                            msg_param,
                        )

                    card_text = f"已为你生成 {len(images)} 张图片 ✨\n\n{image_prompt}"
                    if not sent:
                        card_text += "\n\n⚠️ 图片消息发送失败，请查看对话"
                else:
                    card_text = "⚠️ 图片上传失败，请稍后重试"

                await self.card_helper.stream_update(
                    out_track_id,
                    card_text,
                    is_finalize=True,
                    is_full=True,
                )
                print(f"✅ [生图] 完成，{len(images)} 张")

            except RuntimeError as e:
                error_msg = str(e)
                print(f"⚠️ [生图] 业务错误: {error_msg}")
                if "安全过滤" in error_msg:
                    friendly = "图片生成被安全过滤器拒绝，请调整描述后重试"
                elif "无法生成" in error_msg:
                    friendly = "无法生成图片，请尝试其他描述"
                else:
                    friendly = f"图片生成失败：{error_msg}"
                await self.card_helper.stream_update(out_track_id, friendly, is_finalize=True, is_full=True)

            except Exception as e:
                print(f"❌ [生图] 异常: {e}")
                await self.card_helper.stream_update(
                    out_track_id,
                    "图片生成失败，请稍后重试 🥲",
                    is_finalize=True,
                    is_full=True,
                )

            return  # 跳过正常 AI 流
        # ===== 生图分支结束 =====
```

- [ ] **Step 3: 验证编译通过**

Run: `python -m compileall -q app/dingtalk_bot.py`
Expected: 无输出

- [ ] **Step 4: 运行全部测试**

Run: `python -m compileall -q app main.py && pytest -q tests`
Expected: 编译通过，全部测试 PASS

- [ ] **Step 5: Commit**

```bash
git add app/dingtalk_bot.py
git commit -m "feat: add image generation branch in message handler"
```

---

### Task 6: 编译检查 + 全量测试

**Files:**
- 无变更，仅验证

- [ ] **Step 1: 全量编译检查**

Run: `python -m compileall -q app main.py`
Expected: 无输出（全部编译通过）

- [ ] **Step 2: 运行全部测试**

Run: `pytest -q tests`
Expected: 全部 PASS

- [ ] **Step 3: 确认无遗漏**

对照设计文档检查：
- [x] 新增 `app/image_gen.py`
- [x] 修改 `app/config.py` — 生图配置
- [x] 修改 `app/gemini_client.py` — 预分析 prompt + 字段验证
- [x] 修改 `app/dingtalk_bot.py` — LiteLLM 预分析 + 生图分支
- [x] 新增 `tests/test_image_gen.py`
- [x] 不变的文件: `app/ai/handler.py`, `app/ai/backend.py`, `app/ai/router.py`, `app/dingtalk_card.py`

---

## 前置条件（用户操作，部署前）

1. **OpenResty 超时**：`/www/sites/clip.ifitnesslog.cn/proxy/root.conf` 中需持久化 `proxy_read_timeout 180s;`（通过 1Panel 修改，容器内临时修改重启会丢）
2. **EdgeOne CDN**：确保 `/v1/images/generations` 路径超时 ≥ 180s
3. **环境变量**（可选）：`GEMINI_IMAGE_MODEL`, `OPENAI_IMAGE_MODEL`, `DEFAULT_IMAGE_ASPECT_RATIO`, `DEFAULT_IMAGE_COUNT`

## 降级策略（已在设计文档中定义）

| 场景 | 处理 |
|------|------|
| 预分析失败 | `need_image_gen=false`，走正常对话 |
| `image_gen_params` 缺失 | 用原始消息作 prompt，默认参数 |
| 生图 API 安全过滤 | 卡片显示 "图片生成被安全过滤器拒绝" |
| 生图 API 超时 | 卡片显示 "图片生成失败，请稍后重试" |
| 上传失败 | 卡片显示 "图片上传失败" |
| 发送失败 | 卡片文字追加 "图片消息发送失败" 警告 |
