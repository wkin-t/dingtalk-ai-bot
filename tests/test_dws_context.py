# -*- coding: utf-8 -*-
"""dws 只读群聊上下文与引用上下文单元测试。"""
import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("GEMINI_API_KEY", "test-dummy-key")


class TestReadonlySecurityGate:
    """硬编码只读白名单门禁测试：严禁任何发送、修改、撤回、下载等非只读指令通过。"""

    def test_allows_exact_readonly_commands(self):
        from app.dws_context import validate_readonly_dws_args

        assert validate_readonly_dws_args(
            ["auth", "status", "--readonly", "-f", "json"]
        ) == ["auth", "status", "--readonly", "-f", "json"]

        assert validate_readonly_dws_args(
            ["chat", "message", "list", "--conversation-id", "cid123==", "--limit", "40", "-f", "json"]
        ) == ["chat", "message", "list", "--conversation-id", "cid123==", "--limit", "40", "-f", "json"]

        assert validate_readonly_dws_args(
            [
                "chat",
                "message",
                "list",
                "--conversation-id",
                "cid123==",
                "--time",
                "2026-11-11 14:00:00",
                "--direction",
                "older",
                "--limit",
                "6",
                "-f",
                "json",
            ]
        )

        assert validate_readonly_dws_args(
            [
                "chat",
                "+messages-mget",
                "--msg-ids",
                "msg_1,msg_2",
                "--no-reactions",
                "--no-threads",
                "-f",
                "json",
            ]
        )

    @pytest.mark.parametrize(
        "forbidden_args",
        [
            [],
            ["chat", "message", "send", "--conversation-id", "cid123", "--text", "hi", "-f", "json"],
            ["chat", "+messages-send", "--conversation-id", "cid123", "--text", "hi", "-f", "json"],
            ["chat", "+messages-reply", "--msg-id", "m1", "--text", "hi", "-f", "json"],
            ["chat", "+messages-send-card", "--conversation-id", "cid123", "-f", "json"],
            ["chat", "+messages-recall", "--msg-id", "m1", "-f", "json"],
            ["chat", "group", "update", "--conversation-id", "cid123", "-f", "json"],
            ["file", "upload", "--path", "/etc/passwd", "-f", "json"],
            ["todo", "create", "--title", "x", "-f", "json"],
            ["auth", "logout", "-f", "json"],
            ["auth", "login", "--device", "-f", "json"],
            # +messages-mget 禁止附带资源下载或缺少 --no-reactions / --no-threads
            [
                "chat",
                "+messages-mget",
                "--msg-ids",
                "msg_1",
                "--download-resources",
                "--no-reactions",
                "--no-threads",
                "-f",
                "json",
            ],
            ["chat", "+messages-mget", "--msg-ids", "msg_1", "-f", "json"],
            # 禁止写文件参数与未知参数
            ["chat", "message", "list", "--conversation-id", "cid123", "-o", "/tmp/out.json", "-f", "json"],
            ["chat", "message", "list", "--conversation-id", "cid123", "--output=/tmp/out.json", "-f", "json"],
            ["chat", "message", "list", "--conversation-id", "cid123", "--direction", "invalid", "-f", "json"],
            ["chat", "message", "list", "--conversation-id", "cid123", "-f", "table"],
            ["chat", "message", "list", "--conversation-id", "cid123\nrm", "-f", "json"],
            ["chat", "message", "list", "extra_positional", "--conversation-id", "cid123", "-f", "json"],
        ],
    )
    def test_rejects_all_write_download_or_unapproved_args(self, forbidden_args):
        from app.dws_context import validate_readonly_dws_args

        with pytest.raises(PermissionError):
            validate_readonly_dws_args(forbidden_args)


