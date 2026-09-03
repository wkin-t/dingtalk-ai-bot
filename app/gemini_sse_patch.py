# -*- coding: utf-8 -*-
"""修补 google-genai SDK 的 SSE 流解析器，跳过 SSE 规范允许的注释行。

根因（2026-08-25 定位）：`google.genai._api_client.HttpResponse` 的
`_iter_response_stream` / `_aiter_response_stream` 只认识空行和 "data: " 前缀行，
其余任何非空行（包括 SSE 规范允许的 `:` 开头注释行，见
https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events#event_stream_format）
都会被当成 JSON 片段塞进 balance-counting 兜底逻辑，最终 json.loads 失败抛出
`google.genai.errors.UnknownApiResponseError`（消息形如
"Failed to parse response as JSON. Raw response: : keep-alive"）。

cli-proxy-api 的 `streaming.keepalive-seconds` 配置会在流式响应静默期（典型触发
场景：Gemini 3.8 thinking_level=medium/high 的思考停顿）插入 `: keep-alive`
维持连接，必然命中这个 bug，表现为"流式响应中断"——已用生产日志 + 容器内复现
的真实 traceback 确认，antigravity 和 vertex 两个上游都会中招（bug 在客户端
解析层，与上游账号池无关）。

**已确认现状（2026-08-25，google-genai==2.19.0）**：
- 本仓库实际安装版本可稳定复现
- 对照 GitHub googleapis/python-genai **main 分支**源码逐行比对，两个函数依然
  是同样的解析逻辑，未被修复
- 社区曾提交修复 PR #487（googleapis/python-genai#487），维护者以"main 已包含
  该修改"为由关闭，但该说法与当前公开仓库源码不符（可能是维护者混淆，或内部
  monorepo 有未同步到开源镜像的修复）
- CHANGELOG.md 历史中两条看似相关的修复（"Streaming method doesn't handle
  multi-line SSE"、"Handle SSE error message types properly in streaming"）
  均未涉及注释行跳过

**升级 google-genai 后必须做的事**：本文件的补丁是对 `_aiter_response_stream`/
`_iter_response_stream` 方法体的完整替换，不是简单包装——如果官方在某个新版本
里改了这两个方法的内部结构（哪怕只是改了 SSE 注释行处理之外的逻辑），本补丁会
静默生效但可能覆盖掉官方的新实现，或者因为引用的私有符号（`_HTTPX_RESPONSE_TYPES`
/ `has_aiohttp` / `READ_BUFFER_SIZE` / `_common.loaded_requests`）改名而在
`apply()` 内直接报错。`apply()` 会在版本不匹配时打印警告但仍会尝试应用，出现
警告后应该人工用 `inspect.getsource()` 重新核对方法体是否与本文件假设的实现
一致，确认后更新 `EXPECTED_VERSION`；如果官方已经原生修复了这个 bug，应该整个
删除本文件和 gemini_client.py 里的调用。
"""
import logging
from typing import Any, AsyncIterator, Iterator

logger = logging.getLogger(__name__)

EXPECTED_VERSION = "2.19.0"
_PATCHED = False


