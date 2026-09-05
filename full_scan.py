#!/usr/bin/env python3
"""
Unattended full-market scanner for smc_scanner.py.

Designed for GitHub Actions / cron and for repeated ~15-minute scans.
Uses the Phase 1-4.5 pipeline exposed by the current smc_scanner module:
  regime -> setup -> structural R:R -> setup/entry quality -> execution state
  -> lifecycle/deduplication -> actionable ranking.

BingX position synchronization and ATHENA position health are layered on top
of the existing setup scanner. They do not modify SMC setup generation.
"""

import sys
import time
from datetime import datetime, timezone

import smc_scanner as scanner
import derivatives_monitor as derivatives
import bingx_position_tracker as bingx
import position_health as health


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

    header = (
        f"🆕 NEW SETUP: {symbol} {direction} [{plan.get('trade_type', 'INTRADAY')}]"
        if is_new else
        f"🔄 ACTIVE SETUP UPDATE: {symbol} {direction} [{plan.get('trade_type', 'INTRADAY')}]"
    )
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
        lines.append(
            f"Zone: {zone_low:.8g} - {zone_high:.8g} "
            f"({plan.get('zone_label', 'structural')})"
        )

    if invalidation is not None:
        lines.append(f"SL / Invalidation: {invalidation:.8g}")

    targets = plan.get("validated_targets") or []
    if targets:
        for i, target in enumerate(targets[:3], 1):
            lines.append(
                f"TP{i}: {target['price']:.8g} "
                f"(~{target.get('r', 0):.2f}R)"
            )
    else:
        lines.append("TP: no validated structural target")

    dctx = plan.get("derivatives_context") or {}
    if dctx:
        dstate = dctx.get("state", "NEUTRAL")
        dscore = dctx.get("score")
        dreason = dctx.get("reason")

        lines.append(
            f"Derivatives: {dstate}" +
            (f" ({dscore:+.0f})"
             if isinstance(dscore, (int, float)) else "")
        )

        if dreason:
            lines.append(f"Derivatives Context: {dreason}")

    lines.extend([
        f"Setup Quality: {plan.get('setup_quality', 0):.0f}",
        f"Entry Quality: {plan.get('entry_quality', 0):.0f}",
        f"Structural R:R: {plan.get('structural_rr', 0):.2f}",
        f"Regime: {plan.get('regime', 'n/a')} / "
        f"{plan.get('trend_alignment', 'n/a')}",
    ])

    return "\n".join(lines)


def _format_health_alert(entry, snapshot):
    symbol = entry.get("symbol", "?")
    direction = entry.get("direction", "?")
    state = snapshot.get("health_state", "UNKNOWN")
    prev = entry.get("position_health_previous_state") or "INITIAL"
    reason = snapshot.get("reason", "")
    current_r = snapshot.get(
        "current_r",
        entry.get("current_r", 0.0)
    )
    max_r = snapshot.get(
        "max_r",
        entry.get("max_r", 0.0)
    )

    if state == "RECOVERY":
        prefix = "🟢"
        action = (
            "Risk is improving; continue monitoring "
            "the existing position."
        )
    elif state == "EXIT_WARNING":
        prefix = "🔴"
        action = (
            "Protect / evaluate exit; this is an existing "
            "position, not a new entry."
        )
    elif state == "ELEVATED_RISK":
        prefix = "🟠"
        action = (
            "Risk is elevated; monitor closely. "
            "Existing position only."
        )
    elif state == "CAUTION":
        prefix = "⚠️"
        action = (
            "Caution; existing position only, "
            "not a new entry signal."
        )
    else:
        prefix = "🟢"
        action = "Existing position remains healthy."

    return (
        f"{prefix} POSITION HEALTH: {symbol} {direction}\n"
        f"State: {state} (from {prev})\n"
        f"Exchange: BingX OPEN\n"
        f"Current R: {float(current_r):+.2f}R | "
        f"Max R: {float(max_r):+.2f}R\n"
        f"Why: {reason}\n"
        f"Action: {action}"
    )


