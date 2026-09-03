#!/usr/bin/env python3
"""
SMC Futures Scanner (Pydroid 3 friendly) -- v2 (watchlist upgrade)
====================================================================
Single-file, menu-driven Smart Money Concepts scanner for crypto futures.

WHAT'S NEW IN THIS VERSION (watchlist v2)
------------------------------------------
- Watchlist entries now have FOUR statuses instead of three:
    "pending"      -> not yet triggered
    "triggered"    -> confirmed, actively tracked
    "invalidated"  -> stop was breached (message says whether that was
                       before or after it triggered)
    "expired"      -> was pending too long, or price ran away without ever
                       tagging the zone (never got the chance to trigger)
- Every time you refresh a TRIGGERED entry, it is re-scored from scratch
  against fresh candles -- it is NOT stuck at its original score. The new
  score is UNCAPPED (can exceed 100) since it reflects live conviction
  (profit progress + continued HTF agreement), not original entry quality.
- When adding a qualifying setup to the watchlist you now choose how the
  stop behaves once it triggers:
    "fixed"            -> stop never moves once set
    "trail_structural" -> stop tightens toward price as new structure
                          forms; never loosens
    "trail_breakeven"  -> stays fixed until price reaches TRAIL_TRIGGER_R,
                          then jumps to entry price, then trails
                          structurally beyond that
- Entries can optionally be split into multiple fill legs (scaled entries)
  instead of a single price.

Everything else (exchange fallback chain, SMC detection engine, scoring,
entry planning) is unchanged from the original build.
"""

import json
import math
import os
import sys
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import requests

# ============================================================================
# CONFIG
# ============================================================================

TFS_ALL = ["1D", "4H", "1H", "15M"]          # scanned top -> bottom every time
ENTRY_TF = "15M"                              # lowest TF = the one we plan entries from
HTF_TFS = ["1D", "4H"]                        # used for higher-timeframe agreement scoring

REQUEST_DELAY = 0.25                          # seconds between symbol requests (be polite to APIs)
CANDLE_LIMIT = 200                            # candles pulled per timeframe

ZONE_PROXIMITY_PCT = 1.5                      # "approaching" a zone = within this % of it
MIN_SETUP_SCORE = 60                          # discovery floor: interesting setups are retained
MIN_TRADE_SCORE = 80                          # minimum score for a trade alert / READY trade
HIGH_CONVICTION_SCORE = 90                    # high-conviction tier
AUTO_ADD_MIN_SCORE = HIGH_CONVICTION_SCORE                       # unattended full-scan (GitHub Actions) only
                                               # auto-adds setups scoring at or above this
AUTO_TRAIL_MODE = "trail_structural"          # default stop behavior for auto-added entries
                                               # (no human present to choose interactively)
AUTO_ADD_EXCLUDE_TYPES = {"SCALP"}            # trade_types full_scan.py will never auto-add,
                                               # regardless of score -- e.g. {"SCALP"} or
                                               # {"SCALP", "SWING"} to only keep INTRADAY
AUTO_ADD_READY_ONLY = True                    # if True, full_scan.py only auto-adds setups that
                                               # are immediately actionable RIGHT NOW -- price
                                               # already inside the zone (market entry) or a
                                               # breakout retest already underway (clean limit
                                               # entry right there). Genuinely-still-approaching
                                               # setups (mode "WAIT FOR PULLBACK") are skipped
                                               # entirely rather than added to sit and wait.

WATCHLIST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "smc_watchlist.json")

EXCHANGE_CHAIN = ["bybit", "bingx", "mexc", "kucoin", "bitget", "okx", "binance"]
# Order matters: Binance blocks API access from US-based cloud IPs (which is
# where GitHub Actions runners live), so it's pushed to the back -- it'll
# still work fine if this ever runs from a non-US environment (e.g. your
# phone), it's just no longer tried first and won't stall the chain when
# running unattended. Bybit is first since it doesn't geo-block those IPs
# and uses a plain "BTCUSDT"-style symbol format. BingX and MEXC come next
# since those are your actual trading venues -- a setup GitHub finds on one
# of those is a symbol you can paste directly into that exchange without
# translating naming conventions (unlike KuCoin's "BTCUSDTM" format).

TF_MINUTES = {"15M": 15, "1H": 60, "4H": 240, "1D": 1440}

HEADERS = {"User-Agent": "Mozilla/5.0 (SMC-Scanner)"}

# ----------------------------------------------------------------------
# PHASE 4 CONFIG -- execution-state thresholds and lifecycle tolerances.
# This file has no separate config.py (single-file architecture), so
# these live here alongside the rest of # CONFIG above, same as every
# other tunable in this codebase. All of Phase 4's gating logic reads
# from these constants -- nothing is hard-coded inline. Tune freely
# during backtesting without touching function bodies.
# ----------------------------------------------------------------------
MIN_SETUP_QUALITY = 70            # below this, the underlying thesis isn't good enough to track at all
MIN_READY_SETUP_QUALITY = 80      # setup_quality floor to ever reach a READY_* state
MIN_READY_ENTRY_QUALITY = 75      # entry_quality floor to ever reach a READY_* state
NEAR_READY_ENTRY_QUALITY = 55     # entry_quality between this and MIN_READY_ENTRY_QUALITY -> NEAR_READY
MIN_STRUCTURAL_RR = 2.0           # structural_rr floor -- below this, prefer NO_TRADE over forcing it

MAX_LIMIT_DISTANCE_ATR = 1.5      # a limit entry beyond this many ATRs from current price is too far
                                    # to call READY_LIMIT -- becomes NEAR_READY or WAIT_PULLBACK instead
ATR_PERIOD = 14                    # standard ATR lookback, computed on the entry timeframe

STALE_EXPIRE_CYCLES = 8            # ~2 hours at a 15-min scan cadence -- a non-READY setup this old expires
READY_STALE_EXPIRE_CYCLES = 24     # READY setups get more patience (~6 hours) before auto-expiring unactioned

# Material-change tolerances -- a re-scan only triggers a new alert if a
# change EXCEEDS these, otherwise it's "EXISTING SETUP -- UNCHANGED"
MATERIAL_CHANGE_PRICE_TOL_PCT = 0.5     # entry zone / preferred entry / SL / target shift tolerance
MATERIAL_CHANGE_RR_TOL = 0.3            # structural_rr must move by more than this to count as a change
SETUP_FINGERPRINT_BUCKET_PCT = 0.5      # rounding granularity used to build a stable setup identity

SETUP_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "smc_setup_states.json")

# --- Telegram notifications (optional) ---
# Fill these in with your own bot token + chat ID, or set them as
# environment variables (TG_BOT_TOKEN / TG_CHAT_ID) so you don't have to
# paste secrets directly into the script. Leave both blank to disable.
TELEGRAM_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
TELEGRAM_ENABLED = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)


def send_telegram_message(text):
    """Best-effort push notification. Never raises -- a Telegram outage
    should never crash a scan or a watchlist refresh."""
    if not TELEGRAM_ENABLED:
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        resp = requests.post(url, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "disable_web_page_preview": True,
        }, timeout=8)
        return resp.status_code == 200
    except Exception:
        return False

# --- watchlist v2 config ---
TRAIL_TRIGGER_R = 1.0          # for "trail_breakeven": R-multiple at which stop jumps to entry
EXPIRE_AFTER_HOURS = 48        # pending entries older than this with no trigger -> expired
EXPIRE_DIST_MULT = 3           # if price is now this many x ZONE_PROXIMITY_PCT away from the
                                # zone (having run without you) -> expired
VALID_TRAIL_MODES = ("fixed", "trail_structural", "trail_breakeven")

# Failed-trade / re-entry protection
REENTRY_COOLDOWN_HOURS = 4
REENTRY_HISTORY_HOURS = 168
REENTRY_MIN_NEW_R = 0.50


# ============================================================================
# EXCHANGE ADAPTERS
# ----------------------------------------------------------------------------
# Each adapter exposes:
#   get_symbols(limit)      -> list[str] of top futures symbols by 24h turnover
#   get_klines(symbol, tf)  -> pandas.DataFrame[time, open, high, low, close, volume]
# All network calls are wrapped by the caller in try/except, so adapters can
# raise freely on any failure (bad field, timeout, HTTP error, etc).
# ============================================================================

class ExchangeAdapter:
    name = "base"

    def get_symbols(self, limit):
        raise NotImplementedError

    def get_klines(self, symbol, tf):
        raise NotImplementedError


class BinanceAdapter(ExchangeAdapter):
    name = "Binance Futures"
    BASE = "https://fapi.binance.com"
    TF_MAP = {"15M": "15m", "1H": "1h", "4H": "4h", "1D": "1d"}

    def get_symbols(self, limit):
        r = requests.get(f"{self.BASE}/fapi/v1/ticker/24hr", headers=HEADERS, timeout=10)
        r.raise_for_status()
        data = r.json()
        usdt_perp = [d for d in data if d.get("symbol", "").endswith("USDT")]
        usdt_perp.sort(key=lambda d: float(d.get("quoteVolume", 0)), reverse=True)
        return [d["symbol"] for d in usdt_perp[:limit]]

    def get_klines(self, symbol, tf):
        interval = self.TF_MAP[tf]
        url = f"{self.BASE}/fapi/v1/klines"
        params = {"symbol": symbol, "interval": interval, "limit": CANDLE_LIMIT}
        r = requests.get(url, params=params, headers=HEADERS, timeout=10)
        r.raise_for_status()
        raw = r.json()
        df = pd.DataFrame(raw, columns=[
            "time", "open", "high", "low", "close", "volume", "ct", "qav",
            "trades", "tbbav", "tbqav", "ignore"
        ])
        for c in ["open", "high", "low", "close", "volume"]:
            df[c] = df[c].astype(float)
        return df[["time", "open", "high", "low", "close", "volume"]]


class BybitAdapter(ExchangeAdapter):
    name = "Bybit"
    BASE = "https://api.bybit.com"
    TF_MAP = {"15M": "15", "1H": "60", "4H": "240", "1D": "D"}

    def get_symbols(self, limit):
        r = requests.get(f"{self.BASE}/v5/market/tickers", params={"category": "linear"},
                          headers=HEADERS, timeout=10)
        r.raise_for_status()
        data = r.json()["result"]["list"]
        usdt_perp = [d for d in data if d.get("symbol", "").endswith("USDT")]
        usdt_perp.sort(key=lambda d: float(d.get("turnover24h", 0)), reverse=True)
        return [d["symbol"] for d in usdt_perp[:limit]]

    def get_klines(self, symbol, tf):
        interval = self.TF_MAP[tf]
        url = f"{self.BASE}/v5/market/kline"
        params = {"category": "linear", "symbol": symbol, "interval": interval, "limit": CANDLE_LIMIT}
        r = requests.get(url, params=params, headers=HEADERS, timeout=10)
        r.raise_for_status()
        raw = r.json()["result"]["list"]
        raw = list(reversed(raw))  # bybit returns newest-first
        df = pd.DataFrame(raw, columns=["time", "open", "high", "low", "close", "volume", "turnover"])
        for c in ["open", "high", "low", "close", "volume"]:
            df[c] = df[c].astype(float)
        return df[["time", "open", "high", "low", "close", "volume"]]


class KucoinAdapter(ExchangeAdapter):
    name = "KuCoin Futures"
    BASE = "https://api-futures.kucoin.com"
    TF_MAP = {"15M": 15, "1H": 60, "4H": 240, "1D": 1440}

    def get_symbols(self, limit):
        r = requests.get(f"{self.BASE}/api/v1/contracts/active", headers=HEADERS, timeout=10)
        r.raise_for_status()
        data = r.json()["data"]
        usdt_perp = [d for d in data if str(d.get("symbol", "")).endswith("USDTM")]
        usdt_perp.sort(key=lambda d: float(d.get("turnoverOf24h", 0) or 0), reverse=True)
        return [d["symbol"] for d in usdt_perp[:limit]]

    def get_klines(self, symbol, tf):
        granularity = self.TF_MAP[tf]
        end = int(time.time() * 1000)
        start = end - granularity * 60 * 1000 * CANDLE_LIMIT
        url = f"{self.BASE}/api/v1/kline/query"
        params = {"symbol": symbol, "granularity": granularity, "from": start, "to": end}
        r = requests.get(url, params=params, headers=HEADERS, timeout=10)
        r.raise_for_status()
        raw = r.json()["data"]
        df = pd.DataFrame(raw, columns=["time", "open", "high", "low", "close", "volume"])
        for c in ["open", "high", "low", "close", "volume"]:
            df[c] = df[c].astype(float)
        return df


class BingxAdapter(ExchangeAdapter):
    name = "BingX"
    BASE = "https://open-api.bingx.com"
    TF_MAP = {"15M": "15m", "1H": "1h", "4H": "4h", "1D": "1d"}

    def get_symbols(self, limit):
        r = requests.get(f"{self.BASE}/openApi/swap/v2/quote/ticker", headers=HEADERS, timeout=10)
        r.raise_for_status()
        data = r.json()["data"]
        usdt_perp = [d for d in data if d.get("symbol", "").endswith("-USDT")]
        usdt_perp.sort(key=lambda d: float(d.get("quoteVolume", 0) or 0), reverse=True)
        return [d["symbol"] for d in usdt_perp[:limit]]

    def get_klines(self, symbol, tf):
        interval = self.TF_MAP[tf]
        url = f"{self.BASE}/openApi/swap/v3/quote/klines"
        params = {"symbol": symbol, "interval": interval, "limit": CANDLE_LIMIT}
        r = requests.get(url, params=params, headers=HEADERS, timeout=10)
        r.raise_for_status()
        raw = r.json()["data"]
        df = pd.DataFrame(raw)
        df = df.rename(columns={"time": "time", "open": "open", "high": "high",
                                 "low": "low", "close": "close", "volume": "volume"})
        for c in ["open", "high", "low", "close", "volume"]:
            df[c] = df[c].astype(float)
        return df[["time", "open", "high", "low", "close", "volume"]]


class MexcAdapter(ExchangeAdapter):
    name = "MEXC"
    BASE = "https://contract.mexc.com"
    TF_MAP = {"15M": "Min15", "1H": "Min60", "4H": "Hour4", "1D": "Day1"}

    def get_symbols(self, limit):
        r = requests.get(f"{self.BASE}/api/v1/contract/ticker", headers=HEADERS, timeout=10)
        r.raise_for_status()
        data = r.json()["data"]
        usdt_perp = [d for d in data if d.get("symbol", "").endswith("_USDT")]
        usdt_perp.sort(key=lambda d: float(d.get("amount24", 0) or 0), reverse=True)
        return [d["symbol"] for d in usdt_perp[:limit]]

    def get_klines(self, symbol, tf):
        interval = self.TF_MAP[tf]
        url = f"{self.BASE}/api/v1/contract/kline/{symbol}"
        params = {"interval": interval}
        r = requests.get(url, params=params, headers=HEADERS, timeout=10)
        r.raise_for_status()
        d = r.json()["data"]
        df = pd.DataFrame({
            "time": d["time"], "open": d["open"], "high": d["high"],
            "low": d["low"], "close": d["close"], "volume": d["vol"],
        })
        for c in ["open", "high", "low", "close", "volume"]:
            df[c] = df[c].astype(float)
        return df.tail(CANDLE_LIMIT).reset_index(drop=True)


class BitgetAdapter(ExchangeAdapter):
    name = "Bitget"
    BASE = "https://api.bitget.com"
    TF_MAP = {"15M": "15m", "1H": "1H", "4H": "4H", "1D": "1D"}

    def get_symbols(self, limit):
        r = requests.get(f"{self.BASE}/api/v2/mix/market/tickers",
                          params={"productType": "usdt-futures"}, headers=HEADERS, timeout=10)
        r.raise_for_status()
        data = r.json()["data"]
        usdt_perp = [d for d in data if d.get("symbol", "").endswith("USDT")]
        usdt_perp.sort(key=lambda d: float(d.get("usdtVolume", 0) or 0), reverse=True)
        return [d["symbol"] for d in usdt_perp[:limit]]

    def get_klines(self, symbol, tf):
        interval = self.TF_MAP[tf]
        url = f"{self.BASE}/api/v2/mix/market/candles"
        params = {"symbol": symbol, "granularity": interval, "productType": "usdt-futures",
                   "limit": str(CANDLE_LIMIT)}
        r = requests.get(url, params=params, headers=HEADERS, timeout=10)
        r.raise_for_status()
        raw = r.json()["data"]
        raw = list(reversed(raw))
        df = pd.DataFrame(raw, columns=["time", "open", "high", "low", "close", "volume", "qv"])
        for c in ["open", "high", "low", "close", "volume"]:
            df[c] = df[c].astype(float)
        return df[["time", "open", "high", "low", "close", "volume"]]


