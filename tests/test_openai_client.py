# -*- coding: utf-8 -*-
"""
openai_client 单测——覆盖以下回归点：
1. _split_messages_for_responses：system 提取 / 图片剥离 / 多轮 role 保留
2. call_openai_stream 路由分支：Gemini → Chat Completions，Claude/GPT → Responses
3. content_chars 兜底：sub2api Gemini 无 usage 字段时不误判"无返回"
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.openai_client import _split_messages_for_responses, call_openai_stream


# ------------- _split_messages_for_responses (pure) -------------

def test_split_extracts_system_string_to_instructions():
    """system 角色的 string content 应被合并到 instructions"""
    messages = [
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": "hi"},
    ]
    instructions, input_items = _split_messages_for_responses(messages, supports_vision=True)
    assert instructions == "你是助手"
    assert input_items == [{"role": "user", "content": "hi"}]


def test_split_extracts_system_list_blocks_to_instructions():
    """Stage B 的三段 system blocks 应被拼成 instructions（cache_control 在此路径丢失是已知 tradeoff）"""
    messages = [
        {
            "role": "system",
            "content": [
                {"type": "text", "text": "稳定段", "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": "半稳段", "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": "变动段"},
            ],
        },
        {"role": "user", "content": "hi"},
    ]
    instructions, input_items = _split_messages_for_responses(messages, supports_vision=True)
    assert "稳定段" in instructions and "半稳段" in instructions and "变动段" in instructions
    assert len(input_items) == 1
    assert input_items[0]["role"] == "user"


def test_split_keeps_multi_turn_user_assistant_in_input():
    """多轮 user/assistant 历史应原样保留——这是 Responses API 取代 chat completions 的核心动机"""
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "Q1"},
        {"role": "assistant", "content": "A1"},
        {"role": "user", "content": "Q2"},
    ]
    instructions, input_items = _split_messages_for_responses(messages, supports_vision=True)
    assert instructions == "sys"
    roles = [m["role"] for m in input_items]
    assert roles == ["user", "assistant", "user"]
    assert input_items[1]["content"] == "A1"


def test_split_strips_images_when_not_vision():
    """非视觉模型应剥掉图片块、保留文字"""
    messages = [
        {"role": "user", "content": [
            {"type": "text", "text": "看图说话"},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,xxx"}},
        ]},
    ]
    _, input_items = _split_messages_for_responses(messages, supports_vision=False)
    assert input_items[0]["content"] == "看图说话"


def test_split_strips_images_yields_placeholder_when_only_images():
    """非视觉模型且消息只有图片时，应返回占位符避免空 content"""
    messages = [
        {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,xxx"}},
        ]},
    ]
    _, input_items = _split_messages_for_responses(messages, supports_vision=False)
    assert input_items[0]["content"] == "[图片已移除]"


def test_split_preserves_list_content_when_vision():
    """视觉模型应原样保留 list content（含图片块）"""
    content_list = [
        {"type": "text", "text": "看图"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,xxx"}},
    ]
    messages = [{"role": "user", "content": content_list}]
    _, input_items = _split_messages_for_responses(messages, supports_vision=True)
    assert input_items[0]["content"] == content_list


def test_split_empty_when_only_system():
    """只有 system 时 input 应为空（让上游决定是否拒绝）"""
    messages = [{"role": "system", "content": "sys"}]
    instructions, input_items = _split_messages_for_responses(messages, supports_vision=True)
    assert instructions == "sys"
    assert input_items == []


# ------------- 路由分支测试 -------------

def _make_async_stream(events):
    """构造一个 async generator 模拟 SDK 返回的 stream 对象。"""
    async def _gen():
        for e in events:
            yield e
    return _gen()


def _model_config(name, supports_reasoning=False, supports_vision=True):
    return {
        "model": name,
        "region": "global",
        "supports_reasoning": supports_reasoning,
        "supports_search": False,
        "supports_vision": supports_vision,
        "reasoning_param": "openai_effort",
    }


@pytest.mark.asyncio
@patch("app.openai_client.get_litellm_model_config")
@patch("app.openai_client.AsyncOpenAI")
async def test_routes_gemini_to_chat_completions(mock_openai_cls, mock_get_config):
    """gemini-* 模型必须走 chat.completions.create（sub2api Gemini 不支持 Responses）"""
    mock_get_config.return_value = _model_config("gemini-3.5-flash")
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    mock_client.chat.completions.create = AsyncMock(return_value=_make_async_stream([]))
    mock_client.responses.create = AsyncMock(return_value=_make_async_stream([]))

    async for _ in call_openai_stream(
        [{"role": "user", "content": "hi"}],
        target_model="fast",
    ):
        pass

    mock_client.chat.completions.create.assert_called_once()
    mock_client.responses.create.assert_not_called()


@pytest.mark.asyncio
@patch("app.openai_client.get_litellm_model_config")
@patch("app.openai_client.AsyncOpenAI")
async def test_routes_claude_to_responses(mock_openai_cls, mock_get_config):
    """anthropic/claude-* 模型必须走 responses.create（避开 sub2api chat 多轮 bug）"""
    mock_get_config.return_value = _model_config("anthropic/claude-haiku-4.5")
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    mock_client.chat.completions.create = AsyncMock(return_value=_make_async_stream([]))
    mock_client.responses.create = AsyncMock(return_value=_make_async_stream([]))

    async for _ in call_openai_stream(
        [{"role": "user", "content": "hi"}],
        target_model="fast",
    ):
        pass

    mock_client.responses.create.assert_called_once()
    mock_client.chat.completions.create.assert_not_called()


@pytest.mark.asyncio
@patch("app.openai_client.get_litellm_model_config")
@patch("app.openai_client.AsyncOpenAI")
async def test_routes_gpt_to_responses(mock_openai_cls, mock_get_config):
    """gpt-5.5 等 GPT 模型必须走 responses.create（与 Claude 路径统一）"""
    mock_get_config.return_value = _model_config("gpt-5.5", supports_reasoning=True)
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    mock_client.chat.completions.create = AsyncMock(return_value=_make_async_stream([]))
    mock_client.responses.create = AsyncMock(return_value=_make_async_stream([]))

    async for _ in call_openai_stream(
        [{"role": "user", "content": "hi"}],
        target_model="fast",
    ):
        pass

    mock_client.responses.create.assert_called_once()
    mock_client.chat.completions.create.assert_not_called()


@pytest.mark.asyncio
@patch("app.openai_client.get_litellm_model_config")
@patch("app.openai_client.AsyncOpenAI")
async def test_responses_payload_uses_instructions_and_input(mock_openai_cls, mock_get_config):
    """Responses API 路径应把 system 拆到 instructions，其他消息留在 input 数组"""
    mock_get_config.return_value = _model_config("anthropic/claude-haiku-4.5")
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    mock_client.responses.create = AsyncMock(return_value=_make_async_stream([]))

    async for _ in call_openai_stream(
        [
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "Q"},
            {"role": "assistant", "content": "A"},
            {"role": "user", "content": "Q2"},
        ],
        target_model="fast",
    ):
        pass

    call_kwargs = mock_client.responses.create.call_args.kwargs
    assert call_kwargs["instructions"] == "你是助手"
    assert call_kwargs["input"][0]["role"] == "user"
    assert call_kwargs["input"][-1]["content"] == "Q2"
    # 关键：不应该出现 messages 参数（那是 chat completions 用的）
    assert "messages" not in call_kwargs


# ------------- content_chars 兜底测试 -------------

class _ChunkDelta:
    """模拟 OpenAI ChatCompletionChunk.choices[0].delta"""
    def __init__(self, content=None):
        self.content = content
        self.reasoning_content = None
        self.thinking = None
        self.model_extra = {}


class _Chunk:
    def __init__(self, content=None, model="test-model"):
        delta = _ChunkDelta(content=content)
        choice = MagicMock()
        choice.delta = delta
        self.choices = [choice]
        self.model = model
        self.usage = None  # 关键：sub2api Gemini 模拟——不下发 usage


@pytest.mark.asyncio
@patch("app.openai_client.get_litellm_model_config")
@patch("app.openai_client.AsyncOpenAI")
async def test_chat_completions_no_usage_no_false_error(mock_openai_cls, mock_get_config):
    """sub2api Gemini stream 不带 usage 时，只要 content 流出来了就不应误报"无返回"——这是 commit 77e4e53 修的 bug"""
    mock_get_config.return_value = _model_config("gemini-3.5-flash")
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    # 模拟 sub2api Gemini stream：内容流出，但所有 chunk 都没有 usage
    events = [
        _Chunk(content="Hello"),
        _Chunk(content=", world"),
        _Chunk(content="!"),
    ]
    mock_client.chat.completions.create = AsyncMock(return_value=_make_async_stream(events))

    yielded = []
    async for chunk in call_openai_stream(
        [{"role": "user", "content": "hi"}],
        target_model="fast",
    ):
        yielded.append(chunk)

    # 应该有 3 个 content chunk
    contents = [c["content"] for c in yielded if "content" in c]
    assert contents == ["Hello", ", world", "!"]
    # 关键回归：不应该出现 error chunk（旧代码会因为 output_tokens==0 误报）
    errors = [c["error"] for c in yielded if "error" in c]
    assert errors == [], f"流出 content 后仍报错（false-positive 回归）: {errors}"
    # 最终应该有 usage chunk
    usages = [c["usage"] for c in yielded if "usage" in c]
    assert len(usages) == 1


@pytest.mark.asyncio
@patch("app.openai_client.get_litellm_model_config")
@patch("app.openai_client.AsyncOpenAI")
async def test_chat_completions_empty_stream_yields_error(mock_openai_cls, mock_get_config):
    """空 stream（无 content 流出 + 无 usage）应该报"无返回"，不能让 false-negative 也漏过去"""
    mock_get_config.return_value = _model_config("gemini-3.5-flash")
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    # 完全空的 stream
    mock_client.chat.completions.create = AsyncMock(return_value=_make_async_stream([]))

    yielded = []
    async for chunk in call_openai_stream(
        [{"role": "user", "content": "hi"}],
        target_model="fast",
    ):
        yielded.append(chunk)

    errors = [c for c in yielded if "error" in c]
    assert len(errors) == 1
    assert "未返回" in errors[0]["error"]
