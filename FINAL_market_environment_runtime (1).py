"""Live, read-only market-environment inputs for ATHENA.

The evaluator in market_environment.py remains pure and deterministic. This
module only acquires public/read-only data and converts it into the evaluator's
normalized event/liquidity inputs.

Sources:
- U.S. Bureau of Labor Statistics public release calendar (ICS):
  Employment Situation (NFP), CPI, PPI, and published U.S. holidays.
- Federal Reserve public FOMC calendar.
- BingX public swap ticker/klines endpoints for market-wide liquidity.

No authenticated account endpoint and no trading endpoint is used.
"""
from __future__ import annotations

import html
import math
import re
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any, Iterable, Mapping, Optional
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import market_environment as evaluator

BLS_ICS_URL = "https://www.bls.gov/schedule/news_release/bls.ics"
FOMC_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
BINGX_TICKER_URL = "https://open-api.bingx.com/openApi/swap/v2/quote/ticker"
BINGX_KLINES_URL = "https://open-api.bingx.com/openApi/swap/v3/quote/klines"

UTC = timezone.utc
NEW_YORK = ZoneInfo("America/New_York")

EVENT_WINDOWS = {
    "Employment Situation": (timedelta(minutes=30), timedelta(minutes=90)),
    "Consumer Price Index": (timedelta(minutes=30), timedelta(minutes=90)),
    "Producer Price Index": (timedelta(minutes=30), timedelta(minutes=90)),
}


def _get(url: str, timeout: int = 10) -> str:
    request = Request(url, headers={"User-Agent": "ATHENA/1.0 market-environment"})
    with urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def _parse_ics_datetime(value: str) -> Optional[datetime]:
    value = value.strip()
    if ":" in value:
        value = value.split(":", 1)[1]
    if value.endswith("Z"):
        try:
            return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        except ValueError:
            return None
    for fmt in ("%Y%m%dT%H%M%S", "%Y%m%d"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=NEW_YORK).astimezone(UTC)
        except ValueError:
            pass
    return None


# BLS publishes the official calendar as ICS, but that endpoint can reject
# GitHub Actions egress with HTTP 403. Keep a bounded, official-calendar
# fallback for the remainder of 2026 so a transport problem does not become
# a permanent NEW-ENTRY outage. The dates below mirror the BLS 2026 release
# calendar; the fallback expires at year-end rather than guessing future dates.
# The Fed calendar is public and authoritative, but GitHub Actions egress can
# occasionally reject the HTML endpoint. Keep the bounded 2026 meeting dates
# as a transport fallback; this is not a forecast and expires at year-end.
_FOMC_2026_MEETINGS = (
    ("2026-01-27T00:00:00-05:00", "2026-01-28T23:59:00-05:00"),
    ("2026-03-17T00:00:00-04:00", "2026-03-18T23:59:00-04:00"),
    ("2026-04-28T00:00:00-04:00", "2026-04-29T23:59:00-04:00"),
    ("2026-06-16T00:00:00-04:00", "2026-06-17T23:59:00-04:00"),
    ("2026-07-28T00:00:00-04:00", "2026-07-29T23:59:00-04:00"),
    ("2026-09-15T00:00:00-04:00", "2026-09-16T23:59:00-04:00"),
    ("2026-10-27T00:00:00-04:00", "2026-10-28T23:59:00-04:00"),
    ("2026-12-08T00:00:00-05:00", "2026-12-09T23:59:00-05:00"),
)

def _fallback_fomc_events(now: datetime) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    current = now.astimezone(UTC)
    for start_value, end_value in _FOMC_2026_MEETINGS:
        start = datetime.fromisoformat(start_value).astimezone(UTC)
        end = datetime.fromisoformat(end_value).astimezone(UTC)
        if end < current - timedelta(days=2) or start > current + timedelta(days=45):
            continue
        events.append({
            "name": "FOMC",
            "impact": "HIGH",
            "starts_at": start.isoformat(),
            "ends_at": end.isoformat(),
            "source": "FOMC-2026-official-calendar-fallback",
        })
    return events

_BLS_2026_HIGH_IMPACT = (
    ("Employment Situation", "2026-09-04T08:30:00-04:00"),
    ("Producer Price Index", "2026-09-10T08:30:00-04:00"),
    ("Consumer Price Index", "2026-09-11T08:30:00-04:00"),
    ("Employment Situation", "2026-10-02T08:30:00-04:00"),
    ("Consumer Price Index", "2026-10-14T08:30:00-04:00"),
    ("Producer Price Index", "2026-10-15T08:30:00-04:00"),
    ("Employment Situation", "2026-11-06T08:30:00-05:00"),
    ("Consumer Price Index", "2026-11-12T08:30:00-05:00"),
    ("Producer Price Index", "2026-11-13T08:30:00-05:00"),
    ("Employment Situation", "2026-12-04T08:30:00-05:00"),
    ("Consumer Price Index", "2026-12-10T08:30:00-05:00"),
    ("Producer Price Index", "2026-12-11T08:30:00-05:00"),
)

