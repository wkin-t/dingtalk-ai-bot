# -*- coding: utf-8 -*-
"""钉钉引用图片与引用文档/群文件的严格只读内存提取与正文解析模块。

安全边界：
1. 在线文档（adoc）仅走 `dws doc info` + `dws doc read`（禁止 `-o/--output`）。
2. 群文件/云盘节点（docx/pdf/xlsx/txt/md 等）仅走 `dws drive download --url-only` 拿临时签名链接，
   经 `app.http_safe.fetch_public_bytes` 在内存中完成上限校验与流式读取，绝不落盘。
3. 引用图片优先走公网 OSS 签名 URL 内存读取；仅在内网表情包域名（`*.oss-internal.aliyun-inc.com`）
   或链接失效时，降级到 `tempfile.TemporaryDirectory(prefix="dws_ro_media_")` 读取字节后立即销毁目录。
"""
from __future__ import annotations

import io
import os
import re
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Sequence

from app import config as cfg
from app.bqb_image import detect_image_format, normalize_image_for_vision
from app.http_safe import fetch_public_bytes, validate_safe_public_url

_ALIDOCS_NODE_RE = re.compile(
    r"https?://alidocs\.dingtalk\.com/i/nodes/([A-Za-z0-9_-]{6,128})",
    re.IGNORECASE,
)
_FILE_ID_TEXT_RE = re.compile(r"\bfileId:\s*([A-Za-z0-9_-]{6,128})")

_MAX_ZIP_XML_UNCOMPRESSED_BYTES = 25 * 1024 * 1024
_MAX_QUOTE_DOCS = 2
_MAX_IMAGE_BYTES = 8 * 1024 * 1024

_TEXT_EXTENSIONS = frozenset(
    {
        "txt",
        "md",
        "markdown",
        "csv",
        "tsv",
        "json",
        "jsonl",
        "yaml",
        "yml",
        "toml",
        "ini",
        "cfg",
        "conf",
        "xml",
        "html",
        "htm",
        "log",
        "sql",
        "py",
        "js",
        "ts",
        "tsx",
        "jsx",
        "java",
        "go",
        "rs",
        "c",
        "cpp",
        "h",
        "cs",
        "sh",
        "ps1",
        "vue",
    }
)


@dataclass(frozen=True)
class QuotedDocExtract:
    """单篇被引用文档或群文件的只读解析结果。"""

    node_id: str
    name: str
    extension: str
    content: str = ""
    note: str = ""


@dataclass
class QuotedMediaAndDocsResult:
    """被引用消息关联的图片字节列表与文档解析列表。"""

    images: list[bytes] = field(default_factory=list)
    documents: list[QuotedDocExtract] = field(default_factory=list)


def extract_alidocs_node_ids(text: str) -> list[str]:
    """从消息文本或 URL 中提取钉钉文档/云盘 nodeId（保序去重）。"""
    s = str(text or "")
    if not s:
        return []
    found: list[str] = []
    seen: set[str] = set()
    for regex in (_ALIDOCS_NODE_RE, _FILE_ID_TEXT_RE):
        for match in regex.finditer(s):
            nid = match.group(1).strip()
            if nid and nid not in seen:
                seen.add(nid)
                found.append(nid)
    return found


