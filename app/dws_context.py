# -*- coding: utf-8 -*-
"""DingTalk Workspace CLI (dws) 严格只读上下文与引用消息解析模块。

安全红线：
1. 本模块绝不暴露给大模型工具调用（MCP / Function Calling），仅由服务端内部在接收消息前拉取只读上下文。
2. 所有子进程调用必须先通过 validate_readonly_dws_args 正向白名单校验，且一律使用
   asyncio.create_subprocess_exec（禁用 shell=True），从根源阻断发送、回复、撤回、改群、上传等写操作。
3. 企业归属校验严格遵循 fail-closed：缺少期望 CorpId、auth status 超时/异常或企业不一致时，一律阻断后续读取。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Collection, Sequence

from app import config as cfg
from app.ai.history_format import get_bot_source_name
from app.dws_files import QuotedDocExtract, fetch_quoted_media_and_docs

_SHANGHAI_TZ = timezone(timedelta(hours=8))

# 正向只读子命令与参数白名单：任何未在此声明的子命令或参数一律拒绝执行
_READONLY_COMMAND_SPECS: dict[tuple[str, ...], dict[str, set[str]]] = {
    ("auth", "status"): {
        "value_flags": {"-f", "--format"},
        "bool_flags": {"--readonly"},
        "required_bool_flags": set(),
        "required_value_flags": set(),
    },
    ("chat", "message", "list"): {
        "value_flags": {
            "--conversation-id",
            "--limit",
            "--time",
            "--direction",
            "-f",
            "--format",
        },
        "bool_flags": set(),
        "required_bool_flags": set(),
        "required_value_flags": set(),
    },
    ("chat", "+messages-mget"): {
        "value_flags": {"--msg-ids", "-f", "--format"},
        "bool_flags": {"--no-reactions", "--no-threads"},
        "required_bool_flags": {"--no-reactions", "--no-threads"},
        "required_value_flags": {"--msg-ids"},
    },
    ("chat", "message", "download-media"): {
        "value_flags": {
            "--conversation-id",
            "--message-id",
            "--resource-id",
            "--type",
            "--output",
            "-f",
            "--format",
        },
        "bool_flags": set(),
        "required_bool_flags": set(),
        "required_value_flags": {
            "--conversation-id",
            "--message-id",
            "--resource-id",
            "--type",
            "--output",
        },
    },
    ("doc", "info"): {
        "value_flags": {"--node", "-f", "--format"},
        "bool_flags": set(),
        "required_bool_flags": set(),
        "required_value_flags": {"--node"},
    },
    ("doc", "read"): {
        "value_flags": {"--node", "-f", "--format"},
        "bool_flags": set(),
        "required_bool_flags": set(),
        "required_value_flags": {"--node"},
    },
    ("drive", "download"): {
        "value_flags": {"--node", "-f", "--format"},
        "bool_flags": {"--url-only"},
        "required_bool_flags": {"--url-only"},
        "required_value_flags": {"--node"},
    },
}

_NOISE_TEXTS = frozenset(
    {
        "[加密消息，无法解码]",
        "Thinking...",
        "思考中...",
        "[AILoading]",
        "[卡片消息]",
        "[互动卡片]",
    }
)

_PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)

_AT_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9._%+-])@([^\s@]+)")

_backoff_until: float = 0.0
_backoff_keychain_marker: tuple[tuple[str, int, int], ...] | None = None
_cached_identity: DwsAuthIdentity | None = None
_cached_identity_until: float = 0.0
_cached_identity_marker: tuple[tuple[str, int, int], ...] | None = None

_AUTH_CONFIG_WATCH_FILES = ("profiles.json", "token.json", "identity.json")


@dataclass(frozen=True)
class DwsAuthIdentity:
    """dws 当前登录账号的企业与用户摘要（不含任何 token/密钥）。"""

    authenticated: bool = False
    token_valid: bool = False
    refresh_token_valid: bool = False
    corp_id: str = ""
    corp_name: str = ""
    user_id: str = ""
    user_name: str = ""


@dataclass(frozen=True)
class DwsCorpCheckResult:
    """企业一致性门禁结果（fail-closed：仅 allowed=True 时允许执行后续 dws 读取）。"""

    allowed: bool = False
    corp_mismatch_error: str = ""
    identity: DwsAuthIdentity | None = None


@dataclass
class DwsQuoteInfo:
    """被引用消息的结构化摘要。"""

    message_id: str = ""
    conversation_id: str = ""
    sender: str = ""
    sender_id: str = ""
    text: str = ""
    create_time: str = ""
    message_type: str = ""
    resources: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True, order=True)
class QuoteTimelineRow:
    """引用时间线排序行（消除裸下标元组操作）。"""

    timestamp: str
    role_rank: int
    seq: int
    message_id: str = field(compare=False, default="")
    sender: str = field(compare=False, default="")
    text: str = field(compare=False, default="")


@dataclass
class DwsContextBundle:
    """单次请求由 dws 与 Stream 解析出的群聊历史、引用上下文、媒体文档与还原文本包。"""

    dws_available: bool = False
    merged_history: list[dict[str, Any]] = field(default_factory=list)
    persistable_messages: list[dict[str, Any]] = field(default_factory=list)
    restored_user_content: str = ""
    quote_info: DwsQuoteInfo | None = None
    quotes: list[DwsQuoteInfo] = field(default_factory=list)
    quoted_images: list[bytes] = field(default_factory=list)
    quoted_documents: list[QuotedDocExtract] = field(default_factory=list)
    quote_prompt_block: str = ""
    quote_router_text: str = ""
    quote_history_prefix: str = ""
    corp_mismatch_error: str = ""


def reset_dws_RuntimeState_for_tests() -> None:
    """重置进程内负缓存与企业身份缓存状态（仅供单测隔离使用）。"""
    global _backoff_until, _backoff_keychain_marker
    global _cached_identity, _cached_identity_until, _cached_identity_marker
    _backoff_until = 0.0
    _backoff_keychain_marker = None
    _cached_identity = None
    _cached_identity_until = 0.0
    _cached_identity_marker = None


def _get_keychain_marker() -> tuple[tuple[str, int, int], ...]:
    """仅读取凭据与 profile 目录文件元数据，在登录/切企业后毫秒级失效旧缓存。"""
    entries: list[tuple[str, int, int]] = []

    keychain_dir = cfg.get_dws_keychain_dir()
    if keychain_dir and os.path.isdir(keychain_dir):
        try:
            with os.scandir(keychain_dir) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            with os.scandir(entry.path) as sub_it:
                                for sub_entry in sub_it:
                                    try:
                                        st = sub_entry.stat()
                                        entries.append(
                                            (
                                                f"k:{entry.name}/{sub_entry.name}",
                                                int(st.st_mtime_ns),
                                                int(st.st_size),
                                            )
                                        )
                                    except OSError:
                                        continue
                        else:
                            st = entry.stat()
                            entries.append((f"k:{entry.name}", int(st.st_mtime_ns), int(st.st_size)))
                    except OSError:
                        continue
        except OSError:
            pass

    config_dir = cfg.get_dws_config_dir()
    if config_dir and os.path.isdir(config_dir):
        for fname in _AUTH_CONFIG_WATCH_FILES:
            fpath = os.path.join(config_dir, fname)
            try:
                st = os.stat(fpath)
                entries.append((f"c:{fname}", int(st.st_mtime_ns), int(st.st_size)))
            except OSError:
                continue

    entries.sort()
    return tuple(entries)


def parse_dws_auth_status(payload: Any) -> DwsAuthIdentity | None:
    """解析 dws auth status --readonly -f json 的企业与登录态信息。"""
    if not isinstance(payload, dict):
        return None
    if payload.get("ok") is False:
        return DwsAuthIdentity(authenticated=False)

    src = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    return DwsAuthIdentity(
        authenticated=bool(src.get("authenticated")),
        token_valid=bool(src.get("token_valid", src.get("tokenValid", False))),
        refresh_token_valid=bool(
            src.get("refresh_token_valid", src.get("refreshTokenValid", False))
        ),
        corp_id=str(src.get("corp_id") or src.get("corpId") or "").strip(),
        corp_name=str(src.get("corp_name") or src.get("corpName") or "").strip(),
        user_id=str(src.get("user_id") or src.get("userId") or "").strip(),
        user_name=str(src.get("user_name") or src.get("userName") or "").strip(),
    )


async def get_dws_auth_identity(*, force_refresh: bool = False) -> DwsAuthIdentity | None:
    """只读查询当前 dws 登录企业身份；凭据文件未变化时复用内存缓存。"""
    global _cached_identity, _cached_identity_until, _cached_identity_marker

    now = time.monotonic()
    current_marker = _get_keychain_marker()
    if (
        not force_refresh
        and _cached_identity is not None
        and now < _cached_identity_until
        and _cached_identity_marker == current_marker
    ):
        return _cached_identity

    payload = await run_dws_readonly_json(["auth", "status", "--readonly", "-f", "json"])
    if payload is None:
        return None

    identity = parse_dws_auth_status(payload)
    if identity is not None:
        _cached_identity = identity
        _cached_identity_marker = _get_keychain_marker()
        _cached_identity_until = time.monotonic() + cfg.DWS_AUTH_STATUS_TTL_SECONDS
    return identity


async def check_dws_corp_access(*, expected_corp_ids: Sequence[str]) -> DwsCorpCheckResult:
    """执行 fail-closed 企业归属校验：仅当已登录且企业精确一致时返回 allowed=True。"""
    clean_expected: list[str] = []
    for raw_cid in expected_corp_ids:
        cid = str(raw_cid or "").strip()
        if cid and cid not in clean_expected:
            clean_expected.append(cid)

    if not clean_expected:
        print("⚠️ [dws] 缺少机器人归属企业标识 (chatbot_corp_id / DWS_EXPECTED_CORP_ID)，按 fail-closed 跳过读取")
        return DwsCorpCheckResult(allowed=False, corp_mismatch_error="")

    identity = await get_dws_auth_identity()
    if identity is None:
        return DwsCorpCheckResult(allowed=False, corp_mismatch_error="")

    has_login = bool(
        identity.authenticated
        or identity.token_valid
        or identity.refresh_token_valid
        or identity.corp_id
    )
    if not has_login:
        return DwsCorpCheckResult(allowed=False, corp_mismatch_error="", identity=identity)

    if identity.corp_id and all(identity.corp_id == exp for exp in clean_expected):
        return DwsCorpCheckResult(allowed=True, corp_mismatch_error="", identity=identity)

    dws_corp_label = (
        f"{identity.corp_name} (`{identity.corp_id}`)"
        if identity.corp_name and identity.corp_id
        else f"`{identity.corp_id or '未知企业'}`"
    )
    bot_corp_label = " / ".join(f"`{cid}`" for cid in clean_expected)
    err_msg = (
        "❌ **dws 登录企业与当前机器人所在企业不一致**\n\n"
        f"- **dws 当前登录企业**：{dws_corp_label}\n"
        f"- **机器人所在企业**：{bot_corp_label}\n\n"
        "为防止跨企业串读数据，已拒绝拉取群聊上下文与引用消息。\n"
        "请管理员在服务器重新执行设备码登录，并选择机器人所在企业授权：\n"
        "`docker exec -it dingtalk-ai-bot-gemini dws auth login --device`"
    )
    print(
        f"❌ [dws] 登录企业与机器人所在企业不一致: "
        f"dws={identity.corp_name}({identity.corp_id}) != bot={clean_expected}，已阻断读取并报错"
    )
    return DwsCorpCheckResult(allowed=False, corp_mismatch_error=err_msg, identity=identity)


async def verify_dws_corp_match(*, expected_corp_ids: Sequence[str]) -> str:
    """兼容包装：若 dws 已登录且企业不一致，返回中文报错文案。"""
    res = await check_dws_corp_access(expected_corp_ids=expected_corp_ids)
    return res.corp_mismatch_error


def log_dws_startup_status_sync() -> None:
    """容器启动时只读自检 dws 登录状态与（可选配置的）期望企业一致性。"""
    if not (cfg.ENABLE_DWS_GROUP_CONTEXT or cfg.ENABLE_DWS_QUOTE_CONTEXT):
        return
    try:
        expected = getattr(cfg, "DWS_EXPECTED_CORP_ID", "")
        if expected:
            err = asyncio.run(verify_dws_corp_match(expected_corp_ids=[expected]))
            if err:
                return
        identity = asyncio.run(get_dws_auth_identity(force_refresh=True))
        if identity and (identity.authenticated or identity.corp_id):
            print(
                f"✅ [dws] 登录态就绪: 企业={identity.corp_name or '-'} ({identity.corp_id or '-'}), "
                f"用户={identity.user_name or '-'}（每次收到钉钉消息时将自动校验机器人归属企业）"
            )
        else:
            print("ℹ️ [dws] 当前未登录，群聊非@上下文增强暂未启用（可执行 dws auth login --device 登录）")
    except Exception as exc:
        print(f"⚠️ [dws] 启动状态自检跳过: {exc}")


def validate_readonly_dws_args(args: Sequence[str]) -> list[str]:
    """严格校验 dws 参数列表，仅放行白名单内的只读命令与只读参数。"""
    if not args:
        raise PermissionError("dws 参数不能为空")

    normalized: list[str] = []
    for item in args:
        if not isinstance(item, str) or not item:
            raise PermissionError("dws 参数必须是非空字符串")
        if any(ch in item for ch in ("\x00", "\r", "\n")):
            raise PermissionError("dws 参数包含非法控制字符")
        normalized.append(item)

    matched_prefix: tuple[str, ...] | None = None
    for prefix_len in (3, 2):
        if len(normalized) >= prefix_len:
            candidate = tuple(normalized[:prefix_len])
            if candidate in _READONLY_COMMAND_SPECS:
                matched_prefix = candidate
                break

    if matched_prefix is None:
        raise PermissionError(f"禁止执行非只读白名单的 dws 子命令: {normalized[:3]}")

    spec = _READONLY_COMMAND_SPECS[matched_prefix]
    value_flags = spec["value_flags"]
    bool_flags = spec["bool_flags"]
    required_bool_flags = spec["required_bool_flags"]
    required_value_flags = spec.get("required_value_flags", set())

    seen_bool_flags: set[str] = set()
    seen_value_flags: set[str] = set()
    output_format: str | None = None

    rest = normalized[len(matched_prefix) :]
    idx = 0
    while idx < len(rest):
        token = rest[idx]
        if not token.startswith("-"):
            raise PermissionError(f"禁止向 dws 传递未预期的定位参数: {token}")

        if "=" in token:
            flag, val = token.split("=", 1)
            if flag not in value_flags or not val:
                raise PermissionError(f"禁止使用未授权的 dws 参数: {token}")
            _validate_flag_value(matched_prefix, flag, val)
            seen_value_flags.add(flag)
            if flag in {"-f", "--format"}:
                output_format = val
            idx += 1
            continue

        if token in value_flags:
            if idx + 1 >= len(rest):
                raise PermissionError(f"dws 参数缺少对应值: {token}")
            val = rest[idx + 1]
            if not val or val.startswith("-"):
                raise PermissionError(f"dws 参数值非法: {token}={val}")
            _validate_flag_value(matched_prefix, token, val)
            seen_value_flags.add(token)
            if token in {"-f", "--format"}:
                output_format = val
            idx += 2
            continue

        if token in bool_flags:
            seen_bool_flags.add(token)
            idx += 1
            continue

        raise PermissionError(f"禁止使用未授权的 dws 参数: {token}")

    if not required_bool_flags.issubset(seen_bool_flags):
        missing = sorted(required_bool_flags - seen_bool_flags)
        raise PermissionError(f"dws 只读命令缺少必选安全参数: {missing}")

    if not required_value_flags.issubset(seen_value_flags):
        missing_v = sorted(required_value_flags - seen_value_flags)
        raise PermissionError(f"dws 只读命令缺少必选参数: {missing_v}")

    if output_format != "json":
        raise PermissionError("dws 只读调用必须显式指定 -f json / --format json")

    return normalized


def _validate_flag_value(matched_prefix: tuple[str, ...], flag: str, val: str) -> None:
    if flag in {"-f", "--format"} and val != "json":
        raise PermissionError(f"仅允许 json 输出格式，实际收到: {val}")
    if flag == "--direction" and val not in {"older", "newer"}:
        raise PermissionError(f"--direction 仅允许 older/newer，实际收到: {val}")
    if flag == "--limit":
        try:
            n = int(val)
        except ValueError as exc:
            raise PermissionError(f"--limit 必须为整数: {val}") from exc
        if n < 1 or n > 100:
            raise PermissionError(f"--limit 超出安全范围 [1, 100]: {n}")
    if matched_prefix == ("chat", "message", "download-media"):
        if flag == "--type" and val != "mediaId":
            raise PermissionError(f"download-media 仅允许 --type mediaId，拒绝: {val}")
        if flag == "--output":
            real_out = os.path.realpath(val)
            real_tmp = os.path.realpath(tempfile.gettempdir())
            base = os.path.basename(real_out)
            if (
                real_out == real_tmp
                or not real_out.startswith(real_tmp + os.sep)
                or not base.startswith("dws_ro_media_")
                or not os.path.isdir(real_out)
            ):
                raise PermissionError(f"download-media 仅允许写入受控只读临时目录，拒绝: {val}")


def normalize_dws_timestamp(raw_ts: Any) -> str:
    """统一转换 dws / Stream 的各种时间表达为北京时间 'YYYY-MM-DD HH:MM:SS'。"""
    if raw_ts is None or raw_ts == "":
        return ""

    if isinstance(raw_ts, (int, float)):
        sec = float(raw_ts) / 1000.0 if float(raw_ts) > 1e11 else float(raw_ts)
        if sec <= 0:
            return ""
        return datetime.fromtimestamp(sec, tz=_SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M:%S")

    text = str(raw_ts).strip()
    if not text:
        return ""

    if text.isdigit():
        num = float(text)
        sec = num / 1000.0 if num > 1e11 else num
        if sec <= 0:
            return ""
        return datetime.fromtimestamp(sec, tz=_SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M:%S")

    if re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", text):
        return text

    try:
        iso_str = text.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_SHANGHAI_TZ)
        else:
            dt = dt.astimezone(_SHANGHAI_TZ)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return ""


def _parse_ts_epoch(ts_str: str) -> float | None:
    if not ts_str:
        return None
    try:
        dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_SHANGHAI_TZ)
        return dt.timestamp()
    except ValueError:
        return None


def extract_messages_from_payload(payload: Any) -> list[dict[str, Any]]:
    """兼容 dws 统一 JSON envelope（ok/data/messages）与扁平结构（messages/result）。"""
    if payload is None:
        return []

    if isinstance(payload, list):
        return [m for m in payload if isinstance(m, dict)]

    if not isinstance(payload, dict):
        return []

    if payload.get("ok") is False:
        return []

    inner: Any = payload
    if "ok" in payload and isinstance(payload.get("data"), (dict, list)):
        inner = payload["data"]
    elif isinstance(payload.get("result"), (dict, list)):
        inner = payload["result"]

    if isinstance(inner, list):
        return [m for m in inner if isinstance(m, dict)]

    if isinstance(inner, dict):
        msgs = inner.get("messages")
        if isinstance(msgs, list):
            return [m for m in msgs if isinstance(m, dict)]
        items = inner.get("items")
        if isinstance(items, list):
            return [m for m in items if isinstance(m, dict)]

    return []


def _extract_resources_from_dict(src: dict[str, Any]) -> list[dict[str, Any]]:
    raw_res = src.get("resources")
    if isinstance(raw_res, list):
        return [dict(r) for r in raw_res if isinstance(r, dict)]
    return []


def _parse_quote_dict(q: dict[str, Any]) -> DwsQuoteInfo | None:
    if not isinstance(q, dict):
        return None
    msg_id = str(q.get("messageId") or q.get("openMessageId") or q.get("msgId") or "").strip()
    conv_id = str(q.get("conversationId") or q.get("openConversationId") or "").strip()
    sender = str(q.get("sender") or q.get("senderNick") or q.get("nick") or "").strip()
    sender_id = str(q.get("senderId") or q.get("senderUserId") or q.get("senderStaffId") or "").strip()
    create_time = normalize_dws_timestamp(
        q.get("createTime") or q.get("createdAt") or q.get("sendTime")
    )
    msg_type = str(q.get("messageType") or q.get("msgType") or "").strip()
    resources = _extract_resources_from_dict(q)

    text = ""
    if isinstance(q.get("text"), str) and q["text"].strip():
        text = q["text"].strip()
    else:
        raw_content = q.get("content")
        text = _extract_text_from_stream_content(raw_content, msg_type=msg_type)

    if not msg_id and not text and not resources:
        return None

    return DwsQuoteInfo(
        message_id=msg_id,
        conversation_id=conv_id,
        sender=sender,
        sender_id=sender_id,
        text=text,
        create_time=create_time,
        message_type=msg_type,
        resources=resources,
    )


def _extract_text_from_stream_content(raw_content: Any, msg_type: str = "") -> str:
    if isinstance(raw_content, str):
        stripped = raw_content.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, dict):
                    return _extract_text_from_stream_content(parsed, msg_type=msg_type)
            except json.JSONDecodeError:
                pass
        return stripped

    if isinstance(raw_content, dict):
        if isinstance(raw_content.get("text"), str) and raw_content["text"].strip():
            return raw_content["text"].strip()
        if isinstance(raw_content.get("content"), str) and raw_content["content"].strip():
            return raw_content["content"].strip()
        rich_list = raw_content.get("richText")
        if isinstance(rich_list, list):
            parts: list[str] = []
            for elem in rich_list:
                if isinstance(elem, dict):
                    if isinstance(elem.get("text"), str):
                        parts.append(elem["text"])
                    elif elem.get("downloadCode") or elem.get("pictureUrl"):
                        parts.append("[图片]")
            joined = "".join(parts).strip()
            if joined:
                return joined
        if raw_content.get("downloadCode") or msg_type in {"picture", "image"}:
            return "[图片]"

    return ""


def extract_clean_dws_item(item: dict[str, Any]) -> dict[str, Any] | None:
    """将 dws 单条消息投影清洗为统一内部结构。"""
    if not isinstance(item, dict):
        return None

    message_id = str(
        item.get("messageId") or item.get("openMessageId") or item.get("msgId") or ""
    ).strip()
    conversation_id = str(
        item.get("conversationId") or item.get("openConversationId") or ""
    ).strip()
    sender = str(
        item.get("sender") or item.get("senderNick") or item.get("senderName") or ""
    ).strip()
    sender_id = str(
        item.get("senderId") or item.get("senderUserId") or item.get("senderStaffId") or ""
    ).strip()
    sender_type = str(item.get("senderType") or "").strip().lower()
    message_type = str(
        item.get("messageType") or item.get("msgType") or item.get("contentType") or ""
    ).strip()
    timestamp = normalize_dws_timestamp(
        item.get("createTime") or item.get("createdAt") or item.get("sendTime")
    )
    resources = _extract_resources_from_dict(item)

    raw_text = ""
    if isinstance(item.get("text"), str):
        raw_text = item["text"].strip()
    elif isinstance(item.get("content"), str):
        candidate = item["content"].strip()
        if not (candidate.startswith("{") and "cipher" in candidate.lower()):
            raw_text = candidate

    is_noise = raw_text in _NOISE_TEXTS

    is_bot_like = (
        sender_type in {"robot", "bot", "chatbot", "system"}
        or message_type in {"interactiveCard", "actionCard", "markdownCard", "system"}
        or raw_text.lstrip().startswith("👋 @")
        or "<font color='#808080'" in raw_text
    )

    quoted_info = (
        _parse_quote_dict(item["quotedMessage"])
        if isinstance(item.get("quotedMessage"), dict)
        else None
    )

    forwarded_parts: list[str] = []
    if isinstance(item.get("forwarded"), list):
        for fwd in item["forwarded"][:6]:
            if isinstance(fwd, dict):
                f_sender = str(fwd.get("sender") or "某人").strip()
                f_text = str(fwd.get("text") or "").strip()
                if f_text and f_text not in _NOISE_TEXTS:
                    forwarded_parts.append(f"{f_sender}: {f_text[:80]}")

    display_text = "" if is_noise else raw_text
    if forwarded_parts:
        fwd_summary = f"[合并转发: {' | '.join(forwarded_parts)}]"
        display_text = f"{display_text} {fwd_summary}".strip() if display_text else fwd_summary

    if not display_text and not is_noise and message_type.lower() in {"picture", "image", "photo"}:
        display_text = "[图片]"

    return {
        "message_id": message_id,
        "conversation_id": conversation_id,
        "sender": sender,
        "sender_id": sender_id,
        "sender_type": sender_type,
        "message_type": message_type,
        "timestamp": timestamp,
        "raw_text": raw_text,
        "display_text": display_text,
        "is_noise": is_noise,
        "is_bot_like": is_bot_like,
        "quoted": quoted_info,
        "resources": resources,
    }


def extract_stream_quote_hint(incoming_message: Any) -> DwsQuoteInfo | None:
    """从钉钉 Stream 回调消息的扩展字段（如 text.extensions.repliedMsg）提取引用线索。"""
    if incoming_message is None:
        return None

    ext_sources: list[dict[str, Any]] = []
    text_obj = getattr(incoming_message, "text", None)
    if text_obj is not None and isinstance(getattr(text_obj, "extensions", None), dict):
        ext_sources.append(text_obj.extensions)

    rich_obj = getattr(incoming_message, "rich_text_content", None)
    if rich_obj is not None and isinstance(getattr(rich_obj, "extensions", None), dict):
        ext_sources.append(rich_obj.extensions)

    msg_ext = getattr(incoming_message, "extensions", None)
    if isinstance(msg_ext, dict):
        ext_sources.append(msg_ext)
        if isinstance(msg_ext.get("content"), dict):
            ext_sources.append(msg_ext["content"])

    for ext in ext_sources:
        for key in ("repliedMsg", "quotedMessage", "quoteContent", "originalMsg"):
            val = ext.get(key)
            if isinstance(val, dict):
                parsed = _parse_quote_dict(val)
                if parsed is not None:
                    return parsed
            elif isinstance(val, str) and val.strip():
                return DwsQuoteInfo(text=val.strip())

        direct_qid = str(ext.get("originalMsgId") or ext.get("quoteMsgId") or "").strip()
        if direct_qid:
            return DwsQuoteInfo(message_id=direct_qid)

    return None


async def run_dws_readonly_json(
    args: Sequence[str],
    *,
    timeout_seconds: float | None = None,
) -> dict[str, Any] | list[Any] | None:
    """通过严格只读门禁执行 dws 命令并解析 JSON 结果；失败或未登录时安静降级返回 None。"""
    global _backoff_until, _backoff_keychain_marker

    validated_args = validate_readonly_dws_args(args)

    now = time.monotonic()
    if now < _backoff_until:
        current_marker = _get_keychain_marker()
        if _backoff_keychain_marker is not None and current_marker != _backoff_keychain_marker:
            _backoff_until = 0.0
            _backoff_keychain_marker = None
        else:
            return None

    env = dict(os.environ)
    for proxy_key in _PROXY_ENV_KEYS:
        env.pop(proxy_key, None)

    dws_bin = cfg.DWS_BIN
    timeout = timeout_seconds if timeout_seconds is not None else cfg.DWS_TIMEOUT_SECONDS

    try:
        proc = await asyncio.create_subprocess_exec(
            dws_bin,
            *validated_args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except (FileNotFoundError, OSError) as exc:
        _backoff_keychain_marker = _get_keychain_marker()
        _backoff_until = time.monotonic() + cfg.DWS_AUTH_BACKOFF_SECONDS
        print(f"⚠️ [dws] 未找到可执行文件 ({dws_bin}): {exc}，降级使用本地历史")
        return None

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        _backoff_keychain_marker = _get_keychain_marker()
        _backoff_until = time.monotonic() + min(10.0, float(cfg.DWS_AUTH_BACKOFF_SECONDS))
        print(f"⚠️ [dws] 只读查询超时 ({timeout:.1f}s): {validated_args[:3]}，短期退避并降级使用本地历史")
        return None

    raw_out = (stdout_bytes or b"").decode("utf-8", errors="replace").strip()
    raw_err = (stderr_bytes or b"").decode("utf-8", errors="replace").strip()

    parsed: Any = None
    for candidate in (raw_out, raw_err):
        if candidate.startswith("{") or candidate.startswith("["):
            try:
                parsed = json.loads(candidate)
                break
            except json.JSONDecodeError:
                continue

    err_type = ""
    err_msg = ""
    if isinstance(parsed, dict) and parsed.get("ok") is False:
        err_obj = parsed.get("error") if isinstance(parsed.get("error"), dict) else {}
        err_type = str(err_obj.get("type") or "").lower()
        err_msg = str(err_obj.get("message") or "")

    combined_lower = f"{err_type} {err_msg} {raw_err}".lower()
    if (
        proc.returncode in (2, 4)
        or err_type in {"auth", "permission"}
        or "not logged in" in combined_lower
        or "auth login" in combined_lower
    ):
        _backoff_keychain_marker = _get_keychain_marker()
        _backoff_until = time.monotonic() + cfg.DWS_AUTH_BACKOFF_SECONDS
        print(
            f"⚠️ [dws] 未登录或无权限 (code={proc.returncode}, type={err_type or 'auth'})，"
            f"{cfg.DWS_AUTH_BACKOFF_SECONDS}s 内跳过 dws 增强（可执行 dws auth login --device 登录）"
        )
        return None

    if proc.returncode != 0 or parsed is None:
        short_err = (err_msg or raw_err or raw_out)[:160]
        print(f"⚠️ [dws] 查询返回非预期结果 (code={proc.returncode}): {short_err}")
        return None

    if isinstance(parsed, dict) and parsed.get("ok") is False:
        return None

    return parsed


def _normalize_for_dedup(text: str) -> str:
    """归一化消息文本用于匹配同一条 @机器人 消息（剥离引用头、@提及、图片计数与空白）。"""
    s = str(text or "").strip()
    s = re.sub(r"^\[引用[^\]]*\]\s*", "", s)
    s = _AT_TOKEN_RE.sub("", s)
    s = re.sub(r"\[(?:引用)?图片x\d+\]", "", s)
    s = re.sub(r"\s+", "", s)
    return s.lower()


def _is_self_bot_mention_name(mention_name: str, *, bot_id: str = "", bot_name: str = "") -> bool:
    """判断 @提及词是否指向当前容器自身的机器人（保留对人类同事或其他机器人的 @提及）。"""
    cleaned = mention_name.strip().rstrip("，,。！!？?：:；;)")
    if not cleaned:
        return False
    lower = cleaned.lower()

    explicit_names = {
        str(bot_name or "").strip().lower(),
        str(cfg.get_bot_display_name() or "").strip().lower(),
    }
    explicit_names.discard("")
    if lower in explicit_names:
        return True

    bid = (bot_id or cfg.BOT_ID or "").strip().lower()
    if bid == "gemini":
        return lower == "gem" or lower.startswith("gemini") or ("gemini" in lower and ("助手" in lower or "机器人" in lower or "bot" in lower))
    if bid in {"anthropic", "openrouter"}:
        return lower in {"小克", "小克助手"} or "claude" in lower
    if bid == "openai":
        return lower in {"小g", "小g助手"} or "gpt" in lower or "openai" in lower
    if bid == "openclaw":
        return "claw" in lower
    return False


def restore_at_mentions_from_dws_text(
    stream_text: str,
    dws_text: str,
    *,
    bot_id: str = "",
    bot_name: str = "",
) -> str:
    """利用 dws 完整文本还原被钉钉 Stream 剥除的 @人类同事 提及，同时剔除 @当前机器人。"""
    raw_dws = (dws_text or "").strip()
    if not raw_dws or "@" not in raw_dws:
        return stream_text

    stream_mentions = {m.group(1) for m in _AT_TOKEN_RE.finditer(stream_text or "")}
    dws_mentions = [m.group(1) for m in _AT_TOKEN_RE.finditer(raw_dws)]
    if not dws_mentions:
        return stream_text

    def _replace_at(match: re.Match[str]) -> str:
        name = match.group(1)
        if _is_self_bot_mention_name(name, bot_id=bot_id, bot_name=bot_name):
            return ""
        return match.group(0)

    cleaned = _AT_TOKEN_RE.sub(_replace_at, raw_dws)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()
    remaining_mentions = {m.group(1) for m in _AT_TOKEN_RE.finditer(cleaned)}
    if remaining_mentions - stream_mentions:
        return cleaned
    return stream_text


def _has_non_bot_at_users(incoming_messages: Sequence[Any], chatbot_user_id: str) -> bool:
    for msg in incoming_messages:
        at_list = getattr(msg, "at_users", None) or []
        for entry in at_list:
            uid = ""
            if isinstance(entry, dict):
                uid = str(entry.get("dingtalkId") or entry.get("staffId") or "").strip()
            else:
                uid = str(getattr(entry, "dingtalk_id", "") or getattr(entry, "staff_id", "") or "").strip()
            if uid and (not chatbot_user_id or uid != chatbot_user_id):
                return True
    return False


def _format_inline_dws_user_content(item: dict[str, Any]) -> str:
    """为群聊历史中的普通人类消息拼装内联引用说明（若有）。"""
    body = item.get("display_text", "").strip()
    q: DwsQuoteInfo | None = item.get("quoted")
    if q and (q.text or q.sender):
        q_sender = q.sender or "群成员"
        q_brief = (q.text or "[消息]").replace("\n", " ").strip()
        if len(q_brief) > 60:
            q_brief = q_brief[:60] + "…"
        prefix = f"[引用 {q_sender}: {q_brief}]"
        if not body.startswith("[引用"):
            return f"{prefix} {body}".strip()
    return body


def extract_persistable_dws_messages(
    dws_items: Sequence[dict[str, Any]],
    *,
    current_message_ids: Collection[str] = (),
    chatbot_user_id: str = "",
    ignore_senders: Collection[str] = (),
) -> list[dict[str, Any]]:
    """提取可持久化写入数据库/文件的群聊人类消息（不受单 bot /clear cutoff 影响，排除本轮未完结消息）。"""
    excluded_ids = {str(mid).strip() for mid in current_message_ids if str(mid).strip()}
    ignored_nicks = {str(s).strip() for s in ignore_senders if str(s).strip()}
    out: list[dict[str, Any]] = []

    for item in dws_items:
        mid = str(item.get("message_id") or "").strip()
        ts = str(item.get("timestamp") or "").strip()
        if not mid or not ts or mid in excluded_ids:
            continue
        if item.get("is_noise") or item.get("is_bot_like"):
            continue
        if chatbot_user_id and item.get("sender_id") == chatbot_user_id:
            continue
        if item.get("sender") and item["sender"] in ignored_nicks:
            continue
        content = _format_inline_dws_user_content(item).strip()
        if not content:
            continue
        out.append(
            {
                "external_msg_id": mid,
                "content": content,
                "sender_nick": item.get("sender") or "User",
                "timestamp": ts,
            }
        )
    return out


def merge_dws_group_messages_with_history(
    dws_items: Sequence[dict[str, Any]],
    history_messages: Sequence[dict[str, Any]],
    current_message_ids: Collection[str] = (),
    cutoff_at: str | None = None,
    max_length: int = 50,
    chatbot_user_id: str = "",
    ignore_senders: Collection[str] = (),
) -> list[dict[str, Any]]:
    """将 dws 拉取的群聊人类消息与机器人持久化的问答历史按时间线去重合并。"""
    excluded_ids = {str(mid).strip() for mid in current_message_ids if str(mid).strip()}
    ignored_nicks = {str(s).strip() for s in ignore_senders if str(s).strip()}

    filtered_history: list[dict[str, Any]] = []
    for msg in history_messages:
        ts = msg.get("timestamp")
        if cutoff_at and ts and ts <= cutoff_at:
            continue
        filtered_history.append(msg)

    # 仅针对长文本且落在相近时间窗内的非标记 webhook 机器人复读做去重，绝不误杀人类短回复（如「收到」「🙂」）
    assistant_timed_norms: list[tuple[float | None, str]] = [
        (_parse_ts_epoch(str(m.get("timestamp") or "")), _normalize_for_dedup(m.get("content", "")))
        for m in filtered_history
        if m.get("role") == "assistant" and m.get("content")
    ]

    human_dws: list[dict[str, Any]] = []
    for item in dws_items:
        mid = item.get("message_id", "")
        if mid and mid in excluded_ids:
            continue
        ts = item.get("timestamp", "")
        if cutoff_at and ts and ts <= cutoff_at:
            continue
        if item.get("is_noise") or item.get("is_bot_like"):
            continue
        if chatbot_user_id and item.get("sender_id") == chatbot_user_id:
            continue
        if item.get("sender") and item["sender"] in ignored_nicks:
            continue
        display_text = item.get("display_text", "").strip()
        if not display_text:
            continue

        norm = _normalize_for_dedup(display_text)
        if norm and len(norm) >= 16:
            d_ep = _parse_ts_epoch(ts)
            if any(
                a_norm == norm
                and (d_ep is None or a_ep is None or -15.0 <= (a_ep - d_ep) <= 180.0)
                for a_ep, a_norm in assistant_timed_norms
            ):
                continue

        human_dws.append(item)

    matched_history_user_indices: set[int] = set()
    used_dws_indices: set[int] = set()
    skipped_dws_indices: set[int] = set()
    dws_override_content: dict[int, str] = {}

    for h_idx, h_msg in enumerate(filtered_history):
        if h_msg.get("role") != "user":
            continue
        h_content = str(h_msg.get("content") or "")
        h_ext_id = str(h_msg.get("external_msg_id") or "").strip()
        h_norm = _normalize_for_dedup(h_content)
        h_norm_lines = [
            _normalize_for_dedup(line)
            for line in h_content.splitlines()
            if _normalize_for_dedup(line)
        ]
        h_sender = str(h_msg.get("sender_nick") or "").strip()
        h_epoch = _parse_ts_epoch(str(h_msg.get("timestamp") or ""))

        # 1. 多行 2s 缓冲消息（A\nB）：匹配同一发送者在相近时间窗内的各拆分单条消息
        if len(h_norm_lines) > 1:
            line_matches: list[int] = []
            remaining_lines = list(h_norm_lines)
            for d_idx, d_item in enumerate(human_dws):
                if d_idx in used_dws_indices:
                    continue
                d_mid = str(d_item.get("message_id") or "").strip()
                if h_ext_id and d_mid == h_ext_id:
                    line_matches.append(d_idx)
                    d_norm = _normalize_for_dedup(d_item.get("raw_text") or d_item.get("display_text", ""))
                    if d_norm in remaining_lines:
                        remaining_lines.remove(d_norm)
                    continue
                d_sender = d_item.get("sender", "")
                if h_sender and d_sender and h_sender != d_sender:
                    continue
                d_epoch = _parse_ts_epoch(d_item.get("timestamp", ""))
                if h_epoch is not None and d_epoch is not None:
                    delta = h_epoch - d_epoch
                    if delta < -20 or delta > 300:
                        continue
                d_norm = _normalize_for_dedup(d_item.get("raw_text") or d_item.get("display_text", ""))
                if d_norm and d_norm in remaining_lines:
                    remaining_lines.remove(d_norm)
                    line_matches.append(d_idx)

            if line_matches:
                used_dws_indices.update(line_matches)
                if not remaining_lines:
                    # 全部拆分行均在 dws 窗口中：保留带精确时间与 @提及 的 dws 行，移除合并副本
                    matched_history_user_indices.add(h_idx)
                else:
                    # 仅部分行落在窗口边界：隐藏部分 dws 行，保留完整历史合并消息防丢字
                    skipped_dws_indices.update(line_matches)
                continue

        # 2. 单行消息匹配（优先按 external_msg_id，其次按发送者+时间窗+归一化文本）
        best_d_idx: int | None = None
        best_score: float = float("inf")
        best_is_image_only = False

        for d_idx, d_item in enumerate(human_dws):
            if d_idx in used_dws_indices:
                continue
            d_mid = str(d_item.get("message_id") or "").strip()
            if h_ext_id and d_mid == h_ext_id:
                best_d_idx = d_idx
                best_is_image_only = (
                    d_item.get("display_text") == "[图片]" and "[图片" in h_content
                )
                break

            d_sender = d_item.get("sender", "")
            if h_sender and d_sender and h_sender != d_sender:
                continue

            d_epoch = _parse_ts_epoch(d_item.get("timestamp", ""))
            delta_abs = 0.0
            if h_epoch is not None and d_epoch is not None:
                delta = h_epoch - d_epoch
                if delta < -20 or delta > 300:
                    continue
                delta_abs = abs(delta)

            d_norm = _normalize_for_dedup(d_item.get("raw_text") or d_item.get("display_text", ""))
            same_text = bool(h_norm and d_norm and h_norm == d_norm)
            same_image_turn = (
                d_item.get("display_text") == "[图片]" and "[图片" in h_content
            )
            if (same_text or same_image_turn) and delta_abs < best_score:
                best_score = delta_abs
                best_d_idx = d_idx
                best_is_image_only = same_image_turn and not same_text

        if best_d_idx is not None:
            matched_history_user_indices.add(h_idx)
            used_dws_indices.add(best_d_idx)
            if best_is_image_only:
                dws_override_content[best_d_idx] = h_content

    combined_entries: list[tuple[str, int, int, dict[str, Any]]] = []
    seq = 0

    for d_idx, d_item in enumerate(human_dws):
        if d_idx in skipped_dws_indices:
            continue
        seq += 1
        content_val = dws_override_content.get(d_idx) or _format_inline_dws_user_content(d_item)
        ts_val = d_item.get("timestamp", "")
        entry_dict: dict[str, Any] = {
            "role": "user",
            "content": content_val,
            "timestamp": ts_val,
            "sender_nick": d_item.get("sender") or "User",
        }
        if d_item.get("message_id"):
            entry_dict["external_msg_id"] = d_item["message_id"]
        combined_entries.append((ts_val, 0, seq, entry_dict))

    for h_idx, h_msg in enumerate(filtered_history):
        role = h_msg.get("role", "user")
        if role == "user" and h_idx in matched_history_user_indices:
            continue
        seq += 1
        ts_val = str(h_msg.get("timestamp") or "")
        role_rank = 0 if role == "user" else 1
        combined_entries.append((ts_val, role_rank, seq, dict(h_msg)))

    combined_entries.sort(key=lambda x: (x[0], x[1], x[2]))
    merged = [entry[3] for entry in combined_entries]
    if max_length > 0 and len(merged) > max_length:
        merged = merged[-max_length:]
    return merged


def _merge_same_quote_info(primary: DwsQuoteInfo, secondary: DwsQuoteInfo) -> DwsQuoteInfo:
    """仅在同一被引用消息内部补齐缺失字段与资源，绝不跨不同引用消息混拼。"""
    p_text = primary.text if primary.text not in _NOISE_TEXTS else ""
    s_text = secondary.text if secondary.text not in _NOISE_TEXTS else ""
    chosen_text = p_text or s_text or primary.text or secondary.text

    merged_res: list[dict[str, Any]] = []
    seen_res: set[str] = set()
    for res in [*(primary.resources or []), *(secondary.resources or [])]:
        if not isinstance(res, dict):
            continue
        rk = str(res.get("resourceId") or res.get("url") or json.dumps(res, sort_keys=True))
        if rk not in seen_res:
            seen_res.add(rk)
            merged_res.append(res)

    return DwsQuoteInfo(
        message_id=primary.message_id or secondary.message_id,
        conversation_id=primary.conversation_id or secondary.conversation_id,
        sender=primary.sender or secondary.sender,
        sender_id=primary.sender_id or secondary.sender_id,
        text=chosen_text,
        create_time=primary.create_time or secondary.create_time,
        message_type=primary.message_type or secondary.message_type,
        resources=merged_res,
    )


def _append_or_merge_quote(quotes: list[DwsQuoteInfo], candidate: DwsQuoteInfo | None) -> None:
    if candidate is None:
        return
    for idx, existing in enumerate(quotes):
        same_id = bool(
            existing.message_id
            and candidate.message_id
            and existing.message_id == candidate.message_id
        )
        same_idless_text = bool(
            (not existing.message_id or not candidate.message_id)
            and existing.text
            and candidate.text
            and _normalize_for_dedup(existing.text) == _normalize_for_dedup(candidate.text)
        )
        if same_id or same_idless_text:
            quotes[idx] = _merge_same_quote_info(existing, candidate)
            return
    quotes.append(candidate)


def _quote_needs_mget_hydration(quote: DwsQuoteInfo) -> bool:
    if not quote.message_id:
        return False
    if (
        not quote.create_time
        or not quote.sender
        or not quote.text
        or quote.text in _NOISE_TEXTS
    ):
        return True
    if not quote.resources:
        q_type = quote.message_type.lower()
        if (
            q_type in {"picture", "image", "photo", "file", "richtext"}
            or quote.text == "[图片]"
            or "[图片]" in quote.text
            or quote.text.startswith("[文件]")
            or "fileId:" in quote.text
        ):
            return True
    return False


def _item_to_quote_info(item: dict[str, Any]) -> DwsQuoteInfo:
    return DwsQuoteInfo(
        message_id=item.get("message_id", ""),
        conversation_id=item.get("conversation_id", ""),
        sender=item.get("sender", ""),
        sender_id=item.get("sender_id", ""),
        text=item.get("display_text", ""),
        create_time=item.get("timestamp", ""),
        message_type=item.get("message_type", ""),
        resources=list(item.get("resources") or []),
    )


def _enrich_quote_from_history(
    quote: DwsQuoteInfo,
    history_messages: Sequence[dict[str, Any]],
) -> DwsQuoteInfo:
    """当用户引用了机器人的 AI 互动卡片（dws/Stream 仅存 Thinking... 占位）时，从本地历史恢复真实回复全文。"""
    if quote.text and quote.text not in _NOISE_TEXTS:
        return quote

    q_epoch = _parse_ts_epoch(quote.create_time)
    if q_epoch is None:
        return quote

    best_match: dict[str, Any] | None = None
    best_distance = float("inf")

    for msg in history_messages:
        if msg.get("role") != "assistant":
            continue
        content = str(msg.get("content") or "").strip()
        if not content:
            continue
        m_epoch = _parse_ts_epoch(str(msg.get("timestamp") or ""))
        if m_epoch is None:
            continue
        delta = m_epoch - q_epoch
        if -15.0 <= delta <= 240.0 and abs(delta) < best_distance:
            best_distance = abs(delta)
            best_match = msg

    if best_match is not None:
        bot_id = str(best_match.get("bot_id") or "")
        recovered_sender = quote.sender or (get_bot_source_name(bot_id) if bot_id else "AI助手")
        return DwsQuoteInfo(
            message_id=quote.message_id,
            conversation_id=quote.conversation_id,
            sender=recovered_sender,
            sender_id=quote.sender_id,
            text=str(best_match["content"]).strip(),
            create_time=quote.create_time,
            message_type=quote.message_type,
            resources=list(quote.resources),
        )

    return quote


def _build_quote_neighbor_lines(
    neighbor_items: Sequence[dict[str, Any]],
    history_messages: Sequence[dict[str, Any]],
    quote: DwsQuoteInfo,
    current_message_ids: Collection[str],
    before_count: int,
    after_count: int,
) -> list[str]:
    """在引用消息所处时间点附近组装前后上下文行（含同时间段的机器人历史回复）。"""
    excluded = {str(x).strip() for x in current_message_ids if str(x).strip()}
    timeline: list[QuoteTimelineRow] = []
    seen_ids: set[str] = set()
    seq = 0

    for item in neighbor_items:
        mid = item.get("message_id", "")
        if mid and (mid in excluded or mid in seen_ids):
            continue
        if mid:
            seen_ids.add(mid)

        if quote.message_id and mid == quote.message_id:
            q_text = (quote.text or item.get("display_text") or "").strip()
            if q_text and q_text not in _NOISE_TEXTS:
                seq += 1
                timeline.append(
                    QuoteTimelineRow(
                        timestamp=quote.create_time or item.get("timestamp", ""),
                        role_rank=0,
                        seq=seq,
                        message_id=mid,
                        sender=quote.sender or item.get("sender") or "群成员",
                        text=q_text,
                    )
                )
            continue

        if item.get("is_noise") or item.get("is_bot_like"):
            continue
        text = item.get("display_text", "").strip()
        if not text:
            continue
        seq += 1
        timeline.append(
            QuoteTimelineRow(
                timestamp=item.get("timestamp", ""),
                role_rank=0,
                seq=seq,
                message_id=mid,
                sender=item.get("sender") or "群成员",
                text=text,
            )
        )

    has_target = any(quote.message_id and row.message_id == quote.message_id for row in timeline)
    if not has_target and quote.text and quote.text not in _NOISE_TEXTS:
        seq += 1
        timeline.append(
            QuoteTimelineRow(
                timestamp=quote.create_time,
                role_rank=0,
                seq=seq,
                message_id=quote.message_id or "__quoted_target__",
                sender=quote.sender or "群成员",
                text=quote.text,
            )
        )

    epochs = [e for row in timeline if (e := _parse_ts_epoch(row.timestamp)) is not None]
    if epochs:
        min_ep, max_ep = min(epochs) - 15.0, max(epochs) + 180.0
        for h_msg in history_messages:
            if h_msg.get("role") != "assistant":
                continue
            h_ts = str(h_msg.get("timestamp") or "")
            h_ep = _parse_ts_epoch(h_ts)
            if h_ep is None or not (min_ep <= h_ep <= max_ep):
                continue
            h_text = str(h_msg.get("content") or "").strip()
            if not h_text or h_text == quote.text:
                continue
            bot_id = str(h_msg.get("bot_id") or "")
            bot_name = get_bot_source_name(bot_id) if bot_id else "AI助手"
            seq += 1
            timeline.append(
                QuoteTimelineRow(
                    timestamp=h_ts,
                    role_rank=1,
                    seq=seq,
                    message_id="",
                    sender=bot_name,
                    text=h_text,
                )
            )

    timeline.sort()

    target_idx = -1
    for idx, row in enumerate(timeline):
        if quote.message_id and row.message_id == quote.message_id:
            target_idx = idx
            break
        if row.message_id == "__quoted_target__":
            target_idx = idx
            break
        if quote.text and row.text == quote.text:
            target_idx = idx
            break

    if target_idx >= 0:
        start = max(0, target_idx - before_count)
        end = min(len(timeline), target_idx + after_count + 1)
        sliced = timeline[start:end]
        rel_target = target_idx - start
    else:
        sliced = timeline[-(before_count + after_count + 1) :]
        rel_target = -1

    lines: list[str] = []
    for i, row in enumerate(sliced):
        compact = row.text.replace("\n", " ").strip()
        if len(compact) > 220:
            compact = compact[:220] + "…"
        ts_part = f"[{row.timestamp}] " if row.timestamp else ""
        marker = "👉 " if i == rel_target else ""
        lines.append(f"- {marker}{ts_part}{row.sender}: {compact}")
    return lines


def _format_multi_quote_outputs(
    quotes_with_neighbors: Sequence[tuple[DwsQuoteInfo, Sequence[str]]],
    quoted_documents: Sequence[QuotedDocExtract],
    quoted_images_count: int,
    raw_user_content: str,
) -> tuple[str, str, str]:
    """生成支持单/多引用、引用图片与引用文档正文的统一输出块。"""
    block_lines = ["<quoted_context>"]
    router_summaries: list[str] = []
    history_prefixes: list[str] = []

    multi = len(quotes_with_neighbors) > 1
    for idx, (quote, neighbor_lines) in enumerate(quotes_with_neighbors, start=1):
        q_sender = quote.sender or "群成员"
        q_time_part = f"[{quote.create_time}] " if quote.create_time else ""
        q_body = (quote.text or "[引用消息]").strip()
        if len(q_body) > 800:
            q_body = q_body[:800] + "…"

        label = f"[被引用原消息 #{idx}]" if multi else "[被引用原消息]"
        block_lines.append(f"{label} {q_time_part}{q_sender}: {q_body}")
        if len(neighbor_lines) > 1:
            ctx_label = f"[引用 #{idx} 处前后上下文]" if multi else "[引用处前后上下文]"
            block_lines.append(ctx_label)
            block_lines.extend(neighbor_lines)

        router_q = q_body.replace("\n", " ")
        if len(router_q) > 240:
            router_q = router_q[:240] + "…"
        router_neighbors = " / ".join(line.lstrip("- ") for line in neighbor_lines[:5])
        if router_neighbors:
            router_summaries.append(f"{q_sender}: {router_q}（上下文：{router_neighbors}）")
        else:
            router_summaries.append(f"{q_sender}: {router_q}")

        short_q = q_body.replace("\n", " ").strip()
        if len(short_q) > 50:
            short_q = short_q[:50] + "…"
        history_prefixes.append(f"[引用 {q_sender}: {short_q}]")

    if quoted_images_count > 0:
        block_lines.append(f"[被引用图片] 已提取 {quoted_images_count} 张原图并附带至多模态视觉输入。")

    doc_router_names: list[str] = []
    for doc in quoted_documents:
        doc_router_names.append(f"{doc.name}({doc.extension})")
        header = f"[被引用文档正文: {doc.name} ({doc.extension})]"
        if doc.content:
            block_lines.append(header)
            block_lines.append(doc.content)
        elif doc.note:
            block_lines.append(f"{header} {doc.note}")

    block_lines.append("</quoted_context>")
    prompt_block = "\n".join(block_lines)

    doc_part = f"；附带引用文档：{', '.join(doc_router_names)}" if doc_router_names else ""
    img_part = f"；附带引用图片 {quoted_images_count} 张" if quoted_images_count > 0 else ""
    router_text = (
        f"[用户正在引用消息（{' | '.join(router_summaries)}{doc_part}{img_part}）]\n"
        f"用户当前发言: {raw_user_content}"
    )
    history_prefix = " ".join(history_prefixes) + " "
    return prompt_block, router_text, history_prefix


def _extract_single_incoming_stream_text(msg_obj: Any, fallback_text: str = "") -> str:
    text_attr = getattr(msg_obj, "text", None)
    if text_attr is not None and isinstance(getattr(text_attr, "content", None), str):
        return text_attr.content.strip()
    if callable(getattr(msg_obj, "get_text_list", None)):
        try:
            return "".join(msg_obj.get_text_list() or []).strip()
        except Exception:
            pass
    return fallback_text


def _identify_current_dws_items(
    recent_dws_items: Sequence[dict[str, Any]],
    incoming_messages: Sequence[Any],
    curr_id_set: set[str],
    raw_user_content: str,
) -> tuple[list[dict[str, Any]], set[str]]:
    """识别 recent_dws_items 中属于当前触发轮次的消息（兼容 Stream openMsgId 与 dws messageId 异构）。"""
    effective_ids = set(curr_id_set)
    matched_items: list[dict[str, Any]] = []
    matched_indices: set[int] = set()

    for idx, item in enumerate(recent_dws_items):
        mid = item.get("message_id", "")
        if mid and mid in effective_ids:
            matched_items.append(item)
            matched_indices.add(idx)

    for msg_obj in incoming_messages:
        obj_mid = str(getattr(msg_obj, "message_id", "") or "").strip()
        if obj_mid and any(it.get("message_id") == obj_mid for it in matched_items):
            continue

        m_sender = str(getattr(msg_obj, "sender_nick", "") or "").strip()
        m_ts = normalize_dws_timestamp(getattr(msg_obj, "create_at", None))
        m_epoch = _parse_ts_epoch(m_ts)
        part_text = _extract_single_incoming_stream_text(
            msg_obj,
            fallback_text=raw_user_content if len(incoming_messages) == 1 else "",
        )
        m_norm = _normalize_for_dedup(part_text)

        for idx in range(len(recent_dws_items) - 1, -1, -1):
            if idx in matched_indices:
                continue
            item = recent_dws_items[idx]
            if item.get("is_bot_like") or item.get("is_noise"):
                continue
            i_sender = item.get("sender", "")
            if m_sender and i_sender and m_sender != i_sender:
                continue

            i_epoch = _parse_ts_epoch(item.get("timestamp", ""))
            if m_epoch is not None and i_epoch is not None:
                if abs(i_epoch - m_epoch) > 15.0:
                    continue
            elif idx != len(recent_dws_items) - 1:
                continue

            i_norm = _normalize_for_dedup(item.get("raw_text") or item.get("display_text", ""))
            same_text = bool(m_norm and i_norm and m_norm == i_norm)
            same_img = not m_norm and item.get("display_text") == "[图片]"
            if same_text or same_img:
                matched_indices.add(idx)
                matched_items.append(item)
                if item.get("message_id"):
                    effective_ids.add(item["message_id"])
                break

    return matched_items, effective_ids


def _build_restored_user_content(
    incoming_messages: Sequence[Any],
    matched_incoming_items: Sequence[dict[str, Any]],
    raw_user_content: str,
) -> str:
    """将匹配到的当前轮 dws 原文中的 @人类同事 提及还原回用户本轮输入。"""
    if not matched_incoming_items or not raw_user_content:
        return ""

    by_id = {
        str(it.get("message_id") or "").strip(): it
        for it in matched_incoming_items
        if it.get("message_id")
    }

    if len(incoming_messages) <= 1:
        item = matched_incoming_items[0]
        cand = restore_at_mentions_from_dws_text(
            raw_user_content,
            item.get("raw_text") or item.get("display_text", ""),
            bot_id=cfg.BOT_ID,
            bot_name=cfg.BOT_NAME,
        )
        return cand if cand != raw_user_content else ""

    restored_parts: list[str] = []
    changed = False
    fallback_iter = iter(matched_incoming_items)
    for msg_obj in incoming_messages:
        stream_part = _extract_single_incoming_stream_text(msg_obj)
        mid = str(getattr(msg_obj, "message_id", "") or "").strip()
        dws_it = by_id.get(mid) or next(fallback_iter, None)
        if dws_it and stream_part:
            restored_p = restore_at_mentions_from_dws_text(
                stream_part,
                dws_it.get("raw_text") or dws_it.get("display_text", ""),
                bot_id=cfg.BOT_ID,
                bot_name=cfg.BOT_NAME,
            )
            if restored_p != stream_part:
                changed = True
            restored_parts.append(restored_p)
        elif stream_part:
            restored_parts.append(stream_part)

    joined = "\n".join(p for p in restored_parts if p)
    return joined if (changed and joined) else ""


def _find_quote_target_in_items(
    items: Sequence[dict[str, Any]],
    quote: DwsQuoteInfo,
) -> tuple[int, dict[str, Any] | None]:
    """按 message_id 优先、时间+文本兜底在消息列表中定位被引用原消息。"""
    if quote.message_id:
        for idx, it in enumerate(items):
            if it.get("message_id") == quote.message_id:
                return idx, it

    q_epoch = _parse_ts_epoch(quote.create_time)
    q_norm = _normalize_for_dedup(quote.text)
    if q_epoch is not None and q_norm and quote.text not in _NOISE_TEXTS:
        for idx, it in enumerate(items):
            it_epoch = _parse_ts_epoch(it.get("timestamp", ""))
            if it_epoch is not None and abs(it_epoch - q_epoch) <= 3.0:
                it_norm = _normalize_for_dedup(it.get("raw_text") or it.get("display_text", ""))
                if it_norm == q_norm:
                    return idx, it

    return -1, None


async def fetch_dws_context_bundle(
    *,
    conversation_id: str,
    is_group: bool,
    incoming_messages: Sequence[Any],
    current_message_ids: Sequence[str],
    raw_user_content: str,
    history_messages: Sequence[dict[str, Any]],
    cutoff_at: str | None = None,
    context_limit: int | None = None,
    quote_before: int | None = None,
    quote_after: int | None = None,
) -> DwsContextBundle:
    """拉取只读群聊近期消息、还原 @提及、并解析引用消息/上下文/图片/文档。"""
    limit_val = context_limit if context_limit is not None else cfg.DWS_CONTEXT_LIMIT
    before_val = quote_before if quote_before is not None else cfg.DWS_QUOTE_CONTEXT_BEFORE
    after_val = quote_after if quote_after is not None else cfg.DWS_QUOTE_CONTEXT_AFTER

    # 1. 提取钉钉 Stream 自带的引用线索（支持 2s 窗口多条引用独立保存）与机器人归属企业 CorpId
    quotes: list[DwsQuoteInfo] = []
    chatbot_user_id = ""
    expected_corp_ids: list[str] = []
    cfg_expected_corp = str(getattr(cfg, "DWS_EXPECTED_CORP_ID", "") or "").strip()
    if cfg_expected_corp:
        expected_corp_ids.append(cfg_expected_corp)

    for msg_obj in incoming_messages:
        if not chatbot_user_id:
            chatbot_user_id = str(getattr(msg_obj, "chatbot_user_id", "") or "").strip()
        msg_bot_corp = str(getattr(msg_obj, "chatbot_corp_id", "") or "").strip()
        if msg_bot_corp and msg_bot_corp not in expected_corp_ids:
            expected_corp_ids.append(msg_bot_corp)
        hint = extract_stream_quote_hint(msg_obj)
        _append_or_merge_quote(quotes, hint)

    if len(history_messages) > cfg.MAX_HISTORY_LENGTH:
        fallback_history = list(history_messages[-cfg.MAX_HISTORY_LENGTH :])
    else:
        fallback_history = list(history_messages)

    # 1.5 企业一致性门禁（严格 fail-closed）
    need_dws = (is_group and bool(conversation_id) and cfg.ENABLE_DWS_GROUP_CONTEXT) or cfg.ENABLE_DWS_QUOTE_CONTEXT
    dws_ready = False
    if need_dws:
        corp_check = await check_dws_corp_access(expected_corp_ids=expected_corp_ids)
        if corp_check.corp_mismatch_error:
            return DwsContextBundle(
                dws_available=False,
                merged_history=fallback_history,
                corp_mismatch_error=corp_check.corp_mismatch_error,
            )
        dws_ready = corp_check.allowed

    recent_dws_items: list[dict[str, Any]] = []
    dws_available = False

    # 2. 群聊开启且通过企业门禁时拉取近期群聊消息
    if dws_ready and is_group and conversation_id and cfg.ENABLE_DWS_GROUP_CONTEXT:
        payload = await run_dws_readonly_json(
            [
                "chat",
                "message",
                "list",
                "--conversation-id",
                conversation_id,
                "--limit",
                str(limit_val),
                "-f",
                "json",
            ]
        )
        if payload is not None:
            dws_available = True
            for raw_item in extract_messages_from_payload(payload):
                cleaned = extract_clean_dws_item(raw_item)
                if cleaned is not None:
                    recent_dws_items.append(cleaned)
            recent_dws_items.sort(key=lambda x: x.get("timestamp", ""))

    # 3. 识别本次触发消息对应的 dws 记录（同时服务引用发现与 @他人 还原）
    raw_curr_id_set = {str(mid).strip() for mid in current_message_ids if str(mid).strip()}
    matched_incoming_items, curr_id_set = _identify_current_dws_items(
        recent_dws_items, incoming_messages, raw_curr_id_set, raw_user_content
    )
    has_other_ats = _has_non_bot_at_users(incoming_messages, chatbot_user_id)

    if (
        dws_ready
        and not matched_incoming_items
        and raw_curr_id_set
        and ((cfg.ENABLE_DWS_QUOTE_CONTEXT and not quotes) or has_other_ats)
    ):
        mget_payload = await run_dws_readonly_json(
            [
                "chat",
                "+messages-mget",
                "--msg-ids",
                ",".join(sorted(raw_curr_id_set)),
                "--no-reactions",
                "--no-threads",
                "-f",
                "json",
            ]
        )
        if mget_payload is not None:
            dws_available = True
            mget_cleaned = [
                cleaned
                for raw_item in extract_messages_from_payload(mget_payload)
                if (cleaned := extract_clean_dws_item(raw_item)) is not None
            ]
            mget_matched, curr_id_set = _identify_current_dws_items(
                mget_cleaned, incoming_messages, curr_id_set, raw_user_content
            )
            matched_incoming_items.extend(mget_matched)

    if cfg.ENABLE_DWS_QUOTE_CONTEXT:
        for item in matched_incoming_items:
            if item.get("quoted") is not None:
                _append_or_merge_quote(quotes, item["quoted"])

    restored_user_content = _build_restored_user_content(
        incoming_messages=incoming_messages,
        matched_incoming_items=matched_incoming_items,
        raw_user_content=raw_user_content,
    )
    effective_user_content = restored_user_content or raw_user_content

    # 3.5 补全每条被引用消息的完整正文、时间戳与媒体/文件资源元数据
    if cfg.ENABLE_DWS_QUOTE_CONTEXT and quotes:
        missing_q_ids: list[str] = []
        for idx, q in enumerate(quotes):
            _t_idx, target_it = _find_quote_target_in_items(recent_dws_items, q)
            if target_it is not None:
                quotes[idx] = _merge_same_quote_info(q, _item_to_quote_info(target_it))
            elif dws_ready and _quote_needs_mget_hydration(q) and q.message_id not in missing_q_ids:
                missing_q_ids.append(q.message_id)

        if dws_ready and missing_q_ids:
            q_mget = await run_dws_readonly_json(
                [
                    "chat",
                    "+messages-mget",
                    "--msg-ids",
                    ",".join(missing_q_ids),
                    "--no-reactions",
                    "--no-threads",
                    "-f",
                    "json",
                ]
            )
            if q_mget is not None:
                dws_available = True
                for raw_q in extract_messages_from_payload(q_mget):
                    cleaned_q = extract_clean_dws_item(raw_q)
                    if cleaned_q is not None:
                        _append_or_merge_quote(quotes, _item_to_quote_info(cleaned_q))

        for idx, q in enumerate(quotes):
            quotes[idx] = _enrich_quote_from_history(q, history_messages)

    # 4. 合并群聊近期历史与本地机器人问答历史，并提取可持久化的人类群消息
    persistable_messages: list[dict[str, Any]] = []
    if is_group and dws_available and recent_dws_items:
        merged_history = merge_dws_group_messages_with_history(
            dws_items=recent_dws_items,
            history_messages=history_messages,
            current_message_ids=curr_id_set,
            cutoff_at=cutoff_at,
            max_length=cfg.MAX_HISTORY_LENGTH,
            chatbot_user_id=chatbot_user_id,
            ignore_senders=cfg.DWS_IGNORE_SENDERS,
        )
        if cfg.DWS_PERSIST_GROUP_MESSAGES:
            persistable_messages = extract_persistable_dws_messages(
                recent_dws_items,
                current_message_ids=curr_id_set,
                chatbot_user_id=chatbot_user_id,
                ignore_senders=cfg.DWS_IGNORE_SENDERS,
            )
    else:
        merged_history = fallback_history

    # 5. 若存在引用消息（含单聊与群聊），提取前后邻居上下文、只读图片字节与只读文档正文
    quote_prompt_block = ""
    quote_router_text = ""
    quote_history_prefix = ""
    quoted_images: list[bytes] = []
    quoted_documents: list[QuotedDocExtract] = []

    valid_quotes = [q for q in quotes if q.text or q.message_id or q.resources]
    if cfg.ENABLE_DWS_QUOTE_CONTEXT and valid_quotes:
        quotes_with_neighbors: list[tuple[DwsQuoteInfo, list[str]]] = []
        for q in valid_quotes:
            neighbor_source_items = recent_dws_items
            target_conv = q.conversation_id or conversation_id
            if dws_ready and target_conv and q.create_time:
                target_idx_in_recent, _ = _find_quote_target_in_items(recent_dws_items, q)
                has_enough_in_window = target_idx_in_recent >= 0 and (
                    target_idx_in_recent >= before_val or len(recent_dws_items) < limit_val
                )
                if not has_enough_in_window:
                    older_limit = min(50, before_val + 4)
                    newer_limit = min(50, after_val + 4)
                    older_res, newer_res = await asyncio.gather(
                        run_dws_readonly_json(
                            [
                                "chat",
                                "message",
                                "list",
                                "--conversation-id",
                                target_conv,
                                "--time",
                                q.create_time,
                                "--direction",
                                "older",
                                "--limit",
                                str(older_limit),
                                "-f",
                                "json",
                            ]
                        ),
                        run_dws_readonly_json(
                            [
                                "chat",
                                "message",
                                "list",
                                "--conversation-id",
                                target_conv,
                                "--time",
                                q.create_time,
                                "--direction",
                                "newer",
                                "--limit",
                                str(newer_limit),
                                "-f",
                                "json",
                            ]
                        ),
                    )
                    around_items: list[dict[str, Any]] = []
                    for sub_payload in (older_res, newer_res):
                        if sub_payload is not None:
                            dws_available = True
                        for raw_m in extract_messages_from_payload(sub_payload):
                            cleaned_m = extract_clean_dws_item(raw_m)
                            if cleaned_m is not None:
                                around_items.append(cleaned_m)
                    if around_items:
                        neighbor_source_items = around_items

            n_lines = _build_quote_neighbor_lines(
                neighbor_items=neighbor_source_items,
                history_messages=history_messages,
                quote=q,
                current_message_ids=curr_id_set,
                before_count=before_val,
                after_count=after_val,
            )
            quotes_with_neighbors.append((q, n_lines))

        media_docs = await fetch_quoted_media_and_docs(
            quotes=valid_quotes,
            conversation_id=conversation_id,
            dws_ready=dws_ready,
            run_dws_fn=run_dws_readonly_json,
        )
        quoted_images = media_docs.images
        quoted_documents = media_docs.documents

        quote_prompt_block, quote_router_text, quote_history_prefix = _format_multi_quote_outputs(
            quotes_with_neighbors=quotes_with_neighbors,
            quoted_documents=quoted_documents,
            quoted_images_count=len(quoted_images),
            raw_user_content=effective_user_content,
        )

    return DwsContextBundle(
        dws_available=dws_available,
        merged_history=merged_history,
        persistable_messages=persistable_messages,
        restored_user_content=restored_user_content,
        quote_info=valid_quotes[0] if valid_quotes else None,
        quotes=valid_quotes,
        quoted_images=quoted_images,
        quoted_documents=quoted_documents,
        quote_prompt_block=quote_prompt_block,
        quote_router_text=quote_router_text,
        quote_history_prefix=quote_history_prefix,
    )