class OkxAdapter(ExchangeAdapter):
    name = "OKX"
    BASE = "https://www.okx.com"
    TF_MAP = {"15M": "15m", "1H": "1H", "4H": "4H", "1D": "1D"}

    def get_symbols(self, limit):
        r = requests.get(f"{self.BASE}/api/v5/market/tickers", params={"instType": "SWAP"},
                          headers=HEADERS, timeout=10)
        r.raise_for_status()
        data = r.json()["data"]
        usdt_perp = [d for d in data if d.get("instId", "").endswith("-USDT-SWAP")]
        usdt_perp.sort(key=lambda d: float(d.get("volCcy24h", 0) or 0), reverse=True)
        return [d["instId"] for d in usdt_perp[:limit]]

    def get_klines(self, symbol, tf):
        interval = self.TF_MAP[tf]
        url = f"{self.BASE}/api/v5/market/candles"
        params = {"instId": symbol, "bar": interval, "limit": str(CANDLE_LIMIT)}
        r = requests.get(url, params=params, headers=HEADERS, timeout=10)
        r.raise_for_status()
        raw = r.json()["data"]
        raw = list(reversed(raw))
        df = pd.DataFrame(raw, columns=["time", "open", "high", "low", "close", "volume",
                                         "volCcy", "volCcyQuote", "confirm"])
        for c in ["open", "high", "low", "close", "volume"]:
            df[c] = df[c].astype(float)
        return df[["time", "open", "high", "low", "close", "volume"]]


ADAPTERS = {
    "binance": BinanceAdapter(),
    "bybit": BybitAdapter(),
    "kucoin": KucoinAdapter(),
    "bingx": BingxAdapter(),
    "mexc": MexcAdapter(),
    "bitget": BitgetAdapter(),
    "okx": OkxAdapter(),
}


def detect_active_exchange():
    """Try each exchange in EXCHANGE_CHAIN order; return the first that responds."""
    for key in EXCHANGE_CHAIN:
        adapter = ADAPTERS[key]
        try:
            syms = adapter.get_symbols(5)
            if syms:
                print(f"Connected to {adapter.name}.")
                return key
        except Exception:
            continue
    return None


def get_symbols_with_fallback(active_key, limit):
    order = [active_key] + [k for k in EXCHANGE_CHAIN if k != active_key]
    for key in order:
        try:
            syms = ADAPTERS[key].get_symbols(limit)
            if syms:
                return key, syms
        except Exception:
            continue
    return None, []


def get_klines_with_fallback(active_key, symbol, tf, symbol_map=None):
    """
    Try the active exchange first for this symbol/timeframe; on failure fall
    through the rest of the chain. symbol_map lets us translate a symbol
    string between exchanges is skipped here for simplicity -- if a symbol
    genuinely doesn't exist on a fallback exchange it will just fail fast
    and move on.
    """
    order = [active_key] + [k for k in EXCHANGE_CHAIN if k != active_key]
    for key in order:
        try:
            df = ADAPTERS[key].get_klines(symbol, tf)
            if df is not None and len(df) >= 20:
                # Defensive fix: don't trust any adapter's raw ordering.
                # Some exchanges' adapters explicitly reverse newest-first
                # data (see BybitAdapter/BitgetAdapter/OkxAdapter), but a
                # gap in that per-adapter handling (confirmed missing on
                # BingX) silently treated the OLDEST pulled candle as
                # "current price" -- with CANDLE_LIMIT candles on a short
                # timeframe that can be many hours stale. Sorting by time
                # here, once, centrally, makes correctness independent of
                # whatever order any given exchange's API happens to
                # return, present or future.
                df = df.copy()
                df["time"] = pd.to_numeric(df["time"], errors="coerce")
                df = df.sort_values("time").reset_index(drop=True)
                return key, df
        except Exception:
            continue
    return None, None


# ============================================================================
# SMC ANALYSIS ENGINE
# ============================================================================

def find_swings(df, left=2, right=2):
    """Fractal swing highs/lows: a bar is a swing if it's the local extreme
    within `left` bars before and `right` bars after."""
    highs, lows = [], []
    n = len(df)
    for i in range(left, n - right):
        window_h = df["high"].iloc[i - left:i + right + 1]
        window_l = df["low"].iloc[i - left:i + right + 1]
        if df["high"].iloc[i] == window_h.max():
            highs.append(i)
        if df["low"].iloc[i] == window_l.min():
            lows.append(i)
    return highs, lows


def find_structure_breaks(df, highs, lows):
    """
    Walk swings in time order, tracking the last confirmed swing high/low,
    and flag BoS (break in the direction of the prevailing trend) vs CHoCH
    (a break that flips the trend) using close-based breaks.
    Returns a list of dicts: {index, type ('BoS'/'CHoCH'), direction, price}
    """
    events = []
    swing_points = sorted([(i, "H") for i in highs] + [(i, "L") for i in lows])
    if not swing_points:
        return events

    trend = None  # 'up' or 'down'
    last_swing_high = None
    last_swing_low = None

    for i in range(len(df)):
        close = df["close"].iloc[i]

        # update tracked swing extremes as we pass them
        for idx, kind in swing_points:
            if idx == i:
                if kind == "H":
                    last_swing_high = df["high"].iloc[idx]
                else:
                    last_swing_low = df["low"].iloc[idx]

        if last_swing_high is not None and close > last_swing_high:
            if trend in (None, "up"):
                events.append({"index": i, "type": "BoS", "direction": "bullish", "price": last_swing_high})
            else:
                events.append({"index": i, "type": "CHoCH", "direction": "bullish", "price": last_swing_high})
            trend = "up"
            last_swing_high = None  # consumed; wait for the next one

        elif last_swing_low is not None and close < last_swing_low:
            if trend in (None, "down"):
                events.append({"index": i, "type": "BoS", "direction": "bearish", "price": last_swing_low})
            else:
                events.append({"index": i, "type": "CHoCH", "direction": "bearish", "price": last_swing_low})
            trend = "down"
            last_swing_low = None

    return events


def find_order_blocks(df, events):
    """
    For each structure-break event, the order block is the last opposing
    candle before the impulsive move that caused the break.
    Returns list of dicts: {direction, low, high, index, mitigated(bool set later)}
    """
    obs = []
    for ev in events:
        idx = ev["index"]
        direction = ev["direction"]
        # look back from idx for the last opposite-colored candle
        search_start = max(0, idx - 15)
        found = None
        for j in range(idx - 1, search_start - 1, -1):
            o, c = df["open"].iloc[j], df["close"].iloc[j]
            is_bearish_candle = c < o
            is_bullish_candle = c > o
            if direction == "bullish" and is_bearish_candle:
                found = j
                break
            if direction == "bearish" and is_bullish_candle:
                found = j
                break
        if found is not None:
            low = df["low"].iloc[found]
            high = df["high"].iloc[found]
            obs.append({"direction": direction, "low": low, "high": high,
                        "index": found, "break_index": idx})
    return obs


def find_fvgs(df):
    """3-candle fair value gap: gap between candle[i-1] and candle[i+1]."""
    fvgs = []
    for i in range(1, len(df) - 1):
        prev_high = df["high"].iloc[i - 1]
        prev_low = df["low"].iloc[i - 1]
        next_high = df["high"].iloc[i + 1]
        next_low = df["low"].iloc[i + 1]
        if next_low > prev_high:
            fvgs.append({"direction": "bullish", "low": prev_high, "high": next_low, "index": i})
        elif next_high < prev_low:
            fvgs.append({"direction": "bearish", "low": next_high, "high": prev_low, "index": i})
    return fvgs


def find_liquidity_sweeps(df, highs, lows, lookback=30):
    """A sweep = wick beyond a prior swing point, followed by a close back
    inside range. Only checks the most recent `lookback` bars for relevance."""
    sweeps = []
    n = len(df)
    start = max(0, n - lookback)
    for i in range(start, n):
        row = df.iloc[i]
        for hi in highs:
            if hi < i and row["high"] > df["high"].iloc[hi] and row["close"] < df["high"].iloc[hi]:
                sweeps.append({"direction": "bearish", "price": df["high"].iloc[hi], "index": i})
        for lo in lows:
            if lo < i and row["low"] < df["low"].iloc[lo] and row["close"] > df["low"].iloc[lo]:
                sweeps.append({"direction": "bullish", "price": df["low"].iloc[lo], "index": i})
    return sweeps


def mark_mitigated(zones, df, current_index):
    """Mark a zone mitigated if price has traded back through it after formation."""
    out = []
    for z in zones:
        mitigated = False
        z_idx = z.get("break_index", z.get("index", 0))
        for i in range(z_idx + 1, current_index + 1):
            if df["low"].iloc[i] <= z["high"] and df["high"].iloc[i] >= z["low"]:
                mitigated = True
                break
        z2 = dict(z)
        z2["mitigated"] = mitigated
        out.append(z2)
    return out


def analyze_timeframe(df):
    """
    Run the full SMC pipeline on one timeframe's OHLCV dataframe.
    Returns a dict summarizing bias, last break, zones, sweeps, price.
    """
    df = df.reset_index(drop=True)
    n = len(df)
    if n < 30:
        return None

    highs, lows = find_swings(df)
    events = find_structure_breaks(df, highs, lows)
    obs_raw = find_order_blocks(df, events)
    fvgs_raw = find_fvgs(df)

    current_index = n - 1
    price = df["close"].iloc[-1]

    obs = mark_mitigated(obs_raw, df, current_index)
    fvgs = mark_mitigated(fvgs_raw, df, current_index)

    unmitigated_obs = [z for z in obs if not z["mitigated"]]
    unmitigated_fvgs = [z for z in fvgs if not z["mitigated"]]
    all_unmitigated = unmitigated_obs + unmitigated_fvgs

    last_event = events[-1] if events else None
    bias = "RANGING"
    if last_event:
        bias = "BULLISH" if last_event["direction"] == "bullish" else "BEARISH"

    # nearest unmitigated zone to current price
    nearest_zone = None
    if all_unmitigated:
        def dist(z):
            mid = (z["low"] + z["high"]) / 2
            return abs(price - mid)
        nearest_zone = min(all_unmitigated, key=dist)

    sweeps = find_liquidity_sweeps(df, highs, lows)
    recent_sweep = sweeps[-1] if sweeps else None

    # is a fresh structure break within the last few bars?
    fresh_break = last_event is not None and (current_index - last_event["index"] <= 3)

    # swing price levels -- used downstream for structural stops and
    # liquidity-based take-profit targets (previous highs/lows are where
    # resting liquidity tends to sit).
    swing_high_prices = sorted(set(round(float(df["high"].iloc[i]), 10) for i in highs))
    swing_low_prices = sorted(set(round(float(df["low"].iloc[i]), 10) for i in lows))

    bullish_zones = [z for z in unmitigated_obs + unmitigated_fvgs if z["direction"] == "bullish"]
    bearish_zones = [z for z in unmitigated_obs + unmitigated_fvgs if z["direction"] == "bearish"]

    return {
        "price": price,
        "bias": bias,
        "last_event": last_event,
        "nearest_zone": nearest_zone,
        "unmitigated_obs": len(unmitigated_obs),
        "unmitigated_fvgs": len(unmitigated_fvgs),
        "recent_sweep": recent_sweep,
        "fresh_break": fresh_break,
        "current_index": current_index,
        "swing_high_prices": swing_high_prices,
        "swing_low_prices": swing_low_prices,
        "bullish_zones": bullish_zones,
        "bearish_zones": bearish_zones,
        "df": df,
    }


# ============================================================================
# SETUP SCORING + ENTRY PLANNING
# ============================================================================

def zone_label(zone):
    return "Order Block" if "break_index" in zone else "FVG"


def pct_distance(price, zone):
    mid = (zone["low"] + zone["high"]) / 2
    if mid == 0:
        return 999
    return abs(price - mid) / mid * 100


def score_setup(tf_results):
    """
    tf_results: dict tf -> analyze_timeframe() output (or None).
    Scores 0-100 based on:
      - HTF (1D/4H) agreement with the entry-timeframe bias   (up to 40)
      - a real unmitigated zone on the entry timeframe near price (up to 30)
      - a fresh BoS/CHoCH on the entry timeframe or an aligned HTF (up to 20)
      - a liquidity sweep supporting the direction (up to 10)
    Returns (score, direction) where direction is 'BULLISH'/'BEARISH'/None.
    """
    entry = tf_results.get(ENTRY_TF)
    if entry is None or entry["bias"] == "RANGING":
        return 0, None

    direction = entry["bias"]
    score = 0

    # HTF agreement
    agree = 0
    for tf in HTF_TFS:
        r = tf_results.get(tf)
        if r and r["bias"] == direction:
            agree += 1
    score += agree * 20  # up to 40

    # real nearby zone on entry TF
    zone = entry.get("nearest_zone")
    if zone:
        dist = pct_distance(entry["price"], zone)
        zone_matches_direction = zone["direction"] == ("bullish" if direction == "BULLISH" else "bearish")
        if zone_matches_direction and dist <= ZONE_PROXIMITY_PCT:
            score += 30
        elif zone_matches_direction and dist <= ZONE_PROXIMITY_PCT * 2:
            score += 15

    # fresh break, entry TF or any HTF
    if entry.get("fresh_break"):
        score += 12
    for tf in HTF_TFS:
        r = tf_results.get(tf)
        if r and r.get("fresh_break") and r["bias"] == direction:
            score += 8
            break

    # sweep support
    sweep = entry.get("recent_sweep")
    if sweep and sweep["direction"] == ("bullish" if direction == "BULLISH" else "bearish"):
        score += 10

    return min(score, 100), direction


# ----------------------------------------------------------------------
# PHASE 2 -- MARKET REGIME FILTER
# ----------------------------------------------------------------------
# score_setup() above is untouched (Phase 1 behavior preserved exactly).
# Regime evaluation is a separate, additive layer: compute_market_regime()
# reads 1D/4H/1H only (15M is execution structure, not a regime vote),
# check_reversal_evidence() demands more than a lone 15M CHoCH before
# calling something a genuine reversal, and evaluate_regime() combines
# both into the classification + penalty. score_setup_with_regime() is
# the new entry point callers should use going forward; score_setup()
# itself still works standalone for anything that only wants Phase 1.

REGIME_TF_WEIGHTS = {"1D": 0.50, "4H": 0.33, "1H": 0.17}   # macro -> primary -> setup structure
COUNTERTREND_PENALTY = 30   # points subtracted from score_setup()'s 0-100 total
CONFLICTED_PENALTY = 20
RANGE_PENALTY = 10


