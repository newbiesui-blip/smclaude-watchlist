"""ATHENA opportunity-quality layer."""
from __future__ import annotations
from typing import Any, Dict, Optional, Tuple

LOCATION_NEUTRAL_LOW = 40
LOCATION_NEUTRAL_HIGH = 60
QUALITY_FLOOR = 45
QUALITY_STRONG = 75


def _range_from_tf(r: Optional[Dict[str, Any]]) -> Optional[Tuple[float, float, str]]:
    if not isinstance(r, dict): return None
    price = float(r.get("price", 0) or 0)
    highs = [float(x) for x in (r.get("swing_high_prices") or [])]
    lows = [float(x) for x in (r.get("swing_low_prices") or [])]
    hi = min((x for x in highs if x > price), default=None)
    lo = max((x for x in lows if x < price), default=None)
    df = r.get("df")
    if df is not None and len(df):
        w = df.tail(min(80, len(df)))
        hi = hi if hi is not None else float(w["high"].max())
        lo = lo if lo is not None else float(w["low"].min())
    if hi is None or lo is None or hi <= lo: return None
    return lo, hi, "4H structural range"


def _location(price, dr, direction):
    if price is None or not dr: return {"state":"UNKNOWN","score":50,"position_pct":None,"reason":"No reliable 4H dealing range."}
    lo, hi, source = dr; span = hi-lo
    if span <= 0: return {"state":"UNKNOWN","score":50,"position_pct":None,"reason":"Invalid dealing range."}
    pct = max(0,min(100,(float(price)-lo)/span*100))
    if direction == "BULLISH":
        state, score = (("DISCOUNT",90) if pct <= 40 else ("EQUILIBRIUM",65) if pct <= 60 else ("PREMIUM",30))
    else:
        state, score = (("PREMIUM",90) if pct >= 60 else ("EQUILIBRIUM",65) if pct >= 40 else ("DISCOUNT",30))
    return {"state":state,"score":score,"position_pct":round(pct,1),"reason":f"{source}: price is {pct:.1f}% through the range ({state.lower()} for {direction.lower()}).","low":lo,"high":hi,"equilibrium":(lo+hi)/2,"source":source}


def _market_quality(tf_results):
    r = tf_results.get("15M") if isinstance(tf_results,dict) else None
    if not isinstance(r,dict) or r.get("df") is None: return {"score":0,"state":"POOR","reason":"Missing 15M market data."}
    df=r["df"]
    if len(df)<30: return {"score":35,"state":"WEAK","reason":"Insufficient 15M history."}
    score=70; reasons=[]
    try:
        q=(df["close"]*df["volume"]).tail(24).median()
        if q>5_000_000: score+=15; reasons.append("healthy quote-volume proxy")
        elif q<250_000: score-=20; reasons.append("thin quote-volume proxy")
        else: reasons.append("adequate quote-volume proxy")
    except Exception: score-=10; reasons.append("volume quality unavailable")
    try:
        mr=((df["high"]-df["low"])/df["close"].replace(0,float("nan"))).tail(24).median()
        if mr<.001 or mr>.08: score-=15; reasons.append("extreme/compressed volatility")
        else: score+=5; reasons.append("usable volatility")
    except Exception: pass
    score=max(0,min(100,round(score))); state="STRONG" if score>=QUALITY_STRONG else "ACCEPTABLE" if score>=QUALITY_FLOOR else "WEAK"
    return {"score":score,"state":state,"reason":"; ".join(reasons) or "market-quality context available"}