def apply() -> bool:
    """打上补丁；返回是否成功应用。重复调用是幂等的（不会重复替换）。"""
    global _PATCHED
    if _PATCHED:
        return True

    try:
        import google.genai as genai_pkg
        from google.genai import _api_client
    except ImportError:
        logger.warning("⚠️ [Gemini SSE Patch] google-genai 未安装，跳过补丁")
        return False

    installed_version = getattr(genai_pkg, "__version__", "unknown")
    if installed_version != EXPECTED_VERSION:
        logger.warning(
            "⚠️ [Gemini SSE Patch] google-genai 版本是 %s，补丁验证过的版本是 %s，"
            "请人工核对 _api_client.HttpResponse._aiter_response_stream 源码是否"
            "仍与本文件假设的实现一致后再信任此补丁",
            installed_version, EXPECTED_VERSION,
        )

    required = ("_HTTPX_RESPONSE_TYPES", "has_aiohttp", "aiohttp", "READ_BUFFER_SIZE", "_common")
    missing = [name for name in required if not hasattr(_api_client, name)]
    if missing or not hasattr(_api_client._common, "loaded_requests"):
        logger.warning(
            "⚠️ [Gemini SSE Patch] SDK 内部符号缺失 %s，可能是版本升级改了实现，"
            "跳过补丁（流式响应遇到 SSE 注释行仍会报错，需要人工重新适配本文件）",
            missing,
        )
        return False

    def _iter_response_stream(self) -> Iterator[str]:
        """同步版：balance-counting 兜底逻辑前跳过 SSE 注释行（`:` 开头）。"""
        requests_module = _api_client._common.loaded_requests()
        if not (
            isinstance(self.response_stream, _api_client._HTTPX_RESPONSE_TYPES)
            or (
                requests_module is not None
                and isinstance(self.response_stream, requests_module.Response)
            )
        ):
            raise TypeError(
                'Expected self.response_stream to be an httpx.Response object, '
                f'but got {type(self.response_stream).__name__}.'
            )

        chunk = ''
        balance = 0
        data_buffer: list = []
        if isinstance(self.response_stream, _api_client._HTTPX_RESPONSE_TYPES):
            response_stream = self.response_stream.iter_lines()
        else:
            response_stream = self.response_stream.iter_lines(decode_unicode=True)
        for line in response_stream:
            if not line:
                if data_buffer:
                    yield '\n'.join(data_buffer)
                    data_buffer = []
                continue
            if line.startswith(':'):
                # SSE 规范允许的注释行（如 cli-proxy-api 的 `: keep-alive`）。
                # 原版 SDK 没有这个分支，会误当 JSON 片段解析导致崩溃——这是本
                # 补丁唯一新增的逻辑，其余部分是原方法的忠实复制。
                continue
            if line.startswith('data: '):
                data_buffer.append(line[len('data: '):])
                continue
            for c in line:
                if c == '{':
                    balance += 1
                elif c == '}':
                    balance -= 1
            chunk += line
            if balance == 0:
                yield chunk
                chunk = ''
        if chunk:
            yield chunk
        if data_buffer:
            yield '\n'.join(data_buffer)

    async def _aiter_response_stream(self) -> AsyncIterator[str]:
        """异步版：httpx / aiohttp 两条分支都加上 SSE 注释行跳过。"""
        is_valid_response = isinstance(
            self.response_stream, _api_client._HTTPX_RESPONSE_TYPES
        ) or (
            _api_client.has_aiohttp
            and isinstance(self.response_stream, _api_client.aiohttp.ClientResponse)
        )
        if not is_valid_response:
            raise TypeError(
                'Expected self.response_stream to be an httpx.Response or'
                ' aiohttp.ClientResponse object, but got'
                f' {type(self.response_stream).__name__}.'
            )

        chunk = ''
        balance = 0
        data_buffer: list = []
        if isinstance(self.response_stream, _api_client._HTTPX_RESPONSE_TYPES):
            try:
                response_stream: Any = self.response_stream
                async for line in response_stream.aiter_lines():
                    if not line:
                        if data_buffer:
                            yield '\n'.join(data_buffer)
                            data_buffer = []
                        continue
                    if line.startswith(':'):
                        continue
                    if line.startswith('data: '):
                        data_buffer.append(line[len('data: '):])
                        continue
                    for c in line:
                        if c == '{':
                            balance += 1
                        elif c == '}':
                            balance -= 1
                    chunk += line
                    if balance == 0:
                        yield chunk
                        chunk = ''
                if chunk:
                    yield chunk
                if data_buffer:
                    yield '\n'.join(data_buffer)
            finally:
                await response_stream.aclose()

        elif _api_client.has_aiohttp and isinstance(
            self.response_stream, _api_client.aiohttp.ClientResponse
        ):
            try:
                while True:
                    try:
                        line_bytes = await self.response_stream.content.readline(
                            max_line_length=_api_client.READ_BUFFER_SIZE
                        )
                    except TypeError:
                        line_bytes = await self.response_stream.content.readline()
                    if not line_bytes:
                        break
                    line = line_bytes.decode('utf-8').rstrip()
                    if not line:
                        if data_buffer:
                            yield '\n'.join(data_buffer)
                            data_buffer = []
                        continue
                    if line.startswith(':'):
                        continue
                    if line.startswith('data: '):
                        data_buffer.append(line[len('data: '):])
                        continue
                    for c in line:
                        if c == '{':
                            balance += 1
                        elif c == '}':
                            balance -= 1
                    chunk += line
                    if balance == 0:
                        yield chunk
                        chunk = ''
                if chunk:
                    yield chunk
                if data_buffer:
                    yield '\n'.join(data_buffer)
            finally:
                self.response_stream.release()

    _api_client.HttpResponse._iter_response_stream = _iter_response_stream
    _api_client.HttpResponse._aiter_response_stream = _aiter_response_stream
    _PATCHED = True
    logger.info("✅ [Gemini SSE Patch] 已修补 SSE 流解析器，跳过 `:` 开头注释行")
    print("✅ [Gemini SSE Patch] 已修补 google-genai SSE 解析器，跳过 keep-alive 注释行")
    return True
