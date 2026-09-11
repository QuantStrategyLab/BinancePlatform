import unittest
from types import SimpleNamespace

from application.state_service import append_trend_pool_source_logs, load_cycle_state
from infra.state_store import load_runtime_trade_state, save_runtime_trade_state
from trade_state_support import build_default_state, normalize_trade_state


class StateServiceTests(unittest.TestCase):
    def test_order_submission_guard_survives_trade_state_normalization(self):
        kwargs = {
            "trend_universe": {"ETHUSDT"},
            "last_good_payload_key": "last_good_payload",
            "action_history_key": "action_history",
            "retired_positions_key": "retired_positions",
        }
        unknown = {
            "state": "SUBMISSION_UNKNOWN",
            "identity_sha256": "a" * 64,
            "symbol": "ETHUSDT",
        }

        self.assertEqual(build_default_state(**kwargs)["order_submission"], {"state": "RESERVED"})
        self.assertEqual(normalize_trade_state({"order_submission": unknown}, **kwargs)["order_submission"], unknown)

    def test_daily_trend_accounting_survives_state_normalization(self):
        kwargs = {
            "trend_universe": {"ETHUSDT": {"base_asset": "ETH"}},
            "last_good_payload_key": "last_good",
            "action_history_key": "trend_actions",
            "retired_positions_key": "retired",
        }
        raw = {
            "daily_trend_pnl_basis": "trend_mark_plus_cash_flow_v1",
            "daily_trend_cash_flow_usdt": 125.0,
            "daily_trend_net_invested_usdt": -125.0,
            "daily_trend_risk_base_usdt": 1000.0,
            "daily_trend_third_fee_usdt": 1.5,
        }

        normalized = normalize_trade_state(raw, **kwargs)

        for key, value in raw.items():
            self.assertEqual(normalized[key], value)

    def test_external_cash_flow_state_survives_normalization_without_resetting_daily_principal(self):
        kwargs = {
            "trend_universe": {},
            "last_good_payload_key": "last_good",
            "action_history_key": "trend_actions",
            "retired_positions_key": "retired",
        }
        cursor = {
            "version": 1,
            "observed_at": "2026-09-12T10:00:00+00:00",
            "records": {"a" * 64: {"kind": "deposit", "payload_sha256": "b" * 64, "status": "final"}},
        }

        normalized = normalize_trade_state(
            {"daily_external_principal_usdt": 25.0, "external_cash_flow_cursor": cursor},
            **kwargs,
        )

        self.assertEqual(normalized["daily_external_principal_usdt"], 25.0)
        self.assertEqual(normalized["external_cash_flow_cursor"], cursor)

    def test_incomplete_or_non_finite_daily_trend_accounting_is_marked_invalid(self):
        kwargs = {
            "trend_universe": {},
            "last_good_payload_key": "last_good",
            "action_history_key": "trend_actions",
            "retired_positions_key": "retired",
        }
        base = {
            "daily_trend_pnl_basis": "trend_mark_plus_cash_flow_v1",
            "daily_trend_cash_flow_usdt": -100.0,
            "daily_trend_net_invested_usdt": 100.0,
            "daily_trend_third_fee_usdt": 0.0,
        }
        for raw in (base, {**base, "daily_trend_risk_base_usdt": float("nan")}):
            with self.subTest(raw=raw):
                self.assertEqual(
                    normalize_trade_state(raw, **kwargs)["daily_trend_pnl_basis"],
                    "invalid_trend_accounting",
                )

    def test_load_cycle_state_marks_report_aborted_when_state_load_fails(self):
        report = {"status": "ok"}
        observed_errors = []

        result = load_cycle_state(
            SimpleNamespace(),
            report,
            allow_new_trend_entries_on_degraded=False,
            state_loader=lambda *, normalize: None,
            resolve_runtime_trend_pool=lambda *_args, **_kwargs: None,
            normalize_trade_state=lambda state: state,
            update_trend_pool_state=lambda *_args, **_kwargs: None,
            runtime_set_trade_state=lambda *_args, **_kwargs: None,
            get_runtime_trend_universe=lambda state: state,
            append_report_error=lambda report, message, stage: observed_errors.append((stage, message)),
            trend_universe_setter=lambda _value: None,
        )

        self.assertIsNone(result)
        self.assertEqual(report["status"], "aborted")
        self.assertEqual(len(observed_errors), 1)
        self.assertEqual(observed_errors[0][0], "state_load")

    def test_load_cycle_state_refreshes_runtime_state_metadata(self):
        runtime = SimpleNamespace(name="runtime")
        report = {"status": "ok", "gating_summary": {}, "gating_events": []}
        observed = {"trend_universe": None, "persist_reasons": []}
        raw_state = {"foo": "bar"}
        normalized_state = {"normalized": True}
        trend_pool_resolution = {"degraded": True, "source_kind": "last_known_good"}
        runtime_trend_universe = {"ETHUSDT": {"base_asset": "ETH"}}

        result = load_cycle_state(
            runtime,
            report,
            allow_new_trend_entries_on_degraded=True,
            state_loader=lambda *, normalize: raw_state,
            resolve_runtime_trend_pool=lambda _runtime, _raw_state: (runtime_trend_universe, trend_pool_resolution),
            normalize_trade_state=lambda state: normalized_state if state is raw_state else None,
            update_trend_pool_state=lambda state, resolution: state.update(resolution_seen=resolution["source_kind"]),
            runtime_set_trade_state=lambda _runtime, _report, state, reason: observed["persist_reasons"].append(
                (reason, dict(state))
            ),
            get_runtime_trend_universe=lambda state: runtime_trend_universe if state is normalized_state else None,
            append_report_error=lambda *_args, **_kwargs: None,
            trend_universe_setter=lambda value: observed.__setitem__("trend_universe", value),
        )

        self.assertEqual(observed["trend_universe"], runtime_trend_universe)
        self.assertEqual(observed["persist_reasons"][0][0], "trend_pool_metadata_refresh")
        self.assertEqual(observed["persist_reasons"][0][1]["resolution_seen"], "last_known_good")
        self.assertEqual(
            result,
            (normalized_state, trend_pool_resolution, runtime_trend_universe, True),
        )

    def test_load_cycle_state_records_degraded_buy_pause_gate(self):
        report = {"status": "ok", "gating_summary": {}, "gating_events": []}

        result = load_cycle_state(
            SimpleNamespace(),
            report,
            allow_new_trend_entries_on_degraded=False,
            state_loader=lambda *, normalize: {"ok": True},
            resolve_runtime_trend_pool=lambda *_args, **_kwargs: (
                {"ETHUSDT": {"base_asset": "ETH"}},
                {"degraded": True, "source_kind": "last_known_good", "source": "last_known_good"},
            ),
            normalize_trade_state=lambda state: state,
            update_trend_pool_state=lambda *_args, **_kwargs: None,
            runtime_set_trade_state=lambda *_args, **_kwargs: None,
            get_runtime_trend_universe=lambda state: {"ETHUSDT": {"base_asset": "ETH"}},
            append_report_error=lambda *_args, **_kwargs: None,
            trend_universe_setter=lambda _value: None,
        )

        self.assertFalse(result[3])
        self.assertEqual(report["gating_summary"]["trend_buy_paused_degraded_mode"], 1)
        self.assertEqual(report["gating_events"][0]["category"], "trend")

    def test_append_trend_pool_source_logs_appends_all_lines(self):
        log_buffer = []

        append_trend_pool_source_logs(
            log_buffer,
            {"source_kind": "fresh_upstream"},
            allow_new_trend_entries=False,
            formatter=lambda resolution, *, allow_new_trend_entries: [
                resolution["source_kind"],
                f"allow_new={allow_new_trend_entries}",
            ],
            append_log_fn=lambda target, message: target.append(message),
        )

        self.assertEqual(log_buffer, ["fresh_upstream", "allow_new=False"])