def compute_market_regime(tf_results):
    """
    Weighted read of 1D/4H/1H to determine the dominant regime. 15M gets
    no vote here -- that's the whole point of Phase 2: a 15M CHoCH must
    not flip the read of an actual 4H/1D trend.

    Returns dict: regime, regime_bias, regime_confidence, regime_reason.
    regime is one of: STRONG_UPTREND, WEAK_UPTREND, STRONG_DOWNTREND,
    WEAK_DOWNTREND, RANGE, TRANSITION, UNKNOWN (no HTF data at all).
    """
    bull_w = bear_w = range_w = available_w = 0.0
    for tf, w in REGIME_TF_WEIGHTS.items():
        r = tf_results.get(tf)
        if r is None:
            continue
        available_w += w
        if r["bias"] == "BULLISH":
            bull_w += w
        elif r["bias"] == "BEARISH":
            bear_w += w
        else:
            range_w += w

    reason = ", ".join(
        f"{tf}={(tf_results.get(tf) or {}).get('bias', 'no data')}"
        for tf in ["1D", "4H", "1H"]
    )

    if available_w == 0:
        return {"regime": "UNKNOWN", "regime_bias": None,
                "regime_confidence": 0, "regime_reason": "No HTF data available."}

    bull_pct = bull_w / available_w
    bear_pct = bear_w / available_w
    range_pct = range_w / available_w

    d1, h4, h1 = tf_results.get("1D"), tf_results.get("4H"), tf_results.get("1H")

    # An explicit disagreement between the two dominant regime timeframes
    # is a real conflict regardless of what weighted averaging alone would
    # say -- 1D's larger weight shouldn't be allowed to paper over 4H
    # pointing the other way. Check this BEFORE the weighted-majority path.
    if (d1 and h4 and d1["bias"] in ("BULLISH", "BEARISH")
            and h4["bias"] in ("BULLISH", "BEARISH") and d1["bias"] != h4["bias"]):
        bias, strength = "TRANSITION", max(bull_pct, bear_pct, range_pct)
    elif bull_pct >= 0.5 and bull_pct >= bear_pct:
        bias, strength = "BULLISH", bull_pct
    elif bear_pct >= 0.5 and bear_pct > bull_pct:
        bias, strength = "BEARISH", bear_pct
    elif range_pct >= 0.5:
        bias, strength = "RANGE", range_pct
    elif abs(bull_pct - bear_pct) < 0.2:
        # no side has a real majority AND bull/bear are close -- the HTFs
        # are genuinely fighting each other, not just one abstaining
        bias, strength = "TRANSITION", max(bull_pct, bear_pct, range_pct)
    else:
        bias = "BULLISH" if bull_pct > bear_pct else "BEARISH"
        strength = max(bull_pct, bear_pct)

    if bias in ("BULLISH", "BEARISH"):
        trend_word = "UPTREND" if bias == "BULLISH" else "DOWNTREND"
        strong = (d1 and d1["bias"] == bias) and (h4 and h4["bias"] == bias) and \
                 (h1 is None or h1["bias"] == bias)
        regime = f"{'STRONG' if strong else 'WEAK'}_{trend_word}"
    elif bias == "RANGE":
        regime = "RANGE"
    else:
        regime = "TRANSITION"

    return {"regime": regime, "regime_bias": bias,
            "regime_confidence": round(strength * 100), "regime_reason": reason}


def check_reversal_evidence(tf_results, direction):
    """
    A genuine HTF reversal needs more than a 15M CHoCH. Requires, on at
    least one of 4H/1H: a fresh structural break (BoS or CHoCH) matching
    the NEW direction, preceded/accompanied by a liquidity sweep in that
    same new direction -- the "sweep the old trend's liquidity, then
    break structure" pattern that separates a real reversal from a
    normal pullback wiggle on a lower timeframe.

    Returns (confirmed: bool, reason: str).
    """
    dir_word = "bullish" if direction == "BULLISH" else "bearish"
    for tf in ["4H", "1H"]:
        r = tf_results.get(tf)
        if not r:
            continue
        fresh = r.get("fresh_break")
        last_event = r.get("last_event")
        sweep = r.get("recent_sweep")
        if (fresh and last_event and last_event["direction"] == dir_word
                and sweep and sweep["direction"] == dir_word):
            return True, (f"{tf} shows a liquidity sweep followed by a fresh "
                           f"{last_event['type']} {dir_word} -- real reversal evidence, "
                           f"not just a lower-timeframe wiggle.")
    return False, ("No HTF (4H/1H) sweep-then-structural-break combination found -- "
                    "a 15M-only signal isn't enough to call this a reversal.")


def evaluate_regime(tf_results, direction):
    """
    Full Phase 2 regime evaluation for one candidate direction. Every
    candidate gets exactly these fields, per spec:
      regime, regime_confidence, trend_alignment, countertrend_penalty,
      reversal_confirmation, regime_reason
    trend_alignment is one of: TREND_ALIGNED, COUNTERTREND, REVERSAL,
    RANGE, CONFLICTED.
    """
    info = compute_market_regime(tf_results)
    bias = info["regime_bias"]
    reversal_confirmed = False

    if bias is None:
        alignment, penalty = "CONFLICTED", CONFLICTED_PENALTY
        reason = "No usable HTF data -- cannot establish a regime."
    elif info["regime"] == "TRANSITION":
        alignment, penalty = "CONFLICTED", CONFLICTED_PENALTY
        reason = f"HTFs disagree with each other ({info['regime_reason']}) -- no clean regime to trade with or against."
    elif bias == "RANGE":
        alignment, penalty = "RANGE", RANGE_PENALTY
        reason = f"No directional HTF regime ({info['regime_reason']}) -- setup stands on local structure only."
    elif bias == direction:
        alignment, penalty = "TREND_ALIGNED", 0
        reason = f"Setup direction agrees with the HTF regime ({info['regime_reason']})."
    else:
        reversal_confirmed, reversal_reason = check_reversal_evidence(tf_results, direction)
        if reversal_confirmed:
            alignment, penalty = "REVERSAL", 0
            reason = f"HTF regime was {bias} ({info['regime_reason']}), but {reversal_reason}"
        else:
            alignment, penalty = "COUNTERTREND", COUNTERTREND_PENALTY
            reason = (f"Setup direction ({direction}) opposes the HTF regime "
                      f"({bias}, {info['regime_reason']}). {reversal_reason}")

    return {
        "regime": info["regime"],
        "regime_confidence": info["regime_confidence"],
        "trend_alignment": alignment,
        "countertrend_penalty": penalty,
        "reversal_confirmation": reversal_confirmed,
        "regime_reason": reason,
    }


def score_setup_with_regime(tf_results):
    """
    New entry point for callers that want Phase 1 + Phase 2 together:
    runs score_setup() exactly as before, then applies the regime
    penalty on top and returns everything the spec wants exposed.
    score_setup() itself is unchanged and still usable standalone.

    Returns (score, direction, regime_info) where score already has the
    countertrend/conflicted/range penalty applied (floored at 0), and
    regime_info is the dict from evaluate_regime().
    """
    score, direction = score_setup(tf_results)
    if direction is None:
        return score, direction, None

    regime_info = evaluate_regime(tf_results, direction)
    adjusted_score = max(0, score - regime_info["countertrend_penalty"])
    return adjusted_score, direction, regime_info


STRUCT_BUFFER_PCT = 0.15   # extra cushion beyond the structural stop point, in %


def find_structural_invalidation(entry, direction, zone):
    """
    Anchor the stop to the swing point that actually defines the zone's
    invalidation, not just the zone's own edge (the zone edge alone is
    usually inside normal wick noise). For a bullish setup, that's the
    nearest swing LOW at or below the zone low -- if price trades below
    that swing low, the liquidity/structure the zone depended on is gone.
    Mirror logic for bearish. Falls back to the zone edge (with buffer) if
    no qualifying swing point exists in the recent data.
    """
    low, high = zone["low"], zone["high"]

    if direction == "BULLISH":
        candidates = [p for p in entry["swing_low_prices"] if p <= low * 1.001]
        base = max(candidates) if candidates else low
        return base * (1 - STRUCT_BUFFER_PCT / 100)
    else:
        candidates = [p for p in entry["swing_high_prices"] if p >= high * 0.999]
        base = min(candidates) if candidates else high
        return base * (1 + STRUCT_BUFFER_PCT / 100)


def find_structural_invalidation_no_zone(entry, direction, price):
    """
    Same idea as find_structural_invalidation, but with no OB/FVG zone to
    anchor to at all -- used by setup types that aren't zone-based
    (breakout continuation, BOS continuation, liquidity sweep reversal,
    plain trend pullback to a swing point). Anchors to the nearest real
    swing point on the correct side of price; falls back to the most
    recent N-bar extreme if no qualifying swing exists yet (e.g. very
    early in a fresh trend with only one or two swings recorded).
    """
    if direction == "BULLISH":
        candidates = [p for p in entry["swing_low_prices"] if p < price]
        if candidates:
            base = max(candidates)  # nearest swing low below price
        else:
            df = entry["df"]
            base = float(df["low"].tail(20).min())
        return base * (1 - STRUCT_BUFFER_PCT / 100)
    else:
        candidates = [p for p in entry["swing_high_prices"] if p > price]
        if candidates:
            base = min(candidates)  # nearest swing high above price
        else:
            df = entry["df"]
            base = float(df["high"].tail(20).max())
        return base * (1 + STRUCT_BUFFER_PCT / 100)


def find_tp_targets(entry, direction, price, invalidation, max_targets=3):
    """
    Structure-based take-profit targets: resting liquidity (prior opposing
    swing highs/lows) and opposing unmitigated zones (supply/demand,
    support/resistance) that sit beyond current price in the trade's
    direction. Only real, detected levels are used -- no arbitrary fixed
    multiples. Returns a list of dicts sorted nearest-to-furthest, each
    with its price, a label, and its R-multiple relative to risk.
    Fewer than `max_targets` are returned if fewer real levels exist.
    """
    risk = abs(price - invalidation)
    if risk <= 0:
        return []

    candidates = []  # (price_level, label)

    if direction == "BULLISH":
        for p in entry["swing_high_prices"]:
            if p > price:
                candidates.append((p, "prior swing high (liquidity)"))
        for z in entry["bearish_zones"]:
            mid = (z["low"] + z["high"]) / 2
            if mid > price:
                candidates.append((z["low"], f"{zone_label(z)} (supply)"))
        candidates.sort(key=lambda t: t[0])
    else:
        for p in entry["swing_low_prices"]:
            if p < price:
                candidates.append((p, "prior swing low (liquidity)"))
        for z in entry["bullish_zones"]:
            mid = (z["low"] + z["high"]) / 2
            if mid < price:
                candidates.append((z["high"], f"{zone_label(z)} (demand)"))
        candidates.sort(key=lambda t: t[0], reverse=True)

    # de-dupe levels that are essentially the same price
    deduped = []
    for level, label in candidates:
        if not any(abs(level - lv) / max(abs(lv), 1e-9) < 0.001 for lv, _ in deduped):
            deduped.append((level, label))

    targets = []
    for level, label in deduped[:max_targets]:
        reward = abs(level - price)
        r_multiple = reward / risk
        targets.append({"price": level, "label": label, "r": r_multiple})

    return targets


# --- trade type classification ---
SCALP_RISK_PCT = 0.6     # stop within this % of price -> scalp territory
SWING_RISK_PCT = 2.0     # stop beyond this % of price -> swing territory
SCALP_MAX_R = 1.0        # if the furthest target is still under this many R, it's a scalp
                          # regardless of % (thin reward-to-risk = fast/tight setup)


def classify_trade_type(price, invalidation, targets):
    """
    Heuristic label -- SCALP / INTRADAY / SWING -- based on how tight the
    stop is (as % of price) and how far the furthest real target sits in
    R-multiples. Not a timeframe label from the analysis engine itself,
    just a practical read on "how much room does this setup actually have."
    Tune SCALP_RISK_PCT / SWING_RISK_PCT / SCALP_MAX_R above per-asset if
    a class of symbols (majors vs low-caps) consistently reads wrong.
    """
    if price == 0:
        return "INTRADAY"
    risk_pct = abs(price - invalidation) / price * 100
    max_r = max((t["r"] for t in targets), default=0)

    if risk_pct < SCALP_RISK_PCT or max_r < SCALP_MAX_R:
        return "SCALP"
    elif risk_pct < SWING_RISK_PCT:
        return "INTRADAY"
    else:
        return "SWING"


def is_ready_now(plan):
    """
    True if this setup is immediately actionable. As of Phase 1, every
    setup_type detect_setup_type() can return is itself a "this is
    happening now" signal -- a sweep+CHoCH just confirmed, a BoS just
    confirmed, or price is already sitting at the pullback level -- so
    there's no more "still approaching, wait" state to filter out here.
    (A genuine WAIT state returns to relevance in a later phase, once
    price-not-yet-at-level setups are reintroduced as their own type.)
    """
    return plan.get("setup_type") in SETUP_MODE_TEXT


# ----------------------------------------------------------------------
# PHASE 3 -- STRUCTURAL R:R + SETUP/ENTRY QUALITY
# ----------------------------------------------------------------------
# Everything above (Phase 1 setup detection, Phase 2 regime) is untouched.
# This section is purely additive: it reads the SAME tf_results dict
# (which already carries swing/zone data for every timeframe -- 1D, 4H,
# 1H, 15M -- from analyze_timeframe()) and produces a properly-sourced,
# properly-typed set of targets plus the setup/entry quality split.
#
# Target tiers below are checked in priority order (spec section 2).
# Each tier's candidate, if one exists, is tagged with target_type +
# target_reason + timeframe_source -- nothing here is ever invented; a
# tier with no real detected level simply contributes nothing.

TARGET_TIERS = [
    # (target_type, timeframe_source)
    ("EXTERNAL_LIQUIDITY", "1D"),
    ("HTF_SWING", "4H"),
    ("HTF_STRUCTURE", "4H"),
    ("STRUCTURE_1H", "1H"),
    ("RANGE_BOUNDARY", "1H"),
    ("SUPPORT_RESISTANCE", "1H"),
    ("INTERNAL_LIQUIDITY", ENTRY_TF),
]
# base confidence per tier, used by structural_rr_quality -- reflects how
# much a target sourced from that tier should be trusted vs. a nearby but
# structurally thin level
TIER_BASE_QUALITY = {
    "EXTERNAL_LIQUIDITY": 95,
    "HTF_SWING": 85,
    "HTF_STRUCTURE": 80,
    "STRUCTURE_1H": 70,
    "RANGE_BOUNDARY": 60,
    "SUPPORT_RESISTANCE": 55,
    "INTERNAL_LIQUIDITY": 40,
}


def _nearest_swing_beyond(tf_data, direction, price):
    """Nearest swing-point liquidity beyond price, in the trade's direction."""
    if tf_data is None:
        return None
    prices = tf_data["swing_high_prices"] if direction == "BULLISH" else tf_data["swing_low_prices"]
    beyond = [p for p in prices if (p > price if direction == "BULLISH" else p < price)]
    if not beyond:
        return None
    return min(beyond) if direction == "BULLISH" else max(beyond)


def _nearest_zone_edge_beyond(tf_data, direction, price):
    """Nearest opposing-zone edge beyond price on a given timeframe."""
    if tf_data is None:
        return None
    zones = tf_data["bearish_zones"] if direction == "BULLISH" else tf_data["bullish_zones"]
    edge = "low" if direction == "BULLISH" else "high"
    beyond = [z[edge] for z in zones if (z[edge] > price if direction == "BULLISH" else z[edge] < price)]
    if not beyond:
        return None
    return min(beyond) if direction == "BULLISH" else max(beyond)


def _range_boundary(tf_data, direction, price, lookback=60):
    """
    Only meaningful in a genuinely ranging market: the far side of the
    recent trading range on this timeframe's own data.
    """
    if tf_data is None:
        return None
    df = tf_data["df"]
    window = df.tail(lookback)
    level = float(window["high"].max()) if direction == "BULLISH" else float(window["low"].min())
    if (direction == "BULLISH" and level > price) or (direction == "BEARISH" and level < price):
        return level
    return None


def gather_structural_targets(tf_results, direction, price, regime_alignment):
    """
    Walks TARGET_TIERS in priority order, collecting whichever real
    candidates exist. Returns a list of dicts:
      {price, target_type, target_reason, timeframe_source}
    in TIER PRIORITY order (not yet sorted by distance -- that happens
    separately for the TP1/TP2/TP3 display list).
    """
    found = []
    seen_prices = []

    def add(price_level, ttype, tf_source, reason):
        if price_level is None:
            return
        if any(abs(price_level - p) / max(abs(p), 1e-9) < 0.001 for p in seen_prices):
            return  # de-dupe near-identical levels across tiers
        seen_prices.append(price_level)
        found.append({
            "price": price_level,
            "target_type": ttype,
            "target_reason": reason,
            "timeframe_source": tf_source,
        })

    d1, h4, h1, entry = (tf_results.get("1D"), tf_results.get("4H"),
                          tf_results.get("1H"), tf_results.get(ENTRY_TF))

    add(_nearest_swing_beyond(d1, direction, price), "EXTERNAL_LIQUIDITY", "1D",
        "Nearest opposing swing point on the daily timeframe -- major resting liquidity.")
    add(_nearest_swing_beyond(h4, direction, price), "HTF_SWING", "4H",
        "Nearest opposing swing high/low on the 4H timeframe.")
    add(_nearest_zone_edge_beyond(h4, direction, price), "HTF_STRUCTURE", "4H",
        "Nearest unmitigated opposing OB/FVG on the 4H timeframe.")
    add(_nearest_zone_edge_beyond(h1, direction, price), "STRUCTURE_1H", "1H",
        "Nearest unmitigated opposing OB/FVG on the 1H timeframe.")
    if regime_alignment == "RANGE":
        add(_range_boundary(h1, direction, price), "RANGE_BOUNDARY", "1H",
            "Far side of the current 1H trading range.")
    add(_nearest_swing_beyond(h1, direction, price), "SUPPORT_RESISTANCE", "1H",
        "Nearest opposing swing high/low on the 1H timeframe.")
    if entry is not None:
        add(_nearest_swing_beyond(entry, direction, price), "INTERNAL_LIQUIDITY", ENTRY_TF,
            f"Nearest opposing swing high/low on the {ENTRY_TF} timeframe -- local liquidity only.")
        add(_nearest_zone_edge_beyond(entry, direction, price), "INTERNAL_LIQUIDITY", ENTRY_TF,
            f"Nearest unmitigated opposing OB/FVG on the {ENTRY_TF} timeframe -- local liquidity only.")

    return found


