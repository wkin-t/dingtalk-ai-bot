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
        from app.openclaw_client import call_openclaw_stream, OpenClawClient
        print("✓ openclaw_client 导入成功")
    except ImportError as e:
        print(f"✗ openclaw_client 导入失败: {e}")
        return False

    try:
        from app.dingtalk_bot import GeminiBotHandler
        print("✓ dingtalk_bot 导入成功")
    except ImportError as e:
        print(f"✗ dingtalk_bot 导入失败: {e}")
        return False

    try:
        from app.gemini_client import call_gemini_stream
        print("✓ gemini_client 导入成功")
    except ImportError as e:
        print(f"✗ gemini_client 导入失败: {e}")
        return False

    print()
    return True

def test_backend_selection():
    """测试后端选择逻辑"""
    print("=== 测试 3: 后端选择逻辑 ===")

    from app.config import AI_BACKEND

    if AI_BACKEND == "openclaw":
        print("✓ 当前后端: OpenClaw")
        print("  - 应使用 call_openclaw_stream")
        print("  - 应跳过 Gemini 智能路由")
    elif AI_BACKEND == "gemini":
        print("✓ 当前后端: Gemini")
        print("  - 应使用 call_gemini_stream")
        print("  - 应执行智能路由分析")
    else:
        print(f"✗ 未知后端: {AI_BACKEND}")
        return False

    print()
    return True

def test_websockets_dependency():
    """测试 websockets 依赖"""
    print("=== 测试 4: WebSockets 依赖 ===")

    try:
        import websockets
        print(f"✓ websockets 已安装 (version: {websockets.__version__})")
    except ImportError:
        print("✗ websockets 未安装")
        print("  请运行: pip install websockets>=12.0")
        return False

    print()
    return True

def test_docker_config():
    """测试 Docker 配置"""
    print("=== 测试 5: Docker 配置检查 ===")

    # 检查 docker-compose.yml
    if os.path.exists("docker-compose.yml"):
        print("✓ docker-compose.yml 存在")
        with open("docker-compose.yml", "r") as f:
            content = f.read()
            if "openclaw-app:" in content:
                print("✓ openclaw-app 服务已配置")
            else:
                print("✗ openclaw-app 服务未找到")
                return False
    else:
        print("✗ docker-compose.yml 不存在")
        return False

    # 检查示例配置
    if os.path.exists(".env.openclaw.example"):
        print("✓ .env.openclaw.example 存在")
    else:
        print("✗ .env.openclaw.example 不存在")
        return False

    print()
    return True

def main():
    """运行所有测试"""
    print("\n" + "="*60)
    print("OpenClaw 集成测试")
    print("="*60 + "\n")

    results = []

    try:
        test_config()
        results.append(("配置加载", True))
    except Exception as e:
        print(f"✗ 配置加载失败: {e}\n")
        results.append(("配置加载", False))

    try:
        success = test_imports()
        results.append(("模块导入", success))
    except Exception as e:
        print(f"✗ 模块导入失败: {e}\n")
        results.append(("模块导入", False))

    try:
        success = test_backend_selection()
        results.append(("后端选择", success))
    except Exception as e:
        print(f"✗ 后端选择失败: {e}\n")
        results.append(("后端选择", False))

    try:
        success = test_websockets_dependency()
        results.append(("WebSockets", success))
    except Exception as e:
        print(f"✗ WebSockets 检查失败: {e}\n")
        results.append(("WebSockets", False))

    try:
        success = test_docker_config()
        results.append(("Docker 配置", success))
    except Exception as e:
        print(f"✗ Docker 配置检查失败: {e}\n")
        results.append(("Docker 配置", False))

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
        print("\n下一步:")
        print("1. 复制 .env.openclaw.example 为 .env.openclaw 并填入真实凭证")
        print("2. 确保 OpenClaw Gateway 已部署并可访问")
        print("3. 运行: docker-compose up -d --build openclaw-app")
        return 0
    else:
        print("\n❌ 部分测试失败,请检查上述错误。")
        return 1

if __name__ == "__main__":
    sys.exit(main())