class StateStoreTests(unittest.TestCase):
    def test_accounting_rebase_marker_survives_normalize_then_save_payload(self):
        kwargs = {
            "trend_universe": {},
            "last_good_payload_key": "last_good",
            "action_history_key": "actions",
            "retired_positions_key": "retired",
        }
        marker = {
            "archive_document": "MULTI_ASSET_STATE__before_rebase_34601984051",
            "approved_proposal_run_id": "34601984051",
            "historical_difference_unresolved": True,
        }
        observed = {}
        normalized = normalize_trade_state({"accounting_rebase": marker}, **kwargs)

        save_runtime_trade_state(
            normalized,
            normalize_fn=lambda value: normalize_trade_state(value, **kwargs),
            saver_fn=lambda data, **_kwargs: observed.update(data=data) or True,
        )

        self.assertEqual(observed["data"]["accounting_rebase"], marker)

    def test_normalization_does_not_add_accounting_rebase_to_legacy_state(self):
        kwargs = {
            "trend_universe": {},
            "last_good_payload_key": "last_good",
            "action_history_key": "actions",
            "retired_positions_key": "retired",
        }

        self.assertNotIn("accounting_rebase", normalize_trade_state({}, **kwargs))

    def test_load_runtime_trade_state_uses_default_collection_document(self):
        observed = {}

        result = load_runtime_trade_state(
            normalize_fn=lambda value: value,
            default_state_factory=dict,
            loader_fn=lambda **kwargs: observed.update(kwargs) or {"ok": True},
        )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(observed["collection"], "strategy")
        self.assertEqual(observed["document"], "MULTI_ASSET_STATE")
        self.assertTrue(observed["normalize"])

    def test_save_runtime_trade_state_uses_default_collection_document(self):
        observed = {}

        save_runtime_trade_state(
            {"ok": True},
            normalize_fn=lambda value: value,
            saver_fn=lambda data, **kwargs: observed.update({"data": data, **kwargs}),
        )

        self.assertEqual(observed["data"], {"ok": True})
        self.assertEqual(observed["collection"], "strategy")
        self.assertEqual(observed["document"], "MULTI_ASSET_STATE")

    def test_save_runtime_trade_state_propagates_persistence_failure(self):
        result = save_runtime_trade_state(
            {"ok": True},
            normalize_fn=lambda value: value,
            saver_fn=lambda *_args, **_kwargs: False,
        )

        self.assertIs(result, False)


if __name__ == "__main__":
    unittest.main()
