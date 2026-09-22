# -*- coding: utf-8 -*-
"""
openai_client 单测——覆盖以下回归点：
1. _split_messages_for_responses：system 提取 / 图片剥离 / 多轮 role 保留
2. call_openai_stream 路由分支：Gemini → Chat Completions，Claude/GPT → Responses
3. content_chars 兜底：sub2api Gemini 无 usage 字段时不误判"无返回"
"""
import asyncio

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.openai_client import (
    ANTIGRAVITY_GROUNDING_SOURCE_LINK_RE,
    _build_client,
    _split_messages_for_responses,
    call_openai_stream,
)


def test_search_fallback_provider_defaults_to_none(monkeypatch):
    """环境未配置时默认关闭，不受开发机 .env 或进程配置影响。"""
    import dotenv
    import runpy
    from pathlib import Path

    monkeypatch.delenv("SEARCH_FALLBACK_PROVIDER", raising=False)
    original_load_dotenv = dotenv.load_dotenv
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: False)
    try:
        isolated_config = runpy.run_path(
            str(Path(__file__).resolve().parents[1] / "app" / "config.py"),
            run_name="__test_config__",
        )
    finally:
        monkeypatch.setattr(dotenv, "load_dotenv", original_load_dotenv)

    assert isolated_config["SEARCH_FALLBACK_PROVIDER"] == "none"


# ------------- _split_messages_for_responses (pure) -------------

def test_build_client_omits_base_url_for_native_openai(monkeypatch):
    """OPENAI_API_BASE 为空时应使用 OpenAI SDK 默认官方 endpoint。"""
    monkeypatch.setattr("app.openai_client.OPENAI_API_BASE", "")
    monkeypatch.setattr("app.openai_client.OPENAI_API_KEY_CUSTOM", "sk-test")
    monkeypatch.setattr("app.openai_client.HTTPX_PROXY", "")
    with patch("app.openai_client.AsyncOpenAI") as mock_client:
        _build_client()
    assert "base_url" not in mock_client.call_args.kwargs
    assert mock_client.call_args.kwargs["api_key"] == "sk-test"


def test_build_client_uses_base_url_for_openai_compatible_gateway(monkeypatch):
    """配置 OPENAI_API_BASE 时继续走 OpenAI 兼容网关。"""
    monkeypatch.setattr("app.openai_client.OPENAI_API_BASE", "http://127.0.0.1:38090/v1")
    monkeypatch.setattr("app.openai_client.OPENAI_API_KEY_CUSTOM", "sk-test")
    monkeypatch.setattr("app.openai_client.HTTPX_PROXY", "")
    with patch("app.openai_client.AsyncOpenAI") as mock_client:
        _build_client()
    assert mock_client.call_args.kwargs["base_url"] == "http://127.0.0.1:38090/v1"

def test_split_extracts_system_string_to_instructions():
    """system 角色的 string content 应被合并到 instructions"""
    messages = [
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": "hi"},
    ]
    instructions, input_items = _split_messages_for_responses(messages, supports_vision=True)
    assert instructions == "你是助手"
    assert input_items == [{"role": "user", "content": "hi"}]


def test_split_extracts_system_list_blocks_to_instructions_when_store_supported():
    """supports_store=True（GPT，会用 previous_response_id 精简续接）时，Stage B 的三段
    system blocks 仍拼成 instructions——因为 instructions 每轮都无条件重发，精简路径
    不会丢内容；cache_control 在此路径确实丢失，但这是刻意选择（见 store=False 分支）"""
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
    instructions, input_items = _split_messages_for_responses(
        messages, supports_vision=True, supports_store=True
    )
    assert "稳定段" in instructions and "半稳段" in instructions and "变动段" in instructions
    assert len(input_items) == 1
    assert input_items[0]["role"] == "user"


def test_split_routes_system_list_blocks_to_system_input_when_store_not_supported():
    """supports_store=False（Claude，每轮全量重发 input，没有服务端续接）时，Stage B 的
    system blocks 应转换后作为一条 role="system" 消息插到 input 最前面，保留 cache_control
    ——这样每轮重发才有机会命中 sub2api 转译层的缓存断点"""
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
    instructions, input_items = _split_messages_for_responses(
        messages, supports_vision=True, supports_store=False
    )
    assert instructions == ""
    assert len(input_items) == 2
    system_msg = input_items[0]
    assert system_msg["role"] == "system"
    assert system_msg["content"] == [
        {"type": "input_text", "text": "稳定段", "cache_control": {"type": "ephemeral"}},
        {"type": "input_text", "text": "半稳段", "cache_control": {"type": "ephemeral"}},
        {"type": "input_text", "text": "变动段"},
    ]
    assert input_items[1] == {"role": "user", "content": "hi"}


def test_split_system_string_content_always_goes_to_instructions_regardless_of_store():
    """system 角色的 string content（比如搜索兜底注入的临时摘要）永远走 instructions，
    不受 supports_store 影响——一次性内容不需要、也不应该占用缓存前缀的位置"""
    messages = [
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": "hi"},
    ]
    instructions, input_items = _split_messages_for_responses(
        messages, supports_vision=True, supports_store=False
    )
    assert instructions == "你是助手"
    assert input_items == [{"role": "user", "content": "hi"}]


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


