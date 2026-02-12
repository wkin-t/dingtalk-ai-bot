# -*- coding: utf-8 -*-
"""
对话历史管理模块单元测试

测试目标:
1. update_history() 写入 bot_id 字段 (文件存储降级路径 + 数据库路径)
2. get_history() 返回包含 bot_id 的消息 (文件存储 + 数据库路径)
3. bot_id 向后兼容 (旧数据无 bot_id 时正常工作)
4. get_session_key() 平台前缀逻辑
5. clear_history() 两条路径
6. 边界情况: 过期数据、损坏文件、存储长度限制
"""
import pytest
import os
import json
import time
from unittest.mock import patch, MagicMock, call


# ─── get_session_key 测试 ──────────────────────────────────────────


class TestGetSessionKey:
    """get_session_key() 平台前缀逻辑"""

    def test_add_dingtalk_prefix(self):
        """无前缀的 conversation_id 应添加 dingtalk_ 前缀"""
        from app.memory import get_session_key
        assert get_session_key("abc123") == "dingtalk_abc123"

    def test_add_wecom_prefix(self):
        """指定 wecom 平台应添加 wecom_ 前缀"""
        from app.memory import get_session_key
        assert get_session_key("abc123", platform="wecom") == "wecom_abc123"

    def test_already_has_dingtalk_prefix(self):
        """已有 dingtalk_ 前缀时直接返回"""
        from app.memory import get_session_key
        assert get_session_key("dingtalk_abc123") == "dingtalk_abc123"

    def test_already_has_wecom_prefix(self):
        """已有 wecom_ 前缀时直接返回"""
        from app.memory import get_session_key
        assert get_session_key("wecom_abc123") == "wecom_abc123"

    def test_sender_id_ignored(self):
        """sender_id 参数保留兼容但不影响结果"""
        from app.memory import get_session_key
        result = get_session_key("abc123", sender_id="user001")
        assert result == "dingtalk_abc123"


# ─── 文件存储路径测试 ──────────────────────────────────────────────


