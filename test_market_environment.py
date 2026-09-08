import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import market_environment as env


NOW = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
GOOD_LIQUIDITY = {
    "spread_bps": 5,
    "volume_ratio": 1.0,
    "atr_ratio": 1.0,
    "participation_ratio": 1.0,
}


class MarketEnvironmentTests(unittest.TestCase):
    def test_normal_market_allows_new_entry(self):
        state = env.evaluate(now=NOW, events=[], liquidity=GOOD_LIQUIDITY)
        self.assertTrue(state.allowed)
        self.assertEqual(state.state, env.NORMAL)

    def test_fomc_blocks_new_entry(self):
        events = [{
            "name": "FOMC",
            "impact": "HIGH",
            "starts_at": "2026-09-07T11:00:00Z",
            "ends_at": "2026-09-07T13:00:00Z",
        }]
        state = env.evaluate(now=NOW, events=events, liquidity=GOOD_LIQUIDITY)
        self.assertFalse(state.allowed)
        self.assertEqual(state.reason, "HIGH_IMPACT_EVENT")
        self.assertEqual(state.event, "FOMC")

    def test_nfp_blocks_new_entry(self):
        events = [{
            "name": "NFP",
            "starts_at": "2026-09-07T11:00:00Z",
            "ends_at": "2026-09-07T13:00:00Z",
        }]
        state = env.evaluate(now=NOW, events=events, liquidity=GOOD_LIQUIDITY)
        self.assertFalse(state.allowed)

    def test_event_passed_allows_when_liquidity_is_good(self):
        events = [{
            "name": "CPI",
            "impact": "HIGH",
            "starts_at": "2026-09-07T09:00:00Z",
            "ends_at": "2026-09-07T10:00:00Z",
        }]
        state = env.evaluate(now=NOW, events=events, liquidity=GOOD_LIQUIDITY)
        self.assertTrue(state.allowed)

    def test_low_liquidity_blocks(self):
        poor = dict(GOOD_LIQUIDITY)
        poor["volume_ratio"] = 0.10
        state = env.evaluate(now=NOW, events=[], liquidity=poor)
        self.assertFalse(state.allowed)
        self.assertEqual(state.reason, "LOW_LIQUIDITY_VOLUME")

    def test_high_spread_blocks(self):
        poor = dict(GOOD_LIQUIDITY)
        poor["spread_bps"] = 30
        state = env.evaluate(now=NOW, events=[], liquidity=poor)
        self.assertFalse(state.allowed)
        self.assertEqual(state.reason, "LOW_LIQUIDITY_SPREAD")

    def test_unknown_event_data_fails_closed(self):
        state = env.evaluate(now=NOW, events=None, liquidity=GOOD_LIQUIDITY)
        self.assertFalse(state.allowed)

    def test_unknown_liquidity_data_fails_closed(self):
        state = env.evaluate(now=NOW, events=[], liquidity=None)
        self.assertFalse(state.allowed)

    def test_incomplete_liquidity_fails_closed(self):
        state = env.evaluate(now=NOW, events=[], liquidity={"spread_bps": 5})
        self.assertFalse(state.allowed)

    def test_weekend_policy_is_explicit(self):
        saturday = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
        state = env.evaluate(
            now=saturday, events=[], liquidity=GOOD_LIQUIDITY, block_weekends=True
        )
        self.assertFalse(state.allowed)
        self.assertEqual(state.reason, "CALENDAR_WEEKEND")

    def test_env_loader_is_fail_closed_when_unconfigured(self):
        with patch.dict(os.environ, {}, clear=True):
            state = env.load_state(now=NOW)
        self.assertFalse(state.allowed)

    def test_entry_gate_has_no_position_monitoring_dependency(self):
        # Architectural regression guard: the entry gate only returns policy
        # state; it has no registry/BingX calls or lifecycle mutations.
        state = env.evaluate(now=NOW, events=[], liquidity=GOOD_LIQUIDITY)
        self.assertEqual(set(state.as_dict()), {
            "allowed", "state", "reason", "event", "starts_at", "ends_at", "source"
        })


if __name__ == "__main__":
    unittest.main()
