#!/usr/bin/env python3
"""
Headless watchlist refresh -- meant to be run by GitHub Actions on a
schedule (see .github/workflows/watchlist.yml), not by hand.

Does exactly what menu option 5 -> 1 ("View / refresh status") does in
smc_scanner.py, minus any interactive input(). Sends a Telegram message
on every real status change via send_telegram_message() already in
smc_scanner.py.
"""

import sys
import smc_scanner as scanner


def main():
    if not scanner.TELEGRAM_ENABLED:
        print("WARNING: TG_BOT_TOKEN / TG_CHAT_ID not set -- running with "
              "notifications disabled. Check your repo Secrets.")

    active_key = scanner.detect_active_exchange()
    if not active_key:
        print("Couldn't reach any exchange this run -- skipping (will retry next schedule).")
        sys.exit(0)

    scanner.check_watchlist(active_key)
    print("Refresh complete.")


if __name__ == "__main__":
    main()
