"""捕获官方 SDK 最终 HTTP JSON，验证思考字段没有在序列化中丢失。"""
import json

import httpx
import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("model,requested,expected", [("gemini-4-hydrogen-low", "xhigh", "xhigh"), ("gemini-4-argon-high", "xhigh", "high")])
async def test_gemini_sdk_wire_thinking_level_is_independent_of_alias(monkeypatch, model, requested, expected):
    from google import genai
    from google.genai import types
    from app import gemini_client as gc

    bodies = []

    async def handle(request):
        bodies.append(json.loads(request.content))
        chunk = {"modelVersion": model, "candidates": [{"content": {"role": "model", "parts": [{"text": "OK"}]}, "finishReason": "STOP"}], "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1, "totalTokenCount": 2}}
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content="data: " + json.dumps(chunk) + "\n\n")

    transport = httpx.AsyncClient(transport=httpx.MockTransport(handle), trust_env=False)
    client = genai.Client(api_key="test", http_options=types.HttpOptions(base_url="http://127.0.0.1:12345", httpx_async_client=transport))
    monkeypatch.setattr(gc, "client", client)
    monkeypatch.setattr(gc, "fallback_client", None)
    monkeypatch.setattr(gc, "ENABLE_THINKING", True)
    monkeypatch.setattr(gc, "_stale_circuit_checked", True)
    try:
        chunks = [chunk async for chunk in gc.call_gemini_stream([{"role": "user", "content": "hi"}], target_model=model, thinking_level=requested)]
    finally:
        await client.aio.aclose()
        await transport.aclose()
    assert not any(chunk.get("error") for chunk in chunks), chunks
    thinking = bodies[0]["generationConfig"]["thinkingConfig"]
    # 锁定 SDK 在嵌套配置中保留 protobuf snake_case，不能只检查 Python 对象。
    assert thinking.get("thinkingLevel", thinking.get("thinking_level")).lower() == expected
    assert next(chunk["usage"] for chunk in chunks if "usage" in chunk)["thinking_level"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("model,level", [("gpt-6-luna", "none"), ("gpt-6.1-sol", "max"), ("claude-sonnet-5-5-low", "max")])
async def test_responses_sdk_wire_preserves_explicit_none_and_max(monkeypatch, model, level):
    import openai
    from app import openai_client as oc

    bodies = []

    async def handle(request):
        bodies.append(json.loads(request.content))
        response = {"id": "resp_wire", "object": "response", "created_at": 1, "status": "completed", "model": model, "output": []}
        events = [{"type": "response.created", "response": response}, {"type": "response.output_text.delta", "delta": "OK", "item_id": "msg", "output_index": 0, "content_index": 0}, {"type": "response.completed", "response": response}]
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content="".join("data: " + json.dumps(e) + "\n\n" for e in events))

    transport = httpx.AsyncClient(transport=httpx.MockTransport(handle), trust_env=False)
    client = openai.AsyncOpenAI(api_key="test", base_url="http://127.0.0.1:12345/v1", http_client=transport)
    monkeypatch.setattr(oc, "_build_client", lambda: client)
    monkeypatch.setattr(oc, "get_litellm_model_config", lambda _: {"model": model, "supports_reasoning": True, "supports_vision": True, "supports_search": False})
    try:
        chunks = [chunk async for chunk in oc.call_openai_stream([{"role": "user", "content": "hi"}], "fast", thinking_level=level)]
    finally:
        await client.close()
    assert not any(chunk.get("error") for chunk in chunks), chunks
    assert bodies[0]["model"] == model
    assert bodies[0]["reasoning"] == {"effort": level}
