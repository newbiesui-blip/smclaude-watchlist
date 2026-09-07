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

import os
import sys
import time
from datetime import datetime, timezone

import smc_scanner as scanner
import derivatives_monitor as derivatives
import bingx_position_tracker as bingx
import position_health as health
import position_intelligence as position_intel
from market_data_aggregator import MarketDataAggregator
from market_intelligence import AssetIdentifiers, MarketIntelligence

try:
    from coinmarketcap_client import CoinMarketCapClient
except Exception:
    CoinMarketCapClient = None

try:
    from cryptorank_client import CryptoRankClient
except Exception:
    CryptoRankClient = None


MAX_SYMBOLS = 150
TOP_ACTIONABLE_TO_PRINT = 10
MARKET_INTELLIGENCE_MAX_ASSETS = 10
POSITION_INTELLIGENCE_ALERT_STATES = {
    "EXIT_WARNING",
    "ELEVATED_RISK",
    "TAKE_PARTIAL_CONSIDERATION",
    "BREAK_EVEN_ELIGIBLE",
    "CAUTION",
    "RECOVERY",
}

# Position intelligence also needs to report material movement while the
# operating state remains HEALTHY. These thresholds are notification-only;
# they never alter execution, orders, SL, TP, or scanner decisions.
POSITION_INTELLIGENCE_MATERIAL_R_DELTA = 0.50


def _is_auto_add_candidate(score, plan):
    """Apply the unattended watchlist policy for exchange-position monitoring.

    Every actionable setup type is eligible here, including SCALP. The
    watchlist is read-only position intelligence: excluding SCALP at this
    layer meant a real BingX position could exist without ever entering the
    exchange discovery/synchronization pipeline. Scanner-level exclusions
    remain untouched; they do not govern position monitoring.
    """
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
        lines.append(f"Zone: {zone_low:.8g} - {zone_high:.8g} ({plan.get('zone_label', 'structural')})")
    if invalidation is not None:
        lines.append(f"SL / Invalidation: {invalidation:.8g}")

    targets = plan.get("validated_targets") or []
    if targets:
        for i, target in enumerate(targets[:3], 1):
            lines.append(f"TP{i}: {target['price']:.8g} (~{target.get('r', 0):.2f}R)")
    else:
        lines.append("TP: no validated structural target")

    dctx = plan.get("derivatives_context") or {}
    if dctx:
        dstate = dctx.get("state", "NEUTRAL")
        dscore = dctx.get("score")
        dreason = dctx.get("reason")
        lines.append(
            f"Derivatives: {dstate}" +
            (f" ({dscore:+.0f})" if isinstance(dscore, (int, float)) else "")
        )
        if dreason:
            lines.append(f"Derivatives Context: {dreason}")

    lines.extend([
        f"Setup Quality: {plan.get('setup_quality', 0):.0f}",
        f"Entry Quality: {plan.get('entry_quality', 0):.0f}",
        f"Structural R:R: {plan.get('structural_rr', 0):.2f}",
        f"Regime: {plan.get('regime', 'n/a')} / {plan.get('trend_alignment', 'n/a')}",
    ])
    return "\n".join(lines)


def _format_health_alert(entry, snapshot):
    symbol = entry.get("symbol", "?")
    direction = entry.get("direction", "?")
    state = snapshot.get("health_state", "UNKNOWN")
    prev = entry.get("position_health_previous_state") or "INITIAL"
    reason = snapshot.get("reason", "")
    current_r = snapshot.get("current_r", entry.get("current_r", 0.0))
    max_r = snapshot.get("max_r", entry.get("max_r", 0.0))

    if state == "RECOVERY":
        prefix = "🟢"
        action = "Risk is improving; continue monitoring the existing position."
    elif state == "EXIT_WARNING":
        prefix = "🔴"
        action = "Protect / evaluate exit; this is an existing position, not a new entry."
    elif state == "ELEVATED_RISK":
        prefix = "🟠"
        action = "Risk is elevated; monitor closely. Existing position only."
    elif state == "CAUTION":
        prefix = "⚠️"
        action = "Caution; existing position only, not a new entry signal."
    else:
        prefix = "🟢"
        action = "Existing position remains healthy."

    return (
        f"{prefix} POSITION HEALTH: {symbol} {direction}\n"
        f"State: {state} (from {prev})\n"
        f"Exchange: BingX OPEN\n"
        f"Current R: {float(current_r):+.2f}R | Max R: {float(max_r):+.2f}R\n"
        f"Why: {reason}\n"
        f"Action: {action}"
    )


