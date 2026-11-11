# -*- coding: utf-8 -*-
"""ChineseBQB 表情包索引同步、中文混合检索、双层缓存与标签解析模块。"""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
import hashlib
import io
import json
import os
from pathlib import Path
import random
import re
import tempfile
import threading
import time
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

import httpx

import app.config as cfg

BQB_DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "bqb"
)

# 仅放行钉钉 oapi /media/upload 原生支持且路径为固定 64 位 sha256 的静态文件，阻断路径穿越与 SSRF
_SAFE_SRC_RE = re.compile(
    r"^media/([0-9a-f]{64})\.(jpg|jpeg|png|gif|bmp)$", re.IGNORECASE
)

# 完整标签：[[bqb:关键词]]（兼容大小写与全角冒号）
_COMPLETE_TAG_RE = re.compile(
    r"\[\[\s*bqb\s*[:：]\s*([^\[\]\n]{1,80}?)\s*\]\]", re.IGNORECASE
)
# 流式尾部尚未闭合的候选前缀（至少出现连续两个 '[' 才暂隐，避免误伤普通 Markdown 链接 [text](url)）
_TRAILING_PARTIAL_TAG_RE = re.compile(
    r"\[\[\s*(?:b(?:q(?:b(?:\s*[:：][^\[\]\n]*)?)?)?)?$", re.IGNORECASE
)

_EXPLICIT_BQB_KEYWORDS = (
    "表情包",
    "梗图",
    "斗图",
    "沙雕图",
    "搞笑图",
    "动图",
)
_EXPLICIT_BQB_PATTERN = re.compile(
    r"(?:来|发|给|整|甩|丢|求|换|再来|配)\s*(?:一|几|两|个|张|点|波)?[^，。！？\n]{0,12}(?:表情|梗图|图|滑稽|熊猫头|蘑菇头|猫猫图|狗头)",
    re.IGNORECASE,
)
_IMAGE_DRAW_EXCLUSION_RE = re.compile(
    r"(?:画一|绘制|AI生图|生成一张.*(?:壁纸|插画|海报|头像|写实|油画|赛博))",
    re.IGNORECASE,
)

_WILDCARD_TOKENS = frozenset(
    {
        "随机",
        "随便",
        "任意",
        "斗图",
        "表情包",
        "表情",
        "梗图",
        "来一张",
        "再来一张",
        "搞笑",
        "沙雕",
    }
)

# 俗称/角色别名 → 仓库真实分类关键词
_CATEGORY_ALIASES: Dict[str, Tuple[str, ...]] = {
    "熊猫头": ("金馆长熊猫", "崔成国", "斗图之王"),
    "熊猫人": ("金馆长熊猫", "崔成国"),
    "蘑菇头": ("金馆长熊猫", "斗图之王"),
    "金馆长": ("金馆长熊猫", "崔成国"),
    "滑稽": ("滑稽大佬", "表情符号"),
    "狗头": ("狗", "滑稽大佬", "表情符号"),
    "旺柴": ("狗", "滑稽大佬"),
    "修狗": ("狗",),
    "狗狗": ("狗",),
    "小狗": ("狗",),
    "猫": ("是喵星人啦", "猫咪日常", "Mur猫"),
    "猫猫": ("是喵星人啦", "猫咪日常", "Mur猫"),
    "猫咪": ("是喵星人啦", "猫咪日常", "Mur猫"),
    "小猫": ("是喵星人啦", "猫咪日常"),
    "喵": ("是喵星人啦", "猫咪日常"),
    "汤姆": ("猫和老鼠",),
    "杰瑞": ("猫和老鼠",),
    "张三": ("罗翔",),
    "法外狂徒": ("罗翔",),
    "老罗": ("罗永浩",),
    "佩奇": ("小猪佩奇",),
    "小黄鸭": ("开心鸭",),
    "鸭鸭": ("开心鸭",),
    "坤坤": ("蔡徐坤",),
    "ikun": ("蔡徐坤",),
    "只因": ("蔡徐坤",),
    "闪电五连鞭": ("马保国",),
    "耗子尾汁": ("马保国",),
    "大意了": ("马保国",),
    "饮茶": ("饮茶哥",),
    "三点几": ("饮茶哥",),
    "喂茶": ("饮茶哥",),
    "码农": ("程序员",),
    "写代码": ("程序员",),
    "改bug": ("程序员",),
    "悟空": ("黑神话悟空",),
    "阿尼亚": ("间谍过家家",),
    "wakuwaku": ("间谍过家家",),
    "老干妈": ("腾讯与老千妈",),
    "真香": ("斗图之王", "名人明星"),
}

