"""ATHENA exchange-first Position Registry.

Read-only discovery, identity, reconciliation and persistence layer for
BingX positions. Independent of SMC score, trade type, watchlist membership,
and setup lifecycle.

The registry owns only exchange discovery/identity/reconciliation/persistence
and orphan coordination. It never places, cancels, closes, modifies, or
sends Telegram messages.
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
REGISTRY_VERSION = 2


def registry_enabled() -> bool:
    return os.environ.get("USE_POSITION_REGISTRY", "0").strip().lower() in {
        "1", "true", "yes", "on"
    }


def _canonical_symbol(symbol: Any) -> str:
    return bingx.normalize_symbol(symbol)


def _numeric_or_original(value: Any) -> Any:
    try:
        number = float(value)
        if number == number and abs(number) != float("inf"):
            return number
    except (TypeError, ValueError):
        pass
    return value


def _identity_payload(symbol: Any, side: Any, avg_entry_price: Any, amount: Any) -> str:
    payload = {
        "symbol": _canonical_symbol(symbol),
        "side": str(side or "").upper().strip(),
        "avg_entry_price": str(avg_entry_price if avg_entry_price is not None else ""),
        "amount": str(amount if amount is not None else ""),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _fallback_base_identity(symbol: Any, side: Any, avg_entry_price: Any, amount: Any) -> str:
    digest = hashlib.sha256(
        _identity_payload(symbol, side, avg_entry_price, amount).encode("utf-8")
    ).hexdigest()[:24]
    return f"fb:{digest}"


def _make_identity(
    symbol: str,
    side: str,
    exchange_position_id: Optional[str],
    avg_entry_price: Any = None,
    amount: Any = None,
) -> str:
    if exchange_position_id and str(exchange_position_id).strip():
        return f"id:{str(exchange_position_id).strip()}"
    return _fallback_base_identity(symbol, side, avg_entry_price, amount)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _empty_registry() -> Dict[str, Any]:
    return {"version": REGISTRY_VERSION, "updated_at": _now_iso(), "positions": {}}


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
    except (FileNotFoundError, OSError, ValueError, TypeError, json.JSONDecodeError):
        return _empty_registry()


def save_registry(registry: Dict[str, Any], path: str = REGISTRY_PATH) -> None:
    data = dict(registry)
    data["version"] = REGISTRY_VERSION
    data["updated_at"] = _now_iso()
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".registry_", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True, default=str)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _group_live_positions(live: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Build unique identities without overwriting duplicate no-ID positions."""
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for pos in live:
        identity = _make_identity(
            pos["symbol"], pos["side"], pos.get("exchange_position_id"),
            pos.get("avg_entry_price"), pos.get("amount"),
        )
        grouped.setdefault(identity, []).append(pos)

    result: Dict[str, Dict[str, Any]] = {}
    for base, positions in grouped.items():
        if len(positions) == 1:
            result[base] = positions[0]
            continue
        for ordinal, pos in enumerate(positions):
            result[f"{base}:{ordinal}"] = pos
    return result


def _extract_exchange_opened_at(pos: Dict[str, Any]) -> Optional[str]:
    """Extract an explicit exchange open/creation timestamp when supplied."""
    for key in ("openTime", "openTimestamp", "positionOpenTime", "position_opened_at", "openedAt", "open_time", "entryTime", "entry_time", "createTime", "createdAt", "createTimestamp"):
        value = pos.get(key)
        if value in (None, ""):
            continue
        try:
            number = float(value)
            if number < 10_000_000_000:
                number *= 1000
            return datetime.fromtimestamp(number / 1000, tz=timezone.utc).isoformat()
        except (TypeError, ValueError, OSError, OverflowError):
            try:
                text = str(value).strip()
                if text.endswith("Z"):
                    text = text[:-1] + "+00:00"
                parsed = datetime.fromisoformat(text)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.astimezone(timezone.utc).isoformat()
            except ValueError:
                continue
    return None


