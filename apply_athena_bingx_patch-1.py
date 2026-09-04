"""Patch the current smclaude-watchlist main files with read-only BingX lifecycle sync.
Run from the repository root:
    python apply_athena_bingx_patch.py
Creates smc_scanner.py.bak before changing anything.
"""
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parent
scanner = ROOT / "smc_scanner.py"
if not scanner.exists():
    raise SystemExit("Place this script beside smc_scanner.py in the repository root.")
s = scanner.read_text(encoding="utf-8")
backup = scanner.with_suffix(scanner.suffix + ".bak")
shutil.copy2(scanner, backup)

# 1) imports
needle = "import requests\n"
insert = "import requests\n\nfrom bingx_position_tracker import (\n    SYNC_OPEN, SYNC_CLOSED, SYNC_NOT_FOUND, SYNC_NOT_MATCHED, SYNC_ERROR, SYNC_UNKNOWN,\n    sync_position, freeze_fill_if_needed, target_hits,\n)\n"
if "from bingx_position_tracker import" not in s:
    if needle not in s:
        raise SystemExit("Could not find import anchor.")
    s = s.replace(needle, insert, 1)

# 2) config flags
needle = 'TELEGRAM_ENABLED = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)\n'
insert = needle + '\n# Read-only BingX private-position synchronization. Never used for order execution.\nBINGX_SYNC_ENABLED = bool(os.environ.get("BINGX_API_KEY") and (os.environ.get("BINGX_SECRET_KEY") or os.environ.get("BINGX_API_SECRET")))\nBINGX_UNKNOWN_SUPPRESS_ACTIVE = True\n'
if "BINGX_SYNC_ENABLED" not in s:
    if needle not in s:
        raise SystemExit("Could not find Telegram config anchor.")
    s = s.replace(needle, insert, 1)

# 3) insert exchange sync at the very beginning of refresh_entry, before candles are fetched.
needle = 'def refresh_entry(entry, active_key):\n    """Refresh ONE watchlist entry in place (mutates entry, sets last_note/history)."""\n    symbol, direction = entry["symbol"], entry["direction"]\n\n'
insert = needle + '''    # EXCHANGE-FIRST GATE: for a triggered setup, BingX is authoritative about whether\n    # the actual position exists. Unknown is never treated as OPEN.\n    if entry.get("status") == "triggered" and BINGX_SYNC_ENABLED:\n        sync_state = sync_position(entry)\n        print(f"    BingX sync: {symbol} -> {sync_state}")\n        if sync_state == SYNC_OPEN:\n            freeze_fill_if_needed(entry)\n        elif sync_state == SYNC_CLOSED:\n            entry["position_lifecycle"] = "CLOSED"\n            entry["last_note"] = (\n                f"BingX confirmed position closure ({entry.get('position_exit_reason', 'CLOSED')}) "\n                f"at {entry.get('exchange_close_price') or 'unknown price'}."\n            )\n            return\n        elif sync_state in (SYNC_NOT_FOUND, SYNC_NOT_MATCHED, SYNC_ERROR, SYNC_UNKNOWN):\n            entry["last_note"] = (\n                f"ACTIVE tracking suppressed: BingX state is {sync_state}; "\n                "exchange state is not confirmed OPEN."\n            )\n            return\n    elif entry.get("status") == "triggered" and not BINGX_SYNC_ENABLED:\n        entry["exchange_sync_status"] = SYNC_UNKNOWN\n        entry["last_note"] = "Active tracking suppressed: BingX sync is unavailable."\n        return\n\n'''
if 'EXCHANGE-FIRST GATE: for a triggered setup' not in s:
    if needle not in s:
        raise SystemExit("Could not find refresh_entry anchor.")
    s = s.replace(needle, insert, 1)

# 4) replace candle-based triggered stop check with exchange-first logic fallback only when sync disabled.
old = '''    if entry["status"] == "triggered":\n        breached = (direction == "BULLISH" and price <= entry["invalidation"]) or \\\n                   (direction == "BEARISH" and price >= entry["invalidation"])\n        if breached:\n'''
new = '''    if entry["status"] == "triggered":\n        # With BingX sync enabled this branch is reached only after a confirmed OPEN.\n        # Exchange closure is therefore checked before this candle-level fallback.\n        breached = (direction == "BULLISH" and price <= entry["invalidation"]) or \\\n                   (direction == "BEARISH" and price >= entry["invalidation"])\n        if breached:\n'''
# This is intentionally a no-op replacement if exact formatting differs; exchange-first gate above is the protection.
if old in s:
    s = s.replace(old, new, 1)

# 5) add TP event detection immediately after current R is calculated.
needle = '        entry["current_r"] = r_now\n'
insert = needle + '''        # TP event engine: each target generates at most one notification.\n        new_tp_hits = target_hits(entry, price)\n        for tp_no, tp_price, tp_r in new_tp_hits:\n            msg = (f"🎯 POSITION TARGET HIT: {symbol} {direction} [{entry.get('trade_type', 'INTRADAY')}]\\n"\n                   f"Lifecycle: TP{tp_no}_HIT\\n"\n                   f"TP{tp_no}: {tp_price:.6g} (~{tp_r:.2f}R)\\n"\n                   f"Current R: {r_now:+.2f}R | Max R: {entry.get('max_r', 0.0):+.2f}R\\n"\n                   "Existing position only -- NOT a new entry.")\n            send_telegram_message(msg)\n            _log(entry, f"TP{tp_no} hit at {tp_price:.6g} (~{tp_r:.2f}R).")\n'''
if 'new_tp_hits = target_hits(entry, price)' not in s:
    if needle not in s:
        raise SystemExit("Could not find current R anchor.")
    s = s.replace(needle, insert, 1)

# 6) freeze R basis on the first confirmed open; keep original stop as denominator.
# Existing _position_r_basis already prefers exchange_filled_entry_price, so no further change needed.

scanner.write_text(s, encoding="utf-8")
print(f"Patched {scanner}")
print(f"Backup: {backup}")
