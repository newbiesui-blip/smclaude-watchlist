import pandas as pd

from athena_institutional_engine import evaluate, _defined_dealing_range, _range_sweep_state, _sweep_matrix


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
    data = tf_results()
    data["1D"]["swing_high_prices"] = [145.0]
    data["4H"]["swing_high_prices"] = [140.0, 145.0]
    data["1H"]["swing_high_prices"] = [140.0, 145.0]
    result = evaluate({"setup_type": "MOMENTUM_CONTINUATION", "trade_type": "INTRADAY", "extension_ratio_pct": 150}, data, "BULLISH")
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
    # The hard structural-R:R gate is evaluated before the HTF-conflict gate.
    assert result["no_trade_code"] == "STRUCTURAL_RR_BELOW_2"


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
    data["1H"]["df"] = data["1H"]["df"].copy()
    data["1H"]["df"]["high"] = 119.0
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
    data["1D"]["swing_high_prices"] = [140.0, 150.0]
    data["4H"]["swing_high_prices"] = [140.0, 150.0]
    data["1H"]["swing_high_prices"] = [140.0, 150.0]
    data["15M"]["last_event"] = {"type": "BoS", "direction": "bullish", "price": 105.0, "index": 47}
    result = evaluate({"setup_type": "MOMENTUM_CONTINUATION", "trade_type": "INTRADAY", "extension_ratio_pct": 0}, data, "BULLISH")
    assert result["structural_rr"] >= 2.0
    assert result["entry_quality"] >= 75
    assert result["status"] == "READY_MARKET"
    assert result["execution_type"] == "MARKET"


def test_extended_price_never_becomes_ready_market():
    data = tf_results(price=105.0)
    data["1D"]["swing_high_prices"] = [145.0]
    data["4H"]["swing_high_prices"] = [140.0, 145.0]
    data["1H"]["swing_high_prices"] = [140.0, 145.0]
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
    data["1H"]["df"] = data["1H"]["df"].copy()
    data["1H"]["df"]["high"] = 119.0
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


def test_invalidated_structure_cannot_be_ready():
    data = tf_results(price=99.0)
    data["4H"]["swing_low_prices"] = [100.0]
    data["1D"]["swing_low_prices"] = [90.0]
    data["1H"]["swing_low_prices"] = [98.0]
    data["4H"]["last_event"] = {"type": "MSS", "direction": "bullish", "price": 110.0, "index": 47}
    data["4H"]["df"].loc[47, ["open", "close"]] = [110.0, 110.0]
    result = evaluate(
        {"setup_type": "MOMENTUM_CONTINUATION", "trade_type": "INTRADAY", "extension_ratio_pct": 0},
        data,
        "BULLISH",
    )
    assert result["status"] == "INVALID"
    assert result["final_decision"] == "INVALID"


def test_delayed_range_reclaim_is_detected_after_sweep():
    data = tf_results(price=106.0)
    f4 = data["4H"]["df"].copy()
    f4.loc[44, ["low", "open", "close", "high"]] = [95.0, 98.0, 96.0, 100.0]
    f4.loc[45, ["low", "open", "close", "high"]] = [99.0, 99.0, 99.0, 101.0]
    f4.loc[46, ["low", "open", "close", "high"]] = [99.0, 99.0, 100.5, 102.0]
    f4.loc[47, ["low", "open", "close", "high"]] = [100.0, 100.5, 103.0, 104.0]
    data["4H"]["df"] = f4
    result = evaluate(
        {"setup_type": "TREND_PULLBACK", "trade_type": "INTRADAY", "extension_ratio_pct": 0},
        data,
        "BULLISH",
    )
    assert result["setup_type"] == "ACCUMULATION_RANGE_LOW_RECLAIM"


def test_sweep_without_reclaim_is_not_promoted_to_range_reversal():
    data = tf_results(price=97.0)
    f4 = data["4H"]["df"].copy()
    for i in range(40, 48):
        f4.loc[i, ["low", "open", "close", "high"]] = [94.0, 99.0, 97.0, 100.0]
    data["4H"]["df"] = f4
    result = evaluate(
        {"setup_type": "TREND_PULLBACK", "trade_type": "INTRADAY", "extension_ratio_pct": 0},
        data,
        "BULLISH",
    )
    assert result["status"] == "INVALID"
    assert result["no_trade_code"] == "SWEEP_RECLAIM_EXPIRED"