class TestEnvelopeAndMessageParsing:
    """dws JSON 响应信封兼容与消息清洗测试。"""

    def test_unwraps_unified_and_legacy_envelopes(self):
        from app.dws_context import extract_messages_from_payload

        unified = {
            "ok": True,
            "outcome": "success",
            "data": {"messages": [{"messageId": "m1", "sender": "曾工", "text": "你好"}]},
        }
        legacy = {"messages": [{"messageId": "m2", "sender": "杰少", "text": "收到"}]}

        assert len(extract_messages_from_payload(unified)) == 1
        assert extract_messages_from_payload(unified)[0]["messageId"] == "m1"
        assert len(extract_messages_from_payload(legacy)) == 1
        assert extract_messages_from_payload(legacy)[0]["messageId"] == "m2"
        assert extract_messages_from_payload({"ok": False, "error": {"type": "auth"}}) == []

    def test_extract_clean_dws_item_prefers_cleaned_text_and_filters_noise(self):
        from app.dws_context import extract_clean_dws_item

        clean_msg = extract_clean_dws_item(
            {
                "messageId": "m_ok",
                "sender": "我司曾工",
                "senderId": "u_zeng",
                "createTime": "2026-11-11 14:10:00",
                "content": '{"raw_cipher":"xxxx"}',
                "text": "越脱离群众越好",
            }
        )
        assert clean_msg is not None
        assert clean_msg["display_text"] == "越脱离群众越好"
        assert not clean_msg["is_noise"]
        assert not clean_msg["is_bot_like"]

        cipher_msg = extract_clean_dws_item(
            {
                "messageId": "m_cipher",
                "sender": "Gemini小助手",
                "createTime": "2026-11-11 14:10:05",
                "text": "[加密消息，无法解码]",
            }
        )
        assert cipher_msg is not None
        assert cipher_msg["is_noise"] is True

        card_msg = extract_clean_dws_item(
            {
                "messageId": "m_card",
                "sender": "Gemini小助手",
                "createTime": "2026-11-11 14:10:06",
                "text": "👋 @我司曾工 \n\n送你一朵小滑稽",
            }
        )
        assert card_msg is not None
        assert card_msg["is_bot_like"] is True


class TestAuthBackoffAndSubprocessEnv:
    """未登录/缺少二进制时的负缓存退避与国内直连环境变量剥离测试。"""

    @pytest.mark.asyncio
    async def test_auth_failure_triggers_backoff_and_keychain_change_clears_backoff(self, tmp_path):
        import app.dws_context as dws_ctx

        dws_ctx.reset_dws_RuntimeState_for_tests()
        keychain_dir = tmp_path / "keychain"
        keychain_dir.mkdir()

        auth_err_payload = json.dumps(
            {
                "ok": False,
                "outcome": "error",
                "error": {"type": "auth", "message": "not logged in", "exit_code": 2},
            }
        ).encode("utf-8")

        ok_payload = json.dumps(
            {
                "ok": True,
                "outcome": "success",
                "data": {"messages": [{"messageId": "m1", "sender": "杰少", "text": "登录成功啦", "createTime": "2026-11-11 14:20:00"}]},
            }
        ).encode("utf-8")

        spawn_envs = []
        call_count = 0

        async def fake_create_subprocess_exec(*cmd, **kwargs):
            nonlocal call_count
            call_count += 1
            spawn_envs.append(kwargs.get("env", {}))
            proc = AsyncMock()
            if call_count == 1:
                proc.returncode = 2
                proc.communicate = AsyncMock(return_value=(auth_err_payload, b""))
            else:
                proc.returncode = 0
                proc.communicate = AsyncMock(return_value=(ok_payload, b""))
            return proc

        with patch.dict(
            os.environ,
            {
                "HTTP_PROXY": "http://127.0.0.1:1087",
                "HTTPS_PROXY": "http://127.0.0.1:1087",
                "DWS_KEYCHAIN_DIR": str(keychain_dir),
            },
            clear=False,
        ), patch("asyncio.create_subprocess_exec", side_effect=fake_create_subprocess_exec):
            # 第 1 次：返回未登录，进入退避
            res1 = await dws_ctx.run_dws_readonly_json(
                ["chat", "message", "list", "--conversation-id", "cid1", "--limit", "10", "-f", "json"]
            )
            assert res1 is None
            assert call_count == 1
            assert "HTTP_PROXY" not in spawn_envs[0]
            assert "HTTPS_PROXY" not in spawn_envs[0]

            # 第 2 次：keychain 目录未变，命中退避，不拉起子进程
            res2 = await dws_ctx.run_dws_readonly_json(
                ["chat", "message", "list", "--conversation-id", "cid1", "--limit", "10", "-f", "json"]
            )
            assert res2 is None
            assert call_count == 1

            # 模拟管理员完成 dws auth login --device 写入了凭据文件：退避应立即自动解除
            (keychain_dir / "auth-token.enc").write_bytes(b"encrypted-token")
            res3 = await dws_ctx.run_dws_readonly_json(
                ["chat", "message", "list", "--conversation-id", "cid1", "--limit", "10", "-f", "json"]
            )
            assert res3 is not None
            assert call_count == 2


