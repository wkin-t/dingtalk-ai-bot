# -*- coding: utf-8 -*-
"""表情包与引用图片魔数识别、等比自适应缩放及 GIF 均匀抽帧调色板量化模块。"""
from __future__ import annotations

import io
from typing import Any, Dict, List, Optional, Tuple


def detect_image_format(data: bytes) -> Optional[Tuple[str, str]]:
    """根据文件头魔数检测钉钉 /media/upload 支持的图片格式，拒绝伪装 HTML 或不支持的编码。"""
    if not data or len(data) < 12:
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png", "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg", "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif", "image/gif"
    if data[:2] == b"BM":
        return "bmp", "image/bmp"
    return None


def normalize_image_for_vision(data: bytes) -> Optional[bytes]:
    """校验引用图片魔数（含 WebP），并将部分多模态上游不兼容的 BMP 转为内存 PNG。"""
    if not data or len(data) < 12:
        return None
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return data
    fmt = detect_image_format(data)
    if fmt is None:
        return None
    if fmt[0] != "bmp":
        return data
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as im:
            out = io.BytesIO()
            im.convert("RGBA").save(out, format="PNG", optimize=True)
            return out.getvalue()
    except Exception:
        return None


def _compute_display_geometry(
    w: int,
    h: int,
    max_px: int,
    canvas_width_px: int,
) -> Tuple[int, int, int]:
    """计算前景宽高与透明基准画布宽度，兼顾 PC 防 100% 撑大与移动端竖长梗图可读性。"""
    if max(w, h) > max_px:
        scale = max_px / float(max(w, h))
        if h > w * 1.35:
            # 竖长条漫/台词梗图：允许高度适度放宽至 2.4x max_px，保住至少约 65% max_px 的可读文字宽度
            min_readable_w = min(w, round(max_px * 0.65))
            max_tall_h = round(max_px * 2.4)
            scale = min(
                1.0,
                max_px / float(w),
                max_tall_h / float(h),
                max(scale, min_readable_w / float(w)),
            )
        new_w = max(1, round(w * scale))
        new_h = max(1, round(h * scale))
    else:
        new_w, new_h = w, h

    if canvas_width_px > 0:
        # 限制画布宽度不超过前景宽度的 3.5 倍，防止窄图在手机端被挤成看不清的细条
        proportional_cap = max(new_w, round(new_w * 3.5))
        target_canvas_w = max(new_w, min(canvas_width_px, proportional_cap))
    else:
        target_canvas_w = new_w

    return new_w, new_h, target_canvas_w


