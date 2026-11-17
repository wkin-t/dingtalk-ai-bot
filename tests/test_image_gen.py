# -*- coding: utf-8 -*-
"""生图模块单元测试"""
import pytest
import asyncio
import base64
from types import SimpleNamespace
from unittest.mock import patch, MagicMock


@pytest.mark.parametrize("base, expected_transport", [
    ("http://relay.example:38090/v1", {"trust_env": False, "timeout": 300.0}),
    ("", {"proxy": "socks5://127.0.0.1:1080", "timeout": 300.0}),
])
def test_openai_image_client_routes_gateway_direct_and_native_through_proxy(monkeypatch, base, expected_transport):
    import app.image_gen as image_gen

    monkeypatch.setattr(image_gen, "_openai_image_client", None)
    monkeypatch.setattr(image_gen, "OPENAI_API_BASE", base)
    monkeypatch.setattr(image_gen, "SOCKS_PROXY", "socks5h://127.0.0.1:1080")
    monkeypatch.setattr(image_gen, "OPENAI_API_KEY_CUSTOM", "sk-test")
    with patch("httpx.Client") as transport, patch("openai.OpenAI") as client:
        image_gen._get_openai_client()
    transport.assert_called_once_with(**expected_transport)
    assert client.call_args.kwargs["http_client"] is transport.return_value