def _format_close_alert(entry):
    symbol = entry.get("symbol", "?")
    direction = entry.get("direction", "?")
    reason = entry.get("position_exit_reason", "CLOSED")
    price = entry.get("exchange_close_price")
    closed_at = entry.get("exchange_closed_at", "unknown")

    price_text = (
        f" at {float(price):.8g}"
        if isinstance(price, (int, float))
        else ""
    )

    return (
        f"🔔 POSITION CLOSED: {symbol} {direction}\n"
        f"Exit reason: {reason}{price_text}\n"
        f"BingX closed at: {closed_at}\n"
        "This position is no longer actively monitored.\n"
        "Historical record retained; this is NOT a new entry signal."
    )


def _prepare_watchlist_entry(it):
    """Back-fill legacy fields exactly where scanner.check_watchlist would."""
    it.setdefault("status", "pending")
    it.setdefault(
        "entries",
        [{
            "price": it.get("price", 0),
            "size_pct": 100,
            "filled": False
        }]
    )
    it.setdefault("trail_mode", "fixed")
    it.setdefault(
        "original_invalidation",
        it.get("invalidation")
    )
    it.setdefault(
        "current_score",
        it.get("score", it.get("added_score", 0))
    )
    it.setdefault(
        "added_score",
        it.get("current_score", 0)
    )
    it.setdefault("history", [])
    it.setdefault(
        "added_at",
        datetime.now(timezone.utc).isoformat()
    )
    it.setdefault("targets", it.get("targets", []))
    it.setdefault("entry_instruction", None)
    it.setdefault("entry_price", None)
    it.setdefault("max_r", 0.0)
    it.setdefault("max_favorable_price", None)
    it.setdefault(
        "peak_score",
        it.get("current_score", it.get("added_score", 0))
    )
    it.setdefault("last_warning", None)
    it.setdefault("reversal_state", "STABLE")
    it.setdefault(
        "reversal_reason",
        "No active reversal evidence detected."
    )
    it.setdefault("reversal_alert_state", "STABLE")
    it.setdefault("current_r", 0.0)
    it.setdefault("setup_fingerprint", None)
    it.setdefault("trade_type", "INTRADAY")
    it.setdefault("attempt_num", 1)
    it.setdefault("lineage_note", None)
    it.setdefault("dead_reported", False)
    it.setdefault("position_close_reported", False)
    it.setdefault("last_score_delta_pct", 0.0)
    it.setdefault("last_score_arrow", "flat")

    for key in (
        "triggered_at",
        "invalidated_at",
        "expired_at",
        "expire_reason",
    ):
        it.setdefault(key, None)


