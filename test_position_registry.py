import json
import os
import tempfile
import unittest
from unittest import mock

import position_registry as reg
import bingx_position_tracker as bingx


class PositionRegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "position_registry.json")

    def tearDown(self):
        self.tmp.cleanup()

    def pos(self, pid=None, symbol="EVAA-USDT", side="SHORT", amount=10, avg=1.0):
        return {
            "symbol": symbol,
            "side": side,
            "amount": amount,
            "exchange_position_id": pid,
            "avg_entry_price": avg,
            "mark_price": 0.9,
            "unrealized_pnl": 1.0,
        }

    def test_discovers_exchange_position(self):
        with mock.patch.object(bingx, "list_open_positions", return_value=[self.pos("p1")]):
            r = reg.reconcile(path=self.path)
        self.assertTrue(r["ok"])
        self.assertEqual(r["discovered"], ["id:p1"])
        self.assertEqual(r["registry"]["positions"]["id:p1"]["lifecycle"], bingx.OPEN)

    def test_api_failure_preserves_open(self):
        initial = reg._empty_registry()
        initial["positions"]["id:p1"] = reg._new_entry_from_exchange(self.pos("p1"))
        reg.save_registry(initial, self.path)
        with mock.patch.object(bingx, "list_open_positions", side_effect=RuntimeError("timeout")):
            r = reg.reconcile(path=self.path)
        entry = r["registry"]["positions"]["id:p1"]
        self.assertFalse(r["ok"])
        self.assertEqual(entry["lifecycle"], bingx.OPEN)
        self.assertIn("last_error", entry)

    def test_missing_is_not_found_without_closure_confirmation(self):
        initial = reg._empty_registry()
        initial["positions"]["id:p1"] = reg._new_entry_from_exchange(self.pos("p1"))
        reg.save_registry(initial, self.path)
        with mock.patch.object(bingx, "list_open_positions", return_value=[]), \
             mock.patch.object(bingx, "confirm_closed_position", return_value=None):
            r = reg.reconcile(path=self.path)
        self.assertEqual(r["registry"]["positions"]["id:p1"]["lifecycle"], "NOT_FOUND")
        self.assertEqual(r["closed"], [])

    def test_missing_with_matching_history_closes(self):
        initial = reg._empty_registry()
        initial["positions"]["id:p1"] = reg._new_entry_from_exchange(self.pos("p1"))
        reg.save_registry(initial, self.path)
        history = {"positionId": "p1", "symbol": "EVAA-USDT", "positionSide": "SHORT"}
        with mock.patch.object(bingx, "list_open_positions", return_value=[]), \
             mock.patch.object(bingx, "confirm_closed_position", return_value=history):
            r = reg.reconcile(path=self.path)
        entry = r["registry"]["positions"]["id:p1"]
        self.assertEqual(entry["lifecycle"], bingx.CLOSED)
        self.assertEqual(r["closed"], ["id:p1"])

    def test_fallback_single_position_has_stable_identity(self):
        p = self.pos(None)
        with mock.patch.object(bingx, "list_open_positions", return_value=[p]):
            r1 = reg.reconcile(path=self.path)
        first = r1["discovered"][0]
        with mock.patch.object(bingx, "list_open_positions", return_value=[p]):
            r2 = reg.reconcile(path=self.path)
        self.assertEqual(r2["discovered"], [])
        self.assertEqual(r2["updated"], [first])

    def test_duplicate_fallback_positions_are_not_overwritten(self):
        p1 = self.pos(None, amount=10, avg=1.0)
        p2 = self.pos(None, amount=10, avg=1.0)
        p2["mark_price"] = 0.8
        p2["unrealized_pnl"] = 2.0
        with mock.patch.object(bingx, "list_open_positions", return_value=[p1, p2]):
            r = reg.reconcile(path=self.path)
        self.assertEqual(len(r["discovered"]), 2)
        entries = [r["registry"]["positions"][k] for k in r["discovered"]]
        self.assertEqual(len(entries), 2)
        self.assertTrue(all(e["identity_ambiguous"] for e in entries))
        self.assertEqual(sorted(e["amount"] for e in entries), [10, 10])

    def test_identical_fallback_duplicates_are_both_retained(self):
        p1 = self.pos(None, amount=10, avg=1.0)
        p2 = dict(p1)
        with mock.patch.object(bingx, "list_open_positions", return_value=[p1, p2]):
            r = reg.reconcile(path=self.path)
        self.assertEqual(len(r["discovered"]), 2)
        self.assertNotEqual(r["discovered"][0], r["discovered"][1])

    def test_fallback_duplicate_transition_keeps_existing_alert_state(self):
        p = self.pos(None, amount=10, avg=1.0)
        with mock.patch.object(bingx, "list_open_positions", return_value=[p]):
            r = reg.reconcile(path=self.path)
        old_id = r["discovered"][0]
        r["registry"]["positions"][old_id]["open_alert_sent"] = True
        reg.save_registry(r["registry"], self.path)
        p2 = self.pos(None, amount=10, avg=1.0)
        p2["mark_price"] = 0.8
        with mock.patch.object(bingx, "list_open_positions", return_value=[p, p2]):
            r2 = reg.reconcile(path=self.path)
        zero = [k for k in r2["registry"]["positions"] if k.endswith(":0")]
        self.assertEqual(len(zero), 1)
        self.assertTrue(r2["registry"]["positions"][zero[0]]["open_alert_sent"])

    def test_orphan_classification_with_empty_watchlist(self):
        p = self.pos("p1")
        with mock.patch.object(bingx, "list_open_positions", return_value=[p]):
            r = reg.reconcile(path=self.path)
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as fh:
            json.dump([], fh)
            watch = fh.name
        try:
            c = reg.classify_smc_links(r["registry"], watch)
        finally:
            os.unlink(watch)
        self.assertEqual(c["orphans"], 1)
        self.assertTrue(r["registry"]["positions"]["id:p1"]["orphan"])

    def test_linked_position_is_excluded_from_orphan_alert_iterator(self):
        p = self.pos("p1")
        with mock.patch.object(bingx, "list_open_positions", return_value=[p]):
            r = reg.reconcile(path=self.path)
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as fh:
            json.dump([{"symbol": "EVAA-USDT", "direction": "SHORT", "exchange_position_id": "p1"}], fh)
            watch = fh.name
        try:
            reg.classify_smc_links(r["registry"], watch)
        finally:
            os.unlink(watch)
        self.assertEqual(reg.iter_open_needing_alert(r["registry"], orphans_only=True), [])

    def test_open_alert_is_idempotent(self):
        p = self.pos("p1")
        with mock.patch.object(bingx, "list_open_positions", return_value=[p]):
            r = reg.reconcile(path=self.path)
        self.assertEqual(len(reg.iter_open_needing_alert(r["registry"])), 1)
        reg.mark_open_alert_sent("id:p1", self.path)
        self.assertEqual(reg.iter_open_needing_alert(path_registry := reg.load_registry(self.path)), [])

    def test_minimal_context_does_not_fabricate_smc_fields(self):
        entry = reg._new_entry_from_exchange(self.pos("p1"))
        ctx = reg.build_minimal_context_for_health(entry)
        self.assertEqual(ctx["symbol"], "EVAA-USDT")
        self.assertNotIn("invalidation", ctx)
        self.assertNotIn("validated_targets", ctx)

    def test_migration_is_idempotent(self):
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as fh:
            json.dump([{
                "symbol": "EVAA-USDT", "direction": "SHORT", "exchange_sync_status": "OPEN",
                "exchange_position_id": "p1", "exchange_avg_price": 1.0, "status": "triggered"
            }], fh)
            watch = fh.name
        try:
            a = reg.migrate_from_watchlist(watch, self.path)
            b = reg.migrate_from_watchlist(watch, self.path)
        finally:
            os.unlink(watch)
        self.assertEqual(a["migrated"], 1)
        self.assertEqual(b["migrated"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
