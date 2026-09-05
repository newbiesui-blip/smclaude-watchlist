"""
market_intelligence.py

Thin orchestration/normalization layer for ATHENA market intelligence.

Responsibilities (and ONLY these):
    1. Accept already-constructed source clients via dependency injection.
    2. Call each requested source, isolating failures per-source.
    3. Select/normalize each source's output into a flat observation dict
       using the aggregator's canonical field names.
    4. Build a single observations list and call
       market_data_aggregator.py's aggregate(records) ONCE.
    5. Return the aggregator's result plus symbol/source_status/errors.

This file explicitly does NOT:
    - Perform any HTTP/network calls itself (that belongs to the source clients).
    - Duplicate market_data_aggregator.py's reconciliation logic.
    - Duplicate the source clients' own normalization/HTTP logic.
    - Import, call, or modify smc_scanner.py, position_health.py,
      bingx_position_tracker.py, or full_scan.py.
    - Create, close, or modify any trade, order, leverage, or margin.
    - Invent asset IDs, pool addresses, ETF symbols, prices, market caps,
      or timestamps.
    - Guess a source-specific identifier (cmc_symbol, cryptorank_symbol,
      coingecko_id) from the generic `symbol` field.

ASSUMPTIONS (must be verified against the real repository files before
upload -- flagged explicitly to the reviewer):

    1. coingecko_client.get_asset(coin_id: str, vs_currency: str = "usd")
       returns a FLAT dict using CoinGecko-native field names, e.g.:
           id, symbol, name, current_price, market_cap, total_volume,
           circulating_supply, total_supply, max_supply,
           fully_diluted_valuation, price_change_percentage_24h,
           last_updated
       get_asset() is called exactly once per snapshot (it already
       internally calls markets()/coin() -- this layer never calls
       coin()/markets() directly to avoid duplicate requests).

    2. coinmarketcap_client.listings(...) and cryptorank_client.currencies(...)
       are called with NO arguments (default page) and return a LIST of
       dicts shaped like the raw CMC / CryptoRank API schemas described
       in the architecture doc:
           CMC item:        id, symbol, name, cmc_rank, circulating_supply,
                             total_supply, max_supply,
                             quote: {USD: {price, market_cap, volume_24h,
                                     fully_diluted_market_cap,
                                     percent_change_24h, last_updated}}
           CryptoRank item: id, key, symbol, name, rank, price, marketCap,
                             volume24h, circulatingSupply, totalSupply,
                             maxSupply, fullyDilutedValuation,
                             percentChange: {h24, d7, d30}, lastUpdated
       KNOWN LIMITATION: if the requested symbol is not present on the
       default (first) page of listings()/currencies(), selection will
       report NO_MATCH. If these methods accept a symbol/search filter,
       the `_fetch_coinmarketcap` / `_fetch_cryptorank` wrappers below
       are the only places that need to change.

    3. sosovalue_client.etf_summary_history(symbol=..., country_code=...,
       start_date=..., end_date=..., limit=..., extra_params=...) returns
       either a top-level list of rows, or an envelope dict with a "data"
       list. Each row has a "date" field used verbatim as its timestamp.

    4. geckoterminal_client.pool_by_address(network, address) returns the
       standard GeckoTerminal JSON:API shape:
           {"data": {"attributes": {name, address, reserve_in_usd,
                                     fdv_usd, market_cap_usd, volume_usd,
                                     price_change_percentage,
                                     transactions, pool_created_at}}}
       pool_created_at is retained INSIDE the record but is never used as
       the observation timestamp (it is pool creation time, not a fetch/
       update time) -- per instruction, timestamp is passed as None
       unless a genuine update timestamp field exists.

    5. market_data_aggregator.py exposes:
           MarketDataAggregator(...)
           aggregate(records: list[dict]) -> dict with at least
               {"categories": ..., "record_count": ..., "source_count": ...}
       aggregate() is called EXACTLY ONCE per snapshot, with the full
       list of successfully-normalized observations.
       Each record passed to aggregate() is a FLAT dict containing at
       least "source", "category", and "timestamp" keys alongside the
       canonical payload fields -- this shape is inferred from context
       (there must be some way for the aggregator to know which category
       an observation belongs to) and is the single highest-risk
       assumption in this file. If aggregate() expects a different
       envelope, only `_build_observation` needs to change.

No third-party dependencies are used -- Python standard library only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Source category constants (must match market_data_aggregator.py categories)
# ---------------------------------------------------------------------------

CATEGORY_MARKET = "market"
CATEGORY_ETF_FLOW = "etf_flow"
CATEGORY_POOL = "pool"

SOURCE_COINGECKO = "coingecko"
SOURCE_COINMARKETCAP = "coinmarketcap"
SOURCE_CRYPTORANK = "cryptorank"
SOURCE_SOSOVALUE = "sosovalue"
SOURCE_GECKOTERMINAL = "geckoterminal"

STATUS_OK = "OK"
STATUS_ERROR = "ERROR"
STATUS_NOT_REQUESTED = "NOT_REQUESTED"
STATUS_SKIPPED_MISSING_IDENTIFIER = "SKIPPED_MISSING_IDENTIFIER"
STATUS_NO_MATCH = "NO_MATCH"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_KEY_LIKE_PATTERN = re.compile(
    r"(api[_-]?key|apikey|authorization|token|secret)\s*[:=]\s*\S+",
    re.IGNORECASE,
)


def _sanitize_error(exc: Exception) -> str:
    """Convert an exception into a brief, safe-to-log string."""
    raw = str(exc)
    if _KEY_LIKE_PATTERN.search(raw):
        return f"{type(exc).__name__}: [redacted - possible credential in error]"
    if len(raw) > 300:
        raw = raw[:300] + "...(truncated)"
    return f"{type(exc).__name__}: {raw}"


def _safe_get(d: Any, *path, default=None):
    """Nested dict/list access that never raises."""
    cur = d
    for key in path:
        if cur is None:
            return default
        try:
            cur = cur[key]
        except (KeyError, IndexError, TypeError):
            return default
    return cur if cur is not None else default


def _select_symbol_record(
    rows: List[Dict[str, Any]],
    symbol: str,
    rank_field_candidates: Tuple[str, ...] = ("rank", "cmc_rank"),
) -> Optional[Dict[str, Any]]:
    """
    Deterministically select the row matching `symbol` (case-insensitive)
    from a list of source records.

    Tie-break rule when multiple rows share the same symbol: the row with
    the LOWEST rank value wins (rank 1 beats rank 500), since a lower
    market-cap rank is the most defensible signal of "the asset the
    ticker usually refers to." Rows with no rank field sort last. This
    rule is applied consistently and documented here rather than picking
    an arbitrary match silently.

    Returns None if there is no match -- never invents a record.
    """
    if not rows or not symbol:
        return None
    target = symbol.strip().upper()
    matches = [
        r
        for r in rows
        if isinstance(r, dict) and str(r.get("symbol", "")).strip().upper() == target
    ]
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]

    def _rank_key(r: Dict[str, Any]):
        for f in rank_field_candidates:
            val = r.get(f)
            if val is not None:
                return val
        return float("inf")

    matches.sort(key=_rank_key)
    return matches[0]


# ---------------------------------------------------------------------------
# Request/identifier bundles
# ---------------------------------------------------------------------------

@dataclass
class AssetIdentifiers:
    """
    Explicit per-source identifiers for a single logical asset.

    No fallback guessing: if a source-specific identifier is None, that
    source is skipped cleanly (SKIPPED_MISSING_IDENTIFIER), even if
    `symbol` is set. `symbol` is descriptive/output-only, never used as
    an implicit identifier for CMC or CryptoRank.
    """
    symbol: Optional[str] = None
    coingecko_id: Optional[str] = None
    cmc_symbol: Optional[str] = None
    cryptorank_symbol: Optional[str] = None


@dataclass
class ETFRequest:
    """Explicit parameters for a SoSoValue ETF history lookup."""
    symbol: str
    country_code: str
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    limit: Optional[int] = None
    extra_params: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PoolRequest:
    """Explicit parameters for a GeckoTerminal pool lookup."""
    network: str
    pool_address: str


# ---------------------------------------------------------------------------
# Main orchestration class
# ---------------------------------------------------------------------------

class MarketIntelligence:
    """
    Coordinates independent, read-only market-intelligence source clients
    and feeds a single normalized observations list into
    market_data_aggregator.py's aggregate() in one call.
    """

    def __init__(
        self,
        aggregator: Any,
        coingecko_client: Optional[Any] = None,
        coinmarketcap_client: Optional[Any] = None,
        cryptorank_client: Optional[Any] = None,
        sosovalue_client: Optional[Any] = None,
        geckoterminal_client: Optional[Any] = None,
    ) -> None:
        if aggregator is None:
            raise ValueError("MarketIntelligence requires an aggregator instance")

        self._aggregator = aggregator
        self._clients = {
            SOURCE_COINGECKO: coingecko_client,
            SOURCE_COINMARKETCAP: coinmarketcap_client,
            SOURCE_CRYPTORANK: cryptorank_client,
            SOURCE_SOSOVALUE: sosovalue_client,
            SOURCE_GECKOTERMINAL: geckoterminal_client,
        }

    # -- public API ---------------------------------------------------

    def get_snapshot(
        self,
        asset: AssetIdentifiers,
        include_etf: Optional[ETFRequest] = None,
        include_pool: Optional[PoolRequest] = None,
    ) -> Dict[str, Any]:
        """
        Build one market-intelligence snapshot for a given asset.

        Fetches/normalizes each requested source into a local list of flat
        observation dicts, then calls `aggregator.aggregate(observations)`
        exactly once with everything that succeeded.
        """
        source_status: Dict[str, str] = {
            SOURCE_COINGECKO: STATUS_NOT_REQUESTED,
            SOURCE_COINMARKETCAP: STATUS_NOT_REQUESTED,
            SOURCE_CRYPTORANK: STATUS_NOT_REQUESTED,
            SOURCE_SOSOVALUE: STATUS_NOT_REQUESTED,
            SOURCE_GECKOTERMINAL: STATUS_NOT_REQUESTED,
        }
        errors: Dict[str, str] = {}
        observations: List[Dict[str, Any]] = []

        # --- CoinGecko (market) ---
        if asset.coingecko_id:
            status, err, obs = self._run_source(
                source=SOURCE_COINGECKO,
                category=CATEGORY_MARKET,
                fetch_fn=lambda: self._fetch_coingecko(asset.coingecko_id),
                normalize_fn=self._normalize_coingecko,
                multi=False,
            )
            source_status[SOURCE_COINGECKO] = status
            if err:
                errors[SOURCE_COINGECKO] = err
            observations.extend(obs)
        elif self._clients[SOURCE_COINGECKO] is not None:
            source_status[SOURCE_COINGECKO] = STATUS_SKIPPED_MISSING_IDENTIFIER

        # --- CoinMarketCap (market) -- NO fallback to generic symbol ---
        if asset.cmc_symbol:
            status, err, obs = self._run_source(
                source=SOURCE_COINMARKETCAP,
                category=CATEGORY_MARKET,
                fetch_fn=self._fetch_coinmarketcap,
                normalize_fn=lambda rows: self._select_and_normalize_cmc(
                    rows, asset.cmc_symbol
                ),
                multi=False,
            )
            source_status[SOURCE_COINMARKETCAP] = status
            if err:
                errors[SOURCE_COINMARKETCAP] = err
            observations.extend(obs)
        elif self._clients[SOURCE_COINMARKETCAP] is not None:
            source_status[SOURCE_COINMARKETCAP] = STATUS_SKIPPED_MISSING_IDENTIFIER

        # --- CryptoRank (market) -- NO fallback to generic symbol ---
        if asset.cryptorank_symbol:
            status, err, obs = self._run_source(
                source=SOURCE_CRYPTORANK,
                category=CATEGORY_MARKET,
                fetch_fn=self._fetch_cryptorank,
                normalize_fn=lambda rows: self._select_and_normalize_cryptorank(
                    rows, asset.cryptorank_symbol
                ),
                multi=False,
            )
            source_status[SOURCE_CRYPTORANK] = status
            if err:
                errors[SOURCE_CRYPTORANK] = err
            observations.extend(obs)
        elif self._clients[SOURCE_CRYPTORANK] is not None:
            source_status[SOURCE_CRYPTORANK] = STATUS_SKIPPED_MISSING_IDENTIFIER

        # --- SoSoValue (etf_flow) -- explicit opt-in only ---
        if include_etf is not None:
            status, err, obs = self._run_source(
                source=SOURCE_SOSOVALUE,
                category=CATEGORY_ETF_FLOW,
                fetch_fn=lambda: self._fetch_sosovalue(include_etf),
                normalize_fn=self._normalize_sosovalue,
                multi=True,
            )
            source_status[SOURCE_SOSOVALUE] = status
            if err:
                errors[SOURCE_SOSOVALUE] = err
            observations.extend(obs)

        # --- GeckoTerminal (pool) -- explicit opt-in only ---
        if include_pool is not None:
            status, err, obs = self._run_source(
                source=SOURCE_GECKOTERMINAL,
                category=CATEGORY_POOL,
                fetch_fn=lambda: self._fetch_geckoterminal(include_pool),
                normalize_fn=self._normalize_geckoterminal,
                multi=False,
            )
            source_status[SOURCE_GECKOTERMINAL] = status
            if err:
                errors[SOURCE_GECKOTERMINAL] = err
            observations.extend(obs)

        # Single aggregate() call with everything that succeeded, even if
        # that list is empty -- a failed/skipped source never blocks the
        # successful ones from reaching the aggregator.
        try:
            aggregator_result = self._aggregator.aggregate(observations)
            if not isinstance(aggregator_result, dict):
                aggregator_result = {}
        except Exception as exc:  # noqa: BLE001
            aggregator_result = {}
            errors["aggregator"] = _sanitize_error(exc)

        snapshot: Dict[str, Any] = dict(aggregator_result)
        snapshot["symbol"] = asset.symbol
        snapshot["source_status"] = source_status
        snapshot["errors"] = errors
        return snapshot

    # -- per-source fetch wrappers (isolate assumed client signatures) --

    def _fetch_coingecko(self, coingecko_id: str) -> Any:
        client = self._clients[SOURCE_COINGECKO]
        # get_asset() already calls markets()+coin() internally -- never
        # call those directly here to avoid duplicating requests.
        return client.get_asset(coingecko_id, vs_currency="usd")

    def _fetch_coinmarketcap(self) -> Any:
        client = self._clients[SOURCE_COINMARKETCAP]
        return client.listings()

    def _fetch_cryptorank(self) -> Any:
        client = self._clients[SOURCE_CRYPTORANK]
        return client.currencies()

    def _fetch_sosovalue(self, req: ETFRequest) -> Any:
        client = self._clients[SOURCE_SOSOVALUE]
        return client.etf_summary_history(
            symbol=req.symbol,
            country_code=req.country_code,
            start_date=req.start_date,
            end_date=req.end_date,
            limit=req.limit,
            extra_params=req.extra_params,
        )

    def _fetch_geckoterminal(self, req: PoolRequest) -> Any:
        client = self._clients[SOURCE_GECKOTERMINAL]
        return client.pool_by_address(req.network, req.pool_address)

    # -- per-source normalization into canonical aggregator field names --
    #
    # Canonical market fields (per aggregator contract):
    #   price, market_cap, volume_24h, circulating_supply, total_supply,
    #   max_supply, fully_diluted_market_cap, percent_change_24h

    @staticmethod
    def _normalize_coingecko(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not isinstance(raw, dict):
            return None
        record = {
            "source_id": raw.get("id"),
            "symbol": raw.get("symbol"),
            "name": raw.get("name"),
            "price": raw.get("current_price"),
            "market_cap": raw.get("market_cap"),
            "volume_24h": raw.get("total_volume"),
            "circulating_supply": raw.get("circulating_supply"),
            "total_supply": raw.get("total_supply"),
            "max_supply": raw.get("max_supply"),
            "fully_diluted_market_cap": raw.get("fully_diluted_valuation"),
            "percent_change_24h": raw.get("price_change_percentage_24h"),
        }
        record = {k: v for k, v in record.items() if v is not None}
        if not record:
            return None
        return {"record": record, "timestamp": raw.get("last_updated")}

    @staticmethod
    def _select_and_normalize_cmc(
        rows: Any, symbol: str
    ) -> Optional[Dict[str, Any]]:
        if not isinstance(rows, list):
            return None
        match = _select_symbol_record(rows, symbol, rank_field_candidates=("cmc_rank", "rank"))
        if match is None:
            return None
        quote_usd = _safe_get(match, "quote", "USD", default={}) or {}
        record = {
            "source_id": match.get("id"),
            "symbol": match.get("symbol"),
            "name": match.get("name"),
            "price": quote_usd.get("price"),
            "market_cap": quote_usd.get("market_cap"),
            "volume_24h": quote_usd.get("volume_24h"),
            "circulating_supply": match.get("circulating_supply"),
            "total_supply": match.get("total_supply"),
            "max_supply": match.get("max_supply"),
            "fully_diluted_market_cap": quote_usd.get("fully_diluted_market_cap"),
            "percent_change_24h": quote_usd.get("percent_change_24h"),
        }
        record = {k: v for k, v in record.items() if v is not None}
        if not record:
            return None
        return {"record": record, "timestamp": quote_usd.get("last_updated")}

    @staticmethod
    def _select_and_normalize_cryptorank(
        rows: Any, symbol: str
    ) -> Optional[Dict[str, Any]]:
        if not isinstance(rows, list):
            return None
        match = _select_symbol_record(rows, symbol, rank_field_candidates=("rank",))
        if match is None:
            return None
        record = {
            "source_id": match.get("id") or match.get("key"),
            "symbol": match.get("symbol"),
            "name": match.get("name"),
            "price": match.get("price"),
            "market_cap": match.get("marketCap"),
            "volume_24h": match.get("volume24h"),
            "circulating_supply": match.get("circulatingSupply"),
            "total_supply": match.get("totalSupply"),
            "max_supply": match.get("maxSupply"),
            "fully_diluted_market_cap": match.get("fullyDilutedValuation"),
            "percent_change_24h": _safe_get(match, "percentChange", "h24"),
        }
        record = {k: v for k, v in record.items() if v is not None}
        if not record:
            return None
        return {"record": record, "timestamp": match.get("lastUpdated")}

    @staticmethod
    def _normalize_sosovalue(raw: Any) -> List[Dict[str, Any]]:
        """
        SoSoValue rows are multiple observations from the SAME source.
        Each row's own "date" field is used verbatim as its timestamp --
        never manufactured.
        """
        if isinstance(raw, dict):
            rows = raw.get("data")
            if rows is None:
                rows = [raw]
        elif isinstance(raw, list):
            rows = raw
        else:
            rows = []

        normalized: List[Dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            record = {
                "date": row.get("date"),
                "total_net_inflow": row.get("total_net_inflow"),
                "total_value_traded": row.get("total_value_traded"),
                "total_net_assets": row.get("total_net_assets"),
                "cum_net_inflow": row.get("cum_net_inflow"),
            }
            record = {k: v for k, v in record.items() if v is not None}
            if record:
                normalized.append({"record": record, "timestamp": row.get("date")})
        return normalized

    @staticmethod
    def _normalize_geckoterminal(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        pool_created_at is retained INSIDE the record (pool metadata) but
        is never used as the observation timestamp -- it reflects when
        the pool was created, not when this observation was fetched.
        """
        if not isinstance(raw, dict):
            return None
        data = raw.get("data")
        attributes = _safe_get(data, "attributes", default={}) if data else {}
        if not attributes:
            return None
        record = {
            "pool_name": attributes.get("name"),
            "pool_address": attributes.get("address"),
            "reserve_in_usd": attributes.get("reserve_in_usd"),
            "fdv_usd": attributes.get("fdv_usd"),
            "market_cap_usd": attributes.get("market_cap_usd"),
            "volume_usd": attributes.get("volume_usd"),
            "price_change_percentage": attributes.get("price_change_percentage"),
            "transactions": attributes.get("transactions"),
            "pool_created_at": attributes.get("pool_created_at"),
        }
        record = {k: v for k, v in record.items() if v is not None}
        if not record:
            return None
        # No genuine fetch/update timestamp is known to exist in this
        # response shape -- pass None rather than mislabeling creation
        # time as freshness.
        return {"record": record, "timestamp": None}

    # -- internal execution plumbing ------------------------------------

    def _run_source(
        self,
        source: str,
        category: str,
        fetch_fn: Callable[[], Any],
        normalize_fn: Callable[[Any], Any],
        multi: bool,
    ) -> Tuple[str, Optional[str], List[Dict[str, Any]]]:
        """
        Run one source end-to-end (fetch -> select/normalize) with full
        error isolation. Never touches the aggregator directly -- returns
        (status, error_message_or_None, list_of_observation_dicts) for
        the caller to fold into a single batch.
        """
        client = self._clients.get(source)
        if client is None:
            return STATUS_NOT_REQUESTED, None, []

        try:
            raw = fetch_fn()
        except Exception as exc:  # noqa: BLE001 - intentional broad isolation
            return STATUS_ERROR, _sanitize_error(exc), []

        try:
            if multi:
                items = normalize_fn(raw)
                if not items:
                    return STATUS_ERROR, "normalization produced no usable records", []
                obs = [self._build_observation(source, category, it) for it in items]
                return STATUS_OK, None, obs
            else:
                item = normalize_fn(raw)
                if item is None:
                    return STATUS_NO_MATCH, "no matching record found for requested identifier", []
                return STATUS_OK, None, [self._build_observation(source, category, item)]
        except Exception as exc:  # noqa: BLE001 - normalization must never crash the snapshot
            return STATUS_ERROR, _sanitize_error(exc), []

    @staticmethod
    def _build_observation(
        source: str, category: str, item: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Flatten a {"record": {...}, "timestamp": ...} item into the flat
        observation shape assumed for aggregator.aggregate(): source +
        category + timestamp alongside the canonical payload fields.
        """
        observation = {
            "source": source,
            "category": category,
            "timestamp": item.get("timestamp"),
        }
        observation.update(item.get("record", {}))
        return observation


# ---------------------------------------------------------------------------
# Embedded lightweight tests (no network, no real API keys, no pytest dep)
# ---------------------------------------------------------------------------

def _run_tests() -> None:
    import unittest

    class FakeAggregator:
        """Minimal stand-in for market_data_aggregator.py's real API."""

        def __init__(self, fail: bool = False):
            self.calls: List[List[Dict[str, Any]]] = []
            self._fail = fail

        def aggregate(self, records):
            self.calls.append(records)
            if self._fail:
                raise RuntimeError("aggregator boom")
            categories = sorted({r["category"] for r in records})
            sources = sorted({r["source"] for r in records})
            return {
                "categories": categories,
                "record_count": len(records),
                "source_count": len(sources),
            }

    class FakeCoingeckoClient:
        def __init__(self):
            self.get_asset_calls = 0

        def get_asset(self, coin_id, vs_currency="usd"):
            self.get_asset_calls += 1
            assert coin_id == "bitcoin"
            assert vs_currency == "usd"
            return {
                "id": "bitcoin",
                "symbol": "btc",
                "name": "Bitcoin",
                "current_price": 65000.0,
                "market_cap": 1_200_000_000_000,
                "total_volume": 30_000_000_000,
                "circulating_supply": 19_700_000,
                "fully_diluted_valuation": 1_365_000_000_000,
                "price_change_percentage_24h": 1.5,
                "last_updated": "2025-01-01T00:00:00.000Z",
            }

        # Deliberately no coin()/markets() methods on this fake -- if the
        # orchestration layer tried to call them, the test would raise
        # AttributeError, proving get_asset() alone is used.

    class FailingClient:
        def __getattr__(self, name):
            def _raise(*args, **kwargs):
                raise RuntimeError("upstream 500 error, api_key=SECRETVALUE123")
            return _raise

    class FakeCMCClient:
        def listings(self):
            return [
                {
                    "id": 1,
                    "symbol": "BTC",
                    "name": "Bitcoin",
                    "cmc_rank": 1,
                    "circulating_supply": 19_700_000,
                    "total_supply": 21_000_000,
                    "max_supply": 21_000_000,
                    "quote": {
                        "USD": {
                            "price": 65010.0,
                            "market_cap": 1_200_500_000_000,
                            "volume_24h": 30_100_000_000,
                            "fully_diluted_market_cap": 1_365_210_000_000,
                            "percent_change_24h": 1.2,
                            "last_updated": "2025-01-01T00:00:05.000Z",
                        }
                    },
                },
                {
                    # A lower-quality/unrelated project that also uses
                    # ticker "BTC" with a much worse rank -- selection
                    # must deterministically prefer the rank-1 entry.
                    "id": 9999,
                    "symbol": "BTC",
                    "name": "Some Other BTC Token",
                    "cmc_rank": 4500,
                    "quote": {"USD": {"price": 0.0001}},
                },
                {
                    "id": 2,
                    "symbol": "ETH",
                    "name": "Ethereum",
                    "cmc_rank": 2,
                    "quote": {"USD": {"price": 3000.0}},
                },
            ]

    class FakeCryptorankClient:
        def currencies(self):
            return [
                {
                    "id": 1,
                    "key": "bitcoin",
                    "symbol": "BTC",
                    "name": "Bitcoin",
                    "rank": 1,
                    "price": 64990.0,
                    "marketCap": 1_199_000_000_000,
                    "volume24h": 29_900_000_000,
                    "circulatingSupply": 19_700_000,
                    "totalSupply": 21_000_000,
                    "maxSupply": 21_000_000,
                    "fullyDilutedValuation": 1_364_790_000_000,
                    "percentChange": {"h24": 1.1, "d7": 3.4, "d30": 10.2},
                    "lastUpdated": "2025-01-01T00:00:10.000Z",
                },
                {
                    "id": 2,
                    "key": "ethereum",
                    "symbol": "ETH",
                    "name": "Ethereum",
                    "rank": 2,
                    "price": 3000.0,
                },
            ]

    class FakeSosoValueClient:
        def etf_summary_history(self, **kwargs):
            assert kwargs["symbol"] == "BTC"
            assert kwargs["country_code"] == "US"
            return {
                "data": [
                    {
                        "date": "2025-01-01",
                        "total_net_inflow": 100_000_000,
                        "total_net_assets": 50_000_000_000,
                    },
                    {
                        "date": "2025-01-02",
                        "total_net_inflow": -50_000_000,
                        "total_net_assets": 49_950_000_000,
                    },
                ]
            }

    class FakeGeckoTerminalClient:
        def pool_by_address(self, network, address):
            assert network == "eth"
            assert address == "0xabc"
            return {
                "data": {
                    "attributes": {
                        "name": "WETH/USDC",
                        "address": "0xabc",
                        "reserve_in_usd": "12345678.90",
                        "fdv_usd": "999999999",
                        "volume_usd": {"h24": "5000000"},
                        "pool_created_at": "2024-01-01T00:00:00Z",
                    }
                }
            }

    class MarketIntelligenceTests(unittest.TestCase):
        def test_dependency_injection_and_full_success(self):
            agg = FakeAggregator()
            mi = MarketIntelligence(
                aggregator=agg,
                coingecko_client=FakeCoingeckoClient(),
                coinmarketcap_client=FakeCMCClient(),
                cryptorank_client=FakeCryptorankClient(),
                sosovalue_client=FakeSosoValueClient(),
                geckoterminal_client=FakeGeckoTerminalClient(),
            )
            asset = AssetIdentifiers(
                symbol="BTC",
                coingecko_id="bitcoin",
                cmc_symbol="BTC",
                cryptorank_symbol="BTC",
            )
            snapshot = mi.get_snapshot(
                asset,
                include_etf=ETFRequest(symbol="BTC", country_code="US"),
                include_pool=PoolRequest(network="eth", pool_address="0xabc"),
            )
            self.assertEqual(snapshot["symbol"], "BTC")
            self.assertEqual(snapshot["source_status"][SOURCE_COINGECKO], STATUS_OK)
            self.assertEqual(snapshot["source_status"][SOURCE_COINMARKETCAP], STATUS_OK)
            self.assertEqual(snapshot["source_status"][SOURCE_CRYPTORANK], STATUS_OK)
            self.assertEqual(snapshot["source_status"][SOURCE_SOSOVALUE], STATUS_OK)
            self.assertEqual(snapshot["source_status"][SOURCE_GECKOTERMINAL], STATUS_OK)
            self.assertEqual(snapshot["errors"], {})
            self.assertIn(CATEGORY_MARKET, snapshot["categories"])
            self.assertIn(CATEGORY_ETF_FLOW, snapshot["categories"])
            self.assertIn(CATEGORY_POOL, snapshot["categories"])
            # record_count: 1 coingecko + 1 cmc + 1 cryptorank + 2 sosovalue + 1 pool = 6
            self.assertEqual(snapshot["record_count"], 6)

        def test_aggregate_called_exactly_once_with_full_batch(self):
            agg = FakeAggregator()
            mi = MarketIntelligence(
                aggregator=agg,
                coingecko_client=FakeCoingeckoClient(),
                coinmarketcap_client=FakeCMCClient(),
            )
            asset = AssetIdentifiers(symbol="BTC", coingecko_id="bitcoin", cmc_symbol="BTC")
            mi.get_snapshot(asset)
            self.assertEqual(len(agg.calls), 1)  # exactly one aggregate() call
            self.assertEqual(len(agg.calls[0]), 2)  # both observations in that one call

        def test_one_source_fails_others_still_reach_aggregator(self):
            agg = FakeAggregator()
            mi = MarketIntelligence(
                aggregator=agg,
                coingecko_client=FakeCoingeckoClient(),
                cryptorank_client=FailingClient(),
            )
            asset = AssetIdentifiers(
                symbol="BTC", coingecko_id="bitcoin", cryptorank_symbol="BTC"
            )
            snapshot = mi.get_snapshot(asset)
            self.assertEqual(snapshot["source_status"][SOURCE_COINGECKO], STATUS_OK)
            self.assertEqual(snapshot["source_status"][SOURCE_CRYPTORANK], STATUS_ERROR)
            self.assertIn(SOURCE_CRYPTORANK, snapshot["errors"])
            self.assertEqual(snapshot["record_count"], 1)  # only coingecko made it

        def test_cmc_missing_symbol_skips_cmc(self):
            agg = FakeAggregator()
            mi = MarketIntelligence(aggregator=agg, coinmarketcap_client=FakeCMCClient())
            asset = AssetIdentifiers(symbol="BTC", cmc_symbol=None)
            snapshot = mi.get_snapshot(asset)
            self.assertEqual(
                snapshot["source_status"][SOURCE_COINMARKETCAP],
                STATUS_SKIPPED_MISSING_IDENTIFIER,
            )
            self.assertEqual(len(agg.calls[0]), 0)

        def test_cryptorank_missing_symbol_skips_cryptorank(self):
            agg = FakeAggregator()
            mi = MarketIntelligence(aggregator=agg, cryptorank_client=FakeCryptorankClient())
            asset = AssetIdentifiers(symbol="BTC", cryptorank_symbol=None)
            snapshot = mi.get_snapshot(asset)
            self.assertEqual(
                snapshot["source_status"][SOURCE_CRYPTORANK],
                STATUS_SKIPPED_MISSING_IDENTIFIER,
            )

        def test_coingecko_requires_explicit_id_no_symbol_fallback(self):
            agg = FakeAggregator()
            mi = MarketIntelligence(aggregator=agg, coingecko_client=FakeCoingeckoClient())
            # symbol is set but coingecko_id is not -- must NOT be guessed.
            asset = AssetIdentifiers(symbol="BTC", coingecko_id=None)
            snapshot = mi.get_snapshot(asset)
            self.assertEqual(
                snapshot["source_status"][SOURCE_COINGECKO],
                STATUS_SKIPPED_MISSING_IDENTIFIER,
            )

        def test_coingecko_get_asset_called_exactly_once(self):
            agg = FakeAggregator()
            client = FakeCoingeckoClient()
            mi = MarketIntelligence(aggregator=agg, coingecko_client=client)
            asset = AssetIdentifiers(symbol="BTC", coingecko_id="bitcoin")
            mi.get_snapshot(asset)
            self.assertEqual(client.get_asset_calls, 1)

        def test_coingecko_normalization_uses_canonical_field_names(self):
            result = MarketIntelligence._normalize_coingecko(
                FakeCoingeckoClient().get_asset("bitcoin")
            )
            record = result["record"]
            self.assertEqual(record["price"], 65000.0)
            self.assertEqual(record["volume_24h"], 30_000_000_000)
            self.assertEqual(record["percent_change_24h"], 1.5)
            self.assertNotIn("current_price", record)
            self.assertNotIn("total_volume", record)

        def test_cmc_selection_and_canonical_fields(self):
            rows = FakeCMCClient().listings()
            result = MarketIntelligence._select_and_normalize_cmc(rows, "BTC")
            record = result["record"]
            # Must pick the rank-1 BTC, not the rank-4500 impostor.
            self.assertEqual(record["source_id"], 1)
            self.assertEqual(record["price"], 65010.0)
            self.assertEqual(record["volume_24h"], 30_100_000_000)
            self.assertNotIn("cmc_rank", record)

        def test_cmc_no_match_reports_no_match_status(self):
            agg = FakeAggregator()
            mi = MarketIntelligence(aggregator=agg, coinmarketcap_client=FakeCMCClient())
            asset = AssetIdentifiers(symbol="DOGE", cmc_symbol="DOGE")
            snapshot = mi.get_snapshot(asset)
            self.assertEqual(snapshot["source_status"][SOURCE_COINMARKETCAP], STATUS_NO_MATCH)
            self.assertEqual(snapshot["record_count"], 0)

        def test_cryptorank_selection_and_canonical_fields(self):
            rows = FakeCryptorankClient().currencies()
            result = MarketIntelligence._select_and_normalize_cryptorank(rows, "BTC")
            record = result["record"]
            self.assertEqual(record["price"], 64990.0)
            self.assertEqual(record["market_cap"], 1_199_000_000_000)
            self.assertEqual(record["percent_change_24h"], 1.1)
            self.assertNotIn("marketCap", record)

        def test_sosovalue_etf_flow_multiple_observations_same_source(self):
            agg = FakeAggregator()
            mi = MarketIntelligence(aggregator=agg, sosovalue_client=FakeSosoValueClient())
            asset = AssetIdentifiers(symbol="BTC")
            snapshot = mi.get_snapshot(
                asset, include_etf=ETFRequest(symbol="BTC", country_code="US")
            )
            self.assertEqual(snapshot["source_status"][SOURCE_SOSOVALUE], STATUS_OK)
            batch = agg.calls[0]
            etf_obs = [o for o in batch if o["category"] == CATEGORY_ETF_FLOW]
            self.assertEqual(len(etf_obs), 2)
            self.assertTrue(all(o["source"] == SOURCE_SOSOVALUE for o in etf_obs))
            self.assertEqual(etf_obs[0]["timestamp"], "2025-01-01")

        def test_geckoterminal_pool_category_and_timestamp_not_creation_time(self):
            agg = FakeAggregator()
            mi = MarketIntelligence(
                aggregator=agg, geckoterminal_client=FakeGeckoTerminalClient()
            )
            asset = AssetIdentifiers(symbol="ETH")
            snapshot = mi.get_snapshot(
                asset, include_pool=PoolRequest(network="eth", pool_address="0xabc")
            )
            self.assertEqual(snapshot["source_status"][SOURCE_GECKOTERMINAL], STATUS_OK)
            batch = agg.calls[0]
            pool_obs = [o for o in batch if o["category"] == CATEGORY_POOL]
            self.assertEqual(len(pool_obs), 1)
            # pool_created_at retained as metadata...
            self.assertEqual(pool_obs[0]["pool_created_at"], "2024-01-01T00:00:00Z")
            # ...but NOT used as the observation timestamp.
            self.assertIsNone(pool_obs[0]["timestamp"])

        def test_errors_never_expose_api_keys(self):
            agg = FakeAggregator()
            mi = MarketIntelligence(aggregator=agg, cryptorank_client=FailingClient())
            asset = AssetIdentifiers(symbol="BTC", cryptorank_symbol="BTC")
            snapshot = mi.get_snapshot(asset)
            error_text = snapshot["errors"][SOURCE_CRYPTORANK]
            self.assertNotIn("SECRETVALUE123", error_text)
            self.assertIn("redacted", error_text)

        def test_aggregator_failure_handled_safely(self):
            agg = FakeAggregator(fail=True)
            mi = MarketIntelligence(aggregator=agg, coingecko_client=FakeCoingeckoClient())
            asset = AssetIdentifiers(symbol="BTC", coingecko_id="bitcoin")
            snapshot = mi.get_snapshot(asset)  # must not raise
            self.assertIn("aggregator", snapshot["errors"])
            self.assertNotIn("categories", snapshot)  # aggregator result withheld on failure

        def test_no_smc_fields_present_anywhere_in_module(self):
            unambiguous_forbidden = {
                "trade_signal",
                "entry_signal",
                "trade_score",
                "confidence_to_trade",
                "execution_ready",
                "stop_loss",
                "take_profit",
            }
            quoted_only_forbidden = {"buy", "sell", "long", "short"}

            with open(__file__, "r", encoding="utf-8") as f:
                source_text = f.read()
            production_code = source_text.split("def _run_tests() -> None:")[0]

            for term in unambiguous_forbidden:
                pattern = r"\b" + re.escape(term) + r"\b"
                self.assertIsNone(
                    re.search(pattern, production_code),
                    msg=f"forbidden trading/SMC term '{term}' found in production code",
                )
            for term in quoted_only_forbidden:
                quoted_pattern = r"""["']""" + re.escape(term) + r"""["']"""
                self.assertIsNone(
                    re.search(quoted_pattern, production_code),
                    msg=f"forbidden trading/SMC field literal '{term}' found in production code",
                )

        def test_no_trading_or_smc_imports(self):
            with open(__file__, "r", encoding="utf-8") as f:
                source_text = f.read()
            production_code = source_text.split("def _run_tests() -> None:")[0]
            for banned_import in (
                "import smc_scanner",
                "from smc_scanner",
                "import full_scan",
                "from full_scan",
                "import bingx_position_tracker",
                "from bingx_position_tracker",
                "import position_health",
                "from position_health",
            ):
                self.assertNotIn(banned_import, production_code)

    suite = unittest.TestLoader().loadTestsFromTestCase(MarketIntelligenceTests)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    if not result.wasSuccessful():
        raise SystemExit(1)


if __name__ == "__main__":
    _run_tests()