def _sync_watchlist(active_key):
    """Synchronize persisted positions before local watchlist refresh.

    Only confirmed BingX OPEN positions enter active health monitoring.
    CLOSED positions are retained but are not passed back through the scanner's
    triggered-position refresh, preventing local setup logic from rewriting a
    confirmed exchange closure.
    """
    try:
        items = scanner.load_watchlist()
    except Exception as exc:
        print(
            f"! Could not load watchlist: "
            f"{type(exc).__name__}: {exc}"
        )
        return

    if not items:
        print("Watchlist is empty.")
        return

    # Keep the scanner's historical pruning behavior for old, already-reported
    # setup deaths. Exchange-closed positions are never pruned here.
    before = len(items)

    items = [
        it for it in items
        if not (
            it.get("dead_reported")
            and it.get("status") in ("invalidated", "expired")
            and scanner._hours_from_now(
                it.get("invalidated_at")
                or it.get("expired_at")
            ) >= scanner.REENTRY_HISTORY_HOURS
        )
    ]

    if before != len(items):
        print(
            f"(pruned {before - len(items)} "
            f"previously-reported dead setup(s))"
        )

    for it in items:
        _prepare_watchlist_entry(it)
        prev_status = it.get("status")

        # Non-triggered setups remain under the original SMC watchlist engine.
        if prev_status != "triggered":
            try:
                scanner.refresh_entry(it, active_key)
                time.sleep(0.1)

                if it.get("status") != prev_status:
                    scanner._notify_status_change(
                        it,
                        prev_status
                    )

                    if it.get("status") in (
                        "invalidated",
                        "expired",
                    ):
                        it["dead_reported"] = True

            except Exception as exc:
                scanner._log(
                    it,
                    f"Refresh error, left as-is: {exc}"
                )

            continue

        # Position lifecycle is now exchange-authoritative.
        #
        # IMPORTANT:
        # bingx.sync_position() returns a dictionary, not the old
        # SYNC_OPEN / SYNC_CLOSED constants.
        sync_result = bingx.sync_position(it)
        sync_state = str(
            (sync_result or {}).get("state", "UNKNOWN")
        ).upper()

        if sync_state == "OPEN":
            bingx.freeze_fill_if_needed(it)

            try:
                scanner.refresh_entry(it, active_key)
                time.sleep(0.1)

            except Exception as exc:
                scanner._log(
                    it,
                    f"Active refresh error, "
                    f"exchange state preserved: {exc}"
                )

            # Re-sync after local refresh. If the exchange closed during the
            # refresh window, the final exchange state wins.
            sync_result = bingx.sync_position(it)
            sync_state = str(
                (sync_result or {}).get("state", "UNKNOWN")
            ).upper()

            if sync_state == "OPEN":
                bingx.freeze_fill_if_needed(it)

                dctx = None

                # Derivatives context is informational only.
                try:
                    dctx = health.get_derivatives_context(
                        it.get("symbol")
                    )
                except Exception:
                    dctx = None

                snapshot, transitioned = health.apply_health(
                    it,
                    dctx
                )

                if transitioned:
                    message = _format_health_alert(
                        it,
                        snapshot
                    )

                    print("\n" + "-" * 78)
                    print("POSITION HEALTH ALERT")
                    print(message)
                    print("-" * 78)

                    if scanner.send_telegram_message(message):
                        print("  Telegram health alert sent.")

            elif sync_state == "CLOSED":
                if not it.get("position_close_reported"):
                    message = _format_close_alert(it)

                    print("\n" + "-" * 78)
                    print("POSITION CLOSED ALERT")
                    print(message)
                    print("-" * 78)

                    if scanner.send_telegram_message(message):
                        print("  Telegram closure alert sent.")

                    it["position_close_reported"] = True

            continue

        if sync_state == "CLOSED":
            if not it.get("position_close_reported"):
                message = _format_close_alert(it)

                print("\n" + "-" * 78)
                print("POSITION CLOSED ALERT")
                print(message)
                print("-" * 78)

                if scanner.send_telegram_message(message):
                    print("  Telegram closure alert sent.")

                it["position_close_reported"] = True

            # IMPORTANT: do not call refresh_entry here. The position is closed
            # on BingX; retain the historical record and stop active monitoring.
            continue

        # UNKNOWN / ERROR / NOT_FOUND / NOT_MATCHED are explicitly non-open.
        # Do not produce active health, and do not treat the entry as live.
        print(
            f"  {it.get('symbol', '?')} "
            f"{it.get('direction', '?')}: "
            f"BingX sync={sync_state}; "
            f"active position monitoring suppressed."
        )

    scanner.save_watchlist(items)