def test_two_tier_target_gate_allows_t1_below_two_when_macro_t2_is_25r_plus():
    data = tf_results(price=105.0)
    data["1D"]["swing_high_prices"] = [145.0]
    data["4H"]["swing_high_prices"] = [115.0, 140.0]
    data["1H"]["swing_high_prices"] = [115.0, 120.0]
    result = evaluate(
        {"setup_type": "MOMENTUM_CONTINUATION", "trade_type": "INTRADAY", "extension_ratio_pct": 0},
        data,
        "BULLISH",
    )
    assert result["nearest_target_rr"] < 2.0
    assert result["primary_macro_target_rr"] >= 2.5
    assert result["target_gate"]["gate_b_two_tier_active"] is True
    assert result["status"] in {"READY_MARKET", "READY_LIMIT", "WAIT_PULLBACK"}
    assert result["position_management"]["scale_out_pct"] == 50
    assert result["position_management"]["scale_out_target"] == result["validated_targets"][0]["price"]
    assert result["position_management"]["runner_pct"] == 50
    assert result["position_management"]["runner_target"] == result["validated_targets"][1]["price"]
    assert result["position_management"]["post_t1_stop"] == "BREAKEVEN"


def test_two_tier_target_gate_aborts_when_both_t1_and_t2_fail_two_r():
    data = tf_results(price=105.0)
    data["1D"]["swing_high_prices"] = [112.0]
    data["4H"]["swing_high_prices"] = [110.0]
    data["1H"]["swing_high_prices"] = [110.0]
    result = evaluate(
        {"setup_type": "MOMENTUM_CONTINUATION", "trade_type": "INTRADAY", "extension_ratio_pct": 0},
        data,
        "BULLISH",
    )
    assert result["nearest_target_rr"] < 2.0
    assert result["primary_macro_target_rr"] < 2.0
    assert result["status"] == "NO_TRADE"
    assert result["no_trade_code"] == "STRUCTURAL_RR_BELOW_2"


def test_initial_mss_displacement_body_controls_structural_stop():
    data = tf_results(price=110.0)
    data["4H"]["last_event"] = {"type": "MSS", "direction": "bullish", "price": 105.0, "index": 47}
    data["4H"]["df"].loc[47, ["open", "close"]] = [105.0, 110.0]
    data["4H"]["swing_low_prices"] = [100.0]
    result = evaluate(
        {"setup_type": "MOMENTUM_CONTINUATION", "trade_type": "INTRADAY", "extension_ratio_pct": 0},
        data,
        "BULLISH",
    )
    # Flat fixture ATR is 20, so 0.5 ATR below the displacement body floor is 95.
    assert result["invalidation_timeframe"] == "4H"
    assert abs(result["invalidation"] - 95.0) < 1e-9
    assert "initial 4H MSS displacement body floor" in result["invalidation_reason"]


def test_four_candle_sweep_window_keeps_unreclaimed_sweep_pending_before_expiry():
    data = tf_results(price=97.0)
    f4 = data["4H"]["df"].copy()
    f4.loc[45, ["low", "open", "close", "high"]] = [94.0, 99.0, 97.0, 100.0]
    f4.loc[46, ["low", "open", "close", "high"]] = [97.0, 98.0, 97.0, 100.0]
    f4.loc[47, ["low", "open", "close", "high"]] = [97.0, 98.0, 97.0, 100.0]
    data["4H"]["df"] = f4
    result = evaluate(
        {"setup_type": "TREND_PULLBACK", "trade_type": "INTRADAY", "extension_ratio_pct": 0},
        data,
        "BULLISH",
    )
    assert result["sweep_state"]["state"] == "PENDING_RECLAIM"
    assert result["status"] != "INVALID"