def build_validated_targets(structural_targets, price, risk, max_targets=3):
    """
    Turns the tier-ordered structural_targets into the TP1/TP2/TP3 display
    list, sorted nearest-to-furthest (this is what actually gets shown/
    used for order placement). Only real found levels -- never padded to
    reach 3. Each entry keeps its type/reason/timeframe_source, per spec
    section 3's TP1/TP2/TP3 format, plus 'r' (R-multiple) and 'label' for
    compatibility with the existing print/consumer code.
    """
    by_distance = sorted(structural_targets, key=lambda t: abs(t["price"] - price))
    out = []
    for t in by_distance[:max_targets]:
        r_multiple = abs(t["price"] - price) / risk if risk > 0 else 0
        out.append({
            "price": t["price"],
            "target_type": t["target_type"],
            "target_reason": t["target_reason"],
            "timeframe_source": t["timeframe_source"],
            "r": r_multiple,
            "label": f"{t['target_type'].replace('_', ' ').title()} ({t['timeframe_source']})",
        })
    return out


def pick_primary_structural_target(structural_targets):
    """
    The FIRST tier (in priority order) that has a real candidate --
    NOT the nearest by distance. This is what makes structural_rr honest:
    if the only real target is internal/nearby, structural R:R reflects
    that (poor); if genuine external/HTF liquidity exists further out,
    THAT is used (can legitimately be a large R:R). structural_targets is
    already tier-ordered by gather_structural_targets(), so the first
    entry in tier priority is just the first element whose tier appears
    earliest in TARGET_TIERS.
    """
    if not structural_targets:
        return None
    tier_rank = {t: i for i, (t, _tf) in enumerate(TARGET_TIERS)}
    return min(structural_targets, key=lambda t: tier_rank.get(t["target_type"], 99))


def compute_structural_rr(entry_price, invalidation, primary_target):
    """Mechanical formula (reward/risk), but reward is measured to the
    PRIMARY STRUCTURAL TARGET, not an arbitrary TP multiple."""
    if primary_target is None:
        return 0.0
    risk = abs(entry_price - invalidation)
    if risk <= 0:
        return 0.0
    reward = abs(primary_target["price"] - entry_price)
    return reward / risk


def compute_structural_rr_quality(structural_rr, primary_target):
    """
    0-100 confidence in the structural_rr number itself -- NOT the same
    as the R:R value. A big number sourced from a thin/nearby level scores
    low; a modest number sourced from major HTF liquidity scores high.
    Reflects spec section 8/4: don't reward huge R:R from an unrealistic
    target, and don't punish a small-but-solid R:R from a good tier.
    """
    if primary_target is None:
        return 0
    base = TIER_BASE_QUALITY.get(primary_target["target_type"], 30)
    # a very thin reward (well under 1R) is weak regardless of tier --
    # the target may be "real" but doesn't justify taking the trade
    rr_factor = min(1.0, structural_rr / 1.5) if structural_rr > 0 else 0.0
    return round(base * rr_factor)


def compute_mechanical_rr(entry_price, invalidation, validated_targets):
    """Conventional R:R using the nearest actionable target (TP1) --
    kept as a diagnostic figure per spec section 1, separate from
    structural_rr."""
    if not validated_targets:
        return 0.0
    risk = abs(entry_price - invalidation)
    if risk <= 0:
        return 0.0
    tp1 = validated_targets[0]
    reward = abs(tp1["price"] - entry_price)
    return reward / risk


SETUP_TYPE_BASE_SCORE = {
    "LIQUIDITY_SWEEP_REVERSAL": 18,
    "MOMENTUM_CONTINUATION": 18,
    "BOS_CONTINUATION": 14,
    "TREND_PULLBACK": 12,
}
REGIME_ALIGNMENT_SCORE = {
    "TREND_ALIGNED": 30,
    "REVERSAL": 25,
    "RANGE": 15,
    "COUNTERTREND": 8,
    "CONFLICTED": 5,
}


def compute_setup_quality(setup_type, regime_info, structural_rr, structural_rr_quality,
                           validated_targets):
    """
    0-100: is the underlying trade THESIS good, independent of whether
    NOW is the right moment to enter it. Components (spec section 6):
      - regime/HTF alignment                     up to 30
      - setup clarity (which setup_type, how strong its own evidence is) up to 20
      - structural R:R (capped, diminishing returns past ~4R)          up to 30
      - target availability/quality                                    up to 20
    """
    regime_component = REGIME_ALIGNMENT_SCORE.get(
        regime_info["trend_alignment"] if regime_info else "CONFLICTED", 5)

    setup_component = SETUP_TYPE_BASE_SCORE.get(setup_type, 8)

    rr_component = min(structural_rr, 4.0) / 4.0 * 30

    if not validated_targets:
        target_component = 0
    else:
        # reward having MORE than just an internal-liquidity target, and
        # reward the quality/confidence of the primary structural target
        non_internal = any(t["target_type"] != "INTERNAL_LIQUIDITY" for t in validated_targets)
        target_component = (10 if non_internal else 4) + (structural_rr_quality / 100 * 10)

    return round(min(100, regime_component + setup_component + rr_component + target_component))


EXTENSION_OK_PCT = 40      # extension_ratio below this % of risk -> not extended
EXTENSION_WAIT_PCT = 100   # beyond this % of risk -> meaningfully extended


def compute_extension(entry, direction, setup_type, price, invalidation, last_event):
    """
    How far current price has already run from the setup's own "ideal"
    reference point, measured as a % of the trade's RISK -- so it scales
    sensibly across both tight-stop and wide-stop setups. Returns
    (extension_ratio_pct, extension_status, ideal_reference_price).

    BUG FIX (Phase 4.5 audit): risk must be measured from the IDEAL
    reference price to invalidation -- a FIXED quantity for a given
    setup -- not from the current (drifting) price to invalidation. The
    original version used abs(price - invalidation) as the denominator,
    which grows right alongside the numerator as price runs further away
    (since invalidation stays put): for a typical bullish continuation
    (invalidation below the ideal entry), the ratio algebraically
    approaches but can NEVER reach 100%, making the EXTENDED bucket
    unreachable. Anchoring risk to the ideal price fixes the denominator
    so genuine extension is actually measurable.

    Ideal reference:
      - continuation/reversal types (momentum, BOS, sweep reversal): the
        break/sweep level itself -- that's where the thesis activated.
      - TREND_PULLBACK: the zone/swing level price pulled back to (here
        extension is naturally ~0 since the setup IS "price is at the
        level" by construction).
    """
    if setup_type in ("MOMENTUM_CONTINUATION", "BOS_CONTINUATION", "LIQUIDITY_SWEEP_REVERSAL") and last_event:
        ideal = last_event["price"]
    else:
        ideal = price  # TREND_PULLBACK: by construction, price IS at the reference

    risk = abs(ideal - invalidation)
    if risk <= 0:
        return 0.0, "UNKNOWN", ideal

    extension_dist = abs(price - ideal)
    extension_ratio_pct = extension_dist / risk * 100

    if extension_ratio_pct <= EXTENSION_OK_PCT:
        status = "NOT_EXTENDED"
    elif extension_ratio_pct <= EXTENSION_WAIT_PCT:
        status = "MODERATELY_EXTENDED"
    else:
        status = "EXTENDED"

    return extension_ratio_pct, status, ideal


def compute_entry_quality(extension_ratio_pct, extension_status, validated_targets,
                           price, invalidation, setup_type):
    """
    0-100: is NOW a good time/location to execute the (separately scored)
    setup thesis. Spec section 6/7 -- extension is the dominant factor;
    remaining room to the nearest target and setup freshness are minor
    adjustments.
    """
    # extension: 100 at zero extension, decaying to 0 by ~150% of risk
    extension_score = max(0.0, 100 - extension_ratio_pct * (100 / 150))

    # room left to the nearest real target, as a fraction of risk -- if
    # price has already eaten most of the move to TP1, entering now is
    # chasing a setup that's mostly already played out
    if validated_targets:
        risk = abs(price - invalidation)
        room = abs(validated_targets[0]["price"] - price)
        room_ratio = min(1.0, room / risk) if risk > 0 else 0.0
        room_score = room_ratio * 100
    else:
        room_score = 50  # no detected target at all -- neutral, not penalized twice

    freshness_bonus = 10 if setup_type in ("LIQUIDITY_SWEEP_REVERSAL", "MOMENTUM_CONTINUATION") else 0

    score = extension_score * 0.65 + room_score * 0.25 + freshness_bonus * 0.10 / 10 * 100
    return round(min(100, max(0, score)))


def refine_trade_type(base_trade_type, primary_target, tf_results, direction):
    """
    Spec section 9 sanity check: don't let trade_type be decided purely
    by "the TP happens to be far away." A SWING label should require the
    primary structural target to actually be sourced from 4H/1D
    structure -- otherwise a wide-but-coincidental internal target
    shouldn't be allowed to masquerade as a swing trade.
    """
    if primary_target is None:
        return base_trade_type
    htf_sourced = primary_target["timeframe_source"] in ("1D", "4H")
    if base_trade_type == "SWING" and not htf_sourced:
        return "INTRADAY"  # demote -- the "swing"-sized R:R wasn't actually HTF-backed
    return base_trade_type


SWEEP_REVERSAL_MAX_BAR_GAP = 5   # sweep must be this close (in bars) to the
                                  # fresh CHoCH for it to count as the same event
MOMENTUM_FRESH_BAR_MAX = 1        # break happened this recently -> momentum, not
                                   # a settled continuation


def detect_setup_type(tf_results, direction):
    """
    Setup-type engine (Phase 1 of the scanner redesign).

    Chooses a setup type from price-action structure FIRST -- OB/FVG zones
    are optional supporting confluence layered on afterward, never a
    requirement. Order below matters (first match wins, most specific
    first):

      1. LIQUIDITY_SWEEP_REVERSAL -- a recent sweep beyond a swing point,
         immediately followed by a CHoCH in the new direction.
      2. MOMENTUM_CONTINUATION -- a BoS in this direction on the very
         latest bar(s) -- displacement still happening right now.
      3. BOS_CONTINUATION -- a BoS in this direction, slightly less fresh
         than pure momentum but still recent enough to trade.
      4. TREND_PULLBACK -- no fresh break, but price has pulled back to a
         real structural level (an OB/FVG if one exists there, otherwise
         a prior swing high/low acting as support/resistance).
      5. NO_TRADE -- none of the above lined up; the bot should say so
         rather than force a setup.

    Returns (setup_type, confluence_notes) where confluence_notes is a
    list of short strings describing supporting evidence (OB/FVG presence
    is mentioned here if relevant, but never required to reach a type).
    """
    entry = tf_results.get(ENTRY_TF)
    price = entry["price"]
    last_event = entry.get("last_event")
    sweep = entry.get("recent_sweep")
    zone = entry.get("nearest_zone")
    idx = entry["current_index"]

    dir_word = "bullish" if direction == "BULLISH" else "bearish"
    notes = []

    # 1. Liquidity sweep reversal: sweep + a CHoCH shortly after, same direction
    if (sweep and last_event and last_event["type"] == "CHoCH"
            and last_event["direction"] == dir_word
            and abs(last_event["index"] - sweep["index"]) <= SWEEP_REVERSAL_MAX_BAR_GAP):
        notes.append(f"liquidity swept @ {sweep['price']:.6g} then CHoCH confirmed reversal")
        if zone and zone["direction"] == dir_word:
            notes.append(f"{zone_label(zone)} present as extra confluence (not required)")
        return "LIQUIDITY_SWEEP_REVERSAL", notes

    # 2/3. BoS continuation -- momentum if it just happened, continuation otherwise
    if last_event and last_event["type"] == "BoS" and last_event["direction"] == dir_word:
        bars_since = idx - last_event["index"]
        if bars_since <= MOMENTUM_FRESH_BAR_MAX:
            notes.append(f"BoS {dir_word} confirmed this bar -- active displacement")
            if zone and zone["direction"] == dir_word:
                notes.append(f"{zone_label(zone)} nearby as extra confluence (not required)")
            return "MOMENTUM_CONTINUATION", notes
        else:
            notes.append(f"BoS {dir_word} @ {last_event['price']:.6g}, {bars_since} bars ago")
            if zone and zone["direction"] == dir_word:
                notes.append(f"{zone_label(zone)} nearby as extra confluence (not required)")
            return "BOS_CONTINUATION", notes

    # 4. Trend pullback -- to a zone if one exists here, otherwise to a swing point
    if zone and zone["direction"] == dir_word:
        dist = pct_distance(price, zone)
        if dist <= ZONE_PROXIMITY_PCT * 2:
            notes.append(f"pulled back to {zone_label(zone)} at "
                         f"{zone['low']:.6g}-{zone['high']:.6g}")
            return "TREND_PULLBACK", notes

    swing_prices = entry["swing_low_prices"] if direction == "BULLISH" else entry["swing_high_prices"]
    nearby_swing = None
    for p in swing_prices:
        if pct_distance(price, {"low": p, "high": p}) <= ZONE_PROXIMITY_PCT * 2:
            nearby_swing = p
            break
    if nearby_swing is not None and direction == entry["bias"]:
        notes.append(f"pulled back to prior structural {'low' if direction == 'BULLISH' else 'high'} "
                     f"@ {nearby_swing:.6g} (no OB/FVG here -- pure structure)")
        return "TREND_PULLBACK", notes

    return "NO_TRADE", []


SETUP_MODE_TEXT = {
    "LIQUIDITY_SWEEP_REVERSAL": (
        "READY -- LIQUIDITY SWEEP REVERSAL",
        "Liquidity was swept and price confirmed a structure shift back in this direction."
    ),
    "MOMENTUM_CONTINUATION": (
        "READY -- MOMENTUM CONTINUATION",
        "Fresh break-of-structure happening right now -- active displacement in this direction."
    ),
    "BOS_CONTINUATION": (
        "READY -- BOS CONTINUATION",
        "A break-of-structure confirmed this trend; price is continuing in that direction."
    ),
    "TREND_PULLBACK": (
        "READY -- TREND PULLBACK",
        "Price has pulled back to a real structural level in an already-established direction."
    ),
}


