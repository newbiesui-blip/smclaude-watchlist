"""Entry-only market-environment gate for ATHENA.

This module is independent of SMC setup generation and Position Registry.
It answers only: may ATHENA create/announce a NEW entry right now?

Existing BingX OPEN positions must continue through discovery, health,
intelligence, and BUG-2 monitoring regardless of this state.

Unattended operation is fail-closed: missing or malformed environment data
blocks NEW entries. Existing-position monitoring is not affected.

Inputs:
  MARKET_ENV_EVENTS_JSON
    [{"name":"FOMC","impact":"HIGH","starts_at":"...","ends_at":"..."}]
  MARKET_ENV_LIQUIDITY_JSON
    {"spread_bps":...,"volume_ratio":...,"atr_ratio":...,
     "participation_ratio":...}

Optional:
  MARKET_ENV_MAX_SPREAD_BPS=20
  MARKET_ENV_MIN_VOLUME_RATIO=0.35
  MARKET_ENV_MIN_ATR_RATIO=0.35
  MARKET_ENV_MIN_PARTICIPATION_RATIO=0.35
  MARKET_ENV_BLOCK_WEEKENDS=1
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional

NORMAL = "NORMAL"
CAUTION = "CAUTION"
BLOCK_NEW_ENTRIES = "BLOCK_NEW_ENTRIES"

_HIGH_IMPACT_NAMES = {
    "NFP", "NONFARM PAYROLLS", "NON-FARM PAYROLLS", "CPI", "FOMC",
    "FEDERAL RESERVE", "ECB", "BOE", "BOJ", "PPI", "ISM",
    "INTEREST RATE DECISION",
}


@dataclass(frozen=True)
class EnvironmentState:
    allowed: bool
    state: str
    reason: str
    event: Optional[str] = None
    starts_at: Optional[str] = None
    ends_at: Optional[str] = None
    source: str = "market_environment"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _utc_now(now: Optional[datetime]) -> datetime:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_time(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _number(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _event_name(event: Mapping[str, Any]) -> str:
    return str(event.get("name") or event.get("event") or "HIGH_IMPACT_EVENT").strip()


def _is_high_impact(event: Mapping[str, Any]) -> bool:
    impact = str(event.get("impact", "")).upper().strip()
    if impact in {"HIGH", "3", "RED", "CRITICAL"}:
        return True
    name = _event_name(event).upper()
    return any(token in name for token in _HIGH_IMPACT_NAMES)


def _active_event(events: Iterable[Mapping[str, Any]], now: datetime):
    for event in events:
        if not isinstance(event, Mapping) or not _is_high_impact(event):
            continue
        start = _parse_time(event.get("starts_at") or event.get("start"))
        end = _parse_time(event.get("ends_at") or event.get("end"))
        if start is None or end is None or end <= start:
            continue
        if start <= now <= end:
            return event
    return None


def evaluate(
    *,
    now: Optional[datetime] = None,
    events: Optional[Iterable[Mapping[str, Any]]] = None,
    liquidity: Optional[Mapping[str, Any]] = None,
    block_weekends: bool = False,
) -> EnvironmentState:
    """Evaluate whether a NEW entry is permitted.

    None means the source is unknown, not that conditions are safe.
    """
    current = _utc_now(now)

    if events is None:
        return EnvironmentState(
            False, BLOCK_NEW_ENTRIES, "HIGH_IMPACT_EVENT_DATA_UNAVAILABLE"
        )

    active = _active_event(events, current)
    if active is not None:
        return EnvironmentState(
            False,
            BLOCK_NEW_ENTRIES,
            "HIGH_IMPACT_EVENT",
            event=_event_name(active),
            starts_at=str(active.get("starts_at") or active.get("start")),
            ends_at=str(active.get("ends_at") or active.get("end")),
        )

    if liquidity is None:
        return EnvironmentState(
            False, BLOCK_NEW_ENTRIES, "LIQUIDITY_DATA_UNAVAILABLE"
        )

    thresholds = (
        _number(os.getenv("MARKET_ENV_MAX_SPREAD_BPS", "20")),
        _number(os.getenv("MARKET_ENV_MIN_VOLUME_RATIO", "0.35")),
        _number(os.getenv("MARKET_ENV_MIN_ATR_RATIO", "0.35")),
        _number(os.getenv("MARKET_ENV_MIN_PARTICIPATION_RATIO", "0.35")),
    )
    if any(value is None for value in thresholds):
        return EnvironmentState(
            False, BLOCK_NEW_ENTRIES, "LIQUIDITY_THRESHOLD_CONFIG_INVALID"
        )

    max_spread, min_volume, min_atr, min_participation = thresholds
    spread = _number(liquidity.get("spread_bps"))
    volume = _number(liquidity.get("volume_ratio"))
    atr = _number(liquidity.get("atr_ratio"))
    participation = _number(liquidity.get("participation_ratio"))

    if any(value is None for value in (spread, volume, atr, participation)):
        return EnvironmentState(
            False, BLOCK_NEW_ENTRIES, "LIQUIDITY_DATA_INCOMPLETE"
        )

    if spread > max_spread:
        return EnvironmentState(False, BLOCK_NEW_ENTRIES, "LOW_LIQUIDITY_SPREAD")
    if volume < min_volume:
        return EnvironmentState(False, BLOCK_NEW_ENTRIES, "LOW_LIQUIDITY_VOLUME")
    if atr < min_atr:
        return EnvironmentState(False, BLOCK_NEW_ENTRIES, "LOW_LIQUIDITY_ATR")
    if participation < min_participation:
        return EnvironmentState(
            False, BLOCK_NEW_ENTRIES, "LOW_LIQUIDITY_PARTICIPATION"
        )

    if block_weekends and current.weekday() >= 5:
        return EnvironmentState(False, BLOCK_NEW_ENTRIES, "CALENDAR_WEEKEND")

    return EnvironmentState(True, NORMAL, "ENVIRONMENT_CLEAR")


def _load_json_env(name: str) -> Any:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def load_state(*, now: Optional[datetime] = None) -> EnvironmentState:
    events = _load_json_env("MARKET_ENV_EVENTS_JSON")
    liquidity = _load_json_env("MARKET_ENV_LIQUIDITY_JSON")

    if events is not None and not isinstance(events, list):
        events = None
    if liquidity is not None and not isinstance(liquidity, dict):
        liquidity = None

    return evaluate(
        now=now,
        events=events,
        liquidity=liquidity,
        block_weekends=os.getenv("MARKET_ENV_BLOCK_WEEKENDS", "0") == "1",
    )


def can_open_new_position(*, now: Optional[datetime] = None) -> EnvironmentState:
    """Public entry gate; never controls existing-position monitoring."""
    return load_state(now=now)