# 聊天高频情绪/场景词 → 仓库标签常见词根
_MOOD_SYNONYMS: Dict[str, Tuple[str, ...]] = {
    "摸鱼": ("摸鱼", "请病假", "饮茶", "喝茶", "不想", "放假", "困"),
    "划水": ("水", "摸鱼", "饮茶", "喝茶"),
    "不想上班": ("请病假", "上班", "放假", "打工", "累"),
    "请假": ("请病假", "病假", "难受"),
    "摆烂": ("躺", "算了", "请病假", "战败", "废"),
    "加班": ("加班", "程序员", "上班", "打工", "累", "困"),
    "打工": ("打工", "上班", "搬砖", "累", "请病假"),
    "搬砖": ("打工", "上班", "累"),
    "收到": ("收到", "好的", "依你", "没问题", "安排", "懂", "可以"),
    "好的": ("好的", "依你", "收到", "没问题", "可以"),
    "没问题": ("没问题", "依你", "包在", "安排"),
    "震惊": ("刺激", "震惊", "还能这样", "卧槽", "目瞪口呆"),
    "离谱": ("刺激", "还能这样", "离谱", "槽", "鬼"),
    "逆天": ("刺激", "还能这样", "不做人"),
    "刺激": ("刺激",),
    "破防": ("战败", "太难", "哭", "委屈", "心碎", "裂开"),
    "裂开": ("战败", "太难", "哭", "自闭", "累"),
    "麻了": ("太难", "累", "战败", "凝视", "无语"),
    "崩溃": ("太难", "哭", "战败", "怎么办"),
    "委屈": ("委屈", "哭", "欺负", "别哭"),
    "酸": ("柠檬", "酸", "羡慕"),
    "酸了": ("柠檬", "酸", "羡慕"),
    "羡慕": ("柠檬", "羡慕", "酸"),
    "柠檬": ("柠檬",),
    "疑惑": ("问号", "怎么办", "疑惑", "什么", "凝视"),
    "问号": ("问号", "什么", "疑惑", "看不懂"),
    "懵": ("问号", "怎么办", "我是谁"),
    "无语": ("凝视", "无语", "静静", "假笑", "尴尬"),
    "尴尬": ("假笑", "尴尬", "凝视"),
    "开心": ("哈哈", "开心", "快乐", "得意", "上线"),
    "哈哈": ("哈哈", "笑", "开心", "得意"),
    "笑死": ("哈哈", "笑", "逗"),
    "得意": ("得意", "靓仔", "美到", "冠军"),
    "打人": ("找抽", "打死", "打头", "下手很重", "打错"),
    "欠揍": ("找抽", "打死", "打头", "坏坏"),
    "怼": ("找抽", "打死", "打错", "坏坏"),
    "爱你": ("心心", "小宝贝", "喜欢", "爱你", "比心"),
    "比心": ("心心", "小宝贝", "爱你"),
    "喝茶": ("饮茶", "喝咖啡", "咖啡", "茶"),
    "咖啡": ("喝咖啡", "咖啡"),
    "困": ("困", "睡觉", "晚安"),
    "晚安": ("晚安", "睡"),
    "孤独": ("孤独", "单身"),
    "买买买": ("买买买", "钱"),
}

_JUNK_LABEL_TOKENS_RE = re.compile(
    r"^(?:webwxgetmsgimg|mmexport|wx_camera|doutuzhiwang|weixiin|img|image|gif|jpg|png|screenshot)[-_\d\s]*$",
    re.IGNORECASE,
)
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
_SPLIT_QUERY_RE = re.compile(r"[\s|,，、/\\;；:：\-_]+")


@dataclass(frozen=True)
class BQBEntry:
    sha256: str
    ext: str
    src: str
    raw_label: str
    clean_label: str
    clean_label_lower: str
    category_title: str
    clean_category: str
    clean_category_lower: str
    animated: bool
    bytes_size: int
    has_semantic: bool
    cjk_chars: frozenset[str]
    cjk_bigrams: frozenset[str]


@dataclass
class BQBResolveResult:
    card_text: str
    history_text: str
    sent_count: int = 0
    throttled: bool = False


def _cjk_features(text: str) -> Tuple[frozenset[str], frozenset[str]]:
    chunks = _CJK_RE.findall(text or "")
    joined = "".join(chunks)
    chars = frozenset(joined)
    bigrams = (
        frozenset(joined[i : i + 2] for i in range(len(joined) - 1))
        if len(joined) >= 2
        else frozenset()
    )
    return chars, bigrams


def _clean_category_name(category_title: str, folder: str) -> str:
    raw = (category_title or folder or "").strip()
    raw = re.sub(r"BQB$", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"^\d+[_\s]*", "", raw)
    # 剥离表情符号与装饰符，保留中英文与数字
    cleaned = re.sub(r"[^\w\u4e00-\u9fff\s]", " ", raw)
    cleaned = re.sub(r"[_\s]+", " ", cleaned).strip()
    # 若前半段为纯英文前缀且后半段含中文（如 "Panda金馆长熊猫"、"Hat绿帽子"），保留中文主体与专有名词（如 JOJO）
    m = re.match(r"^[A-Za-z]+\s*([\u4e00-\u9fff].*)$", cleaned)
    if m and not cleaned.upper().startswith("JOJO"):
        cleaned = m.group(1).strip()
    return cleaned or raw


def _clean_item_label(raw_label: str, clean_category: str) -> Tuple[str, bool]:
    s = (raw_label or "").strip()
    if not s or _JUNK_LABEL_TOKENS_RE.match(s):
        return "", False

    # 剥离形如 "金馆长熊猫00191-这么刺激吗" 或 "00012_xxx" 的分类补零编号前缀
    stripped = re.sub(r"^.{0,24}?(?:0\d+|\d{3,6})[-_\s.]+", "", s).strip()
    if stripped and stripped != s:
        s = stripped
    else:
        # 形如 "斗图之王00002"（分类名后紧跟纯数字，无额外台词）
        if re.fullmatch(r".{0,24}?(?:0\d+|\d{2,6})", s):
            return "", False

    s = re.sub(r"[-_\s]+", " ", s).strip()
    if not s or s.isdigit() or _JUNK_LABEL_TOKENS_RE.match(s):
        return "", False

    if clean_category and s == clean_category:
        return "", False

    has_meaningful = bool(
        re.search(r"[\u4e00-\u9fff]{1,}|[A-Za-z]{3,}", s)
    )
    return (s if has_meaningful else ""), has_meaningful


