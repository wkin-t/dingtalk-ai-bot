# tests/test_e2e_sample_override.py
# -*- coding: utf-8 -*-
"""E2E: set/get/reset 完整流程 + sampling_pipeline resolve 行为"""
import pytest


@pytest.fixture(autouse=True)
def _clean_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_ID", "test_e2e_bot")
    monkeypatch.setattr("app.sample_override.SAMPLE_DIR", str(tmp_path))
    monkeypatch.setattr("app.sample_override._redis_client", False)
    yield


def test_full_set_get_reset_flow():
    from app.sample_override import set_override, get_override, reset_override

    assert get_override("s1") is None

    # 设温度
    set_override("s1", temperature=1.5, set_by="u1", set_by_nick="张三")
    rec = get_override("s1")
    assert rec["temperature"] == 1.5
    assert rec["top_p"] is None
    assert rec["set_by_nick"] == "张三"

    # 增加 top_p
    set_override("s1", top_p=0.9, set_by="u2", set_by_nick="李四")
    rec = get_override("s1")
    assert rec["temperature"] == 1.5  # 保留
    assert rec["top_p"] == 0.9
    assert rec["set_by_nick"] == "李四"  # 最新设置人

    # 单独 reset temp
    reset_override("s1", what="temperature")
    rec = get_override("s1")
    assert rec is not None
    assert rec["temperature"] is None
    assert rec["top_p"] == 0.9

    # reset 全部
    reset_override("s1", what="all")
    assert get_override("s1") is None


def test_resolve_sampling_uses_router_when_no_override(monkeypatch):
    from app.ai.sampling_pipeline import resolve_sampling
    final_temp, final_top_p, rec = resolve_sampling("no_override_session", router_temperature=0.7)
    assert final_temp == 0.7
    assert final_top_p is None
    assert rec is None


def test_resolve_sampling_uses_manual_override():
    from app.sample_override import set_override
    from app.ai.sampling_pipeline import resolve_sampling

    set_override("manual_session", temperature=1.5, top_p=0.9, set_by="u", set_by_nick="n")
    final_temp, final_top_p, rec = resolve_sampling("manual_session", router_temperature=0.7)
    assert final_temp == 1.5
    assert final_top_p == 0.9
    assert rec is not None


def test_resolve_sampling_partial_manual_temp_only():
    from app.sample_override import set_override
    from app.ai.sampling_pipeline import resolve_sampling

    set_override("partial_temp", temperature=1.3, set_by="u", set_by_nick="n")
    final_temp, final_top_p, _ = resolve_sampling("partial_temp", router_temperature=0.7)
    assert final_temp == 1.3
    assert final_top_p is None  # top_p 未手动，不传 API


def test_resolve_sampling_disabled_flag(monkeypatch):
    """ENABLE_SAMPLE_OVERRIDE=False 时 override 被旁路"""
    from app.sample_override import set_override
    import app.ai.sampling_pipeline as sp

    set_override("disabled_session", temperature=1.5, set_by="u", set_by_nick="n")
    monkeypatch.setattr(sp, "ENABLE_SAMPLE_OVERRIDE", False)
    final_temp, final_top_p, rec = sp.resolve_sampling("disabled_session", router_temperature=0.7)
    assert final_temp == 0.7  # 不读 override
    assert rec is None
