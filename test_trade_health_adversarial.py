import pandas as pd

from trade_health_engine import evaluate


def _frame(closes, low=100.0, high=120.0, n=48):
    values = list(closes)
    while len(values) < n:
        values.insert(0, values[0])
    rows = []
    for close in values:
        rows.append({
            "open": close,
            "high": high,
            "low": low,
            "close": close,
            "volume": 1000.0,
        })
    return pd.DataFrame(rows)


def _tf(price=105.0):
    return {
        "4H": {
            "bias": "BULLISH",
            "swing_low_prices": [90, 100],
            "swing_high_prices": [120, 140],
            "df": _frame([price]),
        },
        "1H": {
            "bias": "BULLISH",
            "swing_low_prices": [100, 102],
            "swing_high_prices": [115, 125],
            "df": _frame([price]),
        },
        "15M": {
            "bias": "BULLISH",
            "swing_low_prices": [102],
            "swing_high_prices": [110],
            "price": price,
            "df": _frame([price]),
        },
    }


def _entry(symbol, price, direction="BULLISH"):
    return {
        "symbol": symbol,
        "direction": direction,
        "status": "triggered",
        "exchange_sync_status": "OPEN",
        "entry_price": 100.0,
        "original_invalidation": 90.0 if direction == "BULLISH" else 110.0,
        "current_r": (price - 100.0) / 10.0 if direction == "BULLISH" else (100.0 - price) / 10.0,
        "max_r": 5.0,
        "reversal_state": "STABLE",
    }


def _cg(oi=(100.0, 104.0), spot=(0.0, 100.0), long_liq=1000.0, short_liq=500.0):
    return {
        "enabled": True,
        "availability": "OK",
        "oi_history": [{"close": oi[0]}, {"close": oi[1]}],
        "spot_cvd_history": [
            {"cum_vol_delta": spot[0]},
            {"cum_vol_delta": spot[1]},
        ],
        "futures_cvd_history": [
            {"cum_vol_delta": 0.0},
            {"cum_vol_delta": 80.0},
        ],
        "liquidation_history": [
            {
                "aggregated_long_liquidation_usd": long_liq,
                "aggregated_short_liquidation_usd": short_liq,
            },
            {
                "aggregated_long_liquidation_usd": long_liq,
                "aggregated_short_liquidation_usd": short_liq,
            },
        ],
        "spot_taker_history": [
            {"aggregated_buy_volume_usd": 1100.0, "aggregated_sell_volume_usd": 900.0},
            {"aggregated_buy_volume_usd": 1200.0, "aggregated_sell_volume_usd": 800.0},
        ],
    }


def test_zec_like_pullback_with_long_liquidations_and_positive_spot_flow_stays_healthy():
    data = _tf(105.0)
    data["1H"]["df"] = _frame([108.0, 105.0])
    cg = _cg(
        oi=(110.0, 100.0),
        spot=(50.0, 80.0),
        long_liq=9000.0,
        short_liq=500.0,
    )

    result = evaluate(
        _entry("ZEC-USDT", 105.0),
        data,
        {"state": "LIQUIDATION_EVENT", "score": -10},
        cg,
    )

    assert result["participation"]["state"] == "LIQUIDATION_DRIVEN_DOWN"
    assert result["structural_status"] == "INTACT"
    assert result["trade_health_state"] == "HEALTHY"
    assert result["health_action"] == "HOLD_TRAIL"


def test_arb_like_rally_driven_by_short_liquidations_is_deteriorating():
    data = _tf(120.0)
    data["1H"]["df"] = _frame([115.0, 120.0])
    cg = _cg(
        oi=(110.0, 100.0),
        spot=(100.0, 20.0),
        long_liq=500.0,
        short_liq=17000.0,
    )

    result = evaluate(
        _entry("ARB-USDT", 120.0),
        data,
        {"state": "NEUTRAL", "score": 0},
        cg,
    )

    assert result["participation"]["state"] == "LIQUIDATION_DRIVEN_UP"
    assert result["trade_health_state"] == "DETERIORATING"
    assert result["health_action"] == "TAKE_PARTIALS_OR_TIGHTEN"
    assert "liquidation/covering-driven" in result["reason"]


def test_fresh_arb_participation_is_not_penalized_as_liquidation_driven():
    data = _tf(120.0)
    data["1H"]["df"] = _frame([115.0, 120.0])
    cg = _cg(
        oi=(100.0, 105.0),
        spot=(0.0, 100.0),
        long_liq=500.0,
        short_liq=500.0,
    )

    result = evaluate(
        _entry("ARB-USDT", 120.0),
        data,
        {"state": "SUPPORTIVE", "score": 40},
        cg,
    )

    assert result["participation"]["state"] == "FRESH_PARTICIPATION"
    assert result["trade_health_state"] == "HEALTHY"
    assert result["health_action"] == "HOLD_TRAIL"


def test_structural_failure_overrides_favorable_participation():
    data = _tf(89.0)
    data["1H"]["df"] = _frame([88.0, 89.0])
    cg = _cg(
        oi=(100.0, 105.0),
        spot=(0.0, 100.0),
        long_liq=500.0,
        short_liq=500.0,
    )

    result = evaluate(
        _entry("ARB-USDT", 89.0),
        data,
        {"state": "SUPPORTIVE", "score": 50},
        cg,
    )

    assert result["participation"]["state"] == "FRESH_PARTICIPATION"
    assert result["trade_health_state"] == "INVALIDATED"
    assert result["health_action"] == "EXIT"


def test_missing_coinglass_requires_reassessment_instead_of_fake_health():
    data = _tf(110.0)
    data["1H"]["df"] = _frame([106.0, 110.0])

    result = evaluate(
        _entry("ARB-USDT", 110.0),
        data,
        {"state": "NEUTRAL", "score": 0},
        {"enabled": False, "availability": "NOT_CONFIGURED"},
    )

    assert result["trade_health_state"] == "REASSESS"
    assert result["health_action"] == "REASSESS"


def test_short_trade_uses_directional_symmetry():
    data = _tf(90.0)
    for tf_name in ("4H", "1H", "15M"):
        data[tf_name]["bias"] = "BEARISH"
    data["1H"]["swing_high_prices"] = [98, 102]

    result = evaluate(
        _entry("ARB-USDT", 90.0, direction="BEARISH"),
        data,
        {"state": "SUPPORTIVE", "score": 40},
        _cg(),
    )

    assert result["trade_health_state"] in {"HEALTHY", "REASSESS"}
    assert result["trail_guidance"]["candidate"] > 90.0
