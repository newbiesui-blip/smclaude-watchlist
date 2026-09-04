"""ATHENA position-health engine.

Read-only decision layer. It combines existing SMC position diagnostics with the
persisted derivatives state. It NEVER changes SMC score, direction, entry, SL,
TP, execution state, or trade classification.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DERIVATIVES_STATE_FILE = os.path.join(HERE, "derivatives_state.json")

HEALTH_STATES = ("HEALTHY", "CAUTION", "ELEVATED_RISK", "EXIT_WARNING", "RECOVERY")
ACTIVE_EXCHANGE_STATES = {"OPEN"}
NON_OPEN_EXCHANGE_STATES = {"UNKNOWN", "ERROR", "NOT_FOUND", "NOT_MATCHED", "UNSYNCED", "CLOSED", "REVERSED"}

# Risk contribution from the existing SMC reversal diagnostic.
SMC_RISK = {
    "STABLE": 0,
    "NORMAL_PULLBACK": 1,
    "LIQUIDITY_SWEEP": 2,
    "MOMENTUM_DETERIORATION": 2,
    "CONFIRMED_REVERSAL": 4,
}

# Derivatives contribution. Direction matters for crowding/trap states.
DERIV_RISK = {
    "SUPPORTIVE": 0,
    "NEUTRAL": 0,
    "CROWDED_LONG": 1,
    "CROWDED_SHORT": 1,
    "LONG_TRAP_RISK": 3,
    "SHORT_TRAP_RISK": 3,
    "LIQUIDATION_EVENT": 2,
    "REVERSAL_RISK": 2,
}


def load_derivatives_state(path: str = DERIVATIVES_STATE_FILE):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def normalize_symbol(symbol):
    s = str(symbol or "").upper().strip()
    for suffix in ("-SWAP", "_USDT", "-USDT"):
        if s.endswith(suffix):
            s = s[: -len(suffix)] + "USDT"
    if not s.endswith("USDT"):
        s = s.replace("-", "") + "USDT"
    return s


def get_derivatives_context(symbol, state_store=None):
    store = state_store if state_store is not None else load_derivatives_state()
    key = normalize_symbol(symbol)
    if key in store:
        return store[key]
    # Defensive fallback for stores that use exchange-native casing/format.
    for k, v in store.items():
        if normalize_symbol(k) == key:
            return v
    return None


def _same_side_crowding(direction, deriv_state):
    if deriv_state == "CROWDED_LONG":
        return direction == "BULLISH"
    if deriv_state == "CROWDED_SHORT":
        return direction == "BEARISH"
    return False


def _opposite_side_crowding(direction, deriv_state):
    if deriv_state == "CROWDED_LONG":
        return direction == "BEARISH"
    if deriv_state == "CROWDED_SHORT":
        return direction == "BULLISH"
    return False


def _trap_matches_position(direction, deriv_state):
    # LONG_TRAP_RISK means long participants are trapped: dangerous for BULLISH.
    # SHORT_TRAP_RISK means short participants are trapped: dangerous for BEARISH.
    return (
        (direction == "BULLISH" and deriv_state == "LONG_TRAP_RISK")
        or (direction == "BEARISH" and deriv_state == "SHORT_TRAP_RISK")
    )


def _derivatives_risk(direction, deriv_state):
    risk = DERIV_RISK.get(deriv_state, 0)
    if _same_side_crowding(direction, deriv_state):
        risk += 1
    elif _opposite_side_crowding(direction, deriv_state):
        # Opposite-side crowding can support the position; don't add risk.
        risk = max(0, risk - 1)
    return risk


def evaluate_position_health(entry, derivatives_context=None):
    """Return a normalized health snapshot.

    The exchange gate is absolute: if a triggered entry is not confirmed OPEN,
    no active-position health is produced.
    """
    if entry.get("status") != "triggered":
        return {"active": False, "health_state": None, "reason": "Position is not triggered."}

    exchange_state = str(entry.get("exchange_sync_status", "UNKNOWN")).upper()
    if exchange_state != "OPEN":
        return {
            "active": False,
            "health_state": None,
            "reason": f"Active health suppressed: BingX state is {exchange_state}, not confirmed OPEN.",
            "exchange_sync_status": exchange_state,
        }

    direction = str(entry.get("direction", "")).upper()
    smc_state = str(entry.get("reversal_state", "STABLE")).upper()
    deriv_state = str((derivatives_context or {}).get("state", "NEUTRAL")).upper()
    deriv_score = float((derivatives_context or {}).get("score", 0.0) or 0.0)
    current_r = float(entry.get("current_r", 0.0) or 0.0)
    max_r = float(entry.get("max_r", 0.0) or 0.0)

    smc_risk = SMC_RISK.get(smc_state, 0)
    deriv_risk = _derivatives_risk(direction, deriv_state)
    risk = smc_risk + deriv_risk

    reasons = []
    if smc_state != "STABLE":
        reasons.append(f"SMC={smc_state}: {entry.get('reversal_reason', 'structural deterioration')}")
    if deriv_state not in ("NEUTRAL", "SUPPORTIVE"):
        reasons.append(f"Derivatives={deriv_state} ({deriv_score:+.1f}): {(derivatives_context or {}).get('reason', '')}")

    if smc_state == "CONFIRMED_REVERSAL":
        health = "EXIT_WARNING"
    elif _trap_matches_position(direction, deriv_state) and smc_state in ("LIQUIDITY_SWEEP", "MOMENTUM_DETERIORATION", "CONFIRMED_REVERSAL"):
        health = "EXIT_WARNING"
    elif risk >= 5:
        health = "EXIT_WARNING"
    elif risk >= 3:
        health = "ELEVATED_RISK"
    elif risk >= 1:
        health = "CAUTION"
    else:
        health = "HEALTHY"

    # Recovery is only a transition state: a previously-risky position must
    # improve before it is allowed to return directly to HEALTHY.
    previous = str(entry.get("position_health_state", "")).upper()
    if previous in ("CAUTION", "ELEVATED_RISK", "EXIT_WARNING") and health == "HEALTHY":
        health = "RECOVERY"

    if not reasons:
        reasons.append("SMC structure and derivatives are currently supportive/neutral.")

    return {
        "active": True,
        "health_state": health,
        "exchange_sync_status": exchange_state,
        "smc_state": smc_state,
        "derivatives_state": deriv_state,
        "derivatives_score": round(deriv_score, 1),
        "risk_score": risk,
        "current_r": round(current_r, 3),
        "max_r": round(max_r, 3),
        "reason": " | ".join(str(x) for x in reasons if x),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def apply_health(entry, derivatives_context=None):
    """Mutate only health fields on entry; return (snapshot, transitioned)."""
    previous = str(entry.get("position_health_state", "")).upper() or None
    snapshot = evaluate_position_health(entry, derivatives_context)

    if not snapshot.get("active"):
        return snapshot, False

    current = snapshot["health_state"]
    transitioned = previous is not None and previous != current
    entry["position_health_state"] = current
    entry["position_health_previous_state"] = previous
    entry["position_health_reason"] = snapshot["reason"]
    entry["position_health_updated_at"] = snapshot["updated_at"]
    entry["position_health_risk_score"] = snapshot["risk_score"]
    entry["position_health_derivatives_state"] = snapshot["derivatives_state"]
    entry["position_health_derivatives_score"] = snapshot["derivatives_score"]
    entry["position_health_transition"] = transitioned
    return snapshot, transitioned


def _test(name, condition):
    if not condition:
        raise AssertionError(name)
    print(f"[PASS] {name}")


def run_tests():
    base = {
        "status": "triggered",
        "direction": "BULLISH",
        "exchange_sync_status": "OPEN",
        "reversal_state": "STABLE",
        "reversal_reason": "No active reversal evidence detected.",
        "current_r": 2.0,
        "max_r": 3.0,
    }
    d = {"state": "SUPPORTIVE", "score": 35, "reason": "Price/OI structure supportive."}
    s = evaluate_position_health(base.copy(), d)
    _test("supportive open position -> HEALTHY", s["health_state"] == "HEALTHY")

    x = base.copy(); x["exchange_sync_status"] = "UNKNOWN"
    s = evaluate_position_health(x, d)
    _test("UNKNOWN never produces active health", s["active"] is False)

    x = base.copy(); x["reversal_state"] = "MOMENTUM_DETERIORATION"
    d = {"state": "LONG_TRAP_RISK", "score": -55, "reason": "Price down + OI up + positive funding."}
    s = evaluate_position_health(x, d)
    _test("bullish position + long trap + deterioration -> EXIT_WARNING", s["health_state"] == "EXIT_WARNING")

    x = base.copy(); x["reversal_state"] = "STABLE"
    d = {"state": "CROWDED_LONG", "score": -20, "reason": "Long crowding."}
    s = evaluate_position_health(x, d)
    _test("same-side crowding -> CAUTION", s["health_state"] == "CAUTION")

    x = base.copy(); x["reversal_state"] = "CONFIRMED_REVERSAL"
    s = evaluate_position_health(x, {"state": "NEUTRAL", "score": 0})
    _test("confirmed SMC reversal -> EXIT_WARNING", s["health_state"] == "EXIT_WARNING")

    x = base.copy(); x["position_health_state"] = "ELEVATED_RISK"
    s = evaluate_position_health(x, {"state": "SUPPORTIVE", "score": 50})
    _test("risk improving -> RECOVERY", s["health_state"] == "RECOVERY")

    x = base.copy(); x["position_health_state"] = "HEALTHY"
    snap, changed = apply_health(x, {"state": "SUPPORTIVE", "score": 20})
    _test("first health assignment is not a transition alert", changed is False)
    snap, changed = apply_health(x, {"state": "CROWDED_LONG", "score": -20})
    _test("health deterioration creates transition", changed is True and x["position_health_state"] == "CAUTION")
    return True


if __name__ == "__main__":
    run_tests()
