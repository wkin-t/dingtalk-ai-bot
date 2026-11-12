# -*- coding: utf-8 -*-
"""dws 只读群聊上下文与引用上下文单元测试。"""
import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("GEMINI_API_KEY", "test-dummy-key")


@pytest.mark.asyncio
async def test_dws_only_history_includes_bots_and_media_without_local_history():
    from app.dws_context import fetch_dws_context_bundle, DwsCorpCheckResult
    from app.dws_files import QuotedMediaAndDocsResult
    rows = [
        {"messageId": "p", "sender": "同事", "text": "[图片消息](mediaId=abc)", "createTime": "2026-11-12 10:00:00", "resources": [{"resourceType": "image", "resourceIdType": "mediaId", "resourceId": "abc"}]},
        {"messageId": "b", "sender": "小克", "senderId": "bot", "senderType": "robot", "text": "之前的回答", "createTime": "2026-11-12 10:00:01"},
    ]
    with patch("app.dws_context.check_dws_corp_access", new_callable=AsyncMock, return_value=DwsCorpCheckResult(allowed=True)), \
         patch("app.dws_context.run_dws_readonly_json", new_callable=AsyncMock, return_value={"messages": rows}), \
         patch("app.dws_context.fetch_quoted_media_and_docs", new_callable=AsyncMock, return_value=QuotedMediaAndDocsResult(images=[b"png"])) as media:
        result = await fetch_dws_context_bundle(conversation_id="conv", is_group=False, incoming_messages=[SimpleNamespace(chatbot_user_id="bot")], current_message_ids=[], raw_user_content="怎么看", history_messages=[{"role": "user", "content": "旧库残留"}], dws_only=True)
    assert result.dws_available
    assert all("旧库残留" not in m["content"] for m in result.merged_history)
    assert any(m["role"] == "assistant" and m["content"] == "之前的回答" for m in result.merged_history)
    assert result.context_images == [b"png"]
    assert media.await_args.kwargs["quotes"][0].message_id == "p"


@pytest.mark.asyncio
async def test_dws_only_failure_never_falls_back_to_local_history():
    from app.dws_context import fetch_dws_context_bundle, DwsCorpCheckResult
    with patch("app.dws_context.check_dws_corp_access", new_callable=AsyncMock, return_value=DwsCorpCheckResult(allowed=False)):
        result = await fetch_dws_context_bundle(conversation_id="conv", is_group=True, incoming_messages=[], current_message_ids=[], raw_user_content="?", history_messages=[{"role": "user", "content": "旧库"}], dws_only=True)
    assert result.merged_history == []
    assert result.context_error


def test_members_mentions_only_resolve_current_group_ids():
    from app.dws_members import parse_members, resolve_mentions
    members, complete = parse_members({"complete": True, "users": [
        {"openDingtalkId": "real", "name": "甲"}, {"name": "无ID"}]})
    assert complete and len(members) == 1
    text, targets = resolve_mentions("请 [[at:1]] 看一下 [[at:999]]", members)
    assert text == "请 @甲 看一下 "
    assert targets == ["real"]


def test_parse_members_prefers_group_nickname_then_personal_nickname():
    from app.dws_members import parse_members

    members, _ = parse_members({"complete": True, "users": [
        {"openDingtalkId": "member-1", "name": "陈一", "memberNick": "我司陈工"},
        {"openDingtalkId": "member-2", "name": "李四", "memberNick": "小李", "memberGroupNick": "值班李工"},
    ]})
    assert [member["name"] for member in members] == ["我司陈工", "值班李工"]


