"""Read-only BingX perpetual-position synchronizer for ATHENA/SMC.

No order, cancel, close, leverage, or margin endpoint is used here.
It queries only authenticated account/position/history endpoints.
"""
import hashlib
import os
import hmac
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

import requests

BINGX_BASE = "https://open-api.bingx.com"
POSITIONS_PATH = "/openApi/swap/v2/user/positions"
POSITION_HISTORY_PATH = "/openApi/swap/v1/trade/positionHistory"

SYNC_OPEN = "OPEN"
SYNC_CLOSED = "CLOSED"
SYNC_NOT_FOUND = "NOT_FOUND"
SYNC_NOT_MATCHED = "NOT_MATCHED"
SYNC_ERROR = "ERROR"
SYNC_UNKNOWN = "UNKNOWN"

TERMINAL_LIFECYCLES = {"CLOSED", "STOPPED", "TP_CLOSED", "SL_CLOSED"}


def _clean_symbol(symbol):
    s = str(symbol or "").upper().strip()
    for suffix in ("-SWAP", "_USDT", "-USDT"):
        if s.endswith(suffix):
            s = s[: -len(suffix)] + "-USDT"
    if s.endswith("USDT") and "-" not in s:
        s = s[:-4] + "-USDT"
    return s


def _sign(params, secret):
    query = urlencode(sorted(params.items()))
    return hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()


def signed_get(path, api_key, secret_key, params=None, timeout=8):
    params = dict(params or {})
    params["timestamp"] = int(time.time() * 1000)
    params.setdefault("recvWindow", 5000)
    params["signature"] = _sign(params, secret_key)
    headers = {"X-BX-APIKEY": api_key, "X-SOURCE-KEY": "ATHENA-SMC"}
    r = requests.get(
        BINGX_BASE + path,
        params=params,
        headers=headers,
        timeout=timeout,
    )
    r.raise_for_status()
    payload = r.json()
    if payload.get("code") not in (0, "0", None):
        raise RuntimeError(
            f"BingX {payload.get('code')}: {payload.get('msg', '')}"
        )
    return payload.get("data")