def resize_bqb_for_display(
    data: bytes,
    ext: str,
    mime_type: str,
    max_px: int,
    canvas_width_px: int = 0,
) -> Tuple[bytes, str, str]:
    """将表情包主体限制在合理尺寸内，并按比例左对齐贴入透明基准画布以阻止钉钉卡片 100% 拉伸放大。"""
    if max_px <= 0 or not data:
        return data, ext, mime_type

    try:
        from PIL import Image, ImageOps, ImageSequence
    except ImportError:
        return data, ext, mime_type

    try:
        with Image.open(io.BytesIO(data)) as im:
            w, h = im.size
            if w <= 0 or h <= 0:
                return data, ext, mime_type

            new_w, new_h, target_canvas_w = _compute_display_geometry(
                w, h, max_px, canvas_width_px
            )
            need_pad = target_canvas_w > new_w
            if new_w == w and new_h == h and not need_pad:
                return data, ext, mime_type

            resample = Image.Resampling.LANCZOS
            n_total = int(getattr(im, "n_frames", 1))
            is_anim = bool(getattr(im, "is_animated", False)) and n_total > 1

            if is_anim:
                max_gif_frames = 120
                if n_total <= max_gif_frames:
                    keep_indices = set(range(n_total))
                else:
                    keep_indices = {
                        round(k * (n_total - 1) / float(max_gif_frames - 1))
                        for k in range(max_gif_frames)
                    }

                default_dur = int(im.info.get("duration") or 80)
                loop_val = int(im.info.get("loop", 0))

                # 第一遍：均匀抽帧并收集 RGBA 画布与精确累计时长，先确定全局是否存在透明帧
                rgba_frames: List[Tuple[Any, bool]] = []
                durations: List[int] = []
                pending_dur = 0

                for idx, frame in enumerate(ImageSequence.Iterator(im)):
                    dur = max(20, int(frame.info.get("duration") or default_dur or 80))
                    if idx not in keep_indices:
                        if durations:
                            durations[-1] += dur
                        else:
                            pending_dur += dur
                        continue

                    fg = frame.convert("RGBA")
                    if (new_w, new_h) != fg.size:
                        fg = fg.resize((new_w, new_h), resample)

                    if need_pad:
                        # 不传 mask 直接原样拷贝 RGBA 四通道，避免 Pillow paste 按非预乘通道混合把半透明边缘染黑
                        canvas = Image.new("RGBA", (target_canvas_w, new_h), (255, 255, 255, 0))
                        canvas.paste(fg, (0, 0))
                    else:
                        canvas = fg

                    min_alpha, _ = canvas.getchannel("A").getextrema()
                    rgba_frames.append((canvas, min_alpha < 255))
                    durations.append(dur + pending_dur)
                    pending_dur = 0

                if pending_dur and durations:
                    durations[-1] += pending_dur

                any_transparent = any(has_a for _, has_a in rgba_frames)

                # 第二遍：调色板量化；只要任一帧含透明像素，不透明帧也统一用 255 色避开索引 255 透明色槽位
                frames_out: List[Any] = []
                for canvas, frame_has_alpha in rgba_frames:
                    if frame_has_alpha:
                        alpha = canvas.getchannel("A")
                        white_bg = Image.new("RGBA", canvas.size, (255, 255, 255, 255))
                        composited_rgb = Image.alpha_composite(white_bg, canvas).convert("RGB")
                        rgb_q = composited_rgb.quantize(
                            colors=255, method=Image.Quantize.FASTOCTREE
                        )
                        pal = rgb_q.getpalette() or []
                        if len(pal) < 768:
                            pal = pal + [255] * (768 - len(pal))
                        pal[255 * 3 : 255 * 3 + 3] = [255, 255, 255]
                        rgb_q.putpalette(pal[:768])
                        mask = Image.eval(alpha, lambda a: 255 if a < 128 else 0)
                        rgb_q.paste(255, mask)
                        rgb_q.info["transparency"] = 255
                        frames_out.append(rgb_q)
                    elif any_transparent:
                        rgb_q = canvas.convert("RGB").quantize(
                            colors=255, method=Image.Quantize.MEDIANCUT
                        )
                        pal = rgb_q.getpalette() or []
                        if len(pal) < 768:
                            pal = pal + [255] * (768 - len(pal))
                        pal[255 * 3 : 255 * 3 + 3] = [255, 255, 255]
                        rgb_q.putpalette(pal[:768])
                        frames_out.append(rgb_q)
                    else:
                        rgb_q = canvas.convert("RGB").quantize(
                            colors=256, method=Image.Quantize.MEDIANCUT
                        )
                        frames_out.append(rgb_q)

                if len(frames_out) > 1:
                    out_buf = io.BytesIO()
                    save_kwargs: Dict[str, Any] = {
                        "format": "GIF",
                        "save_all": True,
                        "append_images": frames_out[1:],
                        "duration": durations,
                        "loop": loop_val,
                        "disposal": 2,
                    }
                    if any_transparent:
                        save_kwargs["transparency"] = 255
                    frames_out[0].save(out_buf, **save_kwargs)
                    return out_buf.getvalue(), "gif", "image/gif"

            transposed = ImageOps.exif_transpose(im)
            has_alpha = need_pad or transposed.mode in ("RGBA", "LA") or (
                transposed.mode == "P" and "transparency" in transposed.info
            )
            out_buf = io.BytesIO()
            if has_alpha:
                fg = transposed.convert("RGBA")
                if (new_w, new_h) != fg.size:
                    fg = fg.resize((new_w, new_h), resample)
                if need_pad:
                    canvas = Image.new("RGBA", (target_canvas_w, new_h), (255, 255, 255, 0))
                    canvas.paste(fg, (0, 0))
                else:
                    canvas = fg
                canvas.save(out_buf, format="PNG", optimize=True)
                return out_buf.getvalue(), "png", "image/png"

            resized = transposed.convert("RGB").resize((new_w, new_h), resample)
            resized.save(out_buf, format="JPEG", quality=88, optimize=True)
            return out_buf.getvalue(), "jpg", "image/jpeg"
    except Exception as exc:
        print(f"⚠️ [BQB] 缩放表情包失败，回退原图上传: {exc}")
        return data, ext, mime_type
