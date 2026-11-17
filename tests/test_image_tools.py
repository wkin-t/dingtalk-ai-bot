"""模型自主图片工具的参数边界、执行预算与取消契约。"""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_tools_generate_then_edit_only_uses_approved_bytes(monkeypatch):
    from app.image_tools import ImageToolSession
    import app.image_gen as gen

    generated = b"generated-final"
    generate = AsyncMock(return_value=[generated])
    edit = AsyncMock(return_value=[b"edited-final"])
    monkeypatch.setattr(gen, "generate_image", generate)
    monkeypatch.setattr(gen, "edit_image", edit)
    session = ImageToolSession("gemini", [])
    first = await session.execute("generate_image", {"prompt": "red apple", "aspect_ratio": "1:1", "number_of_images": 1})
    assert first.images == [generated]
    assert first.response["ok"] is True
    second = await session.execute("edit_image", {"prompt": "green apple", "image_index": 1})
    assert second.images == [b"edited-final"]
    edit.assert_awaited_once_with(generated, "green apple", backend="gemini", aspect_ratio="1:1")
    assert session.calls == 2
    assert session.remaining_images == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("name,args", [
    ("unknown", {"prompt": "apple"}),
    ("generate_image", {"prompt": "apple", "model": "other-model"}),
    ("generate_image", {"prompt": "apple", "url": "https://example.com/image.png"}),
    ("generate_image", {"prompt": "apple", "number_of_images": True}),
    ("generate_image", {"prompt": "apple", "number_of_images": 5}),
    ("generate_image", {"prompt": "apple", "aspect_ratio": "invalid"}),
    ("generate_image", {"prompt": "x" * 8193}),
    ("generate_image", '{"prompt":"a","prompt":"b"}'),
    ("edit_image", {"prompt": "green apple", "image_index": 1}),
])
async def test_image_tool_invalid_arguments_never_call_provider(monkeypatch, name, args):
    from app.image_tools import ImageToolSession
    import app.image_gen as gen

    generate, edit = AsyncMock(), AsyncMock()
    monkeypatch.setattr(gen, "generate_image", generate)
    monkeypatch.setattr(gen, "edit_image", edit)
    result = await ImageToolSession("openai", []).execute(name, args)
    assert result.images == []
    assert result.response["ok"] is False
    generate.assert_not_called()
    edit.assert_not_called()


@pytest.mark.asyncio
async def test_image_tool_call_and_unique_image_budget_are_bounded(monkeypatch):
    from app.image_tools import ImageToolSession
    import app.image_gen as gen

    generate = AsyncMock(side_effect=[[b"a", b"a", b"b"], [b"c", b"d"], [b"e"]])
    monkeypatch.setattr(gen, "generate_image", generate)
    session = ImageToolSession("openai", [])
    first = await session.execute("generate_image", {"prompt": "apples", "number_of_images": 2})
    assert first.images == [b"a", b"b"]
    second = await session.execute("generate_image", {"prompt": "pears", "number_of_images": 2})
    assert second.images == [b"c", b"d"]
    third = await session.execute("generate_image", {"prompt": "more"})
    assert third.response["ok"] is False
    assert generate.await_count == 2


@pytest.mark.asyncio
async def test_image_tool_failure_is_sanitized_and_consumes_attempt(monkeypatch):
    from app.image_tools import ImageToolSession
    import app.image_gen as gen

    generate = AsyncMock(side_effect=RuntimeError("upstream sk-secret-value"))
    monkeypatch.setattr(gen, "generate_image", generate)
    session = ImageToolSession("openai", [])
    result = await session.execute("generate_image", {"prompt": "apple"})
    assert result.response["ok"] is False
    assert "sk-secret" not in json.dumps(result.response)
    assert session.calls == 1


@pytest.mark.asyncio
async def test_image_tool_cancellation_propagates(monkeypatch):
    from app.image_tools import ImageToolSession
    import app.image_gen as gen

    monkeypatch.setattr(gen, "generate_image", AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await ImageToolSession("gemini", []).execute("generate_image", {"prompt": "apple"})


def test_image_tool_declarations_cannot_select_credentials_or_models():
    from app.image_tools import image_tool_declarations

    definitions = image_tool_declarations()
    assert {x["name"] for x in definitions} == {"generate_image", "edit_image"}
    for definition in definitions:
        assert definition["parameters"]["additionalProperties"] is False
        assert not {"model", "backend", "url", "path", "api_key", "base_url"}.intersection(definition["parameters"]["properties"])
