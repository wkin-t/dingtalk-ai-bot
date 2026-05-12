# 双后端生图功能设计

**日期**: 2026-05-12
**状态**: 设计中

## Context

钉钉 AI 机器人当前支持文本对话和图片识别，但不支持图片生成。用户希望 Gemini 和 GPT 两个后端都能根据用户描述生成图片并发送到钉钉群聊/单聊。

## 设计决策

| 决策项 | 选择 |
|--------|------|
| 触发方式 | 纯路由模型判断（预分析 prompt 增加 `need_image_gen` 字段） |
| Gemini 模型 | `imagen-4.0-generate-001`（`client.models.generate_images()`） |
| GPT 模型 | `gpt-image-2`（OpenAI SDK `client.images.generate()`） |
| 文字回复 | 生图 API + 模板文字（跳过正常 AI 流） |
| 参数控制 | 从用户消息中解析（比例、数量） |
| 错误处理 | 在卡片中发送错误提示文本 |
| 图片展示 | 独立图片消息（`sampleImageMsg` + `upload_media`） |
| 架构方案 | 方案 A：预分析扩展 + 独立生图模块 |

## 架构

```
用户消息
   │
   ▼
预分析模型 (gemini-3.1-flash-lite / gpt-5.4-mini)
   │
   ├─ need_image_gen: false → 正常 AI 流式对话（不变）
   │
   └─ need_image_gen: true + image_gen_params
         │
         ▼
      image_gen.py: generate_image()
         ┌──────────┴──────────┐
    Gemini 后端            OpenAI 后端
    imagen-4.0-generate    gpt-image-2
         └──────────┬──────────┘
                    │
              ┌─────┴─────┐
              │ 成功      │ 失败
              ▼           ▼
      upload_media()   卡片显示错误文本
      send_image_msg
      卡片显示模板文字
```

## 文件变更清单

### 新增文件

**`app/image_gen.py`** — 统一生图模块

```python
# 核心接口
async def generate_image(
    prompt: str,
    backend: str = "gemini",  # 或 "openai"
    aspect_ratio: str = "1:1",
    number_of_images: int = 1,
) -> List[bytes]:
    """调用生图 API，返回图片 bytes 列表"""
```

内部实现：
- `_generate_with_gemini(prompt, aspect_ratio, number_of_images)` → 用 `google-genai` SDK 的 `client.models.generate_images()`
- `_generate_with_openai(prompt, aspect_ratio, number_of_images)` → 用 `openai` SDK 的 `client.images.generate()`
- 代理配置复用 `config.py` 中的 `SOCKS_PROXY` / `HTTPX_PROXY`

### 修改文件

**`app/gemini_client.py`** — `analyze_complexity_with_model()` 函数

预分析 prompt 增加第 5 个字段：

```
5. need_image_gen:
   - true: 用户明确要求生成图片、画画、插图、绘制
   - false: 不需要生图（默认）

6. image_gen_params (仅当 need_image_gen=true 时):
   - prompt: 提取用户描述的图片内容，转为英文
   - aspect_ratio: 解析比例 → "1:1" | "3:4" | "4:3" | "9:16" | "16:9"
   - number_of_images: 解析数量 → 1-4
```

返回 JSON 扩展：
```json
{
  "model": "gemini-3-flash-preview",
  "thinking_level": "low",
  "need_search": false,
  "need_image_gen": true,
  "image_gen_params": {
    "prompt": "A cat running under moonlight",
    "aspect_ratio": "1:1",
    "number_of_images": 1
  },
  "reason": "用户要求生成图片",
  "thinking_text": "正在画画 🎨"
}
```

**`app/dingtalk_bot.py`** — `_analyze_with_litellm()` 函数

同步扩展预分析 prompt，增加 `need_image_gen` 和 `image_gen_params` 字段，格式与 Gemini 版一致。

**`app/dingtalk_bot.py`** — `handle_gemini_stream()` 函数

在路由分析结果返回后（约 line 1041 之后），增加生图分支判断：

```python
# 路由分析结果
route_result = ...  # 已有代码

if route_result.get("need_image_gen"):
    # 生图分支
    params = route_result.get("image_gen_params", {})
    image_prompt = params.get("prompt", content)
    aspect_ratio = params.get("aspect_ratio", "1:1")
    num_images = params.get("number_of_images", 1)

    # 更新卡片状态
    await stream_update(out_track_id, "正在生成图片...", is_finalize=False)

    # 调用生图
    try:
        images = await generate_image(image_prompt, AI_BACKEND, aspect_ratio, num_images)
        for img_bytes in images:
            media_id = await upload_media(img_bytes)
            await send_image_message(conversation_id, media_id)

        # 更新卡片文字
        template = f"已为你生成 {len(images)} 张图片：{content}"
        await stream_update(out_track_id, template, is_finalize=True)
    except Exception as e:
        error_msg = f"图片生成失败：{str(e)}"
        await stream_update(out_track_id, error_msg, is_finalize=True)

    return  # 跳过正常 AI 流
```

