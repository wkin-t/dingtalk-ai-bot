# -*- coding: utf-8 -*-
import pytest

from app.ai.sampling_clamp import clamp_temperature, clamp_top_p


@pytest.mark.parametrize(
    ("provider", "low", "high"),
    [
        ("gemini", 0.0, 2.0),
        ("openai", 0.0, 2.0),
        ("openrouter", 0.0, 2.0),
        ("openclaw", 0.0, 1.0),
    ],
)
def test_clamp_temperature_boundaries(provider, low, high):
    assert clamp_temperature(low, provider) == low
    assert clamp_temperature(high, provider) == high
    assert clamp_temperature(low - 0.01, provider) == low
    assert clamp_temperature(high + 0.01, provider) == high


def test_clamp_temperature_unknown_provider_falls_through():
    assert clamp_temperature(-0.5, "unknown") == -0.5
    assert clamp_temperature(2.5, "unknown") == 2.5


@pytest.mark.parametrize("provider", ["gemini", "openai", "openrouter", "openclaw"])
def test_clamp_top_p_boundaries(provider):
    assert clamp_top_p(0.0, provider) == 0.0
    assert clamp_top_p(1.0, provider) == 1.0
    assert clamp_top_p(-0.01, provider) == 0.0
    assert clamp_top_p(1.01, provider) == 1.0


def test_clamp_top_p_unknown_provider_falls_through():
    assert clamp_top_p(-0.1, "unknown") == -0.1
    assert clamp_top_p(1.2, "unknown") == 1.2
