# -*- coding: utf-8 -*-
"""
钉钉 AI 卡片辅助类
使用阿里云官方 SDK (alibabacloud_dingtalk) 替代原生 requests 调用
"""
import json
import uuid
import time
import asyncio
import random
import traceback
from typing import Optional, Dict, Any, List, Callable
from functools import wraps

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from alibabacloud_dingtalk.oauth2_1_0.client import Client as OAuth2Client
from alibabacloud_dingtalk.oauth2_1_0 import models as oauth2_models
from alibabacloud_dingtalk.card_1_0.client import Client as CardClient
from alibabacloud_dingtalk.card_1_0 import models as card_models
from alibabacloud_dingtalk.robot_1_0.client import Client as RobotClient
from alibabacloud_dingtalk.robot_1_0 import models as robot_models
from alibabacloud_dingtalk.im_1_0.client import Client as ImClient
from alibabacloud_dingtalk.im_1_0 import models as im_models
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_tea_util import models as util_models

from app.config import (
    DINGTALK_CORP_ID,
    DINGTALK_COOL_APP_CODE,
    DINGTALK_FORCE_DIRECT,
    DINGTALK_RETRY_ATTEMPTS,
    DINGTALK_RETRY_BASE_DELAY,
    DINGTALK_RETRY_MAX_DELAY,
    DINGTALK_RETRY_JITTER,
    DINGTALK_CONNECT_TIMEOUT_MS,
    DINGTALK_READ_TIMEOUT_MS,
    DINGTALK_RUNTIME_MAX_ATTEMPTS,
    DINGTALK_FILE_DOWNLOAD_TIMEOUT,
    DINGTALK_TOKEN_EARLY_REFRESH_SEC,
)

RETRYABLE_ERROR_KEYWORDS = (
    "ssl",
    "eof",
    "timeout",
    "timed out",
    "connection reset",
    "connection aborted",
    "connection refused",
    "connection error",
    "temporarily unavailable",
    "max retries exceeded",
    "bad gateway",
    "service unavailable",
    "gateway timeout",
)

AUTH_ERROR_KEYWORDS = (
    "401",
    "403",
    "unauthorized",
    "forbidden",
    "invalid access token",
    "token expired",
)

PERMANENT_FAIL = "__PERMANENT_FAIL__"


def _is_auth_error(error: Exception) -> bool:
    error_msg = str(error).lower()
    return any(keyword in error_msg for keyword in AUTH_ERROR_KEYWORDS)


def _is_retryable_exception(error: Exception) -> bool:
    if _is_auth_error(error):
        return False
    error_msg = str(error).lower()
    return any(keyword in error_msg for keyword in RETRYABLE_ERROR_KEYWORDS)


def _retry_wait_seconds(attempt_number: int, base_delay: float, max_delay: float, jitter: float) -> float:
    base = min(max_delay, base_delay * (2 ** max(attempt_number - 1, 0)))
    if jitter > 0:
        base += random.uniform(0, jitter)
    return base


def _build_requests_retry(max_attempts: int, base_delay: float) -> Retry:
    retry_total = max(1, max_attempts - 1)
    kwargs = {
        "total": retry_total,
        "connect": retry_total,
        "read": retry_total,
        "status": retry_total,
        "status_forcelist": [429, 500, 502, 503, 504],
        "backoff_factor": max(0.1, base_delay),
        "raise_on_status": False,
    }
    try:
        return Retry(allowed_methods=frozenset(["GET"]), **kwargs)
    except TypeError:
        return Retry(method_whitelist=frozenset(["GET"]), **kwargs)


