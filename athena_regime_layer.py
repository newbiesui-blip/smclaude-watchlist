"""ATHENA additive market-regime intelligence.

This layer sits outside smc_scanner.py. It detects accumulation-like and
distribution-like range structure from the existing multi-timeframe OHLCV
frames and optionally uses the already-persisted derivatives context as
confirmation. It never changes SMC direction, entry, SL, TP, execution state,
or trade classification.

Important: the detector identifies *accumulation-like* / *distribution-like*
market structure. It does not claim to prove institutional intent.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple

ACCUMULATION = "CONFIRMED_ACCUMULATION_LIKE"
DISTRIBUTION = "CONFIRMED_DISTRIBUTION_LIKE"
POTENTIAL_ACCUMULATION = "POTENTIAL_ACCUMULATION"
POTENTIAL_DISTRIBUTION = "POTENTIAL_DISTRIBUTION"
ORDINARY_RANGE = "ORDINARY_RANGE"
TRENDING = "TRENDING"
UNKNOWN = "UNKNOWN"


def _df(tf_results: Dict[str, Any], tf: str):
    row = tf_results.get(tf) if isinstance(tf_results, dict) else None
    if not isinstance(row, dict):
        return None
    value = row.get("df")
    return value if value is not None and len(value) else None


def _safe_float(v) -> Optional[float]:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _range_stats(df, window: int = 48) -> Optional[Dict[str, float]]:
    if df is None or len(df) < max(24, window // 2):
        return None
    w = df.tail(min(window, len(df)))
    hi = _safe_float(w["high"].max())
    lo = _safe_float(w["low"].min())
    close = _safe_float(w["close"].iloc[-1])
    if hi is None or lo is None or close is None or hi <= lo:
        return None
    span_pct = (hi - lo) / close * 100.0
    pos = (close - lo) / (hi - lo) * 100.0
    return {"high": hi, "low": lo, "close": close, "span_pct": span_pct, "position_pct": pos}


def _efficiency(df, window: int = 24) -> Optional[float]:
    if df is None or len(df) < window + 1:
        return None
    w = df.tail(window + 1)
    try:
        net = abs(float(w["close"].iloc[-1]) - float(w["close"].iloc[0]))
        path = float(w["close"].diff().abs().sum())
        return net / path if path else 0.0
    except Exception:
        return None


def _volatility_ratio(df, window: int = 24) -> Optional[float]:
    if df is None or len(df) < window * 2:
        return None
    try:
        r = ((df["high"] - df["low"]) / df["close"].replace(0, float("nan"))).dropna()
        if len(r) < window * 2:
            return None
        recent = float(r.tail(window).median())
        prior = float(r.iloc[-2 * window:-window].median())
        return recent / prior if prior else None
    except Exception:
        return None


def _volume_ratio(df, window: int = 24) -> Optional[float]:
    if df is None or len(df) < window * 2:
        return None
    try:
        recent = float(df["volume"].tail(window).median())
        prior = float(df["volume"].iloc[-2 * window:-window].median())
        return recent / prior if prior else None
    except Exception:
        return None


def _edge_sweep(df, side: str) -> Tuple[bool, str]:
    """Detect a recent edge raid against the range that existed before it.

    The boundary is deliberately calculated from older bars, not from the
    sweep candle itself. Otherwise the sweep would redefine the range edge
    and become impossible to detect.
    """
    if df is None or len(df) < 24:
        return False, ""
    try:
        base = df.iloc[:-6].tail(30)
        recent = df.tail(6)
        base_low = float(base["low"].min())
        base_high = float(base["high"].max())
        last = recent.iloc[-1]
        if side == "LOW":
            swept = float(recent["low"].min()) < base_low * 0.9975
            reclaimed = float(last["close"]) > base_low and float(last["close"]) > float(last["open"])
            if swept and reclaimed:
                return True, "recent range-low liquidity sweep followed by bullish reclaim"
        else:
            swept = float(recent["high"].max()) > base_high * 1.0025
            rejected = float(last["close"]) < base_high and float(last["close"]) < float(last["open"])
            if swept and rejected:
                return True, "recent range-high liquidity sweep followed by bearish rejection"
    except Exception:
        pass
    return False, ""


def _range_regime(df) -> Dict[str, Any]:
    stats = _range_stats(df)
    if not stats:
        return {"state": UNKNOWN, "confidence": 0, "evidence": [], "stats": {}}

    eff = _efficiency(df)
    vr = _volatility_ratio(df)
    evidence: List[str] = []
    score = 0

    if eff is not None and eff < 0.35:
        score += 25
        evidence.append("low directional efficiency / rotational price action")
    if stats["span_pct"] <= 18:
        score += 20
        evidence.append("bounded 4H range")
    if vr is not None and vr < 0.85:
        score += 15
        evidence.append("volatility contraction inside the range")

    low_sweep, low_reason = _edge_sweep(df, "LOW")
    high_sweep, high_reason = _edge_sweep(df, "HIGH")
    if low_sweep:
        score += 25
        evidence.append(low_reason)
    if high_sweep:
        score += 25
        evidence.append(high_reason)

    pos = stats["position_pct"]
    if low_sweep and pos <= 55:
        score += 10
    if high_sweep and pos >= 45:
        score += 10

    if low_sweep and not high_sweep:
        state = ACCUMULATION if score >= 70 else POTENTIAL_ACCUMULATION
    elif high_sweep and not low_sweep:
        state = DISTRIBUTION if score >= 70 else POTENTIAL_DISTRIBUTION
    elif score >= 55:
        state = ORDINARY_RANGE
    else:
        state = TRENDING

    return {
        "state": state,
        "confidence": max(0, min(100, int(score))),
        "evidence": evidence,
        "stats": stats,
        "efficiency": eff,
        "volatility_ratio": vr,
        "volume_ratio": _volume_ratio(df),
        "low_sweep": low_sweep,
        "high_sweep": high_sweep,
    }


def _derivatives_alignment(direction: str, dctx: Dict[str, Any], regime: str) -> Dict[str, Any]:
    if not isinstance(dctx, dict) or not dctx:
        return {"state": "UNAVAILABLE", "score": 0, "raw_score": 0.0, "reason": "No persisted derivatives context."}
    state = str(dctx.get("state", "NEUTRAL")).upper()
    score = _safe_float(dctx.get("score")) or 0.0
    reason = str(dctx.get("reason", ""))
    bonus = 0
    notes: List[str] = []
    if regime in (ACCUMULATION, POTENTIAL_ACCUMULATION):
        if state in ("CROWDED_SHORT", "SHORT_TRAP_RISK"):
            bonus += 10
            notes.append("derivatives show short-side crowding/trap risk")
        elif state == "CROWDED_LONG":
            bonus -= 8
            notes.append("derivatives show long-side crowding against accumulation thesis")
    elif regime in (DISTRIBUTION, POTENTIAL_DISTRIBUTION):
        if state in ("CROWDED_LONG", "LONG_TRAP_RISK"):
            bonus += 10
            notes.append("derivatives show long-side crowding/trap risk")
        elif state == "CROWDED_SHORT":
            bonus -= 8
            notes.append("derivatives show short-side crowding against distribution thesis")
    return {
        "state": state,
        "score": max(-20, min(20, bonus)),
        "raw_score": score,
        "reason": (reason + ("; " if reason and notes else "") + "; ".join(notes)).strip("; "),
    }


def enrich_plan(plan: Dict[str, Any], tf_results: Dict[str, Any]) -> Dict[str, Any]:
    df4 = _df(tf_results, "4H")
    df1 = _df(tf_results, "1H")
    base = _range_regime(df4)
    state = base["state"]

    confirm = 0
    if df1 is not None and len(df1) >= 12:
        eff1 = _efficiency(df1, 12)
        if state in (ACCUMULATION, POTENTIAL_ACCUMULATION) and eff1 is not None and eff1 > 0.25:
            try:
                if float(df1["close"].iloc[-1]) > float(df1["close"].iloc[-6]):
                    confirm += 10
                    base["evidence"].append("1H price is improving after the range-low event")
            except Exception:
                pass
        if state in (DISTRIBUTION, POTENTIAL_DISTRIBUTION) and eff1 is not None and eff1 > 0.25:
            try:
                if float(df1["close"].iloc[-1]) < float(df1["close"].iloc[-6]):
                    confirm += 10
                    base["evidence"].append("1H price is weakening after the range-high event")
            except Exception:
                pass

    confidence = max(0, min(100, int(base.get("confidence", 0)) + confirm))
    if state == POTENTIAL_ACCUMULATION and confidence >= 70:
        state = ACCUMULATION
    elif state == POTENTIAL_DISTRIBUTION and confidence >= 70:
        state = DISTRIBUTION

    direction = str(plan.get("direction", "")).upper()
    d = _derivatives_alignment(direction, plan.get("derivatives_context") or {}, state)
    regime_quality = max(0, min(100, confidence + d["score"]))

    plan.update({
        "market_regime": state,
        "regime_quality": int(regime_quality),
        "regime_confidence": int(confidence),
        "regime_evidence": list(base.get("evidence", [])),
        "regime_range_low": base.get("stats", {}).get("low"),
        "regime_range_high": base.get("stats", {}).get("high"),
        "regime_range_position_pct": base.get("stats", {}).get("position_pct"),
        "regime_volatility_ratio": base.get("volatility_ratio"),
        "regime_volume_ratio": base.get("volume_ratio"),
        "regime_derivatives_state": d["state"],
        "regime_derivatives_score": d["score"],
        "regime_derivatives_raw_score": d["raw_score"],
        "regime_derivatives_reason": d["reason"],
    })

    why = plan.get("why_this_setup", "")
    regime_text = f"regime={state} ({confidence}/100)"
    if d["state"] != "UNAVAILABLE":
        regime_text += f" | derivatives={d['state']} ({d['score']:+d})"
    plan["why_this_setup"] = (regime_text + " | " + why).strip(" |")
    return plan


def refine_qualifying(qualifying: Iterable[Tuple[Any, Dict[str, Any], Any, Any, Any, Dict[str, Any]]]):
    for item in qualifying:
        try:
            _symbol, tf_results, _used, _score, _direction, plan = item
            enrich_plan(plan, tf_results)
        except Exception as exc:
            try:
                item[-1]["regime_layer_error"] = f"{type(exc).__name__}: {exc}"
            except Exception:
                pass
    return qualifying


def install():
    import athena_quality_layer
    full_scan = athena_quality_layer.install()
    original_scan_all = full_scan.scan_all
    original_format = full_scan._format_alert

    def wrapped_scan_all(active_key, symbols):
        return refine_qualifying(original_scan_all(active_key, symbols))

    def wrapped_format(symbol, used, score, direction, plan, is_new=True, reason=None):
        msg = original_format(symbol, used, score, direction, plan, is_new=is_new, reason=reason)
        regime = plan.get("market_regime", "UNKNOWN")
        confidence = plan.get("regime_confidence", 0)
        rq = plan.get("regime_quality", 0)
        evidence = plan.get("regime_evidence") or []
        dstate = plan.get("regime_derivatives_state", "UNAVAILABLE")
        dscore = plan.get("regime_derivatives_score", 0)
        extra = [
            f"Market Regime: {regime} | confidence {confidence}/100 | regime quality {rq}/100",
            f"Regime Location: {plan.get('regime_range_position_pct', 0):.1f}% of 4H range" if plan.get("regime_range_position_pct") is not None else "Regime Location: unavailable",
            f"Derivatives Context: {dstate} ({dscore:+d})",
        ]
        if evidence:
            extra.append("Regime Evidence: " + "; ".join(evidence[:4]))
        if plan.get("regime_derivatives_reason"):
            extra.append("Derivatives Read: " + plan["regime_derivatives_reason"])
        return msg + "\n" + "\n".join(extra)

    full_scan.scan_all = wrapped_scan_all
    full_scan._format_alert = wrapped_format
    return full_scan


def main():
    return install().main()


if __name__ == "__main__":
    raise SystemExit(main())
