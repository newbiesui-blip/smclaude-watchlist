"""Thin, read-only orchestration layer for ATHENA market intelligence.

This module calls injected source clients, normalizes their *existing adapter
outputs*, and sends one batch to MarketDataAggregator. It does not contain
trading, SMC, BingX, position, or execution logic.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from market_intelligence_config import MarketAssetConfig, MarketIntelligenceConfig, PoolRequestConfig
from market_intelligence_config_adapter import to_asset_requests, to_request_bundle

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

_KEY_LIKE_PATTERN = re.compile(r"(api[_-]?key|apikey|authorization|token|secret)\s*[:=]\s*\S+", re.I)


def _sanitize_error(exc: Exception) -> str:
    raw = str(exc)
    if _KEY_LIKE_PATTERN.search(raw):
        return f"{type(exc).__name__}: [redacted - possible credential in error]"
    return f"{type(exc).__name__}: {raw[:300]}{'...(truncated)' if len(raw) > 300 else ''}"


def _select_symbol_record(rows: Any, symbol: str, rank_fields: Tuple[str, ...] = ("rank", "cmc_rank")) -> Optional[Dict[str, Any]]:
    if not isinstance(rows, list) or not symbol:
        return None
    target = symbol.strip().upper()
    matches = [r for r in rows if isinstance(r, dict) and str(r.get("symbol", "")).strip().upper() == target]
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    def rank_key(row: Dict[str, Any]):
        for field_name in rank_fields:
            value = row.get(field_name)
            if value is not None:
                try:
                    return float(value)
                except (TypeError, ValueError):
                    pass
        return float("inf")
    return sorted(matches, key=rank_key)[0]


@dataclass
class AssetIdentifiers:
    symbol: Optional[str] = None
    coingecko_id: Optional[str] = None
    cmc_symbol: Optional[str] = None
    cryptorank_symbol: Optional[str] = None


@dataclass
class ETFRequest:
    symbol: str
    country_code: str
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    limit: Optional[int] = None
    extra_params: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PoolRequest:
    network: str
    pool_address: str


class MarketIntelligence:
    """Coordinate independent read-only source adapters."""

    def __init__(self, aggregator: Any, coingecko_client: Any = None,
                 coinmarketcap_client: Any = None, cryptorank_client: Any = None,
                 sosovalue_client: Any = None, geckoterminal_client: Any = None) -> None:
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

    def get_snapshot(self, asset: AssetIdentifiers, include_etf: Optional[ETFRequest] = None,
                     include_pool: Optional[PoolRequest] = None,
                     include_pools: Optional[List[PoolRequest]] = None) -> Dict[str, Any]:
        statuses = {source: STATUS_NOT_REQUESTED for source in self._clients}
        errors: Dict[str, str] = {}
        observations: List[Dict[str, Any]] = []

        if asset.coingecko_id:
            status, error, obs = self._run_source(SOURCE_COINGECKO, CATEGORY_MARKET,
                lambda: self._fetch_coingecko(asset.coingecko_id), self._normalize_coingecko, False)
            statuses[SOURCE_COINGECKO] = status
            if error: errors[SOURCE_COINGECKO] = error
            observations.extend(obs)
        elif self._clients[SOURCE_COINGECKO] is not None:
            statuses[SOURCE_COINGECKO] = STATUS_SKIPPED_MISSING_IDENTIFIER

        if asset.cmc_symbol:
            status, error, obs = self._run_source(SOURCE_COINMARKETCAP, CATEGORY_MARKET,
                self._fetch_coinmarketcap,
                lambda rows: self._select_and_normalize_cmc(rows, asset.cmc_symbol), False)
            statuses[SOURCE_COINMARKETCAP] = status
            if error: errors[SOURCE_COINMARKETCAP] = error
            observations.extend(obs)
        elif self._clients[SOURCE_COINMARKETCAP] is not None:
            statuses[SOURCE_COINMARKETCAP] = STATUS_SKIPPED_MISSING_IDENTIFIER

        if asset.cryptorank_symbol:
            status, error, obs = self._run_source(SOURCE_CRYPTORANK, CATEGORY_MARKET,
                self._fetch_cryptorank,
                lambda rows: self._select_and_normalize_cryptorank(rows, asset.cryptorank_symbol), False)
            statuses[SOURCE_CRYPTORANK] = status
            if error: errors[SOURCE_CRYPTORANK] = error
            observations.extend(obs)
        elif self._clients[SOURCE_CRYPTORANK] is not None:
            statuses[SOURCE_CRYPTORANK] = STATUS_SKIPPED_MISSING_IDENTIFIER

        if include_etf is not None:
            status, error, obs = self._run_source(SOURCE_SOSOVALUE, CATEGORY_ETF_FLOW,
                lambda: self._fetch_sosovalue(include_etf), self._normalize_sosovalue, True)
            statuses[SOURCE_SOSOVALUE] = status
            if error: errors[SOURCE_SOSOVALUE] = error
            observations.extend(obs)

        pool_requests: List[PoolRequest] = []
        if include_pool is not None:
            pool_requests.append(include_pool)
        if include_pools:
            pool_requests.extend(include_pools)
        if pool_requests:
            pool_errors: List[str] = []
            any_success = False
            any_no_match = False
            for req in pool_requests:
                status, error, obs = self._run_source(SOURCE_GECKOTERMINAL, CATEGORY_POOL,
                    lambda req=req: self._fetch_geckoterminal(req), self._normalize_geckoterminal, False)
                any_success |= status == STATUS_OK
                any_no_match |= status == STATUS_NO_MATCH
                if error: pool_errors.append(error)
                observations.extend(obs)
            if any_success:
                statuses[SOURCE_GECKOTERMINAL] = STATUS_ERROR if pool_errors else STATUS_OK
            elif any_no_match and not pool_errors:
                statuses[SOURCE_GECKOTERMINAL] = STATUS_NO_MATCH
            else:
                statuses[SOURCE_GECKOTERMINAL] = STATUS_ERROR
            if pool_errors:
                errors[SOURCE_GECKOTERMINAL] = "; ".join(pool_errors)

        try:
            aggregate_result = self._aggregator.aggregate(observations)
            if not isinstance(aggregate_result, dict):
                aggregate_result = {}
        except Exception as exc:
            aggregate_result = {}
            errors["aggregator"] = _sanitize_error(exc)

        result = dict(aggregate_result)
        result.update(symbol=asset.symbol, source_status=statuses, errors=errors)
        return result

    def get_snapshot_from_config(self, asset_config: MarketAssetConfig) -> Dict[str, Any]:
        request = to_asset_requests(asset_config)
        return self.get_snapshot(request.identifiers, include_etf=request.etf_request,
                                 include_pools=request.pool_requests)

    def get_snapshots_from_config(self, config: MarketIntelligenceConfig) -> List[Dict[str, Any]]:
        return [self.get_snapshot(request.identifiers, include_etf=request.etf_request,
                                  include_pools=request.pool_requests)
                for request in to_request_bundle(config)]

    def _fetch_coingecko(self, coingecko_id: str) -> Any:
        return self._clients[SOURCE_COINGECKO].get_asset(coingecko_id, vs_currency="usd")

    def _fetch_coinmarketcap(self) -> Any:
        return self._clients[SOURCE_COINMARKETCAP].listings()

    def _fetch_cryptorank(self) -> Any:
        return self._clients[SOURCE_CRYPTORANK].currencies()

    def _fetch_sosovalue(self, req: ETFRequest) -> Any:
        return self._clients[SOURCE_SOSOVALUE].etf_summary_history(
            symbol=req.symbol, country_code=req.country_code, start_date=req.start_date,
            end_date=req.end_date, limit=req.limit, extra_params=req.extra_params)

    def _fetch_geckoterminal(self, req: PoolRequest) -> Any:
        return self._clients[SOURCE_GECKOTERMINAL].pool_by_address(req.network, req.pool_address)

    @staticmethod
    def _normalize_coingecko(raw: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(raw, dict):
            return None
        record = {k: raw.get(v) for k, v in {
            "source_id": "id", "symbol": "symbol", "name": "name", "price": "current_price",
            "market_cap": "market_cap", "volume_24h": "total_volume",
            "circulating_supply": "circulating_supply", "total_supply": "total_supply",
            "max_supply": "max_supply", "fully_diluted_market_cap": "fully_diluted_valuation",
            "percent_change_24h": "price_change_percentage_24h"}.items() if raw.get(v) is not None}
        return {"record": record, "timestamp": raw.get("last_updated")} if record else None

    @staticmethod
    def _select_and_normalize_cmc(rows: Any, symbol: str) -> Optional[Dict[str, Any]]:
        """Consume the installed CMC client's already-normalized flat rows."""
        match = _select_symbol_record(rows, symbol, ("market_cap_rank", "cmc_rank", "rank"))
        if match is None:
            return None
        record = {k: match.get(v) for k, v in {
            "source_id": "source_id", "symbol": "symbol", "name": "name", "price": "price",
            "market_cap": "market_cap", "volume_24h": "volume_24h",
            "circulating_supply": "circulating_supply", "total_supply": "total_supply",
            "max_supply": "max_supply", "fully_diluted_market_cap": "fully_diluted_market_cap",
            "percent_change_24h": "percent_change_24h"}.items() if match.get(v) is not None}
        return {"record": record, "timestamp": match.get("last_updated")} if record else None

    @staticmethod
    def _select_and_normalize_cryptorank(rows: Any, symbol: str) -> Optional[Dict[str, Any]]:
        """Consume the installed CryptoRank client's already-normalized flat rows."""
        match = _select_symbol_record(rows, symbol, ("rank",))
        if match is None:
            return None
        record = {k: match.get(v) for k, v in {
            "source_id": "source_id", "symbol": "symbol", "name": "name", "price": "price",
            "market_cap": "market_cap", "volume_24h": "volume_24h",
            "circulating_supply": "circulating_supply", "total_supply": "total_supply",
            "max_supply": "max_supply", "fully_diluted_market_cap": "fully_diluted_market_cap",
            "percent_change_24h": "percent_change_24h"}.items() if match.get(v) is not None}
        return {"record": record, "timestamp": match.get("last_updated")} if record else None

    @staticmethod
    def _normalize_sosovalue(raw: Any) -> List[Dict[str, Any]]:
        rows = raw.get("data") if isinstance(raw, dict) else raw
        if rows is None and isinstance(raw, dict):
            rows = [raw]
        if not isinstance(rows, list):
            return []
        output = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            record = {k: row.get(k) for k in (
                "date", "total_net_inflow", "total_value_traded", "total_net_assets", "cum_net_inflow")
                if row.get(k) is not None}
            if record:
                output.append({"record": record, "timestamp": row.get("date")})
        return output

    @staticmethod
    def _normalize_geckoterminal(raw: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(raw, dict):
            return None
        attrs = ((raw.get("data") or {}).get("attributes") or {})
        if not attrs:
            return None
        record = {k: attrs.get(v) for k, v in {
            "pool_name": "name", "pool_address": "address", "reserve_in_usd": "reserve_in_usd",
            "fdv_usd": "fdv_usd", "market_cap_usd": "market_cap_usd", "volume_usd": "volume_usd",
            "price_change_percentage": "price_change_percentage", "transactions": "transactions",
            "pool_created_at": "pool_created_at"}.items() if attrs.get(v) is not None}
        return {"record": record, "timestamp": None} if record else None

    def _run_source(self, source: str, category: str, fetch_fn: Callable[[], Any],
                    normalize_fn: Callable[[Any], Any], multi: bool) -> Tuple[str, Optional[str], List[Dict[str, Any]]]:
        if self._clients.get(source) is None:
            return STATUS_NOT_REQUESTED, None, []
        try:
            raw = fetch_fn()
            normalized = normalize_fn(raw)
            if multi:
                if not normalized:
                    return STATUS_ERROR, "normalization produced no usable records", []
                items = normalized
            else:
                if normalized is None:
                    return STATUS_NO_MATCH, "no matching record found for requested identifier", []
                items = [normalized]
            observations = [self._build_observation(source, category, item) for item in items]
            return STATUS_OK, None, observations
        except Exception as exc:
            return STATUS_ERROR, _sanitize_error(exc), []

    @staticmethod
    def _build_observation(source: str, category: str, item: Dict[str, Any]) -> Dict[str, Any]:
        observation = {"source": source, "category": category, "timestamp": item.get("timestamp")}
        observation.update(item.get("record", {}))
        return observation


# Lightweight offline tests. They validate the installed adapter shapes,
# especially the CMC/CryptoRank flat-row compatibility that was previously wrong.
def _run_tests() -> None:
    import unittest

    class Agg:
        def __init__(self, fail=False): self.calls = []; self.fail = fail
        def aggregate(self, records):
            self.calls.append(records)
            if self.fail: raise RuntimeError("aggregator boom")
            return {"categories": sorted({r["category"] for r in records}),
                    "record_count": len(records), "source_count": len({r["source"] for r in records})}

    class CG:
        def __init__(self): self.calls = 0
        def get_asset(self, coin_id, vs_currency="usd"):
            self.calls += 1
            return {"id": coin_id, "symbol": "btc", "name": "Bitcoin", "current_price": 65000,
                    "market_cap": 1.2e12, "total_volume": 3e10, "circulating_supply": 19.7e6,
                    "fully_diluted_valuation": 1.36e12, "price_change_percentage_24h": 1.5,
                    "last_updated": "2025-01-01T00:00:00Z"}

    class CMC:
        def listings(self):
            return [{"source": "coinmarketcap", "source_id": 1, "symbol": "BTC", "name": "Bitcoin",
                     "market_cap_rank": 1, "price": 65010, "market_cap": 1.2e12, "volume_24h": 3.01e10,
                     "circulating_supply": 19.7e6, "total_supply": 21e6, "max_supply": 21e6,
                     "fully_diluted_market_cap": 1.365e12, "percent_change_24h": 1.2,
                     "last_updated": "2025-01-01T00:00:05Z"}]

    class CR:
        def currencies(self):
            return [{"source": "cryptorank", "source_id": "bitcoin", "symbol": "BTC", "name": "Bitcoin",
                     "rank": 1, "price": 64990, "market_cap": 1.199e12, "volume_24h": 2.99e10,
                     "circulating_supply": 19.7e6, "total_supply": 21e6, "max_supply": 21e6,
                     "fully_diluted_market_cap": 1.364e12, "percent_change_24h": 1.1,
                     "last_updated": "2025-01-01T00:00:10Z"}]

    class Failing:
        def currencies(self): raise RuntimeError("upstream api_key=SECRETVALUE")

    class Soso:
        def etf_summary_history(self, **kwargs):
            return {"data": [{"date": "2025-01-01", "total_net_inflow": 100}, {"date": "2025-01-02", "total_net_inflow": -50}]}

    class Gecko:
        def pool_by_address(self, network, address):
            return {"data": {"attributes": {"name": "WETH/USDC", "address": address,
                    "reserve_in_usd": "123", "pool_created_at": "2024-01-01T00:00:00Z"}}}

    class Tests(unittest.TestCase):
        def test_actual_flat_adapter_shapes(self):
            agg = Agg(); mi = MarketIntelligence(agg, CG(), CMC(), CR())
            snap = mi.get_snapshot(AssetIdentifiers("BTC", "bitcoin", "BTC", "BTC"))
            self.assertEqual(snap["record_count"], 3)
            self.assertEqual(snap["source_status"][SOURCE_COINMARKETCAP], STATUS_OK)
            self.assertEqual(snap["source_status"][SOURCE_CRYPTORANK], STATUS_OK)
            self.assertEqual(len(agg.calls), 1)
            self.assertEqual(agg.calls[0][1]["price"], 65010)
            self.assertEqual(agg.calls[0][2]["market_cap"], 1.199e12)

        def test_no_identifier_fallback(self):
            agg = Agg(); mi = MarketIntelligence(agg, CG())
            snap = mi.get_snapshot(AssetIdentifiers(symbol="BTC"))
            self.assertEqual(snap["source_status"][SOURCE_COINGECKO], STATUS_SKIPPED_MISSING_IDENTIFIER)
            self.assertEqual(agg.calls, [[]])

        def test_one_source_failure_isolated(self):
            agg = Agg(); mi = MarketIntelligence(agg, CG(), cryptorank_client=Failing())
            snap = mi.get_snapshot(AssetIdentifiers("BTC", "bitcoin", cryptorank_symbol="BTC"))
            self.assertEqual(snap["source_status"][SOURCE_COINGECKO], STATUS_OK)
            self.assertEqual(snap["source_status"][SOURCE_CRYPTORANK], STATUS_ERROR)
            self.assertNotIn("SECRETVALUE", snap["errors"][SOURCE_CRYPTORANK])

        def test_optional_sources_and_single_aggregate(self):
            agg = Agg(); mi = MarketIntelligence(agg, sosovalue_client=Soso(), geckoterminal_client=Gecko())
            snap = mi.get_snapshot(AssetIdentifiers("BTC"), ETFRequest("BTC", "US"),
                                   PoolRequest("eth", "0xabc"))
            self.assertEqual(snap["source_status"][SOURCE_SOSOVALUE], STATUS_OK)
            self.assertEqual(snap["source_status"][SOURCE_GECKOTERMINAL], STATUS_OK)
            self.assertEqual(len(agg.calls), 1)
            pool = [r for r in agg.calls[0] if r["category"] == CATEGORY_POOL][0]
            self.assertIsNone(pool["timestamp"])
            self.assertEqual(pool["pool_created_at"], "2024-01-01T00:00:00Z")

        def test_aggregator_failure_isolated(self):
            agg = Agg(True); mi = MarketIntelligence(agg, CG())
            snap = mi.get_snapshot(AssetIdentifiers("BTC", "bitcoin"))
            self.assertIn("aggregator", snap["errors"])

    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Tests))
    if not result.wasSuccessful():
        raise SystemExit(1)


if __name__ == "__main__":
    _run_tests()