def detect_image_format(data: bytes) -> Optional[Tuple[str, str]]:
    """根据文件头魔数检测钉钉 /media/upload 支持的图片格式，拒绝伪装 HTML 或不支持的编码。"""
    if not data or len(data) < 12:
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png", "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg", "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif", "image/gif"
    if data[:2] == b"BM":
        return "bmp", "image/bmp"
    return None


def normalize_dingtalk_media_id(media_id: str) -> str:
    """钉钉 Markdown 引用 media_id 必须恰好以单个 '@' 开头。"""
    cleaned = (media_id or "").strip()
    if not cleaned:
        return ""
    cleaned = cleaned.lstrip("@")
    return f"@{cleaned}" if cleaned else ""


def strip_bqb_tags_for_streaming(text: str) -> str:
    """流式阶段实时剥离完整 [[bqb:...]] 与末尾未闭合的 [[bqb:... 残片，避免卡片闪烁源码。"""
    if not text:
        return ""
    cleaned = _COMPLETE_TAG_RE.sub("", text)
    cleaned = _TRAILING_PARTIAL_TAG_RE.sub("", cleaned)
    return cleaned


def is_explicit_bqb_request(user_content: str) -> bool:
    """判断用户当轮消息是否明确讨要表情包/斗图（命中时绕过自发配图冷却）。"""
    text = (user_content or "").strip()
    if not text:
        return False
    if _IMAGE_DRAW_EXCLUSION_RE.search(text):
        return False
    lower = text.lower()
    if any(kw in lower for kw in _EXPLICIT_BQB_KEYWORDS):
        return True
    return bool(_EXPLICIT_BQB_PATTERN.search(text))


