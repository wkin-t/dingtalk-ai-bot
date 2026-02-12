#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
OpenClaw 集成测试脚本
验证配置加载、模块导入和基本逻辑
"""
import os
import sys

def test_config():
    """测试配置加载"""
    print("=== 测试 1: 配置加载 ===")
    from app.config import AI_BACKEND, OPENCLAW_GATEWAY_URL, OPENCLAW_GATEWAY_TOKEN, OPENCLAW_AGENT_ID

    print(f"✓ AI_BACKEND: {AI_BACKEND}")
    print(f"✓ OPENCLAW_GATEWAY_URL: {OPENCLAW_GATEWAY_URL}")
    print(f"✓ OPENCLAW_GATEWAY_TOKEN: {'[已设置]' if OPENCLAW_GATEWAY_TOKEN else '[未设置]'}")
    print(f"✓ OPENCLAW_AGENT_ID: {OPENCLAW_AGENT_ID}")
    print()

def test_imports():
    """测试模块导入"""
    print("=== 测试 2: 模块导入 ===")

    try:
        from app.openclaw_client import call_openclaw_stream
        print("✓ openclaw_client 导入成功")
    except ImportError as e:
        print(f"✗ openclaw_client 导入失败: {e}")
        assert False, f"openclaw_client 导入失败: {e}"

    try:
        from app.dingtalk_bot import GeminiBotHandler
        print("✓ dingtalk_bot 导入成功")
    except ImportError as e:
        print(f"✗ dingtalk_bot 导入失败: {e}")
        assert False, f"dingtalk_bot 导入失败: {e}"

    try:
        from app.gemini_client import call_gemini_stream
        print("✓ gemini_client 导入成功")
    except ImportError as e:
        print(f"✗ gemini_client 导入失败: {e}")
        assert False, f"gemini_client 导入失败: {e}"

    print()

def test_backend_selection():
    """测试后端选择逻辑"""
    print("=== 测试 3: 后端选择逻辑 ===")

    from app.config import AI_BACKEND

    if AI_BACKEND == "openclaw":
        print("✓ 当前后端: OpenClaw")
        print("  - 应使用 call_openclaw_stream")
    elif AI_BACKEND == "gemini":
        print("✓ 当前后端: Gemini")
        print("  - 应使用 call_gemini_stream")
        print("  - 应执行智能路由分析")
    else:
        assert False, f"未知后端: {AI_BACKEND}"

    print()

def test_websockets_dependency():
    """测试 websockets 依赖"""
    print("=== 测试 4: WebSockets 依赖 ===")

    try:
        import websockets
        print(f"✓ websockets 已安装 (version: {websockets.__version__})")
    except ImportError:
        print("✗ websockets 未安装")
        assert False, "websockets 未安装，请运行: pip install websockets>=12.0"

    print()

def test_docker_config():
    """测试 Docker 配置"""
    print("=== 测试 5: Docker 配置检查 ===")

    assert os.path.exists("docker-compose.yml"), "docker-compose.yml 不存在"
    print("✓ docker-compose.yml 存在")

    with open("docker-compose.yml", "r", encoding='utf-8') as f:
        content = f.read()

    # 检查示例配置
    assert os.path.exists(".env.openclaw.example"), ".env.openclaw.example 不存在"
    print("✓ .env.openclaw.example 存在")

    print()

def main():
    """运行所有测试"""
    print("\n" + "="*60)
    print("OpenClaw 集成测试")
    print("="*60 + "\n")

    tests = [
        ("配置加载", test_config),
        ("模块导入", test_imports),
        ("后端选择", test_backend_selection),
        ("WebSockets", test_websockets_dependency),
        ("Docker 配置", test_docker_config),
    ]

    results = []
    for test_name, test_func in tests:
        try:
            test_func()
            results.append((test_name, True))
        except (AssertionError, Exception) as e:
            print(f"  ✗ 失败: {e}\n")
            results.append((test_name, False))

    # 总结
    print("="*60)
    print("测试结果汇总:")
    print("="*60)

    all_passed = True
    for test_name, passed in results:
        status = "✓ 通过" if passed else "✗ 失败"
        print(f"{test_name:20s}: {status}")
        if not passed:
            all_passed = False

    print("="*60)

    if all_passed:
        print("\n✅ 所有测试通过! OpenClaw 集成准备就绪。")
        return 0
    else:
        print("\n❌ 部分测试失败,请检查上述错误。")
        return 1

if __name__ == "__main__":
    sys.exit(main())
