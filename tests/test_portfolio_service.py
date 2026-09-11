import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from runtime_support import ExecutionIntegrityError

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
        self.assertEqual(state["daily_trend_pnl_basis"], "trend_mark_plus_cash_flow_v1")
        self.assertEqual(state["daily_trend_cash_flow_usdt"], 0.0)
        self.assertEqual(state["daily_trend_risk_base_usdt"], 400.0)
        self.assertEqual(state["daily_external_principal_usdt"], 0.0)
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

        with self.assertRaises(ExecutionIntegrityError):
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

        self.assertEqual(observed, [])
        self.assertEqual(state["daily_trend_equity_base"], 100.0)
        self.assertEqual(state["daily_trend_pnl_basis"], "legacy")

    def test_build_balance_snapshot_tracks_total_balances_by_asset(self):
        snapshot = build_balance_snapshot(
            {"ETHUSDT": {"base_asset": "ETH"}, "SOLUSDT": {"base_asset": "SOL"}},
            {"ETHUSDT": 1.25, "SOLUSDT": 3.5, "BTCUSDT": 0.2},
            412.3456,
        )

        self.assertEqual(snapshot, {"USDT": 412.3456, "BTC": 0.2, "ETH": 1.25, "SOL": 3.5})

        with_fuel = build_balance_snapshot(
            {"ETHUSDT": {"base_asset": "ETH"}},
            {"ETHUSDT": 1.25, "BTCUSDT": 0.2, "BNBUSDT": 0.75},
            412.3456,
        )
        self.assertEqual(with_fuel["BNB"], 0.75)

    def test_maybe_rebase_daily_state_for_balance_change_blocks_without_resetting_bases(self):
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

        with self.assertRaises(ExecutionIntegrityError):
            maybe_rebase_daily_state_for_balance_change(
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

        self.assertEqual(observed, [])
        self.assertEqual(state["daily_equity_base"], 1200.0)
        self.assertEqual(state["daily_trend_equity_base"], 400.0)
        self.assertEqual(state["last_balance_snapshot"], {"USDT": 1000.0, "BTC": 0.1, "ETH": 2.0})
        self.assertTrue(any("balance_change_unexplained" in line for line in log_buffer))

    def test_maybe_rebase_daily_state_for_usdt_transfer_is_not_assumed_external(self):
        runtime = SimpleNamespace(name="runtime")
        report = {"status": "ok"}
        state = {
            "last_balance_snapshot": {"USDT": 1000.0, "BTC": 0.1, "ETH": 2.0},
            "daily_equity_base": 1200.0,
            "daily_trend_equity_base": 400.0,
            "daily_trend_pnl_basis": "trend_val",
        }
        log_buffer = []

        with self.assertRaises(ExecutionIntegrityError):
            maybe_rebase_daily_state_for_balance_change(
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

        self.assertEqual(report["diagnostics"]["balance_change"]["assets"], ["USDT"])

    def test_confirmed_spot_usdt_deposit_updates_cursor_and_principal_without_diluting_loss(self):
        now = datetime(2026, 9, 12, 10, tzinfo=timezone.utc)
        runtime = SimpleNamespace(name="runtime", client=object(), now_utc=now)
        report = {"status": "ok"}
        state = {
            "last_reset_date": "2026-09-12",
            "last_balance_snapshot": {"USDT": 800.0, "BTC": 0.1, "ETH": 2.0},
            "daily_equity_base": 1000.0,
            "daily_external_principal_usdt": 0.0,
            "daily_trend_equity_base": 400.0,
            "daily_trend_pnl_basis": "trend_mark_plus_cash_flow_v1",
            "daily_trend_cash_flow_usdt": 0.0,
            "daily_trend_net_invested_usdt": 0.0,
            "daily_trend_risk_base_usdt": 400.0,
            "daily_trend_third_fee_usdt": 0.0,
            "is_circuit_broken": True,
            "external_cash_flow_cursor": {"version": 1, "observed_at": "2026-09-12T09:00:00+00:00", "records": {}},
        }
        next_cursor = {"version": 1, "observed_at": "2026-09-12T10:00:00+00:00", "records": {"hash": {}}}
        writes = []

        changed = maybe_rebase_daily_state_for_balance_change(
            state,
            runtime,
            report,
            900.0,
            400.0,
            {"USDT": 900.0, "BTC": 0.1, "ETH": 2.0},
            [],
            collect_external_cash_flows_fn=lambda *_args, **_kwargs: {
                "bootstrap": False,
                "new_deposit_principal_usdt": "100",
                "new_confirmed_deposit_count": 1,
                "new_deposit_completed_at": ["2026-09-12T09:30:00+00:00"],
                "cursor": next_cursor,
            },
            runtime_set_trade_state_fn=lambda _runtime, _report, current, reason: writes.append(
                (reason, dict(current))
            ),
            append_log_fn=lambda *_args: None,
            translate_fn=lambda key, **_kwargs: key,
        )

        self.assertTrue(changed)
        self.assertEqual(state["daily_equity_base"], 1000.0)
        self.assertEqual(state["daily_external_principal_usdt"], 100.0)
        self.assertEqual(state["last_balance_snapshot"]["USDT"], 900.0)
        self.assertEqual(state["external_cash_flow_cursor"], next_cursor)
        self.assertTrue(state["is_circuit_broken"])
        self.assertEqual(state["daily_trend_equity_base"], 400.0)
        self.assertEqual(writes[0][0], "external_cash_flow_reconciliation")
        self.assertAlmostEqual(compute_daily_pnls(state, 900.0, 400.0)[0], -0.2)

    def test_external_deposit_must_exactly_explain_only_usdt_balance_change(self):
        runtime = SimpleNamespace(client=object(), now_utc=datetime(2026, 9, 12, 10, tzinfo=timezone.utc))
        base = {
            "last_reset_date": "2026-09-12",
            "last_balance_snapshot": {"USDT": 800.0, "BTC": 0.1},
            "external_cash_flow_cursor": {"version": 1, "observed_at": "2026-09-12T09:00:00+00:00", "records": {}},
        }
        def proof(*_args, **_kwargs):
            return {
                "bootstrap": False,
                "new_deposit_principal_usdt": "100",
                "new_confirmed_deposit_count": 1,
                "new_deposit_completed_at": ["2026-09-12T09:30:00+00:00"],
                "cursor": {"version": 1, "observed_at": "2026-09-12T10:00:00+00:00", "records": {}},
            }

        for snapshot in ({"USDT": 850.0, "BTC": 0.1}, {"USDT": 900.0, "BTC": 0.2}):
            with self.subTest(snapshot=snapshot), self.assertRaises(ExecutionIntegrityError):
                maybe_rebase_daily_state_for_balance_change(
                    dict(base), runtime, {"status": "ok"}, 0.0, 0.0, snapshot, [],
                    collect_external_cash_flows_fn=proof,
                    runtime_set_trade_state_fn=lambda *_args, **_kwargs: self.fail("unsafe state write"),
                    append_log_fn=lambda *_args: None,
                    translate_fn=lambda key, **_kwargs: key,
                )

    def test_late_deposit_is_not_silently_booked_into_current_day(self):
        runtime = SimpleNamespace(client=object(), now_utc=datetime(2026, 9, 12, 10, tzinfo=timezone.utc))
        state = {
            "last_reset_date": "2026-09-12",
            "last_balance_snapshot": {"USDT": 800.0},
            "external_cash_flow_cursor": {"version": 1, "observed_at": "2026-09-12T09:00:00+00:00", "records": {}},
        }
        with self.assertRaises(ExecutionIntegrityError):
            maybe_rebase_daily_state_for_balance_change(
                state, runtime, {"status": "ok"}, 900.0, 0.0, {"USDT": 900.0}, [],
                collect_external_cash_flows_fn=lambda *_args, **_kwargs: {
                    "bootstrap": False,
                    "new_deposit_principal_usdt": "100",
                    "new_confirmed_deposit_count": 1,
                    "new_deposit_completed_at": ["2026-09-11T23:59:00+00:00"],
                    "cursor": {},
                },
                runtime_set_trade_state_fn=lambda *_args, **_kwargs: self.fail("unsafe state write"),
                append_log_fn=lambda *_args: None,
                translate_fn=lambda key, **_kwargs: key,
            )

    def test_first_cursor_is_only_bootstrapped_when_balance_is_unchanged(self):
        runtime = SimpleNamespace(client=object(), now_utc=datetime(2026, 9, 12, 10, tzinfo=timezone.utc))
        cursor = {"version": 1, "observed_at": "2026-09-12T10:00:00+00:00", "records": {}}
        writes = []
        state = {"last_balance_snapshot": {"USDT": 800.0}, "last_reset_date": "2026-09-12"}
        result = maybe_rebase_daily_state_for_balance_change(
            state, runtime, {"status": "ok"}, 800.0, 0.0, {"USDT": 800.0}, [],
            collect_external_cash_flows_fn=lambda *_args, **_kwargs: {
                "bootstrap": True,
                "new_deposit_principal_usdt": "0",
                "new_confirmed_deposit_count": 0,
                "new_deposit_completed_at": [],
                "cursor": cursor,
            },
            runtime_set_trade_state_fn=lambda *_args, **kwargs: writes.append(kwargs["reason"]),
            append_log_fn=lambda *_args: None,
            translate_fn=lambda key, **_kwargs: key,
        )
        self.assertFalse(result)
        self.assertEqual(state["external_cash_flow_cursor"], cursor)
        self.assertEqual(writes, ["external_cash_flow_cursor"])

        with self.assertRaises(ExecutionIntegrityError):
            maybe_rebase_daily_state_for_balance_change(
                {"last_balance_snapshot": {"USDT": 800.0}}, runtime, {"status": "ok"},
                900.0, 0.0, {"USDT": 900.0}, [],
                collect_external_cash_flows_fn=lambda *_args, **_kwargs: self.fail("must not backfill"),
                runtime_set_trade_state_fn=lambda *_args, **_kwargs: None,
                append_log_fn=lambda *_args: None,
                translate_fn=lambda key, **_kwargs: key,
            )

    def test_new_day_deposit_is_retriable_when_history_read_fails_before_daily_reset(self):
        now = datetime(2026, 9, 12, 1, tzinfo=timezone.utc)
        runtime = SimpleNamespace(client=object(), now_utc=now)
        original = {
            "last_reset_date": "2026-09-11",
            "last_balance_snapshot": {"USDT": 800.0},
            "daily_equity_base": 800.0,
            "daily_external_principal_usdt": 0.0,
            "daily_trend_pnl_basis": "trend_mark_plus_cash_flow_v1",
            "daily_trend_cash_flow_usdt": 0.0,
            "daily_trend_net_invested_usdt": 0.0,
            "daily_trend_risk_base_usdt": 0.0,
            "daily_trend_third_fee_usdt": 0.0,
            "external_cash_flow_cursor": {"version": 1, "observed_at": "2026-09-11T23:00:00+00:00", "records": {}},
        }
        first = dict(original)
        with self.assertRaises(ExecutionIntegrityError):
            maybe_rebase_daily_state_for_balance_change(
                first, runtime, {"status": "ok"}, 900.0, 0.0, {"USDT": 900.0}, [],
                collect_external_cash_flows_fn=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    ValueError("external_cash_flow_history_read_failed")
                ),
                runtime_set_trade_state_fn=lambda *_args, **_kwargs: self.fail("unsafe state write"),
                append_log_fn=lambda *_args: None,
                translate_fn=lambda key, **_kwargs: key,
            )
        self.assertEqual(first["daily_equity_base"], 800.0)
        self.assertEqual(first["last_reset_date"], "2026-09-11")

        reloaded = dict(original)
        maybe_rebase_daily_state_for_balance_change(
            reloaded, runtime, {"status": "ok"}, 900.0, 0.0, {"USDT": 900.0}, [],
            collect_external_cash_flows_fn=lambda *_args, **_kwargs: {
                "bootstrap": False,
                "new_deposit_principal_usdt": "100",
                "new_confirmed_deposit_count": 1,
                "new_deposit_completed_at": ["2026-09-12T00:30:00+00:00"],
                "new_or_changed_withdrawal_count": 0,
                "new_unsupported_deposit_count": 0,
                "cursor": {"version": 1, "observed_at": now.isoformat(), "records": {}},
            },
            runtime_set_trade_state_fn=lambda *_args, **_kwargs: None,
            append_log_fn=lambda *_args: None,
            translate_fn=lambda key, **_kwargs: key,
        )
        self.assertEqual(reloaded["daily_equity_base"], 800.0)
        self.assertEqual(reloaded["daily_external_principal_usdt"], 0.0)
        maybe_reset_daily_state(
            reloaded, runtime, {"status": "ok"}, "2026-09-12", 900.0, 0.0,
            runtime_set_trade_state_fn=lambda *_args, **_kwargs: None,
        )
        self.assertEqual(reloaded["daily_equity_base"], 900.0)
        self.assertEqual(reloaded["daily_external_principal_usdt"], 0.0)

    def test_compute_daily_pnls_returns_zero_when_bases_missing_for_empty_portfolio(self):
        daily_pnl, trend_daily_pnl = compute_daily_pnls({}, 0.0, 0.0)

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
