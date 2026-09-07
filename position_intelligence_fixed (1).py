"""ATHENA position-intelligence engine.

Read-only decision layer for already-confirmed exchange positions.

This module does NOT:
- place, cancel, close, or modify orders;
- change SMC score, direction, entry, SL, TP, execution state, or trade type;
- decide whether a setup should be entered.

It converts existing SMC/setup fields plus BingX exchange facts into a
normalized operating snapshot: P&L, R, distance to SL/TP, BE eligibility,
partial-profit opportunity, time in trade, and conservative exit warnings.

Exchange facts are preferred when available. Derived values are explicitly
marked as calculated so callers can preserve source provenance.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict, Optional


HEALTH_STATES = {
    "HEALTHY",
    "CAUTION",
    "ELEVATED_RISK",
    "EXIT_WARNING",
    "RECOVERY",
}


def _float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _timestamp_ms(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None

    number = _float(value)
    if number is not None:
        if number < 10_000_000_000:
            number *= 1000
        return int(number)

    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return int(parsed.astimezone(timezone.utc).timestamp() * 1000)


def _first_float(source: Dict[str, Any], *keys: str) -> Optional[float]:
    for key in keys:
        value = _float(source.get(key))
        if value is not None:
            return value
    return None


def _direction(entry: Dict[str, Any]) -> str:
    return str(entry.get("direction", "")).upper().strip()


def _entry_price(entry: Dict[str, Any]) -> Optional[float]:
    return _first_float(
        entry,
        "exchange_avg_price",
        "entry_price",
        "avg_entry_price",
        "average_entry_price",
        "preferred_entry",
    )


def _current_price(
    entry: Dict[str, Any],
    exchange_position: Optional[Dict[str, Any]] = None,
) -> Optional[float]:
    position = exchange_position or {}
    value = _first_float(
        position,
        "markPrice",
        "mark_price",
        "lastPrice",
        "last_price",
        "currentPrice",
        "current_price",
        "price",
    )
    if value is not None:
        return value

    return _first_float(
        entry,
        "exchange_mark_price",
        "exchange_current_price",
        "current_price",
        "mark_price",
        "last_price",
    )


def _amount(
    entry: Dict[str, Any],
    exchange_position: Optional[Dict[str, Any]] = None,
) -> Optional[float]:
    position = exchange_position or {}
    value = _first_float(
        position,
        "positionAmt",
        "positionAmount",
        "quantity",
        "size",
    )
    if value is not None:
        return value

    return _first_float(
        entry,
        "exchange_position_amount",
        "position_amount",
        "quantity",
        "size",
    )


def _stop_price(entry: Dict[str, Any]) -> Optional[float]:
    return _first_float(
        entry,
        "invalidation",
        "original_invalidation",
        "stop_loss",
        "sl",
    )


def _targets(entry: Dict[str, Any]):
    targets = entry.get("validated_targets")
    if not isinstance(targets, list):
        targets = entry.get("targets")

    if not isinstance(targets, list):
        return []

    normalized = []
    for index, target in enumerate(targets, 1):
        if not isinstance(target, dict):
            continue
        price = _first_float(target, "price", "target", "level")
        if price is None:
            continue
        r = _first_float(target, "r", "R", "risk_reward")
        normalized.append(
            {
                "index": index,
                "price": price,
                "r": r,
                "raw": target,
            }
        )
    return normalized


def _unrealized_pnl(
    entry: Dict[str, Any],
    exchange_position: Optional[Dict[str, Any]] = None,
):
    position = exchange_position or {}
    exchange_pnl = _first_float(
        position,
        "unrealizedProfit",
        "unrealizedPnl",
        "unrealisedProfit",
        "unrealisedPnl",
    )
    if exchange_pnl is None:
        exchange_pnl = _first_float(
            entry,
            "exchange_unrealized_pnl",
            "unrealized_pnl",
            "unrealizedProfit",
        )

    if exchange_pnl is not None:
        return exchange_pnl, "exchange"

    entry_price = _entry_price(entry)
    current_price = _current_price(entry, exchange_position)
    amount = _amount(entry, exchange_position)

    if entry_price is None or current_price is None or amount is None:
        return None, None

    direction = _direction(entry)
    size = abs(amount)

    if direction in {"BULLISH", "LONG", "BUY"}:
        pnl = (current_price - entry_price) * size
    elif direction in {"BEARISH", "SHORT", "SELL"}:
        pnl = (entry_price - current_price) * size
    else:
        return None, None

    return pnl, "calculated"


def _risk_per_unit(entry: Dict[str, Any]) -> Optional[float]:
    entry_price = _entry_price(entry)
    stop = _stop_price(entry)

    if entry_price is None or stop is None:
        return None

    distance = abs(entry_price - stop)
    return distance if distance > 0 else None


def _current_r(
    entry: Dict[str, Any],
    pnl: Optional[float],
    amount: Optional[float],
) -> Optional[float]:
    risk_per_unit = _risk_per_unit(entry)

    # Prefer fresh exchange-derived P&L when enough data exists.
    if pnl is not None and amount is not None and risk_per_unit is not None:
        risk_amount = risk_per_unit * abs(amount)
        if risk_amount > 0:
            return pnl / risk_amount

    # Fall back to the stored R only when fresh calculation is impossible.
    existing = _float(entry.get("current_r"))
    if existing is not None:
        return existing

    return None


def _distance_percent(current: float, reference: float) -> Optional[float]:
    if current == 0:
        return None
    return abs(reference - current) / abs(current) * 100.0


def _sl_distance(
    entry: Dict[str, Any],
    current_price: Optional[float],
):
    stop = _stop_price(entry)
    if stop is None or current_price is None:
        return None

    direction = _direction(entry)
    if direction in {"BULLISH", "LONG", "BUY"}:
        safe_distance = current_price - stop
    elif direction in {"BEARISH", "SHORT", "SELL"}:
        safe_distance = stop - current_price
    else:
        safe_distance = abs(current_price - stop)

    return {
        "price": abs(current_price - stop),
        "percent": _distance_percent(current_price, stop),
        "favorable": safe_distance > 0,
        "at_or_beyond": safe_distance <= 0,
    }


def _target_distance(
    current_price: Optional[float],
    target: Dict[str, Any],
    direction: str,
):
    if current_price is None:
        return None

    target_price = target["price"]
    if direction in {"BULLISH", "LONG", "BUY"}:
        reached = current_price >= target_price
    elif direction in {"BEARISH", "SHORT", "SELL"}:
        reached = current_price <= target_price
    else:
        reached = False

    return {
        "index": target["index"],
        "price": target_price,
        "r": target.get("r"),
        "reached": reached,
        "distance_percent": _distance_percent(current_price, target_price),
    }


def _first_unreached_target(target_statuses):
    for target in target_statuses:
        if not target["reached"]:
            return target
    return None


def _trade_duration_ms(entry: Dict[str, Any], now_ms: Optional[int] = None):
    start = (
        entry.get("triggered_at")
        or entry.get("opened_at")
        or entry.get("position_opened_at")
    )
    start_ms = _timestamp_ms(start)
    if start_ms is None:
        return None

    end_ms = now_ms if now_ms is not None else int(
        datetime.now(timezone.utc).timestamp() * 1000
    )
    return max(0, end_ms - start_ms)


def format_duration(duration_ms: Optional[int]) -> Optional[str]:
    if duration_ms is None:
        return None

    total_seconds = duration_ms // 1000
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)

    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _be_state(
    direction: str,
    current_r: Optional[float],
    stop: Optional[float],
    entry_price: Optional[float],
    current_price: Optional[float],
):
    """Return a suggestion state; never changes the actual exchange stop."""

    if entry_price is None or current_price is None or stop is None:
        return {
            "status": "UNKNOWN",
            "eligible": False,
            "reason": "Insufficient price/SL data.",
        }

    if current_r is not None and current_r >= 1.0:
        return {
            "status": "ELIGIBLE",
            "eligible": True,
            "reason": "Position has reached at least +1R.",
        }

    if direction in {"BULLISH", "LONG", "BUY"}:
        favorable = current_price > entry_price
    elif direction in {"BEARISH", "SHORT", "SELL"}:
        favorable = current_price < entry_price
    else:
        favorable = False

    if favorable:
        return {
            "status": "PROFITING",
            "eligible": False,
            "reason": "Position is favorable but has not reached +1R.",
        }

    return {
        "status": "NOT_ELIGIBLE",
        "eligible": False,
        "reason": "Position has not reached the conservative +1R threshold.",
    }


def _partial_state(target_statuses, current_r: Optional[float]):
    if not target_statuses:
        return {
            "status": "UNAVAILABLE",
            "suggest": False,
            "reason": "No validated targets are available.",
        }

    reached = [item for item in target_statuses if item["reached"]]
    next_target = _first_unreached_target(target_statuses)

    if reached:
        if next_target:
            return {
                "status": "TP_REACHED",
                "suggest": False,
                "reason": (
                    f"TP{reached[-1]['index']} has been reached; "
                    "partial state is already represented by target progress."
                ),
                "next_target": next_target,
            }
        return {
            "status": "ALL_TARGETS_REACHED",
            "suggest": False,
            "reason": "All configured targets are currently reached.",
        }

    first = target_statuses[0]
    first_r = first.get("r")
    if current_r is not None and first_r is not None and current_r >= first_r:
        return {
            "status": "PARTIAL_OPPORTUNITY",
            "suggest": True,
            "reason": f"Current position is at/above TP1's {first_r:.2f}R objective.",
            "next_target": first,
        }

    return {
        "status": "WAITING",
        "suggest": False,
        "reason": "No validated partial-profit threshold has been reached.",
        "next_target": first,
    }


def build_position_intelligence(
    entry: Dict[str, Any],
    exchange_position: Optional[Dict[str, Any]] = None,
    health_snapshot: Optional[Dict[str, Any]] = None,
    now_ms: Optional[int] = None,
) -> Dict[str, Any]:
    """Build a read-only operating snapshot for one position."""

    exchange_position = exchange_position or {}
    direction = _direction(entry)
    entry_price = _entry_price(entry)
    current_price = _current_price(entry, exchange_position)
    amount = _amount(entry, exchange_position)
    stop = _stop_price(entry)

    pnl, pnl_source = _unrealized_pnl(entry, exchange_position)
    current_r = _current_r(entry, pnl, amount)

    targets = _targets(entry)
    target_statuses = [
        status
        for status in (
            _target_distance(current_price, target, direction)
            for target in targets
        )
        if status is not None
    ]

    sl_distance = _sl_distance(entry, current_price)
    be = _be_state(direction, current_r, stop, entry_price, current_price)
    partial = _partial_state(target_statuses, current_r)

    duration_ms = _trade_duration_ms(entry, now_ms=now_ms)

    health_state = None
    if health_snapshot:
        health_state = health_snapshot.get("health_state")
    if health_state is None:
        health_state = entry.get("position_health_state")

    warnings = []

    if sl_distance and sl_distance["at_or_beyond"]:
        warnings.append("PRICE_AT_OR_BEYOND_SL")

    if health_state == "EXIT_WARNING":
        warnings.append("HEALTH_EXIT_WARNING")
    elif health_state == "ELEVATED_RISK":
        warnings.append("HEALTH_ELEVATED_RISK")
    elif health_state == "CAUTION":
        warnings.append("HEALTH_CAUTION")

    if partial.get("suggest"):
        warnings.append("PARTIAL_PROFIT_OPPORTUNITY")

    if be.get("eligible"):
        warnings.append("BREAK_EVEN_ELIGIBLE")

    return {
        "symbol": entry.get("symbol"),
        "direction": direction,
        "exchange": entry.get("exchange"),
        "exchange_sync_status": entry.get("exchange_sync_status", "UNKNOWN"),
        "position_lifecycle": entry.get("position_lifecycle"),
        "active": entry.get("status") == "triggered"
        and entry.get("exchange_sync_status") == "OPEN",

        "entry_price": entry_price,
        "current_price": current_price,
        "position_amount": amount,

        "unrealized_pnl": round(pnl, 8) if pnl is not None else None,
        "unrealized_pnl_source": pnl_source,

        "current_r": round(current_r, 4) if current_r is not None else None,
        "max_r": _float(entry.get("max_r")),

        "sl": stop,
        "sl_distance": sl_distance,

        "targets": target_statuses,
        "partial_profit": partial,

        "break_even": be,

        "health_state": health_state,
        "health_reason": (
            (health_snapshot or {}).get("reason")
            or entry.get("position_health_reason")
        ),

        "duration_ms": duration_ms,
        "duration": format_duration(duration_ms),

        "warnings": warnings,
        "operating_state": _operating_state(
            health_state,
            sl_distance,
            partial,
            be,
        ),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _operating_state(health_state, sl_distance, partial, be):
    if sl_distance and sl_distance["at_or_beyond"]:
        return "EXIT_WARNING"

    if health_state == "EXIT_WARNING":
        return "EXIT_WARNING"

    if health_state == "ELEVATED_RISK":
        return "ELEVATED_RISK"

    if partial.get("suggest"):
        return "TAKE_PARTIAL_CONSIDERATION"

    if be.get("eligible"):
        return "BREAK_EVEN_ELIGIBLE"

    if health_state == "CAUTION":
        return "CAUTION"

    if health_state == "RECOVERY":
        return "RECOVERY"

    return "HEALTHY"


def format_position_intelligence(snapshot: Dict[str, Any]) -> str:
    """Create a concise Telegram-ready operating summary."""

    symbol = snapshot.get("symbol") or "?"
    direction = snapshot.get("direction") or "?"
    state = snapshot.get("operating_state") or "UNKNOWN"

    lines = [
        f"📊 POSITION INTELLIGENCE: {symbol} {direction}",
        f"State: {state}",
        f"Exchange: {snapshot.get('exchange_sync_status', 'UNKNOWN')}",
    ]

    pnl = snapshot.get("unrealized_pnl")
    pnl_source = snapshot.get("unrealized_pnl_source")
    if pnl is not None:
        lines.append(f"Unrealized P&L: {pnl:+.4f} ({pnl_source})")

    current_r = snapshot.get("current_r")
    max_r = snapshot.get("max_r")
    if current_r is not None:
        max_text = f" | Max: {max_r:+.2f}R" if max_r is not None else ""
        lines.append(f"R: {current_r:+.2f}R{max_text}")

    entry_price = snapshot.get("entry_price")
    current_price = snapshot.get("current_price")
    if entry_price is not None:
        lines.append(f"Entry: {entry_price:.8g}")
    if current_price is not None:
        lines.append(f"Current: {current_price:.8g}")

    sl_distance = snapshot.get("sl_distance")
    if sl_distance:
        pct = sl_distance.get("percent")
        pct_text = f" ({pct:.2f}%)" if pct is not None else ""
        lines.append(f"SL distance: {sl_distance['price']:.8g}{pct_text}")

    be = snapshot.get("break_even", {})
    lines.append(f"BE: {be.get('status', 'UNKNOWN')} — {be.get('reason', '')}")

    partial = snapshot.get("partial_profit", {})
    lines.append(
        f"Partials: {partial.get('status', 'UNKNOWN')} — "
        f"{partial.get('reason', '')}"
    )

    duration = snapshot.get("duration")
    if duration:
        lines.append(f"Time in trade: {duration}")

    health = snapshot.get("health_state")
    if health:
        lines.append(f"Health: {health}")

    reason = snapshot.get("health_reason")
    if reason:
        lines.append(f"Why: {reason}")

    warnings = snapshot.get("warnings") or []
    if warnings:
        lines.append("Warnings: " + ", ".join(warnings))

    lines.append(
        "Note: intelligence only; no order/SL/TP modification is performed."
    )
    return "\n".join(lines)


def _test(name, condition):
    if not condition:
        raise AssertionError(name)
    print(f"[PASS] {name}")


def run_tests():
    base = {
        "symbol": "BTCUSDT",
        "direction": "BULLISH",
        "status": "triggered",
        "exchange": "bingx",
        "exchange_sync_status": "OPEN",
        "position_lifecycle": "OPEN",
        "exchange_avg_price": 100.0,
        "exchange_position_amount": 2.0,
        "invalidation": 90.0,
        "targets": [
            {"price": 110.0, "r": 1.0},
            {"price": 120.0, "r": 2.0},
        ],
        "triggered_at": "2026-09-05T10:00:00+00:00",
        "position_health_state": "HEALTHY",
        "position_health_reason": "Structure stable.",
        "current_r": 1.0,
        "max_r": 1.25,
    }

    snap = build_position_intelligence(
        base,
        exchange_position={
            "markPrice": 110.0,
            "positionAmt": 2.0,
            "unrealizedProfit": 20.0,
        },
        now_ms=_timestamp_ms("2026-09-05T11:00:00+00:00"),
    )

    _test("exchange P&L is preferred", snap["unrealized_pnl"] == 20.0)
    _test("exchange P&L source is preserved", snap["unrealized_pnl_source"] == "exchange")
    _test("current R is calculated from fresh P&L", snap["current_r"] == 1.0)
    _test("BE is eligible at +1R", snap["break_even"]["eligible"] is True)
    _test("TP1 is reached", snap["targets"][0]["reached"] is True)
    _test("trade duration is one hour", snap["duration"] == "1h 0m")

    calculated = dict(base)
    calculated.pop("current_r")
    snap = build_position_intelligence(
        calculated,
        exchange_position={"markPrice": 105.0, "positionAmt": 2.0},
    )
    _test("calculated P&L fallback works", snap["unrealized_pnl"] == 10.0)
    _test("calculated P&L is marked", snap["unrealized_pnl_source"] == "calculated")
    _test("calculated R fallback works", snap["current_r"] == 0.5)

    signed_short = dict(calculated)
    signed_short.update({
        "direction": "BEARISH",
        "invalidation": 110.0,
        "targets": [{"price": 90.0, "r": 1.0}],
    })
    snap = build_position_intelligence(
        signed_short,
        exchange_position={"markPrice": 95.0, "positionAmt": -2.0},
    )
    _test("signed short amount calculates positive P&L", snap["unrealized_pnl"] == 10.0)
    _test("signed short amount calculates positive R", snap["current_r"] == 0.5)
    _test("short BE state recognizes favorable price", snap["break_even"]["status"] == "PROFITING")
    _test("short BE state is not eligible below +1R", snap["break_even"]["eligible"] is False)

    short = dict(base)
    short.update({
        "direction": "BEARISH",
        "exchange_avg_price": 100.0,
        "invalidation": 110.0,
        "targets": [{"price": 90.0, "r": 1.0}],
    })
    snap = build_position_intelligence(
        short,
        exchange_position={
            "markPrice": 90.0,
            "positionAmt": 2.0,
            "unrealizedProfit": 20.0,
        },
    )
    _test("short target direction works", snap["targets"][0]["reached"] is True)
    _test("short P&L is positive", snap["unrealized_pnl"] == 20.0)

    warning = dict(base)
    warning["position_health_state"] = "EXIT_WARNING"
    snap = build_position_intelligence(
        warning,
        exchange_position={
            "markPrice": 101.0,
            "positionAmt": 2.0,
            "unrealizedProfit": 2.0,
        },
    )
    _test("health exit warning propagates", snap["operating_state"] == "EXIT_WARNING")
    _test("health warning is surfaced", "HEALTH_EXIT_WARNING" in snap["warnings"])

    no_position = dict(base)
    no_position["exchange_sync_status"] = "NOT_FOUND"
    no_position["status"] = "triggered"
    snap = build_position_intelligence(no_position)
    _test("NOT_FOUND is never active", snap["active"] is False)

    print("[PASS] all position-intelligence tests")
    return True


if __name__ == "__main__":
    run_tests()
