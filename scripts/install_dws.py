# -*- coding: utf-8 -*-
"""在 Docker 构建阶段下载并校验官方 DingTalk Workspace CLI (dws) 二进制。

优先走官方 Gitee Release 镜像源保障国内云主机快速构建，失败时回退 GitHub Release，
无论哪个源都强制比对固定版本的官方 SHA256 摘要，杜绝损坏包或篡改产物。
"""
from __future__ import annotations

import hashlib
import io
import os
import platform
import subprocess
import sys
import tarfile
import urllib.request

DWS_VERSION = "v1.0.67"
EXPECTED_SHA256 = {
    "amd64": "25959351407e51d4aba7638790ff1cd04fdd7eaa56b313a26b689c03628e0ee2",
    "arm64": "1170453962f1bd049f27097385cab8b68dbf25cd69f45eb020e5d3bca0cad605",
}


def resolve_arch() -> str:
    machine = platform.machine().lower()
    if machine in {"x86_64", "amd64"}:
        return "amd64"
    if machine in {"aarch64", "arm64"}:
        return "arm64"
    raise RuntimeError(f"不支持的 CPU 架构: {machine}")


def download_verified_tarball(arch: str) -> bytes:
    expected = EXPECTED_SHA256[arch]
    filename = f"dws-linux-{arch}.tar.gz"
    urls = [
        f"https://gitee.com/DingTalk-Real-AI/dingtalk-workspace-cli/releases/download/{DWS_VERSION}/{filename}",
        f"https://github.com/DingTalk-Real-AI/dingtalk-workspace-cli/releases/download/{DWS_VERSION}/{filename}",
    ]
    errors: list[str] = []
    for url in urls:
        print(f"[install_dws] 正在尝试下载: {url}", flush=True)
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "dingtalk-ai-bot-docker-builder/1.0"},
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read()
            actual = hashlib.sha256(data).hexdigest()
            if actual != expected:
                errors.append(f"{url} SHA256 不匹配: expected={expected}, actual={actual}")
                continue
            print(f"[install_dws] 下载并校验 SHA256 成功 ({len(data)} bytes)", flush=True)
            return data
        except Exception as exc:
            errors.append(f"{url} 下载失败: {exc}")

    raise RuntimeError("无法获取通过校验的 dws 安装包:\n" + "\n".join(errors))


def main() -> int:
    arch = resolve_arch()
    tar_bytes = download_verified_tarball(arch)

    target_bin = "/usr/local/bin/dws"
    extracted = False
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tf:
        for member in tf.getmembers():
            if member.isfile() and os.path.basename(member.name) == "dws":
                src = tf.extractfile(member)
                if src is None:
                    continue
                with open(target_bin, "wb") as dst:
                    dst.write(src.read())
                os.chmod(target_bin, 0o755)
                extracted = True
                break

    if not extracted:
        raise RuntimeError("压缩包中未找到 dws 可执行文件")

    for subdir in ("/app/data-dws/config", "/app/data-dws/keychain"):
        os.makedirs(subdir, mode=0o700, exist_ok=True)

    res = subprocess.run([target_bin, "version"], check=True, capture_output=True, text=True)
    print(f"[install_dws] 就绪: {res.stdout.strip()}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
