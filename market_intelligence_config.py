"""
market_intelligence_config.py

Reusable, dependency-free configuration structures for defining ATHENA
market-intelligence assets and optional data requests.

This is a data/configuration contract ONLY. It contains:
    - no HTTP/network code
    - no API clients
    - no SMC logic
    - no trading/order/BingX logic
    - no environment-variable loading
    - no API keys
    - no source-client imports

It never calculates or modifies SMC score, direction, entry, SL, TP,
execution state, or trade classification, and it never infers a
source-specific identifier (coingecko_id, cmc_symbol, cryptorank_symbol,
pool address, ETF symbol) from the generic `symbol` field. A missing
source identifier stays missing so market_intelligence.py can skip that
source cleanly (SKIPPED_MISSING_IDENTIFIER) -- this file performs no
guessing on its behalf.

These dataclasses are intentionally structured to be easy to convert
into market_intelligence.py's AssetIdentifiers / ETFRequest / PoolRequest
later, without importing market_intelligence.py here (avoids circular
imports; conversion is the orchestration layer's job, not this file's).

Python standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


class MarketIntelligenceConfigError(ValueError):
    """Raised when a configuration object fails lightweight validation.

    This validates SHAPE only (blank/missing required values) -- it
    never checks whether an identifier actually exists at an external
    provider. That verification belongs to the source clients.
    """


@dataclass
class ETFRequestConfig:
    """
    Configuration for an optional SoSoValue ETF-flow data request.

    Mirrors market_intelligence.py's ETFRequest fields so conversion is
    a straight field-for-field copy, without importing that module here.
    """
    symbol: str
    country_code: str
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    limit: Optional[int] = None

    def validate(self) -> None:
        if not self.symbol or not self.symbol.strip():
            raise MarketIntelligenceConfigError(
                "ETFRequestConfig.symbol must not be empty/blank"
            )
        if not self.country_code or not self.country_code.strip():
            raise MarketIntelligenceConfigError(
                "ETFRequestConfig.country_code must not be empty/blank"
            )


@dataclass
class PoolRequestConfig:
    """
    Configuration for an optional GeckoTerminal pool lookup.

    Mirrors market_intelligence.py's PoolRequest fields. Pool addresses
    are never generated or inferred -- they must be supplied explicitly.
    """
    network: str
    pool_address: str

    def validate(self) -> None:
        if not self.network or not self.network.strip():
            raise MarketIntelligenceConfigError(
                "PoolRequestConfig.network must not be empty/blank"
            )
        if not self.pool_address or not self.pool_address.strip():
            raise MarketIntelligenceConfigError(
                "PoolRequestConfig.pool_address must not be empty/blank"
            )


@dataclass
class MarketAssetConfig:
    """
    Configuration for a single logical asset's market-intelligence
    identifiers and optional data requests.

    `symbol` is a generic/display ticker only. It is NEVER substituted
    for a missing source-specific identifier -- each of `coingecko_id`,
    `cmc_symbol`, and `cryptorank_symbol` must be supplied explicitly or
    is left as None, meaning that source is skipped by the orchestration
    layer.
    """
    symbol: str
    coingecko_id: Optional[str] = None
    cmc_symbol: Optional[str] = None
    cryptorank_symbol: Optional[str] = None
    etf_request: Optional[ETFRequestConfig] = None
    pool_requests: List[PoolRequestConfig] = field(default_factory=list)

    def validate(self) -> None:
        if not self.symbol or not self.symbol.strip():
            raise MarketIntelligenceConfigError(
                "MarketAssetConfig.symbol must not be empty/blank"
            )
        # No fallback/inference logic here: a None source identifier is
        # a valid, meaningful configuration (that source is skipped).
        if self.etf_request is not None:
            self.etf_request.validate()
        for pool_request in self.pool_requests:
            pool_request.validate()


@dataclass
class MarketIntelligenceConfig:
    """
    Top-level container for one or more MarketAssetConfig entries.

    Kept intentionally minimal -- a plain list container, not a
    registry, resolver, or lookup index.
    """
    assets: List[MarketAssetConfig] = field(default_factory=list)

    def validate(self) -> None:
        for asset in self.assets:
            asset.validate()


# ---------------------------------------------------------------------------
# Embedded lightweight tests (no network, no real API keys, no pytest dep)
# ---------------------------------------------------------------------------

def _run_tests() -> None:
    import unittest

    class MarketAssetConfigTests(unittest.TestCase):
        def test_valid_asset_with_only_generic_symbol(self):
            asset = MarketAssetConfig(symbol="BTC")
            asset.validate()  # must not raise
            self.assertEqual(asset.symbol, "BTC")
            self.assertIsNone(asset.coingecko_id)
            self.assertIsNone(asset.cmc_symbol)
            self.assertIsNone(asset.cryptorank_symbol)

        def test_valid_asset_with_explicit_source_identifiers(self):
            asset = MarketAssetConfig(
                symbol="BTC",
                coingecko_id="bitcoin",
                cmc_symbol="BTC",
                cryptorank_symbol="BTC",
            )
            asset.validate()  # must not raise
            self.assertEqual(asset.coingecko_id, "bitcoin")
            self.assertEqual(asset.cmc_symbol, "BTC")
            self.assertEqual(asset.cryptorank_symbol, "BTC")

        def test_missing_coingecko_id_remains_none(self):
            asset = MarketAssetConfig(symbol="BTC", cmc_symbol="BTC")
            asset.validate()  # must not raise
            self.assertIsNone(asset.coingecko_id)

        def test_generic_symbol_never_copied_into_source_ids(self):
            asset = MarketAssetConfig(symbol="BTC")
            asset.validate()
            # The explicit-ID rule: symbol must NEVER leak into any
            # source-specific identifier field, by construction.
            self.assertIsNone(asset.coingecko_id)
            self.assertIsNone(asset.cmc_symbol)
            self.assertIsNone(asset.cryptorank_symbol)
            self.assertNotEqual(asset.coingecko_id, asset.symbol.lower())

        def test_blank_symbol_is_rejected(self):
            asset = MarketAssetConfig(symbol="   ")
            with self.assertRaises(MarketIntelligenceConfigError):
                asset.validate()

        def test_empty_symbol_is_rejected(self):
            asset = MarketAssetConfig(symbol="")
            with self.assertRaises(MarketIntelligenceConfigError):
                asset.validate()

        def test_invalid_pool_request_is_rejected(self):
            asset = MarketAssetConfig(
                symbol="ETH",
                pool_requests=[PoolRequestConfig(network="eth", pool_address="")],
            )
            with self.assertRaises(MarketIntelligenceConfigError):
                asset.validate()

        def test_invalid_pool_request_blank_network_is_rejected(self):
            asset = MarketAssetConfig(
                symbol="ETH",
                pool_requests=[PoolRequestConfig(network="  ", pool_address="0xabc")],
            )
            with self.assertRaises(MarketIntelligenceConfigError):
                asset.validate()

        def test_valid_pool_request_passes(self):
            asset = MarketAssetConfig(
                symbol="ETH",
                pool_requests=[PoolRequestConfig(network="eth", pool_address="0xabc")],
            )
            asset.validate()  # must not raise

        def test_invalid_etf_request_is_rejected(self):
            asset = MarketAssetConfig(
                symbol="BTC",
                etf_request=ETFRequestConfig(symbol="BTC", country_code=""),
            )
            with self.assertRaises(MarketIntelligenceConfigError):
                asset.validate()

        def test_invalid_etf_request_blank_symbol_is_rejected(self):
            asset = MarketAssetConfig(
                symbol="BTC",
                etf_request=ETFRequestConfig(symbol="  ", country_code="US"),
            )
            with self.assertRaises(MarketIntelligenceConfigError):
                asset.validate()

        def test_valid_etf_request_passes(self):
            asset = MarketAssetConfig(
                symbol="BTC",
                etf_request=ETFRequestConfig(symbol="BTC", country_code="US"),
            )
            asset.validate()  # must not raise

    class MarketIntelligenceConfigTests(unittest.TestCase):
        def test_multiple_assets_can_coexist(self):
            config = MarketIntelligenceConfig(
                assets=[
                    MarketAssetConfig(symbol="BTC", coingecko_id="bitcoin"),
                    MarketAssetConfig(symbol="ETH", cmc_symbol="ETH"),
                    MarketAssetConfig(symbol="DOGE"),
                ]
            )
            config.validate()  # must not raise
            self.assertEqual(len(config.assets), 3)
            self.assertEqual(config.assets[0].symbol, "BTC")
            self.assertEqual(config.assets[2].coingecko_id, None)

        def test_one_invalid_asset_fails_whole_config_validation(self):
            config = MarketIntelligenceConfig(
                assets=[
                    MarketAssetConfig(symbol="BTC"),
                    MarketAssetConfig(symbol=""),
                ]
            )
            with self.assertRaises(MarketIntelligenceConfigError):
                config.validate()

        def test_empty_config_is_valid(self):
            config = MarketIntelligenceConfig()
            config.validate()  # must not raise
            self.assertEqual(config.assets, [])

        def test_no_network_or_api_behavior(self):
            # This module must expose no HTTP/network primitives at all --
            # confirm no such names leak into its public surface.
            import market_intelligence_config as mic

            forbidden_names = (
                "requests",
                "urllib",
                "http",
                "socket",
                "aiohttp",
            )
            module_attrs = dir(mic)
            for name in forbidden_names:
                self.assertNotIn(name, module_attrs)

        def test_no_source_client_or_smc_or_bingx_imports(self):
            with open(__file__, "r", encoding="utf-8") as f:
                source_text = f.read()
            production_code = source_text.split("def _run_tests() -> None:")[0]
            for banned_import in (
                "import coingecko_client",
                "from coingecko_client",
                "import coinmarketcap_client",
                "from coinmarketcap_client",
                "import cryptorank_client",
                "from cryptorank_client",
                "import sosovalue_client",
                "from sosovalue_client",
                "import geckoterminal_client",
                "from geckoterminal_client",
                "import market_data_aggregator",
                "from market_data_aggregator",
                "import market_intelligence",
                "from market_intelligence",
                "import smc_scanner",
                "from smc_scanner",
                "import bingx_position_tracker",
                "from bingx_position_tracker",
                "import full_scan",
                "from full_scan",
                "import position_health",
                "from position_health",
            ):
                self.assertNotIn(banned_import, production_code)

    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(MarketAssetConfigTests))
    suite.addTests(loader.loadTestsFromTestCase(MarketIntelligenceConfigTests))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    if not result.wasSuccessful():
        raise SystemExit(1)


if __name__ == "__main__":
    _run_tests()
