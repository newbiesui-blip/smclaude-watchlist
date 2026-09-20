import pandas as pd

from trade_health_engine import evaluate


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


def tf(price=105.0):
    f4 = frame(price=price, low=100, high=120)
    f1 = frame(price=price, low=100, high=115)
    f15 = frame(price=price, low=102, high=110)
    return {
        "4H": {"bias": "BULLISH", "swing_low_prices": [90, 100], "swing_high_prices": [120, 140], "df": f4},
        "1H": {"bias": "BULLISH", "swing_low_prices": [100, 102], "swing_high_prices": [115, 125], "df": f1},
        "15M": {"bias": "BULLISH", "swing_low_prices": [102], "swing_high_prices": [110], "price": price, "df": f15},
    }


def entry(price=105.0):
    return {
        "symbol": "ZEC-USDT",
        "direction": "BULLISH",
        "status": "triggered",
        "exchange_sync_status": "OPEN",
        "entry_price": 100.0,
        "original_invalidation": 90.0,
        "current_r": (price - 100.0) / 10.0,
        "max_r": 5.0,
        "reversal_state": "STABLE",
    }


def cg_base():
    return {
        "enabled": True,
        "availability": "OK",
        "oi_history": [{"close": 100.0}, {"close": 104.0}],
        "spot_cvd_history": [{"cum_vol_delta": 0.0}, {"cum_vol_delta": 100.0}],
        "futures_cvd_history": [{"cum_vol_delta": 0.0}, {"cum_vol_delta": 80.0}],
        "liquidation_history": [
            {"aggregated_long_liquidation_usd": 1000.0, "aggregated_short_liquidation_usd": 500.0},
            {"aggregated_long_liquidation_usd": 1000.0, "aggregated_short_liquidation_usd": 500.0},
        ],
        "spot_taker_history": [
            {"aggregated_buy_volume_usd": 1100.0, "aggregated_sell_volume_usd": 900.0},
            {"aggregated_buy_volume_usd": 1200.0, "aggregated_sell_volume_usd": 800.0},
        ],
    }


def test_fresh_participation_supports_healthy_hold():
    data = tf(110)
    data["1H"]["df"].loc[46, "close"] = 106
    data["1H"]["df"].loc[47, "close"] = 110
    result = evaluate(entry(110), data, {"state": "SUPPORTIVE", "score": 40}, cg_base())
    assert result["trade_health_state"] == "HEALTHY"
    assert result["participation"]["state"] == "FRESH_PARTICIPATION"
    assert result["health_action"] == "HOLD_TRAIL"


def test_rising_price_falling_oi_short_liquidations_is_not_called_fresh_money():
    data = tf(120)
    data["1H"]["df"].loc[46, "close"] = 115
    data["1H"]["df"].loc[47, "close"] = 120
    cg = cg_base()
    cg["oi_history"] = [{"close": 110.0}, {"close": 100.0}]
    cg["spot_cvd_history"] = [{"cum_vol_delta": 100.0}, {"cum_vol_delta": 20.0}]
    cg["liquidation_history"] = [
        {"aggregated_long_liquidation_usd": 1000.0, "aggregated_short_liquidation_usd": 9000.0},
        {"aggregated_long_liquidation_usd": 500.0, "aggregated_short_liquidation_usd": 8000.0},
    ]
    result = evaluate(entry(120), data, {"state": "NEUTRAL", "score": 0}, cg)
    assert result["participation"]["state"] == "LIQUIDATION_DRIVEN_UP"
    assert "liquidation" in result["reason"].lower()


def test_long_liquidation_pullback_can_remain_healthy_when_structure_intact():
    data = tf(105)
    data["1H"]["df"].loc[46, "close"] = 108
    data["1H"]["df"].loc[47, "close"] = 105
    cg = cg_base()
    cg["oi_history"] = [{"close": 110.0}, {"close": 100.0}]
    cg["spot_cvd_history"] = [{"cum_vol_delta": 50.0}, {"cum_vol_delta": 80.0}]
    cg["liquidation_history"] = [
        {"aggregated_long_liquidation_usd": 9000.0, "aggregated_short_liquidation_usd": 500.0},
        {"aggregated_long_liquidation_usd": 8000.0, "aggregated_short_liquidation_usd": 300.0},
    ]
    result = evaluate(entry(105), data, {"state": "LIQUIDATION_EVENT", "score": -10}, cg)
    assert result["participation"]["state"] == "LIQUIDATION_DRIVEN_DOWN"
    assert result["trade_health_state"] != "INVALIDATED"


def test_structure_break_is_invalidated_even_if_derivatives_look_supportive():
    data = tf(89)
    data["1H"]["df"].loc[46, "close"] = 92
    data["1H"]["df"].loc[47, "close"] = 89
    result = evaluate(entry(89), data, {"state": "SUPPORTIVE", "score": 50}, cg_base())
    assert result["trade_health_state"] == "INVALIDATED"
    assert result["health_action"] == "EXIT"


def test_missing_coinglass_does_not_create_fake_confidence():
    data = tf(110)
    data["1H"]["df"].loc[46, "close"] = 106
    data["1H"]["df"].loc[47, "close"] = 110
    result = evaluate(entry(110), data, {"state": "NEUTRAL", "score": 0}, {"enabled": False, "availability": "NOT_CONFIGURED"})
    assert result["trade_health_state"] == "REASSESS"


def test_trail_candidate_is_structural_and_keeps_noise_room():
    data = tf(130)
    data["1H"]["swing_low_prices"] = [118, 122]
    data["4H"]["swing_low_prices"] = [105, 115]
    result = evaluate(entry(130), data, {"state": "SUPPORTIVE", "score": 40}, cg_base())
    trail = result["trail_guidance"]
    assert trail is not None
    assert trail["candidate"] < 130
    assert trail["timeframe"] in {"1H", "4H"}


def test_directional_symmetry_for_short():
    data = tf(90)
    for tf_name in ("4H", "1H", "15M"):
        data[tf_name]["bias"] = "BEARISH"
    data["1H"]["swing_high_prices"] = [98, 102]
    e = entry(90)
    e["direction"] = "BEARISH"
    e["entry_price"] = 100
    e["original_invalidation"] = 110
    e["current_r"] = 1
    result = evaluate(e, data, {"state": "SUPPORTIVE", "score": 40}, cg_base())
    assert result["trade_health_state"] in {"HEALTHY", "REASSESS"}
    assert result["trail_guidance"]["candidate"] > 90