class TestGroupHistoryMerge:
    """群聊普通消息与 Redis/MySQL 机器人对话历史的合并去重测试。"""

    def test_merges_non_at_group_messages_and_deduplicates_at_turns(self):
        from app.dws_context import extract_clean_dws_item, merge_dws_group_messages_with_history

        raw_dws = [
            {
                "messageId": "m1",
                "sender": "杰少",
                "senderId": "u_jie",
                "createTime": "2026-11-11 14:10:05",
                "text": "越脱离群众越好，汇报要有高度",
            },
            {
                "messageId": "m2",
                "sender": "我司曾工",
                "senderId": "u_zeng",
                "createTime": "2026-11-11 14:10:18",
                "text": "@Gemini小助手 来张符合华少气质的图",
            },
            {
                "messageId": "m3",
                "sender": "Gemini小助手",
                "senderId": "bot_gem",
                "createTime": "2026-11-11 14:10:19",
                "text": "Thinking...",
            },
            {
                "messageId": "m4",
                "sender": "杰少",
                "senderId": "u_jie",
                "createTime": "2026-11-11 14:12:00",
                "text": "哈哈哈华少其实就是曾工本尊！",
            },
            {
                "messageId": "m_curr",
                "sender": "我司曾工",
                "senderId": "u_zeng",
                "createTime": "2026-11-11 14:15:00",
                "text": "@Gemini小助手 ?",
            },
        ]
        dws_items = [x for item in raw_dws if (x := extract_clean_dws_item(item)) is not None]

        # 机器人历史是在流式结束（14:10:28）才落库的，且去掉了 @Gemini小助手 前缀
        history_messages = [
            {
                "role": "user",
                "content": "来张符合华少气质的图",
                "timestamp": "2026-11-11 14:10:28",
                "sender_nick": "我司曾工",
            },
            {
                "role": "assistant",
                "content": "给你安排一张滑稽图 [[bqb:滑稽]]",
                "timestamp": "2026-11-11 14:10:28",
                "bot_id": "gemini",
            },
        ]

        merged = merge_dws_group_messages_with_history(
            dws_items=dws_items,
            history_messages=history_messages,
            current_message_ids={"m_curr"},
            cutoff_at=None,
            max_length=50,
            chatbot_user_id="bot_gem",
        )

        # 预期顺序：m1(杰少) -> m2(曾工 @bot 去重后仅保留1条) -> assistant(机器人回复) -> m4(杰少非@消息)，且不含当前 m_curr 与 Thinking...
        assert len(merged) == 4
        assert merged[0]["role"] == "user"
        assert "越脱离群众越好" in merged[0]["content"]
        assert merged[0]["sender_nick"] == "杰少"

        assert merged[1]["role"] == "user"
        assert "来张符合华少气质的图" in merged[1]["content"]
        assert merged[1]["timestamp"] == "2026-11-11 14:10:18"

        assert merged[2]["role"] == "assistant"
        assert merged[2]["bot_id"] == "gemini"
        assert "给你安排一张滑稽图" in merged[2]["content"]

        assert merged[3]["role"] == "user"
        assert "华少其实就是曾工本尊" in merged[3]["content"]

    def test_respects_clear_cutoff(self):
        from app.dws_context import extract_clean_dws_item, merge_dws_group_messages_with_history

        dws_items = [
            extract_clean_dws_item(
                {
                    "messageId": "old_1",
                    "sender": "杰少",
                    "createTime": "2026-11-11 13:00:00",
                    "text": "清空前的旧消息",
                }
            ),
            extract_clean_dws_item(
                {
                    "messageId": "new_1",
                    "sender": "杰少",
                    "createTime": "2026-11-11 13:05:00",
                    "text": "清空后的新消息",
                }
            ),
        ]
        merged = merge_dws_group_messages_with_history(
            dws_items=[x for x in dws_items if x],
            history_messages=[],
            current_message_ids=set(),
            cutoff_at="2026-11-11 13:02:00",
            max_length=50,
        )
        assert len(merged) == 1
        assert merged[0]["content"] == "清空后的新消息"


