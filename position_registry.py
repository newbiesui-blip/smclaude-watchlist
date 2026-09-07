"""ATHENA exchange-first Position Registry.

Read-only discovery, identity, reconciliation and persistence layer for
BingX positions.  Independent of SMC score, trade type, watchlist
membership and setup lifecycle.

This module:
- Discovers OPEN positions from BingX via the public list_open_positions API
- Registers, reconciles and persists them
- Coordinates lifecycle state (OPEN / NOT_FOUND / API_ERROR / CLOSED)
- Marks orphans (positions with no matching SMC watchlist entry)
- Never places, cancels, closes or modifies trades
- Never sends Telegram messages itself
- Never duplicates health / intelligence / BUG-2 calculations

Notification emission remains the responsibility of the single existing
coordinator (full_scan._sync_watchlist path under feature-flag control).
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import bingx_position_tracker as bingx

HERE = os.path.dirname(os.path.abspath(__file__))
REGISTRY_PATH = os.path.join(HERE, "position_registry.json")
REGISTRY_VERSION = 1

# Feature flag – matches common env-var pattern used elsewhere in the repo.
# When false / unset, registry discovery still works for tests but the
# integration in full_scan is gated.
def registry_enabled() -> bool:
    raw = os.environ.get("USE_POSITION_REGISTRY", "0").strip().lower()
    return raw in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

def _canonical_symbol(symbol: Any) -> str:
    return bingx.normalize_symbol(symbol)


def _stable_fallback_payload(pos: Dict[str, Any]) -> Dict[str, Any]:
    """Return fields that should identify a no-ID exchange position.

    Volatile mark/P&L fields are deliberately excluded so a normal market
    move cannot create a new identity.
    """
    return {
        "symbol": _canonical_symbol(pos.get("symbol")),
        "side": bingx.normalize_side(pos.get("side") or ""),
        "avg_entry_price": pos.get("avg_entry_price"),
        "amount": pos.get("amount"),
    }


def _fallback_base_identity(pos: Dict[str, Any]) -> str:
    payload = _stable_fallback_payload(pos)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()[:20]
    return f"fb:{digest}"


def _make_identity(
    symbol: str,
    side: str,
    exchange_position_id: Optional[str],
    avg_entry_price: Any = None,
    amount: Any = None,
) -> str:
    """Return a safe identity. Exchange position ID is authoritative.

    Without an exchange ID, identity is explicitly a fallback fingerprint.
    Callers must handle duplicate fingerprints as ambiguous instead of
    overwriting one position with another.
    """
    if exchange_position_id and str(exchange_position_id).strip():
        return f"id:{str(exchange_position_id).strip()}"
    return _fallback_base_identity({
        "symbol": symbol,
        "side": side,
        "avg_entry_price": avg_entry_price,
        "amount": amount,
    })


def _group_live_positions(live: List[Dict[str, Any]]) -> List[Tuple[str, Dict[str, Any]]]:
    """Build collision-safe identities for one successful exchange snapshot.

    A duplicate no-ID fingerprint is inherently ambiguous. Preserve every
    record with a deterministic ordinal rather than silently dropping one.
    """
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for pos in live:
        eid = pos.get("exchange_position_id")
        if eid and str(eid).strip():
            grouped.setdefault(f"id:{str(eid).strip()}", []).append(pos)
        else:
            grouped.setdefault(_fallback_base_identity(pos), []).append(pos)

    out: List[Tuple[str, Dict[str, Any]]] = []
    for base, positions in grouped.items():
        if base.startswith("id:") or len(positions) == 1:
            out.append((base, positions[0]))
            continue
        # Duplicate fallback fingerprints cannot be uniquely identified by
        # exchange data. Keep each one distinct and mark the ambiguity.
        for ordinal, pos in enumerate(positions):
            tagged = dict(pos)
            tagged["_identity_ambiguous"] = True
            tagged["_identity_base"] = base
            out.append((f"{base}:{ordinal}", tagged))
    return out


# ---------------------------------------------------------------------------
# Persistence (atomic write, versioned)
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _empty_registry() -> Dict[str, Any]:
    return {
        "version": REGISTRY_VERSION,
        "updated_at": _now_iso(),
        "positions": {},
    }


def load_registry(path: str = REGISTRY_PATH) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return _empty_registry()
        data.setdefault("version", REGISTRY_VERSION)
        data.setdefault("positions", {})
        if not isinstance(data["positions"], dict):
            data["positions"] = {}
        return data
    except FileNotFoundError:
        return _empty_registry()
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        # Corruption → start clean rather than crash the monitoring loop
        return _empty_registry()


def save_registry(registry: Dict[str, Any], path: str = REGISTRY_PATH) -> None:
    registry = dict(registry)
    registry["version"] = REGISTRY_VERSION
    registry["updated_at"] = _now_iso()

    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".registry_", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(registry, fh, indent=2, sort_keys=True, default=str)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Core registry operations
# ---------------------------------------------------------------------------

def _new_entry_from_exchange(pos: Dict[str, Any], orphan: bool = True) -> Dict[str, Any]:
    now = _now_iso()
    return {
        "symbol": pos["symbol"],
        "side": pos["side"],
        "amount": pos["amount"],
        "exchange_position_id": pos.get("exchange_position_id"),
        "avg_entry_price": pos.get("avg_entry_price"),
        "mark_price": pos.get("mark_price"),
        "unrealized_pnl": pos.get("unrealized_pnl"),
        "lifecycle": bingx.OPEN,
        "orphan": bool(orphan),
        "first_seen_at": now,
        "last_seen_at": now,
        "closed_at": None,
        "open_alert_sent": False,   # never assume delivery
        "close_alert_sent": False,
        "source": "exchange_discovery",
        "smc_context": None,        # never fabricate
        "identity_kind": "exchange_id" if pos.get("exchange_position_id") else "fallback",
        "identity_ambiguous": bool(pos.get("_identity_ambiguous", False)),
        "identity_base": pos.get("_identity_base"),
    }


def reconcile(
    registry: Optional[Dict[str, Any]] = None,
    path: str = REGISTRY_PATH,
    api_key: Optional[str] = None,
    secret: Optional[str] = None,
) -> Dict[str, Any]:
    """Reconcile registry against live BingX OPEN positions.

    Returns a result dict:
        {
          "ok": bool,
          "error": str | None,
          "discovered": [...],      # newly registered
          "updated": [...],         # still open
          "not_found": [...],       # missing from successful response
          "closed": [...],          # confirmed closed (history)
          "registry": <full registry>
        }
    """
    if registry is None:
        registry = load_registry(path)

    positions_map: Dict[str, Any] = registry.setdefault("positions", {})
    result = {
        "ok": True,
        "error": None,
        "discovered": [],
        "updated": [],
        "not_found": [],
        "closed": [],
        "registry": registry,
    }

    try:
        live = bingx.list_open_positions(api_key=api_key, secret=secret)
    except Exception as exc:
        # A failed read is not evidence that a live position disappeared.
        # Preserve OPEN exactly and record transport/API diagnostics separately.
        result["ok"] = False
        result["error"] = f"API_ERROR: {type(exc).__name__}: {exc}"
        for entry in positions_map.values():
            if entry.get("lifecycle") == bingx.OPEN:
                entry["last_error"] = result["error"]
                entry["api_error_at"] = _now_iso()
        save_registry(registry, path)
        return result

    live_by_id: Dict[str, Dict[str, Any]] = dict(_group_live_positions(live))

    # If a previously single fallback identity becomes an ambiguous duplicate,
    # retain its historical alert/health state as ordinal zero rather than
    # leaving a stale duplicate behind.
    for identity in list(live_by_id):
        if not identity.endswith(":0"):
            continue
        base = identity[:-2]
        if base in positions_map and identity not in positions_map:
            positions_map[identity] = positions_map.pop(base)

    # Update / discover
    seen_identities = set()
    for identity, pos in live_by_id.items():
        seen_identities.add(identity)
        if identity in positions_map:
            entry = positions_map[identity]
            entry["lifecycle"] = bingx.OPEN
            entry["amount"] = pos["amount"]
            entry["mark_price"] = pos.get("mark_price")
            entry["unrealized_pnl"] = pos.get("unrealized_pnl")
            entry["avg_entry_price"] = pos.get("avg_entry_price") or entry.get("avg_entry_price")
            entry["last_seen_at"] = _now_iso()
            entry.pop("last_error", None)
            entry.pop("api_error_at", None)
            if pos.get("_identity_ambiguous"):
                entry["identity_ambiguous"] = True
                entry["identity_base"] = pos.get("_identity_base")
            result["updated"].append(identity)
        else:
            entry = _new_entry_from_exchange(pos, orphan=True)
            positions_map[identity] = entry
            result["discovered"].append(identity)

    # Missing from a successful live response is NOT_FOUND first. Closure is
    # accepted only when BingX positionHistory confirms the same exchange ID.
    for identity, entry in list(positions_map.items()):
        if identity in seen_identities:
            continue
        if entry.get("lifecycle") not in {bingx.OPEN, "API_ERROR", "NOT_FOUND"}:
            continue

        entry["lifecycle"] = "NOT_FOUND"
        entry["last_seen_at"] = _now_iso()
        result["not_found"].append(identity)

        if entry.get("identity_ambiguous"):
            continue
        try:
            history_item = bingx.confirm_closed_position(entry, api_key=api_key, secret=secret)
        except Exception as exc:
            entry["last_error"] = f"CLOSE_CONFIRMATION_ERROR: {type(exc).__name__}: {exc}"
            entry["close_confirmation_error_at"] = _now_iso()
            continue
        if history_item is not None:
            entry["lifecycle"] = bingx.CLOSED
            entry["closed_at"] = _now_iso()
            entry["close_reason"] = "CONFIRMED_HISTORY"
            entry["close_history"] = history_item
            result["closed"].append(identity)

    save_registry(registry, path)
    result["registry"] = registry
    return result


def mark_open_alert_sent(identity: str, path: str = REGISTRY_PATH) -> None:
    """Record that a POSITION OPEN Telegram was successfully delivered."""
    registry = load_registry(path)
    entry = registry.get("positions", {}).get(identity)
    if entry is not None:
        entry["open_alert_sent"] = True
        entry["open_alert_sent_at"] = _now_iso()
        save_registry(registry, path)


def mark_closed(
    identity: str,
    reason: str = "CONFIRMED_HISTORY",
    path: str = REGISTRY_PATH,
    history_item: Optional[Dict[str, Any]] = None,
) -> bool:
    """Persist CLOSED only when authoritative history evidence is supplied."""
    if reason != "CONFIRMED_HISTORY" or not isinstance(history_item, dict):
        return False
    registry = load_registry(path)
    entry = registry.get("positions", {}).get(identity)
    if entry is None or entry.get("lifecycle") == bingx.CLOSED:
        return False
    entry["lifecycle"] = bingx.CLOSED
    entry["closed_at"] = _now_iso()
    entry["close_reason"] = reason
    entry["close_history"] = history_item
    save_registry(registry, path)
    return True


# ---------------------------------------------------------------------------
# Migration from smc_watchlist.json
# ---------------------------------------------------------------------------

def migrate_from_watchlist(
    watchlist_path: str = None,
    registry_path: str = REGISTRY_PATH,
) -> Dict[str, Any]:
    """Idempotent migration of confirmed OPEN watchlist entries.

    open_alert_sent is deliberately set to False unless there is hard
    evidence of successful Telegram delivery (none currently exists).
    """
    if watchlist_path is None:
        watchlist_path = os.path.join(HERE, "smc_watchlist.json")

    registry = load_registry(registry_path)
    positions_map = registry.setdefault("positions", {})
    migrated = 0
    skipped = 0

    try:
        with open(watchlist_path, "r", encoding="utf-8") as fh:
            items = json.load(fh)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        items = []

    if not isinstance(items, list):
        items = []

    for it in items:
        if not isinstance(it, dict):
            continue
        if str(it.get("exchange_sync_status", "")).upper() != bingx.OPEN:
            continue
        if str(it.get("position_lifecycle", "")).upper() == bingx.CLOSED:
            continue

        symbol = _canonical_symbol(it.get("symbol"))
        side = bingx.normalize_side(
            it.get("exchange_position_side")
            or it.get("direction")
            or ""
        )
        if side not in {"LONG", "SHORT"}:
            skipped += 1
            continue

        exchange_id = (
            it.get("exchange_position_id")
            or it.get("position_id")
            or it.get("bingx_position_id")
        )
        avg = it.get("exchange_avg_price") or it.get("entry_price")
        identity = _make_identity(symbol, side, exchange_id, avg)

        if identity in positions_map:
            skipped += 1
            continue

        # NO reliable persisted Telegram delivery proof exists.
        # Always start with open_alert_sent = False.
        entry = {
            "symbol": symbol,
            "side": side,
            "amount": it.get("exchange_position_amount") or it.get("amount"),
            "exchange_position_id": exchange_id,
            "avg_entry_price": avg,
            "mark_price": it.get("exchange_mark_price") or it.get("current_price"),
            "unrealized_pnl": it.get("exchange_unrealized_pnl"),
            "lifecycle": bingx.OPEN,
            "orphan": False,  # came from watchlist
            "first_seen_at": it.get("triggered_at") or it.get("added_at") or _now_iso(),
            "last_seen_at": _now_iso(),
            "closed_at": None,
            "open_alert_sent": False,
            "close_alert_sent": bool(it.get("position_close_reported")),
            "source": "migrated_from_watchlist",
            "smc_context": {
                "trade_type": it.get("trade_type"),
                "added_score": it.get("added_score"),
                "status": it.get("status"),
            },
        }
        positions_map[identity] = entry
        migrated += 1

    save_registry(registry, registry_path)
    return {
        "migrated": migrated,
        "skipped": skipped,
        "registry": registry,
    }


# ---------------------------------------------------------------------------
# Helpers for the notification coordinator
# ---------------------------------------------------------------------------

def _load_watchlist_items(watchlist_path: Optional[str] = None) -> List[Dict[str, Any]]:
    path = watchlist_path or os.path.join(HERE, "smc_watchlist.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            items = json.load(fh)
        return items if isinstance(items, list) else []
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return []


def classify_smc_links(
    registry: Optional[Dict[str, Any]] = None,
    watchlist_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Mark each registry OPEN position as orphan or SMC-linked.

    Matching priority:
      1. exact exchange_position_id
      2. canonical symbol + side (conservative)

    Never silently merges uncertain positions.
    Updates entry['orphan'] and entry['smc_linked'] in place.
    """
    if registry is None:
        registry = load_registry()
    items = _load_watchlist_items(watchlist_path)

    # Build lookup tables from watchlist
    by_pos_id: Dict[str, Dict[str, Any]] = {}
    by_sym_side: Dict[str, Dict[str, Any]] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        eid = (
            it.get("exchange_position_id")
            or it.get("position_id")
            or it.get("bingx_position_id")
        )
        if eid and str(eid).strip():
            by_pos_id[str(eid).strip()] = it
        sym = _canonical_symbol(it.get("symbol"))
        side = bingx.normalize_side(
            it.get("exchange_position_side") or it.get("direction") or ""
        )
        if sym and side in {"LONG", "SHORT"}:
            by_sym_side[f"{sym}:{side}"] = it

    linked = 0
    orphans = 0
    for identity, entry in registry.get("positions", {}).items():
        if entry.get("lifecycle") != bingx.OPEN:
            continue
        eid = entry.get("exchange_position_id")
        match = None
        if eid and str(eid).strip() in by_pos_id:
            match = by_pos_id[str(eid).strip()]
        else:
            key = f"{_canonical_symbol(entry.get('symbol'))}:{bingx.normalize_side(entry.get('side') or "")}"
            match = by_sym_side.get(key)

        if match is not None:
            entry["orphan"] = False
            entry["smc_linked"] = True
            # Preserve any useful SMC context already present; never fabricate
            if not entry.get("smc_context"):
                entry["smc_context"] = {
                    "trade_type": match.get("trade_type"),
                    "added_score": match.get("added_score"),
                    "status": match.get("status"),
                }
            linked += 1
        else:
            entry["orphan"] = True
            entry["smc_linked"] = False
            orphans += 1

    return {"linked": linked, "orphans": orphans, "registry": registry}