def _format_position_open_alert(entry, snapshot, discovered=False):
    """Format a one-time confirmation that BingX has an active position."""
    symbol = entry.get("symbol", "?")
    direction = entry.get("direction", "?")
    pnl = snapshot.get("unrealized_pnl")
    pnl_source = snapshot.get("unrealized_pnl_source")
    current_r = snapshot.get("current_r")
    duration = snapshot.get("duration")
    entry_price = snapshot.get("entry_price")
    current_price = snapshot.get("current_price")

    lines = [
        f"🟢 POSITION OPEN: {symbol} {direction}",
        "Exchange: BingX OPEN",
        "Discovery: existing exchange position confirmed." if discovered else "Discovery: local triggered position confirmed OPEN.",
    ]
    if entry_price is not None:
        lines.append(f"Entry: {entry_price:.8g}")
    if current_price is not None:
        lines.append(f"Current: {current_price:.8g}")
    if pnl is not None:
        lines.append(
            f"Unrealized P&L: {pnl:+.4f}"
            + (f" ({pnl_source})" if pnl_source else "")
        )
    if current_r is not None:
        lines.append(f"R: {current_r:+.2f}R")
    if duration:
        lines.append(f"Time in trade: {duration}")
    lines.append("ATHENA is now actively monitoring this existing position.")
    lines.append("No order/SL/TP modification is performed.")
    return "\n".join(lines)


def _format_close_alert(entry):
    symbol = entry.get("symbol", "?")
    direction = entry.get("direction", "?")
    reason = entry.get("position_exit_reason", "CLOSED")
    price = entry.get("exchange_close_price")
    closed_at = entry.get("exchange_closed_at", "unknown")
    realized_pnl = entry.get("exchange_realized_pnl")
    close_source = entry.get("exchange_close_source", "BingX")
    position_id = entry.get("exchange_position_id")
    price_text = f" at {float(price):.8g}" if isinstance(price, (int, float)) else ""
    pnl_text = (
        f"\nRealized P&L: {float(realized_pnl):+.4f} USDT"
        if isinstance(realized_pnl, (int, float))
        else (f"\nRealized P&L: {realized_pnl}" if realized_pnl not in (None, "") else "")
    )
    duration_ms = None
    start = entry.get("triggered_at") or entry.get("opened_at") or entry.get("position_opened_at")
    if start and closed_at not in (None, "", "unknown"):
        try:
            start_ms = position_intel._timestamp_ms(start)
            end_ms = position_intel._timestamp_ms(closed_at)
            if start_ms is not None and end_ms is not None:
                duration_ms = max(0, end_ms - start_ms)
        except Exception:
            duration_ms = None
    duration_text = (
        f"\nTime in trade: {position_intel.format_duration(duration_ms)}"
        if duration_ms is not None
        else ""
    )
    id_text = f"\nPosition ID: {position_id}" if position_id else ""
    return (
        f"🔴 POSITION CLOSED: {symbol} {direction}\n"
        f"Exit reason: {reason}{price_text}"
        f"{pnl_text}"
        f"{duration_text}"
        f"\nClose source: {close_source}"
        f"{id_text}\n"
        f"BingX closed at: {closed_at}\n"
        "This position is no longer actively monitored.\n"
        "Historical record retained; this is NOT a new entry signal."
    )


