#!/usr/bin/env python3
"""
Unattended full-market scanner for smc_scanner.py.

Designed for GitHub Actions / cron and for repeated ~15-minute scans.
Uses the Phase 1-4.5 pipeline exposed by the current smc_scanner module:
  regime -> setup -> structural R:R -> setup/entry quality -> execution state
  -> lifecycle/deduplication -> actionable ranking.

No interactive input is used.
"""

import sys
import time
from datetime import datetime, timezone

import smc_scanner as scanner


MAX_SYMBOLS = 150
TOP_ACTIONABLE_TO_PRINT = 10


def _is_auto_add_candidate(score, plan):
    """Apply the scanner's unattended watchlist policy."""
    trade_type = str(plan.get("trade_type", "INTRADAY")).upper()
    if trade_type in getattr(scanner, "AUTO_ADD_EXCLUDE_TYPES", set()):
        return False

    if score < getattr(scanner, "AUTO_ADD_MIN_SCORE", 90):
        return False

    if getattr(scanner, "AUTO_ADD_READY_ONLY", True):
        return plan.get("status") in ("READY_MARKET", "READY_LIMIT")

    return True


def _format_alert(symbol, used, score, direction, plan, is_new=True, reason=None):
    status = plan.get("status", "UNKNOWN")
    execution_type = plan.get("execution_type", "UNKNOWN")
    price = plan.get("current_price")
    entry = plan.get("preferred_entry")
    zone_low = plan.get("zone_low")
    zone_high = plan.get("zone_high")
    invalidation = plan.get("invalidation")

    if is_new:
        header = f"🆕 NEW SETUP: {symbol} {direction} [{plan.get('trade_type', 'INTRADAY')}]"
        score_line = f"Current Score: {score:.0f}/100"
    else:
        header = f"🔄 ACTIVE SETUP UPDATE: {symbol} {direction} [{plan.get('trade_type', 'INTRADAY')}]"
        score_line = f"Current Score: {score:.0f}/100"

    lines = [
        header,
        score_line,
        "Entry signal: FRESH" if is_new else "Already tracked -- NOT a new entry signal.",
        f"Source: {used.get(scanner.ENTRY_TF) or 'fallback exchange'}",
        f"✅ {status}",
        f"Execution: {execution_type}",
    ]

    if price is not None:
        lines.append(f"Current: {price:.8g}")
    if entry is not None:
        lines.append(f"Preferred Entry: {entry:.8g}")
    if zone_low is not None and zone_high is not None:
        lines.append(f"Zone: {zone_low:.8g} - {zone_high:.8g} ({plan.get('zone_label', 'structural')})")
    if invalidation is not None:
        lines.append(f"SL / Invalidation: {invalidation:.8g}")

    targets = plan.get("validated_targets") or []
    if targets:
        for i, target in enumerate(targets[:3], 1):
            lines.append(
                f"TP{i}: {target['price']:.8g} (~{target.get('r', 0):.2f}R)"
            )
    else:
        lines.append("TP: no validated structural target")

    lines.extend([
        f"Setup Quality: {plan.get('setup_quality', 0):.0f}",
        f"Entry Quality: {plan.get('entry_quality', 0):.0f}",
        f"Structural R:R: {plan.get('structural_rr', 0):.2f}",
        f"Regime: {plan.get('regime', 'n/a')} / {plan.get('trend_alignment', 'n/a')}",
    ])
    return "\n".join(lines)


