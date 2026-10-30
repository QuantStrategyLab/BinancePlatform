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


class BtcNoSubmitPlanTests(unittest.TestCase):
    def test_buy_and_noop_preserve_holdings(self):
        metrics = {"total_equity": 1000.0, "dca_value": 200.0}
        self.assertEqual(approved_buy_amount(decision(), metrics), 100.0)
        self.assertEqual(approved_buy_amount(decision(0.0, no_execute=True), metrics), 0.0)

    def test_exit_and_inconsistent_contract_fail_closed(self):
        metrics = {"total_equity": 1000.0, "dca_value": 200.0}
        for route in ("normal", "risk_on"):
            with self.subTest(route=route):
                self.assertEqual(approved_buy_amount(decision(exit_applied=True, route=route), metrics), 100.0)
                self.assertEqual(approved_buy_amount(decision(0.0, exit_applied=True, route=route,
                                                               no_execute=True), metrics), 0.0)
        for route in ("risk_off", "risk_reduced"):
            with self.subTest(route=route), self.assertRaisesRegex(ValueError, "btc_validation_exit_unsupported"):
                approved_buy_amount(decision(0.0, exit_applied=True, route=route, no_execute=True), metrics)
        candidate = decision(exit_applied=True, route="normal")
        candidate.diagnostics["zscore_exit"]["target_btc_exposure"] = 0.5
        with self.assertRaisesRegex(ValueError, "btc_validation_exit_unsupported"):
            approved_buy_amount(candidate, metrics)
        candidate = decision(0.0, no_execute=True)
        candidate.diagnostics["target_btc_value_usd"] = 100.0
        with self.assertRaisesRegex(ValueError, "btc_validation_exit_unsupported"):
            approved_buy_amount(candidate, metrics)
        for candidate in (decision(0.0), decision(no_execute=True), decision(current=0.0)):
            with self.subTest(candidate=candidate), self.assertRaisesRegex(ValueError, "btc_validation_plan_invalid"):
                approved_buy_amount(candidate, metrics)

    def test_missing_or_invalid_values_and_missing_budget_fail_closed(self):
        metrics = {"total_equity": 1000.0, "dca_value": 200.0}
        for amount in (None, True, float("nan"), float("inf"), -1):
            candidate = decision()
            candidate.diagnostics["planned_investment_usd"] = amount
            with self.subTest(amount=amount), self.assertRaisesRegex(ValueError, "btc_validation_plan_invalid"):
                approved_buy_amount(candidate, metrics)
        candidate = decision()
        candidate.budgets = ()
        with self.assertRaisesRegex(ValueError, "btc_validation_plan_invalid"):
            approved_buy_amount(candidate, metrics)


if __name__ == "__main__":
    unittest.main()