def _format_position_intelligence_alert(entry, snapshot, previous_state=None):
    """Format a meaningful position-intelligence transition for Telegram."""
    transition = previous_state or "INITIAL"
    return (
        f"🔔 POSITION INTELLIGENCE UPDATE: {entry.get('symbol', '?')} "
        f"{entry.get('direction', '?')}\n"
        f"Transition: {transition} → {snapshot.get('operating_state', 'UNKNOWN')}\n\n"
        + position_intel.format_position_intelligence(snapshot)
    )


def _material_position_move(previous_snapshot, snapshot):
    """Return a notification reason for material movement since last alert.

    The comparison is deliberately against the last *alerted* snapshot, not
    the immediately previous 15-minute scan. That prevents a large move from
    being hidden by repeated small scan-to-scan changes.
    """
    if not isinstance(previous_snapshot, dict):
        return None

    old_r = previous_snapshot.get("current_r")
    new_r = snapshot.get("current_r")
    if old_r is not None and new_r is not None:
        try:
            r_delta = float(new_r) - float(old_r)
            if abs(r_delta) >= POSITION_INTELLIGENCE_MATERIAL_R_DELTA:
                return f"R moved {r_delta:+.2f}R since last intelligence alert"
        except (TypeError, ValueError):
            pass

    return None


def _maybe_send_position_intelligence(entry, sync_result, health_snapshot):
    """Build intelligence and alert on state changes or material movement."""
    exchange_position = (sync_result or {}).get("position") if isinstance(sync_result, dict) else None
    snapshot = position_intel.build_position_intelligence(
        entry,
        exchange_position=exchange_position,
        health_snapshot=health_snapshot,
    )
    state = snapshot.get("operating_state", "UNKNOWN")
    previous_state = entry.get("position_intelligence_state")
    last_alert_snapshot = entry.get("position_intelligence_alert_snapshot")

    entry["position_intelligence_state"] = state

    state_transition = (
        previous_state != state
        and state in POSITION_INTELLIGENCE_ALERT_STATES
        and not (previous_state is None and state in {"CAUTION", "RECOVERY"})
    )
    material_reason = _material_position_move(last_alert_snapshot, snapshot)

    # A state transition remains the primary alert path. HEALTHY positions
    # can now also alert when P&L/R has moved materially since the last alert.
    should_alert = state_transition or material_reason is not None

    if not should_alert:
        # Establish the first snapshot as a baseline without sending an
        # unsolicited HEALTHY message.
        if last_alert_snapshot is None:
            entry["position_intelligence_alert_snapshot"] = snapshot
        return snapshot, False

    transition = previous_state
    message = _format_position_intelligence_alert(entry, snapshot, transition)
    if material_reason and not state_transition:
        message = (
            f"🔔 POSITION INTELLIGENCE MOVEMENT: {entry.get('symbol', '?')} "
            f"{entry.get('direction', '?')}\n"
            f"Reason: {material_reason}\n\n"
            + position_intel.format_position_intelligence(snapshot)
        )
    print("\n" + "-" * 78)
    print("POSITION INTELLIGENCE ALERT")
    print(message)
    print("-" * 78)
    sent = bool(scanner.send_telegram_message(message))
    if sent:
        entry["position_intelligence_alert_snapshot"] = snapshot
        print("  Telegram position-intelligence alert sent.")
    return snapshot, sent


