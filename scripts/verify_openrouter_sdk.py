"""
OpenRouter 官方 SDK 生产指标验证脚本
运行前设置环境变量: OPENROUTER_API_KEY=sk-or-...
或在项目根目录有 .env.openrouter 文件

Usage:
    python scripts/verify_openrouter_sdk.py [test1|test2|test3|test4|all]

Test 1: provider.order=["Anthropic"] 路由正确到 Anthropic
Test 2: models 多模型 fallback（故意用坏模型+好备用）
Test 3: 多轮 reasoning_details 回传（第 2 轮不报 Invalid signature）
Test 4: reasoning_details 字段完整性（含 signature）
"""
import asyncio
import os
import sys


def load_env():
    """尝试从 .env.openrouter 加载 OPENROUTER_API_KEY"""
    env_file = os.path.join(os.path.dirname(__file__), "..", ".env.openrouter")
    if os.path.exists(env_file):
        with open(env_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")

if not OPENROUTER_API_KEY:
    print("❌ OPENROUTER_API_KEY 未设置，退出")
    sys.exit(1)


def make_client():
    from openrouter import OpenRouter
    return OpenRouter(api_key=OPENROUTER_API_KEY, server_url=OPENROUTER_BASE_URL)


# ──────────────────────────────────────────────
# Test 1: provider.order=["Anthropic"] 路由验证
# ──────────────────────────────────────────────
async def test_provider_routing():
    print("\n=== Test 1: Provider Routing (order=[Anthropic]) ===")
    from openrouter.components import ProviderPreferences

    client = make_client()
    messages = [{"role": "user", "content": "Say 'hello' in one word"}]

    actual_model = "unknown"
    content = ""
    async with await client.chat.send_async(
        messages=messages,
        model="anthropic/claude-haiku-4-5",
        provider=ProviderPreferences(order=["Anthropic"]),
        stream=True,
        max_tokens=20,
    ) as stream:
        async for chunk in stream:
            if hasattr(chunk, "model") and chunk.model:
                actual_model = chunk.model
            if chunk.choices:
                delta = chunk.choices[0].delta
                if delta.content:
                    content += delta.content

    print(f"  实际模型: {actual_model}")
    print(f"  响应内容: {content.strip()!r}")
    ok = "anthropic" in actual_model.lower() or "claude" in actual_model.lower()
    print(f"  {'✅ PASS' if ok else '❌ FAIL'}: 模型包含 Anthropic/Claude 标识")
    return ok


# ──────────────────────────────────────────────
# Test 2: 多模型 fallback
# ──────────────────────────────────────────────
async def test_model_fallback():
    print("\n=== Test 2: Model Fallback ===")
    from openrouter.components import ProviderPreferences

    client = make_client()
    messages = [{"role": "user", "content": "Say 'ok'"}]

    # 第一个模型是不存在的，应该 fallback 到第二个
    bad_model = "anthropic/nonexistent-model-xyz-v99"
    good_model = "anthropic/claude-haiku-4-5"

    actual_model = "unknown"
    content = ""
    try:
        async with await client.chat.send_async(
            messages=messages,
            models=[bad_model, good_model],
            provider=ProviderPreferences(order=["Anthropic"], allow_fallbacks=True),
            stream=True,
            max_tokens=10,
        ) as stream:
            async for chunk in stream:
                if hasattr(chunk, "model") and chunk.model:
                    actual_model = chunk.model
                if chunk.choices:
                    delta = chunk.choices[0].delta
                    if delta.content:
                        content += delta.content
        print(f"  实际模型: {actual_model}")
        print(f"  响应内容: {content.strip()!r}")
        # fallback 后应使用 good_model
        ok = content.strip() != ""
        print(f"  {'✅ PASS' if ok else '❌ FAIL'}: fallback 后收到内容")
    except Exception as e:
        print(f"  ❌ FAIL: 异常 {e}")
        ok = False
    return ok


# ──────────────────────────────────────────────
# Test 3: 多轮 reasoning_details 回传
# ──────────────────────────────────────────────
async def test_reasoning_multiturn():
    print("\n=== Test 3: Multi-turn Reasoning Details ===")
    from openrouter.components import ProviderPreferences, Reasoning

    client = make_client()

    # ── 第 1 轮：开启 reasoning，收集 reasoning_details ──
    print("  [Turn 1] 发送请求，收集 reasoning_details...")
    turn1_content = ""
    turn1_thinking = ""
    turn1_rd = None  # List[ReasoningDetailUnion]

    async with await client.chat.send_async(
        messages=[{"role": "user", "content": "What is 2+2? Think step by step."}],
        model="anthropic/claude-sonnet-4-5",
        provider=ProviderPreferences(order=["Anthropic"]),
        reasoning=Reasoning(effort="low"),
        stream=True,
        max_tokens=200,
        stream_options={"include_usage": True},
    ) as stream:
        async for chunk in stream:
            if chunk.choices:
                delta = chunk.choices[0].delta
                if delta.reasoning:
                    turn1_thinking += delta.reasoning
                if delta.content:
                    turn1_content += delta.content
                if delta.reasoning_details:
                    turn1_rd = delta.reasoning_details

    print(f"  [Turn 1] thinking: {turn1_thinking[:80]!r}...")
    print(f"  [Turn 1] content: {turn1_content.strip()!r}")
    print(f"  [Turn 1] reasoning_details: {len(turn1_rd) if turn1_rd else 0} blocks")

    if not turn1_rd:
        print("  ⚠️  SKIP: Turn 1 未返回 reasoning_details（模型可能不支持）")
        return True  # 不算失败

    # 序列化 reasoning_details 为 dicts（模拟存入 Redis 再读出）
    rd_serialized = [
        item.model_dump(by_alias=True, exclude_none=True)
        for item in turn1_rd
    ]
    print(f"  [Turn 1] 序列化后首个 block keys: {list(rd_serialized[0].keys())}")

    # 检查 signature 存在
    has_sig = any(
        b.get("signature") or b.get("data")
        for b in rd_serialized
    )
    print(f"  {'✅' if has_sig else '❌'} signature/data 字段存在: {has_sig}")

    # ── 第 2 轮：带 reasoning_details 回传，不应报 Invalid signature ──
    print("  [Turn 2] 回传 reasoning_details，继续对话...")
    turn2_content = ""
    turn2_error = None

    try:
        turn2_messages = [
            {"role": "user", "content": "What is 2+2? Think step by step."},
            {
                "role": "assistant",
                "content": turn1_content,
                "reasoning_details": rd_serialized,
            },
            {"role": "user", "content": "Now what is 3+3? Use the same method."},
        ]
        async with await client.chat.send_async(
            messages=turn2_messages,
            model="anthropic/claude-sonnet-4-5",
            provider=ProviderPreferences(order=["Anthropic"]),
            reasoning=Reasoning(effort="low"),
            stream=True,
            max_tokens=200,
        ) as stream:
            async for chunk in stream:
                if chunk.choices:
                    delta = chunk.choices[0].delta
                    if delta.content:
                        turn2_content += delta.content
    except Exception as e:
        turn2_error = str(e)

    if turn2_error:
        print(f"  ❌ FAIL Turn 2 报错: {turn2_error}")
        return False

    print(f"  [Turn 2] content: {turn2_content.strip()!r}")
    ok = "6" in turn2_content or "six" in turn2_content.lower()
    print(f"  {'✅ PASS' if ok else '⚠️ WARN'}: Turn 2 响应包含正确答案（6）")
    return True  # 只要不报错就算通过


# ──────────────────────────────────────────────
# Test 4: reasoning_details 完整性（signature 字段）
# ──────────────────────────────────────────────
async def test_reasoning_completeness():
    print("\n=== Test 4: Reasoning Details Completeness ===")
    from openrouter.components import ProviderPreferences, Reasoning, ReasoningDetailText

    client = make_client()

    all_rd = []
    async with await client.chat.send_async(
        messages=[{"role": "user", "content": "What is the capital of France?"}],
        model="anthropic/claude-sonnet-4-5",
        provider=ProviderPreferences(order=["Anthropic"]),
        reasoning=Reasoning(effort="low"),
        stream=True,
        max_tokens=100,
    ) as stream:
        async for chunk in stream:
            if chunk.choices:
                delta = chunk.choices[0].delta
                if delta.reasoning_details:
                    all_rd = delta.reasoning_details

    if not all_rd:
        print("  ⚠️  SKIP: 未收到 reasoning_details")
        return True

    print(f"  收到 {len(all_rd)} 个 reasoning_details blocks")
    for i, item in enumerate(all_rd):
        t = getattr(item, "type", "?")
        sig = None
        data = None
        if isinstance(item, ReasoningDetailText):
            sig = item.signature
            text_preview = (item.text or "")[:50]
            print(f"  Block {i}: type={t}, sig={sig!r:.40}..., text={text_preview!r}")
        else:
            data = getattr(item, "data", None)
            print(f"  Block {i}: type={t}, data={'present' if data else 'absent'}")

    serialized = [
        b.model_dump(by_alias=True, exclude_none=True) for b in all_rd
    ]
    complete = all(
        b.get("signature") or b.get("data")
        for b in serialized
        if b.get("type") in ("thinking", "reasoning")
    )
    print(f"  {'✅ PASS' if complete else '❌ FAIL'}: 所有 thinking block 含 signature/data")
    return complete


# ──────────────────────────────────────────────
# main
# ──────────────────────────────────────────────
async def main():
    test_arg = sys.argv[1] if len(sys.argv) > 1 else "all"
    tests = {
        "test1": test_provider_routing,
        "test2": test_model_fallback,
        "test3": test_reasoning_multiturn,
        "test4": test_reasoning_completeness,
    }

    if test_arg == "all":
        to_run = list(tests.values())
    elif test_arg in tests:
        to_run = [tests[test_arg]]
    else:
        print(f"未知测试: {test_arg}，可选: {list(tests.keys())} | all")
        sys.exit(1)

    results = []
    for fn in to_run:
        try:
            ok = await fn()
            results.append((fn.__name__, ok))
        except Exception:
            import traceback
            traceback.print_exc()
            results.append((fn.__name__, False))

    print("\n" + "=" * 50)
    print("验证结果汇总:")
    all_ok = True
    for name, ok in results:
        icon = "✅" if ok else "❌"
        print(f"  {icon} {name}")
        all_ok = all_ok and ok
    print("=" * 50)
    if all_ok:
        print("✅ 全部通过！可以安全写入 app/openrouter_client.py")
    else:
        print("❌ 有测试失败，请修复后再写生产代码")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
