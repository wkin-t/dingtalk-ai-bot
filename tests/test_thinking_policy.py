# -*- coding: utf-8 -*-

import pytest

from app.ai import thinking_policy


@pytest.mark.parametrize(
    "model",
    [
        "gemini-4-hydrogen-low",
        "gemini-4-hydrogen-medium",
        "gemini-4-hydrogen-high",
        "gemini-4-hydrogen-xhigh",
        "google/gemini-4-hydrogen-low",
    ],
)
def test_hydrogen_support_is_independent_of_alias_suffix(model):
    assert thinking_policy.supported_thinking_levels(model, "gemini") == ("low", "medium", "high", "xhigh")


@pytest.mark.parametrize("model", ["gemini-4-argon-high", "gemini-4-carbon-low", "google/gemini-4-carbon-xhigh"])
def test_other_gemini4_models_do_not_gain_xhigh_from_alias(model):
    assert thinking_policy.supported_thinking_levels(model, "gemini") == ("low", "medium", "high")


def test_gemini_family_boundary_does_not_match_hydrogenated_alias():
    assert thinking_policy.supported_thinking_levels("gemini-4-hydrogenated-low", "gemini") == ("low", "medium", "high")


def test_openai_modern_families_and_legacy_capabilities():
    assert thinking_policy.supported_thinking_levels("gpt-6-luna", "openai") == (
        "none", "low", "medium", "high", "xhigh", "max"
    )
    assert thinking_policy.supported_thinking_levels("gpt-6.1-terra", "openai") == (
        "none", "low", "medium", "high", "xhigh", "max"
    )
    assert thinking_policy.supported_thinking_levels("gpt-6.1-sol", "openai") == (
        "low", "medium", "high", "xhigh", "max"
    )
    assert thinking_policy.supported_thinking_levels("gpt-5.6-terra", "openai") == (
        "low", "medium", "high", "xhigh", "max"
    )
    assert thinking_policy.supported_thinking_levels("claude-sonnet-5-5-low", "openai") == (
        "low", "medium", "high", "xhigh", "max"
    )
    assert thinking_policy.supported_thinking_levels("claude-opus-4-6-thinking", "openai") == (
        "low", "medium", "high"
    )
    assert "max" not in thinking_policy.supported_thinking_levels("gpt-4o", "openai")


def test_openrouter_does_not_inherit_relay_max_or_xhigh():
    assert thinking_policy.supported_thinking_levels("claude-sonnet-5-5-low", "openrouter") == ("low", "medium", "high")
    assert thinking_policy.supported_thinking_levels("gpt-6.1-terra", "openrouter") == ("low", "medium", "high")
    assert thinking_policy.supported_thinking_levels("google/gemini-4-hydrogen-low", "openrouter") == ("low", "medium", "high")


def test_unknown_model_is_conservative():
    assert thinking_policy.supported_thinking_levels("vendor/future-model", "openai") == ("low", "medium", "high")


def test_server_side_fallback_uses_common_depth(monkeypatch):
    monkeypatch.setattr(thinking_policy.cfg, "THINKING_LEVEL_OVERRIDES", {"openrouter:primary": ["low", "medium", "high", "max"], "openrouter:backup": ["low", "high"]})
    assert thinking_policy.normalize_thinking_for_models("max", ["primary", "backup"], "openrouter") == "high"
    assert thinking_policy.normalize_thinking_for_models("medium", ["primary", "backup"], "openrouter") == "low"


@pytest.mark.parametrize("model,off", [("gpt-6-luna", True), ("gpt-6-sol", True), ("gpt-6.1-terra", True), ("gpt-6.1-sol", False), ("gpt-6-astra", False), ("gpt-6.1-luna", False), ("gpt-6-terra", False)])
def test_off_support_is_per_model_not_per_gpt_family(model, off):
    assert ("none" in thinking_policy.supported_thinking_levels(model, "openai")) is off


@pytest.mark.parametrize(
    ("level", "model", "backend", "expected"),
    [
        ("xhigh", "gemini-4-argon-low", "gemini", "high"),
        ("xhigh", "gemini-4-hydrogen-low", "gemini", "xhigh"),
        ("max", "gemini-4-hydrogen-low", "gemini", "xhigh"),
        ("max", "gemini-4-carbon-high", "gemini", "high"),
        ("max", "claude-sonnet-5-5-low", "openai", "max"),
        ("xhigh", "claude-sonnet-5-5-low", "openai", "xhigh"),
        ("max", "claude-sonnet-5-5-low", "openrouter", "high"),
        ("minimal", "gemini-4-hydrogen-low", "gemini", "low"),
        ("minimal", "gpt-6-luna", "openai", "none"),
        ("none", "gpt-6-luna", "openai", "none"),
        ("none", "gpt-6.1-sol", "openai", "low"),
        ("minimal", "vendor/future-model", "openai", "low"),
        ("none", "vendor/model", "openai", "low"),
    ],
)
def test_normalize_respects_model_capability(level, model, backend, expected):
    assert thinking_policy.normalize_thinking_level(level, model, backend) == expected


def test_invalid_level_uses_override_lowest_level(monkeypatch):
    monkeypatch.setattr(thinking_policy.cfg, "THINKING_LEVEL_OVERRIDES", {"vendor/model": ["medium", "high"]}, raising=False)
    assert thinking_policy.normalize_thinking_level("invalid", "vendor/model", "openai") == "medium"


def test_override_prefers_backend_and_is_exact(monkeypatch):
    monkeypatch.setattr(
        thinking_policy.cfg,
        "THINKING_LEVEL_OVERRIDES",
        {
            "vendor/exact": ["low", "medium"],
            "openai:vendor/exact": ["none", "low", "medium", "high", "xhigh", "max"],
        },
        raising=False,
    )
    assert thinking_policy.supported_thinking_levels("vendor/exact", "openai") == ("none", "low", "medium", "high", "xhigh", "max")
    assert thinking_policy.supported_thinking_levels("vendor/exact", "gemini") == ("low", "medium")
    assert thinking_policy.supported_thinking_levels("vendor/exact-alias", "openai") == ("low", "medium", "high")


@pytest.mark.parametrize(
    "override",
    [
        {"vendor/model": []},
        {"vendor/model": ["minimal", "low"]},
        {"vendor/model": ["high", "low"]},
        {"vendor/model": ["low", "low"]},
        {"vendor/model": ["bogus"]},
    ],
)
def test_invalid_override_is_not_silently_ignored(monkeypatch, override):
    monkeypatch.setattr(thinking_policy.cfg, "THINKING_LEVEL_OVERRIDES", override, raising=False)
    with pytest.raises(ValueError):
        thinking_policy.supported_thinking_levels("vendor/model", "openai")


def test_capability_prompt_describes_independent_thinking_field():
    prompt = thinking_policy.thinking_capabilities_prompt(
        "gemini",
        {"lite": "gemini-4-hydrogen-low", "fast": "gemini-4-argon-high", "pro": "gemini-4-carbon-high"},
    )
    assert "独立字段 thinking_level" in prompt
    assert "minimal" in prompt
    assert "gemini-4-hydrogen-low → low, medium, high, xhigh" in prompt
    assert "gemini-4-argon-high → low, medium, high" in prompt
    assert "gemini-4-carbon-high → low, medium, high" in prompt
