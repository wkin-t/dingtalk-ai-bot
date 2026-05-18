# -*- coding: utf-8 -*-
"""Provider-aware sampling parameter clamps."""


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def clamp_temperature(temp: float, provider: str) -> float:
    """按 provider 约束 temperature 范围。"""
    normalized_provider = (provider or "").lower()
    if normalized_provider in ("gemini", "openai", "openrouter"):
        return _clamp(temp, 0.0, 2.0)
    if normalized_provider == "openclaw":
        return _clamp(temp, 0.0, 1.0)
    return temp


def clamp_top_p(top_p: float, provider: str) -> float:
    """按 provider 约束 top_p 范围。"""
    normalized_provider = (provider or "").lower()
    if normalized_provider in ("gemini", "openai", "openrouter", "openclaw"):
        return _clamp(top_p, 0.0, 1.0)
    return top_p
