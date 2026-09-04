#!/usr/bin/env python3
"""
ATHENA / SMC Claude -- Derivatives Intelligence Layer (Phase 1)
====================================================================
Standalone module. Independently fetches and analyzes Bybit linear
perpetual derivatives data (open interest, funding rate, volume,
long/short account ratio) and turns it into a contextual state +
score that is SEPARATE from the SMC score.

This module does NOT modify, import from, or depend on smc_scanner.py
or full_scan.py. It can be run and tested completely on its own:

    python derivatives_monitor.py

Architecture
------------
    fetch (adapter)  ->  normalize  ->  rolling history (ring buffer)
        ->  deltas (OI / price / funding, multi-window)
        ->  funding persistence tracking
        ->  state classification (crowding / trap / liquidation-like)
        ->  bounded contextual score (-100..+100)
        ->  persistence (derivatives_history.json / derivatives_state.json)

Exchange-specific fetching lives entirely in the `BybitAdapter` class.
Downstream code (history, deltas, classification, scoring) only ever
touches the normalized snapshot dict, so a future BingX/Binance
adapter can be dropped in without touching anything else.

Nothing here is wired into Telegram, BingX, CoinGecko/CMC, or GitHub
Actions -- that is explicitly out of scope for Phase 1.
"""

import json
import os
import time
import math
from datetime import datetime, timezone, timedelta

import requests

# ============================================================================
# CONFIG -- every tunable threshold lives here, nowhere else in the file.
# ============================================================================

BASE_URL = "https://api.bybit.com"
REQUEST_TIMEOUT = 10                 # seconds, per HTTP request
RETRY_ON_429 = 1                     # single short retry on rate-limit, then skip
RETRY_BACKOFF_SECONDS = 1.5

MAX_HISTORY_SNAPSHOTS = 24           # ring buffer depth per symbol (~6h at 15m cadence)
DEFAULT_CADENCE_MINUTES = 15         # assumed snapshot cadence, used to pick delta windows

# Windows we attempt to compute deltas over, expressed in snapshots-back
# (approximate, given a ~15 min cadence). If history is shorter than a
# window needs, that delta is None rather than fabricated.
DELTA_WINDOWS = {
    "15m": 1,
    "1h": 4,
    "4h": 16,
}

# --- materiality thresholds (price / OI) ---
PRICE_MOVE_MATERIAL_PCT = 1.0        # price change considered "material" for state logic
OI_MOVE_MATERIAL_PCT = 2.0           # OI change considered "material"

# --- funding: magnitude / persistence, not raw % change (funding % change
# is misleading -- 0.00001 -> 0.00002 is +100% but not meaningfully crowded) ---
FUNDING_MAGNITUDE_THRESHOLD = 0.0003   # |funding_rate| beyond this counts as "elevated"
FUNDING_PERSISTENCE_MIN_SNAPSHOTS = 3   # consecutive same-sign elevated readings to call it "persistent"

# --- liquidation-like inference (OI shock + price shock) ---
LIQUIDATION_OI_DROP_PCT = 5.0         # OI drop over a short window beyond this...
LIQUIDATION_PRICE_MOVE_PCT = 3.0      # ...paired with a price move beyond this
LIQUIDATION_WINDOW = "15m"            # window used for the liquidation-like check

# --- long/short account ratio: supporting evidence only, capped influence ---
LS_RATIO_EXTREME_HIGH = 1.8           # buy/sell ratio above this = extreme long skew
LS_RATIO_EXTREME_LOW = 1.0 / 1.8      # below this = extreme short skew
LS_RATIO_SCORE_CAP = 15               # max absolute score contribution from L/S ratio

# --- scoring weights (initial heuristics, NOT empirically validated) ---
SCORE_WEIGHT_PRICE_OI_FUNDING = 50
SCORE_WEIGHT_FUNDING_PERSISTENCE = 20
SCORE_WEIGHT_OI_VELOCITY = 15
SCORE_WEIGHT_LS_RATIO = LS_RATIO_SCORE_CAP

HERE = os.path.dirname(os.path.abspath(__file__))
HISTORY_FILE = os.path.join(HERE, "derivatives_history.json")
STATE_FILE = os.path.join(HERE, "derivatives_state.json")

HEADERS = {"User-Agent": "Mozilla/5.0 (ATHENA-Derivatives-Monitor)"}

STATE_PRIORITY = [
    "LIQUIDATION_EVENT",
    "REVERSAL_RISK",
    "LONG_TRAP_RISK",
    "SHORT_TRAP_RISK",
    "CROWDED_LONG",
    "CROWDED_SHORT",
    "SUPPORTIVE",
    "NEUTRAL",
]


# ============================================================================
# SYMBOL NORMALIZATION
# ============================================================================

def normalize_symbol(raw):
    """Canonical symbol form used as the internal key everywhere.

    BTCUSDT / BTC-USDT / BTC_USDT / BTC:USDT -> BTCUSDT
    """
    if not raw:
        return raw
    s = str(raw).upper().strip()
    for ch in ("-", "_", ":", " ", "/"):
        s = s.replace(ch, "")
    if not s.endswith("USDT") and not s.endswith("USD") and not s.endswith("PERP"):
        s = s + "USDT"
    return s