def _atomic_write_bytes(target_path: str, payload: bytes) -> None:
    parent = os.path.dirname(target_path)
    os.makedirs(parent, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
        os.replace(tmp_path, target_path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _atomic_write_json(target_path: str, obj: Any) -> None:
    raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    _atomic_write_bytes(target_path, raw)


class BQBPacer:
    """按会话控制自发配图密度：防紧邻背靠背连发 + 滑动窗口密度上限，显式求图直通。"""

    def __init__(self, max_sessions: int = 500, session_ttl: float = 86400.0):
        self._max_sessions = max_sessions
        self._session_ttl = session_ttl
        self._states: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def _cleanup_unlocked(self, now: float) -> None:
        stale = [
            k
            for k, v in self._states.items()
            if now - v.get("updated_at", 0.0) > self._session_ttl
        ]
        for k in stale:
            self._states.pop(k, None)
        if len(self._states) > self._max_sessions:
            oldest = sorted(
                self._states.items(), key=lambda item: item[1].get("updated_at", 0.0)
            )
            for k, _ in oldest[: len(self._states) - self._max_sessions]:
                self._states.pop(k, None)

    def _get_state_unlocked(self, session_key: str, now: float) -> Dict[str, Any]:
        self._cleanup_unlocked(now)
        window_size = max(1, cfg.BQB_WINDOW_SIZE)
        state = self._states.get(session_key)
        if state is None:
            state = {
                "last_spontaneous_at": 0.0,
                "last_turn_sent": False,
                "recent_turns": deque(maxlen=window_size),
                "updated_at": now,
            }
            self._states[session_key] = state
        elif state["recent_turns"].maxlen != window_size:
            state["recent_turns"] = deque(state["recent_turns"], maxlen=window_size)
        state["updated_at"] = now
        return state

    def should_allow(
        self, session_key: str, *, explicit: bool, now: Optional[float] = None
    ) -> bool:
        if explicit:
            return True
        ts = time.time() if now is None else now
        cooldown = max(0, cfg.BQB_BACK_TO_BACK_COOLDOWN_SECONDS)
        window_size = max(1, cfg.BQB_WINDOW_SIZE)
        max_in_window = max(1, cfg.BQB_WINDOW_MAX_COUNT)
        with self._lock:
            state = self._get_state_unlocked(session_key or "default", ts)
            if (
                state["last_turn_sent"]
                and cooldown > 0
                and (ts - state["last_spontaneous_at"]) < cooldown
            ):
                return False
            # 保证「含当前轮在内的连续 window_size 轮」总发送数 <= max_in_window，需检查前 window_size - 1 轮
            prior_count = window_size - 1
            prior_turns = list(state["recent_turns"])[-prior_count:] if prior_count > 0 else []
            if sum(1 for x in prior_turns if x) >= max_in_window:
                return False
            return True

    def record_turn(
        self,
        session_key: str,
        *,
        sent: bool,
        explicit: bool,
        now: Optional[float] = None,
    ) -> None:
        if explicit:
            return
        ts = time.time() if now is None else now
        with self._lock:
            state = self._get_state_unlocked(session_key or "default", ts)
            state["last_turn_sent"] = bool(sent)
            state["recent_turns"].append(bool(sent))
            if sent:
                state["last_spontaneous_at"] = ts

    def reset(self) -> None:
        with self._lock:
            self._states.clear()


_pacer = BQBPacer()
_recent_sent_ids: Dict[str, Deque[str]] = {}
_recent_lock = threading.Lock()
_RECENT_HISTORY_PER_SESSION = 15

# 若配置的 SOCKS 代理在当前运行环境不可达（如本地开发机无容器代理），短暂跳过代理直连避免每轮空等超时
_proxy_skip_until: float = 0.0


async def _http_fetch_bytes(
    url: str, *, max_bytes: int, timeout_seconds: float
) -> bytes:
    global _proxy_skip_until
    now = time.time()
    proxy = (cfg.HTTPX_PROXY or "").strip()
    attempts: List[Optional[str]] = []
    if proxy and now >= _proxy_skip_until:
        attempts.append(proxy)
    attempts.append(None)

    headers = {
        "User-Agent": "dingtalk-ai-bot/1.0 (+https://github.com/zhaoolee/ChineseBQB)",
        "Accept-Encoding": "gzip, deflate",
    }
    last_error: Optional[Exception] = None

    for chosen_proxy in attempts:
        connect_timeout = min(2.0 if chosen_proxy else 3.0, timeout_seconds)
        timeout_cfg = httpx.Timeout(timeout_seconds, connect=connect_timeout)
        client_kwargs: Dict[str, Any] = {
            "follow_redirects": True,
            "timeout": timeout_cfg,
            "headers": headers,
        }
        if chosen_proxy:
            client_kwargs["proxy"] = chosen_proxy
            client_kwargs["trust_env"] = False
        else:
            client_kwargs["trust_env"] = True

        try:
            chunks: List[bytes] = []
            total = 0
            async with httpx.AsyncClient(**client_kwargs) as client:
                async with client.stream("GET", url) as resp:
                    resp.raise_for_status()
                    content_len = resp.headers.get("Content-Length")
                    if content_len and content_len.isdigit() and int(content_len) > max_bytes:
                        raise ValueError(f"远端文件超过上限 ({content_len} > {max_bytes})")
                    async for chunk in resp.aiter_bytes(chunk_size=65536):
                        total += len(chunk)
                        if total > max_bytes:
                            raise ValueError(f"流式读取超过上限 ({total} > {max_bytes})")
                        chunks.append(chunk)
            return b"".join(chunks)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_error = exc
            if chosen_proxy:
                _proxy_skip_until = time.time() + 60.0
                continue
            break

    raise RuntimeError(f"拉取失败 ({url}): {last_error}")


def parse_raw_catalog(
    raw_items: List[Dict[str, Any]], *, max_image_bytes: Optional[int] = None
) -> List[BQBEntry]:
    """清洗 search.json 条目，剔除不支持格式与非法路径并预计算中文检索特征。"""
    limit_bytes = cfg.BQB_MAX_IMAGE_BYTES if max_image_bytes is None else max_image_bytes
    entries: List[BQBEntry] = []
    seen_sha: set[str] = set()

    for item in raw_items:
        if not isinstance(item, dict):
            continue
        src = str(item.get("src") or "").strip()
        m = _SAFE_SRC_RE.match(src)
        if not m:
            continue
        sha256 = m.group(1).lower()
        ext = m.group(2).lower()
        if ext == "jpeg":
            ext = "jpg"
        if sha256 in seen_sha:
            continue

        size_val = item.get("bytes")
        bytes_size = int(size_val) if isinstance(size_val, (int, float)) else 0
        if bytes_size < 0 or (bytes_size > limit_bytes):
            continue

        seen_sha.add(sha256)
        cat_title = str(item.get("categoryTitle") or "").strip()
        folder = str(item.get("folder") or "").strip()
        raw_label = str(item.get("label") or Path(str(item.get("name") or "")).stem).strip()

        clean_cat = _clean_category_name(cat_title, folder)
        clean_lbl, has_sem = _clean_item_label(raw_label, clean_cat)
        cjk_chars, cjk_bigrams = _cjk_features(f"{clean_cat} {clean_lbl}")

        entries.append(
            BQBEntry(
                sha256=sha256,
                ext=ext,
                src=src,
                raw_label=raw_label,
                clean_label=clean_lbl,
                clean_label_lower=clean_lbl.lower(),
                category_title=cat_title or clean_cat,
                clean_category=clean_cat,
                clean_category_lower=clean_cat.lower(),
                animated=bool(item.get("animated", ext == "gif")),
                bytes_size=bytes_size,
                has_semantic=has_sem,
                cjk_chars=cjk_chars,
                cjk_bigrams=cjk_bigrams,
            )
        )
    return entries


class BQBIndexManager:
    """维护内存索引与本地持久化副本，支持过期后台单飞刷新。"""

    def __init__(self) -> None:
        self._entries: List[BQBEntry] = []
        self._loaded_from_path: str = ""
        self._loaded_mtime: float = 0.0
        self._refresh_lock: Optional[asyncio.Lock] = None
        self._refresh_task: Optional[asyncio.Task] = None

    def _index_file_path(self) -> str:
        return os.path.join(BQB_DATA_DIR, "search_index.json")

    def _get_lock(self) -> asyncio.Lock:
        if self._refresh_lock is None:
            self._refresh_lock = asyncio.Lock()
        return self._refresh_lock

    def _load_disk_if_needed(self, path: str) -> bool:
        if not os.path.isfile(path):
            return False
        try:
            mtime = os.path.getmtime(path)
            if self._entries and self._loaded_from_path == path and mtime == self._loaded_mtime:
                return True
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, list):
                return False
            parsed = parse_raw_catalog(raw)
            if not parsed:
                return False
            self._entries = parsed
            self._loaded_from_path = path
            self._loaded_mtime = mtime
            return True
        except Exception as exc:
            print(f"⚠️ [BQB] 读取本地索引失败: {exc}")
            return False

    async def refresh_online(self, *, timeout_seconds: float = 8.0) -> bool:
        async with self._get_lock():
            path = self._index_file_path()
            try:
                raw_bytes = await _http_fetch_bytes(
                    cfg.BQB_INDEX_URL,
                    max_bytes=12 * 1024 * 1024,
                    timeout_seconds=timeout_seconds,
                )
                data = json.loads(raw_bytes.decode("utf-8"))
                if not isinstance(data, list):
                    raise ValueError("search.json 顶层不是数组")
                parsed = parse_raw_catalog(data)
                if not parsed:
                    raise ValueError("清洗后无有效表情包条目")
                _atomic_write_bytes(path, raw_bytes)
                self._entries = parsed
                self._loaded_from_path = path
                self._loaded_mtime = os.path.getmtime(path)
                print(f"🎭 [BQB] 索引已同步: {len(parsed)} 张可用表情包")
                return True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"⚠️ [BQB] 在线同步索引失败: {exc}")
                return False

    def _schedule_background_refresh(self) -> None:
        if self._refresh_task is not None and not self._refresh_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
            self._refresh_task = loop.create_task(self.refresh_online(timeout_seconds=10.0))
        except RuntimeError:
            pass

    async def get_entries(self, *, timeout_seconds: float = 3.0) -> List[BQBEntry]:
        path = self._index_file_path()
        has_local = self._load_disk_if_needed(path)
        if has_local:
            age = time.time() - self._loaded_mtime
            if age > cfg.BQB_INDEX_TTL_SECONDS:
                self._schedule_background_refresh()
            return self._entries

        await self.refresh_online(timeout_seconds=timeout_seconds)
        return self._entries

    def reset_for_test(self) -> None:
        self._entries = []
        self._loaded_from_path = ""
        self._loaded_mtime = 0.0
        self._refresh_lock = None
        self._refresh_task = None


