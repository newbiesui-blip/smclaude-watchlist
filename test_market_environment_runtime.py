import unittest
from unittest.mock import patch
from datetime import datetime, timezone

import market_environment_runtime as runtime


class RuntimeTests(unittest.TestCase):
    def test_bls_ics_parser(self):
        ics = """BEGIN:VCALENDAR\nBEGIN:VEVENT\nSUMMARY:Employment Situation for August 2026\nDTSTART;TZID=America/New_York:20260904T083000\nEND:VEVENT\nBEGIN:VEVENT\nSUMMARY:Independence Day\nDTSTART;VALUE=DATE:20260704\nEND:VEVENT\nEND:VCALENDAR"""
        with patch.object(runtime, "_get", return_value=ics):
            events = runtime.fetch_bls_events(datetime(2026, 9, 1, tzinfo=timezone.utc))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["name"], "Employment Situation")

    def test_fomc_parser(self):
        text = """2026 FOMC Meetings January 27-28 March 17-18* April 28-29 June 16-17* July 28-29 September 15-16* October 27-28 December 8-9* 2025 FOMC Meetings"""
        with patch.object(runtime, "_get", return_value=text):
            events = runtime.fetch_fomc_events(datetime(2026, 9, 1, tzinfo=timezone.utc))
        self.assertTrue(any(e["name"] == "FOMC" for e in events))

    def test_liquidity_metrics(self):
        tickers = [
            {"symbol": f"C{i}-USDT", "quoteVolume": 1000-i*10, "bid": 100, "ask": 100.02}
            for i in range(10)
        ]
        candles = [
            {"time": i, "high": 101, "low": 99, "close": 100, "volume": 100}
            for i in range(200)
        ]
        with patch.object(runtime, "_ticker_rows", return_value=tickers), patch.object(runtime, "_klines", return_value=candles):
            state = runtime.fetch_liquidity_state()
        self.assertAlmostEqual(state["spread_bps"], 1.9998, places=4)
        self.assertGreater(state["volume_ratio"], 0)
        self.assertGreater(state["atr_ratio"], 0)
        self.assertEqual(state["participation_ratio"], 1.0)

    def test_live_state_fails_closed_on_source_error(self):
        with patch.object(runtime, "fetch_high_impact_events", side_effect=RuntimeError("offline")):
            with self.assertRaises(RuntimeError):
                runtime.load_live_state()


if __name__ == "__main__":
    unittest.main()
