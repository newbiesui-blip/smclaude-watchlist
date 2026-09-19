import pandas as pd

import full_scan
import smc_scanner as scanner


def _frame(price=119.0, low=100.0, high=120.0, n=48):
    return pd.DataFrame(
        [{"open": price, "high": high, "low": low, "close": price, "volume": 1000.0}
         for _ in range(n)]
    )


def _tf_results():
    return {
        "1D": {
            "bias": "BULLISH",
            "swing_high_prices": [121.0],
            "swing_low_prices": [90.0],
            "df": _frame(low=90.0, high=121.0),
        },
        "4H": {
            "bias": "BULLISH",
            "swing_high_prices": [120.0],
            "swing_low_prices": [100.0],
            "df": _frame(),
        },
        "1H": {
            "bias": "BULLISH",
            "swing_high_prices": [120.5],
            "swing_low_prices": [100.0],
            "df": _frame(high=120.5),
        },
        "15M": {
            "bias": "BULLISH",
            "price": 119.0,
            "swing_high_prices": [120.0],
            "swing_low_prices": [110.0],
            "last_event": {
                "type": "BoS",
                "direction": "bullish",
                "price": 119.0,
                "index": 47,
            },
            "df": _frame(low=110.0, high=120.0),
        },
    }


def test_scan_all_institutional_decision_owns_final_status(monkeypatch):
    tf = _tf_results()

    monkeypatch.setattr(
        scanner,
        "scan_symbol",
        lambda active_key, symbol: (tf, {"1D": "test", "4H": "test", "1H": "test", "15M": "test"}),
    )
    monkeypatch.setattr(
        scanner,
        "score_setup_with_regime",
        lambda tf_results: (95, "BULLISH", {"regime": "TRENDING", "trend_alignment": "ALIGNED"}),
    )
    monkeypatch.setattr(
        scanner,
        "build_entry_plan",
        lambda tf_results, direction, regime_info, setup_score=None: {
            "setup_type": "MOMENTUM_CONTINUATION",
            "trade_type": "INTRADAY",
            "extension_ratio_pct": 0,
            "setup_score": setup_score,
        },
    )
    monkeypatch.setattr(
        scanner,
        "determine_execution_state",
        lambda plan, tf_results, direction: {
            "status": "READY_MARKET",
            "execution_type": "MARKET",
            "final_decision": "READY_MARKET",
            "preferred_entry": 119.0,
            "entry": 119.0,
            "invalidation": 99.0,
            "validated_targets": [{"price": 130.0, "r": 3.0}],
            "structural_rr": 3.0,
            "mechanical_rr": 3.0,
        },
    )
    monkeypatch.setattr(
        scanner,
        "update_setup_lifecycle",
        lambda symbol, plan, scan_cycle: {"lifecycle": "DEVELOPING", "send_alert": False, "reason": "test"},
    )
    monkeypatch.setattr(
        full_scan.derivatives,
        "monitor",
        lambda symbols: {"results": {}, "bulk_fetch_ok": True},
    )
    monkeypatch.setattr(full_scan, "_attach_market_intelligence", lambda qualifying: None)

    result = full_scan.scan_all("test", ["TEST-USDT"])

    assert len(result) == 1
    plan = result[0][5]

    # The legacy classifier deliberately reports READY_MARKET above.
    # The institutional engine must reassert its hard structural R:R gate.
    assert plan["status"] == "NO_TRADE"
    assert plan["final_decision"] == "NO_TRADE"
    assert plan["no_trade_code"] == "STRUCTURAL_RR_BELOW_2"
    assert plan["structural_rr"] < 2.0