def iter_open_needing_alert(
    registry: Optional[Dict[str, Any]] = None,
    orphans_only: bool = True,
) -> List[Tuple[str, Dict[str, Any]]]:
    """Return (identity, entry) pairs that still need a POSITION OPEN alert.

    By default only true orphans are returned. SMC-linked positions are
    left to the legacy _sync_watchlist path so there is exactly one OPEN
    notification owner per exchange position.
    """
    if registry is None:
        registry = load_registry()
    out = []
    for identity, entry in registry.get("positions", {}).items():
        if entry.get("lifecycle") != bingx.OPEN:
            continue
        if entry.get("open_alert_sent"):
            continue
        if orphans_only and not entry.get("orphan", True):
            continue
        out.append((identity, entry))
    return out


def build_minimal_context_for_health(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Produce the smallest legitimate context for position_health / intelligence.

    Never fabricates targets, invalidation or SMC setup fields for orphans.
    """
    ctx = {
        "symbol": entry.get("symbol"),
        "direction": entry.get("side"),  # health expects direction-like field
        "status": "triggered",
        "exchange_sync_status": entry.get("lifecycle"),
        "exchange_position_id": entry.get("exchange_position_id"),
        "exchange_avg_price": entry.get("avg_entry_price"),
        "exchange_mark_price": entry.get("mark_price"),
        "exchange_unrealized_pnl": entry.get("unrealized_pnl"),
        "exchange_position_amount": entry.get("amount"),
        "position_lifecycle": entry.get("lifecycle"),
        "triggered_at": entry.get("first_seen_at"),
        "orphan": entry.get("orphan", True),
    }
    # Only attach real SMC context if migration supplied it
    smc = entry.get("smc_context")
    if isinstance(smc, dict):
        for k in ("trade_type", "added_score", "status", "invalidation", "validated_targets"):
            if k in smc:
                ctx[k] = smc[k]
    return ctx
