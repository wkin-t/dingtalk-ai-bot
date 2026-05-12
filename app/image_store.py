# -*- coding: utf-8 -*-
"""生图结果本地存储 + URL 生成 + 过期清理"""
import os
import uuid
import time
import threading

from app.config import GEN_IMAGES_DIR, IMAGE_TTL_HOURS, EXTERNAL_BASE_URL

# 延迟初始化目录
_dir_initialized = False


def _ensure_dir():
    global _dir_initialized
    if not _dir_initialized:
        os.makedirs(GEN_IMAGES_DIR, exist_ok=True)
        _dir_initialized = True


def save_image(image_bytes: bytes, ext: str = ".png") -> tuple[str, str]:
    """
    原子保存图片到本地，返回 (filename, url)
    先写 .tmp 再 rename，避免崩溃留下半文件。
    """
    _ensure_dir()
    filename = f"{uuid.uuid4().hex}{ext}"
    filepath = os.path.join(GEN_IMAGES_DIR, filename)
    tmp_path = filepath + ".tmp"
    try:
        with open(tmp_path, "wb") as f:
            f.write(image_bytes)
        os.replace(tmp_path, filepath)
    except OSError:
        # 清理临时文件
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise

    url = f"{EXTERNAL_BASE_URL}/gen-images/{filename}" if EXTERNAL_BASE_URL else ""
    return filename, url


def get_image_path(filename: str) -> str | None:
    """
    获取图片的绝对路径（供 Flask 路由使用）
    安全检查：防止路径遍历攻击；跳过非 .png 和零字节文件。
    """
    if not filename or os.path.sep in filename or "/" in filename or "\\" in filename:
        return None
    if not filename.endswith(".png"):
        return None
    filepath = os.path.join(GEN_IMAGES_DIR, filename)
    if not os.path.exists(filepath):
        return None
    if os.path.getsize(filepath) == 0:
        return None
    return filepath


def cleanup_expired() -> int:
    """
    清理过期的图片文件
    Returns:
        删除的文件数
    """
    _ensure_dir()
    cutoff = time.time() - IMAGE_TTL_HOURS * 3600
    removed = 0
    for name in os.listdir(GEN_IMAGES_DIR):
        if not name.endswith(".png"):
            continue
        filepath = os.path.join(GEN_IMAGES_DIR, name)
        if not os.path.isfile(filepath):
            continue
        try:
            if os.path.getmtime(filepath) < cutoff:
                os.remove(filepath)
                removed += 1
        except OSError:
            continue
    return removed


def start_cleanup_timer(interval_hours: float = 1.0):
    """
    启动定时清理线程（Gunicorn 多 worker 安全：用文件锁只让一个进程清理）
    """
    def _loop():
        while True:
            time.sleep(interval_hours * 3600)
            try:
                n = cleanup_expired()
                if n > 0:
                    print(f"[图片清理] 已删除 {n} 张过期图片")
            except Exception as e:
                print(f"[图片清理] 异常: {e}")

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    print(f"[图片清理] 定时器已启动 (每 {interval_hours}h 清理，TTL={IMAGE_TTL_HOURS}h)")
