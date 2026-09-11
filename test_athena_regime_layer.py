"""Focused tests for the additive ATHENA Wyckoff regime layer."""
from __future__ import annotations

import pandas as pd

from athena_regime_layer import (
    ACCUMULATION,
    DISTRIBUTION,
    ORDINARY_RANGE,
    enrich_plan,
)


def _frame(closes, lows=None, highs=None):
    lows = lows or [x * 0.99 for x in closes]
    highs = highs or [x * 1.01 for x in closes]
    return pd.DataFrame({
        "open": closes,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": [100.0] * len(closes),
    })


def test_accumulation_like_range():
    closes = [100 + (i % 6) * 0.15 for i in range(48)]
    lows = [99.5] * 47 + [97.5]
    highs = [101.0] * 48
    closes[-1] = 100.4
    df4 = _frame(closes, lows=lows, highs=highs)
    plan = {"direction": "BULLISH", "derivatives_context": {"state": "SHORT_TRAP_RISK", "score": 25, "reason": "short crowding"}}
    enrich_plan(plan, {"4H": {"df": df4}, "1H": {"df": _frame(closes[-24:])}})
    assert plan["market_regime"] == ACCUMULATION
    assert plan["regime_confidence"] >= 70


def test_distribution_like_range():
    closes = [100 + (i % 6) * 0.15 for i in range(48)]
    lows = [99.0] * 48
    highs = [101.0] * 47 + [103.0]
    closes[-1] = 99.7
    df4 = _frame(closes, lows=lows, highs=highs)
    plan = {"direction": "BEARISH", "derivatives_context": {"state": "LONG_TRAP_RISK", "score": -25, "reason": "long crowding"}}
    enrich_plan(plan, {"4H": {"df": df4}, "1H": {"df": _frame(closes[-24:])}})
    assert plan["market_regime"] == DISTRIBUTION
    assert plan["regime_confidence"] >= 70


def test_ordinary_range_is_not_forced_into_accumulation():
    closes = [100 + (i % 4) * 0.2 for i in range(48)]
    df4 = _frame(closes)
    plan = {"direction": "BULLISH", "derivatives_context": {"state": "NEUTRAL", "score": 0}}
    enrich_plan(plan, {"4H": {"df": df4}, "1H": {"df": _frame(closes[-24:])}})
    assert plan["market_regime"] in {ORDINARY_RANGE, "TRENDING"}


if __name__ == "__main__":
    test_accumulation_like_range()
    test_distribution_like_range()
    test_ordinary_range_is_not_forced_into_accumulation()
    print("ATHENA regime-layer tests passed")