def async_retry(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 8.0,
    jitter: float = 0.25,
    retry_on_none: bool = True,
    retry_if: Optional[Callable[[Exception], bool]] = None,
):
    """
    异步重试装饰器

    Args:
        max_attempts: 最大尝试次数
        base_delay: 基础延迟时间（秒）
        max_delay: 最大退避时间（秒）
        jitter: 随机抖动（秒）
        retry_on_none: 返回 None 是否重试
        retry_if: 异常是否可重试的判断函数
    """

    def decorator(func: Callable):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            for attempt in range(1, max_attempts + 1):
                try:
                    result = await func(*args, **kwargs)
                    if result is not None or not retry_on_none:
                        if attempt > 1:
                            print(f"✅ {func.__name__} 成功（重试 {attempt - 1} 次后）")
                        return result

                    if attempt < max_attempts:
                        wait_time = _retry_wait_seconds(attempt, base_delay, max_delay, jitter)
                        print(
                            f"⏳ {func.__name__} 返回 None，第 {attempt}/{max_attempts} 次，"
                            f"{wait_time:.1f}秒后重试..."
                        )
                        await asyncio.sleep(wait_time)

                except Exception as e:
                    should_retry = retry_if(e) if retry_if else True
                    if attempt < max_attempts and should_retry:
                        wait_time = _retry_wait_seconds(attempt, base_delay, max_delay, jitter)
                        print(
                            f"⚠️ {func.__name__} 异常（第 {attempt}/{max_attempts} 次），"
                            f"{wait_time:.1f}秒后重试: {e}"
                        )
                        await asyncio.sleep(wait_time)
                        continue

                    if not should_retry:
                        print(f"⛔ {func.__name__} 非重试型异常，停止重试: {e}")
                    else:
                        print(f"❌ {func.__name__} 最终失败（已重试 {max_attempts} 次）: {e}")
                    traceback.print_exc()
                    return None

            print(f"❌ {func.__name__} 最终失败（结果为空，已重试 {max_attempts} 次）")
            return None

        return wrapper

    return decorator


def _create_client() -> open_api_models.Config:
    """创建 OpenAPI 配置"""
    config = open_api_models.Config()
    config.protocol = "https"
    config.region_id = "central"
    return config