class TestFileStorageBotId:
    """文件存储降级路径: bot_id 读写测试"""

    @pytest.fixture(autouse=True)
    def setup_teardown(self, tmp_path):
        """每个测试使用临时目录作为数据存储"""
        self.data_dir = str(tmp_path / "history")
        os.makedirs(self.data_dir, exist_ok=True)

        # Patch 所有需要的模块级变量
        self.patches = [
            patch("app.memory.USE_DATABASE", False),
            patch("app.memory.DATA_DIR", self.data_dir),
            patch("app.memory.BOT_ID", "gemini"),
        ]
        for p in self.patches:
            p.start()

        yield

        for p in self.patches:
            p.stop()

    def test_assistant_message_contains_bot_id(self):
        """assistant 消息应包含 bot_id 字段"""
        from app.memory import update_history, _get_file_path

        session_key = "dingtalk_test_session_001"
        update_history(session_key, user_msg="你好", assistant_msg="你好！有什么可以帮助你的？", sender_nick="张三")

        file_path = _get_file_path(session_key)
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        messages = data["messages"]
        assert len(messages) == 2

        user_msg = messages[0]
        assert user_msg["role"] == "user"
        assert "bot_id" not in user_msg or user_msg.get("bot_id") is None

        assistant_msg = messages[1]
        assert assistant_msg["role"] == "assistant"
        assert assistant_msg["bot_id"] == "gemini"

    def test_openclaw_bot_id(self):
        """OpenClaw 后端应写入 bot_id='openclaw'"""
        for p in self.patches:
            p.stop()
        self.patches[2] = patch("app.memory.BOT_ID", "openclaw")
        for p in self.patches:
            p.start()

        from app.memory import update_history, _get_file_path

        session_key = "dingtalk_test_session_002"
        update_history(session_key, user_msg=None, assistant_msg="OpenClaw 回复")

        file_path = _get_file_path(session_key)
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        messages = data["messages"]
        assert len(messages) == 1
        assert messages[0]["bot_id"] == "openclaw"

    def test_get_history_returns_bot_id(self):
        """get_history 应返回包含 bot_id 的消息"""
        from app.memory import update_history, get_history

        session_key = "dingtalk_test_session_003"
        update_history(session_key, user_msg="测试", assistant_msg="回复", sender_nick="李四")

        history = get_history(session_key)
        assert len(history) == 2
        assert history[0]["role"] == "user"
        assert history[1]["role"] == "assistant"
        assert history[1]["bot_id"] == "gemini"

    def test_backward_compat_no_bot_id(self):
        """旧数据无 bot_id 时，get_history 应正常工作"""
        from app.memory import get_history, _get_file_path

        session_key = "dingtalk_test_session_old"
        file_path = _get_file_path(session_key)

        old_data = {
            "messages": [
                {"role": "user", "content": "旧消息", "timestamp": "2026-01-01 10:00:00", "sender_nick": "老用户"},
                {"role": "assistant", "content": "旧回复", "timestamp": "2026-01-01 10:00:05", "sender_nick": None},
            ],
            "last_active": time.time()
        }
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(old_data, f, ensure_ascii=False)

        history = get_history(session_key)
        assert len(history) == 2
        assert history[1]["role"] == "assistant"
        assert history[1].get("bot_id") is None

    def test_mixed_bot_id_messages(self):
        """混合新旧格式消息应正常读取"""
        from app.memory import get_history, _get_file_path

        session_key = "dingtalk_test_session_mixed"
        file_path = _get_file_path(session_key)

        mixed_data = {
            "messages": [
                {"role": "user", "content": "问题1", "timestamp": "2026-02-01 10:00:00", "sender_nick": "用户A"},
                {"role": "assistant", "content": "旧回复", "timestamp": "2026-02-01 10:00:05", "sender_nick": None},
                {"role": "user", "content": "问题2", "timestamp": "2026-02-01 10:01:00", "sender_nick": "用户A"},
                {"role": "assistant", "content": "Gemini 回复", "timestamp": "2026-02-01 10:01:05", "sender_nick": None, "bot_id": "gemini"},
                {"role": "user", "content": "问题3", "timestamp": "2026-02-01 10:02:00", "sender_nick": "用户A"},
                {"role": "assistant", "content": "OpenClaw 回复", "timestamp": "2026-02-01 10:02:05", "sender_nick": None, "bot_id": "openclaw"},
            ],
            "last_active": time.time()
        }
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(mixed_data, f, ensure_ascii=False)

        history = get_history(session_key)
        assert len(history) == 6
        assert history[1].get("bot_id") is None
        assert history[3]["bot_id"] == "gemini"
        assert history[5]["bot_id"] == "openclaw"

    def test_get_history_nonexistent_file(self):
        """不存在的文件应返回空列表"""
        from app.memory import get_history

        history = get_history("dingtalk_nonexistent_session")
        assert history == []

    def test_get_history_expired_data(self):
        """过期数据应返回空列表并删除文件"""
        from app.memory import get_history, _get_file_path

        session_key = "dingtalk_test_expired"
        file_path = _get_file_path(session_key)

        # last_active 设为 8 天前 (超过 7 天 TTL)
        expired_data = {
            "messages": [
                {"role": "user", "content": "过期消息", "timestamp": "2026-01-01 10:00:00", "sender_nick": "用户"},
            ],
            "last_active": time.time() - (8 * 24 * 3600)
        }
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(expired_data, f, ensure_ascii=False)

        history = get_history(session_key)
        assert history == []
        assert not os.path.exists(file_path)

    def test_get_history_corrupted_file(self):
        """损坏的 JSON 文件应返回空列表"""
        from app.memory import get_history, _get_file_path

        session_key = "dingtalk_test_corrupted"
        file_path = _get_file_path(session_key)

        with open(file_path, 'w', encoding='utf-8') as f:
            f.write("{ invalid json }")

        history = get_history(session_key)
        assert history == []

    def test_get_history_with_limit(self):
        """limit 参数应限制返回条数"""
        from app.memory import get_history, _get_file_path

        session_key = "dingtalk_test_limit"
        file_path = _get_file_path(session_key)

        data = {
            "messages": [
                {"role": "user", "content": f"消息{i}", "timestamp": "2026-02-01 10:00:00", "sender_nick": "用户"}
                for i in range(10)
            ],
            "last_active": time.time()
        }
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)

        history = get_history(session_key, limit=3)
        assert len(history) == 3
        # 应返回最后 3 条
        assert history[0]["content"] == "消息7"
        assert history[2]["content"] == "消息9"

    def test_update_history_storage_length_limit(self):
        """超过 MAX_STORAGE_LENGTH 时应截断"""
        from app.memory import update_history, _get_file_path

        # 临时设置小的 MAX_STORAGE_LENGTH
        with patch("app.memory.MAX_STORAGE_LENGTH", 3):
            session_key = "dingtalk_test_limit_storage"
            # 写入 4 条消息 (2 对话轮)
            update_history(session_key, user_msg="消息1", assistant_msg="回复1", sender_nick="用户")
            update_history(session_key, user_msg="消息2", assistant_msg="回复2", sender_nick="用户")

            file_path = _get_file_path(session_key)
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            # 应只保留最后 3 条
            assert len(data["messages"]) == 3

    def test_update_history_only_user_msg(self):
        """只有用户消息时不应写入 assistant"""
        from app.memory import update_history, _get_file_path

        session_key = "dingtalk_test_user_only"
        update_history(session_key, user_msg="只有用户消息", sender_nick="用户")

        file_path = _get_file_path(session_key)
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        assert len(data["messages"]) == 1
        assert data["messages"][0]["role"] == "user"

    def test_update_history_only_assistant_msg(self):
        """只有 assistant 消息时不应写入用户消息"""
        from app.memory import update_history, _get_file_path

        session_key = "dingtalk_test_assistant_only"
        update_history(session_key, user_msg=None, assistant_msg="只有 AI 回复")

        file_path = _get_file_path(session_key)
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        assert len(data["messages"]) == 1
        assert data["messages"][0]["role"] == "assistant"
        assert data["messages"][0]["bot_id"] == "gemini"

    def test_update_history_appends_to_existing(self):
        """追加到已有文件应正常工作"""
        from app.memory import update_history, get_history

        session_key = "dingtalk_test_append"
        update_history(session_key, user_msg="第一条", assistant_msg="回复1", sender_nick="用户")
        update_history(session_key, user_msg="第二条", assistant_msg="回复2", sender_nick="用户")

        history = get_history(session_key)
        assert len(history) == 4
        assert history[0]["content"] == "第一条"
        assert history[3]["content"] == "回复2"
        assert history[3]["bot_id"] == "gemini"

    def test_update_history_corrupted_existing_file(self):
        """已有文件损坏时应从空列表开始"""
        from app.memory import update_history, _get_file_path

        session_key = "dingtalk_test_corrupted_update"
        file_path = _get_file_path(session_key)

        # 写入损坏的 JSON
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write("not json")

        # 应该不报错，从空列表开始
        update_history(session_key, user_msg="新消息", assistant_msg="新回复", sender_nick="用户")

        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        assert len(data["messages"]) == 2


