"""COS 图片存储模块测试"""
import pytest
from unittest.mock import patch, MagicMock


@pytest.fixture
def cos_env(monkeypatch):
    """设置 COS 配置（直接 patch image_store 模块的导入值）"""
    monkeypatch.setattr("app.image_store.COS_SECRET_ID", "test-id")
    monkeypatch.setattr("app.image_store.COS_SECRET_KEY", "test-key")
    monkeypatch.setattr("app.image_store.COS_BUCKET", "test-bucket-1234567890")
    monkeypatch.setattr("app.image_store.COS_REGION", "ap-guangzhou")
    # 重置模块级 _client 缓存
    import app.image_store
    app.image_store._client = None


def test_save_image_returns_key_and_presigned_url(cos_env):
    with patch("app.image_store.CosS3Client") as MockClient:
        mock_instance = MagicMock()
        mock_instance.get_presigned_url.return_value = "https://cos.example.com/signed?token=abc"
        MockClient.return_value = mock_instance

        from app.image_store import save_image
        key, url = save_image(b"\x89PNG" + b"\x00" * 100)

        assert key.startswith("images/")
        assert key.endswith(".png")
        assert url == "https://cos.example.com/signed?token=abc"
        mock_instance.put_object.assert_called_once()
        mock_instance.get_presigned_url.assert_called_once_with(
            Method="GET", Bucket="test-bucket-1234567890", Key=key, Expires=600
        )


def test_save_image_uses_uuid_no_collision(cos_env):
    with patch("app.image_store.CosS3Client") as MockClient:
        MockClient.return_value = MagicMock()

        from app.image_store import save_image
        key1, _ = save_image(b"\x00" * 10)
        key2, _ = save_image(b"\x00" * 10)
        assert key1 != key2


def test_save_image_raises_on_missing_config(monkeypatch):
    """COS 配置缺失时应 raise"""
    monkeypatch.setattr("app.image_store.COS_SECRET_ID", "")
    monkeypatch.setattr("app.image_store.COS_SECRET_KEY", "")
    monkeypatch.setattr("app.image_store.COS_BUCKET", "")
    import app.image_store
    app.image_store._client = None

    from app.image_store import save_image
    with pytest.raises(RuntimeError, match="COS 配置不完整"):
        save_image(b"\x00" * 10)
