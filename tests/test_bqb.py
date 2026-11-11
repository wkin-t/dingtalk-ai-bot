# -*- coding: utf-8 -*-
"""ChineseBQB 表情包模块单元测试（离线隔离，不访问外网与真实 data/ 目录）。"""
import json
import os
import random
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.bqb as bqb
import app.config as cfg

PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)
GIF_MIN = b"GIF89a\x01\x00\x01\x00\x00\x00\x00;"

SHA_PANDA = "a" * 64
SHA_ZHENHUAN = "b" * 64
SHA_TOM = "c" * 64
SHA_DOG_PLAIN = "d" * 64
SHA_LUOXIANG = "e" * 64


def _sample_catalog_raw():
    return [
        {
            "src": f"media/{SHA_PANDA}.jpg",
            "label": "金馆长熊猫00191-这么刺激吗",
            "categoryTitle": "026_Panda金馆长熊猫BQB",
            "folder": "026_Panda金馆长熊猫BQB",
            "animated": False,
            "bytes": 18200,
        },
        {
            "src": f"media/{SHA_ZHENHUAN}.gif",
            "label": "甄嬛传00006-要不请病假吧",
            "categoryTitle": "甄嬛传BQB",
            "folder": "076_甄嬛传BQB",
            "animated": True,
            "bytes": 124000,
        },
        {
            "src": f"media/{SHA_TOM}.gif",
            "label": "猫和老鼠_柠檬真酸",
            "categoryTitle": "猫和老鼠BQB",
            "folder": "012_猫和老鼠BQB",
            "animated": True,
            "bytes": 96000,
        },
        {
            "src": f"media/{SHA_DOG_PLAIN}.png",
            "label": "斗图之王00002",
            "categoryTitle": "斗图之王BQB",
            "folder": "001_斗图之王BQB",
            "animated": False,
            "bytes": 15000,
        },
        {
            "src": f"media/{SHA_LUOXIANG}.jpg",
            "label": "罗翔00012-目瞪口呆",
            "categoryTitle": "JOJO与罗翔BQB",
            "folder": "088_JOJO与罗翔BQB",
            "animated": False,
            "bytes": 21000,
        },
        # 应被过滤的异常条目：webp、路径穿越、超体积、重复 sha256
        {
            "src": f"media/{'f' * 64}.webp",
            "label": "不支持的格式",
            "categoryTitle": "测试BQB",
            "bytes": 9000,
        },
        {
            "src": "../../etc/passwd.png",
            "label": "非法路径",
            "categoryTitle": "测试BQB",
            "bytes": 100,
        },
        {
            "src": f"media/{'9' * 64}.gif",
            "label": "超大动图",
            "categoryTitle": "测试BQB",
            "bytes": 99 * 1024 * 1024,
        },
        {
            "src": f"media/{SHA_PANDA}.jpg",
            "label": "重复哈希",
            "categoryTitle": "重复BQB",
            "bytes": 18200,
        },
    ]