_index_manager = BQBIndexManager()


def schedule_bqb_index_warmup() -> None:
    """首次对话时非阻塞预热索引（若本地已存在且未过期则零网络开销）。"""
    if not cfg.ENABLE_BQB:
        return
    path = os.path.join(BQB_DATA_DIR, "search_index.json")
    if os.path.isfile(path):
        age = time.time() - os.path.getmtime(path)
        if age <= cfg.BQB_INDEX_TTL_SECONDS:
            return
    _index_manager._schedule_background_refresh()


def _record_recent_sent(session_key: str, sha256: str) -> None:
    key = session_key or "default"
    with _recent_lock:
        if len(_recent_sent_ids) > 500 and key not in _recent_sent_ids:
            oldest_key = next(iter(_recent_sent_ids))
            _recent_sent_ids.pop(oldest_key, None)
        dq = _recent_sent_ids.get(key)
        if dq is None:
            dq = deque(maxlen=_RECENT_HISTORY_PER_SESSION)
            _recent_sent_ids[key] = dq
        dq.append(sha256)


def _get_recent_sent(session_key: str) -> set[str]:
    with _recent_lock:
        dq = _recent_sent_ids.get(session_key or "default")
        return set(dq) if dq else set()


def search_entries(
    query: str,
    entries: List[BQBEntry],
    *,
    session_key: str = "",
    rng: Optional[random.Random] = None,
) -> Optional[BQBEntry]:
    """在内存索引中执行多级语义/分类/同义词打分，并结合会话近期去重加权抽样。"""
    if not entries:
        return None
    raw_q = (query or "").strip()
    if not raw_q:
        return None

    rand = rng or random
    recent_ids = _get_recent_sent(session_key)

    tokens = [t.strip() for t in _SPLIT_QUERY_RE.split(raw_q) if t.strip()]
    if not tokens:
        return None

    # 纯通配随机请求（如 [[bqb:随机]] / [[bqb:斗图]]）直接从有台词标注的精品池抽样
    if all(t.lower() in _WILDCARD_TOKENS for t in tokens):
        pool = [e for e in entries if e.has_semantic and e.sha256 not in recent_ids]
        if not pool:
            pool = [e for e in entries if e.sha256 not in recent_ids] or entries
        return rand.choice(pool)

    # 筛除无意义修饰词（保留有区分度的查询词）
    effective_tokens = [
        t for t in tokens if t.lower() not in {"表情包", "表情", "梗图", "图片", "来张", "发张"}
    ] or tokens

    expanded_categories: List[str] = []
    expanded_moods: List[str] = []
    for tok in effective_tokens:
        tok_low = tok.lower()
        for alias_key, targets in _CATEGORY_ALIASES.items():
            if alias_key == tok_low or (len(alias_key) >= 2 and alias_key in tok_low):
                expanded_categories.extend(x.lower() for x in targets)
        for mood_key, targets in _MOOD_SYNONYMS.items():
            if mood_key == tok_low or (len(mood_key) >= 2 and mood_key in tok_low):
                expanded_moods.extend(x.lower() for x in targets)

    q_chars, q_bigrams = _cjk_features("".join(effective_tokens))
    scored: List[Tuple[float, BQBEntry]] = []

    for entry in entries:
        score = 0.0
        for tok in effective_tokens:
            tok_low = tok.lower()
            if not tok_low:
                continue
            if entry.clean_label_lower and tok_low in entry.clean_label_lower:
                score += 10.0 if len(tok_low) >= 2 else 6.0
            if entry.clean_category_lower and (
                tok_low in entry.clean_category_lower or entry.clean_category_lower in tok_low
            ):
                score += 6.5

        for cat_kw in expanded_categories:
            if cat_kw and cat_kw in entry.clean_category_lower:
                score += 6.0
                break

        for mood_kw in expanded_moods:
            if mood_kw and entry.clean_label_lower and mood_kw in entry.clean_label_lower:
                score += 5.0
                break

        if q_bigrams and entry.cjk_bigrams:
            overlap = len(q_bigrams & entry.cjk_bigrams) / len(q_bigrams)
            score += 3.5 * overlap
        elif len(q_chars) >= 2 and entry.cjk_chars:
            char_overlap = len(q_chars & entry.cjk_chars) / len(q_chars)
            if char_overlap >= 0.5:
                score += 2.5 * char_overlap

        if score >= 3.0:
            if entry.has_semantic:
                score += 0.6
            if entry.animated:
                score += 0.25
            scored.append((score, entry))

    if not scored:
        return None

    scored.sort(key=lambda pair: pair[0], reverse=True)
    best_score = scored[0][0]
    cutoff_score = max(3.0, best_score * 0.72)
    top_candidates = [pair for pair in scored[:16] if pair[0] >= cutoff_score]

    fresh_candidates = [pair for pair in top_candidates if pair[1].sha256 not in recent_ids]
    chosen_pool = fresh_candidates if fresh_candidates else top_candidates

    weights = [pair[0] for pair in chosen_pool]
    picked = rand.choices([pair[1] for pair in chosen_pool], weights=weights, k=1)[0]
    return picked


