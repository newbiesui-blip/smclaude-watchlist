```python
"""
market_intelligence_config.py

Explicit configuration contract for market-intelligence sources.

This module contains configuration shapes and validation only.
It performs no network I/O and does not import source clients.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


class MarketIntelligenceConfigError(ValueError):
    """Raised when market-intelligence configuration is invalid."""


@dataclass(frozen=True)
class ETFRequestConfig:
    """Explicit configuration for an ETF-flow request."""

    symbol: str
    country_code: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    limit: Optional[int] = None

    def validate(self) -> None:
        if not self.symbol:
            raise MarketIntelligenceConfigError(
                "ETF symbol must be explicitly provided."
            )

        if self.country_code is not None and not self.country_code:
            raise MarketIntelligenceConfigError(
                "ETF country_code cannot be empty when supplied."
            )

        if self.limit is not None and self.limit <= 0:
            raise MarketIntelligenceConfigError(
                "ETF limit must be greater than zero when supplied."
            )


@dataclass(frozen=True)
class PoolRequestConfig:
    """Explicit configuration for a GeckoTerminal pool request."""

    network: str
    pool_address: str

    def validate(self) -> None:
        if not self.network:
            raise MarketIntelligenceConfigError(
                "Pool network must be explicitly provided."
            )

        if not self.pool_address:
            raise MarketIntelligenceConfigError(
                "Pool address must be explicitly provided."
            )


@dataclass(frozen=True)
class MarketAssetConfig:
    """Explicit source identifiers and optional source requests for one asset."""

    symbol: str
    coingecko_id: Optional[str] = None
    cmc_symbol: Optional[str] = None
    cryptorank_symbol: Optional[str] = None
    etf_request: Optional[ETFRequestConfig] = None
    pool_requests: List[PoolRequestConfig] = field(default_factory=list)

    def validate(self) -> None:
        if not self.symbol:
            raise MarketIntelligenceConfigError(
                "Asset symbol must be explicitly provided."
            )

        if self.coingecko_id is not None and not self.coingecko_id:
            raise MarketIntelligenceConfigError(
                "CoinGecko ID cannot be empty when supplied."
            )

        if self.cmc_symbol is not None and not self.cmc_symbol:
            raise MarketIntelligenceConfigError(
                "CoinMarketCap symbol cannot be empty when supplied."
            )

        if self.cryptorank_symbol is not None and not self.cryptorank_symbol:
            raise MarketIntelligenceConfigError(
                "CryptoRank symbol cannot be empty when supplied."
            )

        if self.etf_request is not None:
            self.etf_request.validate()

        for request in self.pool_requests:
            request.validate()


@dataclass(frozen=True)
class MarketIntelligenceConfig:
    """Top-level explicit market-intelligence configuration."""

    assets: List[MarketAssetConfig] = field(default_factory=list)

    def validate(self) -> None:
        for asset in self.assets:
            asset.validate()
```
