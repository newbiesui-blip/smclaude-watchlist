import pandas as pd

from athena_institutional_engine import evaluate


def frame(price=105.0, low=100.0, high=120.0, n=48):
    rows = []
    for i in range(n):
        rows.append({
            "open": price,
            "high": high,
            "low": low,
            "close": price,
            "volume": 1000.0,
        })
    return pd.DataFrame(rows)


def tf_results(price=105.0, bullish=True, accumulation=False):
    bias = "BULLISH" if bullish else "BEARISH"
    f4 = frame(price=price, low=100, high=120)
    if accumulation:
        # Old range establishes 100/120; recent candles raid 100 and reclaim it.
        for i in range(40, 48):
            f4.loc[i, "low"] = 95.0
            f4.loc[i, "open"] = 98.0
            f4.loc[i, "close"] = 104.0
            f4.loc[i, "high"] = 106.0
    f1 = frame(price=price, low=100, high=125)
    f15 = frame(price=price, low=102, high=112)
    return {
        "1D": {"bias": bias, "swing_high_prices": [130], "swing_low_prices": [90], "df": frame(price=price, low=90, high=130)},
        "4H": {"bias": bias if not accumulation else "RANGING", "swing_high_prices": [120, 130], "swing_low_prices": [100, 90], "df": f4},
        "1H": {"bias": bias, "swing_high_prices": [115, 125], "swing_low_prices": [100, 95], "df": f1},
        "15M": {
            "bias": bias,
            "price": price,
            "swing_high_prices": [112],
            "swing_low_prices": [102],
            "recent_sweep": {"direction": "bullish", "price": 100, "index": 47} if bullish else None,
            "last_event": {"type": "BoS", "direction": "bullish" if bullish else "bearish", "price": price, "index": 47},
            "df": f15,
        },
    }


def test_structural_stop_uses_4h_before_15m():
    result = evaluate({"setup_type": "MOMENTUM_CONTINUATION", "trade_type": "INTRADAY", "extension_ratio_pct": 0}, tf_results(), "BULLISH")
    assert result["invalidation_timeframe"] == "4H"
    assert result["invalidation"] < 100


def test_structural_rr_below_two_is_no_trade():
    data = tf_results(price=119.0)
    data["4H"]["swing_high_prices"] = [120.0]
    data["1D"]["swing_high_prices"] = [121.0]
    data["1H"]["swing_high_prices"] = [120.5]
    result = evaluate({"setup_type": "MOMENTUM_CONTINUATION", "trade_type": "INTRADAY", "extension_ratio_pct": 0}, data, "BULLISH")
    assert result["status"] == "NO_TRADE"
    assert result["no_trade_code"] == "STRUCTURAL_RR_BELOW_2"


def test_accumulation_range_low_reclaim_is_supported():
    result = evaluate({"setup_type": "TREND_PULLBACK", "trade_type": "INTRADAY", "extension_ratio_pct": 0}, tf_results(accumulation=True), "BULLISH")
    assert result["setup_type"] == "ACCUMULATION_RANGE_LOW_RECLAIM"
    assert result["status"] in {"READY_MARKET", "READY_LIMIT", "WAIT_PULLBACK", "NO_TRADE"}


def test_scalp_penalty_is_exactly_thirty():
    base = {"setup_type": "MOMENTUM_CONTINUATION", "trade_type": "INTRADAY", "extension_ratio_pct": 0}
    normal = evaluate(base, tf_results(), "BULLISH")
    scalp = evaluate({**base, "trade_type": "SCALP"}, tf_results(), "BULLISH")
    assert scalp["scalp_like"] is True
    assert scalp["confidence"] == max(0, normal["confidence"] - 30)


def test_extended_entry_waits_for_pullback():
    result = evaluate({"setup_type": "MOMENTUM_CONTINUATION", "trade_type": "INTRADAY", "extension_ratio_pct": 150}, tf_results(), "BULLISH")
    assert result["status"] == "WAIT_PULLBACK"