def _new_entry_from_exchange(
    pos: Dict[str, Any], orphan: bool = True, *, identity: Optional[str] = None,
    identity_ambiguous: bool = False, identity_base: Optional[str] = None,
) -> Dict[str, Any]:
    now = _now_iso()
    return {
        "symbol": pos["symbol"],
        "side": pos["side"],
        "amount": _numeric_or_original(pos.get("amount")),
        "exchange_position_id": pos.get("exchange_position_id"),
        "avg_entry_price": _numeric_or_original(pos.get("avg_entry_price")),
        "mark_price": _numeric_or_original(pos.get("mark_price")),
        "unrealized_pnl": _numeric_or_original(pos.get("unrealized_pnl")),
        "lifecycle": bingx.OPEN,
        "orphan": bool(orphan),
        "smc_linked": not orphan,
        "identity_kind": "exchange_id" if pos.get("exchange_position_id") else "fallback",
        "identity_ambiguous": bool(identity_ambiguous),
        "identity_base": identity_base or identity,
        "first_seen_at": now,
        "position_opened_at": _extract_exchange_opened_at(pos) or now,
        "last_seen_at": now,
        "closed_at": None,
        "open_alert_sent": False,
        "close_alert_sent": False,
        "source": "exchange_discovery",
        "smc_context": None,
    }


def _migrate_single_fallback_to_ordinal(
    positions_map: Dict[str, Any], base: str, live_count: int
) -> None:
    if live_count <= 1 or base not in positions_map:
        return
    old = positions_map.pop(base)
    old["identity_base"] = base
    old["identity_ambiguous"] = True
    old["identity_kind"] = "fallback"
    positions_map[f"{base}:0"] = old


def _close_from_history(entry: Dict[str, Any], history_item: Dict[str, Any]) -> None:
    entry["lifecycle"] = bingx.CLOSED
    entry["closed_at"] = _now_iso()
    entry["close_reason"] = "CONFIRMED_HISTORY"
    entry["exchange_close_source"] = "BingX positionHistory"
    entry["position_close_reported"] = False
    for src, dst in (
        ("closePrice", "exchange_close_price"),
        ("price", "exchange_close_price"),
        ("avgClosePrice", "exchange_close_price"),
        ("realisedProfit", "exchange_realized_pnl"),
        ("realizedProfit", "exchange_realized_pnl"),
        ("realizedPnl", "exchange_realized_pnl"),
    ):
        if history_item.get(src) not in (None, ""):
            entry[dst] = _numeric_or_original(history_item[src])
    for key in ("closeTime", "closedAt", "time", "timestamp"):
        if history_item.get(key) not in (None, ""):
            entry["exchange_closed_at"] = history_item[key]
            break
    for key in ("orderId", "orderID", "order_id"):
        if history_item.get(key) not in (None, ""):
            entry["exchange_close_order_id"] = str(history_item[key])
            break
    pid = (
        history_item.get("positionId")
        or history_item.get("positionID")
        or history_item.get("position_id")
    )
    if pid:
        entry["exchange_position_id"] = str(pid)


def reconcile(
    registry: Optional[Dict[str, Any]] = None,
    path: str = REGISTRY_PATH,
    api_key: Optional[str] = None,
    secret: Optional[str] = None,
) -> Dict[str, Any]:
    if registry is None:
        registry = load_registry(path)
    positions_map: Dict[str, Any] = registry.setdefault("positions", {})
    result = {
        "ok": True, "error": None, "discovered": [], "updated": [],
        "not_found": [], "closed": [], "registry": registry,
    }

    try:
        live = bingx.list_open_positions(api_key=api_key, secret=secret)
    except Exception as exc:
        result["ok"] = False
        result["error"] = f"API_ERROR: {type(exc).__name__}: {exc}"
        # Preserve OPEN. API failure is not evidence that a position vanished.
        for entry in positions_map.values():
            if entry.get("lifecycle") == bingx.OPEN:
                entry["last_error"] = result["error"]
                entry["api_error_at"] = _now_iso()
        save_registry(registry, path)
        return result

    # Migrate a pre-hardening single fallback identity before matching duplicates.
    base_counts: Dict[str, int] = {}
    for pos in live:
        if not pos.get("exchange_position_id"):
            base = _fallback_base_identity(
                pos["symbol"], pos["side"], pos.get("avg_entry_price"), pos.get("amount")
            )
            base_counts[base] = base_counts.get(base, 0) + 1
    for base, count in base_counts.items():
        _migrate_single_fallback_to_ordinal(positions_map, base, count)

    live_by_id = _group_live_positions(live)
    seen = set()
    for identity, pos in live_by_id.items():
        seen.add(identity)
        base = identity.rsplit(":", 1)[0] if identity.count(":") and identity.rsplit(":", 1)[1].isdigit() else identity
        ambiguous = identity != base and not pos.get("exchange_position_id")
        if identity in positions_map:
            entry = positions_map[identity]
            entry["lifecycle"] = bingx.OPEN
            entry["amount"] = _numeric_or_original(pos.get("amount"))
            entry["mark_price"] = _numeric_or_original(pos.get("mark_price"))
            entry["unrealized_pnl"] = _numeric_or_original(pos.get("unrealized_pnl"))
            avg = _numeric_or_original(pos.get("avg_entry_price"))
            if avg is not None:
                entry["avg_entry_price"] = avg
            entry["last_seen_at"] = _now_iso()
            if not entry.get("position_opened_at"):
                entry["position_opened_at"] = _extract_exchange_opened_at(pos) or _now_iso()
            entry.pop("last_error", None)
            entry.pop("api_error_at", None)
            entry["identity_ambiguous"] = bool(entry.get("identity_ambiguous", False) or ambiguous)
            entry["identity_base"] = entry.get("identity_base") or base
            result["updated"].append(identity)
        else:
            entry = _new_entry_from_exchange(
                pos, orphan=True, identity=identity,
                identity_ambiguous=ambiguous, identity_base=base,
            )
            positions_map[identity] = entry
            result["discovered"].append(identity)

    # A successful empty/missing response is NOT closure by itself. Ask the
    # authoritative position-history endpoint before transitioning CLOSED.
    for identity, entry in list(positions_map.items()):
        if identity in seen or entry.get("lifecycle") == bingx.CLOSED:
            continue
        if entry.get("lifecycle") not in {bingx.OPEN, "API_ERROR", "NOT_FOUND"}:
            continue
        entry["lifecycle"] = "NOT_FOUND"
        entry["last_seen_at"] = _now_iso()
        result["not_found"].append(identity)
        if entry.get("identity_ambiguous"):
            continue
        try:
            history = bingx.confirm_closed_position(entry, api_key=api_key, secret=secret)
        except Exception as exc:
            entry["close_confirmation_error"] = f"{type(exc).__name__}: {exc}"
            continue
        if isinstance(history, dict):
            _close_from_history(entry, history)
            result["closed"].append(identity)

    save_registry(registry, path)
    result["registry"] = registry
    return result


