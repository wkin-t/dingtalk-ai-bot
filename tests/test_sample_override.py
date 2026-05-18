# tests/test_sample_override.py
import os
import json
import time
import pytest
from datetime import datetime, timedelta
from unittest.mock import patch


@pytest.fixture(autouse=True)
def clean_sample_dir(tmp_path, monkeypatch):
    """每个测试用独立临时目录"""
    monkeypatch.setenv("BOT_ID", "test_bot")
    monkeypatch.setattr("app.sample_override.SAMPLE_DIR", str(tmp_path))
    monkeypatch.setattr("app.sample_override._redis_client", False)
    yield


def test_set_get_temperature():
    from app.sample_override import set_override, get_override
    set_override("session_x", temperature=1.5, set_by="user1", set_by_nick="张三")
    rec = get_override("session_x")
    assert rec is not None
    assert rec["temperature"] == 1.5
    assert rec["top_p"] is None
    assert rec["set_by"] == "user1"


def test_set_get_top_p():
    from app.sample_override import set_override, get_override
    set_override("session_x", top_p=0.9, set_by="user1", set_by_nick="张三")
    rec = get_override("session_x")
    assert rec["top_p"] == 0.9


def test_reset_clears_all():
    from app.sample_override import set_override, get_override, reset_override
    set_override("session_x", temperature=1.5, set_by="u", set_by_nick="n")
    reset_override("session_x", what="all")
    assert get_override("session_x") is None


def test_reset_only_temp():
    from app.sample_override import set_override, get_override, reset_override
    set_override("session_x", temperature=1.5, top_p=0.9, set_by="u", set_by_nick="n")
    reset_override("session_x", what="temperature")
    rec = get_override("session_x")
    assert rec is not None
    assert rec["temperature"] is None
    assert rec["top_p"] == 0.9


def test_expires_at_respected():
    from app.sample_override import set_override, get_override, _DEFAULT_TTL_HOURS
    set_override("session_x", temperature=1.5, set_by="u", set_by_nick="n")
    rec = get_override("session_x")
    expires = datetime.fromisoformat(rec["expires_at"])
    now = datetime.now()
    delta = expires - now
    assert timedelta(hours=23) < delta < timedelta(hours=25)


def test_expired_record_returns_none(tmp_path):
    from app.sample_override import set_override, get_override, SAMPLE_DIR
    # 写一条已过期的记录
    path = os.path.join(SAMPLE_DIR, "test_bot__session_y.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({
            "temperature": 1.5, "top_p": None,
            "set_at": "2020-01-01 00:00:00", "set_by": "u", "set_by_nick": "n",
            "expires_at": "2020-01-02 00:00:00"
        }, f)
    rec = get_override("session_y")
    assert rec is None
    # 过期文件应被删除
    assert not os.path.exists(path)


def test_validate_temperature_rejects_oob():
    from app.sample_override import validate_temperature
    ok, _ = validate_temperature(1.5)
    assert ok
    ok, err = validate_temperature(3.0)
    assert not ok
    assert "0" in err and "2" in err

    ok, err = validate_temperature(-0.1)
    assert not ok

    ok, err = validate_temperature("abc")
    assert not ok


def test_validate_top_p_rejects_zero():
    from app.sample_override import validate_top_p
    ok, _ = validate_top_p(0.9)
    assert ok
    ok, err = validate_top_p(0.0)
    assert not ok
    ok, err = validate_top_p(1.1)
    assert not ok