@pytest.mark.asyncio
async def test_mentions_send_is_scoped_to_bot_and_current_group():
    from app.dws_members import send_mentions
    from unittest.mock import MagicMock
    proc = MagicMock(returncode=0)
    proc.communicate = AsyncMock(return_value=(b'{"success":true}', b''))
    with patch("app.dws_members.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=proc) as spawn:
        await send_mentions("group-current", ["member"], [{"id": "member", "name": "甲"}], content="这是本次回复的实际内容。")
        args = spawn.await_args.args
        assert args[args.index("--as") + 1] == "bot"
        assert args[args.index("--group") + 1] == "group-current"
        assert args[args.index("--at-open-dingtalk-ids") + 1] == "member"
        assert "--at-all" not in args
        assert args[args.index("--markdown") + 1] == "-"
        assert "--body-stdin" in args
        body = proc.communicate.await_args.kwargs["input"].decode()
        assert "@member" in body
        assert "这是本次回复的实际内容。" in body
        assert "请查看上方回复" not in body
    with patch("app.dws_members.asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn:
        with pytest.raises(ValueError):
            await send_mentions("group-current", ["outsider"], [{"id": "member", "name": "甲"}], content="实际回复")
        spawn.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [" ", "正" * 90000], ids=["empty", "oversized"])
async def test_mentions_rejects_empty_or_oversized_body_before_sending(content):
    from app.dws_members import send_mentions

    with patch("app.dws_members.asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn:
        with pytest.raises(ValueError):
            await send_mentions("group", ["member"], [{"id": "member", "name": "甲"}], content=content)
        spawn.assert_not_awaited()


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

        assert validate_readonly_dws_args(
            ["doc", "info", "--node", "QBnd5ExVEv9jBrqgFaplzLB2JyeZqMmz", "-f", "json"]
        )
        assert validate_readonly_dws_args(
            ["doc", "read", "--node", "QBnd5ExVEv9jBrqgFaplzLB2JyeZqMmz", "-f", "json"]
        )
        assert validate_readonly_dws_args(
            ["drive", "download", "--node", "QBnd5ExVEv9jBrqgFaplzLB2JyeZqMmz", "--url-only", "-f", "json"]
        )

        import tempfile

        with tempfile.TemporaryDirectory(prefix="dws_ro_media_") as ro_tmp:
            assert validate_readonly_dws_args(
                [
                    "chat",
                    "message",
                    "download-media",
                    "--conversation-id",
                    "cid123==",
                    "--message-id",
                    "msg_1",
                    "--resource-id",
                    "$iwEl_test",
                    "--type",
                    "mediaId",
                    "--output",
                    ro_tmp,
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
            # 文档/网盘只读红线：禁止写操作、禁止缺 --url-only、禁止携带 -o/--output 落盘
            ["doc", "create", "--title", "hack", "-f", "json"],
            ["doc", "write", "--node", "n1", "--content", "x", "-f", "json"],
            ["doc", "delete", "--node", "n1", "-f", "json"],
            ["doc", "read", "--node", "n1", "-o", "/tmp/doc.md", "-f", "json"],
            ["drive", "download", "--node", "n1", "-f", "json"],
            ["drive", "download", "--node", "n1", "--url-only", "-o", "/tmp/a.docx", "-f", "json"],
            ["drive", "upload", "--file", "/etc/hosts", "-f", "json"],
            # download-media 仅允许写入受控临时目录且仅限 mediaId
            [
                "chat",
                "message",
                "download-media",
                "--conversation-id",
                "cid123",
                "--message-id",
                "m1",
                "--resource-id",
                "r1",
                "--type",
                "mediaId",
                "--output",
                "/tmp/not_allowed_dir",
                "-f",
                "json",
            ],
            [
                "chat",
                "message",
                "download-media",
                "--conversation-id",
                "cid123",
                "--message-id",
                "m1",
                "--resource-id",
                "r1",
                "--type",
                "fileId",
                "--output",
                "/tmp/dws_ro_media_fake",
                "-f",
                "json",
            ],
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
            if list(args[:2]) == ["auth", "status"]:
                return {
                    "authenticated": True,
                    "token_valid": True,
                    "corp_id": "ding42b8b39e5adac00a4ac5d6980864d335",
                    "corp_name": "Vibe Coding 联盟",
                }
            calls.append(list(args))
            return recent_payload

        incoming = SimpleNamespace(
            message_id="m_curr",
            conversation_id="cid_group_1",
            conversation_type="2",
            chatbot_user_id="bot_gem",
            chatbot_corp_id="ding42b8b39e5adac00a4ac5d6980864d335",
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
            if list(args[:2]) == ["auth", "status"]:
                return {
                    "authenticated": True,
                    "token_valid": True,
                    "corp_id": "ding42b8b39e5adac00a4ac5d6980864d335",
                    "corp_name": "Vibe Coding 联盟",
                }
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
            chatbot_corp_id="ding42b8b39e5adac00a4ac5d6980864d335",
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
            chatbot_corp_id="ding42b8b39e5adac00a4ac5d6980864d335",
            text=SimpleNamespace(content="?", extensions={}),
            extensions={},
        )

        async def fake_run_json_hetero(args, **_kwargs):
            if list(args[:2]) == ["auth", "status"]:
                return {
                    "authenticated": True,
                    "token_valid": True,
                    "corp_id": "ding42b8b39e5adac00a4ac5d6980864d335",
                    "corp_name": "Vibe Coding 联盟",
                }
            return recent_payload

        with patch("app.dws_context.run_dws_readonly_json", side_effect=fake_run_json_hetero):
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

    @pytest.mark.asyncio
    async def test_fail_closed_when_auth_status_returns_none_or_missing_expected_corp(self):
        """Standards #1 & Spec #4：auth status 超时/异常或缺少机器人归属企业 ID 时必须 fail-closed，严禁继续拉取群消息。"""
        import app.dws_context as dws_ctx

        dws_ctx.reset_dws_RuntimeState_for_tests()
        chat_called = False

        async def fake_run_none(args, **_kwargs):
            nonlocal chat_called
            if list(args[:2]) == ["auth", "status"]:
                return None
            chat_called = True
            return {"ok": True, "data": {"messages": []}}

        incoming_with_corp = SimpleNamespace(
            message_id="m_curr",
            conversation_id="cid_group_1",
            conversation_type="2",
            chatbot_user_id="bot_gem",
            chatbot_corp_id="ding42b8b39e5adac00a4ac5d6980864d335",
            text=SimpleNamespace(content="测试", extensions={}),
            extensions={},
        )

        with patch("app.dws_context.run_dws_readonly_json", side_effect=fake_run_none):
            b_timeout = await dws_ctx.fetch_dws_context_bundle(
                conversation_id="cid_group_1",
                is_group=True,
                incoming_messages=[incoming_with_corp],
                current_message_ids=["m_curr"],
                raw_user_content="测试",
                history_messages=[],
            )
        assert chat_called is False
        assert b_timeout.dws_available is False

        # 回调缺失 chatbot_corp_id 且未配 DWS_EXPECTED_CORP_ID：同样必须 fail-closed
        dws_ctx.reset_dws_RuntimeState_for_tests()
        any_called = False

        async def fake_should_not_call(args, **_kwargs):
            nonlocal any_called
            any_called = True
            return None

        incoming_no_corp = SimpleNamespace(
            message_id="m_curr",
            conversation_id="cid_group_1",
            conversation_type="2",
            chatbot_user_id="bot_gem",
            text=SimpleNamespace(content="测试", extensions={}),
            extensions={},
        )
        with patch.object(dws_ctx.cfg, "DWS_EXPECTED_CORP_ID", ""), \
             patch("app.dws_context.run_dws_readonly_json", side_effect=fake_should_not_call):
            b_no_corp = await dws_ctx.fetch_dws_context_bundle(
                conversation_id="cid_group_1",
                is_group=True,
                incoming_messages=[incoming_no_corp],
                current_message_ids=["m_curr"],
                raw_user_content="测试",
                history_messages=[],
            )
        assert any_called is False
        assert b_no_corp.dws_available is False

    @pytest.mark.asyncio
    async def test_corp_mismatch_persists_across_cached_calls_within_ttl(self):
        """Spec #4：缓存有效期内重复触发企业不一致时，每次都必须返回显式报错而非静默吞掉。"""
        import app.dws_context as dws_ctx

        dws_ctx.reset_dws_RuntimeState_for_tests()

        async def fake_run_json(args, **_kwargs):
            if list(args[:2]) == ["auth", "status"]:
                return {
                    "authenticated": True,
                    "token_valid": True,
                    "corp_id": "ding_wrong_999",
                    "corp_name": "串号公司",
                }
            raise AssertionError("不应调用 chat")

        incoming = SimpleNamespace(
            message_id="m1",
            conversation_id="cid_1",
            conversation_type="2",
            chatbot_user_id="bot_gem",
            chatbot_corp_id="ding42b8b39e5adac00a4ac5d6980864d335",
            text=SimpleNamespace(content="你好", extensions={}),
            extensions={},
        )

        with patch("app.dws_context.run_dws_readonly_json", side_effect=fake_run_json):
            b1 = await dws_ctx.fetch_dws_context_bundle(
                conversation_id="cid_1",
                is_group=True,
                incoming_messages=[incoming],
                current_message_ids=["m1"],
                raw_user_content="你好",
                history_messages=[],
            )
            b2 = await dws_ctx.fetch_dws_context_bundle(
                conversation_id="cid_1",
                is_group=True,
                incoming_messages=[incoming],
                current_message_ids=["m1"],
                raw_user_content="你好",
                history_messages=[],
            )
        assert "串号公司" in b1.corp_mismatch_error
        assert "串号公司" in b2.corp_mismatch_error


class TestSpec6MergeAndPersistenceFixes:
    """Spec #6 + F1：保留与助手同文的人类短回复、2s 缓冲多行去重、生成 persistable_messages。"""

    def test_human_short_reply_matching_assistant_text_is_never_dropped_and_multiline_dedupes(self):
        from app.dws_context import extract_clean_dws_item, merge_dws_group_messages_with_history

        history_messages = [
            {
                "role": "assistant",
                "content": "收到",
                "timestamp": "2026-11-11 10:00:00",
                "bot_id": "gemini",
            },
            {
                "role": "user",
                "content": "第一条补充\n第二条补充",
                "timestamp": "2026-11-11 14:05:15",
                "sender_nick": "曾工",
            },
        ]
        raw_dws = [
            {
                "messageId": "m_human_ack",
                "sender": "杰少",
                "senderId": "u_jie",
                "createTime": "2026-11-11 14:04:00",
                "text": "收到",
            },
            {
                "messageId": "m_buf_a",
                "sender": "曾工",
                "senderId": "u_zeng",
                "createTime": "2026-11-11 14:05:01",
                "text": "@Gemini小助手 第一条补充",
            },
            {
                "messageId": "m_buf_b",
                "sender": "曾工",
                "senderId": "u_zeng",
                "createTime": "2026-11-11 14:05:02",
                "text": "第二条补充",
            },
        ]
        dws_items = [x for item in raw_dws if (x := extract_clean_dws_item(item)) is not None]
        merged = merge_dws_group_messages_with_history(
            dws_items=dws_items,
            history_messages=history_messages,
            current_message_ids=set(),
            chatbot_user_id="bot_gem",
        )
        user_contents = [m["content"] for m in merged if m["role"] == "user"]
        # 杰少的「收到」被完整保留，且曾工缓冲合并的两行不会与历史「第一条补充\n第二条补充」重复成 4 条
        assert "收到" in user_contents
        assert len(user_contents) == 3
        assert "第一条补充\n第二条补充" not in user_contents


class TestSingleChatQuoteMultiQuoteAndAtMentionRestore:
    """Spec #2/#5 + F2：单聊引用拉上下文、2s 多引用隔离不串字段、还原被 Stream 剥离的 @他人。"""

    @pytest.mark.asyncio
    async def test_single_chat_quote_with_only_msg_id_hydrates_and_fetches_neighbors(self):
        import app.dws_context as dws_ctx

        dws_ctx.reset_dws_RuntimeState_for_tests()

        async def fake_run_json(args, **_kwargs):
            if list(args[:2]) == ["auth", "status"]:
                return {
                    "authenticated": True,
                    "token_valid": True,
                    "corp_id": "ding42b8b39e5adac00a4ac5d6980864d335",
                    "corp_name": "Vibe Coding 联盟",
                }
            if list(args[:2]) == ["chat", "+messages-mget"]:
                return {
                    "ok": True,
                    "data": {
                        "messages": [
                            {
                                "messageId": "m_dm_quote",
                                "conversationId": "cid_dm_1",
                                "sender": "曾工",
                                "text": "单聊里昨天发的预算表密码是 8848",
                                "createTime": "2026-11-10 18:00:00",
                            }
                        ]
                    },
                }
            if "--direction" in args:
                direction = args[args.index("--direction") + 1]
                if direction == "older":
                    return {
                        "ok": True,
                        "data": {
                            "messages": [
                                {"messageId": "m_dm_prev", "sender": "曾工", "text": "给你发一下密码", "createTime": "2026-11-10 17:59:50"},
                                {"messageId": "m_dm_quote", "sender": "曾工", "text": "单聊里昨天发的预算表密码是 8848", "createTime": "2026-11-10 18:00:00"},
                            ]
                        },
                    }
                return {
                    "ok": True,
                    "data": {
                        "messages": [
                            {"messageId": "m_dm_quote", "sender": "曾工", "text": "单聊里昨天发的预算表密码是 8848", "createTime": "2026-11-10 18:00:00"},
                            {"messageId": "m_dm_next", "sender": "曾工", "text": "别发到大群里", "createTime": "2026-11-10 18:00:10"},
                        ]
                    },
                }
            raise AssertionError(f"unexpected args: {args}")

        incoming_dm = SimpleNamespace(
            message_id="m_dm_curr",
            conversation_id="cid_dm_1",
            conversation_type="1",
            chatbot_user_id="bot_gem",
            chatbot_corp_id="ding42b8b39e5adac00a4ac5d6980864d335",
            text=SimpleNamespace(content="这个密码改了吗？", extensions={"originalMsgId": "m_dm_quote"}),
            extensions={},
        )

        with patch.object(dws_ctx.cfg, "ENABLE_DWS_GROUP_CONTEXT", False), \
             patch("app.dws_context.run_dws_readonly_json", side_effect=fake_run_json):
            bundle = await dws_ctx.fetch_dws_context_bundle(
                conversation_id="cid_dm_1",
                is_group=False,
                incoming_messages=[incoming_dm],
                current_message_ids=["m_dm_curr"],
                raw_user_content="这个密码改了吗？",
                history_messages=[],
            )

        assert bundle.quote_info is not None
        assert "8848" in bundle.quote_prompt_block
        assert "给你发一下密码" in bundle.quote_prompt_block
        assert "别发到大群里" in bundle.quote_prompt_block

    @pytest.mark.asyncio
    async def test_buffered_multiple_quotes_preserve_both_without_field_mixing_and_restore_at_mentions(self):
        import app.dws_context as dws_ctx

        dws_ctx.reset_dws_RuntimeState_for_tests()
        recent_payload = {
            "ok": True,
            "data": {
                "messages": [
                    {"messageId": "q1", "sender": "杰少", "text": "接口超时设成 5 秒", "createTime": "2026-11-11 14:00:00"},
                    {"messageId": "q2", "sender": "阿强", "text": "重试次数最多 2 次", "createTime": "2026-11-11 14:01:00"},
                    {
                        "messageId": "m_c1",
                        "sender": "曾工",
                        "text": "@Gemini小助手 结合这两条帮我通知 @曾健华(我司曾工) 改网关配置",
                        "createTime": "2026-11-11 14:02:00",
                        "quotedMessage": {"messageId": "q1", "sender": "杰少", "text": "接口超时设成 5 秒", "createTime": "2026-11-11 14:00:00"},
                    },
                    {
                        "messageId": "m_c2",
                        "sender": "曾工",
                        "text": "还有这一条也看下",
                        "createTime": "2026-11-11 14:02:01",
                        "quotedMessage": {"messageId": "q2", "sender": "阿强", "text": "重试次数最多 2 次", "createTime": "2026-11-11 14:01:00"},
                    },
                ]
            },
        }

        async def fake_run_json(args, **_kwargs):
            if list(args[:2]) == ["auth", "status"]:
                return {
                    "authenticated": True,
                    "token_valid": True,
                    "corp_id": "ding42b8b39e5adac00a4ac5d6980864d335",
                }
            return recent_payload

        inc1 = SimpleNamespace(
            message_id="m_c1",
            sender_nick="曾工",
            conversation_id="cid_g",
            conversation_type="2",
            chatbot_user_id="bot_gem",
            chatbot_corp_id="ding42b8b39e5adac00a4ac5d6980864d335",
            at_users=[SimpleNamespace(dingtalk_id="bot_gem"), SimpleNamespace(dingtalk_id="u_zeng")],
            text=SimpleNamespace(content="结合这两条帮我通知  改网关配置", extensions={}),
            extensions={},
        )
        inc2 = SimpleNamespace(
            message_id="m_c2",
            sender_nick="曾工",
            conversation_id="cid_g",
            conversation_type="2",
            chatbot_user_id="bot_gem",
            chatbot_corp_id="ding42b8b39e5adac00a4ac5d6980864d335",
            at_users=[SimpleNamespace(dingtalk_id="bot_gem")],
            text=SimpleNamespace(content="还有这一条也看下", extensions={}),
            extensions={},
        )

        with patch("app.dws_context.run_dws_readonly_json", side_effect=fake_run_json):
            bundle = await dws_ctx.fetch_dws_context_bundle(
                conversation_id="cid_g",
                is_group=True,
                incoming_messages=[inc1, inc2],
                current_message_ids=["m_c1", "m_c2"],
                raw_user_content="结合这两条帮我通知  改网关配置\n还有这一条也看下",
                history_messages=[],
            )

        # 两条引用都进入 <quoted_context> 且各自归属不混串
        assert "杰少: 接口超时设成 5 秒" in bundle.quote_prompt_block
        assert "阿强: 重试次数最多 2 次" in bundle.quote_prompt_block
        # F2: 还原被钉钉 Stream 剥除的 @曾健华(我司曾工)，同时剔除 @Gemini小助手
        assert "@曾健华(我司曾工)" in bundle.restored_user_content
        assert "@Gemini小助手" not in bundle.restored_user_content
        # F1: q1 与 q2 进入可持久化外部消息列表，当前触发消息 m_c1/m_c2 不提前落库
        persisted_ids = [m["external_msg_id"] for m in bundle.persistable_messages]
        assert persisted_ids == ["q1", "q2"]


class TestQuotedImagesAndDocumentsReadonly:
    """F3 & F4：引用图片（公网 OSS / 内网表情降级）与引用文档（adoc / docx / pdf）内存只读提取测试。"""

    @pytest.mark.asyncio
    async def test_quoted_public_and_internal_images_extracted_into_memory(self):
        import io
        from PIL import Image
        import app.dws_context as dws_ctx

        dws_ctx.reset_dws_RuntimeState_for_tests()
        png_buf = io.BytesIO()
        Image.new("RGB", (32, 32), color=(40, 120, 220)).save(png_buf, format="PNG")
        png_bytes = png_buf.getvalue()

        created_temp_dirs = []

        async def fake_run_json(args, **_kwargs):
            if list(args[:2]) == ["auth", "status"]:
                return {"authenticated": True, "token_valid": True, "corp_id": "ding_corp_ok"}
            if list(args[:3]) == ["chat", "message", "list"]:
                return {
                    "ok": True,
                    "data": {
                        "messages": [
                            {
                                "messageId": "m_img_pub",
                                "conversationId": "cid_g",
                                "sender": "杰少",
                                "messageType": "picture",
                                "text": "[图片]",
                                "createTime": "2026-11-11 15:00:00",
                                "resources": [
                                    {
                                        "resourceId": "@lQLP_pub",
                                        "resourceIdType": "mediaId",
                                        "resourceType": "image",
                                        "url": "https://wukong-file.oss-cn-shenzhen.aliyuncs.com/pub.png",
                                    }
                                ],
                            },
                            {
                                "messageId": "m_img_int",
                                "conversationId": "cid_g",
                                "sender": "阿强",
                                "messageType": "picture",
                                "text": "[图片]",
                                "createTime": "2026-11-11 15:00:10",
                                "resources": [
                                    {
                                        "resourceId": "$iwEl_internal",
                                        "resourceIdType": "mediaId",
                                        "resourceType": "image",
                                        "url": "http://lippi-space.oss-internal.aliyun-inc.com/emo.png",
                                    }
                                ],
                            },
                            {
                                "messageId": "m_curr_1",
                                "conversationId": "cid_g",
                                "sender": "曾工",
                                "text": "@Gemini小助手 这两张图分别是什么？",
                                "createTime": "2026-11-11 15:01:00",
                                "quotedMessage": {"messageId": "m_img_pub", "sender": "杰少", "text": "[图片]"},
                            },
                            {
                                "messageId": "m_curr_2",
                                "conversationId": "cid_g",
                                "sender": "曾工",
                                "text": "还有这张",
                                "createTime": "2026-11-11 15:01:01",
                                "quotedMessage": {"messageId": "m_img_int", "sender": "阿强", "text": "[图片]"},
                            },
                        ]
                    },
                }
            if list(args[:3]) == ["chat", "message", "download-media"]:
                out_dir = args[args.index("--output") + 1]
                created_temp_dirs.append(out_dir)
                out_file = os.path.join(out_dir, "emo.png")
                with open(out_file, "wb") as f:
                    f.write(png_bytes)
                return {"success": True, "path": out_file}
            raise AssertionError(f"unexpected dws call: {args}")

        inc1 = SimpleNamespace(
            message_id="m_curr_1",
            sender_nick="曾工",
            conversation_id="cid_g",
            conversation_type="2",
            chatbot_user_id="bot_gem",
            chatbot_corp_id="ding_corp_ok",
            text=SimpleNamespace(content="这两张图分别是什么？", extensions={}),
            extensions={},
        )
        inc2 = SimpleNamespace(
            message_id="m_curr_2",
            sender_nick="曾工",
            conversation_id="cid_g",
            conversation_type="2",
            chatbot_user_id="bot_gem",
            chatbot_corp_id="ding_corp_ok",
            text=SimpleNamespace(content="还有这张", extensions={}),
            extensions={},
        )

        with patch("app.dws_context.run_dws_readonly_json", side_effect=fake_run_json), \
             patch("app.dws_files.fetch_public_bytes", new_callable=AsyncMock, return_value=png_bytes):
            bundle = await dws_ctx.fetch_dws_context_bundle(
                conversation_id="cid_g",
                is_group=True,
                incoming_messages=[inc1, inc2],
                current_message_ids=["m_curr_1", "m_curr_2"],
                raw_user_content="这两张图分别是什么？\n还有这张",
                history_messages=[],
            )

        assert len(bundle.quoted_images) == 2
        assert all(img.startswith(b"\x89PNG") for img in bundle.quoted_images)
        # 临时目录在读取完字节后必须立即被自动销毁，不残留磁盘文件
        assert len(created_temp_dirs) == 1
        assert not os.path.exists(created_temp_dirs[0])

    @pytest.mark.asyncio
    async def test_quoted_adoc_docx_and_pdf_documents_parsed_in_memory(self):
        import io
        import zipfile
        from pypdf import PdfWriter
        import app.dws_context as dws_ctx

        dws_ctx.reset_dws_RuntimeState_for_tests()

        # 构造内存中的真实最小 .docx（含段落与表格）
        docx_buf = io.BytesIO()
        with zipfile.ZipFile(docx_buf, "w") as zf:
            zf.writestr(
                "word/document.xml",
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                "<w:body>"
                "<w:p><w:r><w:t>发布清单：今晚 21:00 灰度上线</w:t></w:r></w:p>"
                "<w:tbl><w:tr>"
                "<w:tc><w:p><w:r><w:t>服务名</w:t></w:r></w:p></w:tc>"
                "<w:tc><w:p><w:r><w:t>dingtalk-ai-bot</w:t></w:r></w:p></w:tc>"
                "</w:tr></w:tbl>"
                "</w:body></w:document>",
            )
        docx_bytes = docx_buf.getvalue()

        # 构造内存中的真实最小空白 PDF（同时测试 pypdf 解析调用链）
        pdf_buf = io.BytesIO()
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        writer.write(pdf_buf)
        pdf_bytes = pdf_buf.getvalue()

        async def fake_run_json(args, **_kwargs):
            if list(args[:2]) == ["auth", "status"]:
                return {"authenticated": True, "token_valid": True, "corp_id": "ding_corp_ok"}
            if list(args[:3]) == ["chat", "message", "list"]:
                return {
                    "ok": True,
                    "data": {
                        "messages": [
                            {
                                "messageId": "m_doc_1",
                                "conversationId": "cid_g",
                                "sender": "曾工",
                                "messageType": "file",
                                "text": "[文件] 上线方案.docx fileId: NODE_DOCX_1",
                                "createTime": "2026-11-11 16:00:00",
                                "resources": [
                                    {
                                        "resourceId": "NODE_DOCX_1",
                                        "resourceIdType": "fileId",
                                        "resourceType": "file",
                                        "url": "https://alidocs.dingtalk.com/i/nodes/NODE_DOCX_1",
                                    }
                                ],
                            },
                            {
                                "messageId": "m_doc_2",
                                "conversationId": "cid_g",
                                "sender": "杰少",
                                "text": "在线架构文档看这里 https://alidocs.dingtalk.com/i/nodes/NODE_ADOC_2",
                                "createTime": "2026-11-11 16:01:00",
                            },
                            {
                                "messageId": "m_curr_doc",
                                "conversationId": "cid_g",
                                "sender": "阿强",
                                "text": "@Gemini小助手 总结下这两个文档的核心结论",
                                "createTime": "2026-11-11 16:02:00",
                                "quotedMessage": {
                                    "messageId": "m_doc_1",
                                    "sender": "曾工",
                                    "text": "[文件] 上线方案.docx fileId: NODE_DOCX_1 https://alidocs.dingtalk.com/i/nodes/NODE_ADOC_2",
                                },
                            },
                        ]
                    },
                }
            if list(args[:2]) == ["doc", "info"]:
                node = args[args.index("--node") + 1]
                if node == "NODE_DOCX_1":
                    return {"success": True, "nodeId": node, "name": "上线方案.docx", "extension": "docx"}
                return {"success": True, "nodeId": node, "name": "架构评审纪要", "extension": "adoc"}
            if list(args[:2]) == ["doc", "read"]:
                return {"success": True, "markdown": "# 架构评审纪要\n采用 MySQL uk_session_ext_msg 幂等索引。"}
            if list(args[:2]) == ["drive", "download"]:
                assert "--url-only" in args
                return {
                    "success": True,
                    "urlOnly": True,
                    "fileName": "上线方案.docx",
                    "fileSize": len(docx_bytes),
                    "downloadUrl": "https://dingtalk-drive.oss-cn-hangzhou.aliyuncs.com/plan.docx",
                }
            raise AssertionError(f"unexpected dws call: {args}")

        inc = SimpleNamespace(
            message_id="m_curr_doc",
            sender_nick="阿强",
            conversation_id="cid_g",
            conversation_type="2",
            chatbot_user_id="bot_gem",
            chatbot_corp_id="ding_corp_ok",
            text=SimpleNamespace(content="总结下这两个文档的核心结论", extensions={}),
            extensions={},
        )

        with patch("app.dws_context.run_dws_readonly_json", side_effect=fake_run_json), \
             patch("app.dws_files.fetch_public_bytes", new_callable=AsyncMock, return_value=docx_bytes):
            bundle = await dws_ctx.fetch_dws_context_bundle(
                conversation_id="cid_g",
                is_group=True,
                incoming_messages=[inc],
                current_message_ids=["m_curr_doc"],
                raw_user_content="总结下这两个文档的核心结论",
                history_messages=[],
            )

        assert "发布清单：今晚 21:00 灰度上线" in bundle.quote_prompt_block
        assert "dingtalk-ai-bot" in bundle.quote_prompt_block
        assert "采用 MySQL uk_session_ext_msg 幂等索引" in bundle.quote_prompt_block
        assert "上线方案.docx" in bundle.quote_router_text

        # 同时单独验证 parse_pdf_bytes 对有效 PDF 字节流无异常运行
        from app.dws_files import parse_pdf_bytes

        assert isinstance(parse_pdf_bytes(pdf_bytes), str)