def _prune_media_cache(media_dir: str, max_bytes: int) -> None:
    try:
        files = []
        total = 0
        for name in os.listdir(media_dir):
            full = os.path.join(media_dir, name)
            if not os.path.isfile(full) or name.endswith(".tmp"):
                continue
            st = os.stat(full)
            total += st.st_size
            files.append((st.st_mtime, st.st_size, full))
        if total <= max_bytes:
            return
        files.sort(key=lambda x: x[0])
        for _, sz, full in files:
            try:
                os.remove(full)
                total -= sz
            except OSError:
                pass
            if total <= max_bytes * 0.85:
                break
    except OSError:
        pass


async def fetch_bqb_image_bytes(
    entry: BQBEntry, *, timeout_seconds: Optional[float] = None
) -> Tuple[bytes, str, str]:
    """读取本地原图缓存或从远端下载，校验魔数并执行 LRU 容量治理。"""
    media_dir = os.path.join(BQB_DATA_DIR, "media")
    os.makedirs(media_dir, exist_ok=True)
    cached_path = os.path.join(media_dir, f"{entry.sha256}.{entry.ext}")

    if os.path.isfile(cached_path):
        try:
            with open(cached_path, "rb") as f:
                data = f.read()
            fmt = detect_image_format(data)
            if fmt is not None:
                now = time.time()
                try:
                    os.utime(cached_path, (now, now))
                except OSError:
                    pass
                return data, fmt[0], fmt[1]
        except OSError:
            pass

    if not _SAFE_SRC_RE.match(entry.src):
        raise ValueError(f"非法图片路径: {entry.src}")

    url = f"{cfg.BQB_BASE_URL.rstrip('/')}/{entry.src.lstrip('/')}"
    timeout = cfg.BQB_FETCH_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    data = await _http_fetch_bytes(
        url,
        max_bytes=cfg.BQB_MAX_IMAGE_BYTES,
        timeout_seconds=timeout,
    )
    fmt = detect_image_format(data)
    if fmt is None:
        raise ValueError("下载内容不是合法的 PNG/JPEG/GIF/BMP 图片")

    actual_ext, mime_type = fmt
    final_path = os.path.join(media_dir, f"{entry.sha256}.{actual_ext}")
    try:
        _atomic_write_bytes(final_path, data)
        _prune_media_cache(media_dir, cfg.BQB_CACHE_MAX_MB * 1024 * 1024)
    except OSError as exc:
        # 磁盘满、只读卷或 Windows 并发句柄竞争时不阻断当轮内存字节上传
        print(f"⚠️ [BQB] 写入本地图片缓存失败，降级内存直传: {exc}")
    return data, actual_ext, mime_type


_media_id_mem_cache: Dict[str, Dict[str, Dict[str, Any]]] = {}
_media_id_lock = threading.Lock()


def _safe_client_scope(card_helper: Any) -> str:
    raw = str(getattr(card_helper, "client_id", "") or cfg.DINGTALK_CLIENT_ID or cfg.BOT_ID or "default")
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return digest


def _media_id_cache_file(scope: str) -> str:
    return os.path.join(BQB_DATA_DIR, f"media_ids_{scope}.json")


