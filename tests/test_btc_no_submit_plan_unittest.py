import unittest
from types import SimpleNamespace

from infra.btc_no_submit import approved_buy_amount


def decision(amount=100.0, *, current=200.0, exit_applied=False, route="", no_execute=False):
    equity = 1000.0
    ratio = (current + amount) / equity
    return SimpleNamespace(
        diagnostics={"planned_investment_usd": amount, "btc_target_ratio": ratio,
                     "btc_value_usd": current, "target_btc_value_usd": current + amount,
                     "zscore_exit": {"applied": exit_applied, "route": route,
                                     "target_btc_exposure": 1.0}},
        positions=(SimpleNamespace(symbol="BTCUSDT", target_weight=ratio),),
        budgets=(SimpleNamespace(name="btc_dca_pool", symbol="BTCUSDT", amount=amount),),
        risk_flags=("no_execute",) if no_execute else (),
    )


def buy(candidate, metrics, *, parts=None):
    if parts is None:
        parts = {"spot_free": str(metrics.get("cash_usdt")), "spot_locked": "0", "earn": "0",
                 "managed_total": metrics.get("cash_usdt")}
    return approved_buy_amount(candidate, metrics, usdt_parts=parts)


class BtcNoSubmitPlanTests(unittest.TestCase):
    def test_buy_and_noop_preserve_holdings(self):
        metrics = {"total_equity": 1000.0, "dca_value": 200.0, "cash_usdt": 100.0}
        self.assertEqual(buy(decision(), metrics), 100.0)
        self.assertEqual(buy(decision(0.0, no_execute=True), metrics), 0.0)

    def test_exit_and_inconsistent_contract_fail_closed(self):
        metrics = {"total_equity": 1000.0, "dca_value": 200.0, "cash_usdt": 100.0}
        for route in ("normal", "risk_on"):
            with self.subTest(route=route):
                self.assertEqual(buy(decision(exit_applied=True, route=route), metrics), 100.0)
                self.assertEqual(buy(decision(0.0, exit_applied=True, route=route,
                                                               no_execute=True), metrics), 0.0)
        for route in ("risk_off", "risk_reduced"):
            with self.subTest(route=route), self.assertRaisesRegex(ValueError, "btc_validation_exit_unsupported"):
                buy(decision(0.0, exit_applied=True, route=route, no_execute=True), metrics)
        candidate = decision(exit_applied=True, route="normal")
        candidate.diagnostics["zscore_exit"]["target_btc_exposure"] = 0.5
        with self.assertRaisesRegex(ValueError, "btc_validation_exit_unsupported"):
            buy(candidate, metrics)
        candidate = decision(0.0, no_execute=True)
        candidate.diagnostics["target_btc_value_usd"] = 100.0
        with self.assertRaisesRegex(ValueError, "btc_validation_exit_unsupported"):
            buy(candidate, metrics)
        for candidate in (decision(0.0), decision(no_execute=True), decision(current=0.0)):
            with self.subTest(candidate=candidate), self.assertRaisesRegex(ValueError, "btc_validation_plan_invalid"):
                buy(candidate, metrics)

    def test_buy_cannot_exceed_managed_usdt_total(self):
        metrics = {"total_equity": 1000.0, "dca_value": 200.0, "cash_usdt": 99.99}
        with self.assertRaisesRegex(ValueError, "btc_validation_insufficient_managed_usdt"):
            buy(decision(), metrics)
        metrics["cash_usdt"] = 100.0
        self.assertEqual(buy(decision(), metrics), 100.0)
        for value in (None, True, -1, float("nan"), float("inf")):
            metrics["cash_usdt"] = value
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "btc_validation_plan_invalid"):
                buy(decision(), metrics)

    def test_spot_free_and_snapshot_are_required_for_positive_buy(self):
        metrics = {"total_equity": 1000.0, "dca_value": 200.0, "cash_usdt": 200.0}
        cases = [
            ({"spot_free": "99.99", "spot_locked": "100.01", "earn": "0", "managed_total": 200.0},
             "btc_validation_insufficient_spot_usdt"),
            ({"spot_free": "0", "spot_locked": "200", "earn": "0", "managed_total": 200.0},
             "btc_validation_insufficient_spot_usdt"),
            ({"spot_free": "0", "spot_locked": "0", "earn": "200", "managed_total": 200.0},
             "btc_validation_insufficient_spot_usdt"),
            ({}, "btc_validation_usdt_split_unavailable"),
            ({"spot_free": "100", "spot_locked": "0", "earn": "100", "managed_total": 199.0},
             "btc_validation_usdt_split_unavailable"),
        ]
        for parts, reason in cases:
            with self.subTest(parts=parts), self.assertRaisesRegex(ValueError, reason):
                buy(decision(), metrics, parts=parts)
        self.assertEqual(buy(decision(), metrics, parts={"spot_free": "100", "spot_locked": "0", "earn": "100", "managed_total": 200.0}), 100)
        self.assertEqual(buy(decision(0, no_execute=True), metrics, parts={}), 0)

    def test_decimal_boundary_binds_the_original_managed_read(self):
        metrics = {"total_equity": 1000.0, "dca_value": 200.0, "cash_usdt": 0.3}
        parts = {"spot_free": "0.1", "spot_locked": "0.2", "earn": "0", "managed_total": 0.3}
        self.assertEqual(buy(decision(0.1), metrics, parts=parts), 0.1)
        with self.assertRaisesRegex(ValueError, "btc_validation_insufficient_spot_usdt"):
            buy(decision(0.10000000000000002), metrics, parts=parts)
        parts["managed_total"] = 0.30000000000000004
        with self.assertRaisesRegex(ValueError, "btc_validation_usdt_split_unavailable"):
            buy(decision(0.1), metrics, parts=parts)

    def test_missing_or_invalid_values_and_missing_budget_fail_closed(self):
        metrics = {"total_equity": 1000.0, "dca_value": 200.0, "cash_usdt": 100.0}
        for amount in (None, True, float("nan"), float("inf"), -1):
            candidate = decision()
            candidate.diagnostics["planned_investment_usd"] = amount
            with self.subTest(amount=amount), self.assertRaisesRegex(ValueError, "btc_validation_plan_invalid"):
                buy(candidate, metrics)
        candidate = decision()
        candidate.budgets = ()
        with self.assertRaisesRegex(ValueError, "btc_validation_plan_invalid"):
            buy(candidate, metrics)


if __name__ == "__main__":
    unittest.main()