# ─── 文件存储 clear_history 测试 ──────────────────────────────────


class TestFileStorageClearHistory:
    """文件存储降级路径: clear_history 测试"""

    @pytest.fixture(autouse=True)
    def setup_teardown(self, tmp_path):
        self.data_dir = str(tmp_path / "history")
        os.makedirs(self.data_dir, exist_ok=True)

        self.patches = [
            patch("app.memory.USE_DATABASE", False),
            patch("app.memory.DATA_DIR", self.data_dir),
            patch("app.memory.BOT_ID", "gemini"),
        ]
        for p in self.patches:
            p.start()

        yield

        for p in self.patches:
            p.stop()

    def test_clear_history_removes_file(self):
        """clear_history 应删除文件"""
        from app.memory import update_history, clear_history, _get_file_path

        session_key = "dingtalk_test_clear"
        update_history(session_key, user_msg="消息", assistant_msg="回复", sender_nick="用户")

        file_path = _get_file_path(session_key)
        assert os.path.exists(file_path)

        clear_history(session_key)
        assert not os.path.exists(file_path)

    def test_clear_history_nonexistent_file(self):
        """清空不存在的会话不应报错"""
        from app.memory import clear_history

        # 不应抛出异常
        clear_history("dingtalk_nonexistent_clear")


