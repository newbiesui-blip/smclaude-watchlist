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
MIN_SETUP_SCORE = 60                          # symbols below this score are not shown as setups
AUTO_ADD_MIN_SCORE = 80                       # unattended full-scan (GitHub Actions) only
                                               # auto-adds setups scoring at or above this
AUTO_TRAIL_MODE = "trail_structural"          # default stop behavior for auto-added entries
                                               # (no human present to choose interactively)

WATCHLIST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "smc_watchlist.json")

EXCHANGE_CHAIN = ["binance", "bybit", "kucoin", "bingx", "mexc", "bitget", "okx"]

TF_MINUTES = {"15M": 15, "1H": 60, "4H": 240, "1D": 1440}

HEADERS = {"User-Agent": "Mozilla/5.0 (SMC-Scanner)"}

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


def build_entry_plan(tf_results, direction):
    """
    Build a human-readable entry plan: entry zone, a structurally-anchored
    invalidation, and structure-based take-profit targets (TP1/TP2/TP3,
    however many real ones exist), each expressed as an R-multiple.
    """
    entry = tf_results.get(ENTRY_TF)
    zone = entry.get("nearest_zone")
    price = entry["price"]

    if not zone:
        return None

    low, high = zone["low"], zone["high"]
    label = zone_label(zone)
    inside = low <= price <= high

    invalidation = find_structural_invalidation(entry, direction, zone)

    if direction == "BULLISH":
        if inside:
            mode = "PULLBACK (price already inside zone)"
            note = (f"Price is currently INSIDE the bullish {label} at "
                    f"{low:.6g}-{high:.6g}. Wait for a bullish reaction/rejection "
                    f"candle here before entering -- don't enter blind just because "
                    f"price tagged the zone.")
        else:
            mode = "WAIT FOR PULLBACK"
            note = (f"Price is approaching the bullish {label} at "
                    f"{low:.6g}-{high:.6g}. Wait for price to pull back into the "
                    f"zone and show a bullish reaction before entering.")
    else:
        if inside:
            mode = "PULLBACK (price already inside zone)"
            note = (f"Price is currently INSIDE the bearish {label} at "
                    f"{low:.6g}-{high:.6g}. Wait for a bearish reaction/rejection "
                    f"candle here before entering.")
        else:
            mode = "WAIT FOR PULLBACK"
            note = (f"Price is approaching the bearish {label} at "
                    f"{low:.6g}-{high:.6g}. Wait for price to pull back into the "
                    f"zone and show a bearish reaction before entering.")

    # breakout-retest variant: if the entry TF just had a fresh break in this
    # direction and the zone is behind price (already broken through), frame
    # it as a breakout retest instead of a first-touch pullback.
    if entry.get("fresh_break") and entry["last_event"]["direction"] == direction.lower():
        break_price = entry["last_event"]["price"]
        broke_through = (direction == "BULLISH" and price > break_price) or \
                         (direction == "BEARISH" and price < break_price)
        if broke_through:
            mode = "WAIT FOR BREAKOUT RETEST"
            note = (f"Fresh {entry['last_event']['type']} {direction.lower()} at "
                    f"{break_price:.6g}. Wait for price to retest the broken level "
                    f"or the {label.lower()} at {low:.6g}-{high:.6g} and confirm "
                    f"before entering.")

    targets = find_tp_targets(entry, direction, price, invalidation)
    trade_type = classify_trade_type(price, invalidation, targets)

    return {
        "mode": mode,
        "note": note,
        "zone_low": low,
        "zone_high": high,
        "zone_label": label,
        "invalidation": invalidation,
        "targets": targets,
        "direction": direction,
        "trade_type": trade_type,
    }


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


