# -*- coding: utf-8 -*-
import os
import logging
from flask import Flask, request
from tencentcloud.common import credential
from tencentcloud.common.profile.client_profile import ClientProfile
from tencentcloud.common.profile.http_profile import HttpProfile
from tencentcloud.vpc.v20170312 import vpc_client, models

app = Flask(__name__)

# ================= 🔧 配置区域 (读取环境变量) =================
# 这样设计是为了安全，密码和Key都存在docker-compose.yml里，代码文件是干净的
SECRET_ID = os.getenv("TENCENT_SECRET_ID", "")
SECRET_KEY = os.getenv("TENCENT_SECRET_KEY", "")
REGION = os.getenv("TENCENT_REGION", "ap-guangzhou")
SECURITY_GROUP_ID = os.getenv("SECURITY_GROUP_ID", "")
TARGET_PORT = os.getenv("TARGET_PORT", "ALL") # 默认为 ALL，放行所有端口
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN", "")
# ==========================================================

# 设置日志格式
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def get_client():
    cred = credential.Credential(SECRET_ID, SECRET_KEY)
    httpProfile = HttpProfile()
    httpProfile.endpoint = "vpc.tencentcloudapi.com"
    clientProfile = ClientProfile()
    clientProfile.httpProfile = httpProfile
    return vpc_client.VpcClient(cred, REGION, clientProfile)


def update_security_group(current_ip, device_id):
    try:
        client = get_client()
        # 构造唯一的设备标识
        description_tag = f"Device: {device_id}"

        # 1. 查询当前规则
        req_desc = models.DescribeSecurityGroupPoliciesRequest()
        req_desc.SecurityGroupId = SECURITY_GROUP_ID
        resp_desc = client.DescribeSecurityGroupPolicies(req_desc)

        found_old_rules = []
        ip_changed = False
        current_ip_rules_exist = False # 标记当前IP是否已经有规则了

        # 2. 遍历现有规则 (查找是否已存在该设备)
        if resp_desc.SecurityGroupPolicySet.Ingress:
            for policy in resp_desc.SecurityGroupPolicySet.Ingress:
                # 获取备注，防止为None报错
                current_desc = policy.PolicyDescription if policy.PolicyDescription else ""

                # 匹配条件：备注里包含设备ID
                if description_tag in current_desc:
                    # === 🔍 核心逻辑：IP 比对 ===
                    # 腾讯云返回的 CidrBlock 可能是 "1.2.3.4" 也可能是 "1.2.3.4/32"
                    remote_ip = policy.CidrBlock.split("/")[0]

                    if remote_ip == current_ip:
                        current_ip_rules_exist = True
                        # 继续循环，确保 TCP 和 UDP 都在
                    else:
                        # IP 变了，记录这条旧规则，稍后删除
                        ip_changed = True
                        found_old_rules.append(policy)

        if current_ip_rules_exist and not ip_changed:
            logger.info(f"设备 [{device_id}] IP 未变化 ({current_ip})，且规则已存在，跳过更新。")
            return True, f"IP 未变 ({current_ip})"

        # 3. 删除旧规则 (如果有)
        if found_old_rules:
            logger.info(f"设备 [{device_id}] IP 变更，删除 {len(found_old_rules)} 条旧规则...")
            req_del = models.DeleteSecurityGroupPoliciesRequest()
            req_del.SecurityGroupId = SECURITY_GROUP_ID
            req_del.SecurityGroupPolicySet = models.SecurityGroupPolicySet()
            
            del_policies = []
            for old_policy in found_old_rules:
                clean_policy = models.SecurityGroupPolicy()
                if old_policy.PolicyIndex is not None:
                    clean_policy.PolicyIndex = old_policy.PolicyIndex
                else:
                    clean_policy.Protocol = old_policy.Protocol
                    clean_policy.Port = str(old_policy.Port)
                    clean_policy.Action = old_policy.Action
                    clean_policy.CidrBlock = old_policy.CidrBlock
                del_policies.append(clean_policy)

            req_del.SecurityGroupPolicySet.Ingress = del_policies
            client.DeleteSecurityGroupPolicies(req_del)

        # 4. 添加新规则 (TCP 和 UDP)
        # 如果当前IP已经有规则（可能是部分规则），为了保险起见，我们还是尝试添加
        # 腾讯云 API 通常会自动去重或忽略已存在的完全相同规则
        
        req_add = models.CreateSecurityGroupPoliciesRequest()
        req_add.SecurityGroupId = SECURITY_GROUP_ID
        req_add.SecurityGroupPolicySet = models.SecurityGroupPolicySet()
        
        new_policies = []
        protocols = ["TCP", "UDP"]
        
        for proto in protocols:
            policy_new = models.SecurityGroupPolicy()
            policy_new.Port = str(TARGET_PORT)
            policy_new.CidrBlock = f"{current_ip}/32"
            policy_new.Action = "ACCEPT"
            policy_new.Protocol = proto
            # 写入备注，作为下次识别的依据
            policy_new.PolicyDescription = f"{description_tag} (Auto Updated)"
            new_policies.append(policy_new)

        req_add.SecurityGroupPolicySet.Ingress = new_policies
        client.CreateSecurityGroupPolicies(req_add)

        action_type = "更新" if ip_changed else "新增"
        msg = f"✅ {action_type}: [{device_id}] -> {current_ip} (TCP+UDP)"
        logger.info(msg)
        return True, msg

    except Exception as e:
        logger.error(f"TencentCloud API Error: {e}")
        return False, str(e)


@app.route('/open-door', methods=['GET'])
def open_door():
    # 1. 验证密码
    token = request.args.get('key')
    if token != ACCESS_TOKEN:
        return "Unauthorized", 403

    # 2. 获取设备ID
    device_id = request.args.get('device', 'Unknown-Device')

    # 3. 获取真实IP
    user_ip = request.headers.get('X-Real-IP') or request.remote_addr

    # 4. 执行业务
    success, msg = update_security_group(user_ip, device_id)

    if success:
        return msg, 200
    else:
        return f"❌ Error: {msg}", 500


if __name__ == '__main__':
    # 监听所有接口，等待 Docker 端口映射
    app.run(host='0.0.0.0', port=35555)