"""
market_data_aggregator.py

Conservative, provenance-preserving reconciliation layer for ATHENA's
independent market-intelligence adapters.

This module consumes already-normalized dictionaries. It performs no network
I/O, imports no source client, and has no trading/SMC/BingX/derivatives logic.

The aggregator never changes source records and never turns market-data
agreement or disagreement into a trading decision.
"""

from __future__ import annotations

import copy
import math
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


SOURCE_CATEGORIES: Dict[str, str] = {
    "coingecko": "market",
    "coinmarketcap": "market",
    "cryptorank": "market",
    "sosovalue": "etf_flow",
    "geckoterminal": "pool",
}

MARKET_FIELD_ALIASES: Dict[str, Dict[str, Tuple[str, ...]]] = {
    "price": {
        "coingecko": ("current_price", "price"),
        "coinmarketcap": ("price",),
        "cryptorank": ("price",),
    },
    "market_cap": {
        "coingecko": ("market_cap",),
        "coinmarketcap": ("market_cap",),
        "cryptorank": ("market_cap",),
    },
    "volume_24h": {
        "coingecko": ("total_volume", "volume_24h"),
        "coinmarketcap": ("volume_24h",),
        "cryptorank": ("volume_24h",),
    },
    "circulating_supply": {
        "coingecko": ("circulating_supply",),
        "coinmarketcap": ("circulating_supply",),
        "cryptorank": ("circulating_supply",),
    },
    "total_supply": {
        "coingecko": ("total_supply",),
        "coinmarketcap": ("total_supply",),
        "cryptorank": ("total_supply",),
    },
    "max_supply": {
        "coingecko": ("max_supply",),
        "coinmarketcap": ("max_supply",),
        "cryptorank": ("max_supply",),
    },
    "fully_diluted_market_cap": {
        "coingecko": ("fully_diluted_valuation",),
        "coinmarketcap": ("fully_diluted_market_cap",),
        "cryptorank": ("fully_diluted_market_cap",),
    },
    "percent_change_24h": {
        "coingecko": ("price_change_percentage_24h",),
        "coinmarketcap": ("percent_change_24h",),
        "cryptorank": ("percent_change_24h",),
    },
}

CATEGORY_RECONCILE_FIELDS: Dict[str, Tuple[str, ...]] = {
    "market": tuple(MARKET_FIELD_ALIASES.keys()),
    "etf_flow": (
        "total_net_inflow",
        "total_value_traded",
        "total_net_assets",
        "cum_net_inflow",
    ),
    "pool": (
        "base_token_price_usd",
        "quote_token_price_usd",
        "reserve_in_usd",
        "fdv_usd",
        "market_cap_usd",
    ),
}

DEFAULT_REL_TOLERANCE = 0.005
DEFAULT_ABS_TOLERANCE = 1e-12

_TIMESTAMP_KEYS = (
    "last_updated",
    "source_updated_at",
    "fetched_at",
    "date",
    "pool_created_at",
)