@pytest.fixture(autouse=True)
def _isolated_bqb_state(tmp_path, monkeypatch):
    monkeypatch.setattr(bqb, "BQB_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(cfg, "ENABLE_BQB", True)
    monkeypatch.setattr(cfg, "BQB_MAX_PER_REPLY", 1)
    monkeypatch.setattr(cfg, "BQB_BACK_TO_BACK_COOLDOWN_SECONDS", 120)
    monkeypatch.setattr(cfg, "BQB_WINDOW_SIZE", 5)
    monkeypatch.setattr(cfg, "BQB_WINDOW_MAX_COUNT", 2)
    bqb._pacer.reset()
    bqb._index_manager.reset_for_test()
    bqb._recent_sent_ids.clear()
    bqb._media_id_mem_cache.clear()
    yield
    bqb._pacer.reset()
    bqb._index_manager.reset_for_test()
    bqb._recent_sent_ids.clear()
    bqb._media_id_mem_cache.clear()


class TestStreamingTagStrip:
    def test_strips_complete_and_chinese_colon_tags(self):
        raw = "笑死我了 [[bqb:熊猫头 刺激]] 真的离谱[[BQB：甄嬛传 请病假]]！"
        assert bqb.strip_bqb_tags_for_streaming(raw) == "笑死我了  真的离谱！"

    @pytest.mark.parametrize(
        "partial",
        [
            "今天先撤了 [[",
            "今天先撤了 [[b",
            "今天先撤了 [[bq",
            "今天先撤了 [[bqb",
            "今天先撤了 [[bqb:",
            "今天先撤了 [[bqb:熊猫头 刺",
        ],
    )
    def test_hides_trailing_unclosed_tag(self, partial):
        assert bqb.strip_bqb_tags_for_streaming(partial) == "今天先撤了 "

    def test_preserves_markdown_links_and_non_bqb_brackets(self):
        md = "参考 [官方文档](https://example.com) 以及代码 `arr[[0]]` 和末尾 arr[[0"
        assert bqb.strip_bqb_tags_for_streaming(md) == md


class TestCatalogAndSearch:
    def test_parse_raw_catalog_filters_unsafe_and_cleans_labels(self):
        entries = bqb.parse_raw_catalog(_sample_catalog_raw(), max_image_bytes=8 * 1024 * 1024)
        shas = [e.sha256 for e in entries]
        assert shas == [SHA_PANDA, SHA_ZHENHUAN, SHA_TOM, SHA_DOG_PLAIN, SHA_LUOXIANG]

        by_sha = {e.sha256: e for e in entries}
        assert by_sha[SHA_PANDA].clean_category == "金馆长熊猫"
        assert by_sha[SHA_PANDA].clean_label == "这么刺激吗"
        assert by_sha[SHA_PANDA].has_semantic is True

        assert by_sha[SHA_DOG_PLAIN].clean_label == ""
        assert by_sha[SHA_DOG_PLAIN].has_semantic is False
        assert "JOJO" in by_sha[SHA_LUOXIANG].clean_category

    def test_search_matches_dialogue_alias_and_mood_synonyms(self):
        entries = bqb.parse_raw_catalog(_sample_catalog_raw())
        rng = random.Random(42)

        hit_dialogue = bqb.search_entries("甄嬛传 请病假", entries, rng=rng)
        assert hit_dialogue is not None and hit_dialogue.sha256 == SHA_ZHENHUAN

        hit_alias = bqb.search_entries("熊猫头 刺激", entries, rng=rng)
        assert hit_alias is not None and hit_alias.sha256 == SHA_PANDA

        hit_mood = bqb.search_entries("酸了", entries, rng=rng)
        assert hit_mood is not None and hit_mood.sha256 == SHA_TOM

        hit_miss = bqb.search_entries("规范场论重整化群方程", entries, rng=rng)
        assert hit_miss is None

    def test_search_avoids_recently_sent_in_same_session(self):
        raw = [
            {
                "src": f"media/{'1' * 64}.jpg",
                "label": "熊猫001-刺激现场",
                "categoryTitle": "金馆长熊猫BQB",
                "bytes": 12000,
            },
            {
                "src": f"media/{'2' * 64}.jpg",
                "label": "熊猫002-太刺激了",
                "categoryTitle": "金馆长熊猫BQB",
                "bytes": 12000,
            },
        ]
        entries = bqb.parse_raw_catalog(raw)
        bqb._record_recent_sent("sess-1", "1" * 64)
        picked = bqb.search_entries("熊猫头 刺激", entries, session_key="sess-1", rng=random.Random(7))
        assert picked is not None
        assert picked.sha256 == "2" * 64


class TestExplicitIntentDetection:
    @pytest.mark.parametrize(
        "msg",
        [
            "来一张熊猫头表情包",
            "斗图！看招",
            "给我发个滑稽",
            "再来点沙雕图",
        ],
    )
    def test_explicit_request_true(self, msg):
        assert bqb.is_explicit_bqb_request(msg) is True

    @pytest.mark.parametrize(
        "msg",
        [
            "帮我画一张赛博朋克城市壁纸",
            "今天加班好累啊",
            "这段 Python 报错怎么修",
        ],
    )
    def test_non_bqb_or_draw_request_false(self, msg):
        assert bqb.is_explicit_bqb_request(msg) is False


class TestCachingAndUpload:
    @pytest.mark.asyncio
    async def test_media_id_cached_per_client_and_reuploads_after_ttl(self, tmp_path, monkeypatch):
        entries = bqb.parse_raw_catalog(_sample_catalog_raw())
        entry = entries[0]

        media_dir = tmp_path / "media"
        media_dir.mkdir(parents=True, exist_ok=True)
        (media_dir / f"{entry.sha256}.{entry.ext}").write_bytes(PNG_1X1)

        helper_a = SimpleNamespace(
            client_id="app-client-a",
            upload_media=AsyncMock(return_value="lALP_FIRST_ID"),
        )
        helper_b = SimpleNamespace(
            client_id="app-client-b",
            upload_media=AsyncMock(return_value="@lALP_OTHER_BOT"),
        )

        # 首次上传：自动补齐单个 '@' 前缀并写入缓存
        mid_1 = await bqb.get_or_upload_media_id(entry, helper_a)
        assert mid_1 == "@lALP_FIRST_ID"
        assert helper_a.upload_media.await_count == 1

        # 同 client_id 二次调用命中 48h 缓存，零上传
        mid_2 = await bqb.get_or_upload_media_id(entry, helper_a)
        assert mid_2 == "@lALP_FIRST_ID"
        assert helper_a.upload_media.await_count == 1

        # 另一机器人容器不同 client_id 独立上传，互不串用失效 token
        mid_b = await bqb.get_or_upload_media_id(entry, helper_b)
        assert mid_b == "@lALP_OTHER_BOT"
        assert helper_b.upload_media.await_count == 1

        # 模拟超过 TTL 后自动重传
        monkeypatch.setattr(cfg, "BQB_MEDIA_ID_TTL_SECONDS", -1)
        helper_a.upload_media = AsyncMock(return_value="@@lALP_REFRESHED")
        mid_3 = await bqb.get_or_upload_media_id(entry, helper_a)
        assert mid_3 == "@lALP_REFRESHED"
        assert helper_a.upload_media.await_count == 1

    @pytest.mark.asyncio
    async def test_fetch_rejects_invalid_magic_bytes(self, monkeypatch):
        entries = bqb.parse_raw_catalog(_sample_catalog_raw())

        async def _fake_html_fetch(*_args, **_kwargs):
            return b"<html><body>404 Not Found</body></html>"

        monkeypatch.setattr(bqb, "_http_fetch_bytes", _fake_html_fetch)
        with pytest.raises(ValueError, match="合法"):
            await bqb.fetch_bqb_image_bytes(entries[0])


class TestPacingAndResolution:
    @pytest.mark.asyncio
    async def test_spontaneous_pacing_cooldown_window_and_explicit_bypass(self, tmp_path):
        # 预置本地索引与原图缓存
        index_path = tmp_path / "search_index.json"
        index_path.write_text(json.dumps(_sample_catalog_raw(), ensure_ascii=False), encoding="utf-8")
        media_dir = tmp_path / "media"
        media_dir.mkdir(parents=True, exist_ok=True)
        for sha, ext, blob in [
            (SHA_PANDA, "jpg", PNG_1X1),
            (SHA_ZHENHUAN, "gif", GIF_MIN),
            (SHA_TOM, "gif", GIF_MIN),
        ]:
            (media_dir / f"{sha}.{ext}").write_bytes(blob)

        helper = SimpleNamespace(
            client_id="bot-test",
            upload_media=AsyncMock(return_value="@lALP_TEST_MEDIA"),
        )
        clock = [1000.0]
        now_fn = lambda: clock[0]

        # Turn 1 (t=1000s): 闲聊首轮允许自发配图
        r1 = await bqb.resolve_bqb_tags(
            "太真实了 [[bqb:熊猫头 刺激]]",
            user_content="今天上班差点迟到",
            card_helper=helper,
            session_key="group-1",
            now_fn=now_fn,
        )
        assert r1.sent_count == 1
        assert r1.throttled is False
        assert "![这么刺激吗](@lALP_TEST_MEDIA)" in r1.card_text
        assert "[[bqb:金馆长熊猫 这么刺激吗]]" in r1.history_text
        assert "@lALP_TEST_MEDIA" not in r1.history_text

        # Turn 2 (t=1030s, <120s 冷却): 紧邻下一轮被背靠背冷却拦截，标签剥净
        clock[0] = 1030.0
        r2 = await bqb.resolve_bqb_tags(
            "确实得歇会儿 [[bqb:甄嬛传 请病假]]",
            user_content="感觉身体被掏空",
            card_helper=helper,
            session_key="group-1",
            now_fn=now_fn,
        )
        assert r2.sent_count == 0
        assert r2.throttled is True
        assert r2.card_text == "确实得歇会儿"
        assert r2.history_text == "确实得歇会儿"

        # Turn 3 (t=1060s): 上一轮未发图，不再背靠背，允许发第 2 张（窗口 [T, F, T] 共 2 张）
        clock[0] = 1060.0
        r3 = await bqb.resolve_bqb_tags(
            "不如直接请假 [[bqb:甄嬛传 请病假]]",
            user_content="下午还有三个会",
            card_helper=helper,
            session_key="group-1",
            now_fn=now_fn,
        )
        assert r3.sent_count == 1
        assert r3.throttled is False

        # Turn 4 (t=1300s, 已过 120s 冷却): 5 轮窗口内已达 2 张上限，触发滑动窗口限流
        clock[0] = 1300.0
        r4 = await bqb.resolve_bqb_tags(
            "我酸了 [[bqb:猫和老鼠 柠檬]]",
            user_content="隔壁组今天提前下班了",
            card_helper=helper,
            session_key="group-1",
            now_fn=now_fn,
        )
        assert r4.sent_count == 0
        assert r4.throttled is True

        # 显式讨要直通：即使处于窗口限流中，用户明确要图也 100% 放行
        clock[0] = 1310.0
        r_explicit = await bqb.resolve_bqb_tags(
            "安排上 [[bqb:猫和老鼠 柠檬]]",
            user_content="来一张柠檬表情包",
            card_helper=helper,
            session_key="group-1",
            now_fn=now_fn,
        )
        assert r_explicit.sent_count == 1
        assert r_explicit.throttled is False

        # Turn 5 (t=1500s): 自发窗口仍为 [T, F, T, F] (2 张)，继续限流并推进窗口到 [T, F, T, F, F]
        clock[0] = 1500.0
        r5 = await bqb.resolve_bqb_tags(
            "再忍忍 [[bqb:熊猫头 刺激]]",
            user_content="快到周末了",
            card_helper=helper,
            session_key="group-1",
            now_fn=now_fn,
        )
        assert r5.sent_count == 0
        assert r5.throttled is True

        # Turn 6 (t=1700s): Turn 1 滑出 5 轮窗口（剩余 [F, T, F, F] 仅 1 张），重新恢复自发配图
        clock[0] = 1700.0
        r6 = await bqb.resolve_bqb_tags(
            "周末快乐 [[bqb:猫和老鼠 柠檬]]",
            user_content="终于周五下班啦",
            card_helper=helper,
            session_key="group-1",
            now_fn=now_fn,
        )
        assert r6.sent_count == 1
        assert r6.throttled is False