def _prepare_watchlist_entry(it):
    """Back-fill legacy fields exactly where scanner.check_watchlist would."""
    it.setdefault("status", "pending")
    it.setdefault("entries", [{"price": it.get("price", 0), "size_pct": 100, "filled": False}])
    it.setdefault("trail_mode", "fixed")
    it.setdefault("original_invalidation", it.get("invalidation"))
    it.setdefault("current_score", it.get("score", it.get("added_score", 0)))
    it.setdefault("added_score", it.get("current_score", 0))
    it.setdefault("history", [])
    it.setdefault("added_at", datetime.now(timezone.utc).isoformat())
    it.setdefault("targets", it.get("targets", []))
    it.setdefault("entry_instruction", None)
    it.setdefault("entry_price", None)
    it.setdefault("max_r", 0.0)
    it.setdefault("max_favorable_price", None)
    it.setdefault("peak_score", it.get("current_score", it.get("added_score", 0)))
    it.setdefault("last_warning", None)
    it.setdefault("reversal_state", "STABLE")
    it.setdefault("reversal_reason", "No active reversal evidence detected.")
    it.setdefault("reversal_alert_state", "STABLE")
    it.setdefault("current_r", 0.0)
    it.setdefault("setup_fingerprint", None)
    it.setdefault("trade_type", "INTRADAY")
    it.setdefault("attempt_num", 1)
    it.setdefault("lineage_note", None)
    it.setdefault("dead_reported", False)
    it.setdefault("position_close_reported", False)
    it.setdefault("position_open_reported", False)
    it.setdefault("last_score_delta_pct", 0.0)
    it.setdefault("last_score_arrow", "flat")
    it.setdefault("position_intelligence_state", None)
    it.setdefault("position_intelligence_snapshot", None)
    it.setdefault("position_intelligence_alert_snapshot", None)
    for key in ("triggered_at", "invalidated_at", "expired_at", "expire_reason"):
        it.setdefault(key, None)


def _base_asset_symbol(symbol):
    """Return the exchange-independent base symbol without guessing a source ID."""
    value = str(symbol or "").upper().strip()
    for suffix in ("-SWAP", "_USDT", "-USDT", "USDT"):
        if value.endswith(suffix):
            value = value[:-len(suffix)]
            break
    return value


def _build_market_intelligence():
    """Build the read-only MI layer from installed source adapters."""
    clients = {}
    if CoinMarketCapClient is not None:
        try:
            clients["coinmarketcap"] = CoinMarketCapClient()
        except Exception as exc:
            print(f"  ! CoinMarketCap client unavailable: {type(exc).__name__}: {exc}")
    if CryptoRankClient is not None:
        try:
            clients["cryptorank"] = CryptoRankClient()
        except Exception as exc:
            print(f"  ! CryptoRank client unavailable: {type(exc).__name__}: {exc}")

    return MarketIntelligence(
        aggregator=MarketDataAggregator(),
        coinmarketcap_client=clients.get("coinmarketcap"),
        cryptorank_client=clients.get("cryptorank"),
    )


def _market_intelligence_snapshot(mi, symbol):
    """Fetch corroborating market context without touching SMC/trade fields.

    CMC and CryptoRank use the normalized trading base symbol only as their
    explicit source-symbol selector. CoinGecko, ETF, and pool sources remain
    opt-in and are not guessed from an exchange symbol.
    """
    base = _base_asset_symbol(symbol)
    return mi.get_snapshot(
        AssetIdentifiers(
            symbol=base,
            cmc_symbol=base,
            cryptorank_symbol=base,
        )
    )


def _attach_market_intelligence(qualifying):
    """Attach compact, context-only MI results to the top qualifying plans."""
    if not qualifying:
        return

    try:
        mi = _build_market_intelligence()
    except Exception as exc:
        print(f"Market intelligence unavailable: {type(exc).__name__}: {exc}")
        return

    ranked = sorted(qualifying, key=lambda item: float(item[3]), reverse=True)
    for symbol, _tf_results, _used, _score, _direction, plan in ranked[:MARKET_INTELLIGENCE_MAX_ASSETS]:
        try:
            snapshot = _market_intelligence_snapshot(mi, symbol)
            plan["market_intelligence_context"] = {
                "symbol": snapshot.get("symbol"),
                "source_status": snapshot.get("source_status", {}),
                "errors": snapshot.get("errors", {}),
                "record_count": snapshot.get("record_count", 0),
                "source_count": snapshot.get("source_count", 0),
                "categories": snapshot.get("categories", []),
                "consensus": snapshot.get("consensus", {}),
                "conflicts": snapshot.get("conflicts", []),
                "unreconciled": snapshot.get("unreconciled", []),
                "missing": snapshot.get("missing", []),
                "freshness": snapshot.get("freshness", {}),
            }
        except Exception as exc:
            plan["market_intelligence_context"] = {
                "symbol": _base_asset_symbol(symbol),
                "source_status": {},
                "errors": {"orchestration": f"{type(exc).__name__}: {exc}"},
            }