def test_countertrend_15m_does_not_override_htf():
    data = tf_results()
    data["1D"]["bias"] = "BEARISH"
    data["4H"]["bias"] = "BEARISH"
    data["1H"]["bias"] = "BEARISH"
    data["15M"]["bias"] = "BULLISH"
    data["15M"]["last_event"] = {"type": "BoS", "direction": "bullish", "price": 105, "index": 47}
    result = evaluate({"setup_type": "MOMENTUM_CONTINUATION", "trade_type": "INTRADAY", "extension_ratio_pct": 0}, data, "BULLISH")
    assert result["status"] == "NO_TRADE"
    assert result["no_trade_code"] == "HTF_CONFLICT_WITHOUT_REVERSAL"


def test_distribution_range_high_rejection_is_detected():
    data = tf_results(price=115.0, bullish=False)
    f4 = data["4H"]["df"].copy()
    for i in range(40, 48):
        f4.loc[i, "high"] = 125.0
        f4.loc[i, "open"] = 122.0
        f4.loc[i, "close"] = 114.0
        f4.loc[i, "low"] = 112.0
    data["4H"]["df"] = f4
    data["4H"]["swing_high_prices"] = [120.0, 130.0]
    data["4H"]["swing_low_prices"] = [100.0, 90.0]
    result = evaluate({"setup_type": "TREND_PULLBACK", "trade_type": "INTRADAY", "extension_ratio_pct": 0}, data, "BEARISH")
    assert result["setup_type"] == "DISTRIBUTION_RANGE_HIGH_REJECTION"
    assert result["invalidation_timeframe"] == "4H"


def test_no_structural_invalidation_aborts():
    data = tf_results(price=105.0)
    data["4H"]["swing_low_prices"] = []
    data["1D"]["swing_low_prices"] = []
    data["1H"]["swing_low_prices"] = []
    result = evaluate({"setup_type": "MOMENTUM_CONTINUATION", "trade_type": "INTRADAY", "extension_ratio_pct": 0}, data, "BULLISH")
    assert result["status"] == "NO_TRADE"
    assert result["no_trade_code"] == "NO_STRUCTURAL_INVALIDATION"


def test_good_structure_can_produce_ready_market_when_rr_and_location_are_valid():
    data = tf_results(price=105.0)
    data["1D"]["swing_high_prices"] = [130.0]
    data["4H"]["swing_high_prices"] = [125.0, 130.0]
    data["1H"]["swing_high_prices"] = [125.0, 130.0]
    data["15M"]["last_event"] = {"type": "BoS", "direction": "bullish", "price": 105.0, "index": 47}
    result = evaluate({"setup_type": "MOMENTUM_CONTINUATION", "trade_type": "INTRADAY", "extension_ratio_pct": 0}, data, "BULLISH")
    assert result["structural_rr"] >= 2.0
    assert result["entry_quality"] >= 75
    assert result["status"] == "READY_MARKET"
    assert result["execution_type"] == "MARKET"


def test_extended_price_never_becomes_ready_market():
    data = tf_results(price=105.0)
    data["1D"]["swing_high_prices"] = [130.0]
    data["4H"]["swing_high_prices"] = [125.0, 130.0]
    data["1H"]["swing_high_prices"] = [125.0, 130.0]
    result = evaluate({"setup_type": "MOMENTUM_CONTINUATION", "trade_type": "INTRADAY", "extension_ratio_pct": 125}, data, "BULLISH")
    assert result["status"] == "WAIT_PULLBACK"
    assert result["execution_type"] is None


def test_current_price_rr_gate_cannot_be_hidden_by_farther_target():
    data = tf_results(price=119.0)
    # Nearest meaningful structural target is only 1R from current price,
    # while a farther target is >2R. The hard gate must use realistic current
    # execution geometry and reject the trade rather than manufacture R:R.
    data["4H"]["swing_high_prices"] = [120.0, 130.0]
    data["1D"]["swing_high_prices"] = [121.0, 130.0]
    data["1H"]["swing_high_prices"] = [120.5, 130.0]
    result = evaluate(
        {"setup_type": "MOMENTUM_CONTINUATION", "trade_type": "INTRADAY", "extension_ratio_pct": 0},
        data,
        "BULLISH",
    )
    assert result["status"] == "NO_TRADE"
    assert result["no_trade_code"] == "STRUCTURAL_RR_BELOW_2"