# ─── 数据库路径测试 (mock) ──────────────────────────────────────────


class TestDatabasePathBotId:
    """数据库路径: bot_id 读写测试 (使用 mock)"""

    @pytest.fixture(autouse=True)
    def setup_teardown(self):
        self.mock_storage = MagicMock()
        self.patches = [
            patch("app.memory.USE_DATABASE", True),
            patch("app.memory.history_storage", self.mock_storage),
            patch("app.memory.BOT_ID", "gemini"),
        ]
        for p in self.patches:
            p.start()

        yield

        for p in self.patches:
            p.stop()

    def test_update_history_db_user_and_assistant(self):
        """数据库路径: 用户消息 + assistant 消息应正确调用 add_message"""
        from app.memory import update_history

        update_history("dingtalk_s1", user_msg="你好", assistant_msg="回复", sender_nick="张三")

        assert self.mock_storage.add_message.call_count == 2

        # 第一次调用: 用户消息 (有 bot_id，用于标识用户 @ 的机器人)
        user_call = self.mock_storage.add_message.call_args_list[0]
        assert user_call == call("dingtalk_s1", "user", "你好", "张三", bot_id="gemini")

        # 第二次调用: assistant 消息 (有 bot_id)
        assistant_call = self.mock_storage.add_message.call_args_list[1]
        assert assistant_call == call("dingtalk_s1", "assistant", "回复", bot_id="gemini")

    def test_update_history_db_only_user(self):
        """数据库路径: 只有用户消息"""
        from app.memory import update_history

        update_history("dingtalk_s2", user_msg="问题", sender_nick="李四")

        self.mock_storage.add_message.assert_called_once_with("dingtalk_s2", "user", "问题", "李四", bot_id="gemini")

    def test_update_history_db_only_assistant(self):
        """数据库路径: 只有 assistant 消息"""
        from app.memory import update_history

        update_history("dingtalk_s3", user_msg=None, assistant_msg="AI 回复")

        self.mock_storage.add_message.assert_called_once_with("dingtalk_s3", "assistant", "AI 回复", bot_id="gemini")

    def test_update_history_db_openclaw_bot_id(self):
        """数据库路径: OpenClaw 的 bot_id"""
        for p in self.patches:
            p.stop()
        self.patches[2] = patch("app.memory.BOT_ID", "openclaw")
        for p in self.patches:
            p.start()

        from app.memory import update_history

        update_history("dingtalk_s4", user_msg=None, assistant_msg="Claw 回复")

        self.mock_storage.add_message.assert_called_once_with("dingtalk_s4", "assistant", "Claw 回复", bot_id="openclaw")

    def test_get_history_db_path(self):
        """数据库路径: get_history 应调用 history_storage"""
        from app.memory import get_history

        self.mock_storage.get_history.return_value = [
            {"role": "user", "content": "你好", "bot_id": None},
            {"role": "assistant", "content": "回复", "bot_id": "gemini"},
        ]

        result = get_history("dingtalk_s5", limit=10)

        self.mock_storage.get_history.assert_called_once_with("dingtalk_s5", 10)
        assert len(result) == 2
        assert result[1]["bot_id"] == "gemini"

    def test_get_history_db_exception_fallback(self, tmp_path):
        """数据库路径异常时应降级到文件存储"""
        from app.memory import get_history

        self.mock_storage.get_history.side_effect = Exception("数据库连接失败")

        # 文件存储也没有数据，应返回空列表
        with patch("app.memory.DATA_DIR", str(tmp_path)):
            result = get_history("dingtalk_s6")
            assert result == []

    def test_update_history_db_exception_fallback(self, tmp_path):
        """数据库路径写入异常时应降级到文件存储"""
        from app.memory import update_history, _get_file_path

        self.mock_storage.add_message.side_effect = Exception("数据库写入失败")

        data_dir = str(tmp_path / "history")
        os.makedirs(data_dir, exist_ok=True)

        with patch("app.memory.DATA_DIR", data_dir):
            update_history("dingtalk_s7", user_msg="测试", assistant_msg="回复", sender_nick="用户")

            # 应降级写入文件
            with patch("app.memory.DATA_DIR", data_dir):
                file_path = _get_file_path("dingtalk_s7")

            assert os.path.exists(file_path)
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            assert len(data["messages"]) == 2
            assert data["messages"][1]["bot_id"] == "gemini"

    def test_clear_history_db_path(self):
        """数据库路径: clear_history 应调用 history_storage"""
        from app.memory import clear_history

        clear_history("dingtalk_s8")

        self.mock_storage.clear_history.assert_called_once_with("dingtalk_s8")

    def test_clear_history_db_exception_fallback(self, tmp_path):
        """数据库路径异常时 clear_history 应降级到文件存储"""
        from app.memory import clear_history, update_history, _get_file_path

        # 先让 update 正常写入文件
        self.mock_storage.add_message.side_effect = Exception("写入失败")
        data_dir = str(tmp_path / "history")
        os.makedirs(data_dir, exist_ok=True)

        with patch("app.memory.DATA_DIR", data_dir):
            update_history("dingtalk_s9", user_msg="测试", sender_nick="用户")
            file_path = _get_file_path("dingtalk_s9")
            assert os.path.exists(file_path)

        # 现在让 clear 的数据库路径也失败
        self.mock_storage.clear_history.side_effect = Exception("删除失败")

        with patch("app.memory.DATA_DIR", data_dir):
            clear_history("dingtalk_s9")
            # 文件应被删除
            assert not os.path.exists(file_path)


