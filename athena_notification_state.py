#!/usr/bin/env python3
"""Central durable Telegram notification state for ATHENA.

This module is imported by the GitHub Actions launcher before full_scan.py.
It owns Telegram transport diagnostics/deduplication without modifying
smc_scanner.py.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

STATE_PATH = Path(os.environ.get("ATHENA_NOTIFICATION_STATE", "notification_state.json"))
RETENTION_DAYS = 30
OPPORTUNITY_REARM_CYCLES = 4

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
            data.setdefault("opportunities", {})
            return data
    except (OSError, ValueError, TypeError):
        pass
    return {"version": 2, "events": {}, "opportunities": {}}


def _save(data):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".athena-notification-", suffix=".tmp", dir=str(STATE_PATH.parent))
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
    for section in ("events", "opportunities"):
        kept = {}
        for key, value in data.get(section, {}).items():
            stamp = value if isinstance(value, str) else value.get("last_seen_at") if isinstance(value, dict) else None
            try:
                if datetime.fromisoformat(str(stamp).replace("Z", "+00:00")) >= cutoff:
                    kept[key] = value
            except (TypeError, ValueError):
                pass
        data[section] = kept


def claim_opportunity_alert(symbol, direction, fingerprint, status, scan_cycle=None):
    """Decide whether a qualifying opportunity merits an alert.

    This does not mark the alert as delivered. Call record_opportunity_alert()
    only after Telegram returns success, so failed delivery remains retryable.
    """
    key = f"{str(symbol).upper()}:{str(direction).upper()}:{fingerprint or 'none'}"
    cycle = int(scan_cycle or 0)
    data = _load(); _prune(data)
    old = data["opportunities"].get(key)

    if old is None:
        return True, "first qualifying observation"

    previous_status = old.get("status")
    last_seen = int(old.get("last_seen_cycle", cycle))
    if previous_status != status:
        return True, f"status {previous_status} -> {status}"
    if cycle and last_seen and cycle - last_seen > OPPORTUNITY_REARM_CYCLES:
        return True, "setup reappeared after absence"
    return False, "unchanged"


def record_opportunity_alert(symbol, direction, fingerprint, status, scan_cycle=None):
    """Persist the latest successfully delivered opportunity state."""
    key = f"{str(symbol).upper()}:{str(direction).upper()}:{fingerprint or 'none'}"
    data = _load(); _prune(data)
    data["opportunities"][key] = {
        "symbol": str(symbol).upper(),
        "direction": str(direction).upper(),
        "fingerprint": fingerprint,
        "status": status,
        "last_seen_cycle": int(scan_cycle or 0),
        "last_seen_at": _now(),
    }
    _save(data)

def event_key(text):
    first = text.splitlines()[0] if text else ""
    match = EVENT_RE.search(first)
    if not match:
        return None
    event = match.group(1).upper().replace(" ", "_")
    symbol = match.group(2).upper()
    direction = match.group(3).upper()
    trade = (match.group(4) or "").upper()
    stable = []
    for line in text.splitlines():
        if re.match(r"^\s*(Current R|Max R|R|Unrealized P&L|Price|Score|Duration|Time in trade|SL distance)\s*:", line, re.I):
            continue
        stable.append(line.strip())
    return f"{event}:{symbol}:{direction}:{trade}:{_hash(chr(10).join(stable))}"


def _already_sent(key):
    data = _load(); _prune(data)
    result = key in data["events"]
    _save(data)
    return result


def _record(key):
    data = _load(); _prune(data)
    data["events"][key] = _now()
    _save(data)


def _telegram_send(text):
    token = os.environ.get("TG_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TG_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("  [telegram] NOT_SENT: TG_BOT_TOKEN or TG_CHAT_ID is missing.")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        resp = requests.post(url, data={"chat_id": chat_id, "text": text, "disable_web_page_preview": True}, timeout=10)
    except requests.RequestException as exc:
        print(f"  [telegram] NOT_SENT: transport error: {type(exc).__name__}: {exc}")
        return False
    if resp.status_code == 200:
        try:
            payload = resp.json()
            if payload.get("ok") is True:
                return True
            print(f"  [telegram] NOT_SENT: HTTP 200 but Telegram returned ok={payload.get('ok')!r}.")
        except ValueError:
            print("  [telegram] NOT_SENT: HTTP 200 with non-JSON response.")
        return False
    detail = ""
    try:
        payload = resp.json()
        desc = payload.get("description")
        if desc:
            detail = f"; {desc}"
    except ValueError:
        pass
    print(f"  [telegram] NOT_SENT: HTTP {resp.status_code}{detail}")
    return False


def install():
    import smc_scanner as scanner
    if getattr(scanner, "_ATHENA_NOTIFICATION_STATE_INSTALLED", False):
        return

    def guarded_send(text):
        key = event_key(text) if isinstance(text, str) else None
        if key and _already_sent(key):
            print(f"  [notification-state] SUPPRESSED duplicate: {key}")
            return True
        ok = _telegram_send(text)
        if ok and key:
            _record(key)
        return ok

    scanner.send_telegram_message = guarded_send
    scanner._ATHENA_NOTIFICATION_STATE_INSTALLED = True
    scanner.TELEGRAM_ENABLED = bool(os.environ.get("TG_BOT_TOKEN", "").strip() and os.environ.get("TG_CHAT_ID", "").strip())
    print("ATHENA central notification state: INSTALLED")


if __name__ != "__main__":
    try:
        install()
    except Exception as exc:
        print(f"! ATHENA notification state unavailable: {type(exc).__name__}: {exc}")
