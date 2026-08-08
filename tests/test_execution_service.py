import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from application.cycle_service import execute_strategy_cycle
from application.execution_service import (
    execute_btc_dca_cycle,
    execute_trend_buys,
    execute_trend_rotation,
    execute_trend_sells,
    run_daily_circuit_breaker,
)
from decision_mapper import map_strategy_decision_to_rotation_plan
from market_snapshot_support import capture_market_snapshot
from quant_platform_kit.risk.contracts import CandidateRiskIdentity
from quant_platform_kit.risk.gate import _canonical_digest, _decision_metrics
from quant_platform_kit.strategy_contracts import StrategyDecision


_CANDIDATE_IDENTITY = CandidateRiskIdentity(
    strategy_profile="crypto_live_pool_rotation",
    account_mode="single_strategy_account_v1",
    strategy_revision="1" * 40,
    runner_revision="2" * 40,
    config_sha256="3" * 64,
    input_manifest_sha256="4" * 64,
    authority_receipt_sha256="5" * 64,
)


def _utc_now_text():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _risk_assessment(scope, *, outcome="APPROVE", candidate_sha=None, decision_sha):
    return {
        "contract_version": "qsl.risk_gate_assessment.v1",
        "scope": scope,
        "evaluated_at": _utc_now_text(),
        "policy_id": "qpk.risk_gate",
        "policy_version": "v1",
        "mandate_authority_receipt_sha256": "7" * 64,
        "candidate_identity_sha256": candidate_sha or _CANDIDATE_IDENTITY.candidate_sha256,
        "decision_digest_sha256": decision_sha,
        "portfolio_snapshot_digest_sha256": "8" * 64,
        "outcome": outcome,
        "reason_codes": () if outcome == "APPROVE" else ("rejected",),
        "assessment_sha256": "9" * 64,
    }


def _decision_with_authority(
    *,
    risk_gate="APPROVE",
    member="APPROVE",
    account="APPROVE",
    account_candidate_sha=None,
):
    decision = StrategyDecision(
        diagnostics={
            "trend_pool": ("ETHUSDT",),
            "rotation_candidates": {
                "ETHUSDT": {"weight": 1.0, "relative_score": 1.5, "abs_momentum": 0.4},
            },
            "eligible_buy_symbols": ("ETHUSDT",),
            "planned_trend_buys": {"ETHUSDT": 320.0},
            "sell_reasons": {"ETHUSDT": "stale_rotated_out"},
        }
    )
    payload, _, _ = _decision_metrics(decision, total_equity=None)
    decision_sha = _canonical_digest(payload)
    return StrategyDecision(
        positions=decision.positions,
        budgets=decision.budgets,
        risk_flags=decision.risk_flags,
        diagnostics={
            **dict(decision.diagnostics),
            "risk_gate": risk_gate,
            "candidate_risk_identity": _CANDIDATE_IDENTITY,
            "member_risk_assessment": _risk_assessment(
                "MEMBER", outcome=member, decision_sha=decision_sha
            ),
            "account_risk_assessment": _risk_assessment(
                "ACCOUNT",
                outcome=account,
                candidate_sha=account_candidate_sha,
                decision_sha=decision_sha,
            ),
        },
    )


