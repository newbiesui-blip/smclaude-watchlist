#!/usr/bin/env python3
"""
Headless full market scan -- meant to be run by GitHub Actions on a
schedule (see .github/workflows/full_scan.yml), not by hand.

Scans the top 150 symbols by 24h volume across all timeframes, and
auto-adds anything scoring >= AUTO_ADD_MIN_SCORE straight to the
watchlist -- no interactive trail-mode/split-entry prompts, since there's
no human at the keyboard. Uses AUTO_TRAIL_MODE and a single entry leg at
the zone midpoint for every auto-added setup.

Unlike earlier versions, this sends a full per-setup Telegram message the
moment each one is added -- not just a count at the end -- and flags
whether price is ALREADY inside the entry zone (needs a decision now, not
just a watch) versus genuinely still approaching it. The 15-min
refresh_watchlist.py job picks up whatever this adds from there and
tracks it exactly like a manually-added entry.
"""

import sys
import time
import smc_scanner as scanner


def _urgency_line(plan):
    """
    build_entry_plan()'s "mode" tells us whether price is already sitting
    inside the zone (PULLBACK) or a breakout retest is already underway,
    versus genuinely still approaching (WAIT FOR PULLBACK). Surface that
    distinction plainly instead of burying it in the plan note.
    """
    mode = plan.get("mode", "")
    if mode.startswith("PULLBACK") or mode.startswith("WAIT FOR BREAKOUT RETEST"):
        return "\u26A0\uFE0F ALREADY LIVE -- price is at/through the zone now, this may need a decision today, not just a watch."
    return "\U0001F440 Genuinely still approaching -- fine to just let the watchlist track this one."


def _announce_setup(symbol, direction, score, plan):
    if not scanner.TELEGRAM_ENABLED:
        return
    lines = [
        f"\U0001F195 {symbol} {direction} [{plan.get('trade_type', 'INTRADAY')}] -- score {score}",
        _urgency_line(plan),
        f"Mode: {plan['mode']}",
        f"Zone: {plan['zone_low']:.6g} - {plan['zone_high']:.6g} ({plan['zone_label']})",
        f"SL: {plan['invalidation']:.6g}",
    ]
    if plan.get("targets"):
        tp_str = " | ".join(f"TP{i+1} {t['price']:.6g} (~{t['r']:.2f}R)"
                             for i, t in enumerate(plan["targets"]))
        lines.append(f"TP: {tp_str}")
    scanner.send_telegram_message("\n".join(lines))


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
                    _announce_setup(symbol, direction, score, plan)
        except Exception as e:
            print(f"  (error scanning {symbol}, skipped: {e})")
        time.sleep(scanner.REQUEST_DELAY)

    print(f"\nFull scan complete. Checked {checked}/{total} symbols, "
          f"added {added} new setup(s) at score >= {scanner.AUTO_ADD_MIN_SCORE}.")

    if scanner.TELEGRAM_ENABLED and not added:
        scanner.send_telegram_message(
            f"\U0001F50D Market scan complete: checked {checked}/{total} symbols, "
            f"nothing cleared score >= {scanner.AUTO_ADD_MIN_SCORE} this round. "
            f"Ran fine, just no qualifying setups right now."
        )
    elif scanner.TELEGRAM_ENABLED and added > 1:
        # each setup already got its own detailed message above -- just a
        # short tail-note when more than one landed in the same run
        scanner.send_telegram_message(
            f"\U0001F50D Market scan complete: {added} setups added this round (details above)."
        )


if __name__ == "__main__":
    main()