def _get_cached_media_id(scope: str, sha256: str, now: float) -> Optional[str]:
    ttl = cfg.BQB_MEDIA_ID_TTL_SECONDS
    with _media_id_lock:
        bucket = _media_id_mem_cache.get(scope)
        if bucket is None:
            path = _media_id_cache_file(scope)
            bucket = {}
            if os.path.isfile(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        loaded = json.load(f)
                    if isinstance(loaded, dict):
                        bucket = loaded
                except Exception:
                    bucket = {}
            _media_id_mem_cache[scope] = bucket

        rec = bucket.get(sha256)
        if not isinstance(rec, dict):
            return None
        media_id = normalize_dingtalk_media_id(str(rec.get("media_id") or ""))
        uploaded_at = float(rec.get("uploaded_at") or 0.0)
        if not media_id or (now - uploaded_at) >= ttl:
            bucket.pop(sha256, None)
            return None
        return media_id


def _save_cached_media_id(scope: str, sha256: str, media_id: str, now: float) -> None:
    ttl = cfg.BQB_MEDIA_ID_TTL_SECONDS
    with _media_id_lock:
        bucket = _media_id_mem_cache.setdefault(scope, {})
        expired = [
            k
            for k, v in bucket.items()
            if not isinstance(v, dict) or (now - float(v.get("uploaded_at") or 0.0)) >= ttl
        ]
        for k in expired:
            bucket.pop(k, None)
        bucket[sha256] = {"media_id": media_id, "uploaded_at": now}
        snapshot = dict(bucket)
    try:
        _atomic_write_json(_media_id_cache_file(scope), snapshot)
    except Exception as exc:
        print(f"⚠️ [BQB] 写入 media_id 缓存失败: {exc}")


def resize_bqb_for_display(
    data: bytes,
    ext: str,
    mime_type: str,
    max_px: int,
    canvas_width_px: int = 0,
) -> Tuple[bytes, str, str]:
    """将表情包主体限制在 max_px 内，并按需左对齐贴入透明基准画布以阻止钉钉卡片 100% 拉伸放大。"""
    if max_px <= 0 or not data:
        return data, ext, mime_type

    try:
        from PIL import Image, ImageOps, ImageSequence
    except ImportError:
        return data, ext, mime_type

    try:
        with Image.open(io.BytesIO(data)) as im:
            w, h = im.size
            if w <= 0 or h <= 0:
                return data, ext, mime_type

            if max(w, h) > max_px:
                scale = max_px / float(max(w, h))
                new_w = max(1, round(w * scale))
                new_h = max(1, round(h * scale))
            else:
                new_w, new_h = w, h

            target_canvas_w = max(new_w, canvas_width_px) if canvas_width_px > 0 else new_w
            need_pad = target_canvas_w > new_w
            if new_w == w and new_h == h and not need_pad:
                return data, ext, mime_type

            resample = Image.Resampling.LANCZOS
            is_anim = bool(getattr(im, "is_animated", False)) and int(getattr(im, "n_frames", 1)) > 1

            if is_anim:
                frames_out: List[Any] = []
                durations: List[int] = []
                default_dur = int(im.info.get("duration") or 80)
                loop_val = int(im.info.get("loop", 0))
                any_transparent = False

                for idx, frame in enumerate(ImageSequence.Iterator(im)):
                    if idx >= 120:
                        break
                    fg = frame.convert("RGBA")
                    if (new_w, new_h) != fg.size:
                        fg = fg.resize((new_w, new_h), resample)

                    if need_pad:
                        # 不传 mask 直接原样拷贝 RGBA 四通道，避免 Pillow paste 按非预乘通道混合把半透明边缘染黑
                        canvas = Image.new("RGBA", (target_canvas_w, new_h), (255, 255, 255, 0))
                        canvas.paste(fg, (0, 0))
                    else:
                        canvas = fg

                    alpha = canvas.getchannel("A")
                    min_alpha, _ = alpha.getextrema()
                    if min_alpha < 255:
                        any_transparent = True
                        white_bg = Image.new("RGBA", canvas.size, (255, 255, 255, 255))
                        composited_rgb = Image.alpha_composite(white_bg, canvas).convert("RGB")
                        rgb_q = composited_rgb.quantize(
                            colors=255, method=Image.Quantize.FASTOCTREE
                        )
                        pal = rgb_q.getpalette() or []
                        if len(pal) < 768:
                            pal = pal + [255] * (768 - len(pal))
                        pal[255 * 3 : 255 * 3 + 3] = [255, 255, 255]
                        rgb_q.putpalette(pal[:768])
                        mask = Image.eval(alpha, lambda a: 255 if a < 128 else 0)
                        rgb_q.paste(255, mask)
                        rgb_q.info["transparency"] = 255
                        frames_out.append(rgb_q)
                    else:
                        rgb_q = canvas.convert("RGB").quantize(
                            colors=256, method=Image.Quantize.MEDIANCUT
                        )
                        frames_out.append(rgb_q)

                    dur = int(frame.info.get("duration") or default_dur or 80)
                    durations.append(max(20, dur))

                if len(frames_out) > 1:
                    out_buf = io.BytesIO()
                    save_kwargs: Dict[str, Any] = {
                        "format": "GIF",
                        "save_all": True,
                        "append_images": frames_out[1:],
                        "duration": durations,
                        "loop": loop_val,
                        "disposal": 2,
                    }
                    if any_transparent:
                        save_kwargs["transparency"] = 255
                    frames_out[0].save(out_buf, **save_kwargs)
                    return out_buf.getvalue(), "gif", "image/gif"

            transposed = ImageOps.exif_transpose(im)
            has_alpha = need_pad or transposed.mode in ("RGBA", "LA") or (
                transposed.mode == "P" and "transparency" in transposed.info
            )
            out_buf = io.BytesIO()
            if has_alpha:
                fg = transposed.convert("RGBA")
                if (new_w, new_h) != fg.size:
                    fg = fg.resize((new_w, new_h), resample)
                if need_pad:
                    canvas = Image.new("RGBA", (target_canvas_w, new_h), (255, 255, 255, 0))
                    canvas.paste(fg, (0, 0))
                else:
                    canvas = fg
                canvas.save(out_buf, format="PNG", optimize=True)
                return out_buf.getvalue(), "png", "image/png"

            resized = transposed.convert("RGB").resize((new_w, new_h), resample)
            resized.save(out_buf, format="JPEG", quality=88, optimize=True)
            return out_buf.getvalue(), "jpg", "image/jpeg"
    except Exception as exc:
        print(f"⚠️ [BQB] 缩放表情包失败，回退原图上传: {exc}")
        return data, ext, mime_type


async def get_or_upload_media_id(
    entry: BQBEntry,
    card_helper: Any,
    *,
    timeout_seconds: Optional[float] = None,
) -> Optional[str]:
    """返回钉钉 Markdown 可直接引用的 @media_id，命中 48h 缓存则零上传开销。"""
    scope = _safe_client_scope(card_helper)
    max_px = max(0, cfg.BQB_MAX_DISPLAY_PX)
    canvas_w = max(0, cfg.BQB_CARD_CANVAS_WIDTH_PX)
    cache_key = (
        f"{entry.sha256}@px{max_px}w{canvas_w}" if max_px > 0 else entry.sha256
    )
    now = time.time()
    cached = _get_cached_media_id(scope, cache_key, now)
    if cached:
        return cached

    img_bytes, ext, mime_type = await fetch_bqb_image_bytes(
        entry, timeout_seconds=timeout_seconds
    )
    img_bytes, ext, mime_type = resize_bqb_for_display(
        img_bytes, ext, mime_type, max_px, canvas_width_px=canvas_w
    )
    raw_media_id = await card_helper.upload_media(
        img_bytes,
        filetype="image",
        filename=f"bqb_{entry.sha256[:12]}.{ext}",
        mimetype=mime_type,
    )
    normalized = normalize_dingtalk_media_id(raw_media_id or "")
    if not normalized:
        return None
    _save_cached_media_id(scope, cache_key, normalized, time.time())
    return normalized


def _collapse_blank_lines(text: str) -> str:
    cleaned = re.sub(r"[ \t]+\n", "\n", text)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _format_alt_text(entry: BQBEntry) -> str:
    raw = entry.clean_label or entry.clean_category or "表情包"
    # 剔除 Markdown 图片 alt 敏感字符 '[' ']' 与换行
    safe = re.sub(r"[\[\]\r\n]+", " ", raw).strip()
    return safe[:24] or "表情包"


def _format_history_tag(entry: BQBEntry) -> str:
    parts = [p for p in (entry.clean_category, entry.clean_label) if p]
    summary = " ".join(parts).strip() or "表情包"
    summary = re.sub(r"[\[\]\r\n]+", " ", summary)[:36].strip()
    return f"[[bqb:{summary}]]"


async def resolve_bqb_tags(
    response_text: str,
    *,
    user_content: str = "",
    card_helper: Any,
    session_key: str = "",
    now_fn: Callable[[], float] = time.time,
) -> BQBResolveResult:
    """解析模型输出中的 [[bqb:关键词]] 标签，结合节奏闸门返回卡片渲染文本与防污染历史文本。"""
    src = response_text or ""
    explicit = is_explicit_bqb_request(user_content)
    matches = list(_COMPLETE_TAG_RE.finditer(src))

    if not matches:
        clean = _TRAILING_PARTIAL_TAG_RE.sub("", src).strip()
        _pacer.record_turn(session_key, sent=False, explicit=explicit, now=now_fn())
        return BQBResolveResult(card_text=clean, history_text=clean, sent_count=0)

    if not cfg.ENABLE_BQB or card_helper is None:
        stripped = _collapse_blank_lines(strip_bqb_tags_for_streaming(src))
        return BQBResolveResult(card_text=stripped, history_text=stripped, sent_count=0)

    now_ts = now_fn()
    if not _pacer.should_allow(session_key, explicit=explicit, now=now_ts):
        stripped = _collapse_blank_lines(strip_bqb_tags_for_streaming(src))
        _pacer.record_turn(session_key, sent=False, explicit=explicit, now=now_ts)
        print(f"⏳ [BQB] 触发自发配图节流，本轮仅发送文字 (session={session_key[-12:]})")
        return BQBResolveResult(
            card_text=stripped,
            history_text=stripped,
            sent_count=0,
            throttled=True,
        )

    max_tags = max(1, cfg.BQB_MAX_PER_REPLY)
    timeout_total = cfg.BQB_FETCH_TIMEOUT_SECONDS

    async def _resolve_selected() -> List[Optional[Tuple[BQBEntry, str]]]:
        entries = await _index_manager.get_entries(timeout_seconds=min(3.0, timeout_total))
        if not entries:
            return [None] * min(len(matches), max_tags)
        results: List[Optional[Tuple[BQBEntry, str]]] = []
        for m in matches[:max_tags]:
            query = m.group(1).strip()
            entry = search_entries(query, entries, session_key=session_key)
            if entry is None:
                print(f"🔍 [BQB] 未命中匹配表情包: query='{query}'")
                results.append(None)
                continue
            media_id = await get_or_upload_media_id(
                entry, card_helper, timeout_seconds=timeout_total
            )
            if not media_id:
                results.append(None)
                continue
            _record_recent_sent(session_key, entry.sha256)
            print(
                f"🎭 [BQB] 命中并就绪: query='{query}' -> {entry.clean_category}/{entry.clean_label or entry.raw_label} ({media_id[:12]}...)"
            )
            results.append((entry, media_id))
        return results

    try:
        resolved_list = await asyncio.wait_for(_resolve_selected(), timeout=timeout_total)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        print(f"⚠️ [BQB] 解析或上传超时/失败，静默降级纯文本: {exc}")
        resolved_list = [None] * min(len(matches), max_tags)

    card_parts: List[str] = []
    hist_parts: List[str] = []
    cursor = 0
    sent_count = 0

    for idx, m in enumerate(matches):
        card_parts.append(src[cursor : m.start()])
        hist_parts.append(src[cursor : m.start()])
        cursor = m.end()

        resolved = resolved_list[idx] if idx < len(resolved_list) else None
        if resolved is not None:
            entry, media_id = resolved
            alt = _format_alt_text(entry)
            card_parts.append(f"\n\n![{alt}]({media_id})\n\n")
            hist_parts.append(f" {_format_history_tag(entry)} ")
            sent_count += 1

    tail = _TRAILING_PARTIAL_TAG_RE.sub("", src[cursor:])
    card_parts.append(tail)
    hist_parts.append(tail)

    card_out = _collapse_blank_lines("".join(card_parts))
    hist_out = _collapse_blank_lines("".join(hist_parts))

    _pacer.record_turn(
        session_key,
        sent=(sent_count > 0),
        explicit=explicit,
        now=now_fn(),
    )
    return BQBResolveResult(
        card_text=card_out,
        history_text=hist_out,
        sent_count=sent_count,
    )