def test_four_candle_sweep_window_expires_without_reclaim():
    data = tf_results(price=97.0)
    f4 = data["4H"]["df"].copy()
    f4.loc[42, ["low", "open", "close", "high"]] = [94.0, 99.0, 97.0, 100.0]
    for i in (43, 44, 45, 46, 47):
        f4.loc[i, ["low", "open", "close", "high"]] = [97.0, 98.0, 97.0, 100.0]
    data["4H"]["df"] = f4
    result = evaluate(
        {"setup_type": "TREND_PULLBACK", "trade_type": "INTRADAY", "extension_ratio_pct": 0},
        data,
        "BULLISH",
    )
    assert result["status"] == "INVALID"
    assert result["final_decision"] == "INVALID"
    assert result["no_trade_code"] == "SWEEP_RECLAIM_EXPIRED"


def test_dynamic_range_uses_verified_spot_cvd_and_macro_foundation():
    data = tf_results(price=105.0)
    data["4H"]["spot_cvd_expansion_verified"] = True
    data["4H"]["macro_higher_low"] = 90.0
    data["4H"]["swing_low_prices"] = [90.0]
    data["4H"]["swing_high_prices"] = [130.0]
    data["1D"]["swing_high_prices"] = [145.0]
    data["1H"]["swing_high_prices"] = [130.0, 145.0]
    from athena_institutional_engine import _range
    lo, hi = _range(data, "4H", direction="BULLISH", price=105.0)
    assert lo == 90.0
    assert hi >= 120.0


def test_multiple_wick_penetrations_stay_one_sweep_until_delayed_reclaim():
    data = tf_results(price=104.0)
    f4 = data["4H"]["df"].copy()
    f4.loc[44, ["low", "open", "close", "high"]] = [95.0, 99.0, 98.0, 100.0]
    f4.loc[45, ["low", "open", "close", "high"]] = [94.0, 98.0, 97.0, 100.0]
    f4.loc[46, ["low", "open", "close", "high"]] = [99.0, 98.0, 99.0, 101.0]
    f4.loc[47, ["low", "open", "close", "high"]] = [100.0, 100.0, 104.0, 105.0]
    data["4H"]["df"] = f4

    info = _sweep_matrix(data, "BULLISH")
    assert info["index"] == 44
    assert info["reclaimed"] is True
    assert info["reclaim_index"] == 47
    assert info["penetration_indices"] == [44, 45]
    assert info["extreme"] == 94.0


def test_reclaim_on_third_execution_candle_is_valid():
    data = tf_results(price=104.0)
    f4 = data["4H"]["df"].copy()
    f4.loc[44, ["low", "open", "close", "high"]] = [95.0, 99.0, 97.0, 100.0]
    f4.loc[45, ["low", "open", "close", "high"]] = [98.0, 98.0, 97.0, 100.0]
    f4.loc[46, ["low", "open", "close", "high"]] = [99.0, 98.0, 99.0, 101.0]
    f4.loc[47, ["low", "open", "close", "high"]] = [100.0, 100.0, 104.0, 105.0]
    data["4H"]["df"] = f4

    state = _range_sweep_state(data, "BULLISH")
    assert state["state"] == "RECLAIMED"
    assert state["reclaim_index"] - state["index"] == 3


def test_fourth_execution_candle_without_reclaim_is_expired():
    data = tf_results(price=97.0)
    f4 = data["4H"]["df"].copy()
    f4.loc[43, ["low", "open", "close", "high"]] = [100.0, 102.0, 102.0, 103.0]
    f4.loc[44, ["low", "open", "close", "high"]] = [95.0, 99.0, 97.0, 100.0]
    f4.loc[45, ["low", "open", "close", "high"]] = [97.0, 98.0, 97.0, 100.0]
    f4.loc[46, ["low", "open", "close", "high"]] = [96.0, 98.0, 97.0, 100.0]
    f4.loc[47, ["low", "open", "close", "high"]] = [97.0, 98.0, 97.0, 100.0]
    f4.loc[48, ["low", "open", "close", "high"]] = [96.0, 98.0, 97.0, 100.0]
    data["4H"]["df"] = f4
    data["4H"]["df"].index = range(49)

    state = _range_sweep_state(data, "BULLISH")
    assert state["state"] == "EXPIRED"
    assert state["age_candles"] == 4