class TestGenerateImage:
    """generate_image() 统一接口测试"""

    @pytest.mark.asyncio
    async def test_gemini_backend_success(self):
        """Gemini 后端正常生图"""
        from app.image_gen import generate_image

        fake_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100

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

        with patch("app.image_gen.direct_client") as mock_client:
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

        with patch("app.image_gen.direct_client") as mock_client:
            mock_client.models.generate_images.return_value = mock_response
            with pytest.raises(RuntimeError, match="无法生成图片"):
                await _generate_with_gemini("inappropriate content", "1:1", 1)

    @pytest.mark.asyncio
    async def test_content_image_model_uses_relay_and_deduplicates(self, monkeypatch):
        """Gemini 原生图片模型走中转 client，并去除重复图片。"""
        from app.image_gen import _generate_with_gemini
        import app.image_gen as image_gen

        first = b"first-image"
        duplicate = b"first-image"
        second = b"second-image"
        parts = [
            SimpleNamespace(inline_data=SimpleNamespace(mime_type="image/png", data=first)),
            SimpleNamespace(inline_data=SimpleNamespace(mime_type="image/png", data=duplicate)),
            SimpleNamespace(inline_data=SimpleNamespace(mime_type="image/png", data=second)),
        ]
        response = SimpleNamespace(
            candidates=[SimpleNamespace(content=SimpleNamespace(parts=parts))]
        )
        relay = MagicMock()
        relay.models.generate_content.return_value = response
        direct = MagicMock()

        monkeypatch.setattr(image_gen, "GEMINI_IMAGE_MODEL", "gemini-nano-banana-2.1")
        monkeypatch.setattr(image_gen, "GEMINI_API_BASE", "http://relay.example")
        monkeypatch.setenv("GEMINI_API_BASE_KEY", "relay-sentinel")
        monkeypatch.setattr(image_gen, "genai_client", relay)
        monkeypatch.setattr(image_gen, "direct_client", direct)

        images = await _generate_with_gemini("a cat", "1:1", 2)

        assert images == [first, second]
        relay.models.generate_content.assert_called_once()
        direct.models.generate_content.assert_not_called()
        config = relay.models.generate_content.call_args.kwargs["config"]
        assert config.response_modalities == ["IMAGE", "TEXT"]
        assert config.image_config.aspect_ratio == "1:1"

    @pytest.mark.asyncio
    async def test_content_image_model_requires_explicit_relay_key(self, monkeypatch):
        """配置中转时缺少显式 key 必须 fail closed。"""
        from app.image_gen import _generate_with_gemini
        import app.image_gen as image_gen

        monkeypatch.setattr(image_gen, "GEMINI_IMAGE_MODEL", "gemini-nano-banana-2.1")
        monkeypatch.setattr(image_gen, "GEMINI_API_BASE", "http://relay.example")
        monkeypatch.delenv("GEMINI_API_BASE_KEY", raising=False)
        relay = MagicMock()
        monkeypatch.setattr(image_gen, "genai_client", relay)

        with pytest.raises(RuntimeError, match="中转 key 未配置"):
            await _generate_with_gemini("a cat", "1:1", 1)
        relay.models.generate_content.assert_not_called()

    @pytest.mark.asyncio
    async def test_content_image_model_raises_when_unique_count_is_insufficient(self, monkeypatch):
        """有界补调用后仍不足时显式失败，不能重复计数。"""
        from app.image_gen import _generate_with_gemini
        import app.image_gen as image_gen

        part = SimpleNamespace(
            inline_data=SimpleNamespace(mime_type="image/png", data=b"same-image")
        )
        response = SimpleNamespace(
            candidates=[SimpleNamespace(content=SimpleNamespace(parts=[part]))]
        )
        relay = MagicMock()
        relay.models.generate_content.return_value = response
        monkeypatch.setattr(image_gen, "GEMINI_IMAGE_MODEL", "gemini-nano-banana-2.1")
        monkeypatch.setattr(image_gen, "GEMINI_API_BASE", "")
        monkeypatch.setattr(image_gen, "genai_client", relay)

        with pytest.raises(RuntimeError, match="数量不足"):
            await _generate_with_gemini("a cat", "1:1", 2)
        assert relay.models.generate_content.call_count == 2

    @pytest.mark.asyncio
    async def test_content_image_model_propagates_cancellation_without_retry(self, monkeypatch):
        """调用取消后不启动下一次补图请求。"""
        from app.image_gen import _generate_with_gemini
        import app.image_gen as image_gen

        relay = MagicMock()
        relay.models.generate_content.side_effect = asyncio.CancelledError()
        monkeypatch.setattr(image_gen, "GEMINI_IMAGE_MODEL", "gemini-nano-banana-2.1")
        monkeypatch.setattr(image_gen, "GEMINI_API_BASE", "")
        monkeypatch.setattr(image_gen, "genai_client", relay)

        with pytest.raises(asyncio.CancelledError):
            await _generate_with_gemini("a cat", "1:1", 2)
        assert relay.models.generate_content.call_count == 1

    @pytest.mark.asyncio
    async def test_gemini_edit_uses_actual_png_mime_for_relay_model(self, monkeypatch):
        """Gemini 中转改图必须传递输入图片的真实 MIME。"""
        from app.image_gen import _edit_with_gemini
        import app.image_gen as image_gen

        output = b"edited-image"
        part = SimpleNamespace(
            inline_data=SimpleNamespace(mime_type="image/png", data=output)
        )
        response = SimpleNamespace(
            candidates=[SimpleNamespace(content=SimpleNamespace(parts=[part]))]
        )
        relay = MagicMock()
        relay.models.generate_content.return_value = response
        direct = MagicMock()
        monkeypatch.setattr(image_gen, "GEMINI_IMAGE_EDIT_MODEL", "gemini-nano-banana-2.1")
        monkeypatch.setattr(image_gen, "genai_client", relay)
        monkeypatch.setattr(image_gen, "direct_client", direct)

        images = await _edit_with_gemini(b"\x89PNG\r\n\x1a\ninput", "make it blue")

        assert images == [output]
        relay.models.generate_content.assert_called_once()
        direct.models.generate_content.assert_not_called()
        contents = relay.models.generate_content.call_args.kwargs["contents"]
        assert contents[0].inline_data.mime_type == "image/png"

    @pytest.mark.asyncio
    async def test_gemini_edit_requires_explicit_relay_key(self, monkeypatch):
        import app.image_gen as image_gen

        monkeypatch.setattr(image_gen, "GEMINI_IMAGE_EDIT_MODEL", "gemini-nano-banana-2.1")
        monkeypatch.setattr(image_gen, "GEMINI_API_BASE", "http://relay.example")
        monkeypatch.delenv("GEMINI_API_BASE_KEY", raising=False)
        relay = MagicMock()
        monkeypatch.setattr(image_gen, "genai_client", relay)
        with pytest.raises(RuntimeError, match="中转 key 未配置"):
            await image_gen._edit_with_gemini(b"\x89PNG\r\n\x1a\ninput", "make it green")
        relay.models.generate_content.assert_not_called()


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
    async def test_gpt_image_26_uses_high_complexity_and_png_output(self, monkeypatch):
        """GPT Image 2.6 使用最高图片复杂度和 PNG 输出。"""
        from app.image_gen import _generate_with_openai
        import app.image_gen as image_gen

        fake_png = b"\x89PNG\r\n\x1a\n" + b"image"
        mock_img = MagicMock(b64_json=base64.b64encode(fake_png).decode())
        mock_response = MagicMock(data=[mock_img])
        mock_client = MagicMock()
        mock_client.images.generate.return_value = mock_response
        monkeypatch.setattr(image_gen, "OPENAI_IMAGE_MODEL", "gpt-image-2.6-sunburst")
        monkeypatch.setattr(image_gen, "OPENAI_IMAGE_COMPLEXITY", "high")
        monkeypatch.setattr(image_gen, "_get_openai_client", lambda: mock_client)

        images = await _generate_with_openai("a cat", "1:1", 1)

        assert images == [fake_png]
        kwargs = mock_client.images.generate.call_args.kwargs
        assert kwargs["complexity"] == "high"
        assert kwargs["output_format"] == "png"
        assert "response_format" not in kwargs

    @pytest.mark.asyncio
    async def test_gpt_image_26_edit_preserves_input_mime(self, monkeypatch):
        """GPT Image 2.6 改图保留 PNG 输入 MIME，并发送最高复杂度。"""
        from app.image_gen import _edit_with_openai
        import app.image_gen as image_gen

        output = b"\x89PNG\r\n\x1a\noutput"
        mock_img = MagicMock(b64_json=base64.b64encode(output).decode())
        mock_client = MagicMock()
        mock_client.images.edit.return_value = MagicMock(data=[mock_img])
        monkeypatch.setattr(image_gen, "OPENAI_IMAGE_MODEL", "gpt-image-2.6-sunburst")
        monkeypatch.setattr(image_gen, "OPENAI_IMAGE_COMPLEXITY", "high")
        monkeypatch.setattr(image_gen, "_get_openai_client", lambda: mock_client)

        images = await _edit_with_openai(b"\x89PNG\r\n\x1a\ninput", "make it blue")

        assert images == [output]
        kwargs = mock_client.images.edit.call_args.kwargs
        assert kwargs["complexity"] == "high"
        assert kwargs["output_format"] == "png"
        assert "response_format" not in kwargs
        assert kwargs["image"][2] == "image/png"

    @pytest.mark.asyncio
    async def test_size_mapping(self):
        """aspect_ratio 正确映射为 size"""
        from app.image_gen import _map_openai_size

        assert _map_openai_size("1:1") == "1024x1024"
        assert _map_openai_size("3:4") == "1024x1536"
        assert _map_openai_size("4:3") == "1536x1024"
        assert _map_openai_size("9:16") == "1024x1792"
        assert _map_openai_size("16:9") == "1792x1024"
        assert _map_openai_size("unknown") == "1024x1024"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,count", [("generate", 1), ("generate", 2), ("edit", 1)])
