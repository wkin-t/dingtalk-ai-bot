"""按模型家族管理思考深度能力。

模型名中的 ``-low``、``-high`` 等只是中转站别名的一部分，不代表本次
请求真正使用的思考深度。真正的深度由独立的 ``thinking_level`` 字段决定。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any

from app import config as cfg

logger = logging.getLogger(__name__)

THINKING_LEVEL_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
_LEVEL_INDEX = {level: index for index, level in enumerate(THINKING_LEVEL_ORDER)}
_GEMINI_4_LEVELS = ("low", "medium", "high")
_GEMINI_4_HYDROGEN_LEVELS = ("low", "medium", "high", "xhigh")
_GEMINI_LEGACY_LEVELS = ("low", "medium", "high")
_OPENAI_LEGACY_LEVELS = ("low", "medium", "high", "xhigh")
_CLAUDE_5_LEVELS = ("low", "medium", "high", "xhigh", "max")
_CLAUDE_5_OPENROUTER_LEVELS = ("low", "medium", "high")
_GPT_5_LEVELS = ("low", "medium", "high", "xhigh", "max")
_GPT_5_OPENROUTER_LEVELS = ("low", "medium", "high", "xhigh")
_UNKNOWN_LEVELS = _GEMINI_LEGACY_LEVELS


def _clean_model_name(model_name: Any) -> str:
    return model_name.strip().lower() if isinstance(model_name, str) else ""


def _model_override(model_name: str, backend: str) -> tuple[str, ...] | None:
    """读取并严格校验精确覆盖；配置错误不能静默退回默认能力。"""
    overrides = getattr(cfg, "THINKING_LEVEL_OVERRIDES", {})
    if overrides is None:
        return None
    if not isinstance(overrides, Mapping):
        raise ValueError("THINKING_LEVEL_OVERRIDES 必须是字典")
    exact_model = model_name.strip() if isinstance(model_name, str) else ""
    backend_key = str(backend or "").strip().lower() + ":" + exact_model
    key = backend_key if backend_key in overrides else exact_model
    if key not in overrides:
        return None
    value = overrides[key]
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or not value:
        raise ValueError("THINKING_LEVEL_OVERRIDES 的每项必须是非空档位列表")
    levels = tuple(str(item).strip().lower() for item in value)
    if any(level not in _LEVEL_INDEX or level == "minimal" for level in levels):
        raise ValueError("THINKING_LEVEL_OVERRIDES 含未知思考档位")
    if len(set(levels)) != len(levels):
        raise ValueError("THINKING_LEVEL_OVERRIDES 不允许重复思考档位")
    if tuple(sorted(levels, key=_LEVEL_INDEX.__getitem__)) != levels:
        raise ValueError("THINKING_LEVEL_OVERRIDES 必须按思考档位由浅到深排列")
    return levels


def _is_gemini_4(model_name: str) -> bool:
    return bool(re.search(r"gemini-4-(?:argon|carbon|hydrogen)(?:[-.]|$)", model_name))


def _is_hydrogen(model_name: str) -> bool:
    return bool(re.search(r"gemini-4-hydrogen(?:[-.]|$)", model_name))


def _supported_openai_model(model_name: str, backend: str) -> tuple[str, ...] | None:
    if backend not in {"openai", "openrouter"}:
        return None
    if re.search(r"claude-(?:sonnet|opus|haiku)-5(?:[-.]|$)", model_name):
        return _CLAUDE_5_LEVELS if backend == "openai" else _CLAUDE_5_OPENROUTER_LEVELS
    if backend == "openrouter" and any(family in model_name for family in ("claude", "gpt", "o1", "o3", "o4")):
        return ("low", "medium", "high")
    if "gpt-6" in model_name or "gpt-5" in model_name:
        if backend != "openai":
            return _GPT_5_OPENROUTER_LEVELS
        if re.search(r"(?:^|/)(?:gpt-6-luna|gpt-6-sol|gpt-6\.1-terra)(?:-|$)", model_name):
            return ("none",) + _GPT_5_LEVELS
        return _GPT_5_LEVELS
    if any(family in model_name for family in ("claude",)):
        return ("low", "medium", "high")
    if any(family in model_name for family in ("gpt", "o1", "o3", "o4")):
        return _OPENAI_LEGACY_LEVELS
    return None


def supported_thinking_levels(model_name: str, backend: str = "openai") -> tuple[str, ...]:
    """返回模型实际支持的思考档位，顺序从浅到深。"""
    raw_name = model_name if isinstance(model_name, str) else ""
    name = _clean_model_name(raw_name)
    normalized_backend = str(backend or "").strip().lower()
    override = _model_override(raw_name, normalized_backend)
    if override is not None:
        return override
    if normalized_backend == "openrouter":
        return ("low", "medium", "high")
    if _is_gemini_4(name):
        return _GEMINI_4_HYDROGEN_LEVELS if _is_hydrogen(name) else _GEMINI_4_LEVELS
    openai_levels = _supported_openai_model(name, normalized_backend)
    if openai_levels is not None:
        return openai_levels
    if normalized_backend == "gemini" and "gemini" in name:
        return _GEMINI_LEGACY_LEVELS
    return _UNKNOWN_LEVELS


def normalize_thinking_level(level: Any, model_name: str, backend: str = "openai") -> str:
    """将路由输出归一化为模型可接受的独立思考深度字段。"""
    requested = level.strip().lower() if isinstance(level, str) else ""
    supported = supported_thinking_levels(model_name, backend)
    if requested not in _LEVEL_INDEX:
        normalized = supported[0]
    elif requested in {"none", "minimal"}:
        normalized = supported[0]
    elif requested in supported:
        normalized = requested
    else:
        requested_index = _LEVEL_INDEX[requested]
        candidates = [item for item in supported if _LEVEL_INDEX[item] <= requested_index]
        normalized = candidates[-1] if candidates else supported[0]
    if normalized != requested:
        safe_requested = requested if requested in _LEVEL_INDEX else "invalid"
        print(f"🧠 思考档位已按模型能力归一化: {safe_requested}→{normalized}")
    return normalized


def normalize_thinking_for_models(level: Any, models: Sequence[str], backend: str) -> str:
    """服务端自主 fallback 共用一个字段时，只发送所有候选都支持的档位。"""
    if not models:
        raise ValueError("思考能力校验至少需要一个模型")
    common = set(supported_thinking_levels(models[0], backend))
    for model in models[1:]:
        common.intersection_update(supported_thinking_levels(model, backend))
    if not common:
        raise ValueError("主模型与 fallback 没有共同支持的思考档位")
    requested = level.strip().lower() if isinstance(level, str) else ""
    ordered = sorted(common, key=_LEVEL_INDEX.__getitem__)
    allowed = [candidate for candidate in ordered if _LEVEL_INDEX[candidate] <= _LEVEL_INDEX.get(requested, -1)]
    return allowed[-1] if allowed else ordered[0]


def thinking_capabilities_prompt(backend: str, models: Mapping[str, str]) -> str:
    """生成供路由模型使用的模型能力说明。"""
    if not isinstance(models, Mapping):
        raise TypeError("models 必须是 slot 到模型名的字典")
    normalized_backend = str(backend or "").strip().lower()
    lines = [
        "思考深度是独立字段 thinking_level，不要根据模型名末尾的 low、medium、high 或 xhigh 推断深度。",
        "只返回当前模型能力列表中的档位；若请求档位不支持，程序会降到不超过请求档位的最高支持档。none 是完全不思考，只有能力列表明确包含时才可返回；minimal 是旧兼容输入，会归一化到当前模型最低档，不要主动返回 minimal。",
        "简单问候和短确认选择最低可用档（支持 none 时优先 none，否则 low）；普通问答用 low，需要推理用 medium，复杂分析用 high，极难多步问题才考虑 xhigh，只有确需最高预算的困难任务才用 max。",
    ]
    for slot, model_name in models.items():
        if not isinstance(slot, str) or not isinstance(model_name, str):
            raise ValueError("models 的 slot 和模型名必须是字符串")
        levels = supported_thinking_levels(model_name, normalized_backend)
        lowest = levels[0]
        lines.append(f"- {slot}: {model_name} → {', '.join(levels)}（最低可用：{lowest}）")
    return "\n".join(lines)
