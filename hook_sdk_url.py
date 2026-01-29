import asyncio
from alibabacloud_dingtalk.im_1_0.client import Client
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_dingtalk.im_1_0 import models as dingtalk_im_models
from alibabacloud_tea_util import models as util_models

# Mock do_request to print URL
async def mock_do_request_async(self, action, version, protocol, method, auth_type, pathname, body_type, request, runtime):
    print(f"🎯 [SDK Hook] Method: {method}")
    print(f"🎯 [SDK Hook] Pathname: {pathname}")
    return {}

async def mock_execute_async(self, params, request, runtime):
    print(f"🎯 [SDK Hook] Execute Params: {params}")
    # params 里面应该包含 pathname
    if hasattr(params, 'pathname'):
        print(f"🎯 [SDK Hook] Pathname: {params.pathname}")
    return {}

# Monkey Patch all possible methods
Client.do_roarequest_async = mock_do_request_async
Client.do_request_async = mock_do_request_async
Client.execute_async = mock_execute_async

async def main():
    config = open_api_models.Config()
    config.protocol = "https"
    config.region_id = "central"
    client = Client(config)
    
    print("--- Testing query_group_member ---")
    try:
        request = dingtalk_im_models.QueryGroupMemberRequest(
            open_conversation_id="test_cid"
        )
        await client.query_group_member_with_options_async(
            request, 
            dingtalk_im_models.QueryGroupMemberHeaders(), 
            util_models.RuntimeOptions()
        )
    except Exception as e:
        print(f"Error: {e}")

    print("\n--- Testing get_scene_group_members ---")
    try:
        # 修正参数: size, cursor
        request2 = dingtalk_im_models.GetSceneGroupMembersRequest(
            open_conversation_id="test_cid",
            size=10,
            cool_app_code="test_code" # 这个接口似乎必填 cool_app_code
        )
        await client.get_scene_group_members_with_options_async(
            request2,
            dingtalk_im_models.GetSceneGroupMembersHeaders(),
            util_models.RuntimeOptions()
        )
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    # 兼容 Python 3.10+
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    loop.run_until_complete(main())