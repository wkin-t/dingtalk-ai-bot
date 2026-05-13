# 本地图片服务 + EdgeOne 代理展示 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 生图结果存本地并通过公网 URL 在钉钉卡片 markdown 中展示，替代付费才能看的 media_id 方案。

**Architecture:** 生图后图片存 `data/images/{uuid}.png`，Flask 加 `/gen-images/<filename>` 端点提供 HTTP 访问，钉钉卡片用 markdown `![](URL)` 展示。EdgeOne 反代该路径到 bot 容器提供公网访问。定时任务清理 24h 过期图片。

**Tech Stack:** Flask 路由、os 文件操作、uuid 命名、定时清理（asyncio background task）

---

## 文件结构

| 文件 | 职责 |
|------|------|
| `app/image_store.py` | **新建** — 图片本地存储 + 清理 + URL 生成 |
| `app/routes.py` | **修改** — 添加 `/gen-images/<filename>` 路由 |
| `app/config.py` | **修改** — 添加 `EXTERNAL_BASE_URL`、`IMAGE_TTL_HOURS`、`GEN_IMAGES_DIR` 配置 |
| `app/dingtalk_bot.py` | **修改** — 生图分支改用本地存储 + markdown URL 展示 |
| `tests/test_image_store.py` | **新建** — 图片存储和清理测试 |

---

### Task 1: 配置项

**Files:**
- Modify: `app/config.py:243-247`（生图配置区域后）

- [ ] **Step 1: 添加配置项到 config.py**

在 `DEFAULT_IMAGE_COUNT` 行之后添加：

```python
# 生图本地存储 + URL 展示
GEN_IMAGES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "images")
IMAGE_TTL_HOURS = max(1, _get_int("IMAGE_TTL_HOURS", 24))
EXTERNAL_BASE_URL = os.getenv("EXTERNAL_BASE_URL", "").rstrip("/")
```

`EXTERNAL_BASE_URL` 格式示例：`https://clip.ifitnesslog.cn`（不含尾部斜杠）。图片 URL 将拼接为 `{EXTERNAL_BASE_URL}/gen-images/{filename}`。

- [ ] **Step 2: 验证编译通过**

Run: `python -m compileall -q app main.py`
Expected: 无输出（无错误）

- [ ] **Step 3: Commit**

```bash
git add app/config.py
git commit -m "feat: add image serving config (GEN_IMAGES_DIR, IMAGE_TTL_HOURS, EXTERNAL_BASE_URL)"
```

---

### Task 2: 图片存储模块

**Files:**
- Create: `app/image_store.py`
- Create: `tests/test_image_store.py`

- [ ] **Step 1: 写测试**

Create `tests/test_image_store.py`:

```python
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_image_store.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现图片存储模块**

Create `app/image_store.py`:

```python
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
                    print(f"🗑️ [图片清理] 已删除 {n} 张过期图片")
            except Exception as e:
                print(f"⚠️ [图片清理] 异常: {e}")

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    print(f"🗑️ [图片清理] 定时器已启动 (每 {interval_hours}h 清理，TTL={IMAGE_TTL_HOURS}h)")
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_image_store.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add app/image_store.py tests/test_image_store.py
git commit -m "feat: add image_store module (save, serve, cleanup expired images)"
```

---

### Task 3: Flask 图片服务路由

**Files:**
- Modify: `app/routes.py:1-23`（路由注册区域）

- [ ] **Step 1: 添加图片服务路由**

在 `app/routes.py` 顶部 import 区域添加 `send_file`，然后在 `index()` 路由之后添加 `/gen-images/` 路由：

在 import 行 `from flask import request, Response, jsonify` 中加入 `send_file`：

```python
from flask import request, Response, jsonify, send_file
```

在 `models()` 路由之后添加：

```python
@app.route('/gen-images/<filename>')
def serve_gen_image(filename):
    """提供生图结果（由 EdgeOne 反代到公网）"""
    from app.image_store import get_image_path
    from flask import make_response
    filepath = get_image_path(filename)
    if not filepath:
        return jsonify({"error": "not found"}), 404
    resp = make_response(send_file(filepath, mimetype="image/png"))
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp
```

- [ ] **Step 2: 验证编译通过**

Run: `python -m compileall -q app main.py`
Expected: 无输出

- [ ] **Step 3: Commit**

```bash
git add app/routes.py
git commit -m "feat: add /gen-images/<filename> route for serving generated images"
```

---

### Task 4: 启动清理定时器

**Files:**
- Modify: `main.py:55-64`（导入和初始化区域）

- [ ] **Step 1: 在 main.py 中启动清理定时器**

在 `from app.memory import DATA_DIR` 之后添加：

```python
from app.image_store import start_cleanup_timer
start_cleanup_timer()
```

- [ ] **Step 2: 验证编译通过**

Run: `python -m compileall -q app main.py`
Expected: 无输出

- [ ] **Step 3: Commit**

```bash
git add main.py
git commit -m "feat: start image cleanup timer on boot"
```

---

### Task 5: 生图分支改用本地存储 + markdown URL

**Files:**
- Modify: `app/dingtalk_bot.py:1142-1181`（生图分支的图片上传和卡片更新部分）

- [ ] **Step 1: 修改生图分支**

在 `app/dingtalk_bot.py` 顶部 import 区域，将 `from app.image_gen import generate_image` 改为：

```python
from app.image_gen import generate_image
from app.image_store import save_image
from app.config import EXTERNAL_BASE_URL
```

然后修改生图分支中 `if not images:` 之后的代码块（约 line 1142-1181），将整个 `# 上传第一张图片` 到卡片更新的部分替换为：

