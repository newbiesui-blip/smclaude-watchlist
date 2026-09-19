"""Read-only CoinGlass V4 adapter for ATHENA trade-health analysis.

No order, position, SL, TP, or exchange state is modified here.
The adapter is optional: when COINGLASS_API_KEY is absent or a source fails,
the health engine receives explicit source-unavailable metadata instead of
fabricating derivatives conclusions.
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict, Optional

import requests

BASE_URL = "https://open-api-v4.coinglass.com"
DEFAULT_EXCHANGES = os.getenv("COINGLASS_EXCHANGES", "Binance,OKX,Bybit")
TIMEOUT_SECONDS = float(os.getenv("COINGLASS_TIMEOUT_SECONDS", "8"))
CACHE_SECONDS = int(os.getenv("COINGLASS_CACHE_SECONDS", "30"))

_CACHE: Dict[str, tuple[float, Any]] = {}


def _base_symbol(symbol: str) -> str:
    value = str(symbol or "").upper().strip()
    for suffix in ("-SWAP", "_USDT", "-USDT", "USDT"):
        if value.endswith(suffix):
            value = value[: -len(suffix)]
            break
    return value


def _pair_symbol(symbol: str) -> str:
    base = _base_symbol(symbol)
    return f"{base}USDT"


def _f(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        value = float(value)
        return value if value == value and abs(value) != float("inf") else None
    except (TypeError, ValueError):
        return None


class CoinGlassClient:
    def __init__(self, api_key: Optional[str] = None, session=None):
        self.api_key = api_key or os.getenv("COINGLASS_API_KEY")
        self.session = session or requests.Session()

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _get(self, path: str, params: Dict[str, Any]) -> Optional[Any]:
        if not self.enabled:
            return None
        key = path + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))
        now = time.time()
        cached = _CACHE.get(key)
        if cached and now - cached[0] <= CACHE_SECONDS:
            return cached[1]

        try:
            response = self.session.get(
                BASE_URL + path,
                params=params,
                headers={"CG-API-KEY": self.api_key},
                timeout=TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or str(payload.get("code", "0")) not in {"0", "200"}:
                return None
            data = payload.get("data")
            _CACHE[key] = (now, data)
            return data
        except Exception:
            return None

    def snapshot(self, symbol: str) -> Dict[str, Any]:
        base = _base_symbol(symbol)
        pair = _pair_symbol(symbol)
        result: Dict[str, Any] = {
            "enabled": self.enabled,
            "symbol": pair,
            "base_symbol": base,
            "source": "coinglass_v4",
            "sources": {},
        }
        if not self.enabled:
            result["availability"] = "NOT_CONFIGURED"
            return result

        result["availability"] = "PARTIAL"

        oi = self._get(
            "/api/futures/open-interest/aggregated-history",
            {"exchange_list": DEFAULT_EXCHANGES, "symbol": base, "interval": "1h", "limit": 6, "unit": "usd"},
        )
        if isinstance(oi, list) and len(oi) >= 2:
            result["oi_history"] = oi
            result["sources"]["oi"] = "OK"
        else:
            result["sources"]["oi"] = "UNAVAILABLE"

        funding = self._get(
            "/api/futures/funding-rate/history",
            {"exchange": "Binance", "symbol": pair, "interval": "1h", "limit": 6},
        )
        if isinstance(funding, list) and funding:
            result["funding_history"] = funding
            result["sources"]["funding"] = "OK"
        else:
            result["sources"]["funding"] = "UNAVAILABLE"

        spot_cvd = self._get(
            "/api/spot/aggregated-cvd/history",
            {"exchange_list": DEFAULT_EXCHANGES, "symbol": base, "interval": "1h", "limit": 6, "unit": "usd"},
        )
        if isinstance(spot_cvd, list) and len(spot_cvd) >= 2:
            result["spot_cvd_history"] = spot_cvd
            result["sources"]["spot_cvd"] = "OK"
        else:
            result["sources"]["spot_cvd"] = "UNAVAILABLE"

        futures_cvd = self._get(
            "/api/futures/aggregated-cvd/history",
            {"exchange_list": DEFAULT_EXCHANGES, "symbol": base, "interval": "1h", "limit": 6, "unit": "usd"},
        )
        if isinstance(futures_cvd, list) and len(futures_cvd) >= 2:
            result["futures_cvd_history"] = futures_cvd
            result["sources"]["futures_cvd"] = "OK"
        else:
            result["sources"]["futures_cvd"] = "UNAVAILABLE"

        liquidations = self._get(
            "/api/futures/liquidation/aggregated-history",
            {"exchange_list": DEFAULT_EXCHANGES, "symbol": base, "interval": "1h", "limit": 6},
        )
        if isinstance(liquidations, list) and liquidations:
            result["liquidation_history"] = liquidations
            result["sources"]["liquidations"] = "OK"
        else:
            result["sources"]["liquidations"] = "UNAVAILABLE"

        taker = self._get(
            "/api/spot/aggregated-taker-buy-sell-volume/history",
            {"exchange_list": DEFAULT_EXCHANGES, "symbol": base, "interval": "1h", "limit": 6, "unit": "usd"},
        )
        if isinstance(taker, list) and len(taker) >= 2:
            result["spot_taker_history"] = taker
            result["sources"]["spot_taker"] = "OK"
        else:
            result["sources"]["spot_taker"] = "UNAVAILABLE"

        result["availability"] = "OK" if any(v == "OK" for v in result["sources"].values()) else "UNAVAILABLE"
        return result