def build_entry_plan(tf_results, direction, regime_info=None, setup_score=None):
    """
    Build a human-readable entry plan. As of Phase 1 of the scanner
    redesign, this NO LONGER requires an OB/FVG zone to exist -- the
    setup type is decided by detect_setup_type() from price-action
    structure first (liquidity sweep reversal, momentum/BoS continuation,
    trend pullback), with OB/FVG treated as optional confluence rather
    than a gatekeeper. Returns None only when detect_setup_type() finds
    genuinely nothing to trade (NO_TRADE) -- this should now be rarer
    than it used to be, since most structurally reasonable setups no
    longer get thrown away for lacking a zone.

    Phase 3 addition: also computes structural (multi-timeframe,
    tier-sourced) targets, mechanical vs structural R:R, setup/entry
    quality, and extension status. All Phase 1/2 keys are unchanged;
    everything Phase 3 adds is new keys on the same dict. `regime_info`
    is optional (defaults to None) so this function remains callable
    exactly as it was before Phase 2/3 for any old caller.
    """
    entry = tf_results.get(ENTRY_TF)
    price = entry["price"]
    zone = entry.get("nearest_zone")

    setup_type, confluence_notes = detect_setup_type(tf_results, direction)
    if setup_type == "NO_TRADE":
        return None

    mode, base_note = SETUP_MODE_TEXT[setup_type]
    note = base_note
    if confluence_notes:
        note += " " + " | ".join(confluence_notes)

    # invalidation: anchor to the zone if this setup happens to be sitting
    # in one, otherwise anchor to structure directly -- either way it's a
    # real swing point, never an arbitrary distance from entry
    zone_here = zone if (zone and zone["direction"] == ("bullish" if direction == "BULLISH" else "bearish")
                          and pct_distance(price, zone) <= ZONE_PROXIMITY_PCT * 2) else None
    if zone_here:
        invalidation = find_structural_invalidation(entry, direction, zone_here)
        zone_low, zone_high, zone_lbl = zone_here["low"], zone_here["high"], zone_label(zone_here)
        invalidation_reason = (f"Structural swing point beyond the {zone_lbl.lower()} at "
                                f"{zone_low:.6g}-{zone_high:.6g} -- if price trades past this, "
                                f"the zone's premise is invalidated.")
    else:
        invalidation = find_structural_invalidation_no_zone(entry, direction, price)
        # no real zone backing this setup -- express the "entry zone" as a
        # tight band around current price rather than faking OB/FVG edges
        band = price * 0.0015
        zone_low, zone_high = (price - band, price + band) if direction == "BULLISH" else (price - band, price + band)
        zone_lbl = "structural level" if setup_type != "TREND_PULLBACK" or not zone else zone_label(zone)
        invalidation_reason = ("Nearest real swing point on the correct side of price -- "
                                "if it breaks, the structure this setup relies on is gone.")

    # legacy Phase-1 target list kept for backward compatibility
    targets = find_tp_targets(entry, direction, price, invalidation)
    trade_type = classify_trade_type(price, invalidation, targets)

    # ---- Phase 3 additions ----
    alignment = regime_info["trend_alignment"] if regime_info else None
    structural_candidates = gather_structural_targets(tf_results, direction, price, alignment)
    risk = abs(price - invalidation)
    validated_targets = build_validated_targets(structural_candidates, price, risk)
    primary_target = pick_primary_structural_target(structural_candidates)

    structural_rr = compute_structural_rr(price, invalidation, primary_target)
    structural_rr_quality = compute_structural_rr_quality(structural_rr, primary_target)
    mechanical_rr = compute_mechanical_rr(price, invalidation, validated_targets)

    setup_quality = compute_setup_quality(setup_type, regime_info, structural_rr,
                                           structural_rr_quality, validated_targets)
    extension_ratio_pct, extension_status, _ideal = compute_extension(
        entry, direction, setup_type, price, invalidation, entry.get("last_event"))
    entry_quality = compute_entry_quality(extension_ratio_pct, extension_status,
                                           validated_targets, price, invalidation, setup_type)

    trade_type = refine_trade_type(trade_type, primary_target, tf_results, direction)

    return {
        "mode": mode,
        "note": note,
        "setup_type": setup_type,
        "zone_low": zone_low,
        "zone_high": zone_high,
        "zone_label": zone_lbl,
        "invalidation": invalidation,
        "targets": targets,
        "direction": direction,
        "setup_score": setup_score,
        "trade_type": trade_type,
        # Phase 3 fields
        "mechanical_rr": mechanical_rr,
        "structural_rr": structural_rr,
        "structural_target": primary_target["price"] if primary_target else None,
        "structural_target_type": primary_target["target_type"] if primary_target else "NONE",
        "structural_target_reason": primary_target["target_reason"] if primary_target else
            "No meaningful structural target found beyond current price.",
        "validated_targets": validated_targets,
        "setup_quality": setup_quality,
        "entry_quality": entry_quality,
        "structural_rr_quality": structural_rr_quality,
        "invalidation_level": invalidation,
        "invalidation_reason": invalidation_reason,
        "invalidation_timeframe": ENTRY_TF,
        "extension_status": extension_status,
        "extension_ratio_pct": round(extension_ratio_pct, 1),
    }


# ============================================================================
# PHASE 4 -- EXECUTION STATE MACHINE + CONTINUOUS SCAN LIFECYCLE
# ============================================================================
# Everything above (Phase 1 setup detection, Phase 2 regime, Phase 3
# structural R:R / quality split) is untouched. This section turns a
# technically valid plan into an honest execution state, and gives each
# setup a stable identity across repeated scans so the Telegram bot isn't
# spammed every 15 minutes with the same unchanged idea.
#
# NOTE on invalidation_timeframe (spec item 14): the existing invalidation
# functions (find_structural_invalidation / find_structural_invalidation_no_zone)
# only ever read entry-timeframe (15M) swing data -- there is no HTF-aware
# invalidation logic anywhere in this codebase yet. Extending that is a
# real change to Phase 1's invalidation functions, not something Phase 4
# can honestly bolt on without touching them (which the brief says not to
# do). So invalidation_timeframe is preserved as ENTRY_TF exactly as
# Phase 3 reported it -- this section does NOT fabricate an HTF source.

import hashlib

EXECUTION_STATES = {
    "READY_MARKET", "READY_LIMIT", "NEAR_READY",
    "WAIT_PULLBACK", "WAIT_BREAKOUT", "WAIT_RETEST",
    "INVALID", "NO_TRADE",
}

LIFECYCLE_STATES = [
    "DETECTED", "DEVELOPING", "NEAR_READY", "READY",
    "ACTIVE", "TP_EXIT", "INVALIDATED", "EXPIRED",
]

_EXEC_STATE_TO_LIFECYCLE = {
    "READY_MARKET": "READY", "READY_LIMIT": "READY",
    "NEAR_READY": "NEAR_READY",
    "WAIT_PULLBACK": "DEVELOPING", "WAIT_BREAKOUT": "DEVELOPING", "WAIT_RETEST": "DEVELOPING",
    "INVALID": "INVALIDATED", "NO_TRADE": None,   # NO_TRADE isn't tracked as a lifecycle at all
}


def compute_atr(df, period=ATR_PERIOD):
    """
    Standard Average True Range on the entry timeframe. Used to normalize
    'how far is this limit entry from current price' across assets of
    wildly different volatility (spec section 3/13 -- no fixed % allowed).
    """
    if df is None or len(df) < 2:
        return None
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr_series = tr.rolling(period).mean()
    val = atr_series.iloc[-1]
    return float(val) if pd.notna(val) else None


def determine_execution_state(plan, tf_results, direction):
    """
    Core Phase 4 classifier. Takes a Phase-1/2/3 plan (already has
    setup_quality, entry_quality, structural_rr, extension_status,
    invalidation, targets, etc.) and returns the honest execution state
    plus every field spec section 21 requires for a READY signal.

    PRECEDENCE (audited, fixed in Phase 4.5 -- see report):
      1. INVALIDATION breach                (always wins, no exceptions)
      2. NO_TRADE quality/structural-R:R gates
      3. BREAKOUT / RETEST conditions
      4. Entry-location calculation
      5. READY_MARKET / READY_LIMIT / NEAR_READY / WAIT_PULLBACK

    Returns a dict merged onto the plan (caller does plan.update(...)),
    never mutates plan itself.
    """
    entry = tf_results.get(ENTRY_TF)
    price = entry["price"]
    invalidation = plan["invalidation"]
    setup_type = plan["setup_type"]
    setup_quality = plan["setup_quality"]
    entry_quality = plan["entry_quality"]
    structural_rr = plan["structural_rr"]
    extension_status = plan["extension_status"]

    atr = compute_atr(entry.get("df"))
    risk = abs(price - invalidation)
    stop_distance_pct = risk / price * 100 if price else 0
    stop_distance_atr = risk / atr if atr else None

    out = {
        "current_price": price,
        "stop_distance_pct": round(stop_distance_pct, 3),
        "stop_distance_atr": round(stop_distance_atr, 2) if stop_distance_atr is not None else None,
        "breakout_level": None,
        "required_confirmation": None,
        "trade_horizon": plan["trade_type"],
        "scalp_like": plan["trade_type"] == "SCALP",
    }

    # ---- 1. INVALIDATION -- absolute top priority, overrides EVERYTHING ----
    # else below, including a pending breakout/retest flag. Checked first,
    # unconditionally, before any other gate.
    breached = (direction == "BULLISH" and price <= invalidation) or \
               (direction == "BEARISH" and price >= invalidation)
    if breached:
        out.update({
            "status": "INVALID",
            "execution_type": None,
            "invalidation_reason": plan["invalidation_reason"] + " -- LEVEL HAS BEEN BREACHED.",
        })
        return out

    # ---- 2. NO_TRADE quality/R:R gates -- prefer NO_TRADE over forcing ----
    # a mediocre trade; no automatic exceptions
    setup_score = plan.get("setup_score")
    if setup_score is not None and setup_score < MIN_TRADE_SCORE:
        out.update({"status": "NO_TRADE", "execution_type": None,
                    "no_trade_reason": f"Setup score {setup_score:.0f} is below trade threshold {MIN_TRADE_SCORE}."})
        return out

    if setup_quality < MIN_SETUP_QUALITY or structural_rr < MIN_STRUCTURAL_RR:
        out.update({"status": "NO_TRADE", "execution_type": None})
        return out

    # ---- 3. BREAKOUT / RETEST conditions ----
    # Only meaningful if the plan carries these hints -- none of the
    # Phase 1 setup types currently set them (all four are "already
    # confirmed" event types), but the state machine honors them fully
    # if a future setup type (e.g. plain BREAKOUT, pending) supplies them,
    # so this isn't dead code -- it's forward-compatible plumbing.
    if plan.get("breakout_confirmed") is False:
        out.update({
            "status": "WAIT_BREAKOUT",
            "execution_type": None,
            "breakout_level": plan.get("breakout_level"),
            "required_confirmation": plan.get("required_confirmation")
                or f"Price must close beyond {plan.get('breakout_level')}.",
        })
        return out
    if plan.get("breakout_confirmed") is True and plan.get("retest_confirmed") is False:
        out.update({
            "status": "WAIT_RETEST",
            "execution_type": None,
            "required_confirmation": "Price must retest the broken level before entry.",
        })
        return out

    # ---- 4. Entry-location calculation ----
    if setup_type in ("MOMENTUM_CONTINUATION", "BOS_CONTINUATION", "LIQUIDITY_SWEEP_REVERSAL"):
        preferred_entry = entry.get("last_event", {}).get("price", price) if entry.get("last_event") else price
    else:  # TREND_PULLBACK -- the zone itself is the reference
        preferred_entry = (plan["zone_low"] + plan["zone_high"]) / 2

    distance = abs(price - preferred_entry)
    distance_to_entry_pct = distance / price * 100 if price else 0
    distance_to_entry_atr = distance / atr if atr else None
    out["preferred_entry"] = preferred_entry
    out["entry_zone"] = (plan["zone_low"], plan["zone_high"])
    out["distance_to_entry_pct"] = round(distance_to_entry_pct, 3)
    out["distance_to_entry_atr"] = round(distance_to_entry_atr, 2) if distance_to_entry_atr is not None else None

    # ---- 5. READY_MARKET / READY_LIMIT / NEAR_READY / WAIT_PULLBACK ----
    ready_gate = (setup_quality >= MIN_READY_SETUP_QUALITY
                  and entry_quality >= MIN_READY_ENTRY_QUALITY
                  and extension_status != "EXTENDED")

    if ready_gate:
        essentially_at_price = (distance_to_entry_atr is not None and distance_to_entry_atr < 0.15) or \
                                distance_to_entry_pct < 0.05
        # BUG FIX (Phase 4.5 audit): extension_status (risk-normalized) and
        # distance_to_entry_atr (volatility-normalized) use different
        # denominators and can disagree on the same absolute distance --
        # a tight-stop setup can look "moderately extended" by risk terms
        # while looking "basically at price" in ATR terms. READY_MARKET
        # must require BOTH measures agree the setup is not extended;
        # otherwise it silently violates "READY_MARKET cannot occur when
        # price is excessively extended" (spec section 12). A disagreement
        # here still allows READY_LIMIT (a real execution zone exists),
        # just not a blind market order.
        if extension_status == "NOT_EXTENDED" and essentially_at_price:
            out.update({"status": "READY_MARKET", "execution_type": "MARKET", "entry": price})
        elif distance_to_entry_atr is not None and distance_to_entry_atr <= MAX_LIMIT_DISTANCE_ATR:
            out.update({"status": "READY_LIMIT", "execution_type": "LIMIT", "entry": preferred_entry})
        else:
            # structurally justified but too far in volatility terms --
            # spec section 13: NOT READY_LIMIT, becomes NEAR_READY or WAIT
            if extension_status == "MODERATELY_EXTENDED":
                out.update({"status": "WAIT_PULLBACK", "execution_type": None})
            else:
                out.update({"status": "NEAR_READY", "execution_type": None})
        return out

    # not at the READY bar -- NEAR_READY vs WAIT_PULLBACK based on entry_quality
    if entry_quality >= NEAR_READY_ENTRY_QUALITY:
        out.update({"status": "NEAR_READY", "execution_type": None})
    else:
        out.update({"status": "WAIT_PULLBACK", "execution_type": None})
    return out


# ----------------------------------------------------------------------
# Setup identity + lifecycle + duplicate-alert suppression
# ----------------------------------------------------------------------

def _bucket(value, bucket_pct=SETUP_FINGERPRINT_BUCKET_PCT):
    """
    Rounds a price to a coarse bucket so tiny noise between scans doesn't
    change a setup's identity. None-safe.

    BUG FIX (Phase 4.5 audit): the step size must be tied to the value's
    ORDER OF MAGNITUDE, not to the exact value itself -- using the exact
    value to compute its own step meant two prices a fraction of a
    percent apart (e.g. 115.00 vs 115.02) could still land in different
    buckets, because each computed a slightly different step size from
    itself. Freezing the step to the order of magnitude makes the grid
    stable across ordinary price noise.
    """
    if value is None:
        return None
    if value == 0:
        return 0.0
    magnitude = 10 ** math.floor(math.log10(abs(value)))
    step = magnitude * bucket_pct / 100
    if step == 0:
        return round(value, 6)
    return round(value / step) * step