class MarketDataAggregator:
    """Conservatively group and reconcile normalized source observations."""

    def __init__(
        self,
        relative_tolerance: float = DEFAULT_REL_TOLERANCE,
        absolute_tolerance: float = DEFAULT_ABS_TOLERANCE,
    ) -> None:
        if relative_tolerance < 0:
            raise ValueError("relative_tolerance must be >= 0")
        if absolute_tolerance < 0:
            raise ValueError("absolute_tolerance must be >= 0")
        self.relative_tolerance = float(relative_tolerance)
        self.absolute_tolerance = float(absolute_tolerance)

    def aggregate(
        self,
        records: Sequence[Mapping[str, Any]]
        | Mapping[str, Mapping[str, Any] | Sequence[Mapping[str, Any]]],
    ) -> Dict[str, Any]:
        """
        Aggregate one logical batch of normalized observations.

        Accepted inputs:
          * list/tuple of records, where each record has a ``source`` key;
          * mapping of source name -> record or sequence of records.

        The list form is canonical and allows one source to contribute more
        than one observation (e.g. multiple DEX pools).

        The returned structure contains category-local source provenance,
        conservative consensus values, explicit conflicts, and timestamps.
        """
        normalized = self._normalize_input(records)

        categories: Dict[str, Dict[str, Any]] = {}
        for category in ("market", "etf_flow", "pool", "other"):
            category_records = [
                record
                for record in normalized
                if self._category_for_source(record.get("source")) == category
            ]
            if category_records:
                categories[category] = self._aggregate_category(
                    category, category_records
                )

        return {
            "categories": categories,
            "record_count": len(normalized),
            "source_count": len({r.get("source") for r in normalized}),
        }

    def _normalize_input(
        self,
        records: Sequence[Mapping[str, Any]]
        | Mapping[str, Mapping[str, Any] | Sequence[Mapping[str, Any]]],
    ) -> List[Dict[str, Any]]:
        if isinstance(records, Mapping):
            output: List[Dict[str, Any]] = []
            for source_name, value in records.items():
                source = str(source_name).strip().lower()
                if isinstance(value, Mapping):
                    candidates: Iterable[Any] = (value,)
                elif isinstance(value, Sequence) and not isinstance(
                    value, (str, bytes, bytearray)
                ):
                    candidates = value
                else:
                    candidates = ()

                for item in candidates:
                    if not isinstance(item, Mapping):
                        continue
                    record = dict(item)
                    existing_source = record.get("source")
                    if existing_source is None:
                        record["source"] = source
                    else:
                        record["source"] = str(existing_source).strip().lower()
                    output.append(record)
            return output

        if isinstance(records, (str, bytes, bytearray)) or not isinstance(
            records, Sequence
        ):
            raise TypeError(
                "records must be a sequence of mappings or a source mapping"
            )

        output = []
        for item in records:
            if not isinstance(item, Mapping):
                continue
            record = dict(item)
            source = record.get("source")
            if source is not None:
                record["source"] = str(source).strip().lower()
            output.append(record)
        return output

    @staticmethod
    def _category_for_source(source: Any) -> str:
        if source is None:
            return "other"
        return SOURCE_CATEGORIES.get(str(source).strip().lower(), "other")

    def _aggregate_category(
        self,
        category: str,
        records: Sequence[Mapping[str, Any]],
    ) -> Dict[str, Any]:
        sources: Dict[str, Any] = {}
        for record in records:
            source = str(record.get("source") or "unknown").strip().lower()
            stored = copy.deepcopy(dict(record))
            if source not in sources:
                sources[source] = stored
            elif isinstance(sources[source], list):
                sources[source].append(stored)
            else:
                sources[source] = [sources[source], stored]

        result: Dict[str, Any] = {
            "sources": sources,
            "consensus": {},
            "conflicts": [],
            "unreconciled": [],
            "missing": [],
            "freshness": self._freshness(records),
        }

        if category == "other":
            result["reconciliation"] = "not_attempted"
            return result

        result["reconciliation"] = "conservative"
        for field in CATEGORY_RECONCILE_FIELDS[category]:
            observations = self._field_observations(category, field, records)
            if not observations:
                result["missing"].append(field)
                continue

            # Do not compare multiple observations from the same source.
            # SoSoValue returns time-series rows and GeckoTerminal can
            # legitimately supply multiple pools; those are not independent
            # corroborating observations of one scalar value.
            source_counts: Dict[str, int] = {}
            for item in observations:
                source_counts[item["source"]] = source_counts.get(item["source"], 0) + 1

            if any(count > 1 for count in source_counts.values()):
                result["unreconciled"].append(
                    {
                        "field": field,
                        "observation_count": len(observations),
                        "sources": source_counts,
                        "reason": "multiple_observations_from_same_source",
                    }
                )
                continue

            values = [item["value"] for item in observations]
            if self._values_agree(values):
                result["consensus"][field] = values[0]
            else:
                result["conflicts"].append(
                    {
                        "field": field,
                        "sources": {
                            item["source"]: item["value"]
                            for item in observations
                        },
                        "observation_count": len(observations),
                        "reason": "material_numeric_disagreement",
                    }
                )

        return result

    def _field_observations(
        self,
        category: str,
        canonical_field: str,
        records: Sequence[Mapping[str, Any]],
    ) -> List[Dict[str, Any]]:
        observations: List[Dict[str, Any]] = []

        if category == "market":
            aliases_by_source = MARKET_FIELD_ALIASES[canonical_field]
        else:
            aliases_by_source = {
                source: (canonical_field,)
                for source in {
                    str(r.get("source") or "").strip().lower() for r in records
                }
            }

        for record in records:
            source = str(record.get("source") or "").strip().lower()
            aliases = aliases_by_source.get(source, (canonical_field,))
            value = self._first_present(record, aliases)
            if not self._is_numeric(value):
                continue
            observations.append({"source": source or "unknown", "value": float(value)})

        return observations

    @staticmethod
    def _first_present(
        record: Mapping[str, Any],
        aliases: Iterable[str],
    ) -> Any:
        for field in aliases:
            if field in record and record[field] is not None:
                return record[field]
        return None

    def _values_agree(self, values: Sequence[float]) -> bool:
        if len(values) <= 1:
            return True
        if any(not math.isfinite(value) for value in values):
            return False

        reference = values[0]
        for value in values[1:]:
            scale = max(abs(reference), abs(value), self.absolute_tolerance)
            if abs(reference - value) > max(
                self.absolute_tolerance,
                self.relative_tolerance * scale,
            ):
                return False
        return True

    @staticmethod
    def _is_numeric(value: Any) -> bool:
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
        )

    @staticmethod
    def _freshness(records: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        by_source: Dict[str, Any] = {}

        for record in records:
            source = str(record.get("source") or "unknown").strip().lower()
            timestamps = {
                key: record.get(key)
                for key in _TIMESTAMP_KEYS
                if record.get(key) is not None
            }

            parsed_source_time = None
            for key in ("last_updated", "source_updated_at", "date"):
                parsed_source_time = MarketDataAggregator._parse_timestamp(
                    record.get(key)
                )
                if parsed_source_time is not None:
                    break

            fetched_at = MarketDataAggregator._parse_timestamp(
                record.get("fetched_at")
            )

            item: Dict[str, Any] = {"timestamps": timestamps}
            if parsed_source_time is not None:
                item["source_timestamp"] = parsed_source_time
                item["source_age_seconds"] = max(
                    0.0, time.time() - parsed_source_time
                )
            if fetched_at is not None:
                item["fetched_timestamp"] = fetched_at
                item["fetch_age_seconds"] = max(
                    0.0, time.time() - fetched_at
                )

            if source not in by_source:
                by_source[source] = item
            elif isinstance(by_source[source], list):
                by_source[source].append(item)
            else:
                by_source[source] = [by_source[source], item]

        return by_source

    @staticmethod
    def _parse_timestamp(value: Any) -> Optional[float]:
        if value is None or isinstance(value, bool):
            return None

        if isinstance(value, (int, float)):
            numeric = float(value)
            if not math.isfinite(numeric):
                return None
            if numeric > 10_000_000_000:
                numeric /= 1000.0
            return numeric

        if not isinstance(value, str):
            return None

        text = value.strip()
        if not text:
            return None

        try:
            numeric = float(text)
            if math.isfinite(numeric):
                if numeric > 10_000_000_000:
                    numeric /= 1000.0
                return numeric
        except ValueError:
            pass

        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()


def get_aggregator(
    relative_tolerance: float = DEFAULT_REL_TOLERANCE,
    absolute_tolerance: float = DEFAULT_ABS_TOLERANCE,
) -> MarketDataAggregator:
    """Return a configured, dependency-free MarketDataAggregator."""
    return MarketDataAggregator(
        relative_tolerance=relative_tolerance,
        absolute_tolerance=absolute_tolerance,
    )


def run_tests() -> Dict[str, str]:
    """Run lightweight self-tests without network access or third-party tools."""
    tests: Dict[str, str] = {}

    def check(name: str, condition: bool) -> None:
        if not condition:
            raise AssertionError(name)
        tests[name] = "PASS"

    aggregator = get_aggregator()

    single = aggregator.aggregate(
        [{"source": "coingecko", "symbol": "BTC", "current_price": 100000.0}]
    )
    check(
        "one_source",
        single["categories"]["market"]["consensus"]["price"] == 100000.0,
    )

    agreeing = aggregator.aggregate(
        [
            {"source": "coingecko", "current_price": 100000.0},
            {"source": "coinmarketcap", "price": 100100.0},
            {"source": "cryptorank", "price": 100050.0},
        ]
    )
    check(
        "agreeing_sources",
        agreeing["categories"]["market"]["consensus"]["price"] == 100000.0,
    )
    check(
        "provenance_preserved",
        len(agreeing["categories"]["market"]["sources"]) == 3,
    )

    conflicting = aggregator.aggregate(
        [
            {"source": "coingecko", "current_price": 100000.0},
            {"source": "coinmarketcap", "price": 103000.0},
        ]
    )
    check(
        "numeric_conflict",
        conflicting["categories"]["market"]["consensus"] == {},
    )
    check(
        "conflict_explicit",
        conflicting["categories"]["market"]["conflicts"][0]["field"] == "price",
    )

    missing = aggregator.aggregate(
        [{"source": "coingecko", "current_price": 100000.0}]
    )
    check(
        "missing_field",
        "market_cap" in missing["categories"]["market"]["missing"],
    )

    source_specific = aggregator.aggregate(
        [
            {
                "source": "sosovalue",
                "symbol": "BTC",
                "date": "2026-09-05",
                "total_net_inflow": 123.0,
            }
        ]
    )
    check(
        "source_specific_field",
        source_specific["categories"]["etf_flow"]["sources"]["sosovalue"][
            "total_net_inflow"
        ]
        == 123.0,
    )

    timestamped = aggregator.aggregate(
        [
            {
                "source": "coingecko",
                "current_price": 100000.0,
                "last_updated": "2026-09-05T12:00:00Z",
                "fetched_at": 1788609600,
            }
        ]
    )
    freshness = timestamped["categories"]["market"]["freshness"]["coingecko"]
    check("timestamp_preserved", "last_updated" in freshness["timestamps"])
    check("freshness_exposed", "source_timestamp" in freshness)

    malformed = aggregator.aggregate(
        [
            None,  # type: ignore[list-item]
            {"source": "coingecko", "current_price": "not-a-number"},
            {"source": "coinmarketcap", "price": 100000.0},
        ]
    )
    check(
        "malformed_input",
        malformed["categories"]["market"]["consensus"].get("price") == 100000.0,
    )

    # Assert that the result contains no trading/execution vocabulary.
    forbidden = (
        "trade_signal",
        "entry_signal",
        "execution_ready",
        "signal",
        "buy",
        "sell",
    )
    rendered = str(agreeing).lower()
    check(
        "no_trading_fields_introduced",
        not any(token in rendered for token in forbidden),
    )

    # Verify that input records are not mutated.
    original = {"source": "coingecko", "current_price": 100000.0}
    snapshot = copy.deepcopy(original)
    aggregator.aggregate([original])
    check("input_not_mutated", original == snapshot)

    # Multiple rows from one source must not be treated as cross-source
    # disagreement. This covers ETF time-series rows and multiple DEX pools.
    multi_same_source = aggregator.aggregate(
        [
            {
                "source": "sosovalue",
                "symbol": "BTC",
                "date": "2026-09-05",
                "total_net_inflow": 100.0,
            },
            {
                "source": "sosovalue",
                "symbol": "BTC",
                "date": "2026-09-04",
                "total_net_inflow": 200.0,
            },
        ]
    )
    etf_result = multi_same_source["categories"]["etf_flow"]
    check(
        "same_source_timeseries_not_reconciled",
        "total_net_inflow" not in etf_result["consensus"]
        and etf_result["unreconciled"][0]["reason"]
        == "multiple_observations_from_same_source",
    )

    return tests


if __name__ == "__main__":
    results = run_tests()
    for name, status in results.items():
        print(f"{status}: {name}")