def _fallback_bls_events(now: datetime) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    current = now.astimezone(UTC)
    for name, value in _BLS_2026_HIGH_IMPACT:
        start = datetime.fromisoformat(value).astimezone(UTC)
        if start < current - timedelta(days=2) or start > current + timedelta(days=45):
            continue
        before, after = EVENT_WINDOWS[name]
        events.append({
            "name": name,
            "impact": "HIGH",
            "starts_at": (start - before).isoformat(),
            "ends_at": (start + after).isoformat(),
            "source": "BLS-2026-official-calendar-fallback",
        })
    return events


def fetch_bls_events(now: Optional[datetime] = None) -> list[dict[str, Any]]:
    """Fetch relevant BLS releases/holidays from the official ICS feed."""
    current = (now or datetime.now(UTC)).astimezone(UTC)
    try:
        raw = _get(BLS_ICS_URL)
    except Exception as exc:
        fallback = _fallback_bls_events(current)
        if fallback:
            return fallback
        raise RuntimeError(f"BLS calendar unavailable: {type(exc).__name__}: {exc}") from exc
    events: list[dict[str, Any]] = []
    for block in raw.split("BEGIN:VEVENT"):
        if "END:VEVENT" not in block:
            continue
        summary_match = re.search(r"^SUMMARY(?:;[^:]*)?:(.*)$", block, re.MULTILINE)
        start_match = re.search(r"^DTSTART(?:;[^:]*)?:(.*)$", block, re.MULTILINE)
        if not summary_match or not start_match:
            continue
        summary = html.unescape(summary_match.group(1).strip())
        start = _parse_ics_datetime(start_match.group(1))
        if start is None:
            continue
        if start < current - timedelta(days=2) or start > current + timedelta(days=45):
            continue

        matched = next((name for name in EVENT_WINDOWS if name.lower() in summary.lower()), None)
        if matched:
            before, after = EVENT_WINDOWS[matched]
            events.append({
                "name": matched,
                "impact": "HIGH",
                "starts_at": (start - before).isoformat(),
                "ends_at": (start + after).isoformat(),
                "source": BLS_ICS_URL,
            })
        elif re.search(r"\b(Day|Holiday)\b", summary, re.IGNORECASE):
            events.append({
                "name": summary,
                "impact": "HIGH",
                "starts_at": start.isoformat(),
                "ends_at": (start + timedelta(hours=24)).isoformat(),
                "source": BLS_ICS_URL,
            })
    return events