def test_multiple_independent_sweeps_selects_latest_episode():
    data = tf_results(price=104.0)
    f4 = data["4H"]["df"].copy()
    # First sweep: reclaim, therefore a completed independent episode.
    f4.loc[42, ["low", "open", "close", "high"]] = [95.0, 99.0, 98.0, 100.0]
    f4.loc[43, ["low", "open", "close", "high"]] = [100.0, 100.0, 104.0, 105.0]
    # Second sweep happens later and is the active episode.
    f4.loc[45, ["low", "open", "close", "high"]] = [94.0, 99.0, 97.0, 100.0]
    f4.loc[46, ["low", "open", "close", "high"]] = [96.0, 98.0, 98.0, 100.0]
    f4.loc[47, ["low", "open", "close", "high"]] = [100.0, 100.0, 104.0, 105.0]
    data["4H"]["df"] = f4

    info = _sweep_matrix(data, "BULLISH")
    assert info["index"] == 45
    assert info["extreme"] == 94.0
    assert info["reclaim_index"] == 47


def test_price_already_outside_range_is_not_a_fresh_liquidity_sweep():
    data = tf_results(price=97.0)
    f4 = data["4H"]["df"].copy()
    for i in range(42, 48):
        f4.loc[i, ["low", "open", "close", "high"]] = [94.0, 96.0, 97.0, 99.0]
    data["4H"]["df"] = f4

    info = _sweep_matrix(data, "BULLISH")
    assert info == {}


def test_sweep_boundary_stays_fixed_while_execution_candles_change():
    data = tf_results(price=104.0)
    before = _defined_dealing_range(data, "BULLISH", 104.0)
    assert before == (100.0, 120.0)

    f4 = data["4H"]["df"].copy()
    f4.loc[47, ["low", "open", "close", "high"]] = [94.0, 99.0, 104.0, 110.0]
    data["4H"]["df"] = f4

    after = _defined_dealing_range(data, "BULLISH", 104.0)
    assert after == before


def test_sweep_reclaim_direction_is_symmetric_for_bearish_setup():
    data = tf_results(price=114.0, bullish=False)
    f4 = data["4H"]["df"].copy()
    f4.loc[44, ["low", "open", "close", "high"]] = [110.0, 119.0, 121.0, 126.0]
    f4.loc[45, ["low", "open", "close", "high"]] = [110.0, 121.0, 121.0, 125.0]
    f4.loc[46, ["low", "open", "close", "high"]] = [110.0, 121.0, 121.0, 126.0]
    f4.loc[47, ["low", "open", "close", "high"]] = [110.0, 121.0, 114.0, 122.0]
    data["4H"]["df"] = f4

    info = _sweep_matrix(data, "BEARISH")
    assert info["index"] == 44
    assert info["reclaimed"] is True
    assert info["reclaim_index"] == 47
    assert info["extreme"] == 126.0


def test_breakdown_after_sweep_never_becomes_accumulation_reclaim():
    data = tf_results(price=97.0)
    f4 = data["4H"]["df"].copy()
    f4.loc[43, ["low", "open", "close", "high"]] = [100.0, 102.0, 102.0, 103.0]
    for i in range(44, 48):
        f4.loc[i, ["low", "open", "close", "high"]] = [94.0, 99.0, 97.0, 100.0]
    data["4H"]["df"] = f4

    result = evaluate(
        {"setup_type": "TREND_PULLBACK", "trade_type": "INTRADAY", "extension_ratio_pct": 0},
        data,
        "BULLISH",
    )
    assert result["setup_type"] != "ACCUMULATION_RANGE_LOW_RECLAIM"
    assert result["sweep_state"]["state"] in {"PENDING_RECLAIM", "EXPIRED"}


