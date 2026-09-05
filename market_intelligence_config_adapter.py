"""
market_intelligence_config_adapter.py

Small conversion layer between the explicit market-intelligence
configuration contract and the existing market_intelligence.py request
dataclasses.

This module performs no network I/O and never resolves or infers identifiers.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from market_intelligence import AssetIdentifiers, ETFRequest, PoolRequest
from market_intelligence_config import (
    ETFRequestConfig,
    MarketAssetConfig,
    MarketIntelligenceConfig,
    PoolRequestConfig,
)


@dataclass(frozen=True)
class MarketAssetRequests:
    """Converted requests for one configured asset."""

    identifiers: AssetIdentifiers
    etf_request: Optional[ETFRequest]
    pool_requests: List[PoolRequest]


def to_asset_identifiers(asset: MarketAssetConfig) -> AssetIdentifiers:
    """Convert one validated config asset without applying any fallback."""
    asset.validate()
    return AssetIdentifiers(
        symbol=asset.symbol,
        coingecko_id=asset.coingecko_id,
        cmc_symbol=asset.cmc_symbol,
        cryptorank_symbol=asset.cryptorank_symbol,
    )


def to_etf_request(request: Optional[ETFRequestConfig]) -> Optional[ETFRequest]:
    """Convert an optional ETF configuration request exactly as supplied."""
    if request is None:
        return None
    request.validate()
    extra_params = {}
    if request.start_date is not None:
        extra_params["start_date"] = request.start_date
    if request.end_date is not None:
        extra_params["end_date"] = request.end_date
    if request.limit is not None:
        extra_params["limit"] = request.limit
    return ETFRequest(
        symbol=request.symbol,
        country_code=request.country_code,
        extra_params=extra_params,
    )


def to_pool_request(request: PoolRequestConfig) -> PoolRequest:
    """Convert one validated pool configuration request exactly as supplied."""
    request.validate()
    return PoolRequest(
        network=request.network,
        pool_address=request.pool_address,
    )


def to_asset_requests(asset: MarketAssetConfig) -> MarketAssetRequests:
    """Convert one configured asset into existing market-intelligence requests."""
    asset.validate()
    return MarketAssetRequests(
        identifiers=to_asset_identifiers(asset),
        etf_request=to_etf_request(asset.etf_request),
        pool_requests=[to_pool_request(req) for req in asset.pool_requests],
    )


def to_request_bundle(config: MarketIntelligenceConfig) -> List[MarketAssetRequests]:
    """Convert every configured asset, preserving order and explicit values."""
    config.validate()
    return [to_asset_requests(asset) for asset in config.assets]


def _run_tests() -> None:
    import unittest

    class AdapterTests(unittest.TestCase):
        def test_generic_symbol_is_not_used_as_source_identifier(self):
            asset = MarketAssetConfig(symbol="BTC")
            converted = to_asset_identifiers(asset)

            self.assertEqual(converted.symbol, "BTC")
            self.assertIsNone(converted.coingecko_id)
            self.assertIsNone(converted.cmc_symbol)
            self.assertIsNone(converted.cryptorank_symbol)

        def test_explicit_identifiers_are_preserved(self):
            asset = MarketAssetConfig(
                symbol="BTC",
                coingecko_id="bitcoin",
                cmc_symbol="BTC",
                cryptorank_symbol="BTC",
            )
            converted = to_asset_identifiers(asset)

            self.assertEqual(converted.coingecko_id, "bitcoin")
            self.assertEqual(converted.cmc_symbol, "BTC")
            self.assertEqual(converted.cryptorank_symbol, "BTC")

        def test_missing_optional_requests_stay_none_or_empty(self):
            converted = to_asset_requests(MarketAssetConfig(symbol="ETH"))

            self.assertIsNone(converted.etf_request)
            self.assertEqual(converted.pool_requests, [])

        def test_etf_request_is_field_for_field_conversion(self):
            source = ETFRequestConfig(
                symbol="BTC",
                country_code="US",
                start_date="2026-01-01",
                end_date="2026-02-01",
                limit=25,
            )
            converted = to_etf_request(source)

            self.assertIsNotNone(converted)
            self.assertEqual(converted.symbol, source.symbol)
            self.assertEqual(converted.country_code, source.country_code)
            self.assertEqual(
                converted.extra_params,
                {
                    "start_date": "2026-01-01",
                    "end_date": "2026-02-01",
                    "limit": 25,
                },
            )

        def test_multiple_pool_requests_are_preserved_in_order(self):
            asset = MarketAssetConfig(
                symbol="ETH",
                pool_requests=[
                    PoolRequestConfig(network="eth", pool_address="0xone"),
                    PoolRequestConfig(network="base", pool_address="0xtwo"),
                ],
            )
            converted = to_asset_requests(asset)

            self.assertEqual(len(converted.pool_requests), 2)
            self.assertEqual(converted.pool_requests[0].network, "eth")
            self.assertEqual(converted.pool_requests[0].pool_address, "0xone")
            self.assertEqual(converted.pool_requests[1].network, "base")
            self.assertEqual(converted.pool_requests[1].pool_address, "0xtwo")

        def test_invalid_config_is_rejected_before_conversion(self):
            with self.assertRaises(ValueError):
                to_asset_identifiers(MarketAssetConfig(symbol=""))

        def test_config_bundle_preserves_asset_order(self):
            config = MarketIntelligenceConfig(
                assets=[
                    MarketAssetConfig(symbol="BTC", coingecko_id="bitcoin"),
                    MarketAssetConfig(symbol="ETH", cmc_symbol="ETH"),
                ]
            )
            bundle = to_request_bundle(config)

            self.assertEqual([x.identifiers.symbol for x in bundle], ["BTC", "ETH"])
            self.assertEqual(bundle[0].identifiers.coingecko_id, "bitcoin")
            self.assertEqual(bundle[1].identifiers.cmc_symbol, "ETH")

        def test_no_network_modules_are_imported_by_adapter_source(self):
            source = Path(__file__).read_text(encoding="utf-8")
            production = source.split("def _run_tests() -> None:", 1)[0]

            for forbidden in (
                "requests",
                "urllib",
                "http.client",
                "socket",
                "aiohttp",
                "ccxt",
            ):
                self.assertNotIn(f"import {forbidden}", production)
                self.assertNotIn(f"from {forbidden}", production)

        def test_adapter_does_not_import_source_clients_or_smc_layers(self):
            source = Path(__file__).read_text(encoding="utf-8")
            production = source.split("def _run_tests() -> None:", 1)[0]

            for forbidden in (
                "coingecko_client",
                "coinmarketcap_client",
                "cryptorank_client",
                "sosovalue_client",
                "geckoterminal_client",
                "market_data_aggregator",
                "smc_scanner",
                "bingx_position_tracker",
                "position_health",
                "full_scan",
            ):
                self.assertNotIn(forbidden, production)

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(AdapterTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        raise SystemExit(1)


if __name__ == "__main__":
    _run_tests()
