#!/usr/bin/env python3
"""
Read-only CoinGecko market-data adapter for ATHENA.

This module is intentionally independent of SMC setup generation, execution,
BingX lifecycle state, and derivatives decisions.

Environment:
    COINGECKO_API_KEY (optional)

The adapter uses the public/demo CoinGecko API when no key is supplied and
supports the Pro API host when a key is supplied. No trading actions are
performed.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterable, List, Optional


DEFAULT_PUBLIC_BASE_URL = "https://api.coingecko.com/api/v3"
DEFAULT_PRO_BASE_URL = "https://pro-api.coingecko.com/api/v3"
DEFAULT_TIMEOUT = 15
SOURCE_NAME = "coingecko"


class CoinGeckoError(RuntimeError):
    """Base exception for CoinGecko adapter failures."""


class CoinGeckoClient:
    """Small, dependency-free, read-only CoinGecko client."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        timeout: int = DEFAULT_TIMEOUT,
        base_url: Optional[str] = None,
    ) -> None:
        self.api_key = api_key or os.getenv("COINGECKO_API_KEY")
        self.timeout = timeout
        self.base_url = (
            base_url
            or (DEFAULT_PRO_BASE_URL if self.api_key else DEFAULT_PUBLIC_BASE_URL)
        ).rstrip("/")

    @staticmethod
    def _number(value: Any) -> Optional[float]:
        if value is None or value == "":
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _int(value: Any) -> Optional[int]:
        if value is None or value == "":
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _request(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        query = urllib.parse.urlencode(
            [(key, value) for key, value in (params or {}).items() if value is not None]
        )
        url = f"{self.base_url}/{path.lstrip('/')}"
        if query:
            url = f"{url}?{query}"

        headers = {
            "Accept": "application/json",
            "User-Agent": "ATHENA-CoinGecko-Client/1.0",
        }
        if self.api_key:
            # CoinGecko Pro uses x-cg-pro-api-key.
            headers["x-cg-pro-api-key"] = self.api_key

        request = urllib.request.Request(url, headers=headers, method="GET")

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8")[:500]
            except Exception:
                pass
            raise CoinGeckoError(
                f"HTTP {exc.code} from CoinGecko: {detail or exc.reason}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise CoinGeckoError(f"CoinGecko request failed: {exc}") from exc

        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CoinGeckoError("CoinGecko returned invalid JSON") from exc

    def markets(
        self,
        vs_currency: str = "usd",
        ids: Optional[Iterable[str]] = None,
        per_page: int = 100,
        page: int = 1,
    ) -> List[Dict[str, Any]]:
        """Return normalized market records for one page of CoinGecko markets."""
        params: Dict[str, Any] = {
            "vs_currency": vs_currency.lower(),
            "ids": ",".join(ids) if ids else None,
            "order": "market_cap_desc",
            "per_page": max(1, min(int(per_page), 250)),
            "page": max(1, int(page)),
            "sparkline": "false",
            "price_change_percentage": "24h,7d,30d",
        }
        payload = self._request("/coins/markets", params)
        if not isinstance(payload, list):
            raise CoinGeckoError("Unexpected CoinGecko markets response")
        return [self.normalize_market(item) for item in payload if isinstance(item, dict)]

    def coin(self, coin_id: str) -> Dict[str, Any]:
        """Return a normalized CoinGecko coin record by CoinGecko ID."""
        if not coin_id or not str(coin_id).strip():
            raise ValueError("coin_id is required")

        payload = self._request(
            f"/coins/{urllib.parse.quote(str(coin_id).strip(), safe='')}",
            {
                "localization": "false",
                "tickers": "false",
                "market_data": "true",
                "community_data": "false",
                "developer_data": "false",
                "sparkline": "false",
            },
        )
        if not isinstance(payload, dict):
            raise CoinGeckoError("Unexpected CoinGecko coin response")
        return self.normalize_coin(payload)

    def get_asset(
        self,
        coin_id: str,
        vs_currency: str = "usd",
    ) -> Dict[str, Any]:
        """Convenience method returning one combined normalized asset record."""
        market_rows = self.markets(vs_currency=vs_currency, ids=[coin_id], per_page=1)
        market = market_rows[0] if market_rows else {}
        coin = self.coin(coin_id)

        return {
            "source": SOURCE_NAME,
            "source_id": coin.get("id") or coin_id,
            "identity": {
                "id": coin.get("id") or coin_id,
                "symbol": coin.get("symbol"),
                "name": coin.get("name"),
                "platforms": coin.get("platforms") or {},
            },
            "market": market,
            "fundamentals": {
                "categories": coin.get("categories") or [],
                "description": coin.get("description") or {},
                "links": coin.get("links") or {},
            },
            "supply": coin.get("supply") or {},
            "source_updated_at": coin.get("last_updated"),
            "fetched_at": time.time(),
        }

    @classmethod
    def normalize_market(cls, item: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize the stable fields used by ATHENA's future aggregator."""
        return {
            "source": SOURCE_NAME,
            "source_id": item.get("id"),
            "symbol": str(item.get("symbol") or "").upper() or None,
            "name": item.get("name"),
            "image": item.get("image"),
            "current_price": cls._number(item.get("current_price")),
            "market_cap": cls._number(item.get("market_cap")),
            "market_cap_rank": cls._int(item.get("market_cap_rank")),
            "fully_diluted_valuation": cls._number(
                item.get("fully_diluted_valuation")
            ),
            "total_volume": cls._number(item.get("total_volume")),
            "high_24h": cls._number(item.get("high_24h")),
            "low_24h": cls._number(item.get("low_24h")),
            "price_change_24h": cls._number(item.get("price_change_24h")),
            "price_change_percentage_24h": cls._number(
                item.get("price_change_percentage_24h")
            ),
            "price_change_percentage_7d": cls._number(
                item.get("price_change_percentage_7d_in_currency")
            ),
            "price_change_percentage_30d": cls._number(
                item.get("price_change_percentage_30d_in_currency")
            ),
            "circulating_supply": cls._number(item.get("circulating_supply")),
            "total_supply": cls._number(item.get("total_supply")),
            "max_supply": cls._number(item.get("max_supply")),
            "ath": cls._number(item.get("ath")),
            "ath_change_percentage": cls._number(item.get("ath_change_percentage")),
            "atl": cls._number(item.get("atl")),
            "atl_change_percentage": cls._number(item.get("atl_change_percentage")),
            "last_updated": item.get("last_updated"),
        }

    @classmethod
    def normalize_coin(cls, item: Dict[str, Any]) -> Dict[str, Any]:
        market_data = item.get("market_data") or {}
        current_price = market_data.get("current_price") or {}
        market_cap = market_data.get("market_cap") or {}
        fdv = market_data.get("fully_diluted_valuation") or {}
        volume = market_data.get("total_volume") or {}
        supply = {
            "circulating": cls._number(market_data.get("circulating_supply")),
            "total": cls._number(market_data.get("total_supply")),
            "max": cls._number(market_data.get("max_supply")),
        }

        return {
            "source": SOURCE_NAME,
            "id": item.get("id"),
            "symbol": str(item.get("symbol") or "").upper() or None,
            "name": item.get("name"),
            "categories": item.get("categories") or [],
            "platforms": item.get("platforms") or {},
            "contract_address": item.get("contract_address"),
            "links": item.get("links") or {},
            "description": item.get("description") or {},
            "market": {
                "current_price": cls._number(current_price.get("usd")),
                "market_cap": cls._number(market_cap.get("usd")),
                "fully_diluted_valuation": cls._number(fdv.get("usd")),
                "total_volume_24h": cls._number(volume.get("usd")),
                "market_cap_rank": cls._int(item.get("market_cap_rank")),
                "last_updated": item.get("last_updated"),
            },
            "supply": supply,
        }


def get_client(**kwargs: Any) -> CoinGeckoClient:
    """Factory kept small so a future aggregator can inject configuration."""
    return CoinGeckoClient(**kwargs)


if __name__ == "__main__":
    # Safe smoke test: performs one read-only request only when explicitly run.
    client = get_client()
    print(json.dumps(client.markets(per_page=1), indent=2))