def test_immediate_15m_resistance_blocks_ready_market():
    data = tf_results(price=105.0)
    data["1D"]["swing_high_prices"] = [140.0, 150.0]
    data["4H"]["swing_high_prices"] = [140.0, 150.0]
    data["1H"]["swing_high_prices"] = [140.0, 150.0]
    data["15M"]["swing_high_prices"] = [105.25]
    data["15M"]["last_event"] = {"type": "BoS", "direction": "bullish", "price": 105.0, "index": 47}

    result = evaluate(
        {"setup_type": "MOMENTUM_CONTINUATION", "trade_type": "INTRADAY", "extension_ratio_pct": 0},
        data,
        "BULLISH",
    )

    assert result["entry_separation"]["level"] == 105.25
    assert result["entry_separation"]["distance_atr"] < 0.50
    assert result["entry_quality"] < 60
    assert result["status"] == "WAIT_PULLBACK"
    assert result["final_decision"] == "WAIT_PULLBACK"


def test_immediate_15m_support_blocks_bearish_market_entry():
    data = tf_results(price=115.0, bullish=False)
    data["1D"]["swing_low_prices"] = [80.0, 85.0]
    data["4H"]["swing_low_prices"] = [80.0, 85.0]
    data["1H"]["swing_low_prices"] = [80.0, 85.0]
    data["15M"]["swing_low_prices"] = [114.75]
    data["15M"]["last_event"] = {"type": "BoS", "direction": "bearish", "price": 115.0, "index": 47}

    result = evaluate(
        {"setup_type": "MOMENTUM_CONTINUATION", "trade_type": "INTRADAY", "extension_ratio_pct": 0},
        data,
        "BEARISH",
    )

    assert result["entry_separation"]["level"] == 114.75
    assert result["entry_quality"] < 60
    assert result["status"] == "WAIT_PULLBACK"


def test_good_setup_with_resistance_beyond_one_atr_can_still_be_ready():
    data = tf_results(price=105.0)
    data["1D"]["swing_high_prices"] = [140.0, 150.0]
    data["4H"]["swing_high_prices"] = [140.0, 150.0]
    data["1H"]["swing_high_prices"] = [140.0, 150.0]
    data["15M"]["swing_high_prices"] = [115.5]
    data["15M"]["last_event"] = {"type": "BoS", "direction": "bullish", "price": 105.0, "index": 47}

    result = evaluate(
        {"setup_type": "MOMENTUM_CONTINUATION", "trade_type": "INTRADAY", "extension_ratio_pct": 0},
        data,
        "BULLISH",
    )

    assert result["entry_separation"]["distance_atr"] > 1.0
    assert result["entry_quality"] >= 60
    assert result["status"] == "READY_MARKET"


def test_nearby_supply_zone_is_treated_as_entry_resistance():
    data = tf_results(price=105.0)
    data["1D"]["swing_high_prices"] = [140.0, 150.0]
    data["4H"]["swing_high_prices"] = [140.0, 150.0]
    data["1H"]["swing_high_prices"] = [140.0, 150.0]
    data["15M"]["swing_high_prices"] = []
    data["15M"]["bearish_zones"] = [{"low": 105.2, "high": 106.0}]
    data["15M"]["last_event"] = {"type": "BoS", "direction": "bullish", "price": 105.0, "index": 47}

    result = evaluate(
        {"setup_type": "MOMENTUM_CONTINUATION", "trade_type": "INTRADAY", "extension_ratio_pct": 0},
        data,
        "BULLISH",
    )

    assert result["entry_separation"]["level"] == 105.2
    assert result["entry_quality"] < 60
    assert result["status"] == "WAIT_PULLBACK"


def test_entry_separation_does_not_override_structural_rr_gate():
    data = tf_results(price=119.0)
    data["4H"]["swing_high_prices"] = [120.0, 130.0]
    data["1D"]["swing_high_prices"] = [121.0, 130.0]
    data["1H"]["swing_high_prices"] = [120.5, 130.0]
    data["15M"]["swing_high_prices"] = [119.1]

    result = evaluate(
        {"setup_type": "MOMENTUM_CONTINUATION", "trade_type": "INTRADAY", "extension_ratio_pct": 0},
        data,
        "BULLISH",
    )

    assert result["status"] == "NO_TRADE"
    assert result["no_trade_code"] == "STRUCTURAL_RR_BELOW_2"