def add_to_watchlist(symbol, exchange_key, score, direction, plan,
                      trail_mode="fixed", split_entries=None):
    items = load_watchlist()
    # avoid exact duplicate (same symbol+direction still pending/triggered)
    for it in items:
        if it["symbol"] == symbol and it["direction"] == direction and it["status"] in ("pending", "triggered"):
            print(f"  ({symbol} already on watchlist as {it['status']})")
            return

    # lineage: how many prior dead attempts (invalidated/expired) exist for
    # this symbol+direction? Lets a re-add be clearly flagged as a NEW setup,
    # not the old one coming back to life -- invalidated/expired are terminal
    # and never revert.
    prior_attempts = [it for it in items if it["symbol"] == symbol and it["direction"] == direction
                       and it["status"] in ("invalidated", "expired")]
    attempt_num = len(prior_attempts) + 1
    lineage_note = None
    if prior_attempts:
        last = prior_attempts[-1]
        reason = last.get("expire_reason") or "stop breached"
        when = last.get("invalidated_at") or last.get("expired_at")
        lineage_note = (f"Attempt #{attempt_num} for {symbol} {direction} -- this is a NEW setup, "
                         f"not a revival of the previous one ({last['status']} on {when}: {reason}).")
        print(f"  (note: {lineage_note})")

    if trail_mode not in VALID_TRAIL_MODES:
        trail_mode = "fixed"
    if split_entries is None:
        mid = (plan["zone_low"] + plan["zone_high"]) / 2
        split_entries = [{"price": mid, "size_pct": 100, "filled": False}]

    items.append({
        "symbol": symbol,
        "exchange": exchange_key,
        "direction": direction,
        "status": "pending",              # pending | triggered | invalidated | expired
        "added_score": score,             # original entry-quality score, frozen forever
        "current_score": score,           # live score, updated every refresh once triggered
        "trade_type": plan.get("trade_type", "INTRADAY"),
        "attempt_num": attempt_num,
        "lineage_note": lineage_note,
        "added_at": datetime.now(timezone.utc).isoformat(),
        "triggered_at": None,
        "invalidated_at": None,
        "expired_at": None,
        "expire_reason": None,
        "zone_low": plan["zone_low"],
        "zone_high": plan["zone_high"],
        "zone_label": plan["zone_label"],
        "entries": split_entries,
        "invalidation": plan["invalidation"],
        "original_invalidation": plan["invalidation"],   # kept for R-multiple reference, never overwritten
        "targets": plan.get("targets", []),
        "trail_mode": trail_mode,
        "last_note": "Waiting for price to reach entry zone.",
        "entry_instruction": None,
        "history": [],
    })
    save_watchlist(items)
    print(f"  + Added {symbol} ({direction}) to watchlist. Stop mode: {trail_mode}.")


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
            f"\U0001F7E2 TRIGGERED: {symbol} {direction} [{trade_type}]",
            f"Score: {entry['current_score']:.0f}",
            entry.get("entry_instruction", ""),
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
    """
    Per-cycle update for an entry that's STILL triggered (no status change).
    This is what answers 'update my confidence every 15 min' -- separate
    from _notify_status_change, which only fires on pending/triggered/
    invalidated/expired transitions.
    """
    if not TELEGRAM_ENABLED:
        return
    pct = entry.get("last_score_delta_pct", 0.0)
    if abs(pct) < SCORE_UPDATE_MIN_PCT:
        return

    arrow_emoji = {"up": "\U0001F53C", "down": "\U0001F53D", "flat": "\u27A1\uFE0F"}.get(
        entry.get("last_score_arrow", "flat"), "\u27A1\uFE0F")
    symbol, direction = entry["symbol"], entry["direction"]
    trade_type = entry.get("trade_type", "INTRADAY")

    lines = [
        f"{arrow_emoji} {symbol} {direction} [{trade_type}] -- score {entry['current_score']:.0f} "
        f"({pct:+.1f}%)",
        entry.get("entry_instruction", ""),
        f"SL: {entry['invalidation']:.6g}",
    ]
    if entry.get("targets"):
        tp_str = " | ".join(f"TP{i+1} {t['price']:.6g} (~{t['r']:.2f}R)"
                             for i, t in enumerate(entry["targets"]))
        lines.append(f"TP: {tp_str}")
    send_telegram_message("\n".join(str(l) for l in lines if l))


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

    return round(score, 1)


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
            _log(entry, f"Triggered on {entry['triggered_at']}, but has now invalidated -- "
                        f"price {price:.6g} broke the stop at {entry['invalidation']:.6g}.")
            return

        r_now = _r_multiple(direction, price, primary_entry_price, entry["original_invalidation"])
        old_score = entry["current_score"]
        entry["current_score"] = _score_confirmed(tf_results, direction, r_now)

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
    items = [it for it in items if not (it.get("dead_reported") and it.get("status") in ("invalidated", "expired"))]
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
    print(f"ENTRY PLAN: {plan['mode']}")
    print(f"  {plan['note']}")
    print(f"  Zone: {plan['zone_low']:.6g} - {plan['zone_high']:.6g}  ({plan['zone_label']})")
    print(f"  Invalidation (structural): {plan['invalidation']:.6g}")
    if plan["targets"]:
        print(f"  Take-profit targets (structure-based):")
        for i, t in enumerate(plan["targets"], 1):
            print(f"    TP{i}: {t['price']:.6g}  -- {t['label']}  (~{t['r']:.2f}R)")
    else:
        print(f"  Take-profit targets: none found in current structure -- "
              f"treat this one with extra caution or skip it.")
    print("=" * 70)


def run_scan(active_key, symbols):
    qualifying = []  # list of (symbol, tf_results, used, score, direction, plan)
    total = len(symbols)
    for n, symbol in enumerate(symbols, 1):
        print(f"\nScanning {symbol} ({n}/{total})...")
        tf_results, used = scan_symbol(active_key, symbol)
        score, direction = score_setup(tf_results)
        if score >= MIN_SETUP_SCORE and direction:
            plan = build_entry_plan(tf_results, direction)
            if plan:
                qualifying.append((symbol, tf_results, used, score, direction, plan))

    print(f"\nDone scanning {total} symbol(s). {len(qualifying)} met the setup bar "
          f"(score >= {MIN_SETUP_SCORE}).")

    if not qualifying:
        return

    qualifying.sort(key=lambda x: x[3], reverse=True)
    for symbol, tf_results, used, score, direction, plan in qualifying:
        print_setup(symbol, tf_results, used, score, direction, plan)

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