def _freeze_discovered_fill(entry, discovery):
    """Convert a confirmed BingX discovery into local lifecycle state only."""
    position = discovery.get("position") or {}
    amount = discovery.get("amount")

    entry["status"] = "triggered"
    entry["exchange_sync_status"] = bingx.OPEN
    entry["position_lifecycle"] = bingx.OPEN
    entry["position_close_reported"] = False
    entry["triggered_at"] = entry.get("triggered_at") or datetime.now(timezone.utc).isoformat()

    if amount is not None:
        entry["exchange_position_amount"] = amount

    position_id = position.get("positionId") or position.get("positionID") or position.get("position_id")
    if position_id:
        entry["exchange_position_id"] = str(position_id)

    avg_price = position.get("avgPrice") or position.get("averagePrice")
    if avg_price not in (None, ""):
        try:
            entry["entry_price"] = float(avg_price)
        except (TypeError, ValueError):
            pass

    entries = entry.get("entries") or []
    for leg in entries:
        if isinstance(leg, dict):
            leg["filled"] = True
            leg["filled_at"] = leg.get("filled_at") or datetime.now(timezone.utc).isoformat()


def _sync_watchlist(active_key):
    """Synchronize watchlist lifecycle against the authoritative BingX state."""
    try:
        items = scanner.load_watchlist()
    except Exception as exc:
        print(f"! Could not load watchlist: {type(exc).__name__}: {exc}")
        return

    if not items:
        print("Watchlist is empty.")
        return

    before = len(items)
    items = [it for it in items if not (
        it.get("dead_reported") and it.get("status") in ("invalidated", "expired")
        and scanner._hours_from_now(it.get("invalidated_at") or it.get("expired_at")) >= scanner.REENTRY_HISTORY_HOURS
    )]
    if before != len(items):
        print(f"(pruned {before - len(items)} previously-reported dead setup(s))")

    for it in items:
        _prepare_watchlist_entry(it)
        prev_status = it.get("status")
        was_exchange_open = it.get("exchange_sync_status") == bingx.OPEN
        discovered_open = False

        # A pending BingX setup may already have a real exchange position.
        # Discover it before local refresh; UNKNOWN/ERROR/NOT_FOUND/NOT_MATCHED
        # never become OPEN or CLOSED.
        if (
            str(it.get("exchange", "")).lower() == "bingx"
            and prev_status not in ("triggered", "invalidated", "expired")
        ):
            try:
                discovery = bingx.discover_open_position(it)
                if discovery.get("state") == bingx.OPEN:
                    print(f"  {it.get('symbol', '?')}: BingX OPEN discovered for pending setup; freezing local fill state.")
                    _freeze_discovered_fill(it, discovery)
                    discovered_open = True
                    prev_status = "triggered"
                else:
                    print(
                        f"  {it.get('symbol', '?')} {it.get('direction', '?')}: "
                        f"BingX discovery={discovery.get('state')}; no lifecycle change."
                    )
            except Exception as exc:
                print(f"  ! {it.get('symbol', '?')}: BingX discovery error: {type(exc).__name__}: {exc}")

        if prev_status != "triggered":
            try:
                scanner.refresh_entry(it, active_key)
                time.sleep(0.1)
                if it.get("status") != prev_status:
                    scanner._notify_status_change(it, prev_status)
                    if it.get("status") in ("invalidated", "expired"):
                        it["dead_reported"] = True
            except Exception as exc:
                scanner._log(it, f"Refresh error, left as-is: {exc}")
            continue

        sync_result = bingx.sync_position(it)
        sync_state = sync_result.get("state") if isinstance(sync_result, dict) else sync_result

        if sync_state == bingx.OPEN:
            dctx = None
            try:
                dctx = health.get_derivatives_context(it.get("symbol"))
            except Exception:
                dctx = None

            snapshot, transitioned = health.apply_health(it, dctx)

            if transitioned:
                message = _format_health_alert(it, snapshot)
                print("\n" + "-" * 78)
                print("POSITION HEALTH ALERT")
                print(message)
                print("-" * 78)
                if scanner.send_telegram_message(message):
                    print("  Telegram health alert sent.")

            intel_snapshot, _intel_sent = _maybe_send_position_intelligence(
                it, sync_result, snapshot
            )
            it["position_intelligence_snapshot"] = intel_snapshot

            # The first confirmed OPEN must always produce one Telegram
            # confirmation. This is separate from risk-state alerts: an OPEN
            # position is an important lifecycle event even when its initial
            # operating state is HEALTHY.
            if not was_exchange_open:
                message = _format_position_open_alert(
                    it,
                    intel_snapshot,
                    discovered=discovered_open,
                )
                print("\n" + "-" * 78)
                print("POSITION OPEN ALERT")
                print(message)
                print("-" * 78)
                if scanner.send_telegram_message(message):
                    print("  Telegram position-open alert sent.")
            continue

        if sync_state == bingx.CLOSED:
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

        print(
            f"  {it.get('symbol', '?')} {it.get('direction', '?')}: "
            f"BingX sync={sync_state}; active position monitoring suppressed."
        )

    scanner.save_watchlist(items)