def _as_list(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("list", "positions", "data", "orders"):
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []


def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _position_amount(p):
    """Return a positive live-position amount, or None if it cannot be verified."""
    for key in ("positionAmt", "availableAmt", "quantity", "positionAmount", "size"):
        value = _float(p.get(key))
        if value is not None:
            return abs(value)
    return None


def _position_matches(p, symbol, direction):
    """Strictly match a genuinely non-zero live position."""
    psym = _clean_symbol(p.get("symbol"))
    want = _clean_symbol(symbol)
    if psym != want:
        return False

    side = str(p.get("positionSide", p.get("side", ""))).upper()
    if direction == "BULLISH" and side not in ("LONG", "BOTH"):
        return False
    if direction == "BEARISH" and side not in ("SHORT", "BOTH"):
        return False

    # Never infer OPEN when BingX did not provide a usable amount.
    amount = _position_amount(p)
    return amount is not None and amount > 0


def _history_timestamp(entry):
    raw = entry.get("triggered_at") or entry.get("added_at")
    if not raw:
        return int((time.time() - 86400) * 1000)
    try:
        return int(datetime.fromisoformat(raw).timestamp() * 1000)
    except Exception:
        return int((time.time() - 86400) * 1000)


def _history_close_timestamp(item):
    for key in (
        "closeTime",
        "closedTime",
        "closeTimestamp",
        "updateTime",
        "time",
        "timestamp",
    ):
        value = _float(item.get(key))
        if value is not None and value > 0:
            # BingX timestamps are normally milliseconds. Normalize seconds.
            if value < 10_000_000_000:
                value *= 1000
            return int(value)
    return None


def _history_reason(item):
    text = " ".join(
        str(item.get(k, ""))
        for k in (
            "closeType",
            "closeReason",
            "reason",
            "positionCloseType",
            "type",
            "orderType",
            "remark",
        )
    ).upper()

    if any(x in text for x in ("LIQUIDATION", "ADL", "FORCE")):
        return "LIQUIDATION"
    if any(x in text for x in ("STOP_LOSS", "STOPLOSS", "STOP LOSS", "SL")):
        return "STOP_LOSS"
    if any(x in text for x in ("TAKE_PROFIT", "TAKEPROFIT", "TAKE PROFIT", "TP")):
        return "TAKE_PROFIT"
    return "CLOSED"


def _history_close_price(item):
    for key in (
        "closeAvgPrice",
        "avgClosePrice",
        "closePrice",
        "exitPrice",
        "avgPrice",
        "price",
    ):
        value = _float(item.get(key))
        if value is not None and value > 0:
            return value
    return None


def _history_id(item):
    for key in ("positionId", "orderId", "closeOrderId", "id"):
        if item.get(key) is not None:
            return str(item[key])
    return None


def _history_matches_entry(row, entry, direction):
    """Reject historical rows that clearly belong to another symbol/side."""
    symbol = entry.get("symbol")
    if row.get("symbol") and _clean_symbol(row.get("symbol")) != _clean_symbol(symbol):
        return False

    want_side = "LONG" if direction == "BULLISH" else "SHORT"
    side = str(row.get("positionSide", row.get("side", ""))).upper()
    if side and side not in (want_side, "BOTH"):
        return False

    return True


def _find_closed_history(symbol, direction, start_ms, entry=None):
    now = int(time.time() * 1000)
    params = {
        "symbol": _clean_symbol(symbol),
        "startTs": start_ms,
        "endTs": now,
        "pageIndex": 1,
        "pageSize": 100,
    }

    data = signed_get(
        POSITION_HISTORY_PATH,
        os.environ["BINGX_API_KEY"],
        os.environ["BINGX_SECRET_KEY"],
        params,
    )
    rows = _as_list(data)

    candidates = []
    for row in rows:
        if entry is not None and not _history_matches_entry(row, entry, direction):
            continue

        close_ts = _history_close_timestamp(row)
        # A historical close must be inside the tracked position's window.
        if close_ts is not None and close_ts < start_ms:
            continue

        candidates.append(row)

    if not candidates:
        return None

    candidates.sort(
        key=lambda x: _history_close_timestamp(x) or 0,
        reverse=True,
    )
    return candidates[0]


def _record_closed(entry, closed):
    """Persist exchange closure facts without rewriting SMC setup semantics."""
    reason = _history_reason(closed)
    close_ts = _history_close_timestamp(closed)
    close_id = _history_id(closed)
    close_price = _history_close_price(closed)

    # Closure identity makes repeated scans idempotent.
    previous_status = entry.get("exchange_sync_status")
    previous_id = entry.get("exchange_close_order_id")

    entry["exchange_sync_status"] = SYNC_CLOSED
    entry["exchange_close_source"] = "BingX positionHistory"
    entry["exchange_close_price"] = close_price
    entry["exchange_close_order_id"] = close_id
    entry["position_exit_reason"] = reason
    entry["position_lifecycle"] = "CLOSED"

    if close_ts is not None:
        entry["exchange_closed_at"] = datetime.fromtimestamp(
            close_ts / 1000, tz=timezone.utc
        ).isoformat()
    elif not entry.get("exchange_closed_at"):
        entry["exchange_closed_at"] = datetime.now(timezone.utc).isoformat()

    entry["exchange_sync_checked_at"] = datetime.now(timezone.utc).isoformat()
    entry["exchange_sync_error"] = None

    # Do not turn a position closure into SMC setup invalidation.
    # Preserve the existing setup status exactly as supplied.
    #
    # Alerting code can use this flag to emit one lifecycle notification.
    new_close_event = (
        previous_status != SYNC_CLOSED
        or (close_id is not None and previous_id != close_id)
        or not entry.get("position_close_reported", False)
    )

    if new_close_event:
        entry["position_close_reported"] = False

    return new_close_event


def sync_position(entry):
    """Return a normalized exchange state and mutate entry with exchange facts.

    IMPORTANT:
      - OPEN requires a positively verified non-zero position amount.
      - UNKNOWN/ERROR/NOT_FOUND/NOT_MATCHED are never converted to OPEN.
      - A historical close is only accepted inside the tracked position window.
      - Confirmed closure is idempotent and does not rewrite SMC setup status.
    """
    api_key = os.environ.get("BINGX_API_KEY", "")
    secret = (
        os.environ.get("BINGX_SECRET_KEY", "")
        or os.environ.get("BINGX_API_SECRET", "")
    )

    if not api_key or not secret:
        entry["exchange_sync_status"] = SYNC_UNKNOWN
        entry["exchange_sync_error"] = "BingX credentials unavailable"
        entry["exchange_sync_checked_at"] = datetime.now(timezone.utc).isoformat()
        return SYNC_UNKNOWN

    symbol = entry.get("symbol")
    direction = str(entry.get("direction", "")).upper()

    try:
        data = signed_get(
            POSITIONS_PATH,
            api_key,
            secret,
            {"symbol": _clean_symbol(symbol)},
        )
        positions = _as_list(data)
        matches = [
            p for p in positions
            if _position_matches(p, symbol, direction)
        ]

        if matches:
            p = matches[0]
            avg = _float(p.get("avgPrice"))
            amount = _position_amount(p)

            entry["exchange_sync_status"] = SYNC_OPEN
            entry["exchange_sync_checked_at"] = datetime.now(timezone.utc).isoformat()
            entry["exchange_sync_error"] = None
            entry["exchange_position_id"] = (
                str(p.get("positionId"))
                if p.get("positionId") is not None
                else None
            )
            entry["exchange_position_side"] = p.get("positionSide")
            entry["exchange_position_amt"] = amount
            entry["exchange_avg_price"] = avg
            entry["exchange_unrealized_pnl"] = _float(
                p.get("unrealizedProfit")
            )
            entry["position_lifecycle"] = "OPEN"
            return SYNC_OPEN

        # No positively verified live position. History is checked before
        # returning NOT_FOUND, but history failures remain ERROR.
        try:
            closed = _find_closed_history(
                symbol,
                direction,
                _history_timestamp(entry),
                entry=entry,
            )
        except Exception as hist_exc:
            entry["exchange_sync_status"] = SYNC_ERROR
            entry["exchange_sync_error"] = f"history: {hist_exc}"
            entry["exchange_sync_checked_at"] = datetime.now(timezone.utc).isoformat()
            return SYNC_ERROR

        if closed:
            _record_closed(entry, closed)
            return SYNC_CLOSED

        entry["exchange_sync_status"] = SYNC_NOT_FOUND
        entry["exchange_sync_checked_at"] = datetime.now(timezone.utc).isoformat()
        entry["exchange_sync_error"] = None
        return SYNC_NOT_FOUND

    except Exception as exc:
        entry["exchange_sync_status"] = SYNC_ERROR
        entry["exchange_sync_checked_at"] = datetime.now(timezone.utc).isoformat()
        entry["exchange_sync_error"] = str(exc)
        return SYNC_ERROR


def freeze_fill_if_needed(entry):
    avg = _float(entry.get("exchange_avg_price"))
    if avg is None or entry.get("exchange_filled_entry_price") is not None:
        return False

    original_stop = _float(entry.get("original_invalidation"))
    if original_stop is None or avg <= 0:
        return False

    risk = abs(avg - original_stop)
    if risk <= 0:
        return False

    entry["exchange_filled_entry_price"] = avg
    entry["exchange_original_risk"] = risk
    entry["exchange_fill_frozen_at"] = datetime.now(timezone.utc).isoformat()
    return True


def target_hits(entry, current_price):
    """Mark newly crossed targets once. Uses frozen exchange entry/stop basis."""
    try:
        price = float(current_price)
        ep = float(
            entry.get("exchange_filled_entry_price")
            or entry.get("entry_price")
        )
        stop = float(entry.get("original_invalidation"))
        risk = abs(ep - stop)
        if risk <= 0:
            return []
    except (TypeError, ValueError):
        return []

    targets = entry.get("position_targets") or entry.get("targets") or []
    status = entry.setdefault("tp_status", ["PENDING"] * len(targets))
    while len(status) < len(targets):
        status.append("PENDING")

    hits = []
    for i, t in enumerate(targets[:3]):
        try:
            tp = float(t["price"])
        except (TypeError, ValueError, KeyError):
            continue

        crossed = (
            price >= tp
            if entry["direction"] == "BULLISH"
            else price <= tp
        )
        if crossed and status[i] != "HIT":
            status[i] = "HIT"
            r = (
                (tp - ep) if entry["direction"] == "BULLISH"
                else (ep - tp)
            ) / risk
            hits.append((i + 1, tp, r))

    entry["tp_status"] = status
    return hits