**`app/config.py`** — 增加生图相关配置

```python
# 生图配置
GEMINI_IMAGE_MODEL = os.environ.get("GEMINI_IMAGE_MODEL", "imagen-4.0-generate-001")
OPENAI_IMAGE_MODEL = os.environ.get("OPENAI_IMAGE_MODEL", "gpt-image-2")
DEFAULT_IMAGE_ASPECT_RATIO = os.environ.get("DEFAULT_IMAGE_ASPECT_RATIO", "1:1")
DEFAULT_IMAGE_COUNT = _get_int("DEFAULT_IMAGE_COUNT", 1)
```

**`requirements.txt`** — 确认依赖

`openai` 包已通过 `litellm` 间接安装，无需新增。但建议显式添加：
```
openai>=1.0.0
```

### 不变的文件

- `app/ai/handler.py` — 不修改，`process_message` 不参与生图流程
- `app/ai/backend.py` — 不修改，生图不走 `create_backend_stream`
- `app/ai/router.py` — 不修改，关键词路由不涉及生图判断
- `app/dingtalk_card.py` — 不修改，复用已有的 `upload_media` 和 `send_group_message`

## Gemini Imagen 4 调用细节

```python
from google import genai
from google.genai import types

# 复用已有的 genai.Client 实例（已配置代理）
from app.gemini_client import client

response = client.models.generate_images(
    model='imagen-4.0-generate-001',
    prompt=prompt,  # 英文描述
    config=types.GenerateImagesConfig(
        number_of_images=number_of_images,  # 1-4
        aspect_ratio=aspect_ratio,          # "1:1" | "3:4" | "4:3" | "9:16" | "16:9"
    )
)

# 提取图片 bytes
images = []
for generated_image in response.generated_images:
    # generated_image.image.image_bytes → PNG bytes
    images.append(generated_image.image.image_bytes)
```

注意事项：
- Imagen 4 只支持英文 prompt，预分析模型需将中文描述翻译为英文
- 代理配置复用 `gemini_client.py` 中已有的 `genai.Client` 实例
- `response.generated_images` 可能为空（安全过滤器拒绝），需处理

## OpenAI GPT-image-2 调用细节

```python
from openai import OpenAI

client = OpenAI(
    api_key=OPENAI_API_KEY,
    base_url=OPENAI_API_BASE,  # 可选
    http_client=httpx.Client(proxy=proxy_url),  # 代理
)

response = client.images.generate(
    model="gpt-image-2",
    prompt=prompt,
    n=number_of_images,
    size=_map_size(aspect_ratio),  # "1024x1024" | "1024x1536" | "1536x1024" 等
)

images = []
for img in response.data:
    images.append(base64.b64decode(img.b64_json))
```

尺寸映射：
| aspect_ratio | GPT-image-2 size |
|-------------|-----------------|
| 1:1 | 1024x1024 |
| 3:4 | 1024x1536 |
| 4:3 | 1536x1024 |
| 9:16 | 1024x1792 |
| 16:9 | 1792x1024 |

## 错误处理策略

| 错误场景 | 处理方式 |
|---------|---------|
| 安全过滤器拒绝 | 卡片显示 "图片生成被安全过滤器拒绝，请调整描述" |
| API 超时 | 卡片显示 "图片生成超时，请稍后重试" |
| 限额用完 | 卡片显示 "图片生成服务暂时不可用" |
| 网络错误 | 卡片显示 "网络错误，请稍后重试" |
| 代理配置错误 | 卡片显示 "服务配置错误" |

## 降级策略

- 预分析失败（模型不可用）→ `need_image_gen=false`，走正常对话流
- 预分析返回 `need_image_gen=true` 但 `image_gen_params` 缺失 → 用原始消息作为 prompt，默认参数
- 生图 API 失败 → 卡片显示错误信息，不重试

## 测试计划

1. **单元测试** (`tests/test_image_gen.py`)
   - 参数解析测试（消息中提取 prompt、比例、数量）
   - Gemini Imagen 4 mock 调用
   - OpenAI gpt-image-2 mock 调用
   - 错误处理测试

2. **集成测试**
   - 发送"画一只猫" → 预分析返回 `need_image_gen=true`
   - 发送"画3只狗 16:9" → 正确解析参数
   - 发送"你好" → `need_image_gen=false`
   - 安全过滤器触发 → 正确显示错误

3. **编译检查**
   - `python -m compileall -q app main.py` 通过