class ExecutionServiceTests(unittest.TestCase):
    def test_trend_rotation_reject_never_reaches_order_helpers(self):
        runtime = SimpleNamespace(now_utc="2026-08-07T00:00:00Z")
        report = {
            "selected_symbols": {"active_trend_pool": [], "selected_candidates": []},
            "gating_summary": {},
            "gating_events": [],
        }
        rejected = _decision_with_authority(risk_gate="REJECT")
        plan = {**map_strategy_decision_to_rotation_plan(rejected), "decision": rejected}
        observed_order_client_calls = []

        result = execute_trend_rotation(
            runtime,
            report,
            {},
            {"ETHUSDT": {"base_asset": "ETH"}},
            {"ETHUSDT": {}},
            {},
            {"ETHUSDT": 100.0},
            {"ETHUSDT": 1.0},
            1000.0,
            0.0,
            [],
            "20260807",
            True,
            True,
            resolve_strategy_plan=lambda *_args, **_kwargs: plan,
            append_rotation_summary=lambda *_args: None,
            execute_trend_sells=lambda *_args: observed_order_client_calls.append("sell") or 1000.0,
            execute_trend_buys=lambda *_args: observed_order_client_calls.append("buy") or 1000.0,
            append_trend_symbol_status=lambda *_args: None,
            official_trend_pool_symbols=["ETHUSDT"],
        )

        self.assertEqual(result, 1000.0)
        self.assertEqual(observed_order_client_calls, [])

    def test_daily_breaker_account_reject_makes_zero_order_client_calls(self):
        report = {"buy_sell_intents": [], "gating_summary": {}, "gating_events": []}
        observed_client_calls = []

        result = run_daily_circuit_breaker(
            SimpleNamespace(client=object()),
            report,
            {},
            {"ETHUSDT": {"base_asset": "ETH"}},
            {"ETHUSDT": 2.0},
            50.0,
            {"ETHUSDT": 100.0},
            -0.10,
            -0.05,
            [],
            decision=_decision_with_authority(account="REJECT"),
            format_qty_fn=lambda *_args: 1.5,
            runtime_notify_fn=lambda *_args: None,
            ensure_asset_available_fn=lambda *_args: True,
            runtime_call_client_fn=lambda *_args, **_kwargs: observed_client_calls.append("sell"),
            set_symbol_trade_state_fn=lambda *_args: None,
            runtime_set_trade_state_fn=lambda *_args, **_kwargs: None,
            build_balance_snapshot_fn=lambda *_args: {},
            translate_fn=lambda key, **_kwargs: key,
        )

        self.assertTrue(result)
        self.assertEqual(report["buy_sell_intents"], [])
        self.assertEqual(observed_client_calls, [])

    def test_cycle_gates_bnb_top_up_until_execution_authority_is_approved(self):
        cases = {
            "missing": None,
            "rejected": _decision_with_authority(account="REJECT"),
            "mismatched": _decision_with_authority(account_candidate_sha="f" * 64),
        }

        for name, decision in cases.items():
            with self.subTest(name=name):
                capture_calls, order_client_calls = self._run_fuel_gate_cycle(decision)

                self.assertEqual(capture_calls, [(float("-inf"), 15.0)])
                self.assertEqual(order_client_calls, [])

    def test_cycle_allows_bnb_top_up_only_after_approved_execution_authority(self):
        capture_calls, order_client_calls = self._run_fuel_gate_cycle(_decision_with_authority())

        self.assertEqual(capture_calls, [(float("-inf"), 15.0), (10.0, 15.0)])
        self.assertEqual(order_client_calls, ["order_market_buy"])

    def _run_fuel_gate_cycle(self, decision):
        capture_calls = []
        order_client_calls = []

        def capture_snapshot(current_runtime, report, universe, logs, min_bnb_value, buy_bnb_amount):
            capture_calls.append((min_bnb_value, buy_bnb_amount))
            return capture_market_snapshot(
                current_runtime,
                report,
                universe,
                logs,
                min_bnb_value,
                buy_bnb_amount,
                get_total_balance_fn=lambda _client, asset, **_kwargs: 1000.0
                if asset == "USDT"
                else 0.0,
                ensure_asset_available_fn=lambda *_args: True,
                runtime_call_client_fn=lambda _runtime, _report, method_name, **_kwargs: (
                    order_client_calls.append(method_name)
                ),
                runtime_notify_fn=lambda *_args: None,
                append_log_fn=lambda *_args: None,
                resolve_btc_snapshot_fn=lambda *_args: {},
                resolve_trend_indicators_fn=lambda *_args: {},
            )

        runtime = SimpleNamespace(
            client=SimpleNamespace(get_avg_price=lambda **_kwargs: {"price": "100.0"}),
            dry_run=True,
            now_utc=datetime.now(timezone.utc),
            print_traceback=False,
            tg_token="",
            tg_chat_id="",
        )
        with patch("application.cycle_service.try_record_platform_execution"):
            execute_strategy_cycle(
                runtime,
                build_execution_report=lambda _runtime: {
                    "status": "ok",
                    "log_lines": [],
                    "buy_sell_intents": [],
                },
                ensure_runtime_client=lambda *_args: True,
                load_cycle_execution_settings=lambda: SimpleNamespace(
                    btc_status_report_interval_hours=24,
                    allow_new_trend_entries_on_degraded=False,
                ),
                load_cycle_state=lambda *_args: (
                    {"is_circuit_broken": True},
                    {"degraded": False},
                    {"ETHUSDT": {"base_asset": "ETH"}},
                    True,
                ),
                append_trend_pool_source_logs=lambda *_args: None,
                capture_market_snapshot=capture_snapshot,
                compute_portfolio_allocation=lambda *_args: {
                    "total_equity": 1000.0,
                    "trend_val": 0.0,
                    "execution_decision": decision,
                },
                build_balance_snapshot=lambda *_args: {},
                maybe_reset_daily_state=lambda *_args: None,
                maybe_rebase_daily_state_for_balance_change=lambda *_args: False,
                compute_daily_pnls=lambda *_args: (0.0, 0.0),
                append_portfolio_report=lambda *_args: None,
                run_daily_circuit_breaker=lambda *_args, **_kwargs: False,
                execute_trend_rotation=lambda *_args, **_kwargs: 1000.0,
                execute_btc_dca_cycle=lambda *_args, **_kwargs: 1000.0,
                manage_usdt_earn_buffer_runtime=lambda *_args, **_kwargs: None,
                maybe_send_periodic_btc_status_report=lambda *_args, **_kwargs: None,
                runtime_set_trade_state=lambda *_args, **_kwargs: None,
                append_report_error=lambda *_args, **_kwargs: None,
                runtime_notify=lambda *_args: None,
                translate_fn=lambda key, **_kwargs: key,
                traceback_module=SimpleNamespace(print_exc=lambda: None),
            )

        return capture_calls, order_client_calls

    def test_btc_dca_identity_mismatch_makes_zero_order_client_calls(self):
        report = {"btc_dca_intents": [], "gating_summary": {}, "gating_events": []}
        observed_client_calls = []

        result = execute_btc_dca_cycle(
            SimpleNamespace(client=object()),
            report,
            {},
            {"BTCUSDT": 0.1},
            {"BTCUSDT": 50_000.0},
            1000.0,
            20_000.0,
            300.0,
            5000.0,
            {"ahr999": 0.4, "zscore": 0.0, "sell_trigger": 3.5},
            0.25,
            50.0,
            "20260807",
            [],
            decision=_decision_with_authority(account_candidate_sha="f" * 64),
            append_log_fn=lambda *_args: None,
            translate_fn=lambda key, **_kwargs: key,
            format_qty_fn=lambda *_args: 1.0,
            ensure_asset_available_fn=lambda *_args: True,
            runtime_call_client_fn=lambda *_args, **_kwargs: observed_client_calls.append("buy"),
            next_order_id_fn=lambda *_args: "unused",
            runtime_notify_fn=lambda *_args: None,
            runtime_set_trade_state_fn=lambda *_args, **_kwargs: None,
        )

        self.assertEqual(result, 1000.0)
        self.assertEqual(report["btc_dca_intents"], [])
        self.assertEqual(observed_client_calls, [])

    def test_run_daily_circuit_breaker_liquidates_and_latches_state(self):
        runtime = SimpleNamespace(client=object())
        report = {"buy_sell_intents": []}
        state = {}
        balances = {"ETHUSDT": 2.0}
        prices = {"ETHUSDT": 100.0}
        observed = {"asset_checks": [], "client_calls": [], "state_sets": [], "persist_reasons": [], "notifications": []}

        result = run_daily_circuit_breaker(
            runtime,
            report,
            state,
            {"ETHUSDT": {"base_asset": "ETH"}},
            balances,
            50.0,
            prices,
            -0.10,
            -0.05,
            [],
            decision=_decision_with_authority(),
            format_qty_fn=lambda _client, _symbol, qty: round(qty - 0.5, 4),
            runtime_notify_fn=lambda _runtime, _report, text: observed["notifications"].append(text),
            ensure_asset_available_fn=lambda _runtime, _report, asset, amount, _log_buffer: observed["asset_checks"].append((asset, amount)) or True,
            runtime_call_client_fn=lambda _runtime, _report, method_name, payload, effect_type: observed["client_calls"].append(
                (method_name, payload, effect_type)
            ),
            set_symbol_trade_state_fn=lambda _state, symbol, symbol_state: observed["state_sets"].append((symbol, dict(symbol_state))),
            runtime_set_trade_state_fn=lambda _runtime, _report, _state, reason: observed["persist_reasons"].append(reason),
            build_balance_snapshot_fn=lambda _universe, current_balances, u_total: {"USDT": u_total, "ETH": current_balances["ETHUSDT"]},
            translate_fn=lambda key, **kwargs: f"{key}:{kwargs}" if kwargs else key,
        )

        self.assertTrue(result)
        self.assertAlmostEqual(balances["ETHUSDT"], 0.5, places=6)
        self.assertTrue(state["is_circuit_broken"])
        self.assertEqual(state["last_balance_snapshot"], {"USDT": 200.0, "ETH": 0.5})
        self.assertTrue(report["circuit_breaker_triggered"])
        self.assertEqual(report["buy_sell_intents"][0]["reason"], "daily_circuit_breaker")
        self.assertEqual(observed["asset_checks"][0][0], "ETH")
        self.assertEqual(observed["client_calls"][0][0], "order_market_sell")
        self.assertEqual(observed["state_sets"][0][0], "ETHUSDT")
        self.assertEqual(observed["persist_reasons"], ["daily_circuit_breaker"])
        self.assertGreaterEqual(len(observed["notifications"]), 1)

    def test_execute_trend_sells_executes_sell_and_updates_runtime_state(self):
        runtime = SimpleNamespace(client=object())
        report = {"buy_sell_intents": []}
        state = {}
        balances = {"ETHUSDT": 2.0}
        prices = {"ETHUSDT": 100.0}
        observed = {
            "asset_checks": [],
            "client_calls": [],
            "state_sets": [],
            "actions": [],
            "persist_reasons": [],
            "notifications": [],
            "logs": [],
        }

        result = execute_trend_sells(
            runtime,
            report,
            state,
            {"ETHUSDT": {"base_asset": "ETH"}},
            {"ETHUSDT": "rotated_out"},
            prices,
            balances,
            50.0,
            [],
            "20260329",
            should_skip_duplicate_trend_action_fn=lambda *_args: False,
            append_log_fn=lambda _buffer, message: observed["logs"].append(message),
            translate_fn=lambda key, **kwargs: f"{key}:{kwargs}" if kwargs else key,
            format_qty_fn=lambda _client, _symbol, _qty: 1.5,
            ensure_asset_available_fn=lambda _runtime, _report, asset, amount, _log_buffer: observed["asset_checks"].append((asset, amount)) or True,
            runtime_call_client_fn=lambda _runtime, _report, method_name, payload, effect_type: observed["client_calls"].append(
                (method_name, payload, effect_type)
            ),
            next_order_id_fn=lambda *_args: "sell-order-id",
            set_symbol_trade_state_fn=lambda _state, symbol, symbol_state: observed["state_sets"].append((symbol, dict(symbol_state))),
            record_trend_action_fn=lambda _state, symbol, action, action_date: observed["actions"].append((symbol, action, action_date)),
            runtime_set_trade_state_fn=lambda _runtime, _report, _state, reason: observed["persist_reasons"].append(reason),
            runtime_notify_fn=lambda _runtime, _report, text: observed["notifications"].append(text),
        )

        self.assertAlmostEqual(result, 200.0, places=6)
        self.assertAlmostEqual(balances["ETHUSDT"], 0.5, places=6)
        self.assertEqual(report["buy_sell_intents"][0]["action"], "sell")
        self.assertEqual(report["buy_sell_intents"][0]["reason"], "rotated_out")
        self.assertEqual(observed["asset_checks"][0][0], "ETH")
        self.assertEqual(observed["client_calls"][0][0], "order_market_sell")
        self.assertEqual(observed["actions"], [("ETHUSDT", "sell", "20260329")])
        self.assertEqual(observed["persist_reasons"], ["trend_sell:ETHUSDT"])
        self.assertGreaterEqual(len(observed["notifications"]), 1)

    def test_execute_trend_buys_executes_buy_and_updates_runtime_state(self):
        runtime = SimpleNamespace(client=object())
        report = {"buy_sell_intents": [], "gating_summary": {}, "gating_events": []}
        state = {}
        balances = {"ETHUSDT": 0.0}
        prices = {"ETHUSDT": 100.0}
        observed = {
            "asset_checks": [],
            "client_calls": [],
            "state_sets": [],
            "actions": [],
            "persist_reasons": [],
            "notifications": [],
            "logs": [],
        }

        result = execute_trend_buys(
            runtime,
            report,
            state,
            {"ETHUSDT": {"weight": 0.6, "relative_score": 1.2}},
            ["ETHUSDT"],
            {"ETHUSDT": 200.0},
            prices,
            balances,
            500.0,
            [],
            "20260329",
            should_skip_duplicate_trend_action_fn=lambda *_args: False,
            append_log_fn=lambda _buffer, message: observed["logs"].append(message),
            translate_fn=lambda key, **kwargs: f"{key}:{kwargs}" if kwargs else key,
            format_qty_fn=lambda _client, _symbol, qty: round(qty, 6),
            ensure_asset_available_fn=lambda _runtime, _report, asset, amount, _log_buffer: observed["asset_checks"].append((asset, amount)) or True,
            runtime_call_client_fn=lambda _runtime, _report, method_name, payload, effect_type: observed["client_calls"].append(
                (method_name, payload, effect_type)
            ),
            next_order_id_fn=lambda *_args: "buy-order-id",
            set_symbol_trade_state_fn=lambda _state, symbol, symbol_state: observed["state_sets"].append((symbol, dict(symbol_state))),
            record_trend_action_fn=lambda _state, symbol, action, action_date: observed["actions"].append((symbol, action, action_date)),
            runtime_set_trade_state_fn=lambda _runtime, _report, _state, reason: observed["persist_reasons"].append(reason),
            runtime_notify_fn=lambda _runtime, _report, text: observed["notifications"].append(text),
        )

        self.assertAlmostEqual(result, 303.0, places=6)
        self.assertAlmostEqual(balances["ETHUSDT"], 1.97, places=6)
        self.assertEqual(report["buy_sell_intents"][0]["action"], "buy")
        self.assertEqual(report["buy_sell_intents"][0]["budget"], 200.0)
        self.assertEqual(observed["asset_checks"][0][0], "USDT")
        self.assertEqual(observed["client_calls"][0][0], "order_market_buy")
        self.assertEqual(observed["actions"], [("ETHUSDT", "buy", "20260329")])
        self.assertEqual(observed["persist_reasons"], ["trend_buy:ETHUSDT"])
        self.assertGreaterEqual(len(observed["notifications"]), 1)

    def test_execute_trend_buys_records_gate_when_budget_below_threshold(self):
        runtime = SimpleNamespace(client=object())
        report = {"buy_sell_intents": [], "gating_summary": {}, "gating_events": []}

        result = execute_trend_buys(
            runtime,
            report,
            {},
            {"ETHUSDT": {"weight": 0.6, "relative_score": 1.2}},
            ["ETHUSDT"],
            {"ETHUSDT": 12.0},
            {"ETHUSDT": 100.0},
            {"ETHUSDT": 0.0},
            500.0,
            [],
            "20260329",
            should_skip_duplicate_trend_action_fn=lambda *_args: False,
            append_log_fn=lambda *_args: None,
            translate_fn=lambda key, **_kwargs: key,
            format_qty_fn=lambda *_args: 0.0,
            ensure_asset_available_fn=lambda *_args: True,
            runtime_call_client_fn=lambda *_args, **_kwargs: None,
            next_order_id_fn=lambda *_args: "buy-order-id",
            set_symbol_trade_state_fn=lambda *_args, **_kwargs: None,
            record_trend_action_fn=lambda *_args, **_kwargs: None,
            runtime_set_trade_state_fn=lambda *_args, **_kwargs: None,
            runtime_notify_fn=lambda *_args, **_kwargs: None,
        )

        self.assertEqual(result, 500.0)
        self.assertEqual(report["buy_sell_intents"], [])
        self.assertEqual(report["gating_summary"]["trend_buy_below_min_budget"], 1)
        self.assertEqual(report["gating_events"][0]["symbol"], "ETHUSDT")

    def test_execute_trend_rotation_delegates_sell_buy_and_status_flow(self):
        runtime = SimpleNamespace(now_utc="2026-03-29T00:00:00Z")
        report = {"selected_symbols": {"active_trend_pool": [], "selected_candidates": []}, "gating_summary": {}, "gating_events": []}
        state = {}
        runtime_trend_universe = {"ETHUSDT": {"base_asset": "ETH"}}
        trend_indicators = {"ETHUSDT": {"sma20": 1.0}}
        btc_snapshot = {"ahr999": 0.6}
        prices = {"ETHUSDT": 2000.0}
        balances = {"ETHUSDT": 0.5}
        log_buffer = []
        observed = {"plan_calls": []}

        plans = [
            {
                "decision": _decision_with_authority(),
                "active_trend_pool": ["ETHUSDT"],
                "selected_candidates": {"ETHUSDT": {"weight": 1.0, "relative_score": 1.5}},
                "combo_diagnostics": {"regime_tier": "hard", "effective_btc_weight": 0.25},
                "eligible_buy_symbols": [],
                "planned_trend_buys": {},
                "sell_reasons": {"ETHUSDT": "rotated_out"},
            },
            {
                "decision": _decision_with_authority(),
                "active_trend_pool": ["ETHUSDT"],
                "selected_candidates": {"ETHUSDT": {"weight": 1.0, "relative_score": 1.5}},
                "eligible_buy_symbols": ["ETHUSDT"],
                "planned_trend_buys": {"ETHUSDT": 320.0},
                "sell_reasons": {},
            },
        ]

        def fake_resolve_strategy_plan(*args, **kwargs):
            observed["plan_calls"].append((args, kwargs))
            return plans[len(observed["plan_calls"]) - 1]

        def fake_execute_trend_sells(*_args):
            observed["sell_called"] = True
            return 1150.0

        def fake_execute_trend_buys(*_args):
            observed["buy_plan"] = dict(_args[5])
            return 980.0

        result = execute_trend_rotation(
            runtime,
            report,
            state,
            runtime_trend_universe,
            trend_indicators,
            btc_snapshot,
            prices,
            balances,
            1000.0,
            15.0,
            log_buffer,
            "20260329",
            True,
            False,
            resolve_strategy_plan=fake_resolve_strategy_plan,
            append_rotation_summary=lambda *_args: observed.__setitem__("summary_called", True),
            execute_trend_sells=fake_execute_trend_sells,
            execute_trend_buys=fake_execute_trend_buys,
            append_trend_symbol_status=lambda *_args: observed.__setitem__("status_called", True),
            official_trend_pool_symbols=["ETHUSDT", "SOLUSDT"],
        )

        self.assertEqual(result, 980.0)
        self.assertEqual(
            report["selected_symbols"],
            {
                "active_trend_pool": ["ETHUSDT"],
                "selected_candidates": ["ETHUSDT"],
            },
        )
        self.assertEqual(len(observed["plan_calls"]), 2)
        self.assertTrue(observed["summary_called"])
        self.assertTrue(observed["sell_called"])
        self.assertTrue(observed["status_called"])
        self.assertEqual(observed["buy_plan"], {"ETHUSDT": 320.0})
        self.assertEqual(report["diagnostics"]["combo"]["regime_tier"], "hard")
        self.assertAlmostEqual(report["diagnostics"]["combo"]["effective_btc_weight"], 0.25)

    def test_execute_trend_rotation_records_candidate_filter_reasons(self):
        runtime = SimpleNamespace(now_utc="2026-03-29T00:00:00Z")
        report = {"selected_symbols": {"active_trend_pool": [], "selected_candidates": []}, "gating_summary": {}, "gating_events": []}
        state = {}
        runtime_trend_universe = {"ETHUSDT": {"base_asset": "ETH"}}
        trend_indicators = {
            "ETHUSDT": {
                "sma20": 2100.0,
                "sma60": 1900.0,
                "sma200": 1700.0,
                "roc20": 0.02,
                "roc60": 0.04,
                "roc120": 0.08,
                "vol20": 0.5,
            }
        }
        btc_snapshot = {"regime_on": True, "btc_roc20": 0.10, "btc_roc60": 0.08, "btc_roc120": 0.06}
        plans = [
            {
                "decision": _decision_with_authority(),
                "active_trend_pool": ["ETHUSDT"],
                "selected_candidates": {},
                "eligible_buy_symbols": [],
                "planned_trend_buys": {},
                "sell_reasons": {},
            },
            {
                "decision": _decision_with_authority(),
                "active_trend_pool": ["ETHUSDT"],
                "selected_candidates": {},
                "eligible_buy_symbols": [],
                "planned_trend_buys": {},
                "sell_reasons": {},
            },
        ]
        observed = {"plan_calls": 0}

        def fake_resolve_strategy_plan(*_args, **_kwargs):
            plan = plans[observed["plan_calls"]]
            observed["plan_calls"] += 1
            return plan

        result = execute_trend_rotation(
            runtime,
            report,
            state,
            runtime_trend_universe,
            trend_indicators,
            btc_snapshot,
            {"ETHUSDT": 2000.0},
            {"ETHUSDT": 0.0},
            1000.0,
            15.0,
            [],
            "20260329",
            True,
            True,
            resolve_strategy_plan=fake_resolve_strategy_plan,
            append_rotation_summary=lambda *_args: None,
            execute_trend_sells=lambda *_args: 1000.0,
            execute_trend_buys=lambda *_args: 1000.0,
            append_trend_symbol_status=lambda *_args: None,
            official_trend_pool_symbols=["ETHUSDT"],
        )

        self.assertEqual(result, 1000.0)
        detail = report["gating_events"][0]["detail"]
        self.assertEqual(detail["active_trend_pool_size"], 1)
        reasons = detail["candidate_filter_reasons"]["ETHUSDT"]["reasons"]
        self.assertIn("price_lte_sma20", reasons)
        self.assertIn("relative_score_lte_zero", reasons)

    def test_execute_btc_dca_cycle_executes_buy_branch(self):
        runtime = SimpleNamespace(client=object())
        report = {"btc_dca_intents": [], "gating_summary": {}, "gating_events": []}
        state = {}
        balances = {"BTCUSDT": 0.1}
        prices = {"BTCUSDT": 50_000.0}
        log_buffer = []
        observed = {"asset_checks": [], "client_calls": [], "persist_reasons": [], "notifications": []}

        result = execute_btc_dca_cycle(
            runtime,
            report,
            state,
            balances,
            prices,
            1000.0,
            20_000.0,
            300.0,
            5000.0,
            {"ahr999": 0.4, "zscore": 0.0, "sell_trigger": 3.5},
            0.25,
            50.0,
            "20260329",
            log_buffer,
            decision=_decision_with_authority(),
            append_log_fn=lambda buffer, message: buffer.append(message),
            translate_fn=lambda key, **_kwargs: key,
            format_qty_fn=lambda _client, _symbol, qty: round(qty, 6),
            ensure_asset_available_fn=lambda _runtime, _report, asset, amount, _log_buffer: observed["asset_checks"].append((asset, amount)) or True,
            runtime_call_client_fn=lambda _runtime, _report, method_name, payload, effect_type: observed["client_calls"].append(
                (method_name, payload, effect_type)
            ),
            next_order_id_fn=lambda *_args: "buy-order-id",
            runtime_notify_fn=lambda _runtime, _report, text: observed["notifications"].append(text),
            runtime_set_trade_state_fn=lambda _runtime, _report, _state, reason: observed["persist_reasons"].append(reason),
        )

        self.assertAlmostEqual(result, 753.75, places=2)
        self.assertAlmostEqual(balances["BTCUSDT"], 0.104925, places=6)
        self.assertEqual(state["dca_last_buy_date"], "20260329")
        self.assertEqual(report["btc_dca_intents"][0]["action"], "buy")
        self.assertEqual(observed["asset_checks"][0][0], "USDT")
        self.assertEqual(observed["client_calls"][0][0], "order_market_buy")
        self.assertEqual(observed["persist_reasons"], ["btc_dca_buy"])
        self.assertEqual(log_buffer, ["btc_accumulation_radar_line"])

    def test_execute_btc_dca_cycle_executes_trim_branch(self):
        runtime = SimpleNamespace(client=object())
        report = {"btc_dca_intents": [], "gating_summary": {}, "gating_events": []}
        state = {}
        balances = {"BTCUSDT": 1.0}
        prices = {"BTCUSDT": 10_000.0}
        log_buffer = []
        observed = {"asset_checks": [], "client_calls": [], "persist_reasons": []}

        result = execute_btc_dca_cycle(
            runtime,
            report,
            state,
            balances,
            prices,
            500.0,
            20_000.0,
            5.0,
            10_000.0,
            {"ahr999": 2.0, "zscore": 4.5, "sell_trigger": 3.5},
            0.25,
            50.0,
            "20260329",
            log_buffer,
            decision=_decision_with_authority(),
            append_log_fn=lambda buffer, message: buffer.append(message),
            translate_fn=lambda key, **_kwargs: key,
            format_qty_fn=lambda _client, _symbol, qty: round(qty, 6),
            ensure_asset_available_fn=lambda _runtime, _report, asset, amount, _log_buffer: observed["asset_checks"].append((asset, amount)) or True,
            runtime_call_client_fn=lambda _runtime, _report, method_name, payload, effect_type: observed["client_calls"].append(
                (method_name, payload, effect_type)
            ),
            next_order_id_fn=lambda *_args: "sell-order-id",
            runtime_notify_fn=lambda *_args, **_kwargs: None,
            runtime_set_trade_state_fn=lambda _runtime, _report, _state, reason: observed["persist_reasons"].append(reason),
        )

        self.assertAlmostEqual(result, 3500.0, places=2)
        self.assertAlmostEqual(balances["BTCUSDT"], 0.7, places=6)
        self.assertEqual(state["dca_last_sell_date"], "20260329")
        self.assertEqual(report["btc_dca_intents"][0]["action"], "sell")
        self.assertEqual(report["btc_dca_intents"][0]["sell_pct"], 0.3)
        self.assertEqual(observed["asset_checks"][0][0], "BTC")
        self.assertEqual(observed["client_calls"][0][0], "order_market_sell")
        self.assertEqual(observed["persist_reasons"], ["btc_dca_sell"])

    def test_execute_btc_dca_cycle_records_gate_when_pool_too_small(self):
        runtime = SimpleNamespace(client=object())
        report = {"btc_dca_intents": [], "gating_summary": {}, "gating_events": []}

        result = execute_btc_dca_cycle(
            runtime,
            report,
            {},
            {"BTCUSDT": 0.0},
            {"BTCUSDT": 50_000.0},
            100.0,
            1_000.0,
            8.0,
            6.0,
            {"ahr999": 0.7, "zscore": 0.0, "sell_trigger": 3.5},
            0.25,
            50.0,
            "20260329",
            [],
            decision=_decision_with_authority(),
            append_log_fn=lambda *_args: None,
            translate_fn=lambda key, **_kwargs: key,
            format_qty_fn=lambda *_args: 0.0,
            ensure_asset_available_fn=lambda *_args: True,
            runtime_call_client_fn=lambda *_args, **_kwargs: None,
            next_order_id_fn=lambda *_args: "noop",
            runtime_notify_fn=lambda *_args, **_kwargs: None,
            runtime_set_trade_state_fn=lambda *_args, **_kwargs: None,
        )

        self.assertEqual(result, 100.0)
        self.assertEqual(report["btc_dca_intents"], [])
        self.assertEqual(report["gating_summary"]["btc_dca_pool_too_small"], 1)


if __name__ == "__main__":
    unittest.main()