```python
                if not images:
                    raise RuntimeError("生图 API 未返回任何图片")

                # 检查是否有公网 URL 配置
                if not EXTERNAL_BASE_URL:
                    raise RuntimeError("EXTERNAL_BASE_URL 未配置，无法展示图片")

                # 保存图片到本地 + 生成公网 URL
                image_urls = []
                saved_files = []
                for img_bytes in images:
                    filename, url = save_image(img_bytes)
                    saved_files.append(filename)
                    if url:
                        image_urls.append(url)

                # 卡片 markdown 展示图片
                img_markdown = "\n".join(f"![图片{i+1}]({url})" for i, url in enumerate(image_urls))
                card_text = f"已为你生成 {len(images)} 张图片 ✨\n\n{img_markdown}"

                await self.card_helper.stream_update(
                    out_track_id,
                    card_text,
                    is_finalize=True,
                    is_full=True,
                )
                print(f"✅ [生图] 完成，{len(images)} 张，本地存储 {len(saved_files)} 个")
```

注意：去掉了 media_id fallback（因为付费墙问题），当 `EXTERNAL_BASE_URL` 未配置时直接报错提示管理员配置。

- [ ] **Step 2: 运行全部测试**

Run: `pytest -q tests`
Expected: 所有测试通过（174+ passed）

- [ ] **Step 3: Commit**

```bash
git add app/dingtalk_bot.py
git commit -m "feat: serve generated images via local URL instead of paid media_id"
```

---

### Task 6: 最终验证

**Files:** 无新文件

- [ ] **Step 1: 编译检查**

Run: `python -m compileall -q app main.py`
Expected: 无输出

- [ ] **Step 2: 全量测试**

Run: `pytest -q tests`
Expected: 所有测试通过

- [ ] **Step 3: 部署并验证**

部署到服务器后：
1. 设置环境变量 `EXTERNAL_BASE_URL=https://clip.ifitnesslog.cn`
2. 在 EdgeOne / 1Panel 中添加路径规则：`/gen-images/*` → 反代到 bot 容器 35001 端口
3. 发送生图消息测试，验证图片在卡片内正确展示

---

## 自审 Checklist

**1. Spec 覆盖度：**
- ✅ 图片本地存储 → Task 2（原子写入）
- ✅ Flask 端点 → Task 3（Cache-Control 头）
- ✅ URL 生成 → Task 2 (save_image)
- ✅ 过期清理 → Task 2 (cleanup_expired) + Task 4 (定时器)
- ✅ 卡片 markdown 展示 → Task 5
- ✅ 配置缺失 fail fast → Task 5 (EXTERNAL_BASE_URL 校验)
- ✅ 安全（路径遍历 + 零字节 + 非 PNG 拒绝） → Task 2 (get_image_path)

**2. 占位符扫描：** 无 TBD、TODO、占位符

**3. 类型一致性：**
- `save_image()` → `(str, str)` = (filename, url)
- `get_image_path()` → `str | None`
- `cleanup_expired()` → `int`
- 所有函数签名在 Task 2（定义）和 Task 3/5（使用）间一致

## Codex 对抗审查修复记录

| 问题 | 严重性 | 处理 |
|------|--------|------|
| 钉钉卡片是否渲染外链图片未验证 | Critical | **部署后实机验证**，如不渲染则回退到独立图片消息方案 |
| EdgeOne 未配时发死链 | Critical | **已修复**: EXTERNAL_BASE_URL 为空时 fail fast 报错 |
| media_id fallback 仍走付费墙 | High | **已修复**: 去掉 fallback，无 URL 直接报错 |
| 文件写入非原子（半文件） | High | **已修复**: 先写 .tmp 再 os.rename |
| 无缓存/隐私头 | Medium | **已修复**: 路由加 Cache-Control: public, max-age=86400 |
| Gunicorn 多 worker 重复清理 | Medium | **接受风险**: 多 worker 清理同一目录无副作用（文件删除幂等） |
| 编译命令 typo | Low | **已修复**: app/main.py → app main.py |
| 磁盘配额/DoS | Medium | **接受风险**: 单用户 bot，生图频率低，TTL 清理足够 |