async def test_gemini_thought_images_never_count_as_delivered_results(monkeypatch, operation, count):
    import app.image_gen as image_gen

    def response(index):
        return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[
            SimpleNamespace(thought=True, inline_data=SimpleNamespace(mime_type="image/png", data=b"draft-" + bytes([index]))),
            SimpleNamespace(thought=False, inline_data=SimpleNamespace(mime_type="image/png", data=b"final-" + bytes([index]))),
        ]))])

    relay = MagicMock()
    relay.models.generate_content.side_effect = [response(1), response(2)]
    monkeypatch.setattr(image_gen, "genai_client", relay)
    monkeypatch.setattr(image_gen, "GEMINI_API_BASE", "")
    monkeypatch.setattr(image_gen, "GEMINI_IMAGE_MODEL", "gemini-nano-banana-2.1")
    monkeypatch.setattr(image_gen, "GEMINI_IMAGE_EDIT_MODEL", "gemini-nano-banana-2.1")
    if operation == "generate":
        results = await image_gen.generate_image("a cat", backend="gemini", number_of_images=count)
    else:
        results = await image_gen.edit_image(b"\x89PNG\r\n\x1a\ninput", "make it blue", backend="gemini")

    assert results == [b"final-" + bytes([i]) for i in range(1, count + 1)]
    assert relay.models.generate_content.call_count == count


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["generate", "edit"])
async def test_gemini_only_thought_image_is_not_a_success(monkeypatch, operation):
    import app.image_gen as image_gen

    relay = MagicMock()
    relay.models.generate_content.return_value = SimpleNamespace(parts=[
        SimpleNamespace(thought=True, inline_data=SimpleNamespace(mime_type="image/png", data=b"draft-only")),
    ])
    monkeypatch.setattr(image_gen, "genai_client", relay)
    monkeypatch.setattr(image_gen, "GEMINI_API_BASE", "")
    monkeypatch.setattr(image_gen, "GEMINI_IMAGE_MODEL", "gemini-nano-banana-2.1")
    monkeypatch.setattr(image_gen, "GEMINI_IMAGE_EDIT_MODEL", "gemini-nano-banana-2.1")
    with pytest.raises(RuntimeError):
        if operation == "generate":
            await image_gen.generate_image("a cat", backend="gemini")
        else:
            await image_gen.edit_image(b"\x89PNG\r\n\x1a\ninput", "make it blue", backend="gemini")
    assert relay.models.generate_content.call_count == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