class TestQuoteContextResolution:
    """引用消息及引用处前后上下文提取测试（含窗口内零额外调用、历史跨窗口查询、引用机器人卡片恢复）。"""

    @pytest.mark.asyncio
    async def test_in_window_quote_extracts_neighbors_without_extra_rpc(self):
        from app.dws_context import fetch_dws_context_bundle, reset_dws_RuntimeState_for_tests

        reset_dws_RuntimeState_for_tests()
        recent_payload = {
            "ok": True,
            "outcome": "success",
            "data": {
                "messages": [
                    {"messageId": "m1", "sender": "曾工", "text": "季度汇报大家怎么写？", "createTime": "2026-11-11 14:00:01"},
                    {"messageId": "m2", "sender": "杰少", "text": "越脱离群众越好", "createTime": "2026-11-11 14:00:10"},
                    {"messageId": "m3", "sender": "阿强", "text": "这话太狠了哈哈", "createTime": "2026-11-11 14:00:20"},
                    {
                        "messageId": "m_curr",
                        "sender": "曾工",
                        "text": "@Gemini小助手 ?",
                        "createTime": "2026-11-11 14:05:00",
                        "quotedMessage": {
                            "messageId": "m2",
                            "sender": "杰少",
                            "text": "越脱离群众越好",
                            "createTime": "2026-11-11 14:00:10",
                        },
                    },
                ]
            },
        }

        calls = []

        async def fake_run_json(args, **_kwargs):
            calls.append(list(args))
            return recent_payload

        incoming = SimpleNamespace(
            message_id="m_curr",
            conversation_id="cid_group_1",
            conversation_type="2",
            chatbot_user_id="bot_gem",
            text=SimpleNamespace(content="?", extensions={}),
            extensions={},
        )

        with patch("app.dws_context.run_dws_readonly_json", side_effect=fake_run_json):
            bundle = await fetch_dws_context_bundle(
                conversation_id="cid_group_1",
                is_group=True,
                incoming_messages=[incoming],
                current_message_ids=["m_curr"],
                raw_user_content="?",
                history_messages=[],
                cutoff_at=None,
            )

        # 窗口内已包含 m2 及前后邻居，只需 1 次 list 调用
        assert len(calls) == 1
        assert bundle.quote_info is not None
        assert bundle.quote_info.sender == "杰少"
        assert "越脱离群众越好" in bundle.quote_prompt_block
        assert "季度汇报大家怎么写" in bundle.quote_prompt_block
        assert "这话太狠了哈哈" in bundle.quote_prompt_block
        assert "越脱离群众越好" in bundle.quote_router_text
        assert bundle.quote_history_prefix.startswith("[引用 杰少:")

    @pytest.mark.asyncio
    async def test_out_of_window_historical_quote_fetches_older_and_newer_neighbors(self):
        from app.dws_context import fetch_dws_context_bundle, reset_dws_RuntimeState_for_tests

        reset_dws_RuntimeState_for_tests()
        # 最近窗口已被 5 条下午的新消息占满（假设 context_limit=5），被引用的早上消息 m_old_target 不在最近窗口内
        recent_messages = [
            {
                "messageId": f"m_recent_{i}",
                "sender": "群友",
                "text": f"下午闲聊 {i}",
                "createTime": f"2026-11-11 14:10:0{i}",
            }
            for i in range(1, 5)
        ]
        recent_messages.append(
            {
                "messageId": "m_curr",
                "sender": "曾工",
                "text": "@Gemini小助手 这个方案现在还能用吗？",
                "createTime": "2026-11-11 14:15:00",
                "quotedMessage": {
                    "messageId": "m_old_target",
                    "sender": "架构师老王",
                    "text": "我们先把 Redis TTL 设为 48 小时隔离 client_id",
                    "createTime": "2026-11-11 09:30:00",
                },
            }
        )

        older_around_quote = {
            "ok": True,
            "data": {
                "messages": [
                    {"messageId": "m_old_1", "sender": "曾工", "text": "media_id 跨机器人会串吗？", "createTime": "2026-11-11 09:29:10"},
                    {"messageId": "m_old_target", "sender": "架构师老王", "text": "我们先把 Redis TTL 设为 48 小时隔离 client_id", "createTime": "2026-11-11 09:30:00"},
                ]
            },
        }
        newer_around_quote = {
            "ok": True,
            "data": {
                "messages": [
                    {"messageId": "m_old_target", "sender": "架构师老王", "text": "我们先把 Redis TTL 设为 48 小时隔离 client_id", "createTime": "2026-11-11 09:30:00"},
                    {"messageId": "m_old_2", "sender": "杰少", "text": "赞同，顺便加上本地磁盘兜底", "createTime": "2026-11-11 09:30:45"},
                ]
            },
        }

        calls = []

        async def fake_run_json(args, **_kwargs):
            calls.append(list(args))
            if "--direction" in args:
                idx = args.index("--direction")
                direction = args[idx + 1]
                return older_around_quote if direction == "older" else newer_around_quote
            return {"ok": True, "data": {"messages": recent_messages}}

        incoming = SimpleNamespace(
            message_id="m_curr",
            conversation_id="cid_group_1",
            conversation_type="2",
            chatbot_user_id="bot_gem",
            text=SimpleNamespace(content="这个方案现在还能用吗？", extensions={}),
            extensions={},
        )

        with patch("app.dws_context.run_dws_readonly_json", side_effect=fake_run_json):
            bundle = await fetch_dws_context_bundle(
                conversation_id="cid_group_1",
                is_group=True,
                incoming_messages=[incoming],
                current_message_ids=["m_curr"],
                raw_user_content="这个方案现在还能用吗？",
                history_messages=[],
                cutoff_at=None,
                context_limit=5,
                quote_before=3,
                quote_after=3,
            )

        directions = [c[c.index("--direction") + 1] for c in calls if "--direction" in c]
        assert sorted(directions) == ["newer", "older"]
        assert "media_id 跨机器人会串吗？" in bundle.quote_prompt_block
        assert "我们先把 Redis TTL 设为 48 小时隔离 client_id" in bundle.quote_prompt_block
        assert "赞同，顺便加上本地磁盘兜底" in bundle.quote_prompt_block

    @pytest.mark.asyncio
    async def test_quoting_bot_ai_card_recovers_real_assistant_reply_from_history(self):
        from app.dws_context import fetch_dws_context_bundle, reset_dws_RuntimeState_for_tests

        reset_dws_RuntimeState_for_tests()
        # 用户引用了机器人之前发的互动卡片，Stream repliedMsg 里只有 Thinking... 占位，而本地 history 有完整回复
        incoming = SimpleNamespace(
            message_id="m_curr_q",
            conversation_id="cid_group_1",
            conversation_type="2",
            chatbot_user_id="bot_gem",
            text=SimpleNamespace(
                content="?",
                extensions={
                    "isReplyMsg": True,
                    "repliedMsg": {
                        "msgId": "m_bot_card_1",
                        "msgType": "interactiveCard",
                        "senderNick": "Gemini小助手",
                        "createdAt": 1794377650000,  # 2026-11-11 14:14:10 UTC+8
                        "content": {"text": "Thinking..."},
                    },
                },
            ),
            extensions={},
        )
        history_messages = [
            {
                "role": "user",
                "content": "给华少配张表情包",
                "timestamp": "2026-11-11 14:14:22",
                "sender_nick": "杰少",
            },
            {
                "role": "assistant",
                "content": "送华少一朵贱兮兮的小滑稽 [[bqb:滑稽]]",
                "timestamp": "2026-11-11 14:14:22",
                "bot_id": "gemini",
            },
        ]

        # 即使 dws 暂未登录（返回 None），也应能用 Stream repliedMsg + history 恢复出被引用卡片的真实文本！
        with patch("app.dws_context.run_dws_readonly_json", new_callable=AsyncMock, return_value=None):
            bundle = await fetch_dws_context_bundle(
                conversation_id="cid_group_1",
                is_group=True,
                incoming_messages=[incoming],
                current_message_ids=["m_curr_q"],
                raw_user_content="?",
                history_messages=history_messages,
                cutoff_at=None,
            )

        assert bundle.quote_info is not None
        assert "送华少一朵贱兮兮的小滑稽" in bundle.quote_info.text
        assert "送华少一朵贱兮兮的小滑稽" in bundle.quote_prompt_block
        assert "送华少一朵贱兮兮的小滑稽" in bundle.quote_router_text

    @pytest.mark.asyncio
    async def test_matches_incoming_message_even_when_stream_msgid_differs_from_dws_id(self):
        """当 Stream msgId 与 dws messageId 编码空间不同时，仍能按发送者+时间+文本识别当前消息以提取引用并避免历史重复。"""
        from app.dws_context import fetch_dws_context_bundle, reset_dws_RuntimeState_for_tests

        reset_dws_RuntimeState_for_tests()
        recent_payload = {
            "ok": True,
            "data": {
                "messages": [
                    {"messageId": "10001", "sender": "杰少", "text": "今晚发版窗口定在九点", "createTime": "2026-11-11 14:14:00"},
                    {
                        "messageId": "10002",
                        "sender": "我司曾工",
                        "text": "@Gemini小助手 ?",
                        "createTime": "2026-11-11 14:15:00",
                        "quotedMessage": {
                            "messageId": "10001",
                            "sender": "杰少",
                            "text": "今晚发版窗口定在九点",
                            "createTime": "2026-11-11 14:14:00",
                        },
                    },
                ]
            },
        }
        incoming = SimpleNamespace(
            message_id="msgStreamOpenId_DifferentFrom10002==",
            sender_nick="我司曾工",
            create_at=1794377700000,  # 2026-11-11 14:15:00 UTC+8
            conversation_id="cid_group_1",
            conversation_type="2",
            chatbot_user_id="bot_gem",
            text=SimpleNamespace(content="?", extensions={}),
            extensions={},
        )

        with patch("app.dws_context.run_dws_readonly_json", new_callable=AsyncMock, return_value=recent_payload):
            bundle = await fetch_dws_context_bundle(
                conversation_id="cid_group_1",
                is_group=True,
                incoming_messages=[incoming],
                current_message_ids=["msgStreamOpenId_DifferentFrom10002=="],
                raw_user_content="?",
                history_messages=[],
                cutoff_at=None,
            )

        # 10002 被正确识别为当前消息：既提取出了 quotedMessage，又不会混入 merged_history 造成重复
        assert bundle.quote_info is not None
        assert bundle.quote_info.text == "今晚发版窗口定在九点"
        assert len(bundle.merged_history) == 1
        assert bundle.merged_history[0]["content"] == "今晚发版窗口定在九点"