def test_missing_atr_cannot_be_treated_as_at_price():
    data = tf_results(price=105.0)
    data["15M"]["df"] = data["15M"]["df"].iloc[:1].copy()
    data["1H"]["df"] = data["1H"]["df"].iloc[:1].copy()
    data["1D"]["swing_high_prices"] = [130.0]
    data["4H"]["swing_high_prices"] = [125.0, 130.0]
    data["1H"]["swing_high_prices"] = [125.0, 130.0]
    data["15M"]["last_event"] = {"type": "BoS", "direction": "bullish", "price": 100.0, "index": 0}
    result = evaluate(
        {"setup_type": "MOMENTUM_CONTINUATION", "trade_type": "INTRADAY", "extension_ratio_pct": 0},
        data,
        "BULLISH",
    )
    # Without a volatility measure we cannot prove that the planned execution
    # level is actually close enough for a market entry.
    assert result["status"] != "READY_MARKET"


def test_no_trade_when_thesis_has_no_structural_target():
    data = tf_results(price=105.0)
    data["1D"]["swing_high_prices"] = []
    data["4H"]["swing_high_prices"] = []
    data["1H"]["swing_high_prices"] = []
    result = evaluate(
        {"setup_type": "MOMENTUM_CONTINUATION", "trade_type": "INTRADAY", "extension_ratio_pct": 0},
        data,
        "BULLISH",
    )
    assert result["status"] == "NO_TRADE"
    assert result["no_trade_code"] == "STRUCTURAL_RR_BELOW_2"


def test_bearish_engine_is_directionally_symmetric():
    data = tf_results(price=105.0, bullish=False)
    data["1D"]["swing_low_prices"] = [80.0]
    data["4H"]["swing_low_prices"] = [85.0, 80.0]
    data["1H"]["swing_low_prices"] = [85.0, 80.0]
    data["15M"]["last_event"] = {"type": "BoS", "direction": "bearish", "price": 105.0, "index": 47}
    result = evaluate(
        {"setup_type": "MOMENTUM_CONTINUATION", "trade_type": "INTRADAY", "extension_ratio_pct": 0},
        data,
        "BEARISH",
    )
    assert result["invalidation_timeframe"] == "4H"
    assert result["invalidation"] > 105.0
    assert result["structural_target"] is not None


def test_institutional_engine_does_not_reintroduce_ob_as_mandatory():
    data = tf_results(price=105.0)
    for tf in ("1D", "4H", "1H", "15M"):
        data[tf]["bullish_zones"] = []
        data[tf]["bearish_zones"] = []
    data["1D"]["swing_high_prices"] = [130.0]
    data["4H"]["swing_high_prices"] = [125.0, 130.0]
    data["1H"]["swing_high_prices"] = [125.0, 130.0]
    result = evaluate(
        {"setup_type": "MOMENTUM_CONTINUATION", "trade_type": "INTRADAY", "extension_ratio_pct": 0},
        data,
        "BULLISH",
    )
    assert result["zone_label"] == "structural execution level"
    assert result["status"] in {"READY_MARKET", "READY_LIMIT", "WAIT_PULLBACK", "NO_TRADE"}


def test_far_structural_entry_cannot_be_called_ready():
    data = tf_results(price=119.0)
    data["1D"]["swing_high_prices"] = [130.0]
    data["4H"]["swing_high_prices"] = [125.0, 130.0]
    data["1H"]["swing_high_prices"] = [125.0, 130.0]
    data["15M"]["last_event"] = {"type": "BoS", "direction": "bullish", "price": 105.0, "index": 47}
    result = evaluate(
        {"setup_type": "MOMENTUM_CONTINUATION", "trade_type": "INTRADAY", "extension_ratio_pct": 0},
        data,
        "BULLISH",
    )
    assert result["status"] in {"WAIT_PULLBACK", "NO_TRADE"}
    assert result["status"] != "READY_MARKET"
