"""ATHENA post-entry Trade Health engine.

This is a read-only thesis-management layer. It does not place orders or
modify the exchange position, SL, TP, registry ownership, or SMC decisions.

It evaluates a live position against its original thesis using:
- 4H / 1H / 15M structure and momentum;
- current structural invalidation;
- CoinGlass participation/liquidation/CVD evidence when available;
- existing derivatives context;
- current R and position location.

The engine deliberately uses "participation" rather than claiming that a
specific metric proves institutional intent.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Iterable, Optional, Tuple


STATES = ("HEALTHY", "DETERIORATING", "CRITICAL", "INVALIDATED", "REASSESS")
MATERIAL_PRICE_PCT = 0.75
MATERIAL_OI_PCT = 1.0
TRAIL_ATR_BUFFER = 0.50
MIN_TRAIL_ATR_DISTANCE = 1.00


def _f(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        x = float(value)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _direction(entry: Dict[str, Any]) -> str:
    value = str(entry.get("direction") or entry.get("side") or "").upper()
    if value in {"LONG", "BUY"}:
        return "BULLISH"
    if value in {"SHORT", "SELL"}:
        return "BEARISH"
    return value


def _df(tf_results: Dict[str, Any], tf: str):
    row = tf_results.get(tf) if isinstance(tf_results, dict) else None
    df = row.get("df") if isinstance(row, dict) else None
    return df if df is not None and len(df) else None


def _price(entry: Dict[str, Any], tf_results: Dict[str, Any]) -> Optional[float]:
    for source in (
        entry.get("exchange_mark_price"),
        entry.get("current_price"),
        (tf_results.get("15M") or {}).get("price"),
        (tf_results.get("1H") or {}).get("price"),
        (tf_results.get("4H") or {}).get("price"),
    ):
        value = _f(source)
        if value is not None:
            return value
    for tf in ("15M", "1H", "4H"):
        df = _df(tf_results, tf)
        if df is not None:
            value = _f(df["close"].iloc[-1])
            if value is not None:
                return value
    return None


def _atr(df, period=14) -> Optional[float]:
    if df is None or len(df) < 2:
        return None
    try:
        import pandas as pd
        prev = df["close"].shift(1)
        tr = pd.concat(
            [df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()],
            axis=1,
        ).max(axis=1)
        return _f(tr.rolling(period).mean().iloc[-1])
    except Exception:
        return None


def _swings(tf_results, tf, side):
    row = tf_results.get(tf) if isinstance(tf_results, dict) else None
    if not isinstance(row, dict):
        return []
    key = "swing_low_prices" if side == "low" else "swing_high_prices"
    out = []
    for value in row.get(key) or []:
        value = _f(value)
        if value is not None:
            out.append(value)
    return sorted(set(out))


def _last_close_change(tf_results, tf, bars=1) -> Optional[float]:
    df = _df(tf_results, tf)
    if df is None or len(df) <= bars:
        return None
    old = _f(df["close"].iloc[-1 - bars])
    new = _f(df["close"].iloc[-1])
    if old in (None, 0) or new is None:
        return None
    return (new - old) / abs(old) * 100.0


def _structural_status(entry, tf_results, direction, price):
    invalidation = _f(entry.get("original_invalidation") or entry.get("invalidation"))
    if invalidation is not None:
        breached = price <= invalidation if direction == "BULLISH" else price >= invalidation
        if breached:
            return "INVALIDATED", "Original thesis invalidation has been breached."

    # Require an HTF structure observation before calling the thesis invalid.
    htf_biases = []
    for tf in ("4H", "1H"):
        row = tf_results.get(tf)
        if isinstance(row, dict):
            htf_biases.append(str(row.get("bias", "")).upper())
    if htf_biases and all(b in {"BEARISH", "RANGING"} for b in htf_biases) and direction == "BULLISH":
        return "CONFLICT", "Both 4H and 1H no longer support the original bullish direction."
    if htf_biases and all(b in {"BULLISH", "RANGING"} for b in htf_biases) and direction == "BEARISH":
        return "CONFLICT", "Both 4H and 1H no longer support the original bearish direction."
    return "INTACT", "Original structural invalidation has not been breached."


def _participation(cg: Dict[str, Any], direction: str, price_change: Optional[float]) -> Dict[str, Any]:
    oi_hist = cg.get("oi_history") if isinstance(cg, dict) else None
    spot_cvd = cg.get("spot_cvd_history") if isinstance(cg, dict) else None
    futures_cvd = cg.get("futures_cvd_history") if isinstance(cg, dict) else None
    liq = cg.get("liquidation_history") if isinstance(cg, dict) else None
    taker = cg.get("spot_taker_history") if isinstance(cg, dict) else None

    def closes(rows, keys):
        if not isinstance(rows, list):
            return None
        vals = [_f(row.get(key)) for row in rows if isinstance(row, dict) for key in keys]
        return vals[-1] if vals else None

    oi_change = None
    if isinstance(oi_hist, list) and len(oi_hist) >= 2:
        old = _f(oi_hist[0].get("close"))
        new = _f(oi_hist[-1].get("close"))
        if old not in (None, 0) and new is not None:
            oi_change = (new - old) / abs(old) * 100.0

    def delta(rows, key):
        if not isinstance(rows, list) or len(rows) < 2:
            return None
        a, b = _f(rows[0].get(key)), _f(rows[-1].get(key))
        return None if a is None or b is None else b - a

    spot_cvd_delta = delta(spot_cvd, "cum_vol_delta")
    futures_cvd_delta = delta(futures_cvd, "cum_vol_delta")

    spot_net = None
    if isinstance(taker, list) and taker:
        buy = sum((_f(x.get("aggregated_buy_volume_usd")) or 0) for x in taker if isinstance(x, dict))
        sell = sum((_f(x.get("aggregated_sell_volume_usd")) or 0) for x in taker if isinstance(x, dict))
        total = buy + sell
        if total > 0:
            spot_net = (buy - sell) / total

    long_liq = short_liq = 0.0
    if isinstance(liq, list):
        for row in liq:
            if not isinstance(row, dict):
                continue
            long_liq += _f(
                row.get("aggregated_long_liquidation_usd")
                or row.get("long_liquidation_usd")
                or row.get("long_liquidation")
            ) or 0.0
            short_liq += _f(
                row.get("aggregated_short_liquidation_usd")
                or row.get("short_liquidation_usd")
                or row.get("short_liquidation")
            ) or 0.0

    evidence = []
    state = "MIXED"
    if price_change is not None and oi_change is not None:
        price_up = price_change >= MATERIAL_PRICE_PCT
        price_down = price_change <= -MATERIAL_PRICE_PCT
        oi_up = oi_change >= MATERIAL_OI_PCT
        oi_down = oi_change <= -MATERIAL_OI_PCT
        spot_buying = spot_cvd_delta is not None and spot_cvd_delta > 0
        spot_selling = spot_cvd_delta is not None and spot_cvd_delta < 0
        if price_up and oi_up and spot_buying:
            state = "FRESH_PARTICIPATION"
            evidence.append("price, OI and spot CVD are aligned higher")
        elif price_up and oi_down and short_liq > long_liq:
            state = "LIQUIDATION_DRIVEN_UP"
            evidence.append("price rose while OI fell and short liquidations dominated")
        elif price_down and oi_down and long_liq > short_liq:
            state = "LIQUIDATION_DRIVEN_DOWN"
            evidence.append("price fell while OI fell and long liquidations dominated")
        elif price_down and oi_up:
            state = "NEW_SHORT_RISK"
            evidence.append("price fell while OI increased")
        elif price_down and oi_down and spot_buying:
            state = "ABSORPTION_POSSIBLE"
            evidence.append("price fell/OI fell while spot CVD remained positive")
        elif price_up and oi_down:
            state = "SHORT_COVERING"
            evidence.append("price rose while OI fell")
        elif price_up and oi_up:
            state = "FUTURES_PARTICIPATION"
            evidence.append("price and OI increased, but spot confirmation is incomplete")
        elif price_down and spot_selling:
            state = "SELLING_PRESSURE"
            evidence.append("price and spot CVD weakened")

    if futures_cvd_delta is not None:
        evidence.append(f"futures CVD {'positive' if futures_cvd_delta > 0 else 'negative'}")
    if spot_net is not None:
        evidence.append(f"spot taker net={spot_net:+.1%}")

    return {
        "state": state,
        "oi_change_pct": oi_change,
        "spot_cvd_delta": spot_cvd_delta,
        "futures_cvd_delta": futures_cvd_delta,
        "long_liquidations": long_liq,
        "short_liquidations": short_liq,
        "spot_taker_net": spot_net,
        "reason": "; ".join(evidence) if evidence else "Insufficient CoinGlass participation evidence.",
        "available": bool(cg.get("enabled") and cg.get("availability") in {"OK", "PARTIAL"}) if isinstance(cg, dict) else False,
    }


def _trail_candidate(entry, tf_results, direction, price, health_state, participation_state):
    candidates = []
    for tf, multiplier in (("4H", 0.75), ("1H", TRAIL_ATR_BUFFER)):
        df = _df(tf_results, tf)
        atr = _atr(df)
        if atr is None:
            continue
        side = "low" if direction == "BULLISH" else "high"
        swings = _swings(tf_results, tf, side)
        if direction == "BULLISH":
            levels = [x for x in swings if x < price]
            if levels:
                candidate = max(levels) - multiplier * atr
                candidates.append((tf, candidate, atr))
        else:
            levels = [x for x in swings if x > price]
            if levels:
                candidate = min(levels) + multiplier * atr
                candidates.append((tf, candidate, atr))

    if not candidates:
        return None

    # A healthy trend may use the 1H structural trail. A deteriorating trade
    # may tighten, but never tighter than one 15M ATR from current price.
    preferred = next((x for x in candidates if x[0] == "1H"), candidates[0])
    candidate = preferred[1]
    atr15 = _atr(_df(tf_results, "15M"))
    min_gap = (atr15 or preferred[2]) * MIN_TRAIL_ATR_DISTANCE
    if direction == "BULLISH":
        candidate = min(candidate, price - min_gap)
        existing = _f(entry.get("trail_stop") or entry.get("stop_loss") or entry.get("invalidation"))
        if existing is not None:
            candidate = max(candidate, existing)
    else:
        candidate = max(candidate, price + min_gap)
        existing = _f(entry.get("trail_stop") or entry.get("stop_loss") or entry.get("invalidation"))
        if existing is not None:
            candidate = min(candidate, existing)

    return {
        "candidate": candidate,
        "timeframe": preferred[0],
        "reason": (
            f"{preferred[0]} structural swing trail with volatility room; "
            f"participation={participation_state}, health={health_state}"
        ),
    }


def evaluate(entry: Dict[str, Any], tf_results: Dict[str, Any],
             derivatives_context: Optional[Dict[str, Any]] = None,
             coinglass_context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    direction = _direction(entry)
    price = _price(entry, tf_results)
    if direction not in {"BULLISH", "BEARISH"} or price is None:
        return {
            "trade_health_state": "REASSESS",
            "health_action": "REASSESS",
            "reason": "Insufficient live price/direction context for post-entry thesis evaluation.",
            "active": False,
        }

    structural, structural_reason = _structural_status(entry, tf_results, direction, price)
    price_change = _last_close_change(tf_results, "1H", 1)
    participation = _participation(coinglass_context or {}, direction, price_change)

    deriv_state = str((derivatives_context or {}).get("state", "NEUTRAL")).upper()
    smc_state = str(entry.get("reversal_state", "STABLE")).upper()
    momentum_15m = _last_close_change(tf_results, "15M", 2)
    momentum_1h = _last_close_change(tf_results, "1H", 2)

    if structural == "INVALIDATED":
        state = "INVALIDATED"
        action = "EXIT"
    else:
        deterioration = 0
        critical = 0
        reasons = [structural_reason]

        if structural == "CONFLICT":
            critical += 2
            reasons.append("Higher-timeframe direction conflicts with the original thesis.")
        if smc_state == "CONFIRMED_REVERSAL":
            critical += 3
            reasons.append("Existing SMC diagnostic reports confirmed reversal.")
        elif smc_state == "MOMENTUM_DETERIORATION":
            deterioration += 1
            reasons.append("Existing SMC diagnostic reports momentum deterioration.")

        if deriv_state in {"LONG_TRAP_RISK", "SHORT_TRAP_RISK", "LIQUIDATION_EVENT", "REVERSAL_RISK"}:
            deterioration += 1
            reasons.append(f"Derivatives state={deriv_state}.")
        if participation["state"] in {"NEW_SHORT_RISK", "SELLING_PRESSURE"} and direction == "BULLISH":
            deterioration += 2
            reasons.append(participation["reason"])
        if participation["state"] == "LIQUIDATION_DRIVEN_UP" and direction == "BULLISH":
            deterioration += 1
            reasons.append("Upside expansion is increasingly liquidation/covering-driven rather than confirmed fresh participation.")
        if participation["state"] == "LIQUIDATION_DRIVEN_DOWN" and direction == "BULLISH":
            # A long-side liquidation reset is not automatically bearish when
            # structure survives; it is a watch condition, not an exit.
            reasons.append("Downside move is accompanied by falling OI and long liquidations; possible leveraged reset while structure remains intact.")
        if momentum_15m is not None and momentum_15m < -MATERIAL_PRICE_PCT and momentum_1h is not None and momentum_1h < -MATERIAL_PRICE_PCT:
            deterioration += 1
            reasons.append("15M and 1H momentum are both weakening.")

        if critical >= 3 or deterioration >= 4:
            state, action = "CRITICAL", "PROTECT"
        elif critical >= 2 or deterioration >= 2:
            state, action = "DETERIORATING", "TAKE_PARTIALS_OR_TIGHTEN"
        else:
            state, action = "HEALTHY", "HOLD_TRAIL"

        reason = " ".join(reasons)

    current_r = _f(entry.get("current_r"))
    max_r = _f(entry.get("max_r"))
    trail = _trail_candidate(entry, tf_results, direction, price, state, participation["state"])

    if state == "HEALTHY" and participation["state"] in {"MIXED", "SHORT_COVERING"}:
        # No derivatives confirmation is not a reason to manufacture certainty.
        if not participation["available"]:
            state = "REASSESS"
            action = "REASSESS"
            reason += " CoinGlass participation data is unavailable; no strong health conclusion is promoted."
    if state == "DETERIORATING" and participation["state"] == "FRESH_PARTICIPATION":
        # Independent participation confirmation can downgrade a weak signal.
        state = "HEALTHY"
        action = "HOLD_TRAIL"
        reason += " Fresh participation confirmation offsets the initial deterioration flag."

    return {
        "active": True,
        "trade_health_state": state,
        "health_action": action,
        "reason": reason,
        "current_price": price,
        "current_r": current_r,
        "max_r": max_r,
        "structural_status": structural,
        "participation": participation,
        "momentum_15m_pct": momentum_15m,
        "momentum_1h_pct": momentum_1h,
        "trail_guidance": trail,
    }


def apply(entry: Dict[str, Any], snapshot: Dict[str, Any]) -> bool:
    previous = entry.get("trade_health_state")
    current = snapshot.get("trade_health_state")
    if not current:
        return False
    entry["trade_health_previous_state"] = previous
    entry["trade_health_state"] = current
    entry["trade_health_action"] = snapshot.get("health_action")
    entry["trade_health_reason"] = snapshot.get("reason")
    entry["trade_health_snapshot"] = snapshot
    entry["trade_health_updated_at"] = snapshot.get("updated_at")
    return previous is not None and previous != current


__all__ = ["STATES", "evaluate", "apply"]
