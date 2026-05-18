# app/ai/sampling_pipeline.py
# -*- coding: utf-8 -*-
"""Resolve final sampling params: router output 优先级低于用户手动 override。

返回 (final_temperature, final_top_p, override_record_or_None)。
- final_temperature 总是有值（router 的兜底）
- final_top_p 是 None 表示不传给 API（沿用模型默认 1.0）
- override_record 用于卡片 thinkingText 显示 set_by_nick / ⚙️ 标记
"""
from typing import Optional, Tuple, Dict, Any

from app.config import ENABLE_SAMPLE_OVERRIDE, ENABLE_TOP_P_PIPELINE


def resolve_sampling(
    session_key: str,
    router_temperature: float,
) -> Tuple[float, Optional[float], Optional[Dict[str, Any]]]:
    """返回 (final_temp, final_top_p, override_record_or_None).

    - ENABLE_SAMPLE_OVERRIDE=False: 忽略手动设置，直接用 router_temperature, top_p=None
    - ENABLE_TOP_P_PIPELINE=False: 即使有手动 top_p 也不下发到 API（回滚 C stage 用）
    """
    if not ENABLE_SAMPLE_OVERRIDE:
        return router_temperature, None, None

    try:
        from app.sample_override import get_override
        rec = get_override(session_key)
    except Exception as e:
        print(f"⚠️ [sampling_pipeline] 读取 override 失败，降级 router: {e}")
        return router_temperature, None, None

    if not rec:
        return router_temperature, None, None

    final_temp = rec["temperature"] if rec.get("temperature") is not None else router_temperature
    final_top_p = rec.get("top_p") if ENABLE_TOP_P_PIPELINE else None
    return final_temp, final_top_p, rec
