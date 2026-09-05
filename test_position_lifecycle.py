"""Focused regression tests for ATHENA position lifecycle safeguards.

Run from the repository root with:
    python -m unittest discover -s tests -p "test_*.py" -v

These tests are read-only and never contact BingX.
"""

import copy
import unittest
from unittest.mock import patch

import bingx_position_tracker as bingx
import position_health


class TestPositionHealthGate(unittest.TestCase):
    def base_entry(self):
        return {
            "status": "triggered",
            "symbol": "BTC-USDT",
            "direction": "BULLISH",
            "exchange_sync_status": "OPEN",
            "reversal_state": "STABLE",
            "reversal_reason": "No active reversal evidence detected.",
            "current_r": 2.0,
            "max_r": 3.0,
        }

    def test_open_supportive_is_healthy(self):
        result = position_health.evaluate_position_health(
            self.base_entry(),
            {"state": "SUPPORTIVE", "score": 35},
        )
        self.assertTrue(result["active"])
        self.assertEqual(result["health_state"], "HEALTHY")

    def test_every_non_open_exchange_state_suppresses_health(self):
        for state in (
            "UNKNOWN",
            "ERROR",
            "NOT_FOUND",
            "NOT_MATCHED",
            "UNSYNCED",
            "CLOSED",
            "REVERSED",
        ):
            with self.subTest(state=state):
                entry = self.base_entry()
                entry["exchange_sync_status"] = state
                result = position_health.evaluate_position_health(
                    entry,
                    {"state": "SUPPORTIVE", "score": 35},
                )
                self.assertFalse(result["active"])
                self.assertIsNone(result["health_state"])

    def test_trap_plus_deterioration_is_exit_warning(self):
        entry = self.base_entry()
        entry["reversal_state"] = "MOMENTUM_DETERIORATION"
        result = position_health.evaluate_position_health(
            entry,
            {"state": "LONG_TRAP_RISK", "score": -55},
        )
        self.assertEqual(result["health_state"], "EXIT_WARNING")

    def test_same_side_crowding_is_caution(self):
        result = position_health.evaluate_position_health(
            self.base_entry(),
            {"state": "CROWDED_LONG", "score": -20},
        )
        self.assertEqual(result["health_state"], "CAUTION")

    def test_confirmed_reversal_is_exit_warning(self):
        entry = self.base_entry()
        entry["reversal_state"] = "CONFIRMED_REVERSAL"
        result = position_health.evaluate_position_health(
            entry,
            {"state": "NEUTRAL", "score": 0},
        )
        self.assertEqual(result["health_state"], "EXIT_WARNING")

    def test_risk_improvement_enters_recovery(self):
        entry = self.base_entry()
        entry["position_health_state"] = "ELEVATED_RISK"
        result = position_health.evaluate_position_health(
            entry,
            {"state": "SUPPORTIVE", "score": 50},
        )
        self.assertEqual(result["health_state"], "RECOVERY")

    def test_first_assignment_is_not_a_transition(self):
        entry = self.base_entry()
        snapshot, transitioned = position_health.apply_health(
            entry,
            {"state": "SUPPORTIVE", "score": 20},
        )
        self.assertEqual(snapshot["health_state"], "HEALTHY")
        self.assertFalse(transitioned)

    def test_deterioration_creates_one_transition(self):
        entry = self.base_entry()
        position_health.apply_health(
            entry,
            {"state": "SUPPORTIVE", "score": 20},
        )
        snapshot, transitioned = position_health.apply_health(
            entry,
            {"state": "CROWDED_LONG", "score": -20},
        )
        self.assertEqual(snapshot["health_state"], "CAUTION")
        self.assertTrue(transitioned)
        self.assertEqual(entry["position_health_state"], "CAUTION")


