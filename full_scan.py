#!/usr/bin/env python3
"""
Headless full market scan -- meant to be run by GitHub Actions every 4
hours (see .github/workflows/full_scan.yml), not by hand.

Scans the top 150 symbols by 24h volume (tiers 1-50, 51-100, 101-150)
across all timeframes, and auto-adds anything scoring >= AUTO_ADD_MIN_SCORE
straight to the watchlist -- no interactive trail-mode/split-entry prompts,
since there's no human at the keyboard. Uses AUTO_TRAIL_MODE and a single
entry leg at the zone midpoint for every auto-added setup (see the two
constants near the top of smc_scanner.py if you want to change those
defaults later).

The 15-minute refresh_watchlist.py job then picks up whatever this adds
and tracks it exactly like a manually-added entry.
"""

import sys
import time
import smc_scanner as scanner


def main():
    active_key = scanner.detect_active_exchange()
    if not active_key:
        print("Couldn't reach any exchange this run -- skipping full scan (will retry next schedule).")
        sys.exit(0)

    print(f"Full scan starting on {scanner.ADAPTERS[active_key].name}, "
          f"auto-add threshold: score >= {scanner.AUTO_ADD_MIN_SCORE}")

    key_used, symbols = scanner.get_symbols_with_fallback(active_key, 150)
    if not symbols:
        print("Couldn't fetch symbol list from any exchange -- aborting this run.")
        sys.exit(0)

    added = 0
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
                if plan:
                    scanner.add_to_watchlist(
                        symbol,
                        used.get(scanner.ENTRY_TF) or active_key,
                        score, direction, plan,
                        trail_mode=scanner.AUTO_TRAIL_MODE,
                        split_entries=None,   # single leg at zone midpoint, the safe default
                    )
                    added += 1
        except Exception as e:
            print(f"  (error scanning {symbol}, skipped: {e})")
        time.sleep(scanner.REQUEST_DELAY)

    print(f"\nFull scan complete. Checked {checked}/{total} symbols, "
          f"added {added} new setup(s) at score >= {scanner.AUTO_ADD_MIN_SCORE}.")

    if added and scanner.TELEGRAM_ENABLED:
        scanner.send_telegram_message(
            f"\U0001F50D 4-hour scan complete: {added} new setup(s) added to watchlist "
            f"(score >= {scanner.AUTO_ADD_MIN_SCORE}). Check the repo or wait for the next "
            f"15-min refresh for trigger alerts."
        )


if __name__ == "__main__":
    main()
              
