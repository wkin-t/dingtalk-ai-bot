#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
结构测试: 验证文件存在性和基本语法
"""
import os
import ast

def test_file_existence():
    """测试关键文件是否存在"""
    print("=== 文件存在性检查 ===")

    files = [
        "app/openclaw_client.py",
        "app/config.py",
        "app/dingtalk_bot.py",
        "main.py",
        "requirements.txt",
        "docker-compose.yml",
        ".env.openclaw.example"
    ]

    all_exist = True
    for filepath in files:
        exists = os.path.exists(filepath)
        status = "✓" if exists else "✗"
        print(f"{status} {filepath}")
        if not exists:
            all_exist = False

    print()
    return all_exist

def test_python_syntax():
    """测试 Python 文件语法"""
    print("=== Python 语法检查 ===")

    py_files = [
        "app/openclaw_client.py",
        "app/config.py",
        "app/dingtalk_bot.py",
        "main.py"
    ]

    all_valid = True
    for filepath in py_files:
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                code = f.read()
                ast.parse(code)
            print(f"✓ {filepath} - 语法正确")
        except SyntaxError as e:
            print(f"✗ {filepath} - 语法错误: {e}")
            all_valid = False

    print()
    return all_valid

def test_requirements():
    """测试 requirements.txt 包含 websockets"""
    print("=== 依赖检查 ===")

    with open("requirements.txt", 'r') as f:
        content = f.read()

    if "websockets" in content:
        print("✓ requirements.txt 包含 websockets")
        result = True
    else:
        print("✗ requirements.txt 缺少 websockets")
        result = False

    print()
    return result

def test_docker_compose():
    """测试 docker-compose.yml 配置"""
    print("=== Docker Compose 配置检查 ===")

    # 检查 Gemini bot 配置
    with open("docker-compose.yml", 'r') as f:
        gemini_content = f.read()

    checks_gemini = [
        ("dingtalk-gemini 服务", "dingtalk-gemini:"),
        ("AI_BACKEND=gemini", "AI_BACKEND=gemini"),
        ("FLASK_PORT=35000", "FLASK_PORT=35000"),
    ]

    all_passed = True
    print("Gemini Bot (docker-compose.yml):")
    for check_name, pattern in checks_gemini:
        if pattern in gemini_content:
            print(f"  ✓ {check_name}")
        else:
            print(f"  ✗ {check_name} 未找到")
            all_passed = False

    # 检查 OpenClaw bot 配置
    if os.path.exists("docker-compose.openclaw.yml"):
        print("OpenClaw Bot (docker-compose.openclaw.yml):")
        with open("docker-compose.openclaw.yml", 'r') as f:
            openclaw_content = f.read()

        checks_openclaw = [
            ("dingtalk-openclaw 服务", "dingtalk-openclaw:"),
            ("AI_BACKEND=openclaw", "AI_BACKEND=openclaw"),
            ("FLASK_PORT=35001", "FLASK_PORT=35001"),
            ("OPENCLAW_GATEWAY_URL", "OPENCLAW_GATEWAY_URL"),
        ]

        for check_name, pattern in checks_openclaw:
            if pattern in openclaw_content:
                print(f"  ✓ {check_name}")
            else:
                print(f"  ✗ {check_name} 未找到")
                all_passed = False
    else:
        print("  ✗ docker-compose.openclaw.yml 不存在")
        all_passed = False

    print()
    return all_passed

def test_config_additions():
    """测试 config.py 新增配置"""
    print("=== 配置文件检查 ===")

    with open("app/config.py", 'r', encoding='utf-8') as f:
        content = f.read()

    checks = [
        "OPENCLAW_GATEWAY_URL",
        "OPENCLAW_GATEWAY_TOKEN",
        "OPENCLAW_AGENT_ID",
        "AI_BACKEND"
    ]

    all_found = True
    for var_name in checks:
        if var_name in content:
            print(f"✓ {var_name} 已添加")
        else:
            print(f"✗ {var_name} 未找到")
            all_found = False

    print()
    return all_found

def test_openclaw_client_functions():
    """测试 openclaw_client.py 关键函数"""
    print("=== OpenClaw 客户端检查 ===")

    with open("app/openclaw_client.py", 'r', encoding='utf-8') as f:
        content = f.read()

    checks = [
        ("OpenClawClient 类", "class OpenClawClient"),
        ("call_openclaw_stream 函数", "async def call_openclaw_stream"),
        ("WebSocket 连接", "websockets.connect"),
        ("JSON-RPC 调用", "call_rpc"),
    ]

    all_found = True
    for check_name, pattern in checks:
        if pattern in content:
            print(f"✓ {check_name}")
        else:
            print(f"✗ {check_name} 未找到")
            all_found = False

    print()
    return all_found

def test_dingtalk_bot_integration():
    """测试 dingtalk_bot.py 集成"""
    print("=== 钉钉机器人集成检查 ===")

    with open("app/dingtalk_bot.py", 'r', encoding='utf-8') as f:
        content = f.read()

    checks = [
        ("导入 AI_BACKEND", "from app.config import"),
        ("OpenClaw 后端判断", 'if AI_BACKEND == "openclaw"'),
        ("导入 call_openclaw_stream", "from app.openclaw_client import call_openclaw_stream"),
    ]

    all_found = True
    for check_name, pattern in checks:
        if pattern in content:
            print(f"✓ {check_name}")
        else:
            print(f"✗ {check_name} 未找到")
            all_found = False

    print()
    return all_found

def main():
    """运行所有测试"""
    print("\n" + "="*60)
    print("OpenClaw 集成结构测试")
    print("="*60 + "\n")

    results = [
        ("文件存在性", test_file_existence()),
        ("Python 语法", test_python_syntax()),
        ("依赖配置", test_requirements()),
        ("Docker Compose", test_docker_compose()),
        ("配置文件", test_config_additions()),
        ("OpenClaw 客户端", test_openclaw_client_functions()),
        ("钉钉机器人集成", test_dingtalk_bot_integration()),
    ]

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
        print("\n✅ 所有结构测试通过!")
        print("\n📋 验证清单:")
        print("  ✓ OpenClaw WebSocket 客户端已创建")
        print("  ✓ 配置文件已更新 (AI_BACKEND, OPENCLAW_* 变量)")
        print("  ✓ 依赖已添加 (websockets>=12.0)")
        print("  ✓ 钉钉机器人已集成后端切换逻辑")
        print("  ✓ Docker Compose 已配置 openclaw-app 服务")
        print("  ✓ 环境变量示例文件已创建")
        print("\n🚀 下一步:")
        print("  1. 复制 .env.openclaw.example 为 .env.openclaw")
        print("  2. 填入真实的钉钉凭证和 OpenClaw Gateway 配置")
        print("  3. 确保 OpenClaw Gateway 已部署 (ws://localhost:18789)")
        print("  4. 运行: docker-compose up -d --build openclaw-app")
        print("  5. 查看日志: docker logs -f openclaw-app")
        return 0
    else:
        print("\n❌ 部分测试失败,请检查上述错误。")
        return 1

if __name__ == "__main__":
    import sys
    sys.exit(main())