def scan_all(active_key, symbols):
    """Run the real Phase 1-4.5 pipeline without calling interactive run_scan()."""
    qualifying = []
    scan_cycle = int(time.time() // 900)

    total = len(symbols)
    for n, symbol in enumerate(symbols, 1):
        print(f"\nScanning {symbol} ({n}/{total})...")
        try:
            tf_results, used = scanner.scan_symbol(active_key, symbol)
            score, direction, regime_info = scanner.score_setup_with_regime(tf_results)

            if score < scanner.MIN_SETUP_SCORE or not direction:
                continue

            plan = scanner.build_entry_plan(tf_results, direction, regime_info)
            if not plan:
                continue

            if regime_info:
                plan.update(regime_info)

            exec_state = scanner.determine_execution_state(plan, tf_results, direction)
            plan.update(exec_state)

            lifecycle = scanner.update_setup_lifecycle(symbol, plan, scan_cycle)
            plan["lifecycle_info"] = lifecycle

            qualifying.append((symbol, tf_results, used, score, direction, plan))

        except Exception as exc:
            # One bad symbol must never kill the 150-market scan.
            print(f"  ! {symbol}: scan error: {type(exc).__name__}: {exc}")

    return qualifying


def main():
    started = time.time()
    print("=" * 78)
    print("FULL MARKET SCAN — SMC PHASE 1-4.5")
    print(f"Started: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 78)

    active_key = scanner.detect_active_exchange()
    if not active_key:
        print("ERROR: no supported exchange is reachable.")
        return 1

    used_key, symbols = scanner.get_symbols_with_fallback(active_key, MAX_SYMBOLS)
    if not symbols:
        print("ERROR: could not retrieve the market list.")
        return 1

    symbols = symbols[:MAX_SYMBOLS]
    print(f"Exchange: {used_key or active_key}")
    print(f"Markets: {len(symbols)}")
    print(f"Timeframes: {', '.join(scanner.TFS_ALL)}")

    qualifying = scan_all(active_key, symbols)
    buckets = scanner.classify_and_rank([q[5] for q in qualifying])

    ready = buckets["READY_NOW"]
    near = buckets["NEAR_READY"]
    waiting = buckets["WAITING"]
    invalidated = buckets["INVALIDATED"]
    no_trade = buckets["NO_TRADE"]

    print("\n" + "#" * 78)
    print("FINAL SCAN RESULT")
    print("#" * 78)
    print(f"Markets scanned : {len(symbols)}")
    print(f"Setup-qualified : {len(qualifying)}")
    print(f"READY NOW       : {len(ready)}")
    print(f"NEAR READY      : {len(near)}")
    print(f"WAITING         : {len(waiting)}")
    print(f"INVALIDATED     : {len(invalidated)}")
    print(f"NO TRADE        : {len(no_trade)}")

    if ready:
        print("\nTOP ACTIONABLE SETUPS")
        for rank, plan in enumerate(ready[:TOP_ACTIONABLE_TO_PRINT], 1):
            print(
                f"{rank}. {plan.get('symbol', '?')} {plan.get('direction', '?')} "
                f"{plan.get('status')} | SQ={plan.get('setup_quality', 0):.0f} "
                f"EQ={plan.get('entry_quality', 0):.0f} "
                f"SRR={plan.get('structural_rr', 0):.2f}"
            )

    # Map the exact plan object back to its scan tuple. A plan may not carry
    # its symbol as a field, so identity is safer than guessing from text.
    by_plan_id = {id(q[5]): q for q in qualifying}
    alerts_sent = 0
    auto_added = 0

    for plan in ready:
        match = by_plan_id.get(id(plan))
        if not match:
            continue

        symbol, _tf_results, used, score, direction, plan = match
        lifecycle = plan.get("lifecycle_info", {})

        # Phase 4 lifecycle is the alert authority. An unchanged setup stays silent.
        if lifecycle.get("send_alert"):
            message = _format_alert(
                symbol, used, score, direction, plan,
                is_new=bool(lifecycle.get("is_new")),
                reason=lifecycle.get("reason"),
            )
            print("\n" + "-" * 78)
            print("ALERT")
            print(message)
            print("-" * 78)
            if scanner.send_telegram_message(message):
                alerts_sent += 1

        if _is_auto_add_candidate(score, plan):
            try:
                exchange_for_watchlist = used.get(scanner.ENTRY_TF) or active_key
                scanner.add_to_watchlist(
                    symbol,
                    exchange_for_watchlist,
                    score,
                    direction,
                    plan,
                    trail_mode=getattr(scanner, "AUTO_TRAIL_MODE", "fixed"),
                    split_entries=None,
                )
                auto_added += 1
            except Exception as exc:
                print(f"  ! auto-add failed for {symbol}: {type(exc).__name__}: {exc}")

    # Persisted watchlist v2 is still refreshed independently from setup lifecycle.
    # This keeps triggered/pending entries alive across scheduled runs.
    try:
        scanner.check_watchlist(active_key)
    except Exception as exc:
        print(f"! Watchlist refresh failed: {type(exc).__name__}: {exc}")

    elapsed = time.time() - started
    print("\n" + "=" * 78)
    print(f"Alerts sent      : {alerts_sent}")
    print(f"Auto-added       : {auto_added}")
    print(f"Elapsed          : {elapsed / 60:.1f} minutes")
    print(f"Finished         : {datetime.now(timezone.utc).isoformat()}")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