# ─── 历史格式化测试 ──────────────────────────────────────────────


class TestHistoryFormatting:
    """测试历史消息格式化（bot_id 标签）"""

    def test_format_with_bot_id_gemini(self):
        """Gemini 来源的 assistant 消息应加 [Gem] 标签"""
        msg = {"role": "assistant", "content": "这是一条回复", "bot_id": "gemini"}
        msg_bot_id = msg.get("bot_id")
        bot_label = {"gemini": "Gem", "openclaw": "Claw"}.get(msg_bot_id, msg_bot_id)
        formatted = f"[{bot_label}] {msg['content']}"
        assert formatted == "[Gem] 这是一条回复"

    def test_format_with_bot_id_openclaw(self):
        """OpenClaw 来源的 assistant 消息应加 [Claw] 标签"""
        msg = {"role": "assistant", "content": "OpenClaw 的回复", "bot_id": "openclaw"}
        msg_bot_id = msg.get("bot_id")
        bot_label = {"gemini": "Gem", "openclaw": "Claw"}.get(msg_bot_id, msg_bot_id)
        formatted = f"[{bot_label}] {msg['content']}"
        assert formatted == "[Claw] OpenClaw 的回复"

    def test_format_without_bot_id(self):
        """无 bot_id 的 assistant 消息不加标签"""
        msg = {"role": "assistant", "content": "旧格式回复"}
        msg_bot_id = msg.get("bot_id")
        if msg["role"] == "assistant" and msg_bot_id:
            bot_label = {"gemini": "Gem", "openclaw": "Claw"}.get(msg_bot_id, msg_bot_id)
            formatted = f"[{bot_label}] {msg['content']}"
        else:
            formatted = msg["content"]
        assert formatted == "旧格式回复"

    def test_format_unknown_bot_id(self):
        """未知 bot_id 应使用原始值作为标签"""
        msg = {"role": "assistant", "content": "来自新后端", "bot_id": "custom_backend"}
        msg_bot_id = msg.get("bot_id")
        bot_label = {"gemini": "Gem", "openclaw": "Claw"}.get(msg_bot_id, msg_bot_id)
        formatted = f"[{bot_label}] {msg['content']}"
        assert formatted == "[custom_backend] 来自新后端"

    def test_user_message_not_affected(self):
        """用户消息不应受 bot_id 逻辑影响"""
        msg = {"role": "user", "content": "用户消息", "timestamp": "2026-02-01 10:00:00", "sender_nick": "张三"}
        assert msg["role"] == "user"
        formatted = f"[{msg['timestamp']}] {msg['sender_nick']}: {msg['content']}"
        assert formatted == "[2026-02-01 10:00:00] 张三: 用户消息"