def scan_all(active_key, symbols):
    """Run the SMC pipeline with derivatives as non-invasive context."""
    qualifying = []

    derivatives_data = derivatives.monitor(
        symbols=symbols
    )

    derivatives_results = (
        derivatives_data.get("results", {})
        if isinstance(derivatives_data, dict)
        else {}
    )

    if (
        not isinstance(derivatives_data, dict)
        or not derivatives_data.get("bulk_fetch_ok", False)
    ):
        print(
            "Derivatives intelligence unavailable this cycle; "
            "continuing SMC-only."
        )

    scan_cycle = int(time.time() // 900)

    total = len(symbols)

    for n, symbol in enumerate(symbols, 1):
        print(
            f"\nScanning {symbol} ({n}/{total})..."
        )

        try:
            tf_results, used = scanner.scan_symbol(
                active_key,
                symbol
            )

            score, direction, regime_info = (
                scanner.score_setup_with_regime(
                    tf_results
                )
            )

            if score < scanner.MIN_SETUP_SCORE or not direction:
                continue

            plan = scanner.build_entry_plan(
                tf_results,
                direction,
                regime_info
            )

            if not plan:
                continue

            if regime_info:
                plan.update(regime_info)

            exec_state = scanner.determine_execution_state(
                plan,
                tf_results,
                direction
            )
            plan.update(exec_state)

            lifecycle = scanner.update_setup_lifecycle(
                symbol,
                plan,
                scan_cycle
            )
            plan["lifecycle_info"] = lifecycle

            # Context only: NEVER modify SMC score, direction, entry, SL, TP,
            # execution state, or trade classification.
            dctx = derivatives_results.get(symbol)

            if dctx:
                plan["derivatives_context"] = {
                    "state": dctx.get("state"),
                    "score": dctx.get("score"),
                    "reason": dctx.get("reason"),
                    "since": dctx.get("since"),
                    "is_new_transition": bool(
                        dctx.get(
                            "is_new_transition",
                            False
                        )
                    ),
                }

            qualifying.append(
                (
                    symbol,
                    tf_results,
                    used,
                    score,
                    direction,
                    plan,
                )
            )

        except Exception as exc:
            print(
                f"  ! {symbol}: scan error: "
                f"{type(exc).__name__}: {exc}"
            )

    return qualifying


def main():
    started = time.time()

    print("=" * 78)
    print("FULL MARKET SCAN — SMC PHASE 1-4.5")
    print(
        f"Started: "
        f"{datetime.now(timezone.utc).isoformat()}"
    )
    print("=" * 78)

    active_key = scanner.detect_active_exchange()

    if not active_key:
        print(
            "ERROR: no supported exchange is reachable."
        )
        return 1

    used_key, symbols = scanner.get_symbols_with_fallback(
        active_key,
        MAX_SYMBOLS
    )

    if not symbols:
        print(
            "ERROR: could not retrieve the market list."
        )
        return 1

    symbols = symbols[:MAX_SYMBOLS]

    print(
        f"Exchange: {used_key or active_key}"
    )
    print(
        f"Markets: {len(symbols)}"
    )
    print(
        f"Timeframes: {', '.join(scanner.TFS_ALL)}"
    )

    # Exchange synchronization must happen before the final watchlist refresh,
    # so confirmed closures cannot be reinterpreted as active setup changes.
    _sync_watchlist(active_key)

    qualifying = scan_all(
        active_key,
        symbols
    )

    buckets = scanner.classify_and_rank(
        [q[5] for q in qualifying]
    )

    ready = buckets["READY_NOW"]
    near = buckets["NEAR_READY"]
    waiting = buckets["WAITING"]
    invalidated = buckets["INVALIDATED"]
    no_trade = buckets["NO_TRADE"]

    print("\n" + "#" * 78)
    print("FINAL SCAN RESULT")
    print("#" * 78)

    print(
        f"Markets scanned : {len(symbols)}"
    )
    print(
        f"Setup-qualified : {len(qualifying)}"
    )
    print(
        f"READY NOW       : {len(ready)}"
    )
    print(
        f"NEAR READY      : {len(near)}"
    )
    print(
        f"WAITING         : {len(waiting)}"
    )
    print(
        f"INVALIDATED     : {len(invalidated)}"
    )
    print(
        f"NO TRADE        : {len(no_trade)}"
    )

    if ready:
        print("\nTOP ACTIONABLE SETUPS")

        for rank, plan in enumerate(
            ready[:TOP_ACTIONABLE_TO_PRINT],
            1
        ):
            print(
                f"{rank}. "
                f"{plan.get('symbol', '?')} "
                f"{plan.get('direction', '?')} "
                f"{plan.get('status')} | "
                f"SQ={plan.get('setup_quality', 0):.0f} "
                f"EQ={plan.get('entry_quality', 0):.0f} "
                f"SRR={plan.get('structural_rr', 0):.2f}"
            )

    by_plan_id = {
        id(q[5]): q
        for q in qualifying
    }

    alerts_sent = 0
    auto_added = 0

    for plan in ready:
        match = by_plan_id.get(
            id(plan)
        )

        if not match:
            continue

        (
            symbol,
            _tf_results,
            used,
            score,
            direction,
            plan,
        ) = match

        lifecycle = plan.get(
            "lifecycle_info",
            {}
        )

        if lifecycle.get("send_alert"):
            message = _format_alert(
                symbol,
                used,
                score,
                direction,
                plan,
                is_new=bool(
                    lifecycle.get("is_new")
                ),
                reason=lifecycle.get("reason"),
            )

            print("\n" + "-" * 78)
            print("ALERT")
            print(message)
            print("-" * 78)

            if scanner.send_telegram_message(message):
                alerts_sent += 1

       