def scan_all(active_key, symbols):
    """Run the SMC pipeline with derivatives as non-invasive context."""
    qualifying = []
    derivatives_data = derivatives.monitor(symbols=symbols)
    derivatives_results = (
        derivatives_data.get("results", {})
        if isinstance(derivatives_data, dict) else {}
    )
    if not isinstance(derivatives_data, dict) or not derivatives_data.get("bulk_fetch_ok", False):
        print("Derivatives intelligence unavailable this cycle; continuing SMC-only.")
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

            # Context only: NEVER modify SMC score, direction, entry, SL, TP,
            # execution state, or trade classification.
            dctx = derivatives_results.get(symbol)
            if dctx:
                plan["derivatives_context"] = {
                    "state": dctx.get("state"),
                    "score": dctx.get("score"),
                    "reason": dctx.get("reason"),
                    "since": dctx.get("since"),
                    "is_new_transition": bool(dctx.get("is_new_transition", False)),
                }
            qualifying.append((symbol, tf_results, used, score, direction, plan))
        except Exception as exc:
            print(f"  ! {symbol}: scan error: {type(exc).__name__}: {exc}")

    _attach_market_intelligence(qualifying)
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

    # Exchange synchronization must happen before the final watchlist refresh,
    # so confirmed closures cannot be reinterpreted as active setup changes.
    _sync_watchlist(active_key)

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

    by_plan_id = {id(q[5]): q for q in qualifying}
    alerts_sent = 0
    auto_added = 0

    for plan in ready:
        match = by_plan_id.get(id(plan))
        if not match:
            continue
        symbol, _tf_results, used, score, direction, plan = match
        lifecycle = plan.get("lifecycle_info", {})

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

    # Do NOT call scanner.check_watchlist() here: _sync_watchlist() already
    # refreshed pending setups and exchange-gated triggered positions. Calling
    # the old helper again would risk reprocessing confirmed BingX closures.

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
