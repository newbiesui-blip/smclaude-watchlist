"""Central notification-state compatibility layer for ATHENA.

This module is intentionally self-contained. The production workflow imports
it before ``full_scan`` starts, so it must never depend on a second notification
module that may not be present in the repository.

Responsibilities:
- patch the legacy scanner lifecycle gate so qualifying WAIT/NEAR/READY
  opportunities can produce an alert;
- provide persistent, cross-run opportunity deduplication;
- commit an opportunity to persistent state only after Telegram confirms the
  send succeeded;
- never touch order/position execution logic;
- never modify ``smc_scanner.py`` itself.

Telegram transport remains owned by ``smc_scanner.send_telegram_message``.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notification_state.json")
OPPORTUNITY_PREFIX = "opportunity:"
OPPORTUNITY_REARM_CYCLES = 4

# Claims made during this process but not yet successfully delivered to
# Telegram. They are deliberately in-memory: a failed send must be retryable
# on the next scan rather than being permanently marked as alerted.
_PENDING = {}

_SETUP_HEADER_RE = re.compile(
    r"^(?:🆕 NEW SETUP:|🔄 ACTIVE SETUP UPDATE:)\s+([^\s]+)\s+([^\s]+)\s+\[",
    re.MULTILINE,
)
_STATUS_RE = re.compile(r"^✅\s+([A-Z_]+)\s*$", re.MULTILINE)


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
    if not isinstance(data.get("events"), dict):
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


def _is_recent_duplicate(previous, scan_cycle) -> bool:
    if previous is None:
        return False
    previous_cycle = None
    if isinstance(previous, dict):
        previous_cycle = _cycle_int(previous.get("scan_cycle"))
    if scan_cycle is None or previous_cycle is None:
        # Legacy timestamp-only event records are treated as already alerted.
        return True
    return scan_cycle - previous_cycle <= OPPORTUNITY_REARM_CYCLES


def claim_opportunity_alert(symbol, direction, fingerprint, status, scan_cycle=None):
    """Reserve an opportunity for this process without marking it delivered.

    The persistent record is written only by the Telegram send wrapper after a
    successful API response. This gives us true retry-on-send-failure behavior.
    """
    key = event_key(symbol, direction, fingerprint, status)
    if key in _PENDING:
        return False, "opportunity already queued this scan"

    data = _load_state()
    previous = data["events"].get(key)
    current_cycle = _cycle_int(scan_cycle)
    if _is_recent_duplicate(previous, current_cycle):
        return False, "existing opportunity -- unchanged"

    _PENDING[key] = {
        "alerted_at": _now_iso(),
        "scan_cycle": current_cycle,
        "symbol": symbol,
        "direction": direction,
        "status": status,
        "fingerprint": fingerprint,
    }
    if previous is None:
        return True, "new qualifying opportunity"
    return True, "opportunity reappeared after cooldown"


def record_opportunity_alert(symbol, direction, fingerprint, status, scan_cycle=None):
    """Persist a successfully delivered opportunity alert."""
    key = event_key(symbol, direction, fingerprint, status)
    record = _PENDING.pop(key, None) or {
        "alerted_at": _now_iso(),
        "scan_cycle": _cycle_int(scan_cycle),
        "symbol": symbol,
        "direction": direction,
        "status": status,
        "fingerprint": fingerprint,
    }
    data = _load_state()
    data["events"][key] = record
    _save_state(data)
    return True


def _commit_sent_setup_from_message(text: str) -> None:
    """Commit the pending opportunity corresponding to a successful setup send."""
    header = _SETUP_HEADER_RE.search(text or "")
    status_match = _STATUS_RE.search(text or "")
    if not header or not status_match:
        return

    symbol = header.group(1)
    direction = header.group(2)
    status = status_match.group(1)

    # Match by the human-readable identity fields. Fingerprint remains the
    # dedup identity but is not embedded in Telegram text.
    matches = [
        (key, value)
        for key, value in _PENDING.items()
        if str(value.get("symbol")) == symbol
        and str(value.get("direction")) == direction
        and str(value.get("status")) == status
    ]
    if not matches:
        return

    key, record = matches[0]
    data = _load_state()
    data["events"][key] = record
    _PENDING.pop(key, None)
    _save_state(data)


try:
    import smc_scanner as scanner

    # Keep the existing scanner Telegram transport. We only add a post-success
    # bookkeeping hook for setup alerts. Position-health/open/close messages
    # pass through untouched.
    _original_send_telegram = getattr(scanner, "send_telegram_message", None)
    if callable(_original_send_telegram) and not getattr(scanner, "_ATHENA_NOTIFICATION_SEND_PATCHED", False):

        def _send_with_opportunity_commit(text):
            sent = bool(_original_send_telegram(text))
            if sent:
                _commit_sent_setup_from_message(str(text or ""))
            return sent

        scanner.send_telegram_message = _send_with_opportunity_commit
        scanner._ATHENA_NOTIFICATION_SEND_PATCHED = True

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
    # starting. full_scan imports smc_scanner independently immediately after.
    print(
        f"! ATHENA lifecycle compatibility patch unavailable: "
        f"{type(exc).__name__}: {exc}"
    )
