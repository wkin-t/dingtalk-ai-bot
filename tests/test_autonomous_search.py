"""全自主搜索策略测试 — app/ai/backend.py::resolve_enable_search

SEARCH_AUTONOMOUS 开启时 fast/pro 档强制挂原生搜索工具（模型自决），
lite 档与无原生搜索的路径保持路由 need_search 门控。
"""
import os
import pytest

os.environ.setdefault("GEMINI_API_KEY", "test-dummy-key")

import app.config as cfg
from app.ai.backend import resolve_enable_search

# module import 时、autouse fixture 生效前捕获，钉住代码默认值（防默认翻转的静默变异）
_IMPORT_TIME_DEFAULT = cfg.SEARCH_AUTONOMOUS


@pytest.fixture(autouse=True)
def _autonomous_on(monkeypatch):
    monkeypatch.setattr(cfg, "SEARCH_AUTONOMOUS", True)


def test_default_value_pinned():
    """代码默认值必须为 True——生产 .env 未设此键，默认翻转等于特性静默全关"""
    if os.getenv("SEARCH_AUTONOMOUS") is not None:
        pytest.skip("环境显式设置了 SEARCH_AUTONOMOUS，默认值断言不适用")
    assert _IMPORT_TIME_DEFAULT is True


class TestRequestedPassthrough:
    """路由显式要求搜索时原样放行，不受 flag 影响"""

    def test_requested_true_with_flag_off(self, monkeypatch):
        monkeypatch.setattr(cfg, "SEARCH_AUTONOMOUS", False)
        assert resolve_enable_search("gemini", "fast", True) is True

    def test_requested_true_even_for_lite(self):
        assert resolve_enable_search("gemini", "lite", True) is True

    def test_flag_off_keeps_router_gating(self, monkeypatch):
        monkeypatch.setattr(cfg, "SEARCH_AUTONOMOUS", False)
        assert resolve_enable_search("gemini", "fast", False) is False


class TestGeminiBackend:
    """gemini 后端原生 google_search 工具，fast/pro 强制挂载"""

    def test_fast_forced_on(self):
        assert resolve_enable_search("gemini", "fast", False) is True

    def test_pro_forced_on(self):
        assert resolve_enable_search("gemini", "pro", False) is True

    def test_lite_exempt(self):
        assert resolve_enable_search("gemini", "lite", False) is False

    def test_configured_model_name_resolves_tier(self, monkeypatch):
        # 路由可能输出配置里的具体模型名而非抽象 tier 名
        assert resolve_enable_search("gemini", cfg.MODEL_PRO, False) is True
        # 显式构造确定映射到 lite 的模型名做豁免断言。不用 cfg.MODEL_LITE：gemini 默认配置
        # MODEL_LITE == MODEL_FAST（ROUTE_KEY_MAP 同名键覆盖解析为 fast），lite 豁免天然不生效
        monkeypatch.setitem(cfg.ROUTE_KEY_MAP, "custom-lite-model", "lite")
        assert resolve_enable_search("gemini", "custom-lite-model", False) is False


class TestOpenclawBackend:
    def test_never_forced(self):
        assert resolve_enable_search("openclaw", "fast", False) is False
        assert resolve_enable_search("openclaw", "pro", False) is False


class TestOpenaiBackend:
    def test_supports_search_forced_on(self, monkeypatch):
        monkeypatch.setitem(
            cfg.LITELLM_MODEL_CONFIG, "fast",
            {**cfg.LITELLM_MODEL_CONFIG["fast"], "model": "gpt-5.5", "supports_search": True},
        )
        assert resolve_enable_search("openai", "fast", False) is True

    def test_no_support_keeps_gating(self, monkeypatch):
        monkeypatch.setitem(
            cfg.LITELLM_MODEL_CONFIG, "fast",
            {**cfg.LITELLM_MODEL_CONFIG["fast"], "model": "gpt-5.5", "supports_search": False},
        )
        assert resolve_enable_search("openai", "fast", False) is False

    def test_gemini_upstream_exempt(self, monkeypatch):
        # gemini 上游走 Chat Completions 无原生搜索工具，强制开会落入
        # fallback 注入路径（每条消息真实搜一次），必须豁免
        monkeypatch.setitem(
            cfg.LITELLM_MODEL_CONFIG, "pro",
            {**cfg.LITELLM_MODEL_CONFIG["pro"], "model": "gemini-3.5-pro", "supports_search": True},
        )
        assert resolve_enable_search("openai", "pro", False) is False

    def test_lite_exempt(self, monkeypatch):
        monkeypatch.setitem(
            cfg.LITELLM_MODEL_CONFIG, "lite",
            {**cfg.LITELLM_MODEL_CONFIG["lite"], "model": "gpt-5.5", "supports_search": True},
        )
        assert resolve_enable_search("openai", "lite", False) is False