def mark_open_alert_sent(identity: str, path: str = REGISTRY_PATH) -> None:
    registry = load_registry(path)
    entry = registry.get("positions", {}).get(identity)
    if entry is not None:
        entry["open_alert_sent"] = True
        entry["open_alert_sent_at"] = _now_iso()
        save_registry(registry, path)


def mark_close_alert_sent(identity: str, path: str = REGISTRY_PATH) -> None:
    registry = load_registry(path)
    entry = registry.get("positions", {}).get(identity)
    if entry is not None:
        entry["close_alert_sent"] = True
        entry["close_alert_sent_at"] = _now_iso()
        save_registry(registry, path)


def mark_closed(
    identity: str,
    reason: str = "CONFIRMED_HISTORY",
    path: str = REGISTRY_PATH,
    history_item: Optional[Dict[str, Any]] = None,
) -> bool:
    """Only permit CLOSED when authoritative history was supplied."""
    if reason != "CONFIRMED_HISTORY" or not isinstance(history_item, dict):
        return False
    registry = load_registry(path)
    entry = registry.get("positions", {}).get(identity)
    if entry is None:
        return False
    _close_from_history(entry, history_item)
    save_registry(registry, path)
    return True


def migrate_from_watchlist(
    watchlist_path: str = None, registry_path: str = REGISTRY_PATH,
) -> Dict[str, Any]:
    if watchlist_path is None:
        watchlist_path = os.path.join(HERE, "smc_watchlist.json")
    registry = load_registry(registry_path)
    positions_map = registry.setdefault("positions", {})
    migrated = skipped = 0
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
        side = bingx.normalize_side(it.get("exchange_position_side") or it.get("direction") or "")
        if side not in {"LONG", "SHORT"}:
            skipped += 1
            continue
        exchange_id = it.get("exchange_position_id") or it.get("position_id") or it.get("bingx_position_id")
        avg = it.get("exchange_avg_price") or it.get("entry_price")
        amount = it.get("exchange_position_amount") or it.get("amount")
        identity = _make_identity(symbol, side, exchange_id, avg, amount)
        if identity in positions_map:
            skipped += 1
            continue
        positions_map[identity] = {
            "symbol": symbol, "side": side, "amount": _numeric_or_original(amount),
            "exchange_position_id": exchange_id,
            "avg_entry_price": _numeric_or_original(avg),
            "mark_price": _numeric_or_original(it.get("exchange_mark_price") or it.get("current_price")),
            "unrealized_pnl": _numeric_or_original(it.get("exchange_unrealized_pnl")),
            "lifecycle": bingx.OPEN, "orphan": False, "smc_linked": True,
            "identity_kind": "exchange_id" if exchange_id else "fallback",
            "identity_ambiguous": False, "identity_base": identity,
            "first_seen_at": it.get("triggered_at") or it.get("added_at") or _now_iso(),
            "position_opened_at": it.get("position_opened_at"),
            "last_seen_at": _now_iso(), "closed_at": None,
            "open_alert_sent": False,
            "close_alert_sent": bool(it.get("position_close_reported")),
            "source": "migrated_from_watchlist",
            "smc_context": {
                "trade_type": it.get("trade_type"),
                "added_score": it.get("added_score"),
                "status": it.get("status"),
            },
        }
        migrated += 1
    save_registry(registry, registry_path)
    return {"migrated": migrated, "skipped": skipped, "registry": registry}


