#!/usr/bin/env python3
"""
Headless full market scan -- meant to be run by GitHub Actions on a
schedule (see .github/workflows/full_scan.yml), not by hand.

Scans the top 150 symbols by 24h volume across all timeframes and sends a
Telegram alert the moment it finds something that is:
  - score >= AUTO_ADD_MIN_SCORE
  - NOT a trade_type in AUTO_ADD_EXCLUDE_TYPES (SCALP, by default)
  - "ready now" -- price already inside the entry zone (market entry) or
    a breakout retest already underway (a clean, close limit level right
    there) -- NOT a genuinely-still-approaching setup

This is one-shot, alert-only. There is no ongoing tracking after the
alert fires -- no watchlist file, no 15-minute rescoring, no stop
trailing, no invalidation/expiry detection. Once you get pinged, you're
managing that trade yourself from there. (An earlier version of this
project did include ongoing tracking via a separate watchlist.yml +
refresh_watchlist.py + smc_watchlist.json -- that whole system has been
retired. If you still see those files in the repo, they're safe to
delete; nothing in this script touches them.)
"""

import sys
import time
import smc_scanner as scanner


def _announce_setup(symbol, direction, score, plan, exchange_name, scanned_price):
    if not scanner.TELEGRAM_ENABLED:
        return
    lines = [
        f"\U0001F195 {symbol} {direction} [{plan.get('trade_type', 'INTRADAY')}] -- score {score}",
        f"Source: {exchange_name} | Scanned price: {scanned_price:.6g}",
        "\u2705 READY NOW",
        f"Mode: {plan['mode']}",
        f"Zone: {plan['zone_low']:.6g} - {plan['zone_high']:.6g} ({plan['zone_label']})",
        f"SL: {plan['invalidation']:.6g}",
    ]
    if plan.get("targets"):
        tp_str = " | ".join(f"TP{i+1} {t['price']:.6g} (~{t['r']:.2f}R)"
                             for i, t in enumerate(plan["targets"]))
        lines.append(f"TP: {tp_str}")
    lines.append("(Check the zone/price above against your own chart before acting -- "
                  "if it looks far off current market price, the source exchange's feed "
                  "may not match what you're seeing. This is a one-shot alert -- no "
                  "further updates will follow for this setup.)")
    scanner.send_telegram_message("\n".join(lines))


def main():
    active_key = scanner.detect_active_exchange()
    if not active_key:
        print("Couldn't reach any exchange this run -- skipping full scan (will retry next schedule).")
        sys.exit(0)

    print(f"Full scan starting on {scanner.ADAPTERS[active_key].name}, "
          f"auto-alert threshold: score >= {scanner.AUTO_ADD_MIN_SCORE}, ready-now only")

    key_used, symbols = scanner.get_symbols_with_fallback(active_key, 150)
    if not symbols:
        print("Couldn't fetch symbol list from any exchange -- aborting this run.")
        sys.exit(0)

    alerted = 0
    checked = 0
    total = len(symbols)

    for n, symbol in enumerate(symbols, 1):
        print(f"[{n}/{total}] {symbol}...")
        try:
            tf_results, used = scanner.scan_symbol(active_key, symbol)
            score, direction = scanner.score_setup(tf_results)
            checked += 1
            if score >= scanner.AUTO_ADD_MIN_SCORE and direction:
                plan = scanner.build_entry_plan(tf_results, direction)
                if plan and plan.get("trade_type") in scanner.AUTO_ADD_EXCLUDE_TYPES:
                    print(f"  (skipped {symbol}: trade_type "
                          f"{plan.get('trade_type')} is excluded)")
                elif plan and not scanner.is_ready_now(plan):
                    print(f"  (skipped {symbol}: not ready now -- still approaching "
                          f"the zone, mode={plan.get('mode')})")
                elif plan:
                    entry_tf_used = used.get(scanner.ENTRY_TF) or active_key
                    entry_tf_data = tf_results.get(scanner.ENTRY_TF)
                    scanned_price = entry_tf_data["price"] if entry_tf_data else 0.0
                    exchange_name = scanner.ADAPTERS.get(entry_tf_used)
                    exchange_name = exchange_name.name if exchange_name else entry_tf_used
                    _announce_setup(symbol, direction, score, plan, exchange_name, scanned_price)
                    alerted += 1
        except Exception as e:
            print(f"  (error scanning {symbol}, skipped: {e})")
        time.sleep(scanner.REQUEST_DELAY)

    print(f"\nFull scan complete. Checked {checked}/{total} symbols, "
          f"alerted on {alerted} ready-now setup(s) at score >= {scanner.AUTO_ADD_MIN_SCORE}.")

    if scanner.TELEGRAM_ENABLED and not alerted:
        scanner.send_telegram_message(
            f"\U0001F50D Market scan complete: checked {checked}/{total} symbols, "
            f"nothing ready-now cleared score >= {scanner.AUTO_ADD_MIN_SCORE} this round. "
            f"Ran fine, just nothing actionable right now."
        )
    elif scanner.TELEGRAM_ENABLED and alerted > 1:
        scanner.send_telegram_message(
            f"\U0001F50D Market scan complete: {alerted} ready-now setups this round (details above)."
        )


if __name__ == "__main__":
    main()
