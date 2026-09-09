#!/usr/bin/env python3
"""Central durable Telegram notification state for ATHENA.

Imported by the GitHub Actions launcher before full_scan.py. It wraps
smc_scanner.send_telegram_message without modifying smc_scanner.py.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATE_PATH = Path(os.environ.get("ATHENA_NOTIFICATION_STATE", "notification_state.json"))
RETENTION_DAYS = 30

EVENT_RE = re.compile(
    r"(NEW SETUP|ACTIVE SETUP UPDATE|INVALIDATED|EXPIRED|POSITION ACTIVE|"
    r"POSITION OPEN|POSITION CLOSED|POSITION HEALTH|POSITION INTELLIGENCE|"
    r"POSITION DIAGNOSTIC|POSITION MONITOR)\s*:\s*"
    r"([A-Z0-9._-]+)\s+([A-Z]+)(?:\s+\[([^\]]+)\])?",
    re.I,
)


def _hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _now():
    return datetime.now(timezone.utc).isoformat()


def _load():
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("events"), dict):
            return data
    except (OSError, ValueError, TypeError):
        pass
    return {"version": 1, "events": {}}


def _save(data):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=".athena-notification-", suffix=".tmp", dir=str(STATE_PATH.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, STATE_PATH)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def _prune(data):
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    kept = {}
    for key, stamp in data.get("events", {}).items():
        try:
            if datetime.fromisoformat(str(stamp).replace("Z", "+00:00")) >= cutoff:
                kept[key] = stamp
        except (TypeError, ValueError):
            pass
    data["events"] = kept


def _watchlist():
    try:
        import smc_scanner as scanner
        return [x for x in (scanner.load_watchlist() or []) if isinstance(x, dict)]
    except Exception:
        return []


def _registry():
    try:
        data = json.loads(Path("position_registry.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _find_entry(symbol, direction, trade, terminal=None):
    candidates = []
    for item in _watchlist():
        if str(item.get("symbol", "")).upper() != symbol:
            continue
        if str(item.get("direction", "")).upper() != direction:
            continue
        if trade and str(item.get("trade_type", "")).upper() != trade:
            continue
        if terminal and str(item.get("status", "")).lower() != terminal:
            continue
        candidates.append(item)
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda x: str(
            x.get("invalidated_at")
            or x.get("expired_at")
            or x.get("triggered_at")
            or x.get("added_at")
            or ""
        ),
    )


def _position_id(symbol, direction):
    for item in _watchlist():
        if (
            str(item.get("symbol", "")).upper() == symbol
            and str(item.get("direction", "")).upper() == direction
        ):
            pid = (
                item.get("exchange_position_id")
                or item.get("position_id")
                or item.get("bingx_position_id")
            )
            if pid:
                return str(pid)

    positions = _registry().get("positions", {})
    if isinstance(positions, dict):
        for item in positions.values():
            if not isinstance(item, dict):
                continue
            if (
                str(item.get("symbol", "")).upper() == symbol
                and str(item.get("direction", "")).upper() == direction
                and str(item.get("lifecycle", "")).upper() == "OPEN"
            ):
                pid = item.get("exchange_position_id")
                if pid:
                    return str(pid)
    return None


def event_key(text):
    """Return a stable identity for recognized Telegram lifecycle events."""
    first = text.splitlines()[0] if text else ""
    match = EVENT_RE.search(first)
    if not match:
        return None

    event = match.group(1).upper().replace(" ", "_")
    symbol = match.group(2).upper()
    direction = match.group(3).upper()
    trade = (match.group(4) or "").upper()

    if event in {"INVALIDATED", "EXPIRED"}:
        item = _find_entry(symbol, direction, trade, event.lower())
        fingerprint = item.get("setup_fingerprint") if item else None
        stop = item.get("invalidation") if item else None
        identity = fingerprint or stop
        if not identity:
            stable = [
                line.strip()
                for line in text.splitlines()
                if "Triggered on" not in line
                and not re.search(r"price\s+[0-9.eE+-]+", line, re.I)
            ]
            identity = _hash("\n".join(stable))
        return f"setup-terminal:{symbol}:{direction}:{trade}:{identity}"

    if event in {"NEW_SETUP", "ACTIVE_SETUP_UPDATE"}:
        item = _find_entry(symbol, direction, trade)
        fingerprint = item.get("setup_fingerprint") if item else None
        epoch = (
            (item.get("added_at") or item.get("created_at"))
            if item else None
        )
        identity = (
            f"{fingerprint}:{epoch}"
            if fingerprint or epoch
            else _hash("\n".join(
                line.strip()
                for line in text.splitlines()
                if not re.match(r"^\s*Current Score:", line, re.I)
            ))
        )
        return f"setup:{event}:{symbol}:{direction}:{trade}:{identity}"

    if event in {"POSITION_OPEN", "POSITION_ACTIVE", "POSITION_CLOSED"}:
        pid = _position_id(symbol, direction)
        return f"position:{event}:{pid or f'{symbol}:{direction}'}"

    if event in {
        "POSITION_HEALTH",
        "POSITION_INTELLIGENCE",
        "POSITION_DIAGNOSTIC",
        "POSITION_MONITOR",
    }:
        stable = []
        for line in text.splitlines():
            if re.match(
                r"^\s*(Current R|Max R|R|Unrealized P&L|Price|Score|"
                r"Duration|Time in trade|SL distance)\s*:",
                line,
                re.I,
            ):
                continue
            stable.append(line.strip())
        return (
            f"position-context:{event}:{symbol}:{direction}:"
            f"{_hash(chr(10).join(stable))}"
        )

    return None


def _already_sent(key):
    data = _load()
    _prune(data)
    result = key in data["events"]
    _save(data)
    return result


def _record(key):
    data = _load()
    _prune(data)
    data["events"][key] = _now()
    _save(data)


def install():
    import smc_scanner as scanner

    if getattr(scanner, "_ATHENA_NOTIFICATION_STATE_INSTALLED", False):
        return

    original = scanner.send_telegram_message

    def guarded_send(text):
        key = event_key(text) if isinstance(text, str) else None

        if key and _already_sent(key):
            print(f"  [notification-state] SUPPRESSED duplicate: {key}")
            return True

        ok = bool(original(text))

        # Record only after Telegram succeeds. Failed sends remain retryable.
        if ok and key:
            _record(key)
        return ok

    scanner.send_telegram_message = guarded_send
    scanner._ATHENA_NOTIFICATION_STATE_INSTALLED = True
    print("ATHENA central notification state: INSTALLED")


if __name__ != "__main__":
    try:
        install()
    except Exception as exc:
        print(
            f"! ATHENA notification state unavailable: "
            f"{type(exc).__name__}: {exc}"
        )
