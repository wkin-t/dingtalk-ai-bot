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
        assert _map_openai_size("unknown") == "1024x1024"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
