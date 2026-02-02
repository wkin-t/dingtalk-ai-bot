# -*- coding: utf-8 -*-
"""
钉钉 AI 卡片辅助类
使用阿里云官方 SDK (alibabacloud_dingtalk) 替代原生 requests 调用
"""
import json
import uuid
import time
import asyncio
import traceback
from typing import Optional, Dict, Any, List

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

from app.config import DINGTALK_CORP_ID


def _create_client() -> open_api_models.Config:
    """创建 OpenAPI 配置"""
    config = open_api_models.Config()
    config.protocol = 'https'
    config.region_id = 'central'
    return config


class DingTalkCardHelper:
    """钉钉 AI 卡片辅助类"""

    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.access_token: Optional[str] = None
        self.token_expires_at: float = 0

        # 初始化各个 SDK 客户端
        config = _create_client()
        self.oauth2_client = OAuth2Client(config)
        self.card_client = CardClient(config)
        self.robot_client = RobotClient(config)
        self.im_client = ImClient(config)

        # 运行时配置 (钉钉是国内服务，不需要代理)
        self.runtime = util_models.RuntimeOptions()
        self.runtime.connect_timeout = 15000  # 15秒 (增加超时时间)
        self.runtime.read_timeout = 60000     # 60秒 (增加读取超时)
        self.runtime.max_attempts = 3         # 最多重试 3 次

    async def get_access_token(self, force_refresh: bool = False) -> Optional[str]:
        """获取钉钉 Access Token（带重试机制）"""
        if not force_refresh and self.access_token and time.time() < self.token_expires_at:
            return self.access_token

        loop = asyncio.get_running_loop()

        def do_get_token():
            try:
                if DINGTALK_CORP_ID:
                    # 企业内部应用
                    request = oauth2_models.GetCorpAccessTokenRequest(
                        suitekey=self.client_id,
                        suitesecret=self.client_secret,
                        auth_corpid=DINGTALK_CORP_ID
                    )
                    response = self.oauth2_client.get_corp_access_token(request)
                else:
                    # 机器人应用
                    request = oauth2_models.GetAccessTokenRequest(
                        app_key=self.client_id,
                        app_secret=self.client_secret
                    )
                    response = self.oauth2_client.get_access_token(request)

                if response.body:
                    return {
                        'access_token': response.body.access_token,
                        'expires_in': response.body.expire_in
                    }
                return None
            except Exception as e:
                print(f"❌ 获取 AccessToken 失败: {e}")
                traceback.print_exc()
                return None

        # 应用层重试逻辑：最多重试 3 次
        max_retries = 3
        for attempt in range(max_retries):
            try:
                data = await loop.run_in_executor(None, do_get_token)
                if data:
                    self.access_token = data['access_token']
                    self.token_expires_at = time.time() + int(data['expires_in']) - 60
                    if attempt > 0:
                        print(f"✅ AccessToken 获取成功（重试 {attempt} 次后），有效期: {data['expires_in']}秒")
                    else:
                        print(f"✅ AccessToken 获取成功，有效期: {data['expires_in']}秒")
                    return self.access_token

                # 如果返回 None，等待后重试
                if attempt < max_retries - 1:
                    wait_time = (attempt + 1) * 1.0  # 递增等待时间：1s, 2s, 3s
                    print(f"⏳ AccessToken 获取失败，{wait_time}秒后重试...")
                    await asyncio.sleep(wait_time)

            except Exception as e:
                if attempt < max_retries - 1:
                    wait_time = (attempt + 1) * 1.0
                    print(f"⚠️ AccessToken 异常（第 {attempt + 1}/{max_retries} 次），{wait_time}秒后重试: {e}")
                    await asyncio.sleep(wait_time)
                else:
                    print(f"❌ AccessToken 获取最终失败（已重试 {max_retries} 次）: {e}")
                    traceback.print_exc()

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
                except:
                    result[key] = ""
        return result

    async def create_and_deliver(
        self,
        conversation_id: str,
        template_id: str,
        card_data: Dict[str, Any],
        at_user_ids: Optional[List[str]] = None
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

        def do_create():
            try:
                # 构造请求头
                headers = card_models.CreateAndDeliverHeaders()
                headers.x_acs_dingtalk_access_token = token

                # 构造卡片数据
                card_data_obj = card_models.CreateAndDeliverRequestCardData(
                    card_param_map=card_param_map
                )

                # 构造群发送模型
                im_group_deliver = card_models.CreateAndDeliverRequestImGroupOpenDeliverModel(
                    robot_code=self.client_id,
                    at_user_ids=at_users_map
                )

                # 构造群空间模型
                im_group_space = card_models.CreateAndDeliverRequestImGroupOpenSpaceModel(
                    support_forward=True
                )

                # 构造请求
                request = card_models.CreateAndDeliverRequest(
                    card_template_id=template_id,
                    out_track_id=out_track_id,
                    callback_type='STREAM',
                    card_data=card_data_obj,
                    open_space_id=f'dtv1.card//im_group.{conversation_id}',
                    im_group_open_deliver_model=im_group_deliver,
                    im_group_open_space_model=im_group_space
                )

                response = self.card_client.create_and_deliver_with_options(
                    request, headers, self.runtime
                )

                if response.status_code == 200:
                    print(f"✅ 卡片创建成功: {out_track_id}")
                    return out_track_id

                print(f"❌ 卡片创建失败: HTTP {response.status_code}")
                return None

            except Exception as e:
                error_msg = str(e)
                if '401' in error_msg or 'Unauthorized' in error_msg:
                    return '401'
                print(f"⚠️ 发送卡片失败: {e}")
                traceback.print_exc()
                return None

        # 应用层重试逻辑：最多重试 3 次
        max_retries = 3
        for attempt in range(max_retries):
            try:
                result = await loop.run_in_executor(None, do_create)

                if result == '401':
                    print("⚠️ Token 可能过期，刷新重试...")
                    await self.get_access_token(force_refresh=True)
                    result = await loop.run_in_executor(None, do_create)

                if result and result != '401':
                    return result

                # 如果失败，等待后重试
                if attempt < max_retries - 1:
                    wait_time = (attempt + 1) * 1.0
                    print(f"⏳ 卡片创建失败，{wait_time}秒后重试...")
                    await asyncio.sleep(wait_time)

            except Exception as e:
                if attempt < max_retries - 1:
                    wait_time = (attempt + 1) * 1.0
                    print(f"⚠️ 卡片创建异常（第 {attempt + 1}/{max_retries} 次），{wait_time}秒后重试: {e}")
                    await asyncio.sleep(wait_time)
                else:
                    print(f"❌ 卡片创建最终失败: {e}")
                    traceback.print_exc()

        return None

    async def stream_update(
        self,
        out_track_id: str,
        content: str,
        is_finalize: bool = False,
        is_full: bool = True,
        content_key: str = "msgContent"
    ) -> bool:
        """流式更新卡片内容"""
        token = await self.get_access_token()
        if not token:
            return False

        loop = asyncio.get_running_loop()

        def do_update():
            try:
                headers = card_models.StreamingUpdateHeaders()
                headers.x_acs_dingtalk_access_token = token

                request = card_models.StreamingUpdateRequest(
                    out_track_id=out_track_id,
                    guid=str(uuid.uuid4()),
                    key=content_key,
                    content=content,
                    is_full=is_full,
                    is_finalize=is_finalize
                )

                response = self.card_client.streaming_update_with_options(
                    request, headers, self.runtime
                )

                if response.status_code == 200:
                    return True

                print(f"❌ 流式更新失败: HTTP {response.status_code}")
                return False

            except Exception as e:
                print(f"⚠️ 流式更新失败: {e}")
                return False

        try:
            return await loop.run_in_executor(None, do_update)
        except Exception as e:
            print(f"⚠️ 流式更新异常: {e}")
            return False

    async def update_card(
        self,
        out_track_id: str,
        card_data: Dict[str, Any]
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
                    card_data=card_data_obj
                )

                response = self.card_client.update_card_with_options(
                    request, headers, self.runtime
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
                    robot_code=self.client_id
                )

                response = self.robot_client.robot_message_file_download_with_options(
                    request, headers, self.runtime
                )

                if response.status_code == 200 and response.body:
                    download_url = response.body.download_url
                    if download_url:
                        # 下载文件内容
                        import requests
                        print(f"📥 下载文件: {download_url[:50]}...")
                        file_resp = requests.get(download_url, timeout=30)
                        file_resp.raise_for_status()
                        print(f"✅ 文件下载成功: {len(file_resp.content)} bytes")
                        return file_resp.content

                print("❌ 获取下载链接失败")
                return None

            except Exception as e:
                print(f"⚠️ 下载文件失败: {e}")
                traceback.print_exc()
                return None

        try:
            return await loop.run_in_executor(None, do_download)
        except Exception as e:
            print(f"❌ 下载文件异常: {e}")
            traceback.print_exc()
            return None

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
                    request, headers, self.runtime
                )

                if response.status_code == 200 and response.body:
                    class GroupInfo:
                        def __init__(self, title):
                            self.title = title

                    return GroupInfo(response.body.title or 'Unknown Group')

                print(f"⚠️ 获取群信息失败")
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