class TestOpenrouterBackend:
    def test_supports_search_forced_on(self, monkeypatch):
        monkeypatch.setitem(
            cfg.OPENROUTER_MODEL_CONFIG, "pro",
            {**cfg.OPENROUTER_MODEL_CONFIG["pro"], "supports_search": True},
        )
        assert resolve_enable_search("openrouter", "pro", False) is True

    def test_no_support_keeps_gating(self, monkeypatch):
        monkeypatch.setitem(
            cfg.OPENROUTER_MODEL_CONFIG, "fast",
            {**cfg.OPENROUTER_MODEL_CONFIG["fast"], "supports_search": False},
        )
        assert resolve_enable_search("openrouter", "fast", False) is False


class TestCreateBackendStreamIntegration:
    """策略在统一入口真实生效：强制后的 enable_search 传给客户端"""

    @pytest.mark.asyncio
    async def test_gemini_stream_receives_forced_search(self, monkeypatch):
        captured = {}

        async def fake_stream(messages, target_model, thinking_level="low",
                              enable_search=False, temperature=0.7, top_p=None):
            captured["enable_search"] = enable_search
            yield {"content": "ok"}

        import app.gemini_client as gc
        monkeypatch.setattr(gc, "call_gemini_stream", fake_stream)
        monkeypatch.setattr(cfg, "AI_BACKEND", "gemini")

        from app.ai.backend import create_backend_stream
        chunks = [c async for c in create_backend_stream(
            [{"role": "user", "content": "hi"}], target_model="fast", enable_search=False,
        )]
        assert captured["enable_search"] is True
        assert chunks == [{"content": "ok"}]

    @pytest.mark.asyncio
    async def test_openai_stream_receives_forced_search(self, monkeypatch):
        """生产两容器是 AI_BACKEND=openai——强制后的 enable_search 必须传进 call_openai_stream"""
        captured = {}

        async def fake_stream(messages, target_model, thinking_level="low",
                              enable_search=False, temperature=0.7, top_p=None,
                              conversation_id=""):
            captured["enable_search"] = enable_search
            yield {"content": "ok"}

        import app.openai_client as oc
        monkeypatch.setattr(oc, "call_openai_stream", fake_stream)
        monkeypatch.setattr(cfg, "AI_BACKEND", "openai")
        monkeypatch.setitem(
            cfg.LITELLM_MODEL_CONFIG, "fast",
            {**cfg.LITELLM_MODEL_CONFIG["fast"], "model": "gpt-5.5", "supports_search": True},
        )

        from app.ai.backend import create_backend_stream
        chunks = [c async for c in create_backend_stream(
            [{"role": "user", "content": "hi"}], target_model="fast", enable_search=False,
        )]
        assert captured["enable_search"] is True
        assert chunks == [{"content": "ok"}]

    @pytest.mark.asyncio
    async def test_lite_stays_off(self, monkeypatch):
        captured = {}

        async def fake_stream(messages, target_model, thinking_level="low",
                              enable_search=False, temperature=0.7, top_p=None):
            captured["enable_search"] = enable_search
            yield {"content": "ok"}

        import app.gemini_client as gc
        monkeypatch.setattr(gc, "call_gemini_stream", fake_stream)
        monkeypatch.setattr(cfg, "AI_BACKEND", "gemini")

        from app.ai.backend import create_backend_stream
        [c async for c in create_backend_stream(
            [{"role": "user", "content": "hi"}], target_model="lite", enable_search=False,
        )]
        assert captured["enable_search"] is False
