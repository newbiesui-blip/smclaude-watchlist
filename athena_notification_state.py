"""Central notification-state compatibility layer for ATHENA.

This module is intentionally self-contained.  The production workflow imports
it before ``full_scan`` starts, so it must never depend on a second notification
module that may not be present in the repository.

Responsibilities:
- patch the legacy scanner lifecycle gate so qualifying WAIT/NEAR/READY
  opportunities can produce an alert;
- provide persistent, cross-run opportunity deduplication;
- never touch order/position execution logic;
- never modify ``smc_scanner.py`` itself.

Telegram transport remains owned by ``smc_scanner.send_telegram_message``.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notification_state.json")
OPPORTUNITY_PREFIX = "opportunity:"
OPPORTUNITY_REARM_CYCLES = 4


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            data = {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        data = {}
    events = data.get("events")
    if not isinstance(events, dict):
        data["events"] = {}
    return data


def _save_state(data: dict) -> None:
    directory = os.path.dirname(STATE_FILE) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".notification_state.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, STATE_FILE)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def event_key(symbol, direction, fingerprint, status) -> str:
    """Return the stable identity of one opportunity alert state."""
    return (
        f"{OPPORTUNITY_PREFIX}{str(symbol or '').upper()}"
        f":{str(direction or '').upper()}"
        f":{str(status or '').upper()}"
        f":{str(fingerprint or 'NO_FINGERPRINT')}"
    )


def _cycle_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def claim_opportunity_alert(symbol, direction, fingerprint, status, scan_cycle=None):
    """Atomically decide whether a qualifying opportunity should alert.

    The claim is persisted only after the decision is made.  This gives us
    deterministic cross-run deduplication for scheduled GitHub Actions runs.
    Status/fingerprint changes generate a new key naturally.  If the exact
    same opportunity disappears for more than ``OPPORTUNITY_REARM_CYCLES``
    scans and later returns, it is allowed to alert again.
    """
    key = event_key(symbol, direction, fingerprint, status)
    data = _load_state()
    events = data["events"]
    previous = events.get(key)
    current_cycle = _cycle_int(scan_cycle)

    if previous is not None:
        previous_cycle = None
        if isinstance(previous, dict):
            previous_cycle = _cycle_int(previous.get("scan_cycle"))
        elif isinstance(previous, str):
            # Legacy timestamp-only records have no cycle information.
            previous_cycle = None

        if current_cycle is None or previous_cycle is None:
            return False, "existing opportunity -- unchanged"
        if current_cycle - previous_cycle <= OPPORTUNITY_REARM_CYCLES:
            return False, "existing opportunity -- unchanged"

        reason = "opportunity reappeared after cooldown"
    else:
        reason = "new qualifying opportunity"

    events[key] = {
        "alerted_at": _now_iso(),
        "scan_cycle": current_cycle,
        "symbol": symbol,
        "direction": direction,
        "status": status,
        "fingerprint": fingerprint,
    }
    _save_state(data)
    return True, reason


def record_opportunity_alert(symbol, direction, fingerprint, status, scan_cycle=None):
    """Compatibility hook for callers that use a two-phase notification API."""
    key = event_key(symbol, direction, fingerprint, status)
    data = _load_state()
    data["events"][key] = {
        "alerted_at": _now_iso(),
        "scan_cycle": _cycle_int(scan_cycle),
        "symbol": symbol,
        "direction": direction,
        "status": status,
        "fingerprint": fingerprint,
    }
    _save_state(data)
    return True


try:
    import smc_scanner as scanner

    _original_lifecycle = getattr(scanner, "update_setup_lifecycle", None)
    if callable(_original_lifecycle) and not getattr(scanner, "_ATHENA_LIFECYCLE_ALERT_PATCHED", False):

        def _lifecycle_with_opportunity_alert(symbol, plan, scan_cycle):
            result = _original_lifecycle(symbol, plan, scan_cycle)
            if not isinstance(result, dict):
                return result

            status = plan.get("status")
            score = float(plan.get("setup_score", plan.get("score", 0)) or 0)
            setup_quality = float(plan.get("setup_quality", 0) or 0)
            actionable_rr = float(plan.get("actionable_rr", 0) or 0)
            trade_type = str(plan.get("trade_type", "INTRADAY")).upper()

            qualifies = (
                status in {
                    "READY_MARKET",
                    "READY_LIMIT",
                    "NEAR_READY",
                    "WAIT_PULLBACK",
                    "WAIT_BREAKOUT",
                    "WAIT_RETEST",
                }
                and score >= float(getattr(scanner, "MIN_SETUP_SCORE", 60))
                and setup_quality >= float(getattr(scanner, "MIN_SETUP_QUALITY", 70))
                and actionable_rr >= float(getattr(scanner, "MIN_STRUCTURAL_RR", 2.0))
                and trade_type not in getattr(scanner, "AUTO_ADD_EXCLUDE_TYPES", set())
            )

            if qualifies:
                fingerprint = result.get("fingerprint") or plan.get("setup_fingerprint")
                should_alert, reason = claim_opportunity_alert(
                    symbol,
                    plan.get("direction"),
                    fingerprint,
                    status,
                    scan_cycle=scan_cycle,
                )
                result["send_alert"] = bool(should_alert)
                result["reason"] = reason

            return result

        scanner.update_setup_lifecycle = _lifecycle_with_opportunity_alert
        scanner._ATHENA_LIFECYCLE_ALERT_PATCHED = True

except Exception as exc:
    # Importing this boundary must never prevent the main scanner from
    # starting.  full_scan imports smc_scanner independently immediately after
    # this module.
    print(
        f"! ATHENA lifecycle compatibility patch unavailable: "
        f"{type(exc).__name__}: {exc}"
    )
