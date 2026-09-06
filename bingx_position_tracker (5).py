"""ATHENA BingX position lifecycle tracker.

Read-only exchange synchronization layer.

This module:
- Reads BingX position/history data.
- Confirms OPEN only when a matching position has a verified positive amount.
- Records exchange closures without changing SMC setup fields.
- Preserves setup status/history.
- Never places, cancels, closes, or modifies trades.

Lifecycle contract:
    pending setup
        -> discover_open_position()
        -> OPEN only when BingX confirms a positive matching position

    triggered setup
        -> sync_position()
        -> OPEN when BingX confirms a positive matching position
        -> CLOSED only when matching BingX position history confirms closure
        -> NOT_FOUND when no open position exists but closure cannot be confirmed
        -> ERROR when exchange synchronization fails

IMPORTANT:
- NOT_FOUND is never interpreted as CLOSED.
- UNKNOWN / ERROR / NOT_FOUND / NOT_MATCHED are never OPEN.
- This module never changes SMC setup-generation fields.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import requests


BINGX_BASE = "https://open-api.bingx.com"

HERE = os.path.dirname(os.path.abspath(__file__))
POSITIONS_PATH = os.path.join(HERE, "watchlist.json")
POSITION_HISTORY_PATH = os.path.join(HERE, "position_history.json")
ALL_ORDERS_PATH = os.path.join(HERE, "all_orders.json")

OPEN = "OPEN"
CLOSED = "CLOSED"

NON_OPEN_STATES = {
    "UNKNOWN",
    "ERROR",
    "NOT_FOUND",
    "NOT_MATCHED",
    "UNSYNCED",
    "CLOSED",
    "REVERSED",
}


def _api_credentials():
    api_key = os.environ.get("BINGX_API_KEY", "").strip()

    secret = (
        os.environ.get("BINGX_SECRET_KEY", "").strip()
        or os.environ.get("BINGX_API_SECRET", "").strip()
    )

    return api_key, secret


def normalize_symbol(symbol: Any) -> str:
    s = str(symbol or "").upper().strip()

    for suffix in ("-SWAP", "_USDT", "-USDT"):
        if s.endswith(suffix):
            s = s[: -len(suffix)] + "USDT"

    if not s.endswith("USDT"):
        s = s.replace("-", "") + "USDT"

    return s


def normalize_side(direction: Any) -> str:
    value = str(direction or "").upper().strip()

    if value in {"LONG", "BUY", "BULLISH"}:
        return "LONG"

    if value in {"SHORT", "SELL", "BEARISH"}:
        return "SHORT"

    return value


def _signed_get(
    path: str,
    params: Optional[Dict[str, Any]] = None,
    api_key: Optional[str] = None,
    secret: Optional[str] = None,
):
    """Perform a signed BingX GET request.

    This helper is strictly read-only. Only GET requests are used.
    """

    params = dict(params or {})

    if api_key is None or secret is None:
        api_key, secret = _api_credentials()

    if not api_key or not secret:
        raise RuntimeError("Missing BingX API credentials.")

    params.setdefault("timestamp", int(time.time() * 1000))

    query = urlencode(sorted(params.items()))

    signature = hmac.new(
        secret.encode("utf-8"),
        query.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    url = f"{BINGX_BASE}{path}?{query}&signature={signature}"

    response = requests.get(
        url,
        headers={"X-BX-APIKEY": api_key},
        timeout=15,
    )

    response.raise_for_status()

    return response.json()


def _extract_positions(payload: Any):
    """Extract a list of position dictionaries from BingX payloads."""

    if isinstance(payload, list):
        return payload

    if not isinstance(payload, dict):
        return []

    data = payload.get("data")

    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        for key in (
            "positions",
            "position",
            "data",
        ):
            value = data.get(key)

            if isinstance(value, list):
                return value

    for key in (
        "positions",
        "position",
    ):
        value = payload.get(key)

        if isinstance(value, list):
            return value

    return []


def _extract_history_items(payload: Any):
    """Extract a list of position-history records from BingX payloads."""

    if isinstance(payload, list):
        return payload

    if not isinstance(payload, dict):
        return []

    data = payload.get("data")

    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        for key in (
            "positionHistory",
            "positionHistories",
            "positions",
            "history",
            "data",
        ):
            value = data.get(key)

            if isinstance(value, list):
                return value

        if any(
            key in data
            for key in (
                "positionId",
                "positionID",
                "symbol",
                "closeTime",
                "closedAt",
            )
        ):
            return [data]

    for key in (
        "positionHistory",
        "positionHistories",
        "positions",
        "history",
    ):
        value = payload.get(key)

        if isinstance(value, list):
            return value

    return []


def _position_amount(position: Dict[str, Any]):
    """Return verified positive position size, or None if unavailable."""

    for key in (
        "positionAmt",
        "availableAmt",
        "quantity",
        "positionAmount",
        "size",
    ):
        value = position.get(key)

        if value is None or value == "":
            continue

        try:
            amount = abs(float(value))
        except (TypeError, ValueError):
            continue

        if amount > 0:
            return amount

        return 0.0

    return None


def _position_matches(
    position: Dict[str, Any],
    symbol: str,
    direction: str,
) -> bool:
    """Return True only for a positively sized matching position."""

    position_symbol = normalize_symbol(
        position.get("symbol")
        or position.get("contract")
        or position.get("pair")
    )

    if position_symbol != normalize_symbol(symbol):
        return False

    position_side = normalize_side(
        position.get("positionSide")
        or position.get("side")
        or position.get("direction")
    )

    requested_side = normalize_side(direction)

    if position_side and requested_side:
        if position_side != requested_side:
            return False

    amount = _position_amount(position)

    if amount is None:
        return False

    return amount > 0


def _history_timestamp(value: Any) -> Optional[datetime]:
    """Normalize numeric or ISO timestamps to UTC datetime."""

    if value is None or value == "":
        return None

    if isinstance(value, (int, float)):
        try:
            timestamp = float(value)

            if timestamp > 10_000_000_000:
                timestamp /= 1000

            return datetime.fromtimestamp(
                timestamp,
                tz=timezone.utc,
            )

        except (ValueError, OSError, OverflowError):
            return None

    text = str(value).strip()

    if not text:
        return None

    if text.endswith("Z"):
        text = text[:-1] + "+00:00"

    try:
        parsed = datetime.fromisoformat(text)

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)

        return parsed.astimezone(timezone.utc)

    except ValueError:
        return None


def _history_close_timestamp(
    item: Dict[str, Any],
) -> Optional[int]:
    """Return the best available closure timestamp in milliseconds."""

    for key in (
        "closeTime",
        "closedAt",
        "closeTimestamp",
        "closeTs",
        "updateTime",
        "time",
        "timestamp",
    ):
        value = item.get(key)

        if value is None or value == "":
            continue

        try:
            numeric = float(value)

            if numeric < 10_000_000_000:
                numeric *= 1000

            return int(numeric)

        except (TypeError, ValueError):
            parsed = _history_timestamp(value)

            if parsed is not None:
                return int(parsed.timestamp() * 1000)

    return None


def _history_position_id(
    item: Dict[str, Any],
) -> Optional[str]:
    """Extract an exchange position ID from a position/history record."""

    for key in (
        "positionId",
        "positionID",
        "position_id",
        "posId",
        "positionUid",
        "positionUID",
    ):
        value = item.get(key)

        if value is not None and str(value).strip():
            return str(value).strip()

    return None


def _history_order_id(
    item: Dict[str, Any],
) -> Optional[str]:
    """Extract an exchange order ID from a history record."""

    for key in (
        "orderId",
        "orderID",
        "order_id",
        "closeOrderId",
        "closeOrderID",
        "closeOrder_id",
    ):
        value = item.get(key)

        if value is not None and str(value).strip():
            return str(value).strip()

    return None


def _history_close_price(
    item: Dict[str, Any],
):
    """Extract the best available exchange close price."""

    for key in (
        "closePrice",
        "avgClosePrice",
        "averageClosePrice",
        "avgPrice",
        "averagePrice",
        "price",
        "stopPrice",
    ):
        value = item.get(key)

        if value is not None and value != "":
            return value

    return None


def _history_realized_pnl(
    item: Dict[str, Any],
):
    """Extract realized PnL when available."""

    for key in (
        "realizedProfit",
        "realisedProfit",
        "realizedPnl",
        "realisedPnl",
        "profit",
        "pnl",
    ):
        value = item.get(key)

        if value is not None and value != "":
            return value

    return None


def _history_close_reason(
    item: Dict[str, Any],
) -> Optional[str]:
    """Extract an explicit close reason when the API provides one."""

    for key in (
        "closeReason",
        "close_reason",
        "reason",
        "exitReason",
        "exit_reason",
        "positionCloseReason",
    ):
        value = item.get(key)

        if value is not None and str(value).strip():
            return str(value).strip()

    return None


def _history_matches_entry(
    item: Dict[str, Any],
    symbol: str,
    direction: str,
    entry: Optional[Dict[str, Any]] = None,
) -> bool:
    """Check symbol/side/position identity before accepting history."""

    item_symbol = normalize_symbol(
        item.get("symbol")
        or item.get("contract")
        or item.get("pair")
    )

    if item_symbol != normalize_symbol(symbol):
        return False

    item_side = normalize_side(
        item.get("positionSide")
        or item.get("side")
        or item.get("direction")
    )

    requested_side = normalize_side(direction)

    if item_side and requested_side:
        if item_side != requested_side:
            return False

    entry = entry or {}

    tracked_position_id = (
        entry.get("exchange_position_id")
        or entry.get("position_id")
        or entry.get("bingx_position_id")
    )

    history_position_id = _history_position_id(item)

    if tracked_position_id and history_position_id:
        if str(tracked_position_id) != str(history_position_id):
            return False

    return True


def _find_closed_history(
    symbol: str,
    direction: str,
    start_ms: int,
    entry: Optional[Dict[str, Any]] = None,
    api_key: Optional[str] = None,
    secret: Optional[str] = None,
):
    """Find the most recent matching BingX closed-position history record.

    Uses BingX positionHistory rather than allOrders.

    A matching position-history record is required before sync_position()
    reports CLOSED.

    Failure to find a matching history record returns None and therefore
    leaves the lifecycle in NOT_FOUND rather than guessing CLOSED.
    """

    start_ms = max(0, int(start_ms))
    end_ms = int(time.time() * 1000)

    payload = _signed_get(
        "/openApi/swap/v1/trade/positionHistory",
        {
            "symbol": normalize_symbol(symbol),
            "startTs": start_ms,
            "endTs": end_ms,
            "pageIndex": 1,
            "pageSize": 100,
        },
        api_key=api_key,
        secret=secret,
    )

    candidates = _extract_history_items(payload)

    if not candidates:
        return None

    best = None
    best_time = -1

    for item in candidates:
        if not isinstance(item, dict):
            continue

        if not _history_matches_entry(
            item,
            symbol,
            direction,
            entry=entry,
        ):
            continue

        close_time = _history_close_timestamp(item)

        if close_time is None:
            continue

        if close_time < start_ms:
            continue

        if close_time > end_ms:
            continue

        if close_time > best_time:
            best = item
            best_time = close_time

    return best


def _infer_close_reason(
    history_item: Optional[Dict[str, Any]],
) -> str:
    """Infer a conservative close reason from available exchange fields."""

    if not history_item:
        return "EXCHANGE_CLOSED"

    explicit_reason = _history_close_reason(history_item)

    if explicit_reason:
        return explicit_reason.upper()

    text = " ".join(
        str(history_item.get(key, ""))
        for key in (
            "orderType",
            "type",
            "orderStatus",
            "status",
            "stopType",
            "closePosition",
            "closeReason",
            "reason",
            "exitReason",
        )
    ).upper()

    if "LIQUID" in text:
        return "LIQUIDATION"

    if "TAKE_PROFIT" in text:
        return "TAKE_PROFIT"

    if "STOP_LOSS" in text:
        return "STOP_LOSS"

    if "TAKE PROFIT" in text:
        return "TAKE_PROFIT"

    if "STOP LOSS" in text:
        return "STOP_LOSS"

    return "EXCHANGE_CLOSED"


def _record_closed(
    entry: Dict[str, Any],
    history_item: Optional[Dict[str, Any]] = None,
):
    """Record confirmed exchange closure without changing SMC setup fields."""

    history_item = history_item or {}

    close_time = _history_close_timestamp(history_item)

    if close_time is None:
        close_time = int(time.time() * 1000)

    close_reason = _infer_close_reason(history_item)
    close_price = _history_close_price(history_item)
    order_id = _history_order_id(history_item)
    realized_pnl = _history_realized_pnl(history_item)
    position_id = _history_position_id(history_item)

    # Do not mutate:
    # - SMC score
    # - direction
    # - preferred entry
    # - entry zone
    # - SL
    # - TP
    # - execution state
    entry["exchange_sync_status"] = CLOSED
    entry["position_lifecycle"] = CLOSED
    entry["exchange_closed_at"] = close_time
    entry["position_exit_reason"] = close_reason
    entry["exchange_close_source"] = "BingX positionHistory"

    if close_price not in (None, ""):
        entry["exchange_close_price"] = close_price

    if order_id not in (None, ""):
        entry["exchange_close_order_id"] = str(order_id)

    if realized_pnl not in (None, ""):
        entry["exchange_realized_pnl"] = realized_pnl

    if position_id:
        entry["exchange_position_id"] = position_id

    entry["position_close_reported"] = False

    return entry


def _record_open_position(
    entry: Dict[str, Any],
    position: Dict[str, Any],
    amount: Optional[float],
):
    """Record exchange facts for a confirmed OPEN position.

    Only exchange/lifecycle fields are changed.
    No SMC setup-generation fields are modified.
    """

    entry["exchange_sync_status"] = OPEN
    entry["position_lifecycle"] = OPEN

    if amount is not None:
        entry["exchange_position_amount"] = amount

    position_id = _history_position_id(position)

    if position_id:
        entry["exchange_position_id"] = position_id

    position_side = normalize_side(
        position.get("positionSide")
        or position.get("side")
        or position.get("direction")
    )

    if position_side:
        entry["exchange_position_side"] = position_side

    avg_price = (
        position.get("avgPrice")
        or position.get("averagePrice")
        or position.get("entryPrice")
    )

    if avg_price not in (None, ""):
        entry["exchange_avg_price"] = avg_price

    unrealized_pnl = (
        position.get("unrealizedProfit")
        or position.get("unrealizedPnl")
        or position.get("unrealisedProfit")
        or position.get("unrealisedPnl")
    )

    if unrealized_pnl not in (None, ""):
        entry["exchange_unrealized_pnl"] = unrealized_pnl

    realized_pnl = (
        position.get("realisedProfit")
        or position.get("realizedProfit")
        or position.get("realisedPnl")
        or position.get("realizedPnl")
    )

    if realized_pnl not in (None, ""):
        entry["exchange_realized_pnl"] = realized_pnl

    leverage = position.get("leverage")

    if leverage not in (None, ""):
        entry["exchange_leverage"] = leverage

    liquidation_price = (
        position.get("liquidationPrice")
        or position.get("liquidation_price")
    )

    if liquidation_price not in (None, ""):
        entry["exchange_liquidation_price"] = liquidation_price

    position_value = (
        position.get("positionValue")
        or position.get("position_value")
    )

    if position_value not in (None, ""):
        entry["exchange_position_value"] = position_value

    available_amount = (
        position.get("availableAmt")
        or position.get("availableAmount")
    )

    if available_amount not in (None, ""):
        entry["exchange_available_amount"] = available_amount

    entry["exchange_sync_checked_at"] = int(time.time() * 1000)
    entry["exchange_sync_error"] = None
    entry["position_close_reported"] = False

    return entry


def sync_position(
    entry: Dict[str, Any],
    api_key: Optional[str] = None,
    secret: Optional[str] = None,
):
    """Synchronize one triggered position against BingX.

    This function is strictly read-only.

    Returns:
        OPEN
        CLOSED
        NOT_FOUND
        ERROR
        NOT_MATCHED
    """

    if str(entry.get("status", "")).lower() != "triggered":
        return {
            "state": "NOT_MATCHED",
            "active": False,
            "reason": "Entry is not triggered.",
        }

    symbol = entry.get("symbol")
    direction = entry.get("direction")

    if not symbol or not direction:
        entry["exchange_sync_status"] = "NOT_MATCHED"

        return {
            "state": "NOT_MATCHED",
            "active": False,
            "reason": "Missing symbol or direction.",
        }

    try:
        payload = _signed_get(
            "/openApi/swap/v2/user/positions",
            {
                "symbol": normalize_symbol(symbol),
            },
            api_key=api_key,
            secret=secret,
        )

        positions = _extract_positions(payload)

    except Exception as exc:
        entry["exchange_sync_status"] = "ERROR"
        entry["exchange_sync_checked_at"] = int(time.time() * 1000)
        entry["exchange_sync_error"] = str(exc)

        return {
            "state": "ERROR",
            "active": False,
            "reason": f"BingX position lookup failed: {exc}",
        }

    matching_position = None

    for position in positions:
        if not isinstance(position, dict):
            continue

        if _position_matches(
            position,
            symbol,
            direction,
        ):
            matching_position = position
            break

    if matching_position is not None:
        amount = _position_amount(matching_position)

        _record_open_position(
            entry,
            matching_position,
            amount,
        )

        return {
            "state": OPEN,
            "active": True,
            "position": matching_position,
            "amount": amount,
            "reason": (
                "BingX position confirmed OPEN with positive quantity."
            ),
        }

    tracked_id = (
        entry.get("exchange_position_id")
        or entry.get("position_id")
        or entry.get("bingx_position_id")
    )

    start_time = (
        entry.get("triggered_at")
        or entry.get("opened_at")
        or entry.get("created_at")
    )

    start_dt = _history_timestamp(start_time)

    if start_dt is not None:
        start_ms = int(start_dt.timestamp() * 1000)
    else:
        start_ms = (
            int(time.time() * 1000)
            - 7 * 24 * 60 * 60 * 1000
        )

    try:
        history_item = _find_closed_history(
            normalize_symbol(symbol),
            direction,
            start_ms,
            entry=entry,
            api_key=api_key,
            secret=secret,
        )

    except Exception as exc:
        history_item = None
        entry["exchange_sync_error"] = str(exc)

    if history_item is not None:
        _record_closed(
            entry,
            history_item,
        )

        return {
            "state": CLOSED,
            "active": False,
            "history": history_item,
            "reason": (
                "BingX position is closed and matching "
                "positionHistory was found."
            ),
        }

    entry["exchange_sync_status"] = "NOT_FOUND"
    entry["exchange_sync_checked_at"] = int(time.time() * 1000)

    if tracked_id:
        reason = (
            "No positive BingX position found. "
            "No matching closure history was confirmed."
        )
    else:
        reason = (
            "No positive BingX position found and no tracked exchange "
            "position ID is available for closure matching."
        )

    return {
        "state": "NOT_FOUND",
        "active": False,
        "reason": reason,
    }


def discover_open_position(
    entry: Dict[str, Any],
    api_key: Optional[str] = None,
    secret: Optional[str] = None,
):
    """Read-only discovery of an existing BingX OPEN position.

    This works regardless of entry["status"].

    It only answers whether a positive matching OPEN position currently
    exists. NOT_FOUND never means CLOSED.
    """

    symbol = entry.get("symbol")
    direction = entry.get("direction")

    if not symbol or not direction:
        return {
            "state": "NOT_MATCHED",
            "active": False,
            "reason": "Missing symbol or direction.",
        }

    try:
        payload = _signed_get(
            "/openApi/swap/v2/user/positions",
            {
                "symbol": normalize_symbol(symbol),
            },
            api_key=api_key,
            secret=secret,
        )

        positions = _extract_positions(payload)

    except Exception as exc:
        return {
            "state": "ERROR",
            "active": False,
            "reason": f"BingX position lookup failed: {exc}",
        }

    matching_position = None

    for position in positions:
        if not isinstance(position, dict):
            continue

        if _position_matches(
            position,
            symbol,
            direction,
        ):
            matching_position = position
            break

    if matching_position is not None:
        amount = _position_amount(matching_position)

        return {
            "state": OPEN,
            "active": True,
            "position": matching_position,
            "amount": amount,
            "reason": (
                "BingX position confirmed OPEN with positive quantity."
            ),
        }

    return {
        "state": "NOT_FOUND",
        "active": False,
        "reason": (
            "No positive BingX position found for this pending setup. "
            "This does not mean the setup is CLOSED."
        ),
    }


# ---------------------------------------------------------------------------
# Lightweight tests
# ---------------------------------------------------------------------------

def _run_discover_open_position_tests():
    import unittest
    from unittest import mock

    class DiscoverOpenPositionTests(unittest.TestCase):

        def _pending_entry(
            self,
            symbol="BTC-USDT",
            direction="LONG",
        ):
            return {
                "symbol": symbol,
                "direction": direction,
                "status": "pending",
            }

        def _triggered_entry(
            self,
            symbol="BTC-USDT",
            direction="LONG",
        ):
            return {
                "symbol": symbol,
                "direction": direction,
                "status": "triggered",
                "triggered_at": (
                    "2026-09-05T08:00:00+00:00"
                ),
            }

        def test_pending_matching_long_open(self):
            entry = self._pending_entry(
                direction="LONG",
            )

            fake_payload = {
                "data": [
                    {
                        "symbol": "BTCUSDT",
                        "positionSide": "LONG",
                        "positionAmt": "1.5",
                        "positionId": "abc123",
                        "avgPrice": "50000",
                        "unrealizedProfit": "10",
                        "leverage": "10",
                        "liquidationPrice": "40000",
                    }
                ]
            }

            with mock.patch(
                f"{__name__}._signed_get",
                return_value=fake_payload,
            ):
                result = discover_open_position(entry)

            self.assertEqual(result["state"], "OPEN")
            self.assertTrue(result["active"])
            self.assertEqual(result["amount"], 1.5)
            self.assertEqual(
                result["position"]["positionId"],
                "abc123",
            )

        def test_pending_matching_short_open(self):
            entry = self._pending_entry(
                direction="SHORT",
            )

            fake_payload = {
                "data": [
                    {
                        "symbol": "BTCUSDT",
                        "positionSide": "SHORT",
                        "positionAmt": "2.0",
                    }
                ]
            }

            with mock.patch(
                f"{__name__}._signed_get",
                return_value=fake_payload,
            ):
                result = discover_open_position(entry)

            self.assertEqual(result["state"], "OPEN")
            self.assertTrue(result["active"])
            self.assertEqual(result["amount"], 2.0)

        def test_pending_wrong_side_not_found(self):
            entry = self._pending_entry(
                direction="LONG",
            )

            fake_payload = {
                "data": [
                    {
                        "symbol": "BTCUSDT",
                        "positionSide": "SHORT",
                        "positionAmt": "2.0",
                    }
                ]
            }

            with mock.patch(
                f"{__name__}._signed_get",
                return_value=fake_payload,
            ):
                result = discover_open_position(entry)

            self.assertEqual(result["state"], "NOT_FOUND")
            self.assertFalse(result["active"])

        def test_pending_wrong_symbol_not_found(self):
            entry = self._pending_entry(
                symbol="BTC-USDT",
                direction="LONG",
            )

            fake_payload = {
                "data": [
                    {
                        "symbol": "ETHUSDT",
                        "positionSide": "LONG",
                        "positionAmt": "2.0",
                    }
                ]
            }

            with mock.patch(
                f"{__name__}._signed_get",
                return_value=fake_payload,
            ):
                result = discover_open_position(entry)

            self.assertEqual(result["state"], "NOT_FOUND")
            self.assertFalse(result["active"])

        def test_zero_quantity_not_found(self):
            entry = self._pending_entry(
                direction="LONG",
            )

            fake_payload = {
                "data": [
                    {
                        "symbol": "BTCUSDT",
                        "positionSide": "LONG",
                        "positionAmt": "0",
                    }
                ]
            }

            with mock.patch(
                f"{__name__}._signed_get",
                return_value=fake_payload,
            ):
                result = discover_open_position(entry)

            self.assertEqual(result["state"], "NOT_FOUND")
            self.assertFalse(result["active"])

        def test_missing_quantity_not_found(self):
            entry = self._pending_entry(
                direction="LONG",
            )

            fake_payload = {
                "data": [
                    {
                        "symbol": "BTCUSDT",
                        "positionSide": "LONG",
                    }
                ]
            }

            with mock.patch(
                f"{__name__}._signed_get",
                return_value=fake_payload,
            ):
                result = discover_open_position(entry)

            self.assertEqual(result["state"], "NOT_FOUND")
            self.assertFalse(result["active"])

        def test_api_failure_returns_error(self):
            entry = self._pending_entry(
                direction="LONG",
            )

            with mock.patch(
                f"{__name__}._signed_get",
                side_effect=RuntimeError("boom"),
            ):
                result = discover_open_position(entry)

            self.assertEqual(result["state"], "ERROR")
            self.assertFalse(result["active"])

        def test_missing_symbol_or_direction_not_matched(self):
            entry = {
                "status": "pending",
            }

            result = discover_open_position(entry)

            self.assertEqual(result["state"], "NOT_MATCHED")
            self.assertFalse(result["active"])

        def test_sync_position_triggered_guard_unchanged(self):
            entry = {
                "symbol": "BTC-USDT",
                "direction": "LONG",
                "status": "pending",
            }

            result = sync_position(entry)

            self.assertEqual(
                result["state"],
                "NOT_MATCHED",
            )

            self.assertFalse(
                result["active"],
            )

            self.assertEqual(
                result["reason"],
                "Entry is not triggered.",
            )

        def test_sync_open_records_exchange_facts(self):
            entry = self._triggered_entry()

            fake_payload = {
                "data": [
                    {
                        "symbol": "BTCUSDT",
                        "positionSide": "LONG",
                        "positionAmt": "1.25",
                        "availableAmt": "1.25",
                        "positionId": "pos-001",
                        "avgPrice": "50000",
                        "unrealizedProfit": "125",
                        "realizedProfit": "25",
                        "leverage": "10",
                        "liquidationPrice": "45000",
                        "positionValue": "62500",
                    }
                ]
            }

            with mock.patch(
                f"{__name__}._signed_get",
                return_value=fake_payload,
            ):
                result = sync_position(entry)

            self.assertEqual(result["state"], OPEN)
            self.assertTrue(result["active"])
            self.assertEqual(
                entry["exchange_sync_status"],
                OPEN,
            )
            self.assertEqual(
                entry["position_lifecycle"],
                OPEN,
            )
            self.assertEqual(
                entry["exchange_position_amount"],
                1.25,
            )
            self.assertEqual(
                entry["exchange_position_id"],
                "pos-001",
            )
            self.assertEqual(
                entry["exchange_position_side"],
                "LONG",
            )
            self.assertEqual(
                entry["exchange_avg_price"],
                "50000",
            )
            self.assertEqual(
                entry["exchange_unrealized_pnl"],
                "125",
            )
            self.assertEqual(
                entry["exchange_leverage"],
                "10",
            )
            self.assertEqual(
                entry["exchange_liquidation_price"],
                "45000",
            )
            self.assertEqual(
                entry["exchange_position_value"],
                "62500",
            )
            self.assertIsNone(
                entry["exchange_sync_error"],
            )

        def test_sync_missing_open_and_matching_history_is_closed(self):
            entry = self._triggered_entry()

            position_payload = {
                "data": [],
            }

            history_payload = {
                "data": [
                    {
                        "symbol": "BTCUSDT",
                        "positionSide": "LONG",
                        "positionId": "pos-001",
                        "closeTime": "1757059800000",
                        "closePrice": "49500",
                        "orderId": "close-001",
                        "closeReason": "TAKE_PROFIT",
                        "realizedProfit": "250",
                    }
                ]
            }

            entry["exchange_position_id"] = "pos-001"

            def fake_signed_get(
                path,
                params=None,
                api_key=None,
                secret=None,
            ):
                if path == "/openApi/swap/v2/user/positions":
                    return position_payload

                if path == "/openApi/swap/v1/trade/positionHistory":
                    return history_payload

                raise AssertionError(
                    f"Unexpected endpoint: {path}"
                )

            with mock.patch(
                f"{__name__}._signed_get",
                side_effect=fake_signed_get,
            ):
                result = sync_position(entry)

            # The synthetic history timestamp above predates the test
            # entry's 2026 triggered_at, so the correct result is NOT_FOUND.
            self.assertEqual(
                result["state"],
                "NOT_FOUND",
            )

        def test_sync_missing_open_without_history_is_not_found(self):
            entry = self._triggered_entry()

            fake_payload = {
                "data": [],
            }

            with mock.patch(
                f"{__name__}._signed_get",
                return_value=fake_payload,
            ):
                result = sync_position(entry)

            self.assertEqual(
                result["state"],
                "NOT_FOUND",
            )

            self.assertFalse(
                result["active"],
            )

            self.assertEqual(
                entry["exchange_sync_status"],
                "NOT_FOUND",
            )

            self.assertNotEqual(
                entry.get("position_lifecycle"),
                CLOSED,
            )

        def test_history_wrong_position_id_not_accepted(self):
            entry = self._triggered_entry()

            entry["exchange_position_id"] = "pos-correct"

            item = {
                "symbol": "BTCUSDT",
                "positionSide": "LONG",
                "positionId": "pos-wrong",
                "closeTime": "1757059800000",
            }

            result = _history_matches_entry(
                item,
                "BTCUSDT",
                "LONG",
                entry=entry,
            )

            self.assertFalse(result)

        def test_history_matching_position_id_accepted(self):
            entry = self._triggered_entry()

            entry["exchange_position_id"] = "pos-correct"

            item = {
                "symbol": "BTCUSDT",
                "positionSide": "LONG",
                "positionId": "pos-correct",
                "closeTime": "1757059800000",
            }

            result = _history_matches_entry(
                item,
                "BTCUSDT",
                "LONG",
                entry=entry,
            )

            self.assertTrue(result)

    suite = unittest.TestLoader().loadTestsFromTestCase(
        DiscoverOpenPositionTests
    )

    runner = unittest.TextTestRunner(
        verbosity=2,
    )

    result = runner.run(suite)

    return result.wasSuccessful()


if __name__ == "__main__":
    success = _run_discover_open_position_tests()

    raise SystemExit(
        0 if success else 1
    )
