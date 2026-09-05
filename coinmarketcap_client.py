"""
coinmarketcap_client.py

Read-only CoinMarketCap market-data adapter for the ATHENA / SMC watchlist
ecosystem.

Role in the system
-------------------
CoinGecko (coingecko_client.py) is the baseline/reference market-data
source. This module is an INDEPENDENT, CORROBORATING source and must never
override, replace, or feed directly into:

  - SMC score / SMC direction
  - entry / stop loss / take profit
  - execution state / setup lifecycle / position lifecycle
  - BingX position state / trade classification

This client only fetches and normalizes CoinMarketCap market data. Any
reconciliation with CoinGecko or use in scoring/execution logic belongs to
a future, separate aggregator layer -- not this file.

Scope (deliberately small)
---------------------------
  - GET /v3/cryptocurrency/listings/latest

No trading, order, position, or derivatives (OI/funding/liquidation)
endpoints are implemented here. Derivatives/microstructure data belongs to
a separate layer.

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


DEFAULT_BASE_URL = "https://pro-api.coinmarketcap.com"
DEFAULT_TIMEOUT = 10.0
LISTINGS_ENDPOINT = "/v3/cryptocurrency/listings/latest"
API_KEY_HEADER = "X-CMC_PRO_API_KEY"
API_KEY_ENV_VAR = "CMC_API_KEY"

# Source tag used on every normalized record. Must never be overwritten.
SOURCE_NAME = "coinmarketcap"


class CoinMarketCapError(Exception):
    """
    Raised for any CoinMarketCap adapter failure:
    missing API key, connection failure, timeout, HTTP error,
    malformed JSON, or unexpected response structure.

    Messages are constructed to never include the API key value.
    """


class CoinMarketCapClient:
    """
    Minimal, read-only CoinMarketCap REST client.

    This client is intentionally narrow in scope. It does not implement a
    full CMC SDK, and it does not implement derivatives, trading, or
    account/portfolio endpoints.

    Example
    -------
        client = CoinMarketCapClient()
        rows = client.listings(limit=50, convert="USD")
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
            Explicit CMC API key. If omitted, falls back to the
            CMC_API_KEY environment variable. The key is not validated
            against CMC until a request is actually made.
        base_url:
            Base URL for the CMC API. Configurable for testing/mocking
            or alternate gateways.
        timeout:
            Per-request timeout, in seconds.
        """
        self._api_key = api_key if api_key is not None else os.environ.get(API_KEY_ENV_VAR)
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def listings(
        self,
        start: int = 1,
        limit: int = 100,
        convert: str = "USD",
        sort: str = "market_cap",
        sort_dir: Optional[str] = None,
        extra_params: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Fetch and normalize the latest cryptocurrency market listings.

        Parameters
        ----------
        start:
            1-based offset into the ranked list (CMC convention).
        limit:
            Number of results to return.
        convert:
            Fiat/crypto symbol to convert quote values into (e.g. "USD").
        sort:
            CMC sort field (e.g. "market_cap", "volume_24h", "percent_change_24h").
        sort_dir:
            Optional explicit sort direction: "asc" or "desc".
        extra_params:
            Optional additional raw query parameters to pass through to
            CMC as-is (escape hatch; not validated).

        Returns
        -------
        List of normalized asset dicts. See module docstring / README for
        the normalized schema. Never raises on a per-row basis -- rows
        that cannot be parsed are skipped defensively rather than
        aborting the whole batch, since this is a corroborating data
        source, not a critical-path dependency.

        Raises
        ------
        CoinMarketCapError
            On missing API key, network failure, HTTP error, malformed
            JSON, or an unexpected top-level response structure.
        """
        params: Dict[str, Any] = {
            "start": start,
            "limit": limit,
            "convert": convert,
            "sort": sort,
        }
        if sort_dir is not None:
            params["sort_dir"] = sort_dir
        if extra_params:
            params.update(extra_params)

        payload = self._get(LISTINGS_ENDPOINT, params)
        raw_rows = self._extract_data_list(payload)
        return [self._normalize_listing(row, convert=convert) for row in raw_rows]

    # ------------------------------------------------------------------
    # Internal HTTP plumbing
    # ------------------------------------------------------------------

    def _get(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Perform a single GET request against the CMC API and return the
        parsed JSON body. GET-only; no other HTTP verbs are supported by
        this client, in line with its read-only scope.
        """
        if not self._api_key:
            raise CoinMarketCapError(
                f"Missing CoinMarketCap API key. Set the {API_KEY_ENV_VAR} "
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
            raise CoinMarketCapError(
                f"CoinMarketCap HTTP error {exc.code} on {path}: {body_snippet}"
            ) from None
        except urllib.error.URLError as exc:
            raise CoinMarketCapError(
                f"CoinMarketCap connection failure on {path}: {exc.reason}"
            ) from None
        except TimeoutError:
            raise CoinMarketCapError(
                f"CoinMarketCap request to {path} timed out after {self._timeout}s"
            ) from None

        try:
            text_body = raw_body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CoinMarketCapError(
                f"CoinMarketCap response on {path} was not valid UTF-8: {exc}"
            ) from None

        try:
            parsed = json.loads(text_body)
        except json.JSONDecodeError as exc:
            raise CoinMarketCapError(
                f"CoinMarketCap response on {path} was not valid JSON: {exc}"
            ) from None

        if not isinstance(parsed, dict):
            raise CoinMarketCapError(
                f"CoinMarketCap response on {path} had unexpected top-level type "
                f"{type(parsed).__name__}; expected an object."
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
                    status = parsed.get("status", {})
                    if isinstance(status, dict) and status.get("error_message"):
                        return str(status["error_message"])[:300]
            except json.JSONDecodeError:
                pass
            return text[:300]
        except Exception:
            return "<no additional error detail available>"

    @staticmethod
    def _extract_data_list(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Pull the list of asset rows out of a CMC listings response,
        raising a clear CoinMarketCapError if the shape isn't what's
        expected instead of failing with a raw KeyError/TypeError.
        """
        status = payload.get("status")
        if isinstance(status, dict) and status.get("error_code") not in (0, None):
            error_message = status.get("error_message") or "unknown CMC API error"
            raise CoinMarketCapError(f"CoinMarketCap API error: {error_message}")

        data = payload.get("data")
        if data is None:
            raise CoinMarketCapError(
                "CoinMarketCap response missing expected 'data' field."
            )
        if not isinstance(data, list):
            raise CoinMarketCapError(
                f"CoinMarketCap 'data' field had unexpected type {type(data).__name__}; "
                "expected a list."
            )
        return data

    # ------------------------------------------------------------------
    # Normalization
    # ------------------------------------------------------------------

    @staticmethod
    def _to_float(value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _to_int(value: Any) -> Optional[int]:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _select_quote(row: Dict[str, Any], convert: str) -> Dict[str, Any]:
        """
        CMC V3 represents 'quote' as a LIST of quote objects (each tagged
        with its own 'symbol', e.g. "USD"), not a dict keyed by currency
        code as in older API versions. Find and return the quote object
        matching the requested convert currency (case-insensitive).

        Returns an empty dict if the quote list is missing, malformed,
        or contains no matching entry -- callers then get None for every
        quote-derived field rather than raising.
        """
        quote_field = row.get("quote")
        if not isinstance(quote_field, list):
            return {}

        target = convert.strip().upper() if isinstance(convert, str) else None
        if not target:
            return {}

        for entry in quote_field:
            if not isinstance(entry, dict):
                continue
            entry_symbol = entry.get("symbol")
            if isinstance(entry_symbol, str) and entry_symbol.strip().upper() == target:
                return entry

        return {}

    def _normalize_listing(self, row: Dict[str, Any], convert: str) -> Dict[str, Any]:
        """
        Convert one raw CMC listings row into the stable ATHENA-friendly
        schema. Missing values become None rather than being fabricated.
        """
        if not isinstance(row, dict):
            row = {}

        quote = self._select_quote(row, convert)

        return {
            "source": SOURCE_NAME,
            "source_id": self._to_int(row.get("id")),
            "symbol": row.get("symbol"),
            "name": row.get("name"),
            "slug": row.get("slug"),
            "market_cap": self._to_float(quote.get("market_cap")),
            "market_cap_rank": self._to_int(row.get("cmc_rank")),
            "price": self._to_float(quote.get("price")),
            "volume_24h": self._to_float(quote.get("volume_24h")),
            "circulating_supply": self._to_float(row.get("circulating_supply")),
            "total_supply": self._to_float(row.get("total_supply")),
            "max_supply": self._to_float(row.get("max_supply")),
            "fully_diluted_market_cap": self._to_float(quote.get("fully_diluted_market_cap")),
            "percent_change_1h": self._to_float(quote.get("percent_change_1h")),
            "percent_change_24h": self._to_float(quote.get("percent_change_24h")),
            "percent_change_7d": self._to_float(quote.get("percent_change_7d")),
            "last_updated": quote.get("last_updated") or row.get("last_updated"),
        }


def get_client(
    api_key: Optional[str] = None,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = DEFAULT_TIMEOUT,
) -> CoinMarketCapClient:
    """
    Small factory for constructing a CoinMarketCapClient. Provided for
    consistency with how a future aggregator layer is expected to obtain
    market-data clients.
    """
    return CoinMarketCapClient(api_key=api_key, base_url=base_url, timeout=timeout)


if __name__ == "__main__":
    # Manual smoke test only -- never runs on import.
    # Requires CMC_API_KEY to be set in the environment.
    _client = get_client()
    try:
        _rows = _client.listings(limit=5)
        for _row in _rows:
            print(_row)
    except CoinMarketCapError as _exc:
        print(f"CoinMarketCap smoke test failed: {_exc}")
