import unittest
from types import SimpleNamespace

from application.portfolio_service import (
    append_portfolio_report,
    build_balance_snapshot,
    compute_daily_pnls,
    compute_portfolio_allocation,
    maybe_rebase_daily_state_for_balance_change,
    maybe_reset_daily_state,
)


class PortfolioServiceTests(unittest.TestCase):
    def test_compute_portfolio_allocation_enriches_budget_output(self):
        observed = {}

        allocation = compute_portfolio_allocation(
            runtime_trend_universe={"ETHUSDT": {"base_asset": "ETH"}, "SOLUSDT": {"base_asset": "SOL"}},
            balances={"ETHUSDT": 1.5, "SOLUSDT": 2.0, "BTCUSDT": 0.1},
            prices={"ETHUSDT": 2000.0, "SOLUSDT": 100.0, "BTCUSDT": 50000.0},
            u_total=300.0,
            fuel_val=20.0,
            compute_allocation_budgets_fn=lambda total_equity, u_total, trend_val, dca_val: observed.update(
                {
                    "total_equity": total_equity,
                    "u_total": u_total,
                    "trend_val": trend_val,
                    "dca_val": dca_val,
                }
            )
            or {"trend_usdt_pool": 123.0},
        )

        self.assertEqual(
            observed,
            {
                "total_equity": 8520.0,
                "u_total": 300.0,
                "trend_val": 3200.0,
                "dca_val": 5000.0,
            },
        )
        self.assertEqual(allocation["trend_usdt_pool"], 123.0)
        self.assertEqual(allocation["trend_val"], 3200.0)
        self.assertEqual(allocation["dca_val"], 5000.0)
        self.assertEqual(allocation["total_equity"], 8520.0)

    def test_maybe_reset_daily_state_resets_on_new_day(self):
        runtime = SimpleNamespace(name="runtime")
        report = {"status": "ok"}
        state = {
            "last_reset_date": "2026-03-28",
            "daily_trend_pnl_basis": "legacy",
            "is_circuit_broken": True,
        }
        observed = []

        maybe_reset_daily_state(
            state,
            runtime,
            report,
            "2026-03-29",
            1000.0,
            400.0,
            runtime_set_trade_state_fn=lambda _runtime, _report, current_state, reason: observed.append(
                (reason, dict(current_state))
            ),
        )

        self.assertEqual(observed[0][0], "daily_reset")
        self.assertEqual(state["daily_equity_base"], 1000.0)
        self.assertEqual(state["daily_trend_equity_base"], 400.0)
        self.assertEqual(state["daily_trend_pnl_basis"], "trend_val")
        self.assertEqual(state["last_reset_date"], "2026-03-29")
        self.assertFalse(state["is_circuit_broken"])

    def test_maybe_reset_daily_state_migrates_basis_within_same_day(self):
        runtime = SimpleNamespace(name="runtime")
        report = {"status": "ok"}
        state = {
            "last_reset_date": "2026-03-29",
            "daily_trend_pnl_basis": "legacy",
            "daily_trend_equity_base": 100.0,
        }
        observed = []

        maybe_reset_daily_state(
            state,
            runtime,
            report,
            "2026-03-29",
            1000.0,
            450.0,
            runtime_set_trade_state_fn=lambda _runtime, _report, current_state, reason: observed.append(
                (reason, dict(current_state))
            ),
        )

        self.assertEqual(observed[0][0], "trend_pnl_basis_migrate")
        self.assertEqual(state["daily_trend_equity_base"], 450.0)
        self.assertEqual(state["daily_trend_pnl_basis"], "trend_val")

    def test_build_balance_snapshot_tracks_total_balances_by_asset(self):
        snapshot = build_balance_snapshot(
            {"ETHUSDT": {"base_asset": "ETH"}, "SOLUSDT": {"base_asset": "SOL"}},
            {"ETHUSDT": 1.25, "SOLUSDT": 3.5, "BTCUSDT": 0.2},
            412.3456,
        )

        self.assertEqual(snapshot, {"USDT": 412.3456, "BTC": 0.2, "ETH": 1.25, "SOL": 3.5})

    def test_maybe_rebase_daily_state_for_balance_change_resets_bases(self):
        runtime = SimpleNamespace(name="runtime")
        report = {"status": "ok"}
        state = {
            "last_balance_snapshot": {"USDT": 1000.0, "BTC": 0.1, "ETH": 2.0},
            "daily_equity_base": 1200.0,
            "daily_trend_equity_base": 400.0,
            "daily_trend_pnl_basis": "trend_val",
        }
        observed = []
        log_buffer = []

        changed = maybe_rebase_daily_state_for_balance_change(
            state,
            runtime,
            report,
            950.0,
            250.0,
            {"USDT": 850.0, "BTC": 0.1, "ETH": 1.5},
            log_buffer,
            runtime_set_trade_state_fn=lambda _runtime, _report, current_state, reason: observed.append(
                (reason, dict(current_state))
            ),
            append_log_fn=lambda buffer, message: buffer.append(message),
            translate_fn=lambda key, **kwargs: f"{key}:{kwargs}" if kwargs else key,
        )

        self.assertTrue(changed)
        self.assertEqual(observed[0][0], "external_balance_flow_rebase")
        self.assertEqual(state["daily_equity_base"], 950.0)
        self.assertEqual(state["daily_trend_equity_base"], 250.0)
        self.assertEqual(state["last_balance_snapshot"], {"USDT": 850.0, "BTC": 0.1, "ETH": 1.5})
        self.assertTrue(any("external_balance_flow_rebased" in line for line in log_buffer))

    def test_maybe_rebase_daily_state_for_usdt_transfer_uses_specific_log_key(self):
        runtime = SimpleNamespace(name="runtime")
        report = {"status": "ok"}
        state = {
            "last_balance_snapshot": {"USDT": 1000.0, "BTC": 0.1, "ETH": 2.0},
            "daily_equity_base": 1200.0,
            "daily_trend_equity_base": 400.0,
            "daily_trend_pnl_basis": "trend_val",
        }
        log_buffer = []

        changed = maybe_rebase_daily_state_for_balance_change(
            state,
            runtime,
            report,
            980.0,
            400.0,
            {"USDT": 900.0, "BTC": 0.1, "ETH": 2.0},
            log_buffer,
            runtime_set_trade_state_fn=lambda *_args, **_kwargs: None,
            append_log_fn=lambda buffer, message: buffer.append(message),
            translate_fn=lambda key, **kwargs: f"{key}:{kwargs}" if kwargs else key,
        )

        self.assertTrue(changed)
        self.assertTrue(any("external_usdt_flow_rebased" in line for line in log_buffer))

    def test_compute_daily_pnls_returns_zero_when_bases_missing(self):
        daily_pnl, trend_daily_pnl = compute_daily_pnls({}, 1000.0, 500.0)

        self.assertEqual(daily_pnl, 0.0)
        self.assertEqual(trend_daily_pnl, 0.0)

    def test_append_portfolio_report_delegates_to_reporting_helper(self):
        log_buffer = []
        observed = {}

        result = append_portfolio_report(
            log_buffer,
            {"total_equity": 1000.0},
            10.0,
            0.02,
            0.03,
            {"ahr999": 0.8},
            append_portfolio_report_fn=lambda *args, **kwargs: observed.update({"args": args, "kwargs": kwargs}) or "ok",
            append_log_fn=lambda buffer, message: buffer.append(message),
            translate_fn=lambda key, **kwargs: key,
            separator="sep",
        )

        self.assertEqual(result, "ok")
        self.assertEqual(observed["args"][0], log_buffer)
        self.assertEqual(observed["args"][1]["total_equity"], 1000.0)
        self.assertEqual(observed["kwargs"]["separator"], "sep")


if __name__ == "__main__":
    unittest.main()
