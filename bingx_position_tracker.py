"""ATHENA BingX position lifecycle tracker.

Read-only exchange synchronization layer.

This module:
- Reads BingX position/history data.
- Confirms OPEN only when a matching position has a verified positive amount.
- Records exchange closures without changing SMC setup fields.
- Preserves setup status/history.
- Never places, cancels, closes, or modifies trades.
"""

from __future__ import annotations

import hashlib
import hmac
import json
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
    if isinstance(payload, list):
        return payload

    if not isinstance(payload, dict):
        return []

    data = payload.get("data")

    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        for key in ("positions", "position", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return value

    for key in ("positions", "position"):
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

    if position_side and requested_side and position_side != requested_side:
        return False

    amount = _position_amount(position)

    # Missing quantity is NOT proof of an open position.
    if amount is None:
        return False

    return amount > 0


def _history_timestamp(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None

    if isinstance(value, (int, float)):
        try:
            timestamp = float(value)

            if timestamp > 10_000_000_000:
                timestamp /= 1000

            return datetime.fromtimestamp(timestamp, tz=timezone.utc)
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


def _history_close_timestamp(item: Dict[str, Any]) -> Optional[int]:
    for key in (
        "closeTime",
        "closedAt",
        "closeTimestamp",
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


def _history_position_id(item: Dict[str, Any]) -> Optional[str]:
    for key in (
        "positionId",
        "positionID",
        "position_id",
        "posId",
        "positionUid",
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

    if item_side and requested_side and item_side != requested_side:
        return False

    entry = entry or {}

    tracked_position_id = (
        entry.get("exchange_position_id")
        or entry.get("position_id")
        or entry.get("bingx_position_id")
    )

    history_position_id = _history_position_id(item)

    # If we have a tracked exchange position ID, require it to match.
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
    payload = _signed_get(
        "/openApi/swap/v1/trade/allOrders",
        {
            "symbol": normalize_symbol(symbol),
            "limit": 100,
            "startTime": max(0, int(start_ms)),
        },
        api_key=api_key,
        secret=secret,
    )

    if not isinstance(payload, dict):
        return None

    data = payload.get("data")

    if isinstance(data, dict):
        candidates = (
            data.get("orders")
            or data.get("order")
            or data.get("data")
            or []
        )
    elif isinstance(data, list):
        candidates = data
    else:
        candidates = []

    if not isinstance(candidates, list):
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

        status = str(
            item.get("status")
            or item.get("orderStatus")
            or ""
        ).upper()

        if status not in {
            "FILLED",
            "CLOSED",
            "TRIGGERED",
            "PARTIALLY_FILLED",
        }:
            continue

        close_time = _history_close_timestamp(item)

        if close_time is None:
            continue

        if close_time < int(start_ms):
            continue

        if close_time > best_time:
            best = item
            best_time = close_time

    return best


def _infer_close_reason(history_item: Optional[Dict[str, Any]]) -> str:
    if not history_item:
        return "EXCHANGE_CLOSED"

    text = " ".join(
        str(history_item.get(key, ""))
        for key in (
            "orderType",
            "type",
            "orderStatus",
            "status",
            "stopType",
            "closePosition",
        )
    ).upper()

    if "TAKE_PROFIT" in text or "TP" in text:
        return "TAKE_PROFIT"

    if "STOP_LOSS" in text or "STOP" in text or "SL" in text:
        return "STOP_LOSS"

    if "LIQUID" in text:
        return "LIQUIDATION"

    return "EXCHANGE_CLOSED"


def _record_closed(
    entry: Dict[str, Any],
    history_item: Optional[Dict[str, Any]] = None,
):
    history_item = history_item or {}

    close_time = _history_close_timestamp(history_item)

    if close_time is None:
        close_time = int(time.time() * 1000)

    close_reason = _infer_close_reason(history_item)

    close_price = (
        history_item.get("avgPrice")
        or history_item.get("averagePrice")
        or history_item.get("price")
        or history_item.get("stopPrice")
    )

    order_id = (
        history_item.get("orderId")
        or history_item.get("orderID")
        or history_item.get("order_id")
    )

    # IMPORTANT:
    # Do not mutate generic setup status here.
    # A setup may remain historically valid/invalid independently
    # of whether its exchange position has closed.
    entry["exchange_sync_status"] = CLOSED
    entry["position_lifecycle"] = CLOSED
    entry["exchange_closed_at"] = close_time
    entry["position_exit_reason"] = close_reason

    if close_price not in (None, ""):
        entry["exchange_close_price"] = close_price

    if order_id not in (None, ""):
        entry["exchange_close_order_id"] = str(order_id)

    entry["position_close_reported"] = False

    return entry


def sync_position(
    entry: Dict[str, Any],
    api_key: Optional[str] = None,
    secret: Optional[str] = None,
):
    """Synchronize one triggered position against BingX.

    This function is strictly read-only.
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
            {"symbol": normalize_symbol(symbol)},
            api_key=api_key,
            secret=secret,
        )

        positions = _extract_positions(payload)

    except Exception as exc:
        entry["exchange_sync_status"] = "ERROR"

        return {
            "state": "ERROR",
            "active": False,
            "reason": f"BingX position lookup failed: {exc}",
        }

    matching_position = None

    for position in positions:
        if not isinstance(position, dict):
            continue

        if _position_matches(position, symbol, direction):
            matching_position = position
            break

    if matching_position is not None:
        amount = _position_amount(matching_position)

        entry["exchange_sync_status"] = OPEN
        entry["position_lifecycle"] = OPEN

        if amount is not None:
            entry["exchange_position_amount"] = amount

        position_id = _history_position_id(matching_position)

        if position_id:
            entry["exchange_position_id"] = position_id

        entry["position_close_reported"] = False

        return {
            "state": OPEN,
            "active": True,
            "position": matching_position,
            "amount": amount,
            "reason": "BingX position confirmed OPEN with positive quantity.",
        }

    # No positive open position found.
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
        start_ms = int(time.time() * 1000) - 7 * 24 * 60 * 60 * 1000

    try:
        history_item = _find_closed_history(
            normalize_symbol(symbol),
            direction,
            start_ms,
            entry=entry,
            api_key=api_key,
            secret=secret,
        )

    except Exception:
        history_item = None

    if history_item is not None:
        _record_closed(entry, history_item)

        return {
            "state": CLOSED,
            "active": False,
            "history": history_item,
            "reason": "BingX position is closed and matching exchange history was found.",
        }

    # Do NOT guess that a position is closed merely because history
    # could not be matched. The exchange state remains unknown.
    entry["exchange_sync_status"] = "NOT_FOUND"

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
    """Read-only discovery: does BingX currently have a positively matched
    OPEN position corresponding to this watchlist setup?

    Unlike sync_position(), this works regardless of entry["status"] -- it
    exists specifically to discover an OPEN position for a *pending* (not
    yet locally triggered) setup, so a real BingX fill can be discovered
    even before the local watchlist believes the setup has triggered.

    This function is strictly read-only and observational:
    - It never places, cancels, closes, reduces, or modifies any order
      or position.
    - It never mutates SMC score, direction, preferred entry, zone, SL,
      TP, execution state, or any other setup-generation field.
    - It never performs closed-history lookup; it only answers whether
      an OPEN position currently exists. A NOT_FOUND result must never
      be interpreted as CLOSED.

    Returns a dict with one of:
        {"state": "NOT_MATCHED", "active": False, "reason": ...}
        {"state": "OPEN", "active": True, "position": ..., "amount": ...,
         "reason": ...}
        {"state": "NOT_FOUND", "active": False, "reason": ...}
        {"state": "ERROR", "active": False, "reason": ...}
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
            {"symbol": normalize_symbol(symbol)},
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

        if _position_matches(position, symbol, direction):
            matching_position = position
            break

    if matching_position is not None:
        amount = _position_amount(matching_position)

        return {
            "state": OPEN,
            "active": True,
            "position": matching_position,
            "amount": amount,
            "reason": "BingX position confirmed OPEN with positive quantity.",
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
# Tests for discover_open_position()
#
# These are lightweight, dependency-free tests (no pytest/unittest fixtures
# required) that monkeypatch _signed_get() to avoid any real network call.
# Run directly with: python bingx_position_tracker.py
# ---------------------------------------------------------------------------

def _run_discover_open_position_tests():
    import unittest
    from unittest import mock

    class DiscoverOpenPositionTests(unittest.TestCase):
        def _pending_entry(self, symbol="BTC-USDT", direction="LONG"):
            return {"symbol": symbol, "direction": direction, "status": "pending"}

        def test_pending_matching_long_open(self):
            entry = self._pending_entry(direction="LONG")
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
            with mock.patch(f"{__name__}._signed_get", return_value=fake_payload):
                result = discover_open_position(entry)
            self.assertEqual(result["state"], "OPEN")
            self.assertTrue(result["active"])
            self.assertEqual(result["amount"], 1.5)
            self.assertEqual(result["position"]["positionId"], "abc123")

        def test_pending_matching_short_open(self):
            entry = self._pending_entry(direction="SHORT")
            fake_payload = {
                "data": [
                    {
                        "symbol": "BTCUSDT",
                        "positionSide": "SHORT",
                        "positionAmt": "2.0",
                    }
                ]
            }
            with mock.patch(f"{__name__}._signed_get", return_value=fake_payload):
                result = discover_open_position(entry)
            self.assertEqual(result["state"], "OPEN")
            self.assertTrue(result["active"])
            self.assertEqual(result["amount"], 2.0)

        def test_pending_wrong_side_not_found(self):
            entry = self._pending_entry(direction="LONG")
            fake_payload = {
                "data": [
                    {
                        "symbol": "BTCUSDT",
                        "positionSide": "SHORT",
                        "positionAmt": "2.0",
                    }
                ]
            }
            with mock.patch(f"{__name__}._signed_get", return_value=fake_payload):
                result = discover_open_position(entry)
            self.assertEqual(result["state"], "NOT_FOUND")
            self.assertFalse(result["active"])

        def test_pending_wrong_symbol_not_found(self):
            entry = self._pending_entry(symbol="BTC-USDT", direction="LONG")
            fake_payload = {
                "data": [
                    {
                        "symbol": "ETHUSDT",
                        "positionSide": "LONG",
                        "positionAmt": "2.0",
                    }
                ]
            }
            with mock.patch(f"{__name__}._signed_get", return_value=fake_payload):
                result = discover_open_position(entry)
            self.assertEqual(result["state"], "NOT_FOUND")
            self.assertFalse(result["active"])

        def test_zero_quantity_not_found(self):
            entry = self._pending_entry(direction="LONG")
            fake_payload = {
                "data": [
                    {
                        "symbol": "BTCUSDT",
                        "positionSide": "LONG",
                        "positionAmt": "0",
                    }
                ]
            }
            with mock.patch(f"{__name__}._signed_get", return_value=fake_payload):
                result = discover_open_position(entry)
            self.assertEqual(result["state"], "NOT_FOUND")
            self.assertFalse(result["active"])

        def test_missing_quantity_not_found(self):
            entry = self._pending_entry(direction="LONG")
            fake_payload = {
                "data": [
                    {
                        "symbol": "BTCUSDT",
                        "positionSide": "LONG",
                    }
                ]
            }
            with mock.patch(f"{__name__}._signed_get", return_value=fake_payload):
                result = discover_open_position(entry)
            self.assertEqual(result["state"], "NOT_FOUND")
            self.assertFalse(result["active"])

        def test_api_failure_returns_error(self):
            entry = self._pending_entry(direction="LONG")
            with mock.patch(
                f"{__name__}._signed_get", side_effect=RuntimeError("boom")
            ):
                result = discover_open_position(entry)
            self.assertEqual(result["state"], "ERROR")
            self.assertFalse(result["active"])

        def test_missing_symbol_or_direction_not_matched(self):
            entry = {"status": "pending"}
            result = discover_open_position(entry)
            self.assertEqual(result["state"], "NOT_MATCHED")
            self.assertFalse(result["active"])

        def test_sync_position_triggered_guard_unchanged(self):
            # sync_position() must still refuse anything not locally
            # "triggered", exactly as before this change.
            entry = {
                "symbol": "BTC-USDT",
                "direction": "LONG",
                "status": "pending",
            }
            result = sync_position(entry)
            self.assertEqual(result["state"], "NOT_MATCHED")
            self.assertFalse(result["active"])
            self.assertEqual(result["reason"], "Entry is not triggered.")

    suite = unittest.TestLoader().loadTestsFromTestCase(
        DiscoverOpenPositionTests
    )
    runner = unittest.TextTestRunner(verbosity=2)
    runner.run(suite)


if __name__ == "__main__":
    _run_discover_open_position_tests()
