"""图片本地存储模块测试"""
import os
import time
import tempfile
import shutil
import pytest


@pytest.fixture
def img_dir(tmp_path, monkeypatch):
    """创建临时图片目录并 monkeypatch 配置"""
    d = tmp_path / "images"
    d.mkdir()
    monkeypatch.setattr("app.image_store.GEN_IMAGES_DIR", str(d))
    monkeypatch.setattr("app.image_store.EXTERNAL_BASE_URL", "https://example.com")
    monkeypatch.setattr("app.image_store.IMAGE_TTL_HOURS", 1)
    return str(d)


def test_save_image_returns_filename_and_url(img_dir):
    from app.image_store import save_image
    data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100  # fake PNG
    filename, url = save_image(data)
    assert filename.endswith(".png")
    assert url == f"https://example.com/gen-images/{filename}"
    # 文件确实存在
    assert os.path.exists(os.path.join(img_dir, filename))
    # 文件内容正确
    with open(os.path.join(img_dir, filename), "rb") as f:
        assert f.read() == data


def test_save_image_uses_uuid_no_collision(img_dir):
    from app.image_store import save_image
    data = b"\x00" * 10
    f1, _ = save_image(data)
    f2, _ = save_image(data)
    assert f1 != f2


def test_cleanup_expired(img_dir):
    from app.image_store import cleanup_expired, save_image
    # 创建一个图片
    data = b"\x00" * 10
    filename, _ = save_image(data)
    filepath = os.path.join(img_dir, filename)
    assert os.path.exists(filepath)
    # 把修改时间改为 2 小时前（超过 TTL=1h）
    old_time = time.time() - 7200
    os.utime(filepath, (old_time, old_time))
    # 清理
    removed = cleanup_expired()
    assert removed == 1
    assert not os.path.exists(filepath)


def test_cleanup_skips_recent(img_dir):
    from app.image_store import cleanup_expired, save_image
    data = b"\x00" * 10
    filename, _ = save_image(data)
    removed = cleanup_expired()
    assert removed == 0
    assert os.path.exists(os.path.join(img_dir, filename))


def test_get_image_path_exists(img_dir):
    from app.image_store import save_image, get_image_path
    data = b"\x00" * 10
    filename, _ = save_image(data)
    path = get_image_path(filename)
    assert path is not None
    assert os.path.exists(path)


def test_get_image_path_not_found(img_dir):
    from app.image_store import get_image_path
    assert get_image_path("nonexistent.png") is None


def test_get_image_path_rejects_traversal(img_dir):
    from app.image_store import get_image_path
    assert get_image_path("../../etc/passwd") is None
    assert get_image_path("../../../etc/shadow") is None
    assert get_image_path("sub/../../etc/passwd") is None