class DingTalkCardHelper:
    """钉钉 AI 卡片辅助类"""

    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.access_token: Optional[str] = None
        self.token_expires_at: float = 0
        self.retry_attempts = max(1, DINGTALK_RETRY_ATTEMPTS)
        self.retry_base_delay = max(0.1, DINGTALK_RETRY_BASE_DELAY)
        self.retry_max_delay = max(self.retry_base_delay, DINGTALK_RETRY_MAX_DELAY)
        self.retry_jitter = max(0.0, DINGTALK_RETRY_JITTER)
        self.connect_timeout_ms = max(1000, DINGTALK_CONNECT_TIMEOUT_MS)
        self.read_timeout_ms = max(1000, DINGTALK_READ_TIMEOUT_MS)

        # 初始化各个 SDK 客户端
        config = _create_client()
        self.oauth2_client = OAuth2Client(config)
        self.card_client = CardClient(config)
        self.robot_client = RobotClient(config)
        self.im_client = ImClient(config)

        # SDK 运行时配置
        self.runtime = util_models.RuntimeOptions()
        self.runtime.connect_timeout = self.connect_timeout_ms
        self.runtime.read_timeout = self.read_timeout_ms
        self.runtime.max_attempts = max(1, DINGTALK_RUNTIME_MAX_ATTEMPTS)

        if DINGTALK_FORCE_DIRECT:
            # 强制钉钉 API 直连，规避代理链路导致的 TLS EOF 抖动
            self.runtime.http_proxy = ""
            self.runtime.https_proxy = ""
            self.runtime.no_proxy = "api.dingtalk.com,oapi.dingtalk.com,.dingtalk.com"

        self.download_session = requests.Session()
        retry_adapter = HTTPAdapter(
            max_retries=_build_requests_retry(self.retry_attempts, self.retry_base_delay)
        )
        self.download_session.mount("https://", retry_adapter)
        self.download_session.mount("http://", retry_adapter)
        if DINGTALK_FORCE_DIRECT:
            self.download_session.trust_env = False

        # Streaming updates (card stream_update) are sensitive to concurrency and update rates.
        # We serialize updates per out_track_id to avoid DingTalk returning intermittent 500s.
        self._stream_locks: Dict[str, asyncio.Lock] = {}
        self._last_stream_at: Dict[str, float] = {}

    def _get_stream_lock(self, out_track_id: str) -> asyncio.Lock:
        lock = self._stream_locks.get(out_track_id)
        if lock is None:
            lock = asyncio.Lock()
            self._stream_locks[out_track_id] = lock
        return lock

    @async_retry(
        max_attempts=DINGTALK_RETRY_ATTEMPTS,
        base_delay=DINGTALK_RETRY_BASE_DELAY,
        max_delay=DINGTALK_RETRY_MAX_DELAY,
        jitter=DINGTALK_RETRY_JITTER,
        retry_if=_is_retryable_exception,
    )
    async def get_access_token(self, force_refresh: bool = False) -> Optional[str]:
        """获取钉钉 Access Token（带重试机制）"""
        if not force_refresh and self.access_token and time.time() < self.token_expires_at:
            return self.access_token

        loop = asyncio.get_running_loop()

        def do_get_token():
            if DINGTALK_CORP_ID:
                # 企业内部应用
                request = oauth2_models.GetCorpAccessTokenRequest(
                    suitekey=self.client_id,
                    suitesecret=self.client_secret,
                    auth_corpid=DINGTALK_CORP_ID,
                )
                response = self.oauth2_client.get_corp_access_token(request)
            else:
                # 机器人应用
                request = oauth2_models.GetAccessTokenRequest(
                    app_key=self.client_id,
                    app_secret=self.client_secret,
                )
                response = self.oauth2_client.get_access_token(request)

            if response.body:
                return {
                    "access_token": response.body.access_token,
                    "expires_in": response.body.expire_in,
                }
            return None

        data = await loop.run_in_executor(None, do_get_token)
        if data:
            self.access_token = data["access_token"]
            expires_in = int(data["expires_in"])
            self.token_expires_at = time.time() + max(
                30,
                expires_in - DINGTALK_TOKEN_EARLY_REFRESH_SEC,
            )
            print(f"✅ AccessToken 获取成功，有效期: {data['expires_in']}秒")
            return self.access_token

        return None

    def _convert_card_data(self, card_data: Dict[str, Any]) -> Dict[str, str]:
        """将卡片数据转换为字符串格式"""
        result = {}
        for key, value in card_data.items():
            if isinstance(value, str):
                result[key] = value
            else:
                try:
                    result[key] = json.dumps(value, ensure_ascii=False)
                except Exception:
                    result[key] = ""
        return result

    async def create_and_deliver(
        self,
        conversation_id: str,
        template_id: str,
        card_data: Dict[str, Any],
        at_user_ids: Optional[List[str]] = None,
    ) -> Optional[str]:
        """创建并发送 AI 卡片"""
        token = await self.get_access_token()
        if not token:
            return None

        at_users_map = {}
        if at_user_ids:
            for uid in at_user_ids:
                at_users_map[uid] = uid

        card_param_map = self._convert_card_data(card_data)
        out_track_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()

        def do_create(current_token: str):
            try:
                headers = card_models.CreateAndDeliverHeaders()
                headers.x_acs_dingtalk_access_token = current_token

                card_data_obj = card_models.CreateAndDeliverRequestCardData(
                    card_param_map=card_param_map
                )
                im_group_deliver = card_models.CreateAndDeliverRequestImGroupOpenDeliverModel(
                    robot_code=self.client_id,
                    at_user_ids=at_users_map,
                )
                im_group_space = card_models.CreateAndDeliverRequestImGroupOpenSpaceModel(
                    support_forward=True
                )

                request = card_models.CreateAndDeliverRequest(
                    card_template_id=template_id,
                    out_track_id=out_track_id,
                    callback_type="STREAM",
                    card_data=card_data_obj,
                    open_space_id=f"dtv1.card//im_group.{conversation_id}",
                    im_group_open_deliver_model=im_group_deliver,
                    im_group_open_space_model=im_group_space,
                )

                response = self.card_client.create_and_deliver_with_options(
                    request,
                    headers,
                    self.runtime,
                )

                if response.status_code == 200:
                    print(f"✅ 卡片创建成功: {out_track_id}")
                    return out_track_id
                if response.status_code in (401, 403):
                    print(f"⚠️ 卡片创建权限错误: HTTP {response.status_code}")
                    return "401"
                if response.status_code in (429, 500, 502, 503, 504):
                    print(f"⚠️ 卡片创建临时失败: HTTP {response.status_code}")
                    return None

                print(f"❌ 卡片创建失败: HTTP {response.status_code}")
                return False

            except Exception as e:
                err_msg = str(e)
                if _is_auth_error(e):
                    print(f"⚠️ 卡片创建认证错误: {err_msg}")
                    return "401"
                if _is_retryable_exception(e):
                    print(f"⚠️ 卡片创建可重试异常: {err_msg}")
                    raise
                print(f"⚠️ 发送卡片失败: {err_msg}")
                traceback.print_exc()
                return False

        @async_retry(
            max_attempts=DINGTALK_RETRY_ATTEMPTS,
            base_delay=DINGTALK_RETRY_BASE_DELAY,
            max_delay=DINGTALK_RETRY_MAX_DELAY,
            jitter=DINGTALK_RETRY_JITTER,
            retry_if=_is_retryable_exception,
        )
        async def _create_with_retry() -> Optional[str]:
            current_token = self.access_token or token
            result = await loop.run_in_executor(None, do_create, current_token)

            if result == "401":
                print("⚠️ Token 可能过期，刷新后重试...")
                refreshed = await self.get_access_token(force_refresh=True)
                if not refreshed:
                    return None
                result = await loop.run_in_executor(None, do_create, refreshed)

            if result is False:
                return PERMANENT_FAIL
            if isinstance(result, str) and result != "401":
                return result
            return None

        result = await _create_with_retry()
        if result == PERMANENT_FAIL:
            return None
        return result

    async def stream_update(
        self,
        out_track_id: str,
        content: str,
        is_finalize: bool = False,
        is_full: bool = True,
        content_key: str = "msgContent",
    ) -> bool:
        """流式更新卡片内容"""
        token = await self.get_access_token()
        if not token:
            return False

        # Drop overly-frequent non-final updates to reduce server-side 500/unknownError bursts.
        # Final updates are always delivered (with retry) to keep the UI consistent.
        now = time.time()
        last = self._last_stream_at.get(out_track_id, 0.0)
        if not is_finalize and (now - last) < 0.15:
            return True

        loop = asyncio.get_running_loop()

        def do_update(current_token: str):
            try:
                headers = card_models.StreamingUpdateHeaders()
                headers.x_acs_dingtalk_access_token = current_token

                request = card_models.StreamingUpdateRequest(
                    out_track_id=out_track_id,
                    guid=str(uuid.uuid4()),
                    key=content_key,
                    content=content,
                    is_full=is_full,
                    is_finalize=is_finalize,
                )

                response = self.card_client.streaming_update_with_options(
                    request,
                    headers,
                    self.runtime,
                )

                if response.status_code == 200:
                    return True

                if response.status_code in (401, 403):
                    return "401"
                # Let transient statuses fall through as retryable None.
                if response.status_code in (429, 500, 502, 503, 504):
                    return None

                print(f"❌ 流式更新失败: HTTP {response.status_code} (key={content_key})")
                return False

            except Exception as e:
                if _is_auth_error(e):
                    return "401"
                if _is_retryable_exception(e):
                    raise
                print(f"⚠️ 流式更新失败: {e}")
                return False

        # Serialize updates per card to avoid concurrent stream_update calls (typing + content).
        async with self._get_stream_lock(out_track_id):
            # Update last-send time only when we're actually attempting a call.
            self._last_stream_at[out_track_id] = time.time()

            # Non-final updates should never block the main flow with long retries.
            # Final updates retry more aggressively.
            max_attempts = DINGTALK_RETRY_ATTEMPTS if is_finalize else 2

            @async_retry(
                max_attempts=max_attempts,
                base_delay=DINGTALK_RETRY_BASE_DELAY,
                max_delay=DINGTALK_RETRY_MAX_DELAY,
                jitter=DINGTALK_RETRY_JITTER,
                retry_if=_is_retryable_exception,
            )
            async def _update_with_retry() -> Optional[bool]:
                current = self.access_token or token
                result = await loop.run_in_executor(None, do_update, current)

                if result == "401":
                    refreshed = await self.get_access_token(force_refresh=True)
                    if not refreshed:
                        return None
                    result = await loop.run_in_executor(None, do_update, refreshed)

                if result is False:
                    return PERMANENT_FAIL
                if result is None:
                    return None
                return bool(result)

            try:
                result = await _update_with_retry()
                return bool(result and result != PERMANENT_FAIL)
            except Exception as e:
                # For non-final updates, suppress noisy errors (they'll be followed by later updates).
                if is_finalize:
                    print(
                        f"⚠️ 流式更新异常(out_track_id={out_track_id}, key={content_key}, finalize={is_finalize}, len={len(content or '')}): {e}"
                    )
                return False

    async def update_card(
        self,
        out_track_id: str,
        card_data: Dict[str, Any],
    ) -> bool:
        """全量更新卡片"""
        token = await self.get_access_token()
        if not token:
            return False

        card_param_map = self._convert_card_data(card_data)
        loop = asyncio.get_running_loop()

        def do_update():
            try:
                headers = card_models.UpdateCardHeaders()
                headers.x_acs_dingtalk_access_token = token

                card_data_obj = card_models.UpdateCardRequestCardData(
                    card_param_map=card_param_map
                )

                request = card_models.UpdateCardRequest(
                    out_track_id=out_track_id,
                    card_data=card_data_obj,
                )

                response = self.card_client.update_card_with_options(
                    request,
                    headers,
                    self.runtime,
                )

                if response.status_code == 200:
                    print(f"✅ 卡片更新成功: {out_track_id}")
                    return True

                print(f"❌ 卡片更新失败: HTTP {response.status_code}")
                return False

            except Exception as e:
                print(f"⚠️ 更新卡片失败: {e}")
                traceback.print_exc()
                return False

        try:
            return await loop.run_in_executor(None, do_update)
        except Exception as e:
            print(f"❌ 卡片全量更新异常: {e}")
            traceback.print_exc()
            return False

    async def download_file(self, download_code: str) -> Optional[bytes]:
        """下载机器人消息中的文件"""
        token = await self.get_access_token()
        if not token:
            return None

        loop = asyncio.get_running_loop()

        def do_download():
            try:
                headers = robot_models.RobotMessageFileDownloadHeaders()
                headers.x_acs_dingtalk_access_token = token

                request = robot_models.RobotMessageFileDownloadRequest(
                    download_code=download_code,
                    robot_code=self.client_id,
                )

                response = self.robot_client.robot_message_file_download_with_options(
                    request,
                    headers,
                    self.runtime,
                )

                if response.status_code == 200 and response.body:
                    download_url = response.body.download_url
                    if download_url:
                        print(f"📥 下载文件: {download_url[:50]}...")
                        file_resp = self.download_session.get(
                            download_url,
                            timeout=max(5, DINGTALK_FILE_DOWNLOAD_TIMEOUT),
                        )
                        file_resp.raise_for_status()
                        print(f"✅ 文件下载成功: {len(file_resp.content)} bytes")
                        return file_resp.content

                if response.status_code in (429, 500, 502, 503, 504):
                    return None

                print("❌ 获取下载链接失败")
                return False

            except Exception as e:
                if _is_retryable_exception(e):
                    raise
                print(f"⚠️ 下载文件失败: {e}")
                traceback.print_exc()
                return False

        @async_retry(
            max_attempts=DINGTALK_RETRY_ATTEMPTS,
            base_delay=DINGTALK_RETRY_BASE_DELAY,
            max_delay=DINGTALK_RETRY_MAX_DELAY,
            jitter=DINGTALK_RETRY_JITTER,
            retry_if=_is_retryable_exception,
        )
        async def _download_with_retry():
            result = await loop.run_in_executor(None, do_download)
            if result is False:
                return PERMANENT_FAIL
            return result

        result = await _download_with_retry()
        if result == PERMANENT_FAIL:
            return None
        return result

    async def upload_media(
        self,
        content: bytes,
        filetype: str = "image",
        filename: str = "image.png",
        mimetype: str = "image/png",
    ) -> Optional[str]:
        """
        上传媒体文件到钉钉，返回 media_id。

        复用 dingtalk-stream SDK 的思路：调用 oapi /media/upload?access_token=...
        """
        token = await self.get_access_token()
        if not token:
            return None

        loop = asyncio.get_running_loop()

        def do_upload(current_token: str):
            try:
                files = {"media": (filename, content, mimetype)}
                values = {"type": filetype}
                url = f"https://oapi.dingtalk.com/media/upload?access_token={current_token}"
                resp = self.download_session.post(url, data=values, files=files, timeout=30)
                if resp.status_code == 401:
                    return "401"
                resp.raise_for_status()
                data = resp.json()
                media_id = data.get("media_id")
                return media_id or False
            except Exception as e:
                if _is_retryable_exception(e):
                    raise
                print(f"⚠️ 上传媒体失败: {e}")
                traceback.print_exc()
                return False

        @async_retry(
            max_attempts=DINGTALK_RETRY_ATTEMPTS,
            base_delay=DINGTALK_RETRY_BASE_DELAY,
            max_delay=DINGTALK_RETRY_MAX_DELAY,
            jitter=DINGTALK_RETRY_JITTER,
            retry_if=_is_retryable_exception,
        )
        async def _upload_with_retry() -> Optional[str]:
            current = self.access_token or token
            result = await loop.run_in_executor(None, do_upload, current)
            if result == "401":
                refreshed = await self.get_access_token(force_refresh=True)
                if not refreshed:
                    return None
                result = await loop.run_in_executor(None, do_upload, refreshed)
            if result is False:
                return PERMANENT_FAIL
            if isinstance(result, str) and result not in {"401"}:
                return result
            return None

        result = await _upload_with_retry()
        if result == PERMANENT_FAIL:
            return None
        return result

    async def send_group_message(
        self,
        open_conversation_id: str,
        msg_key: str,
        msg_param: str,
    ) -> bool:
        """机器人发送群聊消息（OpenAPI）"""
        token = await self.get_access_token()
        if not token:
            return False

        loop = asyncio.get_running_loop()

        def do_send(current_token: str):
            try:
                headers = robot_models.OrgGroupSendHeaders()
                headers.x_acs_dingtalk_access_token = current_token
                req = robot_models.OrgGroupSendRequest(
                    cool_app_code=DINGTALK_COOL_APP_CODE or None,
                    msg_key=msg_key,
                    msg_param=msg_param,
                    open_conversation_id=open_conversation_id,
                    robot_code=self.client_id,
                )
                resp = self.robot_client.org_group_send_with_options(req, headers, self.runtime)
                return bool(resp and resp.status_code == 200)
            except Exception as e:
                if _is_auth_error(e):
                    return "401"
                if _is_retryable_exception(e):
                    raise
                print(f"⚠️ 群消息发送失败: {e}")
                traceback.print_exc()
                return False

        @async_retry(
            max_attempts=DINGTALK_RETRY_ATTEMPTS,
            base_delay=DINGTALK_RETRY_BASE_DELAY,
            max_delay=DINGTALK_RETRY_MAX_DELAY,
            jitter=DINGTALK_RETRY_JITTER,
            retry_if=_is_retryable_exception,
        )
        async def _send_with_retry() -> Optional[bool]:
            current = self.access_token or token
            result = await loop.run_in_executor(None, do_send, current)
            if result == "401":
                refreshed = await self.get_access_token(force_refresh=True)
                if not refreshed:
                    return None
                result = await loop.run_in_executor(None, do_send, refreshed)
            if result is False:
                return PERMANENT_FAIL
            return bool(result)

        result = await _send_with_retry()
        return bool(result and result != PERMANENT_FAIL)

    async def send_private_chat_message(
        self,
        open_conversation_id: str,
        msg_key: str,
        msg_param: str,
    ) -> bool:
        """人与人会话中机器人发送消息（OpenAPI）"""
        token = await self.get_access_token()
        if not token:
            return False

        loop = asyncio.get_running_loop()

        def do_send(current_token: str):
            try:
                headers = robot_models.PrivateChatSendHeaders()
                headers.x_acs_dingtalk_access_token = current_token
                req = robot_models.PrivateChatSendRequest(
                    cool_app_code=DINGTALK_COOL_APP_CODE or None,
                    msg_key=msg_key,
                    msg_param=msg_param,
                    open_conversation_id=open_conversation_id,
                    robot_code=self.client_id,
                )
                resp = self.robot_client.private_chat_send_with_options(req, headers, self.runtime)
                return bool(resp and resp.status_code == 200)
            except Exception as e:
                if _is_auth_error(e):
                    return "401"
                if _is_retryable_exception(e):
                    raise
                print(f"⚠️ 单聊消息发送失败: {e}")
                traceback.print_exc()
                return False

        @async_retry(
            max_attempts=DINGTALK_RETRY_ATTEMPTS,
            base_delay=DINGTALK_RETRY_BASE_DELAY,
            max_delay=DINGTALK_RETRY_MAX_DELAY,
            jitter=DINGTALK_RETRY_JITTER,
            retry_if=_is_retryable_exception,
        )
        async def _send_with_retry() -> Optional[bool]:
            current = self.access_token or token
            result = await loop.run_in_executor(None, do_send, current)
            if result == "401":
                refreshed = await self.get_access_token(force_refresh=True)
                if not refreshed:
                    return None
                result = await loop.run_in_executor(None, do_send, refreshed)
            if result is False:
                return PERMANENT_FAIL
            return bool(result)

        result = await _send_with_retry()
        return bool(result and result != PERMANENT_FAIL)

    async def get_group_info(self, conversation_id: str) -> Optional[Any]:
        """获取群信息"""
        token = await self.get_access_token()
        if not token:
            return None

        loop = asyncio.get_running_loop()

        def do_get_info():
            try:
                headers = im_models.GetSceneGroupInfoHeaders()
                headers.x_acs_dingtalk_access_token = token

                request = im_models.GetSceneGroupInfoRequest(
                    open_conversation_id=conversation_id
                )

                response = self.im_client.get_scene_group_info_with_options(
                    request,
                    headers,
                    self.runtime,
                )

                if response.status_code == 200 and response.body:
                    class GroupInfo:
                        def __init__(self, title):
                            self.title = title

                    return GroupInfo(response.body.title or "Unknown Group")

                print("⚠️ 获取群信息失败")
                return None

            except Exception as e:
                print(f"⚠️ 获取群信息异常: {e}")
                return None

        try:
            return await loop.run_in_executor(None, do_get_info)
        except Exception as e:
            print(f"❌ 获取群信息最终失败: {e}")
            traceback.print_exc()
            return None
