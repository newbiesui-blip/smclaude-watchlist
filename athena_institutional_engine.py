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
PRIMARY_MACRO_TARGET_MIN_RR = 2.5
SCALP_CONFIDENCE_PENALTY = 30
MAX_LIMIT_DISTANCE_ATR = 1.5
RANGE_OUTER_PCT = 25.0
SWEEP_LOOKBACK = 8
SWEEP_WINDOW_CANDLES = 4
SWEEP_MAX_RECLAIM_DELAY = 3
MSS_STOP_ATR_BUFFER = 0.5
RANGE_BREAKOUT_EXCLUSION = 8


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


def _event_type(event: Any) -> str:
    return str((event or {}).get("type", "")).upper().replace(" ", "")


def _mss_event_candidates(row: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract explicit MSS events without inventing structure from price alone."""
    if not isinstance(row, dict):
        return []
    events: List[Dict[str, Any]] = []
    for key in ("mss_event", "last_mss", "mss", "last_event"):
        value = row.get(key)
        if isinstance(value, dict) and _event_type(value) == "MSS":
            events.append(value)
    raw = row.get("events")
    if isinstance(raw, list):
        events.extend(v for v in raw if isinstance(v, dict) and _event_type(v) == "MSS")
    elif isinstance(raw, dict):
        events.extend(v for v in raw.values() if isinstance(v, dict) and _event_type(v) == "MSS")
    return events


def _event_candle(df, event: Dict[str, Any]):
    if df is None or not isinstance(event, dict):
        return None
    for key in ("candle_index", "bar_index", "index"):
        idx = event.get(key)
        try:
            if idx is not None:
                i = int(idx)
                if 0 <= i < len(df):
                    return df.iloc[i]
        except (TypeError, ValueError):
            pass
    event_time = event.get("timestamp", event.get("time"))
    if event_time is not None:
        try:
            matches = df.index == event_time
            if matches.any():
                return df.loc[matches].iloc[-1]
        except Exception:
            pass
    return None


def _mss_structural_stop(
    tf_results: Dict[str, Any], direction: str
) -> Tuple[Optional[float], str, str]:
    """Use the MSS displacement candle body plus exactly 0.5 ATR."""
    for tf in ("4H", "1H", "1D"):
        row = tf_results.get(tf)
        if not isinstance(row, dict):
            continue
        df = _df(tf_results, tf)
        candidates = _mss_event_candidates(row)
        if not candidates:
            continue
        # Prefer the earliest explicitly supplied MSS: this is the initial
        # displacement milestone for the current thesis.
        candidates = sorted(
            candidates,
            key=lambda e: (
                e.get("candle_index", e.get("bar_index", e.get("index", 10**9)))
                if isinstance(e.get("candle_index", e.get("bar_index", e.get("index", 10**9))), (int, float))
                else 10**9
            ),
        )
        for event in candidates:
            candle = _event_candle(df, event)
            atr = _atr(df) or _atr(_df(tf_results, "4H")) or _atr(_df(tf_results, "1H"))
            if candle is None or not atr or atr <= 0:
                continue
            try:
                if direction == "BULLISH":
                    body_floor = min(float(candle["open"]), float(candle["close"]))
                    stop = body_floor - (MSS_STOP_ATR_BUFFER * atr)
                    return stop, tf, (
                        f"below initial {tf} MSS displacement body floor at {body_floor:.8g} "
                        f"with {MSS_STOP_ATR_BUFFER:.1f} ATR buffer"
                    )
                body_ceiling = max(float(candle["open"]), float(candle["close"]))
                stop = body_ceiling + (MSS_STOP_ATR_BUFFER * atr)
                return stop, tf, (
                    f"above initial {tf} MSS displacement body ceiling at {body_ceiling:.8g} "
                    f"with {MSS_STOP_ATR_BUFFER:.1f} ATR buffer"
                )
            except (TypeError, ValueError, KeyError):
                continue
    return None, "", ""


def _structural_stop(tf_results: Dict[str, Any], direction: str, price: float) -> Tuple[Optional[float], str, str]:
    """Return the thesis invalidation using the MSS milestone first.

    Explicit initial MSS displacement is authoritative. If the upstream SMC
    payload does not expose an MSS event, retain a conservative structural-swing
    fallback so legacy setups do not silently lose invalidation protection.
    """
    mss_stop, mss_tf, mss_reason = _mss_structural_stop(tf_results, direction)
    if mss_stop is not None:
        return mss_stop, mss_tf, mss_reason

    atr = _atr(_df(tf_results, "4H")) or _atr(_df(tf_results, "1H")) or _atr(_df(tf_results, "15M"))
    if direction == "BULLISH":
        for tf in ("4H", "1D", "1H"):
            level = _nearest_below(_swings(tf_results, tf, "low"), price)
            if level is not None:
                stop = level - (MSS_STOP_ATR_BUFFER * atr if atr else _buffer(tf_results, level))
                return stop, tf, f"below {tf} structural swing low at {level:.8g} with 0.5 ATR buffer"
    else:
        for tf in ("4H", "1D", "1H"):
            level = _nearest_above(_swings(tf_results, tf, "high"), price)
            if level is not None:
                stop = level + (MSS_STOP_ATR_BUFFER * atr if atr else _buffer(tf_results, level))
                return stop, tf, f"above {tf} structural swing high at {level:.8g} with 0.5 ATR buffer"
    return None, "", "No reliable multi-timeframe structural invalidation was found."

def _spot_cvd_series(tf_results: Dict[str, Any]) -> List[float]:
    """Read an upstream Spot-CVD series when one is explicitly supplied."""
    keys = ("spot_cvd", "spot_cvd_values", "spot_cvd_series", "cvd", "cvd_values")
    for tf in ("4H", "1H", "15M"):
        row = tf_results.get(tf)
        if not isinstance(row, dict):
            continue
        for key in keys:
            value = row.get(key)
            if isinstance(value, dict):
                value = value.get("values", value.get("series"))
            if isinstance(value, (list, tuple)):
                vals = [_f(v) for v in value]
                vals = [v for v in vals if v is not None]
                if len(vals) >= 5:
                    return vals
    return []


def _spot_cvd_expansion_verified(tf_results: Dict[str, Any], direction: str) -> bool:
    for tf in ("4H", "1H", "15M"):
        row = tf_results.get(tf)
        if not isinstance(row, dict):
            continue
        for key in ("spot_cvd_expansion_verified", "cvd_expansion_verified", "spot_cvd_vertical_shift"):
            if row.get(key) is True:
                return True
    values = _spot_cvd_series(tf_results)
    if len(values) < 5:
        return False
    deltas = [values[i] - values[i - 1] for i in range(1, len(values))]
    recent = deltas[-1]
    baseline = deltas[:-1]
    median_abs = sorted(abs(x) for x in baseline)[len(baseline) // 2]
    if median_abs <= 0:
        return False
    aligned = recent > 0 if direction == "BULLISH" else recent < 0
    return aligned and abs(recent) >= 2.5 * median_abs


def _macro_foundation(tf_results: Dict[str, Any], direction: str, price: float) -> Optional[float]:
    keys = (
        "macro_higher_low", "macro_higher_high", "higher_low_foundation",
        "macro_foundation", "structural_foundation",
    )
    for tf in ("4H", "1D"):
        row = tf_results.get(tf)
        if not isinstance(row, dict):
            continue
        for key in keys:
            value = _f(row.get(key))
            if value is not None:
                if direction == "BULLISH" and value < price:
                    return value
                if direction == "BEARISH" and value > price:
                    return value
    side = "low" if direction == "BULLISH" else "high"
    for tf in ("4H", "1D"):
        values = _swings(tf_results, tf, side)
        level = _nearest_below(values, price) if direction == "BULLISH" else _nearest_above(values, price)
        if level is not None:
            return level
    return None


def _dynamic_structural_range(tf_results: Dict[str, Any], direction: str, price: float) -> Optional[Tuple[float, float]]:
    if not _spot_cvd_expansion_verified(tf_results, direction):
        return None
    foundation = _macro_foundation(tf_results, direction, price)
    df = _df(tf_results, "4H")
    if foundation is None or df is None or len(df) < 8:
        return None
    try:
        if direction == "BULLISH":
            candidates = [i for i, v in enumerate(df["low"].tolist()) if float(v) <= foundation * 1.001]
            start = candidates[-1] if candidates else max(0, len(df) - 48)
            lo = float(foundation)
            hi = float(df.iloc[start:]["high"].max())
        else:
            candidates = [i for i, v in enumerate(df["high"].tolist()) if float(v) >= foundation * 0.999]
            start = candidates[-1] if candidates else max(0, len(df) - 48)
            hi = float(foundation)
            lo = float(df.iloc[start:]["low"].min())
        if hi > lo:
            return lo, hi
    except Exception:
        pass
    return None


def _range(
    tf_results: Dict[str, Any], tf: str = "4H",
    direction: Optional[str] = None, price: Optional[float] = None
) -> Optional[Tuple[float, float]]:
    df = _df(tf_results, tf)
    if df is None or len(df) < 24:
        return None
    try:
        if direction is not None and tf == "4H":
            current = price or _price(tf_results, "15M") or _price(tf_results, "1H") or float(df["close"].iloc[-1])
            dynamic = _dynamic_structural_range(tf_results, direction, current)
            if dynamic:
                return dynamic
        # Fixed dealing range excludes the latest execution cushion so a fresh
        # breakout/sweep cannot redefine the boundary that it is being tested against.
        usable = len(df) - RANGE_BREAKOUT_EXCLUSION
        if usable < 8:
            return None
        w = df.iloc[:usable].tail(min(48, usable))
        lo, hi = float(w["low"].min()), float(w["high"].max())
        if hi <= lo:
            return None
        return lo, hi
    except Exception:
        return None


def _defined_dealing_range(tf_results: Dict[str, Any], direction: str, price: float) -> Optional[Tuple[float, float]]:
    for tf in ("4H", "1H", "15M"):
        row = tf_results.get(tf)
        if not isinstance(row, dict):
            continue
        lo, hi = _f(row.get("dealing_range_low")), _f(row.get("dealing_range_high"))
        if lo is not None and hi is not None and hi > lo:
            return lo, hi

    dynamic = _dynamic_structural_range(tf_results, direction, price)
    if dynamic:
        return dynamic

    df = _df(tf_results, "4H")
    if df is None or len(df) < 16:
        return None
    try:
        w = df.iloc[:-RANGE_BREAKOUT_EXCLUSION].tail(min(48, len(df) - RANGE_BREAKOUT_EXCLUSION))
        lo, hi = float(w["low"].min()), float(w["high"].max())
        return (lo, hi) if hi > lo else None
    except Exception:
        return None


def _sweep_matrix(tf_results: Dict[str, Any], direction: str) -> Dict[str, Any]:
    """Detect wick sweeps across a rolling four-candle execution window.

    A sweep remains actionable through the sweep candle plus three subsequent
    candles. A close back inside the cached dealing range validates reclaim.
    """
    price = _price(tf_results, "15M") or _price(tf_results, "1H")
    if price is None:
        return {}
    dealing = _defined_dealing_range(tf_results, direction, price)
    if not dealing:
        return {}
    lo, hi = dealing
    boundary = lo if direction == "BULLISH" else hi
    candidates = []
    for tf in ("15M", "1H", "4H"):
        df = _df(tf_results, tf)
        if df is None or len(df) < SWEEP_WINDOW_CANDLES:
            continue
        start = max(0, len(df) - SWEEP_WINDOW_CANDLES)
        for i in range(len(df) - 1, start - 1, -1):
            try:
                violated = (
                    float(df["low"].iloc[i]) < boundary
                    if direction == "BULLISH"
                    else float(df["high"].iloc[i]) > boundary
                )
            except Exception:
                continue
            if not violated:
                continue
            age = len(df) - 1 - i
            reclaim_idx = None
            for j in range(i, min(len(df), i + SWEEP_MAX_RECLAIM_DELAY + 1)):
                try:
                    reclaimed = (
                        float(df["close"].iloc[j]) > boundary
                        if direction == "BULLISH"
                        else float(df["close"].iloc[j]) < boundary
                    )
                except Exception:
                    reclaimed = False
                if reclaimed:
                    reclaim_idx = j
                    break
            candidates.append({
                "timeframe": tf,
                "index": i,
                "age_candles": age,
                "boundary": boundary,
                "extreme": float(df["low"].iloc[i] if direction == "BULLISH" else df["high"].iloc[i]),
                "reclaimed": reclaim_idx is not None,
                "reclaim_index": reclaim_idx,
                "expired": reclaim_idx is None and age > SWEEP_MAX_RECLAIM_DELAY,
            })
    if not candidates:
        return {}
    # The most recent aligned sweep controls the state.
    return candidates[0]


def _range_sweep_state(tf_results: Dict[str, Any], direction: str) -> Dict[str, Any]:
    info = _sweep_matrix(tf_results, direction)
    if not info:
        return {"state": "NONE"}
    if info.get("reclaimed"):
        return {**info, "state": "RECLAIMED"}
    if info.get("expired"):
        return {**info, "state": "EXPIRED"}
    return {**info, "state": "PENDING_RECLAIM"}


def _edge_sweep_reclaim(tf_results: Dict[str, Any], direction: str) -> Tuple[bool, str]:
    info = _range_sweep_state(tf_results, direction)
    if info.get("state") == "RECLAIMED":
        return True, (
            f"{info['timeframe']} range-{ 'low liquidity sweep' if direction == 'BULLISH' else 'high liquidity sweep' } "
            f"reclaimed within the four-candle validation window"
        )
    return False, ""


def _sweep_extreme(tf_results: Dict[str, Any], direction: str) -> Optional[float]:
    info = _range_sweep_state(tf_results, direction)
    return _f(info.get("extreme")) if info.get("state") == "RECLAIMED" else None


def _pre_sweep_range(tf_results: Dict[str, Any], direction: str) -> Optional[Tuple[float, float]]:
    info = _sweep_matrix(tf_results, direction)
    if info and info.get("boundary") is not None:
        boundary = float(info["boundary"])
        if direction == "BULLISH":
            return boundary, boundary
        return boundary, boundary
    return None


def _location(tf_results: Dict[str, Any], direction: str, price: float) -> Dict[str, Any]:
    dr = _range(tf_results, "4H", direction=direction, price=price)
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
    score = 30
    reasons: List[str] = []
    sweep = _range_sweep_state(tf_results, direction)
    if sweep.get("state") == "RECLAIMED":
        score += 35
        reasons.append(
            f"{sweep.get('timeframe')} liquidity sweep reclaimed within four candles"
        )
    elif sweep.get("state") == "PENDING_RECLAIM":
        score += 10
        reasons.append(
            f"{sweep.get('timeframe')} liquidity sweep is awaiting reclaim "
            f"(age {sweep.get('age_candles', 0)} candle(s))"
        )

    if isinstance(row, dict):
        event = row.get("last_event")
        wanted = "bullish" if direction == "BULLISH" else "bearish"
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
    if not candidates:
        return []

    # T1 is the nearest meaningful structural shelf. T2 is the primary macro
    # thesis milestone: prefer the highest-timeframe structural target beyond T1.
    t1_level, t1_reason, t1_tf = candidates[0]
    t1 = {
        "price": t1_level,
        "r": abs(t1_level - price) / risk,
        "label": t1_reason,
        "target_reason": f"{t1_tf} {t1_reason}",
        "timeframe_source": t1_tf,
        "tier": "T1_TRAILING_FLOOR",
    }

    macro = [c for c in candidates[1:] if c[0] != t1_level]
    tf_rank = {"1D": 3, "4H": 2, "1H": 1}
    macro.sort(key=lambda c: (tf_rank.get(c[2], 0), abs(c[0] - price)), reverse=True)
    selected = [t1]
    if macro:
        level, reason, tf = macro[0]
        selected.append({
            "price": level,
            "r": abs(level - price) / risk,
            "label": reason,
            "target_reason": f"{tf} {reason}",
            "timeframe_source": tf,
            "tier": "T2_PRIMARY_MACRO",
        })

    # Preserve a third structural target when available, while keeping T1/T2
    # explicit so the two-tier gate cannot be hidden by target reordering.
    for level, reason, tf in candidates[1:]:
        if any(abs(level - x["price"]) / max(abs(x["price"]), 1e-12) < 0.001 for x in selected):
            continue
        selected.append({
            "price": level,
            "r": abs(level - price) / risk,
            "label": reason,
            "target_reason": f"{tf} {reason}",
            "timeframe_source": tf,
            "tier": "T3_STRUCTURAL",
        })
        if len(selected) == 3:
            break
    return selected



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
    sweep_state = _range_sweep_state(tf_results, direction)
    if sweep_state.get("state") == "EXPIRED":
        return {
            "status": "INVALID", "execution_type": None, "final_decision": "INVALID",
            "no_trade_code": "SWEEP_RECLAIM_EXPIRED",
            "no_trade_reason": (
                f"{sweep_state.get('timeframe')} sweep at candle {sweep_state.get('index')} "
                f"was not reclaimed within {SWEEP_MAX_RECLAIM_DELAY} execution candles."
            ),
            "invalidation_reason": "Liquidity sweep expired into structural breakdown.",
            "sweep_state": sweep_state,
            "confidence": 0,
        }
    setup_type, setup_notes = _setup_type(tf_results, direction, existing_type)

    stop, stop_tf, stop_reason = _structural_stop(tf_results, direction, price)
    if setup_type in {"ACCUMULATION_RANGE_LOW_RECLAIM", "DISTRIBUTION_RANGE_HIGH_REJECTION"}:
        sweep_extreme = _sweep_extreme(tf_results, direction)
        if sweep_extreme is not None:
            stop = (sweep_extreme - _buffer(tf_results, sweep_extreme)
                    if direction == "BULLISH"
                    else sweep_extreme + _buffer(tf_results, sweep_extreme))
            stop_tf = "4H"
            stop_reason = (
                f"beyond the 4H swept {'range low' if direction == 'BULLISH' else 'range high'} "
                f"at {sweep_extreme:.8g}"
            )
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

    t1 = targets[0] if targets else None
    t2 = targets[1] if len(targets) > 1 else None
    t1_rr = float(t1["r"]) if t1 else 0.0
    t2_rr = float(t2["r"]) if t2 else 0.0
    two_tier_target_gate = bool(
        t1 is not None
        and t1_rr < MIN_STRUCTURAL_RR
        and t2 is not None
        and t2_rr >= PRIMARY_MACRO_TARGET_MIN_RR
    )
    target_gate_passed = bool(
        t1 is not None
        and (
            t1_rr >= MIN_STRUCTURAL_RR
            or two_tier_target_gate
        )
    )
    structural_rr = t2_rr if two_tier_target_gate else t1_rr

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
    pre_range = _pre_sweep_range(tf_results, direction) if setup_type in {
        "ACCUMULATION_RANGE_LOW_RECLAIM", "DISTRIBUTION_RANGE_HIGH_REJECTION"
    } else None
    if pre_range:
        entry_ref = pre_range[0] if direction == "BULLISH" else pre_range[1]
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
        "structural_target": (t2["price"] if two_tier_target_gate and t2 else (t1["price"] if t1 else None)),
        "structural_target_type": (
            "T2_PRIMARY_MACRO" if two_tier_target_gate and t2 else (t1["label"] if t1 else "NONE")
        ),
        "structural_target_reason": (
            t2["target_reason"] if two_tier_target_gate and t2
            else (t1["target_reason"] if t1 else "No meaningful structural target beyond current price.")
        ),
        "nearest_target_rr": round(t1_rr, 3),
        "primary_macro_target_rr": round(t2_rr, 3),
        "target_gate": {
            "gate_a_t1_rr": round(t1_rr, 3),
            "gate_a_passed": t1_rr >= MIN_STRUCTURAL_RR if t1 else False,
            "gate_b_two_tier_active": two_tier_target_gate,
            "t2_min_rr": PRIMARY_MACRO_TARGET_MIN_RR,
            "passed": target_gate_passed,
        },
        "position_management": {
            "mode": "TWO_TIER" if two_tier_target_gate else "STANDARD",
            "scale_out_pct": 50 if two_tier_target_gate else 0,
            "scale_out_target": t1["price"] if two_tier_target_gate and t1 else None,
            "runner_pct": 50 if two_tier_target_gate else 100,
            "runner_target": t2["price"] if two_tier_target_gate and t2 else (t1["price"] if t1 else None),
            "post_t1_stop": "BREAKEVEN" if two_tier_target_gate else None,
        },
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
        "sweep_state": sweep_state,
        "distance_to_entry_atr": round(distance_atr, 3) if distance_atr is not None else None,
        "preferred_entry": entry_ref,
        "entry_zone": (location.get("low"), location.get("high")) if location.get("low") is not None else None,
        "edge_location": edge_location,
        "structural_stop_source": stop_tf,
        "structural_stop_reason": stop_reason,
        "zone_low": entry_ref,
        "zone_high": entry_ref,
        "zone_label": "4H range-edge structural level" if setup_type in {
            "ACCUMULATION_RANGE_LOW_RECLAIM", "DISTRIBUTION_RANGE_HIGH_REJECTION"
        } else "structural execution level",
    }

    # Two-tier target validation:
    # Gate A: T1 may be below 2R only when T2 is a genuine macro milestone >= 2.5R.
    # Gate B: in that case the position-management contract is 50% at T1,
    # then the remaining 50% runs toward T2 with the stop eligible for breakeven.
    # Otherwise, if both T1 and T2 fail 1:2 (or the required T2 floor is absent),
    # the setup is not executable.
    if not target_gate_passed:
        result.update({
            "status": "NO_TRADE", "execution_type": None, "final_decision": "NO_TRADE",
            "no_trade_code": "STRUCTURAL_RR_BELOW_2",
            "no_trade_reason": (
                f"T1={t1_rr:.2f}R and T2={t2_rr:.2f}R do not satisfy the two-tier "
                f"target gate (T1 >= {MIN_STRUCTURAL_RR:.2f}R, or T2 >= {PRIMARY_MACRO_TARGET_MIN_RR:.2f}R "
                f"when T1 is below 2R)."
            ),
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

    if distance_atr is None and abs(entry_ref - price) > 1e-12:
        result.update({
            "status": "WAIT_PULLBACK", "execution_type": None, "final_decision": "WAIT_PULLBACK",
            "required_confirmation": "Execution distance cannot be validated without a usable 15M/1H ATR; do not assume the entry is executable at market.",
        })
        return result

    if distance_atr is not None and distance_atr > MAX_LIMIT_DISTANCE_ATR:
        result.update({
            "status": "WAIT_PULLBACK", "execution_type": None, "final_decision": "WAIT_PULLBACK",
            "required_confirmation": "Structural entry is too far from current price; wait for a realistic pullback.",
        })
        return result

    essentially_at_price = (
        distance_atr is not None and distance_atr <= 0.15
    ) or (
        distance_atr is None and abs(entry_ref - price) <= 1e-12
    )
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