# ============================================================================
# EXCHANGE ADAPTER -- Bybit (first adapter; others can be added later
# without touching anything downstream of the normalized snapshot dict).
# ============================================================================

def _get(path, params, timeout=REQUEST_TIMEOUT):
    """Low-level GET with timeout, one 429 retry, and safe failure.

    Returns the parsed 'result' dict on success, or None on any failure.
    Never raises.
    """
    url = BASE_URL + path
    attempts = 0
    while True:
        try:
            resp = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
        except Exception:
            return None

        if resp.status_code == 429 and attempts < RETRY_ON_429:
            attempts += 1
            time.sleep(RETRY_BACKOFF_SECONDS)
            continue

        if resp.status_code != 200:
            return None

        try:
            data = resp.json()
        except Exception:
            return None

        if not isinstance(data, dict) or data.get("retCode") != 0:
            return None

        return data.get("result")


class BybitAdapter:
    """Bybit V5 public (unauthenticated) linear-perpetual data source.

    All methods return normalized-ish primitives or None; they never raise.
    Field names verified against Bybit's V5 docs as of this build:
      - GET /v5/market/tickers?category=linear
          -> list[{symbol, lastPrice, price24hPcnt, volume24h, turnover24h,
                    openInterest, openInterestValue, fundingRate, ...}]
      - GET /v5/market/open-interest?category=linear&symbol=..&intervalTime=..
          -> list[{openInterest, timestamp}]
      - GET /v5/market/account-ratio?category=linear&symbol=..&period=..
          -> list[{symbol, buyRatio, sellRatio, timestamp}]
    """

    name = "bybit"

    def fetch_bulk_tickers(self):
        """One request covering the whole linear-perpetual universe.

        Returns: list of raw ticker dicts, or [] on failure.
        """
        result = _get("/v5/market/tickers", {"category": "linear"})
        if not result:
            return []
        return result.get("list", []) or []

    def fetch_open_interest(self, symbol, interval_time="15min", limit=2):
        """Deep per-symbol OI history. Only call for a *selected* subset
        of symbols per cycle -- never for the full universe."""
        result = _get("/v5/market/open-interest", {
            "category": "linear",
            "symbol": symbol,
            "intervalTime": interval_time,
            "limit": limit,
        })
        if not result:
            return None
        return result.get("list", []) or []

    def fetch_account_ratio(self, symbol, period="15min", limit=1):
        """Deep per-symbol long/short account ratio. Selected symbols only."""
        result = _get("/v5/market/account-ratio", {
            "category": "linear",
            "symbol": symbol,
            "period": period,
            "limit": limit,
        })
        if not result:
            return None
        return result.get("list", []) or []

    def fetch_snapshot(self, symbol, ticker_row=None, include_ls_ratio=True):
        """Build one normalized snapshot for `symbol`.

        If `ticker_row` (a raw row from fetch_bulk_tickers) is supplied it
        is reused instead of making a redundant request -- this is the
        normal path when scanning many symbols from one bulk call.
        """
        symbol = normalize_symbol(symbol)

        row = ticker_row
        if row is None:
            # Fallback: fetch just this symbol from the tickers endpoint.
            result = _get("/v5/market/tickers", {"category": "linear", "symbol": symbol})
            rows = (result or {}).get("list", []) or []
            row = rows[0] if rows else None

        if not row:
            return None

        def _f(val):
            """Safe float parse -- Bybit returns numeric fields as strings,
            and sometimes empty strings for fields that don't apply."""
            try:
                if val is None or val == "":
                    return None
                return float(val)
            except (TypeError, ValueError):
                return None

        price = _f(row.get("lastPrice"))
        oi = _f(row.get("openInterest"))
        funding_rate = _f(row.get("fundingRate"))
        volume_24h = _f(row.get("turnover24h"))
        if volume_24h is None:
            volume_24h = _f(row.get("volume24h"))

        long_short_ratio = None
        if include_ls_ratio:
            try:
                ls_rows = self.fetch_account_ratio(symbol)
                if ls_rows:
                    buy_ratio = _f(ls_rows[0].get("buyRatio"))
                    sell_ratio = _f(ls_rows[0].get("sellRatio"))
                    if buy_ratio is not None and sell_ratio not in (None, 0):
                        long_short_ratio = buy_ratio / sell_ratio
            except Exception:
                long_short_ratio = None

        snapshot = {
            "symbol": symbol,
            "ts": datetime.now(timezone.utc).isoformat(),
            "price": price,
            "oi": oi,
            "oi_usd": False,   # Bybit's `openInterest` for linear USDT perps is in base-coin units
            "funding_rate": funding_rate,
            "volume_24h": volume_24h,
            "long_short_ratio": long_short_ratio,
        }
        return snapshot


# ============================================================================
# ROLLING HISTORY (ring buffer, per symbol) -- owned exclusively by this
# module. Never touches smc_watchlist.json / smc_setup_states.json.
# ============================================================================

def _atomic_write_json(path, data):
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, path)
        return True
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        return False