def _load_watchlist_items(watchlist_path: Optional[str] = None) -> List[Dict[str, Any]]:
    path = watchlist_path or os.path.join(HERE, "smc_watchlist.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            items = json.load(fh)
        return items if isinstance(items, list) else []
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return []


def classify_smc_links(
    registry: Optional[Dict[str, Any]] = None, watchlist_path: Optional[str] = None,
) -> Dict[str, Any]:
    if registry is None:
        registry = load_registry()
    items = _load_watchlist_items(watchlist_path)
    by_pos_id: Dict[str, Dict[str, Any]] = {}
    by_sym_side: Dict[str, Dict[str, Any]] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        eid = it.get("exchange_position_id") or it.get("position_id") or it.get("bingx_position_id")
        if eid and str(eid).strip():
            by_pos_id[str(eid).strip()] = it
        sym = _canonical_symbol(it.get("symbol"))
        side = bingx.normalize_side(it.get("exchange_position_side") or it.get("direction") or "")
        if sym and side in {"LONG", "SHORT"}:
            by_sym_side[f"{sym}:{side}"] = it
    linked = orphans = 0
    for entry in registry.get("positions", {}).values():
        if entry.get("lifecycle") != bingx.OPEN:
            continue
        eid = entry.get("exchange_position_id")
        match = by_pos_id.get(str(eid).strip()) if eid else None
        if match is None:
            match = by_sym_side.get(f"{entry.get('symbol')}:{entry.get('side')}")
        if match is not None:
            entry["orphan"] = False
            entry["smc_linked"] = True
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
    registry: Optional[Dict[str, Any]] = None, orphans_only: bool = True,
) -> List[Tuple[str, Dict[str, Any]]]:
    if registry is None:
        registry = load_registry()
    out = []
    for identity, entry in registry.get("positions", {}).items():
        if entry.get("lifecycle") != bingx.OPEN or entry.get("open_alert_sent"):
            continue
        if orphans_only and not entry.get("orphan", True):
            continue
        out.append((identity, entry))
    return out


def iter_closed_needing_alert(registry: Optional[Dict[str, Any]] = None) -> List[Tuple[str, Dict[str, Any]]]:
    """Return authoritative CLOSED positions whose closure alert is unsent."""
    if registry is None:
        registry = load_registry()
    return [
        (identity, entry)
        for identity, entry in registry.get("positions", {}).items()
        if entry.get("lifecycle") == bingx.CLOSED and not entry.get("close_alert_sent")
    ]


def build_minimal_context_for_health(entry: Dict[str, Any]) -> Dict[str, Any]:
    ctx = {
        "symbol": entry.get("symbol"),
        "direction": entry.get("side"),
        "status": "triggered",
        "exchange_sync_status": entry.get("lifecycle"),
        "exchange_position_id": entry.get("exchange_position_id"),
        "exchange_avg_price": entry.get("avg_entry_price"),
        "exchange_mark_price": entry.get("mark_price"),
        "exchange_unrealized_pnl": entry.get("unrealized_pnl"),
        "exchange_position_amount": entry.get("amount"),
        "position_lifecycle": entry.get("lifecycle"),
        "triggered_at": entry.get("position_opened_at") or entry.get("first_seen_at"),
        "position_opened_at": entry.get("position_opened_at") or entry.get("first_seen_at"),
        "orphan": entry.get("orphan", True),
    }
    smc = entry.get("smc_context")
    if isinstance(smc, dict):
        for key in ("trade_type", "added_score", "status", "invalidation", "validated_targets"):
            if key in smc:
                ctx[key] = smc[key]
    return ctx