def test_split_converts_list_content_blocks_to_responses_vocab():
    """视觉模型 list content 必须按 Responses API 词汇转换：
       user 的 text → input_text，image_url → input_image（带图片报错的回归测试）"""
    content_list = [
        {"type": "text", "text": "看图"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,xxx"}},
    ]
    messages = [{"role": "user", "content": content_list}]
    _, input_items = _split_messages_for_responses(messages, supports_vision=True)
    converted = input_items[0]["content"]
    assert converted[0] == {"type": "input_text", "text": "看图"}
    assert converted[1] == {"type": "input_image", "image_url": "data:image/jpeg;base64,xxx"}


def test_split_converts_assistant_text_to_output_text():
    """assistant 的 list content 中 text 块应转为 output_text"""
    messages = [
        {"role": "assistant", "content": [{"type": "text", "text": "之前的回答"}]},
    ]
    _, input_items = _split_messages_for_responses(messages, supports_vision=True)
    assert input_items[0]["content"] == [{"type": "output_text", "text": "之前的回答"}]


def test_split_passes_through_already_responses_vocab():
    """已经是 Responses 词汇的 block 不应再次转换"""
    content_list = [
        {"type": "input_text", "text": "已转换"},
        {"type": "input_image", "image_url": "http://x"},
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


def _response_event(event_type, **fields):
    """构造脱敏的 Responses 流事件，不携带真实 prompt、响应或标识。"""
    event = MagicMock()
    event.type = event_type
    for name, value in fields.items():
        setattr(event, name, value)
    return event


GROUNDING_REDIRECT_PREFIX = (
    "https://vertexaisearch.cloud.google.com/grounding-api-redirect/"
)
GROUNDING_OPAQUE_PATH = "AbCdEf0123456789_-AbCdEf0123456789"


@pytest.mark.parametrize(
    ("suffix", "expected"),
    [
        ("A" * 32, True),
        ("A._~%/-09" * 4, True),
        ("A" * 31, False),
        ("A" * 31 + "中" + "B" * 32, False),
        ("A" * 16 + "+" + "B" * 32, False),
    ],
)
def test_grounding_source_link_regex_uses_ascii_opaque_path_boundary(
    suffix, expected,
):
    """opaque path 只接受生产证据中的 ASCII URL 字符，且至少连续 32 字符。"""
    text = GROUNDING_REDIRECT_PREFIX + suffix
    assert bool(ANTIGRAVITY_GROUNDING_SOURCE_LINK_RE.search(text)) is expected


def _model_config(name, supports_reasoning=False, supports_vision=True, supports_search=False):
    return {
        "model": name,
        "region": "global",
        "supports_reasoning": supports_reasoning,
        "supports_search": supports_search,
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


@pytest.mark.asyncio
@patch("app.responses_state.get_response_id", return_value=None)
@patch("app.responses_state.set_response_id")
@patch("app.openai_client.get_litellm_model_config")
@patch("app.openai_client.AsyncOpenAI")
async def test_responses_system_blocks_route_to_system_input_for_claude(
    mock_openai_cls, mock_get_config, mock_set_rid, mock_get_rid,
):
    """Claude（store=False）收到 Stage B 的 list-block system 时，应作为 role="system"
    的 input 消息发送并保留 cache_control，而不是拼进 instructions"""
    mock_get_config.return_value = _model_config("anthropic/claude-haiku-4.5")
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    mock_client.responses.create = AsyncMock(return_value=_make_async_stream([]))

    async for _ in call_openai_stream(
        [
            {
                "role": "system",
                "content": [
                    {"type": "text", "text": "稳定段", "cache_control": {"type": "ephemeral"}},
                    {"type": "text", "text": "半稳段", "cache_control": {"type": "ephemeral"}},
                ],
            },
            {"role": "user", "content": "Q"},
        ],
        target_model="fast",
        conversation_id="conv-claude-cache",
    ):
        pass

    call_kwargs = mock_client.responses.create.call_args.kwargs
    assert not call_kwargs.get("instructions")
    assert call_kwargs["input"][0]["role"] == "system"
    system_blocks = call_kwargs["input"][0]["content"]
    assert system_blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert system_blocks[1]["cache_control"] == {"type": "ephemeral"}
    assert call_kwargs["input"][-1] == {"role": "user", "content": "Q"}


@pytest.mark.asyncio
@patch("app.responses_state.get_response_id", return_value="resp_prev_xyz")
@patch("app.responses_state.set_response_id")
@patch("app.openai_client.get_litellm_model_config")
@patch("app.openai_client.AsyncOpenAI")
async def test_responses_prev_id_still_refreshes_system_blocks_for_gpt(
    mock_openai_cls, mock_get_config, mock_set_rid, mock_get_rid,
):
    """回归防护：GPT 精简续接路径（previous_response_id 存在，只发最后一条 user 消息）下，
    Stage B 的 list-block system 内容必须仍然通过 instructions 每轮刷新——不能因为挪去
    input[0] 而被精简路径（只扫 role=="user"）漏发，导致日期/Soul/群信息冻结在第一轮"""
    mock_get_config.return_value = _model_config("gpt-5.5")
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    mock_client.responses.create = AsyncMock(return_value=_make_async_stream([]))

    async for _ in call_openai_stream(
        [
            {
                "role": "system",
                "content": [
                    {"type": "text", "text": "稳定段"},
                    {"type": "text", "text": "变动段：今天是新的一天"},
                ],
            },
            {"role": "user", "content": "首都？"},
            {"role": "assistant", "content": "巴黎"},
            {"role": "user", "content": "人口？"},
        ],
        target_model="fast",
        conversation_id="conv-gpt-slim",
    ):
        pass

    kwargs = mock_client.responses.create.call_args.kwargs
    # 精简路径确实只发了最后一条 user 消息
    assert len(kwargs["input"]) == 1
    assert kwargs["input"][0]["content"] == "人口？"
    # 但 system 内容（含"变动段"）必须仍然出现在 instructions 里，每轮刷新
    assert "稳定段" in kwargs["instructions"]
    assert "变动段：今天是新的一天" in kwargs["instructions"]


@pytest.mark.asyncio
@patch("app.responses_state.get_response_id", return_value=None)
@patch("app.responses_state.set_response_id")
@patch("app.openai_client.get_litellm_model_config")
@patch("app.openai_client.AsyncOpenAI")
async def test_responses_enable_search_adds_web_search_tool(
    mock_openai_cls, mock_get_config, mock_set_rid, mock_get_rid,
):
    """OpenAI Responses 原生支持搜索时，enable_search 必须下发 web_search tool。"""
    mock_get_config.return_value = _model_config("gpt-5.5", supports_search=True)
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    mock_client.responses.create = AsyncMock(return_value=_make_async_stream([]))

    async for _ in call_openai_stream(
        [{"role": "user", "content": "查一下今天的新闻"}],
        target_model="fast",
        enable_search=True,
        conversation_id="conv-search-native",
    ):
        pass

    call_kwargs = mock_client.responses.create.call_args.kwargs
    assert call_kwargs["tools"] == [{"type": "web_search"}]


@pytest.mark.asyncio
@patch("app.responses_state.get_response_id", return_value=None)
@patch("app.responses_state.set_response_id")
@patch("app.openai_client.get_litellm_model_config")
@patch("app.openai_client.AsyncOpenAI")
@pytest.mark.parametrize(
    "model_name",
    [
        "anthropic/claude-opus-4-6-thinking",
        "claude-opus-4-6-thinking",
    ],
)
async def test_responses_claude_low_sends_low_reasoning_effort(
    mock_openai_cls, mock_get_config, mock_set_rid, mock_get_rid, model_name,
):
    """带或不带 anthropic 前缀的 Claude Responses 都应真实下发 low effort。"""
    mock_get_config.return_value = _model_config(
        model_name, supports_reasoning=True
    )
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    mock_client.responses.create = AsyncMock(return_value=_make_async_stream([]))

    async for _ in call_openai_stream(
        [{"role": "user", "content": "hi"}],
        target_model="fast",
        thinking_level="low",
    ):
        pass

    assert mock_client.responses.create.call_args.kwargs["reasoning"] == {
        "effort": "low"
    }


@pytest.mark.asyncio
@patch("app.responses_state.get_response_id", return_value=None)
@patch("app.responses_state.set_response_id")
@patch("app.openai_client.get_litellm_model_config")
@patch("app.openai_client.AsyncOpenAI")
async def test_responses_gpt_low_keeps_reasoning_omitted(
    mock_openai_cls, mock_get_config, mock_set_rid, mock_get_rid,
):
    """Claude 专属 low 修复不能改变 GPT Responses 的既有请求参数。"""
    mock_get_config.return_value = _model_config("gpt-5.6-sol", supports_reasoning=True)
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    mock_client.responses.create = AsyncMock(return_value=_make_async_stream([]))

    async for _ in call_openai_stream(
        [{"role": "user", "content": "hi"}],
        target_model="fast",
        thinking_level="low",
    ):
        pass

    assert "reasoning" not in mock_client.responses.create.call_args.kwargs


@pytest.mark.asyncio
@patch("app.openai_client.get_litellm_model_config")
@patch("app.openai_client.AsyncOpenAI")
async def test_chat_completions_gemini_low_keeps_reasoning_omitted(
    mock_openai_cls, mock_get_config,
):
    """Claude Responses 修复不能改变 Gemini Chat Completions 的 low 参数。"""
    mock_get_config.return_value = _model_config(
        "gemini-3.6-flash-tiered", supports_reasoning=True
    )
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    mock_client.chat.completions.create = AsyncMock(return_value=_make_async_stream([]))

    async for _ in call_openai_stream(
        [{"role": "user", "content": "hi"}],
        target_model="fast",
        thinking_level="low",
    ):
        pass

    assert "extra_body" not in mock_client.chat.completions.create.call_args.kwargs


@pytest.mark.asyncio
@patch("app.responses_state.get_response_id", return_value=None)
@patch("app.responses_state.set_response_id")
@patch("app.openai_client.get_litellm_model_config")
@patch("app.openai_client.AsyncOpenAI")
async def test_responses_minimal_omits_reasoning(
    mock_openai_cls, mock_get_config, mock_set_rid, mock_get_rid,
):
    """minimal 继续代表关闭 reasoning，不应下发 reasoning 参数。"""
    mock_get_config.return_value = _model_config(
        "anthropic/claude-opus-4-6-thinking", supports_reasoning=True
    )
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    mock_client.responses.create = AsyncMock(return_value=_make_async_stream([]))

    async for _ in call_openai_stream(
        [{"role": "user", "content": "hi"}],
        target_model="fast",
        thinking_level="minimal",
    ):
        pass

    assert "reasoning" not in mock_client.responses.create.call_args.kwargs


@pytest.mark.asyncio
@patch("app.responses_state.get_response_id", return_value=None)
@patch("app.responses_state.set_response_id")
@patch("app.openai_client.get_litellm_model_config")
@patch("app.openai_client.AsyncOpenAI")
async def test_responses_reasoning_summary_normalizes_to_thinking_chunks(
    mock_openai_cls, mock_get_config, mock_set_rid, mock_get_rid,
):
    """Claude reasoning summary 应复用既有 thinking chunk contract。"""
    mock_get_config.return_value = _model_config(
        "anthropic/claude-opus-4-6-thinking", supports_reasoning=True
    )
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    mock_client.responses.create = AsyncMock(return_value=_make_async_stream([
        _response_event("response.reasoning_summary_text.delta", delta="核对证据"),
        _response_event("response.reasoning_summary_text.delta", delta="并形成结论"),
        _response_event("response.output_text.delta", delta="最终回答"),
    ]))

    chunks = [
        chunk
        async for chunk in call_openai_stream(
            [{"role": "user", "content": "hi"}],
            target_model="fast",
            thinking_level="medium",
        )
    ]

    assert chunks[:5] == [
        {"thinking_start": True},
        {"thinking": "核对证据"},
        {"thinking": "并形成结论"},
        {"thinking_end": True},
        {"content": "最终回答"},
    ]


@pytest.mark.asyncio
@patch("app.responses_state.get_response_id", return_value=None)
@patch("app.responses_state.set_response_id")
@patch("app.openai_client.get_litellm_model_config")
@patch("app.openai_client.AsyncOpenAI")
async def test_responses_typed_reasoning_summary_event_is_normalized(
    mock_openai_cls, mock_get_config, mock_set_rid, mock_get_rid,
):
    """当前 OpenAI SDK 的 typed summary event 必须走真实字段契约而非 MagicMock。"""
    responses_types = pytest.importorskip("openai.types.responses")
    event_type = getattr(
        responses_types, "ResponseReasoningSummaryTextDeltaEvent", None
    )
    if event_type is None:
        pytest.skip("当前 OpenAI SDK 尚未提供 typed reasoning summary delta event")
    mock_get_config.return_value = _model_config(
        "claude-opus-4-6-thinking", supports_reasoning=True
    )
    typed_event = event_type(
        delta="脱敏思考摘要",
        item_id="reasoning_item_redacted",
        output_index=0,
        sequence_number=1,
        summary_index=0,
        type="response.reasoning_summary_text.delta",
    )
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    mock_client.responses.create = AsyncMock(return_value=_make_async_stream([
        typed_event,
        _response_event("response.output_text.delta", delta="回答"),
    ]))

    chunks = [
        chunk
        async for chunk in call_openai_stream(
            [{"role": "user", "content": "hi"}],
            target_model="fast",
            thinking_level="medium",
        )
    ]

    assert chunks[:4] == [
        {"thinking_start": True},
        {"thinking": "脱敏思考摘要"},
        {"thinking_end": True},
        {"content": "回答"},
    ]


@pytest.mark.asyncio
@patch("app.responses_state.get_response_id", return_value=None)
@patch("app.responses_state.set_response_id")
@patch("app.openai_client.get_litellm_model_config")
@patch("app.openai_client.AsyncOpenAI")
async def test_responses_empty_output_delta_does_not_close_thinking(
    mock_openai_cls, mock_get_config, mock_set_rid, mock_get_rid,
):
    """空正文 delta 不代表正文开始，不能截断并重启 thinking 状态。"""
    mock_get_config.return_value = _model_config(
        "claude-opus-4-6-thinking", supports_reasoning=True
    )
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    mock_client.responses.create = AsyncMock(return_value=_make_async_stream([
        _response_event("response.reasoning_summary_text.delta", delta="A"),
        _response_event("response.output_text.delta", delta=""),
        _response_event("response.reasoning_summary_text.delta", delta="B"),
        _response_event("response.output_text.delta", delta="answer"),
    ]))

    chunks = [
        chunk
        async for chunk in call_openai_stream(
            [{"role": "user", "content": "hi"}],
            target_model="fast",
            thinking_level="medium",
        )
    ]

    assert chunks[:5] == [
        {"thinking_start": True},
        {"thinking": "A"},
        {"thinking": "B"},
        {"thinking_end": True},
        {"content": "answer"},
    ]


@pytest.mark.asyncio
@patch("app.responses_state.get_response_id", return_value=None)
@patch("app.responses_state.set_response_id")
@patch("app.openai_client.get_litellm_model_config")
@patch("app.openai_client.AsyncOpenAI")
async def test_responses_reasoning_summary_closes_thinking_at_stream_end(
    mock_openai_cls, mock_get_config, mock_set_rid, mock_get_rid,
):
    """没有正文事件时，流结束仍必须关闭已开始的 thinking 状态。"""
    mock_get_config.return_value = _model_config(
        "anthropic/claude-opus-4-6-thinking", supports_reasoning=True
    )
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    mock_client.responses.create = AsyncMock(return_value=_make_async_stream([
        _response_event("response.reasoning_summary_text.delta", delta="核对证据"),
    ]))

    chunks = [
        chunk
        async for chunk in call_openai_stream(
            [{"role": "user", "content": "hi"}],
            target_model="fast",
            thinking_level="medium",
        )
    ]

    assert chunks[:3] == [
        {"thinking_start": True},
        {"thinking": "核对证据"},
        {"thinking_end": True},
    ]


@pytest.mark.asyncio
@patch("app.responses_state.get_response_id", return_value=None)
@patch("app.responses_state.set_response_id")
@patch("app.openai_client.get_litellm_model_config")
@patch("app.openai_client.AsyncOpenAI")
async def test_responses_late_reasoning_after_content_is_ignored(
    mock_openai_cls, mock_get_config, mock_set_rid, mock_get_rid,
):
    """正文开始后迟到的 reasoning 不能重新开启 thinking。"""
    mock_get_config.return_value = _model_config(
        "anthropic/claude-opus-4-6-thinking", supports_reasoning=True
    )
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    mock_client.responses.create = AsyncMock(return_value=_make_async_stream([
        _response_event("response.reasoning_summary_text.delta", delta="先前思考"),
        _response_event("response.output_text.delta", delta="正式回答"),
        _response_event("response.reasoning_summary_text.delta", delta="迟到思考"),
    ]))

    chunks = [
        chunk
        async for chunk in call_openai_stream(
            [{"role": "user", "content": "hi"}],
            target_model="fast",
            thinking_level="medium",
        )
    ]

    assert chunks[:4] == [
        {"thinking_start": True},
        {"thinking": "先前思考"},
        {"thinking_end": True},
        {"content": "正式回答"},
    ]
    assert not any(chunk.get("thinking") == "迟到思考" for chunk in chunks)
    assert sum(1 for chunk in chunks if chunk.get("thinking_start")) == 1


@pytest.mark.asyncio
@patch("app.responses_state.get_response_id", return_value=None)
@patch("app.responses_state.set_response_id")
@patch("app.openai_client.get_litellm_model_config")
@patch("app.openai_client.AsyncOpenAI")
async def test_responses_failed_closes_thinking_before_error(
    mock_openai_cls, mock_get_config, mock_set_rid, mock_get_rid,
):
    """provider terminal failure 必须先闭合已启动的 thinking。"""
    mock_get_config.return_value = _model_config(
        "anthropic/claude-opus-4-6-thinking", supports_reasoning=True
    )
    failed_response = MagicMock()
    failed_response.error = MagicMock(message="上游失败")
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    mock_client.responses.create = AsyncMock(return_value=_make_async_stream([
        _response_event("response.reasoning_summary_text.delta", delta="失败前摘要"),
        _response_event("response.failed", response=failed_response),
    ]))

    chunks = [
        chunk
        async for chunk in call_openai_stream(
            [{"role": "user", "content": "hi"}],
            target_model="fast",
            thinking_level="medium",
        )
    ]

    assert chunks[:4] == [
        {"thinking_start": True},
        {"thinking": "失败前摘要"},
        {"thinking_end": True},
        {"error": "OpenAI Responses Error: 上游失败"},
    ]


@pytest.mark.asyncio
@patch("app.responses_state.get_response_id", return_value=None)
@patch("app.responses_state.set_response_id")
@patch("app.openai_client.get_litellm_model_config")
@patch("app.openai_client.AsyncOpenAI")
async def test_responses_stream_exception_closes_thinking_before_outer_error(
    mock_openai_cls, mock_get_config, mock_set_rid, mock_get_rid,
):
    """async iterator 异常必须先闭合 thinking，再由外层保留 error contract。"""
    mock_get_config.return_value = _model_config(
        "anthropic/claude-opus-4-6-thinking", supports_reasoning=True
    )

    async def failing_stream():
        yield _response_event("response.reasoning_summary_text.delta", delta="异常前摘要")
        raise RuntimeError("stream interrupted")

    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    mock_client.responses.create = AsyncMock(return_value=failing_stream())

    chunks = [
        chunk
        async for chunk in call_openai_stream(
            [{"role": "user", "content": "hi"}],
            target_model="fast",
            thinking_level="medium",
        )
    ]

    assert chunks[:4] == [
        {"thinking_start": True},
        {"thinking": "异常前摘要"},
        {"thinking_end": True},
        {"error": "OpenAI API Error: stream interrupted"},
    ]


@pytest.mark.asyncio
@patch("app.responses_state.get_response_id", return_value=None)
@patch("app.responses_state.set_response_id")
@patch("app.openai_client.get_litellm_model_config")
@patch("app.openai_client.AsyncOpenAI")
async def test_responses_task_cancel_closes_thinking_and_propagates(
    mock_openai_cls, mock_get_config, mock_set_rid, mock_get_rid,
):
    """真实 Task.cancel() 应闭合 thinking、传播取消且不转成 error 或写 response ID。"""
    mock_get_config.return_value = _model_config(
        "claude-opus-4-6-thinking", supports_reasoning=True
    )
    waiting_for_next_event = asyncio.Event()
    blocked = asyncio.Event()

    async def cancellable_stream():
        yield _response_event(
            "response.reasoning_summary_text.delta", delta="取消前摘要"
        )
        waiting_for_next_event.set()
        await blocked.wait()

    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    mock_client.responses.create = AsyncMock(return_value=cancellable_stream())

    chunks = []

    async def consume():
        async for chunk in call_openai_stream(
            [{"role": "user", "content": "hi"}],
            target_model="fast",
            thinking_level="medium",
            conversation_id="cancel-conversation",
        ):
            chunks.append(chunk)

    task = asyncio.create_task(consume())
    await asyncio.wait_for(waiting_for_next_event.wait(), timeout=1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert chunks[:3] == [
        {"thinking_start": True},
        {"thinking": "取消前摘要"},
        {"thinking_end": True},
    ]
    assert sum(1 for chunk in chunks if chunk.get("thinking_end")) == 1
    assert not any("error" in chunk for chunk in chunks)
    mock_set_rid.assert_not_called()
    mock_get_rid.assert_not_called()


@pytest.mark.asyncio
@patch("app.responses_state.get_response_id", return_value=None)
@patch("app.responses_state.set_response_id")
@patch("app.openai_client.get_litellm_model_config")
@patch("app.openai_client.AsyncOpenAI")
async def test_responses_task_cancel_without_thinking_does_not_emit_end(
    mock_openai_cls, mock_get_config, mock_set_rid, mock_get_rid,
):
    """未启动 thinking 的真实取消不得伪造 thinking_end。"""
    mock_get_config.return_value = _model_config(
        "claude-opus-4-6-thinking", supports_reasoning=True
    )
    waiting_for_next_event = asyncio.Event()
    blocked = asyncio.Event()

    async def cancellable_stream():
        waiting_for_next_event.set()
        await blocked.wait()
        yield _response_event("response.completed")

    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    mock_client.responses.create = AsyncMock(return_value=cancellable_stream())

    chunks = []

    async def consume():
        async for chunk in call_openai_stream(
            [{"role": "user", "content": "hi"}],
            target_model="fast",
            thinking_level="medium",
            conversation_id="cancel-without-thinking",
        ):
            chunks.append(chunk)

    task = asyncio.create_task(consume())
    await asyncio.wait_for(waiting_for_next_event.wait(), timeout=1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert not any(chunk.get("thinking_end") for chunk in chunks)
    assert not any("error" in chunk for chunk in chunks)
    mock_set_rid.assert_not_called()
    mock_get_rid.assert_not_called()


@pytest.mark.asyncio
@patch("app.responses_state.get_response_id", return_value=None)
@patch("app.responses_state.set_response_id")
@patch("app.openai_client.get_litellm_model_config")
@patch("app.openai_client.AsyncOpenAI")
async def test_responses_empty_reasoning_delta_does_not_start_thinking(
    mock_openai_cls, mock_get_config, mock_set_rid, mock_get_rid,
):
    """空 reasoning 事件不能伪造 thinking start/end。"""
    mock_get_config.return_value = _model_config(
        "anthropic/claude-opus-4-6-thinking", supports_reasoning=True
    )
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    mock_client.responses.create = AsyncMock(return_value=_make_async_stream([
        _response_event("response.reasoning_summary_text.delta", delta=""),
        _response_event("response.output_text.delta", delta="普通回答"),
    ]))

    chunks = [
        chunk
        async for chunk in call_openai_stream(
            [{"role": "user", "content": "hi"}],
            target_model="fast",
            thinking_level="medium",
        )
    ]

    assert not any("thinking" in chunk or "thinking_start" in chunk for chunk in chunks)


@pytest.mark.asyncio
@patch("app.responses_state.get_response_id", return_value=None)
@patch("app.responses_state.set_response_id")
@patch("app.openai_client.get_litellm_model_config")
@patch("app.openai_client.AsyncOpenAI")
async def test_responses_grounding_prefix_across_deltas_emits_executed_once(
    mock_openai_cls, mock_get_config, mock_set_rid, mock_get_rid,
):
    """Antigravity Grounding URL 跨 delta 时也必须且只能上报一次真实搜索。"""
    mock_get_config.return_value = _model_config(
        "gpt-5.5", supports_search=True
    )
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    split_at = len(GROUNDING_REDIRECT_PREFIX) // 2
    mock_client.responses.create = AsyncMock(return_value=_make_async_stream([
        _response_event(
            "response.output_text.delta",
            delta="来源：" + GROUNDING_REDIRECT_PREFIX[:split_at],
        ),
        _response_event(
            "response.output_text.delta",
            delta=GROUNDING_REDIRECT_PREFIX[split_at:] + GROUNDING_OPAQUE_PATH,
        ),
        _response_event(
            "response.output_text.delta",
            delta="\n备用：" + GROUNDING_REDIRECT_PREFIX + GROUNDING_OPAQUE_PATH[::-1],
        ),
    ]))

    chunks = [
        chunk
        async for chunk in call_openai_stream(
            [{"role": "user", "content": "查询实时资料"}],
            target_model="fast",
            enable_search=True,
        )
    ]

    assert [chunk for chunk in chunks if chunk.get("search", {}).get("executed")] == [
        {"search": {"executed": True}}
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "signal_event",
    [
        _response_event(
            "response.output_item.added",
            item=MagicMock(type="web_search_call"),
        ),
        _response_event(
            "response.output_text.annotation.added",
            annotation=MagicMock(type="url_citation"),
        ),
    ],
)
@patch("app.responses_state.get_response_id", return_value=None)
@patch("app.responses_state.set_response_id")
@patch("app.openai_client.get_litellm_model_config")
@patch("app.openai_client.AsyncOpenAI")
async def test_responses_standard_search_signals_still_emit_executed_once(
    mock_openai_cls, mock_get_config, mock_set_rid, mock_get_rid, signal_event,
):
    """标准 Responses 搜索事件继续作为真实执行证据，且与 Grounding 信号共用门闩。"""
    mock_get_config.return_value = _model_config(
        "gpt-5.5", supports_search=True
    )
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    mock_client.responses.create = AsyncMock(return_value=_make_async_stream([
        signal_event,
        _response_event(
            "response.output_text.delta",
            delta=GROUNDING_REDIRECT_PREFIX + GROUNDING_OPAQUE_PATH,
        ),
    ]))

    chunks = [
        chunk
        async for chunk in call_openai_stream(
            [{"role": "user", "content": "查询实时资料"}],
            target_model="fast",
            enable_search=True,
        )
    ]

    assert [chunk for chunk in chunks if chunk.get("search", {}).get("executed")] == [
        {"search": {"executed": True}}
    ]


@pytest.mark.asyncio
@patch("app.responses_state.get_response_id", return_value=None)
@patch("app.responses_state.set_response_id")
@patch("app.openai_client.get_litellm_model_config")
@patch("app.openai_client.AsyncOpenAI")
async def test_responses_existing_reasoning_text_event_remains_supported(
    mock_openai_cls, mock_get_config, mock_set_rid, mock_get_rid,
):
    """新增 summary 兼容不能破坏原有 reasoning_text 事件。"""
    mock_get_config.return_value = _model_config(
        "gpt-5.5", supports_reasoning=True
    )
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    mock_client.responses.create = AsyncMock(return_value=_make_async_stream([
        _response_event("response.reasoning_text.delta", delta="原有思考"),
        _response_event("response.output_text.delta", delta="回答"),
    ]))

    chunks = [
        chunk
        async for chunk in call_openai_stream(
            [{"role": "user", "content": "hi"}],
            target_model="fast",
            thinking_level="medium",
        )
    ]

    assert chunks[:4] == [
        {"thinking_start": True},
        {"thinking": "原有思考"},
        {"thinking_end": True},
        {"content": "回答"},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "Sources: https://example.com/article",
        "用户正在讨论 web_search 与 google_search 的语法",
        GROUNDING_REDIRECT_PREFIX + "看起来像来源",
    ],
)
@patch("app.responses_state.get_response_id", return_value=None)
@patch("app.responses_state.set_response_id")
@patch("app.openai_client.get_litellm_model_config")
@patch("app.openai_client.AsyncOpenAI")
async def test_responses_without_native_search_does_not_infer_executed_from_text(
    mock_openai_cls, mock_get_config, mock_set_rid, mock_get_rid, text,
):
    """未挂载原生搜索时，即使正文含 URL 或搜索字样也不能点亮图标。"""
    mock_get_config.return_value = _model_config(
        "anthropic/claude-opus-4-6-thinking", supports_search=True
    )
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    mock_client.responses.create = AsyncMock(return_value=_make_async_stream([
        _response_event("response.output_text.delta", delta=text),
    ]))

    chunks = [
        chunk
        async for chunk in call_openai_stream(
            [{"role": "user", "content": "普通请求"}],
            target_model="fast",
            enable_search=False,
        )
    ]

    assert not any(chunk.get("search", {}).get("executed") for chunk in chunks)


@pytest.mark.asyncio
@patch("app.responses_state.get_response_id", return_value=None)
@patch("app.responses_state.set_response_id")
@patch("app.openai_client.get_litellm_model_config")
@patch("app.openai_client.AsyncOpenAI")
async def test_responses_native_search_without_execution_evidence_stays_off(
    mock_openai_cls, mock_get_config, mock_set_rid, mock_get_rid,
):
    """仅挂载工具以及普通 Sources/URL 文本都不是搜索执行证据。"""
    mock_get_config.return_value = _model_config(
        "anthropic/claude-opus-4-6-thinking", supports_search=True
    )
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    mock_client.responses.create = AsyncMock(return_value=_make_async_stream([
        _response_event(
            "response.output_text.delta",
            delta="Sources: https://example.com/article",
        ),
    ]))

    chunks = [
        chunk
        async for chunk in call_openai_stream(
            [{"role": "user", "content": "普通请求"}],
            target_model="fast",
            enable_search=True,
        )
    ]

    assert not any(chunk.get("search", {}).get("executed") for chunk in chunks)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "forged_text",
    [
        GROUNDING_REDIRECT_PREFIX,
        GROUNDING_REDIRECT_PREFIX + "short",
        GROUNDING_REDIRECT_PREFIX + ("a" * 20),
        GROUNDING_REDIRECT_PREFIX + ("a" * 31) + " 非法分隔",
    ],
)
@patch("app.responses_state.get_response_id", return_value=None)
@patch("app.responses_state.set_response_id")
@patch("app.openai_client.get_litellm_model_config")
@patch("app.openai_client.AsyncOpenAI")
async def test_responses_native_search_rejects_bare_or_short_grounding_shape(
    mock_openai_cls, mock_get_config, mock_set_rid, mock_get_rid, forged_text,
):
    """已挂工具时，复述裸前缀或短伪造路径仍不能点亮搜索图标。"""
    mock_get_config.return_value = _model_config(
        "claude-opus-4-6-thinking", supports_search=True
    )
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    mock_client.responses.create = AsyncMock(return_value=_make_async_stream([
        _response_event("response.output_text.delta", delta=forged_text),
    ]))

    chunks = [
        chunk
        async for chunk in call_openai_stream(
            [{"role": "user", "content": "复述链接格式"}],
            target_model="fast",
            enable_search=True,
        )
    ]

    assert not any(chunk.get("search", {}).get("executed") for chunk in chunks)


@pytest.mark.asyncio
@patch("app.openai_client.google_search", new_callable=AsyncMock)
@patch("app.openai_client.get_litellm_model_config")
@patch("app.openai_client.AsyncOpenAI")
async def test_chat_completions_search_fallback_injects_gemini_summary(
    mock_openai_cls, mock_get_config, mock_google_search,
):
    """Chat Completions 路径不支持原生搜索时，应把 Gemini 搜索摘要注入 system prompt。"""
    mock_get_config.return_value = _model_config("gemini-3.5-flash", supports_search=False)
    mock_google_search.return_value = "搜索摘要：今天有重要新闻。"
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    mock_client.chat.completions.create = AsyncMock(return_value=_make_async_stream([]))

    with patch("app.openai_client.SEARCH_FALLBACK_PROVIDER", "gemini"):
        async for _ in call_openai_stream(
            [{"role": "user", "content": "查一下今天的新闻"}],
            target_model="fast",
            enable_search=True,
        ):
            pass

    mock_google_search.assert_awaited_once()
    call_kwargs = mock_client.chat.completions.create.call_args.kwargs
    sent_messages = call_kwargs["messages"]
    assert sent_messages[0]["role"] == "system"
    assert "搜索摘要：今天有重要新闻。" in sent_messages[0]["content"]
    assert sent_messages[-1]["content"] == "查一下今天的新闻"


@pytest.mark.asyncio
@patch("app.openai_client.google_search", new_callable=AsyncMock)
@patch("app.openai_client.get_litellm_model_config")
@patch("app.openai_client.AsyncOpenAI")
async def test_search_fallback_disabled_does_not_call_or_inject_summary(
    mock_openai_cls, mock_get_config, mock_google_search,
):
    """SEARCH_FALLBACK_PROVIDER=none 时不调用旧搜索，也不注入 system 摘要。"""
    mock_get_config.return_value = _model_config("gemini-3.5-flash", supports_search=False)
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    mock_client.chat.completions.create = AsyncMock(return_value=_make_async_stream([]))

    with patch("app.openai_client.SEARCH_FALLBACK_PROVIDER", "none"):
        async for _ in call_openai_stream(
            [{"role": "user", "content": "查一下今天的新闻"}],
            target_model="fast",
            enable_search=True,
        ):
            pass

    mock_google_search.assert_not_awaited()
    sent_messages = mock_client.chat.completions.create.call_args.kwargs["messages"]
    assert not any(
        message.get("role") == "system" and "联网搜索结果" in message.get("content", "")
        for message in sent_messages
    )


@pytest.mark.asyncio
@patch("app.responses_state.get_response_id", return_value=None)
@patch("app.responses_state.set_response_id")
@patch("app.openai_client.google_search", new_callable=AsyncMock)
@patch("app.openai_client.get_litellm_model_config")
@patch("app.openai_client.AsyncOpenAI")
async def test_responses_search_fallback_injects_gemini_summary_when_native_disabled(
    mock_openai_cls, mock_get_config, mock_google_search, mock_set_rid, mock_get_rid,
):
    """Responses 路径未声明原生搜索能力时，应注入 Gemini 搜索摘要且不下发 tools。"""
    mock_get_config.return_value = _model_config("gpt-5.5", supports_search=False)
    mock_google_search.return_value = "搜索摘要：OpenAI 发布了新功能。"
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    mock_client.responses.create = AsyncMock(return_value=_make_async_stream([]))

    with patch("app.openai_client.SEARCH_FALLBACK_PROVIDER", "gemini"):
        async for _ in call_openai_stream(
            [{"role": "user", "content": "最新 OpenAI web search API 是什么"}],
            target_model="fast",
            enable_search=True,
            conversation_id="conv-search-fallback",
        ):
            pass

    mock_google_search.assert_awaited_once()
    call_kwargs = mock_client.responses.create.call_args.kwargs
    assert "tools" not in call_kwargs
    assert "搜索摘要：OpenAI 发布了新功能。" in call_kwargs["instructions"]


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



# ── TD2: Responses API previous_response_id 多轮 thinking ──

def _make_response_created_event(response_id="resp_test_001"):
    """构造 response.created 事件，response 上带 id"""
    response = MagicMock()
    response.id = response_id
    response.model = "anthropic/claude-haiku-4.5"
    response.usage = MagicMock(input_tokens=10, output_tokens=5)
    response.error = None
    evt = MagicMock()
    evt.type = "response.created"
    evt.response = response
    return evt


def _make_response_completed_event(response_id="resp_test_001"):
    response = MagicMock()
    response.id = response_id
    response.model = "anthropic/claude-haiku-4.5"
    response.usage = MagicMock(input_tokens=10, output_tokens=5)
    response.error = None
    evt = MagicMock()
    evt.type = "response.completed"
    evt.response = response
    return evt


@pytest.mark.asyncio
@patch("app.responses_state.get_response_id", return_value=None)
@patch("app.responses_state.set_response_id")
@patch("app.openai_client.get_litellm_model_config")
@patch("app.openai_client.AsyncOpenAI")
async def test_responses_cached_tokens_parsed_from_real_field_shape(
    mock_openai_cls, mock_get_config, mock_set_rid, mock_get_rid,
):
    """回归防护：字段名(usage.input_tokens_details.cached_tokens)手滑打错时必须被测试
    发现——之前只用没设置嵌套字段的 MagicMock，isinstance(int) 兜底会把任何手滑都
    悄悄归零成 cached=0，测试却照样全绿。这里显式给一个真实非零值，断言真的解析到了。"""
    mock_get_config.return_value = _model_config("anthropic/claude-haiku-4.5")
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client

    response = MagicMock()
    response.id = "resp_cache_test"
    response.model = "anthropic/claude-haiku-4.5"
    response.usage = MagicMock(
        input_tokens=1000,
        output_tokens=20,
        input_tokens_details=MagicMock(cached_tokens=800),
    )
    response.error = None
    evt = MagicMock()
    evt.type = "response.completed"
    evt.response = response

    mock_client.responses.create = AsyncMock(return_value=_make_async_stream([evt]))

    yielded = []
    async for chunk in call_openai_stream(
        [{"role": "user", "content": "hi"}],
        target_model="fast",
        conversation_id="conv-cache-parse",
    ):
        yielded.append(chunk)

    usages = [c["usage"] for c in yielded if "usage" in c]
    assert len(usages) == 1
    assert usages[0]["cached_tokens"] == 800
    assert usages[0]["input_tokens"] == 1000


@pytest.mark.asyncio
@patch("app.responses_state.get_response_id", return_value=None)
@patch("app.responses_state.set_response_id")
@patch("app.openai_client.get_litellm_model_config")
@patch("app.openai_client.AsyncOpenAI")
async def test_responses_store_false_for_anthropic_model(
    mock_openai_cls, mock_get_config, mock_set_rid, mock_get_rid,
):
    """Anthropic 模型不支持 store，必须传 store=False，否则 sub2api 转发会 502"""
    mock_get_config.return_value = _model_config("anthropic/claude-haiku-4.5")
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    mock_client.responses.create = AsyncMock(return_value=_make_async_stream([]))

    async for _ in call_openai_stream(
        [{"role": "user", "content": "hi"}],
        target_model="fast",
        conversation_id="conv-A",
    ):
        pass

    kwargs = mock_client.responses.create.call_args.kwargs
    assert kwargs.get("store") is False


@pytest.mark.asyncio
@patch("app.responses_state.get_response_id", return_value="resp_should_not_be_used")
@patch("app.responses_state.set_response_id")
@patch("app.openai_client.get_litellm_model_config")
@patch("app.openai_client.AsyncOpenAI")
async def test_responses_store_false_for_unprefixed_claude_model(
    mock_openai_cls, mock_get_config, mock_set_rid, mock_get_rid,
):
    """Antigravity 无前缀 Claude 也不支持 store 或 previous_response_id。"""
    mock_get_config.return_value = _model_config("claude-opus-4-6-thinking")
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    mock_client.responses.create = AsyncMock(return_value=_make_async_stream([]))

    async for _ in call_openai_stream(
        [{"role": "user", "content": "hi"}],
        target_model="fast",
        conversation_id="conv-antigravity",
    ):
        pass

    kwargs = mock_client.responses.create.call_args.kwargs
    assert kwargs["store"] is False
    assert "previous_response_id" not in kwargs
    mock_get_rid.assert_not_called()
    mock_set_rid.assert_not_called()


@pytest.mark.asyncio
@patch("app.responses_state.get_response_id", return_value=None)
@patch("app.responses_state.set_response_id")
@patch("app.openai_client.get_litellm_model_config")
@patch("app.openai_client.AsyncOpenAI")
async def test_responses_store_true_for_openai_model(
    mock_openai_cls, mock_get_config, mock_set_rid, mock_get_rid,
):
    """GPT 模型支持 store，必须传 store=True 才能使用 previous_response_id"""
    mock_get_config.return_value = _model_config("gpt-5.5")
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    mock_client.responses.create = AsyncMock(return_value=_make_async_stream([]))

    async for _ in call_openai_stream(
        [{"role": "user", "content": "hi"}],
        target_model="fast",
        conversation_id="conv-A",
    ):
        pass

    kwargs = mock_client.responses.create.call_args.kwargs
    assert kwargs.get("store") is True


@pytest.mark.asyncio
@patch("app.responses_state.get_response_id", return_value="resp_prev_xyz")
@patch("app.responses_state.set_response_id")
@patch("app.openai_client.get_litellm_model_config")
@patch("app.openai_client.AsyncOpenAI")
async def test_responses_with_prior_id_sends_only_last_user_and_previous_response_id(
    mock_openai_cls, mock_get_config, mock_set_rid, mock_get_rid,
):
    """有 previous_response_id 时只发最后一条 user 消息，历史在服务端保留（仅 GPT 支持）"""
    mock_get_config.return_value = _model_config("gpt-5.5")
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    mock_client.responses.create = AsyncMock(return_value=_make_async_stream([]))

    async for _ in call_openai_stream(
        [
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "首都？"},
            {"role": "assistant", "content": "巴黎"},
            {"role": "user", "content": "人口？"},
        ],
        target_model="fast",
        conversation_id="conv-multi-turn",
    ):
        pass

    kwargs = mock_client.responses.create.call_args.kwargs
    assert kwargs.get("previous_response_id") == "resp_prev_xyz"
    # 只发最后一条 user 消息
    assert len(kwargs["input"]) == 1
    assert kwargs["input"][0]["role"] == "user"
    assert kwargs["input"][0]["content"] == "人口？"
    # instructions 仍刷新（含 system prompt）
    assert kwargs["instructions"] == "你是助手"


@pytest.mark.asyncio
@patch("app.responses_state.get_response_id", return_value=None)
@patch("app.responses_state.set_response_id")
@patch("app.openai_client.get_litellm_model_config")
@patch("app.openai_client.AsyncOpenAI")
async def test_responses_stores_response_id_after_stream(
    mock_openai_cls, mock_get_config, mock_set_rid, mock_get_rid,
):
    """GPT 的 response.created/completed 事件仍应存 response.id。"""
    mock_get_config.return_value = _model_config("gpt-5.5")
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    text_delta = MagicMock()
    text_delta.type = "response.output_text.delta"
    text_delta.delta = "Paris"
    mock_client.responses.create = AsyncMock(return_value=_make_async_stream([
        _make_response_created_event("resp_new_999"),
        text_delta,
        _make_response_completed_event("resp_new_999"),
    ]))

    async for _ in call_openai_stream(
        [{"role": "user", "content": "首都？"}],
        target_model="fast",
        conversation_id="conv-store-test",
    ):
        pass

    mock_set_rid.assert_called_with("conv-store-test", "resp_new_999")


@pytest.mark.asyncio
@pytest.mark.parametrize("model_name", [
    "anthropic/claude-haiku-4.5",
    "claude-opus-4-6-thinking",
])
@patch("app.responses_state.get_response_id", return_value="resp_should_not_be_used")
@patch("app.responses_state.set_response_id")
@patch("app.openai_client.get_litellm_model_config")
@patch("app.openai_client.AsyncOpenAI")
async def test_responses_claude_does_not_store_response_id_from_success_events(
    mock_openai_cls, mock_get_config, mock_set_rid, mock_get_rid, model_name,
):
    """带/不带 provider 前缀的 Claude 成功流都不读写 response state。"""
    mock_get_config.return_value = _model_config(model_name, supports_reasoning=True)
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    mock_client.responses.create = AsyncMock(return_value=_make_async_stream([
        _make_response_created_event("resp_claude_success"),
        _response_event("response.output_text.delta", delta="回答"),
        _make_response_completed_event("resp_claude_success"),
    ]))

    chunks = [
        chunk
        async for chunk in call_openai_stream(
            [{"role": "user", "content": "hi"}],
            target_model="fast",
            thinking_level="medium",
            conversation_id="conv-claude-no-store",
        )
    ]

    assert any(chunk.get("content") == "回答" for chunk in chunks)
    kwargs = mock_client.responses.create.call_args.kwargs
    assert kwargs["store"] is False
    assert "previous_response_id" not in kwargs
    mock_get_rid.assert_not_called()
    mock_set_rid.assert_not_called()


@pytest.mark.asyncio
@patch("app.responses_state.clear_response_id")
@patch("app.responses_state.get_response_id", return_value="resp_invalid_stale")
@patch("app.responses_state.set_response_id")
@patch("app.openai_client.get_litellm_model_config")
@patch("app.openai_client.AsyncOpenAI")
async def test_responses_retries_full_history_when_prev_id_invalid(
    mock_openai_cls, mock_get_config, mock_set_rid, mock_get_rid, mock_clear_rid,
):
    """previous_response_id 失效时清状态并用全量历史重试（仅 GPT 支持 store）"""
    mock_get_config.return_value = _model_config("gpt-5.5")
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client

    # 第一次失败：模拟 "previous_response_id not found" 错误
    # 第二次成功
    text_delta = MagicMock()
    text_delta.type = "response.output_text.delta"
    text_delta.delta = "retry succeeded"
    call_count = {"n": 0}

    async def fake_create(**kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise Exception("Previous response with id 'resp_invalid_stale' not found")
        return _make_async_stream([
            _make_response_created_event("resp_retry_ok"),
            text_delta,
            _make_response_completed_event("resp_retry_ok"),
        ])

    mock_client.responses.create = fake_create

    chunks = []
    async for c in call_openai_stream(
        [
            {"role": "user", "content": "Q1"},
            {"role": "assistant", "content": "A1"},
            {"role": "user", "content": "Q2"},
        ],
        target_model="fast",
        conversation_id="conv-stale",
    ):
        chunks.append(c)

    # 应该清除了过期的 id
    mock_clear_rid.assert_called_with("conv-stale")
    # 重试后内容仍流出
    assert any(c.get("content") == "retry succeeded" for c in chunks)
    # 重试后存了新的 id
    mock_set_rid.assert_called_with("conv-stale", "resp_retry_ok")

# ------------- Claude function bridge 回归测试 -------------

def _bridge_created(model, input_tokens=2):
    return _response_event(
        "response.created",
        response={"model": model, "usage": {"input_tokens": input_tokens, "output_tokens": 0}},
    )


def _bridge_completed(model, output=None, input_tokens=2, output_tokens=3):
    return _response_event(
        "response.completed",
        response={
            "model": model,
            "output": output or [],
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        },
    )


def _bridge_call_events(
    model="anthropic/claude-sonnet-4-6",
    query="最新新闻",
    content_before_call=None,
):
    item = {
        "type": "function_call",
        "id": "item_search_1",
        "call_id": "call_search_1",
        "name": "search_current_web",
    }
    events = [_bridge_created(model)]
    if content_before_call:
        events.append(_response_event("response.output_text.delta", delta=content_before_call))
    events.extend([
        _response_event("response.output_item.added", item=item),
        _response_event(
            "response.function_call_arguments.delta",
            item_id="item_search_1",
            call_id="call_search_1",
            delta='{"query":"' + query + '"}',
        ),
        # v0.1.168 的 done 事件可能没有 arguments，不能依赖该字段。
        _response_event(
            "response.function_call_arguments.done",
            item_id="item_search_1",
            call_id="call_search_1",
        ),
    ])
    completed_item = {**item, "arguments": '{"query":"' + query + '"}'}
    events.append(_bridge_completed(model, output=[completed_item]))
    return events


@pytest.mark.asyncio
async def test_claude_bridge_searches_once_and_continues_without_tools(monkeypatch):
    import app.openai_client as oc
    from app.antigravity_search import SearchEvidence

    model = "anthropic/claude-sonnet-4-6"
    monkeypatch.setattr(oc, "get_litellm_model_config", lambda _: _model_config(model, supports_reasoning=True))
    monkeypatch.setattr(oc, "bridge_ready", lambda: True)
    monkeypatch.setattr(oc, "ANTIGRAVITY_CLAUDE_BRIDGE_THINKING_LEVELS", frozenset({"low"}))
    google_search_sentinel = AsyncMock(side_effect=AssertionError("旧 google_search 不应被调用"))
    monkeypatch.setattr(oc, "google_search", google_search_sentinel)
    search_mock = AsyncMock(
        return_value=SearchEvidence(
            True,
            summary="新闻事实",
            sources=("https://example.com/news",),
            source_titles=("来源",),
            reason="grounding",
        )
    )
    monkeypatch.setattr(oc, "search_current_web", search_mock)

    first = _make_async_stream(_bridge_call_events(content_before_call="Claude 前缀"))
    second = _make_async_stream([
        _bridge_created(model, input_tokens=4),
        _response_event("response.output_text.delta", delta="Claude 最终回答"),
        _bridge_completed(model, input_tokens=4, output_tokens=5),
    ])
    client = MagicMock()
    client.responses.create = AsyncMock(side_effect=[first, second])
    monkeypatch.setattr(oc, "_build_client", lambda: client)

    chunks = [
        chunk
        async for chunk in oc.call_openai_stream(
            [{"role": "user", "content": "查新闻"}],
            target_model="fast",
            thinking_level="low",
            enable_search=True,
        )
    ]

    assert client.responses.create.await_count == 2
    first_kwargs = client.responses.create.await_args_list[0].kwargs
    second_kwargs = client.responses.create.await_args_list[1].kwargs
    assert first_kwargs["tools"][0]["name"] == "search_current_web"
    assert "web_search" not in str(first_kwargs["tools"])
    assert "google_search" not in str(first_kwargs["tools"])
    assert "tools" not in second_kwargs
    assert any(item.get("type") == "function_call" for item in second_kwargs["input"])
    assert any(item.get("type") == "function_call_output" for item in second_kwargs["input"])
    assert search_mock.await_count == 1
    google_search_sentinel.assert_not_awaited()
    assert any(chunk.get("content") == "Claude 前缀" for chunk in chunks)
    assert any(chunk.get("content") == "Claude 最终回答" for chunk in chunks)
    assert sum(1 for chunk in chunks if chunk.get("search", {}).get("executed")) == 1
    usages = [chunk["usage"] for chunk in chunks if "usage" in chunk]
    assert usages == [{
        "model": model,
        "input_tokens": 6,
        "output_tokens": 8,
        "cached_tokens": 0,
        "latency_ms": usages[0]["latency_ms"] if usages else None,
    }]


@pytest.mark.asyncio
async def test_claude_bridge_no_tool_is_one_request_and_keeps_usage(monkeypatch):
    import app.openai_client as oc

    model = "claude-sonnet-4-6"
    monkeypatch.setattr(oc, "get_litellm_model_config", lambda _: _model_config(model, supports_reasoning=True))
    monkeypatch.setattr(oc, "bridge_ready", lambda: True)
    monkeypatch.setattr(oc, "ANTIGRAVITY_CLAUDE_BRIDGE_THINKING_LEVELS", frozenset({"low"}))
    client = MagicMock()
    client.responses.create = AsyncMock(return_value=_make_async_stream([
        _bridge_created(model),
        _response_event("response.output_text.delta", delta="普通回答"),
        _bridge_completed(model, input_tokens=2, output_tokens=3),
    ]))
    monkeypatch.setattr(oc, "_build_client", lambda: client)

    chunks = [
        chunk
        async for chunk in oc.call_openai_stream(
            [{"role": "user", "content": "你好"}],
            target_model="fast",
            thinking_level="low",
            enable_search=True,
        )
    ]

    client.responses.create.assert_awaited_once()
    assert any(chunk.get("content") == "普通回答" for chunk in chunks)
    assert not any(chunk.get("search", {}).get("executed") for chunk in chunks)
    assert len([chunk for chunk in chunks if "usage" in chunk]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_model", ["gemini-2.5-flash", "openai/gpt-5.5"])
async def test_claude_bridge_identity_gate_suppresses_non_claude_output(monkeypatch, bad_model):
    import app.openai_client as oc

    expected = "anthropic/claude-sonnet-4-6"
    monkeypatch.setattr(oc, "get_litellm_model_config", lambda _: _model_config(expected, supports_reasoning=True))
    monkeypatch.setattr(oc, "bridge_ready", lambda: True)
    monkeypatch.setattr(oc, "ANTIGRAVITY_CLAUDE_BRIDGE_THINKING_LEVELS", frozenset({"low"}))
    client = MagicMock()
    client.responses.create = AsyncMock(return_value=_make_async_stream([
        _bridge_created(bad_model),
        _response_event("response.output_text.delta", delta="不应可见"),
        _bridge_completed(bad_model, input_tokens=2, output_tokens=3),
    ]))
    monkeypatch.setattr(oc, "_build_client", lambda: client)

    chunks = [
        chunk
        async for chunk in oc.call_openai_stream(
            [{"role": "user", "content": "查新闻"}],
            target_model="fast",
            thinking_level="low",
            enable_search=True,
        )
    ]

    assert not any("content" in chunk for chunk in chunks)
    assert not any("thinking" in chunk for chunk in chunks)
    assert not any("usage" in chunk for chunk in chunks)
    assert any(chunk.get("error") == oc.BRIDGE_MODEL_SWAP_ERROR for chunk in chunks)
    client.responses.create.assert_awaited_once()


@pytest.mark.asyncio
async def test_claude_bridge_missing_identity_fails_closed(monkeypatch):
    import app.openai_client as oc

    model = "anthropic/claude-sonnet-4-6"
    monkeypatch.setattr(oc, "get_litellm_model_config", lambda _: _model_config(model, supports_reasoning=True))
    monkeypatch.setattr(oc, "bridge_ready", lambda: True)
    monkeypatch.setattr(oc, "ANTIGRAVITY_CLAUDE_BRIDGE_THINKING_LEVELS", frozenset({"low"}))
    client = MagicMock()
    client.responses.create = AsyncMock(return_value=_make_async_stream([
        _response_event("response.output_text.delta", delta="没有身份不能发布"),
        _response_event("response.completed", response={"output": []}),
    ]))
    monkeypatch.setattr(oc, "_build_client", lambda: client)

    chunks = [
        chunk
        async for chunk in oc.call_openai_stream(
            [{"role": "user", "content": "查新闻"}],
            target_model="fast",
            thinking_level="low",
            enable_search=True,
        )
    ]

    assert not any("content" in chunk or "usage" in chunk for chunk in chunks)
    assert any(chunk.get("error") == oc.BRIDGE_SAFE_ERROR for chunk in chunks)


@pytest.mark.asyncio
async def test_claude_bridge_continuation_tool_call_is_terminal(monkeypatch):
    import app.openai_client as oc
    from app.antigravity_search import SearchEvidence

    model = "anthropic/claude-sonnet-4-6"
    monkeypatch.setattr(oc, "get_litellm_model_config", lambda _: _model_config(model, supports_reasoning=True))
    monkeypatch.setattr(oc, "bridge_ready", lambda: True)
    monkeypatch.setattr(oc, "ANTIGRAVITY_CLAUDE_BRIDGE_THINKING_LEVELS", frozenset({"low"}))
    monkeypatch.setattr(oc, "search_current_web", AsyncMock(return_value=SearchEvidence(
        True, summary="事实", sources=("https://example.com",), source_titles=("来源",)
    )))
    client = MagicMock()
    client.responses.create = AsyncMock(side_effect=[
        _make_async_stream(_bridge_call_events()),
        _make_async_stream([
            _bridge_created(model),
            _response_event(
                "response.output_item.added",
                item={
                    "type": "function_call",
                    "id": "item_second",
                    "call_id": "call_second",
                    "name": "search_current_web",
                    "arguments": '{"query":"再次搜索"}',
                },
            ),
            _bridge_completed(model),
        ]),
    ])
    monkeypatch.setattr(oc, "_build_client", lambda: client)

    chunks = [
        chunk
        async for chunk in oc.call_openai_stream(
            [{"role": "user", "content": "查新闻"}],
            target_model="fast",
            thinking_level="low",
            enable_search=True,
        )
    ]

    assert any(chunk.get("error") == oc.BRIDGE_SAFE_ERROR for chunk in chunks)
    assert not any("usage" in chunk for chunk in chunks)


@pytest.mark.asyncio
async def test_claude_bridge_search_cancellation_does_not_continue(monkeypatch):
    import app.openai_client as oc

    model = "anthropic/claude-sonnet-4-6"
    monkeypatch.setattr(oc, "get_litellm_model_config", lambda _: _model_config(model, supports_reasoning=True))
    monkeypatch.setattr(oc, "bridge_ready", lambda: True)
    monkeypatch.setattr(oc, "ANTIGRAVITY_CLAUDE_BRIDGE_THINKING_LEVELS", frozenset({"low"}))
    monkeypatch.setattr(oc, "search_current_web", AsyncMock(side_effect=asyncio.CancelledError()))
    client = MagicMock()
    client.responses.create = AsyncMock(return_value=_make_async_stream(_bridge_call_events()))
    monkeypatch.setattr(oc, "_build_client", lambda: client)

    with pytest.raises(asyncio.CancelledError):
        async for _ in oc.call_openai_stream(
            [{"role": "user", "content": "查新闻"}],
            target_model="fast",
            thinking_level="low",
            enable_search=True,
        ):
            pass
    assert client.responses.create.await_count == 1

def test_bridge_safe_id_removes_control_characters():
    import app.openai_client as oc

    assert oc._bridge_safe_id("call\n\t\x00-1") == "call-1"


@pytest.mark.asyncio
async def test_bridge_unpaired_function_event_fails_closed(monkeypatch):
    import app.openai_client as oc

    model = "anthropic/claude-sonnet-4-6"
    monkeypatch.setattr(oc, "get_litellm_model_config", lambda _: _model_config(model, supports_reasoning=True))
    monkeypatch.setattr(oc, "bridge_ready", lambda: True)
    monkeypatch.setattr(oc, "ANTIGRAVITY_CLAUDE_BRIDGE_THINKING_LEVELS", frozenset({"low"}))
    client = MagicMock()
    client.responses.create = AsyncMock(return_value=_make_async_stream([
        _bridge_created(model),
        _response_event(
            "response.function_call_arguments.delta",
            call_id="call_without_item",
            delta='{"query":"新闻"}',
        ),
        _response_event("response.output_text.delta", delta="不应以成功终态发布"),
        _bridge_completed(model),
    ]))
    monkeypatch.setattr(oc, "_build_client", lambda: client)

    chunks = [
        chunk
        async for chunk in oc.call_openai_stream(
            [{"role": "user", "content": "查新闻"}],
            target_model="fast",
            thinking_level="low",
            enable_search=True,
        )
    ]

    assert not any("usage" in chunk for chunk in chunks)
    assert any(chunk.get("error") == oc.BRIDGE_SAFE_ERROR for chunk in chunks)
@pytest.mark.asyncio
async def test_claude_bridge_model_conflict_closes_thinking_and_hides_usage(monkeypatch):
    import app.openai_client as oc

    expected = "anthropic/claude-sonnet-4-6"
    monkeypatch.setattr(
        oc,
        "get_litellm_model_config",
        lambda _: _model_config(expected, supports_reasoning=True),
    )
    monkeypatch.setattr(oc, "bridge_ready", lambda: True)
    monkeypatch.setattr(
        oc,
        "ANTIGRAVITY_CLAUDE_BRIDGE_THINKING_LEVELS", frozenset({"low"})
    )
    client = MagicMock()
    client.responses.create = AsyncMock(return_value=_make_async_stream([
        _bridge_created(expected),
        _response_event(
            "response.reasoning_summary_text.delta",
            delta="不应在冲突后继续发布",
        ),
        _response_event("response.output_text.delta", delta="前缀"),
        _response_event(
            "response.output_text.delta",
            delta="",
            response={"model": "gemini-2.5-flash"},
        ),
        _bridge_completed("gemini-2.5-flash", input_tokens=2, output_tokens=3),
    ]))
    monkeypatch.setattr(oc, "_build_client", lambda: client)

    chunks = [
        chunk
        async for chunk in oc.call_openai_stream(
            [{"role": "user", "content": "查新闻"}],
            target_model="fast",
            thinking_level="low",
            enable_search=True,
        )
    ]

    assert any(chunk.get("error") == oc.BRIDGE_MODEL_SWAP_ERROR for chunk in chunks)
    assert not any("usage" in chunk for chunk in chunks)
    assert sum(1 for chunk in chunks if chunk.get("thinking_end")) <= 1
@pytest.mark.asyncio
async def test_claude_bridge_duplicate_call_id_is_not_executed(monkeypatch):
    import app.openai_client as oc
    from app.antigravity_search import SearchEvidence

    model = "anthropic/claude-sonnet-4-6"
    monkeypatch.setattr(
        oc,
        "get_litellm_model_config",
        lambda _: _model_config(model, supports_reasoning=True),
    )
    monkeypatch.setattr(oc, "bridge_ready", lambda: True)
    monkeypatch.setattr(
        oc,
        "ANTIGRAVITY_CLAUDE_BRIDGE_THINKING_LEVELS", frozenset({"low"})
    )
    search_mock = AsyncMock(
        return_value=SearchEvidence(
            True,
            summary="事实",
            sources=("https://example.com",),
            source_titles=("来源",),
        )
    )
    monkeypatch.setattr(oc, "search_current_web", search_mock)

    duplicate_one = {
        "type": "function_call",
        "id": "item_one",
        "call_id": "same_call",
        "name": "search_current_web",
        "arguments": '{"query":"一"}',
    }
    duplicate_two = {
        "type": "function_call",
        "id": "item_two",
        "call_id": "same_call",
        "name": "search_current_web",
        "arguments": '{"query":"二"}',
    }
    client = MagicMock()
    client.responses.create = AsyncMock(side_effect=[
        _make_async_stream([
            _bridge_created(model),
            _bridge_completed(model, output=[duplicate_one, duplicate_two]),
        ]),
        _make_async_stream([
            _bridge_created(model),
            _response_event("response.output_text.delta", delta="安全回答"),
            _bridge_completed(model, output=[]),
        ]),
    ])
    monkeypatch.setattr(oc, "_build_client", lambda: client)

    chunks = [
        chunk
        async for chunk in oc.call_openai_stream(
            [{"role": "user", "content": "查新闻"}],
            target_model="fast",
            thinking_level="low",
            enable_search=True,
        )
    ]

    search_mock.assert_not_awaited()
    assert client.responses.create.await_count == 1
    assert any(chunk.get("error") == oc.BRIDGE_SAFE_ERROR for chunk in chunks)
    assert not any(chunk.get("content") == "安全回答" for chunk in chunks)


def _bridge_disabled_client(model):
    client = MagicMock()
    client.responses.create = AsyncMock(return_value=_make_async_stream([
        _bridge_created(model),
        _response_event("response.output_text.delta", delta="我现在没法联网搜索"),
        _bridge_completed(model),
    ]))
    return client


@pytest.mark.asyncio
async def test_claude_search_disabled_injects_explanation_notice_when_requested(monkeypatch):
    """CLAUDE_SEARCH_BRIDGE_ENABLED 关闭（生产默认）+ 路由/用户真的要求了搜索
    （search_requested=True）时，Claude 不再拿到任何搜索工具，而是应该收到一条
    system 提示，让它自己向用户说明联网搜索为什么用不了。"""
    import app.openai_client as oc

    model = "anthropic/claude-sonnet-4-6"
    monkeypatch.setattr(oc, "get_litellm_model_config", lambda _: _model_config(model, supports_reasoning=True))
    monkeypatch.setattr(oc, "bridge_ready", lambda: False)
    client = _bridge_disabled_client(model)
    monkeypatch.setattr(oc, "_build_client", lambda: client)

    stage_b_system = {
        "role": "system",
        "content": [{"type": "text", "text": "Stage B 稳定段", "cache_control": {"type": "ephemeral"}}],
    }
    chunks = [
        chunk
        async for chunk in oc.call_openai_stream(
            [stage_b_system, {"role": "user", "content": "帮我查一下今天的新闻"}],
            target_model="fast",
            thinking_level="low",
            enable_search=True,
            search_requested=True,
        )
    ]

    # 没有任何工具挂上去——不会再触发 antigravity 的模型置换
    kwargs = client.responses.create.call_args.kwargs
    assert "tools" not in kwargs
    # 提示走 input[0] 的 role=system 消息（Claude/_supports_store=False 的既有路径，
    # Soul/persona 已验证能送达），不是未经验证的 instructions 字符串字段
    system_item = kwargs["input"][0]
    assert system_item["role"] == "system"
    blocks = [blk for blk in system_item["content"] if isinstance(blk, dict)]
    # 提示必须排在 Stage B 稳定段之后，不能顶到最前面破坏 cache 前缀稳定性
    assert blocks[0]["text"] == "Stage B 稳定段"
    assert "换成" in blocks[-1].get("text", "")
    notice_text = "".join(blk.get("text", "") for blk in blocks)
    assert "换成" in notice_text
    assert any(chunk.get("content") == "我现在没法联网搜索" for chunk in chunks)
    assert any(
        chunk.get("search") == {
            "requested": True,
            "native_enabled": False,
            "bridge_enabled": False,
            "fallback_injected": False,
            "reason": "bridge_disabled",
        }
        for chunk in chunks
    )


@pytest.mark.asyncio
async def test_claude_search_autonomous_mount_stays_silent_when_not_requested(monkeypatch):
    """全自主模式会把 enable_search 强制为 True（fast/pro 无条件挂搜索工具，模型
    自决），但路由/用户并没有真的要求搜索（search_requested=False）时，不该往每条
    无关消息里都注入"我不能联网"的说明——否则每条闲聊都会被 Claude 提一嘴搜索。"""
    import app.openai_client as oc

    model = "anthropic/claude-sonnet-4-6"
    monkeypatch.setattr(oc, "get_litellm_model_config", lambda _: _model_config(model, supports_reasoning=True))
    monkeypatch.setattr(oc, "bridge_ready", lambda: False)
    client = _bridge_disabled_client(model)
    monkeypatch.setattr(oc, "_build_client", lambda: client)

    chunks = [
        chunk
        async for chunk in oc.call_openai_stream(
            [{"role": "user", "content": "你好"}],
            target_model="fast",
            thinking_level="low",
            enable_search=True,
            search_requested=False,
        )
    ]

    kwargs = client.responses.create.call_args.kwargs
    assert "tools" not in kwargs
    # 没有 search_requested，原始消息应该原样透传，不插入任何 system 提示
    assert kwargs["input"] == [{"role": "user", "content": "你好"}]
    assert any(
        chunk.get("search") == {
            "requested": False,
            "native_enabled": False,
            "bridge_enabled": False,
            "fallback_injected": False,
            "reason": "bridge_disabled",
        }
        for chunk in chunks
    )