def _strip_html(text: str) -> str:
    text = re.sub(r"<script[\s\S]*?</script>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _month_number(name: str) -> int:
    return datetime.strptime(name[:3], "%b").month


def fetch_fomc_events(now: Optional[datetime] = None) -> list[dict[str, Any]]:
    """Extract FOMC windows, with a bounded official-calendar fallback."""
    current = (now or datetime.now(UTC)).astimezone(NEW_YORK)
    year = current.year
    try:
        text = _strip_html(_get(FOMC_URL))
    except Exception as exc:
        fallback = _fallback_fomc_events(current)
        if fallback:
            return fallback
        raise RuntimeError(f"FOMC calendar unavailable: {type(exc).__name__}: {exc}") from exc
    marker = re.search(rf"{year}\s+FOMC Meetings(.*?){year + 1}\s+FOMC Meetings", text)
    if not marker:
        marker = re.search(rf"{year}\s+FOMC Meetings(.*?)(?:{year - 1}\s+FOMC Meetings|$)", text)
    if not marker and year == 2026:
        fallback = _fallback_fomc_events(current)
        if fallback:
            return fallback
    section = marker.group(1) if marker else text

    months = "January|February|March|April|May|June|July|August|September|October|November|December"
    pattern = re.compile(rf"({months})\s+(\d{{1,2}})(?:-(\d{{1,2}}))?")
    events: list[dict[str, Any]] = []
    for match in pattern.finditer(section):
        month = _month_number(match.group(1))
        day1 = int(match.group(2))
        day2 = int(match.group(3) or day1)
        try:
            start_local = datetime(year, month, day1, 0, 0, tzinfo=NEW_YORK)
            end_local = datetime(year, month, day2, 23, 59, tzinfo=NEW_YORK)
        except ValueError:
            continue
        start = start_local.astimezone(UTC)
        end = end_local.astimezone(UTC)
        if end < (current - timedelta(days=2)) or start > (current + timedelta(days=45)):
            continue
        events.append({
            "name": "FOMC",
            "impact": "HIGH",
            "starts_at": start.isoformat(),
            "ends_at": end.isoformat(),
            "source": FOMC_URL,
        })
    return events


def fetch_high_impact_events(now: Optional[datetime] = None) -> list[dict[str, Any]]:
    """Combine official U.S. macro calendars; fail if either source fails."""
    bls = fetch_bls_events(now=now)
    fomc = fetch_fomc_events(now=now)
    return bls + fomc


def _number(value: Any) -> Optional[float]:
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def _true_ranges(rows: list[Mapping[str, Any]]) -> list[float]:
    result: list[float] = []
    previous_close: Optional[float] = None
    for row in rows:
        high = _number(row.get("high"))
        low = _number(row.get("low"))
        close = _number(row.get("close"))
        if high is None or low is None or close is None:
            continue
        if previous_close is None:
            tr = high - low
        else:
            tr = max(high - low, abs(high - previous_close), abs(low - previous_close))
        result.append(max(0.0, tr))
        previous_close = close
    return result


def _ticker_rows() -> list[dict[str, Any]]:
    payload = __import__("json").loads(_get(BINGX_TICKER_URL))
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise ValueError("BingX ticker response missing data list")
    rows = []
    for item in data:
        if not isinstance(item, Mapping) or not str(item.get("symbol", "")).endswith("-USDT"):
            continue
        volume = _number(item.get("quoteVolume"))
        bid = _number(item.get("bidPrice"))
        ask = _number(item.get("askPrice"))
        if volume is None or volume <= 0 or bid is None or ask is None or bid <= 0 or ask < bid:
            continue
        rows.append({"symbol": item["symbol"], "quoteVolume": volume, "bid": bid, "ask": ask})
    rows.sort(key=lambda row: row["quoteVolume"], reverse=True)
    return rows


def _klines(symbol: str, limit: int = 200) -> list[dict[str, Any]]:
    import json
    payload = json.loads(_get(BINGX_KLINES_URL + f"?symbol={symbol}&interval=1h&limit={limit}"))
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise ValueError(f"BingX kline response missing data for {symbol}")
    rows = []
    for item in data:
        if not isinstance(item, Mapping):
            continue
        rows.append(item)
    rows.sort(key=lambda row: _number(row.get("time")) or 0)
    return rows


def fetch_liquidity_state(sample_size: int = 10) -> dict[str, float]:
    """Build conservative market-wide liquidity ratios from BingX public data."""
    tickers = _ticker_rows()
    if len(tickers) < max(3, sample_size):
        raise ValueError("insufficient BingX ticker observations")

    sample = tickers[:sample_size]
    spreads = []
    for row in sample:
        mid = (row["bid"] + row["ask"]) / 2.0
        spreads.append((row["ask"] - row["bid"]) / mid * 10_000.0)
    spread_bps = median(spreads)

    volume_ratios = []
    atr_ratios = []
    active_participants = 0
    for row in sample:
        try:
            candles = _klines(row["symbol"], limit=200)
            if len(candles) < 30:
                continue
            volumes = [_number(c.get("volume")) for c in candles]
            volumes = [v for v in volumes if v is not None and v >= 0]
            if len(volumes) < 30:
                continue
            latest_volume = volumes[-2] if len(volumes) >= 2 else volumes[-1]
            baseline_volume = median(volumes[-26:-2])
            if baseline_volume > 0:
                volume_ratios.append(latest_volume / baseline_volume)
                if latest_volume >= baseline_volume:
                    active_participants += 1

            trs = _true_ranges(candles)
            if len(trs) >= 30:
                current_atr = median(trs[-15:-1])
                baseline_atrs = []
                for i in range(max(15, len(trs) - 120), len(trs) - 15, 15):
                    window = trs[max(0, i - 14):i + 1]
                    if window:
                        baseline_atrs.append(median(window))
                if baseline_atrs and median(baseline_atrs) > 0:
                    atr_ratios.append(current_atr / median(baseline_atrs))
        except Exception:
            continue

    if not volume_ratios or not atr_ratios:
        raise ValueError("insufficient BingX candle history for liquidity ratios")

    return {
        "spread_bps": float(spread_bps),
        "volume_ratio": float(median(volume_ratios)),
        "atr_ratio": float(median(atr_ratios)),
        "participation_ratio": float(active_participants / max(1, len(volume_ratios))),
    }


def load_live_state(*, now: Optional[datetime] = None) -> evaluator.EnvironmentState:
    """Fetch all live inputs and evaluate them; any source failure fails closed."""
    events = fetch_high_impact_events(now=now)
    liquidity = fetch_liquidity_state()
    return evaluator.evaluate(now=now, events=events, liquidity=liquidity)
