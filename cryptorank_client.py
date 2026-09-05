"""
cryptorank_client.py

Read-only CryptoRank market-data adapter for the ATHENA / SMC watchlist
ecosystem.

Role in the system
-------------------
CoinGecko (coingecko_client.py) remains the baseline/reference market-data
source. CoinMarketCap (coinmarketcap_client.py) is an independent
corroborating source. This module -- CryptoRank -- is likewise an
INDEPENDENT, CORROBORATING source and must never override, replace, or
feed directly into:

  - SMC score / SMC direction
  - entry / stop loss / take profit
  - execution state / setup lifecycle / position lifecycle
  - BingX position state / trade classification

This client only fetches and normalizes CryptoRank market data. Any
reconciliation with CoinGecko/CoinMarketCap or use in scoring/execution
logic belongs to a future, separate aggregator layer -- not this file.

Scope (deliberately small)
---------------------------
  - GET /currencies (CryptoRank API V2)

No trading, order, position, or derivatives (OI/funding/liquidation)
endpoints are implemented here. No Telegram, no GitHub Actions, no
aggregation/reconciliation logic.

No external dependencies: uses only the Python standard library.

Importing this module performs NO network activity.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional


DEFAULT_BASE_URL = "https://api.cryptorank.io/v2"
DEFAULT_TIMEOUT = 10.0
CURRENCIES_ENDPOINT = "/currencies"
API_KEY_HEADER = "X-Api-Key"
API_KEY_ENV_VAR = "CRYPTORANK_API_KEY"

# Source tag used on every normalized record. Must never be overwritten.
SOURCE_NAME = "cryptorank"


class CryptoRankError(Exception):
    """
    Raised for any CryptoRank adapter failure:
    missing API key, connection failure, timeout, HTTP error,
    malformed JSON, or unexpected API response structure.

    Messages are constructed to never include the API key value.
    """


class CryptoRankClient:
    """
    Minimal, read-only CryptoRank API V2 REST client.

    This client is intentionally narrow in scope: it implements only the
    /currencies endpoint. It does not implement trading, derivatives,
    fundraising, or any other CryptoRank API surface.

    Example
    -------
        client = CryptoRankClient()
        rows = client.currencies(limit=50, fiat="USD")
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        """
        Parameters
        ----------
        api_key:
            Explicit CryptoRank API key. If omitted, falls back to the
            CRYPTORANK_API_KEY environment variable. The key is not
            validated against CryptoRank until a request is actually made.
        base_url:
            Base URL for the CryptoRank API. Configurable for
            testing/mocking or alternate gateways.
        timeout:
            Per-request timeout, in seconds.
        """
        self._api_key = api_key if api_key is not None else os.environ.get(API_KEY_ENV_VAR)
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def currencies(
        self,
        limit: int = 100,
        skip: int = 0,
        fiat: str = "USD",
        sort_by: str = "rank",
        sort_direction: str = "ASC",
        symbol: Optional[str] = None,
        category_id: Optional[Any] = None,
        include: Optional[str] = None,
        extra_params: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Fetch and normalize traded cryptocurrencies with market data from
        CryptoRank API V2's /currencies endpoint.

        Parameters
        ----------
        limit:
            Number of results to return.
        skip:
            Pagination offset.
        fiat:
            Fiat currency to express price/market-cap/volume figures in.
        sort_by:
            CryptoRank V2 sort field (mapped to the 'sortBy' query param).
        sort_direction:
            "ASC" or "DESC" (mapped to the 'sortDirection' query param).
        symbol:
            Optional single symbol filter (mapped to 'symbol').
        category_id:
            Optional category filter (mapped to 'categoryId').
        include:
            Optional comma-separated list of extra fields/relations to
            include, as supported by the CryptoRank V2 API (mapped to
            'include').
        extra_params:
            Optional additional raw query parameters, passed through
            as-is using the official V2 names (e.g. marketCapMin,
            marketCapMax, volume24hBaseMin, volume24hBaseMax, name).
            Not validated -- an escape hatch for narrowly scoped,
            already-documented V2 parameters only.

        Returns
        -------
        List of normalized asset dicts. Rows that cannot be parsed are
        skipped defensively rather than aborting the whole batch, since
        this is a corroborating data source, not a critical-path
        dependency.

        Raises
        ------
        CryptoRankError
            On missing API key, network failure, HTTP error, malformed
            JSON, or an unexpected top-level response structure.
        """
        params: Dict[str, Any] = {
            "limit": limit,
            "skip": skip,
            "fiat": fiat,
            "sortBy": sort_by,
            "sortDirection": sort_direction,
        }
        if symbol is not None:
            params["symbol"] = symbol
        if category_id is not None:
            params["categoryId"] = category_id
        if include is not None:
            params["include"] = include
        if extra_params:
            params.update(extra_params)

        payload = self._get(CURRENCIES_ENDPOINT, params)
        raw_rows = self._extract_data_list(payload)
        return [self._normalize_currency(row) for row in raw_rows]

    # ------------------------------------------------------------------
    # Internal HTTP plumbing
    # ------------------------------------------------------------------

    def _get(self, path: str, params: Dict[str, Any]) -> Any:
        """
        Perform a single GET request against the CryptoRank API and
        return the parsed JSON body. GET-only; no other HTTP verbs are
        supported by this client, in line with its read-only scope.
        """
        if not self._api_key:
            raise CryptoRankError(
                f"Missing CryptoRank API key. Set the {API_KEY_ENV_VAR} "
                "environment variable or pass api_key= explicitly."
            )

        query = urllib.parse.urlencode(params, doseq=True)
        url = f"{self._base_url}{path}?{query}"

        request = urllib.request.Request(
            url,
            method="GET",
            headers={
                API_KEY_HEADER: self._api_key,
                "Accept": "application/json",
            },
        )

        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw_body = response.read()
        except urllib.error.HTTPError as exc:
            body_snippet = self._safe_read_error_body(exc)
            raise CryptoRankError(
                f"CryptoRank HTTP error {exc.code} on {path}: {body_snippet}"
            ) from None
        except urllib.error.URLError as exc:
            raise CryptoRankError(
                f"CryptoRank connection failure on {path}: {exc.reason}"
            ) from None
        except TimeoutError:
            raise CryptoRankError(
                f"CryptoRank request to {path} timed out after {self._timeout}s"
            ) from None

        try:
            text_body = raw_body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CryptoRankError(
                f"CryptoRank response on {path} was not valid UTF-8: {exc}"
            ) from None

        try:
            parsed = json.loads(text_body)
        except json.JSONDecodeError as exc:
            raise CryptoRankError(
                f"CryptoRank response on {path} was not valid JSON: {exc}"
            ) from None

        if not isinstance(parsed, (dict, list)):
            raise CryptoRankError(
                f"CryptoRank response on {path} had unexpected top-level type "
                f"{type(parsed).__name__}; expected an object or list."
            )

        return parsed

    @staticmethod
    def _safe_read_error_body(exc: urllib.error.HTTPError) -> str:
        """
        Best-effort extraction of a short, safe error message from an
        HTTPError body. Never includes request headers, so the API key
        (sent only as a header) cannot leak through this path.
        """
        try:
            raw = exc.read()
            text = raw.decode("utf-8", errors="replace")
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    message = parsed.get("message") or parsed.get("error")
                    if message:
                        return str(message)[:300]
            except json.JSONDecodeError:
                pass
            return text[:300]
        except Exception:
            return "<no additional error detail available>"

    @staticmethod
    def _extract_data_list(payload: Any) -> List[Dict[str, Any]]:
        """
        Pull the list of currency rows out of a CryptoRank /currencies
        response, raising a clear CryptoRankError if the shape isn't what
        is expected instead of failing with a raw KeyError/TypeError.

        CryptoRank V2 responses are documented/observed to wrap results
        in a top-level 'data' field (i.e. {"data": [...]}). Defensively
        also accept a bare top-level list, in case a given deployment
        returns the array directly.
        """
        if isinstance(payload, list):
            data = payload
        elif isinstance(payload, dict):
            data = payload.get("data")
            if data is None:
                raise CryptoRankError(
                    "CryptoRank response missing expected 'data' field."
                )
        else:
            raise CryptoRankError(
                f"CryptoRank response had unexpected type {type(payload).__name__}."
            )

        if not isinstance(data, list):
            raise CryptoRankError(
                f"CryptoRank 'data' field had unexpected type {type(data).__name__}; "
                "expected a list."
            )
        return data

    # ------------------------------------------------------------------
    # Normalization
    # ------------------------------------------------------------------

    @staticmethod
    def _to_float(value: Any) -> Optional[float]:
        """
        Safely coerce a value to float. CryptoRank V2 may represent
        numeric fields as strings, so this accepts str, int, and float.
        """
        if value is None:
            return None
        if isinstance(value, bool):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _to_int(value: Any) -> Optional[int]:
        """
        Safely coerce a value to int, tolerating numeric strings
        (including ones formatted as floats, e.g. "12.0").
        """
        if value is None:
            return None
        if isinstance(value, bool):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            pass
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None

    def _normalize_currency(self, row: Dict[str, Any]) -> Dict[str, Any]:
        """
        Convert one raw CryptoRank /currencies row into the stable
        ATHENA-friendly schema. Missing values become None rather than
        being fabricated. Defensive against malformed individual rows --
        never raises; returns best-effort None-filled fields instead.

        Market fields (price, marketCap, volume24h, fullyDilutedValuation,
        percentChange, lastUpdated, circulatingSupply, totalSupply,
        maxSupply) live directly on the currency row in the current
        CryptoRank V2 /currencies response -- there is no nested
        fiat-keyed "values" object.
        """
        if not isinstance(row, dict):
            row = {}

        percent_change_block = row.get("percentChange")
        percent_change_24h = None
        if isinstance(percent_change_block, dict):
            percent_change_24h = self._to_float(percent_change_block.get("h24"))

        return {
            "source": SOURCE_NAME,
            "source_id": self._to_int(row.get("id")),
            "symbol": row.get("symbol"),
            "name": row.get("name"),
            "slug": row.get("slug") or row.get("key"),
            "rank": self._to_int(row.get("rank")),
            "price": self._to_float(row.get("price")),
            "market_cap": self._to_float(row.get("marketCap")),
            "volume_24h": self._to_float(row.get("volume24h")),
            "fully_diluted_market_cap": self._to_float(row.get("fullyDilutedValuation")),
            "circulating_supply": self._to_float(row.get("circulatingSupply")),
            "total_supply": self._to_float(row.get("totalSupply")),
            "max_supply": self._to_float(row.get("maxSupply")),
            "percent_change_24h": percent_change_24h,
            "last_updated": row.get("lastUpdated"),
        }


def get_client(
    api_key: Optional[str] = None,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = DEFAULT_TIMEOUT,
) -> CryptoRankClient:
    """
    Small factory for constructing a CryptoRankClient. Provided for
    consistency with how the other market-data adapters (e.g.
    CoinGecko, CoinMarketCap) expose their own get_client() factories.
    """
    return CryptoRankClient(api_key=api_key, base_url=base_url, timeout=timeout)


if __name__ == "__main__":
    # Manual smoke test only -- never runs on import.
    # Requires CRYPTORANK_API_KEY to be set in the environment.
    _client = get_client()
    try:
        _rows = _client.currencies(limit=5)
        for _row in _rows:
            print(_row)
    except CryptoRankError as _exc:
        print(f"CryptoRank smoke test failed: {_exc}")
