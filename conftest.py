# -*- coding: utf-8 -*-
"""pytest 全局配置"""
import os
import sys

# 在导入 app 模块前设置测试用环境变量
# gemini_client.py 模块级初始化需要 GEMINI_API_KEY
os.environ.setdefault("GEMINI_API_KEY", "test-dummy-key")


def pytest_collection_modifyitems(config, items):
    """跳过 .venv 目录下的测试"""
    skip_venv = []
    for item in items:
        if ".venv" in str(item.fspath):
            skip_venv.append(item)
    for item in skip_venv:
        items.remove(item)
