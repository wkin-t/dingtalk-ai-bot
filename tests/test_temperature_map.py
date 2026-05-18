# -*- coding: utf-8 -*-
from app.ai.handler import TEMPERATURE_MAP


def test_temperature_map_contains_all_supported_tiers():
    assert TEMPERATURE_MAP == {
        "precise": 0.1,
        "balanced": 0.7,
        "creative": 0.9,
        "wild": 1.3,
        "chaotic": 1.8,
    }