def load_history():
    if not os.path.exists(HISTORY_FILE):
        return {}
    try:
        with open(HISTORY_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def save_history(history):
    return _atomic_write_json(HISTORY_FILE, history)


def append_snapshot(history, snapshot):
    """Append `snapshot` to its symbol's ring buffer in-place, discarding
    the oldest entries beyond MAX_HISTORY_SNAPSHOTS. Returns `history`."""
    if not snapshot or not snapshot.get("symbol"):
        return history
    sym = snapshot["symbol"]
    bucket = history.setdefault(sym, {"snapshots": []})
    bucket["snapshots"].append(snapshot)
    if len(bucket["snapshots"]) > MAX_HISTORY_SNAPSHOTS:
        bucket["snapshots"] = bucket["snapshots"][-MAX_HISTORY_SNAPSHOTS:]
    return history


def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    return _atomic_write_json(STATE_FILE, state)


# ============================================================================
# DELTA CALCULATIONS
# ============================================================================

# Timestamp validation keeps snapshot-count windows honest when a scheduled
# run is delayed or missed.  We allow modest scheduling/network jitter, but
# never silently treat a large gap as a 15m/1h/4h observation.
TIMESTAMP_TOLERANCE_MINUTES = 10.0

def _parse_timestamp(ts):
    if not ts:
        return None
    try:
        value = str(ts)
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None

def _timestamp_gap_minutes(older, newer):
    a = _parse_timestamp(older.get("ts") if isinstance(older, dict) else older)
    b = _parse_timestamp(newer.get("ts") if isinstance(newer, dict) else newer)
    if a is None or b is None:
        return None
    return (b - a).total_seconds() / 60.0

def _window_gap_is_valid(older, newer, expected_minutes):
    gap = _timestamp_gap_minutes(older, newer)
    if gap is None or gap <= 0:
        return False
    # Fixed tolerance is deliberately modest.  This rejects missed-run gaps
    # while allowing normal scheduler/network jitter.
    return abs(gap - expected_minutes) <= TIMESTAMP_TOLERANCE_MINUTES

def _snapshots_are_consecutive(snapshots, cadence_minutes=DEFAULT_CADENCE_MINUTES):
    if len(snapshots) < 2:
        return True
    for older, newer in zip(snapshots[:-1], snapshots[1:]):
        if not _window_gap_is_valid(older, newer, cadence_minutes):
            return False
    return True

def _pct_change(old, new):
    if old is None or new is None or old == 0:
        return None
    return ((new - old) / abs(old)) * 100.0


def compute_deltas(snapshots):
    """Compute price/OI deltas using timestamp-validated windows.

    Snapshot counts are only a candidate for a timeframe; the actual elapsed
    time must also be close to the requested window.  Invalid/missed-run gaps
    produce None rather than fabricated timeframe measurements.
    """
    deltas = {
        "price_pct": {"15m": None, "1h": None, "4h": None},
        "oi_pct": {"15m": None, "1h": None, "4h": None},
        "funding_abs_delta": {"1h": None, "4h": None},
    }
    if not snapshots:
        return deltas

    latest = snapshots[-1]
    n = len(snapshots)

    expected_minutes = {"15m": 15.0, "1h": 60.0, "4h": 240.0}
    for label, back in DELTA_WINDOWS.items():
        if n <= back:
            continue
        ref = snapshots[-1 - back]
        if not _window_gap_is_valid(ref, latest, expected_minutes[label]):
            continue
        deltas["price_pct"][label] = _pct_change(ref.get("price"), latest.get("price"))
        deltas["oi_pct"][label] = _pct_change(ref.get("oi"), latest.get("oi"))

    for label in ("1h", "4h"):
        back = DELTA_WINDOWS[label]
        if n <= back:
            continue
        ref = snapshots[-1 - back]
        if not _window_gap_is_valid(ref, latest, expected_minutes[label]):
            continue
        rf, rf_new = ref.get("funding_rate"), latest.get("funding_rate")
        if rf is not None and rf_new is not None:
            deltas["funding_abs_delta"][label] = rf_new - rf
    return deltas


def compute_funding_persistence(snapshots):
    """Count consecutive recent elevated, same-sign funding readings.

    A reading only counts as consecutive when adjacent observations are
    approximately one cadence apart.  A material/missed-run gap breaks the
    persistence chain.
    """
    result = {"direction": None, "consecutive": 0, "persistent": False}
    if not snapshots:
        return result

    direction = None
    consecutive = 0
    reversed_snaps = list(reversed(snapshots))
    for idx, snap in enumerate(reversed_snaps):
        rate = snap.get("funding_rate")
        if rate is None or abs(rate) < FUNDING_MAGNITUDE_THRESHOLD:
            break
        if idx > 0:
            newer = reversed_snaps[idx - 1]
            if not _window_gap_is_valid(snap, newer, float(DEFAULT_CADENCE_MINUTES)):
                break
        sign = "positive" if rate > 0 else "negative"
        if direction is None:
            direction = sign
        if sign != direction:
            break
        consecutive += 1

    result["direction"] = direction
    result["consecutive"] = consecutive
    result["persistent"] = consecutive >= FUNDING_PERSISTENCE_MIN_SNAPSHOTS
    return result


# ============================================================================
# STATE CLASSIFICATION
# ============================================================================

def _reason(text):
    return text


def classify_state(symbol, snapshots, deltas, persistence):
    """Core interpretation logic per section 11-17 of the spec.

    Returns: {"state": str, "reason": str, "severity_hint": float}
    severity_hint (0..1) is only used to scale the long/short-ratio bump
    and is not itself the score.
    """
    latest = snapshots[-1] if snapshots else {}
    ls_ratio = latest.get("long_short_ratio")

    price_1h = deltas["price_pct"]["1h"]
    oi_1h = deltas["oi_pct"]["1h"]
    price_15m = deltas["price_pct"]["15m"]
    oi_15m = deltas["oi_pct"]["15m"]

    funding_dir = persistence["direction"]
    funding_persistent = persistence["persistent"]

    # --- 1) Liquidation-like inference: short-window OI shock + price shock ---
    if oi_15m is not None and price_15m is not None:
        if oi_15m <= -LIQUIDATION_OI_DROP_PCT and abs(price_15m) >= LIQUIDATION_PRICE_MOVE_PCT:
            reason = (
                f"Inferred liquidation event: OI dropped {oi_15m:.1f}% while price moved "
                f"{price_15m:+.1f}% in 15m; not confirmed liquidation-feed data"
            )
            return {
                "state": "LIQUIDATION_EVENT",
                "reason": reason,
                "severity_hint": 1.0,
                "evidence_type": "inferred",
            }

    # Need at least a 1h window for everything below; otherwise NEUTRAL/insufficient data.
    if price_1h is None or oi_1h is None:
        return {
            "state": "NEUTRAL",
            "reason": "Insufficient history for 1h price/OI deltas yet",
            "severity_hint": 0.0,
        }

    price_up = price_1h >= PRICE_MOVE_MATERIAL_PCT
    price_down = price_1h <= -PRICE_MOVE_MATERIAL_PCT
    oi_up = oi_1h >= OI_MOVE_MATERIAL_PCT
    oi_down = oi_1h <= -OI_MOVE_MATERIAL_PCT

    # --- 2) LONG TRAP: price down, OI up, funding positive & persistent ---
    if price_down and oi_up and funding_dir == "positive" and funding_persistent:
        reason = (
            f"Price {price_1h:+.1f}% over 1h while OI {oi_1h:+.1f}% and funding remains "
            f"positive/persistent ({persistence['consecutive']} consecutive elevated readings)"
        )
        return {"state": "LONG_TRAP_RISK", "reason": reason, "severity_hint": 0.9}

    # --- 3) SHORT TRAP: price up, OI up, funding negative & persistent ---
    if price_up and oi_up and funding_dir == "negative" and funding_persistent:
        reason = (
            f"Price {price_1h:+.1f}% over 1h while OI {oi_1h:+.1f}% and funding remains "
            f"negative/persistent ({persistence['consecutive']} consecutive elevated readings)"
        )
        return {"state": "SHORT_TRAP_RISK", "reason": reason, "severity_hint": 0.9}

    # --- 4) CROWDED_LONG: funding strongly positive + persistent, OI rising/flat ---
    if funding_dir == "positive" and funding_persistent and not oi_down:
        reason = (
            f"Funding persistently positive ({persistence['consecutive']} consecutive "
            f"elevated readings) with OI {'rising' if oi_up else 'flat'} -- one-sided long positioning building"
        )
        return {"state": "CROWDED_LONG", "reason": reason, "severity_hint": 0.5}

    # --- 5) CROWDED_SHORT: mirror ---
    if funding_dir == "negative" and funding_persistent and not oi_down:
        reason = (
            f"Funding persistently negative ({persistence['consecutive']} consecutive "
            f"elevated readings) with OI {'rising' if oi_up else 'flat'} -- one-sided short positioning building"
        )
        return {"state": "CROWDED_SHORT", "reason": reason, "severity_hint": 0.5}

    # --- 6) SHORT COVERING: price up, OI down -> bullish, less fresh participation ---
    if price_up and oi_down:
        reason = f"Price {price_1h:+.1f}% while OI {oi_1h:+.1f}%, consistent with short covering"
        return {"state": "SUPPORTIVE", "reason": reason, "severity_hint": 0.2}

    # --- 7) Plain LONG participation: price up, OI up, funding up (but not flagged as trap above) ---
    if price_up and oi_up:
        reason = (
            f"Price {price_1h:+.1f}% with OI {oi_1h:+.1f}% -- long participation increasing "
            f"(bullish/supportive, watch crowding risk)"
        )
        return {"state": "SUPPORTIVE", "reason": reason, "severity_hint": 0.3}

    # --- 8) Plain SHORT confirmation: price down, OI up, funding negative (not persistent/trap) ---
    if price_down and oi_up and funding_dir == "negative":
        reason = f"Price {price_1h:+.1f}% with OI {oi_1h:+.1f}% and funding negative -- bearish confirmation, short participation increasing"
        return {"state": "NEUTRAL", "reason": reason, "severity_hint": 0.3}

    # --- 9) Position closing / long-liquidation-like drift: price down, OI down ---
    if price_down and oi_down:
        reason = f"Price {price_1h:+.1f}% with OI {oi_1h:+.1f}% -- position closing / long unwind"
        return {"state": "NEUTRAL", "reason": reason, "severity_hint": 0.2}

    return {
        "state": "NEUTRAL",
        "reason": f"No material price/OI/funding condition met (price {price_1h:+.1f}%, OI {oi_1h:+.1f}%)",
        "severity_hint": 0.0,
    }


def apply_ls_ratio_adjustment(classification, snapshot):
    """L/S ratio is supporting evidence only -- it may intensify the
    reason/severity of an ALREADY-detected trap/crowding state, but per
    spec it must never independently create or flip a trap state, and
    its score contribution is capped (handled in compute_score)."""
    ls_ratio = snapshot.get("long_short_ratio")
    state = classification["state"]
    if ls_ratio is None or state not in ("LONG_TRAP_RISK", "SHORT_TRAP_RISK", "CROWDED_LONG", "CROWDED_SHORT"):
        return classification

    if ls_ratio >= LS_RATIO_EXTREME_HIGH and state in ("LONG_TRAP_RISK", "CROWDED_LONG"):
        classification["reason"] += f"; reinforced by extreme long account skew (L/S ratio {ls_ratio:.2f})"
        classification["severity_hint"] = min(1.0, classification["severity_hint"] + 0.1)
    elif ls_ratio <= LS_RATIO_EXTREME_LOW and state in ("SHORT_TRAP_RISK", "CROWDED_SHORT"):
        classification["reason"] += f"; reinforced by extreme short account skew (L/S ratio {ls_ratio:.2f})"
        classification["severity_hint"] = min(1.0, classification["severity_hint"] + 0.1)

    return classification


# ============================================================================
# SCORE (-100 .. +100), separate from and never combined with SMC score
# ============================================================================

def compute_score(classification, deltas, persistence, snapshot):
    """Modular, weight-isolated scoring so components can be recalibrated
    independently later against real outcomes. Positive = bullish/
    supportive context, negative = bearish/risk context. Magnitude is a
    confidence/severity signal, not a trade instruction.
    """
    state = classification["state"]
    severity = classification.get("severity_hint", 0.0)

    price_oi_funding = 0.0
    if state == "LONG_TRAP_RISK":
        price_oi_funding = -SCORE_WEIGHT_PRICE_OI_FUNDING * severity
    elif state == "SHORT_TRAP_RISK":
        price_oi_funding = SCORE_WEIGHT_PRICE_OI_FUNDING * severity
    elif state == "CROWDED_LONG":
        price_oi_funding = -SCORE_WEIGHT_PRICE_OI_FUNDING * severity * 0.5
    elif state == "CROWDED_SHORT":
        price_oi_funding = SCORE_WEIGHT_PRICE_OI_FUNDING * severity * 0.5
    elif state == "LIQUIDATION_EVENT":
        price_15m = deltas["price_pct"]["15m"] or 0
        price_oi_funding = -SCORE_WEIGHT_PRICE_OI_FUNDING if price_15m < 0 else SCORE_WEIGHT_PRICE_OI_FUNDING
    elif state == "SUPPORTIVE":
        price_oi_funding = SCORE_WEIGHT_PRICE_OI_FUNDING * 0.4

    funding_component = 0.0
    if persistence["persistent"]:
        sign = -1 if persistence["direction"] == "positive" else 1
        weight = min(1.0, persistence["consecutive"] / (FUNDING_PERSISTENCE_MIN_SNAPSHOTS * 2))
        funding_component = sign * SCORE_WEIGHT_FUNDING_PERSISTENCE * weight

    oi_velocity_component = 0.0
    oi_1h = deltas["oi_pct"]["1h"]
    if oi_1h is not None:
        capped = max(-1.0, min(1.0, oi_1h / (OI_MOVE_MATERIAL_PCT * 3)))
        # OI velocity alone is directionless info; lean it against the
        # detected state's sign so it amplifies rather than fights it.
        if state == "NEUTRAL" or price_oi_funding == 0:
            oi_velocity_component = 0.0
        else:
            direction = 1 if price_oi_funding > 0 else -1
            oi_velocity_component = direction * SCORE_WEIGHT_OI_VELOCITY * abs(capped)

    ls_component = 0.0
    ls_ratio = snapshot.get("long_short_ratio")
    if ls_ratio is not None and ls_ratio > 0:
        # Map ratio to a -1..+1 skew, capped, and only ever nudges --
        # cannot independently flip overall sign (enforced below too).
        log_skew = math.log(ls_ratio)
        skew = max(-1.0, min(1.0, log_skew / math.log(LS_RATIO_EXTREME_HIGH)))
        ls_component = -skew * LS_RATIO_SCORE_CAP  # extreme long skew -> bearish nudge

    raw_total = price_oi_funding + funding_component + oi_velocity_component

    # Cap the L/S component so it cannot flip the sign of raw_total.
    if raw_total > 0:
        ls_component = max(-raw_total, ls_component) if ls_component < 0 else ls_component
    elif raw_total < 0:
        ls_component = min(-raw_total, ls_component) if ls_component > 0 else ls_component
    else:
        ls_component = 0.0  # no directional context yet -> ratio alone contributes nothing

    total = raw_total + ls_component
    total = max(-100.0, min(100.0, total))
    return round(total, 1)


# ============================================================================
# TRANSITION TRACKING (state.json) -- for future alerting; Phase 1 just
# records "since" so a repeated state is not treated as a new transition.
# ============================================================================

def update_state_record(state_store, symbol, classification, score):
    now = datetime.now(timezone.utc).isoformat()
    prev = state_store.get(symbol)
    is_new_transition = not prev or prev.get("state") != classification["state"]
    since = now if is_new_transition else prev.get("since", now)

    state_store[symbol] = {
        "state": classification["state"],
        "reason": classification["reason"],
        "since": since,
        "score": score,
        "updated": now,
        "is_new_transition": is_new_transition,
    }
    return state_store[symbol]


# ============================================================================
# PUBLIC ENTRY POINT
# ============================================================================

def monitor(symbols=None, adapter=None, history=None, state_store=None,
            include_ls_ratio_for=None, persist=True):
    """Run one derivatives-monitoring cycle.

    symbols: optional list of symbols to deep-check (watchlist, extreme
        candidates, etc). If None, every symbol returned by the bulk
        ticker call is processed using ONLY the bulk data (no per-symbol
        L/S ratio calls, to stay API-efficient across ~150 markets).
    include_ls_ratio_for: optional explicit list of symbols to fetch the
        deep long/short account-ratio for. Defaults to `symbols` if given
        (bounded), else none (bulk-only mode).
    persist: if True, writes derivatives_history.json / derivatives_state.json.

    Returns: {"results": {symbol: {...}}, "failed_symbols": [...],
              "bulk_fetch_ok": bool}
    """
    adapter = adapter or BybitAdapter()
    history = history if history is not None else load_history()
    state_store = state_store if state_store is not None else load_state()

    out = {"results": {}, "failed_symbols": [], "bulk_fetch_ok": True}

    raw_rows = adapter.fetch_bulk_tickers()
    if not raw_rows:
        out["bulk_fetch_ok"] = False
        return out  # skip this cycle entirely; caller retains previous history/state

    rows_by_symbol = {}
    for row in raw_rows:
        sym = normalize_symbol(row.get("symbol", ""))
        if sym:
            rows_by_symbol[sym] = row

    target_symbols = [normalize_symbol(s) for s in symbols] if symbols else list(rows_by_symbol.keys())
    # L/S account-ratio is deliberately opt-in. Never infer it from
    # `symbols`, because the base scan may contain the full ~150-market
    # universe and that would turn one bulk request into ~150 extra calls.
    ls_targets = set(
        normalize_symbol(s)
        for s in (
            include_ls_ratio_for
            if include_ls_ratio_for is not None
            else []
        )
    )

    for symbol in target_symbols:
        try:
            row = rows_by_symbol.get(symbol)
            if row is None:
                out["failed_symbols"].append(symbol)
                continue

            snapshot = adapter.fetch_snapshot(
                symbol, ticker_row=row, include_ls_ratio=(symbol in ls_targets)
            )
            if snapshot is None:
                out["failed_symbols"].append(symbol)
                continue

            append_snapshot(history, snapshot)
            snaps = history[symbol]["snapshots"]

            deltas = compute_deltas(snaps)
            persistence = compute_funding_persistence(snaps)
            classification = classify_state(symbol, snaps, deltas, persistence)
            classification = apply_ls_ratio_adjustment(classification, snapshot)
            score = compute_score(classification, deltas, persistence, snapshot)
            record = update_state_record(state_store, symbol, classification, score)

            out["results"][symbol] = {
                "snapshot": snapshot,
                "deltas": deltas,
                "funding_persistence": persistence,
                "state": classification["state"],
                "reason": classification["reason"],
                "score": score,
                "since": record["since"],
                "is_new_transition": record["is_new_transition"],
            }
        except Exception as exc:
            # One symbol's failure must never take down the cycle.
            out["failed_symbols"].append(symbol)
            continue

    if persist:
        save_history(history)
        save_state(state_store)

    return out


# ============================================================================
# STANDALONE TEST / DEMO RUNNER
# ============================================================================

def _make_snapshot(symbol, price, oi, funding_rate, ls_ratio=None, minutes_ago=0):
    ts = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    return {
        "symbol": symbol,
        "ts": ts,
        "price": price,
        "oi": oi,
        "oi_usd": False,
        "funding_rate": funding_rate,
        "volume_24h": 1_000_000.0,
        "long_short_ratio": ls_ratio,
    }


def _run_tests():
    results = []

    def check(name, condition, detail=""):
        results.append((name, bool(condition), detail))

    # --- Test 1: API failure handling ---
    class FailingAdapter(BybitAdapter):
        def fetch_bulk_tickers(self):
            raise RuntimeError("simulated network failure")

    try:
        adapter = FailingAdapter()
        try:
            rows = adapter.fetch_bulk_tickers()
            check("Test1 API failure (unexpected non-raise)", False)
        except RuntimeError:
            # Simulate what monitor() does: it calls adapter methods, but the
            # *real* BybitAdapter never raises (all _get calls are wrapped).
            # Here we directly validate that _get() itself never raises on
            # network errors by hitting an unreachable host.
            pass
        result = _get("/v5/market/tickers", {"category": "linear"}, timeout=0.001)
        check("Test1 API failure: _get() fails safely (no exception, returns None-ish)",
              result is None or isinstance(result, dict))
    except Exception as exc:
        check("Test1 API failure", False, f"raised: {exc}")

    # --- Test 2: positive funding alone must NOT be automatically bearish ---
    snaps = [_make_snapshot("AAAUSDT", 100 + i * 0.05, 1_000_000, 0.0001) for i in range(5)]
    # Synthetic observations are 15m apart (oldest -> newest).
    for i, snap in enumerate(snaps):
        snap["ts"] = (datetime.now(timezone.utc) - timedelta(minutes=(len(snaps)-1-i)*15)).isoformat()
    deltas = compute_deltas(snaps)
    persistence = compute_funding_persistence(snaps)
    classification = classify_state("AAAUSDT", snaps, deltas, persistence)
    check(
        "Test2 positive funding alone is not automatically bearish",
        classification["state"] not in ("LONG_TRAP_RISK", "SHORT_TRAP_RISK"),
        f"state={classification['state']}",
    )

    # --- Test 3: LONG TRAP (price down, OI up, funding positive/persistent) ---
    snaps = []
    price = 100.0
    oi = 1_000_000.0
    for i in range(8):
        price -= 0.6          # cumulative material downward drift
        oi += 30_000          # cumulative OI increase
        snaps.append(_make_snapshot("BBBUSDT", price, oi, 0.0006, minutes_ago=(7-i)*15))
    deltas = compute_deltas(snaps)
    persistence = compute_funding_persistence(snaps)
    classification = classify_state("BBBUSDT", snaps, deltas, persistence)
    check(
        "Test3 LONG_TRAP_RISK detected",
        classification["state"] == "LONG_TRAP_RISK",
        f"state={classification['state']} price_1h={deltas['price_pct']['1h']} oi_1h={deltas['oi_pct']['1h']}",
    )

    # --- Test 4: SHORT TRAP (price up, OI up, funding negative/persistent) ---
    snaps = []
    price = 100.0
    oi = 1_000_000.0
    for i in range(8):
        price += 0.6
        oi += 30_000
        snaps.append(_make_snapshot("CCCUSDT", price, oi, -0.0006, minutes_ago=(7-i)*15))
    deltas = compute_deltas(snaps)
    persistence = compute_funding_persistence(snaps)
    classification = classify_state("CCCUSDT", snaps, deltas, persistence)
    check(
        "Test4 SHORT_TRAP_RISK detected",
        classification["state"] == "SHORT_TRAP_RISK",
        f"state={classification['state']} price_1h={deltas['price_pct']['1h']} oi_1h={deltas['oi_pct']['1h']}",
    )

    # --- Test 5: short covering (price up, OI down) ---
    snaps = []
    price = 100.0
    oi = 1_000_000.0
    for i in range(8):
        price += 0.6
        oi -= 30_000
        snaps.append(_make_snapshot("DDDUSDT", price, oi, 0.00005, minutes_ago=(7-i)*15))
    deltas = compute_deltas(snaps)
    persistence = compute_funding_persistence(snaps)
    classification = classify_state("DDDUSDT", snaps, deltas, persistence)
    check(
        "Test5 short covering -> SUPPORTIVE",
        classification["state"] == "SUPPORTIVE" and "short covering" in classification["reason"],
        f"state={classification['state']} reason={classification['reason']}",
    )

    # --- Test 6: liquidation-like event (sharp OI drop + outsized price move, 15m) ---
    snaps = [_make_snapshot("EEEUSDT", 100.0, 1_000_000.0, 0.0001, minutes_ago=45),
             _make_snapshot("EEEUSDT", 100.0, 1_000_000.0, 0.0001, minutes_ago=30),
             _make_snapshot("EEEUSDT", 100.0, 1_000_000.0, 0.0001, minutes_ago=15)]
    snaps.append(_make_snapshot("EEEUSDT", 95.5, 900_000.0, 0.0001, minutes_ago=0))  # -4.5% price, -10% OI in one step
    deltas = compute_deltas(snaps)
    persistence = compute_funding_persistence(snaps)
    liquidation_classification = classify_state("EEEUSDT", snaps, deltas, persistence)
    classification = liquidation_classification
    check(
        "Test6 LIQUIDATION_EVENT detected",
        classification["state"] == "LIQUIDATION_EVENT",
        f"state={classification['state']}",
    )
    check(
        "Test6 liquidation reason explicitly says inferred / not confirmed",
        "inferred" in classification["reason"].lower() and "not confirmed" in classification["reason"].lower(),
        classification["reason"],
    )

    # --- Test 7: repeated state should not create a new transition ---
    state_store = {}
    snap1 = classify_state("FFFUSDT", snaps, deltas, persistence)  # reuse EEE-style liquidation shape
    score1 = compute_score(snap1, deltas, persistence, snaps[-1])
    rec1 = update_state_record(state_store, "FFFUSDT", snap1, score1)
    time.sleep(0.01)
    rec2 = update_state_record(state_store, "FFFUSDT", snap1, score1)
    check(
        "Test7 repeated state keeps same 'since' (no new transition)",
        rec1["since"] == rec2["since"] and rec2["is_new_transition"] is False,
        f"since1={rec1['since']} since2={rec2['since']} new2={rec2['is_new_transition']}",
    )

    # --- Test 8: insufficient history -> None deltas / NEUTRAL, not fabricated ---
    snaps = [_make_snapshot("GGGUSDT", 100.0, 1_000_000.0, 0.0001)]
    deltas = compute_deltas(snaps)
    persistence = compute_funding_persistence(snaps)
    classification = classify_state("GGGUSDT", snaps, deltas, persistence)
    check(
        "Test8 insufficient history -> deltas are None, state NEUTRAL",
        deltas["price_pct"]["1h"] is None and deltas["oi_pct"]["1h"] is None
        and classification["state"] == "NEUTRAL",
        f"deltas={deltas} state={classification['state']}",
    )

    # --- Extra: L/S ratio cannot independently create a trap state ---
    snaps = [_make_snapshot("HHHUSDT", 100 + i * 0.02, 1_000_000, 0.00005, ls_ratio=3.0, minutes_ago=(7-i)*15) for i in range(8)]
    deltas = compute_deltas(snaps)
    persistence = compute_funding_persistence(snaps)
    classification = classify_state("HHHUSDT", snaps, deltas, persistence)
    classification = apply_ls_ratio_adjustment(classification, snaps[-1])
    check(
        "Extra: extreme L/S skew alone does not create a trap state",
        classification["state"] not in ("LONG_TRAP_RISK", "SHORT_TRAP_RISK"),
        f"state={classification['state']}",
    )

    # --- Extra: full monitor() cycle degrades safely when bulk fetch fails ---
    class DeadAdapter(BybitAdapter):
        def fetch_bulk_tickers(self):
            return []

    out = monitor(symbols=["BTCUSDT"], adapter=DeadAdapter(), history={}, state_store={}, persist=False)
    check(
        "Extra: monitor() skips cycle cleanly when bulk fetch fails",
        out["bulk_fetch_ok"] is False and out["results"] == {},
    )

    # --- Regression: timestamp gap must invalidate timeframe delta ---
    gap_snaps = [
        _make_snapshot("GAPUSDT", 100.0, 1_000_000.0, 0.0001, minutes_ago=90),
        _make_snapshot("GAPUSDT", 95.0, 1_100_000.0, 0.0001, minutes_ago=0),
    ]
    gap_deltas = compute_deltas(gap_snaps)
    check(
        "Regression: missed-run timestamp gap does not fabricate 15m delta",
        gap_deltas["price_pct"]["15m"] is None and gap_deltas["oi_pct"]["15m"] is None,
        f"deltas={gap_deltas}",
    )

    # --- Regression: funding persistence must break across a material gap ---
    funding_gap = [
        _make_snapshot("FUNDGAPUSDT", 100.0, 1_000_000.0, 0.0006, minutes_ago=60),
        _make_snapshot("FUNDGAPUSDT", 100.0, 1_000_000.0, 0.0006, minutes_ago=45),
        _make_snapshot("FUNDGAPUSDT", 100.0, 1_000_000.0, 0.0006, minutes_ago=0),
    ]
    fp = compute_funding_persistence(funding_gap)
    check(
        "Regression: funding persistence breaks across timestamp gap",
        fp["consecutive"] == 1 and fp["persistent"] is False,
        f"persistence={fp}",
    )

    # --- Regression: neutral OI movement cannot create a bullish score ---
    bearish_neutral = [
        _make_snapshot("NEUTRALUSDT", 100.0, 1_000_000.0, 0.00005, minutes_ago=60),
        _make_snapshot("NEUTRALUSDT", 98.0, 1_050_000.0, 0.00005, minutes_ago=45),
        _make_snapshot("NEUTRALUSDT", 98.0, 1_060_000.0, 0.00005, minutes_ago=30),
        _make_snapshot("NEUTRALUSDT", 97.0, 1_070_000.0, 0.00005, minutes_ago=15),
        _make_snapshot("NEUTRALUSDT", 96.0, 1_080_000.0, 0.00005, minutes_ago=0),
    ]
    nd = compute_deltas(bearish_neutral)
    nf = compute_funding_persistence(bearish_neutral)
    nc = classify_state("NEUTRALUSDT", bearish_neutral, nd, nf)
    ns = compute_score(nc, nd, nf, bearish_neutral[-1])
    check(
        "Regression: NEUTRAL state cannot receive misleading positive OI score",
        nc["state"] == "NEUTRAL" and ns <= 0,
        f"state={nc['state']} score={ns} deltas={nd}",
    )

    # --- Regression: liquidation event is explicitly inferred ---
    check(
        "Regression: liquidation event metadata is explicitly inferred",
        liquidation_classification.get("evidence_type") == "inferred",
        f"classification={liquidation_classification}",
    )

    return results


def _print_test_results(results):
    print("=" * 78)
    print("DERIVATIVES MONITOR -- STANDALONE TEST RESULTS")
    print("=" * 78)
    passed = 0
    for name, ok, detail in results:
        mark = "PASS" if ok else "FAIL"
        if ok:
            passed += 1
        line = f"[{mark}] {name}"
        if detail and (not ok):
            line += f"  -- {detail}"
        print(line)
    print("-" * 78)
    print(f"{passed}/{len(results)} tests passed")
    print("=" * 78)


if __name__ == "__main__":
    test_results = _run_tests()
    _print_test_results(test_results)

    print("\nAttempting a live demo cycle against BTCUSDT/ETHUSDT (network permitting)...")
    demo_out = monitor(symbols=["BTCUSDT", "ETHUSDT"], persist=False)
    if not demo_out["bulk_fetch_ok"]:
        print("  (bulk ticker fetch unavailable in this environment -- this is expected/handled "
              "gracefully, not a test failure)")
    else:
        for sym, r in demo_out["results"].items():
            print(f"  {sym}: state={r['state']} score={r['score']}  reason={r['reason']}")
        if demo_out["failed_symbols"]:
            print(f"  failed symbols this cycle: {demo_out['failed_symbols']}")