class TestEnterpriseCorpValidation:
    """dws 登录企业与机器人所在企业一致性校验测试（不一致必须阻断读取并显式报错）。"""

    @pytest.mark.asyncio
    async def test_corp_mismatch_blocks_chat_reads_and_returns_error(self):
        from app.dws_context import fetch_dws_context_bundle, reset_dws_RuntimeState_for_tests

        reset_dws_RuntimeState_for_tests()
        calls = []

        async def fake_run_json(args, **_kwargs):
            calls.append(list(args))
            if list(args[:2]) == ["auth", "status"]:
                return {
                    "success": True,
                    "authenticated": True,
                    "token_valid": True,
                    "corp_id": "ding_wrong_company_888",
                    "corp_name": "其他外部公司",
                    "user_name": "曾健华",
                }
            raise AssertionError(f"企业不一致时严禁调用任何 chat 读取命令: {args}")

        incoming = SimpleNamespace(
            message_id="m_curr",
            conversation_id="cid_group_1",
            conversation_type="2",
            chatbot_user_id="bot_gem",
            chatbot_corp_id="ding42b8b39e5adac00a4ac5d6980864d335",
            text=SimpleNamespace(content="大家好", extensions={}),
            extensions={},
        )

        with patch("app.dws_context.run_dws_readonly_json", side_effect=fake_run_json):
            bundle = await fetch_dws_context_bundle(
                conversation_id="cid_group_1",
                is_group=True,
                incoming_messages=[incoming],
                current_message_ids=["m_curr"],
                raw_user_content="大家好",
                history_messages=[],
                cutoff_at=None,
            )

        assert calls == [["auth", "status", "--readonly", "-f", "json"]]
        assert bundle.dws_available is False
        assert bundle.corp_mismatch_error
        assert "不一致" in bundle.corp_mismatch_error
        assert "其他外部公司" in bundle.corp_mismatch_error
        assert "ding_wrong_company_888" in bundle.corp_mismatch_error
        assert "ding42b8b39e5adac00a4ac5d6980864d335" in bundle.corp_mismatch_error

    @pytest.mark.asyncio
    async def test_corp_match_caches_identity_and_invalidates_when_profile_changes(self, tmp_path):
        import app.dws_context as dws_ctx

        dws_ctx.reset_dws_RuntimeState_for_tests()
        cfg_dir = tmp_path / "config"
        key_dir = tmp_path / "keychain" / "dws-cli"
        cfg_dir.mkdir(parents=True)
        key_dir.mkdir(parents=True)
        profiles_file = cfg_dir / "profiles.json"
        profiles_file.write_text('{"currentProfile":"wrong"}', encoding="utf-8")
        (key_dir / "session.enc").write_bytes(b"v1")

        current_corp = {"id": "ding_wrong_1", "name": "误选企业"}
        auth_calls = 0

        async def fake_run_json(args, **_kwargs):
            nonlocal auth_calls
            if list(args[:2]) == ["auth", "status"]:
                auth_calls += 1
                return {
                    "authenticated": True,
                    "token_valid": True,
                    "corp_id": current_corp["id"],
                    "corp_name": current_corp["name"],
                }
            return {
                "ok": True,
                "data": {
                    "messages": [
                        {"messageId": "m1", "sender": "杰少", "text": "同企业正常读取", "createTime": "2026-11-11 15:00:00"}
                    ]
                },
            }

        incoming = SimpleNamespace(
            message_id="m_curr",
            conversation_id="cid_group_1",
            conversation_type="2",
            chatbot_user_id="bot_gem",
            chatbot_corp_id="ding42b8b39e5adac00a4ac5d6980864d335",
            text=SimpleNamespace(content="测试", extensions={}),
            extensions={},
        )

        with patch.dict(
            os.environ,
            {
                "DWS_CONFIG_DIR": str(cfg_dir),
                "DWS_KEYCHAIN_DIR": str(tmp_path / "keychain"),
            },
            clear=False,
        ), patch("app.dws_context.run_dws_readonly_json", side_effect=fake_run_json):
            # 1. 首次为错误企业：报错阻断
            b1 = await dws_ctx.fetch_dws_context_bundle(
                conversation_id="cid_group_1",
                is_group=True,
                incoming_messages=[incoming],
                current_message_ids=["m_curr"],
                raw_user_content="测试",
                history_messages=[],
            )
            assert b1.corp_mismatch_error
            assert auth_calls == 1

            # 2. 管理员重新登录切换到正确企业（profiles.json 文件变化）：无需重启，立即解除报错并正常拉取
            current_corp["id"] = "ding42b8b39e5adac00a4ac5d6980864d335"
            current_corp["name"] = "Vibe Coding 联盟"
            profiles_file.write_text('{"currentProfile":"ding42b8b39e5adac00a4ac5d6980864d335:manager1829"}', encoding="utf-8")

            b2 = await dws_ctx.fetch_dws_context_bundle(
                conversation_id="cid_group_1",
                is_group=True,
                incoming_messages=[incoming],
                current_message_ids=["m_curr"],
                raw_user_content="测试",
                history_messages=[],
            )
            assert b2.corp_mismatch_error == ""
            assert b2.dws_available is True
            assert len(b2.merged_history) == 1
            assert auth_calls == 2

            # 3. 凭据未变时再次请求：命中进程内身份缓存，不再重复拉起 auth status 子进程
            b3 = await dws_ctx.fetch_dws_context_bundle(
                conversation_id="cid_group_1",
                is_group=True,
                incoming_messages=[incoming],
                current_message_ids=["m_curr"],
                raw_user_content="测试",
                history_messages=[],
            )
            assert b3.corp_mismatch_error == ""
            assert b3.dws_available is True
            assert auth_calls == 2

    @pytest.mark.asyncio
    async def test_expected_corp_id_env_also_enforces_match(self):
        import app.dws_context as dws_ctx

        dws_ctx.reset_dws_RuntimeState_for_tests()

        async def fake_run_json(args, **_kwargs):
            if list(args[:2]) == ["auth", "status"]:
                return {
                    "authenticated": True,
                    "token_valid": True,
                    "corp_id": "ding_other_corp",
                    "corp_name": "别的企业",
                }
            raise AssertionError("不应调用 chat")

        with patch.object(dws_ctx.cfg, "DWS_EXPECTED_CORP_ID", "ding42b8b39e5adac00a4ac5d6980864d335", create=True), \
             patch("app.dws_context.run_dws_readonly_json", side_effect=fake_run_json):
            bundle = await dws_ctx.fetch_dws_context_bundle(
                conversation_id="cid_group_1",
                is_group=True,
                incoming_messages=[],
                current_message_ids=[],
                raw_user_content="测试",
                history_messages=[],
            )

        assert bundle.corp_mismatch_error
        assert "ding_other_corp" in bundle.corp_mismatch_error
        assert "ding42b8b39e5adac00a4ac5d6980864d335" in bundle.corp_mismatch_error


