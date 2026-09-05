"""
geckoterminal_client.py

Read-only GeckoTerminal on-chain DEX pool/liquidity adapter for the
ATHENA / SMC watchlist ecosystem.

Role in the system
-------------------
GeckoTerminal is an INDEPENDENT, READ-ONLY CORROBORATING data source,
alongside CoinGecko (coingecko_client.py), CoinMarketCap
(coinmarketcap_client.py), CryptoRank (cryptorank_client.py), and
SoSoValue (sosovalue_client.py). This module:

  - does NOT modify SMC score, SMC direction, entry, stop loss, or take
    profit
  - does NOT modify execution state, setup lifecycle, or position lifecycle
  - does NOT modify BingX position state or trade classification
  - does NOT place, cancel, close, or modify any trade
  - does NOT modify leverage or margin
  - does NOT contain derivatives logic (no open interest, funding rates,
    liquidations, basis, or futures positioning)
  - does NOT send Telegram messages or interact with GitHub Actions
  - does NOT aggregate or reconcile CoinGecko / CoinMarketCap / CryptoRank /
    SoSoValue data; a future, separate aggregator layer owns cross-source
    reconciliation

This client only fetches and normalizes GeckoTerminal on-chain DEX pool
data. It is a pure source adapter.

Endpoint actually used (verified directly against the current official
GeckoTerminal API documentation at https://apiguide.geckoterminal.com/
immediately before writing this file):

    GET /networks/{network}/pools/{address}

Root URL: https://api.geckoterminal.com/api/v2 (the free, public
GeckoTerminal API -- distinct from CoinGecko's keyed /onchain proxy).

Per the official "Authentication" page: "There is no authentication
needed at the moment. However you are subject to a universal rate
limit." Per the official FAQ page: "The current rate limit for the
Public API is set at 30 calls per minute." This module does not enforce
client-side rate limiting.

Scope (deliberately small)
---------------------------
This is the ONLY endpoint implemented: a single-pool lookup by network +
pool contract address, returning on-chain DEX liquidity/market context
(price, reserve/liquidity in USD, volume, transaction counts, price
change, pool creation time) that is genuinely complementary to --
not a duplicate of -- CoinGecko/CoinMarketCap/CryptoRank/SoSoValue.

No trading, no derivatives (no open interest/funding/liquidations), no
broad SDK. Endpoints such as /networks, /networks/{network}/dexes,
/networks/{network}/trending_pools, /search/pools, token endpoints, and
OHLCV candles are all documented but are NOT implemented here -- this
file is intentionally scoped to the single pool-by-address lookup.

No external dependencies: uses only the Python standard library.

Importing this module performs NO network activity.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional


DEFAULT_BASE_URL = "https://api.geckoterminal.com/api/v2"
DEFAULT_TIMEOUT = 10.0
POOL_BY_ADDRESS_ENDPOINT_TEMPLATE = "/networks/{network}/pools/{address}"

# Source tag used on every normalized record. Must never be overwritten.
SOURCE_NAME = "geckoterminal"

# Documented interval-keyed attribute groups on the pool object.
_INTERVAL_NUMERIC_GROUPS = ("volume_usd", "price_change_percentage")
_INTERVAL_TRANSACTION_GROUP = "transactions"
_TRANSACTION_COUNT_FIELDS = ("buys", "sells", "buyers", "sellers")


class GeckoTerminalError(Exception):
    """
    Raised for any GeckoTerminal adapter failure:
    HTTP errors, connection failures, timeouts, malformed JSON, or an
    unexpected response structure.

    The public GeckoTerminal API requires no credentials, so there is
    nothing secret to leak, but error messages are still kept free of
    raw request internals as a matter of consistent hygiene with the
    other source adapters.
    """


class GeckoTerminalClient:
    """
    Minimal, read-only GeckoTerminal public API client.

    This client is intentionally narrow in scope: it implements only the
    "pool by address" endpoint. It does not implement trading,
    derivatives, token search, trending pools, or any other
    GeckoTerminal API surface.

    Example
    -------
        client = GeckoTerminalClient()
        pool = client.pool_by_address(network="eth", address="0x...")
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        """
        Parameters
        ----------
        base_url:
            Base URL for the GeckoTerminal public API. Configurable for
            testing/mocking or alternate gateways.
        timeout:
            Per-request timeout, in seconds.

        Note: the GeckoTerminal public API requires no API key (verified
        against the official "Authentication" doc page), so there is no
        api_key parameter here.
        """
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def pool_by_address(
        self,
        network: str,
        address: str,
        include: Optional[str] = None,
        extra_params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Fetch and normalize a single on-chain DEX pool by network + pool
        contract address, via GET /networks/{network}/pools/{address}.

        Parameters
        ----------
        network:
            GeckoTerminal network id (e.g. "eth", "solana", "bsc").
            Not validated against /networks here -- pass a known id.
        address:
            Pool contract address on that network.
        include:
            Optional comma-separated related-resource list, per the
            official API (e.g. "base_token,quote_token,dex"). When
            provided, matching token/dex identity fields are resolved
            into the normalized output from the response's top-level
            "included" array.
        extra_params:
            Optional additional raw query parameters, passed through
            as-is. Not validated -- an escape hatch for already-documented
            parameters only.

        Returns
        -------
        A single normalized dict describing the pool. Never fabricates
        missing fields -- uses None where the API did not return a value.

        Raises
        ------
        GeckoTerminalError
            On network failure, HTTP error, malformed JSON, or an
            unexpected response structure.
        """
        path = POOL_BY_ADDRESS_ENDPOINT_TEMPLATE.format(
            network=urllib.parse.quote(str(network), safe=""),
            address=urllib.parse.quote(str(address), safe=""),
        )

        params: Dict[str, Any] = {}
        if include is not None:
            params["include"] = include
        if extra_params:
            params.update(extra_params)

        payload = self._get(path, params)
        pool_resource = self._extract_pool_resource(payload)
        included = self._extract_included(payload)
        return self._normalize_pool(pool_resource, network=network, included=included)

    # ------------------------------------------------------------------
    # Internal HTTP plumbing
    # ------------------------------------------------------------------

    def _get(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Perform a single GET request against the GeckoTerminal API and
        return the parsed JSON body. GET-only; no other HTTP verbs are
        supported by this client, in line with its read-only scope.
        """
        query = urllib.parse.urlencode(params, doseq=True)
        url = f"{self._base_url}{path}"
        if query:
            url = f"{url}?{query}"

        request = urllib.request.Request(
            url,
            method="GET",
            headers={"Accept": "application/json"},
        )

        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw_body = response.read()
        except urllib.error.HTTPError as exc:
            body_snippet = self._safe_read_error_body(exc)
            raise GeckoTerminalError(
                f"GeckoTerminal HTTP error {exc.code} on {path}: {body_snippet}"
            ) from None
        except urllib.error.URLError as exc:
            raise GeckoTerminalError(
                f"GeckoTerminal connection failure on {path}: {exc.reason}"
            ) from None
        except TimeoutError:
            raise GeckoTerminalError(
                f"GeckoTerminal request to {path} timed out after {self._timeout}s"
            ) from None

        try:
            text_body = raw_body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise GeckoTerminalError(
                f"GeckoTerminal response on {path} was not valid UTF-8: {exc}"
            ) from None

        try:
            parsed = json.loads(text_body)
        except json.JSONDecodeError as exc:
            raise GeckoTerminalError(
                f"GeckoTerminal response on {path} was not valid JSON: {exc}"
            ) from None

        if not isinstance(parsed, dict):
            raise GeckoTerminalError(
                f"GeckoTerminal response on {path} had unexpected top-level type "
                f"{type(parsed).__name__}; expected an object."
            )

        return parsed

    @staticmethod
    def _safe_read_error_body(exc: urllib.error.HTTPError) -> str:
        """
        Best-effort extraction of a short error message from an
        HTTPError body, without exposing raw request internals.
        """
        try:
            raw = exc.read()
            text = raw.decode("utf-8", errors="replace")
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    errors = parsed.get("errors")
                    if isinstance(errors, list) and errors:
                        first = errors[0]
                        if isinstance(first, dict):
                            detail = first.get("detail") or first.get("title")
                            if detail:
                                return str(detail)[:300]
            except json.JSONDecodeError:
                pass
            return text[:300]
        except Exception:
            return "<no additional error detail available>"

    @staticmethod
    def _extract_pool_resource(payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Pull the single pool JSON:API resource object out of the
        response, raising a clear GeckoTerminalError if the shape isn't
        what's expected instead of failing with a raw KeyError/TypeError.
        """
        data = payload.get("data")
        if not isinstance(data, dict):
            raise GeckoTerminalError(
                "GeckoTerminal pool response missing expected object 'data' field "
                f"(got {type(data).__name__})."
            )
        if data.get("type") != "pool":
            raise GeckoTerminalError(
                f"GeckoTerminal response 'data.type' was {data.get('type')!r}; "
                "expected 'pool'."
            )
        return data

    @staticmethod
    def _extract_included(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Pull the optional top-level 'included' array (related token/dex
        resources, present when 'include' was requested). Defensive:
        returns an empty list if missing or malformed rather than
        raising, since 'included' is optional.
        """
        included = payload.get("included")
        if isinstance(included, list):
            return [item for item in included if isinstance(item, dict)]
        return []

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

    @staticmethod
    def _to_int(value: Any) -> Optional[int]:
        if value is None:
            return None
        if isinstance(value, bool):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _normalize_interval_group(self, raw: Any) -> Optional[Dict[str, Optional[float]]]:
        """
        Normalize a documented interval-keyed numeric group (e.g.
        volume_usd / price_change_percentage), which the API returns as
        an object keyed by interval ("m5", "m15", "m30", "h1", "h6",
        "h24") with string numeric values. Unknown intervals are passed
        through as-is; missing/malformed input returns None rather than
        being fabricated.
        """
        if not isinstance(raw, dict):
            return None
        return {str(key): self._to_float(value) for key, value in raw.items()}

    def _normalize_transactions_group(self, raw: Any) -> Optional[Dict[str, Dict[str, Optional[int]]]]:
        """
        Normalize the documented 'transactions' interval-keyed group,
        each interval containing integer buy/sell/buyer/seller counts.
        """
        if not isinstance(raw, dict):
            return None
        normalized: Dict[str, Dict[str, Optional[int]]] = {}
        for interval, counts in raw.items():
            if not isinstance(counts, dict):
                continue
            normalized[str(interval)] = {
                field: self._to_int(counts.get(field)) for field in _TRANSACTION_COUNT_FIELDS
            }
        return normalized

    @staticmethod
    def _find_included_token(
        included: List[Dict[str, Any]],
        relationships: Dict[str, Any],
        relationship_key: str,
    ) -> Dict[str, Any]:
        """
        Resolve a relationship (e.g. 'base_token' / 'quote_token') to its
        matching entry in the 'included' array, when the caller requested
        it via the 'include' query parameter. Returns an empty dict if
        not present/resolvable -- callers then get None for every
        token-derived field rather than raising.
        """
        relationship = relationships.get(relationship_key)
        if not isinstance(relationship, dict):
            return {}
        rel_data = relationship.get("data")
        if not isinstance(rel_data, dict):
            return {}
        target_id = rel_data.get("id")
        target_type = rel_data.get("type")
        if target_id is None:
            return {}

        for item in included:
            if item.get("type") == target_type and item.get("id") == target_id:
                return item
        return {}

    def _normalize_pool(
        self,
        pool_resource: Dict[str, Any],
        network: str,
        included: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        Convert one raw GeckoTerminal pool JSON:API resource into a
        stable, ATHENA-friendly dictionary. Only fields the endpoint
        actually documents are included -- nothing is forced into a
        CoinGecko/CMC/CryptoRank/SoSoValue-shaped schema. Missing values
        become None rather than being fabricated.
        """
        attributes = pool_resource.get("attributes")
        if not isinstance(attributes, dict):
            attributes = {}

        relationships = pool_resource.get("relationships")
        if not isinstance(relationships, dict):
            relationships = {}

        dex_relationship = relationships.get("dex")
        dex_id = None
        if isinstance(dex_relationship, dict):
            dex_data = dex_relationship.get("data")
            if isinstance(dex_data, dict):
                dex_id = dex_data.get("id")

        base_token = self._find_included_token(included, relationships, "base_token")
        quote_token = self._find_included_token(included, relationships, "quote_token")

        def _token_fields(token_resource: Dict[str, Any]) -> Dict[str, Optional[str]]:
            token_attrs = token_resource.get("attributes")
            if not isinstance(token_attrs, dict):
                token_attrs = {}
            return {
                "address": token_attrs.get("address"),
                "symbol": token_attrs.get("symbol"),
                "name": token_attrs.get("name"),
            }

        base_token_fields = _token_fields(base_token)
        quote_token_fields = _token_fields(quote_token)

        return {
            "source": SOURCE_NAME,
            "network": network,
            "pool_id": pool_resource.get("id"),
            "pool_address": attributes.get("address"),
            "pool_name": attributes.get("name"),
            "dex": dex_id,
            "base_token_address": base_token_fields["address"],
            "base_token_symbol": base_token_fields["symbol"],
            "base_token_name": base_token_fields["name"],
            "quote_token_address": quote_token_fields["address"],
            "quote_token_symbol": quote_token_fields["symbol"],
            "quote_token_name": quote_token_fields["name"],
            "base_token_price_usd": self._to_float(attributes.get("base_token_price_usd")),
            "quote_token_price_usd": self._to_float(attributes.get("quote_token_price_usd")),
            "base_token_price_native_currency": self._to_float(
                attributes.get("base_token_price_native_currency")
            ),
            "quote_token_price_native_currency": self._to_float(
                attributes.get("quote_token_price_native_currency")
            ),
            "reserve_in_usd": self._to_float(attributes.get("reserve_in_usd")),
            "fdv_usd": self._to_float(attributes.get("fdv_usd")),
            "market_cap_usd": self._to_float(attributes.get("market_cap_usd")),
            "pool_created_at": attributes.get("pool_created_at"),
            "volume_usd": self._normalize_interval_group(attributes.get("volume_usd")),
            "price_change_percentage": self._normalize_interval_group(
                attributes.get("price_change_percentage")
            ),
            "transactions": self._normalize_transactions_group(attributes.get(_INTERVAL_TRANSACTION_GROUP)),
        }


def get_client(
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = DEFAULT_TIMEOUT,
) -> GeckoTerminalClient:
    """
    Small factory for constructing a GeckoTerminalClient. Provided for
    consistency with how the other market-data adapters (CoinGecko,
    CoinMarketCap, CryptoRank, SoSoValue) expose their own get_client()
    factories.
    """
    return GeckoTerminalClient(base_url=base_url, timeout=timeout)


if __name__ == "__main__":
    # Manual smoke test only -- never runs on import.
    # No credentials required (the GeckoTerminal public API is keyless).
    _client = get_client()
    try:
        # Example: WETH/USDC 0.05% pool on Ethereum mainnet (Uniswap V3).
        _pool = _client.pool_by_address(
            network="eth",
            address="0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640",
            include="base_token,quote_token,dex",
        )
        print(_pool)
    except GeckoTerminalError as _exc:
        print(f"GeckoTerminal smoke test failed: {_exc}")