class TestBingXOpenMatching(unittest.TestCase):
    def test_missing_amount_is_not_open(self):
        position = {
            "symbol": "BTC-USDT",
            "positionSide": "LONG",
        }
        self.assertFalse(
            bingx._position_matches(position, "BTC-USDT", "BULLISH")
        )

    def test_zero_amount_is_not_open(self):
        position = {
            "symbol": "BTC-USDT",
            "positionSide": "LONG",
            "positionAmt": "0",
        }
        self.assertFalse(
            bingx._position_matches(position, "BTC-USDT", "BULLISH")
        )

    def test_positive_matching_amount_is_open(self):
        position = {
            "symbol": "BTC-USDT",
            "positionSide": "LONG",
            "positionAmt": "0.01",
        }
        self.assertTrue(
            bingx._position_matches(position, "BTC-USDT", "BULLISH")
        )

    def test_wrong_symbol_is_not_open(self):
        position = {
            "symbol": "ETH-USDT",
            "positionSide": "LONG",
            "positionAmt": "1",
        }
        self.assertFalse(
            bingx._position_matches(position, "BTC-USDT", "BULLISH")
        )

    def test_wrong_side_is_not_open(self):
        position = {
            "symbol": "BTC-USDT",
            "positionSide": "SHORT",
            "positionAmt": "1",
        }
        self.assertFalse(
            bingx._position_matches(position, "BTC-USDT", "BULLISH")
        )


class TestBingXClosureRecording(unittest.TestCase):
    def test_record_closed_preserves_generic_setup_status(self):
        entry = {
            "status": "triggered",
            "symbol": "BTC-USDT",
            "direction": "BULLISH",
        }
        history = {
            "status": "FILLED",
            "orderType": "STOP_LOSS",
            "closeTime": 1760000000000,
            "avgPrice": "100000",
            "orderId": "abc123",
        }

        bingx._record_closed(entry, history)

        self.assertEqual(entry["status"], "triggered")
        self.assertEqual(entry["exchange_sync_status"], "CLOSED")
        self.assertEqual(entry["position_lifecycle"], "CLOSED")
        self.assertEqual(entry["position_exit_reason"], "STOP_LOSS")
        self.assertEqual(entry["exchange_close_price"], "100000")
        self.assertEqual(entry["exchange_close_order_id"], "abc123")
        self.assertFalse(entry["position_close_reported"])


class TestBingXSyncResultContract(unittest.TestCase):
    def test_not_triggered_returns_non_open_dict(self):
        entry = {
            "status": "watching",
            "symbol": "BTC-USDT",
            "direction": "BULLISH",
        }
        result = bingx.sync_position(entry)
        self.assertIsInstance(result, dict)
        self.assertEqual(result["state"], "NOT_MATCHED")
        self.assertFalse(result["active"])

    def test_missing_credentials_or_api_failure_returns_error_dict(self):
        entry = {
            "status": "triggered",
            "symbol": "BTC-USDT",
            "direction": "BULLISH",
        }
        with patch.object(
            bingx,
            "_signed_get",
            side_effect=RuntimeError("test failure"),
        ):
            result = bingx.sync_position(entry)

        self.assertIsInstance(result, dict)
        self.assertEqual(result["state"], "ERROR")
        self.assertFalse(result["active"])
        self.assertEqual(entry["exchange_sync_status"], "ERROR")


class TestLifecycleInvariants(unittest.TestCase):
    def test_health_does_not_modify_smc_fields(self):
        entry = {
            "status": "triggered",
            "symbol": "BTC-USDT",
            "direction": "BULLISH",
            "entry": 100000,
            "sl": 99000,
            "tp": 102000,
            "smc_score": 87,
            "execution_state": "READY_MARKET",
            "trade_classification": "A",
            "exchange_sync_status": "OPEN",
            "reversal_state": "STABLE",
        }
        original = copy.deepcopy(entry)

        position_health.apply_health(
            entry,
            {"state": "CROWDED_LONG", "score": -20},
        )

        for key in (
            "status",
            "symbol",
            "direction",
            "entry",
            "sl",
            "tp",
            "smc_score",
            "execution_state",
            "trade_classification",
        ):
            self.assertEqual(entry[key], original[key], key)

    def test_position_health_has_no_order_mutation_api(self):
        forbidden = (
            "place_order",
            "cancel_order",
            "close_position",
            "set_leverage",
            "adjust_margin",
        )
        for name in forbidden:
            self.assertFalse(hasattr(bingx, name), name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
