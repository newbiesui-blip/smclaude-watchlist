# ATHENA / SMC — BingX lifecycle patch

## What this patch does

- Adds authenticated, **read-only** BingX position synchronization.
- Uses `/openApi/swap/v2/user/positions` to confirm a live position and freeze its actual average fill price.
- Uses `/openApi/swap/v1/trade/positionHistory` when no live position exists to detect a closure.
- Treats `OPEN` as the only state that permits active-position R/health updates.
- Treats `NOT_FOUND`, `NOT_MATCHED`, `ERROR`, and `UNKNOWN` as **not-open** and suppresses active alerts.
- Freezes the R denominator from the actual BingX average fill + original structural stop.
- Adds one-shot TP hit notifications.
- Leaves the SMC scoring, regime, entry plan, watchlist ranking, and auto-add policy intact.
- Makes no order-placement, cancellation, closing, leverage, or margin calls.

BingX documents the current-position endpoint and its `avgPrice`, `positionAmt`, and `positionSide` fields; authenticated requests use HMAC-SHA256. The position-history endpoint is the read-only source for closed positions. See the official/API-maintained references cited in the accompanying project notes.

## Files

- `bingx_position_tracker.py` — new production module.
- `apply_athena_bingx_patch.py` — deterministic patcher for the current `smc_scanner.py`.
- `README.md` — this guide.

## Install

Copy all three files into the repository root beside `smc_scanner.py`, then run:

    python apply_athena_bingx_patch.py

The script creates `smc_scanner.py.bak` before modifying the scanner.

## Required GitHub secrets

    BINGX_API_KEY
    BINGX_SECRET_KEY
    TG_BOT_TOKEN
    TG_CHAT_ID

No Binance/Telegram credentials are substituted for the BingX keys.

## Safety behavior

If BingX credentials are missing or an authenticated request fails, triggered entries are **not** treated as OPEN. This intentionally favors false-negative active alerts over stale/false active-position alerts.

## Test before deployment

1. Run `python -m py_compile smc_scanner.py bingx_position_tracker.py`.
2. Run the repository's normal full scan.
3. Confirm logs contain `BingX sync: SYMBOL -> OPEN` for a real open position.
4. Confirm the entry gets `exchange_filled_entry_price` and `exchange_original_risk`.
5. Close a test position manually on BingX and run another scan.
6. Confirm it transitions to `CLOSED` and no subsequent `ACTIVE POSITION UPDATE` is sent.
7. Confirm TP notifications appear once per target.

Do not merge this patch and then immediately raise/lower score thresholds. Lifecycle correctness should be validated first.
