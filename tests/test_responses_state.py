"""responses_state 模块单元测试 — Redis + 文件降级存储 previous_response_id"""
import os
import json
import tempfile
import shutil
import pytest
from unittest.mock import patch

os.environ.setdefault("GEMINI_API_KEY", "test-dummy-key")


class TestResponsesStateRoundTrip:
    """无 Redis 场景下走文件降级的存读删完整链路。"""

    def setup_method(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="responses_state_test_")
        self._patches = [
            patch("app.responses_state.STATE_DIR", self.tmp_dir),
            patch("app.responses_state._get_redis", lambda: None),
        ]
        for p in self._patches:
            p.start()

    def teardown_method(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_set_then_get_returns_response_id(self):
        from app.responses_state import set_response_id, get_response_id
        set_response_id("conv-1", "resp_abc123")
        assert get_response_id("conv-1") == "resp_abc123"

    def test_missing_conversation_returns_none(self):
        from app.responses_state import get_response_id
        assert get_response_id("nonexistent") is None

    def test_clear_removes_stored_id(self):
        from app.responses_state import set_response_id, get_response_id, clear_response_id
        set_response_id("conv-2", "resp_xyz")
        clear_response_id("conv-2")
        assert get_response_id("conv-2") is None

    def test_empty_conversation_id_is_noop(self):
        """空 conversation_id 不应崩溃，不写文件"""
        from app.responses_state import set_response_id, get_response_id
        set_response_id("", "resp_should_not_persist")
        assert get_response_id("") is None

    def test_file_isolation_per_conversation(self):
        from app.responses_state import set_response_id, get_response_id
        set_response_id("A", "resp_A")
        set_response_id("B", "resp_B")
        assert get_response_id("A") == "resp_A"
        assert get_response_id("B") == "resp_B"

    def test_expired_record_returns_none_and_cleans(self):
        """expires_at 已过期应自动清理并返回 None"""
        from app.responses_state import set_response_id, get_response_id, _file_path
        from datetime import datetime, timedelta
        set_response_id("conv-expire", "resp_old")
        path = _file_path("conv-expire")
        with open(path, "r", encoding="utf-8") as f:
            rec = json.load(f)
        rec["expires_at"] = (datetime.now() - timedelta(seconds=10)).isoformat()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rec, f)
        assert get_response_id("conv-expire") is None
        assert not os.path.exists(path)



class TestClearCutoffIntegration:
    """clear_cutoff 的 set/reset 必须同步清掉 previous_response_id"""

    def setup_method(self):
        self.tmp_dir_state = tempfile.mkdtemp(prefix="responses_state_test_")
        self.tmp_dir_cutoff = tempfile.mkdtemp(prefix="cutoff_test_")
        self._patches = [
            patch("app.responses_state.STATE_DIR", self.tmp_dir_state),
            patch("app.responses_state._get_redis", lambda: None),
            patch("app.clear_cutoff.CUTOFF_DIR", self.tmp_dir_cutoff),
        ]
        for p in self._patches:
            p.start()

    def teardown_method(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.tmp_dir_state, ignore_errors=True)
        shutil.rmtree(self.tmp_dir_cutoff, ignore_errors=True)

    def test_set_cutoff_clears_response_id(self):
        """/clear: 软清空时本轮 response_id 失效，否则上游会续接已清空的 thinking 链"""
        from app.responses_state import set_response_id, get_response_id
        from app.clear_cutoff import set_cutoff
        set_response_id("sess-clear", "resp_prev")
        assert get_response_id("sess-clear") == "resp_prev"
        set_cutoff("sess-clear", set_by="u1", set_by_nick="user1")
        assert get_response_id("sess-clear") is None

    def test_reset_cutoff_clears_response_id(self):
        """/resume: 本地恢复完整历史后服务端 response_id 必须同时清空，避免状态不一致"""
        from app.responses_state import set_response_id, get_response_id
        from app.clear_cutoff import reset_cutoff
        set_response_id("sess-resume", "resp_post_cutoff")
        reset_cutoff("sess-resume")
        assert get_response_id("sess-resume") is None