def _local_tag(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _extract_word_paragraph_text(elem: ET.Element) -> str:
    parts: list[str] = []
    for node in elem.iter():
        lname = _local_tag(node.tag)
        if lname == "t" and node.text:
            parts.append(node.text)
        elif lname == "tab":
            parts.append("\t")
        elif lname in {"br", "cr"}:
            parts.append("\n")
    return "".join(parts)


def parse_docx_bytes(data: bytes) -> str:
    """在内存中解析 .docx 的段落与表格文本（零磁盘写入，带解压体积保护）。"""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        if "word/document.xml" not in zf.namelist():
            return ""
        info = zf.getinfo("word/document.xml")
        if info.file_size > _MAX_ZIP_XML_UNCOMPRESSED_BYTES:
            raise ValueError("docx XML 解压后体积超出安全限制")
        xml_bytes = zf.read("word/document.xml")

    root = ET.fromstring(xml_bytes)
    body = next((ch for ch in root if _local_tag(ch.tag) == "body"), root)
    blocks: list[str] = []

    for child in body:
        cname = _local_tag(child.tag)
        if cname == "p":
            txt = _extract_word_paragraph_text(child).strip()
            if txt:
                blocks.append(txt)
        elif cname == "tbl":
            for tr in child:
                if _local_tag(tr.tag) != "tr":
                    continue
                cells: list[str] = []
                for tc in tr:
                    if _local_tag(tc.tag) != "tc":
                        continue
                    cell_txt = re.sub(r"\s+", " ", _extract_word_paragraph_text(tc)).strip()
                    cells.append(cell_txt)
                if any(cells):
                    blocks.append("| " + " | ".join(cells) + " |")

    return "\n".join(blocks).strip()


def parse_xlsx_bytes(data: bytes) -> str:
    """在内存中解析 .xlsx 工作表文本（前 10 个工作表，每表前 200 行）。"""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = set(zf.namelist())
        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in names:
            ss_info = zf.getinfo("xl/sharedStrings.xml")
            if ss_info.file_size <= _MAX_ZIP_XML_UNCOMPRESSED_BYTES:
                ss_root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
                for si in ss_root:
                    if _local_tag(si.tag) == "si":
                        txt = "".join(
                            n.text for n in si.iter() if _local_tag(n.tag) == "t" and n.text
                        )
                        shared_strings.append(txt)

        sheet_files = sorted(
            n for n in names if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")
        )[:10]
        sections: list[str] = []
        for s_idx, s_path in enumerate(sheet_files, start=1):
            s_info = zf.getinfo(s_path)
            if s_info.file_size > _MAX_ZIP_XML_UNCOMPRESSED_BYTES:
                continue
            s_root = ET.fromstring(zf.read(s_path))
            rows_out: list[str] = []
            for row_el in s_root.iter():
                if _local_tag(row_el.tag) != "row":
                    continue
                row_vals: list[str] = []
                for cell in row_el:
                    if _local_tag(cell.tag) != "c":
                        continue
                    c_type = cell.attrib.get("t", "")
                    val_str = ""
                    if c_type == "inlineStr":
                        val_str = "".join(
                            n.text for n in cell.iter() if _local_tag(n.tag) == "t" and n.text
                        )
                    else:
                        v_node = next((n for n in cell if _local_tag(n.tag) == "v"), None)
                        raw_v = (v_node.text or "").strip() if v_node is not None else ""
                        if c_type == "s" and raw_v.isdigit():
                            idx = int(raw_v)
                            if 0 <= idx < len(shared_strings):
                                val_str = shared_strings[idx]
                        else:
                            val_str = raw_v
                    row_vals.append(val_str.replace("\n", " ").strip())
                if any(row_vals):
                    rows_out.append(" | ".join(row_vals))
                if len(rows_out) >= 200:
                    break
            if rows_out:
                sections.append(f"[Sheet {s_idx}]\n" + "\n".join(rows_out))

    return "\n\n".join(sections).strip()


def parse_pdf_bytes(data: bytes) -> str:
    """在内存中解析 .pdf 文本（最多前 60 页）。"""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    pages_out: list[str] = []
    for idx, page in enumerate(reader.pages[:60], start=1):
        txt = (page.extract_text() or "").strip()
        if txt:
            pages_out.append(f"[第 {idx} 页]\n{txt}")
    return "\n\n".join(pages_out).strip()


def parse_text_bytes(data: bytes) -> str:
    """在内存中解码纯文本或代码字节流，拒绝含 NUL 的未知二进制文件。"""
    if not data or b"\x00" in data[:4096]:
        return ""
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(enc).strip()
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace").strip()


def parse_document_bytes(data: bytes, *, filename: str = "", extension: str = "") -> tuple[str, str]:
    """按扩展名分派内存解析，返回 `(解析正文, 附加说明)`。"""
    ext = (extension or "").strip().lstrip(".").lower()
    if not ext and "." in filename:
        ext = filename.rsplit(".", 1)[-1].lower()

    try:
        if ext == "docx":
            text = parse_docx_bytes(data)
            return text, "" if text else f"[文件 {filename or '.docx'} 未提取到可识别文字]"
        if ext == "xlsx":
            text = parse_xlsx_bytes(data)
            return text, "" if text else f"[表格 {filename or '.xlsx'} 未提取到非空单元格]"
        if ext == "pdf":
            text = parse_pdf_bytes(data)
            return text, "" if text else f"[PDF {filename or '.pdf'} 未提取到文字层（可能是扫描件图片）]"
        if ext in _TEXT_EXTENSIONS or not ext:
            text = parse_text_bytes(data)
            if text:
                return text, ""
        return "", f"[暂不支持内存解析 .{ext or 'unknown'} 二进制文件格式]"
    except Exception as exc:
        return "", f"[解析文件 {filename or ext} 失败: {exc}]"


def _unwrap_dws_dict(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    if payload.get("ok") is False or payload.get("success") is False:
        return None
    if isinstance(payload.get("data"), dict):
        return payload["data"]
    if isinstance(payload.get("result"), dict):
        return payload["result"]
    return payload


def _extract_adoc_text(payload: Any) -> str:
    data = _unwrap_dws_dict(payload)
    if not data:
        return ""
    for key in ("markdown", "content", "text", "body"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    doc_obj = data.get("document")
    if isinstance(doc_obj, dict):
        for key in ("markdown", "content", "text", "body"):
            val = doc_obj.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return ""


def _truncate_doc_text(text: str, max_chars: int) -> str:
    clean = text.strip()
    if max_chars > 0 and len(clean) > max_chars:
        return f"{clean[:max_chars]}\n…[文档内容已截断，原文共 {len(clean)} 字]"
    return clean


async def _fetch_single_quoted_image(
    res: dict[str, Any],
    *,
    quote_conv_id: str,
    quote_msg_id: str,
    dws_ready: bool,
    run_dws_fn: Callable[[Sequence[str]], Awaitable[Any]],
) -> bytes | None:
    url = str(res.get("url") or "").strip()
    if url:
        try:
            validate_safe_public_url(url)
            raw_bytes = await fetch_public_bytes(
                url,
                max_bytes=_MAX_IMAGE_BYTES,
                timeout_seconds=8.0,
            )
            norm_bytes = normalize_image_for_vision(raw_bytes)
            if norm_bytes:
                return norm_bytes
        except Exception:
            # 内网 OSS 域名或签名过期：继续尝试只读临时目录媒体提取兜底
            pass

    if not dws_ready:
        return None

    dl_args: dict[str, Any] = {}
    refs = res.get("resourceRefs")
    if isinstance(refs, dict) and isinstance(refs.get("download"), dict):
        cand_args = refs["download"].get("arguments")
        if isinstance(cand_args, dict):
            dl_args = cand_args

    conv_id = str(dl_args.get("open-conversation-id") or quote_conv_id or "").strip()
    msg_id = str(dl_args.get("message-id") or quote_msg_id or "").strip()
    res_id = str(dl_args.get("resource-id") or res.get("resourceId") or "").strip()
    if not (conv_id and msg_id and res_id):
        return None

    with tempfile.TemporaryDirectory(prefix="dws_ro_media_") as tmp_dir:
        real_tmp = os.path.realpath(tmp_dir)
        dl_payload = await run_dws_fn(
            [
                "chat",
                "message",
                "download-media",
                "--conversation-id",
                conv_id,
                "--message-id",
                msg_id,
                "--resource-id",
                res_id,
                "--type",
                "mediaId",
                "--output",
                tmp_dir,
                "-f",
                "json",
            ]
        )
        candidate_files: list[str] = []
        dl_dict = _unwrap_dws_dict(dl_payload)
        if dl_dict:
            for k in ("path", "filePath", "outputPath", "file"):
                p = str(dl_dict.get(k) or "").strip()
                if p:
                    rp = os.path.realpath(p)
                    if (rp == real_tmp or rp.startswith(real_tmp + os.sep)) and os.path.isfile(rp):
                        candidate_files.append(rp)

        if not candidate_files:
            for root_dir, _dirs, files in os.walk(real_tmp):
                for fname in files:
                    fp = os.path.realpath(os.path.join(root_dir, fname))
                    if fp.startswith(real_tmp + os.sep) and os.path.isfile(fp):
                        candidate_files.append(fp)

        for fp in candidate_files:
            try:
                if os.path.getsize(fp) > _MAX_IMAGE_BYTES:
                    continue
                with open(fp, "rb") as f:
                    raw_bytes = f.read(_MAX_IMAGE_BYTES + 1)
                if len(raw_bytes) <= _MAX_IMAGE_BYTES:
                    norm_bytes = normalize_image_for_vision(raw_bytes)
                    if norm_bytes:
                        return norm_bytes
            except OSError:
                continue

    return None


async def fetch_quoted_media_and_docs(
    *,
    quotes: Sequence[Any],
    conversation_id: str,
    dws_ready: bool,
    run_dws_fn: Callable[[Sequence[str]], Awaitable[Any]],
) -> QuotedMediaAndDocsResult:
    """只读提取被引用消息中的图片字节与在线文档/群文件正文。"""
    result = QuotedMediaAndDocsResult()
    if not cfg.DWS_QUOTE_FETCH_MEDIA or not quotes:
        return result

    max_images = max(0, int(cfg.DWS_MAX_QUOTE_IMAGES))
    max_doc_bytes = max(65536, int(cfg.DWS_MAX_DOC_BYTES))
    max_doc_chars = max(500, int(cfg.DWS_MAX_DOC_CHARS))

    seen_img_keys: set[str] = set()
    candidate_nodes: list[str] = []
    seen_nodes: set[str] = set()

    for q in quotes:
        q_conv = str(getattr(q, "conversation_id", "") or conversation_id or "").strip()
        q_mid = str(getattr(q, "message_id", "") or "").strip()
        q_text = str(getattr(q, "text", "") or "")
        q_resources = getattr(q, "resources", None) or []

        for res in q_resources:
            if not isinstance(res, dict):
                continue
            r_type = str(res.get("resourceType") or "").strip().lower()
            r_id_type = str(res.get("resourceIdType") or "").strip()
            r_id = str(res.get("resourceId") or "").strip()
            r_url = str(res.get("url") or "").strip()

            if r_id_type == "fileId" or r_type == "file":
                if r_id and r_id not in seen_nodes:
                    seen_nodes.add(r_id)
                    candidate_nodes.append(r_id)
                for nid in extract_alidocs_node_ids(r_url):
                    if nid not in seen_nodes:
                        seen_nodes.add(nid)
                        candidate_nodes.append(nid)
                continue

            if len(result.images) < max_images and (
                r_type in {"image", "picture", "photo", "emotion"}
                or r_id_type == "mediaId"
            ):
                dedup_k = r_id or r_url
                if dedup_k and dedup_k in seen_img_keys:
                    continue
                if dedup_k:
                    seen_img_keys.add(dedup_k)
                img_bytes = await _fetch_single_quoted_image(
                    res,
                    quote_conv_id=q_conv,
                    quote_msg_id=q_mid,
                    dws_ready=dws_ready,
                    run_dws_fn=run_dws_fn,
                )
                if img_bytes:
                    result.images.append(img_bytes)

        for nid in extract_alidocs_node_ids(q_text):
            if nid not in seen_nodes:
                seen_nodes.add(nid)
                candidate_nodes.append(nid)

    if not dws_ready or not candidate_nodes:
        return result

    for node_id in candidate_nodes[:_MAX_QUOTE_DOCS]:
        info = _unwrap_dws_dict(await run_dws_fn(["doc", "info", "--node", node_id, "-f", "json"]))
        if not info:
            continue

        doc_name = str(info.get("name") or info.get("title") or node_id).strip()
        ext = str(info.get("extension") or "").strip().lstrip(".").lower()
        if not ext and "." in doc_name:
            ext = doc_name.rsplit(".", 1)[-1].lower()

        # 1. 钉钉在线文档（adoc）：直接走 dws doc read
        if ext == "adoc":
            read_raw = await run_dws_fn(["doc", "read", "--node", node_id, "-f", "json"])
            adoc_text = _extract_adoc_text(read_raw)
            if adoc_text:
                result.documents.append(
                    QuotedDocExtract(
                        node_id=node_id,
                        name=doc_name,
                        extension="adoc",
                        content=_truncate_doc_text(adoc_text, max_doc_chars),
                    )
                )
            else:
                result.documents.append(
                    QuotedDocExtract(
                        node_id=node_id,
                        name=doc_name,
                        extension="adoc",
                        note=f"[在线文档《{doc_name}》未返回可读内容或当前账号无阅读权限]",
                    )
                )
            continue

        # 2. 群上传文件或云盘文件：强制走 --url-only 获取临时链接并在内存中解析
        dl_info = _unwrap_dws_dict(
            await run_dws_fn(["drive", "download", "--node", node_id, "--url-only", "-f", "json"])
        )
        if not dl_info:
            result.documents.append(
                QuotedDocExtract(
                    node_id=node_id,
                    name=doc_name,
                    extension=ext or "file",
                    note=f"[无法获取文件《{doc_name}》只读下载链接]",
                )
            )
            continue

        file_name = str(dl_info.get("fileName") or doc_name).strip()
        try:
            file_size = int(dl_info.get("fileSize") or dl_info.get("size") or 0)
        except (TypeError, ValueError):
            file_size = 0

        if file_size > max_doc_bytes:
            result.documents.append(
                QuotedDocExtract(
                    node_id=node_id,
                    name=file_name,
                    extension=ext or "file",
                    note=(
                        f"[文件《{file_name}》大小 {file_size} bytes 超出只读内存解析上限 "
                        f"{max_doc_bytes} bytes，已跳过下载]"
                    ),
                )
            )
            continue

        dl_url = str(dl_info.get("downloadUrl") or dl_info.get("url") or "").strip()
        raw_headers = dl_info.get("headers")
        safe_headers = (
            {str(k): str(v) for k, v in raw_headers.items() if k and v is not None}
            if isinstance(raw_headers, dict)
            else None
        )
        if not dl_url:
            continue

        try:
            file_bytes = await fetch_public_bytes(
                dl_url,
                headers=safe_headers,
                max_bytes=max_doc_bytes,
                timeout_seconds=12.0,
            )
        except Exception as exc:
            result.documents.append(
                QuotedDocExtract(
                    node_id=node_id,
                    name=file_name,
                    extension=ext or "file",
                    note=f"[内存读取文件《{file_name}》失败: {exc}]",
                )
            )
            continue

        parsed_text, parse_note = parse_document_bytes(
            file_bytes,
            filename=file_name,
            extension=ext,
        )
        result.documents.append(
            QuotedDocExtract(
                node_id=node_id,
                name=file_name,
                extension=ext or "file",
                content=_truncate_doc_text(parsed_text, max_doc_chars) if parsed_text else "",
                note=parse_note,
            )
        )

    return result
