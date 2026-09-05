"""
sosovalue_client.py

Read-only SoSoValue market-intelligence adapter for the ATHENA / SMC
watchlist ecosystem.

Role in the system
-------------------
SoSoValue is an INDEPENDENT, READ-ONLY CORROBORATING data source, exactly
like CoinGecko (coingecko_client.py), CoinMarketCap (coinmarketcap_client.py),
and CryptoRank (cryptorank_client.py). This module:

  - does NOT modify SMC score, SMC direction, entry, stop loss, or take
    profit
  - does NOT modify execution state, setup lifecycle, or position lifecycle
  - does NOT modify BingX position state or trade classification
  - does NOT place, cancel, close, or modify any trade
  - does NOT modify leverage or margin
  - does NOT monitor or control positions
  - does NOT send Telegram messages or interact with GitHub Actions
  - does NOT contain any derivatives logic (no open interest, funding
    rates, liquidations, basis, or futures positioning)
  - does NOT aggregate or reconcile CoinGecko / CoinMarketCap / CryptoRank
    data; a future, separate aggregator layer owns cross-source
    reconciliation

This client only fetches and normalizes SoSoValue data. It is a pure
source adapter.

Endpoint actually used (re-verified directly against the current official
SoSoValue API documentation at https://sosovalue.gitbook.io/soso-value-api-doc/
immediately before this revision):

    GET /etfs/summary-history   (SoSoValue API v1, under /openapi/v1)
    Full path: https://openapi.sosovalue.com/openapi/v1/etfs/summary-history

This is the "2.1 ETF Summary History" endpoint, documented under the ETF
module of the v1 API. It returns aggregate daily crypto-ETF market-flow
data (total net inflow/outflow, total value traded, total net assets, and
cumulative net inflow) for a given crypto asset + country/market.

Per the current official documentation, SoSoValue's v1 API also exposes a
"Currency" module (/currencies, .../market-snapshot, .../klines, etc.)
covering coin-level price/market data, plus a separate Market Data API v3
(/openapi/v3/..., US equities & perpetual futures). Neither is implemented
here: this file is deliberately scoped to the ETF-flow endpoint only, to
stay minimal and focused on data that is genuinely *independent* of what
CoinGecko/CoinMarketCap/CryptoRank already provide.

Rate limit note:
The official SoSoValue documentation states that Public API users on the
Beta plan have an approximate limit of 30 calls/minute, varying with
traffic. The Demo Plan has a documented limit of 20 requests/minute and
1,000 total monthly calls. Paid/Pro API limits depend on the subscribed
plan. This module does not enforce client-side rate limiting.

No external dependencies: uses only the Python standard library.

Importing this module performs NO network activity.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Union


DEFAULT_BASE_URL = "https://openapi.sosovalue.com/openapi/v1"
DEFAULT_TIMEOUT = 10.0
ETF_SUMMARY_HISTORY_ENDPOINT = "/etfs/summary-history"
API_KEY_HEADER = "x-soso-api-key"
API_KEY_ENV_VAR = "SOSOVALUE_API_KEY"

# Source tag used on every normalized record. Must never be overwritten.
SOURCE_NAME = "sosovalue"

# Success business code per the official SoSoValue response envelope:
# {"code": 0, "message": "success", "data": ...}
SUCCESS_CODE = 0


class SoSoValueError(Exception):
    """
    Raised for any SoSoValue adapter failure:
    missing API key, connection failure, timeout, HTTP error,
    malformed JSON, an API-level error code, or an unexpected response
    structure.

    Messages are constructed to never include the API key value.
    """


class SoSoValueClient:
    """
    Minimal, read-only SoSoValue API v1 REST client.

    This client is intentionally narrow in scope: it implements only the
    ETF Summary History endpoint. It does not implement coin/currency
    data, trading, derivatives, fundraising, macro, or any other
    SoSoValue API surface.

    Example
    -------
        client = SoSoValueClient()
        rows = client.etf_summary_history(symbol="BTC", country_code="US")
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
            Explicit SoSoValue API key. If omitted, falls back to the
            SOSOVALUE_API_KEY environment variable. The key is not
            validated against SoSoValue until a request is actually made.
        base_url:
            Base URL for the SoSoValue v1 API. Configurable for
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

    def etf_summary_history(
        self,
        symbol: str,
        country_code: str = "US",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: Optional[int] = None,
        extra_params: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Fetch and normalize aggregate daily crypto-ETF flow data from
        SoSoValue's GET /etfs/summary-history endpoint.

        Parameters
        ----------
        symbol:
            Currency symbol, e.g. "BTC", "ETH" (documented as Required).
        country_code:
            Market/country code, e.g. "US", "HK" (documented as Required).
        start_date:
            Optional start date (documented: only the most recent 1
            month of range is supported).
        end_date:
            Optional end date (same documented 1-month range
            restriction).
        limit:
            Optional number of records to request (documented default
            50, documented max 300).
        extra_params:
            Optional additional raw query parameters, passed through
            as-is. Not validated -- an escape hatch for already-documented
            parameters only; do not use it to reach undocumented fields.

        Returns
        -------
        List of normalized ETF-flow dicts, most-recent-first (per the
        documented "reverse chronological order" behavior of the API).
        Rows that cannot be parsed are skipped defensively rather than
        aborting the whole batch, since this is a corroborating data
        source, not a critical-path dependency.

        Raises
        ------
        SoSoValueError
            On missing API key, network failure, HTTP error, malformed
            JSON, an API-level error code, or an unexpected response
            structure.
        """
        params: Dict[str, Any] = {
            "symbol": symbol,
            "country_code": country_code,
        }
        if start_date is not None:
            params["start_date"] = start_date
        if end_date is not None:
            params["end_date"] = end_date
        if limit is not None:
            params["limit"] = limit
        if extra_params:
            params.update(extra_params)

        payload = self._get(ETF_SUMMARY_HISTORY_ENDPOINT, params)
        raw_rows = self._extract_data_list(payload)
        return [self._normalize_etf_summary_row(row, symbol=symbol, country_code=country_code) for row in raw_rows]

    # ------------------------------------------------------------------
    # Internal HTTP plumbing
    # ------------------------------------------------------------------

    def _get(self, path: str, params: Dict[str, Any]) -> Union[Dict[str, Any], List[Any]]:
        """
        Perform a single GET request against the SoSoValue API and return
        the parsed JSON body. GET-only; no other HTTP verbs are supported
        by this client, in line with its read-only scope.

        The official "Response Format" page documents a unified envelope
        ({"code": 0, "message": "success", "data": ...}) for all
        endpoints, but the specific "2.1 ETF Summary History" doc page
        shows its response example as a bare top-level JSON array. Both
        shapes are accepted here defensively; _extract_data_list resolves
        whichever one is actually returned.
        """
        if not self._api_key:
            raise SoSoValueError(
                f"Missing SoSoValue API key. Set the {API_KEY_ENV_VAR} "
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
            raise SoSoValueError(
                f"SoSoValue HTTP error {exc.code} on {path}: {body_snippet}"
            ) from None
        except urllib.error.URLError as exc:
            raise SoSoValueError(
                f"SoSoValue connection failure on {path}: {exc.reason}"
            ) from None
        except TimeoutError:
            raise SoSoValueError(
                f"SoSoValue request to {path} timed out after {self._timeout}s"
            ) from None

        try:
            text_body = raw_body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SoSoValueError(
                f"SoSoValue response on {path} was not valid UTF-8: {exc}"
            ) from None

        try:
            parsed = json.loads(text_body)
        except json.JSONDecodeError as exc:
            raise SoSoValueError(
                f"SoSoValue response on {path} was not valid JSON: {exc}"
            ) from None

        if not isinstance(parsed, (dict, list)):
            raise SoSoValueError(
                f"SoSoValue response on {path} had unexpected top-level type "
                f"{type(parsed).__name__}; expected an object or a list."
            )

        if isinstance(parsed, dict):
            code = parsed.get("code")
            if code is not None and code != SUCCESS_CODE:
                message = parsed.get("message") or "unknown SoSoValue API error"
                raise SoSoValueError(f"SoSoValue API error {code} on {path}: {message}")

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
                if isinstance(parsed, dict) and parsed.get("message"):
                    return str(parsed["message"])[:300]
            except json.JSONDecodeError:
                pass
            return text[:300]
        except Exception:
            return "<no additional error detail available>"

    @staticmethod
    def _extract_data_list(payload: Union[Dict[str, Any], List[Any]]) -> List[Dict[str, Any]]:
        """
        Pull the list of ETF-summary rows out of a SoSoValue response,
        raising a clear SoSoValueError if the shape isn't what is
        expected instead of failing with a raw KeyError/TypeError.

        Accepts either:
          - the unified envelope {"code": 0, "message": "success",
            "data": [...]} documented on the general "Response Format"
            page, or
          - a bare top-level JSON array, as shown in the specific
            "2.1 ETF Summary History" endpoint doc's response example.
        """
        if isinstance(payload, list):
            return payload

        data = payload.get("data")
        if data is None:
            # Per the documented "Empty data response" shape, data can
            # legitimately be null -- treat that as an empty result set,
            # not an error.
            return []
        if not isinstance(data, list):
            raise SoSoValueError(
                f"SoSoValue 'data' field had unexpected type {type(data).__name__}; "
                "expected a list for the ETF summary-history endpoint."
            )
        return data

    # ------------------------------------------------------------------
    # Normalization
    # ------------------------------------------------------------------

    @staticmethod
    def _to_float(value: Any) -> Optional[float]:
        if value is None:
            return None
        if isinstance(value, bool):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _normalize_etf_summary_row(
        self,
        row: Dict[str, Any],
        symbol: str,
        country_code: str,
    ) -> Dict[str, Any]:
        """
        Convert one raw /etfs/summary-history row into a stable,
        ATHENA-friendly dictionary. Only fields the endpoint actually
        documents are included -- nothing is forced into a
        CoinGecko/CMC/CryptoRank-shaped schema. Missing values become
        None rather than being fabricated. Defensive against malformed
        individual rows -- never raises; returns best-effort None-filled
        fields instead.
        """
        if not isinstance(row, dict):
            row = {}

        return {
            "source": SOURCE_NAME,
            "symbol": symbol,
            "country_code": country_code,
            "date": row.get("date"),
            "total_net_inflow": self._to_float(row.get("total_net_inflow")),
            "total_value_traded": self._to_float(row.get("total_value_traded")),
            "total_net_assets": self._to_float(row.get("total_net_assets")),
            "cum_net_inflow": self._to_float(row.get("cum_net_inflow")),
        }


def get_client(
    api_key: Optional[str] = None,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = DEFAULT_TIMEOUT,
) -> SoSoValueClient:
    """
    Small factory for constructing a SoSoValueClient. Provided for
    consistency with how the other market-data adapters (CoinGecko,
    CoinMarketCap, CryptoRank) expose their own get_client() factories.
    """
    return SoSoValueClient(api_key=api_key, base_url=base_url, timeout=timeout)


if __name__ == "__main__":
    # Manual smoke test only -- never runs on import.
    # Requires SOSOVALUE_API_KEY to be set in the environment.
    _client = get_client()
    try:
        _rows = _client.etf_summary_history(symbol="BTC", country_code="US", limit=5)
        for _row in _rows:
            print(_row)
    except SoSoValueError as _exc:
        print(f"SoSoValue smoke test failed: {_exc}")