def compute_setup_fingerprint(symbol, direction, setup_type, structural_target, invalidation):
    """
    Stable identity for a setup across repeated scans. Deliberately
    EXCLUDES current price (spec section 17) -- only the things that
    define the underlying thesis: symbol, direction, setup_type, the
    major structural level it's aiming at, and its invalidation.
    """
    raw = f"{symbol}|{direction}|{setup_type}|{_bucket(structural_target)}|{_bucket(invalidation)}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def load_setup_states():
    if not os.path.exists(SETUP_STATE_FILE):
        return {}
    try:
        with open(SETUP_STATE_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_setup_states(states):
    try:
        with open(SETUP_STATE_FILE, "w") as f:
            json.dump(states, f, indent=2)
    except OSError:
        pass


def detect_material_change(old_snapshot, new_snapshot):
    """
    Compares two consecutive snapshots of the SAME setup (same
    fingerprint) and decides whether the change is big enough to warrant
    a new Telegram alert (spec section 19). Returns (changed, reasons).
    """
    reasons = []

    if old_snapshot.get("status") != new_snapshot.get("status"):
        reasons.append(f"status {old_snapshot.get('status')} -> {new_snapshot.get('status')}")

    if old_snapshot.get("execution_type") != new_snapshot.get("execution_type"):
        reasons.append(f"execution_type {old_snapshot.get('execution_type')} -> {new_snapshot.get('execution_type')}")

    if old_snapshot.get("setup_type") != new_snapshot.get("setup_type"):
        reasons.append("setup_type changed")

    if old_snapshot.get("regime") != new_snapshot.get("regime"):
        reasons.append(f"regime {old_snapshot.get('regime')} -> {new_snapshot.get('regime')}")

    if old_snapshot.get("trend_alignment") != new_snapshot.get("trend_alignment"):
        reasons.append("trend_alignment changed")

    def pct_shift(old_v, new_v):
        if old_v is None or new_v is None or old_v == 0:
            return None
        return abs(new_v - old_v) / abs(old_v) * 100

    for field in ("preferred_entry", "invalidation_level", "structural_target"):
        shift = pct_shift(old_snapshot.get(field), new_snapshot.get(field))
        if shift is not None and shift > MATERIAL_CHANGE_PRICE_TOL_PCT:
            reasons.append(f"{field} shifted {shift:.2f}%")

    old_rr, new_rr = old_snapshot.get("structural_rr"), new_snapshot.get("structural_rr")
    if old_rr is not None and new_rr is not None and abs(new_rr - old_rr) > MATERIAL_CHANGE_RR_TOL:
        reasons.append(f"structural_rr {old_rr:.2f} -> {new_rr:.2f}")

    old_eq, new_eq = old_snapshot.get("entry_quality"), new_snapshot.get("entry_quality")
    if old_eq is not None and new_eq is not None:
        crossed_up = old_eq < MIN_READY_ENTRY_QUALITY <= new_eq
        crossed_down = old_eq >= MIN_READY_ENTRY_QUALITY > new_eq
        if crossed_up or crossed_down:
            reasons.append(f"entry_quality crossed the READY threshold ({old_eq} -> {new_eq})")

    return (len(reasons) > 0), reasons


def update_setup_lifecycle(symbol, plan, scan_cycle=None):
    """
    Call once per symbol per scan, AFTER determine_execution_state() has
    populated plan with a status. Maintains SETUP_STATE_FILE across scans
    and decides whether this scan should produce a new Telegram alert.

    Returns dict: {send_alert, lifecycle, reason, is_new, fingerprint}
    """
    states = load_setup_states()
    direction = plan["direction"]
    setup_type = plan["setup_type"]
    fingerprint = compute_setup_fingerprint(
        symbol, direction, setup_type, plan.get("structural_target"), plan["invalidation_level"])

    status = plan["status"]
    lifecycle = _EXEC_STATE_TO_LIFECYCLE.get(status)

    snapshot = {
        "fingerprint": fingerprint,
        "status": status,
        "execution_type": plan.get("execution_type"),
        "setup_type": setup_type,
        "direction": direction,
        "regime": plan.get("regime"),
        "trend_alignment": plan.get("trend_alignment"),
        "preferred_entry": plan.get("preferred_entry"),
        "invalidation_level": plan.get("invalidation_level"),
        "structural_target": plan.get("structural_target"),
        "structural_rr": plan.get("structural_rr"),
        "entry_quality": plan.get("entry_quality"),
        "setup_quality": plan.get("setup_quality"),
        "lifecycle": lifecycle,
        "stale_cycles": 0,
        "last_seen_cycle": scan_cycle,
    }

    if status == "NO_TRADE":
        # nothing to track -- if this symbol had a prior tracked setup
        # that's now gone entirely (not just changed), that's an implicit
        # expiration, but we don't invent a new alert for "nothing found"
        if symbol in states:
            del states[symbol]
            save_setup_states(states)
        return {"send_alert": False, "lifecycle": None, "reason": "no setup", "is_new": False,
                "fingerprint": fingerprint}

    old = states.get(symbol)

    if old is None:
        states[symbol] = snapshot
        save_setup_states(states)
        return {"send_alert": True, "lifecycle": lifecycle, "reason": "new setup detected",
                "is_new": True, "fingerprint": fingerprint}

    # BUG FIX (Phase 4.5 audit): the coarse fingerprint bucket can flip on
    # perfectly ordinary noise for a value sitting near a grid boundary
    # (e.g. 825.0 -> 825.3, a 0.036% move). Using fingerprint EQUALITY as
    # the sole gate for "is this a different setup" let that quantization
    # artifact skip detect_material_change() entirely and fire a false
    # alert on noise -- exactly the failure duplicate-suppression exists
    # to prevent. Fix: only a change in the CATEGORICAL identity
    # (direction or setup_type -- things that don't have a "how close is
    # close enough" question) counts as an unconditional different setup.
    # Every continuous field (entry, invalidation, target, R:R, quality)
    # is always decided by detect_material_change()'s percentage-based
    # tolerance, never by fingerprint bucket boundaries.
    old_lineage_changed = (old.get("setup_type") != setup_type) or (old.get("direction") != direction)
    if old_lineage_changed:
        states[symbol] = snapshot
        save_setup_states(states)
        return {"send_alert": True, "lifecycle": lifecycle,
                "reason": "different setup (direction or setup_type changed)",
                "is_new": True, "fingerprint": fingerprint}

    # same underlying setup lineage -- check for material change using the
    # continuous, percentage-based comparison (immune to bucket boundaries)
    changed, reasons = detect_material_change(old, snapshot)
    if changed:
        snapshot["stale_cycles"] = 0
        states[symbol] = snapshot
        save_setup_states(states)
        return {"send_alert": True, "lifecycle": lifecycle, "reason": "; ".join(reasons),
                "is_new": False, "fingerprint": fingerprint}

    # unchanged -- bump staleness, check expiration, suppress the alert
    stale_cycles = old.get("stale_cycles", 0) + 1
    snapshot["stale_cycles"] = stale_cycles
    expire_limit = READY_STALE_EXPIRE_CYCLES if lifecycle == "READY" else STALE_EXPIRE_CYCLES
    if stale_cycles >= expire_limit:
        del states[symbol]
        save_setup_states(states)
        return {"send_alert": True, "lifecycle": "EXPIRED", "reason": "stale for too many scan cycles",
                "is_new": False, "fingerprint": fingerprint}

    states[symbol] = snapshot
    save_setup_states(states)
    return {"send_alert": False, "lifecycle": lifecycle, "reason": "existing setup -- unchanged",
            "is_new": False, "fingerprint": fingerprint}


# ----------------------------------------------------------------------
# Ranking (spec section 23) -- only actionable candidates get ranked;
# developing setups are preserved separately for backtesting visibility.
# ----------------------------------------------------------------------

def classify_and_rank(plans):
    """
    plans: list of plan dicts that already have 'status' set by
    determine_execution_state(). Buckets into the 5 final scan
    categories (spec section 24) and ranks READY_NOW by a composite of
    setup_quality, entry_quality, and structural_rr.
    """
    buckets = {"READY_NOW": [], "NEAR_READY": [], "WAITING": [], "INVALIDATED": [], "NO_TRADE": []}
    for p in plans:
        status = p.get("status")
        if status in ("READY_MARKET", "READY_LIMIT"):
            buckets["READY_NOW"].append(p)
        elif status == "NEAR_READY":
            buckets["NEAR_READY"].append(p)
        elif status in ("WAIT_PULLBACK", "WAIT_BREAKOUT", "WAIT_RETEST"):
            buckets["WAITING"].append(p)
        elif status == "INVALID":
            buckets["INVALIDATED"].append(p)
        else:
            buckets["NO_TRADE"].append(p)

    def composite(p):
        return (p.get("setup_quality", 0) * 0.4 + p.get("entry_quality", 0) * 0.35
                + min(p.get("structural_rr", 0), 5) / 5 * 100 * 0.25)

    buckets["READY_NOW"].sort(key=composite, reverse=True)
    buckets["NEAR_READY"].sort(key=composite, reverse=True)
    return buckets


# ============================================================================
# WATCHLIST v2 (persisted JSON) -- 4 statuses, live rescoring, trail/fixed stop
# ============================================================================

def load_watchlist():
    if not os.path.exists(WATCHLIST_FILE):
        return []
    try:
        with open(WATCHLIST_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return []


def save_watchlist(items):
    try:
        with open(WATCHLIST_FILE, "w") as f:
            json.dump(items, f, indent=2)
    except Exception as e:
        print(f"(!) Couldn't save watchlist: {e}")


def prompt_trail_mode():
    """Ask the user how the stop should behave once this setup confirms."""
    print("\nHow should the stop-loss behave once this setup triggers?")
    print("  1) Fixed -- stop never moves once set")
    print("  2) Trail (structural) -- stop tightens as new structure forms, never loosens")
    print(f"  3) Trail (breakeven first) -- stays fixed, jumps to entry at {TRAIL_TRIGGER_R}R, "
          f"then trails structurally")
    choice = input("Choice [1/2/3, default 1]: ").strip()
    return {"1": "fixed", "2": "trail_structural", "3": "trail_breakeven"}.get(choice, "fixed")


def prompt_split_entries(plan, direction):
    """Optionally split the entry into multiple scaled fill levels."""
    ans = input("Split this into multiple entries? (y/N): ").strip().lower()
    if ans != "y":
        mid = (plan["zone_low"] + plan["zone_high"]) / 2
        return [{"price": mid, "size_pct": 100, "filled": False}]

    legs = []
    total = 0
    print(f"Zone range: {plan['zone_low']:.6g} - {plan['zone_high']:.6g}")
    while total < 100:
        try:
            price = float(input(f"  Entry price for leg {len(legs) + 1}: ").strip())
            size = float(input(f"  Size %% for this leg (remaining {100 - total}%%): ").strip())
        except ValueError:
            print("  Invalid number, skipping this leg.")
            continue
        legs.append({"price": price, "size_pct": size, "filled": False})
        total += size
    return legs if legs else [{"price": (plan["zone_low"] + plan["zone_high"]) / 2,
                                 "size_pct": 100, "filled": False}]


def _setup_plan_fingerprint(symbol, direction, plan):
    return compute_setup_fingerprint(symbol, direction, plan.get("setup_type"),
                                      plan.get("structural_target"),
                                      plan.get("invalidation_level", plan.get("invalidation")))


def _hours_from_now(iso_ts):
    if not iso_ts:
        return 1e9
    try:
        then = datetime.fromisoformat(iso_ts)
        return max(0.0, (datetime.now(timezone.utc) - then).total_seconds() / 3600.0)
    except Exception:
        return 1e9


def reentry_gate(symbol, direction, plan):
    """Return (allowed, reason) for a fresh actionable signal."""
    items = load_watchlist()
    new_fp = _setup_plan_fingerprint(symbol, direction, plan)
    prior = [it for it in items if it.get("symbol") == symbol and it.get("direction") == direction
             and it.get("status") in ("invalidated", "expired")]
    if not prior:
        return True, "no prior terminal attempt"
    last = max(prior, key=lambda x: x.get("invalidated_at") or x.get("expired_at") or "")
    when = last.get("invalidated_at") or last.get("expired_at")
    age_h = _hours_from_now(when)
    if last.get("status") == "invalidated" and age_h < REENTRY_COOLDOWN_HOURS:
        return False, f"previous {direction} attempt stopped {age_h:.1f}h ago (cooldown {REENTRY_COOLDOWN_HOURS}h)"
    if last.get("setup_fingerprint") and last.get("setup_fingerprint") == new_fp:
        return False, "same structural setup fingerprint as the last failed attempt"
    return True, f"new structural attempt after previous {last.get('status')}"


def add_to_watchlist(symbol, exchange_key, score, direction, plan,
                      trail_mode="fixed", split_entries=None):
    items = load_watchlist()
    new_fingerprint = _setup_plan_fingerprint(symbol, direction, plan)

    for it in items:
        if it["symbol"] == symbol and it["direction"] == direction and it["status"] in ("pending", "triggered"):
            print(f"  ({symbol} already on watchlist as {it['status']})")
            return False

    prior_attempts = [it for it in items if it["symbol"] == symbol and it["direction"] == direction
                       and it["status"] in ("invalidated", "expired")]
    attempt_num = len(prior_attempts) + 1
    lineage_note = None
    if prior_attempts:
        last = max(prior_attempts, key=lambda x: x.get("invalidated_at") or x.get("expired_at") or "")
        last_fp = last.get("setup_fingerprint")
        age_h = _hours_from_now(last.get("invalidated_at") or last.get("expired_at"))
        if last.get("status") == "invalidated" and age_h < REENTRY_COOLDOWN_HOURS:
            print(f"  (! {symbol} re-entry BLOCKED -- previous {direction} attempt was stopped "
                  f"{age_h:.1f}h ago; cooldown={REENTRY_COOLDOWN_HOURS}h)")
            return False
        if last_fp and last_fp == new_fingerprint:
            print(f"  (! {symbol} re-entry BLOCKED -- same setup fingerprint as the last "
                  f"failed/expired attempt; waiting for genuinely new structure)")
            return False
        reason = last.get("expire_reason") or "stop breached"
        when = last.get("invalidated_at") or last.get("expired_at")
        lineage_note = (f"Attempt #{attempt_num} for {symbol} {direction} -- NEW setup after a prior "
                        f"{last['status']} attempt ({when}: {reason}).")
        print(f"  (note: {lineage_note})")

    if trail_mode not in VALID_TRAIL_MODES:
        trail_mode = "fixed"
    if split_entries is None:
        mid = (plan["zone_low"] + plan["zone_high"]) / 2
        split_entries = [{"price": mid, "size_pct": 100, "filled": False}]

    items.append({
        "symbol": symbol, "exchange": exchange_key, "direction": direction,
        "status": "pending", "added_score": score, "current_score": score,
        "trade_type": plan.get("trade_type", "INTRADAY"), "attempt_num": attempt_num,
        "lineage_note": lineage_note, "setup_fingerprint": new_fingerprint,
        "added_at": datetime.now(timezone.utc).isoformat(), "triggered_at": None,
        "invalidated_at": None, "expired_at": None, "expire_reason": None,
        "zone_low": plan["zone_low"], "zone_high": plan["zone_high"],
        "zone_label": plan["zone_label"], "entries": split_entries,
        "invalidation": plan["invalidation"], "original_invalidation": plan["invalidation"],
        "targets": plan.get("targets", []), "trail_mode": trail_mode,
        "last_note": "Waiting for price to reach entry zone.", "entry_instruction": None,
        "entry_price": None, "max_r": 0.0, "max_favorable_price": None,
        "peak_score": score, "last_warning": None,
        "reversal_state": "STABLE", "reversal_reason": "No active reversal evidence detected.",
        "reversal_alert_state": "STABLE", "current_r": 0.0,
        "history": [],
    })
    save_watchlist(items)
    print(f"  + Added {symbol} ({direction}) to watchlist. Stop mode: {trail_mode}.")
    return True


def remove_from_watchlist(index):
    items = load_watchlist()
    if 0 <= index < len(items):
        removed = items.pop(index)
        save_watchlist(items)
        print(f"Removed: {removed['symbol']} ({removed['direction']})")
    else:
        print("Invalid watchlist number.")


def _log(entry, msg):
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    entry["history"].append(f"[{stamp}] {msg}")
    entry["last_note"] = msg


def _notify_status_change(entry, prev_status):
    """Fire a Telegram message on a real status transition. Silent no-op
    if Telegram isn't configured -- never blocks or crashes the refresh."""
    if not TELEGRAM_ENABLED:
        return

    symbol, direction, new_status = entry["symbol"], entry["direction"], entry["status"]
    trade_type = entry.get("trade_type", "INTRADAY")

    if new_status == "triggered":
        lines = [
            f"\U0001F7E2 POSITION ACTIVE: {symbol} {direction} [{trade_type}]",
            f"Position Health: {entry['current_score']:.0f}/100",
            "Entry already triggered -- ACTIVE position, not a new entry.",
            "DO NOT re-enter from this alert.",
            f"Filled: {sum(float(x.get('size_pct', 0)) for x in entry.get('entries', []) if x.get('filled')):.0f}% of planned allocation.",
        ]
        if entry.get("targets"):
            tp_str = " | ".join(f"TP{i+1} {t['price']:.6g} (~{t['r']:.2f}R)"
                                 for i, t in enumerate(entry["targets"]))
            lines.append(f"Targets: {tp_str}")
        lines.append(f"Invalidation: {entry['invalidation']:.6g}")

    elif new_status == "invalidated":
        lines = [
            f"\U0001F534 INVALIDATED: {symbol} {direction} [{trade_type}]",
            entry.get("last_note", ""),
        ]

    elif new_status == "expired":
        lines = [
            f"\u26AA EXPIRED: {symbol} {direction} [{trade_type}]",
            entry.get("expire_reason", entry.get("last_note", "")),
        ]
    else:
        return  # no notification for other transitions

    send_telegram_message("\n".join(str(l) for l in lines if l))


# Minimum |% change| in confirmed score before a per-cycle update is sent for
# an already-triggered entry. 0 = notify every single refresh (every 15 min
# per entry) as requested. Raise this (e.g. to 10) later if it gets noisy
# with several triggered entries running at once.
SCORE_UPDATE_MIN_PCT = 0


def _notify_score_update(entry):
    """Send an active-position health update, never a new-entry signal."""
    if not TELEGRAM_ENABLED:
        return
    pct = entry.get("last_score_delta_pct", 0.0)
    if abs(pct) < SCORE_UPDATE_MIN_PCT:
        return
    arrow_emoji = {"up": "🔼", "down": "🔽", "flat": "➡️"}.get(entry.get("last_score_arrow", "flat"), "➡️")
    symbol, direction = entry["symbol"], entry["direction"]
    trade_type = entry.get("trade_type", "INTRADAY")
    state = entry.get("reversal_state", "STABLE")
    lines = [
        f"{arrow_emoji} ACTIVE POSITION UPDATE: {symbol} {direction} [{trade_type}]",
        f"Position Health: {entry['current_score']:.0f}/100 ({pct:+.1f}% vs previous check)",
        f"State: {state}",
        "Already triggered/active -- NOT a new entry signal.",
        "DO NOT re-enter from this alert.",
        f"Current R: {entry.get('current_r', 0.0):+.2f}R | Max R: {entry.get('max_r', 0.0):+.2f}R",
        f"SL: {entry['invalidation']:.6g}",
    ]
    if entry.get("reversal_reason"):
        lines.append(f"Why: {entry['reversal_reason']}")
    if entry.get("targets"):
        tp_str = " | ".join(f"TP{i+1} {t['price']:.6g} (~{t['r']:.2f}R)" for i, t in enumerate(entry["targets"]))
        lines.append(f"TP: {tp_str}")
    send_telegram_message("\n".join(str(l) for l in lines if l))


def _position_reversal_diagnostic(tf_results, direction):
    """Distinguish normal pullback, liquidity event, deterioration and confirmed reversal."""
    opposite = "bearish" if direction == "BULLISH" else "bullish"
    entry = tf_results.get(ENTRY_TF)
    if not entry:
        return "STABLE", "Insufficient entry-timeframe data.", 0
    sweep_hits, opposite_breaks, opposite_bias = [], [], []
    for tf in ("15M", "1H", "4H"):
        r = tf_results.get(tf)
        if not r:
            continue
        sweep = r.get("recent_sweep")
        if sweep and sweep.get("direction") == opposite:
            age = r.get("current_index", 0) - sweep.get("index", 0)
            if age <= 5:
                sweep_hits.append(tf)
        ev = r.get("last_event")
        if r.get("fresh_break") and ev and ev.get("direction") == opposite:
            opposite_breaks.append(tf)
        if r.get("bias") == opposite:
            opposite_bias.append(tf)
    if any(tf in ("1H", "4H") for tf in opposite_breaks) and sweep_hits:
        return "CONFIRMED_REVERSAL", f"Opposite liquidity sweep + fresh {opposite} structure break on {','.join(opposite_breaks)}.", 3
    if sweep_hits:
        return "LIQUIDITY_SWEEP", f"{opposite.title()} liquidity sweep detected on {','.join(sweep_hits)}; rejection exists but structure has not confirmed reversal.", 2
    df = entry.get("df")
    if df is not None and len(df) >= 15:
        close = df["close"]
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / loss.replace(0, np.nan)
        rsi = float((100 - (100 / (1 + rs))).iloc[-1]) if pd.notna(rs.iloc[-1]) else None
        closes = df["close"].tail(3).tolist()
        counter = sum(1 for a, b in zip(closes, closes[1:]) if (b < a if direction == "BULLISH" else b > a))
        if counter >= 2 and not opposite_breaks:
            if rsi is not None and ((direction == "BULLISH" and rsi >= 70) or (direction == "BEARISH" and rsi <= 30)):
                return "NORMAL_PULLBACK", f"Momentum is cooling after an extreme RSI reading ({rsi:.0f}); no confirmed opposite structure break. This is a pullback warning, not a reversal confirmation.", 1
            return "NORMAL_PULLBACK", "Momentum is cooling with recent counter-direction candles, but no confirmed opposite structure break.", 1
    if opposite_bias:
        return "MOMENTUM_DETERIORATION", f"Bias is weakening on {','.join(opposite_bias)} without a confirmed reversal structure break.", 1
    return "STABLE", "Original structure remains intact; no active reversal evidence detected.", 0


def _hours_since(iso_ts):
    then = datetime.fromisoformat(iso_ts)
    return (datetime.now(timezone.utc) - then).total_seconds() / 3600.0


def _r_multiple(direction, price, entry_price, invalidation):
    risk = abs(entry_price - invalidation)
    if risk <= 0:
        return 0.0
    if direction == "BULLISH":
        return (price - entry_price) / risk
    return (entry_price - price) / risk


def _score_confirmed(tf_results, direction, r_now):
    """
    Uncapped rescoring for an already-triggered setup. Rebuilds the same
    components score_setup() uses, but WITHOUT the min(score, 100) clamp,
    and adds a momentum/profit bonus -- so a setup that's working and still
    HTF-aligned can legitimately read well above 100.
    """
    entry = tf_results.get(ENTRY_TF)
    if entry is None:
        return 0.0

    live_direction = entry["bias"]
    score = 0

    agree = sum(1 for tf in HTF_TFS if tf_results.get(tf) and tf_results[tf]["bias"] == direction)
    score += agree * 20

    zone = entry.get("nearest_zone")
    if zone:
        dist = pct_distance(entry["price"], zone)
        matches = zone["direction"] == ("bullish" if direction == "BULLISH" else "bearish")
        if matches and dist <= ZONE_PROXIMITY_PCT:
            score += 30
        elif matches and dist <= ZONE_PROXIMITY_PCT * 2:
            score += 15

    if entry.get("fresh_break"):
        score += 12
    for tf in HTF_TFS:
        r = tf_results.get(tf)
        if r and r.get("fresh_break") and r["bias"] == direction:
            score += 8
            break

    sweep = entry.get("recent_sweep")
    if sweep and sweep["direction"] == ("bullish" if direction == "BULLISH" else "bearish"):
        score += 10

    if r_now > 0:
        score += min(r_now * 15, 45)     # up to +45 for being deep in profit
    if live_direction == direction:
        score += 10                       # structure still agrees with the original call

    return round(min(100.0, max(0.0, score)), 1)


def _build_entry_instruction(entry, price, direction):
    """
    Explicit, actionable guidance for a triggered entry: which legs are
    filled, which still need a limit order, and whether to act at CMP or
    wait for a specific level. This is what actually answers "how do I
    enter" -- the score/stop notes alone don't.
    """
    legs = entry["entries"]
    unfilled = [leg for leg in legs if not leg["filled"]]
    filled = [leg for leg in legs if leg["filled"]]

    lines = []
    if filled:
        filled_desc = ", ".join(f"{leg['size_pct']:.0f}% @ {leg['price']:.6g}" for leg in filled)
        lines.append(f"Filled: {filled_desc}.")

    if unfilled:
        for leg in unfilled:
            still_reachable = (direction == "BULLISH" and price >= leg["price"]) or \
                               (direction == "BEARISH" and price <= leg["price"])
            if still_reachable:
                lines.append(f"Remaining {leg['size_pct']:.0f}% -- place/keep a limit order at "
                              f"{leg['price']:.6g} (price hasn't reached it yet).")
            else:
                lines.append(f"Remaining {leg['size_pct']:.0f}% leg at {leg['price']:.6g} has been "
                              f"passed by price ({price:.6g}) -- either chase at CMP or drop this leg.")
    else:
        # single/first leg just filled -- tell them how to actually get in
        lines.append(f"All legs filled. Enter at CMP ({price:.6g}) if you haven't already, "
                      f"or on your next confirmation candle.")

    return " ".join(lines)


def refresh_entry(entry, active_key):
    """Refresh ONE watchlist entry in place (mutates entry, sets last_note/history)."""
    symbol, direction = entry["symbol"], entry["direction"]

    tf_results = {}
    for tf in TFS_ALL:
        used_key, df = get_klines_with_fallback(active_key, symbol, tf)
        if df is None:
            continue
        tf_results[tf] = analyze_timeframe(df)

    entry_tf = tf_results.get(ENTRY_TF)
    if entry_tf is None:
        entry["last_note"] = "Couldn't fetch live data this check -- try again shortly."
        return

    price = entry_tf["price"]
    zone_low, zone_high = entry["zone_low"], entry["zone_high"]
    primary_entry_price = entry["entries"][0]["price"]

    # ---- terminal states: explain, don't rescore ----
    if entry["status"] == "invalidated":
        entry["last_note"] = (
            f"Invalidated on {entry['invalidated_at']}. Stop at {entry['invalidation']:.6g} was breached"
            + (f" after triggering on {entry['triggered_at']}." if entry["triggered_at"]
               else " before ever triggering.")
        )
        return

    if entry["status"] == "expired":
        entry["last_note"] = (
            f"Expired on {entry['expired_at']} -- {entry.get('expire_reason', 'no longer valid')}. "
            f"Current price {price:.6g} vs zone {zone_low:.6g}-{zone_high:.6g}."
        )
        return

    # ---- pending: check trigger / pre-trigger invalidation / expiry ----
    if entry["status"] == "pending":
        in_zone = zone_low <= price <= zone_high
        any_leg_touched = any(
            (direction == "BULLISH" and price <= leg["price"]) or
            (direction == "BEARISH" and price >= leg["price"])
            for leg in entry["entries"]
        )

        breached = (direction == "BULLISH" and price <= entry["invalidation"]) or \
                   (direction == "BEARISH" and price >= entry["invalidation"])
        if breached and not (in_zone or any_leg_touched):
            entry["status"] = "invalidated"
            entry["invalidated_at"] = datetime.now(timezone.utc).isoformat()
            _log(entry, f"Invalidated before ever triggering -- price hit {price:.6g}, past "
                        f"stop {entry['invalidation']:.6g}, without tagging the zone.")
            return

        if any_leg_touched:
            for leg in entry["entries"]:
                if (direction == "BULLISH" and price <= leg["price"]) or \
                   (direction == "BEARISH" and price >= leg["price"]):
                    leg["filled"] = True
            entry["status"] = "triggered"
            entry["triggered_at"] = datetime.now(timezone.utc).isoformat()
            entry["entry_price"] = primary_entry_price
            entry["max_r"] = 0.0
            entry["max_favorable_price"] = price
            entry["peak_score"] = entry.get("current_score", entry.get("added_score", 0))
            entry["last_warning"] = None
            entry["entry_instruction"] = _build_entry_instruction(entry, price, direction)
            _log(entry, f"Entry condition met at {price:.6g} -- now tracking as triggered. "
                        f"{entry['entry_instruction']}")
            # fall through so it gets scored immediately this same refresh
        elif in_zone:
            # inside the zone, but hasn't reached your specific leg price(s) yet --
            # this is NOT a trigger. (This used to incorrectly trigger on zone
            # entry alone, which is what caused BTW to show +3.48R with an
            # unfilled leg.)
            entry["last_note"] = (f"Inside the zone (price {price:.6g}, zone "
                                   f"{zone_low:.6g}-{zone_high:.6g}) but hasn't reached your "
                                   f"leg price(s) yet -- still pending.")
            return
        else:
            ran_away = False
            if direction == "BULLISH" and price > zone_high:
                ran_away = pct_distance(price, {"low": zone_low, "high": zone_high}) >= ZONE_PROXIMITY_PCT * EXPIRE_DIST_MULT
            elif direction == "BEARISH" and price < zone_low:
                ran_away = pct_distance(price, {"low": zone_low, "high": zone_high}) >= ZONE_PROXIMITY_PCT * EXPIRE_DIST_MULT

            timed_out = _hours_since(entry["added_at"]) >= EXPIRE_AFTER_HOURS

            if ran_away or timed_out:
                entry["status"] = "expired"
                entry["expired_at"] = datetime.now(timezone.utc).isoformat()
                entry["expire_reason"] = ("price ran away without ever reaching the zone" if ran_away
                                           else f"still pending after {EXPIRE_AFTER_HOURS}h with no trigger")
                _log(entry, f"Expired -- {entry['expire_reason']}.")
                return
            else:
                dist = pct_distance(price, {"low": zone_low, "high": zone_high})
                entry["last_note"] = f"Still pending -- price {price:.6g}, ~{dist:.2f}% from zone."
                return

    # ---- triggered: rescore fresh, check invalidation, adjust stop/targets ----
    if entry["status"] == "triggered":
        breached = (direction == "BULLISH" and price <= entry["invalidation"]) or \
                   (direction == "BEARISH" and price >= entry["invalidation"])
        if breached:
            entry["status"] = "invalidated"
            entry["invalidated_at"] = datetime.now(timezone.utc).isoformat()
            max_r = entry.get("max_r", 0.0)
            reversal_note = (f" Trade reached +{max_r:.2f}R before the stop -- failed-after-profit; "
                             "do not immediately re-enter the same direction."
                             if max_r >= REENTRY_MIN_NEW_R else "")
            _log(entry, f"Triggered on {entry['triggered_at']}, but has now invalidated -- "
                        f"price {price:.6g} broke the stop at {entry['invalidation']:.6g}." + reversal_note)
            return

        r_now = _r_multiple(direction, price, primary_entry_price, entry["original_invalidation"])
        entry["current_r"] = r_now
        reversal_state, reversal_reason, _reversal_level = _position_reversal_diagnostic(tf_results, direction)
        previous_reversal_state = entry.get("reversal_state", "STABLE")
        entry["reversal_state"] = reversal_state
        entry["reversal_reason"] = reversal_reason
        if reversal_state != previous_reversal_state:
            _log(entry, f"Position state: {previous_reversal_state} -> {reversal_state}. {reversal_reason}")
            if reversal_state == "STABLE" and previous_reversal_state != "STABLE":
                send_telegram_message(f"🟢 POSITION STABLE AGAIN: {symbol} {direction}\nWhy: {reversal_reason}\nCurrent R: {r_now:+.2f}R | Max R: {entry.get('max_r', 0.0):+.2f}R\nExisting position only -- NOT a new entry.")
            elif reversal_state != "STABLE":
                prefix = "🔴" if reversal_state == "CONFIRMED_REVERSAL" else "⚠️"
                action = "PROTECT / EXIT; opposite structure is confirmed." if reversal_state == "CONFIRMED_REVERSAL" else "HOLD cautiously; reversal is not confirmed."
                send_telegram_message(f"{prefix} POSITION DIAGNOSTIC: {symbol} {direction}\nState: {reversal_state}\nWhy: {reversal_reason}\nCurrent R: {r_now:+.2f}R | Max R: {entry.get('max_r', 0.0):+.2f}R\nAction: {action}\nDO NOT re-enter the same direction until new structure forms.")
        if direction == "BULLISH":
            if entry.get("max_favorable_price") is None or price > entry["max_favorable_price"]:
                entry["max_favorable_price"] = price
        else:
            if entry.get("max_favorable_price") is None or price < entry["max_favorable_price"]:
                entry["max_favorable_price"] = price
        entry["max_r"] = max(entry.get("max_r", 0.0), r_now)
        old_score = entry["current_score"]
        entry["current_score"] = _score_confirmed(tf_results, direction, r_now)
        entry["peak_score"] = max(entry.get("peak_score", entry["current_score"]), entry["current_score"])

        stop_note = "Stop unchanged (fixed)."
        if entry["trail_mode"] in ("trail_structural", "trail_breakeven"):
            zone_for_struct = {"low": zone_low, "high": zone_high}
            fresh_stop = find_structural_invalidation(entry_tf, direction, zone_for_struct)
            moved = False

            if entry["trail_mode"] == "trail_breakeven" and r_now >= TRAIL_TRIGGER_R:
                if direction == "BULLISH":
                    candidate = max(entry["invalidation"], primary_entry_price, fresh_stop)
                else:
                    candidate = min(entry["invalidation"], primary_entry_price, fresh_stop)
                if candidate != entry["invalidation"]:
                    entry["invalidation"] = candidate
                    moved = True
            elif entry["trail_mode"] == "trail_structural":
                if direction == "BULLISH" and fresh_stop > entry["invalidation"]:
                    entry["invalidation"] = fresh_stop
                    moved = True
                elif direction == "BEARISH" and fresh_stop < entry["invalidation"]:
                    entry["invalidation"] = fresh_stop
                    moved = True

            stop_note = (f"Stop trailed to {entry['invalidation']:.6g}." if moved else
                         f"Stop held at {entry['invalidation']:.6g} (no tighter structural level yet).")

        new_targets = find_tp_targets(entry_tf, direction, price, entry["invalidation"])
        targets_changed = new_targets != entry["targets"]
        if new_targets:
            entry["targets"] = new_targets

        entry["entry_instruction"] = _build_entry_instruction(entry, price, direction)

        delta = entry["current_score"] - old_score
        pct_change = (delta / old_score * 100) if old_score else (100.0 if delta > 0 else 0.0)
        arrow = "up" if delta > 0 else ("down" if delta < 0 else "flat")
        entry["last_score_delta_pct"] = round(pct_change, 1)
        entry["last_score_arrow"] = arrow
        peak = entry.get("peak_score", entry["current_score"])
        warning = None
        if (entry.get("max_r", 0.0) >= REENTRY_MIN_NEW_R
                and peak > 0 and entry["current_score"] <= peak * 0.80
                and reversal_state in ("LIQUIDITY_SWEEP", "MOMENTUM_DETERIORATION", "CONFIRMED_REVERSAL")):
            warning = (f"⚠️ REVERSAL RISK: {symbol} {direction} -- {reversal_state}. "
                       f"+{entry['max_r']:.2f}R max; health {entry['current_score']:.0f}/100 vs peak {peak:.0f}. "
                       f"Why: {reversal_reason}")
            if warning != entry.get("last_warning"):
                entry["last_warning"] = warning
                send_telegram_message(warning)
        _log(entry, f"Refreshed: score {old_score:.1f} -> {entry['current_score']:.1f} "
                    f"({arrow} {pct_change:+.1f}%), "
                    f"currently {r_now:+.2f}R. {stop_note}"
                    + (" Targets updated." if targets_changed else " Targets unchanged.")
                    + f" | {entry['entry_instruction']}")


def check_watchlist(active_key):
    items = load_watchlist()
    if not items:
        print("Watchlist is empty.")
        return

    # prune entries that already died AND were reported in a previous cycle --
    # they got their one appearance in the run where they flipped, this run
    # they're removed rather than cluttering the list forever.
    before = len(items)
    items = [it for it in items if not (
        it.get("dead_reported") and it.get("status") in ("invalidated", "expired")
        and _hours_from_now(it.get("invalidated_at") or it.get("expired_at")) >= REENTRY_HISTORY_HOURS
    )]
    pruned = before - len(items)
    if pruned:
        print(f"(pruned {pruned} previously-reported dead entr{'y' if pruned == 1 else 'ies'})")

    if not items:
        print("Watchlist is empty after pruning.")
        save_watchlist(items)
        return

    print("\n" + "=" * 60)
    print("WATCHLIST STATUS")
    print("=" * 60)

    for i, it in enumerate(items):
        # back-fill legacy/old-schema entries so this never crashes on old data
        it.setdefault("status", "pending")
        it.setdefault("entries", [{"price": it.get("price", 0), "size_pct": 100, "filled": False}])
        it.setdefault("trail_mode", "fixed")
        it.setdefault("original_invalidation", it.get("invalidation"))
        it.setdefault("current_score", it.get("score", it.get("added_score", 0)))
        it.setdefault("added_score", it.get("current_score", 0))
        it.setdefault("history", [])
        it.setdefault("added_at", datetime.now(timezone.utc).isoformat())
        it.setdefault("targets", it.get("targets", []))
        it.setdefault("entry_instruction", None)
        it.setdefault("entry_price", None)
        it.setdefault("max_r", 0.0)
        it.setdefault("max_favorable_price", None)
        it.setdefault("peak_score", it.get("current_score", it.get("added_score", 0)))
        it.setdefault("last_warning", None)
        it.setdefault("reversal_state", "STABLE")
        it.setdefault("reversal_reason", "No active reversal evidence detected.")
        it.setdefault("reversal_alert_state", "STABLE")
        it.setdefault("current_r", 0.0)
        it.setdefault("setup_fingerprint", None)
        it.setdefault("trade_type", "INTRADAY")
        it.setdefault("attempt_num", 1)
        it.setdefault("lineage_note", None)
        it.setdefault("dead_reported", False)
        it.setdefault("last_score_delta_pct", 0.0)
        it.setdefault("last_score_arrow", "flat")
        for key in ("triggered_at", "invalidated_at", "expired_at", "expire_reason"):
            it.setdefault(key, None)

        try:
            prev_status = it["status"]
            refresh_entry(it, active_key)
            time.sleep(0.1)
            if it["status"] != prev_status:
                _notify_status_change(it, prev_status)
                if it["status"] in ("invalidated", "expired"):
                    it["dead_reported"] = True   # gets pruned at the START of the next run
            elif it["status"] == "triggered":
                _notify_score_update(it)
        except Exception as e:
            _log(it, f"Refresh error, left as-is: {e}")

        score_str = f"score={it['current_score']:.0f}" if it["status"] == "triggered" \
                    else f"score={it['added_score']:.0f}"
        attempt_tag = f"  (attempt #{it['attempt_num']})" if it["attempt_num"] > 1 else ""
        print(f"[{i}] {it['symbol']:<14} {it['direction']:<8} [{it['trade_type']}]  "
              f"status={it['status']:<12} {score_str}{attempt_tag}")
        if it.get("lineage_note"):
            print(f"    !! {it['lineage_note']}")
        print(f"    >> {it['last_note']}")
        if it["status"] == "triggered" and it.get("entry_instruction"):
            print(f"    >> ENTRY: {it['entry_instruction']}")
        if it["targets"]:
            tp_str = " | ".join(f"TP{j+1} {t['price']:.6g} (~{t['r']:.2f}R)"
                                 for j, t in enumerate(it["targets"]))
            print(f"       targets: {tp_str}")

    print("=" * 60)
    save_watchlist(items)


def watchlist_menu(active_key):
    while True:
        print("\n--- WATCHLIST MENU ---")
        print("1) View / refresh status")
        print("2) Remove an entry")
        print("3) Back to main menu")
        choice = input("Choice: ").strip()
        if choice == "1":
            check_watchlist(active_key)
        elif choice == "2":
            items = load_watchlist()
            if not items:
                print("Watchlist is empty.")
                continue
            for i, it in enumerate(items):
                print(f"[{i}] {it['symbol']} ({it['direction']}) - {it['status']}")
            try:
                idx = int(input("Enter number to remove: ").strip())
                remove_from_watchlist(idx)
            except ValueError:
                print("Not a number.")
        elif choice == "3":
            return
        else:
            print("Invalid choice.")


# ============================================================================
# SCAN / PRINT
# ============================================================================

def scan_symbol(active_key, symbol):
    """Run all timeframes for one symbol. Returns (tf_results, used_exchanges)."""
    tf_results = {}
    used = {}
    for tf in TFS_ALL:
        used_key, df = get_klines_with_fallback(active_key, symbol, tf)
        if df is not None:
            result = analyze_timeframe(df)
            tf_results[tf] = result
            used[tf] = used_key
        else:
            tf_results[tf] = None
            used[tf] = None
        time.sleep(REQUEST_DELAY)
    return tf_results, used


def print_setup(symbol, tf_results, used, score, direction, plan):
    print("\n" + "=" * 70)
    print(f"{symbol}  --  SETUP SCORE: {score}/100+   BIAS: {direction}   "
          f"TYPE: {plan.get('trade_type', 'INTRADAY')}")
    print("-" * 70)
    for tf in TFS_ALL:
        r = tf_results.get(tf)
        if r is None:
            print(f"[{tf}] no data ({used.get(tf) or 'all exchanges failed'})")
            continue
        line = f"[{tf}] bias={r['bias']:<8} price={r['price']:.6g}  data={used.get(tf)}"
        print(line)
        if r["last_event"]:
            ev = r["last_event"]
            print(f"       last {ev['type']}: {ev['direction']} @ {ev['price']:.6g}")
        if r["nearest_zone"]:
            z = r["nearest_zone"]
            print(f"       nearest {zone_label(z)}: {z['low']:.6g} - {z['high']:.6g} "
                  f"({'unmitigated' if not z['mitigated'] else 'mitigated'})")
        if r["recent_sweep"]:
            s = r["recent_sweep"]
            print(f"       recent sweep: {s['direction']} @ {s['price']:.6g}")
    print("-" * 70)
    print(f"REGIME: {plan.get('regime', 'n/a')}  "
          f"(confidence {plan.get('regime_confidence', 0)}%)   "
          f"ALIGNMENT: {plan.get('trend_alignment', 'n/a')}"
          + (f"  [-{plan['countertrend_penalty']} pts]" if plan.get('countertrend_penalty') else ""))
    print(f"  {plan.get('regime_reason', '')}")
    print("-" * 70)
    print(f"ENTRY PLAN: {plan['mode']}")
    print(f"  {plan['note']}")
    print(f"  Zone: {plan['zone_low']:.6g} - {plan['zone_high']:.6g}  ({plan['zone_label']})")
    print(f"  Invalidation (structural): {plan['invalidation']:.6g}")
    if plan.get("validated_targets"):
        print(f"  Take-profit targets (Phase 3 -- multi-timeframe, structurally sourced):")
        for i, t in enumerate(plan["validated_targets"], 1):
            print(f"    TP{i}: {t['price']:.6g}  -- {t['label']}  (~{t['r']:.2f}R)  [{t['target_reason']}]")
    else:
        print(f"  Take-profit targets: none found in current structure -- "
              f"treat this one with extra caution or skip it.")
    print("-" * 70)
    status = plan.get("status", "n/a")
    print(f"EXECUTION STATE: {status}   (execution_type={plan.get('execution_type')})")
    print(f"  Setup Quality: {plan.get('setup_quality')}   Entry Quality: {plan.get('entry_quality')}")
    print(f"  Mechanical R:R: {plan.get('mechanical_rr', 0):.2f}   "
          f"Structural R:R: {plan.get('structural_rr', 0):.2f}  "
          f"(quality {plan.get('structural_rr_quality', 0)})")
    print(f"  Structural target: {plan.get('structural_target')}  "
          f"[{plan.get('structural_target_type')}] -- {plan.get('structural_target_reason')}")
    if plan.get("preferred_entry") is not None:
        print(f"  Preferred entry: {plan['preferred_entry']:.6g}   "
              f"Distance: {plan.get('distance_to_entry_pct')}%"
              + (f" / {plan.get('distance_to_entry_atr')} ATR" if plan.get('distance_to_entry_atr') is not None else ""))
    if plan.get("breakout_level") is not None:
        print(f"  Breakout level: {plan['breakout_level']}   Required: {plan.get('required_confirmation')}")
    if plan.get("lifecycle_info"):
        li = plan["lifecycle_info"]
        print(f"  Lifecycle: {li['lifecycle']}   "
              f"Alert: {'YES -- ' + li['reason'] if li['send_alert'] else 'suppressed (' + li['reason'] + ')'}")
    print("=" * 70)


def run_scan(active_key, symbols):
    qualifying = []  # list of (symbol, tf_results, used, score, direction, plan)
    total = len(symbols)
    scan_cycle = int(time.time() // 900)  # coarse ~15-min cycle counter for lifecycle bookkeeping
    for n, symbol in enumerate(symbols, 1):
        print(f"\nScanning {symbol} ({n}/{total})...")
        tf_results, used = scan_symbol(active_key, symbol)
        score, direction, regime_info = score_setup_with_regime(tf_results)
        if score >= MIN_SETUP_SCORE and direction:
            plan = build_entry_plan(tf_results, direction, regime_info, setup_score=score)
            if plan:
                plan.update(regime_info)  # regime, regime_confidence, trend_alignment,
                                            # countertrend_penalty, reversal_confirmation,
                                            # regime_reason all now live on the plan dict
                exec_state = determine_execution_state(plan, tf_results, direction)
                plan.update(exec_state)
                plan["lifecycle_info"] = update_setup_lifecycle(symbol, plan, scan_cycle)
                qualifying.append((symbol, tf_results, used, score, direction, plan))

    print(f"\nDone scanning {total} symbol(s). {len(qualifying)} met the setup bar "
          f"(score >= {MIN_SETUP_SCORE}).")

    if not qualifying:
        return

    qualifying.sort(key=lambda x: x[3], reverse=True)
    for symbol, tf_results, used, score, direction, plan in qualifying:
        print_setup(symbol, tf_results, used, score, direction, plan)

    buckets = classify_and_rank([q[5] for q in qualifying])
    print("\n" + "#" * 70)
    print(f"READY NOW: {len(buckets['READY_NOW'])}" if buckets['READY_NOW'] else "READY NOW: NONE")
    print(f"NEAR READY: {len(buckets['NEAR_READY'])}   WAITING: {len(buckets['WAITING'])}   "
          f"INVALIDATED: {len(buckets['INVALIDATED'])}   NO TRADE: {len(buckets['NO_TRADE'])}")
    print("#" * 70)

    ans = input("\nAdd any of these to your watchlist? Enter symbols comma-separated "
                "(or 'all', or press enter to skip): ").strip()
    if not ans:
        return
    if ans.lower() == "all":
        chosen = [q[0] for q in qualifying]
    else:
        chosen = [s.strip().upper() for s in ans.split(",")]

    by_symbol = {q[0]: q for q in qualifying}
    for sym in chosen:
        match = None
        for k in by_symbol:
            if k.upper() == sym or sym in k.upper():
                match = by_symbol[k]
                break
        if match:
            symbol, tf_results, used, score, direction, plan = match
            trail_mode = prompt_trail_mode()
            split_entries = prompt_split_entries(plan, direction)
            add_to_watchlist(symbol, used.get(ENTRY_TF) or active_key, score, direction, plan,
                              trail_mode=trail_mode, split_entries=split_entries)
        else:
            print(f"  ({sym} not found among qualifying setups, skipped)")


# ============================================================================
# MAIN MENU
# ============================================================================

def main():
    print("SMC Futures Scanner -- detecting active exchange...")
    active_key = detect_active_exchange()
    if not active_key:
        print("Couldn't reach any supported exchange. Check your connection and restart.")
        sys.exit(1)

    while True:
        print("\n" + "=" * 60)
        print(f"MAIN MENU  (data source: {ADAPTERS[active_key].name})")
        print("=" * 60)
        print("1) Single ticker")
        print("2) Top 50 by 24h volume")
        print("3) Top 51-100 by 24h volume")
        print("4) Top 101-150 by 24h volume")
        print("5) Watchlist (view / check / remove)")
        print("6) Re-detect active exchange")
        print("7) Quit")
        choice = input("Choice: ").strip()

        if choice == "1":
            raw = input("Enter symbol (e.g. BTC): ").strip().upper()
            if not raw:
                continue
            candidates = [raw, raw + "USDT", raw + "-USDT-SWAP", raw + "USDTM", raw + "_USDT"]
            found_symbol = None
            for cand in candidates:
                _, df = get_klines_with_fallback(active_key, cand, "1H")
                if df is not None:
                    found_symbol = cand
                    break
            if not found_symbol:
                print(f"Couldn't fetch data for '{raw}' on any exchange in the chain.")
                continue
            print(f"Scanning {found_symbol} across {', '.join(TFS_ALL)} ...")
            run_scan(active_key, [found_symbol])

        elif choice in ("2", "3", "4"):
            ranges = {"2": (0, 50), "3": (50, 100), "4": (100, 150)}
            start, end = ranges[choice]
            key_used, symbols = get_symbols_with_fallback(active_key, end)
            if not symbols:
                print("Couldn't fetch symbol list from any exchange.")
                continue
            tier_symbols = symbols[start:end]
            if not tier_symbols:
                print("No symbols in that range (exchange may have fewer listings).")
                continue
            print(f"Scanning {len(tier_symbols)} symbol(s) across {', '.join(TFS_ALL)} ...")
            run_scan(active_key, tier_symbols)

        elif choice == "5":
            watchlist_menu(active_key)

        elif choice == "6":
            new_key = detect_active_exchange()
            if new_key:
                active_key = new_key
            else:
                print("Still couldn't reach any exchange.")

        elif choice == "7":
            print("Bye.")
            sys.exit(0)

        else:
            print("Invalid choice.")


if __name__ == "__main__":
    main()
