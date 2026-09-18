"""ATHENA institutional entry decision layer.

This module is deliberately outside smc_scanner.py. It consumes the existing
multi-timeframe analysis objects and produces a stricter, structure-first
execution decision without changing exchange/order, registry, monitoring, or
notification mechanics.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple
import math

MIN_STRUCTURAL_RR = 2.0
SCALP_CONFIDENCE_PENALTY = 30
MAX_LIMIT_DISTANCE_ATR = 1.5
RANGE_OUTER_PCT = 25.0
SWEEP_LOOKBACK = 8


def _f(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        x = float(value)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _df(tf_results: Dict[str, Any], tf: str):
    row = tf_results.get(tf) if isinstance(tf_results, dict) else None
    if not isinstance(row, dict):
        return None
    df = row.get("df")
    return df if df is not None and len(df) else None


def _price(tf_results: Dict[str, Any], tf: str) -> Optional[float]:
    row = tf_results.get(tf) if isinstance(tf_results, dict) else None
    return _f(row.get("price")) if isinstance(row, dict) else None


def _swings(tf_results: Dict[str, Any], tf: str, side: str) -> List[float]:
    row = tf_results.get(tf) if isinstance(tf_results, dict) else None
    if not isinstance(row, dict):
        return []
    key = "swing_high_prices" if side == "high" else "swing_low_prices"
    vals = []
    for value in row.get(key) or []:
        x = _f(value)
        if x is not None:
            vals.append(x)
    return sorted(set(vals))


def _atr(df, period: int = 14) -> Optional[float]:
    if df is None or len(df) < 2:
        return None
    try:
        prev = df["close"].shift(1)
        tr = __import__("pandas").concat(
            [df["high"] - df["low"],
             (df["high"] - prev).abs(),
             (df["low"] - prev).abs()],
            axis=1,
        ).max(axis=1)
        value = tr.rolling(period).mean().iloc[-1]
        return _f(value)
    except Exception:
        return None


def _buffer(tf_results: Dict[str, Any], base: float) -> float:
    atr = _atr(_df(tf_results, "4H")) or _atr(_df(tf_results, "1H")) or _atr(_df(tf_results, "15M"))
    return max(abs(base) * 0.0015, (atr or 0.0) * 0.20)


def _nearest_below(values: Iterable[float], price: float) -> Optional[float]:
    xs = [x for x in values if x < price]
    return max(xs) if xs else None


def _nearest_above(values: Iterable[float], price: float) -> Optional[float]:
    xs = [x for x in values if x > price]
    return min(xs) if xs else None


def _structural_stop(tf_results: Dict[str, Any], direction: str, price: float) -> Tuple[Optional[float], str, str]:
    """Return structural invalidation, source TF, and reason.

    4H is the primary multi-hour invalidation source. 1D is used when the
    4H frame has no usable structure on the invalid side. 1H is the final
    fallback for genuinely intraday structure.
    """
    if direction == "BULLISH":
        for tf in ("4H", "1D", "1H"):
            level = _nearest_below(_swings(tf_results, tf, "low"), price)
            if level is not None:
                stop = level - _buffer(tf_results, level)
                return stop, tf, f"below {tf} structural swing low at {level:.8g}"
    else:
        for tf in ("4H", "1D", "1H"):
            level = _nearest_above(_swings(tf_results, tf, "high"), price)
            if level is not None:
                stop = level + _buffer(tf_results, level)
                return stop, tf, f"above {tf} structural swing high at {level:.8g}"
    return None, "", "No reliable multi-timeframe structural invalidation was found."


def _range(tf_results: Dict[str, Any], tf: str = "4H") -> Optional[Tuple[float, float]]:
    df = _df(tf_results, tf)
    if df is None or len(df) < 24:
        return None
    try:
        w = df.tail(min(48, len(df)))
        lo, hi = float(w["low"].min()), float(w["high"].max())
        if hi <= lo:
            return None
        return lo, hi
    except Exception:
        return None


def _edge_sweep_reclaim(tf_results: Dict[str, Any], direction: str) -> Tuple[bool, str]:
    df = _df(tf_results, "4H")
    if df is None or len(df) < 40:
        return False, ""
    try:
        base = df.iloc[:-SWEEP_LOOKBACK].tail(32)
        recent = df.tail(SWEEP_LOOKBACK)
        lo, hi = float(base["low"].min()), float(base["high"].max())
        last = recent.iloc[-1]
        if direction == "BULLISH":
            swept = float(recent["low"].min()) < lo
            reclaimed = float(last["close"]) > lo and float(last["close"]) > float(last["open"])
            if swept and reclaimed:
                return True, "4H range-low liquidity sweep followed by bullish reclaim"
        else:
            swept = float(recent["high"].max()) > hi
            rejected = float(last["close"]) < hi and float(last["close"]) < float(last["open"])
            if swept and rejected:
                return True, "4H range-high liquidity sweep followed by bearish rejection"
    except Exception:
        pass
    return False, ""


def _location(tf_results: Dict[str, Any], direction: str, price: float) -> Dict[str, Any]:
    dr = _range(tf_results, "4H")
    if not dr:
        return {"state": "UNKNOWN", "score": 50, "position_pct": None, "reason": "No reliable 4H dealing range."}
    lo, hi = dr
    pct = max(0.0, min(100.0, (price - lo) / (hi - lo) * 100.0))
    if direction == "BULLISH":
        if pct <= 40:
            state, score = "DISCOUNT", 90
        elif pct <= 60:
            state, score = "EQUILIBRIUM", 65
        else:
            state, score = "PREMIUM", 25
    else:
        if pct >= 60:
            state, score = "PREMIUM", 90
        elif pct >= 40:
            state, score = "EQUILIBRIUM", 65
        else:
            state, score = "DISCOUNT", 25
    return {
        "state": state, "score": score, "position_pct": round(pct, 1),
        "low": lo, "high": hi, "reason": f"4H range position {pct:.1f}% ({state.lower()} for {direction.lower()})",
    }


def _htf_alignment(tf_results: Dict[str, Any], direction: str) -> Tuple[int, str, int]:
    wanted = "BULLISH" if direction == "BULLISH" else "BEARISH"
    votes = []
    for tf in ("1D", "4H", "1H"):
        row = tf_results.get(tf)
        votes.append((tf, str(row.get("bias", "RANGING")).upper()) if isinstance(row, dict) else (tf, "MISSING"))
    aligned = sum(v == wanted for _, v in votes)
    opposite = sum(v in ("BULLISH", "BEARISH") and v != wanted for _, v in votes)
    if aligned >= 2 and opposite == 0:
        return 95, "ALIGNED", aligned
    if aligned >= 2:
        return 70, "MIXED", aligned
    if aligned == 1 and opposite == 0:
        return 50, "WEAK", aligned
    return 25, "COUNTERTREND", aligned


def _liquidity_evidence(tf_results: Dict[str, Any], direction: str, price: float) -> Tuple[int, List[str]]:
    row = tf_results.get("15M") if isinstance(tf_results, dict) else None
    if not isinstance(row, dict):
        return 30, []
    wanted = "bullish" if direction == "BULLISH" else "bearish"
    score = 30
    reasons = []
    sweep = row.get("recent_sweep")
    event = row.get("last_event")
    if isinstance(sweep, dict) and str(sweep.get("direction", "")).lower() == wanted:
        score += 25
        reasons.append("recent aligned liquidity sweep")
    if isinstance(event, dict) and str(event.get("direction", "")).lower() == wanted:
        if event.get("type") in ("BoS", "CHoCH", "MSS"):
            score += 25
            reasons.append(f"15M {event.get('type')} confirms direction")
    return min(score, 100), reasons


def _setup_type(tf_results: Dict[str, Any], direction: str, existing: str = "") -> Tuple[str, List[str]]:
    sweep, reason = _edge_sweep_reclaim(tf_results, direction)
    if sweep:
        return (
            "ACCUMULATION_RANGE_LOW_RECLAIM" if direction == "BULLISH" else "DISTRIBUTION_RANGE_HIGH_REJECTION",
            [reason],
        )
    # Preserve the existing structural detector when it already found a valid type.
    if existing:
        return existing, []
    return "NO_TRADE", []


def _scalp_like(plan: Dict[str, Any], tf_results: Dict[str, Any], stop: float, price: float, targets: List[Dict[str, Any]]) -> bool:
    risk_pct = abs(price - stop) / price * 100.0 if price else 999.0
    max_r = max((float(t.get("r", 0) or 0) for t in targets), default=0.0)
    trade_type = str(plan.get("trade_type", "")).upper()
    return (
        trade_type == "SCALP"
        or (risk_pct < 0.6 and max_r < 1.5)
        or (
            str(tf_results.get("1D", {}).get("bias", "")).upper() == "RANGING"
            and str(tf_results.get("4H", {}).get("bias", "")).upper() == "RANGING"
            and str(plan.get("setup_type", "")).upper() in {"MOMENTUM_CONTINUATION", "BOS_CONTINUATION", "TREND_PULLBACK"}
            and risk_pct < 1.0
        )
    )


def _target_candidates(tf_results: Dict[str, Any], direction: str, price: float) -> List[Tuple[float, str, str]]:
    out: List[Tuple[float, str, str]] = []
    if direction == "BULLISH":
        for tf in ("1D", "4H", "1H"):
            for p in _swings(tf_results, tf, "high"):
                if p > price:
                    out.append((p, "external/structural liquidity", tf))
        for tf in ("1D", "4H", "1H"):
            row = tf_results.get(tf)
            for z in (row.get("bearish_zones") or []) if isinstance(row, dict) else []:
                mid = ((_f(z.get("low")) or 0.0) + (_f(z.get("high")) or 0.0)) / 2
                level = _f(z.get("low"))
                if level is not None and mid > price:
                    out.append((level, "supply zone", tf))
    else:
        for tf in ("1D", "4H", "1H"):
            for p in _swings(tf_results, tf, "low"):
                if p < price:
                    out.append((p, "external/structural liquidity", tf))
        for tf in ("1D", "4H", "1H"):
            row = tf_results.get(tf)
            for z in (row.get("bullish_zones") or []) if isinstance(row, dict) else []:
                mid = ((_f(z.get("low")) or 0) + (_f(z.get("high")) or 0)) / 2
                level = _f(z.get("high"))
                if level is not None and mid < price:
                    out.append((level, "demand zone", tf))
    # Deduplicate by ~0.1% and sort nearest first.
    out.sort(key=lambda x: x[0] if direction == "BULLISH" else -x[0])
    dedup: List[Tuple[float, str, str]] = []
    for item in out:
        if not any(abs(item[0] - old[0]) / max(abs(old[0]), 1e-12) < 0.001 for old in dedup):
            dedup.append(item)
    return dedup


def _targets(tf_results: Dict[str, Any], direction: str, price: float, stop: float) -> List[Dict[str, Any]]:
    risk = abs(price - stop)
    if risk <= 0:
        return []
    candidates = _target_candidates(tf_results, direction, price)
    result = []
    for level, reason, tf in candidates[:6]:
        r = abs(level - price) / risk
        result.append({"price": level, "r": r, "label": reason, "target_reason": f"{tf} {reason}", "timeframe_source": tf})
    return result[:3]


def _entry_reference(tf_results: Dict[str, Any], direction: str, price: float, setup_type: str) -> float:
    if setup_type == "ACCUMULATION_RANGE_LOW_RECLAIM":
        dr = _range(tf_results, "4H")
        if dr:
            return dr[0]
    if setup_type == "DISTRIBUTION_RANGE_HIGH_REJECTION":
        dr = _range(tf_results, "4H")
        if dr:
            return dr[1]
    row = tf_results.get("15M") if isinstance(tf_results, dict) else None
    event = row.get("last_event") if isinstance(row, dict) else None
    if isinstance(event, dict):
        event_price = _f(event.get("price"))
        if event_price is not None:
            return event_price
    return price


def _distance_atr(tf_results: Dict[str, Any], entry: float, price: float) -> Optional[float]:
    atr = _atr(_df(tf_results, "15M")) or _atr(_df(tf_results, "1H"))
    return abs(price - entry) / atr if atr and atr > 0 else None


def evaluate(plan: Dict[str, Any], tf_results: Dict[str, Any], direction: str) -> Dict[str, Any]:
    """Apply the 17-step institutional decision model to an existing plan."""
    price = _price(tf_results, "15M") or _f(plan.get("current_price"))
    if price is None:
        return {
            "status": "NO_TRADE", "execution_type": None,
            "final_decision": "NO_TRADE",
            "no_trade_code": "MISSING_CURRENT_PRICE",
            "no_trade_reason": "No current 15M price available.",
            "confidence": 0,
        }

    direction = str(direction).upper()
    existing_type = str(plan.get("setup_type") or "").upper()
    setup_type, setup_notes = _setup_type(tf_results, direction, existing_type)

    stop, stop_tf, stop_reason = _structural_stop(tf_results, direction, price)
    if stop is None:
        return {
            "status": "NO_TRADE", "execution_type": None,
            "final_decision": "NO_TRADE",
            "no_trade_code": "NO_STRUCTURAL_INVALIDATION",
            "no_trade_reason": stop_reason,
            "confidence": 0,
        }

    targets = _targets(tf_results, direction, price, stop)
    location = _location(tf_results, direction, price)
    alignment_score, alignment_state, aligned = _htf_alignment(tf_results, direction)
    liq_score, liq_reasons = _liquidity_evidence(tf_results, direction, price)

    range_pos = location.get("position_pct")
    edge_location = (
        range_pos is not None
        and ((direction == "BULLISH" and range_pos <= RANGE_OUTER_PCT)
             or (direction == "SHORT" and range_pos >= 100.0 - RANGE_OUTER_PCT)
             or (direction == "BEARISH" and range_pos >= 100.0 - RANGE_OUTER_PCT))
    )
    if direction == "BULLISH":
        location_edge = location.get("score", 50)
    else:
        location_edge = location.get("score", 50)

    primary = targets[0] if targets else None
    structural_rr = float(primary["r"]) if primary else 0.0

    # Hard invalidation gate is evaluated before any R:R or score decision.
    breached = (direction == "BULLISH" and price <= stop) or (direction == "BEARISH" and price >= stop)
    if breached:
        return {
            "status": "INVALID", "execution_type": None, "final_decision": "INVALID",
            "invalidation": stop, "invalidation_level": stop,
            "invalidation_timeframe": stop_tf,
            "invalidation_reason": f"{stop_reason}; thesis is structurally broken.",
            "structural_rr": structural_rr, "mechanical_rr": structural_rr,
            "confidence": 0,
            "setup_type": setup_type,
        }

    # Determine setup quality separately from entry quality.
    setup_quality = round(max(0, min(100,
        alignment_score * 0.40
        + liq_score * 0.20
        + (90 if setup_type != "NO_TRADE" else 0) * 0.20
        + location_edge * 0.20
    )))

    entry_ref = _entry_reference(tf_results, direction, price, setup_type)
    distance_atr = _distance_atr(tf_results, entry_ref, price)
    entry_room = min(100.0, max(0.0, (structural_rr / 2.0) * 100.0))
    extension = _f(plan.get("extension_ratio_pct"))
    if extension is None:
        extension = 0.0
    extension_score = max(0.0, min(100.0, 100.0 - extension * 0.75))
    entry_quality = round(max(0, min(100,
        extension_score * 0.50
        + entry_room * 0.30
        + location.get("score", 50) * 0.20
    )))

    scalp = _scalp_like(plan, tf_results, stop, price, targets)
    confidence = round(max(0, min(100,
        setup_quality * 0.45
        + entry_quality * 0.30
        + min(structural_rr, 4.0) / 4.0 * 100.0 * 0.15
        + alignment_score * 0.10
    )))
    if scalp:
        confidence = max(0, confidence - SCALP_CONFIDENCE_PENALTY)

    result = {
        "setup_type": setup_type,
        "setup_notes": setup_notes,
        "setup_quality": setup_quality,
        "entry_quality": entry_quality,
        "confidence": confidence,
        "scalp_like": scalp,
        "structural_rr": round(structural_rr, 3),
        "mechanical_rr": round(structural_rr, 3),
        "structural_target": primary["price"] if primary else None,
        "structural_target_type": primary["label"] if primary else "NONE",
        "structural_target_reason": primary["target_reason"] if primary else "No meaningful structural target beyond current price.",
        "validated_targets": targets,
        "invalidation": stop,
        "invalidation_level": stop,
        "invalidation_timeframe": stop_tf,
        "invalidation_reason": stop_reason,
        "current_price": price,
        "current_location": location.get("state"),
        "current_location_score": location.get("score"),
        "current_location_pct": location.get("position_pct"),
        "htf_alignment": alignment_state,
        "htf_alignment_score": alignment_score,
        "liquidity_score": liq_score,
        "liquidity_reasons": liq_reasons,
        "distance_to_entry_atr": round(distance_atr, 3) if distance_atr is not None else None,
        "preferred_entry": entry_ref,
        "entry_zone": (location.get("low"), location.get("high")) if location.get("low") is not None else None,
        "edge_location": edge_location,
        "structural_stop_source": stop_tf,
        "structural_stop_reason": stop_reason,
    }

    # Hard R:R gate: realistic structure must supply >= 1:2.
    if structural_rr < MIN_STRUCTURAL_RR:
        result.update({
            "status": "NO_TRADE", "execution_type": None, "final_decision": "NO_TRADE",
            "no_trade_code": "STRUCTURAL_RR_BELOW_2",
            "no_trade_reason": f"Nearest meaningful structural reward is only {structural_rr:.2f}R; minimum is 2.00R.",
        })
        return result

    if setup_type == "NO_TRADE" or setup_quality < 70:
        result.update({
            "status": "NO_TRADE", "execution_type": None, "final_decision": "NO_TRADE",
            "no_trade_code": "INSUFFICIENT_SETUP_QUALITY",
            "no_trade_reason": f"Setup quality {setup_quality}/100 does not establish a sufficient structural edge.",
        })
        return result

    # Strong opposing HTF structure requires a genuine reversal; 15M alone
    # cannot override it.
    if alignment_state == "COUNTERTREND" and setup_type not in {
        "ACCUMULATION_RANGE_LOW_RECLAIM", "DISTRIBUTION_RANGE_HIGH_REJECTION", "LIQUIDITY_SWEEP_REVERSAL"
    }:
        result.update({
            "status": "NO_TRADE", "execution_type": None, "final_decision": "NO_TRADE",
            "no_trade_code": "HTF_CONFLICT_WITHOUT_REVERSAL",
            "no_trade_reason": "Lower-timeframe setup conflicts with 1D/4H structure without a qualifying reversal.",
        })
        return result

    # Entry quality is a separate gate. A good thesis can legitimately wait.
    extended = extension is not None and extension > 100.0
    if extended or entry_quality < 60:
        result.update({
            "status": "WAIT_PULLBACK", "execution_type": None, "final_decision": "WAIT_PULLBACK",
            "required_confirmation": "Wait for price to return toward the structural execution location; do not chase.",
        })
        return result

    if distance_atr is not None and distance_atr > MAX_LIMIT_DISTANCE_ATR:
        result.update({
            "status": "WAIT_PULLBACK", "execution_type": None, "final_decision": "WAIT_PULLBACK",
            "required_confirmation": "Structural entry is too far from current price; wait for a realistic pullback.",
        })
        return result

    essentially_at_price = distance_atr is None or distance_atr <= 0.15
    if essentially_at_price and entry_quality >= 75:
        result.update({
            "status": "READY_MARKET", "execution_type": "MARKET",
            "final_decision": "READY_MARKET", "entry": price,
        })
    else:
        result.update({
            "status": "READY_LIMIT", "execution_type": "LIMIT",
            "final_decision": "READY_LIMIT", "entry": entry_ref,
        })

    return result