def enrich_plan(plan, tf_results):
    direction=str(plan.get("direction","")).upper(); price=plan.get("current_price")
    try: price=float(price) if price is not None else None
    except: price=None
    dr=_range_from_tf(tf_results.get("4H")); current=_location(price,dr,direction)
    try: planned_price=float(plan.get("planned_entry"))
    except: planned_price=None
    planned=_location(planned_price,dr,direction); mq=_market_quality(tf_results)
    ext=str(plan.get("extension_status","UNKNOWN")); eq=float(plan.get("entry_quality",0) or 0)
    chase=(direction=="BULLISH" and current.get("state")=="PREMIUM") or (direction=="BEARISH" and current.get("state")=="DISCOUNT")
    if ext=="EXTENDED" or (chase and eq<70): timing="LATE"; severity="HIGH" if ext=="EXTENDED" and chase else "MEDIUM"; timing_reason="Current price is materially worse than the planned execution location."
    elif planned.get("state") in {"DISCOUNT","PREMIUM","EQUILIBRIUM"} and current.get("state")!=planned.get("state"): timing="AWAY_FROM_PLAN"; severity="MEDIUM"; timing_reason="Underlying thesis remains valid, but current price is not at the preferred location."
    else: timing="TIMELY"; severity="LOW"; timing_reason="Current price is reasonably consistent with the planned execution location."
    loc=float(planned.get("score",50)); market=float(mq.get("score",50)); composite=.45*float(plan.get("setup_quality",0) or 0)+.25*loc+.15*market+.15*float(plan.get("entry_quality",0) or 0)-(12 if timing=="LATE" else 5 if timing=="AWAY_FROM_PLAN" else 0)
    plan.update({"market_quality_score":round(market),"market_quality_state":mq.get("state"),"market_quality_reason":mq.get("reason"),"dealing_range_low":dr[0] if dr else None,"dealing_range_high":dr[1] if dr else None,"equilibrium":current.get("equilibrium"),"current_location":current.get("state"),"current_location_score":current.get("score"),"current_location_pct":current.get("position_pct"),"current_location_reason":current.get("reason"),"planned_location":planned.get("state"),"planned_location_score":planned.get("score"),"planned_location_pct":planned.get("position_pct"),"planned_location_reason":planned.get("reason"),"entry_timing":timing,"entry_timing_severity":severity,"entry_timing_reason":timing_reason,"opportunity_score":max(0,min(100,round(composite))),"why_this_setup":f"{plan.get('regime','HTF unknown')} / {plan.get('trend_alignment','alignment unknown')} | {plan.get('setup_type','structure setup')} | planned location {planned.get('state','UNKNOWN').lower()} | current location {current.get('state','UNKNOWN').lower()} | timing {timing.lower()} | structural R:R {float(plan.get('structural_rr',0) or 0):.2f}"})
    if plan.get("status") in ("READY_MARKET","READY_LIMIT") and timing=="LATE" and planned.get("score",50)>=60 and plan.get("setup_quality",0)>=70:
        plan["status"]="WAIT_PULLBACK"; plan["execution_type"]=None; plan["required_confirmation"]="Wait for price to return toward the planned HTF location; do not chase the move."
    elif plan.get("status")=="READY_MARKET" and current.get("score",50)<40 and planned.get("score",50)>=60:
        plan["status"]="WAIT_PULLBACK"; plan["execution_type"]=None; plan["required_confirmation"]="Current price is poorly located; wait for the planned entry location."
    return plan


def refine_qualifying(qualifying):
    for _symbol,tf_results,_used,_score,_direction,plan in qualifying:
        try: enrich_plan(plan,tf_results)
        except Exception as exc: plan["quality_layer_error"]=f"{type(exc).__name__}: {exc}"
    return qualifying


def install():
    import full_scan
    original_scan_all=full_scan.scan_all
    original_rank=full_scan.scanner.classify_and_rank
    original_format=full_scan._format_alert

    def wrapped_scan_all(active_key,symbols):
        return refine_qualifying(original_scan_all(active_key,symbols))

    def wrapped_rank(plans):
        buckets=original_rank(plans)
        for key in ("READY_NOW","NEAR_READY","WAITING"):
            buckets[key].sort(key=lambda p: (float(p.get("opportunity_score",0) or 0), float(p.get("setup_quality",0) or 0), float(p.get("entry_quality",0) or 0)), reverse=True)
        return buckets

    def wrapped_format(symbol,used,score,direction,plan,is_new=True,reason=None):
        msg=original_format(symbol,used,score,direction,plan,is_new=is_new,reason=reason)
        extra=[
            f"Opportunity Score: {plan.get('opportunity_score',0):.0f}/100",
            f"Market Quality: {plan.get('market_quality_score',0):.0f}/100 ({plan.get('market_quality_state','UNKNOWN')})",
            f"Location: planned={plan.get('planned_location','UNKNOWN')} / current={plan.get('current_location','UNKNOWN')}",
            f"Timing: {plan.get('entry_timing','UNKNOWN')} — {plan.get('entry_timing_reason','')}",
            f"WHY: {plan.get('why_this_setup','')}",
        ]
        if plan.get("required_confirmation"):
            extra.append(f"Wait/Confirm: {plan['required_confirmation']}")
        return msg+"\n"+"\n".join(extra)

    full_scan.scan_all=wrapped_scan_all
    full_scan.scanner.classify_and_rank=wrapped_rank
    full_scan._format_alert=wrapped_format
    return full_scan


def main(): return install().main()

if __name__=="__main__": raise SystemExit(main())
