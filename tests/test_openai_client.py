# -*- coding: utf-8 -*-
"""
openai_client 单测——覆盖以下回归点：
1. _split_messages_for_responses：system 提取 / 图片剥离 / 多轮 role 保留
2. call_openai_stream 路由分支：Gemini → Chat Completions，Claude/GPT → Responses
3. content_chars 兜底：sub2api Gemini 无 usage 字段时不误判"无返回"
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.openai_client import _build_client, _split_messages_for_responses, call_openai_stream


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
    """response.created/completed 事件携带的 response.id 应被存到 responses_state"""
    mock_get_config.return_value = _model_config("anthropic/claude-haiku-4.5")
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
